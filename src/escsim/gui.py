#!/usr/bin/env python3
"""
Renode ESC lab: pick a hardware target, a bootloader and a firmware,
press Start, and get an emulated ESC a configurator can talk to.

The emulator runs gen_target.py TARGET --link, serving the SITL wire
protocols. The configurator port is served by ESCSim on those ports,
on a pty or - so a browser can reach it - on a virtual USB serial
device attached through vhci_hcd. The protocol choice picks what sits
on that port: the fake flight controller (MSP with BLHeli 4-way
passthrough to the emulated bootloader, as a real FC provides) or a
direct single-wire adapter (the raw bootloader protocol with the
adapter's self-echo, as a USB linker soldered to the signal pad
provides). This is the rig for developing am32.tridgell.net against
emulated CAN and non-CAN ESCs with no hardware.

Bootloaders are matched to the target automatically: the image must be
built for the target's signal pin (a PA2 bootloader on a PB4 target
answers nothing), so the list only offers AM32_<MCU>_BOOTLOADER_<PIN>*
builds from configured search paths and the artifact catalog.

The Renode monitor is served on a telnet port and polled once a
second, so the status panel shows the live PC (labelled when it is
executing inside the bootloader), the emulation speed against real
time, the retired instruction rate and the machine's virtual time.

with --control-port N the UI can be driven over a localhost TCP
connection (one command per line), for scripted tests:
  target NAME, bootloader auto|none|PATH, firmware auto|none|PATH,
  eeprom defaults|blank, conf off|serial|usb, protocol 4way|direct,
  escs 1..8, canbus N, download-renode, start, stop, status, quit
replies are prefixed OK/ERR/STATUS.
"""

import argparse
from dataclasses import replace
from importlib import resources
import glob
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

from pathlib import Path
from types import SimpleNamespace

from escsim.control import msp_stub_fc
from escsim.control import ui as sitl_gui
from escsim.control import usbip as sitl_usbip
from escsim.control.backend import CanCommandGroup
from escsim.artifacts.catalog import ArtifactRepository
from escsim.renode import download as renode_download
from escsim.renode import monitor as renode_monitor
from escsim.renode.generator import (
    Unsupported,
    all_targets as available_targets,
    config,
)
from escsim.renode.process import ProcessTree
from escsim.renode.session import generator_command, generator_environment
from escsim.settings import LauncherSettings, SettingsStore, default_cache_dir
from escsim.settings import TargetSourceSpec
from escsim.target.source import TargetSourceManager

REPO = os.fspath(Path.home())

# the bootloader family names as its obj files spell them
FAMILY_MCU = {
    "f051": "F051",
    "f031": "F031",
    "e230": "E230",
    "f415": "F415",
    "f421": "F421",
    "g071": "G071",
    "g431": "G431",
    "l431": "L431",
    "v203": "V203",
    "a153": "A153",
}

SITL_MAGIC = 0x4453
STATE_MAGIC = 0x5353
MAX_ESC_COUNT = 8
INSTANCE_PORT_STRIDE = 10


METRICS_COMMAND = (
    "cpu PC; cpu PerformanceInMips; cpu ExecutedInstructions; "
    "emulation GetTimeSourceInfo"
)


def bootloader_dirs(explicit=None):
    """where bootloader ELFs might live: an explicit dir, the env, then
    the bootloader repo checked out next to this one"""
    cands = []
    if explicit:
        cands.append(explicit)
    env = os.environ.get("AM32_BOOTLOADER_OBJ")
    if env:
        cands.append(env)
    parent = os.path.dirname(REPO)
    for name in ("AM32-bootloader", "am32-bootloader"):
        cands.append(os.path.join(parent, name, "obj"))
    return [d for d in cands if os.path.isdir(d)]


def find_bootloaders(family, pin, dirs, dronecan=False):
    """bootloader images built for this target's MCU and signal pin.

    Ordered so the first entry is the right default: the CAN build for a
    DroneCAN target, the plain default-flash build otherwise. Loading a
    CAN (128K) bootloader on a 64K non-CAN target answers the wire but
    puts the eeprom where neither the firmware nor the configurator
    expects it, so the ordering is load-bearing.
    """
    mcu = FAMILY_MCU.get(family)
    if mcu is None:
        return []
    hits = []
    for d in dirs:
        for ext in ("elf", "hex"):
            pat = os.path.join(d, "AM32_%s_BOOTLOADER_%s*_V*.%s" % (mcu, pin, ext))
            hits += glob.glob(pat)
    # newest version of each variant only, an ELF (which carries the
    # symbols and debug info) beating the hex built beside it
    byvar = {}
    for h in sorted(hits, key=lambda h: (h[:-4], h.endswith(".elf"))):
        var = re.sub(r"_V\d+\.(elf|hex)$", "", os.path.basename(h))
        byvar[var] = h

    def rank(path):
        name = os.path.basename(path)
        is_can = "_CAN_" in name
        is_sized = re.search(r"_\d+K_", name) is not None
        if dronecan:
            return (0 if is_can else 1, name)
        # plain default-flash first, size variants next, CAN last
        return ((2 if is_can else (1 if is_sized else 0)), name)

    return sorted(byvar.values(), key=rank)


class ProcRunner(object):
    """a child process whose output lines land in a queue, killed as a
    group so renode dies with its launcher"""

    def __init__(self, out_q, label=""):
        self.out_q = out_q
        self.label = label
        self.proc = None
        self.tree = None
        self.lock = threading.RLock()

    def start(self, cmd, cwd=None, env=None):
        # stdin must be a pipe we hold open: renode's console exits on
        # EOF, so inheriting a nohup'd or exhausted stdin kills the
        # emulator moments after it starts
        with self.lock:
            self.tree = ProcessTree(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=env,
            )
            self.proc = self.tree.process
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            line = line.rstrip("\n")
            self.out_q.put("%s%s" % (self.label, line))
        self.out_q.put("%s[emulator exited, status %s]" % (self.label, proc.wait()))

    def running(self):
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def stop(self):
        with self.lock:
            if self.proc is None:
                return
            self.tree.stop()
            self.tree = None
            self.proc = None


class ProcGroup(object):
    """One logical launch made up of one or more Renode process trees."""

    def __init__(self, out_q):
        self.out_q = out_q
        self.runners = []
        self.lock = threading.RLock()

    def start(self, commands, cwd=None, env=None):
        with self.lock:
            count = len(commands)
            try:
                for index, command in enumerate(commands):
                    label = "[ESC %u] " % (index + 1) if count > 1 else ""
                    runner = ProcRunner(self.out_q, label=label)
                    self.runners.append(runner)
                    runner.start(command, cwd=cwd, env=env)
            except BaseException:
                self.stop()
                raise

    def running(self):
        with self.lock:
            return any(runner.running() for runner in self.runners)

    def all_running(self):
        with self.lock:
            return bool(self.runners) and all(
                runner.running() for runner in self.runners
            )

    def stop(self):
        with self.lock:
            for runner in self.runners:
                runner.stop()
            self.runners = []


class Lab(object):
    """the launcher's state and actions, UI-independent so the control
    port drives exactly what the buttons do"""

    def __init__(self, args):
        self.args = args
        self.log_q = queue.Queue()
        self.runner = ProcGroup(self.log_q)
        self.emulator_ready = False
        self.stub = None
        self.usb_attached = False
        self.usb_port = None
        self.usb_ports = set()
        self.lifecycle_lock = threading.RLock()
        self.usb_cleanup_lock = threading.Lock()
        self.usb_starting = set()
        self.target = None
        self.info = None  # {'family','pin','dronecan'}
        self.bootloader = "auto"  # auto | none | path
        self.firmware = "auto"  # auto | none | path
        self.targets_header = None
        self.eeprom = "defaults"  # defaults | blank
        self.metrics = None  # latest monitor sample
        self.generation = 0  # invalidates old pollers
        self.conf = "usb" if os.name == "nt" else "serial"
        self.protocol = "4way"  # 4way | direct
        self.esc_count = 1
        self.can_bus = 0
        self.status = "stopped"
        self.conf_port = ""  # the pty / tty path once up
        self.launch_work = None
        self.bl_dirs = bootloader_dirs(args.bootloader_dir)
        cached_bootloaders = default_cache_dir() / "bootloaders"
        if cached_bootloaders.is_dir():
            self.bl_dirs.append(os.fspath(cached_bootloaders))

    def log(self, msg):
        self.log_q.put(msg)

    # -- queries -------------------------------------------------------

    def target_info(self, target):
        """family/pin/dronecan for a target from the active targets.h."""
        try:
            cfg = config(target)
        except Unsupported as error:
            self.log("target %s: %s" % (target, error))
            return None
        return {
            "target": target,
            "family": cfg["family"],
            "pin": cfg["throttle_pin"],
            "dronecan": cfg["dronecan"],
            "app_base": cfg["app_base"],
        }

    def matched_bootloaders(self):
        if self.info is None:
            return []
        return find_bootloaders(
            self.info["family"], self.info["pin"], self.bl_dirs, self.info["dronecan"]
        )

    def pick_bootloader(self):
        """the ELF to load, or None for app-only, or an error string"""
        if self.bootloader == "none":
            return None
        if self.bootloader != "auto":
            if not os.path.isfile(self.bootloader):
                return "no bootloader at %s" % self.bootloader
            return self.bootloader
        hits = self.matched_bootloaders()
        if not hits:
            return (
                "no AM32_%s_BOOTLOADER_%s* ELF found; build one in the "
                "bootloader repo or Browse to it"
                % (FAMILY_MCU.get(self.info["family"], "?"), self.info["pin"])
            )
        return hits[0]

    # -- lifecycle -----------------------------------------------------

    def active_esc_count(self):
        if getattr(self, "protocol", "4way") != "4way":
            return 1
        return getattr(self, "esc_count", 1)

    def instance_ports(self, index):
        """Signal, state and monitor ports for one Renode instance."""
        offset = index * INSTANCE_PORT_STRIDE
        return (
            self.args.gui_port + offset,
            self.args.state_port + offset,
            getattr(self.args, "monitor_port", 0) + offset,
        )

    def _all_emulators_running(self):
        all_running = getattr(self.runner, "all_running", None)
        return all_running() if all_running is not None else self.runner.running()

    @staticmethod
    def wait_port_free(port, timeout=8.0, tcp=False):
        """wait for a port to be bindable; the previous emulator's
        teardown can outlive the Stop click by a moment"""
        deadline = time.time() + timeout
        while True:
            s = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
            )
            try:
                if tcp:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.3)
            finally:
                s.close()

    def emulator_ports_bound(self):
        """Return true once every Renode instance owns both GUI UDP ports."""
        return all(
            not self.wait_port_free(port, timeout=0)
            for index in range(self.active_esc_count())
            for port in self.instance_ports(index)[:2]
        )

    def start(self):
        if self.runner.running():
            return "already running"
        with self.lifecycle_lock:
            if self.usb_starting:
                return "previous USB startup is still being cancelled"
        # A failed detach remains owned so a later Start can retry it instead
        # of silently adding another virtual USB device.
        cleanup_error = self._stop_stub()
        if cleanup_error is not None:
            return "previous USB cleanup failed: %s" % cleanup_error
        if self.target is None or self.info is None:
            return "pick a target first"
        if not 1 <= self.esc_count <= MAX_ESC_COUNT:
            return "ESC count must be 1..%u" % MAX_ESC_COUNT
        instance_ports = [
            self.instance_ports(index) for index in range(self.active_esc_count())
        ]
        flat_ports = [port for ports in instance_ports for port in ports]
        if any(not 1 <= port <= 65535 for port in flat_ports):
            return "instance ports must be 1..65535"
        if len(flat_ports) != len(set(flat_ports)):
            return "instance port ranges overlap; choose different base ports"
        for port in [port for ports in instance_ports for port in ports[:2]]:
            if not self.wait_port_free(port):
                return (
                    "udp port %u is still in use - a leftover emulator? "
                    "try: pkill -f renode" % port
                )
        for port in [ports[2] for ports in instance_ports]:
            if not self.wait_port_free(port, tcp=True):
                return (
                    "monitor port %u is still in use - a leftover emulator? "
                    "try: pkill -f renode" % port
                )
        bl = self.pick_bootloader()
        if isinstance(bl, str) and not os.path.isfile(bl):
            return bl
        command_tail = []
        if bl is not None:
            command_tail += ["--bootloader-elf", bl]
        if self.firmware == "none":
            if bl is None:
                return (
                    "a blank ESC still needs its bootloader: pick one, "
                    "or pick a firmware"
                )
            command_tail += ["--no-firmware"]
        elif self.firmware != "auto":
            if not os.path.isfile(self.firmware):
                return "no firmware at %s" % self.firmware
            command_tail += ["--elf", self.firmware]
        if self.targets_header is not None:
            command_tail += ["--targets-file", self.targets_header]
        if self.eeprom == "blank":
            command_tail += ["--blank-eeprom"]
        if self.info["dronecan"]:
            command_tail += ["--can-bus", str(self.can_bus)]
        if self.args.renode:
            command_tail += ["--renode", self.args.renode]
        if self.launch_work is not None:
            self.launch_work.cleanup()
        self.launch_work = tempfile.TemporaryDirectory(prefix="escsim-launch-")
        commands = []
        for index, (gui_port, state_port, monitor_port) in enumerate(instance_ports):
            outdir = os.path.join(self.launch_work.name, "esc%u" % (index + 1))
            command = generator_command() + [
                self.target,
                "--link",
                "--outdir",
                outdir,
                "--gui-port",
                str(gui_port),
                "--gui-state-port",
                str(state_port),
                "--monitor-port",
                str(monitor_port),
            ]
            if self.info["dronecan"]:
                command += [
                    "--can-node",
                    str(11 + index),
                    "--esc-index",
                    str(index),
                ]
            commands.append(command + command_tail)
        self.emulator_ready = False
        self.start_failed = False
        self.conf_port = ""
        count = self.active_esc_count()
        self.status = "starting %s..." % (
            "emulator" if count == 1 else "%u emulators" % count
        )
        for command in commands:
            self.log("$ " + " ".join(command))
        try:
            self.runner.start(commands, env=generator_environment())
        except Exception as error:
            self.launch_work.cleanup()
            self.launch_work = None
            return "could not start emulator: %s" % error
        with self.lifecycle_lock:
            self.generation += 1
            generation = self.generation
        threading.Thread(
            target=self._wait_ready, args=(generation, bl), daemon=True
        ).start()
        return None

    def _generation_current(self, generation):
        with self.lifecycle_lock:
            return generation == self.generation

    def _wait_ready(self, generation, bl):
        """watch for the emulator's input port, then bring up the
        configurator side"""
        deadline = time.time() + 120
        # With a telnet monitor Renode sends command/setup failures only to
        # that socket, not stdout. Consume the initial prompt so a bad image or
        # platform becomes an immediate launcher error instead of a two-minute
        # wait at the last ordinary log line.
        monitors = []
        for index in range(self.active_esc_count()):
            monitor = renode_monitor.MonitorClient(
                "127.0.0.1", self.instance_ports(index)[2]
            )
            text = None
            while (
                time.time() < deadline
                and self._all_emulators_running()
                and self._generation_current(generation)
            ):
                try:
                    text = monitor.connect(timeout=max(1, deadline - time.time()))
                    break
                except OSError:
                    monitor.close()
                    time.sleep(0.2)
                except TimeoutError:
                    break
            monitors.append(monitor)
            if text is not None:
                error = renode_monitor.startup_error(text)
                if error is not None:
                    self.status = "ESC %u setup failed: %s" % (index + 1, error)
                    self.start_failed = True
                    self.log("[monitor ESC %u] %s" % (index + 1, error))
                    break
        if not self._generation_current(generation):
            for monitor in monitors:
                monitor.close()
            return
        if self.start_failed:
            for monitor in monitors:
                monitor.close()
            if self._generation_current(generation):
                self.runner.stop()
            return
        while (
            time.time() < deadline
            and self._all_emulators_running()
            and self._generation_current(generation)
        ):
            if self.emulator_ports_bound():
                self.emulator_ready = True
                break
            if self.start_failed:
                break
            time.sleep(0.3)
        if not self.emulator_ready:
            for monitor in monitors:
                monitor.close()
            if not self._generation_current(generation):
                return
            # a half-started emulator (a failed port bind still leaves
            # the machine running) must not linger and block the retry
            self.runner.stop()
            if not self.status.startswith("emulator exited"):
                self.status = "emulator did not come up"
            return
        threading.Thread(
            target=self._metrics_loop,
            args=(generation, monitors[0]),
            daemon=True,
        ).start()
        for monitor in monitors[1:]:
            monitor.close()
        if bl is not None:
            self._enter_bootloader()
        if not self._generation_current(generation):
            return
        if self.conf == "off":
            self.status = "running (no configurator port)"
            return
        try:
            self._start_stub(generation)
        except Exception as ex:
            if self._generation_current(generation):
                self._stop_stub()
                self.status = "configurator port failed: %s" % ex
                self.log(self.status)

    def _enter_bootloader(self):
        """hold the signal wire high and reset, so the ESC is parked in
        the bootloader before the first configurator connect"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for index in range(self.active_esc_count()):
                gui_port, _state_port, _monitor_port = self.instance_ports(index)
                # type 5 line level, idle high
                s.sendto(
                    struct.pack("<HBBHH", SITL_MAGIC, 5, 4, 1, 0),
                    ("127.0.0.1", gui_port),
                )
            time.sleep(0.3)
            for index in range(self.active_esc_count()):
                _gui_port, state_port, _monitor_port = self.instance_ports(index)
                s.sendto(
                    struct.pack("<HBB", STATE_MAGIC, 9, 0),
                    ("127.0.0.1", state_port),
                )
        finally:
            s.close()
        self.log("holding the signal wire; ESC reset into the bootloader")

    def _metrics_loop(self, generation, client=None):
        """poll the Renode monitor for PC and timing, and derive the
        realtime speedup from virtual-vs-wall deltas over a sliding
        window (Renode advances in bursts, so an instant ratio just
        flaps around the true speed)"""
        client = client or renode_monitor.MonitorClient(
            "127.0.0.1", self.args.monitor_port
        )
        history = []
        try:
            if client.socket is None:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if (
                        generation != self.generation
                        or not self._all_emulators_running()
                    ):
                        return
                    try:
                        client.connect()
                        break
                    except (OSError, TimeoutError):
                        client.close()
                        time.sleep(0.5)
                else:
                    self.log_q.put(
                        (
                            "__monitor_error__",
                            generation,
                            "monitor did not become ready",
                        )
                    )
                    return
            while generation == self.generation and self._all_emulators_running():
                try:
                    current = renode_monitor.parse_metrics(
                        client.command(
                            METRICS_COMMAND, timeout=60 if not history else 5
                        )
                    )
                except (OSError, TimeoutError, ValueError) as error:
                    self.log_q.put(("__monitor_error__", generation, str(error)))
                    return
                current["wall_seconds"] = time.monotonic()
                history.append(current)
                while (
                    len(history) > 2
                    and current["wall_seconds"] - history[1]["wall_seconds"] >= 8
                ):
                    history.pop(0)
                if len(history) > 1:
                    base = history[0]
                    wall = current["wall_seconds"] - base["wall_seconds"]
                    virtual = current["virtual_seconds"] - base["virtual_seconds"]
                    executed = current["instructions"] - base["instructions"]
                    if wall > 0 and virtual >= 0:
                        current["speedup"] = virtual / wall
                        current["executed_mips"] = executed / wall / 1e6
                self.log_q.put(("__metrics__", generation, current))
                time.sleep(1)
        finally:
            client.close()

    def format_metrics(self):
        """one status line: PC (with the flash region it is executing
        from), realtime speedup, executed MIPS and virtual time"""
        m = self.metrics
        if m is None:
            return ""
        where = ""
        app_base = (self.info or {}).get("app_base")
        if app_base:
            flash_base = 0x08000000 if app_base >= 0x08000000 else 0
            if flash_base <= m["pc"] < app_base:
                where = " (bootloader)"
        parts = ["PC 0x%08X%s" % (m["pc"], where)]
        if m.get("speedup") is not None:
            parts.append("%.2fx realtime" % m["speedup"])
        if m.get("executed_mips") is not None:
            parts.append("%.0f of %u MIPS" % (m["executed_mips"], m["mips"]))
        parts.append("vt %.1fs" % m["virtual_seconds"])
        return " | ".join(parts)

    def _start_stub(self, generation):
        if self.conf == "usb":
            with self.lifecycle_lock:
                self.usb_starting.add(generation)
        endpoint = None
        stub = None
        attached = False
        usb_port = None
        published = False
        try:
            if self.conf == "usb":
                # in direct mode the USB ids make the web configurator
                # treat the port as a single-wire adapter, not an FC
                ids = (
                    {
                        "vid": msp_stub_fc.DIRECT_VENDOR_ID,
                        "pid": msp_stub_fc.DIRECT_PRODUCT_ID,
                    }
                    if self.protocol == "direct"
                    else {}
                )
                endpoint = sitl_usbip.UsbipServer(
                    unix_path=(
                        None
                        if os.name == "nt"
                        else "@am32-renode-usbip.%u.%u" % (os.getuid(), os.getpid())
                    ),
                    serial="RENODE",
                    **ids,
                )
            if self.protocol == "direct":
                stub = msp_stub_fc.DirectBridge(
                    sitl_port=self.args.gui_port, endpoint=endpoint, verbose=False
                )
            else:
                stub = msp_stub_fc.MspStubFC(
                    sitl_port=self.args.gui_port,
                    state_port=self.args.state_port,
                    esc_ports=[
                        self.instance_ports(index)[0]
                        for index in range(self.active_esc_count())
                    ],
                    state_ports=[
                        self.instance_ports(index)[1]
                        for index in range(self.active_esc_count())
                    ],
                    motor=True,
                    endpoint=endpoint,
                    verbose=False,
                )
            if self.conf == "usb":
                attached = sitl_usbip.attach(
                    unix_path=endpoint.unix_path,
                    host=endpoint.host,
                    port=endpoint.port,
                )
                if attached is None or attached is False:
                    raise RuntimeError(
                        "USB/IP virtual-host-controller attach was refused"
                    )
                usb_port = attached
                self._remember_usb(usb_port)
                tty = sitl_usbip.find_tty(
                    "RENODE", timeout=10, vid=endpoint.vid, pid=endpoint.pid
                )
                if tty is None:
                    raise RuntimeError("attached but no tty appeared")
                conf_port = tty
            else:
                conf_port = stub.slave_path
            with self.lifecycle_lock:
                if generation != self.generation or not self._all_emulators_running():
                    return
                self.stub = stub
                self.conf_port = conf_port
                self.status = "running - configurator port: %s" % self.conf_port
                published = True
            self.log(self.status)
        finally:
            try:
                if not published:
                    if attached:
                        error = self._detach_owned_usb(usb_port)
                        if error is not None:
                            self.log(
                                "USB/IP detach failed after cancelled start: %s" % error
                            )
                    if stub is not None:
                        stub.close()
                    elif endpoint is not None:
                        endpoint.close()
            finally:
                if self.conf == "usb":
                    with self.lifecycle_lock:
                        self.usb_starting.discard(generation)

    def _remember_usb(self, port):
        with self.lifecycle_lock:
            self.usb_ports.add(port)
            self.usb_attached = True
            self.usb_port = port

    def _forget_usb(self, port):
        with self.lifecycle_lock:
            self.usb_ports.discard(port)
            self.usb_attached = bool(self.usb_ports)
            self.usb_port = next(iter(self.usb_ports), None)

    def _detach_owned_usb(self, port):
        with self.usb_cleanup_lock:
            with self.lifecycle_lock:
                if port not in self.usb_ports:
                    return None
            try:
                if not sitl_usbip.detach(port):
                    raise RuntimeError("detach was refused")
            except Exception as error:
                return error
            self._forget_usb(port)
            return None

    def _stop_stub(self):
        with self.lifecycle_lock:
            stub = self.stub
            self.stub = None
            usb_ports = list(self.usb_ports)
        cleanup_error = None
        for usb_port in usb_ports:
            error = self._detach_owned_usb(usb_port)
            if error is not None:
                self.log("USB/IP detach failed: %s" % error)
                cleanup_error = error
        if stub is not None:
            stub.close()
        return cleanup_error

    def stop(self):
        with self.lifecycle_lock:
            self.generation += 1
        self.metrics = None
        cleanup_error = self._stop_stub()
        self.runner.stop()
        if self.launch_work is not None:
            self.launch_work.cleanup()
            self.launch_work = None
        self.emulator_ready = False
        self.conf_port = ""
        self.status = (
            "stopped; USB cleanup failed: %s" % cleanup_error
            if cleanup_error is not None
            else "stopped"
        )

    def saw_log_line(self, line):
        if "input port on udp" in line:
            self.emulator_ready = self.emulator_ports_bound()
        if "could not bind the input port" in line:
            # the machine keeps running without its ports; fail the
            # start promptly rather than waiting out the ready timeout
            self.status = "emulator exited: " + line.strip()
            self.start_failed = True
        if "[emulator exited" in line:
            # Renode may exit without a Stop click. Detach immediately rather
            # than leaving the virtual serial device present until app exit.
            if not self._all_emulators_running():
                with self.lifecycle_lock:
                    self.generation += 1
                cleanup_error = self._stop_stub()
                if cleanup_error is not None:
                    self.status = (
                        "emulator exited; USB cleanup failed: %s" % cleanup_error
                    )
                self.runner.stop()
            self.emulator_ready = False
            if self.status.startswith(("running", "starting")):
                exit_at = line.find("[emulator exited")
                self.status = line[exit_at + 1 : -1]


def run_control_server(lab, port, on_command):
    """the scripted-test interface; on_command marshals a closure onto
    the UI thread and returns its reply"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)

    def client(conn):
        f = conn.makefile("rw")
        try:
            for line in f:
                reply = on_command(line.strip())
                f.write(reply + "\n")
                f.flush()
        except OSError:
            pass
        finally:
            conn.close()

    def loop():
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=client, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--gui-port",
        type=int,
        default=57833,
        help="emulator input port (default off the SITL's 57733, so both can run)",
    )
    ap.add_argument("--state-port", type=int, default=57834)
    ap.add_argument(
        "--monitor-port",
        type=int,
        default=57835,
        help="Renode telnet monitor port, polled for the live PC / speedup display",
    )
    ap.add_argument(
        "--bootloader-dir",
        default=None,
        help="directory of bootloader ELFs (default: the "
        "bootloader repo next to this one, or "
        "$AM32_BOOTLOADER_OBJ)",
    )
    ap.add_argument(
        "--renode", default=None, help="renode binary, passed through to gen_target"
    )
    ap.add_argument(
        "--renode-cache", help="download cache (default: ~/.cache/ardupilot/renode)"
    )
    ap.add_argument(
        "--control-port",
        type=int,
        default=0,
        help="TCP port for scripted UI control (default off)",
    )
    args = ap.parse_args(argv)

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QFileDialog,
        QGridLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    renode_cache = (
        Path(args.renode_cache).expanduser()
        if args.renode_cache
        else renode_download.default_cache()
    )

    app = QApplication([sys.argv[0]])
    icon_data = resources.files("escsim").joinpath("resources", "escsim.png")
    icon_pixmap = QPixmap()
    icon_pixmap.loadFromData(icon_data.read_bytes(), "PNG")
    app.setWindowIcon(QIcon(icon_pixmap))
    lab = Lab(args)
    settings_store = SettingsStore()
    preferences = settings_store.load().launcher
    lab.bootloader = preferences.bootloader
    artifact_repository = ArtifactRepository()
    artifact_catalog = [None]

    class LauncherWindow(QWidget):
        def closeEvent(self, event):
            # Embedded control-panel helpers can keep Qt objects alive after
            # the only visible window closes. End the application explicitly
            # so the finally block tears down Renode and releases its ports.
            event.accept()
            app.quit()

    win = LauncherWindow()
    win.setWindowIcon(app.windowIcon())
    win.setWindowTitle("ESCSim - AM32 ESC simulator")
    outer = QVBoxLayout(win)
    tabs = QTabWidget()
    outer.addWidget(tabs)
    target_page = QWidget()
    tabs.addTab(target_page, "Target")
    grid = QGridLayout(target_page)

    control_page = QWidget()
    control_pages = [control_page]
    control_placeholder = QVBoxLayout(control_page)
    control_placeholder.addWidget(
        QLabel("Select a target to load its Renode controls.")
    )
    control_placeholder.addStretch(1)
    tabs.addTab(control_page, "Control")

    # -- target --------------------------------------------------------
    grid.addWidget(QLabel("Target"), 0, 0)
    target_filter = QLineEdit()
    target_filter.setPlaceholderText("filter...")
    grid.addWidget(target_filter, 0, 1)
    target_combo = QComboBox()
    grid.addWidget(target_combo, 0, 2, 1, 2)
    info_label = QLabel("pick a target")
    grid.addWidget(info_label, 1, 2, 1, 2)

    source_url_btn = QPushButton("Targets URL...")
    source_file_btn = QPushButton("Browse targets.h...")
    grid.addWidget(source_url_btn, 1, 0)
    grid.addWidget(source_file_btn, 1, 1)
    source_location = settings_store.load().targets_source.location
    source_url_btn.setToolTip("Current targets.h source: %s" % source_location)
    source_file_btn.setToolTip("Current targets.h source: %s" % source_location)

    target_names = []
    initial_target = [preferences.target]

    def load_targets():
        return available_targets()

    def apply_filter():
        pat = target_filter.text().strip().upper()
        target_combo.blockSignals(True)
        target_combo.clear()
        target_combo.addItems([t for t in target_names if pat in t])
        target_combo.blockSignals(False)
        if target_combo.count():
            preferred = target_combo.findText(initial_target[0])
            target_combo.setCurrentIndex(preferred if preferred >= 0 else 0)
            initial_target[0] = ""
            target_changed()

    def target_changed():
        t = target_combo.currentText()
        if not t:
            return
        if t != lab.target and (
            lab.runner.running() or lab.stub is not None or lab.usb_attached
        ):
            # A target selection replaces the emulated ESC. Tear down its
            # configurator endpoint immediately, before resolving the new
            # target, so the host never sees both USB devices at once.
            do_stop()
        info_label.setText("resolving %s..." % t)

        def resolve():
            info = lab.target_info(t)
            lab.log_q.put(("__target__", t, info))

        threading.Thread(target=resolve, daemon=True).start()

    target_filter.textChanged.connect(apply_filter)
    target_combo.currentIndexChanged.connect(lambda _i: target_changed())

    def select_targets_source(source):
        source_url_btn.setEnabled(False)
        source_file_btn.setEnabled(False)
        info_label.setText("fetching targets.h...")

        def worker():
            try:
                document = TargetSourceManager(settings_store=settings_store).select(
                    source
                )
                lab.log_q.put(("__targets_source__", document))
            except Exception as error:
                lab.log_q.put(("__targets_source_error__", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def choose_targets_url():
        current = settings_store.load().targets_source.location
        url, accepted = QInputDialog.getText(
            win, "targets.h URL", "Download targets.h from:", text=current
        )
        if accepted and url.strip():
            select_targets_source(TargetSourceSpec("url", url.strip()))

    def choose_targets_file():
        path, _ = QFileDialog.getOpenFileName(
            win, "Select targets.h", REPO, "C headers (*.h);;All files (*)"
        )
        if path:
            select_targets_source(TargetSourceSpec("file", path))

    source_url_btn.clicked.connect(choose_targets_url)
    source_file_btn.clicked.connect(choose_targets_file)

    # -- bootloader ----------------------------------------------------
    grid.addWidget(QLabel("Bootloader"), 2, 0)
    bl_combo = QComboBox()
    grid.addWidget(bl_combo, 2, 1, 1, 2)
    bl_browse = QPushButton("Browse...")
    grid.addWidget(bl_browse, 2, 3)

    def refresh_bootloaders():
        desired = lab.bootloader
        bl_combo.blockSignals(True)
        bl_combo.clear()
        hits = lab.matched_bootloaders()
        bl_combo.addItem("Auto (published stable or local match)", "auto")
        catalog = artifact_catalog[0]
        if catalog is not None:
            for release in catalog["releases"]["bootloader"]:
                bl_combo.addItem(
                    "Published V%s (%s)" % (release["id"], release["channel"]),
                    "catalog:bootloader:%s" % release["id"],
                )
        for h in hits:
            bl_combo.addItem(os.path.basename(h), h)
        bl_combo.addItem("None (boot straight into the app)", "none")
        selected = bl_combo.findData(desired)
        if desired == "auto" and catalog is not None:
            selected = -1
        if (
            selected < 0
            and desired not in ("auto", "none")
            and not desired.startswith("catalog:")
        ):
            bl_combo.insertItem(0, os.path.basename(desired), desired)
            selected = 0
        if selected >= 0:
            bl_combo.setCurrentIndex(selected)
        elif catalog is not None:
            stable = catalog.get("channels", {}).get("stable", {}).get("bootloader")
            published = bl_combo.findData("catalog:bootloader:%s" % stable)
            bl_combo.setCurrentIndex(published if published >= 0 else 0)
        elif not hits:
            bl_combo.setCurrentIndex(0)
        bl_combo.blockSignals(False)
        if not (desired.startswith("catalog:") and catalog is None):
            bl_changed()

    def bl_changed():
        data = bl_combo.currentData()
        lab.bootloader = data if data else "auto"

    bl_combo.currentIndexChanged.connect(lambda _i: bl_changed())

    def browse_bl():
        path, _ = QFileDialog.getOpenFileName(
            win,
            "Bootloader image",
            lab.bl_dirs[0] if lab.bl_dirs else REPO,
            "Bootloader (*.elf *.hex *.bin);;All files (*)",
        )
        if path:
            bl_combo.insertItem(0, os.path.basename(path), path)
            bl_combo.setCurrentIndex(0)

    bl_browse.clicked.connect(browse_bl)

    # -- firmware / can ------------------------------------------------
    grid.addWidget(QLabel("Firmware"), 3, 0)
    fw_combo = QComboBox()
    fw_combo.addItem("Auto (newest obj/AM32_<TARGET>_*.elf)", "auto")
    fw_combo.addItem("None (blank flash, factory-fresh ESC)", "none")
    if preferences.firmware.startswith("catalog:"):
        fw_combo.setCurrentIndex(fw_combo.findData("auto"))
    elif preferences.firmware not in ("auto", "none"):
        fw_combo.insertItem(
            0, os.path.basename(preferences.firmware), preferences.firmware
        )
        fw_combo.setCurrentIndex(0)
    else:
        fw_combo.setCurrentIndex(fw_combo.findData(preferences.firmware))
    fw_combo.setToolTip(
        "None gives a part with only the bootloader: everything else\n"
        "reads erased 0xFF, as an ESC fresh from the factory does."
    )
    grid.addWidget(fw_combo, 3, 1, 1, 2)
    fw_browse = QPushButton("Browse...")
    grid.addWidget(fw_browse, 3, 3)

    def browse_fw():
        path, _ = QFileDialog.getOpenFileName(
            win,
            "Firmware image",
            os.path.join(REPO, "obj"),
            "Firmware (*.elf *.hex *.bin);;All files (*)",
        )
        if path:
            fw_combo.insertItem(0, os.path.basename(path), path)
            fw_combo.setCurrentIndex(0)

    fw_browse.clicked.connect(browse_fw)

    def refresh_firmware_releases():
        for index in reversed(range(fw_combo.count())):
            data = fw_combo.itemData(index)
            if isinstance(data, str) and data.startswith("catalog:firmware:"):
                fw_combo.removeItem(index)
        catalog = artifact_catalog[0]
        if catalog is None or not lab.target:
            return
        for release in catalog["releases"]["firmware"]:
            if lab.target in release["targets"]:
                fw_combo.addItem(
                    "Published %s (%s)" % (release["id"], release["channel"]),
                    "catalog:firmware:%s" % release["id"],
                )
        desired = preferences.firmware
        if desired == "auto":
            stable = catalog.get("channels", {}).get("stable", {}).get("firmware")
            desired = "catalog:firmware:%s" % stable
        index = fw_combo.findData(desired)
        if index >= 0:
            fw_combo.setCurrentIndex(index)

    grid.addWidget(QLabel("CAN bus"), 4, 0)
    can_spin = QSpinBox()
    can_spin.setRange(-1, 9)
    can_spin.setValue(preferences.can_bus)
    # -1 leaves the mcast socket closed entirely, like a bench ESC with
    # no CAN cable: any traffic on a shared bus - even another rig's -
    # carries RawCommands that boot the app out from under a config
    # session
    can_spin.setSpecialValueText("off")
    can_spin.setToolTip(
        "mcast bus number for DroneCAN targets "
        "(239.65.82.N, as the SITL and dronecan_gui_tool "
        "use); disabled for targets with no CAN.\n"
        "Defaults off bus 0: CAN traffic from anything "
        "else - an ArduPilot SITL, another bench rig - "
        "makes the CAN bootloader boot the app instead "
        'of waiting for the configurator. "off" leaves '
        "the CAN unconnected entirely."
    )
    can_spin.setEnabled(False)
    grid.addWidget(can_spin, 4, 1)

    grid.addWidget(QLabel("EEPROM"), 5, 0)
    ee_combo = QComboBox()
    ee_combo.addItem("Defaults, tuned for the simulated motor", "defaults")
    ee_combo.addItem("Blank (0xFF, factory-fresh ESC)", "blank")
    ee_combo.setCurrentIndex(max(0, ee_combo.findData(preferences.eeprom)))
    ee_combo.setToolTip(
        "The settings area at the end of flash.\n"
        "Defaults: a generated eeprom tuned for the simulated motor.\n"
        "Blank: erased 0xFF, as a factory-fresh ESC ships - what a\n"
        "configurator sees before the first save."
    )
    grid.addWidget(ee_combo, 5, 1, 1, 2)

    # -- Renode download -----------------------------------------------
    grid.addWidget(QLabel("Renode"), 6, 0)
    renode_path = QLineEdit(args.renode or "not selected")
    renode_path.setReadOnly(True)
    renode_path.setToolTip("Managed downloads are stored in %s" % renode_cache)
    grid.addWidget(renode_path, 6, 1, 1, 2)
    download_renode = QPushButton("Download Renode")
    grid.addWidget(download_renode, 6, 3)

    # -- configurator port ---------------------------------------------
    grid.addWidget(QLabel("Configurator"), 7, 0)
    conf_combo = QComboBox()
    if os.name != "nt":
        conf_combo.addItem("Serial port (pty)", "serial")
    if sys.platform.startswith("linux") or os.name == "nt":
        conf_combo.addItem("USB device (for the browser)", "usb")
    conf_combo.addItem("Off (drive it some other way)", "off")
    saved_conf = conf_combo.findData(preferences.configurator)
    if saved_conf >= 0:
        conf_combo.setCurrentIndex(saved_conf)
    conf_combo.setToolTip(
        "How the configurator reaches the emulated ESC.\n"
        "A pty works for desktop tools on POSIX hosts; the USB device is a\n"
        "real serial port Chrome can open, so am32.tridgell.net works.\n"
        "Linux uses vhci_hcd; Windows uses the separately installed signed\n"
        "USB/IP virtual host-controller driver."
    )
    grid.addWidget(conf_combo, 7, 1, 1, 2)

    # -- protocol: what sits on that port ------------------------------
    grid.addWidget(QLabel("Protocol"), 8, 0)
    proto_combo = QComboBox()
    proto_combo.addItem("FC with 4-way passthrough", "4way")
    proto_combo.addItem("Direct 1-wire adapter", "direct")
    proto_combo.setCurrentIndex(max(0, proto_combo.findData(preferences.protocol)))
    proto_combo.setToolTip(
        "What the configurator port pretends to be.\n"
        "FC: MSP with BLHeli 4-way passthrough, as a real flight\n"
        "controller provides.\n"
        "Direct: a single-wire adapter soldered to the signal pad -\n"
        "the raw 19200 baud bootloader protocol, with the self-echo\n"
        "such an adapter produces. The web configurator decides\n"
        "FC-vs-adapter by USB vendor id, so the USB device enumerates\n"
        "accordingly; the Offline-Configurator uses its direct/1-wire\n"
        "checkbox on the pty or tty."
    )
    grid.addWidget(proto_combo, 8, 1, 1, 2)

    grid.addWidget(QLabel("ESCs"), 9, 0)
    esc_count_spin = QSpinBox()
    esc_count_spin.setRange(1, MAX_ESC_COUNT)
    esc_count_spin.setValue(preferences.esc_count)
    esc_count_spin.setToolTip(
        "Number of independent Renode ESCs exposed through the fake FC.\n"
        "Each ESC uses its own signal, state and monitor ports. This is\n"
        "available only for FC 4-way passthrough; direct wiring reaches\n"
        "one signal pad."
    )
    grid.addWidget(esc_count_spin, 9, 1)

    def protocol_changed():
        esc_count_spin.setEnabled(proto_combo.currentData() == "4way")

    proto_combo.currentIndexChanged.connect(lambda _index: protocol_changed())
    protocol_changed()

    # -- start/stop, status, log ---------------------------------------
    start_btn = QPushButton("Start")
    stop_btn = QPushButton("Stop")
    stop_btn.setEnabled(False)
    grid.addWidget(start_btn, 10, 2)
    grid.addWidget(stop_btn, 10, 3)
    status_label = QLabel("stopped")
    status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    grid.addWidget(status_label, 10, 0, 1, 2)
    metrics_label = QLabel("")
    metrics_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    metrics_label.setToolTip(
        "Live from the Renode monitor: current PC (labelled when it is\n"
        "executing the bootloader), emulation speed against real time,\n"
        "instructions actually retired per wall second against the\n"
        "configured PerformanceInMips, and the machine's virtual time."
    )
    grid.addWidget(metrics_label, 11, 0, 1, 4)
    log_view = QPlainTextEdit()
    log_view.setReadOnly(True)
    log_view.setMaximumBlockCount(2000)
    log_view.setMinimumSize(640, 240)
    grid.addWidget(log_view, 12, 0, 1, 4)

    control_cleanups = []
    control_signature = None

    def rebuild_control_panel():
        nonlocal control_pages, control_cleanups, control_signature
        if lab.target is None or lab.info is None:
            return
        count = esc_count_spin.value() if proto_combo.currentData() == "4way" else 1
        signature = (
            lab.target,
            bool(lab.info["dronecan"]),
            can_spin.value(),
            count,
        )
        if signature == control_signature:
            return
        for cleanup in control_cleanups:
            if cleanup is not None:
                cleanup()
        control_cleanups = []
        can_command_group = CanCommandGroup()

        old_selected = tabs.currentWidget()
        selected_control = next(
            (index for index, page in enumerate(control_pages) if page is old_selected),
            None,
        )
        tab_index = min(tabs.indexOf(page) for page in control_pages)
        for page in control_pages:
            tabs.removeTab(tabs.indexOf(page))
            page.setParent(None)
            page.deleteLater()
        control_pages = []

        for esc_index in range(count):
            control_page = QWidget()
            control_pages.append(control_page)
            tab_name = "Control" if count == 1 else "Control%u" % (esc_index + 1)
            index = tabs.insertTab(tab_index + esc_index, control_page, tab_name)
            gui_port, state_port, _monitor_port = lab.instance_ports(esc_index)
            control_args = SimpleNamespace(
                host="127.0.0.1",
                port=gui_port,
                state_port=state_port,
                can_uri="mcast:%u" % max(0, can_spin.value()),
                backend="renode",
                renode_can=(bool(lab.info["dronecan"]) and can_spin.value() >= 0),
                esc_number=esc_index + 1 if count > 1 else None,
                can_esc_index=esc_index,
                can_command_group=can_command_group,
                poles=14,
                control_port=0,
                log=None,
                replay=None,
            )
            try:
                cleanup = sitl_gui.create_ui(
                    control_args, app=app, container=control_page
                )
                control_cleanups.append(cleanup)
                tabs.setTabToolTip(
                    index,
                    "%s ESC %u controls on UDP %u/%u"
                    % (lab.target, esc_index + 1, gui_port, state_port),
                )
            except Exception as error:
                # create_ui publishes a partial cleanup hook before constructing
                # optional backend resources. It may fail before it can return
                # the normal closure, so use that hook to avoid orphaning its
                # sockets or worker threads behind this error page.
                abort = getattr(control_page, "_sitl_gui_abort_cleanup", None)
                if abort is not None:
                    abort()
                layout = control_page.layout() or QVBoxLayout(control_page)
                layout.addWidget(QLabel("Could not load controls: %s" % error))
                layout.addStretch(1)
                control_cleanups.append(None)
                continue
        if selected_control is not None:
            tabs.setCurrentWidget(control_pages[min(selected_control, count - 1)])
        control_signature = signature

    can_spin.valueChanged.connect(lambda _value: rebuild_control_panel())
    esc_count_spin.valueChanged.connect(lambda _value: rebuild_control_panel())
    proto_combo.currentIndexChanged.connect(lambda _index: rebuild_control_panel())

    download_active = False

    def renode_version(latest):
        return "%s (%s)" % (
            latest.get("renode_version", "?"),
            latest["source"]["revision"][:9],
        )

    def check_renode_download():
        try:
            latest = renode_download.fetch_latest()
            package = renode_download.select_package(latest)
            current = renode_download.cached(renode_cache, latest, package)
            lab.log_q.put(("__renode_check__", current, latest))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            lab.log_q.put(
                ("__renode_download_error__", "update check failed: %s" % error)
            )

    def start_renode_download():
        nonlocal download_active
        if download_active:
            return False
        download_active = True
        download_renode.setEnabled(False)
        download_renode.setText("Checking...")

        def progress(received, total):
            lab.log_q.put(("__renode_download_progress__", received, total))

        def worker():
            try:
                executable, latest, downloaded = renode_download.install_current(
                    renode_cache, progress=progress
                )
                lab.log_q.put(
                    ("__renode_download_done__", executable, latest, downloaded)
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                tarfile.TarError,
                zipfile.BadZipFile,
                json.JSONDecodeError,
            ) as error:
                lab.log_q.put(("__renode_download_error__", str(error)))

        threading.Thread(target=worker, daemon=True).start()
        return True

    download_renode.clicked.connect(start_renode_download)

    def start_lab(firmware, bootloader, targets_header=None):
        lab.firmware = firmware
        lab.bootloader = bootloader
        lab.targets_header = targets_header
        lab.eeprom = ee_combo.currentData()
        lab.conf = conf_combo.currentData()
        lab.protocol = proto_combo.currentData()
        lab.esc_count = esc_count_spin.value()
        lab.can_bus = can_spin.value()
        err = lab.start()
        if err:
            # also into lab.status, which the control port reports
            lab.status = err
            status_label.setText(err)
            return
        start_btn.setEnabled(False)
        stop_btn.setEnabled(True)
        proto_combo.setEnabled(False)
        esc_count_spin.setEnabled(False)

    def do_start():
        firmware = fw_combo.currentData() or "auto"
        bootloader = bl_combo.currentData() or "auto"
        firmware_catalog = isinstance(firmware, str) and firmware.startswith(
            "catalog:firmware:"
        )
        bootloader_catalog = isinstance(bootloader, str) and bootloader.startswith(
            "catalog:bootloader:"
        )
        if not firmware_catalog and not bootloader_catalog:
            start_lab(firmware, bootloader)
            return
        selected_target = lab.target
        firmware_metadata = dict(lab.info or {})
        for control in (target_filter, target_combo, fw_combo, bl_combo):
            control.setEnabled(False)
        start_btn.setEnabled(False)
        status_label.setText("downloading verified firmware/bootloader...")

        def install_artifacts():
            try:
                header = None
                selected_metadata = firmware_metadata
                installed_firmware = firmware
                if firmware_catalog:
                    release = firmware.rsplit(":", 1)[1]
                    installed = artifact_repository.install(
                        "firmware", release, selected_target
                    )
                    installed_firmware = os.fspath(installed.image)
                    header = os.fspath(installed.targets_header)
                    selected_metadata = installed.metadata
                installed_bootloader = bootloader
                if bootloader_catalog:
                    release = bootloader.rsplit(":", 1)[1]
                    variant = artifact_repository.compatible_bootloader(
                        release, selected_metadata
                    )
                    installed = artifact_repository.install(
                        "bootloader", release, variant["name"]
                    )
                    installed_bootloader = os.fspath(installed.image)
                lab.log_q.put(
                    (
                        "__artifacts_ready__",
                        installed_firmware,
                        installed_bootloader,
                        header,
                        selected_target,
                    )
                )
            except Exception as error:
                lab.log_q.put(("__artifacts_error__", str(error)))

        threading.Thread(target=install_artifacts, daemon=True).start()

    def save_preferences():
        current = settings_store.load()
        settings_store.save(
            replace(
                current,
                launcher=LauncherSettings(
                    target=target_combo.currentText(),
                    bootloader=lab.bootloader,
                    firmware=fw_combo.currentData() or "auto",
                    eeprom=ee_combo.currentData(),
                    configurator=conf_combo.currentData(),
                    protocol=proto_combo.currentData(),
                    esc_count=esc_count_spin.value(),
                    can_bus=can_spin.value(),
                ),
            )
        )

    def do_stop():
        lab.stop()
        start_btn.setEnabled(True)
        stop_btn.setEnabled(False)
        proto_combo.setEnabled(True)
        protocol_changed()
        status_label.setText(lab.status)

    start_btn.clicked.connect(do_start)
    stop_btn.clicked.connect(do_stop)

    def drain_log():
        nonlocal download_active
        lines = []
        while True:
            try:
                item = lab.log_q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item[0].startswith("__renode_download"):
                tag = item[0]
                if tag == "__renode_download_progress__":
                    _tag, received, total = item
                    percent = min(100, received * 100 // max(total, 1))
                    download_renode.setText("Downloading %u%%" % percent)
                elif tag == "__renode_download_done__":
                    _tag, executable, latest, downloaded = item
                    download_active = False
                    args.renode = str(executable)
                    renode_path.setText(args.renode)
                    download_renode.setText("Renode current")
                    download_renode.setEnabled(True)
                    action = "downloaded" if downloaded else "using cached"
                    lines.append(
                        "[renode] %s %s: %s"
                        % (action, renode_version(latest), executable)
                    )
                elif tag == "__renode_download_error__":
                    download_active = False
                    download_renode.setText("Retry Renode download")
                    download_renode.setEnabled(True)
                    lines.append("[renode] %s" % item[1])
                continue
            if isinstance(item, tuple) and item[0] == "__renode_check__":
                _tag, current, latest = item
                if current is None:
                    label = (
                        "Update Renode"
                        if renode_download.cached(renode_cache)
                        else "Download Renode"
                    )
                    download_renode.setText(label)
                    download_renode.setToolTip(
                        "Current server version: %s" % renode_version(latest)
                    )
                else:
                    if args.renode is None:
                        args.renode = str(current)
                        renode_path.setText(args.renode)
                    selected = args.renode and Path(args.renode).expanduser() == current
                    download_renode.setText(
                        "Renode current" if selected else "Use cached current"
                    )
                    download_renode.setToolTip(
                        "Cached server version: %s" % renode_version(latest)
                    )
                continue
            if isinstance(item, tuple) and item[0] == "__metrics__":
                if item[1] == lab.generation:
                    lab.metrics = item[2]
                continue
            if isinstance(item, tuple) and item[0] == "__monitor_error__":
                if item[1] == lab.generation:
                    lines.append("[monitor] %s" % item[2])
                continue
            if isinstance(item, tuple) and item[0] == "__target__":
                _tag, t, info = item
                if t != target_combo.currentText():
                    continue
                lab.target, lab.info = t, info
                if info is None:
                    info_label.setText("%s: unsupported" % t)
                    continue
                info_label.setText(
                    "%s: %s, signal pin %s%s"
                    % (
                        t,
                        info["family"].upper(),
                        info["pin"],
                        ", DroneCAN" if info["dronecan"] else "",
                    )
                )
                can_spin.setEnabled(bool(info["dronecan"]))
                refresh_bootloaders()
                refresh_firmware_releases()
                rebuild_control_panel()
                continue
            if isinstance(item, tuple) and item[0] == "__targets__":
                target_names.extend(item[1])
                apply_filter()
                continue
            if isinstance(item, tuple) and item[0] == "__targets_source__":
                document = item[1]
                target_names[:] = document.targets
                initial_target[0] = ""
                location = document.source.location
                source_url_btn.setToolTip("Current targets.h source: %s" % location)
                source_file_btn.setToolTip("Current targets.h source: %s" % location)
                source_url_btn.setEnabled(True)
                source_file_btn.setEnabled(True)
                lines.append(
                    "[targets] %u targets from %s" % (len(document.targets), location)
                )
                apply_filter()
                continue
            if isinstance(item, tuple) and item[0] == "__targets_source_error__":
                source_url_btn.setEnabled(True)
                source_file_btn.setEnabled(True)
                info_label.setText("targets.h failed: %s" % item[1])
                lines.append("[targets] %s" % item[1])
                continue
            if isinstance(item, tuple) and item[0] == "__artifact_catalog__":
                artifact_catalog[0] = item[1]
                refresh_bootloaders()
                refresh_firmware_releases()
                lines.append(
                    "[artifacts] %u firmware and %u bootloader releases"
                    % (
                        len(item[1]["releases"]["firmware"]),
                        len(item[1]["releases"]["bootloader"]),
                    )
                )
                continue
            if isinstance(item, tuple) and item[0] == "__artifact_catalog_error__":
                lines.append("[artifacts] catalog unavailable: %s" % item[1])
                continue
            if isinstance(item, tuple) and item[0] == "__artifacts_ready__":
                _tag, firmware, bootloader, header, selected_target = item
                for control in (target_filter, target_combo, fw_combo, bl_combo):
                    control.setEnabled(True)
                start_btn.setEnabled(True)
                if lab.target != selected_target:
                    lab.status = "target changed while artifacts were downloading"
                    lines.append("[artifacts] " + lab.status)
                else:
                    start_lab(firmware, bootloader, header)
                continue
            if isinstance(item, tuple) and item[0] == "__artifacts_error__":
                for control in (target_filter, target_combo, fw_combo, bl_combo):
                    control.setEnabled(True)
                start_btn.setEnabled(True)
                lab.status = "artifact download failed: %s" % item[1]
                lines.append("[artifacts] %s" % item[1])
                continue
            lab.saw_log_line(item)
            lines.append(item)
        if lines:
            log_view.appendPlainText("\n".join(lines))
        status_label.setText(lab.status)
        metrics_label.setText(lab.format_metrics())

    timer = QTimer()
    timer.timeout.connect(drain_log)
    timer.start(150)

    # -- control port --------------------------------------------------
    pending = queue.Queue()

    def on_command(line):
        done = queue.Queue()
        pending.put((line, done))
        try:
            return done.get(timeout=30)
        except queue.Empty:
            return "ERR timeout"

    def poll_pending():
        try:
            line, done = pending.get_nowait()
        except queue.Empty:
            return
        done.put(handle_command(line))

    def handle_command(line):
        parts = line.split(None, 1)
        if not parts:
            return "ERR empty"
        cmd, rest = parts[0], parts[1] if len(parts) > 1 else ""
        if cmd == "target":
            target_filter.setText(rest)
            if target_combo.findText(rest) < 0:
                return "ERR no target %s" % rest
            target_combo.setCurrentIndex(target_combo.findText(rest))
            # wait for the async resolve
            deadline = time.time() + 60
            while time.time() < deadline and lab.target != rest:
                drain_log()
                time.sleep(0.2)
            return "OK" if lab.target == rest else "ERR resolve timeout"
        if cmd == "bootloader":
            selection = rest or "auto"
            index = bl_combo.findData(selection)
            if index < 0:
                if selection.startswith("catalog:"):
                    return "ERR no bootloader %s" % selection
                bl_combo.insertItem(0, os.path.basename(selection), selection)
                index = 0
            bl_combo.setCurrentIndex(index)
            return "OK"
        if cmd == "firmware":
            if rest in ("", "auto", "none"):
                fw_combo.setCurrentIndex(fw_combo.findData(rest or "auto"))
            else:
                fw_combo.insertItem(0, os.path.basename(rest), rest)
                fw_combo.setCurrentIndex(0)
            return "OK"
        if cmd == "eeprom":
            i = ee_combo.findData(rest)
            if i < 0:
                return "ERR eeprom defaults|blank"
            ee_combo.setCurrentIndex(i)
            return "OK"
        if cmd == "conf":
            i = conf_combo.findData(rest)
            if i < 0:
                return "ERR conf off|serial|usb"
            conf_combo.setCurrentIndex(i)
            return "OK"
        if cmd == "protocol":
            if lab.runner.running():
                return "ERR stop before changing protocol"
            i = proto_combo.findData(rest)
            if i < 0:
                return "ERR protocol 4way|direct"
            proto_combo.setCurrentIndex(i)
            return "OK"
        if cmd == "escs":
            if lab.runner.running():
                return "ERR stop before changing ESC count"
            try:
                count = int(rest)
            except ValueError:
                return "ERR escs 1..%u" % MAX_ESC_COUNT
            if not 1 <= count <= MAX_ESC_COUNT:
                return "ERR escs 1..%u" % MAX_ESC_COUNT
            esc_count_spin.setValue(count)
            return "OK"
        if cmd == "canbus":
            can_spin.setValue(int(rest))
            return "OK"
        if cmd == "download-renode":
            return "OK" if start_renode_download() else "ERR download in progress"
        if cmd == "start":
            do_start()
            return "OK" if not start_btn.isEnabled() else ("ERR " + lab.status)
        if cmd == "stop":
            do_stop()
            return "OK"
        if cmd == "status":
            extra = lab.format_metrics()
            return "STATUS %s | port=%s | emulator=%s%s" % (
                lab.status,
                lab.conf_port,
                "up" if lab.emulator_ready else "down",
                " | " + extra if extra else "",
            )
        if cmd == "quit":
            QTimer.singleShot(100, app.quit)
            return "OK"
        return "ERR unknown %s" % cmd

    if args.control_port:
        run_control_server(lab, args.control_port, on_command)
        ptimer = QTimer()
        ptimer.timeout.connect(poll_pending)
        ptimer.start(50)

    # populate targets in the background so the window opens instantly
    def targets_thread():
        found = load_targets()
        lab.log_q.put("%u targets" % len(found))
        lab.log_q.put(("__targets__", found))

    def artifacts_thread():
        try:
            catalog = artifact_repository.catalog(refresh=True)
            lab.log_q.put(("__artifact_catalog__", catalog))
        except Exception as error:
            lab.log_q.put(("__artifact_catalog_error__", str(error)))

    threading.Thread(target=targets_thread, daemon=True).start()
    threading.Thread(target=artifacts_thread, daemon=True).start()
    threading.Thread(target=check_renode_download, daemon=True).start()

    signal.signal(signal.SIGINT, lambda *a: app.quit())
    signal.signal(signal.SIGTERM, lambda *a: app.quit())
    win.show()
    try:
        app.exec()
    finally:
        save_preferences()
        for cleanup in control_cleanups:
            if cleanup is not None:
                cleanup()
        lab.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

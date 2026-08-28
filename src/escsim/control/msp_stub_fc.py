"""
fake Betaflight FC: a minimal MSP server bridged to the SITL's UDP
DShot input, with BLHeli 4-way passthrough to the simulated ESC

Lets scripts/esc_capture_fc.py run against the SITL binary with no
hardware: the stub answers the MSP preflight queries, streams
bidirectional DShot600 to the SITL from the latest MSP_SET_MOTOR
value, decodes the BDShot/EDT replies and serves them back as
MSP_MOTOR_TELEMETRY, just like a real FC does.

MSP_SET_PASSTHROUGH switches the link into 4-way mode
(sitl_fourway_server.py), so an unmodified ESC configurator can read and
write the settings and flash of a SITL running with --bootloader, the
same way it would through a real flight controller.

With --direct it is a single-wire adapter on the ESC signal pad
instead of a flight controller: the raw 19200 baud bootloader protocol
with the adapter's self-echo, which is how both configurators' direct
modes expect the wire to behave. The line is held idle-high, so the
bootloader stays resident across ESC resets.

Serves MSP on a pty by default (Linux/macOS only), printing the slave
device path to hand to --port. With --usbip it serves on a virtual USB
serial device instead (sitl_usbip.py), which enumerates as a real
/dev/ttyACM* once attached to vhci_hcd and so is reachable from tools
that only accept USB serial ports, the browser included.
"""

import argparse
from dataclasses import dataclass
import os
import select
import socket
import struct
import sys
import threading
import time

from . import dshot as sd
from . import fourway_server as sitl_fourway_server
from . import usbip as sitl_usbip

try:
    import pty
except ImportError:
    pty = None

MSP_API_VERSION = 1
MSP_FC_VARIANT = 2
MSP_FEATURE_CONFIG = 36
MSP_STATUS = 101
MSP_MOTOR = 104
MSP_BOXIDS = 119
MSP_MOTOR_CONFIG = 131
MSP_MOTOR_TELEMETRY = 139
MSP_BATTERY_STATE = 130
MSP_SET_MOTOR = 214
MSP_SET_PASSTHROUGH = 245

# ArduPilot releases motor control when the configurator stops sending MSP.
# Apart from being faithful to the FC we emulate, this prevents a stale slider
# value from fighting the launcher's native Control tabs indefinitely.
MOTOR_ACTIVE_TIMEOUT = 1.0
# Fallback for a backend that does not answer Renode's armed-state query.  A
# real Renode run uses the observed firmware flag instead of wall-clock time.
MOTOR_ARM_TIME = 1.5

# USB identity for --direct: a vendor id the web configurator treats as
# a direct single-wire adapter (WCH), with a product id no kernel
# vendor driver claims, so cdc_acm keeps the port
DIRECT_VENDOR_ID = 0x1A86
DIRECT_PRODUCT_ID = 0x0001


class PtyEndpoint(object):
    """the serial link as a pty, opened by the client by path"""

    def __init__(self):
        if pty is None:
            raise RuntimeError(
                "pseudo terminals are not available on Windows; use a USB/IP endpoint"
            )
        self.master, self.slave = pty.openpty()
        # raw mode now: the default line discipline would echo the
        # client's bytes back at us until pyserial reconfigures it
        import tty

        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)

    def read(self, timeout=0.1):
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(self.master, 4096)
        except OSError:
            return b""

    def drain(self):
        out = b""
        while True:
            chunk = self.read(0)
            if not chunk:
                return out
            out += chunk

    def write(self, data):
        os.write(self.master, data)

    def close(self):
        try:
            os.close(self.master)
            os.close(self.slave)
        except OSError:
            pass


class DirectBridge(object):
    """the serial link wired straight to the ESC's signal pad, the way a
    single-wire USB adapter presents it: no flight controller, no MSP,
    no 4-way - the raw 19200 baud bootloader protocol. TX and RX share
    the wire on such an adapter, so the client reads its own
    transmission back; both configurators' direct modes strip that
    echo. The echo is released at wire pace, not on receipt: the client
    only moves on once its bytes have actually left the wire, and the
    bootloader ends a command frame on line idle, so an instant echo
    lets the client run its next command seamlessly into the previous
    frame and nothing ever parses. The wire idles high, keeping the
    bootloader resident across ESC resets."""

    BAUD = 19200.0
    GAP_S = 0.001  # FLAG_GAP's leading line idle

    def __init__(
        self,
        sitl_host="127.0.0.1",
        sitl_port=57733,
        endpoint=None,
        verbose=False,
        trace=False,
    ):
        self.verbose = verbose
        self.tracing = trace or os.environ.get("AM32_FC_TRACE") == "1"
        self.port = sd.InputPort(sitl_host, sitl_port)
        self.ep = endpoint if endpoint is not None else PtyEndpoint()
        self.running = True
        self.wire_free = 0.0  # when the queued TX will have left
        self.echoes = []  # pending self-echo, dribbled out
        self.skew = 2.0  # emulator wire time / wall time
        self.port.send_level(1)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    @property
    def slave_path(self):
        return self.ep.path

    def _trace(self, direction, data):
        if not self.tracing or not data:
            return
        print(
            "DW %s %3u: %s" % (direction, len(data), data.hex(" ")),
            file=sys.stderr,
            flush=True,
        )

    def _flush_echoes(self):
        for e in self.echoes:
            if e["sent"] < len(e["data"]):
                self._trace("echo", e["data"][e["sent"] :])
                self.ep.write(e["data"][e["sent"] :])
        self.echoes = []

    def _dribble_echoes(self, now):
        """Release each pending echo progressively across its estimated
        transmission time, as the shared wire feeds it back on real
        hardware. A client paces itself on this echo - typically with an
        inactivity timeout - so it must trickle in during transmission,
        not land as one burst at the end: a burst leaves silent windows
        long enough for the client to give up mid-command and retry a
        command the ESC goes on to answer."""
        while self.echoes:
            e = self.echoes[0]
            frac = (now - e["start"]) / (e["wire_s"] * self.skew)
            due = min(len(e["data"]), int(len(e["data"]) * frac))
            if due > e["sent"]:
                self._trace("echo", e["data"][e["sent"] : due])
                self.ep.write(e["data"][e["sent"] : due])
                e["sent"] = due
            if e["sent"] < len(e["data"]):
                return
            self.echoes.pop(0)

    def _loop(self):
        tx_done = self.port.tx_done_count
        while self.running:
            chunk = self.ep.read(0.01)
            now = time.time()
            if chunk:
                self._trace("rx", chunk)
                # Frame separation as the wire itself would show it: a
                # chunk arriving on an idle wire begins a new command
                # and gets the idle gap the bootloader needs to end the
                # previous frame; one arriving while bytes are still
                # going out continues the frame (a USB packet split
                # mid-command must not be torn apart).
                gap = now >= self.wire_free
                if gap:
                    self.wire_free = now + self.GAP_S
                self.port.send_serial(chunk, idle_high=True, gap=gap)
                wire_s = len(chunk) * 10.0 / self.BAUD
                self.wire_free = max(self.wire_free, now) + wire_s
                self.echoes.append(
                    {"data": chunk, "sent": 0, "start": now, "wire_s": wire_s}
                )
            done = self.port.tx_done_count
            if done != tx_done:
                tx_done = done
                # the emulator says everything queued has left the wire:
                # calibrate how fast its wire runs against ours, so the
                # dribble pacing tracks an emulator running slower (or
                # faster) than real time
                if self.echoes:
                    e = self.echoes[0]
                    took = max(now - e["start"], 1e-3)
                    total = sum(x["wire_s"] for x in self.echoes)
                    if total > 0.005:
                        self.skew += 0.3 * (
                            min(max(took / total, 0.5), 10.0) - self.skew
                        )
                self._flush_echoes()
                self.wire_free = min(self.wire_free, now)
            out = self.port.drain_serial()
            if out:
                # a reply proves the command that provoked it has fully
                # left the wire: its echo goes first, in wire order
                self._flush_echoes()
                self._trace("tx", out)
                self.ep.write(out)
            self._dribble_echoes(now)

    def close(self):
        self.running = False
        self.thread.join(0.5)
        self.ep.close()
        self.port.close()


@dataclass
class MotorState:
    value: int = 1000
    rpm: int = 0
    invalid: float = 100.0
    temp: float = 0
    volt_raw: int = 0
    curr_raw: int = 0
    edt_seen: bool = False
    last_edt_cmd: float = 0.0
    output_ready: bool = False


class RenodeArmingProbe:
    """Read each ESC's real armed flag without subscribing to its scope."""

    MAGIC_CMD = 0x5353
    MAGIC_INFO = 0x5359
    REQUEST_INTERVAL = 0.1

    def __init__(self, host, state_ports):
        self.addresses = [(host, port) for port in state_ports if port]
        self.indices = {}
        for index, port in enumerate(state_ports):
            if port:
                self.indices.setdefault(port, []).append(index)
        self.armed = [None] * len(state_ports)
        self.last_request = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)

    def reset(self):
        self.armed = [None] * len(self.armed)
        self.last_request = 0.0

    def poll(self, now):
        if now - self.last_request >= self.REQUEST_INTERVAL:
            request = struct.pack("<HBB", self.MAGIC_CMD, 10, 0)
            for address in self.addresses:
                try:
                    self.sock.sendto(request, address)
                except OSError:
                    pass
            self.last_request = now
        while True:
            try:
                data, address = self.sock.recvfrom(512)
            except BlockingIOError:
                break
            except OSError:
                break
            if len(data) < 20:
                continue
            magic, command, flags = struct.unpack_from("<HBB", data)
            if magic != self.MAGIC_INFO or command != 9:
                continue
            for index in self.indices.get(address[1], ()):
                self.armed[index] = bool(flags & 4)
        return list(self.armed)

    def close(self):
        self.sock.close()


class MspStubFC(object):
    def __init__(
        self,
        sitl_host="127.0.0.1",
        sitl_port=57733,
        poles=14,
        rate=500.0,
        esc_ports=None,
        state_port=57734,
        state_ports=None,
        esc_reset=True,
        motor=True,
        verbose=False,
        endpoint=None,
        trace=False,
    ):
        self.poles = poles
        self.rate = rate
        self.verbose = verbose
        self.tracing = trace or os.environ.get("AM32_FC_TRACE") == "1"
        self.esc_ports = list(esc_ports or [sitl_port])
        # the ESCs reachable over 4-way passthrough: one SITL input port
        # each, defaulting to the one we drive with DShot
        self.fourway = sitl_fourway_server.FourWayServer(
            esc_ports=self.esc_ports,
            host=sitl_host,
            state_port=state_port,
            state_ports=state_ports,
            esc_reset=esc_reset,
            log=self._log,
        )
        self.in_fourway = False
        self.motor_lock = threading.Lock()
        # The real FC keeps sending disarmed (zero-throttle) DShot after
        # leaving 4-way mode and after an MSP motor-command timeout.  Do not
        # start it before the first interface exit: Renode initially parks the
        # ESCs in their bootloaders, where 4-way must own the signal wires.
        self.motor_output_enabled = False
        self.motor_active = False
        self.last_motor_command = 0.0
        self.motor_arming_until = 0.0
        # set to stop updating the telemetry while still answering MSP,
        # reproducing Betaflight serving its cached values after the
        # BDShot replies stop arriving (it never marks them stale)
        self.freeze = False
        self.ports = [sd.InputPort(sitl_host, port) for port in self.esc_ports]
        self.port = self.ports[0]  # compatibility for single-ESC callers
        self.motors = [MotorState() for _port in self.ports]
        self.arming_probe = None
        if motor:
            try:
                self.arming_probe = RenodeArmingProbe(
                    sitl_host, self.fourway.state_ports
                )
            except OSError:
                pass
        self.running = True
        self.ep = endpoint if endpoint is not None else PtyEndpoint()
        # DShot remains dormant until motor control begins or 4-way exits, and
        # pauses while the same signal wires are used by another 4-way session.
        self.dshot_thread = None
        if motor:
            self.dshot_thread = threading.Thread(target=self._dshot_loop, daemon=True)
            self.dshot_thread.start()
        self.msp_thread = threading.Thread(target=self._msp_loop, daemon=True)
        self.msp_thread.start()

    @property
    def slave_path(self):
        """the serial device a client should open"""
        return self.ep.path

    def _log(self, msg):
        if self.verbose:
            print("FC: %s" % msg, file=sys.stderr, flush=True)

    def _trace(self, direction, data):
        """byte level trace of the configurator link, for working out
        which side of a failed session went quiet. AM32_FC_TRACE=1 turns
        it on for a stub started by the GUI, which has no command line"""
        if not self.tracing or not data:
            return
        print(
            "FC %s %3u: %s" % (direction, len(data), data.hex(" ")),
            file=sys.stderr,
            flush=True,
        )

    def close(self):
        self.running = False
        self._stop_motor_control()
        # Do not close a pty descriptor under a blocking read in another
        # thread: close() itself can wait for that read forever on macOS.
        # _msp_loop polls with a short timeout, so both workers can leave
        # before their descriptors are closed.
        self.msp_thread.join(0.5)
        if self.dshot_thread is not None:
            self.dshot_thread.join(0.5)
        self._stop_motor_control()
        self.fourway.close()
        if self.arming_probe is not None:
            self.arming_probe.close()
        self.ep.close()
        for port in self.ports:
            port.close()

    # -- DShot side ----------------------------------------------------

    @property
    def motor_value(self):
        return self.motors[0].value

    @motor_value.setter
    def motor_value(self, value):
        self.motors[0].value = value

    @property
    def rpm(self):
        return self.motors[0].rpm

    @rpm.setter
    def rpm(self, value):
        self.motors[0].rpm = value

    @property
    def invalid(self):
        return self.motors[0].invalid

    @invalid.setter
    def invalid(self, value):
        self.motors[0].invalid = value

    @property
    def temp(self):
        return self.motors[0].temp

    @temp.setter
    def temp(self, value):
        self.motors[0].temp = value

    @property
    def volt_raw(self):
        return self.motors[0].volt_raw

    @volt_raw.setter
    def volt_raw(self, value):
        self.motors[0].volt_raw = value

    @property
    def curr_raw(self):
        return self.motors[0].curr_raw

    @curr_raw.setter
    def curr_raw(self, value):
        self.motors[0].curr_raw = value

    @staticmethod
    def _dshot_value(value):
        """BF motor value 1000..2000 -> 11 bit DShot throttle"""
        if value <= 1000:
            return 0
        return 48 + int((min(value, 2000) - 1000) * (2047 - 48) / 1000)

    def _motor_snapshot(self, now=None):
        """Return the FC outputs, timing active MSP throttle out safely."""
        now = time.monotonic() if now is None else now
        with self.motor_lock:
            observed = (
                self.arming_probe.poll(now) if self.arming_probe is not None else None
            )
            if observed is not None:
                for index, armed in enumerate(observed):
                    if armed:
                        self.motors[index].output_ready = True
            ready = [
                motor.output_ready
                or (
                    (observed is None or observed[index] is None)
                    and now >= self.motor_arming_until
                )
                for index, motor in enumerate(self.motors)
            ]
            # Renode may run below real time when several ESCs are active.
            # Preserve the first requested throttle until the firmware has
            # actually completed its one-second emulated arming interval.
            waiting_to_arm = self.motor_active and any(
                motor.value > 1000 and not ready[index]
                for index, motor in enumerate(self.motors)
            )
            if waiting_to_arm:
                self.last_motor_command = now
            if (
                self.motor_active
                and now - self.last_motor_command > MOTOR_ACTIVE_TIMEOUT
            ):
                self.motor_active = False
                self.motor_arming_until = 0.0
                for motor in self.motors:
                    motor.value = 1000
            if self.in_fourway or not self.motor_output_enabled:
                return None
            if not self.motor_active:
                return [1000] * len(self.motors)
            if waiting_to_arm:
                return [1000] * len(self.motors)
            return [
                motor.value if ready[index] else 1000
                for index, motor in enumerate(self.motors)
            ]

    def _stop_motor_control(self):
        with self.motor_lock:
            self.motor_output_enabled = False
            self.motor_active = False
            self.motor_arming_until = 0.0
            for motor in self.motors:
                motor.value = 1000
                motor.output_ready = False

    def _send_motor_cycle(self, values, now):
        # Serialize a complete output cycle against MSP_SET_PASSTHROUGH. Once
        # that handler marks in_fourway, no later DShot packet can land in the
        # serial transaction on any ESC wire.
        with self.motor_lock:
            if self.in_fourway or not self.motor_output_enabled:
                return False
            for index, port in enumerate(self.ports):
                value = self._dshot_value(values[index])
                motor = self.motors[index]
                # maintain EDT while stopped, like a real FC with dshot_edt on
                # (the firmware ignores commands once spinning)
                if value == 0 and not motor.edt_seen and now - motor.last_edt_cmd > 0.5:
                    motor.last_edt_cmd = now
                    for _ in range(20):
                        port.send_dshot(
                            sd.DSHOT_CMD_EDT_ENABLE,
                            ptype=sd.TYPE_DSHOT600,
                            telem=True,
                            bidir=True,
                        )
                    continue
                port.send_dshot(value, ptype=sd.TYPE_DSHOT600, bidir=True)
        return True

    def _dshot_loop(self):
        nxt = time.time()
        while self.running:
            now = time.time()
            values = self._motor_snapshot()
            if values is None:
                nxt = now
                self._drain_replies()
                time.sleep(0.002)
                continue
            cycles = 0
            while now >= nxt and cycles < 10:
                nxt += 1.0 / self.rate
                if not self._send_motor_cycle(values, now):
                    break
                cycles += 1
            if now - nxt > 0.25:
                nxt = now
            self._drain_replies()
            time.sleep(0.0005)

    def _drain_replies(self):
        for index, port in enumerate(self.ports):
            replies = port.get_replies()
            if self.freeze:
                continue  # discard, keep the cached values
            motor = self.motors[index]
            for r in replies:
                kind, val = sd.decode_reply(r[3], edt_expected=True)
                if kind == "erpm":
                    motor.rpm = int(sd.erpm_period_to_rpm(val, self.poles))
                    motor.invalid = max(0.0, motor.invalid - 1.0)
                elif kind == "temp":
                    motor.temp = val
                    motor.edt_seen = True
                elif kind == "volt":
                    motor.volt_raw = int(round(val / 0.25))
                    motor.edt_seen = True
                elif kind == "current":
                    motor.curr_raw = int(round(val / 0.5))
                    motor.edt_seen = True
                elif kind == "edt":
                    motor.edt_seen = True
                elif kind == "badcrc":
                    motor.invalid = min(100.0, motor.invalid + 0.1)

    # -- MSP side ------------------------------------------------------

    def _reply(self, cmd, payload=b""):
        hdr = struct.pack("<BB", len(payload), cmd)
        ck = 0
        for b in hdr + payload:
            ck ^= b
        out = b"$M>" + hdr + payload + bytes([ck])
        self._trace("tx", out)
        self.ep.write(out)

    def _handle(self, cmd, payload):
        if cmd == MSP_API_VERSION:
            self._reply(cmd, struct.pack("<BBB", 0, 1, 46))
        elif cmd == MSP_FC_VARIANT:
            self._reply(cmd, b"BTFL")
        elif cmd == MSP_STATUS:
            # cycletime, i2c errors, sensors, mode flags (disarmed), profile
            self._reply(cmd, struct.pack("<HHHIB", 125, 0, 0, 0, 0))
        elif cmd == MSP_BOXIDS:
            self._reply(cmd, bytes([0]))  # one box: ARM
        elif cmd == MSP_FEATURE_CONFIG:
            self._reply(cmd, struct.pack("<I", 0))  # no 3D mode
        elif cmd == MSP_MOTOR:
            # MSP v1 has eight fixed motor slots. ArduPilot fills the active
            # channels and leaves the rest at zero.
            with self.motor_lock:
                values = [motor.value for motor in self.motors]
            values += [0] * (8 - len(values))
            self._reply(cmd, struct.pack("<8H", *values[:8]))
        elif cmd == MSP_MOTOR_CONFIG:
            self._reply(
                cmd,
                struct.pack(
                    "<HHHBBBB",
                    1070,
                    2000,
                    1000,
                    self.fourway.esc_count,
                    self.poles,
                    1,
                    0,
                ),
            )
        elif cmd == MSP_MOTOR_TELEMETRY:
            out = bytes([self.fourway.esc_count])
            for motor in self.motors:
                # matches Betaflight's DShot telemetry serialisation:
                # voltage is the 0.25V-step EDT value >> 2, current
                # is the raw EDT byte (msp.c MSP_MOTOR_TELEMETRY)
                out += struct.pack(
                    "<IHBHHH",
                    motor.rpm,
                    int(motor.invalid * 100),
                    int(motor.temp),
                    motor.volt_raw >> 2,
                    motor.curr_raw,
                    0,
                )
            self._reply(cmd, out)
        elif cmd == MSP_BATTERY_STATE:
            # cells, capacity, voltage in 0.1V, mAh drawn, current in 0.01A
            self._reply(cmd, struct.pack("<BHBHH", 4, 1500, 126, 0, 0))
        elif cmd == MSP_SET_PASSTHROUGH:
            with self.motor_lock:
                self.in_fourway = True
                self.motor_output_enabled = False
                self.motor_active = False
                self.motor_arming_until = 0.0
                for motor in self.motors:
                    motor.value = 1000
                    motor.output_ready = False
                if self.arming_probe is not None:
                    self.arming_probe.reset()
            self.fourway.begin()
            self._reply(cmd, bytes([self.fourway.esc_count]))
            self._log("4-way passthrough to %u ESC(s)" % self.fourway.esc_count)
        elif cmd == MSP_SET_MOTOR:
            count = min(len(payload) // 2, len(self.motors))
            if count:
                with self.motor_lock:
                    now = time.monotonic()
                    if not self.motor_output_enabled:
                        self.motor_output_enabled = True
                        self.motor_arming_until = now + MOTOR_ARM_TIME
                    for index in range(count):
                        self.motors[index].value = struct.unpack_from(
                            "<H", payload, index * 2
                        )[0]
                    self.last_motor_command = max(now, self.motor_arming_until)
                    self.motor_active = True
            self._reply(cmd)
        else:
            self.ep.write(b"$M!" + struct.pack("<BB", 0, cmd) + bytes([cmd]))

    def _fourway(self, chunk):
        """run the 4-way session until the client exits the interface"""
        resp = self.fourway.feed(chunk)
        if resp:
            self._trace("tx", resp)
            # A slow transaction (a 256 byte flash chunk is ~140ms of
            # 19200 baud wire time) makes the client retry, and those
            # retries queue up while we work. Answering each one would
            # shift every later response by one, so exact duplicates of
            # the request we just answered are dropped - but ONLY exact
            # duplicates: a client that timed out and moved on has its
            # NEXT command queued here, and discarding that starves the
            # whole session one command at a time.
            pending = self.ep.drain()
            dropped = 0
            last = self.fourway.last_request
            while last and pending.startswith(last):
                pending = pending[len(last) :]
                dropped += 1
            if dropped:
                self._log("dropped %u retries of the answered command" % dropped)
            self.ep.write(resp)
            if pending:
                self._trace("rx", pending)
                self._fourway(pending)
                return
        if self.fourway.exited:
            self._log("4-way interface exited")
            with self.motor_lock:
                self.in_fourway = False
                self.motor_output_enabled = True
                self.motor_active = False
                self.motor_arming_until = time.monotonic() + MOTOR_ARM_TIME
                for motor in self.motors:
                    motor.value = 1000
                    motor.output_ready = False
                if self.arming_probe is not None:
                    self.arming_probe.reset()

    def _msp_loop(self):
        buf = b""
        while self.running:
            chunk = self.ep.read(0.1)
            if not chunk:
                continue
            self._trace("rx", chunk)
            if self.in_fourway:
                self._fourway(chunk)
                continue
            buf += chunk
            while True:
                start = buf.find(b"$M<")
                if start < 0:
                    buf = b""
                    break
                buf = buf[start:]
                if len(buf) < 5:
                    break
                size = buf[3]
                if len(buf) < 6 + size:
                    break
                cmd = buf[4]
                payload = buf[5 : 5 + size]
                ck = 0
                for b in buf[3 : 5 + size]:
                    ck ^= b
                good = ck == buf[5 + size]
                buf = buf[6 + size :]
                if good:
                    self._handle(cmd, payload)
                if self.in_fourway:
                    if buf:
                        self._fourway(buf)
                    buf = b""
                    break


def main():
    parser = argparse.ArgumentParser(description="fake Betaflight FC for the AM32 SITL")
    parser.add_argument("--host", default="127.0.0.1", help="SITL host")
    parser.add_argument(
        "--sitl-port",
        type=int,
        default=57733,
        help="SITL input port, the simulated signal wire",
    )
    parser.add_argument(
        "--esc-ports",
        default=None,
        help="comma separated SITL input ports of the ESCs "
        "reachable over 4-way (default --sitl-port)",
    )
    parser.add_argument(
        "--state-port",
        type=int,
        default=57734,
        help="SITL state port, used to reset an ESC into the bootloader (0 disables)",
    )
    parser.add_argument(
        "--state-ports",
        default=None,
        help="comma separated state/reset ports matching --esc-ports",
    )
    parser.add_argument(
        "--no-esc-reset",
        action="store_true",
        help="never reset an ESC that does not answer",
    )
    parser.add_argument(
        "--no-motor", action="store_true", help="do not drive DShot, 4-way and MSP only"
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="be a single-wire adapter on the ESC signal "
        "pad instead of a flight controller: raw "
        "bootloader protocol with self-echo, no MSP "
        "or 4-way",
    )
    parser.add_argument(
        "--usbip",
        action="store_true",
        help="serve on a virtual USB serial device instead "
        "of a pty, so tools that only take USB ports "
        "(a browser) can reach it",
    )
    parser.add_argument(
        "--usbip-socket",
        default=None,
        help="unix socket the virtual device is exported on, "
        "@name for the abstract namespace",
    )
    parser.add_argument(
        "--usbip-port",
        type=int,
        default=None,
        help="export the virtual device on this tcp port instead of a unix socket",
    )
    parser.add_argument(
        "--usbip-serial",
        default=sitl_usbip.DEFAULT_SERIAL,
        help="usb serial string of the virtual device, which "
        "names its /dev/serial/by-id link",
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help="with --usbip, attach it to vhci_hcd for you",
    )
    parser.add_argument("--poles", type=int, default=14)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="hex dump every byte to and from the client",
    )
    args = parser.parse_args()

    ports = None
    if args.esc_ports:
        ports = [int(p) for p in args.esc_ports.split(",") if p.strip()]
    state_ports = None
    if args.state_ports:
        state_ports = [int(p) for p in args.state_ports.split(",") if p.strip()]

    endpoint = None
    if args.usbip:

        def log(msg):
            if args.verbose:
                print("usbip: %s" % msg, file=sys.stderr, flush=True)

        ids = {"vid": DIRECT_VENDOR_ID, "pid": DIRECT_PRODUCT_ID} if args.direct else {}
        endpoint = sitl_usbip.UsbipServer(
            unix_path=args.usbip_socket,
            port=args.usbip_port,
            serial=args.usbip_serial,
            log=log,
            **ids,
        )

    if args.direct:
        stub = DirectBridge(
            sitl_host=args.host,
            sitl_port=args.sitl_port,
            endpoint=endpoint,
            verbose=args.verbose,
            trace=args.trace,
        )
    else:
        stub = MspStubFC(
            sitl_host=args.host,
            sitl_port=args.sitl_port,
            poles=args.poles,
            esc_ports=ports,
            state_port=args.state_port,
            state_ports=state_ports,
            esc_reset=not args.no_esc_reset,
            motor=not args.no_motor,
            verbose=args.verbose,
            endpoint=endpoint,
            trace=args.trace,
        )
    if args.usbip:
        print(
            "virtual %s exported on %s"
            % ("adapter" if args.direct else "FC", endpoint.endpoint),
            file=sys.stderr,
            flush=True,
        )
        if args.attach:
            attached_port = sitl_usbip.attach(
                unix_path=endpoint.unix_path, host=endpoint.host, port=endpoint.port
            )
            if attached_port is None or attached_port is False:
                print("attach failed", file=sys.stderr)
                stub.close()
                return 1
        else:
            print(
                "attach it with: %s %s --attach-to %s"
                % (
                    sys.executable,
                    sitl_usbip.__file__,
                    endpoint.unix_path or "%s:%u" % (args.host, endpoint.port),
                ),
                file=sys.stderr,
                flush=True,
            )
        if (
            sitl_usbip.find_tty(
                args.usbip_serial,
                timeout=10 if args.attach else 60,
                vid=endpoint.vid,
                pid=endpoint.pid,
            )
            is None
        ):
            print("no serial port appeared from the USB/IP controller", file=sys.stderr)
    print(stub.slave_path, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stub.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run published firmware through Renode and verify PWM/DShot/BDShot spin."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import threading
import time

from escsim.artifacts.catalog import ArtifactRepository
from escsim.control import dshot
from escsim.control.backend import DshotPanel, SimStream
from escsim.renode import download as renode_download
from escsim.renode.process import ProcessTree
from escsim.renode.session import generator_command, generator_environment


ROOT = Path(__file__).resolve().parents[1]


def built_native_library() -> Path | None:
    if os.name == "nt":
        name = "am32sim.dll"
    elif platform.system() == "Darwin":
        name = "libam32sim.dylib"
    else:
        name = "libam32sim.so"
    candidate = ROOT / "build" / f"package-native-{platform.system().lower()}" / name
    return candidate if candidate.is_file() else None


def free_port(tcp=False) -> int:
    kind = socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def wait_ready(lines: list[str], tree: ProcessTree, timeout=120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and tree.running():
        if any("input port on udp" in line for line in lines):
            return
        time.sleep(0.1)
    raise RuntimeError("Renode did not become ready:\n" + "\n".join(lines[-30:]))


def run_one(repository, renode, target, protocol, release) -> dict:
    installed = repository.install("firmware", release, target)
    input_port, state_port = free_port(), free_port()
    monitor_port = free_port(tcp=True)
    command = generator_command() + [
        target,
        "--link",
        "--gui-port",
        str(input_port),
        "--gui-state-port",
        str(state_port),
        "--monitor-port",
        str(monitor_port),
        "--elf",
        os.fspath(installed.image),
        "--targets-file",
        os.fspath(installed.targets_header),
        "--renode",
        os.fspath(renode),
    ]
    lines: list[str] = []
    environment = generator_environment()
    if not environment.get("ESCSIM_AM32SIM_LIBRARY"):
        native_library = built_native_library()
        if native_library is not None:
            environment["ESCSIM_AM32SIM_LIBRARY"] = os.fspath(native_library)
    tree = ProcessTree(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=environment,
    )

    def drain():
        if tree.process.stdout is not None:
            lines.extend(line.rstrip() for line in tree.process.stdout)

    threading.Thread(target=drain, daemon=True).start()
    ds = sim = None
    try:
        wait_ready(lines, tree)
        ds = DshotPanel("127.0.0.1", input_port)
        sim = SimStream("127.0.0.1", state_port, period_us=1000)
        sim.enabled = True
        ds.rate = 500
        if protocol == "pwm":
            ds.ptype = dshot.TYPE_PWM
            ds.value = 1000
        else:
            ds.ptype = dshot.TYPE_DSHOT600
            ds.value = 0
            ds.bidir = protocol == "bdshot"
        ds.enabled = True
        time.sleep(5)
        ds.value = 1500 if protocol == "pwm" else 800
        deadline = time.monotonic() + 15
        omega = 0.0
        while time.monotonic() < deadline:
            sample = sim.latest()
            omega = 0.0 if sample is None else abs(sample[1])
            if omega > 10 and (protocol != "bdshot" or ds.replies.count > 5):
                break
            time.sleep(0.2)
        if omega <= 10:
            raise RuntimeError(f"{target} {protocol} did not spin (omega={omega:.2f})")
        if protocol == "bdshot" and ds.replies.count <= 5:
            raise RuntimeError(f"{target} BDShot spun but returned no telemetry")
        return {
            "target": target,
            "protocol": protocol,
            "status": "passed",
            "omega_rad_s": omega,
            "rpm_reported": ds.rpm,
            "replies": ds.replies.count,
        }
    finally:
        if ds is not None:
            ds.running = False
        if sim is not None:
            sim.close()
        tree.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets", nargs="+", default=("VIMDRONES_L431", "TEKKO32_F415")
    )
    parser.add_argument(
        "--protocols",
        nargs="+",
        choices=("pwm", "dshot600", "bdshot"),
        default=("pwm", "dshot600", "bdshot"),
    )
    parser.add_argument("--firmware-release")
    parser.add_argument("--base-url")
    parser.add_argument("--renode")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repository = ArtifactRepository(args.base_url)
    catalog = repository.catalog(refresh=True)
    release = args.firmware_release or catalog["channels"]["stable"]["firmware"]
    renode = Path(args.renode) if args.renode else renode_download.install_current()[0]
    results = []
    failed = False
    for target in args.targets:
        for protocol in args.protocols:
            try:
                result = run_one(repository, renode, target, protocol, release)
            except Exception as error:
                failed = True
                result = {
                    "target": target,
                    "protocol": protocol,
                    "status": "failed",
                    "error": str(error),
                }
            results.append(result)
            print(json.dumps(result, sort_keys=True), flush=True)
    report = json.dumps({"firmware": release, "results": results}, indent=2) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

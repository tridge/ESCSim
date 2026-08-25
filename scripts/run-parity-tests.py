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
DEFAULT_TARGETS = ("VIMDRONES_L431", "TEKKO32_F415")


def representative_targets(manifest: dict) -> list[str]:
    """Return one deterministic non-CAN target for every published family."""

    representatives = {}
    for item in manifest["targets"]:
        if not item["dronecan"]:
            representatives.setdefault(item["family"].lower(), item["name"])
    return [representatives[family] for family in sorted(representatives)]


def built_native_library() -> Path | None:
    system = platform.system()
    if system == "Windows":
        name = "am32sim.dll"
    elif system == "Darwin":
        name = "libam32sim.dylib"
    else:
        name = "libam32sim.so"
    candidate = ROOT / "build" / f"package-native-{system.lower()}" / name
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


def run_one(repository, renode, target, protocol, release, image_format) -> dict:
    installed = repository.install(
        "firmware", release, target, image_format=image_format
    )
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
        arm_deadline = time.monotonic() + 45
        while time.monotonic() < arm_deadline:
            sim.request_info()
            time.sleep(0.25)
            if (sim.info or {}).get("armed"):
                break
        if not (sim.info or {}).get("armed"):
            runtime = sim.info or {}
            raise RuntimeError(
                f"{target} {protocol} did not arm "
                f"(pc=0x{runtime.get('pc', 0):08X}, "
                f"armed_count={runtime.get('armed_count', 'unknown')})"
            )
        ds.value = 1500 if protocol == "pwm" else 800
        deadline = time.monotonic() + 15
        next_info = 0.0
        omega = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_info:
                sim.request_info()
                next_info = now + 1.0
            sample = sim.latest()
            omega = 0.0 if sample is None else abs(sample[1])
            if omega > 10 and (protocol != "bdshot" or ds.replies.count > 5):
                break
            time.sleep(0.2)
        runtime = sim.info or {}
        runtime_status = "pc=0x%08X armed=%s armed_count=%s" % (
            runtime.get("pc", 0),
            runtime.get("armed", "unknown"),
            runtime.get("armed_count", "unknown"),
        )
        if omega <= 10:
            raise RuntimeError(
                f"{target} {protocol} did not spin "
                f"(omega={omega:.2f}, {runtime_status})"
            )
        if protocol == "bdshot" and ds.replies.count <= 5:
            raise RuntimeError(
                f"{target} BDShot spun but returned no telemetry ({runtime_status})"
            )
        return {
            "target": target,
            "protocol": protocol,
            "format": image_format,
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
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--targets", nargs="+")
    selection.add_argument(
        "--all-mcus",
        action="store_true",
        help="select one non-CAN target for every family in the release",
    )
    parser.add_argument(
        "--protocols",
        nargs="+",
        choices=("pwm", "dshot600", "bdshot"),
        default=("pwm", "dshot600", "bdshot"),
    )
    parser.add_argument(
        "--formats", nargs="+", choices=("elf", "hex"), default=("elf", "hex")
    )
    parser.add_argument("--firmware-release")
    parser.add_argument("--base-url")
    parser.add_argument("--renode")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repository = ArtifactRepository(args.base_url)
    catalog = repository.catalog(refresh=True)
    release = args.firmware_release or catalog["channels"]["stable"]["firmware"]
    targets = (
        representative_targets(repository.manifest("firmware", release))
        if args.all_mcus
        else args.targets or DEFAULT_TARGETS
    )
    renode = Path(args.renode) if args.renode else renode_download.install_current()[0]
    results = []
    failed = False
    for target in targets:
        for image_format in args.formats:
            for protocol in args.protocols:
                try:
                    result = run_one(
                        repository,
                        renode,
                        target,
                        protocol,
                        release,
                        image_format,
                    )
                except Exception as error:
                    failed = True
                    result = {
                        "target": target,
                        "protocol": protocol,
                        "format": image_format,
                        "status": "failed",
                        "error": str(error),
                    }
                results.append(result)
                print(json.dumps(result, sort_keys=True), flush=True)
    passed = sum(item["status"] == "passed" for item in results)
    report = (
        json.dumps(
            {
                "platform": platform.system(),
                "firmware": release,
                "targets": targets,
                "summary": {
                    "passed": passed,
                    "failed": len(results) - passed,
                    "total": len(results),
                },
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

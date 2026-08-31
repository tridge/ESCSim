#!/usr/bin/env python3
"""Run ESCSim and Chrome in a repeatable nested-X WebSerial test lab."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

from escsim.renode.flight_controller import (
    download_flight_controller_firmware,
    flight_controller_firmwares,
)
from escsim.settings import default_cache_dir


REPO = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--display", default=":91", help="nested X display")
    ap.add_argument("--parent-display", default=os.environ.get("DISPLAY"))
    ap.add_argument("--screen", default="1360x900")
    ap.add_argument("--control-port", type=int, default=47654)
    ap.add_argument("--chrome-debug-port", type=int, default=9229)
    ap.add_argument(
        "--url",
        default="https://app.betaflight.com/",
        help="WebSerial configurator URL to open",
    )
    ap.add_argument("--target", default="FOXEER_F421")
    ap.add_argument("--escs", type=int, default=4)
    ap.add_argument("--firmware", default="SPEEDYBEEF405V5")
    ap.add_argument(
        "--no-firmware-update",
        action="store_true",
        help="use the existing cache or bundled fallback instead of downloading",
    )
    ap.add_argument(
        "--run-dir",
        type=Path,
        default=REPO / "build" / "xephyr-webserial",
        help="stable directory for the Chrome profile and overwritten logs",
    )
    ap.add_argument("--chrome", help="Chrome/Chromium executable")
    return ap


def executable(explicit: str | None, names: tuple[str, ...]) -> str:
    if explicit:
        path = shutil.which(explicit) or explicit
        if Path(path).is_file():
            return path
        raise RuntimeError(f"executable not found: {explicit}")
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(f"install one of: {', '.join(names)}")


def line_buffered(command: list[str]) -> list[str]:
    stdbuf = shutil.which("stdbuf")
    return [stdbuf, "-oL", "-eL", *command] if stdbuf else command


def tcp_in_use(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", port)) == 0


def wait_for_control(port: int, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"ESCSim exited with status {process.returncode}")
        if tcp_in_use(port):
            return
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for the ESCSim control port")


def control(port: int, command: str, timeout: float = 75) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as client:
        client.settimeout(timeout)
        client.sendall((command + "\n").encode())
        reply = client.makefile("r", encoding="utf-8").readline().rstrip()
    if not reply:
        raise RuntimeError(f"no reply to control command: {command}")
    if reply.startswith("ERR"):
        raise RuntimeError(f"{command}: {reply}")
    return reply


def wait_for_flight_controller(port: int, timeout: float = 120) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = control(port, "status", timeout=5)
        if "running - flight controller:" in last:
            return last
        time.sleep(0.5)
    raise RuntimeError(f"flight controller did not start: {last}")


def select_target(port: int, target: str, timeout: float = 90) -> str:
    """Wait for the launcher's asynchronous target-source load."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return control(port, f"target {target}")
        except RuntimeError as error:
            if "ERR no target" not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def stop_group(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.parent_display:
        raise RuntimeError("DISPLAY is unset; pass --parent-display for Xephyr")
    try:
        display_number = int(args.display.removeprefix(":").split(".", 1)[0])
    except ValueError as error:
        raise RuntimeError(f"invalid display: {args.display}") from error
    if Path(f"/tmp/.X11-unix/X{display_number}").exists():
        raise RuntimeError(f"display {args.display} is already in use")
    if tcp_in_use(args.control_port):
        raise RuntimeError(f"control port {args.control_port} is already in use")
    if tcp_in_use(args.chrome_debug_port):
        raise RuntimeError(
            f"Chrome debug port {args.chrome_debug_port} is already in use"
        )

    xephyr = executable(None, ("Xephyr",))
    chrome = executable(
        args.chrome,
        ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    )
    args.run_dir = args.run_dir.expanduser().resolve()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    profile = args.run_dir / "chrome-profile"
    profile.mkdir(exist_ok=True)

    if not args.no_firmware_update and args.firmware in flight_controller_firmwares():
        print(f"Updating {args.firmware} from its configured firmware URL...", flush=True)
        download_flight_controller_firmware(args.firmware, default_cache_dir())

    logs = {
        name: (args.run_dir / f"{name}.log").open("w", buffering=1)
        for name in ("xephyr", "escsim", "chrome")
    }
    processes: dict[str, subprocess.Popen | None] = {
        "xephyr": None,
        "escsim": None,
        "chrome": None,
    }
    try:
        parent_env = os.environ.copy()
        parent_env["DISPLAY"] = args.parent_display
        processes["xephyr"] = subprocess.Popen(
            line_buffered(
                [
                    xephyr,
                    args.display,
                    "-screen",
                    args.screen,
                    "-ac",
                    "-br",
                    "-noreset",
                    # The nested display only needs software rendering.  Host
                    # GLX can otherwise load a mismatched proprietary EGL
                    # provider and crash Xephyr before ESCSim starts.
                    "-extension",
                    "GLX",
                ]
            ),
            env=parent_env,
            stdout=logs["xephyr"],
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        x_socket = Path(f"/tmp/.X11-unix/X{display_number}")
        deadline = time.monotonic() + 10
        while not x_socket.exists():
            if processes["xephyr"].poll() is not None:
                raise RuntimeError("Xephyr exited during startup")
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for Xephyr")
            time.sleep(0.1)

        nested_env = os.environ.copy()
        nested_env["DISPLAY"] = args.display
        # Run the GUI, generator children and Renode resource lookup from this
        # checkout. Without this, a src-layout checkout can silently mix the
        # current GUI with an older escsim package from site-packages.
        source_path = str(REPO / "src")
        existing_python_path = nested_env.get("PYTHONPATH")
        nested_env["PYTHONPATH"] = (
            source_path
            if not existing_python_path
            else os.pathsep.join((source_path, existing_python_path))
        )
        processes["escsim"] = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "escsim",
                "gui",
                "--control-port",
                str(args.control_port),
            ],
            cwd=REPO,
            env=nested_env,
            stdout=logs["escsim"],
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_for_control(args.control_port, processes["escsim"])
        print(f"> target {args.target}", flush=True)
        print(select_target(args.control_port, args.target), flush=True)
        for command in (
            "flightcontroller SpeedyBeeF405Mini",
            f"fcfirmware {args.firmware}",
            "conf usb",
            "protocol flightcontroller",
            f"escs {args.escs}",
            "start",
        ):
            print(f"> {command}", flush=True)
            print(control(args.control_port, command), flush=True)
        print(wait_for_flight_controller(args.control_port), flush=True)

        processes["chrome"] = subprocess.Popen(
            line_buffered(
                [
                    chrome,
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--disable-session-crashed-bubble",
                    # Xephyr deliberately has GLX disabled above; keep Chrome's
                    # native chooser and page compositing on the software path.
                    "--disable-gpu",
                    f"--remote-debugging-port={args.chrome_debug_port}",
                    "--remote-allow-origins=*",
                    f"--window-size={args.screen.replace('x', ',')}",
                    args.url,
                ]
            ),
            env=nested_env,
            stdout=logs["chrome"],
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(
            f"\nXephyr WebSerial lab is running on {args.display}.\n"
            f"Opened {args.url}\n"
            "Select 'Betaflight STM Electronics' once if Chrome asks, then Connect.\n"
            f"Chrome permissions persist in {profile}.\n"
            f"Logs are overwritten in {args.run_dir}. Press Ctrl-C to clean up.",
            flush=True,
        )
        while all(process.poll() is None for process in processes.values() if process):
            time.sleep(0.5)
        failed = next(
            name
            for name, process in processes.items()
            if process is not None and process.poll() is not None
        )
        raise RuntimeError(f"{failed} exited unexpectedly")
    except KeyboardInterrupt:
        return 0
    finally:
        if processes["escsim"] is not None and processes["escsim"].poll() is None:
            try:
                control(args.control_port, "stop", timeout=10)
                control(args.control_port, "quit", timeout=10)
            except (OSError, RuntimeError):
                pass
        for name in ("chrome", "escsim", "xephyr"):
            stop_group(processes[name])
        for log in logs.values():
            log.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)

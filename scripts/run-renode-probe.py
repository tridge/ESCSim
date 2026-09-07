#!/usr/bin/env python3
"""Run a development Renode probe with an enforced lifetime and cleanup."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from escsim.renode.generator import find_renode
from escsim.renode.process import ProcessTree


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORD_DIR = ROOT / "build" / "renode-probes"
MAX_TIMEOUT_SECONDS = 3600


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="hard lifetime in seconds (default 300, maximum 3600)",
    )
    ap.add_argument("--renode", help="Renode executable; default is verified cache")
    ap.add_argument(
        "--record-dir",
        type=Path,
        default=DEFAULT_RECORD_DIR,
        help="stable directory for active ownership metadata and latest.log",
    )
    ap.add_argument("renode_args", nargs=argparse.REMAINDER)
    return ap


def _linux_parent_death_signal(expected_parent: int):
    """Return a child hook that kills Renode if this supervisor is SIGKILLed."""

    def configure() -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL) != 0:  # PR_SET_PDEATHSIG
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        # Close the race in which the parent exits immediately before prctl().
        if os.getppid() != expected_parent:
            os.kill(os.getpid(), signal.SIGKILL)

    return configure


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def process_start_ticks(pid: int) -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        # The executable name in field 2 may contain spaces and parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19])  # field 22 after removing pid and comm
    except (OSError, ValueError, IndexError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 0 < args.timeout <= MAX_TIMEOUT_SECONDS:
        raise RuntimeError(
            f"--timeout must be greater than zero and at most {MAX_TIMEOUT_SECONDS}"
        )
    renode_args = list(args.renode_args)
    if renode_args[:1] == ["--"]:
        renode_args.pop(0)
    if not renode_args:
        raise RuntimeError("pass Renode arguments after --")

    record_dir = args.record_dir.expanduser().resolve()
    record_dir.mkdir(parents=True, exist_ok=True)
    active_path = record_dir / f"active-{os.getpid()}.json"
    latest_path = record_dir / "latest.json"
    log_path = record_dir / "latest.log"
    command = [find_renode(args.renode), *renode_args]
    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + args.timeout
    stop_signal: int | None = None

    def request_stop(signum, _frame) -> None:
        nonlocal stop_signal
        stop_signal = signum

    old_handlers = {}
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    for signum in handled_signals:
        old_handlers[signum] = signal.signal(signum, request_stop)

    tree = None
    reason = "launch-failed"
    exit_status = 1
    try:
        with log_path.open("w", encoding="utf-8", errors="replace", buffering=1) as log:
            kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": log,
                "stderr": subprocess.STDOUT,
            }
            if sys.platform.startswith("linux"):
                kwargs["preexec_fn"] = _linux_parent_death_signal(os.getpid())
            tree = ProcessTree(command, **kwargs)
            ownership = {
                "schema": 1,
                "owner": "ESCSim run-renode-probe",
                "supervisor_pid": os.getpid(),
                "renode_pid": tree.process.pid,
                "renode_pgid": tree.process.pid if os.name != "nt" else None,
                "renode_start_ticks": process_start_ticks(tree.process.pid),
                "started_utc": started.isoformat(),
                "timeout_seconds": args.timeout,
                "command": command,
                "log": str(log_path),
            }
            atomic_json(active_path, ownership)
            print(
                f"Renode probe PID {tree.process.pid}; timeout {args.timeout:g}s; "
                f"log {log_path}",
                flush=True,
            )
            while (
                tree.running() and stop_signal is None and time.monotonic() < deadline
            ):
                time.sleep(0.1)
            if stop_signal is not None:
                reason = f"signal-{signal.Signals(stop_signal).name}"
                exit_status = 128 + stop_signal
            elif tree.running():
                reason = "timeout"
                exit_status = 124
            else:
                reason = "exited"
                exit_status = tree.process.returncode or 0
    finally:
        if tree is not None:
            tree.stop(graceful_timeout=2, sweep_timeout=2)
        active_path.unlink(missing_ok=True)
        finished = datetime.now(timezone.utc)
        atomic_json(
            latest_path,
            {
                "schema": 1,
                "owner": "ESCSim run-renode-probe",
                "started_utc": started.isoformat(),
                "finished_utc": finished.isoformat(),
                "reason": reason,
                "exit_status": exit_status,
                "command": command,
                "log": str(log_path),
            },
        )
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return exit_status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)

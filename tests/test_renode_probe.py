from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "run-renode-probe.py"
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="process-group checks use /proc"
)


def fake_renode(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "renode"
    pid_file = tmp_path / "pids"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        "pathlib.Path(os.environ['FAKE_RENODE_PIDS']).write_text("
        "f'{os.getpid()} {child.pid}')\n"
        "print('fake Renode ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    executable.chmod(0o755)
    return executable, pid_file


def process_gone(pid: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def start_probe(tmp_path: Path, timeout: int | float):
    executable, pid_file = fake_renode(tmp_path)
    records = tmp_path / "records"
    environment = os.environ.copy()
    environment["FAKE_RENODE_PIDS"] = str(pid_file)
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROBE),
            "--renode",
            str(executable),
            "--timeout",
            str(timeout),
            "--record-dir",
            str(records),
            "--",
            "--disable-xwt",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            raise AssertionError("probe did not start")
        time.sleep(0.05)
    assert process.poll() is None
    pids = [int(value) for value in pid_file.read_text().split()]
    return process, pids, records


def assert_probe_cleaned(process, pids, records, expected_status, expected_reason):
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == expected_status, (stdout, stderr)
    assert all(process_gone(pid) for pid in pids)
    assert not list(records.glob("active-*.json"))
    latest = json.loads((records / "latest.json").read_text())
    assert latest["reason"] == expected_reason
    assert latest["exit_status"] == expected_status
    assert (records / "latest.log").read_text() == "fake Renode ready\n"


def test_probe_timeout_kills_the_complete_process_group(tmp_path):
    process, pids, records = start_probe(tmp_path, timeout=0.2)

    assert_probe_cleaned(process, pids, records, 124, "timeout")


def test_probe_signal_kills_the_complete_process_group(tmp_path):
    process, pids, records = start_probe(tmp_path, timeout=60)

    process.send_signal(signal.SIGTERM)
    assert_probe_cleaned(process, pids, records, 143, "signal-SIGTERM")

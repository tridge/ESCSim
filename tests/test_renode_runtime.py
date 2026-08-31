from __future__ import annotations

import os
import subprocess
import sys

import pytest

from escsim.renode import process as process_module
from escsim.renode.monitor import (
    clean_monitor_text,
    parse_elapsed,
    parse_metrics,
    startup_error,
)
from escsim.renode.process import ProcessTree
from escsim.renode.session import (
    SessionSpec,
    generator_command,
    generator_environment,
)


def test_elapsed_and_metrics_parsing():
    assert parse_elapsed("00:00:01.500") == 1.5
    assert parse_elapsed("2.03:04:05.5") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5.5
    text = """0x08001234
0x00000030
0x00123456
Elapsed Virtual Time: 00:00:12.500
Elapsed Host Time: 00:00:10.000
0x00000100
0x00000080
0x00000040
"""
    metrics = parse_metrics(text)
    assert metrics["pc"] == 0x08001234
    assert metrics["mips"] == 0x30
    assert metrics["virtual_seconds"] == 12.5
    assert metrics["host_seconds"] == 10.0
    assert metrics["dshot_frames"] == 256
    assert metrics["dshot_replies"] == 128
    assert metrics["dshot_injected"] == 64


def test_metrics_parser_rejects_ambiguous_extra_hex_values():
    text = """
0x08001234
0x00000030
0x00123456
Elapsed Virtual Time: 00:00:12.500
Elapsed Host Time: 00:00:10.000
0x00000001
"""
    with pytest.raises(ValueError, match="incomplete Renode monitor metrics"):
        parse_metrics(text)


def test_metrics_parser_reads_dshot_bridge_diagnostics():
    text = """0x08001234
0x00000030
0x00123456
Elapsed Virtual Time: 00:00:12.500
Elapsed Host Time: 00:00:10.000
0x00000100
0x00000080
0x00000040
0x0000A55A
0x000000F0
0x00000002
"""
    metrics = parse_metrics(text)
    assert metrics["dshot_last_frame"] == 0xA55A
    assert metrics["dshot_bidir_frames"] == 0xF0
    assert metrics["dshot_type"] == 2


def test_metrics_parser_reads_fourway_bridge_diagnostics():
    text = """0x08001234
0x00000030
0x00123456
Elapsed Virtual Time: 00:00:12.500
Elapsed Host Time: 00:00:10.000
0x00000100
0x00000080
0x00000040
0x0000A55A
0x000000F0
0x00000002
0x00000004
0x00000024
"""
    metrics = parse_metrics(text)
    assert metrics["serial_requests"] == 4
    assert metrics["serial_replies"] == 36


def test_metrics_parser_reads_per_motor_dshot_frames():
    text = """0x08001234
0x00000030
0x00123456
Elapsed Virtual Time: 00:00:12.500
Elapsed Host Time: 00:00:10.000
0x00000100
0x00000080
0x00000040
0x0000A55A
0x000000F0
0x00000002
0x00000004
0x00000024
0x00001234
0x00005678
0x00009ABC
0x0000DEF0
"""
    metrics = parse_metrics(text)
    assert metrics["dshot_motor_frames"] == (0x1234, 0x5678, 0x9ABC, 0xDEF0)


def test_monitor_text_and_startup_error():
    data = (
        b"\x1b[31mThere was an error executing command 'include'\x1b[0m\r\n"
        b"Error while loading ELF: bad file\r\n(am32) "
    )
    assert "\x1b" not in clean_monitor_text(data)
    assert startup_error(data) == "Error while loading ELF: bad file"
    assert startup_error("(am32) ") is None
    assert "error" in clean_monitor_text(bytearray(b"error\r\n"))


def test_process_tree_starts_and_stops():
    tree = ProcessTree(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    process = tree.process
    assert process.stdout.readline().strip() == "ready"
    assert tree.running()
    tree.stop(graceful_timeout=0.1, sweep_timeout=0.1)
    assert process.poll() is not None
    assert not tree.running()


def test_windows_process_windows_are_hidden(monkeypatch):
    class StartupInfo:
        dwFlags = 0
        wShowWindow = None

    monkeypatch.setattr(process_module.os, "name", "nt")
    monkeypatch.setattr(
        process_module.subprocess, "STARTUPINFO", StartupInfo, raising=False
    )
    monkeypatch.setattr(
        process_module.subprocess, "STARTF_USESHOWWINDOW", 0x00000001, raising=False
    )
    monkeypatch.setattr(process_module.subprocess, "SW_HIDE", 0, raising=False)

    startupinfo = process_module.hidden_process_startupinfo()

    assert startupinfo.dwFlags == 0x00000001
    assert startupinfo.wShowWindow == 0


def make_session_files(tmp_path):
    values = {}
    for name in (
        "firmware.elf",
        "eeprom.bin",
        "model.json",
        "bootloader.elf",
        "renode",
    ):
        path = tmp_path / name
        path.write_bytes(b"x")
        values[name] = path
    return values


def test_session_spec_builds_package_command(tmp_path):
    files = make_session_files(tmp_path)
    spec = SessionSpec(
        target="VIMDRONES_L431",
        firmware=files["firmware.elf"],
        eeprom=files["eeprom.bin"],
        model=files["model.json"],
        bootloader=files["bootloader.elf"],
        renode=files["renode"],
        can_bus=8,
    )
    spec.validate()
    command = spec.command()
    assert command[:3] == [sys.executable, "-m", "escsim.renode.generator"]
    assert "--bootloader-elf" in command
    assert command[command.index("--can-bus") + 1] == "8"


def test_session_spec_passes_historical_targets_header(tmp_path):
    files = make_session_files(tmp_path)
    header = tmp_path / "targets.h"
    header.write_text("snapshot")
    spec = SessionSpec(
        target="VIMDRONES_L431",
        firmware=files["firmware.elf"],
        eeprom=files["eeprom.bin"],
        model=files["model.json"],
        targets_header=header,
    )
    spec.validate()
    command = spec.command()
    assert command[command.index("--targets-file") + 1] == str(header)


def test_frozen_generator_command(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert generator_command() == [sys.executable, "--internal-generator"]


def test_generator_environment_supports_source_and_frozen(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/existing")
    environment = generator_environment()
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["PYTHONPATH"].endswith("/existing")
    assert "src" in environment["PYTHONPATH"].split(os.pathsep)[0]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert generator_environment()["PYTHONPATH"] == "/existing"


def test_session_spec_rejects_missing_files_and_ports(tmp_path):
    files = make_session_files(tmp_path)
    with pytest.raises(ValueError, match="firmware"):
        SessionSpec(
            target="X",
            firmware=tmp_path / "missing",
            eeprom=files["eeprom.bin"],
            model=files["model.json"],
        ).validate()
    with pytest.raises(ValueError, match="monitor port"):
        SessionSpec(
            target="X",
            firmware=files["firmware.elf"],
            eeprom=files["eeprom.bin"],
            model=files["model.json"],
            monitor_port=0,
        ).validate()

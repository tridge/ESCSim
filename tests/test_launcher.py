from __future__ import annotations

import socket
from types import SimpleNamespace

from escsim.gui import Lab


def test_emulator_ports_bound_requires_both_udp_ports():
    first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    first.bind(("127.0.0.1", 0))
    second.bind(("127.0.0.1", 0))
    args = SimpleNamespace(
        gui_port=first.getsockname()[1], state_port=second.getsockname()[1]
    )
    lab = object.__new__(Lab)
    lab.args = args
    try:
        assert lab.emulator_ports_bound()
        second.close()
        assert not lab.emulator_ports_bound()
    finally:
        first.close()
        second.close()


def test_four_way_instances_use_isolated_port_triplets():
    lab = object.__new__(Lab)
    lab.args = SimpleNamespace(gui_port=57833, state_port=57834, monitor_port=57835)
    lab.protocol = "4way"
    lab.esc_count = 8

    assert [lab.instance_ports(index) for index in range(lab.active_esc_count())] == [
        (57833, 57834, 57835),
        (57843, 57844, 57845),
        (57853, 57854, 57855),
        (57863, 57864, 57865),
        (57873, 57874, 57875),
        (57883, 57884, 57885),
        (57893, 57894, 57895),
        (57903, 57904, 57905),
    ]

    lab.protocol = "direct"
    assert lab.active_esc_count() == 1

    lab.protocol = "flightcontroller"
    assert lab.active_esc_count() == 8


def test_metrics_are_formatted_on_one_line_per_renode_instance():
    lab = object.__new__(Lab)
    lab.protocol = "flightcontroller"
    lab.esc_count = 2
    lab.fc_runner_required = True
    lab.info = {"app_base": 0x08001000}
    lab._all_emulators_running = lambda: True
    sample = {
        "pc": 0x08001234,
        "mips": 125,
        "instructions": 100,
        "virtual_seconds": 12.5,
        "host_seconds": 10.0,
        "speedup": 0.75,
        "executed_mips": 20.0,
    }
    lab.metrics = {
        "FC": dict(sample, pc=0x08010000),
        "ESC 1": sample,
    }

    assert lab.format_metrics_lines() == [
        "FC: PC 0x08010000 | 0.75x realtime | 20 of 125 MIPS | vt 12.5s",
        "ESC 1: PC 0x08001234 | 0.75x realtime | 20 of 125 MIPS | vt 12.5s",
        "ESC 2: waiting for PC and speedup...",
    ]


def test_four_way_start_launches_one_command_per_esc(tmp_path, monkeypatch):
    firmware = tmp_path / "firmware.elf"
    firmware.write_bytes(b"elf")
    args = SimpleNamespace(
        bootloader_dir=None,
        gui_port=57833,
        state_port=57834,
        monitor_port=57835,
        renode=None,
    )
    lab = Lab(args)
    lab.target = "TEST_TARGET"
    lab.info = {
        "family": "f051",
        "pin": "PA2",
        "dronecan": True,
        "app_base": 0x08001000,
    }
    lab.bootloader = "none"
    lab.firmware = str(firmware)
    lab.protocol = "4way"
    lab.esc_count = 8
    lab.conf = "off"

    class CapturingGroup:
        def __init__(self):
            self.commands = []

        def running(self):
            return False

        def start(self, commands, **_kwargs):
            self.commands = commands

        def stop(self):
            pass

    lab.runner = CapturingGroup()
    monkeypatch.setattr(lab, "wait_port_free", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lab, "_wait_ready", lambda *_args: None)

    try:
        assert lab.start() is None
        assert len(lab.runner.commands) == 8
        for index, command in enumerate(lab.runner.commands):
            assert command[command.index("--gui-port") + 1] == str(57833 + index * 10)
            assert command[command.index("--gui-state-port") + 1] == str(
                57834 + index * 10
            )
            assert command[command.index("--monitor-port") + 1] == str(
                57835 + index * 10
            )
            assert command[command.index("--can-node") + 1] == str(11 + index)
            assert command[command.index("--esc-index") + 1] == str(index)
            assert command[command.index("--outdir") + 1].endswith(
                "esc%u" % (index + 1)
            )
    finally:
        lab.stop()


def test_flight_controller_starts_selected_esc_count_with_loader(tmp_path, monkeypatch):
    firmware = tmp_path / "firmware.elf"
    bootloader = tmp_path / "bootloader.elf"
    firmware.write_bytes(b"app")
    bootloader.write_bytes(b"loader")
    args = SimpleNamespace(
        bootloader_dir=None,
        gui_port=58833,
        state_port=58834,
        monitor_port=58835,
        renode=None,
    )
    lab = Lab(args)
    lab.target = "TEST_TARGET"
    lab.info = {
        "family": "f421",
        "pin": "PB4",
        "dronecan": False,
        "app_base": 0x08001000,
    }
    lab.bootloader = str(bootloader)
    lab.firmware = str(firmware)
    lab.flight_controller = "SpeedyBeeF405Mini"
    lab.fc_boot_mode = "flash"
    lab.protocol = "flightcontroller"
    lab.esc_count = 1
    lab.conf = "off"

    class CapturingRunner:
        def __init__(self):
            self.commands = None
            self.command = None
            self.is_running = False

        def running(self):
            return self.is_running

        def all_running(self):
            return self.is_running

        def start(self, command, **_kwargs):
            if command and isinstance(command[0], list):
                self.commands = command
            else:
                self.command = command
            self.is_running = True

        def stop(self):
            self.is_running = False

    class FakeSpec:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def command(self):
            return ["renode-fc"]

    lab.runner = CapturingRunner()
    lab.fc_runner = CapturingRunner()
    monkeypatch.setenv("ESCSIM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("escsim.gui.FlightControllerSpec", FakeSpec)
    monkeypatch.setattr(lab, "wait_port_free", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lab, "_wait_ready", lambda *_args: None)

    try:
        assert lab.start() is None
        assert len(lab.runner.commands) == 1
        assert lab.fc_runner.command == ["renode-fc"]
        selected_flash = (
            tmp_path
            / "cache"
            / "flight-controllers"
            / "SpeedyBeeF405Mini"
            / "flash.bin"
        )
        assert selected_flash.read_bytes()[:8] == bytes.fromhex("f0ff001085240508")
        for command in lab.runner.commands:
            assert "--elf" in command
            assert str(firmware) in command
            assert "--bootloader-elf" in command
            assert str(bootloader) in command
        assert lab.fc_command is not None
    finally:
        lab.stop()

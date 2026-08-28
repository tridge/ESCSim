from __future__ import annotations

import socket
from types import SimpleNamespace

from escsim.gui import Lab


def test_emulator_ports_bound_requires_both_udp_ports(monkeypatch):
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
        assert not lab.packaged_ports_ready()
        monkeypatch.setattr("escsim.gui.sys.frozen", True, raising=False)
        monkeypatch.setattr("escsim.gui.sys.stdout", None)
        assert lab.packaged_ports_ready()
        second.close()
        assert not lab.emulator_ports_bound()
        assert not lab.packaged_ports_ready()
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

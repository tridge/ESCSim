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

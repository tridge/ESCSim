from __future__ import annotations

from escsim.control import fourway_server


def test_reset_uses_selected_esc_state_port(monkeypatch):
    sent = []

    class FakeSocket:
        def sendto(self, packet, address):
            sent.append((packet, address))

        def close(self):
            pass

    monkeypatch.setattr(fourway_server.socket, "socket", lambda *_args: FakeSocket())
    server = fourway_server.FourWayServer(
        esc_ports=[57833, 57843, 57853],
        state_ports=[57834, 57844, 57854],
    )

    server._reset_esc(2)

    assert sent == [(b"SS\t\x00", ("127.0.0.1", 57854))]


def test_state_port_count_must_match_esc_ports():
    try:
        fourway_server.FourWayServer(esc_ports=[57833, 57843], state_ports=[57834])
    except ValueError as error:
        assert str(error) == "state_ports must match esc_ports"
    else:
        raise AssertionError("mismatched state ports were accepted")

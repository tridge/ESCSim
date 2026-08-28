from __future__ import annotations

from escsim.control import fourway_server


def request(command, params=b"\0", address=0):
    body = (
        bytes(
            [
                fourway_server.REQ_MARK,
                command,
                address >> 8,
                address & 0xFF,
                len(params),
            ]
        )
        + params
    )
    crc = fourway_server.crc16_xmodem(body)
    return body + crc.to_bytes(2, "big")


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


def test_interface_exit_runs_every_connected_esc():
    class FakeClient:
        def __init__(self):
            self.run_count = 0

        def run(self):
            self.run_count += 1

    server = fourway_server.FourWayServer(esc_ports=[1, 2, 3])
    clients = [FakeClient(), FakeClient(), FakeClient()]
    server.clients = dict(enumerate(clients))
    server.connected = {0, 2}

    reply = server.feed(request(fourway_server.CMD_INTERFACE_EXIT))

    assert reply[0] == fourway_server.RESP_MARK
    assert reply[1] == fourway_server.CMD_INTERFACE_EXIT
    assert server.exited
    assert not server.connected
    assert [client.run_count for client in clients] == [1, 0, 1]

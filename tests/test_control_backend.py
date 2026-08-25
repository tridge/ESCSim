from __future__ import annotations

import socket
import struct
import time

import pytest

from escsim.control.backend import SimStream


def _receive_command(sock, command, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data, _addr = sock.recvfrom(128)
        if len(data) >= 4:
            magic, received, _flags = struct.unpack_from("<HBB", data)
            if magic == SimStream.MAGIC_CMD and received == command:
                return data
    pytest.fail("timed out waiting for simulation command %u" % command)


def test_speedup_is_retained_and_reapplied_when_state_stream_appears():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    sim = SimStream(port=server.getsockname()[1])
    try:
        sim.set_speedup(1.0)
        first = _receive_command(server, 2)
        assert struct.unpack_from("<f", first, 4)[0] == pytest.approx(1.0)

        # The panel may set its default before Renode binds the state port.
        # Its first state packet must trigger the retained pacing command.
        server.sendto(
            struct.pack("<HBB", SimStream.MAGIC_DATA, 2, 0),
            sim.sock.getsockname(),
        )
        second = _receive_command(server, 2)
        assert struct.unpack_from("<f", second, 4)[0] == pytest.approx(1.0)
    finally:
        sim.close()
        server.close()

from __future__ import annotations

from types import SimpleNamespace
import os

import pytest

from escsim.control import usbip


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.skipif(os.name == "nt", reason="abstract Unix socket test")
def test_server_close_releases_abstract_address_before_returning():
    address = "@escsim-usbip-close-%u" % os.getpid()
    server = usbip.UsbipServer(unix_path=address, serial="RESTART-TEST")
    server.close()

    assert not server.thread.is_alive()
    replacement = usbip.UsbipServer(unix_path=address, serial="RESTART-TEST")
    replacement.close()


def test_linux_attach_returns_exact_vhci_port_zero(monkeypatch):
    sock = FakeSocket()
    monkeypatch.setattr(usbip, "IS_WINDOWS", False)
    monkeypatch.setattr(usbip.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(usbip, "import_device", lambda *args: (sock, 0x10001, 2))
    monkeypatch.setattr(usbip, "attach_socket", lambda *args: 0)

    attached_port = usbip.attach(unix_path="@test")
    assert type(attached_port) is int
    assert attached_port == 0
    assert not sock.closed


def test_privileged_linux_attach_reports_exact_port_zero(monkeypatch):
    monkeypatch.setattr(usbip, "IS_WINDOWS", False)
    monkeypatch.setattr(usbip.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(usbip.os, "access", lambda *args: False)
    monkeypatch.setattr(usbip, "privilege_prefix", lambda: ["pkexec"])
    monkeypatch.setattr(
        usbip.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="0\n", stderr="attached on VHCI port 0\n"
        ),
    )

    attached_port = usbip.attach(unix_path="@test")
    assert type(attached_port) is int
    assert attached_port == 0


def test_fallback_cleanup_uses_local_usb_identity(tmp_path, monkeypatch):
    vhci = tmp_path / "vhci"
    vhci.mkdir()
    (vhci / "status").write_text(
        "hub port sta spd dev sockfd local_busid\n"
        "hs 0000 006 002 00010001 000107 7-1\n"
        "hs 0001 006 002 00010001 000108 7-2\n"
        "hs 0002 004 000 00000000 000000 0-0\n"
    )
    monkeypatch.setattr(usbip, "VHCI", str(vhci))
    monkeypatch.setattr(
        usbip,
        "_usb_identity",
        lambda busid: (
            (usbip.MANUFACTURER, usbip.PRODUCT)
            if busid == "7-2"
            else ("Other", "Device")
        ),
    )

    assert usbip._our_vhci_ports() == [1]

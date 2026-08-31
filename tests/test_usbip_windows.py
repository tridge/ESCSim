from __future__ import annotations

from types import SimpleNamespace
import sys
import threading

import pytest

from escsim.control import usbip


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_windows_server_uses_automatic_loopback_port(monkeypatch):
    monkeypatch.setattr(usbip, "IS_WINDOWS", True)
    server = usbip.UsbipServer(serial="WINDOWS-TEST")
    try:
        assert server.unix_path is None
        assert server.host == "127.0.0.1"
        assert server.port > 0
    finally:
        server.close()


def test_server_close_releases_listener_before_returning():
    server = usbip.UsbipServer(port=0, serial="RESTART-TEST")
    port = server.port
    server.close()

    assert not server.thread.is_alive()
    replacement = usbip.UsbipServer(port=port, serial="RESTART-TEST")
    replacement.close()


def test_windows_attach_returns_owned_port(monkeypatch):
    calls = []
    monkeypatch.setattr(
        usbip,
        "_windows_usbip_checked",
        lambda: (r"C:\Program Files\USBip\usbip.exe", (0, 9, 7, 7)),
    )
    monkeypatch.setattr(
        usbip.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs)) or completed(stdout="12\n")
        ),
    )
    assert usbip._windows_attach("127.0.0.1", 49152, "1-1") == 12
    assert "--once" in calls[0][0]


def test_windows_attach_rejects_noisy_output(monkeypatch):
    monkeypatch.setattr(
        usbip, "_windows_usbip_checked", lambda: ("usbip.exe", (0, 9, 7, 7))
    )
    monkeypatch.setattr(
        usbip.subprocess,
        "run",
        lambda *args, **kwargs: completed(stdout="attached to port 2\n"),
    )
    with pytest.raises(RuntimeError, match="attach failed"):
        usbip._windows_attach("127.0.0.1", 49152, "1-1")


def test_windows_attach_recommends_reboot_for_stopped_host_controller(monkeypatch):
    monkeypatch.setattr(
        usbip, "_windows_usbip_checked", lambda: ("usbip.exe", (0, 9, 7, 7))
    )
    monkeypatch.setattr(
        usbip.subprocess,
        "run",
        lambda *args, **kwargs: completed(
            stderr="error: VHCI device not found, driver not loaded?", returncode=1
        ),
    )
    with pytest.raises(RuntimeError, match="reboot Windows"):
        usbip._windows_attach("127.0.0.1", 49152, "1-1")


def test_unsafe_usbip_win2_release_is_rejected(monkeypatch):
    monkeypatch.setattr(usbip, "windows_usbip_executable", lambda: "usbip.exe")
    monkeypatch.setattr(
        usbip, "windows_usbip_version", lambda _executable: (0, 9, 7, 8)
    )
    with pytest.raises(RuntimeError, match="unsafe"):
        usbip._windows_usbip_checked()


@pytest.mark.parametrize("port", [None, True, False])
def test_windows_detach_requires_exact_owned_port(port):
    with pytest.raises(RuntimeError, match="refusing to detach"):
        usbip._windows_detach(port)


def test_windows_com_discovery_matches_identity(monkeypatch):
    ports = [
        SimpleNamespace(
            device="COM8",
            vid=usbip.VENDOR_ID,
            pid=usbip.PRODUCT_ID,
            serial_number="OTHER",
        ),
        SimpleNamespace(
            device="COM12",
            vid=usbip.VENDOR_ID,
            pid=usbip.PRODUCT_ID,
            serial_number="RENODE",
        ),
    ]
    fake_list_ports = SimpleNamespace(comports=lambda: ports)
    monkeypatch.setattr(usbip, "IS_WINDOWS", True)
    monkeypatch.setitem(
        sys.modules, "serial.tools", SimpleNamespace(list_ports=fake_list_ports)
    )
    assert usbip.find_tty("RENODE", timeout=0) == "COM12"


def test_windows_firmware_com_discovery_uses_pre_attach_snapshot(monkeypatch):
    ports = [
        SimpleNamespace(device="COM8"),
        SimpleNamespace(device="COM12"),
    ]
    fake_list_ports = SimpleNamespace(comports=lambda: ports)
    monkeypatch.setattr(usbip, "IS_WINDOWS", True)
    monkeypatch.setitem(
        sys.modules, "serial.tools", SimpleNamespace(list_ports=fake_list_ports)
    )

    assert usbip.find_new_tty({"COM8"}, timeout=0) == "COM12"


def test_bulk_out_completion_reports_consumed_length():
    server = object.__new__(usbip.UsbipServer)
    server.rx = b""
    server.rx_max = 65536
    server.rx_lock = threading.Condition()
    server.send_lock = threading.Lock()
    calls = []
    server._ret_submit = lambda *args, **kwargs: calls.append((args, kwargs))
    server._submit(17, usbip.DIR_OUT, usbip.EP_BULK, 5, b"", b"hello")
    assert server.rx == b"hello"
    assert calls == [((17, usbip.ST_OK), {"actual_length": 5})]

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
    commands = []
    monkeypatch.setattr(usbip, "IS_WINDOWS", False)
    monkeypatch.setattr(usbip.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(usbip.os, "access", lambda *args: False)
    monkeypatch.setattr(usbip, "privilege_prefix", lambda: ["pkexec"])

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0, stdout="0\n", stderr="attached on VHCI port 0\n"
        )

    monkeypatch.setattr(usbip.subprocess, "run", run)

    attached_port = usbip.attach(unix_path="@test", busid="1-0")
    assert type(attached_port) is int
    assert attached_port == 0
    assert commands[0][-2:] == ["--busid", "1-0"]


def test_udev_rule_names_the_selected_serial_group():
    rule = usbip.udev_rule("uucp")
    assert "chgrp uucp /sys%p/attach /sys%p/detach" in rule
    assert 'KERNEL=="vhci_hcd.0"' in rule
    assert "%%" not in rule


def test_serial_group_prefers_one_the_caller_belongs_to():
    groups = {
        "dialout": SimpleNamespace(gr_name="dialout", gr_gid=20),
        "uucp": SimpleNamespace(gr_name="uucp", gr_gid=30),
    }

    assert usbip.serial_group({30}, groups.__getitem__) == "uucp"
    assert usbip.serial_group(set(), groups.__getitem__) == "dialout"


def test_install_rules_passes_callers_serial_group_to_root(monkeypatch):
    commands = []
    monkeypatch.setattr(usbip.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(usbip, "serial_group", lambda: "uucp")
    monkeypatch.setattr(usbip, "privilege_prefix", lambda: ["pkexec"])
    monkeypatch.setattr(
        usbip.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert usbip.install_rules()
    assert commands == [
        [
            "pkexec",
            usbip.sys.executable,
            usbip.os.path.abspath(usbip.__file__),
            "--install-rules",
            "--serial-group",
            "uucp",
        ]
    ]


def test_ensure_vhci_loads_missing_module(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(usbip, "VHCI", str(tmp_path / "vhci_hcd.0"))
    monkeypatch.setattr(
        usbip.subprocess,
        "run",
        lambda cmd, **kwargs: commands.append(cmd) or SimpleNamespace(returncode=0),
    )

    usbip._ensure_vhci()
    assert commands == [["modprobe", "vhci_hcd"]]

    commands.clear()
    (tmp_path / "vhci_hcd.0").mkdir()
    usbip._ensure_vhci()
    assert commands == []


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


def test_exact_vhci_port_liveness(tmp_path, monkeypatch):
    vhci = tmp_path / "vhci"
    vhci.mkdir()
    (vhci / "status").write_text(
        "hub port sta spd dev sockfd local_busid\n"
        "hs 0000 006 002 00010001 000107 7-1\n"
        "hs 0001 004 000 00000000 000000 0-0\n"
    )
    monkeypatch.setattr(usbip, "VHCI", str(vhci))
    monkeypatch.setattr(usbip, "IS_WINDOWS", False)

    assert usbip.port_attached(0)
    assert not usbip.port_attached(1)
    assert not usbip.port_attached(7)


def test_find_tty_for_exact_vhci_port(tmp_path, monkeypatch):
    vhci = tmp_path / "vhci"
    vhci.mkdir()
    (vhci / "status").write_text(
        "hub port sta spd dev sockfd local_busid\nhs 0002 006 002 00010001 000107 7-1\n"
    )
    sys_usb = tmp_path / "sys-usb"
    tty_dir = sys_usb / "7-1:1.0" / "tty"
    tty_dir.mkdir(parents=True)
    (tty_dir / "ttyACM9").touch()
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyACM9").touch()
    by_id = dev / "serial" / "by-id"
    by_id.mkdir(parents=True)
    stable = by_id / "usb-ArduPilot_emulated-if00"
    stable.symlink_to("../../ttyACM9")
    monkeypatch.setattr(usbip, "VHCI", str(vhci))
    monkeypatch.setattr(usbip, "SYS_USB_DEVICES", str(sys_usb))
    monkeypatch.setattr(usbip, "DEV_ROOT", str(dev))
    monkeypatch.setattr(usbip, "IS_WINDOWS", False)

    assert usbip.find_tty_on_port(2, timeout=0) == str(stable)
    assert usbip.find_tty_on_port(3, timeout=0) is None

from __future__ import annotations

import threading
from types import SimpleNamespace

from escsim import gui


class FakeRunner:
    def __init__(self, running=False):
        self.is_running = running
        self.stop_calls = 0

    def running(self):
        return self.is_running

    def stop(self):
        self.stop_calls += 1
        self.is_running = False


class FakeStub:
    def __init__(self, *args, endpoint=None, **kwargs):
        self.endpoint = endpoint
        self.closed = False
        self.slave_path = "fake-pty"

    def close(self):
        self.closed = True


def make_lab():
    args = SimpleNamespace(
        bootloader_dir=None,
        gui_port=57833,
        state_port=57834,
        monitor_port=57835,
        renode=None,
    )
    lab = gui.Lab(args)
    lab.runner = FakeRunner()
    return lab


def test_failed_usb_detach_is_retained_and_retried(monkeypatch):
    lab = make_lab()
    stub = FakeStub()
    lab.stub = stub
    lab._remember_usb(12)

    def fail(_port):
        raise RuntimeError("driver busy")

    monkeypatch.setattr(gui.sitl_usbip, "detach", fail)
    error = lab._stop_stub()

    assert str(error) == "driver busy"
    assert stub.closed
    assert lab.usb_attached
    assert lab.usb_ports == {12}

    detached = []
    monkeypatch.setattr(
        gui.sitl_usbip, "detach", lambda port: detached.append(port) or True
    )
    assert lab._stop_stub() is None
    assert detached == [12]
    assert not lab.usb_attached
    assert lab.usb_ports == set()


def test_stop_cancels_in_progress_usb_attach(monkeypatch):
    lab = make_lab()
    lab.runner = FakeRunner(running=True)
    lab.conf = "usb"
    lab.protocol = "direct"
    lab.generation = 4
    attach_entered = threading.Event()
    release_attach = threading.Event()
    detached = []
    stubs = []

    class FakeEndpoint:
        def __init__(self, **_kwargs):
            self.unix_path = None
            self.host = "127.0.0.1"
            self.port = 49152
            self.vid = 0x1209
            self.pid = 1

    def make_stub(*args, **kwargs):
        stub = FakeStub(*args, **kwargs)
        stubs.append(stub)
        return stub

    def attach(**_kwargs):
        attach_entered.set()
        assert release_attach.wait(2)
        return 7

    monkeypatch.setattr(gui.sitl_usbip, "UsbipServer", FakeEndpoint)
    monkeypatch.setattr(gui.msp_stub_fc, "DirectBridge", make_stub)
    monkeypatch.setattr(gui.sitl_usbip, "attach", attach)
    monkeypatch.setattr(gui.sitl_usbip, "find_tty", lambda *args, **kwargs: "COM7")
    monkeypatch.setattr(
        gui.sitl_usbip, "detach", lambda port: detached.append(port) or True
    )

    worker = threading.Thread(target=lab._start_stub, args=(4,))
    worker.start()
    assert attach_entered.wait(2)
    lab.stop()
    assert lab.start() == "previous USB startup is still being cancelled"
    release_attach.set()
    worker.join(2)

    assert not worker.is_alive()
    assert detached == [7]
    assert stubs[0].closed
    assert lab.stub is None
    assert not lab.usb_attached
    assert lab.status == "stopped"


def test_unexpected_emulator_exit_detaches_usb(monkeypatch):
    lab = make_lab()
    lab.status = "running - configurator port: COM9"
    stub = FakeStub()
    lab.stub = stub
    lab._remember_usb(9)
    detached = []
    monkeypatch.setattr(
        gui.sitl_usbip, "detach", lambda port: detached.append(port) or True
    )

    lab.saw_log_line("[emulator exited, status 2]")

    assert detached == [9]
    assert stub.closed
    assert not lab.usb_attached
    assert lab.status == "emulator exited, status 2"

from __future__ import annotations

import queue
import threading
import time
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


def test_flight_controller_metrics_do_not_claim_a_bootloader():
    lab = make_lab()
    metrics = {
        "pc": 0x08001000,
        "mips": 200,
        "virtual_seconds": 1.0,
    }

    assert "bootloader" not in lab._format_instance_metrics("FC", metrics)


def test_process_group_stop_is_serialized():
    entered = threading.Event()
    release = threading.Event()

    class BlockingRunner:
        def __init__(self):
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1
            entered.set()
            assert release.wait(2)

    runner = BlockingRunner()
    group = gui.ProcGroup(queue.Queue())
    group.runners = [runner]
    first = threading.Thread(target=group.stop)
    second = threading.Thread(target=group.stop)
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert runner.stop_calls == 1


def test_process_runner_stop_is_serialized():
    entered = threading.Event()
    release = threading.Event()

    class BlockingTree:
        def __init__(self):
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1
            entered.set()
            assert release.wait(2)

    runner = gui.ProcRunner(queue.Queue())
    runner.proc = object()
    runner.tree = BlockingTree()
    first = threading.Thread(target=runner.stop)
    second = threading.Thread(target=runner.stop)
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert runner.tree is None


def test_process_runner_writes_line_buffered_process_log(tmp_path, monkeypatch):
    class FakeProcess:
        stdout = iter(("first line\n", "last line\n"))

        @staticmethod
        def wait():
            return 7

    class FakeTree:
        def __init__(self, *_args, **kwargs):
            assert kwargs["bufsize"] == 1
            assert kwargs["stderr"] == gui.subprocess.STDOUT
            self.process = FakeProcess()

    monkeypatch.setattr(gui, "ProcessTree", FakeTree)
    output = queue.Queue()
    log_path = tmp_path / "logs" / "esc1.log"
    runner = gui.ProcRunner(output)
    runner.start(["renode"], log_path=log_path)

    deadline = time.monotonic() + 2
    while output.qsize() < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert [output.get_nowait() for _index in range(3)] == [
        "first line",
        "last line",
        "[emulator exited, status 7]",
    ]
    assert log_path.read_text() == (
        "first line\nlast line\n[emulator exited, status 7]\n"
    )


def test_stop_interrupts_metrics_and_pauses_on_fresh_monitor(monkeypatch):
    lab = make_lab()
    lab.fc_runner = FakeRunner(running=True)
    lab.protocol = "flightcontroller"
    commands = []

    class MetricsMonitor:
        def close(self):
            commands.append(("metrics-close",))

    class StopMonitor:
        def __init__(self, host, port):
            commands.append(("create", host, port))

        def connect(self, timeout):
            commands.append(("connect", timeout))

        def command(self, command, timeout):
            commands.append((command, timeout))
            return "(monitor)"

        def close(self):
            commands.append(("close",))

    monkeypatch.setattr(gui.renode_monitor, "MonitorClient", StopMonitor)
    lab.fc_monitor = MetricsMonitor()
    lab.stop()

    assert commands == [
        ("metrics-close",),
        ("create", "127.0.0.1", lab.fc_monitor_port()),
        ("connect", 2),
        ("pause", 10),
        ("close",),
    ]
    assert lab.fc_runner.stop_calls == 1


def test_stop_reconnects_monitor_to_persist_flash(monkeypatch):
    lab = make_lab()
    lab.fc_runner = FakeRunner(running=True)
    commands = []

    class FakeMonitor:
        def __init__(self, host, port):
            commands.append(("create", host, port))

        def connect(self, timeout):
            commands.append(("connect", timeout))

        def command(self, command, timeout):
            commands.append((command, timeout))

        def close(self):
            commands.append(("close",))

    monkeypatch.setattr(gui.renode_monitor, "MonitorClient", FakeMonitor)
    lab.stop()

    assert commands == [
        ("create", "127.0.0.1", lab.fc_monitor_port()),
        ("connect", 2),
        ("pause", 10),
        ("close",),
    ]


def test_failed_usb_detach_is_retained_and_retried(monkeypatch):
    lab = make_lab()
    stub = FakeStub()
    lab.stub = stub
    lab._remember_usb(12)

    def fail(_port):
        raise RuntimeError("driver busy")

    monkeypatch.setattr(gui.sitl_usbip, "port_attached", lambda _port: True)
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


def test_already_disconnected_usb_port_is_forgotten(monkeypatch):
    lab = make_lab()
    lab._remember_usb(12)
    detached = []
    monkeypatch.setattr(gui.sitl_usbip, "port_attached", lambda _port: False)
    monkeypatch.setattr(
        gui.sitl_usbip, "detach", lambda port: detached.append(port) or True
    )

    assert lab._stop_stub() is None
    assert detached == []
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
    monkeypatch.setattr(gui.sitl_usbip, "port_attached", lambda _port: True)

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
    monkeypatch.setattr(gui.sitl_usbip, "port_attached", lambda _port: True)

    lab.saw_log_line("[emulator exited, status 2]")

    assert detached == [9]
    assert stub.closed
    assert not lab.usb_attached
    assert lab.status == "emulator exited, status 2"


def test_emulator_exit_during_multi_start_does_not_leave_starting_status():
    lab = make_lab()
    lab.runner = FakeRunner(running=False)
    lab.status = "starting 8 emulators..."

    lab.saw_log_line("[ESC 3] [emulator exited, status 2]")

    assert lab.status == "emulator exited, status 2"


def test_fourway_stub_enables_msp_motor_output(monkeypatch):
    lab = make_lab()
    lab.runner = FakeRunner(running=True)
    lab.conf = "serial"
    lab.protocol = "4way"
    lab.esc_count = 3
    lab.generation = 7
    created = []

    def make_stub(*_args, **kwargs):
        created.append(kwargs)
        return FakeStub()

    monkeypatch.setattr(gui.msp_stub_fc, "MspStubFC", make_stub)

    lab._start_stub(7)

    assert created[0]["motor"] is True
    assert created[0]["esc_ports"] == [57833, 57843, 57853]
    assert lab.stub is not None
    lab._stop_stub()


def test_cancelled_firmware_usb_attach_is_detached(monkeypatch):
    lab = make_lab()
    lab.fc_runner = FakeRunner(running=True)
    lab.generation = 3
    detached = []

    def attach(**_kwargs):
        lab.generation = 4
        return 6

    monkeypatch.setattr(lab, "_wait_fc_usb_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(gui.sitl_usbip, "attach", attach)
    monkeypatch.setattr(lab, "_find_fc_tty", lambda **_kwargs: None)
    monkeypatch.setattr(
        gui.sitl_usbip, "detach", lambda port: detached.append(port) or True
    )
    monkeypatch.setattr(gui.sitl_usbip, "port_attached", lambda _port: True)

    lab._attach_firmware_usb(3)

    assert detached == [6]
    assert lab.usb_ports == set()


def test_firmware_usb_waits_for_connected_device_before_attach(monkeypatch):
    lab = make_lab()
    lab.fc_runner = FakeRunner(running=True)
    lab.generation = 3
    events = []
    responses = iter(("0x00000000\n(monitor)", "0x00000001\n(monitor)"))

    class FakeMonitor:
        def __init__(self, host, port):
            events.append(("monitor", host, port))

        def connect(self, timeout):
            events.append(("connect", timeout))

        def command(self, command, timeout):
            events.append(("command", command, timeout))
            return next(responses)

        def close(self):
            events.append(("close",))

    def attach(**_kwargs):
        events.append(("attach",))
        lab.generation = 4
        return 6

    monkeypatch.setattr(gui.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(gui.sitl_usbip, "attach", attach)
    monkeypatch.setattr(lab, "_find_fc_tty", lambda **_kwargs: None)
    monkeypatch.setattr(gui.sitl_usbip, "detach", lambda _port: True)
    monkeypatch.setattr(gui.sitl_usbip, "port_attached", lambda _port: True)

    monitor = FakeMonitor("127.0.0.1", lab.fc_monitor_port())
    lab._attach_firmware_usb(3, monitor=monitor)

    commands = [event for event in events if event[0] == "command"]
    assert commands == [
        ("command", gui.FC_USB_STATE_COMMAND, 10),
        ("command", gui.FC_USB_STATE_COMMAND, 10),
    ]
    assert events.index(("attach",)) > events.index(commands[-1])


def test_flight_controller_finds_windows_com_port(monkeypatch):
    calls = []

    def find_new_tty(previous, **kwargs):
        calls.append((previous, kwargs))
        return "COM26"

    monkeypatch.setattr(gui.os, "name", "nt")
    monkeypatch.setattr(gui.sitl_usbip, "find_new_tty", find_new_tty)

    assert (
        gui.Lab._find_fc_tty(
            timeout=7, usb_port=1, previous_ttys={"COM5", "COM9"}
        )
        == "COM26"
    )
    assert calls == [
        ({"COM5", "COM9"}, {"timeout": 7}),
    ]


def test_fc_dfu_requires_usb_configurator(tmp_path, monkeypatch):
    firmware = tmp_path / "firmware.elf"
    firmware.write_bytes(b"elf")
    lab = make_lab()
    lab.target = "TEST_TARGET"
    lab.info = {
        "family": "f051",
        "pin": "PA2",
        "dronecan": False,
        "app_base": 0x08001000,
    }
    lab.bootloader = "none"
    lab.firmware = str(firmware)
    lab.flight_controller = "SpeedyBeeF405Mini"
    lab.fc_boot_mode = "dfu"
    lab.conf = "off"

    assert lab.start() == "flight-controller DFU mode requires USB"

import struct
import threading
from types import SimpleNamespace

import pytest

from escsim.control import dshot, msp_stub_fc


def make_stub(count):
    stub = object.__new__(msp_stub_fc.MspStubFC)
    stub.fourway = SimpleNamespace(esc_count=count, begin=lambda: None)
    stub.poles = 14
    stub.motors = [msp_stub_fc.MotorState() for _index in range(count)]
    stub.motor_lock = threading.Lock()
    stub.motor_output_enabled = False
    stub.motor_active = False
    stub.motor_arming_until = 0.0
    stub.in_fourway = False
    stub.freeze = False
    stub.arming_probe = None
    stub._log = lambda _message: None
    stub._reply_payloads = []
    stub._reply = lambda command, payload=b"": stub._reply_payloads.append(
        (command, payload)
    )
    return stub


def test_motor_metadata_and_telemetry_match_fourway_esc_count():
    stub = make_stub(8)
    stub.motors[0].rpm = 1234
    stub.motors[0].invalid = 0.5
    stub.motors[0].temp = 30
    stub.motors[0].volt_raw = 48
    stub.motors[0].curr_raw = 6
    stub.motors[3].rpm = 4321
    stub.motors[3].invalid = 1.25

    stub._handle(msp_stub_fc.MSP_MOTOR_CONFIG, b"")
    stub._handle(msp_stub_fc.MSP_MOTOR_TELEMETRY, b"")

    assert stub._reply_payloads[0][1][6] == 8
    telemetry = stub._reply_payloads[1][1]
    assert telemetry[0] == 8
    assert len(telemetry) == 1 + 8 * 13
    assert struct.unpack_from("<IH", telemetry, 1) == (1234, 50)
    assert struct.unpack_from("<IH", telemetry, 1 + 3 * 13) == (4321, 125)


def test_set_motor_routes_all_simulated_escs_and_msp_motor_reports_them():
    stub = make_stub(6)
    requested = [1000, 1100, 1250, 1500, 1750, 2000, 1900, 1800]

    stub._handle(msp_stub_fc.MSP_SET_MOTOR, struct.pack("<8H", *requested))
    assert stub._motor_snapshot(stub.motor_arming_until - 0.01) == [1000] * 6
    assert stub._motor_snapshot(stub.motor_arming_until) == requested[:6]

    stub._handle(msp_stub_fc.MSP_MOTOR, b"")
    reported = struct.unpack("<8H", stub._reply_payloads[-1][1])
    assert reported == tuple(requested[:6] + [0, 0])


def test_motor_control_pauses_for_fourway_and_holds_last_value():
    stub = make_stub(4)
    stub._handle(msp_stub_fc.MSP_SET_MOTOR, struct.pack("<4H", 1200, 1300, 1400, 1500))
    stub._handle(msp_stub_fc.MSP_SET_PASSTHROUGH, b"")
    assert stub.in_fourway
    assert not stub.motor_output_enabled
    assert not stub.motor_active
    assert stub.motor_arming_until == 0
    assert [motor.value for motor in stub.motors] == [1000] * 4
    assert stub._motor_snapshot() is None

    stub.in_fourway = False
    stub._handle(msp_stub_fc.MSP_SET_MOTOR, struct.pack("<H", 1600))
    assert stub._motor_snapshot(stub.motor_arming_until + 60.0) == [
        1600,
        1000,
        1000,
        1000,
    ]
    assert stub.motor_active
    assert stub.motor_output_enabled
    assert [motor.value for motor in stub.motors] == [1600, 1000, 1000, 1000]


def test_motor_control_waits_for_observed_renode_arming():
    class Probe:
        armed = [False, False]

        def poll(self, _now):
            return self.armed

    stub = make_stub(2)
    probe = Probe()
    stub.arming_probe = probe
    stub._handle(msp_stub_fc.MSP_SET_MOTOR, struct.pack("<2H", 1400, 1500))

    assert stub._motor_snapshot(10.0) == [1000, 1000]
    probe.armed = [True, False]
    assert stub._motor_snapshot(10.1) == [1000, 1000]
    probe.armed = [True, True]
    assert stub._motor_snapshot(10.2) == [1400, 1500]


def test_motor_control_uses_time_fallback_without_renode_info():
    class Probe:
        def poll(self, _now):
            return [None]

    stub = make_stub(1)
    stub.arming_probe = Probe()
    stub._handle(msp_stub_fc.MSP_SET_MOTOR, struct.pack("<H", 1400))

    assert stub._motor_snapshot(stub.motor_arming_until - 0.01) == [1000]
    assert stub._motor_snapshot(stub.motor_arming_until) == [1400]


def test_renode_arming_probe_parses_info_for_each_state_port(monkeypatch):
    replies = [
        (struct.pack("<HBB4I", 0x5359, 9, 4, 0, 0, 0, 0), ("127.0.0.1", 1)),
        (struct.pack("<HBB4I", 0x5359, 9, 0, 0, 0, 0, 0), ("127.0.0.1", 2)),
    ]

    class FakeSocket:
        def bind(self, _address):
            pass

        def setblocking(self, _blocking):
            pass

        def sendto(self, _data, _address):
            pass

        def recvfrom(self, _size):
            if replies:
                return replies.pop(0)
            raise BlockingIOError

        def close(self):
            pass

    monkeypatch.setattr(msp_stub_fc.socket, "socket", lambda *_args: FakeSocket())
    probe = msp_stub_fc.RenodeArmingProbe("127.0.0.1", [1, 2])

    assert probe.poll(1.0) == [True, False]


def test_dshot_cycle_sends_each_motor_value_to_its_own_port():
    class FakePort:
        def __init__(self):
            self.sent = []

        def send_dshot(self, value, **kwargs):
            self.sent.append((value, kwargs))

    stub = make_stub(3)
    stub.ports = [FakePort(), FakePort(), FakePort()]
    values = [1100, 1500, 2000]
    stub.motor_output_enabled = True
    stub.motor_active = True

    stub._send_motor_cycle(values, now=0.1)

    for index, port in enumerate(stub.ports):
        assert port.sent == [
            (
                stub._dshot_value(values[index]),
                {"ptype": dshot.TYPE_DSHOT600, "bidir": True},
            )
        ]


def test_passthrough_transition_waits_for_inflight_dshot_cycle():
    send_started = threading.Event()
    release_send = threading.Event()
    passthrough_started = threading.Event()

    class BlockingPort:
        def __init__(self):
            self.sent = 0

        def send_dshot(self, _value, **_kwargs):
            self.sent += 1
            send_started.set()
            assert release_send.wait(2)

    stub = make_stub(1)
    stub.ports = [BlockingPort()]
    stub.motor_output_enabled = True
    stub.motor_active = True
    stub.fourway.begin = passthrough_started.set
    sender = threading.Thread(target=stub._send_motor_cycle, args=([1500], 0.1))
    transition = threading.Thread(
        target=stub._handle, args=(msp_stub_fc.MSP_SET_PASSTHROUGH, b"")
    )

    sender.start()
    assert send_started.wait(2)
    transition.start()
    assert not passthrough_started.wait(0.05)
    release_send.set()
    sender.join(2)
    transition.join(2)

    assert not sender.is_alive()
    assert not transition.is_alive()
    assert passthrough_started.is_set()
    assert stub.in_fourway
    assert not stub._send_motor_cycle([1500], 0.2)
    assert stub.ports[0].sent == 1


@pytest.mark.parametrize(
    ("msp_value", "dshot_value"),
    [(0, 0), (999, 0), (1000, 0), (1001, 49), (2000, 2047), (2500, 2047)],
)
def test_msp_to_dshot_mapping(msp_value, dshot_value):
    assert msp_stub_fc.MspStubFC._dshot_value(msp_value) == dshot_value

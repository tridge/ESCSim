from types import SimpleNamespace

from escsim.control import msp_stub_fc


def test_motor_metadata_and_telemetry_match_fourway_esc_count():
    stub = object.__new__(msp_stub_fc.MspStubFC)
    stub.fourway = SimpleNamespace(esc_count=8)
    stub.poles = 14
    stub.rpm = 1234
    stub.invalid = 0.5
    stub.temp = 30
    stub.volt_raw = 48
    stub.curr_raw = 6
    replies = []
    stub._reply = lambda command, payload=b"": replies.append((command, payload))

    stub._handle(msp_stub_fc.MSP_MOTOR_CONFIG, b"")
    stub._handle(msp_stub_fc.MSP_MOTOR_TELEMETRY, b"")

    assert replies[0][1][6] == 8
    assert replies[1][1][0] == 8
    assert len(replies[1][1]) == 1 + 8 * 13

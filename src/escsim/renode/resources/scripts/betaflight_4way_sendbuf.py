# ruff: noqa: F821
# Replace BL_SendBuf's byte-at-a-time software-UART loop with one bulk bridge
# operation. The recognized function takes (uint8_t *buffer, uint8_t length),
# where a zero length means 256 bytes. DeviceInfo immediately follows the
# selected-ESC byte in this build; its mode byte says whether the ARM-loader
# CRC must be appended.

bridge_address = 0x60000200
selected_esc = cpu.Bus.ReadByte(selected_esc_address)
buffer_address = cpu.GetRegister(0).RawValue
length = cpu.GetRegister(1).RawValue & 0xFF
append_crc = cpu.Bus.ReadByte(selected_esc_address + 5) != 0

cpu.Bus.WriteDoubleWord(bridge_address + 0x2C, buffer_address)
cpu.Bus.WriteDoubleWord(
    bridge_address + 0x30,
    length | (selected_esc << 8) | ((1 if append_crc else 0) << 16),
)
cpu.Bus.WriteDoubleWord(bridge_address + 0x28, selected_esc)
cpu.PC = cpu.LR

# ruff: noqa: F821
# Feed one Betaflight software-UART byte to the modeled ESC bridge.  The
# selected ESC is firmware state resolved from the matching ELF.

bridge_address = 0x60000200
selected_esc = cpu.Bus.ReadByte(selected_esc_address)
value = cpu.GetRegister(0).RawValue & 0xFF
cpu.Bus.WriteDoubleWord(bridge_address + 0x24, (selected_esc << 8) | value)
cpu.PC = cpu.LR

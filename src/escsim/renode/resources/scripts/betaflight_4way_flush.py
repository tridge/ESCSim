# ruff: noqa: F821
# setEscInput marks the end of a software-UART request.  Complete the same UDP
# transaction the pin decoder would have performed, then let the real function
# restore the GPIO input mode.

bridge_address = 0x60000200
cpu.Bus.WriteDoubleWord(bridge_address + 0x28, cpu.GetRegister(0).RawValue)

# ruff: noqa: F821
# Return a byte already collected by the modeled bridge, bypassing the
# software UART's GPIO/micros polling.  A completed empty response is the same
# timeout result that the real two-millisecond start-bit loop would produce.

from Antmicro.Renode.Peripherals.CPU import RegisterValue

bridge_address = 0x60000200
selected_esc = cpu.Bus.ReadByte(selected_esc_address)
result = cpu.Bus.ReadDoubleWord(bridge_address + 0x40 + selected_esc * 4)
if result & 0x100:
    cpu.Bus.WriteByte(cpu.GetRegister(0).RawValue, result & 0xFF)
    cpu.SetRegister(0, RegisterValue.Create(1, 32))
    cpu.PC = cpu.LR
elif result & 0x200:
    cpu.SetRegister(0, RegisterValue.Create(0, 32))
    cpu.PC = cpu.LR

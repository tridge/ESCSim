# ruff: noqa: F821
# Skip Betaflight's millisecond busy wait while the flight controller boots.
#
# delay() repeatedly calls the inlined micros() implementation, so every
# millisecond retires roughly 125,000 guest instructions.  Renode can advance
# virtual time directly instead.  Keep this optimization confined to startup:
# after SYSTEM_STATE_READY, protocol code and the scheduler execute the real
# delay loop so their interrupt and wire timing stays exact.
#
# Renode coalesces the SysTick events crossed by SkipTime into one pending
# interrupt.  Advance the guest uptime for the other milliseconds; the normal
# handler accounts for the last one after this hook returns.

from Antmicro.Renode.Time import TimeInterval

bus = cpu.Bus
ready = bus.ReadByte(system_state_address) & 0x80
if not ready:
    milliseconds = cpu.GetRegister(0).RawValue
    if milliseconds:
        if milliseconds > 1:
            uptime = bus.ReadDoubleWord(systick_uptime_address)
            bus.WriteDoubleWord(
                systick_uptime_address, (uptime + milliseconds - 1) & 0xFFFFFFFF
            )
        cpu.SkipTime(TimeInterval.FromMilliseconds(milliseconds))
    cpu.PC = cpu.LR

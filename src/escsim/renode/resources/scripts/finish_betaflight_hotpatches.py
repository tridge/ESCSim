# ruff: noqa: F821
# Remove Betaflight's ELF-addressed startup hooks on the first scheduler call.
# init() sets SYSTEM_STATE_READY before main() begins calling scheduler(), so
# protocol and flight-loop delays after this point execute their real code.

if cpu.Bus.ReadByte(system_state_address) & 0x80:
    cpu.RemoveHooksAt(delay_address)
    cpu.RemoveHooksAt(scheduler_address)

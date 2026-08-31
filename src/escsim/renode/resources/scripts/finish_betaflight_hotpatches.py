# ruff: noqa: F821
# Remove Betaflight's ELF-addressed startup hooks on the first scheduler call.
# init() sets SYSTEM_STATE_READY before main() begins calling scheduler(), so
# protocol and flight-loop delays after this point execute their real code.

if cpu.Bus.ReadByte(system_state_address) & 0x80:
    # The target default is 100Hz. At an emulation rate below wall time that
    # lets Configurator requests build an ever-growing queue. This is RAM in
    # task_attributes, so improve host responsiveness without changing the
    # user's saved serial_update_rate_hz setting.
    cpu.Bus.WriteDoubleWord(serial_task_period_address, 1000)
    cpu.RemoveHooksAt(delay_address)
    cpu.RemoveHooksAt(scheduler_address)

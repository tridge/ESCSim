# ESCSim quick start

## Installed application

On Windows, run `ESCSim-installer.exe`, accept the per-user installation, and
open ESCSim from the Start menu. If usbip-win2 is absent, the installer offers
the bundled signed driver needed for browser configurator access. That optional
step requires administrator approval, temporarily reconnects USB devices, and
may require a reboot. PWM, DShot, DroneCAN, graphs, and simulation work without
the driver.

Linux and macOS archives can be unpacked and launched directly. A source setup
is:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[gui]'
.venv/bin/escsim
```

On first use:

1. Choose a target. Typing in the Target field filters the list.
2. Keep the stable published firmware and compatible bootloader selected.
3. Click **Download Renode** if no verified runtime is cached.
4. Choose the configurator transport, or **Off** if only the Control tab is
   needed.
5. Click **Start**. The status changes to running and shows live PC, virtual
   time, MIPS, and realtime speed.
6. In **Control**, enable PWM/DShot, leave throttle at zero until armed, then
   advance it. Motor state, telemetry, parameters, scopes, audio, waveform
   generation, load, and restart controls are the same as AM32's original GUI.

Firmware, bootloader, Renode, target definitions, models, and the last valid
catalog are cached. After one successful setup, the same selection works
offline.

## Targets and local images

**Targets URL...** selects and persists a custom HTTPS `targets.h` URL;
**Browse targets.h...** snapshots a local one. Published historical firmware
always uses the exact header stored in its release, without replacing this
current-target selection.

Firmware and bootloader selectors retain older website releases. Browse
buttons accept local ELF/HEX/BIN images. A non-CAN target must use a non-CAN
bootloader for the same family and signal pin; ESCSim enforces this for
published images.

## Configurator transports

- **FC with 4-way passthrough** presents a minimal flight controller and the
  same MSP/BLHeli path used on a real vehicle. Select one to eight ESCs to
  launch that many independent Renode instances behind the same 4-way port;
  this is useful for testing configurators against a multi-ESC vehicle. With
  multiple ESCs, the launcher provides **Control1**, **Control2**, and so on,
  each driving its matching ESC; graph window titles include the ESC number.
  Configurator motor-test panels can leave 4-way mode and use MSP motor
  control on the same connection. Their motor values drive the corresponding
  simulated ESCs after a short zero-throttle arming interval. Values remain
  active until another 4-way session starts or the simulator connection closes.
- **Direct 1-wire adapter** presents the raw bootloader wire including adapter
  self-echo.
- **FlightController** runs the selected board firmware in an additional Renode
  machine and connects its first four motor timer outputs to independent
  AM32 ESC machines. Select **SpeedyBeeF405Mini** in **FlightController**;
  **Protocol** is then fixed to **FlightController**. The ESC count remains
  selectable from one to eight; this board's modeled outputs drive ESCs 1-4,
  while any remaining ESCs are available through their Control tabs.
  The FC's own USB stack is exposed to the host, so ground stations and web
  tools see the same USB descriptors and protocols as the firmware implements.
- POSIX hosts can use the displayed PTY with desktop configurators.
- Linux virtual USB uses `vhci_hcd`; run the explicit USB/IP rule installer
  only if browser access is needed and the documented local privilege tradeoff
  is acceptable.
- Windows virtual USB is opt-in. The interactive installer offers the bundled,
  SHA-256-verified usbip-win2 0.9.7.7 client when it is missing and warns before
  starting its elevated driver setup.

## Flight-controller DFU

**FC Firmware** selects an image to preload for a direct, non-DFU start. The
bundled choices are Betaflight 2026.6.1 for `SPEEDYBEEF405V5` (revision
`6dbc4218f`) and ArduPilot Copter 4.8.0-dev for `SpeedyBeeF405Mini` (revision
`af2a1bafc8a`). The ArduPilot image includes its bootloader, so either choice
boots directly. Re-selecting the same image preserves its flash-backed
configuration; selecting a different firmware replaces the emulated flash.
Future images are reserved under
`https://firmware.ardupilot.org/Tools/AM32-tools/ESCSim/FC_Firmware/`.

Selecting **Boot in USB DFU** exposes the STM32 ROM-style `0483:df11` DfuSe
device instead of starting the FC. A successful DFU manifestation disconnects
that device, starts Renode from the persistent emulated flash, and attaches the
USB device created by the uploaded bootloader or application. Firmware can be
loaded with the Betaflight web tool or a normal `dfu-util`/DfuSe workflow.

The emulated FC flash persists in ESCSim's cache across Stop/Start. In addition
to direct selection of the bundled ArduPilot image, this permits the usual
two-stage ArduPilot workflow: upload a SpeedyBeeF405Mini bootloader in DFU mode,
then upload `arducopter.apj` through the bootloader's USB serial endpoint.
Sensors return fixed bench values (including a nominal 12 V supply), while the
four motor outputs and bidirectional DShot capture paths remain live.

ESCSim recognizes supported Betaflight Thumb instruction sequences and enables
the startup and 4-way optimizations automatically; no ELF is required. During
4-way mode the FC yields virtual time while waiting for configurator input, and
AM32 bytes pass through the existing UDP wire model without simulating
Betaflight's 19200-baud GPIO and microsecond delay loops instruction by
instruction. Firmware pages use a recognized bulk `BL_SendBuf` path rather
than crossing the Python hook once per byte. Outside 4-way mode, the scheduler's
cycle-counter wait sleeps until the modeled gyro data-ready interrupt at the
configured ODR instead of busy-polling. The actual MSP, 4-way and AM32
bootloader exchanges are unchanged.
Recognition requires unique function signatures, validates their control flow,
and decodes cross-checked PC-relative global addresses. Unknown or ambiguous
firmware runs without hooks. For development builds, `--fc-symbols PATH.elf`
additionally verifies every immutable flash-backed ELF byte (excluding the
saved Betaflight configuration sector) and requires its symbols to agree with
the recognized sequences.

## Command line

```sh
escsim targets status
escsim artifacts list
escsim renode status
escsim generate VIMDRONES_L431 --outdir /tmp/escsim-target
```

Configuration and cache locations follow the native platform conventions via
`platformdirs` (`XDG_CONFIG_HOME`/`XDG_CACHE_HOME` on Linux). Delete an
individual immutable object to force its verified re-download; invalid partial
downloads never replace a valid cache entry.

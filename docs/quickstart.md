# ESCSim quick start

## Installed application

On Windows, run `ESCSim-installer.exe`, accept the per-user installation, and
open ESCSim from the Start menu. The public installer does **not** install a
USB/IP kernel driver; PWM, DShot, DroneCAN, graphs, and simulation work without
one. Browser configurator access over virtual USB remains an explicit lab
feature until a safe signed Windows client is available.

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
  same MSP/BLHeli path used on a real vehicle.
- **Direct 1-wire adapter** presents the raw bootloader wire including adapter
  self-echo.
- POSIX hosts can use the displayed PTY with desktop configurators.
- Linux virtual USB uses `vhci_hcd`; run the explicit USB/IP rule installer
  only if browser access is needed and the documented local privilege tradeoff
  is acceptable.
- Windows virtual USB is opt-in and requires a separately installed safe,
  signed USB/IP client. ESCSim never installs a kernel driver automatically.

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

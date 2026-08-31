# Packaging, CI, and parity

## Local builds

The top-level Makefile builds the native library and a platform-specific
Python wheel containing that library. It can also
run the complete test suite or install the Python GUI and native library
together:

```sh
make
make test
make install
```

Install the packaging dependencies, then build the self-contained application:

```sh
python3 -m pip install -e '.[gui]' pyinstaller
make package
```

The script builds and smoke-tests the native library with GNU Make, stages
exactly one platform library under
`escsim/renode/lib`, and invokes `packaging/escsim.spec`. Outputs are:

- Windows: `dist/ESCSim/`, then compile `packaging/ESCSim.iss` to produce
  `dist/installer/ESCSim-installer.exe`.
- Linux: `dist/ESCSim/`, suitable for a tar archive and later AppImage wrapper.
- macOS: the onedir output and `dist/ESCSim.app`; signing/notarization is a
  release-operator step.

The Windows application is installed per-user. Its installer contains the
SHA-256-verified upstream usbip-win2 prerequisite and offers its separate,
elevated driver setup only when needed. Release signing secrets are not stored
in this repository. Public releases must sign the executable/installer and
notarize the macOS app before upload.

## Continuous integration

- `checks.yml`: Ruff, the Python/GUI suite, ShellCheck, and publisher syntax.
- `native.yml`: Makefile smoke tests and native artifacts on Linux, Windows,
  macOS.
- `package.yml`: three-platform PyInstaller builds, packaged CLI smoke, and
  the Windows Inno Setup installer.
- `publisher.yml`: catalog/publisher fixture and schema tests.
- `renode.yml`: nightly/manual published-firmware ELF and HEX PWM, DShot600,
  and BDShot spin checks for VIMDRONES_L431 and TEKKO32_F415.

## Functional parity matrix

The implementation directly ports the current `launch.py` and embedded
`sitl_gui.create_ui()` paths. The following are covered by unit, offscreen GUI,
local-catalog, or real Renode checks:

| Area | Coverage |
|---|---|
| Target search/info, CAN enablement | GUI and all-target preprocessing tests |
| Custom URL/local `targets.h`, persistence | cache/server/settings tests |
| Firmware/bootloader local and historical ELF/HEX versions | publisher/client tests and real catalog launch |
| Renode current download/cache | manifest, archive hardening, and live runtime check |
| Start/stop/restart, monitor metrics | process tests and real VIMDRONES boot |
| PWM, DShot600, BDShot | nightly parity runner; local DShot600 spin check |
| DroneCAN | imported control backend and target generation tests |
| EEPROM defaults/blank/parameters | network-free image and GUI tests |
| FC 4-way/direct 1-wire | protocol port and real PTY smoke checks |
| Graphs, motor view, model load/edit, waveforms, audio | embedded Control-tab construction and ported control suite |
| Linux/Windows USB/IP ownership | server and Windows identity/port tests |

Run the published-image parity sweep manually with:

```sh
python3 scripts/build-package.py --skip-pyinstaller
python3 scripts/run-parity-tests.py --output parity-report.json
```

Use one published non-CAN target from every supported MCU family with:

```sh
make parity-all-mcus
```

The command tests ELF and HEX images with PWM, DShot600, and BDShot and writes
the platform, selected targets, pass/fail totals, and every result to
`build/parity-all-mcus.json` by default.

## Windows lab build

Run the build from a Cygwin shell, but use native 64-bit Windows Python. Cygwin
Python cannot install the PySide6 Windows wheels. Install Cygwin's
`mingw64-x86_64-gcc-core` toolchain, then create an isolated environment:

```sh
py -3.12 -m venv .venv-win
.venv-win/Scripts/python.exe -m pip install -e '.[test,gui]' pyinstaller
CC=x86_64-w64-mingw32-gcc \
    .venv-win/Scripts/python.exe scripts/build-package.py
```

Microsoft Store Python virtualizes writes below `LocalAppData`, while Renode is
a separate process and cannot see the virtualized files. Give source-tree runs
a shared, ordinary cache directory:

```sh
export ESCSIM_CACHE_DIR="$(cygpath -w "$PWD/build/windows-cache")"
QT_QPA_PLATFORM=offscreen .venv-win/Scripts/python.exe -m pytest
CC=x86_64-w64-mingw32-gcc \
    make parity-all-mcus PYTHON=.venv-win/Scripts/python.exe
```

The interactive ESCSim installer bundles the verified usbip-win2 0.9.7.7
installer. When the driver is absent it offers to run that installer with an
explicit warning that USB hubs are temporarily restarted and Windows may need
a reboot. ESCSim itself remains a per-user install; only the optional driver
step asks for administrator approval. The prerequisite is launched separately
so a stalled driver upgrade cannot hold the ESCSim installer open. Installed
versions are read from usbip-win2's machine-wide uninstall entry because its
executable does not provide a Windows file-version resource. Version 0.9.7.8
is explicitly rejected because it is unsafe.

The installed GUI starts every generator and Renode instance with its Windows
console hidden, so emulator processes do not flash windows into the foreground. Their
combined stdout/stderr is still shown in the GUI and is also flushed line by
line to stable per-process files under
`%LOCALAPPDATA%\\AM32\\ESCSim\\Cache\\logs` (`esc1.log` through `esc8.log` and
`flight-controller.log`). A new Start overwrites the corresponding latest-run
log instead of creating another directory.

With usbip-win2 installed, the real attach/enumerate/serial-echo/detach
integration check is:

```sh
make windows-usbip-test PYTHON=.venv-win/Scripts/python.exe
```

The integration test detaches only the UDE port returned by its own attach
operation.

Build the application, fetch and SHA-256 verify the pinned usbip-win2 release,
and compile the installer with:

```sh
make windows-installer PYTHON=.venv-win/Scripts/python.exe
./dist/installer/ESCSim-installer.exe /VERYSILENT /NORESTART /TASKS=""
```

Silent installs intentionally skip the optional driver prompt, making package
smoke tests non-disruptive. A normal interactive install offers the bundled
driver when `usbip.exe` is not present.

From the Linux development host, the complete lab build can be run remotely:

```sh
make win11
```

This synchronizes the current working tree to the disposable
`~/ESCSim-win11-build` directory on the `win11` SSH host, reuses the native
Python environment in `~/ESCSim/.venv-win`, builds the application and
installer, and smoke-tests the packaged CLI. It leaves the existing remote
checkout and installed application untouched, then prints the Windows path of
the installer to run interactively. `WIN11_HOST`, `WIN11_DIR`, and
`WIN11_PYTHON` can override the defaults; for safety, `WIN11_DIR` must begin
with `ESCSim-win11-`.

The release gate specifically prevents regressions in non-CAN
VIMDRONES_L431 DShot600 and TEKKO32_F415 PWM/DShot/BDShot. A representative
DroneCAN runtime case will be added to the parity runner once the first public
catalog is populated; target generation and CAN control are already tested.

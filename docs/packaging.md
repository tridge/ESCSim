# Packaging, CI, and parity

## Local builds

The top-level Makefile builds the native library and Python wheel. It can also
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

The Windows installer is per-user and deliberately contains no USB/IP driver.
Release signing secrets are not stored in this repository. Public releases
must sign the executable/installer and notarize the macOS app before upload.

## Continuous integration

- `checks.yml`: Ruff, the Python/GUI suite, ShellCheck, and publisher syntax.
- `native.yml`: Makefile smoke tests and native artifacts on Linux, Windows,
  macOS.
- `package.yml`: three-platform PyInstaller builds, packaged CLI smoke, and
  the Windows Inno Setup installer.
- `publisher.yml`: catalog/publisher fixture and schema tests.
- `renode.yml`: nightly/manual published-firmware PWM, DShot600, and BDShot
  spin checks for VIMDRONES_L431 and TEKKO32_F415.

## Functional parity matrix

The implementation directly ports the current `launch.py` and embedded
`sitl_gui.create_ui()` paths. The following are covered by unit, offscreen GUI,
local-catalog, or real Renode checks:

| Area | Coverage |
|---|---|
| Target search/info, CAN enablement | GUI and all-target preprocessing tests |
| Custom URL/local `targets.h`, persistence | cache/server/settings tests |
| Firmware/bootloader local and historical versions | publisher/client tests and real catalog launch |
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

The release gate specifically prevents regressions in non-CAN
VIMDRONES_L431 DShot600 and TEKKO32_F415 PWM/DShot/BDShot. A representative
DroneCAN runtime case will be added to the parity runner once the first public
catalog is populated; target generation and CAN control are already tested.

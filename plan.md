# ESCSim repository plan

## Goal

Create a standalone, cross-platform AM32 ESC simulator based on Renode. The
application must not require an AM32 source checkout, an ARM compiler, Python,
or a manually installed Renode at runtime. Linux is the first supported host,
but the design and file layout must support Windows and macOS without a later
architectural rewrite.

The primary user interface will combine the current `Mcu/Renode/launch.py` and
`Mcu/Renode/gen_target.py --gui` functionality:

- **Target tab:** target search, firmware and bootloader versions, local
  firmware overrides, CAN bus, EEPROM mode, Renode selection, configurator
  transport and protocol, advanced launch options, status, metrics, logs, and
  start/stop controls.
- **Control tab:** PWM, DShot, BDShot, EDT, DroneCAN, throttle waveforms,
  motor/load models, graphs, audio, simulation speed, stuck rotor, parameters,
  and ESC restart.

The same capabilities must be available to automated tests and headless users
through a command-line interface.

## Proposed repository layout

```text
ESCSim/
|-- pyproject.toml
|-- README.md
|-- LICENSE
|-- CHANGELOG.md
|-- CONTRIBUTING.md
|-- SECURITY.md
|-- src/escsim/
|   |-- __main__.py
|   |-- application.py
|   |-- settings.py
|   |-- target/
|   |   |-- source.py
|   |   |-- cache.py
|   |   |-- preprocess.py
|   |   |-- catalog.py
|   |   `-- generator.py
|   |-- firmware/
|   |   |-- catalog.py
|   |   |-- download.py
|   |   `-- resolver.py
|   |-- renode/
|   |   |-- download.py
|   |   |-- session.py
|   |   |-- monitor.py
|   |   `-- resources/
|   |       |-- peripherals/
|   |       |-- platforms/
|   |       `-- scripts/
|   |-- protocols/
|   |   |-- dshot.py
|   |   |-- fourway.py
|   |   |-- dronecan.py
|   |   `-- msp.py
|   |-- usbip/
|   |   |-- common.py
|   |   |-- linux.py
|   |   |-- windows.py
|   |   `-- macos.py
|   |-- gui/
|   |   |-- main_window.py
|   |   |-- target_tab.py
|   |   |-- control_tab.py
|   |   |-- parameters.py
|   |   `-- model_editor.py
|   `-- resources/
|       |-- models/
|       |-- schemas/
|       `-- default-targets.h
|-- native/am32sim/
|   |-- CMakeLists.txt
|   `-- src/
|-- tools/
|   |-- build_target_pack.py
|   |-- compare_target_preprocessors.py
|   `-- publisher/
|       |-- build_release.py
|       |-- build_firmware.py
|       |-- build_bootloaders.py
|       |-- generate_catalog.py
|       |-- generate_index.py
|       |-- verify_repository.py
|       `-- schemas/
|-- tests/
|   |-- unit/
|   |-- integration/
|   |-- renode/
|   |-- gui/
|   |-- package/
|   |-- fixtures/
|   `-- expected/
|-- packaging/
|   |-- windows/
|   |-- linux/
|   `-- macos/
|-- docs/
`-- .github/workflows/
```

The public entry points will be:

```text
escsim                  Open the GUI
escsim run ...          Launch a simulator headlessly
escsim targets ...      List, refresh, and configure target sources
escsim doctor           Diagnose Renode, USB/IP, firmware, and cache state
```

The old `launch.py` and `gen_target.py` may remain temporarily as thin
compatibility wrappers, but they will contain no application logic.
After the baseline tests are captured, ESCSim becomes the authoritative
implementation. The AM32-tree implementation is then frozen except for
critical fixes, which must be applied to ESCSim first and explicitly
backported until the wrappers replace it.

## Application architecture

The Target tab will create an immutable session specification which is passed
to the runtime layer. The GUI must not directly own Renode process management,
target generation, downloads, or protocol implementations. This separation
allows the GUI, CLI, and tests to use the same runtime.

Platform-specific behaviour will be isolated behind adapters for:

- User configuration and cache paths.
- Process creation, termination, child-process containment, and CPU affinity.
- Native library naming and discovery.
- USB/IP attachment and detachment.
- Serial-port discovery.
- Optional external tools such as GDB and PulseView.

The application should report host capabilities in the UI rather than hiding
options based on scattered `sys.platform` checks.

## Target source and cache

The default `targets.h` source will be:

```text
https://raw.githubusercontent.com/am32-firmware/AM32/refs/heads/main/Inc/targets.h
```

The settings UI will allow the user to select the default URL, a custom URL,
or a local file. It will provide refresh and restore-default actions and show
the current URL, content hash, fetch time, and stale/offline status. A
successfully validated selection is persisted across application restarts.

Use `platformdirs` for native locations, including approximately:

- Linux: `~/.config/ESCSim` and `~/.cache/ESCSim`.
- Windows: `%APPDATA%/ESCSim` and `%LOCALAPPDATA%/ESCSim/Cache`.
- macOS: Application Support and Library Caches.

Downloaded headers will be stored by SHA-256 with metadata containing the
source URL or local path, ETag, Last-Modified value, fetch time, and content
hash. Refreshes use conditional HTTP requests and atomic replacement.

Startup behaviour will be:

1. Load the last verified cache entry immediately.
2. Refresh asynchronously when appropriate.
3. Never replace a valid entry with an invalid or incomplete download.
4. Fall back to the snapshot shipped with ESCSim on first start without
   network access.

The installed application must not require `arm-none-eabi-gcc` to preprocess
the header. Use a bundled, in-process C preprocessor and validate its expanded
configuration against GCC for every supported target in CI. Treat a custom
header as untrusted input: bound its size and parse time, reject external
includes and executable directives, and do not expose arbitrary host include
paths. Cache the derived target catalog by header hash and generator schema
version.

## Download repository

Firmware and bootloader images will not be bundled in the application
installer. They will be published at:

```text
https://am32.tridgell.net/ESCSim/
```

The initial document root on fjall will be separate from the existing web
configurator container:

```text
/var/www/am32.tridgell.net/html/ESCSim/
```

Fjall currently runs Apache in front of the configurator container and sends
all paths to `127.0.0.1:3000`. The Apache virtual host will add a static alias
and a proxy exclusion before its catch-all proxy:

```apache
ProxyPass /ESCSim/ !
Alias /ESCSim/ /var/www/am32.tridgell.net/html/ESCSim/

<Directory /var/www/am32.tridgell.net/html/ESCSim/>
    Require all granted
    Options -Indexes
</Directory>

ProxyPass        / http://127.0.0.1:3000/
ProxyPassReverse / http://127.0.0.1:3000/
```

Apache will also set CORS headers, allow byte-range downloads, use short or
no-cache headers for catalogs and channel pointers, and use long immutable
cache headers for versioned files. A generated `index.html` will provide a
human-readable release browser; Apache directory indexes will remain off.

### Repository layout

```text
ESCSim/
|-- index.html
`-- v1/
    |-- catalog.json
    |-- channels/
    |   |-- stable.json
    |   `-- latest.json
    |-- firmware/
    |   `-- 2.21.0-gabcdef12/
    |       |-- manifest.json
    |       |-- targets/
    |       |   |-- VIMDRONES_L431/
    |       |   |   |-- firmware.elf
    |       |   |   |-- firmware.hex
    |       |   |   `-- SHA256SUMS
    |       |   `-- TEKKO32_F415/
    |       `-- sources.tar.xz
    |-- bootloader/
    |   `-- 19-g12345678/
    |       |-- manifest.json
    |       |-- targets/
    |       |   |-- VIMDRONES_L431/
    |       |   |   `-- bootloader.elf
    |       |   `-- TEKKO32_F415/
    |       `-- sources.tar.xz
    `-- targets/
        `-- TARGETS_H_SHA256/
            |-- targets.h
            `-- resolved-targets.json
```

Firmware and bootloader versions are independent. Release identifiers contain
both the reported version and the first 12 hexadecimal characters of the
source commit, extending the hash until unique if a collision is detected, so
that distinct builds cannot collide when upstream retains the same version
string. All paths in manifests will be relative so the repository can be
mirrored.

The top-level catalog provides the complete release list without requiring a
request per target or version. Each release manifest records:

- Project, release identifier, Git commit, and build timestamp.
- Stable or development status.
- Toolchain and builder version.
- Exact `targets.h` URL and SHA-256.
- Supported target names and MCU families.
- Flash layout and application address needed for compatibility checking.
- Artifact URL, size, and SHA-256.
- Source archive URL.
- ESCSim and Renode compatibility information.
- Smoke-test and performance-test status.

The catalogs and manifests will have explicit schema versions and JSON schemas
checked into ESCSim.

### Historical target definitions

A historical firmware image must use the target definition from the AM32
commit that built it, not the current definition on the `main` branch. Every
published firmware release will therefore retain its exact `targets.h`
snapshot and a resolved target catalog. When launching published firmware,
ESCSim uses that release's resolved configuration. The actively selected
custom or upstream header is used to discover current targets and for local
firmware, but cannot silently override a published artifact's configuration.

If a target definition, flash layout, or application address is incompatible
with the selected bootloader, ESCSim will block the combination or present an
explicit expert override rather than guessing.

### Client download behaviour

On first launch ESCSim will:

1. Fetch `v1/catalog.json`.
2. Select the stable firmware and a compatible bootloader for the chosen
   target.
3. Download only the necessary ELF and optional user-requested HEX/BIN files.
4. Verify size and SHA-256 before making the cache entry visible.
5. Store the immutable artifacts in the platform-native user cache.
6. Continue using cached releases when offline.

Users can select and pin historical firmware and bootloader versions
independently, refresh the catalog, use local files, delete individual cached
versions, and inspect source revisions and hashes. Existing immutable cache
entries are never overwritten by an update. An advanced setting and CLI
option may override the artifact repository base URL for mirrors and
development servers.

Pinning a nightly protects only the client's local cached copy. It does not
prevent scheduled server retention; a nightly which must remain downloadable
must first be explicitly promoted to a retained development release.

## Automated builder and publisher

The publishing scripts will be part of ESCSim, while host configuration and
credentials remain outside the repository. A scheduled publisher on fjall, or
a build host that publishes to fjall, will:

1. Acquire a lock so scheduled builds cannot overlap.
2. Fetch AM32 and AM32-bootloader sources.
3. Exit successfully if the selected commits are already published.
4. Build clean detached revisions with a pinned toolchain or build container.
5. Build every target supported by ESCSim.
6. Generate the target snapshot, manifests, checksums, source archives, and
   human-readable index.
7. Run representative Renode smoke and performance tests.
8. Validate the complete staged download repository.
9. Atomically move immutable version directories into the document root.
10. Atomically replace catalog, channel, and index files last.

Builds must happen outside the web root, for example:

```text
/var/lib/escsim-builder/work/
/var/lib/escsim-builder/staging/
```

Only validated output is moved to:

```text
/var/www/am32.tridgell.net/html/ESCSim/
```

A dedicated publisher account should have write permission only to the build
workspace and ESCSim artifact directory. Apache needs read-only access. The
publisher can run from cron; a systemd timer is preferable if available for
service-level timeouts, logging, and failure status. Failed builds are never
published, and publisher failures should be reported through the existing
host monitoring or email.

Retention policy:

- Tagged stable releases are permanent.
- Explicitly promoted development releases are permanent.
- Nightlies retain the most recent 30 to 60 successful builds.
- Existing version directories are never modified in place.
- Cleanup never deletes an object referenced by a retained manifest or
  channel.
- Disk use is checked and reported before cleanup or publication.

## Renode management

Retain the ArduPilot-style manifest download from `firmware.ardupilot.org`,
including platform selection, SHA-256 verification, and caching. Each ESCSim
release will record the exact Renode build tested by CI. The UI may offer a
newer current build after it passes a compatibility and performance test.
The publisher records those results in a versioned `renode-compat.json`
catalog referenced by `catalog.json`; the client never infers compatibility
solely from the upstream Renode manifest.

Performance acceptance will include:

- VIMDRONES_L431 non-CAN using PWM, DShot 600, and BDShot.
- TEKKO32_F415 using PWM, DShot, and BDShot.
- A representative DroneCAN target.
- At least one target from every supported MCU family.

Record simulated time, wall time, arming time, instruction rate, and stable
RPM. Compare the candidate Renode build against the previously accepted build
using documented absolute and relative regression thresholds.

## Native motor simulator

The current native motor model depends on AM32 source-tree files and uses a
Linux-oriented Makefile. It will become a self-contained CMake library that
produces:

```text
libam32sim.so
am32sim.dll
libam32sim.dylib
```

Only the required motor and configuration code will be extracted, preserving
copyright and attribution. Provide small abstractions for threads, timing, and
native library loading, and pass configuration through an API instead of
`getopt`. Renode will load the library from a packaged absolute resource path,
never from the current working directory.

## USB/IP

Use one common USB/IP server with host adapters for Linux `vhci_hcd`, Windows
USB/IP, and future macOS support. Windows will export USB/IP over loopback TCP
and must record the exact virtual port it owns so shutdown never detaches an
unrelated device.

The existing Windows prototype includes useful path, process, serial-port,
attach/detach, timeout, and version checks and should be migrated. It must not
lead to an unsafe driver dependency: usbip-win2 0.9.7.7 has a reported
watchdog/BSOD problem and 0.9.7.8 is explicitly unsafe. A public installer
must not install either automatically. Public USB support requires a fixed,
appropriately signed client; until that exists, Windows USB attachment remains
an opt-in laboratory capability with a prominent warning.

USB/IP configurator access is explicitly outside the public Windows installer
acceptance criteria until that safe signed client exists. PWM, DShot, CAN, GUI
control, and non-USB configurator transports can ship independently.

## Testsuite

### Fast pull-request tests

- Target cache 200/304 responses, timeouts, invalid content, atomic update,
  stale cache, and offline fallback.
- Settings persistence and schema migration.
- Header input limits and rejected directives.
- Target preprocessing and generated-platform golden files.
- Download catalog and manifest schema validation.
- Hash mismatch, truncation, cancellation, resume, and cache recovery.
- Firmware/bootloader compatibility checks.
- Cross-platform path quoting, packaged resource lookup, port allocation, and
  child-process cleanup.
- Protocol and USB/IP server tests without requiring a kernel USB driver.

### Renode integration tests

- Representative targets from every supported MCU family.
- PWM, DShot, BDShot, EDT, DroneCAN, EEPROM, bootloader/configurator, and
  restart paths.
- Preserve the existing expected-RPM fixtures and scripted spin/stop tests.
- Compare the bundled preprocessor against GCC for every supported target.

### GUI and package tests

- Drive both tabs through the existing control interface using offscreen Qt.
- Test target search, version selection, download progress, cancellation,
  errors, start/stop, graphs, parameters, and restart.
- Run packaged applications from an empty temporary directory with isolated
  config and cache paths.
- Prove that the package does not depend on an AM32 checkout, ARM compiler,
  Python installation, or inherited working directory.

### Release and system tests

- Clean installation, upgrade, and uninstall on Windows.
- First-run download, hash verification, cached restart, and offline restart.
- Linux package smoke tests and later macOS package/notarization tests.
- USB attach/detach, configurator traffic, firewall behaviour, sleep/resume,
  and repeated restart testing on dedicated hosts when a release-safe Windows
  USB/IP client is available.

## CI and releases

Suggested GitHub Actions workflows:

- `checks.yml`: formatting, linting, type checks, unit tests, schemas, and
  resource validation.
- `native.yml`: native library builds and tests on Linux, Windows, and macOS.
- `renode.yml`: representative integration tests on pull requests and the full
  target matrix nightly.
- `gui.yml`: headless GUI tests on all three operating systems.
- `package.yml`: PyInstaller bundles and packaged-application smoke tests.
- `release.yml`: tagged application builds, checksums, SBOM, signatures, and
  release upload.
- `publisher.yml`: publisher unit/integration tests using a local static HTTP
  server and fixture repositories; production publishing remains a controlled
  fjall job.
- `upstream-watch.yml`: detect target-header and Renode-manifest changes and
  report unsupported targets or performance regressions.

Windows will initially use a PyInstaller one-directory application wrapped by
Inno Setup or WiX as `ESCSim-installer.exe`. Sign the installer and executable
before public release. Linux can initially support source/virtual-environment
installation, followed by a standalone archive or AppImage. macOS will later
use a signed and notarized application bundle and DMG.

Firmware and bootloader files remain website downloads. The installer or
first-run wizard may prefetch the selected stable target so that, after setup,
the simulator runs offline, but these images are not embedded in the installer.

## Documentation

The initial documentation will include:

- Five-minute Linux and Windows quick starts.
- Target, firmware, and bootloader version selection.
- Custom `targets.h`, cache, refresh, and offline behaviour.
- PWM, DShot, BDShot, EDT, and DroneCAN control.
- Configurator and USB/IP setup by platform.
- Download catalog, manifests, publisher, and retention policy.
- Adding a target, MCU family, peripheral, or motor model.
- Running the tests and producing application packages.
- Troubleshooting and `escsim doctor`.
- Architecture, security model, release process, and data locations.
- Third-party licenses, attribution, and source-offer obligations.

## Implementation sequence

1. Import the existing Renode resources and capture current behaviour in
   regression tests before restructuring.
2. Establish the Python package, resource lookup, runtime/session API, and
   platform adapters.
3. Implement remote/local `targets.h`, persistent settings, validation, and
   offline cache.
4. Remove installed-runtime dependencies on an AM32 checkout, ARM compiler,
   and relative paths.
5. Define catalog schemas and implement the client download/cache/resolver.
6. Implement the staged firmware and bootloader builder/publisher and test it
   against a local static repository.
7. Configure the static `/ESCSim/` path on fjall and begin scheduled builds.
8. Port the combined Target and Control GUI plus the headless CLI.
9. Make the native motor library portable and package it correctly.
10. Complete and validate the Linux MVP.
11. Integrate the proven Windows process, native-library, and USB/IP work and
    produce `ESCSim-installer.exe`.
12. Add macOS packaging and capability-specific USB handling.
13. Move all CI ownership to ESCSim, then remove the duplicated implementation
    from the AM32 tree.

## Initial release acceptance criteria

On a clean supported host, the user can install or start ESCSim, select a
target and published firmware/bootloader versions, download and verify the
required artifacts and Renode, arm the emulated ESC, and spin the motor with
PWM and DShot. After the first successful setup, the same configuration works
without network access and without an AM32 checkout, ARM toolchain, Python
installation, or manually installed Renode.

The release tests must specifically prevent regressions in VIMDRONES_L431
non-CAN DShot 600 and TEKKO32_F415 PWM, DShot, and BDShot operation.

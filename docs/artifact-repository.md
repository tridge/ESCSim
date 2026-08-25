# Firmware and bootloader repository

ESCSim does not bundle AM32 firmware. The Target tab reads the versioned
catalog at
`https://firmware.ardupilot.org/Tools/AM32-tools/ESCSim/v1/catalog.json`, offers every
release containing the selected target, and downloads only the selected ELF.
The byte count and SHA-256 in its manifest must match before a cache entry is
made visible. A firmware release also supplies its exact historical
`targets.h`; the generator uses that snapshot without changing the user's
chosen current/custom header.

Firmware and bootloader versions are independent. ESCSim matches a bootloader
by MCU family, signal pin, and CAN variant, preferring the ordinary unsized
variant. Users can still browse to local images. The last valid catalog,
manifests, and downloaded objects remain usable offline under the
platform-native ESCSim cache directory.

Useful commands:

```sh
escsim artifacts refresh
escsim artifacts list
escsim artifacts install firmware 2.21 VIMDRONES_L431
python -m escsim.artifacts.publisher validate /staging/ESCSim/v1
```

From an ESCSim checkout with sibling `AM32` and `AM32-bootloader` checkouts,
build and publish the current releases with:

```sh
make publish
```

This defaults to the `autotest` SSH host and
`APM/buildlogs/binaries/Tools/AM32-tools/ESCSim`. That path is published as
`https://firmware.ardupilot.org/Tools/AM32-tools/ESCSim/`. The target first
downloads the existing
repository so older releases remain in the catalog, builds detached copies of
each upstream default branch, validates the complete repository, then uploads
immutable release directories before replacing `catalog.json` and
`index.html`. The resulting tree keeps JSON schemas under `schemas/` and
versioned data under `v1/firmware/<version>` and
`v1/bootloader/<version>`, with a target subdirectory for every ELF.

The publisher prefers the toolchains installed under the AM32 source tree and
falls back to `arm-none-eabi-` and `riscv64-unknown-elf-` from `PATH`. Common
overrides include:

```sh
make publish PUBLISH_CHANNEL=nightly PUBLISH_JOBS=16
make publish AM32_REF=origin/develop AM32_BOOTLOADER_REF=origin/master
make publish PUBLISH_HOST=user@example.net PUBLISH_ROOT=/srv/www/ESCSim
```

The toolchain command prefixes can be changed with
`ESCSIM_ARM_SDK_PREFIX` and `ESCSIM_RISCV_SDK_PREFIX` in the environment.

## Repository layout

`catalog.json` is the only mutable pointer. Release directories are immutable:

```text
v1/
  catalog.json
  index.html
  firmware/2.21/manifest.json
  firmware/2.21/targets.h
  firmware/2.21/targets/VIMDRONES_L431/AM32_VIMDRONES_L431_2.21.elf
  bootloader/19/manifest.json
  bootloader/19/targets/AM32_L431_BOOTLOADER_PA2/...elf
```

The schemas are in `schemas/`. The client additionally applies bounded JSON,
same-origin relative-path, identifier, size, and digest validation rather than
trusting a schema library alone.

## Scheduled publishing

Create an unprivileged `escsim-publisher` account. It needs read access to the
source mirrors and ESCSim checkout, write access only to
`/var/lib/escsim-builder`, and an SSH key authorized for the `autotest` host.
Copy the example environment to `/etc/escsim-publisher.conf`, install the
service and timer in `/etc/systemd/system/`, then run:

```sh
systemctl daemon-reload
systemctl enable --now escsim-publisher.timer
systemctl start escsim-publisher.service
journalctl -u escsim-publisher.service
```

The job takes a non-blocking lock, builds detached upstream revisions in a
private temporary tree, retains the existing releases, validates the complete
staged repository, uploads new immutable directories to the ArduPilot firmware
host, and replaces `catalog.json` and `index.html` last. A failed build never
changes those two public pointers. The timer is preferable to cron because it
supplies timeout, logging, missed-run handling, and a constrained service
sandbox; `make publish` is also safe to invoke from cron if required.

Stable and explicitly promoted development releases should be retained
permanently. Nightly cleanup is intentionally not automatic yet: no release
directory should be removed until a retention command proves it is unreferenced
by the generated catalog and channel pointers.

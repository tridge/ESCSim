# ESCSim

ESCSim is a standalone, Renode-based simulator for real AM32 ESC firmware.
It is being extracted from AM32's in-tree Renode support so that end users do
not need an AM32 source checkout or embedded compiler toolchain.

The implementation is currently under construction. See [plan.md](plan.md)
for the staged migration and release plan.

## Target definitions

ESCSim obtains AM32 target definitions from:

```text
https://raw.githubusercontent.com/am32-firmware/AM32/refs/heads/main/Inc/targets.h
```

The source can be changed to another HTTPS URL or a local file. A validated
copy is cached by content hash and the selected source persists across runs.

From a source checkout:

```sh
python3 -m pip install -e '.[test]'
escsim targets status
escsim targets refresh
escsim targets list
pytest
```

## Renode target generation

The target generator and all Renode platform/peripheral resources are part of
the ESCSim package. Target preprocessing is performed in-process and checked
against GCC in the tests; GCC is not used at runtime.

```sh
escsim renode install
escsim generate VIMDRONES_L431 --outdir /tmp/escsim-target
```

The native motor model is built with CMake:

```sh
cmake -S native/am32sim -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native
ctest --test-dir build/native --output-on-failure
```

The downloader uses the same verified `firmware.ardupilot.org` packages and
cache as ArduPilot's Renode launcher. Versioned firmware/bootloader downloads
and the fjall publisher are covered in
[docs/artifact-repository.md](docs/artifact-repository.md).

## Licensing

ESCSim is licensed under GPL-3.0-only. See [THIRD_PARTY.md](THIRD_PARTY.md)
for resources derived from other projects.

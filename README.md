# ESCSim

ESCSim is a standalone, Renode-based simulator for real AM32 ESC firmware.
It contains the combined Target and Control application, downloads verified
Renode/firmware/bootloader releases, and does not require an AM32 checkout or
embedded compiler toolchain at runtime.

See the [five-minute quick start](docs/quickstart.md), the
[packaging guide](docs/packaging.md), and the current
[Windows test matrix](docs/windows-testing.md).

## Target definitions

ESCSim obtains AM32 target definitions from:

```text
https://raw.githubusercontent.com/am32-firmware/AM32/refs/heads/main/Inc/targets.h
```

The source can be changed to another HTTPS URL or a local file. A validated
copy is cached by content hash and the selected source persists across runs.

From a source checkout:

```sh
python3 -m pip install -e '.[test,gui]'
escsim
escsim targets status
escsim targets refresh
escsim targets list
pytest
```

Or use the top-level Makefile to build the native library and Python wheel,
run all tests, install the complete GUI application, or create a standalone
application bundle:

```sh
make
make test
make install
make package
```

`make install` uses the current `python3`. Select a virtual environment or a
user install when needed, for example `make install PYTHON=.venv/bin/python`
or `make install PIP_INSTALL_FLAGS=--user`.

## Renode target generation

The target generator and all Renode platform/peripheral resources are part of
the ESCSim package. Target preprocessing is performed in-process and checked
against GCC in the tests; GCC is not used at runtime.

```sh
escsim renode install
escsim generate VIMDRONES_L431 --outdir /tmp/escsim-target
```

The native motor model can also be built directly with its small portable
Makefile:

```sh
make -C native/am32sim test
```

The downloader uses the same verified `firmware.ardupilot.org` packages and
cache as ArduPilot's Renode launcher. Versioned firmware/bootloader downloads
and the artifact publisher are covered in
[docs/artifact-repository.md](docs/artifact-repository.md).

## SITL

Alongside the Renode emulation, `SITL/` holds everything around AM32's
native software-in-the-loop simulator: the motor/battery models, the
calibration datasets captured from real hardware, the Qt control GUI,
the DroneCAN measurement and capture tools and the test suites. The
simulator itself is C in the firmware repo (`Mcu/SITL`) and is built
from the pinned submodule:

```sh
git submodule update --init modules/am32-firmware modules/am32-bootloader
make sitl-test
```

`AM32_ROOT` selects a different firmware checkout, which is how AM32's
own CI tests a branch against these models and tests:

```sh
AM32_ROOT=/path/to/AM32 python3 SITL/run_ci_tests.py
```

See [SITL/README.md](SITL/README.md) for the simulator's options, the
GUI, and the calibration workflow.

## Licensing

ESCSim is licensed under GPL-3.0-only. See [THIRD_PARTY.md](THIRD_PARTY.md)
for resources derived from other projects.

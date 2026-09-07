PYTHON ?= python3
CONFIGURATION ?= Release
BUILD_DIR ?= $(CURDIR)/build
NATIVE_BUILD_DIR ?= $(BUILD_DIR)/native
WHEEL_DIR ?= $(BUILD_DIR)/wheelhouse
PIP_INSTALL_FLAGS ?=
PYTHON_PACKAGE ?= .[gui]
AM32_SOURCE ?= $(abspath ../AM32)
AM32_BOOTLOADER_SOURCE ?= $(abspath ../AM32-bootloader)
AM32_REF ?= origin/HEAD
AM32_BOOTLOADER_REF ?= origin/HEAD
PUBLISH_HOST ?= autotest
PUBLISH_ROOT ?= APM/buildlogs/binaries/Tools/AM32-tools/ESCSim
PUBLISH_WORK_DIR ?= $(BUILD_DIR)/publisher-work
PUBLISH_SITE_DIR ?= $(BUILD_DIR)/publish-site
PUBLISH_CHANNEL ?= stable
PUBLISH_JOBS ?= 8
PUBLISH_RSYNC_FLAGS ?= -a --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r
PARITY_OUTPUT ?= $(BUILD_DIR)/parity-all-mcus.json
WIN11_HOST ?= win11
WIN11_DIR ?= ESCSim-win11-build
WIN11_PYTHON ?= ../ESCSim/.venv-win/Scripts/python.exe
XEPHYR_WEBSERIAL_ARGS ?=
# the AM32 firmware checkout the SITL simulator is built from
AM32_ROOT ?= $(CURDIR)/modules/am32-firmware
SITL_MAKE_FLAGS ?=

ifeq ($(OS),Windows_NT)
NATIVE_NAME := am32sim.dll
else ifeq ($(shell uname -s),Darwin)
NATIVE_NAME := libam32sim.dylib
else
NATIVE_NAME := libam32sim.so
endif

NATIVE_LIBRARY := $(NATIVE_BUILD_DIR)/$(NATIVE_NAME)

.DEFAULT_GOAL := all
.PHONY: all native wheel test native-test python-test package windows-installer install \
	parity-all-mcus windows-usbip-test xephyr xephyr-webserial win11 publish clean \
	sitl sitl-test sitl-gui

all: native wheel

native:
	$(MAKE) -C native/am32sim BUILD_DIR=$(NATIVE_BUILD_DIR) \
		CONFIGURATION=$(CONFIGURATION) all

wheel: native
	@mkdir -p $(WHEEL_DIR)
	$(PYTHON) scripts/build-wheel.py --native "$(NATIVE_LIBRARY)" \
		--wheel-dir "$(WHEEL_DIR)"

test: native-test python-test

native-test:
	$(MAKE) -C native/am32sim BUILD_DIR=$(NATIVE_BUILD_DIR) \
		CONFIGURATION=$(CONFIGURATION) test

python-test:
	$(PYTHON) -m pytest

# the SITL simulator: built in the firmware checkout, driven from here.
# SITL_MAKE_FLAGS passes the build variants through, eg
#   make sitl-test SITL_MAKE_FLAGS=SITL_SANITIZE=address
sitl:
	$(MAKE) -C $(AM32_ROOT) AM32_SITL_CAN $(SITL_MAKE_FLAGS)

sitl-test: sitl
	AM32_ROOT=$(AM32_ROOT) $(PYTHON) SITL/run_ci_tests.py

sitl-gui: sitl
	AM32_ROOT=$(AM32_ROOT) $(PYTHON) SITL/sitl_gui.py

package:
	$(PYTHON) scripts/build-package.py --configuration $(CONFIGURATION)

windows-installer: package
	$(PYTHON) scripts/build-windows-installer.py

parity-all-mcus:
	$(PYTHON) scripts/build-package.py --skip-pyinstaller \
		--configuration $(CONFIGURATION)
	$(PYTHON) scripts/run-parity-tests.py --all-mcus \
		--output "$(PARITY_OUTPUT)"

windows-usbip-test:
	$(PYTHON) scripts/run-windows-usbip-test.py

xephyr: xephyr-webserial

xephyr-webserial:
	$(PYTHON) scripts/run-xephyr-webserial-test.py $(XEPHYR_WEBSERIAL_ARGS)

# Synchronize the current working tree into a disposable directory on the
# Windows lab host. Keep its build/dist caches between runs, but delete stale
# source files. The existing Windows-native virtualenv supplies PySide6 and
# PyInstaller without modifying the host's intentionally dirty ESCSim checkout.
win11:
	@case "$(WIN11_DIR)" in ESCSim-win11-*) ;; *) \
		echo "WIN11_DIR must start with ESCSim-win11-" >&2; exit 2;; esac
	ssh "$(WIN11_HOST)" 'mkdir -p "$(WIN11_DIR)"'
	rsync -a --delete \
		--exclude=/.git/ \
		--exclude=/.venv*/ \
		--exclude=/build/ \
		--exclude=/dist/ \
		--exclude=/.pytest_cache/ \
		--exclude=/.ruff_cache/ \
		--exclude='__pycache__/' \
		--exclude='*.egg-info/' \
		--exclude=/base.parm \
		--exclude=/sb405.parm \
		--exclude=/mav.parm \
		--exclude='/mav.tlog*' \
		"$(CURDIR)/" "$(WIN11_HOST):$(WIN11_DIR)/"
	ssh "$(WIN11_HOST)" 'set -eu; \
		cd "$(WIN11_DIR)"; \
		test -x "$(WIN11_PYTHON)"; \
		"$(WIN11_PYTHON)" -m pip install -e ".[test,gui]" pyinstaller; \
		CC=x86_64-w64-mingw32-gcc make windows-installer \
			PYTHON="$(WIN11_PYTHON)"; \
		dist/ESCSim/ESCSim.exe targets status; \
		printf "\nWindows installer ready: "; \
		cygpath -w "$$PWD/dist/installer/ESCSim-installer.exe"'

install: native
	$(PYTHON) scripts/install-package.py --native "$(NATIVE_LIBRARY)" \
		--package '$(PYTHON_PACKAGE)' -- $(PIP_INSTALL_FLAGS)

# Pull the existing immutable releases before building so a publish from a
# fresh checkout retains every older version.  Upload release directories
# first and the mutable catalog/index last, so clients never see references to
# artifacts which have not reached the server yet.
publish:
	@test -d "$(AM32_SOURCE)/.git" || { echo "AM32_SOURCE is not a Git checkout: $(AM32_SOURCE)" >&2; exit 2; }
	@test -d "$(AM32_BOOTLOADER_SOURCE)/.git" || { echo "AM32_BOOTLOADER_SOURCE is not a Git checkout: $(AM32_BOOTLOADER_SOURCE)" >&2; exit 2; }
	@mkdir -p "$(PUBLISH_WORK_DIR)" "$(PUBLISH_SITE_DIR)/v1"
	ssh "$(PUBLISH_HOST)" 'mkdir -p "$(PUBLISH_ROOT)/schemas" "$(PUBLISH_ROOT)/v1/firmware" "$(PUBLISH_ROOT)/v1/bootloader"'
	rsync $(PUBLISH_RSYNC_FLAGS) "$(PUBLISH_HOST):$(PUBLISH_ROOT)/v1/" "$(PUBLISH_SITE_DIR)/v1/"
	rsync -a --delete "$(CURDIR)/schemas/" "$(PUBLISH_SITE_DIR)/schemas/"
	ESCSIM_SOURCE="$(CURDIR)" \
	AM32_SOURCE="$(AM32_SOURCE)" \
	AM32_BOOTLOADER_SOURCE="$(AM32_BOOTLOADER_SOURCE)" \
	AM32_REF="$(AM32_REF)" \
	AM32_BOOTLOADER_REF="$(AM32_BOOTLOADER_REF)" \
	ESCSIM_WORK_ROOT="$(PUBLISH_WORK_DIR)" \
	ESCSIM_DOCUMENT_ROOT="$(PUBLISH_SITE_DIR)" \
	ESCSIM_CHANNEL="$(PUBLISH_CHANNEL)" \
	ESCSIM_JOBS="$(PUBLISH_JOBS)" \
		scripts/publish-from-checkouts.sh
	rsync $(PUBLISH_RSYNC_FLAGS) "$(PUBLISH_SITE_DIR)/v1/firmware/" \
		"$(PUBLISH_HOST):$(PUBLISH_ROOT)/v1/firmware/"
	rsync $(PUBLISH_RSYNC_FLAGS) "$(PUBLISH_SITE_DIR)/v1/bootloader/" \
		"$(PUBLISH_HOST):$(PUBLISH_ROOT)/v1/bootloader/"
	rsync $(PUBLISH_RSYNC_FLAGS) "$(PUBLISH_SITE_DIR)/schemas/" \
		"$(PUBLISH_HOST):$(PUBLISH_ROOT)/schemas/"
	rsync $(PUBLISH_RSYNC_FLAGS) "$(PUBLISH_SITE_DIR)/v1/catalog.json" \
		"$(PUBLISH_SITE_DIR)/v1/index.html" \
		"$(PUBLISH_HOST):$(PUBLISH_ROOT)/v1/"

clean:
	$(MAKE) -C native/am32sim BUILD_DIR=$(NATIVE_BUILD_DIR) clean
	$(RM) $(WHEEL_DIR)/escsim-*.whl

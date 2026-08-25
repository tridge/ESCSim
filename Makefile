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

ifeq ($(OS),Windows_NT)
NATIVE_NAME := am32sim.dll
else ifeq ($(shell uname -s),Darwin)
NATIVE_NAME := libam32sim.dylib
else
NATIVE_NAME := libam32sim.so
endif

NATIVE_LIBRARY := $(NATIVE_BUILD_DIR)/$(NATIVE_NAME)

.DEFAULT_GOAL := all
.PHONY: all native wheel test native-test python-test package install publish clean

all: native wheel

native:
	$(MAKE) -C native/am32sim BUILD_DIR=$(NATIVE_BUILD_DIR) \
		CONFIGURATION=$(CONFIGURATION) all

wheel:
	@mkdir -p $(WHEEL_DIR)
	$(PYTHON) -m pip wheel . --no-deps --no-build-isolation \
		--wheel-dir $(WHEEL_DIR)

test: native-test python-test

native-test:
	$(MAKE) -C native/am32sim BUILD_DIR=$(NATIVE_BUILD_DIR) \
		CONFIGURATION=$(CONFIGURATION) test

python-test:
	$(PYTHON) -m pytest

package:
	$(PYTHON) scripts/build-package.py --configuration $(CONFIGURATION)

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

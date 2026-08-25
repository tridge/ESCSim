#!/bin/sh
set -eu

: "${ESCSIM_SOURCE:?set ESCSIM_SOURCE}"
: "${AM32_SOURCE:?set AM32_SOURCE}"
: "${AM32_BOOTLOADER_SOURCE:?set AM32_BOOTLOADER_SOURCE}"
: "${ESCSIM_WORK_ROOT:=/var/lib/escsim-builder}"
: "${ESCSIM_DOCUMENT_ROOT:=/var/lib/escsim-builder/site}"
: "${ESCSIM_CHANNEL:=nightly}"
: "${ESCSIM_JOBS:=4}"
: "${AM32_REF:=origin/HEAD}"
: "${AM32_BOOTLOADER_REF:=origin/HEAD}"

case $(uname -s) in
    Darwin) tool_os=macos ;;
    Linux) tool_os=linux ;;
    *) tool_os= ;;
esac
if [ -z "${ESCSIM_ARM_SDK_PREFIX:-}" ]; then
    bundled_arm="$AM32_SOURCE/tools/$tool_os/xpack-arm-none-eabi-gcc-10.3.1-2.3/bin/arm-none-eabi-"
    if [ -n "$tool_os" ] && [ -x "${bundled_arm}gcc" ]; then
        ESCSIM_ARM_SDK_PREFIX=$bundled_arm
    else
        ESCSIM_ARM_SDK_PREFIX=arm-none-eabi-
    fi
fi
if [ -z "${ESCSIM_RISCV_SDK_PREFIX:-}" ]; then
    bundled_riscv="$AM32_SOURCE/tools/$tool_os/riscv-embedded-gcc/bin/riscv-none-embed-"
    if [ -n "$tool_os" ] && [ -x "${bundled_riscv}gcc" ]; then
        ESCSIM_RISCV_SDK_PREFIX=$bundled_riscv
    else
        ESCSIM_RISCV_SDK_PREFIX=riscv64-unknown-elf-
    fi
fi

command -v "${ESCSIM_ARM_SDK_PREFIX}gcc" >/dev/null 2>&1 || {
    echo "missing publisher toolchain: ${ESCSIM_ARM_SDK_PREFIX}gcc" >&2
    exit 69
}
command -v "${ESCSIM_RISCV_SDK_PREFIX}gcc" >/dev/null 2>&1 || {
    echo "missing publisher toolchain: ${ESCSIM_RISCV_SDK_PREFIX}gcc" >&2
    exit 69
}

mkdir -p "$ESCSIM_WORK_ROOT" "$ESCSIM_DOCUMENT_ROOT"
exec 9>"$ESCSIM_WORK_ROOT/publisher.lock"
if ! flock -n 9; then
    echo "another ESCSim publisher is already using $ESCSIM_WORK_ROOT" >&2
    exit 75
fi

work=$(mktemp -d "$ESCSIM_WORK_ROOT/run.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM

git -C "$AM32_SOURCE" fetch --prune origin
git -C "$AM32_BOOTLOADER_SOURCE" fetch --prune origin
am32_revision=$(git -C "$AM32_SOURCE" rev-parse "$AM32_REF^{commit}")
bootloader_revision=$(git -C "$AM32_BOOTLOADER_SOURCE" rev-parse "$AM32_BOOTLOADER_REF^{commit}")
git clone --no-checkout --shared "$AM32_SOURCE" "$work/AM32"
git -C "$work/AM32" checkout --detach "$am32_revision"
git clone --no-checkout --shared "$AM32_BOOTLOADER_SOURCE" "$work/AM32-bootloader"
git -C "$work/AM32-bootloader" checkout --detach "$bootloader_revision"

major=$(awk '/define VERSION_MAJOR/{print $3}' "$work/AM32/Inc/version.h")
minor=$(awk '/define VERSION_MINOR/{print $3}' "$work/AM32/Inc/version.h")
bootloader_version=$(awk '/define BOOTLOADER_VERSION/{print $3}' "$work/AM32-bootloader/Inc/version.h")

# Rebuild a complete staged tree. Immutable releases already present on the
# public site are copied first; catalog.json and index.html are replaced last.
mkdir -p "$work/site/v1"
if [ -d "$ESCSIM_DOCUMENT_ROOT/v1" ]; then
    cp -a "$ESCSIM_DOCUMENT_ROOT/v1/." "$work/site/v1/"
fi

firmware_action=add
bootloader_action=add
if PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher contains \
        "$work/site/v1" firmware "$major.$minor"; then
    firmware_action=augment
    if PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher complete \
            "$work/site/v1" firmware "$major.$minor"; then
        firmware_action=none
    fi
fi
if PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher contains \
        "$work/site/v1" bootloader "$bootloader_version"; then
    bootloader_action=augment
    if PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher complete \
            "$work/site/v1" bootloader "$bootloader_version"; then
        bootloader_action=none
    fi
fi

if [ "$firmware_action" != none ]; then
    make -C "$work/AM32" -j "$ESCSIM_JOBS" \
        ARM_SDK_PREFIX="$ESCSIM_ARM_SDK_PREFIX" \
        RISCV_SDK_PREFIX="$ESCSIM_RISCV_SDK_PREFIX"
fi
if [ "$firmware_action" = add ]; then
    PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher build \
        --root "$work/site/v1" --channel "$ESCSIM_CHANNEL" \
        --firmware-release "$major.$minor" --firmware-revision "$am32_revision" \
        --firmware-dir "$work/AM32/obj" --targets "$work/AM32/Inc/targets.h"
elif [ "$firmware_action" = augment ]; then
    PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher augment \
        --root "$work/site/v1" --project firmware --release "$major.$minor" \
        --images "$work/AM32/obj"
fi
if [ "$bootloader_action" != none ]; then
    make -C "$work/AM32-bootloader" -j "$ESCSIM_JOBS" \
        ARM_SDK_PREFIX="$ESCSIM_ARM_SDK_PREFIX" \
        RISCV_SDK_PREFIX="$ESCSIM_RISCV_SDK_PREFIX"
fi
if [ "$bootloader_action" = add ]; then
    PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher build \
        --root "$work/site/v1" --channel "$ESCSIM_CHANNEL" \
        --bootloader-release "$bootloader_version" \
        --bootloader-revision "$bootloader_revision" \
        --bootloader-dir "$work/AM32-bootloader/obj"
elif [ "$bootloader_action" = augment ]; then
    PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher augment \
        --root "$work/site/v1" --project bootloader --release "$bootloader_version" \
        --images "$work/AM32-bootloader/obj"
fi
PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher validate "$work/site/v1"

mkdir -p "$ESCSIM_DOCUMENT_ROOT/v1"
mkdir -p "$ESCSIM_DOCUMENT_ROOT/v1/firmware" "$ESCSIM_DOCUMENT_ROOT/v1/bootloader"
# Existing release payloads came from the document root and remain unchanged;
# augmentation adds images and deliberately replaces that release's manifest.
cp -a "$work/site/v1/firmware/." "$ESCSIM_DOCUMENT_ROOT/v1/firmware/"
cp -a "$work/site/v1/bootloader/." "$ESCSIM_DOCUMENT_ROOT/v1/bootloader/"
cp "$work/site/v1/catalog.json" "$ESCSIM_DOCUMENT_ROOT/v1/.catalog.json.new"
mv "$ESCSIM_DOCUMENT_ROOT/v1/.catalog.json.new" "$ESCSIM_DOCUMENT_ROOT/v1/catalog.json"
cp "$work/site/v1/index.html" "$ESCSIM_DOCUMENT_ROOT/v1/.index.html.new"
mv "$ESCSIM_DOCUMENT_ROOT/v1/.index.html.new" "$ESCSIM_DOCUMENT_ROOT/v1/index.html"

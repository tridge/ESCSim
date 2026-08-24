#!/bin/sh
set -eu

: "${ESCSIM_SOURCE:?set ESCSIM_SOURCE}"
: "${AM32_SOURCE:?set AM32_SOURCE}"
: "${AM32_BOOTLOADER_SOURCE:?set AM32_BOOTLOADER_SOURCE}"
: "${ESCSIM_WORK_ROOT:=/var/lib/escsim-builder}"
: "${ESCSIM_DOCUMENT_ROOT:=/var/www/am32.tridgell.net/html/ESCSim}"
: "${ESCSIM_CHANNEL:=nightly}"
: "${ESCSIM_JOBS:=4}"

mkdir -p "$ESCSIM_WORK_ROOT" "$ESCSIM_DOCUMENT_ROOT"
exec 9>"$ESCSIM_WORK_ROOT/publisher.lock"
flock -n 9 || exit 0

work=$(mktemp -d "$ESCSIM_WORK_ROOT/run.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM

git -C "$AM32_SOURCE" fetch --prune origin
git -C "$AM32_BOOTLOADER_SOURCE" fetch --prune origin
am32_revision=$(git -C "$AM32_SOURCE" rev-parse origin/main)
bootloader_revision=$(git -C "$AM32_BOOTLOADER_SOURCE" rev-parse origin/main)
git clone --no-checkout --shared "$AM32_SOURCE" "$work/AM32"
git -C "$work/AM32" checkout --detach "$am32_revision"
git clone --no-checkout --shared "$AM32_BOOTLOADER_SOURCE" "$work/AM32-bootloader"
git -C "$work/AM32-bootloader" checkout --detach "$bootloader_revision"

make -C "$work/AM32" -j "$ESCSIM_JOBS"
make -C "$work/AM32-bootloader" -j "$ESCSIM_JOBS"

major=$(awk '/define VERSION_MAJOR/{print $3}' "$work/AM32/Inc/version.h")
minor=$(awk '/define VERSION_MINOR/{print $3}' "$work/AM32/Inc/version.h")
bootloader_version=$(awk '/define BOOTLOADER_VERSION/{print $3}' "$work/AM32-bootloader/Inc/version.h")

# Rebuild a complete staged tree. Immutable releases already present on the
# public site are copied first; catalog.json and index.html are replaced last.
mkdir -p "$work/site/v1"
if [ -d "$ESCSIM_DOCUMENT_ROOT/v1" ]; then
    cp -a "$ESCSIM_DOCUMENT_ROOT/v1/." "$work/site/v1/"
fi

if ! PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher contains \
        "$work/site/v1" firmware "$major.$minor"; then
    PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher build \
        --root "$work/site/v1" --channel "$ESCSIM_CHANNEL" \
        --firmware-release "$major.$minor" --firmware-revision "$am32_revision" \
        --firmware-dir "$work/AM32/obj" --targets "$work/AM32/Inc/targets.h"
fi
if ! PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher contains \
        "$work/site/v1" bootloader "$bootloader_version"; then
    PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher build \
        --root "$work/site/v1" --channel "$ESCSIM_CHANNEL" \
        --bootloader-release "$bootloader_version" \
        --bootloader-revision "$bootloader_revision" \
        --bootloader-dir "$work/AM32-bootloader/obj"
fi
PYTHONPATH="$ESCSIM_SOURCE/src" python3 -m escsim.artifacts.publisher validate "$work/site/v1"

mkdir -p "$ESCSIM_DOCUMENT_ROOT/v1"
mkdir -p "$ESCSIM_DOCUMENT_ROOT/v1/firmware" "$ESCSIM_DOCUMENT_ROOT/v1/bootloader"
cp -an "$work/site/v1/firmware/." "$ESCSIM_DOCUMENT_ROOT/v1/firmware/"
cp -an "$work/site/v1/bootloader/." "$ESCSIM_DOCUMENT_ROOT/v1/bootloader/"
cp "$work/site/v1/catalog.json" "$ESCSIM_DOCUMENT_ROOT/v1/.catalog.json.new"
mv "$ESCSIM_DOCUMENT_ROOT/v1/.catalog.json.new" "$ESCSIM_DOCUMENT_ROOT/v1/catalog.json"
cp "$work/site/v1/index.html" "$ESCSIM_DOCUMENT_ROOT/v1/.index.html.new"
mv "$ESCSIM_DOCUMENT_ROOT/v1/.index.html.new" "$ESCSIM_DOCUMENT_ROOT/v1/index.html"

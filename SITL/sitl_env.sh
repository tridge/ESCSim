# shared shell helpers for the SITL run scripts: locate the AM32
# firmware and bootloader checkouts and the binaries built in them.
#
# AM32_ROOT / AM32_BOOTLOADER_ROOT override the pinned submodules.
# Sourced, not executed.

SITL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

am32_root() {
    local root="${AM32_ROOT:-$SITL_DIR/../modules/am32-firmware}"
    if [ ! -f "$root/Inc/version.h" ]; then
        echo "no AM32 checkout at $root: set AM32_ROOT, or run" >&2
        echo "  git submodule update --init modules/am32-firmware" >&2
        return 1
    fi
    (cd "$root" && pwd)
}

# the built SITL binary, without hardcoding the firmware version
am32_sitl_elf() {
    local root
    root="$(am32_root)" || return 1
    local hits=("$root"/obj/AM32_AM32_SITL_CAN_*.elf)
    echo "${hits[-1]}"
}

# the SITL bootloader to chain into, if one has been built. Empty when
# there is none: the app runs fine on its own
am32_bootloader_elf() {
    local root="${AM32_BOOTLOADER_ROOT:-$SITL_DIR/../modules/am32-bootloader}"
    local hits=("$root"/obj/AM32_SITL_BOOTLOADER_*.elf)
    [ -f "${hits[0]}" ] && echo "${hits[0]}"
}

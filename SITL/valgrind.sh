#!/bin/bash
# run the SITL under valgrind memcheck
#
# --fair-sched=yes is essential: valgrind serialises threads behind one
# lock and the default scheduler lets the firmware thread hold it
# through its startup-tune busy-wait, starving the sim thread so sim
# time never advances. stdbuf line-buffers the output (piped stdout
# would otherwise buffer and show nothing). --trace-children keeps
# valgrind attached across the SITL's execv on reset/reboot.
#
# expect ~1/10 real time; the measurement tools should be run with
# --sim-state 127.0.0.1:57734 so they pace against simulation time.
set -e
. "$(dirname "$0")/sitl_env.sh"

BOOTLOADER="$(am32_bootloader_elf)"
BOOT=()
[ -n "$BOOTLOADER" ] && BOOT=(--bootloader "$BOOTLOADER")

exec stdbuf -oL -eL valgrind -q --fair-sched=yes --trace-children=yes \
    --error-exitcode=0 \
    "$(am32_sitl_elf)" --verbose "${BOOT[@]}" "$@"

#!/bin/bash
# build the SITL with coverage, run the test suite, and produce a gcov
# report over the firmware sources.
#
#   SITL/run_coverage.sh                # text summary + html in coverage/
#   SITL/run_coverage.sh --no-build     # reuse the current build/gcda
#
# coverage measures the real ESC firmware (Src/) plus the SITL harness
# (Mcu/SITL), both of which live in the AM32 checkout; the generated
# DSDL and libcanard vendor code are excluded. AM32_ROOT selects the
# checkout, defaulting to modules/am32-firmware.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
AM32="${AM32_ROOT:-$HERE/../modules/am32-firmware}"
if [ ! -f "$AM32/Inc/version.h" ]; then
    echo "no AM32 checkout at $AM32: set AM32_ROOT, or run" >&2
    echo "  git submodule update --init modules/am32-firmware" >&2
    exit 1
fi
AM32="$(cd "$AM32" && pwd)"

BUILD=1
[ "$1" = "--no-build" ] && BUILD=0

if [ "$BUILD" = 1 ]; then
    rm -f "$AM32"/obj/AM32_AM32_SITL_CAN_*.elf "$AM32"/obj/AM32_AM32_SITL_CAN_*.d \
          "$AM32"/obj/*.gcda "$AM32"/obj/*.gcno
    make -C "$AM32" AM32_SITL_CAN SITL_COVERAGE=1
fi
rm -f "$AM32"/obj/*.gcda

mkdir -p coverage
COVERAGE="$(cd coverage && pwd)"
python3 "$HERE/run_ci_tests.py" || true

FILTERS=(--filter 'Src/' --filter 'Mcu/SITL/sim/' --filter 'Mcu/SITL/Src/'
         --exclude 'Src/DroneCAN/dsdl_generated/'
         --exclude 'Src/DroneCAN/libcanard/')

echo "=== coverage summary ==="
gcovr --gcov-ignore-parse-errors --root "$AM32" --object-directory "$AM32/obj" \
    "${FILTERS[@]}" --print-summary --txt "$COVERAGE/coverage.txt"

gcovr --gcov-ignore-parse-errors --root "$AM32" --object-directory "$AM32/obj" \
    "${FILTERS[@]}" --html-details "$COVERAGE/index.html"
echo "html report: $COVERAGE/index.html"

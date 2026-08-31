"""Renode launch support for emulated flight controllers."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import json
import os
from pathlib import Path
import struct

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

from escsim.renode.generator import (
    Unsupported,
    find_renode,
    isolated_renode_config,
    parse_ihex,
    renode_execfile,
    renode_path,
)


FLASH_BASE = 0x08000000
FLASH_SIZE = 1024 * 1024
BETAFLIGHT_POLL_TRAMPOLINE = FLASH_BASE + FLASH_SIZE - 16
BETAFLIGHT_SCHEDULER_TRAMPOLINE = FLASH_BASE + FLASH_SIZE - 48
BETAFLIGHT_SOURCE_SPEEDUP_MARKER = b"ESCSIM_SPEEDUP_V1\x00"
BETAFLIGHT_GYRO_RATE_ORIGINAL = bytes.fromhex(
    "122b15bf052202224ff47a704ff4487014bf4ff4fa514ff44861002384f81f21"
)
BETAFLIGHT_GYRO_RATE_1KHZ = bytes.fromhex(
    "122b15bf002202224ff47a704ff4487014bf4ff47a714ff44861002384f81f21"
)
# Both bundled F405 firmwares reserve flash sector 1 for configuration
# (ArduPilot uses STORAGE_FLASH_PAGE 1). Their HEX files are dense and contain
# erased bytes over this sector, so repeat-start matching must ignore settings
# written there by either firmware.
FC_CONFIG_RANGES = ((0x4000, 0x8000),)
USBIP_PORT_OFFSET = 2
MONITOR_PORT_OFFSET = 4
FC_FIRMWARE_BASE_URL = (
    "https://firmware.ardupilot.org/Tools/AM32-tools/ESCSim/FC_Firmware/"
)
FC_FIRMWARES = {
    "SPEEDYBEEF405V5": {
        "filename": "SPEEDYBEEF405V5.hex",
        "label": "Betaflight 2026.6.1 (SPEEDYBEEF405V5)",
    },
    "ARDUPILOT_SPEEDYBEEF405MINI": {
        "filename": "ARDUPILOT_SPEEDYBEEF405MINI.hex",
        "label": "ArduPilot Copter 4.8.0-dev (SpeedyBeeF405Mini)",
    },
}


@dataclass(frozen=True)
class FlightControllerSpec:
    """Everything needed for one SpeedyBeeF405Mini Renode process."""

    model: str
    outdir: Path
    flash: Path
    esc_ports: tuple[int, ...]
    esc_state_ports: tuple[int, ...]
    monitor_port: int
    usbip_port: int
    renode: str | None = None
    symbols: Path | None = None

    def command(self) -> list[str]:
        if self.model != "SpeedyBeeF405Mini":
            raise ValueError(f"unsupported flight controller {self.model}")
        platform = write_speedybee_platform(
            self.outdir,
            self.esc_ports,
            self.flash,
            self.esc_state_ports,
        )
        source_speedup = has_betaflight_source_speedup(self.flash)
        if source_speedup:
            hotpatches = None
        elif self.symbols is not None:
            hotpatches = betaflight_hotpatches(self.symbols, self.flash)
        else:
            try:
                hotpatches = recognize_betaflight_hotpatches(self.flash)
            except ValueError:
                # Unknown firmware retains the complete instruction-level
                # implementation. Recognition is an optional optimization.
                hotpatches = None
        script = write_speedybee_script(
            self.outdir,
            platform,
            self.flash,
            self.usbip_port,
            hotpatches=hotpatches,
            source_speedup=source_speedup,
        )
        config = isolated_renode_config(self.outdir / "renode-config")
        return [
            find_renode(self.renode),
            "--disable-xwt",
            "--config",
            str(config),
            "-e",
            f"include @{renode_path(script)}",
            "--port",
            str(self.monitor_port),
        ]


def resource_root() -> Path:
    return Path(os.fspath(resources.files("escsim.renode").joinpath("resources")))


def has_betaflight_source_speedup(flash: Path) -> bool:
    """Return whether a flash image contains the opt-in source profile."""
    return BETAFLIGHT_SOURCE_SPEEDUP_MARKER in flash.read_bytes()


def flight_controller_firmwares() -> tuple[str, ...]:
    """Stable firmware IDs offered by the FC firmware selector."""
    return tuple(FC_FIRMWARES)


def flight_controller_firmware(name: str) -> Path:
    """Resolve one bundled image; future releases use FC_FIRMWARE_BASE_URL."""
    try:
        filename = FC_FIRMWARES[name]["filename"]
    except KeyError as error:
        raise ValueError(f"unsupported flight-controller firmware {name}") from error
    image = resource_root() / "FC_Firmware" / filename
    if not image.is_file():
        raise RuntimeError(f"missing bundled flight-controller firmware {image}")
    return image


def flight_controller_firmware_label(name: str) -> str:
    try:
        return FC_FIRMWARES[name]["label"]
    except KeyError as error:
        raise ValueError(f"unsupported flight-controller firmware {name}") from error


@dataclass(frozen=True)
class BetaflightHotPatches:
    """Verified Betaflight hook addresses, from symbols or code signatures."""

    symbols: Path | None
    delay: int | None
    scheduler: int | None
    scheduler_wait_poll: int
    gyro_sample_rate_setup: int
    serial_task_period: int
    usb_sof_interval_compares: tuple[int, ...]
    system_state: int | None
    systick_uptime: int | None
    read_byte_crc_poll: int
    selected_esc: int
    bl_send_buf: int
    set_esc_input: int
    suart_getc: int
    suart_putc: int


def _unique_sequence(data: bytes, sequence: bytes, name: str) -> int:
    """Return the sole byte offset of a machine-code sequence."""
    offsets = []
    start = 0
    while True:
        offset = data.find(sequence, start)
        if offset < 0:
            break
        offsets.append(offset)
        start = offset + 1
    if len(offsets) != 1:
        raise ValueError(
            f"flight-controller firmware has {len(offsets)} {name} sequences"
        )
    return offsets[0]


def _thumb_bw(source: int, target: int) -> tuple[int, int]:
    """Encode a Thumb-2 unconditional B.W from source to target."""
    offset = target - (source + 4)
    if offset & 1 or not -(1 << 24) <= offset < (1 << 24):
        raise ValueError("Thumb B.W target is out of range or unaligned")
    encoded = offset & ((1 << 25) - 1)
    sign = (encoded >> 24) & 1
    i1 = (encoded >> 23) & 1
    i2 = (encoded >> 22) & 1
    j1 = ((~i1) & 1) ^ sign
    j2 = ((~i2) & 1) ^ sign
    return (
        0xF000 | (sign << 10) | ((encoded >> 12) & 0x3FF),
        0x9000 | (j1 << 13) | (j2 << 11) | ((encoded >> 1) & 0x7FF),
    )


def _thumb_bw_target(source: int, first: int, second: int) -> int:
    """Decode a Thumb-2 unconditional B.W target."""
    if first & 0xF800 != 0xF000 or second & 0xD000 != 0x9000:
        raise ValueError("invalid Thumb B.W instruction")
    sign = (first >> 10) & 1
    j1 = (second >> 13) & 1
    j2 = (second >> 11) & 1
    i1 = (~(j1 ^ sign)) & 1
    i2 = (~(j2 ^ sign)) & 1
    encoded = (
        (sign << 24)
        | (i1 << 23)
        | (i2 << 22)
        | ((first & 0x3FF) << 12)
        | ((second & 0x7FF) << 1)
    )
    if encoded & (1 << 24):
        encoded -= 1 << 25
    return source + 4 + encoded


def _read_byte_crc_wfi_patch(poll: int) -> tuple[tuple[int, int], ...]:
    """Replace ReadByteCrc's busy branch with an interruptible WFI loop."""
    cave = BETAFLIGHT_POLL_TRAMPOLINE
    patch = _thumb_bw(poll, cave)
    # The original instructions at poll are `cmp r0,#0; beq retry`.  Route
    # both paths through an erased-flash trampoline: ready resumes at the
    # following LDR, while empty sleeps until the next USB/SysTick interrupt
    # and retries serialRxBytesWaiting.  This avoids a Python hook on every
    # received byte as well as the millions of guest polling instructions.
    trampoline = (
        0x2800,  # cmp r0, #0
        0xD001,  # beq wait
        *_thumb_bw(cave + 4, poll + 4),
        0xBF30,  # wait: wfi
        *_thumb_bw(cave + 10, poll - 6),
        0xBF00,  # alignment/padding
    )
    words = ((poll, patch[0]), (poll + 2, patch[1]))
    words += tuple((cave + index * 2, value) for index, value in enumerate(trampoline))
    return words


def _scheduler_wfi_patch(poll: int) -> tuple[tuple[int, int], ...]:
    """Sleep between Betaflight gyro scheduling boundaries instead of spinning."""
    cave = BETAFLIGHT_SCHEDULER_TRAMPOLINE
    patch = _thumb_bw(poll, cave)
    # Preserve the scheduler's DWT cycle-counter comparison.  A pending USB,
    # SysTick or gyro interrupt wakes the CPU, after which the comparison is
    # retried until the target boundary has actually arrived.
    trampoline = (
        0x6854,  # ldr r4, [r2, #4] (DWT_CYCCNT)
        0x1B0B,  # subs r3, r1, r4
        0x2B00,  # cmp r3, #0
        0xDC01,  # bgt wait
        *_thumb_bw(cave + 8, poll + 8),
        0xBF30,  # wait: wfi
        *_thumb_bw(cave + 14, poll),
        0xBF00,
    )
    words = ((poll, patch[0]), (poll + 2, patch[1]))
    words += tuple((cave + index * 2, value) for index, value in enumerate(trampoline))
    return words


def _restore_existing_instruction_patches(data: bytearray) -> None:
    """Normalize exact persisted patches back to their recognized sequences."""
    # The emulation-only gyro-rate patch changes two immediates in one exact,
    # contextualized gyroSetSampleRate sequence. Renode's flash controller
    # persists guest writes, so normalize a previous run before identity and
    # signature checks just as for the WFI and USB patches below.
    original_gyro_count = data.count(BETAFLIGHT_GYRO_RATE_ORIGINAL)
    patched_gyro_count = data.count(BETAFLIGHT_GYRO_RATE_1KHZ)
    if patched_gyro_count:
        if patched_gyro_count != 1 or original_gyro_count:
            raise ValueError(
                "flight-controller firmware has an invalid gyro-rate patch"
            )
        offset = data.find(BETAFLIGHT_GYRO_RATE_1KHZ)
        data[offset : offset + len(BETAFLIGHT_GYRO_RATE_ORIGINAL)] = (
            BETAFLIGHT_GYRO_RATE_ORIGINAL
        )

    # Betaflight's legacy F4 CDC implementation normally drains its transmit
    # ring every 16 one-millisecond SOF interrupts. ESCSim changes this
    # compare from 15 to 0 so a slow guest drains it on every USB frame.
    # Normalize that exact contextualized change before matching persisted
    # flash.
    original_sof = bytes.fromhex("54f83c3c0f2b04d0")
    coalesced_sof = bytes.fromhex("54f83c3c002b04d0")
    original_count = data.count(original_sof)
    coalesced_count = data.count(coalesced_sof)
    if coalesced_count:
        if coalesced_count != 2 or original_count:
            raise ValueError(
                "flight-controller firmware has an invalid USB SOF interval patch"
            )
        start = 0
        for _ in range(coalesced_count):
            offset = data.find(coalesced_sof, start)
            data[offset + 4 : offset + 6] = b"\x0f\x2b"
            start = offset + len(coalesced_sof)

    poll_cave = BETAFLIGHT_POLL_TRAMPOLINE - FLASH_BASE
    if data[poll_cave : poll_cave + 16] != b"\xff" * 16:
        words = struct.unpack_from("<8H", data, poll_cave)
        if words[:2] != (0x2800, 0xD001) or words[4] != 0xBF30 or words[7] != 0xBF00:
            raise ValueError(
                "flight-controller firmware has an invalid polling trampoline"
            )
        poll = _thumb_bw_target(BETAFLIGHT_POLL_TRAMPOLINE + 4, words[2], words[3]) - 4
        if _thumb_bw_target(
            BETAFLIGHT_POLL_TRAMPOLINE + 10, words[5], words[6]
        ) != poll - 6 or struct.unpack_from(
            "<HH", data, poll - FLASH_BASE
        ) != _thumb_bw(poll, BETAFLIGHT_POLL_TRAMPOLINE):
            raise ValueError("flight-controller polling trampoline targets are invalid")
        struct.pack_into("<HH", data, poll - FLASH_BASE, 0x2800, 0xD0FA)
        data[poll_cave : poll_cave + 16] = b"\xff" * 16

    scheduler_cave = BETAFLIGHT_SCHEDULER_TRAMPOLINE - FLASH_BASE
    if data[scheduler_cave : scheduler_cave + 32] != b"\xff" * 32:
        words = struct.unpack_from("<10H", data, scheduler_cave)
        if (
            words[:4] != (0x6854, 0x1B0B, 0x2B00, 0xDC01)
            or words[6] != 0xBF30
            or words[9] != 0xBF00
            or data[scheduler_cave + 20 : scheduler_cave + 32] != b"\xff" * 12
        ):
            raise ValueError(
                "flight-controller firmware has an invalid scheduler trampoline"
            )
        poll = _thumb_bw_target(
            BETAFLIGHT_SCHEDULER_TRAMPOLINE + 14, words[7], words[8]
        )
        if _thumb_bw_target(
            BETAFLIGHT_SCHEDULER_TRAMPOLINE + 8, words[4], words[5]
        ) != poll + 8 or struct.unpack_from(
            "<HH", data, poll - FLASH_BASE
        ) != _thumb_bw(poll, BETAFLIGHT_SCHEDULER_TRAMPOLINE):
            raise ValueError(
                "flight-controller scheduler trampoline targets are invalid"
            )
        data[poll - FLASH_BASE : poll - FLASH_BASE + 8] = bytes.fromhex(
            "54680b1b002bfbdc"
        )
        data[scheduler_cave : scheduler_cave + 32] = b"\xff" * 32


def _matches_selected_firmware(
    current: bytes | bytearray, offset: int, payload: bytes
) -> bool:
    """Compare immutable firmware bytes, excluding saved configuration."""
    spans = [(offset, offset + len(payload))]
    for preserve_start, preserve_end in FC_CONFIG_RANGES:
        next_spans = []
        for start, end in spans:
            if end <= preserve_start or start >= preserve_end:
                next_spans.append((start, end))
                continue
            if start < preserve_start:
                next_spans.append((start, preserve_start))
            if end > preserve_end:
                next_spans.append((preserve_end, end))
        spans = next_spans
    return all(
        current[start:end] == payload[start - offset : end - offset]
        for start, end in spans
    )


def _flash_word(data: bytes, address: int) -> int:
    offset = address - FLASH_BASE
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("flight-controller code references outside flash")
    return struct.unpack_from("<I", data, offset)[0]


def _thumb_literal16(data: bytes, offset: int, register: int) -> int:
    instruction = struct.unpack_from("<H", data, offset)[0]
    if instruction & 0xF800 != 0x4800 or (instruction >> 8) & 7 != register:
        raise ValueError("flight-controller firmware has an invalid literal load")
    pc = (FLASH_BASE + offset + 4) & ~3
    return _flash_word(data, pc + (instruction & 0xFF) * 4)


def _thumb_literal32(data: bytes, offset: int, register: int) -> int:
    first, second = struct.unpack_from("<HH", data, offset)
    if first != 0xF8DF or second >> 12 != register:
        raise ValueError("flight-controller firmware has an invalid wide literal load")
    pc = (FLASH_BASE + offset + 4) & ~3
    return _flash_word(data, pc + (second & 0xFFF))


def _thumb_bl(data: bytes, offset: int) -> bool:
    first, second = struct.unpack_from("<HH", data, offset)
    return first & 0xF800 == 0xF000 and second & 0xD000 == 0xD000


def _thumb_branch_w(data: bytes, offset: int) -> bool:
    first, second = struct.unpack_from("<HH", data, offset)
    return first & 0xF800 == 0xF000 and second & 0xD000 == 0x9000


def _thumb_branch16(data: bytes, offset: int) -> int:
    instruction = struct.unpack_from("<H", data, offset)[0]
    if instruction & 0xF800 != 0xE000:
        raise ValueError("flight-controller firmware has an invalid tail branch")
    displacement = (instruction & 0x7FF) << 1
    if displacement & 0x800:
        displacement -= 0x1000
    return FLASH_BASE + offset + 4 + displacement


def _is_sram(address: int) -> bool:
    return 0x10000000 <= address < 0x10010000 or 0x20000000 <= address < 0x20040000


def recognize_betaflight_hotpatches(flash: Path) -> BetaflightHotPatches:
    """Recognize supported Betaflight Thumb sequences without an ELF.

    Short anchors locate candidates, then surrounding instructions, literal
    loads and cross-function data references are checked.  Every anchor must
    be unique; an absent, changed or duplicated sequence rejects the complete
    fast path rather than risking a hook at a coincidental address.
    """
    data = bytearray(Path(flash).read_bytes())
    if len(data) != FLASH_SIZE:
        raise ValueError(f"FC flash image must be {FLASH_SIZE} bytes: {flash}")
    _restore_existing_instruction_patches(data)
    cave_offset = BETAFLIGHT_POLL_TRAMPOLINE - FLASH_BASE
    if data[cave_offset : cave_offset + 16] != b"\xff" * 16:
        raise ValueError("flight-controller firmware has no polling patch trampoline")
    scheduler_cave_offset = BETAFLIGHT_SCHEDULER_TRAMPOLINE - FLASH_BASE
    if data[scheduler_cave_offset : scheduler_cave_offset + 32] != b"\xff" * 32:
        raise ValueError("flight-controller firmware has no scheduler patch trampoline")
    initial_sp, reset = struct.unpack_from("<II", data)
    if (
        not _is_sram(initial_sp)
        or not reset & 1
        or not (FLASH_BASE <= reset & ~1 < FLASH_BASE + FLASH_SIZE)
    ):
        raise ValueError("flight-controller firmware has invalid ARM vectors")

    # ReadByteCrc: call serialRxBytesWaiting; cmp r0,#0; branch to retry;
    # then load the port again and call serialRead.
    poll_anchor = _unique_sequence(
        data, b"\x00\x28\xfa\xd0\x28\x68", "ReadByteCrc poll"
    )
    read_byte_crc = poll_anchor - 0x0C
    if (
        read_byte_crc < 0
        or data[read_byte_crc : read_byte_crc + 8]
        != b"\x38\xb5\x0f\x4d\x04\x46\x28\x68"
        or not _thumb_bl(data, read_byte_crc + 8)
        or not _thumb_bl(data, read_byte_crc + 0x12)
        or data[read_byte_crc + 0x16 : read_byte_crc + 0x20]
        != b"\x0b\x49\x20\x70\x0b\x88\x41\xf2\x21\x04"
    ):
        raise ValueError("flight-controller firmware has an invalid ReadByteCrc")

    # suart_putc's distinctive construction of start/data/stop bits.  Its two
    # literal loads point at selected_esc and escHardware respectively.
    suart_putc = _unique_sequence(
        data, b"\xf8\xb5\x40\xf2\x01\x44\x44\xea\x80\x04", "suart_putc"
    )
    if (
        not _thumb_bl(data, suart_putc + 0x0A)
        or data[suart_putc + 0x12 : suart_putc + 0x1E]
        != b"\x05\x46\x3b\x78\x56\xf8\x23\x00\xe3\x07\x0a\xd5"
        or data[suart_putc + 0x3A : suart_putc + 0x3C] != b"\xf8\xbd"
    ):
        raise ValueError("flight-controller firmware has an invalid suart_putc")
    selected_esc = _thumb_literal16(data, suart_putc + 0x0E, 7)
    putc_hardware = _thumb_literal16(data, suart_putc + 0x10, 6)

    # suart_getc polls the selected GPIO until its two-millisecond deadline,
    # then samples ten bits at 52 us intervals.  Recover its globals from the
    # actual PC-relative loads instead of embedding their addresses.
    getc_anchor = _unique_sequence(
        data, b"\x96\xf8\xe4\x80\x59\xf8\x28\x00", "suart_getc poll"
    )
    suart_getc = getc_anchor - 0x12
    if (
        suart_getc < 0
        or data[suart_getc : suart_getc + 4] != b"\x2d\xe9\xf0\x47"
        or data[suart_getc + 8 : suart_getc + 0x12]
        != b"\x2c\x68\xdf\xf8\x7c\x90\x07\x46\x02\x34"
        or not _thumb_bl(data, suart_getc + 0x1A)
        or data[suart_getc + 0x6A : suart_getc + 0x78]
        != b"\x40\xf2\x01\x23\x23\x40\xb3\xf5\x00\x7f\xf6\xd1\x64\x08"
    ):
        raise ValueError("flight-controller firmware has an invalid suart_getc")
    systick_uptime = _thumb_literal16(data, suart_getc + 4, 5)
    selected_base = _thumb_literal16(data, suart_getc + 6, 6)
    getc_hardware = _thumb_literal32(data, suart_getc + 0x0A, 9)
    if selected_base + 0xE4 != selected_esc or getc_hardware != putc_hardware:
        raise ValueError("flight-controller 4-way globals are inconsistent")

    # scheduler() polls DWT_CYCCNT at a gyro scheduling boundary.  This exact
    # eight-byte backwards loop is replaced with a compare/WFI/retry
    # trampoline, so unrelated task execution remains unchanged.
    scheduler_wait_poll = _unique_sequence(
        data, bytes.fromhex("54680b1b002bfbdc"), "scheduler cycle wait"
    )
    if not _thumb_bl(data, scheduler_wait_poll + 8):
        raise ValueError("flight-controller firmware has an invalid scheduler wait")

    # gyroSetSampleRate is LTO-inlined into gyroInit. This target's ICM42688
    # takes the default 8kHz branch. Replacing its enum and sample-rate
    # immediates with GYRO_RATE_1_kHz/1000 makes both the Betaflight scheduler
    # and the ICM's programmed ODR 1kHz, avoiding seven redundant SPI/DMA/IRQ
    # cycles out of every eight while preserving the real sensor path.
    gyro_sample_rate_setup = _unique_sequence(
        data, BETAFLIGHT_GYRO_RATE_ORIGINAL, "gyro sample-rate setup"
    )

    # Recover task_attributes from tasksInitData rather than embedding its
    # RAM address. TASK_SERIAL is index 8 in this exact task table and its
    # desiredPeriodUs member is 16 bytes into the 24-byte attribute record.
    tasks_init_data = _unique_sequence(
        data,
        bytes.fromhex("064b074803f52271002240f8223018338b4202f11202f8d1"),
        "tasksInitData",
    )
    if data[tasks_init_data + 0x18 : tasks_init_data + 0x1C] != bytes.fromhex(
        "704700bf"
    ):
        raise ValueError("flight-controller firmware has an invalid tasksInitData")
    task_attributes = _thumb_literal16(data, tasks_init_data, 3)
    tasks = _thumb_literal16(data, tasks_init_data + 2, 0)
    if not _is_sram(task_attributes) or not _is_sram(tasks):
        raise ValueError("flight-controller task tables are outside SRAM")
    serial_task_period = task_attributes + 8 * 24 + 16

    # The legacy STM32F4 CDC class sends queued serial data after every 16th
    # SOF.  Locate the counter compare with enough surrounding instructions
    # to make the on-launch patch safe and firmware-specific.
    usb_sof_sequence = bytes.fromhex("54f83c3c0f2b04d0")
    usb_sof_anchors = []
    start = 0
    while True:
        offset = data.find(usb_sof_sequence, start)
        if offset < 0:
            break
        usb_sof_anchors.append(offset)
        start = offset + 1
    # Betaflight builds both the plain CDC and HID/CDC wrapper callbacks.
    # Either can be selected by the target's runtime USB class table.
    if len(usb_sof_anchors) != 2:
        raise ValueError(
            f"flight-controller firmware has {len(usb_sof_anchors)} USB SOF interval sequences"
        )
    usb_sof_interval_compares = tuple(offset + 4 for offset in usb_sof_anchors)

    # setEscInput is also BL_SendBuf's tail-call target.  Validate that edge so
    # the flush hook cannot be confused with another GPIO configuration call.
    set_esc_input = _unique_sequence(
        data, b"\x02\x4b\x20\x21\x53\xf8\x20\x00", "setEscInput"
    )
    bl_send_buf = set_esc_input + 0x10
    tail = _unique_sequence(data, b"\x38\x46\xbd\xe8\xf8\x40", "BL_SendBuf tail")
    if (
        not _thumb_branch_w(data, set_esc_input + 8)
        or _thumb_literal16(data, set_esc_input, 3) != putc_hardware
        or data[bl_send_buf : bl_send_buf + 0x1C]
        != bytes.fromhex("f8b5154e154b96f8e47004460d4653f827000121cff74afa25440023")
        or _thumb_literal16(data, bl_send_buf + 2, 6) != selected_base
        or _thumb_literal16(data, bl_send_buf + 4, 3) != putc_hardware
        or not bl_send_buf <= tail <= bl_send_buf + 0x70
        or _thumb_branch16(data, tail + 6) != FLASH_BASE + set_esc_input
    ):
        raise ValueError("flight-controller firmware has an invalid BL_SendBuf tail")
    if not all(
        _is_sram(value) for value in (selected_esc, systick_uptime, putc_hardware)
    ):
        raise ValueError("flight-controller 4-way literals are outside SRAM")

    # Startup acceleration is independent of 4-way.  If its signatures evolve,
    # retain the recognized 4-way path and simply run normal startup delays.
    delay = scheduler = system_state = None
    try:
        delay_anchor = _unique_sequence(
            data, b"\x4f\xf4\x7a\x7c\x4f\xf0\x01\x08", "delay"
        )
        delay = delay_anchor - 0x10
        if (
            delay < 0
            or data[delay : delay + 4] != b"\x2d\xe9\xf0\x47"
            or data[delay + 8 : delay + 0x10] != b"\x47\x1e\x10\x26\x4f\xf0\xe0\x20"
            or data[delay + 0x2A : delay + 0x38]
            != b"\xd3\xf8\xd4\x26\x84\x69\xd3\xf8\xd4\x16\x8a\x42\xf8\xd1"
        ):
            raise ValueError("invalid delay sequence")

        scheduler_anchor = _unique_sequence(
            data, b"\x04\x93\x00\x2b\x40\xf0", "scheduler entry"
        )
        scheduler = scheduler_anchor - 0x10
        if (
            scheduler < 0
            or data[scheduler : scheduler + 6] != b"\x2d\xe9\xf0\x4f\x89\xb0"
            or not _thumb_bl(data, scheduler + 6)
            or data[scheduler + 0x0A : scheduler + 0x10] != b"\x9a\x4b\x07\x90\x1b\x78"
            or not scheduler <= scheduler_wait_poll < scheduler + 0x800
        ):
            raise ValueError("invalid scheduler sequence")

        state_anchor = _unique_sequence(
            data, b"\x1b\x78\x03\xf0\x05\x03\x05\x2b", "systemState use"
        )
        state_function = state_anchor - 0x1C
        if (
            state_function < 0
            or data[state_function : state_function + 6] != b"\x08\xb5\x01\x21\x00\x20"
        ):
            raise ValueError("invalid systemState sequence")
        system_state = _thumb_literal16(data, state_function + 0x1A, 3)
        if not _is_sram(system_state):
            raise ValueError("systemState is outside SRAM")
    except ValueError:
        delay = scheduler = system_state = None

    return BetaflightHotPatches(
        symbols=None,
        delay=FLASH_BASE + delay if delay is not None else None,
        scheduler=FLASH_BASE + scheduler if scheduler is not None else None,
        scheduler_wait_poll=FLASH_BASE + scheduler_wait_poll,
        gyro_sample_rate_setup=FLASH_BASE + gyro_sample_rate_setup,
        serial_task_period=serial_task_period,
        usb_sof_interval_compares=tuple(
            FLASH_BASE + offset for offset in usb_sof_interval_compares
        ),
        system_state=system_state,
        systick_uptime=systick_uptime,
        read_byte_crc_poll=FLASH_BASE + poll_anchor,
        selected_esc=selected_esc,
        bl_send_buf=FLASH_BASE + bl_send_buf,
        set_esc_input=FLASH_BASE + set_esc_input,
        suart_getc=FLASH_BASE + suart_getc,
        suart_putc=FLASH_BASE + suart_putc,
    )


def _symbol_address(symbols: dict[str, int], name: str) -> int:
    if name in symbols:
        return symbols[name]
    private = [value for key, value in symbols.items() if key.startswith(name + ".")]
    if len(private) == 1:
        return private[0]
    raise ValueError(f"flight-controller ELF has no unique {name} symbol")


def betaflight_hotpatches(symbol_elf: Path, flash: Path) -> BetaflightHotPatches:
    """Cross-check recognized hot-patch sites with an exact matching ELF.

    Comparing every flash-backed PT_LOAD byte prevents a nearby or similarly
    named Betaflight build from installing hooks at stale addresses. Sequence
    recognition independently proves the hook-specific calling conventions.
    """
    symbol_elf = Path(symbol_elf)
    flash = Path(flash)
    try:
        flash_data = bytearray(flash.read_bytes())
        _restore_existing_instruction_patches(flash_data)
        with symbol_elf.open("rb") as stream:
            elf = ELFFile(stream)
            if elf.header.e_machine != "EM_ARM":
                raise ValueError("flight-controller ELF is not ARM")
            symbols: dict[str, int] = {}
            for section in elf.iter_sections():
                if section.header.sh_type not in ("SHT_SYMTAB", "SHT_DYNSYM"):
                    continue
                for symbol in section.iter_symbols():
                    if symbol.name and symbol.entry.st_value:
                        symbols[symbol.name] = int(symbol.entry.st_value)
            compared = 0
            for segment in elf.iter_segments():
                address = int(segment.header.p_paddr)
                size = int(segment.header.p_filesz)
                if (
                    segment.header.p_type != "PT_LOAD"
                    or not size
                    or not FLASH_BASE <= address < FLASH_BASE + FLASH_SIZE
                ):
                    continue
                offset = address - FLASH_BASE
                payload = segment.data()
                if offset + size > len(flash_data):
                    raise ValueError("flight-controller ELF exceeds emulated flash")
                if not _matches_selected_firmware(flash_data, offset, payload):
                    raise ValueError(
                        "flight-controller ELF does not match the selected firmware"
                    )
                compared += size
    except (OSError, ELFError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith("flight-controller"):
            raise
        raise ValueError(
            f"cannot read flight-controller ELF {symbol_elf}: {error}"
        ) from error
    if not compared:
        raise ValueError("flight-controller ELF has no flash-backed load segments")
    read_byte_crc = _symbol_address(symbols, "ReadByteCrc.isra.0") & ~1

    # Symbols locate the functions, while the recognizer proves the calling
    # convention and control flow assumed by the hooks.  Both routes must
    # resolve to exactly the same sites.
    sequence_patches = recognize_betaflight_hotpatches(flash)
    scheduler_address = _symbol_address(symbols, "scheduler") & ~1
    resolved_4way = {
        "read_byte_crc_poll": read_byte_crc + 0x0C,
        "selected_esc": _symbol_address(symbols, "selected_esc"),
        "bl_send_buf": _symbol_address(symbols, "BL_SendBuf") & ~1,
        "set_esc_input": _symbol_address(symbols, "setEscInput") & ~1,
        "suart_getc": _symbol_address(symbols, "suart_getc_") & ~1,
        "suart_putc": _symbol_address(symbols, "suart_putc_.isra.0") & ~1,
    }
    if (
        any(
            getattr(sequence_patches, name) != address
            for name, address in resolved_4way.items()
        )
        or sequence_patches.scheduler_wait_poll != scheduler_address + 0x31C
        or sequence_patches.gyro_sample_rate_setup
        != (_symbol_address(symbols, "gyroInit") & ~1) + 0x210
        or sequence_patches.serial_task_period
        != _symbol_address(symbols, "task_attributes") + 8 * 24 + 16
    ):
        raise ValueError(
            "flight-controller ELF symbols disagree with recognized sequences"
        )
    return BetaflightHotPatches(
        symbols=symbol_elf,
        # ELF function symbols carry the Thumb-state bit; Renode hook
        # addresses are instruction addresses and must be halfword-aligned.
        delay=_symbol_address(symbols, "delay") & ~1,
        scheduler=scheduler_address,
        scheduler_wait_poll=sequence_patches.scheduler_wait_poll,
        gyro_sample_rate_setup=sequence_patches.gyro_sample_rate_setup,
        serial_task_period=sequence_patches.serial_task_period,
        usb_sof_interval_compares=sequence_patches.usb_sof_interval_compares,
        system_state=_symbol_address(symbols, "systemState"),
        systick_uptime=_symbol_address(symbols, "sysTickUptime"),
        **resolved_4way,
    )


def ensure_flash(path: Path) -> Path:
    """Create a persistent erased F405 flash image when first selected."""
    path = Path(path)
    if path.exists():
        if path.stat().st_size != FLASH_SIZE:
            raise ValueError(f"FC flash image must be {FLASH_SIZE} bytes: {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(b"\xff" * FLASH_SIZE)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def load_image(flash: Path, image: Path, address: int = FLASH_BASE) -> None:
    """Place a raw image into persistent FC flash (used by DFU and tests)."""
    offset = address - FLASH_BASE
    payload = Path(image).read_bytes()
    if offset < 0 or offset + len(payload) > FLASH_SIZE:
        raise ValueError("flight-controller image does not fit in internal flash")
    ensure_flash(flash)
    with Path(flash).open("r+b") as stream:
        stream.seek(offset)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def select_firmware(flash: Path, image: Path) -> bool:
    """Select an Intel HEX FC image, preserving settings on repeat starts.

    Returns true when flash was replaced. The first changed immutable byte
    selects a new image; in that case erased flash is rebuilt atomically and
    every HEX chunk is applied. If all immutable bytes already match, the
    reserved configuration sector and bytes absent from the HEX remain intact.
    """
    image = Path(image)
    try:
        chunks = parse_ihex(image.read_bytes())
    except (OSError, Unsupported) as error:
        raise ValueError(f"cannot read FC firmware {image}: {error}") from error
    if not chunks:
        raise ValueError(f"FC firmware contains no data: {image}")
    normalized = []
    for address, payload in chunks:
        offset = address - FLASH_BASE
        if offset < 0 or offset + len(payload) > FLASH_SIZE:
            raise ValueError(
                f"FC firmware range 0x{address:08X}.."
                f"0x{address + len(payload) - 1:08X} is outside internal flash"
            )
        normalized.append((offset, payload))
    flash = ensure_flash(flash)
    current = flash.read_bytes()
    matching_current = bytearray(current)
    try:
        # Renode's flash model persists monitor writes into the backing file.
        # Exact instruction patches are part of the selected firmware for
        # identity purposes; normalizing only fully validated trampolines
        # prevents them from causing a factory reset on the next launch.
        _restore_existing_instruction_patches(matching_current)
    except ValueError:
        # A partial or foreign modification must retain normal image-mismatch
        # behaviour and cause the selected firmware to be reloaded.
        matching_current = bytearray(current)

    if all(
        _matches_selected_firmware(matching_current, offset, payload)
        for offset, payload in normalized
    ):
        return False
    replacement = bytearray(b"\xff" * FLASH_SIZE)
    for offset, payload in normalized:
        replacement[offset : offset + len(payload)] = payload
    temporary = flash.with_name(f".{flash.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, flash)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def write_speedybee_platform(
    outdir: Path,
    esc_ports: tuple[int, ...],
    flash: Path,
    esc_state_ports: tuple[int, ...] = (),
) -> Path:
    """Generate the small board overlay; port values are launch-specific."""
    if not 1 <= len(esc_ports) <= 8:
        raise ValueError("flight controller requires 1..8 ESC ports")
    root = resource_root()
    base = root / "flight_controllers" / "stm32f405_base.repl"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "SpeedyBeeF405Mini.repl"
    # This board model currently bridges its four primary motor outputs.
    # A zero port leaves an unused output disconnected when fewer ESCs are
    # requested; additional ESCs still run and have their own Control tabs.
    connected_ports = list(esc_ports[:4])
    connected_ports.extend([0] * (4 - len(connected_ports)))
    connected_state_ports = list(esc_state_ports[:4])
    connected_state_ports.extend([0] * (4 - len(connected_state_ports)))
    if esc_state_ports and len(esc_state_ports) != len(esc_ports):
        raise ValueError("ESC state ports must match ESC signal ports")
    ports = "\n".join(
        f"    esc{index + 1}Port: {port}" for index, port in enumerate(connected_ports)
    )
    state_ports = "\n".join(
        f"    esc{index + 1}StatePort: {port}"
        for index, port in enumerate(connected_state_ports)
    )
    path.write_text(
        f"""using "{renode_path(base)}"

rcc:
    hseFrequency: 8000000

persistentFlash: Miscellaneous.AP_PersistentMemory @ none
    fileName: {json.dumps(renode_path(flash))}
    address: 0x08000000
    size: 0x100000

baro0: Sensors.AP_DPS310 @ i2c1 0x76

spi1Mux: Miscellaneous.AP_SPIMultiplexer @ spi1

imu0: Sensors.AP_ICM42688 @ spi1Mux 0
    rotation: 26
    samplePeriodUs: 125
    IRQ -> gpioPortC@4

adc1: Analog.AP_STM32_ADC @ sysbus 0x40012000
    IRQ -> nvic@18

dma1Fix: Miscellaneous.AP_STM32DMA_Fixup @ sysbus 0x60000120
    dma: dma1

dma2Fix: Miscellaneous.AP_STM32DMA_Fixup @ sysbus 0x60000124
    dma: dma2

usart1:
    DMARequest -> dma2@2
usart2:
    DMARequest -> dma1@5
usart3:
    DMARequest -> dma1@1
usart6:
    DMARequest -> dma2@1

spi1:
    DMARecieve -> dma2@0
spi2:
    DMARecieve -> dma1@3
spi3:
    DMARecieve -> dma1@0

i2c1:
    RxDmaRequest -> dma1@0

usart1RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x60000128
    dma: dma2
    stream: 2
    uart: usart1
    peripheralAddress: 0x40011004
usart2RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x6000012C
    dma: dma1
    stream: 5
    uart: usart2
    peripheralAddress: 0x40004404
usart3RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x60000130
    dma: dma1
    stream: 1
    uart: usart3
    peripheralAddress: 0x40004804
usart6RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x60000134
    dma: dma2
    stream: 1
    uart: usart6
    peripheralAddress: 0x40011404

timer3UpdateDMA: Miscellaneous.AP_STM32_Timer_UpdateDMA @ sysbus 0x6000013C
    timer: timer3
    UpdateDMA -> dma1@2
timer4UpdateDMA: Miscellaneous.AP_STM32_Timer_UpdateDMA @ sysbus 0x60000140
    timer: timer4
    UpdateDMA -> dma1@6

motorBridge: Miscellaneous.ESCSim_STM32_DShot @ sysbus 0x60000200
    dma: dma1
    gpio: gpioPortB
    timer2: timer2
    timer3: timer3
    timer4: timer4
{ports}
{state_ports}
    [0-3] -> gpioPortB@[1, 0, 10, 11]

gpioPortB:
    [1, 0, 10, 11] -> motorBridge@[0-3]

gpioPortA:
    4 -> spi1Mux@0
""",
        encoding="utf-8",
    )
    return path


def write_speedybee_script(
    outdir: Path,
    platform: Path,
    flash: Path,
    usbip_port: int,
    hotpatches: BetaflightHotPatches | None = None,
    source_speedup: bool = False,
) -> Path:
    """Write a self-contained Renode script using the vendored models."""
    root = resource_root()
    includes = [
        "apm_common/AP_SigrokInterface.cs",
        "apm_common/AP_Physics.cs",
        "apm_common/AP_I2CRegisterDevice.cs",
        "apm_common/AP_SPIMultiplexer.cs",
        "apm_common/AP_NVIC_RettobaseFix.cs",
        "apm_common/AP_DWT.cs",
        "apm_common/AP_PersistentMemory.cs",
        "apm_stm32/AP_STM32_IWDG.cs",
        "apm_stm32/AP_STM32F4_FlashController.cs",
        "apm_stm32/AP_STM32F4_RCC.cs",
        "apm_stm32/AP_STM32F1_UART.cs",
        "apm_stm32/AP_STM32F4_I2C.cs",
        "apm_stm32/AP_STM32_ADC.cs",
        "apm_stm32/AP_STM32_OTG.cs",
        "apm_stm32/AP_STM32DMA_Fixup.cs",
        "apm_stm32/AP_UartRxDmaPump.cs",
        "apm_stm32/AP_STM32_Timer_UpdateDMA.cs",
        "apm_stm32/ESCSim_STM32_DShot.cs",
        "apm_sensors/AP_DPS310.cs",
        "apm_sensors/AP_ICM42688.cs",
    ]
    lines = [
        f"include @{renode_path(root / 'peripherals' / item)}" for item in includes
    ]
    lines += [
        'mach create "SpeedyBeeF405Mini"',
        f"machine LoadPlatformDescription @{renode_path(platform)}",
        "flash ResetByte 0xFF",
        f"sysbus LoadBinary @{renode_path(flash)} 0x{FLASH_BASE:08X}",
        # Stable non-erased UID: ArduPilot derives USB serial and board ID
        # material from this factory region.
        "sysbus WriteDoubleWord 0x1FFF7A10 0xF96D5489",
        "sysbus WriteDoubleWord 0x1FFF7A14 0xFBD5F38E",
        "sysbus WriteDoubleWord 0x1FFF7A18 0x76181BD3",
        # Factory ADC calibration words used by Betaflight's internal VREF
        # and temperature conversion. Keep them nonzero and consistent with
        # the fixed samples below (approximately 3.3V and 25 degrees C).
        "sysbus WriteWord 0x1FFF7A2A 1500",
        "sysbus WriteWord 0x1FFF7A2C 1000",
        "sysbus WriteWord 0x1FFF7A2E 1300",
        # Fixed bench inputs: 12.0V through the board's 11:1 divider, no
        # current/RSSI, and a mid-scale internal channel used during probes.
        "adc1 FeedSample 1354 10 -1",
        "adc1 FeedSample 0 11 -1",
        "adc1 FeedSample 0 15 -1",
        "adc1 FeedSample 1500 0 -1",
        "adc1 FeedSample 980 16 -1",
        "adc1 FeedSample 1500 17 -1",
        f'emulation CreateUSBIPServer {usbip_port} "usb"',
        "sysbus.usbOtg RegisterUSBIP",
        f"cpu VectorTableOffset 0x{FLASH_BASE:08X}",
        # Renode restores VTOR to zero on SYSRESETREQ. The STM32 flash is
        # mapped at 0x08000000, so restore the hardware reset value here.
        "macro reset",
        '"""',
        f"    cpu VectorTableOffset 0x{FLASH_BASE:08X}",
        '"""',
        "cpu PerformanceInMips 125",
        # Match the proven ArduPilot F405 setup.  DShot response timing is
        # scheduled inside the machine and does not require a sub-millisecond
        # global synchronization quantum; 0.1ms only makes Renode rendezvous
        # ten times as often.
        'emulation SetGlobalQuantum "0.001"',
        "logLevel 3 sysbus.adc1",
        "logLevel 3 sysbus.dma1",
        "logLevel 3 sysbus.dma2",
        "logLevel 3 sysbus.i2c1",
        "logLevel 3 sysbus.spi1",
        "logLevel 3 sysbus.spi2",
        "logLevel 3 sysbus.spi3",
        # ArduPilot drives TIMx_DMAR at the DShot frame rate.  The stock
        # timer does not implement that register; motorBridge consumes the
        # same DMA buffer, so these per-word warnings are expected and would
        # otherwise swamp the launcher and starve real-time emulation.
        "logLevel 3 sysbus.timer1",
        "logLevel 3 sysbus.timer2",
        "logLevel 3 sysbus.timer3",
        "logLevel 3 sysbus.timer4",
    ]
    if source_speedup:
        lines += [
            "# Betaflight source-level ESCSim speed profile",
            "sysbus.usbOtg SetSOFInterval 80",
        ]
    if hotpatches is not None:
        lines.append(
            "# Betaflight hot patches: "
            + ("ELF cross-checked" if hotpatches.symbols else "sequence-recognized")
        )
        skip = renode_execfile(root / "scripts" / "skip_betaflight_delays.py")
        finish = renode_execfile(root / "scripts" / "finish_betaflight_hotpatches.py")
        putc = renode_execfile(root / "scripts" / "betaflight_4way_putc.py")
        getc = renode_execfile(root / "scripts" / "betaflight_4way_getc.py")
        flush = renode_execfile(root / "scripts" / "betaflight_4way_flush.py")
        sendbuf = renode_execfile(root / "scripts" / "betaflight_4way_sendbuf.py")
        if hotpatches.symbols is not None:
            lines.append(f"sysbus LoadSymbolsFrom @{renode_path(hotpatches.symbols)}")
        if all(
            address is not None
            for address in (
                hotpatches.delay,
                hotpatches.scheduler,
                hotpatches.system_state,
                hotpatches.systick_uptime,
            )
        ):
            lines += [
                (
                    f'cpu AddHook 0x{hotpatches.delay:08X} "'
                    f"system_state_address=0x{hotpatches.system_state:08X}; "
                    f"systick_uptime_address=0x{hotpatches.systick_uptime:08X}; "
                    f'{skip}"'
                ),
                (
                    f'cpu AddHook 0x{hotpatches.scheduler:08X} "'
                    f"system_state_address=0x{hotpatches.system_state:08X}; "
                    f"delay_address=0x{hotpatches.delay:08X}; "
                    f"serial_task_period_address=0x{hotpatches.serial_task_period:08X}; "
                    f'scheduler_address=0x{hotpatches.scheduler:08X}; {finish}"'
                ),
            ]
        lines += [
            # Betaflight's legacy F4 CDC transmitter normally drains its ring
            # every 16th SOF.  A flight controller running slower than wall
            # time then takes hundreds of milliseconds to return each MSP
            # response, while Configurator continues to queue 20ms polls.
            # Make the recognised compare fire on every ordinary 1ms SOF so
            # serial latency follows host time closely enough to avoid that
            # self-sustaining backlog.
            "sysbus.usbOtg SetSOFInterval 1",
            # The exact bundled target samples its ICM42688 at 8kHz even
            # when pid_process_denom reduces the downstream PID rate. Run
            # the real gyro path at 1kHz, a supported sensor ODR that is
            # ample for an interactive firmware simulator.
            "# Run the ICM42688 and gyro scheduler at 1kHz",
            f"sysbus WriteWord 0x{hotpatches.gyro_sample_rate_setup + 4:08X} 0x2200",
            f"sysbus WriteWord 0x{hotpatches.gyro_sample_rate_setup + 0x12:08X} 0xF44F",
            f"sysbus WriteWord 0x{hotpatches.gyro_sample_rate_setup + 0x14:08X} 0x717A",
            "# Drain legacy F4 CDC output on every USB frame",
            *(
                f"sysbus WriteWord 0x{address:08X} 0x2B00"
                for address in hotpatches.usb_sof_interval_compares
            ),
            "# WFI-patch scheduler's sequence-recognized cycle-counter poll",
            *(
                f"sysbus WriteWord 0x{address:08X} 0x{value:04X}"
                for address, value in _scheduler_wfi_patch(
                    hotpatches.scheduler_wait_poll
                )
            ),
            "# WFI-patch ReadByteCrc's sequence-recognized empty-input poll",
            *(
                f"sysbus WriteWord 0x{address:08X} 0x{value:04X}"
                for address, value in _read_byte_crc_wfi_patch(
                    hotpatches.read_byte_crc_poll
                )
            ),
            (
                f'cpu AddHook 0x{hotpatches.bl_send_buf:08X} "'
                f"selected_esc_address=0x{hotpatches.selected_esc:08X}; "
                f'{sendbuf}"'
            ),
            (
                f'cpu AddHook 0x{hotpatches.suart_putc:08X} "'
                f"selected_esc_address=0x{hotpatches.selected_esc:08X}; "
                f'{putc}"'
            ),
            (
                f'cpu AddHook 0x{hotpatches.suart_getc:08X} "'
                f"selected_esc_address=0x{hotpatches.selected_esc:08X}; "
                f'{getc}"'
            ),
            (f'cpu AddHook 0x{hotpatches.set_esc_input:08X} "{flush}"'),
        ]
    lines.append("start")
    path = Path(outdir) / "SpeedyBeeF405Mini.resc"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

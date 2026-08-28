"""Minimal STM32-compatible USB DFU device backed by an FC flash image."""

from __future__ import annotations

import os
from pathlib import Path
import struct
import threading

from escsim.control import usbip
from escsim.renode.flight_controller import FLASH_BASE, FLASH_SIZE, ensure_flash


STM32_VENDOR_ID = 0x0483
STM32_DFU_PRODUCT_ID = 0xDF11
TRANSFER_SIZE = 2048

DFU_DETACH = 0
DFU_DNLOAD = 1
DFU_UPLOAD = 2
DFU_GETSTATUS = 3
DFU_CLRSTATUS = 4
DFU_GETSTATE = 5
DFU_ABORT = 6

STATE_APP_IDLE = 0
STATE_DFU_IDLE = 2
STATE_DFU_DNLOAD_SYNC = 3
STATE_DFU_DNBUSY = 4
STATE_DFU_DNLOAD_IDLE = 5
STATE_DFU_MANIFEST_SYNC = 6
STATE_DFU_MANIFEST = 7
STATE_DFU_UPLOAD_IDLE = 9
STATE_DFU_ERROR = 10

STATUS_OK = 0
STATUS_ADDRESS = 8
STATUS_ERASE = 4

MEMORY_LAYOUT = "@Internal Flash /0x08000000/04*016Kg,01*064Kg,07*128Kg"

# One DFU-mode interface and the DFU 1.1a functional descriptor.  The device
# can download/upload and disconnects itself for manifestation, matching the
# STM32F405 factory ROM closely enough for dfu-util and WebUSB DFU clients.
DFU_CONFIG_DESCRIPTOR = b"".join(
    (
        struct.pack("<BBHBBBBB", 9, 2, 27, 1, 1, 0, 0x80, 50),
        struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 0, 0xFE, 0x01, 0x02, 4),
        struct.pack("<BBBHHH", 9, 0x21, 0x0B, 255, TRANSFER_SIZE, 0x011A),
    )
)


class DfuDevice(usbip.UsbipServer):
    """A DfuSe subset which programs one MiB of internal STM32F405 flash."""

    def __init__(self, flash, on_manifest=None, **kwargs):
        self.flash = ensure_flash(Path(flash))
        self.on_manifest = on_manifest
        self.address = FLASH_BASE
        self.state = STATE_DFU_IDLE
        self.status = STATUS_OK
        self._busy_once = False
        self._manifest_scheduled = False
        self._flash_lock = threading.Lock()
        super().__init__(
            vid=STM32_VENDOR_ID,
            pid=STM32_DFU_PRODUCT_ID,
            manufacturer="STMicroelectronics",
            product="STM32 BOOTLOADER",
            serial="RENODE-STM32-DFU",
            config_descriptor=DFU_CONFIG_DESCRIPTOR,
            interfaces=((0xFE, 0x01, 0x02),),
            device_class=0,
            device_subclass=0,
            device_protocol=0,
            **kwargs,
        )
        # The interface string follows the three ordinary USB strings.
        self.strings.append(MEMORY_LAYOUT)

    @property
    def path(self):
        return self.endpoint

    def _submit(self, seqnum, direction, ep, length, setup, data):
        if ep != 0:
            with self.send_lock:
                self._ret_submit(seqnum, usbip.ST_STALL)
            return
        super()._submit(seqnum, direction, ep, length, setup, data)

    def _control(self, setup, data, length):
        rtype, request, value, _index, wlength = struct.unpack("<BBHHH", setup)
        is_dfu = (rtype & 0x60) == 0x20
        if not is_dfu:
            return super()._control(setup, data, length)

        if request == DFU_DNLOAD:
            return self._download(value, data)
        if request == DFU_UPLOAD:
            return self._upload(value, wlength)
        if request == DFU_GETSTATUS:
            return usbip.ST_OK, self._get_status()
        if request == DFU_GETSTATE:
            return usbip.ST_OK, bytes((self.state,))
        if request == DFU_CLRSTATUS:
            self.status = STATUS_OK
            self.state = STATE_DFU_IDLE
            return usbip.ST_OK, b""
        if request == DFU_ABORT:
            self.state = STATE_DFU_IDLE
            return usbip.ST_OK, b""
        if request == DFU_DETACH:
            self._schedule_manifest()
            return usbip.ST_OK, b""
        return usbip.ST_STALL, b""

    def _download(self, block, data):
        if not data:
            self.state = STATE_DFU_MANIFEST_SYNC
            self._busy_once = False
            return usbip.ST_OK, b""
        try:
            if block == 0:
                self._dfuse_command(data)
            elif block >= 2:
                self._write(self.address + (block - 2) * TRANSFER_SIZE, data)
            else:
                raise ValueError("unsupported DfuSe block")
        except ValueError as error:
            self.log(f"DFU download rejected: {error}")
            self.status = STATUS_ADDRESS
            self.state = STATE_DFU_ERROR
            return usbip.ST_OK, b""
        self.state = STATE_DFU_DNLOAD_SYNC
        self._busy_once = True
        return usbip.ST_OK, b""

    def _upload(self, block, length):
        if block < 2:
            return usbip.ST_STALL, b""
        address = self.address + (block - 2) * TRANSFER_SIZE
        try:
            data = self._read(address, min(length, TRANSFER_SIZE))
        except ValueError:
            self.status = STATUS_ADDRESS
            self.state = STATE_DFU_ERROR
            return usbip.ST_OK, b""
        self.state = STATE_DFU_UPLOAD_IDLE
        return usbip.ST_OK, data

    def _dfuse_command(self, data):
        command = data[0]
        if command == 0x21 and len(data) == 5:  # SET_ADDRESS_POINTER
            address = struct.unpack("<I", data[1:])[0]
            self._validate(address, 0)
            self.address = address
            return
        if command == 0x41 and len(data) == 5:  # ERASE_PAGE
            self._erase_sector(struct.unpack("<I", data[1:])[0])
            return
        raise ValueError(f"unsupported DfuSe command 0x{command:02x}")

    def _get_status(self):
        state = self.state
        timeout_ms = 0
        if state == STATE_DFU_DNLOAD_SYNC:
            state = STATE_DFU_DNBUSY if self._busy_once else STATE_DFU_DNLOAD_IDLE
            timeout_ms = 1 if self._busy_once else 0
            self._busy_once = False
            self.state = state
        elif state == STATE_DFU_DNBUSY:
            state = STATE_DFU_DNLOAD_IDLE
            self.state = state
        elif state == STATE_DFU_MANIFEST_SYNC:
            state = STATE_DFU_MANIFEST
            self.state = state
            self._schedule_manifest()
        return bytes(
            (
                self.status,
                timeout_ms & 0xFF,
                (timeout_ms >> 8) & 0xFF,
                (timeout_ms >> 16) & 0xFF,
                state,
                0,
            )
        )

    def _validate(self, address, size):
        if address < FLASH_BASE or address + size > FLASH_BASE + FLASH_SIZE:
            raise ValueError(f"address 0x{address:08x} is outside internal flash")

    def _write(self, address, data):
        self._validate(address, len(data))
        with self._flash_lock, self.flash.open("r+b") as stream:
            stream.seek(address - FLASH_BASE)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _read(self, address, length):
        self._validate(address, length)
        with self._flash_lock, self.flash.open("rb") as stream:
            stream.seek(address - FLASH_BASE)
            return stream.read(length)

    def _erase_sector(self, address):
        sectors = (
            (0x08000000, 0x4000),
            (0x08004000, 0x4000),
            (0x08008000, 0x4000),
            (0x0800C000, 0x4000),
            (0x08010000, 0x10000),
            (0x08020000, 0x20000),
            (0x08040000, 0x20000),
            (0x08060000, 0x20000),
            (0x08080000, 0x20000),
            (0x080A0000, 0x20000),
            (0x080C0000, 0x20000),
            (0x080E0000, 0x20000),
        )
        try:
            start, size = next(item for item in sectors if item[0] == address)
        except StopIteration as error:
            self.status = STATUS_ERASE
            raise ValueError(f"0x{address:08x} is not a sector boundary") from error
        self._write(start, b"\xff" * size)

    def _schedule_manifest(self):
        if self._manifest_scheduled or self.on_manifest is None:
            return
        self._manifest_scheduled = True
        # Let the GETSTATUS response reach vhci_hcd before the launcher swaps
        # the factory-ROM device for firmware-driven USB.
        timer = threading.Timer(0.2, self.on_manifest)
        timer.daemon = True
        timer.start()

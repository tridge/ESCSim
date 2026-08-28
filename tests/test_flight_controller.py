from __future__ import annotations

import struct
import threading

import pytest

from escsim.control import dfu, usbip
from escsim.renode.flight_controller import (
    FLASH_BASE,
    FLASH_SIZE,
    ensure_flash,
    flight_controller_firmware,
    flight_controller_firmwares,
    load_image,
    select_firmware,
    write_speedybee_platform,
    write_speedybee_script,
)


def test_speedybee_platform_wires_four_escs_and_fixed_sensors(tmp_path):
    flash = ensure_flash(tmp_path / "flash.bin")
    platform = write_speedybee_platform(
        tmp_path, (5101, 5102, 5103, 5104), flash
    )
    script = write_speedybee_script(tmp_path, platform, flash, 5200)

    platform_text = platform.read_text()
    script_text = script.read_text()
    assert flash.stat().st_size == FLASH_SIZE
    assert flash.read_bytes()[:32] == b"\xff" * 32
    assert [f"esc{i}Port: {5100 + i}" in platform_text for i in range(1, 5)] == [
        True,
        True,
        True,
        True,
    ]
    assert "motorBridge: Miscellaneous.ESCSim_STM32_DShot" in platform_text
    assert "timer2: timer2" in platform_text
    assert "timer3: timer3" in platform_text
    assert "timer4: timer4" in platform_text
    assert "AP_ICM42688" in platform_text
    assert "AP_DPS310" in platform_text
    assert "AP_PersistentMemory" in platform_text
    assert "adc1 FeedSample 1354 10 -1" in script_text
    assert "adc1 FeedSample 980 16 -1" in script_text
    assert "adc1 FeedSample 1500 17 -1" in script_text
    assert "sysbus WriteWord 0x1FFF7A2A 1500" in script_text
    assert 'emulation CreateUSBIPServer 5200 "usb"' in script_text
    assert 'emulation SetGlobalQuantum "0.001"' in script_text
    assert "macro reset" in script_text
    assert "cpu VectorTableOffset 0x08000000" in script_text


def test_speedybee_dshot_bridge_supports_betaflight_channel_dma():
    bridge = (
        flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
        / "peripherals"
        / "apm_stm32"
        / "ESCSim_STM32_DShot.cs"
    ).read_text(encoding="utf-8")

    assert "TryDecodeDirect" in bridge
    assert "case Timer3Base + Ccr4:" in bridge
    assert "case Timer3Base + Ccr3:" in bridge
    assert "case Timer2Base + Ccr3:" in bridge
    assert "case Timer2Base + Ccr4:" in bridge
    assert "ObservedStreams = { 1, 2, 3, 6, 7 }" in bridge

    uart_pump = (
        flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
        / "peripherals"
        / "apm_stm32"
        / "AP_UartRxDmaPump.cs"
    ).read_text(encoding="utf-8")
    assert "0x14 + 0x18 * stream" in uart_pump
    assert "peripheralAddress" in uart_pump
    assert "DirectionMask" in uart_pump


def test_stm32f4_rcc_reports_software_reset_cause():
    rcc = (
        flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
        / "peripherals"
        / "apm_stm32"
        / "AP_STM32F4_RCC.cs"
    ).read_text(encoding="utf-8")

    assert "AddWatchpointHook" in rcc
    assert "SysResetReq" in rcc
    assert "SFTRSTF" in rcc


def test_stm32f4_otg_uses_connected_hardware_reset_state():
    otg = (
        flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
        / "peripherals"
        / "apm_stm32"
        / "AP_STM32_OTG.cs"
    ).read_text(encoding="utf-8")

    assert "registers[DeviceControl] = 0;" in otg
    assert "(GetRegister(DeviceControl) & SoftDisconnect) == 0" in otg
    assert "(value & GlobalInterruptEnable) == 0" in otg


def test_stm32f4_otg_assigns_firmware_address_before_usbip_setup():
    otg = (
        flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
        / "peripherals"
        / "apm_stm32"
        / "AP_STM32_OTG.cs"
    ).read_text(encoding="utf-8")

    # USB/IP imports an already-addressed remote device, so vhci_hcd does not
    # forward the physical bus SET_ADDRESS transaction.  Betaflight's USB
    # state machine must still see it before accepting SET_CONFIGURATION.
    assert "if(!firmwareAddressAssigned)" in otg
    assert "Request = (byte)StandardRequest.SetAddress" in otg
    assert "Value = SyntheticUsbAddress" in otg
    assert "_ => HandleSetupPacket(packet, additionalData," in otg


def test_bundled_betaflight_image_is_valid_and_selectable(tmp_path):
    assert flight_controller_firmwares() == ("SPEEDYBEEF405V5",)
    image = flight_controller_firmware("SPEEDYBEEF405V5")
    flash = tmp_path / "flash.bin"

    assert select_firmware(flash, image)
    content = flash.read_bytes()
    assert len(content) == FLASH_SIZE
    assert content[:8] == bytes.fromhex("f0ff001085240508")

    # Re-selecting an unchanged firmware must preserve configuration bytes
    # outside the HEX image instead of factory-resetting every app start.
    with flash.open("r+b") as stream:
        stream.seek(0x4000)
        stream.write(b"configured sector")
        stream.seek(0xF0000)
        stream.write(b"configured")
    assert not select_firmware(flash, image)
    assert flash.read_bytes()[0x4000 : 0x4011] == b"configured sector"
    assert flash.read_bytes()[0xF0000 : 0xF000A] == b"configured"


def test_unknown_flight_controller_firmware_is_rejected():
    with pytest.raises(ValueError, match="unsupported flight-controller firmware"):
        flight_controller_firmware("missing")


def test_speedybee_platform_allows_one_esc(tmp_path):
    flash = ensure_flash(tmp_path / "flash.bin")

    platform = write_speedybee_platform(tmp_path / "one", (57833,), flash)
    text = platform.read_text(encoding="utf-8")

    assert "esc1Port: 57833" in text
    assert "esc2Port: 0" in text
    assert "esc3Port: 0" in text
    assert "esc4Port: 0" in text


def test_speedybee_platform_accepts_eight_escs_and_connects_first_four(tmp_path):
    flash = ensure_flash(tmp_path / "flash.bin")

    platform = write_speedybee_platform(
        tmp_path / "eight", tuple(range(57833, 57841)), flash
    )
    text = platform.read_text(encoding="utf-8")

    for index, port in enumerate(range(57833, 57837), 1):
        assert f"esc{index}Port: {port}" in text
    assert "esc5Port" not in text


def test_load_image_rejects_flash_overflow(tmp_path):
    flash = tmp_path / "flash.bin"
    image = tmp_path / "bootloader.bin"
    image.write_bytes(b"boot")
    load_image(flash, image, FLASH_BASE + 8)
    assert flash.read_bytes()[8:12] == b"boot"

    too_large = tmp_path / "too-large.bin"
    too_large.write_bytes(b"x" * 16)
    try:
        load_image(flash, too_large, FLASH_BASE + FLASH_SIZE - 8)
    except ValueError as error:
        assert "does not fit" in str(error)
    else:
        raise AssertionError("overflowing image was accepted")


def make_dfu(tmp_path):
    device = object.__new__(dfu.DfuDevice)
    device.flash = ensure_flash(tmp_path / "flash.bin")
    device.on_manifest = None
    device.address = FLASH_BASE
    device.state = dfu.STATE_DFU_IDLE
    device.status = dfu.STATUS_OK
    device._busy_once = False
    device._manifest_scheduled = False
    device._flash_lock = threading.Lock()
    device.log = lambda *_args: None
    return device


def test_dfuse_set_address_erase_download_and_upload(tmp_path):
    device = make_dfu(tmp_path)
    address = FLASH_BASE + 0x4000

    status, _ = device._download(0, b"\x21" + struct.pack("<I", address))
    assert status == usbip.ST_OK
    assert device.address == address
    status, _ = device._download(2, b"ArduPilot")
    assert status == usbip.ST_OK
    assert device._read(address, 9) == b"ArduPilot"
    assert device._get_status()[4] == dfu.STATE_DFU_DNBUSY
    assert device._get_status()[4] == dfu.STATE_DFU_DNLOAD_IDLE

    status, payload = device._upload(2, 9)
    assert status == usbip.ST_OK
    assert payload == b"ArduPilot"

    status, _ = device._download(0, b"\x41" + struct.pack("<I", address))
    assert status == usbip.ST_OK
    assert device._read(address, 9) == b"\xff" * 9


def test_dfuse_abort_preserves_address_for_betaflight_verification(tmp_path):
    device = make_dfu(tmp_path)
    address = FLASH_BASE + 0x4000
    payload = b"Betaflight verification"

    device._download(0, b"\x21" + struct.pack("<I", address))
    assert device._get_status()[4] == dfu.STATE_DFU_DNBUSY
    assert device._get_status()[4] == dfu.STATE_DFU_DNLOAD_IDLE
    device._download(2, payload)
    assert device._get_status()[4] == dfu.STATE_DFU_DNBUSY
    assert device._get_status()[4] == dfu.STATE_DFU_DNLOAD_IDLE

    # Betaflight loads the block address as a DNLOAD command, then ABORTs
    # from dfuDNLOAD_IDLE before switching direction to UPLOAD.  The STM32
    # DfuSe address pointer survives that state transition.
    setup = struct.pack("<BBHHH", 0x21, dfu.DFU_ABORT, 0, 0, 0)
    status, _ = device._control(setup, b"", 0)
    assert status == usbip.ST_OK
    assert device.state == dfu.STATE_DFU_IDLE
    assert device.address == address
    assert device._upload(2, len(payload)) == (usbip.ST_OK, payload)


def test_dfuse_rejects_out_of_range_address(tmp_path):
    device = make_dfu(tmp_path)
    status, _ = device._download(
        0, b"\x21" + struct.pack("<I", FLASH_BASE + FLASH_SIZE + 4)
    )
    assert status == usbip.ST_OK
    assert device.state == dfu.STATE_DFU_ERROR
    assert device.status == dfu.STATUS_ADDRESS

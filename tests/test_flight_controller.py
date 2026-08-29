from __future__ import annotations

import struct
import threading

import pytest

from escsim.control import dfu, usbip
from escsim.renode import flight_controller
from escsim.renode.flight_controller import (
    BetaflightHotPatches,
    FLASH_BASE,
    FLASH_SIZE,
    FlightControllerSpec,
    betaflight_hotpatches,
    ensure_flash,
    flight_controller_firmware,
    flight_controller_firmwares,
    load_image,
    recognize_betaflight_hotpatches,
    select_firmware,
    write_speedybee_platform,
    write_speedybee_script,
)


def test_speedybee_platform_wires_four_escs_and_fixed_sensors(tmp_path):
    flash = ensure_flash(tmp_path / "flash.bin")
    platform = write_speedybee_platform(
        tmp_path,
        (5101, 5102, 5103, 5104),
        flash,
        (5201, 5202, 5203, 5204),
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
    assert "gpio: gpioPortB" in platform_text
    assert "[0-3] -> gpioPortB@[1, 0, 10, 11]" in platform_text
    assert "[1, 0, 10, 11] -> motorBridge@[0-3]" in platform_text
    assert "esc1StatePort: 5201" in platform_text
    assert "esc4StatePort: 5204" in platform_text
    assert "timer2: timer2" in platform_text
    assert "timer3: timer3" in platform_text
    assert "timer4: timer4" in platform_text
    assert "AP_ICM42688" in platform_text
    assert "samplePeriodUs: 125" in platform_text
    assert "IRQ -> gpioPortC@4" in platform_text
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

    imu = (
        flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
        / "peripherals"
        / "apm_sensors"
        / "AP_ICM42688.cs"
    ).read_text(encoding="utf-8")
    assert "register == GyroConfig0" in imu
    assert "case 3: sampleTimer.Limit = 125" in imu
    assert "IRQ.Blink();" in imu


def test_speedybee_script_installs_verified_elf_hotpatches(tmp_path):
    flash = ensure_flash(tmp_path / "flash.bin")
    platform = write_speedybee_platform(tmp_path, (5101,), flash)
    symbols = tmp_path / "betaflight.elf"
    symbols.write_bytes(b"elf")
    script = write_speedybee_script(
        tmp_path,
        platform,
        flash,
        5200,
        hotpatches=BetaflightHotPatches(
            symbols=symbols,
            delay=0x08001000,
            scheduler=0x08002000,
            scheduler_wait_poll=0x08002100,
            system_state=0x20000100,
            systick_uptime=0x20000104,
            read_byte_crc_poll=0x0800300C,
            selected_esc=0x20000108,
            bl_send_buf=0x08003500,
            set_esc_input=0x08004000,
            suart_getc=0x08005000,
            suart_putc=0x08006000,
        ),
    ).read_text()

    assert f"sysbus LoadSymbolsFrom @{symbols}" in script
    assert "cpu AddHook 0x08001000" in script
    assert "system_state_address=0x20000100" in script
    assert "systick_uptime_address=0x20000104" in script
    assert "skip_betaflight_delays.py" in script
    assert "cpu AddHook 0x08002000" in script
    assert "finish_betaflight_hotpatches.py" in script
    assert "WFI-patch scheduler" in script
    assert "sysbus WriteWord 0x08002100" in script
    assert "sysbus WriteWord 0x080FFFDC 0xBF30" in script
    assert "cpu AddHook 0x0800300C" not in script
    assert "WFI-patch ReadByteCrc" in script
    assert "sysbus WriteWord 0x0800300C" in script
    assert "sysbus WriteWord 0x080FFFF0" in script
    assert "cpu AddHook 0x08003500" in script
    assert "betaflight_4way_sendbuf.py" in script
    assert "cpu AddHook 0x08005000" in script
    assert "betaflight_4way_getc.py" in script
    assert "cpu AddHook 0x08006000" in script
    assert "betaflight_4way_putc.py" in script
    assert "cpu AddHook 0x08004000" in script
    assert "betaflight_4way_flush.py" in script


def test_betaflight_hotpatches_require_exact_flash_identity(tmp_path, monkeypatch):
    class Entry:
        def __init__(self, value):
            self.st_value = value

    class Symbol:
        def __init__(self, name, value):
            self.name = name
            self.entry = Entry(value)

    class Header:
        def __init__(self, **values):
            self.__dict__.update(values)

    class Section:
        header = Header(sh_type="SHT_SYMTAB")

        def iter_symbols(self):
            return iter(
                (
                    Symbol("delay", FLASH_BASE + 1),
                    Symbol("scheduler", FLASH_BASE + 5),
                    Symbol("systemState", 0x20000100),
                    Symbol("sysTickUptime.lto_priv.0", 0x20000104),
                    Symbol("ReadByteCrc.isra.0", FLASH_BASE + 0x11),
                    Symbol("selected_esc", 0x20000108),
                    Symbol("BL_SendBuf", FLASH_BASE + 0x2D),
                    Symbol("setEscInput", FLASH_BASE + 0x31),
                    Symbol("suart_getc_", FLASH_BASE + 0x35),
                    Symbol("suart_putc_.isra.0", FLASH_BASE + 0x39),
                )
            )

    class Segment:
        header = Header(p_type="PT_LOAD", p_paddr=FLASH_BASE, p_filesz=0x8040)

        def data(self):
            payload = bytearray(0x8040)
            payload[0x1C:0x20] = b"\x00\x28\xfa\xd0"
            return bytes(payload)

    class Image:
        header = Header(e_machine="EM_ARM")

        def iter_sections(self):
            return iter((Section(),))

        def iter_segments(self):
            return iter((Segment(),))

    monkeypatch.setattr(flight_controller, "ELFFile", lambda _stream: Image())
    monkeypatch.setattr(
        flight_controller,
        "recognize_betaflight_hotpatches",
        lambda _flash: BetaflightHotPatches(
            symbols=None,
            delay=None,
            scheduler=None,
            scheduler_wait_poll=FLASH_BASE + 0x320,
            system_state=None,
            systick_uptime=0x20000104,
            read_byte_crc_poll=FLASH_BASE + 0x1C,
            selected_esc=0x20000108,
            bl_send_buf=FLASH_BASE + 0x2C,
            set_esc_input=FLASH_BASE + 0x30,
            suart_getc=FLASH_BASE + 0x34,
            suart_putc=FLASH_BASE + 0x38,
        ),
    )
    elf = tmp_path / "betaflight.elf"
    elf.write_bytes(b"elf")
    flash = tmp_path / "flash.bin"
    payload = Segment().data()
    flash.write_bytes(payload + b"\xff" * (FLASH_SIZE - len(payload)))

    patches = betaflight_hotpatches(elf, flash)
    assert patches.delay == FLASH_BASE
    assert patches.systick_uptime == 0x20000104
    assert patches.read_byte_crc_poll == FLASH_BASE + 0x1C
    assert patches.suart_putc == FLASH_BASE + 0x38

    configured = bytearray(flash.read_bytes())
    configured[0x4000] = 0x42
    flash.write_bytes(configured)
    assert betaflight_hotpatches(elf, flash).read_byte_crc_poll == FLASH_BASE + 0x1C

    flash.write_bytes(b"nope" + payload[4:] + b"\xff" * (FLASH_SIZE - len(payload)))
    with pytest.raises(ValueError, match="does not match"):
        betaflight_hotpatches(elf, flash)


def test_speedybee_dshot_bridge_supports_betaflight_channel_dma():
    resource_root = flight_controller_firmware("SPEEDYBEEF405V5").parents[1]
    bridge = (
        resource_root
        / "peripherals"
        / "apm_stm32"
        / "ESCSim_STM32_DShot.cs"
    ).read_text(encoding="utf-8")
    gui_link = (
        resource_root / "peripherals" / "common" / "AM32_GuiLink.cs"
    ).read_text(encoding="utf-8")

    assert "TryDecodeDirect" in bridge
    assert "LastFrameBase = 0x60" in bridge
    assert "case Timer3Base + Ccr4:" in bridge
    assert "case Timer3Base + Ccr3:" in bridge
    assert "case Timer2Base + Ccr3:" in bridge
    assert "case Timer2Base + Ccr4:" in bridge
    assert "ObservedStreams = { 1, 2, 3, 6, 7 }" in bridge
    assert "SampleSerialBit" in bridge
    assert "HoldEscHighAndReset" in bridge
    assert "ReplaySerialReply" in bridge
    assert "QueueDirectTransmit" in bridge
    assert "FlushDirectTransmit" in bridge
    assert "ReadDirectReply" in bridge
    assert "ProbeDirectBootloader" in bridge
    assert "ParkFastBootloader" in gui_link
    assert "ResumeFastBootloader" in gui_link
    assert "FastBootParkAddress = 0x20000000" in gui_link
    assert "DirectBootProbeTimeoutMs = 1500" in bridge
    assert "DirectBootProbeRetryMs = 250" in bridge

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
    legacy = otg.index("if(setConnected == null)")
    assert "return true;" in otg[legacy : legacy + 500]


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


def test_bundled_betaflight_sequences_resolve_without_elf(tmp_path):
    flash = tmp_path / "flash.bin"
    select_firmware(flash, flight_controller_firmware("SPEEDYBEEF405V5"))

    patches = recognize_betaflight_hotpatches(flash)

    assert patches.symbols is None
    assert patches.delay == 0x0800AB1C
    assert patches.scheduler == 0x08026EF8
    assert patches.scheduler_wait_poll == 0x08027214
    assert patches.system_state == 0x2000243D
    assert patches.systick_uptime == 0x200013CC
    assert patches.read_byte_crc_poll == 0x08050EAC
    assert patches.selected_esc == 0x2000BB10
    assert patches.set_esc_input == 0x0803B7B4
    assert patches.suart_getc == 0x0803A0C4
    assert patches.suart_putc == 0x08050E28


def test_sequence_recognition_rejects_ambiguous_or_changed_code(tmp_path):
    flash = tmp_path / "flash.bin"
    select_firmware(flash, flight_controller_firmware("SPEEDYBEEF405V5"))
    content = bytearray(flash.read_bytes())
    putc = bytes.fromhex("f8b540f2014444ea8004")
    content[0xF0000 : 0xF0000 + len(putc)] = putc
    flash.write_bytes(content)

    with pytest.raises(ValueError, match="2 suart_putc sequences"):
        recognize_betaflight_hotpatches(flash)

    select_firmware(flash, flight_controller_firmware("SPEEDYBEEF405V5"))
    content = bytearray(flash.read_bytes())
    content[0x50EAC] ^= 1
    flash.write_bytes(content)
    with pytest.raises(ValueError, match="0 ReadByteCrc poll sequences"):
        recognize_betaflight_hotpatches(flash)


def test_flight_controller_automatically_installs_recognized_hooks(tmp_path):
    flash = tmp_path / "flash.bin"
    select_firmware(flash, flight_controller_firmware("SPEEDYBEEF405V5"))
    outdir = tmp_path / "run"

    FlightControllerSpec(
        model="SpeedyBeeF405Mini",
        outdir=outdir,
        flash=flash,
        esc_ports=(57833, 57843, 57853, 57863),
        esc_state_ports=(57834, 57844, 57854, 57864),
        monitor_port=57915,
        usbip_port=57916,
        renode="/bin/true",
    ).command()
    script = (outdir / "SpeedyBeeF405Mini.resc").read_text()

    assert "LoadSymbolsFrom" not in script
    assert "# Betaflight hot patches: sequence-recognized" in script
    assert "cpu AddHook 0x08050EAC" not in script
    assert "sysbus WriteWord 0x08050EAC 0xF0AF" in script
    assert "sysbus WriteWord 0x08050EAE 0xB8A0" in script
    assert "sysbus WriteWord 0x080FFFF8 0xBF30" in script
    assert "sysbus WriteWord 0x08027214 0xF0D8" in script
    assert "sysbus WriteWord 0x08027216 0xBEDC" in script
    assert "sysbus WriteWord 0x080FFFDC 0xBF30" in script
    assert "cpu AddHook 0x08050E28" in script
    assert "cpu AddHook 0x0803A0C4" in script
    assert "cpu AddHook 0x0803B7B4" in script


def test_persisted_instruction_patches_preserve_fc_configuration(tmp_path):
    flash = tmp_path / "flash.bin"
    image = flight_controller_firmware("SPEEDYBEEF405V5")
    select_firmware(flash, image)
    patches = recognize_betaflight_hotpatches(flash)
    words = (
        flight_controller._read_byte_crc_wfi_patch(patches.read_byte_crc_poll)
        + flight_controller._scheduler_wfi_patch(patches.scheduler_wait_poll)
    )
    with flash.open("r+b") as stream:
        stream.seek(0x4000)
        stream.write(b"saved config")
        for address, value in words:
            stream.seek(address - FLASH_BASE)
            stream.write(struct.pack("<H", value))

    # Both the recognizer and image identity check accept their own complete,
    # exact persisted patches.  Saved settings must not be factory-reset.
    again = recognize_betaflight_hotpatches(flash)
    assert again.scheduler_wait_poll == patches.scheduler_wait_poll
    assert not select_firmware(flash, image)
    assert flash.read_bytes()[0x4000 : 0x400C] == b"saved config"


def test_flight_controller_leaves_unknown_firmware_unpatched(tmp_path):
    flash = ensure_flash(tmp_path / "flash.bin")
    outdir = tmp_path / "run"

    FlightControllerSpec(
        model="SpeedyBeeF405Mini",
        outdir=outdir,
        flash=flash,
        esc_ports=(57833,),
        esc_state_ports=(57834,),
        monitor_port=57915,
        usbip_port=57916,
        renode="/bin/true",
    ).command()

    assert "cpu AddHook" not in (outdir / "SpeedyBeeF405Mini.resc").read_text()


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

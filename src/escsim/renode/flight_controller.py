"""Renode launch support for emulated flight controllers."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import json
import os
from pathlib import Path

from escsim.renode.generator import Unsupported, find_renode, parse_ihex


FLASH_BASE = 0x08000000
FLASH_SIZE = 1024 * 1024
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
}


@dataclass(frozen=True)
class FlightControllerSpec:
    """Everything needed for one SpeedyBeeF405Mini Renode process."""

    model: str
    outdir: Path
    flash: Path
    esc_ports: tuple[int, ...]
    monitor_port: int
    usbip_port: int
    renode: str | None = None

    def command(self) -> list[str]:
        if self.model != "SpeedyBeeF405Mini":
            raise ValueError(f"unsupported flight controller {self.model}")
        platform = write_speedybee_platform(self.outdir, self.esc_ports, self.flash)
        script = write_speedybee_script(self.outdir, platform, self.flash, self.usbip_port)
        return [
            find_renode(self.renode),
            "--disable-xwt",
            "-e",
            f"include @{script}",
            "--port",
            str(self.monitor_port),
        ]


def resource_root() -> Path:
    return Path(os.fspath(resources.files("escsim.renode").joinpath("resources")))


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

    Returns true when flash was replaced. The first changed programmed byte
    selects a new image; in that case erased flash is rebuilt atomically and
    every HEX chunk is applied. If all image chunks already match, bytes not
    present in the HEX (normally firmware configuration) remain untouched.
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
    if all(current[offset : offset + len(payload)] == payload
           for offset, payload in normalized):
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
    outdir: Path, esc_ports: tuple[int, ...], flash: Path
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
    ports = "\n".join(
        f"    esc{index + 1}Port: {port}"
        for index, port in enumerate(connected_ports)
    )
    path.write_text(
        f'''using "{base}"

rcc:
    hseFrequency: 8000000

persistentFlash: Miscellaneous.AP_PersistentMemory @ none
    fileName: {json.dumps(str(Path(flash).resolve()))}
    address: 0x08000000
    size: 0x100000

baro0: Sensors.AP_DPS310 @ i2c1 0x76

spi1Mux: Miscellaneous.AP_SPIMultiplexer @ spi1

imu0: Sensors.AP_ICM42688 @ spi1Mux 0
    rotation: 26

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
usart2RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x6000012C
    dma: dma1
    stream: 5
    uart: usart2
usart3RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x60000130
    dma: dma1
    stream: 1
    uart: usart3
usart6RxPump: Miscellaneous.AP_UartRxDmaPump @ sysbus 0x60000134
    dma: dma2
    stream: 1
    uart: usart6

timer3UpdateDMA: Miscellaneous.AP_STM32_Timer_UpdateDMA @ sysbus 0x6000013C
    timer: timer3
    UpdateDMA -> dma1@2
timer4UpdateDMA: Miscellaneous.AP_STM32_Timer_UpdateDMA @ sysbus 0x60000140
    timer: timer4
    UpdateDMA -> dma1@6

motorBridge: Miscellaneous.ESCSim_STM32_DShot @ sysbus 0x60000200
    dma: dma1
    timer3: timer3
    timer4: timer4
{ports}

gpioPortA:
    4 -> spi1Mux@0
''',
        encoding="utf-8",
    )
    return path


def write_speedybee_script(
    outdir: Path, platform: Path, flash: Path, usbip_port: int
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
    lines = [f"include @{root / 'peripherals' / item}" for item in includes]
    lines += [
        'mach create "SpeedyBeeF405Mini"',
        f"machine LoadPlatformDescription @{platform}",
        "flash ResetByte 0xFF",
        f"sysbus LoadBinary @{flash} 0x{FLASH_BASE:08X}",
        # Stable non-erased UID: ArduPilot derives USB serial and board ID
        # material from this factory region.
        "sysbus WriteDoubleWord 0x1FFF7A10 0xF96D5489",
        "sysbus WriteDoubleWord 0x1FFF7A14 0xFBD5F38E",
        "sysbus WriteDoubleWord 0x1FFF7A18 0x76181BD3",
        # Fixed bench inputs: 12.0V through the board's 11:1 divider, no
        # current/RSSI, and a mid-scale internal channel used during probes.
        "adc1 FeedSample 1354 10 -1",
        "adc1 FeedSample 0 11 -1",
        "adc1 FeedSample 0 15 -1",
        "adc1 FeedSample 1500 0 -1",
        f'emulation CreateUSBIPServer {usbip_port} "usb"',
        "host.usb Register sysbus.usbOtg",
        f"cpu VectorTableOffset 0x{FLASH_BASE:08X}",
        # Renode restores VTOR to zero on SYSRESETREQ. The STM32 flash is
        # mapped at 0x08000000, so restore the hardware reset value here.
        "macro reset",
        '"""',
        f"    cpu VectorTableOffset 0x{FLASH_BASE:08X}",
        '"""',
        "cpu PerformanceInMips 125",
        'emulation SetGlobalQuantum "0.0001"',
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
        "logLevel 3 sysbus.timer3",
        "logLevel 3 sysbus.timer4",
        "start",
    ]
    path = Path(outdir) / "SpeedyBeeF405Mini.resc"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

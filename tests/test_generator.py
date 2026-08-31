from __future__ import annotations

from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile
import pytest

from escsim.renode import generator
from escsim.settings import TargetSourceSpec
from escsim.target.source import TargetSourceManager
from conftest import DEFAULT_TARGETS


def configure_real_header(tmp_path, monkeypatch):
    header = DEFAULT_TARGETS
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-root"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-root"))
    manager = TargetSourceManager()
    manager.select(TargetSourceSpec("file", str(header)))
    return header


def test_config_without_external_preprocessor(tmp_path, monkeypatch):
    configure_real_header(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "")
    config = generator.config("VIMDRONES_L431")
    assert config["family"] == "l431"
    assert config["throttle_pin"] == "PA2"
    assert not config["dronecan"]


def test_generate_uses_packaged_resources(tmp_path, monkeypatch):
    configure_real_header(tmp_path, monkeypatch)
    output = tmp_path / "generated"
    resc, repl = generator.generate("VIMDRONES_L431", str(output))
    resc_text = Path(resc).read_text()
    repl_text = Path(repl).read_text()
    assert "$resources/scripts/am32_l431.resc" in resc_text
    assert "$repo" not in resc_text
    assert "stm32l431_base.repl" in repl_text
    assert generator.renode_path(generator.HERE) in repl_text


def test_a153_generation_uses_packaged_rom_without_compiler(tmp_path, monkeypatch):
    configure_real_header(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "")
    resc, _repl = generator.generate("FRDM_A153", str(tmp_path / "a153"))
    text = Path(resc).read_text()
    assert "mcxa_rom_api.bin" in text
    assert "0x03004001" in text


def test_e230_batches_dshot_with_exact_capture_timestamps(tmp_path, monkeypatch):
    configure_real_header(tmp_path, monkeypatch)
    _resc, repl = generator.generate("GD32DEV_A_E230", str(tmp_path / "e230"))
    assert "batchDshotFrames: true" in Path(repl).read_text()


def test_a153_timer_model_is_loaded_before_gpio_model():
    script = Path(generator.HERE, "scripts", "am32_a153.resc").read_text()
    assert script.index("MCXA_Ctimer.cs") < script.index("MCXA_Gpio.cs")


def test_all_targets_uses_active_header_not_make(tmp_path, monkeypatch):
    configure_real_header(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "")
    targets = generator.all_targets()
    assert "VIMDRONES_L431" in targets
    assert "TEKKO32_F415" in targets
    assert "VIMDRONES_L431_CAN" in targets


def test_native_library_does_not_trust_working_directory(tmp_path, monkeypatch):
    attacker_library = tmp_path / "build" / "native" / "libam32sim.so"
    attacker_library.parent.mkdir(parents=True)
    attacker_library.write_bytes(b"not trusted")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ESCSIM_AM32SIM_LIBRARY", raising=False)
    selected = generator.native_library_path()
    assert selected is None or Path(selected) != attacker_library


def test_native_library_finds_source_checkout_build(tmp_path, monkeypatch):
    monkeypatch.delenv("ESCSIM_AM32SIM_LIBRARY", raising=False)
    source = tmp_path / "checkout"
    module = source / "src" / "escsim" / "renode" / "generator.py"
    module.parent.mkdir(parents=True)
    module.touch()
    (source / "pyproject.toml").touch()
    built = source / "build" / "native" / "libam32sim.so"
    built.parent.mkdir(parents=True)
    built.touch()
    monkeypatch.setattr(generator, "__file__", str(module))
    monkeypatch.setattr(generator, "_PACKAGE_ROOT", tmp_path / "empty-package")
    selected = generator.native_library_path()
    assert Path(selected).resolve() == built.resolve()


def test_renode_setup_precedes_monitor_listener():
    command = generator.renode_command("renode.exe", 57735, "include @target.resc")
    assert command == [
        "renode.exe",
        "--disable-xwt",
        "-e",
        "include @target.resc",
        "--port",
        "57735",
    ]


def test_renode_config_isolates_command_history(tmp_path):
    config_root = tmp_path / "renode-config"

    config = generator.isolated_renode_config(config_root)

    assert config == str(config_root / "config")
    assert Path(config).read_text() == (
        "[general]\nhistory-path = %s\n" % (config_root / "history").resolve()
    )


def test_renode_execfile_uses_unescaped_path():
    expression = generator.renode_execfile(r"C:\Users\test user\ESCSim\work\status.py")
    assert "\\" not in expression
    assert "C:/Users/test user/ESCSim/work/status.py" in expression


def ihex_record(address, record_type, payload):
    body = bytes([len(payload), address >> 8, address & 0xFF, record_type]) + bytes(
        payload
    )
    checksum = (-sum(body)) & 0xFF
    return ":" + (body + bytes([checksum])).hex().upper()


def write_ihex(path, data_address, data, entry, segment_entry=False):
    lines = [
        ihex_record(0, 4, struct.pack(">H", data_address >> 16)),
        ihex_record(data_address & 0xFFFF, 0, data),
    ]
    if segment_entry:
        lines.append(ihex_record(0, 3, struct.pack(">HH", 0, entry)))
    else:
        lines.append(ihex_record(0, 5, struct.pack(">I", entry)))
    lines.append(ihex_record(0, 1, b""))
    path.write_text("\n".join(lines) + "\n")


def test_standalone_arm_hex_gets_minimal_elf_wrapper(tmp_path):
    firmware = tmp_path / "local-build.hex"
    reset = 0x08001009
    write_ihex(
        firmware,
        0x08001000,
        struct.pack("<II", 0x20008000, reset) + b"application",
        reset,
    )
    elf, load, symbols = generator.application_image(
        str(firmware), 0x08001000, family="l431", outdir=str(tmp_path)
    )
    assert load is None
    assert symbols == elf
    with Path(elf).open("rb") as stream:
        image = ELFFile(stream)
        assert image.header.e_machine == "EM_ARM"
        assert image.header.e_entry == reset
        segments = list(image.iter_segments())
        assert segments[0].header.p_vaddr == 0x08001000
        assert segments[0].data().endswith(b"application")


def test_standalone_v203_hex_uses_zero_based_flash_alias(tmp_path):
    firmware = tmp_path / "v203.hex"
    write_ihex(
        firmware,
        0x08001000,
        b"riscv-application",
        0x1000,
        segment_entry=True,
    )
    elf, load, symbols = generator.application_image(
        str(firmware), 0x1000, family="v203", outdir=str(tmp_path)
    )
    assert load is None
    assert symbols == elf
    with Path(elf).open("rb") as stream:
        image = ELFFile(stream)
        assert image.header.e_machine == "EM_RISCV"
        assert image.header.e_entry == 0x1000
        assert list(image.iter_segments())[0].header.p_vaddr == 0x1000


def test_v203_hex_without_start_record_is_rejected(tmp_path):
    firmware = tmp_path / "v203-no-entry.hex"
    firmware.write_text(
        "\n".join(
            (
                ihex_record(0, 4, struct.pack(">H", 0x0800)),
                ihex_record(0x1000, 0, b"riscv-application"),
                ihex_record(0, 1, b""),
            )
        )
        + "\n"
    )
    with pytest.raises(generator.Unsupported, match="no start-address record"):
        generator.application_image(
            str(firmware), 0x1000, family="v203", outdir=str(tmp_path)
        )

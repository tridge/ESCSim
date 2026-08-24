from __future__ import annotations

from pathlib import Path

from escsim.renode import generator
from escsim.settings import TargetSourceSpec
from escsim.target.source import TargetSourceManager


def configure_real_header(tmp_path, monkeypatch):
    header = Path(__file__).parents[2] / "AM32.renode" / "Inc" / "targets.h"
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
    assert str(Path(generator.HERE).resolve()) in repl_text


def test_a153_generation_uses_packaged_rom_without_compiler(tmp_path, monkeypatch):
    configure_real_header(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "")
    resc, _repl = generator.generate("FRDM_A153", str(tmp_path / "a153"))
    text = Path(resc).read_text()
    assert "mcxa_rom_api.bin" in text
    assert "0x03004001" in text


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

from __future__ import annotations

from pathlib import Path
import signal
import time
from types import SimpleNamespace

import pytest

from escsim.control import params
from escsim.settings import (
    LauncherSettings,
    Settings,
    SettingsStore,
    default_config_dir,
    TargetSourceSpec,
)
from escsim.target.source import TargetSourceManager


def test_network_free_eeprom_defaults_match_bundled_firmware():
    image = params.base_image()
    assert len(image) == params.EEPROM_SIZE
    assert image[1] == 4  # EEPROM_VERSION
    assert image[3:5] == bytes((2, 21))
    assert image[48:] == b"\xff" * (params.EEPROM_SIZE - 48)


def test_launcher_preferences_round_trip(tmp_path):
    store = SettingsStore(tmp_path)
    expected = LauncherSettings(
        target="TEKKO32_F415",
        bootloader="/packs/bootloader.elf",
        firmware="/packs/firmware.hex",
        eeprom="blank",
        configurator="off",
        protocol="direct",
        esc_count=8,
        can_bus=-1,
        flight_controller="SpeedyBeeF405Mini",
        fc_firmware="SPEEDYBEEF405V5",
        fc_boot_mode="dfu",
    )
    store.save(Settings(launcher=expected))
    assert store.load().launcher == expected


def test_changing_targets_source_preserves_launcher_preferences(tmp_path):
    store = SettingsStore(tmp_path / "config")
    preferences = LauncherSettings(target="VIMDRONES_L431", can_bus=-1)
    store.save(Settings(launcher=preferences))
    header = tmp_path / "targets.h"
    header.write_text(
        "".join(
            '#ifdef TEST_TARGET_%02u\n#define FILE_NAME "TEST_TARGET_%02u"\n#endif\n'
            % (number, number)
            for number in range(12)
        )
    )
    TargetSourceManager(settings_store=store, cache_dir=tmp_path / "cache").select(
        TargetSourceSpec("file", str(header))
    )
    assert store.load().launcher == preferences


def test_control_gui_embeds_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QWidget

    from escsim.control import ui

    app = QApplication.instance() or QApplication([])
    container = QWidget()
    args = SimpleNamespace(
        host="127.0.0.1",
        port=39833,
        state_port=39834,
        can_uri="mcast:8",
        backend="renode",
        renode_can=False,
        esc_number=2,
        can_esc_index=1,
        poles=14,
        control_port=0,
        log=None,
        replay=None,
    )
    old_sigint = signal.getsignal(signal.SIGINT)

    cleanup = ui.create_ui(args, app=app, container=container)
    app.processEvents()

    assert callable(cleanup)
    assert callable(container._sitl_gui_abort_cleanup)
    assert container.layout() is not None
    assert container.layout().count() >= 4
    runtime = container._sitl_gui_runtime
    assert runtime["sim"].desired_speedup == pytest.approx(1.0)
    assert runtime["speed_slider"].value() == 150
    assert runtime["audio_output"].count() >= 1
    assert runtime["audio_status"] is not None
    if ui.HAVE_PYQTGRAPH:
        runtime["graph_i_check"].setChecked(True)
        runtime["graph_rpm_check"].setChecked(True)
        app.processEvents()
        assert runtime["graph_windows"]["i"][0].windowTitle().endswith("ESC 2")
        assert runtime["rpm_graph"]["win"].windowTitle().endswith("ESC 2")
    assert signal.getsignal(signal.SIGINT) == old_sigint
    models = Path(default_config_dir() / "models")
    assert (models / "default_7inch.json").is_file()
    cleanup()
    cleanup()
    container._sitl_gui_abort_cleanup()
    container.close()


def test_control_gui_eeprom_poll_does_not_block_qt(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QWidget

    from escsim.control import backend, ui

    def slow_fetch(_self):
        time.sleep(0.6)
        return None, None

    monkeypatch.setattr(backend.EepromClient, "fetch", slow_fetch)
    app = QApplication.instance() or QApplication([])
    container = QWidget()
    args = SimpleNamespace(
        host="127.0.0.1",
        port=39933,
        state_port=39934,
        can_uri="mcast:8",
        backend="renode",
        renode_can=False,
        poles=14,
        control_port=0,
        log=None,
        replay=None,
    )
    cleanup = ui.create_ui(args, app=app, container=container)
    try:
        # Let the 100 ms update timer become due, then measure one event-loop
        # dispatch. Before the fix it ran slow_fetch() synchronously here.
        time.sleep(0.12)
        started = time.monotonic()
        app.processEvents()
        assert time.monotonic() - started < 0.3
        assert container._sitl_gui_runtime["param_state"]["fetching"]
    finally:
        cleanup()
        container.close()

from __future__ import annotations

import json

import pytest

from escsim.settings import (
    DEFAULT_ARTIFACT_BASE_URL,
    DEFAULT_TARGETS_URL,
    Settings,
    SettingsStore,
    TargetSourceSpec,
)


def test_defaults_without_file(tmp_path):
    store = SettingsStore(tmp_path)
    settings = store.load()
    assert settings.targets_source == TargetSourceSpec("url", DEFAULT_TARGETS_URL)
    assert settings.artifact_base_url == DEFAULT_ARTIFACT_BASE_URL


def test_round_trip_is_versioned_and_atomic(tmp_path):
    store = SettingsStore(tmp_path)
    expected = Settings(
        targets_source=TargetSourceSpec("file", "/tmp/custom-targets.h"),
        artifact_base_url="https://example.test/ESCSim/v1/",
    )
    store.save(expected)
    assert store.load() == expected
    assert json.loads(store.path.read_text())["schema"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_obsolete_default_artifact_host_is_migrated(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "schema": 1,
                "artifact_base_url": "https://am32.tridgell.net/ESCSim/v1/",
            }
        )
    )
    assert store.load().artifact_base_url == DEFAULT_ARTIFACT_BASE_URL


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"schema": 99}, "unsupported settings schema"),
        ({"schema": 1, "targets_source": []}, "targets_source must be"),
        (
            {"schema": 1, "targets_source": {"kind": "ftp", "location": "x"}},
            "cannot read settings from.*unsupported target source kind",
        ),
    ],
)
def test_invalid_settings_are_not_silently_accepted(tmp_path, payload, message):
    store = SettingsStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        store.load()


def test_target_source_requires_a_location():
    with pytest.raises(ValueError, match="cannot be empty"):
        TargetSourceSpec("url", "")

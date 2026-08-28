"""Persistent ESCSim settings with atomic, versioned updates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from platformdirs import user_cache_path, user_config_path


DEFAULT_TARGETS_URL = (
    "https://raw.githubusercontent.com/am32-firmware/AM32/refs/heads/main/Inc/targets.h"
)
DEFAULT_ARTIFACT_BASE_URL = "https://firmware.ardupilot.org/Tools/AM32-tools/ESCSim/v1/"
_LEGACY_ARTIFACT_BASE_URLS = {"https://am32.tridgell.net/ESCSim/v1/"}
SETTINGS_SCHEMA = 1


@dataclass(frozen=True)
class TargetSourceSpec:
    """A persisted source for targets.h."""

    kind: str = "url"
    location: str = DEFAULT_TARGETS_URL

    def __post_init__(self) -> None:
        if self.kind not in {"url", "file"}:
            raise ValueError(f"unsupported target source kind: {self.kind}")
        if not self.location:
            raise ValueError("target source location cannot be empty")


@dataclass(frozen=True)
class LauncherSettings:
    """Selections restored when the desktop application is reopened."""

    target: str = ""
    bootloader: str = "auto"
    firmware: str = "auto"
    eeprom: str = "defaults"
    configurator: str = "serial"
    protocol: str = "4way"
    esc_count: int = 1
    can_bus: int = 8


@dataclass(frozen=True)
class Settings:
    schema: int = SETTINGS_SCHEMA
    targets_source: TargetSourceSpec = TargetSourceSpec()
    artifact_base_url: str = DEFAULT_ARTIFACT_BASE_URL
    launcher: LauncherSettings = LauncherSettings()


def default_config_dir() -> Path:
    override = os.environ.get("ESCSIM_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path(user_config_path("ESCSim", appauthor="AM32"))


def default_cache_dir() -> Path:
    override = os.environ.get("ESCSIM_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(user_cache_path("ESCSim", appauthor="AM32"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class SettingsStore:
    """Load and save user settings without exposing partial JSON files."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        self.path = self.config_dir / "settings.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"cannot read settings from {self.path}: {error}"
            ) from error

        schema = raw.get("schema")
        if schema != SETTINGS_SCHEMA:
            raise ValueError(
                f"unsupported settings schema {schema!r}; expected {SETTINGS_SCHEMA}"
            )
        source_raw = raw.get("targets_source", {})
        if not isinstance(source_raw, dict):
            raise ValueError("targets_source must be an object")
        try:
            source = TargetSourceSpec(
                kind=str(source_raw.get("kind", "url")),
                location=str(source_raw.get("location", DEFAULT_TARGETS_URL)),
            )
        except ValueError as error:
            raise ValueError(
                f"cannot read settings from {self.path}: {error}"
            ) from error
        artifact_base_url = raw.get("artifact_base_url", DEFAULT_ARTIFACT_BASE_URL)
        if not isinstance(artifact_base_url, str) or not artifact_base_url:
            raise ValueError("artifact_base_url must be a non-empty string")
        if artifact_base_url in _LEGACY_ARTIFACT_BASE_URLS:
            artifact_base_url = DEFAULT_ARTIFACT_BASE_URL
        launcher_raw = raw.get("launcher", {})
        if not isinstance(launcher_raw, dict):
            raise ValueError("launcher must be an object")
        launcher = LauncherSettings(
            target=str(launcher_raw.get("target", "")),
            bootloader=str(launcher_raw.get("bootloader", "auto")),
            firmware=str(launcher_raw.get("firmware", "auto")),
            eeprom=str(launcher_raw.get("eeprom", "defaults")),
            configurator=str(launcher_raw.get("configurator", "serial")),
            protocol=str(launcher_raw.get("protocol", "4way")),
            esc_count=int(launcher_raw.get("esc_count", 1)),
            can_bus=int(launcher_raw.get("can_bus", 8)),
        )
        if launcher.eeprom not in {"defaults", "blank"}:
            raise ValueError("launcher eeprom must be defaults or blank")
        if launcher.configurator not in {"serial", "usb", "off"}:
            raise ValueError("launcher configurator must be serial, usb, or off")
        if launcher.protocol not in {"4way", "direct"}:
            raise ValueError("launcher protocol must be 4way or direct")
        if not 1 <= launcher.esc_count <= 8:
            raise ValueError("launcher esc_count must be 1..8")
        if not -1 <= launcher.can_bus <= 9:
            raise ValueError("launcher can_bus must be -1..9")
        return Settings(
            schema=SETTINGS_SCHEMA,
            targets_source=source,
            artifact_base_url=artifact_base_url,
            launcher=launcher,
        )

    def save(self, settings: Settings) -> None:
        payload = json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n"
        _atomic_write(self.path, payload.encode("utf-8"))

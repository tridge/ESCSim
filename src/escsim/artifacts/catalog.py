"""Validated, hash-addressed ESCSim firmware and bootloader downloads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from escsim.settings import SettingsStore, default_cache_dir


CATALOG_SCHEMA = 1
MANIFEST_SCHEMA = 1
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,127}\Z")


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstalledArtifact:
    project: str
    release: str
    target: str
    image: Path
    targets_header: Path | None = None
    metadata: dict | None = None
    companion_elf: Path | None = None


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_response(response, limit: int) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > limit:
                raise CatalogError("download exceeds size limit")
        except ValueError as error:
            raise CatalogError("invalid Content-Length") from error
    content = response.read(limit + 1)
    if len(content) > limit:
        raise CatalogError("download exceeds size limit")
    return content


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"{label} must be a relative path")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or value.startswith("//")
    ):
        raise CatalogError(f"unsafe {label}: {value!r}")
    return value


def _release_id(value: object, label: str = "release") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CatalogError(f"invalid {label} identifier")
    return value


def _artifact(value: object, label: str = "artifact") -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object")
    path = _safe_relative(value.get("path"), f"{label} path")
    size = value.get("size")
    digest = value.get("sha256")
    if not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise CatalogError(f"invalid {label} size")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise CatalogError(f"invalid {label} sha256")
    return {"path": path, "size": size, "sha256": digest}


def validate_catalog(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != CATALOG_SCHEMA:
        raise CatalogError("unsupported catalog schema")
    releases = raw.get("releases")
    if not isinstance(releases, dict):
        raise CatalogError("catalog releases must be an object")
    clean = {"schema_version": CATALOG_SCHEMA, "generated_at": raw.get("generated_at")}
    clean_releases = {}
    for project in ("firmware", "bootloader"):
        entries = releases.get(project)
        if not isinstance(entries, list) or len(entries) > 1000:
            raise CatalogError(f"catalog {project} releases must be a list")
        seen = set()
        clean_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise CatalogError("catalog release entry must be an object")
            release = _release_id(entry.get("id"))
            if release in seen:
                raise CatalogError(f"duplicate {project} release {release}")
            seen.add(release)
            channel = entry.get("channel", "development")
            if channel not in {"stable", "development", "nightly"}:
                raise CatalogError(f"invalid channel for {project} {release}")
            targets = entry.get("targets")
            if (
                not isinstance(targets, list)
                or len(targets) > 2000
                or any(
                    not isinstance(t, str) or not _TARGET.fullmatch(t) for t in targets
                )
            ):
                raise CatalogError(f"invalid targets for {project} {release}")
            clean_entries.append(
                {
                    "id": release,
                    "channel": channel,
                    "revision": str(entry.get("revision", "")),
                    "manifest": _safe_relative(entry.get("manifest"), "manifest"),
                    "targets": targets,
                }
            )
        clean_releases[project] = clean_entries
    clean["releases"] = clean_releases
    channels = raw.get("channels", {})
    if not isinstance(channels, dict):
        raise CatalogError("catalog channels must be an object")
    for channel, selections in channels.items():
        if channel not in {"stable", "development", "nightly"} or not isinstance(
            selections, dict
        ):
            raise CatalogError("invalid catalog channel")
        for project, release in selections.items():
            if project not in {"firmware", "bootloader"} or not isinstance(
                release, str
            ):
                raise CatalogError("invalid catalog channel selection")
            if not any(entry["id"] == release for entry in clean_releases[project]):
                raise CatalogError("catalog channel references a missing release")
    clean["channels"] = channels
    return clean


def validate_manifest(raw: object, project: str, release: str) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != MANIFEST_SCHEMA:
        raise CatalogError("unsupported manifest schema")
    if raw.get("project") != project or raw.get("release") != release:
        raise CatalogError("manifest identity does not match catalog")
    if raw.get("channel") not in {"stable", "development", "nightly"}:
        raise CatalogError("invalid manifest channel")
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets or len(targets) > 2000:
        raise CatalogError("manifest targets must be a non-empty list")
    clean_targets = []
    seen = set()
    for target in targets:
        if not isinstance(target, dict):
            raise CatalogError("manifest target must be an object")
        name = target.get("name")
        if not isinstance(name, str) or not _TARGET.fullmatch(name) or name in seen:
            raise CatalogError("invalid or duplicate manifest target")
        seen.add(name)
        item = dict(target)
        item["artifact"] = _artifact(target.get("artifact"))
        raw_images = target.get("images")
        if raw_images is not None:
            if not isinstance(raw_images, dict) or set(raw_images) != {"elf", "hex"}:
                raise CatalogError("manifest images must contain exactly ELF and HEX")
            images = {
                image_format: _artifact(spec, f"{image_format} image")
                for image_format, spec in raw_images.items()
            }
            elf_path = PurePosixPath(images["elf"]["path"])
            hex_path = PurePosixPath(images["hex"]["path"])
            if (
                elf_path.suffix.lower() != ".elf"
                or hex_path.suffix.lower() != ".hex"
                or elf_path.with_suffix("") != hex_path.with_suffix("")
            ):
                raise CatalogError("manifest ELF and HEX images must be a matching pair")
            if images["elf"] != item["artifact"]:
                raise CatalogError("manifest default artifact must be the ELF image")
            item["images"] = images
        for field in ("family", "pin"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise CatalogError(f"manifest target has invalid {field}")
        if not isinstance(item.get("dronecan"), bool):
            raise CatalogError("manifest target has invalid dronecan flag")
        if project == "firmware" and (
            not isinstance(item.get("app_base"), int)
            or not 0 <= item["app_base"] <= 0xFFFFFFFF
        ):
            raise CatalogError("firmware target has invalid app_base")
        if project == "bootloader" and not isinstance(item.get("sized", False), bool):
            raise CatalogError("bootloader target has invalid sized flag")
        clean_targets.append(item)
    clean = dict(raw)
    clean["targets"] = clean_targets
    if project == "firmware":
        clean["targets_header"] = _artifact(raw.get("targets_header"), "targets.h")
    return clean


class ArtifactRepository:
    """Fetch catalogs and immutable artifacts, retaining last-known-good data."""

    def __init__(self, base_url: str | None = None, cache_dir: Path | None = None):
        settings = SettingsStore().load()
        self.base_url = (base_url or settings.artifact_base_url).rstrip("/") + "/"
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise CatalogError("artifact repository must be an HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise CatalogError("non-loopback artifact repositories require HTTPS")
        self._origin = (parsed.scheme, parsed.netloc)
        self.cache_dir = Path(cache_dir or default_cache_dir() / "artifacts")

    def _url(self, relative: str) -> str:
        relative = _safe_relative(relative, "repository path")
        result = urljoin(self.base_url, relative)
        parsed = urlparse(result)
        if (parsed.scheme, parsed.netloc) != self._origin or not result.startswith(
            self.base_url
        ):
            raise CatalogError("repository path escapes configured base URL")
        return result

    def _check_response_url(self, response) -> None:
        final_url = response.geturl()
        parsed = urlparse(final_url)
        if (parsed.scheme, parsed.netloc) != self._origin or not final_url.startswith(
            self.base_url
        ):
            raise CatalogError("repository redirected outside configured base URL")

    def _json(self, relative: str, cached: Path, refresh: bool, validator) -> dict:
        if not refresh and cached.is_file():
            try:
                return validator(json.loads(cached.read_text("utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, CatalogError):
                pass
        try:
            request = Request(
                self._url(relative), headers={"Accept": "application/json"}
            )
            with urlopen(request, timeout=30) as response:
                self._check_response_url(response)
                content = _read_response(response, MAX_JSON_BYTES)
            raw = json.loads(content.decode("utf-8"))
            clean = validator(raw)
            _atomic_write(
                cached, (json.dumps(clean, indent=2, sort_keys=True) + "\n").encode()
            )
            return clean
        except (
            OSError,
            UnicodeError,
            HTTPError,
            URLError,
            json.JSONDecodeError,
            CatalogError,
        ) as error:
            if cached.is_file():
                try:
                    return validator(json.loads(cached.read_text("utf-8")))
                except (OSError, UnicodeError, json.JSONDecodeError, CatalogError):
                    pass
            raise CatalogError(f"cannot fetch {relative}: {error}") from error

    def catalog(self, refresh: bool = False) -> dict:
        return self._json(
            "catalog.json", self.cache_dir / "catalog.json", refresh, validate_catalog
        )

    def manifest(self, project: str, release: str, refresh: bool = False) -> dict:
        if project not in {"firmware", "bootloader"}:
            raise CatalogError("project must be firmware or bootloader")
        release = _release_id(release)
        catalog = self.catalog(refresh=refresh)
        entry = next(
            (e for e in catalog["releases"][project] if e["id"] == release), None
        )
        if entry is None:
            raise CatalogError(f"no {project} release {release}")
        cached = self.cache_dir / "manifests" / project / f"{release}.json"
        return self._json(
            entry["manifest"],
            cached,
            refresh,
            lambda raw: validate_manifest(raw, project, release),
        )

    def _download(self, spec: dict, destination: Path) -> Path:
        spec = _artifact(spec)
        if destination.is_file():
            content_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            if (
                destination.stat().st_size == spec["size"]
                and content_hash == spec["sha256"]
            ):
                return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                request = Request(self._url(spec["path"]))
                with urlopen(request, timeout=60) as response:
                    self._check_response_url(response)
                    while True:
                        chunk = response.read(
                            min(1024 * 1024, spec["size"] - received + 1)
                        )
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > spec["size"]:
                            raise CatalogError("artifact exceeds declared size")
                        digest.update(chunk)
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if received != spec["size"] or digest.hexdigest() != spec["sha256"]:
                raise CatalogError("artifact size or SHA-256 mismatch")
            os.replace(temporary, destination)
            return destination
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def install(
        self,
        project: str,
        release: str,
        target: str,
        image_format: str = "elf",
    ) -> InstalledArtifact:
        manifest = self.manifest(project, release)
        item = next((t for t in manifest["targets"] if t["name"] == target), None)
        if item is None:
            raise CatalogError(f"{project} {release} has no target {target}")
        images = item.get("images", {"elf": item["artifact"]})
        if image_format not in {"elf", "hex"}:
            raise CatalogError("image format must be elf or hex")
        if image_format not in images:
            raise CatalogError(
                f"{project} {release} target {target} has no {image_format.upper()} image"
            )
        root = self.cache_dir / "objects" / project / release / target
        selected = images[image_format]
        image_name = PurePosixPath(selected["path"]).name
        image = self._download(selected, root / image_name)
        companion_elf = None
        if image_format != "elf" and "elf" in images:
            elf_spec = images["elf"]
            elf_name = PurePosixPath(elf_spec["path"]).name
            companion_elf = self._download(elf_spec, root / elf_name)
        targets_header = None
        if project == "firmware":
            targets_header = self._download(
                manifest["targets_header"],
                self.cache_dir / "objects" / project / release / "targets.h",
            )
        return InstalledArtifact(
            project=project,
            release=release,
            target=target,
            image=image,
            targets_header=targets_header,
            metadata=item,
            companion_elf=companion_elf,
        )

    def compatible_bootloader(self, release: str, firmware_target: dict) -> dict:
        manifest = self.manifest("bootloader", release)
        candidates = [
            item
            for item in manifest["targets"]
            if item["family"].lower() == firmware_target["family"].lower()
            and item["pin"].upper() == firmware_target["pin"].upper()
        ]
        wanted_can = bool(firmware_target["dronecan"])
        candidates.sort(
            key=lambda item: (
                item["dronecan"] != wanted_can,
                item.get("sized", False),
                item["name"],
            )
        )
        if not candidates or candidates[0]["dronecan"] != wanted_can:
            raise CatalogError("no compatible bootloader variant")
        return candidates[0]

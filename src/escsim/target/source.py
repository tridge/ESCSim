"""Fetch, validate, cache, and select AM32 targets.h inputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
from importlib import resources
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from typing import BinaryIO, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from escsim.settings import (
    DEFAULT_TARGETS_URL,
    SettingsStore,
    TargetSourceSpec,
    default_cache_dir,
)


MAX_TARGETS_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0
INDEX_SCHEMA = 1
MIN_TARGET_COUNT = 10
_DIRECTIVE = re.compile(r"^\s*#\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b(?P<body>.*)$")
_IDENTIFIER = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")
_FILE_NAME = re.compile(r'^\s*#\s*define\s+FILE_NAME\s+"[^"]+"')


class TargetSourceError(RuntimeError):
    """A target source could not be safely loaded or validated."""


@dataclass(frozen=True)
class TargetDocument:
    content: bytes
    sha256: str
    targets: tuple[str, ...]
    source: TargetSourceSpec
    fetched_at: str
    last_checked_at: str
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _read_limited(stream: BinaryIO, declared_length: str | None = None) -> bytes:
    if declared_length:
        try:
            length = int(declared_length)
        except ValueError as error:
            raise TargetSourceError("invalid Content-Length") from error
        if length < 0 or length > MAX_TARGETS_BYTES:
            raise TargetSourceError(
                f"targets.h is too large ({length} bytes; limit {MAX_TARGETS_BYTES})"
            )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, MAX_TARGETS_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_TARGETS_BYTES:
            raise TargetSourceError(
                f"targets.h exceeds the {MAX_TARGETS_BYTES}-byte limit"
            )
    return b"".join(chunks)


def _url_is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return parsed.username is None and parsed.password is None
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def extract_target_names(text: str) -> tuple[str, ...]:
    """Find target blocks which define FILE_NAME without expanding macros.

    This is only the source-browser catalog. The generator's C preprocessor
    remains authoritative for each selected target.
    """

    stack: list[str | None] = []
    found: set[str] = set()
    for line in text.splitlines():
        match = _DIRECTIVE.match(line)
        if not match:
            continue
        directive = match.group("name")
        body = match.group("body")
        if directive in {"ifdef", "ifndef"}:
            identifier = _IDENTIFIER.match(body)
            stack.append(identifier.group(1) if identifier else None)
        elif directive == "if":
            stack.append(None)
        elif directive == "endif":
            if not stack:
                raise TargetSourceError("targets.h contains an unmatched #endif")
            stack.pop()
        elif directive in {"include", "include_next", "import", "embed"}:
            raise TargetSourceError(f"targets.h contains forbidden #{directive}")
        elif directive == "pragma" and "once" not in body.lower():
            raise TargetSourceError("targets.h contains a forbidden #pragma")
        elif directive == "define" and "_Pragma" in body:
            raise TargetSourceError("targets.h contains a forbidden _Pragma")

        if _FILE_NAME.match(line):
            target = next((item for item in reversed(stack) if item), None)
            if target:
                found.add(target)
    if stack:
        raise TargetSourceError("targets.h contains an unterminated conditional")
    return tuple(sorted(found))


def validate_targets_header(content: bytes) -> tuple[str, tuple[str, ...]]:
    if not content:
        raise TargetSourceError("targets.h is empty")
    if len(content) > MAX_TARGETS_BYTES:
        raise TargetSourceError(f"targets.h exceeds the {MAX_TARGETS_BYTES}-byte limit")
    if b"\x00" in content:
        raise TargetSourceError("targets.h contains a NUL byte")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TargetSourceError("targets.h is not valid UTF-8") from error
    if "_Pragma" in text:
        raise TargetSourceError("targets.h contains forbidden _Pragma input")
    targets = extract_target_names(text)
    if len(targets) < MIN_TARGET_COUNT:
        raise TargetSourceError(
            f"targets.h yielded only {len(targets)} targets; expected at least "
            f"{MIN_TARGET_COUNT}"
        )
    return hashlib.sha256(content).hexdigest(), targets


class TargetSourceManager:
    """Own target source selection and its last-known-good snapshots."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        cache_dir: Path | None = None,
        timeout: float = FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self.settings_store = settings_store or SettingsStore()
        root = Path(cache_dir) if cache_dir else default_cache_dir()
        self.cache_dir = root / "targets"
        self.timeout = timeout
        self.index_path = self.cache_dir / "index.json"

    @staticmethod
    def _source_key(source: TargetSourceSpec) -> str:
        value = f"{source.kind}\0{source.location}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _load_index(self) -> dict:
        if not self.index_path.exists():
            return {"schema": INDEX_SCHEMA, "sources": {}}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TargetSourceError(
                f"cannot read target cache index: {error}"
            ) from error
        if value.get("schema") != INDEX_SCHEMA or not isinstance(
            value.get("sources"), dict
        ):
            raise TargetSourceError("unsupported or invalid target cache index")
        return value

    def _save_index(self, index: dict) -> None:
        data = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(self.index_path, data)

    def _cached(self, source: TargetSourceSpec) -> TargetDocument | None:
        index = self._load_index()
        record = index["sources"].get(self._source_key(source))
        if not isinstance(record, dict):
            return None
        digest = record.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None
        content_path = self.cache_dir / "objects" / digest / "targets.h"
        try:
            content = content_path.read_bytes()
        except OSError:
            return None
        actual_digest, targets = validate_targets_header(content)
        if actual_digest != digest:
            raise TargetSourceError(f"cached targets.h hash mismatch at {content_path}")
        return TargetDocument(
            content=content,
            sha256=digest,
            targets=targets,
            source=source,
            fetched_at=str(record.get("fetched_at", "unknown")),
            last_checked_at=str(
                record.get("last_checked_at", record.get("fetched_at", "unknown"))
            ),
            etag=record.get("etag"),
            last_modified=record.get("last_modified"),
            from_cache=True,
        )

    def _fetch_url(
        self, source: TargetSourceSpec, cached: TargetDocument | None
    ) -> tuple[bytes | None, str | None, str | None]:
        # The URL is an explicit, local-user setting. HTTPS endpoints may be
        # public or private intentionally; downloaded content is still bounded
        # and treated as untrusted input after transport.
        if not _url_is_allowed(source.location):
            raise TargetSourceError(
                "target URLs must use HTTPS; plain HTTP is allowed only for loopback"
            )
        headers = {"Accept": "text/plain", "User-Agent": "ESCSim/0.1"}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        request = Request(source.location, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                if not _url_is_allowed(final_url):
                    raise TargetSourceError(
                        f"target URL redirected to unsafe URL: {final_url}"
                    )
                content = _read_limited(
                    response, response.headers.get("Content-Length")
                )
                return (
                    content,
                    response.headers.get("ETag"),
                    response.headers.get("Last-Modified"),
                )
        except HTTPError as error:
            if error.code == 304 and cached:
                return None, cached.etag, cached.last_modified
            raise TargetSourceError(
                f"target download failed with HTTP status {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise TargetSourceError(f"target download failed: {error}") from error

    @staticmethod
    def _read_file(source: TargetSourceSpec) -> bytes:
        path = Path(source.location).expanduser()
        try:
            with path.open("rb") as stream:
                return _read_limited(stream, str(path.stat().st_size))
        except TargetSourceError:
            raise
        except OSError as error:
            raise TargetSourceError(
                f"cannot read targets file {path}: {error}"
            ) from error

    def refresh(self, source: TargetSourceSpec | None = None) -> TargetDocument:
        source = source or self.settings_store.load().targets_source
        cached = self._cached(source)
        if source.kind == "url":
            content, etag, last_modified = self._fetch_url(source, cached)
            if content is None:
                if cached is None:
                    raise TargetSourceError(
                        "server reported targets.h not modified but no cached copy exists"
                    )
                checked_at = _utc_now()
                index = self._load_index()
                record = index["sources"].get(self._source_key(source))
                if not isinstance(record, dict):
                    raise TargetSourceError(
                        "target cache record disappeared during refresh"
                    )
                record["last_checked_at"] = checked_at
                self._save_index(index)
                return TargetDocument(
                    content=cached.content,
                    sha256=cached.sha256,
                    targets=cached.targets,
                    source=cached.source,
                    fetched_at=cached.fetched_at,
                    last_checked_at=checked_at,
                    etag=cached.etag,
                    last_modified=cached.last_modified,
                    from_cache=True,
                )
        else:
            content = self._read_file(source)
            etag = None
            last_modified = None

        digest, targets = validate_targets_header(content)
        fetched_at = _utc_now()
        object_dir = self.cache_dir / "objects" / digest
        content_path = object_dir / "targets.h"
        if not content_path.exists():
            _atomic_write(content_path, content)
        metadata = {
            "schema": INDEX_SCHEMA,
            "source": asdict(source),
            "sha256": digest,
            "fetched_at": fetched_at,
            "last_checked_at": fetched_at,
            "etag": etag,
            "last_modified": last_modified,
            "target_count": len(targets),
        }
        _atomic_write(
            object_dir / "metadata.json",
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        index = self._load_index()
        index["sources"][self._source_key(source)] = metadata
        self._save_index(index)
        return TargetDocument(
            content=content,
            sha256=digest,
            targets=targets,
            source=source,
            fetched_at=fetched_at,
            last_checked_at=fetched_at,
            etag=etag,
            last_modified=last_modified,
        )

    def active(self, refresh: bool = False) -> TargetDocument:
        source = self.settings_store.load().targets_source
        if refresh:
            return self.refresh(source)
        cached = self._cached(source)
        if cached:
            return cached
        try:
            return self.refresh(source)
        except TargetSourceError:
            if source != TargetSourceSpec("url", DEFAULT_TARGETS_URL):
                raise
            return self.bundled_default()

    @staticmethod
    def bundled_default() -> TargetDocument:
        """The release snapshot used for a network-free first start."""

        resource = resources.files("escsim").joinpath("resources", "default-targets.h")
        try:
            content = resource.read_bytes()
        except OSError as error:
            raise TargetSourceError(
                "default targets.h is unavailable and no cached copy exists"
            ) from error
        digest, targets = validate_targets_header(content)
        return TargetDocument(
            content=content,
            sha256=digest,
            targets=targets,
            source=TargetSourceSpec("url", DEFAULT_TARGETS_URL),
            fetched_at="bundled",
            last_checked_at="bundled",
            from_cache=True,
        )

    def select(self, source: TargetSourceSpec) -> TargetDocument:
        """Validate/cache a new source before persisting the selection."""

        document = self.refresh(source)
        current = self.settings_store.load()
        self.settings_store.save(replace(current, targets_source=source))
        return document

    def iter_cached(self) -> Iterator[TargetDocument]:
        index = self._load_index()
        for record in index["sources"].values():
            source_raw = record.get("source", {})
            try:
                source = TargetSourceSpec(**source_raw)
            except (TypeError, ValueError):
                continue
            document = self._cached(source)
            if document:
                yield document

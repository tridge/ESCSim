from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from escsim.settings import DEFAULT_TARGETS_URL, SettingsStore, TargetSourceSpec
from escsim.target.source import (
    MAX_TARGETS_BYTES,
    TargetSourceError,
    TargetSourceManager,
    extract_target_names,
    validate_targets_header,
)


def targets_header(count: int = 12, suffix: str = "") -> bytes:
    blocks = []
    for number in range(count):
        blocks.append(
            f"""#ifdef TEST_TARGET_{number:02d}\n"""
            f"""#define FILE_NAME "TEST_TARGET_{number:02d}"\n"""
            f"""#define MOTOR_PIN {number}\n"""
            "#endif\n"
        )
    return ("/* fixture */\n" + "".join(blocks) + suffix).encode()


class FixtureHandler(BaseHTTPRequestHandler):
    body = targets_header()
    etag = '"fixture-v1"'
    requests = 0
    conditional_requests = 0

    def do_GET(self):
        type(self).requests += 1
        if self.headers.get("If-None-Match") == type(self).etag:
            type(self).conditional_requests += 1
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.send_header("ETag", type(self).etag)
        self.send_header("Last-Modified", "Mon, 24 Aug 2026 00:00:00 GMT")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, _format, *args):
        pass


@contextmanager
def fixture_server():
    FixtureHandler.requests = 0
    FixtureHandler.conditional_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/targets.h"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def manager(tmp_path: Path) -> TargetSourceManager:
    return TargetSourceManager(
        settings_store=SettingsStore(tmp_path / "config"),
        cache_dir=tmp_path / "cache",
        timeout=2,
    )


def test_extracts_target_blocks():
    names = extract_target_names(targets_header().decode())
    assert names[0] == "TEST_TARGET_00"
    assert names[-1] == "TEST_TARGET_11"
    assert len(names) == 12


def test_validates_real_am32_header_when_checkout_is_available():
    header = Path(__file__).parents[2] / "AM32.renode" / "Inc" / "targets.h"
    if not header.exists():
        pytest.skip("sibling AM32 checkout is not available")
    digest, targets = validate_targets_header(header.read_bytes())
    assert len(digest) == 64
    assert "VIMDRONES_L431" in targets
    assert "TEKKO32_F415" in targets
    assert len(targets) > 200


@pytest.mark.parametrize(
    "content, message",
    [
        (b"", "empty"),
        (b"abc\x00def", "NUL"),
        (b"\xff" * 100, "UTF-8"),
        (b'#include "/etc/passwd"\n' + targets_header(), "forbidden #include"),
        (b"#ifdef A\n" + targets_header(), "unterminated"),
        (targets_header(count=2), "only 2 targets"),
    ],
)
def test_rejects_invalid_headers(content, message):
    with pytest.raises(TargetSourceError, match=message):
        validate_targets_header(content)


def test_rejects_oversize_header():
    with pytest.raises(TargetSourceError, match="exceeds"):
        validate_targets_header(b"x" * (MAX_TARGETS_BYTES + 1))


def test_local_source_is_snapshotted_and_persisted(tmp_path):
    source_file = tmp_path / "custom-targets.h"
    source_file.write_bytes(targets_header())
    subject = manager(tmp_path)
    source = TargetSourceSpec("file", str(source_file))

    document = subject.select(source)

    assert document.targets[0] == "TEST_TARGET_00"
    assert subject.settings_store.load().targets_source == source
    cached = subject.active()
    assert cached.from_cache
    assert cached.sha256 == document.sha256
    assert cached.content == source_file.read_bytes()


def test_invalid_selection_does_not_replace_settings(tmp_path):
    source_file = tmp_path / "bad.h"
    source_file.write_text("not targets.h")
    subject = manager(tmp_path)

    with pytest.raises(TargetSourceError):
        subject.select(TargetSourceSpec("file", str(source_file)))

    assert subject.settings_store.load().targets_source.location == DEFAULT_TARGETS_URL


def test_http_refresh_uses_etag_and_last_known_good(tmp_path):
    subject = manager(tmp_path)
    with fixture_server() as url:
        source = TargetSourceSpec("url", url)
        first = subject.select(source)
        second = subject.refresh(source)

    assert first.sha256 == second.sha256
    assert second.from_cache
    assert second.fetched_at == first.fetched_at
    assert second.last_checked_at > first.last_checked_at
    assert FixtureHandler.requests == 2
    assert FixtureHandler.conditional_requests == 1


def test_failed_refresh_keeps_previous_cache(tmp_path):
    subject = manager(tmp_path)
    with fixture_server() as url:
        source = TargetSourceSpec("url", url)
        first = subject.select(source)

    with pytest.raises(TargetSourceError, match="download failed"):
        subject.refresh(source)

    cached = subject.active()
    assert cached.sha256 == first.sha256
    assert cached.from_cache


def test_non_loopback_http_is_rejected(tmp_path):
    subject = manager(tmp_path)
    source = TargetSourceSpec("url", "http://example.com/targets.h")
    with pytest.raises(TargetSourceError, match="must use HTTPS"):
        subject.refresh(source)


def test_cache_content_is_verified(tmp_path):
    source_file = tmp_path / "targets.h"
    source_file.write_bytes(targets_header())
    subject = manager(tmp_path)
    document = subject.select(TargetSourceSpec("file", str(source_file)))
    object_path = subject.cache_dir / "objects" / document.sha256 / "targets.h"
    object_path.write_bytes(targets_header(suffix="\n/* corrupt */\n"))

    with pytest.raises(TargetSourceError, match="hash mismatch"):
        subject.active()


def test_cache_index_contains_source_and_metadata(tmp_path):
    source_file = tmp_path / "targets.h"
    source_file.write_bytes(targets_header())
    subject = manager(tmp_path)
    subject.select(TargetSourceSpec("file", str(source_file)))
    index = json.loads(subject.index_path.read_text())
    record = next(iter(index["sources"].values()))
    assert record["source"]["kind"] == "file"
    assert record["target_count"] == 12
    assert len(record["sha256"]) == 64
    assert record["last_checked_at"] == record["fetched_at"]

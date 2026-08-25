from __future__ import annotations

from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import stat
import threading

import pytest

from escsim.artifacts import catalog as catalog_module
from escsim.artifacts.catalog import (
    ArtifactRepository,
    CatalogError,
    validate_catalog,
    validate_manifest,
)
from escsim.artifacts.publisher import (
    add_bootloaders,
    add_firmware,
    augment_release,
    finish,
    release_has_formats,
    validate_repository,
)
from conftest import DEFAULT_TARGETS


@contextmanager
def static_server(directory: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def make_repository(tmp_path: Path) -> Path:
    targets = DEFAULT_TARGETS
    firmware = tmp_path / "firmware-build"
    bootloaders = tmp_path / "bootloader-build"
    firmware.mkdir()
    bootloaders.mkdir()
    (firmware / "AM32_VIMDRONES_L431_2.21.elf").write_bytes(b"firmware-elf")
    (firmware / "AM32_VIMDRONES_L431_2.21.hex").write_bytes(b"firmware-hex")
    (firmware / "AM32_VIMDRONES_L431_CAN_2.21.elf").write_bytes(b"can-firmware-elf")
    (firmware / "AM32_VIMDRONES_L431_CAN_2.21.hex").write_bytes(b"can-firmware-hex")
    (bootloaders / "AM32_L431_BOOTLOADER_PA2_V19.elf").write_bytes(b"bootloader-elf")
    (bootloaders / "AM32_L431_BOOTLOADER_PA2_V19.hex").write_bytes(b"bootloader-hex")
    (bootloaders / "AM32_L431_BOOTLOADER_PA2_CAN_V19.elf").write_bytes(
        b"can-bootloader-elf"
    )
    (bootloaders / "AM32_L431_BOOTLOADER_PA2_CAN_V19.hex").write_bytes(
        b"can-bootloader-hex"
    )
    (bootloaders / "AM32_SITL_BOOTLOADER_PB4_CAN_V19.elf").write_bytes(b"host-elf")
    (bootloaders / "AM32_SITL_BOOTLOADER_PB4_CAN_V19.hex").write_bytes(b"host-hex")
    root = tmp_path / "site" / "v1"
    catalog = add_firmware(root, "2.21", "abc1234", targets, firmware, "stable")
    catalog = add_bootloaders(root, "19", "def5678", bootloaders, "stable", catalog)
    finish(root, catalog)
    validate_repository(root)
    return root


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are unavailable")
def test_published_files_are_publicly_readable(tmp_path):
    root = make_repository(tmp_path)
    for path in root.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o644, path


def test_publish_fetch_install_and_offline_cache(tmp_path):
    root = make_repository(tmp_path)
    cache = tmp_path / "cache"
    with static_server(root) as url:
        repository = ArtifactRepository(url, cache)
        catalog = repository.catalog(refresh=True)
        assert catalog["channels"]["stable"] == {
            "firmware": "2.21",
            "bootloader": "19",
        }
        assert (
            "AM32_SITL_BOOTLOADER_PB4_CAN"
            not in catalog["releases"]["bootloader"][0]["targets"]
        )
        firmware = repository.install("firmware", "2.21", "VIMDRONES_L431")
        assert firmware.image.read_bytes() == b"firmware-elf"
        assert firmware.targets_header is not None
        firmware_hex = repository.install(
            "firmware", "2.21", "VIMDRONES_L431", image_format="hex"
        )
        assert firmware_hex.image.read_bytes() == b"firmware-hex"
        assert firmware_hex.companion_elf == firmware.image
        variant = repository.compatible_bootloader("19", firmware.metadata)
        assert variant["name"] == "AM32_L431_BOOTLOADER_PA2"
        bootloader = repository.install(
            "bootloader", "19", variant["name"], image_format="hex"
        )
        assert bootloader.image.read_bytes() == b"bootloader-hex"
        assert bootloader.companion_elf.read_bytes() == b"bootloader-elf"

    offline = ArtifactRepository(url, cache)
    assert offline.catalog()["channels"]["stable"]["firmware"] == "2.21"
    assert offline.install("firmware", "2.21", "VIMDRONES_L431").image.is_file()


def test_can_firmware_selects_can_bootloader(tmp_path):
    root = make_repository(tmp_path)
    with static_server(root) as url:
        repository = ArtifactRepository(url, tmp_path / "cache")
        firmware = repository.install("firmware", "2.21", "VIMDRONES_L431_CAN")
        variant = repository.compatible_bootloader("19", firmware.metadata)
        assert variant["dronecan"]
        assert "_CAN" in variant["name"]


def test_catalog_rejects_traversal_duplicate_and_bad_hash():
    raw = {
        "schema_version": 1,
        "releases": {
            "firmware": [
                {
                    "id": "2.21",
                    "manifest": "../secret",
                    "targets": ["A"],
                }
            ],
            "bootloader": [],
        },
    }
    with pytest.raises(CatalogError, match="unsafe manifest"):
        validate_catalog(raw)

    manifest = {
        "schema_version": 1,
        "project": "bootloader",
        "release": "19",
        "channel": "stable",
        "targets": [
            {
                "name": "A",
                "family": "l431",
                "pin": "PA2",
                "dronecan": False,
                "artifact": {"path": "a.elf", "size": 1, "sha256": "x" * 64},
            }
        ],
    }
    with pytest.raises(CatalogError, match="sha256"):
        validate_manifest(manifest, "bootloader", "19")


def test_corrupt_download_never_becomes_cached(tmp_path):
    root = make_repository(tmp_path)
    manifest_path = root / "firmware" / "2.21" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    artifact = root / manifest["targets"][0]["artifact"]["path"]
    artifact.write_bytes(b"corrupt")
    with static_server(root) as url:
        repository = ArtifactRepository(url, tmp_path / "cache")
        with pytest.raises(CatalogError, match="size or SHA-256"):
            repository.install("firmware", "2.21", manifest["targets"][0]["name"])
    assert not list((tmp_path / "cache" / "objects").rglob("*.elf"))


def test_manifest_rejects_nonmatching_image_pair(tmp_path):
    root = make_repository(tmp_path)
    manifest_path = root / "firmware" / "2.21" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["targets"][0]["images"]["hex"]["path"] = "firmware/wrong.hex"
    with pytest.raises(CatalogError, match="matching pair"):
        validate_manifest(manifest, "firmware", "2.21")


def test_legacy_release_can_be_safely_augmented(tmp_path):
    root = make_repository(tmp_path)
    manifest_path = root / "firmware" / "2.21" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for target in manifest["targets"]:
        target.pop("images")
        (root / target["artifact"]["path"]).with_suffix(".hex").unlink()
    manifest_path.write_text(json.dumps(manifest))
    assert not release_has_formats(root, "firmware", "2.21")

    augment_release(root, "firmware", "2.21", tmp_path / "firmware-build")
    assert release_has_formats(root, "firmware", "2.21")
    validate_repository(root)


def test_publish_requires_matching_hex(tmp_path):
    targets = DEFAULT_TARGETS
    firmware = tmp_path / "firmware-build"
    firmware.mkdir()
    (firmware / "AM32_VIMDRONES_L431_2.21.elf").write_bytes(b"firmware-elf")
    with pytest.raises(ValueError, match="missing matching Intel HEX"):
        add_firmware(tmp_path / "site", "2.21", "abc1234", targets, firmware, "stable")


def test_invalid_catalog_refresh_preserves_last_known_good(tmp_path):
    root = make_repository(tmp_path)
    cache = tmp_path / "cache"
    with static_server(root) as url:
        repository = ArtifactRepository(url, cache)
        good = repository.catalog(refresh=True)
        raw = json.loads((root / "catalog.json").read_text())
        raw["releases"]["firmware"][0]["manifest"] = "../escape.json"
        (root / "catalog.json").write_text(json.dumps(raw))
        fallback = repository.catalog(refresh=True)
    assert fallback == good
    assert json.loads((cache / "catalog.json").read_text()) == good


def test_off_origin_redirect_is_rejected(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        headers = {}

        def geturl(self):
            return "https://redirected.example/catalog.json"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        catalog_module, "urlopen", lambda *_args, **_kwargs: Response(b"{}")
    )
    repository = ArtifactRepository("https://origin.example/v1/", tmp_path / "cache")
    with pytest.raises(CatalogError, match="redirected outside"):
        repository.catalog(refresh=True)

from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import zipfile

import pytest

from escsim.renode import download


REVISION = "2a060779f4e2b87d1ae7238a041d858369818805"


def metadata(package, platform="linux", architecture="x86_64", runtime="linux-x64"):
    return {
        "schema_version": 1,
        "renode_version": "1.16.1",
        "source": {"revision": REVISION},
        "artifacts": [
            {
                "target": {
                    "platform": platform,
                    "architecture": architecture,
                    "runtime_identifier": runtime,
                },
                "packages": [package],
            }
        ],
    }


def portable_tar(executable=b"#!/bin/sh\nexit 0\n"):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        info = tarfile.TarInfo("renode-test/renode")
        info.mode = 0o755
        info.size = len(executable)
        bundle.addfile(info, io.BytesIO(executable))
    return archive.getvalue()


def portable_zip(executable=b"renode executable"):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("renode-test/renode.exe", executable)
    return archive.getvalue()


def test_selects_portable_package_for_each_planned_platform():
    linux = {"filename": "renode.linux.tar.gz", "sha256": "a" * 64, "size": 1}
    windows = {"filename": "renode.windows.zip", "sha256": "b" * 64, "size": 2}
    macos = {"filename": "renode.macos.tar.gz", "sha256": "c" * 64, "size": 3}
    assert (
        download.select_package(metadata(linux), "Linux", "AMD64")["platform"]
        == "linux"
    )
    assert (
        download.select_package(
            metadata(windows, "windows", "x86_64", "win-x64"),
            "Windows",
            "AMD64",
        )["filename"]
        == windows["filename"]
    )
    assert (
        download.select_package(
            metadata(macos, "macos", "arm64", "osx-arm64"), "Darwin", "arm64"
        )["platform"]
        == "macos"
    )


@pytest.mark.parametrize("runtime", ["../../outside", "bad/name", "", None])
def test_rejects_unsafe_runtime_identifier(runtime):
    package = {"filename": "renode.tar.gz", "sha256": "a" * 64, "size": 1}
    with pytest.raises(RuntimeError, match="runtime identifier"):
        download.select_package(metadata(package, runtime=runtime), "linux", "x86_64")


def test_download_cache_is_verified_reused_and_preserves_conflict(
    tmp_path, monkeypatch
):
    executable_data = b"#!/bin/sh\nexit 0\n"
    archive = portable_tar(executable_data)
    package = {
        "filename": "renode.linux.tar.gz",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size": len(archive),
    }
    latest = metadata(package)
    selected = download.select_package(latest, "linux", "x86_64")
    conflict = tmp_path / download.cache_key(latest, selected)
    conflict.mkdir()
    (conflict / "preserve").write_text("diagnostic")
    monkeypatch.setattr(download, "host_target", lambda *_args: ("linux", "x86_64"))

    def open_archive(_request, timeout):
        assert timeout == 60
        return io.BytesIO(archive)

    executable, _latest, installed = download.install_current(
        tmp_path, latest=latest, opener=open_archive
    )
    assert installed
    assert executable.read_bytes() == executable_data
    assert executable.parents[1].name.startswith(conflict.name + "-")
    assert (conflict / "preserve").read_text() == "diagnostic"

    def no_download(_request, _timeout):
        raise AssertionError("verified current cache should be reused")

    same, _latest, installed = download.install_current(
        tmp_path, latest=latest, opener=no_download
    )
    assert not installed
    assert same == executable


def test_bad_digest_never_selects_install(tmp_path, monkeypatch):
    archive = portable_zip()
    package = {
        "filename": "renode.windows.zip",
        "sha256": "0" * 64,
        "size": len(archive),
    }
    latest = metadata(package, "windows", "x86_64", "win-x64")
    monkeypatch.setattr(download, "host_target", lambda *_args: ("windows", "x86_64"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        download.install_current(
            tmp_path,
            latest=latest,
            opener=lambda *_args, **_kwargs: io.BytesIO(archive),
        )
    assert download.cached(tmp_path) is None


def test_tar_path_traversal_and_links_are_rejected(tmp_path):
    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as bundle:
        info = tarfile.TarInfo("../outside")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    package = {"filename": "renode.tar.gz", "platform": "linux"}
    with pytest.raises(RuntimeError, match="unsafe path"):
        download.extract(traversal, tmp_path / "traversal", package)

    linked = tmp_path / "linked.tar.gz"
    with tarfile.open(linked, "w:gz") as bundle:
        info = tarfile.TarInfo("renode/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/tmp/outside"
        bundle.addfile(info)
    with pytest.raises(RuntimeError, match="unsafe link"):
        download.extract(linked, tmp_path / "linked", package)

    escaping = tmp_path / "escaping.tar.gz"
    with tarfile.open(escaping, "w:gz") as bundle:
        info = tarfile.TarInfo("renode/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        bundle.addfile(info)
    with pytest.raises(RuntimeError, match="unsafe link"):
        download.extract(escaping, tmp_path / "escaping", package)

    hard = tmp_path / "hard.tar.gz"
    with tarfile.open(hard, "w:gz") as bundle:
        info = tarfile.TarInfo("renode/link")
        info.type = tarfile.LNKTYPE
        info.linkname = "renode/renode"
        bundle.addfile(info)
    with pytest.raises(RuntimeError, match="links and special"):
        download.extract(hard, tmp_path / "hard", package)


def test_tar_internal_symlink_is_extracted(tmp_path):
    archive = tmp_path / "internal.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("renode/renode")
        data = b"#!/bin/sh\n"
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo("renode/plugins/lib/socket-cpp")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../renode"
        bundle.addfile(info)
    package = {"filename": "renode.tar.gz", "platform": "linux"}
    destination = tmp_path / "internal"
    executable = download.extract(archive, destination, package)
    assert executable == destination / "renode" / "renode"
    link = destination / "renode" / "plugins" / "lib" / "socket-cpp"
    assert link.is_symlink()
    assert link.resolve() == executable.resolve()


def test_zip_path_traversal_and_links_are_rejected(tmp_path):
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as bundle:
        bundle.writestr("../renode.exe", b"bad")
    package = {"filename": "renode.zip", "platform": "windows"}
    with pytest.raises(RuntimeError, match="unsafe path"):
        download.extract(traversal, tmp_path / "traversal", package)

    linked = tmp_path / "linked.zip"
    with zipfile.ZipFile(linked, "w") as bundle:
        info = zipfile.ZipInfo("renode/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, "/tmp/outside")
    with pytest.raises(RuntimeError, match="links are not allowed"):
        download.extract(linked, tmp_path / "linked", package)


def test_malformed_selection_is_ignored(tmp_path):
    (tmp_path / download.SELECTION).write_text("[]\n")
    assert download.cached(tmp_path) is None

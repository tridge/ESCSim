#!/usr/bin/env python3
"""Download and cache ArduPilot's patched portable Renode build.

The ArduPilot build contains the Renode changes used by both the ArduPilot
and AM32 emulations.  latest.json is authoritative for the host package,
size and SHA-256 digest; an installation is selected only after the complete
archive has been verified and extracted.
"""

import argparse
import errno
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile


DOWNLOAD_BASE = "https://firmware.ardupilot.org/Tools/Renode/"
LATEST_URL = urllib.parse.urljoin(DOWNLOAD_BASE, "latest.json")
SELECTION = "selected.json"
MAX_METADATA_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100000
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024


def default_cache():
    """The same default used by the ArduPilot Renode launcher.

    Keeping the cache name identical lets both checkouts reuse the 80-120 MB
    portable package instead of downloading and unpacking it twice.
    """
    root = os.environ.get("XDG_CACHE_HOME")
    if root:
        return Path(root).expanduser() / "ardupilot" / "renode"
    return Path.home() / ".cache" / "ardupilot" / "renode"


def host_target(system=None, machine=None):
    """Return the platform and architecture names used by latest.json."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    platforms = {"linux": "linux", "darwin": "macos", "windows": "windows"}
    architectures = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "aarch64" if system == "linux" else "arm64",
        "arm64": "aarch64" if system == "linux" else "arm64",
    }
    if system not in platforms or machine not in architectures:
        raise RuntimeError("no ArduPilot Renode download for %s/%s" % (system, machine))
    return platforms[system], architectures[machine]


def select_package(latest, system=None, machine=None):
    """Select the one portable package for this host."""
    wanted_platform, wanted_architecture = host_target(system, machine)
    for artifact in latest.get("artifacts", []):
        target = artifact.get("target", {})
        if (
            target.get("platform") != wanted_platform
            or target.get("architecture") != wanted_architecture
        ):
            continue
        packages = artifact.get("packages", [])
        if wanted_platform == "windows":
            packages = [
                package
                for package in packages
                if package.get("filename", "").endswith(".zip")
            ]
        elif wanted_platform in ("linux", "macos"):
            packages = [
                package
                for package in packages
                if package.get("filename", "").endswith(".tar.gz")
            ]
        if len(packages) != 1:
            raise RuntimeError(
                "latest.json has no unique portable package for "
                "%s/%s" % (wanted_platform, wanted_architecture)
            )
        package = dict(packages[0])
        filename = package.get("filename")
        digest = package.get("sha256")
        size = package.get("size")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", filename)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise RuntimeError("latest.json has invalid portable package metadata")
        runtime_identifier = target.get("runtime_identifier")
        if not isinstance(runtime_identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", runtime_identifier
        ):
            raise RuntimeError("latest.json has no valid runtime identifier")
        package["runtime_identifier"] = runtime_identifier
        package["platform"] = wanted_platform
        package["architecture"] = wanted_architecture
        return package
    raise RuntimeError(
        "latest.json has no package for %s/%s" % (wanted_platform, wanted_architecture)
    )


def fetch_latest(opener=None):
    """Fetch uncached current-version metadata."""
    opener = opener or urllib.request.urlopen
    separator = "&" if "?" in LATEST_URL else "?"
    url = "%s%st=%u" % (LATEST_URL, separator, time.time_ns())
    request = urllib.request.Request(
        url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
    )
    with opener(request, timeout=30) as response:
        data = response.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise RuntimeError("Renode latest.json exceeds the size limit")
    latest = json.loads(data.decode("utf-8"))
    if latest.get("schema_version") != 1:
        raise RuntimeError("unsupported Renode latest.json schema")
    revision = latest.get("source", {}).get("revision")
    if not revision or not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
        raise RuntimeError("latest.json has no valid source revision")
    return latest


def cache_key(latest, package):
    revision = latest["source"]["revision"]
    return "%s-%s-%s" % (
        package["runtime_identifier"],
        revision[:12],
        package["sha256"][:12],
    )


def verified_install(cache, install_name, executable_name, expected=None):
    """Return one valid contained cache executable, or None."""
    try:
        cache = Path(cache).expanduser().resolve()
        if not isinstance(install_name, str) or not isinstance(executable_name, str):
            return None
        install = (cache / install_name).resolve()
        executable = (install / executable_name).resolve()
        if (
            not install.is_relative_to(cache.resolve())
            or not executable.is_relative_to(install)
            or not executable.is_file()
        ):
            return None
        manifest = json.loads((install / "ardupilot-renode.json").read_text())
        if (
            not isinstance(manifest, dict)
            or manifest.get("executable") != executable_name
        ):
            return None
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return None
    if expected is not None:
        if any(manifest.get(key) != value for key, value in expected.items()):
            return None
    return executable


def cached(cache, latest=None, package=None):
    """Return a verified cache executable, optionally requiring latest."""
    cache = Path(cache).expanduser()
    try:
        selection = json.loads((cache / SELECTION).read_text())
        if not isinstance(selection, dict):
            return None
        install_name = selection.get("install")
        executable_name = selection.get("executable")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    expected = None
    if latest is not None and package is not None:
        expected = {
            "revision": latest["source"]["revision"],
            "filename": package["filename"],
            "sha256": package["sha256"],
            "runtime_identifier": package["runtime_identifier"],
        }
    return verified_install(cache, install_name, executable_name, expected)


def download_file(url, destination, size, sha256, progress=None, opener=None):
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    digest = hashlib.sha256()
    received = 0
    with opener(request, timeout=60) as response, destination.open("wb") as out:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            out.write(block)
            digest.update(block)
            received += len(block)
            if received > size:
                raise RuntimeError("Renode download exceeds its declared size")
            if progress:
                progress(received, size)
    if received != size:
        raise RuntimeError(
            "Renode download is %u bytes; expected %u" % (received, size)
        )
    if digest.hexdigest().lower() != sha256.lower():
        raise RuntimeError("Renode download SHA-256 does not match latest.json")


def _safe_member_name(name):
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError("unsafe path in Renode package: %s" % name)


def _check_expanded_size(sizes):
    sizes = list(sizes)
    if len(sizes) > MAX_ARCHIVE_MEMBERS or sum(sizes) > MAX_EXTRACTED_BYTES:
        raise RuntimeError("Renode package exceeds extraction limits")


def extract(archive, destination, package):
    destination.mkdir()
    if package["filename"].endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            _check_expanded_size(member.size for member in members)
            for member in members:
                _safe_member_name(member.name)
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError(
                        "links and special files are not allowed " "in Renode packages"
                    )
            bundle.extractall(destination, members=members)
        executable_name = "renode"
    elif package["filename"].endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            _check_expanded_size(member.file_size for member in members)
            for member in members:
                _safe_member_name(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise RuntimeError("links are not allowed in Renode packages")
            bundle.extractall(destination)
        executable_name = "renode.exe"
    else:
        raise RuntimeError("unsupported Renode package %s" % package["filename"])
    candidates = [path for path in destination.rglob(executable_name) if path.is_file()]
    if len(candidates) != 1:
        raise RuntimeError(
            "downloaded package has %u %s executables"
            % (len(candidates), executable_name)
        )
    executable = candidates[0]
    if package["platform"] != "windows":
        executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def install_current(cache=None, latest=None, progress=None, opener=None):
    """Ensure the cache holds the freshly queried current Renode package."""
    cache = Path(cache or default_cache()).expanduser().resolve()
    latest = latest or fetch_latest(opener)
    package = select_package(latest)
    executable = cached(cache, latest, package)
    if executable is not None:
        return executable, latest, False

    cache.mkdir(parents=True, exist_ok=True)
    install_name = cache_key(latest, package)
    install = cache / install_name
    with tempfile.TemporaryDirectory(prefix=".download-", dir=cache) as temp:
        temporary = Path(temp)
        archive = temporary / package["filename"]
        filename = package["filename"]
        if Path(filename).name != filename:
            raise RuntimeError("invalid Renode package filename")
        url = urllib.parse.urljoin(DOWNLOAD_BASE, urllib.parse.quote(filename))
        download_file(
            url, archive, package["size"], package["sha256"], progress, opener
        )
        payload = temporary / "payload"
        executable = extract(archive, payload, package)
        manifest = {
            "revision": latest["source"]["revision"],
            "renode_version": latest.get("renode_version"),
            "filename": filename,
            "sha256": package["sha256"],
            "runtime_identifier": package["runtime_identifier"],
            "executable": str(executable.relative_to(payload)),
        }
        (payload / "ardupilot-renode.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        expected = {
            key: manifest[key]
            for key in ("revision", "filename", "sha256", "runtime_identifier")
        }
        while True:
            try:
                payload.rename(install)
                break
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                existing = verified_install(
                    cache, install_name, manifest["executable"], expected
                )
                if existing is not None:
                    break
                install_name = "%s-%u" % (cache_key(latest, package), time.time_ns())
                install = cache / install_name

    selection = {"install": install_name, "executable": manifest["executable"]}
    fd, selection_name = tempfile.mkstemp(prefix=".selected-", dir=cache)
    selection_tmp = Path(selection_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(selection, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        selection_tmp.replace(cache / SELECTION)
    except BaseException:
        selection_tmp.unlink(missing_ok=True)
        raise
    return install / manifest["executable"], latest, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default=None)
    parser.add_argument(
        "--cached",
        action="store_true",
        help="print the selected cached executable, if any",
    )
    args = parser.parse_args()
    cache = Path(args.cache).expanduser() if args.cache else default_cache()
    if args.cached:
        executable = cached(cache)
        if executable is None:
            return 1
        print(executable)
        return 0

    def progress(received, size):
        print(
            "\rDownloading Renode: %u%%" % (received * 100 // size), end="", flush=True
        )

    executable, latest, downloaded = install_current(cache, progress=progress)
    if downloaded:
        print()
    print(
        "%s Renode %s (%s)"
        % ("Installed" if downloaded else "Using", latest["renode_version"], executable)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

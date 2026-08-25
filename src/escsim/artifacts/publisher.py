"""Build and validate the static, multi-version ESCSim artifact repository.

The CLI serializes updates with a repository lock. Callers using the lower-level
``add_*``/``finish`` API must serialize the whole read-modify-write transaction.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from elftools.elf.elffile import ELFFile

try:
    import fcntl
except ImportError:  # the publisher is deployed on Linux; imports stay portable
    fcntl = None

from escsim.artifacts.catalog import validate_catalog, validate_manifest
from escsim.renode.generator import Unsupported, config
from escsim.target.source import validate_targets_header


_FIRMWARE = re.compile(r"AM32_(?P<target>[A-Za-z0-9_]+)_(?P<version>[0-9.]+)\.elf\Z")
_BOOTLOADER = re.compile(
    r"AM32_(?P<family>[A-Z0-9]+)_BOOTLOADER_(?P<pin>P[A-Z][0-9]+)"
    r"(?P<variant>(?:_[A-Z0-9]+)*)_V(?P<version>[0-9]+)\.elf\Z"
)


@contextmanager
def repository_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".publisher.lock").open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_write(path: Path, value: object) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
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
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _copy(source: Path, root: Path, relative: str) -> dict:
    destination = root / relative
    if destination.exists():
        raise ValueError(f"immutable artifact already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    shutil.copyfile(source, temporary)
    temporary.chmod(0o644)
    os.replace(temporary, destination)
    content = destination.read_bytes()
    return {
        "path": relative,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _matching_images(source: Path, root: Path, relative: str) -> dict[str, dict]:
    """Publish the matching ELF and Intel HEX produced by one build."""
    if source.suffix.lower() != ".elf":
        raise ValueError(f"primary build image is not an ELF: {source}")
    hex_source = source.with_suffix(".hex")
    if not hex_source.is_file():
        raise ValueError(f"missing matching Intel HEX image: {hex_source}")
    parent = Path(relative).parent.as_posix()
    return {
        "elf": _copy(source, root, relative),
        "hex": _copy(hex_source, root, f"{parent}/{hex_source.name}"),
    }


def _catalog(root: Path) -> dict:
    path = root / "catalog.json"
    if not path.exists():
        return {
            "schema_version": 1,
            "generated_at": _now(),
            "channels": {},
            "releases": {"firmware": [], "bootloader": []},
        }
    return validate_catalog(json.loads(path.read_text("utf-8")))


def _release_exists(catalog: dict, project: str, release: str) -> bool:
    return any(entry["id"] == release for entry in catalog["releases"][project])


def add_firmware(
    root: Path,
    release: str,
    revision: str,
    targets_header: Path,
    images: Path,
    channel: str,
) -> dict:
    catalog = _catalog(root)
    if _release_exists(catalog, "firmware", release):
        raise ValueError(f"firmware release {release} is already published")
    content = targets_header.read_bytes()
    _digest, names = validate_targets_header(content)
    text = content.decode("utf-8")
    targets = []
    for source in sorted(images.glob("*.elf")):
        match = _FIRMWARE.fullmatch(source.name)
        if not match or match.group("version") != release:
            continue
        target = match.group("target")
        if target not in names:
            continue
        try:
            cfg = config(target, targets_text=text)
        except Unsupported:
            continue
        relative = f"firmware/{release}/targets/{target}/{source.name}"
        image_specs = _matching_images(source, root, relative)
        targets.append(
            {
                "name": target,
                "family": cfg["family"],
                "pin": cfg["throttle_pin"],
                "dronecan": bool(cfg["dronecan"]),
                "app_base": cfg["app_base"],
                # artifact remains the backward-compatible/default ELF.
                "artifact": image_specs["elf"],
                "images": image_specs,
            }
        )
    if not targets:
        raise ValueError(f"no supported firmware {release} ELFs in {images}")
    header_spec = _copy(targets_header, root, f"firmware/{release}/targets.h")
    manifest = {
        "schema_version": 1,
        "project": "firmware",
        "release": release,
        "revision": revision,
        "channel": channel,
        "built_at": _now(),
        "targets_header": header_spec,
        "targets": targets,
    }
    manifest_path = f"firmware/{release}/manifest.json"
    _json_write(root / manifest_path, manifest)
    catalog["releases"]["firmware"].append(
        {
            "id": release,
            "channel": channel,
            "revision": revision,
            "manifest": manifest_path,
            "targets": [item["name"] for item in targets],
        }
    )
    if channel == "stable":
        catalog.setdefault("channels", {}).setdefault("stable", {})["firmware"] = (
            release
        )
    return catalog


def add_bootloaders(
    root: Path,
    release: str,
    revision: str,
    images: Path,
    channel: str,
    catalog: dict | None = None,
) -> dict:
    catalog = catalog or _catalog(root)
    if _release_exists(catalog, "bootloader", release):
        raise ValueError(f"bootloader release {release} is already published")
    targets = []
    for source in sorted(images.glob("*.elf")):
        match = _BOOTLOADER.fullmatch(source.name)
        if not match or match.group("version") != release:
            continue
        # This is a native host test executable, not a Renode-loadable MCU
        # image or an end-user bootloader.
        if match.group("family") == "SITL":
            continue
        suffix = match.group("variant")
        tokens = {token for token in suffix.split("_") if token}
        name = source.stem.rsplit("_V", 1)[0]
        relative = f"bootloader/{release}/targets/{name}/{source.name}"
        image_specs = _matching_images(source, root, relative)
        targets.append(
            {
                "name": name,
                "family": match.group("family").lower(),
                "pin": match.group("pin"),
                "dronecan": "CAN" in tokens,
                "sized": any(
                    token.endswith("K") and token[:-1].isdigit() for token in tokens
                ),
                "artifact": image_specs["elf"],
                "images": image_specs,
            }
        )
    if not targets:
        raise ValueError(f"no bootloader V{release} ELFs in {images}")
    manifest = {
        "schema_version": 1,
        "project": "bootloader",
        "release": release,
        "revision": revision,
        "channel": channel,
        "built_at": _now(),
        "targets": targets,
    }
    manifest_path = f"bootloader/{release}/manifest.json"
    _json_write(root / manifest_path, manifest)
    catalog["releases"]["bootloader"].append(
        {
            "id": release,
            "channel": channel,
            "revision": revision,
            "manifest": manifest_path,
            "targets": [item["name"] for item in targets],
        }
    )
    if channel == "stable":
        catalog.setdefault("channels", {}).setdefault("stable", {})["bootloader"] = (
            release
        )
    return catalog


def release_has_formats(root: Path, project: str, release: str) -> bool:
    """Whether every target in an existing release offers ELF and HEX."""
    catalog = _catalog(root)
    entry = next(
        (item for item in catalog["releases"][project] if item["id"] == release),
        None,
    )
    if entry is None:
        return False
    manifest = validate_manifest(
        json.loads((root / entry["manifest"]).read_text("utf-8")), project, release
    )
    return all(set(target.get("images", {})) >= {"elf", "hex"} for target in manifest["targets"])


def _verify_source(source: Path, spec: dict) -> None:
    if not source.is_file():
        raise ValueError(f"missing build image needed to augment release: {source}")
    content = source.read_bytes()
    if len(content) != spec["size"] or hashlib.sha256(content).hexdigest() != spec["sha256"]:
        raise ValueError(f"build image does not match published release: {source}")


def _elf_load_fingerprint(path: Path) -> tuple:
    """Executable ELF identity, excluding path-dependent debug sections."""
    try:
        with path.open("rb") as stream:
            image = ELFFile(stream)
            segments = tuple(
                (
                    int(segment.header.p_vaddr),
                    int(segment.header.p_paddr),
                    int(segment.header.p_filesz),
                    int(segment.header.p_memsz),
                    int(segment.header.p_flags),
                    segment.data(),
                )
                for segment in image.iter_segments()
                if segment.header.p_type == "PT_LOAD"
            )
            return image.header.e_machine, int(image.header.e_entry), segments
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot inspect ELF image {path}: {error}") from error


def _verify_elf_source(source: Path, published: Path, spec: dict) -> None:
    """Accept an exact ELF or one differing only in non-loadable metadata."""
    if not source.is_file():
        raise ValueError(f"missing build image needed to augment release: {source}")
    content = source.read_bytes()
    if (
        len(content) == spec["size"]
        and hashlib.sha256(content).hexdigest() == spec["sha256"]
    ):
        return
    _verify_source(published, spec)
    if _elf_load_fingerprint(source) != _elf_load_fingerprint(published):
        raise ValueError(
            f"build ELF load image does not match published release: {source}"
        )


def augment_release(root: Path, project: str, release: str, images: Path) -> dict:
    """Add paired HEX files to a legacy ELF-only manifest.

    The built ELF must have the same entry point and loadable segments as the
    already-published artifact. Path-dependent debug metadata may differ.
    """
    catalog = _catalog(root)
    entry = next(
        (item for item in catalog["releases"][project] if item["id"] == release),
        None,
    )
    if entry is None:
        raise ValueError(f"{project} release {release} is not published")
    manifest_path = root / entry["manifest"]
    manifest = validate_manifest(
        json.loads(manifest_path.read_text("utf-8")), project, release
    )
    if project == "bootloader":
        manifest["targets"] = [
            target
            for target in manifest["targets"]
            if target["family"].lower() != "sitl"
        ]
        entry["targets"] = [target["name"] for target in manifest["targets"]]
    for target in manifest["targets"]:
        elf_spec = target.get("images", {}).get("elf", target["artifact"])
        elf_name = PurePosixPath(elf_spec["path"]).name
        elf_source = images / elf_name
        _verify_elf_source(elf_source, root / elf_spec["path"], elf_spec)
        hex_source = elf_source.with_suffix(".hex")
        if not hex_source.is_file():
            raise ValueError(f"missing matching Intel HEX image: {hex_source}")
        relative = str(PurePosixPath(elf_spec["path"]).with_suffix(".hex"))
        destination = root / relative
        if destination.exists():
            content = destination.read_bytes()
            hex_spec = {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            _verify_source(hex_source, hex_spec)
        else:
            hex_spec = _copy(hex_source, root, relative)
        target["artifact"] = elf_spec
        target["images"] = {"elf": elf_spec, "hex": hex_spec}
    _json_write(manifest_path, manifest)
    return catalog


def finish(root: Path, catalog: dict) -> None:
    for project in ("firmware", "bootloader"):
        catalog["releases"][project].sort(key=lambda entry: entry["id"], reverse=True)
    catalog["generated_at"] = _now()
    validate_catalog(catalog)
    _json_write(root / "catalog.json", catalog)
    rows = []
    for project in ("firmware", "bootloader"):
        for release in catalog["releases"][project]:
            rows.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%u targets</td></tr>"
                % (
                    html.escape(project),
                    html.escape(release["id"]),
                    html.escape(release["channel"]),
                    len(release["targets"]),
                )
            )
    index = (
        "<!doctype html><meta charset=utf-8><title>ESCSim downloads</title>"
        "<h1>ESCSim downloads</h1><p><a href=catalog.json>catalog.json</a></p>"
        "<table><tr><th>Project</th><th>Release</th><th>Channel</th><th>Contents</th></tr>"
        + "".join(rows)
        + "</table>\n"
    )
    index_path = root / "index.html"
    index_path.write_text(index, encoding="utf-8")
    index_path.chmod(0o644)


def validate_repository(root: Path) -> None:
    catalog = validate_catalog(json.loads((root / "catalog.json").read_text("utf-8")))
    for project in ("firmware", "bootloader"):
        for release in catalog["releases"][project]:
            manifest = validate_manifest(
                json.loads((root / release["manifest"]).read_text("utf-8")),
                project,
                release["id"],
            )
            specs_by_path = {}
            for target in manifest["targets"]:
                for spec in target.get("images", {"elf": target["artifact"]}).values():
                    specs_by_path[spec["path"]] = spec
            if project == "firmware":
                spec = manifest["targets_header"]
                specs_by_path[spec["path"]] = spec
            for spec in specs_by_path.values():
                path = root / spec["path"]
                if not path.is_file() or path.stat().st_size != spec["size"]:
                    raise ValueError(f"missing or wrong-size artifact {path}")
                if hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
                    raise ValueError(f"hash mismatch for {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="add one firmware/bootloader release")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument(
        "--channel", choices=("stable", "development", "nightly"), default="development"
    )
    build.add_argument("--firmware-release")
    build.add_argument("--firmware-revision", default="unknown")
    build.add_argument("--firmware-dir", type=Path)
    build.add_argument("--targets", type=Path)
    build.add_argument("--bootloader-release")
    build.add_argument("--bootloader-revision", default="unknown")
    build.add_argument("--bootloader-dir", type=Path)
    check = sub.add_parser("validate")
    check.add_argument("root", type=Path)
    contains = sub.add_parser("contains")
    contains.add_argument("root", type=Path)
    contains.add_argument("project", choices=("firmware", "bootloader"))
    contains.add_argument("release")
    complete = sub.add_parser("complete")
    complete.add_argument("root", type=Path)
    complete.add_argument("project", choices=("firmware", "bootloader"))
    complete.add_argument("release")
    augment = sub.add_parser("augment")
    augment.add_argument("--root", type=Path, required=True)
    augment.add_argument("--project", choices=("firmware", "bootloader"), required=True)
    augment.add_argument("--release", required=True)
    augment.add_argument("--images", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        validate_repository(args.root)
        return 0
    if args.command == "contains":
        return (
            0 if _release_exists(_catalog(args.root), args.project, args.release) else 1
        )
    if args.command == "complete":
        return 0 if release_has_formats(args.root, args.project, args.release) else 1
    if args.command == "augment":
        with repository_lock(args.root):
            catalog = augment_release(
                args.root, args.project, args.release, args.images
            )
            finish(args.root, catalog)
            validate_repository(args.root)
        return 0
    with repository_lock(args.root):
        catalog = _catalog(args.root)
        if args.firmware_release:
            if not args.firmware_dir or not args.targets:
                parser.error("firmware release needs --firmware-dir and --targets")
            catalog = add_firmware(
                args.root,
                args.firmware_release,
                args.firmware_revision,
                args.targets,
                args.firmware_dir,
                args.channel,
            )
        if args.bootloader_release:
            if not args.bootloader_dir:
                parser.error("bootloader release needs --bootloader-dir")
            catalog = add_bootloaders(
                args.root,
                args.bootloader_release,
                args.bootloader_revision,
                args.bootloader_dir,
                args.channel,
                catalog,
            )
        if not args.firmware_release and not args.bootloader_release:
            parser.error("select at least one release")
        finish(args.root, catalog)
        validate_repository(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

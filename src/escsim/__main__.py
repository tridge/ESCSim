"""ESCSim command-line entry point."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

from escsim.artifacts.catalog import CatalogError
from escsim.settings import DEFAULT_TARGETS_URL, SettingsStore, TargetSourceSpec
from escsim.target.source import TargetSourceError, TargetSourceManager


def _targets_command(args: argparse.Namespace) -> int:
    manager = TargetSourceManager()
    if args.targets_action == "set-url":
        document = manager.select(TargetSourceSpec("url", args.url))
    elif args.targets_action == "set-file":
        document = manager.select(
            TargetSourceSpec("file", str(Path(args.path).resolve()))
        )
    elif args.targets_action == "use-default":
        document = manager.select(TargetSourceSpec("url", DEFAULT_TARGETS_URL))
    elif args.targets_action == "refresh":
        document = manager.active(refresh=True)
    else:
        document = manager.active()

    if args.targets_action == "list":
        print("\n".join(document.targets))
    else:
        freshness = "cached" if document.from_cache else "refreshed"
        print(f"source: {document.source.kind}:{document.source.location}")
        print(f"sha256: {document.sha256}")
        print(f"targets: {len(document.targets)}")
        print(f"fetched: {document.fetched_at}")
        print(f"checked: {document.last_checked_at} ({freshness})")
    return 0


def _generate_command(args: argparse.Namespace) -> int:
    from escsim.renode.generator import Unsupported, generate

    try:
        resc, repl = generate(
            args.target,
            str(args.outdir),
            sigrok=args.sigrok,
            bootloader_elf=str(args.bootloader) if args.bootloader else None,
            no_firmware=args.no_firmware,
        )
    except Unsupported as error:
        print(f"unsupported target: {error}", file=sys.stderr)
        return 77
    print(repl)
    print(resc)
    return 0


def _renode_command(args: argparse.Namespace) -> int:
    from escsim.renode import download

    if args.renode_action == "status":
        executable = download.cached(args.cache or download.default_cache())
        if executable is None:
            print("Renode is not cached")
            return 1
        print(executable)
        return 0

    def progress(received: int, size: int) -> None:
        print(f"\rDownloading Renode: {received * 100 // size}%", end="", flush=True)

    executable, metadata, installed = download.install_current(
        args.cache, progress=progress
    )
    if installed:
        print()
    print(f"Renode {metadata.get('renode_version')} at {executable}")
    return 0


def _artifacts_command(args: argparse.Namespace) -> int:
    from escsim.artifacts.catalog import ArtifactRepository

    if args.artifact_action == "set-base":
        ArtifactRepository(args.url, args.cache)
        store = SettingsStore()
        store.save(replace(store.load(), artifact_base_url=args.url.rstrip("/") + "/"))
        print(store.load().artifact_base_url)
        return 0
    repository = ArtifactRepository(args.base_url, args.cache)
    if args.artifact_action in {"list", "refresh"}:
        catalog = repository.catalog(refresh=args.artifact_action == "refresh")
        for project in ("firmware", "bootloader"):
            for release in catalog["releases"][project]:
                print(
                    f"{project} {release['id']} {release['channel']} "
                    f"({len(release['targets'])} targets)"
                )
        return 0
    installed = repository.install(args.project, args.release, args.target)
    print(installed.image)
    if installed.targets_header:
        print(installed.targets_header)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="escsim")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("gui", help="open the Target and Control application")
    targets = subparsers.add_parser("targets", help="manage the targets.h source")
    actions = targets.add_subparsers(dest="targets_action", required=True)
    for name in ("status", "refresh", "list", "use-default"):
        actions.add_parser(name)
    set_url = actions.add_parser("set-url")
    set_url.add_argument("url")
    set_file = actions.add_parser("set-file")
    set_file.add_argument("path")
    targets.set_defaults(handler=_targets_command)

    generate = subparsers.add_parser("generate", help="generate a Renode target")
    generate.add_argument("target")
    generate.add_argument("--outdir", type=Path, required=True)
    generate.add_argument("--sigrok", action="store_true")
    generate.add_argument("--bootloader", type=Path)
    generate.add_argument("--no-firmware", action="store_true")
    generate.set_defaults(handler=_generate_command)

    renode = subparsers.add_parser("renode", help="manage the verified Renode runtime")
    renode_actions = renode.add_subparsers(dest="renode_action", required=True)
    for name in ("status", "install"):
        action = renode_actions.add_parser(name)
        action.add_argument("--cache", type=Path, default=None)
    renode.set_defaults(handler=_renode_command)

    artifacts = subparsers.add_parser(
        "artifacts", help="manage versioned firmware and bootloader downloads"
    )
    artifact_actions = artifacts.add_subparsers(dest="artifact_action", required=True)
    for name in ("list", "refresh"):
        action = artifact_actions.add_parser(name)
        action.add_argument("--base-url")
        action.add_argument("--cache", type=Path)
    set_base = artifact_actions.add_parser("set-base")
    set_base.add_argument("url")
    set_base.add_argument("--cache", type=Path)
    install = artifact_actions.add_parser("install")
    install.add_argument("project", choices=("firmware", "bootloader"))
    install.add_argument("release")
    install.add_argument("target")
    install.add_argument("--base-url")
    install.add_argument("--cache", type=Path)
    artifacts.set_defaults(handler=_artifacts_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
    effective_argv = sys.argv[1:] if argv is None else argv
    if not effective_argv:
        effective_argv = ["gui"]
    if effective_argv[:1] == ["--internal-generator"]:
        from escsim.renode.generator import main as generator_main

        return generator_main(effective_argv[1:])
    if effective_argv[:1] == ["gui"]:
        from escsim.gui import main as gui_main

        return gui_main(effective_argv[1:])
    parser = build_parser()
    args = parser.parse_args(effective_argv)
    try:
        return args.handler(args)
    except (CatalogError, TargetSourceError, ValueError) as error:
        parser.error(str(error))
        return 2


if __name__ == "__main__":
    sys.exit(main())

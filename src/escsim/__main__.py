"""ESCSim command-line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from escsim.settings import DEFAULT_TARGETS_URL, TargetSourceSpec
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="escsim")
    subparsers = parser.add_subparsers(dest="command", required=True)
    targets = subparsers.add_parser("targets", help="manage the targets.h source")
    actions = targets.add_subparsers(dest="targets_action", required=True)
    for name in ("status", "refresh", "list", "use-default"):
        actions.add_parser(name)
    set_url = actions.add_parser("set-url")
    set_url.add_argument("url")
    set_file = actions.add_parser("set-file")
    set_file.add_argument("path")
    targets.set_defaults(handler=_targets_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TargetSourceError, ValueError) as error:
        parser.error(str(error))
        return 2


if __name__ == "__main__":
    sys.exit(main())

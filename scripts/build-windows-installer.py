#!/usr/bin/env python3
"""Fetch the pinned usbip-win2 prerequisite and build the Windows installer."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
USBIP_NAME = "USBip-0.9.7.7-x64.exe"
USBIP_URL = (
    "https://github.com/vadimgrn/usbip-win2/releases/download/"
    "v.0.9.7.7/USBip-0.9.7.7-x64.exe"
)
USBIP_SIZE = 33_226_344
USBIP_SHA256 = "51620fa5f9f8be5932bc9d786deee557ce06d5407a99cab490dcfac71f185fea"


def verified(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != USBIP_SIZE:
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest == USBIP_SHA256


def fetch_usbip(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / USBIP_NAME
    if verified(output):
        print(f"using verified {output}")
        return output
    partial = output.with_suffix(output.suffix + ".part")
    partial.unlink(missing_ok=True)
    print(f"downloading {USBIP_URL}", flush=True)
    try:
        with urllib.request.urlopen(USBIP_URL, timeout=120) as response:
            with partial.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if not verified(partial):
            raise RuntimeError("usbip-win2 download failed size/SHA-256 verification")
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    return output


def find_iscc(explicit: str | None) -> Path:
    candidates = [explicit, os.environ.get("ISCC"), shutil.which("ISCC.exe")]
    if os.name == "nt":
        candidates += [
            os.path.expandvars(r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"),
            os.path.expandvars(r"%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"),
        ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("Inno Setup 6 ISCC.exe was not found; pass --iscc")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iscc", help="path to Inno Setup 6 ISCC.exe")
    parser.add_argument(
        "--fetch-only", action="store_true", help="verify/download usbip-win2 only"
    )
    args = parser.parse_args(argv)
    fetch_usbip(ROOT / "build" / "packaging")
    if not args.fetch_only:
        subprocess.run(
            [os.fspath(find_iscc(args.iscc)), os.fspath(ROOT / "packaging/ESCSim.iss")],
            cwd=ROOT,
            check=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

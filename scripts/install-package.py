#!/usr/bin/env python3
"""Install ESCSim's Python package with its built native motor library."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_LIB = ROOT / "src" / "escsim" / "renode" / "lib"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", required=True, type=Path)
    parser.add_argument("--package", default=".[gui]")
    parser.add_argument("pip_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    native = args.native.resolve()
    if not native.is_file():
        parser.error(f"native library does not exist: {native}")
    if native.name not in {"am32sim.dll", "libam32sim.dylib", "libam32sim.so"}:
        parser.error(f"unsupported native library name: {native.name}")

    PACKAGE_LIB.mkdir(parents=True, exist_ok=True)
    staged = PACKAGE_LIB / native.name
    existed = staged.exists()
    if not existed:
        shutil.copy2(native, staged)
    try:
        pip_args = args.pip_args
        if pip_args[:1] == ["--"]:
            pip_args = pip_args[1:]
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            *pip_args,
            args.package,
        ]
        print("+", " ".join(command), flush=True)
        subprocess.run(command, cwd=ROOT, check=True)
    finally:
        if not existed:
            staged.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

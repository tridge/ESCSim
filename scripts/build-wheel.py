#!/usr/bin/env python3
"""Build a platform wheel containing the matching native motor library."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_LIB = ROOT / "src" / "escsim" / "renode" / "lib"
NATIVE_NAMES = {"am32sim.dll", "libam32sim.dylib", "libam32sim.so"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    native = args.native.resolve()
    if not native.is_file() or native.name not in NATIVE_NAMES:
        parser.error(f"invalid native library: {native}")
    PACKAGE_LIB.mkdir(parents=True, exist_ok=True)
    staged = PACKAGE_LIB / native.name
    existed = staged.exists()
    if existed and staged.read_bytes() != native.read_bytes():
        parser.error(f"different native library is already staged: {staged}")
    if not existed:
        shutil.copy2(native, staged)
    args.wheel_dir.mkdir(parents=True, exist_ok=True)
    try:
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--wheel-dir",
            str(args.wheel_dir),
        ]
        print("+", " ".join(command), flush=True)
        subprocess.run(command, cwd=ROOT, check=True)
    finally:
        if not existed:
            staged.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

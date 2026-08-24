#!/usr/bin/env python3
"""Build the native motor library and a self-contained ESCSim application."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def native_name() -> str:
    if os.name == "nt":
        return "am32sim.dll"
    if sys.platform == "darwin":
        return "libam32sim.dylib"
    return "libam32sim.so"


def find_native(build: Path) -> Path:
    matches = list(build.rglob(native_name()))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {native_name()} under {build}, found {matches}"
        )
    return matches[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-pyinstaller", action="store_true")
    parser.add_argument("--configuration", default="Release")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)

    native_build = ROOT / "build" / f"package-native-{platform.system().lower()}"
    run(
        [
            "cmake",
            "-S",
            "native/am32sim",
            "-B",
            str(native_build),
            f"-DCMAKE_BUILD_TYPE={args.configuration}",
        ]
    )
    run(
        [
            "cmake",
            "--build",
            str(native_build),
            "--config",
            args.configuration,
            "--parallel",
        ]
    )
    run(
        [
            "ctest",
            "--test-dir",
            str(native_build),
            "-C",
            args.configuration,
            "--output-on-failure",
        ]
    )

    package_lib = ROOT / "src" / "escsim" / "renode" / "lib" / native_name()
    package_lib.parent.mkdir(parents=True, exist_ok=True)
    existed = package_lib.exists()
    shutil.copy2(find_native(native_build), package_lib)
    print(f"staged {package_lib}")

    try:
        if not args.skip_pyinstaller:
            run(
                [
                    sys.executable,
                    "-m",
                    "PyInstaller",
                    "--noconfirm",
                    "--clean",
                    "--distpath",
                    str(args.dist),
                    "--workpath",
                    str(ROOT / "build" / "pyinstaller"),
                    "packaging/escsim.spec",
                ]
            )
    finally:
        # This is package input for PyInstaller, not a source-tree artifact.
        # Leaving it behind can make a later pure-Python wheel platform-specific.
        if not existed:
            package_lib.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

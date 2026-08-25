# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.compat import is_darwin, is_win


root = Path(SPECPATH).parent
sys.path.insert(0, str(root / "src"))
datas = collect_data_files("escsim")
# These are executed by Renode's IronPython engine, not imported by CPython,
# so PyInstaller cannot discover them as modules and collect_data_files omits
# them by default.
datas += [
    (str(path), "escsim/renode/resources/scripts")
    for path in (root / "src" / "escsim" / "renode" / "resources" / "scripts").glob("*.py")
]
# pydronecan turns these definitions into its uavcan.* namespaces at import
# time; without the source DSDL files CAN controls fail before the GUI opens.
datas += collect_data_files("dronecan", includes=["dsdl_specs/**/*"])
native_binaries = [
    (str(path), "escsim/renode/lib")
    for pattern in ("*.dll", "*.dylib", "*.so")
    for path in (root / "src" / "escsim" / "renode" / "lib").glob(pattern)
]
if len(native_binaries) != 1:
    raise RuntimeError(f"expected one staged native simulator, found {native_binaries}")
hiddenimports = []
# DroneCAN generates its DSDL classes dynamically and pyserial discovers
# platform backends dynamically.  PyInstaller's pyqtgraph hook already handles
# pyqtgraph; collecting all of its optional modules also drags every Qt binding
# installed on the build host into the analysis.
for package in ("dronecan", "serial"):
    hiddenimports += collect_submodules(package)

analysis = Analysis(
    [str(root / "src" / "escsim" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=native_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        # ESCSim deliberately uses one Qt binding.  Build hosts often have
        # another binding installed for unrelated applications, and
        # PyInstaller refuses to bundle more than one.
        "PyQt5",
        "PyQt6",
        "PySide2",
        # pydronecan probes MAVLink support at import time.  ESCSim uses its
        # multicast driver, so bundling pymavlink/MAVProxy (and their mapping,
        # plotting and notebook stacks) is both unnecessary and enormous.
        "dronecan.driver.mavcan",
        "pymavlink",
        "MAVProxy",
        # Optional pyqtgraph front-ends are not used by ESCSim's PlotWidget
        # graphs and pull interactive-development dependencies from a busy
        # build host.
        "pyqtgraph.console",
        "pyqtgraph.examples",
        "pyqtgraph.jupyter",
        "IPython",
        "jupyter",
        "matplotlib",
        "scipy",
        "sphinx",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ESCSim",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / "packaging" / "escsim.ico") if is_win else None,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ESCSim",
)

if is_darwin:
    app = BUNDLE(
        collection,
        name="ESCSim.app",
        bundle_identifier="net.tridgell.am32.escsim",
        info_plist={"NSHighResolutionCapable": True},
    )

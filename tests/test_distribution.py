from __future__ import annotations

from importlib import resources
from pathlib import Path


def test_required_runtime_resources_are_package_owned():
    package = resources.files("escsim")
    required = (
        package.joinpath("resources", "default-targets.h"),
        package.joinpath("resources", "escsim.png"),
        package.joinpath("renode", "resources", "models", "default_7inch.json"),
        package.joinpath("renode", "resources", "platforms", "stm32l431_base.repl"),
        package.joinpath("renode", "resources", "scripts", "am32_l431.resc"),
        package.joinpath(
            "renode", "resources", "FC_Firmware", "SPEEDYBEEF405V5.hex"
        ),
        package.joinpath(
            "renode", "resources", "peripherals", "common", "AM32_GuiLink.cs"
        ),
    )
    assert all(resource.is_file() for resource in required)


def test_packaging_declares_native_library_and_frozen_entrypoint():
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    spec = (root / "packaging" / "escsim.spec").read_text()
    entrypoint = (root / "src" / "escsim" / "__main__.py").read_text()
    assert '"renode/lib/*"' in pyproject
    assert '"renode/resources/FC_Firmware/*.hex"' in pyproject
    assert 'collect_data_files("escsim")' in spec
    assert 'collect_data_files("dronecan"' in spec
    assert '.glob("*.py")' in spec
    assert '"escsim/renode/lib"' in spec
    assert "native_binaries" in spec
    assert 'icon=str(root / "packaging" / "escsim.ico")' in spec
    assert "multiprocessing.freeze_support()" in entrypoint
    assert 'effective_argv[:1] == ["--internal-generator"]' in entrypoint


def test_windows_installer_is_per_user_and_bundles_verified_usbip_driver():
    root = Path(__file__).parents[1]
    installer = (root / "packaging" / "ESCSim.iss").read_text()
    builder = (root / "scripts" / "build-windows-installer.py").read_text()
    assert "PrivilegesRequired=lowest" in installer
    assert "{localappdata}\\Programs\\ESCSim" in installer
    assert "SetupIconFile=escsim.ico" in installer
    assert "USBip-0.9.7.7-x64.exe" in installer
    assert "ExtractTemporaryFile" in installer
    assert "GetVersionNumbers" in installer
    assert "Build = 8" in installer
    assert "temporarily restarts" in installer
    assert "may require a Windows reboot" in installer
    assert "WizardSilent" in installer
    assert "51620fa5f9f8be5932bc9d786deee557" in builder
    assert "hashlib.sha256" in builder
    assert "usbip-win2-BSD-2-Clause.txt" in installer


def test_native_build_uses_make_without_cmake():
    root = Path(__file__).parents[1]
    top_makefile = (root / "Makefile").read_text()
    installer = (root / "scripts" / "install-package.py").read_text()
    wheel_builder = (root / "scripts" / "build-wheel.py").read_text()
    makefile = root / "native" / "am32sim" / "Makefile"
    build_script = (root / "scripts" / "build-package.py").read_text()
    assert makefile.is_file()
    assert not (makefile.parent / "CMakeLists.txt").exists()
    assert "cmake" not in build_script.lower()
    assert 'os.environ.get("MAKE", "make")' in build_script
    assert "all: native wheel" in top_makefile
    assert "wheel: native" in top_makefile
    assert "scripts/build-wheel.py" in top_makefile
    assert "install: native" in top_makefile
    assert "scripts/install-package.py" in top_makefile
    assert '"pip",' in installer
    assert "staged.unlink(missing_ok=True)" in installer
    assert "staged.unlink(missing_ok=True)" in wheel_builder

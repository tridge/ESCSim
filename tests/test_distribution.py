from __future__ import annotations

from importlib import resources
from pathlib import Path


def test_required_runtime_resources_are_package_owned():
    package = resources.files("escsim")
    required = (
        package.joinpath("resources", "default-targets.h"),
        package.joinpath("renode", "resources", "models", "default_7inch.json"),
        package.joinpath("renode", "resources", "platforms", "stm32l431_base.repl"),
        package.joinpath("renode", "resources", "scripts", "am32_l431.resc"),
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
    assert 'collect_data_files("escsim")' in spec
    assert 'collect_data_files("dronecan"' in spec
    assert '.glob("*.py")' in spec
    assert '"escsim/renode/lib"' in spec
    assert "native_binaries" in spec
    assert "multiprocessing.freeze_support()" in entrypoint
    assert 'effective_argv[:1] == ["--internal-generator"]' in entrypoint


def test_windows_installer_is_per_user_and_does_not_bundle_driver():
    root = Path(__file__).parents[1]
    installer = (root / "packaging" / "ESCSim.iss").read_text()
    assert "PrivilegesRequired=lowest" in installer
    assert "{localappdata}\\Programs\\ESCSim" in installer
    assert "usbip" not in installer.lower()

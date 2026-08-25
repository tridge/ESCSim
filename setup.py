"""Setuptools hook marking wheels as platform-specific.

The platform library is staged by ``scripts/build-wheel.py`` or
``scripts/install-package.py`` immediately before setuptools runs.
"""

from setuptools import Distribution, setup
from wheel.bdist_wheel import bdist_wheel


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class PlatformWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        _python, _abi, platform = super().get_tag()
        return "py3", "none", platform


setup(
    distclass=BinaryDistribution,
    cmdclass={"bdist_wheel": PlatformWheel},
)

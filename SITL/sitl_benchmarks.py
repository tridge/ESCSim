"""Named motor benchmarks. Selecting a recipe never starts a simulation.

A recipe supplies an isolated model/EEPROM, timed DShot stages and an
observer-only scope setup. Future recipes share the GUI's Start/Stop controls.
Durations are simulated seconds; a model change represents an external load.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stage:
    throttle: int
    seconds: float
    label: str = ''
    capture: bool = False
    load: float | None = None


@dataclass(frozen=True)
class Benchmark:
    key: str
    name: str
    description: str
    model: str
    stages: tuple
    trigger: str = 'Commutation'
    time_div_us: int = 50
    pretrigger: float = .25
    settings: dict = field(default_factory=dict)
    full_duty: bool = False
    any_phase: bool = False


def registry():
    from demag_bench import benchmarks
    return {recipe.key: recipe for recipe in benchmarks()}


def eeprom_image(seed, settings):
    """Create a benchmark EEPROM without needing the firmware source tree."""
    from sitl_params import PARAMS_BY_NAME
    image = bytearray(seed)
    if len(image) != 192:
        raise ValueError('expected a 192-byte EEPROM seed')
    for name, value in settings.items():
        image[PARAMS_BY_NAME[name][0]] = value
    image[13] = 0  # zero-throttle brake; reserved in older firmware
    return bytes(image)

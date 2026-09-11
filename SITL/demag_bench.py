"""Reproducible synthetic 6S demag bench; settings are stored EEPROM bytes."""
SETTINGS = {
    'INPUT_SIGNAL_TYPE': 1, 'MOTOR_KV': 43, 'MOTOR_POLES': 14,
    'CURRENT_LIMIT': 102, 'TEMPERATURE_LIMIT': 255, 'BEEP_VOLUME': 0,
    'ADVANCE_LEVEL': 16, 'AUTO_ADVANCE': 0, 'VARIABLE_PWM': 0,
    'PWM_FREQUENCY': 24, 'COMP_PWM': 1, 'MAX_RAMP': 160,
    'MIN_DUTY_CYCLE': 1, 'STARTUP_POWER': 100,
    'USE_SINE_START': 0, 'BI_DIRECTIONAL': 0, 'DIR_REVERSED': 0,
    'STUCK_ROTOR_PROTECTION': 1, 'STALL_PROTECTION': 0,
    'LOW_VOLTAGE_CUTOFF': 0, 'REQUIRE_ARMING': 0,
}
# Seconds at each command in simulated time. Startup includes zero-throttle
# arming; the last transition exercises an otherwise stable spinning motor.
RAMP = ((0, 3.0), (150, .7), (300, .7), (500, .7), (750, .7), (1000, 1.0))


def eeprom_image(seed, overrides=None):
    """Use a bundled seed too: packaged users do not need firmware sources."""
    from sitl_benchmarks import eeprom_image as make_image
    return make_image(seed, dict(SETTINGS, **(overrides or {})))


def benchmarks():
    from sitl_benchmarks import Benchmark, Stage
    ramp = tuple(Stage(value, duration) for value, duration in
                 ((0, 3), (150, .5), (300, .5), (500, .5), (750, .5),
                  (1000, .5), (1400, .5), (1800, .5), (2047, 1.0)))
    capture = (Stage(2047, .1, 'Capture steady full duty', capture=True),)
    recipes = (
        Benchmark('demag_full_duty', 'Demag: full duty, 6S / 50 A',
                  'Full-duty voltage trace and winding-current decay at approximately 50 A bus current.',
                  'demag_full_duty_6s', ramp + capture, full_duty=True),
        Benchmark('demag_light_load', 'Demag: full duty, light load',
                  'The same motor under light load: short clamp followed by a clear BEMF slope.',
                  'demag_full_duty_light', ramp + capture, full_duty=True),
        Benchmark('demag_full_overload', 'Demag: full duty, load to desync',
                  'Reach 100% duty at 50 A, then increase mechanical load until a crossing is masked.',
                  'demag_full_duty_6s', ramp + (
                      Stage(2047, .15, 'Full duty: load 5e-8', capture=True, load=5e-8),
                      Stage(2047, .15, 'Full duty: load 8e-8', load=8e-8),
                      Stage(2047, .25, 'Full duty: load 1.2e-7', load=1.2e-7)),
                  trigger='Masked zero crossing', time_div_us=100,
                  pretrigger=.75, full_duty=True, any_phase=True),
        Benchmark('demag_pwm', 'Demag: partial-duty PWM',
                  'Instantaneous phase voltage showing separate PWM-on and PWM-off envelopes.',
                  'demag_full_duty_6s', ramp[:6] + (
                      Stage(1000, .1, 'Capture partial-duty PWM', capture=True),)),
        Benchmark('demag_partial_desync', 'Demag: partial-duty desync',
                  'Original high-inductance regression case; loses sync during the throttle ramp.',
                  'demag_6s_6inch', tuple(Stage(v, t, capture=i == len(RAMP)-1)
                                            for i, (v, t) in enumerate(RAMP)),
                  trigger='Masked zero crossing', time_div_us=100, pretrigger=.5),
    )
    from dataclasses import replace
    return tuple(replace(recipe, settings=dict(SETTINGS, **recipe.settings)) for recipe in recipes)

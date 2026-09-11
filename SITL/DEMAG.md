# Demagnetisation bench and virtual scope

This bench runs real AM32 firmware against the motor/bridge physics. It
starts from zero, reaches stable running and raises throttle into a regime
where residual winding current can hide a back-EMF zero crossing. No pulse
or desync is injected. The scope observes the simulation; its trigger
conditions do not influence the firmware.

## Start here

Build the firmware and run the GUI from the ESCSim checkout:

```sh
make -C /path/to/AM32 AM32_SITL_CAN
AM32_ROOT=/path/to/AM32 python3 SITL/sitl_gui.py
```

A packaged GUI uses its bundled firmware. The **SITL binary** Browse button
selects a different native SITL build for testing firmware changes.

The simulation panel has a **Benchmark** dropdown, defaulting to **None**.
Selecting an entry does not start it. Choose a recipe and click **Start
benchmark**. **Stop benchmark** cancels its remaining stages and sends zero
throttle. Every recipe finishes at zero throttle and retains the scope capture.
The selector is disabled while a benchmark runs; manual throttle/enable changes
cancel the recipe and hand control back to you.

| Benchmark | Purpose / reference trace |
|---|---|
| Demag: full duty, 6S / 50 A | Full-duty phase voltage with a broad current-decay clamp and then a visible BEMF slope, like Alka's loaded capture. Baseline gives approximately 50 A bus current, 29,500 RPM and a 16 us decay pulse. |
| Demag: full duty, light load | Same motor with lower external load; short current-decay pulse, analogous to the ideal waveform. |
| Demag: full duty, load to desync | First reaches the 50 A full-duty point, then raises mechanical load. Captures the first masked crossing across all three phases, while applied duty remains 100%. Current rises above 50 A before the baseline loses sync. |
| Demag: partial-duty PWM | Stable partial-duty running; shows the two instantaneous PWM voltage envelopes in `image4.jpg`. |
| Demag: partial-duty desync | Retains the original high-inductance regression, which fails during the partial-duty ramp. |

For Alka's full-duty trace, start with **Demag: full duty, 6S / 50 A**.
The benchmark opens the scope, arms the motor, ramps to DShot 2047, waits
one simulated second at full duty, then slows to 0.1x and captures at
500 ns/sample. It checks actual applied duty before arming the capture;
a failed startup cannot masquerade as a full-duty experiment.

The scope starts at 50 us/div for an overview. Set **10 us/div** after the
capture to inspect the current-decay pulse from time zero. CH1 is terminal
voltage; CH2 is phase current. Disable CH2 for a voltage-only view like the
physical scope, or enable CH3 (BEMF) to compare the true zero crossing.
The measurement panel shows bus current, duty, decay time, commutation-to-ZC
time and **decay margin**: actual BEMF zero-crossing time minus diode release
time. A negative margin means the current hid the crossing. This uses the
actual crossing, since commutation advance changes its position in the sector.

For the overload recipe, **Motor phase = Any** watches all three phases and
holds the first masked crossing. Phase channels follow the triggering phase,
which is also recorded in the export. This avoids capturing a later recovery
transient on a preselected phase. The baseline eventually increments its
firmware desync counter; a successful firmware fix may leave the masked
trigger waiting. Use Commutation or Long demag to inspect its normal operation.

Choose **Save CSV + setup** or **Save screen PNG** to export. CSV includes
instantaneous bus current as well as phase currents. Metadata records the
firmware hash, isolated EEPROM, model, benchmark key, executed load/throttle
stages and triggering phase.

Each run restarts only the selected ESC. It uses a temporary directory
(`am32-benchmark-escN-*` under the system temporary directory) containing a
fresh `eeprom.bin` and model snapshots; ordinary EEPROM/model files and other
ESCs are untouched. Disconnect USB motor control before starting a benchmark.
**Stop** in the SITL process panel terminates the simulation itself.

Recipes live in `SITL/demag_bench.py`; the dropdown registry and recipe/stage
types live in `SITL/sitl_benchmarks.py`. Models are in `SITL/models/`.
Add future recipes to the registry to expose them through the same controls.
No firmware source checkout is needed by a packaged GUI to run a recipe.

## DHO804-style scope controls

The layout and acquisition controls follow the Rigol DHO804 used in the
reference captures: dark 12-by-8 graticule, four colour-coded channel
controls, time/div, channel scale and position, trigger settings, Run/Stop,
Single and A/B cursors. This is a virtual scope for the motor model, not an
emulation of Rigol's ADC, probe attenuation or 70 MHz bandwidth.

- **CH1–CH4:** independently select terminal voltage, phase current, back
  EMF, filtered comparator input, virtual neutral, bus voltage, comparator
  logic or applied PWM duty. The comparator trace is the currently selected
  comparator output; its selected phase is included in the CSV.
- **Normal:** repeat complete acquisitions only when the trigger occurs.
- **Auto:** also acquire without a matching trigger, marked untriggered.
- **Single:** acquire once, including pre-trigger and post-trigger data,
  then stop acquisition and retain the waveform.
- **RUN / STOP:** stop acquisition or rearm it. The simulation keeps
  running. Changing the scope settings while stopped changes the view;
  rearm to acquire a new window.
- **Timebase:** twelve horizontal divisions, from 1 us/div to 2 ms/div.
  **Pre-trigger** places the trigger from 0% to 90% across the window.
- **Units/div** and **Position (div):** independent vertical scaling for
  each channel. Voltage is relative to battery negative, current is positive
  into the phase, and back EMF is signed relative to the motor neutral.
- **A/B cursors:** time difference and its reciprocal; the reciprocal is
  not necessarily electrical motor frequency.

Triggers:

| Trigger | Meaning |
|---|---|
| Commutation | Selected phase becomes floating. Rising selects its transition out of LOW; Falling selects its transition out of PWM. |
| Edge | Selected channel crosses the level in the selected direction. Level uses that signal's physical units. |
| Long demag | Selected phase is still diode-clamped after the selected duration from entering FLOAT. It does not wait for the pulse to end. |
| Masked zero crossing | The selected phase's model BEMF crosses zero while the phase is floating and a diode still conducts. This uses simulator truth, not firmware knowledge. |
| Firmware desync | The firmware's desync counter increases. This can be later than the electrical problem. |

For **Masked zero crossing**, phase **Any** watches all three phases.
Other triggers use the explicitly selected phase.

The demag measurement reports completed outgoing-current pulses only,
excluding ordinary PWM deadtime on driven phases. Pulses starting before
the capture are not assigned a fabricated start time. A pulse still
conducting at the end is marked incomplete. Measurements are sampled at
the acquired resolution, so compare at finer timesteps when studying a
threshold within a sample or two. A trigger timestamp is the first sample
that satisfies the condition.

## Resolution and PWM

The default physics step is **500 ns**. The scope requests instantaneous
samples down to that step; it never substitutes a PWM-period average,
even at a wide timebase. This preserves the two voltage levels in Alka's
partial-duty capture (`image4.jpg`).

The state stream limits wall-clock sample rate to roughly 200,000/s.
At 1x this means about 5 us per sample; **Fine capture · 0.1x** permits
500 ns per sample. The acquisition bar reports the measured sample period,
not an assumed Rigol sample rate. Use fine capture after arming: slowing
the simulation also slows arming and firmware timeouts.

Acquisition memory is bounded to 100,000 samples. Packet gaps are reported
and timestamps remain in exports; reduce simulation speed before measuring
short pulses in a capture with gaps. A simulator reboot clears pending
acquisition history so no waveform joins two different boots.

Extended channels require the scope-capable AM32 SITL build. Older builds
still support ordinary voltage/current/commutation captures, but cannot
provide BEMF/diode/desync triggers or filtered nodes.

## Model and limitations

Alka's updated description identifies a 6S / 6-inch setup and approximately
50 A at full duty for the stressed captures. His Kv, phase R/L, inertia and
exact operating points are not known. These are physically simulated
**reproduction cases**, not an identified model of his hardware. They reproduce
full-duty operation, a finite winding-current clamp, its visible release into
the BEMF slope, and a full-duty overload that masks the crossing and loses sync.
No current, voltage pulse or desync is injected; the AM32 firmware drives every
commutation and the original electrical/mechanical equations determine the result.

| Parameter | Full-duty / PWM recipes |
|---|---:|
| Motor | 1750 Kv, 14 poles |
| Phase resistance | 25 milliohm |
| Effective phase inductance L-M | 3 uH |
| Rotor/prop inertia | 1.2e-5 kg m² |
| Propeller torque coefficient | 3.4e-8 N m / (rad/s)² |
| Light-load coefficient | 3e-9 N m / (rad/s)² |
| Supply | 25.2 V, 15 milliohm source resistance, 2 mF bus capacitance |
| Bridge | 3 milliohm FET resistance, 0.7 V diode drop |
| Instant commutation current transfer | **0 — disabled throughout every recipe** |
| Comparator noise | 0, for repeatable investigation |

At the default 500 ns timestep the steady 50 A case produces approximately
16 us of current decay, with the true ZC about 25 us after commutation and
about 9 us of remaining margin. Alka's zoomed example measures 13.6 us:
this reproduces its shape and scale, not that exact measured duration.
A native firmware timestep check at 500, 250 and 125 ns retained a 16 us
pulse at each resolution, with approximately 49.5–49.6 A and 29,517–29,522
RPM in the captured windows. This checks the steady operating point; a
precise failure threshold still needs its own timestep convergence check.

The overload recipe holds DShot 2047 and raises only external load torque:
k becomes 5e-8 for 0.15 s, 8e-8 for 0.15 s and 1.2e-7 for 0.25 s, all in
simulated time. This represents a load-bench experiment, not a fixed propeller
suddenly changing its geometry. Bus current passes roughly 60 and 74 A;
loss of synchronism can subsequently produce much larger phase currents.
It is not a claim that Alka's particular motor fails at those currents.
Winding parameters, rotor state, supply and commutation transfer stay fixed.

The original partial-duty failure profile retains its 8 uH inductance and
3e-8 load coefficient. It remains available to compare with previous results.

The comparator RC constants are 800 ns and inertial response time is 2 us.
EEPROM settings use fixed 24 kHz complementary PWM, fixed 5.625-degree advance
(stored byte 16), and disabled current/temperature limiting, as listed in
`demag_bench.py`. Change models with **Edit...** and firmware settings with
**Parameters...** for exploratory runs; a benchmark restores its recipe.
Use ordinary Start and manual throttle to retain your modified setup.

Saturation, detailed switching capacitances, reverse-recovery ringing,
probe/ADC response and thermal dynamics are not modelled. The broad clamp
comes from inductance and persistent body-diode conduction. Profiles with
`commutation_transfer=1` bypass this decay and must not be used to assess
demag compensation. They are not used to make the light-load case pass.

The experimental Alka branch also changes MCU-specific interrupt and
bridge functions. A native build of those experiments needs their SITL
counterparts; changing shared `main.c` alone is insufficient. In particular,
synchronous FET drive requires additional phase-output support. This bench
does not silently substitute a different firmware algorithm for those paths.

## Automated capture / comparison

```sh
# Reproduce and verify the full-duty, approximately 50 A waveform:
AM32_ROOT=/path/to/AM32 python3 SITL/demag_scope_test.py \
    --outdir /tmp/am32-full-duty --benchmark demag_full_duty

# Verify the baseline full-duty overload and masked crossing:
AM32_ROOT=/path/to/AM32 python3 SITL/demag_scope_test.py \
    --outdir /tmp/am32-overload --benchmark demag_full_overload --expect-masked
```

This drives the actual GUI offscreen and saves `demag.csv`, its JSON
metadata, `demag.png`, `result.json` and the GUI log. It verifies the default
None selection, explicit start, completed ramp, sample resolution and absence
of gaps. Full-duty captures check the actual duty; the steady case additionally
checks 45–55 A bus current, a 12–20 us decay pulse, no desync and no PWM-off
notches while the high-side phase is driven. The masked case checks a true
BEMF sign change and a floating, diode-clamped phase at that crossing.

`--expect-masked` additionally requires a captured crossing and a firmware
desync; omit it when testing a fix that should avoid the fault. `--sitl
/path/to/firmware.elf` selects a build. The full-duty steady recipe is the default. `--check-controls` also verifies
Stop at nonzero throttle, returning to None, and manual throttle cancellation.

Core acquisition tests (no Qt required):

```sh
python3 -m unittest discover -s SITL -p test_sitl_scope.py -v
```

The GUI control port accepts `benchmark KEY|None`, `benchmark_start`,
`benchmark_stop`, `benchmark_status`, `scope 0|1`, `scope_single`,
`scope_trigger NAME`, `scope_status`, `scope_save PATH`, and `scope_snap PATH`.
`demag_bench` / `demag_stop` remain aliases for the original partial-duty recipe.

Instrument control reference:
[Rigol DHO800 User Guide](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/oscillosopes/DHO800_UserGuide_EN.pdf).

Physical mechanism reference: [ST AN4220, Appendix C (demagnetization time
allowance)](https://www.st.com/resource/en/application_note/an4220-sensorless-sixstep-bldc-commutation-stmicroelectronics.pdf).

"""Triggered, bounded acquisitions of actual SITL samples (no Qt dependency).

The scope is an observer: triggers never alter the motor or firmware.
Times are simulated seconds. Version 2 sample columns stay unchanged;
version 3 adds BEMF, filtered nodes, diode states, active duty and desyncs.
"""
from collections import deque
from dataclasses import dataclass
import csv
import json
import math
import statistics
import threading


SIGNALS = {}
for p, phase in enumerate('ABC'):
    for key, label, unit, col in [('v', 'Voltage', 'V', 7),
                                  ('i', 'Current', 'A', 4),
                                  ('e', 'Back EMF', 'V', 15),
                                  ('f', 'Filtered voltage', 'V', 18)]:
        SIGNALS[key + phase] = (label + ' ' + phase, unit, col + p)
SIGNALS.update({'bus': ('Bus voltage', 'V', 10),
                'ibus': ('Bus current', 'A', 11),
                'neutral': ('Virtual neutral', 'V', None),
                'filtered_neutral': ('Filtered neutral', 'V', 21),
                'comp': ('Comparator', 'logic', 14),
                'duty': ('Applied PWM duty', '%', 23)})


def signal_value(sample, key):
    if key == 'neutral':
        return sum(sample[7:10]) / 3
    col = SIGNALS[key][2]
    if col >= len(sample):
        return math.nan
    value = sample[col]
    return value * 100 if key == 'duty' else value


def diode(sample, phase):
    if len(sample) < 25:
        return 0
    value = sample[22][phase]
    return value - 256 if value > 127 else value


@dataclass(frozen=True)
class ScopeFrame:
    samples: tuple
    trigger_time: float
    reason: str
    span: float
    pretrigger: float
    trigger_phase: int = 0

    def measurements(self, phase):
        samples = self.samples
        intervals = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
        dt = statistics.median(intervals) if intervals else 0
        gaps = sum(d > dt * 1.5 for d in intervals) if dt else 0
        # Only report complete freewheel pulses: PWM/deadtime diode
        # conduction on a driven leg is not a commutation demag pulse.
        pulses = []
        start = None
        for a, b in zip(samples, samples[1:]):
            if dt and b[0] - a[0] > dt * 1.5:
                start = None
                continue
            if b[12][phase] == 0 and a[12][phase] != 0:
                start = b[0] if diode(b, phase) else None
            elif start is not None:
                if b[12][phase] != 0:
                    start = None
                elif not diode(b, phase):
                    pulses.append((start, b[0], b[0] - start))
                    start = None
        # Associate actual diode release and rotor BEMF crossing with
        # each floating sector. The margin is measured, not half a nominal
        # step: commutation advance changes the available decay time.
        sectors = []
        active = None
        for a, b in zip(samples, samples[1:]):
            if dt and b[0] - a[0] > dt * 1.5:
                active = None  # never interpolate a missing pulse/zero crossing
                continue
            if b[12][phase] != 0:
                active = None
                continue
            if a[12][phase] != 0:
                active = dict(start_s=b[0], current_a=b[4+phase],
                              release_s=None if diode(b, phase) else b[0],
                              zero_cross_s=None, margin_s=None)
                sectors.append(active)
            if active is None or len(b) < 25:
                continue
            if active['release_s'] is None and not diode(b, phase):
                active['release_s'] = b[0]
            ea, eb = a[15+phase], b[15+phase]
            if (active['zero_cross_s'] is None and a[12][phase] == 0 and
                    ((ea < 0 <= eb) or (ea > 0 >= eb))):
                active['zero_cross_s'] = a[0] + (b[0]-a[0]) * (-ea)/(eb-ea)
            if active['release_s'] is not None and active['zero_cross_s'] is not None:
                active['margin_s'] = active['zero_cross_s'] - active['release_s']
        # Time-weight means so partial final intervals do not bias a reading.
        pairs = [(a, b) for a, b in zip(samples, samples[1:])
                 if 0 < b[0]-a[0] <= dt * 1.5]
        duration = sum(b[0]-a[0] for a, b in pairs)
        def mean(col):
            return (sum((b[0]-a[0])*(a[col]+b[col])*.5 for a,b in pairs)/duration
                    if duration else samples[-1][col])
        return {'sample_interval_s': dt, 'gaps': gaps,
                'demag_pulses': pulses, 'sectors': sectors,
                'incomplete_demag_start_s': start,
                'bus_current_a': mean(11), 'rpm': mean(1)*60/(2*math.pi),
                'duty_min': min(s[23] for s in samples) if len(samples[0]) >= 25 else None,
                'duty_max': max(s[23] for s in samples) if len(samples[0]) >= 25 else None}

    def save(self, path, metadata=None):
        """CSV in physical units plus JSON setup/trigger metadata."""
        columns = list(SIGNALS)
        with open(path, 'w', newline='', encoding='utf-8') as out:
            writer = csv.writer(out)
            writer.writerow(['time_s', 'relative_us'] + columns +
                            ['mode_A', 'mode_B', 'mode_C', 'comparator_phase',
                             'diode_A', 'diode_B', 'diode_C', 'desync_count'])
            for s in self.samples:
                writer.writerow([s[0], (s[0] - self.trigger_time) * 1e6] +
                                [signal_value(s, key) for key in columns] +
                                list(s[12]) + [s[13]] +
                                [diode(s, p) if len(s) >= 25 else '' for p in range(3)] +
                                [s[24] if len(s) >= 25 else ''])
        info = dict(metadata or {})
        info.update(trigger_time_s=self.trigger_time, trigger=self.reason,
                    span_s=self.span, pretrigger=self.pretrigger, trigger_phase=self.trigger_phase,
                    sample_count=len(self.samples),
                    sample_version=3 if len(self.samples[0]) >= 25 else 2)
        with open(str(path) + '.json', 'w', encoding='utf-8') as out:
            json.dump(info, out, indent=2, allow_nan=False)
            out.write('\n')


class ScopeCapture:
    """Reader-thread acquisition; GUI only takes completed immutable frames.

    Changing setup rearms acquisition. Stop retains the last complete frame.
    A simulator reset discards pretrigger/posttrigger data across the reset.
    """
    MAX_SAMPLES = 100000

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.mode = 'Normal'
        self.trigger = 'Commutation'
        self.phase = 0
        self.source = 'vA'
        self.level = 24.0
        self.edge = 'Rising'
        self.demag_us = 15.0
        self.span = .0006
        self.pretrigger = .25
        self.frame = None
        self.generation = 0
        self.status = 'STOP'
        self._reset()

    def _reset(self):
        self.history = deque(maxlen=self.MAX_SAMPLES)
        self.pending = None
        self.trigger_time = None
        self.previous = None
        self.float_start = None
        self.demag_fired = False
        self.wait_since = None
        self.hit_phase = 0
        self.last_interval = None

    def arm(self, mode='Normal', **setup):
        with self.lock:
            for name, value in setup.items():
                if name not in ('trigger', 'phase', 'source', 'level', 'edge',
                                'demag_us', 'span', 'pretrigger'):
                    raise ValueError(name)
                setattr(self, name, value)
            if not 0 < self.span <= .05 or not 0 <= self.pretrigger <= .9:
                raise ValueError('scope window out of range')
            if (mode not in ('Normal', 'Auto', 'Single') or
                    (self.phase not in range(4) or (self.phase == 3 and self.trigger != 'Masked zero crossing')) or self.source not in SIGNALS or
                    not math.isfinite(self.level) or not math.isfinite(self.demag_us) or
                    self.demag_us <= 0):
                raise ValueError('invalid scope setup')
            self.mode = mode
            self.enabled = True
            self.status = 'WAIT'
            self._reset()

    def stop(self):
        with self.lock:
            self.enabled = False
            self.status = 'STOP'
            self.pending = None

    def snapshot(self):
        with self.lock:
            return self.generation, self.frame, self.status

    def _triggered(self, a, b):
        p = self.phase
        if a is None:
            return False
        if p == 3:
            if len(a) < 25 or len(b) < 25:
                return False
            for phase in range(3):
                ea, eb = a[15+phase], b[15+phase]
                if (((ea < 0 <= eb) or (ea > 0 >= eb)) and
                        a[12][phase] == 0 and b[12][phase] == 0 and diode(b, phase)):
                    self.hit_phase = phase
                    return True
            return False
        self.hit_phase = p
        entered = a[12][p] != 0 and b[12][p] == 0
        if entered:
            self.float_start = b[0]
            self.demag_fired = False
        if b[12][p] != 0:
            self.float_start = None
        if self.trigger == 'Commutation':
            return entered and ((a[12][p] == 1) == (self.edge == 'Rising'))
        if self.trigger == 'Edge':
            av, bv = signal_value(a, self.source), signal_value(b, self.source)
            return (av < self.level <= bv if self.edge == 'Rising'
                    else av > self.level >= bv)
        if len(b) < 25 or len(a) < 25:
            return False
        if self.trigger == 'Firmware desync':
            return b[24] > a[24]
        if self.trigger == 'Masked zero crossing':
            ea, eb = a[15 + p], b[15 + p]
            crossed = (ea < 0 <= eb) or (ea > 0 >= eb)
            return crossed and a[12][p] == 0 and b[12][p] == 0 and bool(diode(b, p))
        if self.trigger == 'Long demag':
            if not diode(b, p):
                self.float_start = None
            if (self.float_start is not None and not self.demag_fired and
                    diode(b, p) and
                    b[0] - self.float_start >= self.demag_us * 1e-6):
                self.demag_fired = True
                return True
        return False

    def feed(self, samples):
        if not self.enabled:
            return
        with self.lock:
            for s in samples:
                if not self.enabled:
                    break
                if self.previous is not None and s[0] == self.previous[0]:
                    continue  # duplicate UDP sample
                if self.previous is not None and s[0] < self.previous[0]:
                    self._reset()
                    self.status = 'WAIT (ESC reset)'
                if self.wait_since is None:
                    self.wait_since = s[0]
                interval = s[0] - self.previous[0] if self.previous is not None else None
                gap = (interval is not None and self.last_interval is not None and
                       interval > self.last_interval * 1.5)
                if gap:
                    self.float_start = None
                hit = False if gap else self._triggered(self.previous, s)
                self.last_interval = interval
                self.previous = s
                self.history.append(s)
                cutoff = s[0] - self.span * self.pretrigger
                # Retain one sample before the boundary for interpolation.
                while len(self.history) > 2 and self.history[1][0] < cutoff:
                    self.history.popleft()
                if self.pending is not None:
                    self.pending.append(s)
                    if len(self.pending) >= self.MAX_SAMPLES:
                        self.enabled = False
                        self.status = 'STOP: capture memory full; shorten time/div'
                        self.pending = None
                    elif s[0] >= self.trigger_time + self.span * (1 - self.pretrigger):
                        self.frame = ScopeFrame(tuple(self.pending), self.trigger_time,
                                                self.reason, self.span, self.pretrigger,
                                                self.pending_phase)
                        self.generation += 1
                        self.wait_since = s[0]
                        self.pending = None
                        if self.mode == 'Single':
                            self.enabled = False
                            self.status = 'STOP'
                        else:
                            self.status = 'RUN'
                elif self.history[0][0] <= cutoff:
                    forced = (self.mode == 'Auto' and
                              s[0] - self.wait_since >= self.span)
                    if hit or forced:
                        self.trigger_time = s[0]
                        self.pending_phase = self.hit_phase
                        self.reason = self.trigger if hit else 'Auto (untriggered)'
                        self.pending = list(self.history)
                        self.status = 'TRIGGERED'

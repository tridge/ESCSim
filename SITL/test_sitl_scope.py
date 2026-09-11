"""Acquisition semantics and wire compatibility, independent of Qt."""
import csv
import tempfile
import unittest
from pathlib import Path
from sitl_scope import ScopeCapture, ScopeFrame, signal_value
from sitl_gui_backend import SimStream


def sample(us, voltage=0, mode=1, current=-20, emf=-10, diode=1, desync=0):
    return (us*1e-6, 1000., 0., 0., current, 0., -current,
            voltage, 0., 48., 48., 10., bytes([mode, 1, 2]), 0, 0,
            emf, 10., -10., voltage, 0., 48., 24.,
            bytes([diode % 256, 0, 0]), .5, desync)


class ScopeTests(unittest.TestCase):
    def test_single_has_pre_and_post_trigger_and_remains_frozen(self):
        scope = ScopeCapture()
        scope.arm('Single', trigger='Edge', source='vA', level=24,
                  span=40e-6, pretrigger=.25)
        scope.feed([sample(t, 0 if t < 20 else 48) for t in range(80)])
        generation, frame, state = scope.snapshot()
        self.assertEqual(generation, 1)
        self.assertEqual(state, 'STOP')
        self.assertAlmostEqual(frame.trigger_time, 20e-6)
        self.assertLessEqual(frame.samples[0][0], 10e-6)
        self.assertGreaterEqual(frame.samples[-1][0], 50e-6)
        scope.feed([sample(t) for t in range(80, 200)])
        self.assertIs(scope.snapshot()[1], frame)

    def test_long_demag_triggers_before_trailing_edge(self):
        scope = ScopeCapture()
        scope.arm('Single', trigger='Long demag', demag_us=15,
                  span=40e-6, pretrigger=.5)
        scope.feed([sample(t, mode=1 if t < 25 else 0) for t in range(100)])
        frame = scope.snapshot()[1]
        self.assertIsNotNone(frame)
        self.assertAlmostEqual(frame.trigger_time, 40e-6)

    def test_masked_crossing_needs_floating_phase_and_conducting_diode(self):
        for mode, diode_value, expected in [(0, 1, True), (0, 0, False), (2, 1, False)]:
            with self.subTest(mode=mode, diode=diode_value):
                scope = ScopeCapture()
                scope.arm('Single', trigger='Masked zero crossing', span=20e-6)
                scope.feed([sample(t, mode=mode, diode=diode_value,
                                   emf=t-30) for t in range(80)])
                self.assertEqual(scope.frame is not None, expected)

    def test_reset_discards_partial_capture(self):
        scope = ScopeCapture()
        scope.arm('Single', trigger='Edge', source='vA', level=24, span=40e-6)
        scope.feed([sample(t, voltage=48 if t >= 20 else 0) for t in range(30)])
        scope.feed([sample(t) for t in range(30)])
        self.assertIsNone(scope.frame)
        self.assertIsNone(scope.pending)

    def test_pwm_is_not_averaged_and_packet_gaps_are_reported(self):
        values = tuple(sample(t, voltage=48 if t % 2 else 0) for t in [0, 1, 2, 5, 6, 7])
        frame = ScopeFrame(values, 2e-6, 'Edge', 10e-6, .25)
        self.assertEqual(frame.measurements(0)['gaps'], 1)
        self.assertEqual({signal_value(s, 'vA') for s in frame.samples}, {0, 48})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'capture.csv'
            frame.save(path)
            with path.open() as source:
                rows = list(csv.DictReader(source))
            self.assertEqual({float(r['vA']) for r in rows}, {0, 48})
            self.assertTrue(Path(str(path)+'.json').exists())

    def test_auto_forces_capture_but_normal_waits_without_edge(self):
        for mode, expected in [('Auto', True), ('Normal', False)]:
            scope = ScopeCapture()
            scope.arm(mode, trigger='Edge', source='vA', level=24, span=20e-6)
            scope.feed([sample(t) for t in range(100)])
            self.assertEqual(scope.frame is not None, expected)

    def test_measured_release_margin_can_be_positive_or_negative(self):
        for release_us, expected in [(15, 5e-6), (25, -5e-6)]:
            rows = tuple(sample(t, mode=1 if t < 10 else 0,
                                emf=t-20, diode=int(t < release_us)) for t in range(40))
            frame = ScopeFrame(rows, 10e-6, 'Commutation', 40e-6, .25)
            sector = frame.measurements(0)['sectors'][0]
            self.assertAlmostEqual(sector['margin_s'], expected)
            self.assertAlmostEqual(sector['zero_cross_s'], 20e-6)

    def test_missing_samples_do_not_create_a_masked_crossing(self):
        scope = ScopeCapture()
        scope.arm('Single', trigger='Masked zero crossing', span=20e-6)
        scope.feed([sample(t, mode=0, emf=-10) for t in range(20)])
        scope.feed([sample(t, mode=0, emf=10) for t in range(40, 100)])
        self.assertIsNone(scope.frame)

    def test_any_phase_retains_the_first_trigger_phase(self):
        scope = ScopeCapture()
        scope.arm('Single', trigger='Masked zero crossing', phase=3, span=20e-6)
        rows = []
        for t in range(80):
            row = list(sample(t, mode=2, emf=-10))
            row[12] = bytes([2, 0, 1])
            row[16] = t-30  # phase B crossing
            row[22] = bytes([0, 1, 0])
            rows.append(tuple(row))
        scope.feed(rows)
        self.assertEqual(scope.frame.trigger_phase, 1)
        self.assertAlmostEqual(scope.frame.trigger_time, 30e-6)

    def test_wire_layout_preserves_version_two_prefix(self):
        self.assertEqual(SimStream.SAMPLE.size, 60)
        self.assertEqual(SimStream.SCOPE_SAMPLE.size, 100)
        values = sample(50)
        raw = SimStream.SCOPE_SAMPLE.pack(50000, *values[1:])
        self.assertEqual(SimStream.SAMPLE.unpack(raw[:60]),
                         SimStream.SCOPE_SAMPLE.unpack(raw)[:15])
        self.assertEqual(SimStream.SCOPE_SAMPLE.unpack(raw)[22], b'\x01\x00\x00')


if __name__ == '__main__':
    unittest.main()

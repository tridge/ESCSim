"""Four-channel virtual scope with DHO804-style acquisition controls.

Layout/control reference: RIGOL DHO800 User Guide, acquisition, trigger
and cursor sections. This displays motor-model samples, not a simulation
of the instrument's bandwidth, ADC, probes or sample-rate specifications.
"""
import math
import os
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QDoubleSpinBox, QCheckBox, QFileDialog,
    QGroupBox)
from sitl_scope import SIGNALS, signal_value

COLORS = ('#ffe329', '#49c9ff', '#ee65d5', '#729aff')


class DemagScopeWindow(QWidget):
    def __init__(self, stream, on_close, fine_capture, title='ESC 1', metadata=None, benchmark_status=None):
        super().__init__()
        self.stream, self.on_close = stream, on_close
        self.capture = stream.scope
        self.frame = None
        self.metadata = metadata or (lambda: {})
        self.benchmark_status = benchmark_status or (lambda: None)
        self.frame_metadata = {}
        self.generation = -1
        self.inspect_time_us = None
        self.readout_frame = None
        self.readout_times = np.array([])
        self.readout_interval = 0.0
        self.setWindowTitle('AM32 virtual scope — DHO804 layout — ' + title)
        self.resize(1240, 740)
        self.setStyleSheet('''
            QWidget { background:#202630; color:#e5ebf0; font-size:12px; }
            QGroupBox { border:1px solid #54606d; border-radius:5px;
                        margin-top:10px; padding-top:8px; }
            QGroupBox::title { subcontrol-origin:margin; left:8px; }
            QPushButton, QComboBox, QDoubleSpinBox { background:#354252;
                border:1px solid #627081; border-radius:4px; padding:5px; }
            QPushButton:pressed { background:#596e84; }
            QPushButton:checked { background:#355d4b; }
        ''')
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        brand = QLabel('AM32  <b>VIRTUAL SCOPE</b>')
        brand.setStyleSheet('color:#52c6f3; font-size:18px; padding:8px')
        header.addWidget(brand)
        self.state = QLabel('WAIT')
        header.addWidget(self.state)
        self.acquisition = QLabel('H 50 µs/div     A waiting for samples')
        header.addWidget(self.acquisition, 1)
        self.trigger_label = QLabel('T  Commutation A')
        header.addWidget(self.trigger_label)
        outer.addLayout(header)
        self.benchmark_label = QLabel('')
        self.benchmark_label.setWordWrap(True)
        self.benchmark_label.setVisible(False)
        outer.addWidget(self.benchmark_label)
        middle = QHBoxLayout()
        self.plot = pg.PlotWidget(background='#151b23')
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=False, y=False)
        self.grid_x = [pg.InfiniteLine(angle=90, pen=pg.mkPen('#64717d', style=Qt.DotLine))
                       for _ in range(13)]
        self.grid_y = [pg.InfiniteLine(y, angle=0, pen=pg.mkPen('#64717d', style=Qt.DotLine))
                       for y in range(-4, 5)]
        for line in self.grid_x + self.grid_y:
            line.setZValue(-10)
            self.plot.addItem(line)
        self.plot.setLabel('bottom', 'Time from trigger', 'µs')
        self.plot.getAxis('bottom').enableAutoSIPrefix(False)
        self.plot.getAxis('left').setTicks([[(x, '') for x in range(-4, 5)]])
        self.plot.getAxis('left').setWidth(20)
        self.plot.setYRange(-4, 4, padding=0)
        self.plot.setDownsampling(auto=True, mode='peak')
        self.plot.setClipToView(True)
        self.curves = [self.plot.plot(pen=pg.mkPen(c, width=1.3)) for c in COLORS]
        self.trigger_line = pg.InfiniteLine(0, pen=pg.mkPen('#eab83f', style=Qt.DashLine),
                                            label='T', labelOpts={'position': .95})
        self.plot.addItem(self.trigger_line)
        self.inspect_line = pg.InfiniteLine(0, pen=pg.mkPen('#9ba8b6', style=Qt.DotLine))
        self.inspect_line.setVisible(False)
        self.plot.addItem(self.inspect_line)
        self.cursors = [pg.InfiniteLine(x, movable=True,
                         pen=pg.mkPen('#c1d0dc', style=Qt.DashLine), label=name,
                         labelOpts={'position': .85}) for x, name in [(0, 'A'), (15, 'B')]]
        for cursor in self.cursors:
            self.plot.addItem(cursor)
            cursor.sigPositionChanged.connect(self.cursor_readout)
            cursor.sigDragged.connect(lambda line: self.inspect_at_us(line.value()))
        middle.addWidget(self.plot, 1)
        side = QVBoxLayout()
        keys = QGridLayout()
        self.run_btn = QPushButton('RUN / STOP')
        self.run_btn.clicked.connect(self.run_stop)
        single = QPushButton('SINGLE')
        single.clicked.connect(lambda: self.arm('Single'))
        auto = QPushButton('AUTO SCALE')
        auto.clicked.connect(self.auto_scale)
        fine = QPushButton('Fine capture · 0.1×')
        fine.setToolTip('Request 500 ns samples and slow simulated time to 0.1×.\nUse after the motor has armed and started.')
        fine.clicked.connect(fine_capture)
        keys.addWidget(self.run_btn, 0, 0)
        keys.addWidget(single, 0, 1)
        keys.addWidget(auto, 1, 0)
        keys.addWidget(fine, 1, 1)
        side.addLayout(keys)
        setup = QGroupBox('Horizontal / Trigger')
        grid = QGridLayout(setup)
        self.timebase = QComboBox()
        for us in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000):
            self.timebase.addItem('%g µs/div' % us, us)
        self.timebase.setCurrentIndex(5)
        self.mode = QComboBox(); self.mode.addItems(['Normal', 'Auto'])
        self.trigger = QComboBox()
        self.trigger.addItems(['Commutation', 'Edge', 'Long demag',
                               'Masked zero crossing', 'Firmware desync'])
        self.phase = QComboBox(); self.phase.addItems(list('ABC'))
        self.source = QComboBox(); self.source.addItems(['CH1', 'CH2', 'CH3', 'CH4'])
        self.edge = QComboBox(); self.edge.addItems(['Rising', 'Falling'])
        self.level = QDoubleSpinBox(); self.level.setRange(-10000, 10000); self.level.setValue(24)
        self.demag = QDoubleSpinBox(); self.demag.setRange(.5, 10000); self.demag.setValue(15); self.demag.setSuffix(' µs')
        self.pre = QDoubleSpinBox(); self.pre.setRange(0, 90); self.pre.setValue(25); self.pre.setSuffix(' %')
        for row, (label, widget) in enumerate([
                ('Timebase', self.timebase), ('Sweep', self.mode),
                ('Trigger', self.trigger), ('Motor phase', self.phase),
                ('Edge source', self.source), ('Slope', self.edge),
                ('Level', self.level), ('Demag longer than', self.demag),
                ('Pre-trigger', self.pre)]):
            grid.addWidget(QLabel(label), row, 0); grid.addWidget(widget, row, 1)
        side.addWidget(setup)
        measure = QGroupBox('Measure / Cursor')
        ml = QVBoxLayout(measure)
        cursor_toggle = QCheckBox('Time cursors A / B'); cursor_toggle.setChecked(True)
        cursor_toggle.toggled.connect(lambda on: [c.setVisible(on) for c in self.cursors])
        ml.addWidget(cursor_toggle)
        self.cursor_label = QLabel(''); ml.addWidget(self.cursor_label)
        self.measure_label = QLabel('Waiting for a complete capture')
        self.measure_label.setWordWrap(True); ml.addWidget(self.measure_label)
        side.addWidget(measure)
        save = QPushButton('Save CSV + setup'); save.clicked.connect(self.save_csv)
        png = QPushButton('Save screen PNG'); png.clicked.connect(self.save_png)
        side.addWidget(save); side.addWidget(png); side.addStretch(1)
        middle.addLayout(side)
        outer.addLayout(middle, 1)
        self.channels = []
        self.channel_values = []
        bottom = QHBoxLayout()
        for i, (source_key, scale, offset) in enumerate([
                ('vA', 5, -2.5), ('iA', 20, -1), ('eA', 10, -1), ('comp', 1, -3.5)]):
            box = QGroupBox('CH%d' % (i + 1))
            box.setStyleSheet('QGroupBox { color:%s; border:1px solid %s; }' % (COLORS[i], COLORS[i]))
            controls = QGridLayout(box)
            enabled = QCheckBox('On'); enabled.setChecked(i in (0, 1))
            signal = QComboBox()
            for key, (label, unit, _) in SIGNALS.items():
                signal.addItem(label + ' (' + unit + ')', key)
            signal.setCurrentIndex(signal.findData(source_key))
            gain = QDoubleSpinBox(); gain.setRange(.01, 10000); gain.setDecimals(2); gain.setValue(scale)
            position = QDoubleSpinBox(); position.setRange(-100, 100); position.setDecimals(2); position.setValue(offset)
            controls.addWidget(enabled, 0, 0); controls.addWidget(signal, 0, 1)
            controls.addWidget(QLabel('Units/div'), 1, 0); controls.addWidget(gain, 1, 1)
            controls.addWidget(QLabel('Position (div)'), 2, 0); controls.addWidget(position, 2, 1)
            value = QLabel('—')
            value.setStyleSheet('color:%s; font-size:14px; font-weight:bold' % COLORS[i])
            value.setMinimumWidth(120)
            value.setToolTip('Nearest recorded sample at the mouse or dragged time cursor; no interpolation across PWM edges.')
            controls.addWidget(QLabel('At cursor'), 3, 0)
            controls.addWidget(value, 3, 1)
            self.channel_values.append(value)
            self.channels.append((enabled, signal, gain, position))
            enabled.toggled.connect(self.render)
            signal.currentIndexChanged.connect(self.channel_changed)
            gain.valueChanged.connect(self.render); position.valueChanged.connect(self.render)
            bottom.addWidget(box)
        outer.addLayout(bottom)
        self.notice = QLabel('Virtual physics • DC terminal voltages relative to battery negative • 12 × 8 divisions')
        outer.addWidget(self.notice)
        for widget in (self.timebase, self.mode, self.trigger, self.phase, self.source, self.edge):
            widget.currentIndexChanged.connect(self.setup_changed)
        for widget in (self.level, self.demag, self.pre):
            widget.valueChanged.connect(self.setup_changed)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(100)
        self.plot.scene().sigMouseMoved.connect(self.inspect_mouse)
        self.cursor_readout()
        self.arm()

    def setup(self):
        return dict(trigger=self.trigger.currentText(), phase=self.phase.currentIndex(),
                    source=self.channels[self.source.currentIndex()][1].currentData(),
                    edge=self.edge.currentText(), level=self.level.value(),
                    demag_us=self.demag.value(), span=self.timebase.currentData() * 12e-6,
                    pretrigger=self.pre.value() / 100)

    def arm(self, mode=None):
        self.capture.arm(mode or self.mode.currentText(), **self.setup())

    def setup_changed(self, *_):
        # Any captures the first affected phase, before a later recovery
        # transient can be mistaken for the initiating full-duty event.
        self.phase.blockSignals(True)
        if self.trigger.currentText() == 'Masked zero crossing':
            if self.phase.count() == 3:
                self.phase.addItem('Any')
        elif self.phase.count() == 4:
            if self.phase.currentIndex() == 3:
                self.phase.setCurrentIndex(0)
            self.phase.removeItem(3)
        self.phase.blockSignals(False)
        if self.capture.enabled:
            self.arm(self.capture.mode)
        self.render()

    def channel_changed(self, *_):
        self.setup_changed()

    def run_stop(self):
        if self.capture.enabled:
            self.capture.stop()
        else:
            self.arm()
        self.refresh()

    def refresh(self):
        generation, frame, state = self.capture.snapshot()
        progress = self.benchmark_status()
        preparing = bool(progress and progress[1] and frame is None and not self.capture.enabled)
        self.benchmark_label.setVisible(progress is not None)
        if progress:
            self.benchmark_label.setText('Benchmark: ' + progress[0])
        self.state.setText('PREPARING' if preparing else state)
        if frame is None:
            self.acquisition.setText('H %g µs/div · No capture acquired' % self.timebase.currentData())
            if preparing:
                self.measure_label.setText('The benchmark will arm the scope after startup.\n' + progress[0])
            elif self.capture.enabled:
                self.measure_label.setText('Waiting for trigger: ' + self.trigger.currentText())
            elif progress:
                self.measure_label.setText(progress[0])
            else:
                self.measure_label.setText('No capture yet. Use RUN / STOP or SINGLE to acquire.')
        latest = self.stream.latest()
        if latest is not None and len(latest) < 25:
            self.notice.setText('Older firmware: BEMF/diode/desync channels unavailable. Rebuild SITL for demag triggers.')
        self.state.setStyleSheet('color:%s; font-size:17px; font-weight:bold' %
                                ('#ff7474' if state.startswith('STOP') and not preparing else '#6ade97'))
        if generation != self.generation and frame is not None:
            self.generation, self.frame = generation, frame
            self.frame_metadata = self.metadata()
            if self.phase.currentIndex() == 3:
                for _, source, _, _ in self.channels:
                    key = source.currentData()
                    if len(key) == 2 and key[0] in 'vief' and key[1] in 'ABC':
                        source.blockSignals(True)
                        source.setCurrentIndex(source.findData(key[0] + 'ABC'[frame.trigger_phase]))
                        source.blockSignals(False)
            self.render()

    def render(self, *_):
        self.update_channel_values()
        us = self.timebase.currentData()
        span = us * 12
        pre = self.pre.value() / 100
        self.plot.setXRange(-span * pre, span * (1 - pre), padding=0)
        self.plot.getAxis('bottom').setTickSpacing(us, us / 5)
        for i, line in enumerate(self.grid_x):
            line.setValue(-span * pre + i * us)
        self.plot.getAxis('left').setTickSpacing(1, .2)
        self.trigger_label.setText('T  %s · %s' % (self.trigger.currentText(), self.phase.currentText()))
        if self.frame is None:
            self.acquisition.setText('H %g µs/div    A waiting for samples' % us)
            return
        frame = self.frame
        samples = frame.samples
        times = np.array([s[0] - frame.trigger_time for s in samples]) * 1e6
        unavailable = []
        for i, (enabled, source, scale, position) in enumerate(self.channels):
            self.curves[i].setVisible(enabled.isChecked())
            if enabled.isChecked():
                key = source.currentData()
                values = np.array([signal_value(s, key) for s in samples])
                if not np.isfinite(values).any():
                    unavailable.append(source.currentText())
                self.curves[i].setData(times, values / scale.value() + position.value())
        phase = frame.trigger_phase if self.phase.currentIndex() == 3 else self.phase.currentIndex()
        stats = frame.measurements(phase)
        dt = stats['sample_interval_s']
        self.acquisition.setText('H %g µs/div    A %s Sa/s · %d pts · %.3g µs/pt' %
                                 (us, '%g' % (1 / dt) if dt else '?', len(samples), dt * 1e6))
        pulses = stats['demag_pulses']
        lines = [frame.reason + ' · ' + 'ABC'[phase], 'Sample gaps: %d' % stats['gaps']]
        if pulses:
            lines.append('Demag max: %.2f µs' % (max(p[2] for p in pulses) * 1e6))
        elif stats['incomplete_demag_start_s'] is not None:
            lines.append('Demag still conducting at capture end')
        else:
            lines.append('No complete demag pulse in this capture')
        sectors = [sector for sector in stats['sectors'] if sector['zero_cross_s'] is not None]
        if sectors:
            sector = min(sectors, key=lambda event: abs(event['start_s'] - frame.trigger_time))
            lines.append('Commutation to ZC: %.2f µs' %
                         ((sector['zero_cross_s']-sector['start_s'])*1e6))
            if sector['margin_s'] is not None:
                lines.append('Decay margin: %.2f µs' % (sector['margin_s']*1e6))
            else:
                lines.append('Current still flowing at ZC')
        lines.append('RPM %.0f  ·  Bus %.1f V' % (stats['rpm'], samples[-1][10]))
        lines.append('Mean bus current: %.1f A' % stats['bus_current_a'])
        if stats['duty_min'] is not None:
            lines.append('PWM duty: %.1f–%.1f%%' % (100*stats['duty_min'], 100*stats['duty_max']))
        self.measure_label.setText('\n'.join(lines))
        if unavailable or len(samples[0]) < 25:
            self.notice.setText('Older firmware: extended physics channels/triggers unavailable. Rebuild SITL with scope support.')
        elif stats['gaps']:
            self.notice.setText('Capture has sample gaps; reduce simulation speed before measuring short pulses.')
        else:
            self.notice.setText('Virtual physics • DC terminal voltages relative to battery negative • trigger at t=%.9f s' % frame.trigger_time)
        self.cursor_readout()

    def cursor_readout(self, *_):
        a, b = (c.value() for c in self.cursors)
        delta = abs(b - a)
        self.cursor_label.setText('A %.3f µs    B %.3f µs\nΔT %.3f µs    1/ΔT %s' %
            (a, b, delta, ('%.3f kHz' % (1000 / delta)) if delta else '—'))

    def inspect_mouse(self, position):
        view = self.plot.getViewBox()
        if view.sceneBoundingRect().contains(position):
            self.inspect_at_us(view.mapSceneToView(position).x())

    def inspect_at_us(self, time_us):
        self.inspect_time_us = time_us
        self.update_channel_values()

    def update_channel_values(self):
        # Mouse motion only performs a binary search and four label updates;
        # it must not rebuild curves or rescan a large acquisition.
        frame = self.frame
        if frame is not self.readout_frame:
            self.readout_frame = frame
            self.readout_times = (np.array([s[0] - frame.trigger_time for s in frame.samples])*1e6
                                  if frame is not None else np.array([]))
            self.readout_interval = (float(np.median(np.diff(self.readout_times)))
                                     if len(self.readout_times) > 1 else 0.0)
        sample = None
        times = self.readout_times
        time_us = self.inspect_time_us
        if time_us is not None and len(times) and math.isfinite(time_us):
            index = int(np.searchsorted(times, time_us))
            candidates = [i for i in (index-1, index) if 0 <= i < len(times)]
            index = min(candidates, key=lambda i: abs(times[i]-time_us))
            # Do not invent a voltage within missing data or outside the
            # acquired window. Show the selected sample's timestamp too.
            tolerance = max(1e-9, self.readout_interval*.51)
            if abs(times[index]-time_us) <= tolerance:
                sample = frame.samples[index]
                self.inspect_line.setValue(times[index])
                for label in self.channel_values:
                    label.setToolTip('Sample at t = %.3f µs from trigger' % times[index])
        self.inspect_line.setVisible(sample is not None)
        for label, (_, source, _, _) in zip(self.channel_values, self.channels):
            key = source.currentData()
            value = signal_value(sample, key) if sample is not None else math.nan
            unit = SIGNALS[key][1]
            text = ('%d' % value if unit == 'logic' else '%.3f %s' % (value, unit)) if math.isfinite(value) else '—'
            label.setText(text)
            if sample is None:
                label.setToolTip('No recorded sample at cursor')

    def auto_scale(self):
        if self.frame is None:
            return
        for enabled, source, gain, position in self.channels:
            if not enabled.isChecked():
                continue
            values = [signal_value(s, source.currentData()) for s in self.frame.samples]
            values = [v for v in values if math.isfinite(v)]
            if not values:
                continue
            lo, hi = min(values), max(values)
            scale = max(.01, (hi - lo) / 6, abs((lo + hi) / 2) / 20)
            gain.blockSignals(True); position.blockSignals(True)
            gain.setValue(scale)
            position.setValue(-(lo + hi) / (2 * gain.value()))
            gain.blockSignals(False); position.blockSignals(False)
        self.render()

    def save_csv(self, path=None):
        if self.frame is None:
            self.notice.setText('Acquire a waveform before saving.')
            return
        if not isinstance(path, (str, os.PathLike)):
            path, _ = QFileDialog.getSaveFileName(self, 'Save waveform', 'am32-scope.csv', 'CSV (*.csv)')
        if path:
            try:
                channels = [dict(enabled=e.isChecked(), source=s.currentData(),
                                 units_per_div=g.value(), position_div=p.value())
                            for e, s, g, p in self.channels]
                self.frame.save(path, dict(self.frame_metadata, scope_setup=self.setup(),
                                          channels=channels, cursor_us=[c.value() for c in self.cursors]))
                self.notice.setText('Saved ' + str(path))
            except OSError as ex:
                self.notice.setText('Save failed: ' + str(ex))

    def save_png(self, path=None):
        if not isinstance(path, (str, os.PathLike)):
            path, _ = QFileDialog.getSaveFileName(self, 'Save screen', 'am32-scope.png', 'PNG (*.png)')
        if path and not self.grab().save(str(path)):
            self.notice.setText('Could not save ' + str(path))

    def closeEvent(self, event):
        self.capture.stop()
        self.on_close()
        event.accept()

#!/usr/bin/env python3
"""Exercise the actual GUI demag bench and export its scope capture.

AM32_ROOT=/path/to/AM32 python3 SITL/demag_scope_test.py --outdir /tmp/demag
The default recipe checks the steady full-duty waveform at approximately 50 A.
For failure tests choose --benchmark demag_full_overload --expect-masked.
Omit --expect-masked when a firmware fix should leave the fault trigger waiting.
Use --exe dist/am32-sitl-gui.exe to test the packaged Windows GUI and firmware.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--outdir', type=Path, required=True)
    binaries = ap.add_mutually_exclusive_group()
    binaries.add_argument('--sitl')
    binaries.add_argument('--exe', type=Path, help='test a packaged GUI with its bundled firmware')
    from sitl_benchmarks import registry
    recipes = registry()
    ap.add_argument('--benchmark', choices=recipes, default='demag_full_duty')
    ap.add_argument('--expect-masked', action='store_true')
    ap.add_argument('--check-controls', action='store_true',
                    help='also exercise Stop and manual throttle cancellation before the capture')
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    from gui_ci_test import free_control_port
    control = free_control_port()
    ports = [free_port(socket.SOCK_DGRAM) for _ in range(2)]
    while ports[0] == ports[1]:
        ports[1] = free_port(socket.SOCK_DGRAM)
    env = dict(os.environ)
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    if args.sitl:
        env['AM32_SITL'] = os.path.abspath(args.sitl)
    path = Path(__file__).with_name('sitl_gui.py')
    launcher = [sys.executable, str(path)]
    if args.exe:
        launcher = [str(args.exe.resolve())]
        env['LOCALAPPDATA'] = str(args.outdir.resolve()/'appdata')
        env.pop('AM32_SITL', None)
        # Verify the packaged runtime without relying on installed Cygwin DLLs.
        env['PATH'] = os.pathsep.join(p for p in env.get('PATH', '').split(os.pathsep)
                                      if 'cygwin' not in p.lower())
    with (args.outdir/'gui.log').open('w') as log:
        proc = subprocess.Popen(launcher + ['--can-uri', 'mcast:8',
            '--port', str(ports[0]), '--state-port', str(ports[1]),
            '--control-port', str(control)], stdout=log, stderr=log, env=env)
        def command(text):
            with socket.create_connection(('127.0.0.1', control), timeout=5) as s:
                s.sendall((text+'\n').encode())
                # Quit has no reply; wait for normal process cleanup below.
                if text == 'quit':
                    return []
                response = []
                with s.makefile() as f:
                    for line in f:
                        line = line.rstrip()
                        if line.startswith('ERR'):
                            raise RuntimeError(line)
                        if line.startswith('OK'):
                            return response
                        response.append(line)
                raise RuntimeError('GUI disconnected')
        try:
            deadline = time.monotonic()+(120 if args.exe else 30)
            while True:
                try:
                    command('scope_status')
                    break
                except OSError:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('GUI failed to start; see gui.log')
                    time.sleep(.2)
            initial = command('benchmark_status')
            initial_info = json.loads(next(r[len('STATUS scope: '):] for r in initial
                                          if r.startswith('STATUS scope: ')))
            assert initial_info['benchmark'] is None and not initial_info['bench_active'], initial_info
            try:
                command('benchmark_start')
            except RuntimeError as ex:
                assert 'select a benchmark first' in str(ex)
            else:
                raise AssertionError('None must not start a benchmark')
            command('benchmark '+args.benchmark)
            if args.check_controls:
                def status():
                    response = command('benchmark_status')
                    return json.loads(next(r[len('STATUS scope: '):] for r in response
                                           if r.startswith('STATUS scope: ')))
                command('benchmark_start')
                limit = time.monotonic()+15
                while status()['throttle_command'] == 0:
                    if time.monotonic() > limit:
                        raise RuntimeError('benchmark never advanced from startup')
                    time.sleep(.1)
                try:
                    command('benchmark None')
                except RuntimeError as ex:
                    assert 'stop the active benchmark' in str(ex)
                else:
                    raise AssertionError('selection must stay fixed during a benchmark')
                command('benchmark_stop')
                time.sleep(.7)
                stopped = status()
                assert not stopped['bench_active'] and not stopped['bench_complete'], stopped
                assert stopped['throttle_command'] == 0, stopped
                command('benchmark None')
                assert status()['benchmark'] is None
                command('benchmark '+args.benchmark)
                command('benchmark_start')
                command('ds_value 150')
                time.sleep(.2)
                manual = status()
                assert not manual['bench_active'] and manual['throttle_command'] == 150, manual
                command('zero')
            command('benchmark_start')
            deadline = time.monotonic()+100
            while True:
                response = command('scope_status')
                info = json.loads(next(r[len('STATUS scope: '):] for r in response
                                       if r.startswith('STATUS scope: ')))
                if not info['bench_active']:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('bench did not finish')
                time.sleep(.2)
            if not info.get('bench_complete') or 'desync_count' not in info:
                raise RuntimeError('bench did not complete with scope-capable firmware: '+str(info))
            if info['points']:
                csv_path = args.outdir/'demag.csv'
                command('scope_save '+str(csv_path))
                command('scope_snap '+str(args.outdir/'demag.png'))
                with csv_path.open() as source:
                    rows = list(csv.DictReader(source))
                near = min(rows, key=lambda r: abs(float(r['relative_us'])))
                recipe = recipes[args.benchmark]
                phase = 'ABC'[info['trigger_phase']]
                if recipe.trigger == 'Masked zero crossing':
                    assert near['mode_'+phase] == '0' and near['diode_'+phase] != '0', near
                    index = rows.index(near)
                    assert index > 0
                    before = float(rows[index-1]['e'+phase])
                    after = float(near['e'+phase])
                    assert before*after <= 0 and before != after, near
                else:
                    assert near['mode_'+phase] == '0', near
                    assert info['desync_count'] == 0, info
                assert info['trigger'] == recipe.trigger, info
                assert info['sample_us'] <= .51, info
                assert info['gaps'] == 0, info
                if recipe.full_duty:
                    assert float(near['duty']) >= 99.9, near
                    if recipe.trigger == 'Commutation':
                        assert min(float(r['duty']) for r in rows) >= 99.9
                        # No PWM-off notches during the driven-high interval.
                        high = [r for r in rows if r['mode_'+phase] == '2']
                        assert len(high) > 20
                        assert sum(abs(float(r['v'+phase])-float(r['bus'])) < .1
                                   for r in high)/len(high) > .98
                if args.benchmark == 'demag_full_duty':
                    stats = info['measurements']
                    assert 45 <= stats['bus_current_a'] <= 55, stats
                    pulses = stats['demag_pulses']
                    assert pulses and 12e-6 <= max(p[2] for p in pulses) <= 20e-6, stats
                if args.benchmark == 'demag_pwm':
                    high = [r for r in rows if r['mode_'+phase] == '2']
                    assert any(float(r['v'+phase]) < 1 for r in high)
                    assert any(float(r['v'+phase]) > float(r['bus'])-.1 for r in high)
                if args.expect_masked:
                    assert recipe.trigger == 'Masked zero crossing' and info['desync_count'] > 0, info
            elif args.expect_masked or recipes[args.benchmark].trigger != 'Masked zero crossing':
                raise AssertionError('benchmark did not capture its expected waveform: '+str(info))
            (args.outdir/'result.json').write_text(json.dumps(info, indent=2)+'\n')
            print(json.dumps(info, indent=2))
        finally:
            try:
                command('quit')
            except (OSError, RuntimeError):
                proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.returncode:
            raise RuntimeError('GUI exit %s; see gui.log' % proc.returncode)


if __name__ == '__main__':
    main()

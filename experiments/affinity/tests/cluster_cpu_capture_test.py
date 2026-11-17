#!/usr/bin/env python3
"""Counter fixtures and short Linux subprocess checks; no target workload."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'cluster_cpu_capture.py'
SPEC = importlib.util.spec_from_file_location('cluster_cpu_capture', TOOL)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)
MS = 1_000_000


def proc_line(pid=42, comm='worker ) x', user=10, system=5, start=123):
    fields = ['S'] + ['0'] * 19
    fields[11], fields[12], fields[19] = str(user), str(system), str(start)
    return '%d (%s) %s' % (pid, comm, ' '.join(fields))


def snapshot(at, user=0, idle=0, iowait=0, steal=0, guest=0, pid=42, start=123):
    stat = 'cpu0 %d 0 0 %d %d 0 0 %d %d 0\ncpu1 %d 0 0 %d %d 0 0 %d %d 0\n' % (
        user, idle, iowait, steal, guest, user, idle, iowait, steal, guest)
    return dict(at_ns=at, process_at_ns=at, cpus=capture.parse_cpu_stat(stat),
                process=capture.parse_process_stat(proc_line(pid=pid, user=user, system=0, start=start)),
                cpu_error='', process_error='', cpu_read_start_ns=at-100,
                cpu_read_end_ns=at+100, collection_start_ns=at-200,
                collection_end_ns=at+300, collector_cpu_start_ns=at//100,
                collector_cpu_end_ns=at//100+500, scheduled_ns=at-1000)


class ParsingTest(unittest.TestCase):
    def test_partitions_expansion_and_reject_ambiguity(self):
        self.assertEqual(capture.parse_partitions('0-2,4;6,8-9'), [[0,1,2,4], [6,8,9]])
        for text in ('', '0;', '0;0', '2-1', '0,0', '-1', 'a', '0-2-3'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                capture.parse_partitions(text)

    def test_proc_stat_ignores_aggregate_and_does_not_double_guest(self):
        parsed = capture.parse_cpu_stat('cpu 999 0 0 0 0 0 0 0\ncpu0 10 2 3 40 5 6 7 8 9 1\nintr 0')
        self.assertEqual(set(parsed), {0})
        self.assertEqual(sum(parsed[0][k] for k in capture.BUSY), 28)
        for text in ('cpu 1 2 3', 'cpu0 1 2', 'cpu0 -1 0 0 0 0 0 0 0'):
            with self.assertRaises(ValueError):
                capture.parse_cpu_stat(text)

    def test_process_stat_comm_and_starttime(self):
        parsed = capture.parse_process_stat(proc_line())
        self.assertEqual(parsed, {'pid':42, 'state':'S', 'utime':10, 'stime':5, 'starttime':123})
        with self.assertRaises(ValueError):
            capture.parse_process_stat('42 (broken) S 0')

    def test_snapshot_read_failures_are_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            sample = capture.read_snapshot(1, proc_root=Path(root))
        self.assertTrue(sample['cpu_error'])
        self.assertTrue(sample['process_error'])
        self.assertIsNone(sample['process'])
        self.assertGreaterEqual(sample['collection_end_ns'], sample['collection_start_ns'])


class DifferenceTest(unittest.TestCase):
    def test_actual_window_busy_and_separate_idle_iowait_steal(self):
        before = snapshot(0)
        after = snapshot(200*MS, user=10, idle=5, iowait=3, steal=2, guest=9)
        row = capture.difference(before, after, [[0,1]], 100)[0]
        self.assertTrue(row['valid'])
        self.assertAlmostEqual(row['busy_cpu_equivalent'], 1)
        self.assertAlmostEqual(row['util_percent'], 50)
        self.assertAlmostEqual(row['idle_cpu_equivalent'], .5)
        self.assertAlmostEqual(row['iowait_cpu_equivalent'], .3)
        self.assertAlmostEqual(row['steal_cpu_equivalent'], .2)
        self.assertAlmostEqual(row['total_cpu_equivalent'], 2)
        self.assertAlmostEqual(row['process_cpu_equivalent'], .5)

    def test_no_cap_hides_tick_quantization_or_bad_measurement(self):
        row = capture.difference(snapshot(0), snapshot(20*MS, user=3), [[0]], 100)[0]
        self.assertTrue(row['valid'])
        self.assertEqual(row['util_percent'], 150)
        self.assertTrue(row['util_outside_nominal_range'])

    def test_counter_decrease_invalidates_only_affected_cluster(self):
        before, after = snapshot(0, user=10), snapshot(100*MS, user=12)
        after['cpus'][0]['iowait'] = -1
        rows = capture.difference(before, after, [[0],[1]], 100)
        self.assertFalse(rows[0]['valid'])
        self.assertIn('counter_decreased:0/iowait', rows[0]['invalid_reason'])
        self.assertIsNone(rows[0]['util_percent'])
        self.assertTrue(rows[1]['valid'])
        before, after = snapshot(0, guest=10), snapshot(100*MS, guest=9)
        self.assertIn('counter_decreased:0/guest', capture.difference(before, after, [[0]], 100)[0]['invalid_reason'])

    def test_offline_missing_cpu_and_bad_snapshot_are_unavailable(self):
        before, after = snapshot(0), snapshot(100*MS)
        del after['cpus'][1]
        row = capture.difference(before, after, [[0,1]], 100)[0]
        self.assertEqual(row['invalid_reason'], 'missing_cpu:1')
        self.assertIsNone(row['busy_cpu_seconds'])
        after['cpu_error'] = 'read failed'
        self.assertEqual(capture.difference(before, after, [[0]], 100)[0]['invalid_reason'], 'cpu_snapshot_error')

    def test_process_reuse_rollback_missing_endpoint_not_zero(self):
        before, after = snapshot(0, user=4), snapshot(100*MS, user=7, start=456)
        self.assertEqual(capture.process_difference(before, after, 100)['process_invalid_reason'], 'process_identity_changed')
        after['process']['starttime'] = 123
        after['process']['utime'] = 2
        self.assertEqual(capture.process_difference(before, after, 100)['process_invalid_reason'], 'process_counter_decreased')
        after['process'] = None
        result = capture.process_difference(before, after, 100)
        self.assertEqual(result['process_invalid_reason'], 'missing_process_endpoint')
        self.assertIsNone(result['process_cpu_seconds'])

    def test_zero_or_reverse_wall_rejected(self):
        row = capture.difference(snapshot(MS), snapshot(0), [[0]], 100)[0]
        self.assertEqual(row['invalid_reason'], 'nonpositive_window')
        self.assertFalse(row['process_valid'])

    def test_formal_scope_keeps_complete_windows_and_invalid_coverage(self):
        # Request scope is [100,400] ms; reject partial edge windows.
        snapshots = [snapshot(t*MS, user=t//10) for t in (50,150,250,350,450)]
        rows = []
        for i in range(4):
            rows.extend(capture.difference(snapshots[i], snapshots[i+1], [[0],[1]], 100, i))
        rows[4]['valid'] = False
        rows[4]['invalid_reason'] = 'fixture_invalid'
        result = capture.summarize(rows, [[0],[1]], 0, .1, .3)
        self.assertEqual(result['clusters'][0]['complete_windows'], 2)
        self.assertEqual(result['clusters'][0]['valid_windows'], 1)
        self.assertAlmostEqual(result['clusters'][0]['valid_coverage_percent'], 100/3)
        self.assertEqual(result['clusters'][0]['invalid_reasons'], {'fixture_invalid':1})
        self.assertAlmostEqual(result['clusters'][1]['valid_coverage_percent'], 200/3)
        self.assertEqual(result['process']['valid_windows'], 2)  # not counted twice for two clusters
        self.assertAlmostEqual(result['process']['cpu_seconds'], .2)
        self.assertFalse(capture.summarize(rows, [[0],[1]])['available'])

    def test_scope_read_boundary_equal_is_included_and_weighted_mean(self):
        rows = capture.difference(snapshot(100*MS), snapshot(200*MS, user=10), [[0]], 100)
        rows += capture.difference(snapshot(200*MS, user=10), snapshot(400*MS, user=10), [[0]], 100, 1)
        self.assertEqual(capture.summarize(rows, [[0]], 0, .1, .3)['clusters'][0]['complete_windows'], 0)
        rows[0]['start_read_begin_ns'] = 100*MS
        rows[1]['end_read_end_ns'] = 400*MS
        result = capture.summarize(rows, [[0]], 0, .1, .3)['clusters'][0]
        self.assertEqual(result['complete_windows'], 2)
        self.assertAlmostEqual(result['valid_coverage_percent'], 100)
        self.assertAlmostEqual(result['util_time_weighted_mean_percent'], 100/3)
        self.assertEqual(result['window_util_p99_percent'], 100)


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/proc/stat').exists(), 'Linux proc required')
class ShortProcessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cluster-cpu-capture-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cpu = min(os.sched_getaffinity(0))

    def command(self, code, *extra, name='capture'):
        return [sys.executable, str(TOOL), '--partitions', str(self.cpu), '--interval-ms', '50',
                '--output', str(self.root/name), '--timeout-seconds', '8', *extra,
                '--', sys.executable, '-c', code]

    def run_capture(self, code, *extra, name='capture'):
        result = subprocess.run(self.command(code, *extra, name=name), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=12)
        metadata = json.loads((self.root/name/'capture.json').read_text())
        return result, metadata

    def test_real_cpu_process_scope_alignment_and_artifacts(self):
        summary = self.root/'summary.json'
        code = '''import json, os, time
os.sched_setaffinity(0, {%d})
open(%r, 'w').write(json.dumps({'origin_ns': time.monotonic_ns()}))
time.sleep(.08)
end = time.process_time() + .18
value = 1
while time.process_time() < end:
    value = (value * 1664525 + 1013904223) %% 4294967296
time.sleep(.12)
print('child-complete', value)
''' % (self.cpu, str(summary))
        result, meta = self.run_capture(code, '--summary-path', str(summary), '--warmup-seconds', '0', '--window-seconds', '.35')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(meta['child_returncode'], 0)
        self.assertGreater(meta['window_count'], 3)
        self.assertGreater(meta['formal_scope']['process']['cpu_seconds'], .02)
        self.assertGreater(meta['formal_scope']['clusters'][0]['valid_seconds'], .15)
        self.assertGreater(meta['collector_cpu_seconds'], 0)
        self.assertGreater(meta['collector_sampling_cpu_seconds'], 0)
        self.assertIn('CPU_SERIES_BEGIN', result.stdout)
        self.assertIn('child-complete', (self.root/'capture/run.log').read_text())
        self.assertTrue((self.root/'capture/cpu_windows.csv').exists())
        self.assertGreaterEqual(meta['missing_process_windows'], 1)

    def test_child_failure_keeps_exit_status_and_log(self):
        result, meta = self.run_capture("import time; print('expected failure'); time.sleep(.06); raise SystemExit(7)")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(meta['child_returncode'], 7)
        self.assertEqual(meta['status'], 'partial_or_failed')
        self.assertIn('expected failure', (self.root/'capture/run.log').read_text())

    def test_refuses_existing_directory_before_launch(self):
        directory = self.root/'capture'
        directory.mkdir()
        (directory/'keep').write_text('retain')
        result = subprocess.run(self.command("raise RuntimeError('must not launch')"), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((directory/'keep').read_text(), 'retain')
        self.assertFalse((directory/'run.log').exists())

    def test_timeout_terminates_process_group_and_retains_partial(self):
        pidfile = self.root/'descendant.pid'
        nested = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
        code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',%r]); open(%r,'w').write(str(p.pid)); time.sleep(60)" % (nested, str(pidfile))
        result, meta = self.run_capture(code, '--timeout-seconds', '.3')
        self.assertEqual(result.returncode, 124)
        self.assertEqual(meta['exit_reason'], 'timeout')
        self.assertIsNotNone(meta['child_returncode'])
        self.assertGreater(meta['sample_count'], 1)
        pid = int(pidfile.read_text())
        # A killed orphan may remain a zombie until its new parent reaps it.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = capture.parse_process_stat(Path('/proc/%d/stat' % pid).read_text())['state']
            except FileNotFoundError:
                break
            if state == 'Z':
                break
            time.sleep(.02)
        else:
            self.fail('descendant still running after timeout process-group cleanup')

    def test_interrupt_retains_partial(self):
        child = subprocess.Popen(self.command('import time; time.sleep(60)'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(.18)
            child.send_signal(signal.SIGINT)
            stdout, stderr = child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(child.returncode, 130, stderr + stdout)
        meta = json.loads((self.root/'capture/capture.json').read_text())
        self.assertEqual(meta['exit_reason'], 'signal_2')
        self.assertEqual(meta['status'], 'partial_or_failed')

    def test_missing_summary_is_failure_not_fabricated_window(self):
        result, meta = self.run_capture('import time; time.sleep(.08)', '--summary-path', str(self.root/'missing.json'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(meta['child_returncode'], 0)
        self.assertFalse(meta['formal_scope']['available'])
        self.assertTrue(meta['summary_error'])

    def test_missing_executable_retains_launch_failure(self):
        command = self.command('pass')
        command = command[:command.index('--')+1] + [str(self.root/'does-not-exist')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        meta = json.loads((self.root/'capture/capture.json').read_text())
        self.assertEqual(meta['exit_reason'], 'launch_failed')
        self.assertIsNone(meta['child_returncode'])
        self.assertTrue((self.root/'capture/cpu_windows.csv').exists())


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Guard DAG cohort, failure visibility and measurement-scope boundaries."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]/'scripts'))
import dag_terminal_report as report


def write_csv(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class DagTerminalReportTest(unittest.TestCase):
    def fixture(self, root, count=1):
        phase = root/'s4-baseline'
        phase.mkdir()
        manifest = []
        for seed in range(count):
            path = phase/f'r{seed}-home'
            path.mkdir()
            origin = 200_000_000
            meta = {'schema_version': 3, 'phase': 'completed', 'backend': 'business',
                    'arrival_origin_ns': origin, 'allowed_cpus': [0, 1],
                    'config': {'duration': 4, 'seed': seed, 'pools': 2, 'threads_per_pool': 2,
                               'affinity': 'cluster', 'policy': 'home', 'metric': 'queue', 'queue': 'default',
                               'task_log': True, 'graph': 'waves', 'wave_widths': '1', 'wave_depths': '1',
                               'cpu_us': 1000, 'work_mode': 'cpu', 'arrival': 'poisson'},
                    'groups': [{'group': i, 'effective_cpus': [i]} for i in range(2)]}
            summary = {'offered': 3, 'accepted': 3, 'completed': 3, 'rejected': 0, 'failed': 0,
                       'censored': 0, 'task_rows': 3, 'conservation_ok': True,
                       'io_failed': False, 'drain_timed_out': False}
            (path/'metadata.json').write_text(json.dumps(meta))
            (path/'summary.json').write_text(json.dumps(summary))
            requests, tasks = [], []
            for rid, planned, finish in [(0, .5, .8), (1, 1.5, 4.0), (2, 2.5, 2.8)]:
                n = lambda seconds: origin+round(seconds*1e9)
                requests.append({'request_id': rid, 'scheduled_ns': n(planned), 'offered_ns': n(planned+.001),
                                 'finish_ns': n(finish), 'status': 'completed', 'planned_cpu_us': 1000})
                tasks.append({'request_id': rid, 'node_id': 0, 'stage': 0, 'work_target_us': 1000,
                              'predecessors': '', 'kind': 'operator', 'rpc_delay_target_us': 0,
                              'submit_ns': n(planned+.01), 'start_ns': n(planned+.02),
                              'finish_ns': n(finish-.01), 'status': 'completed', 'cpu_us': 1000,
                              'migrated': 0, 'cross_numa': 0, 'queue_before': 0, 'wave': 0, 'is_fanout': 1})
            write_csv(path/'requests.csv', requests)
            write_csv(path/'tasks.csv', tasks)
            write_csv(path/'pools.csv', [
                {'time_ns': origin+seconds*1_000_000_000, 'group': group,
                 'queue_depth': 2*seconds+group-1, 'pending': 2*seconds+group,
                 'inflight': 2*seconds+group+1}
                for seconds in (1, 2) for group in (0, 1)])
            write_csv(path/'workers.csv', [{'group': group, 'tid': 10+group*2+i}
                                          for group in (0, 1) for i in (0, 1)])
            manifest.append({'name': 'home', 'repeat': seed, 'seed': seed, 'directory': str(path),
                             'returncode': 0, 'binary_sha256': 'hash', 'proc_interval_seconds': 0,
                             'command': ['demo', '--output', str(path), '--threads', '2']})
        (phase/'manifest.json').write_text(json.dumps(manifest))
        (root/'plan').mkdir()
        (root/'plan'/'plan.json').write_text(json.dumps({'phases': {'s4-baseline': {'runs': count}}}))
        (root/'plan'/'s4-baseline-contrasts.json').write_text(json.dumps({
            'stage': 'dag', 'window': {'start_s': 1, 'end_s': 3}}))
        return manifest

    def test_request_cohort_keeps_late_task_completion_and_cpu_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            lines = []
            self.assertTrue(report.phase_report(root, 's4-baseline', emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('recorded=1 planned=1 window=[1,3)', output)
            self.assertIn('planned_mean/p99=1400.000/2500.000', output)
            self.assertIn('TASK_COHORT statuses={"completed": 2}', output)
            self.assertIn('work_wall_mean/p99_ms=1370.000/2470.000', output)
            self.assertIn('logged_cohort_work_cpu_s=0.002 process_cpu_s=not_recorded', output)
            self.assertIn('queue_depth_sum_mean/p99=5.000/7.000', output)
            self.assertIn('pending_sum_mean/p99=7.000/9.000', output)
            self.assertIn('inflight_sum_mean/p99=9.000/11.000', output)
            self.assertIn('QUEUE_ZERO task_n=2 wait_gt1ms=2', output)

    def test_explicit_window_uses_arrival_origin_not_first_poisson_arrival(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            lines = []
            self.assertTrue(report.phase_report(root, 's4-baseline', start=0, end=.4, emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('source=explicit CLI bounds', output)
            self.assertIn('cohort={}', output)
            self.assertIn('planned_mean/p99=?/?', output)
            self.assertIn('logged_cohort_work_cpu_s=?', output)

    def test_failed_or_missing_run_does_not_hide_later_runs(self):
        for failure in ('exit', 'missing', 'invalid_task'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = self.fixture(root, 2)
                path = Path(rows[0]['directory'])
                if failure == 'exit':
                    rows[0]['returncode'] = 137
                    (root/'s4-baseline'/'manifest.json').write_text(json.dumps(rows))
                elif failure == 'missing':
                    (path/'summary.json').unlink()
                else:
                    tasks = report.read_csv(path/'tasks.csv')
                    tasks[1]['start_ns'] = '0'
                    write_csv(path/'tasks.csv', tasks)
                lines = []
                self.assertFalse(report.phase_report(root, 's4-baseline', emit=lines.append))
                output = '\n'.join(lines)
                self.assertIn('INCOMPLETE', output)
                self.assertIn('seed=1 rc=0', output)
                self.assertIn('REQUEST_LAT', output)

    def test_missing_or_invalid_window_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root/'plan'/'s4-baseline-contrasts.json').unlink()
            lines = []
            self.assertFalse(report.phase_report(root, 's4-baseline', emit=lines.append))
            self.assertIn('measurement window absent', '\n'.join(lines))
            for start, end in [(1, None), (-1, 3), (float('nan'), 3), (3, 1)]:
                with self.assertRaises(ValueError):
                    report.window(root, 's4-baseline', start=start, end=end)
            self.assertFalse(report.phase_report(root, 's4-baseline', start=1, end=5, emit=lines.append))
            self.assertIn('invalid measurement window', '\n'.join(lines))

    def test_pagination_keeps_global_failures_and_duplicates_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.fixture(root, 2)
            rows[1]['seed'] = rows[0]['seed']
            rows[1]['returncode'] = 137
            (root/'s4-baseline'/'manifest.json').write_text(json.dumps(rows))
            lines = []
            self.assertFalse(report.phase_report(root, 's4-baseline', limit=1, emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('duplicate case/seed', output)
            self.assertIn('nonzero_or_missing_rc=1', output)
            self.assertIn('unshown artifacts have not been checked', output)
            self.assertIn('NEXT: --offset 1 --limit 1', output)

    def test_partial_pool_snapshots_are_not_summed_as_full_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.fixture(root)
            path = Path(rows[0]['directory'])/'pools.csv'
            samples = report.read_csv(path)
            write_csv(path, samples[1:])
            lines = []
            self.assertFalse(report.phase_report(root, 's4-baseline', emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('incomplete_group_sets=1', output)
            self.assertIn('queue_depth_sum_mean/p99=7.000/7.000 known_times=1', output)

    def test_unknown_queue_is_not_treated_as_zero_and_bad_cpu_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.fixture(root)
            path = Path(rows[0]['directory'])/'tasks.csv'
            tasks = report.read_csv(path)
            tasks[1]['queue_before'] = '-1'
            write_csv(path, tasks)
            lines = []
            self.assertTrue(report.phase_report(root, 's4-baseline', emit=lines.append))
            self.assertIn('queue_unknown_task_n=1', '\n'.join(lines))
            tasks[1]['cpu_us'] = 'nan'
            write_csv(path, tasks)
            self.assertFalse(report.phase_report(root, 's4-baseline', emit=lines.append))
            self.assertIn('nonfinite cpu_us', '\n'.join(lines))

    def test_relocated_artifacts_and_partial_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.fixture(root, 2)
            rows[0]['directory'] = '/another/host/r0-home'
            rows.pop()
            (root/'s4-baseline'/'manifest.json').write_text(json.dumps(rows))
            lines = []
            self.assertFalse(report.phase_report(root, 's4-baseline', emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('recorded=1 planned=2', output)
            self.assertIn('REQUEST_LAT', output)


if __name__ == '__main__':
    unittest.main()

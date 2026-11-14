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
                              'migrated': 0, 'cross_numa': 0, 'queue_before': 0, 'wave': 0, 'is_fanout': 1,
                              'depth': 0, 'chosen_group': 0, 'move_reason': 'home'})
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
            self.assertIn('ROUTING_TRANSITION initial_fork tasks=2 migrated_source=0 cross_numa=0', output)
            self.assertIn('ROUTING_CONFIG balance_forks=unknown candidate_scope=unknown estimate_us=unknown', output)

    def test_transition_counts_share_cohort_and_keep_unknown_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.fixture(root)
            path = Path(rows[0]['directory'])
            original = report.read_csv(path/'tasks.csv')
            templates = [
                {'kind': 'operator', 'stage': '0', 'wave': '0', 'depth': '0'},
                {'kind': 'operator', 'stage': '0', 'wave': '1', 'depth': '0'},
                {'kind': 'operator', 'stage': '0', 'wave': '1', 'depth': '1'},
                {'kind': 'join', 'stage': '0', 'wave': '1', 'depth': '0'},
                {'kind': 'operator', 'stage': '1', 'wave': '0', 'depth': '0'},
                {'kind': '', 'stage': '0', 'wave': '0', 'depth': '0'},
            ]
            selected = []
            for index, descriptors in enumerate(templates):
                task = dict(original[1], **descriptors, node_id=str(index),
                            chosen_group=str(index % 2), migrated=str(index % 2),
                            cross_numa='0', move_reason='return_home' if index == 3 else 'preferred')
                task['start_ns'] = str(int(task['start_ns'])+index*1_000_000)
                selected.append(task)
            # Exclude both an early request and a later request, and keep selected
            # stages finishing beyond end=2. A noncompleted selected stage is not
            # a routing outcome, even though it remains visible in status totals.
            incomplete = dict(original[1], node_id='6', status='censored')
            write_csv(path/'tasks.csv', [original[0], *selected, incomplete, original[2]])
            meta, summary = report.read_json(path/'metadata.json'), report.read_json(path/'summary.json')
            details = report.details(path, meta, summary, 1, 2)
            routing = details['routing']
            self.assertEqual(details['completed'], 6)
            self.assertEqual(details['statuses'], {'completed': 6, 'censored': 1})
            self.assertEqual(set(routing), set(report.TRANSITIONS) | {'unknown'})
            self.assertEqual(sum(row['tasks'] for row in routing.values()), details['completed'])
            self.assertTrue(all(row['tasks'] == 1 for row in routing.values()))
            self.assertTrue(all(sum(row['chosen_groups'].values()) == row['tasks'] for row in routing.values()))
            self.assertTrue(all(sum(row['move_reasons'].values()) == row['tasks'] for row in routing.values()))
            self.assertEqual(routing['join']['return_home_reason']['count'], 1)
            self.assertEqual(routing['join']['migrated_source']['count'], 1)
            self.assertEqual(routing['rpc_resume']['waits'], [14])
            self.assertEqual(details['first_waits'], [10])
            self.assertEqual(sum(row['migrated_source']['count'] for row in routing.values()), 3)

    def test_absent_flags_and_transition_fields_are_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = self.fixture(root)
            path = Path(rows[0]['directory'])
            tasks = report.read_csv(path/'tasks.csv')
            for key in ('migrated', 'cross_numa', 'move_reason', 'chosen_group', 'depth'):
                tasks[1][key] = ''
            write_csv(path/'tasks.csv', tasks)
            metadata = report.read_json(path/'metadata.json')
            metadata['config'].update(balance_forks='initial', candidate_scope='all', estimate_us=0,
                                      min_gain_us=250, relative_gain=.2, cross_numa_penalty_us=100)
            (path/'metadata.json').write_text(json.dumps(metadata))
            lines = []
            self.assertTrue(report.phase_report(root, 's4-baseline', emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('ROUTING_CONFIG balance_forks=initial candidate_scope=all estimate_us=0 '
                          'min_gain_us=250 relative_gain=0.2 cross_numa_penalty_us=100', output)
            self.assertIn('ROUTING_TRANSITION unknown tasks=1 migrated_source=unknown[known=0,true=0,missing=1]', output)
            self.assertIn('cross_numa=unknown[known=0,true=0,missing=1]', output)
            self.assertIn('return_home_reason=unknown[known=0,true=0,missing=1]', output)
            self.assertIn('chosen_groups={"unknown": 1} move_reasons={"unknown": 1}', output)
            self.assertIn('migrated_source=unknown[known=1,true=0,missing=1]', output)
            tasks[2]['migrated'] = '2'
            write_csv(path/'tasks.csv', tasks)
            self.assertFalse(report.phase_report(root, 's4-baseline', emit=lines.append))
            self.assertIn('invalid migrated flag', '\n'.join(lines))

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

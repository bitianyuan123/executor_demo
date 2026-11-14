#!/usr/bin/env python3
"""Fixtures test inference boundaries, not synthetic performance claims."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('compare_runs', Path(__file__).parents[1] / 'scripts/compare_runs.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class ComparisonTest(unittest.TestCase):
    def fixture(self, root, name='base', seed=1, shift=0, failed=False):
        path = root / f'{name}-{seed}'
        path.mkdir()
        meta = {'stage': 3, 'duration_seconds': 4, 'allowed_cpus': [0, 1], 'same_cpu_union': True}
        summary = {'planned_tasks': 4, 'completed_tasks': 3 if failed else 4, 'conservation_ok': True}
        (path / 'metadata.json').write_text(json.dumps(meta))
        (path / 'summary.json').write_text(json.dumps(summary))
        rows = ['task,burst,pool,status,target_cpu_us,planned_ns,submit_ns,start_ns,end_ns']
        for i in range(4):
            begin = i * 1_000_000_000
            # The last measured cohort member finishes outside the window.
            end = begin + (2_000_000_000 if i == 2 else 10_000_000) + shift
            state = 'canceled' if failed and i == 3 else 'completed'
            rows.append(f'{i},{i},0,{state},1000,{begin},{begin + 1},{begin + 2},{end}')
        (path / 'tasks.csv').write_text('\n'.join(rows) + '\n')
        (path / 'pools.csv').write_text('time_ns,pool,native_queue\n1000000000,0,1\n1500000000,0,2\n2000000000,0,3\n2500000000,0,4\n')
        return {'case': name, 'seed': seed, 'repeat': seed, 'path': path, 'returncode': 0,
                'binary_sha256': 'fixedhash', 'command': ['binary', '--output', str(path), '--threads', '1']}

    def test_cohort_keeps_slow_completions_outside_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.fixture(Path(tmp))
            result = c.summarize(row, 'independent', 1, 3)
            self.assertEqual(result['completed'], 2)
            self.assertAlmostEqual(result['metrics']['planned_latency_ms_mean'], 1005)
            self.assertEqual(result['diagnostics']['completed_outstanding_at_start_mid_end'], [0, 0, 1])
            self.assertIn('later_cohort_latency_growth: inspect overload_or_nonstationarity', result['warnings'])

    def test_failed_tasks_remain_in_validity_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = c.summarize(self.fixture(Path(tmp), 'a'), 'independent', 0, 4)
            b = c.summarize(self.fixture(Path(tmp), 'b', failed=True), 'independent', 0, 4)
            errors, _ = c.contract_errors(a, b, {})
            self.assertTrue(any('incomplete/rejected' in error for error in errors))

    def test_undeclared_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = c.summarize(self.fixture(Path(tmp), 'a'), 'independent', 0, 4)
            b = c.summarize(self.fixture(Path(tmp), 'b'), 'independent', 0, 4)
            b['options']['threads'] = 16
            self.assertTrue(c.contract_errors(a, b, {})[0])
            self.assertEqual(c.contract_errors(a, b, {'allowed_changes': ['threads']})[0], [])

    def test_workload_contract_detects_hidden_budget_and_schedule_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = c.summarize(self.fixture(Path(tmp), 'a'), 'independent', 0, 4)
            b = c.summarize(self.fixture(Path(tmp), 'b'), 'independent', 0, 4)
            b['workload']['service_sha256'] = 'different'
            b['workload']['arrival_sha256'] = 'different'
            self.assertEqual(len(c.contract_errors(a, b, {})[0]), 2)
            contract = {'workload': {'service': 'equal_total', 'arrival': 'count'}}
            self.assertEqual(c.contract_errors(a, b, contract)[0], [])
            b['workload']['total_target_cpu_us'] *= 2
            self.assertTrue(c.contract_errors(a, b, contract)[0])

    def test_whole_pair_resampling_and_small_sample_gate(self):
        result = c.paired_estimate([-2] * 6, 42, 1000, 5)
        self.assertEqual(result['ci95'], [-2, -2])
        self.assertIsNone(c.paired_estimate([-2, -2], 42, 1000, 5)['ci95'])
        self.assertEqual(result['pairs'], 6)

    def test_integration_small_p99_has_no_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for seed in range(5):
                for name, shift in [('base', 0), ('candidate', 1_000_000)]:
                    row = self.fixture(root, name, seed, shift)
                    row['path'] = str(row['path'])
                    rows.append(row)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'runs': rows}))
            spec = {'stage': 'independent', 'window': {'start_s': 1, 'end_s': 3}, 'bootstrap_draws': 1000,
                    'comparisons': [{'baseline': 'base', 'candidate': 'candidate'}]}
            result = c.compare(manifest, spec)['comparisons'][0]
            self.assertEqual(result['status'], 'paired_estimate')
            self.assertAlmostEqual(result['estimates']['planned_latency_ms_mean']['mean'], 1)
            self.assertEqual(result['estimates']['planned_latency_ms_mean']['ci95'], [1, 1])
            self.assertIsNone(result['estimates']['planned_latency_ms_p99']['ci95'])
            self.assertFalse(result['estimates']['planned_latency_ms_p99']['tail_sample_adequate'])
            self.assertIn('探索性', c.markdown(c.compare(manifest, spec)))

    def test_duplicate_seed_is_not_an_independent_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = self.fixture(root)
            row['path'] = str(row['path'])
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'runs': [row, row]}))
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                c.entries(manifest)

    def test_bad_window_or_truncated_data_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.fixture(Path(tmp))
            with self.assertRaisesRegex(ValueError, 'window'):
                c.summarize(row, 'independent', 3, 5)
            (row['path'] / 'tasks.csv').write_text('task,status\n')
            with self.assertRaisesRegex(ValueError, 'truncated'):
                c.summarize(row, 'independent', 0, 4)

    def test_negative_wait_and_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.fixture(Path(tmp))
            path = entry['path'] / 'tasks.csv'
            original = path.read_text()
            path.write_text(original.replace(',0,1,2,10000000', ',0,1,0,10000000'))
            with self.assertRaisesRegex(ValueError, 'callback'):
                c.summarize(entry, 'independent', 0, 4)
            path.write_text(original.replace('\n1,1,', '\n0,1,'))
            with self.assertRaisesRegex(ValueError, 'task IDs'):
                c.summarize(entry, 'independent', 0, 4)

    def test_manifest_seed_must_match_actual_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = self.fixture(Path(tmp))
            path = entry['path'] / 'metadata.json'
            meta = json.loads(path.read_text()); meta['seed'] = 999
            path.write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, 'seed'):
                c.summarize(entry, 'independent', 0, 4)

    def test_last_command_option_wins(self):
        self.assertEqual(c.options(['app', '--threads', '1', '--threads', '2', '--seed', '4']), {'threads': 2})
        with self.assertRaises(ValueError):
            c.options(['app', '--threads'])

    def test_slope_and_constant_values(self):
        self.assertEqual(c.slope([(1, 3), (2, 5), (3, 7)]), 2)
        self.assertEqual(c.slope([(1, 3), (2, 3), (3, 3)]), 0)
        self.assertIsNone(c.slope([(1, 3)]))

    def test_resource_union_mismatch_is_not_pure_affinity(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = c.summarize(self.fixture(Path(tmp), 'a'), 'independent', 0, 4)
            b = c.summarize(self.fixture(Path(tmp), 'b'), 'independent', 0, 4)
            b['effective_cpu_union'] = [0]
            self.assertTrue(any('worker CPU' in e for e in c.contract_errors(a, b, {})[0]))

    def test_dag_origin_and_completion_order_do_not_change_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for n, origin in enumerate([200_000_000, 300_000_000]):
                path = root / str(n)
                path.mkdir()
                meta = {'phase': 'completed', 'schema_version': 3, 'allowed_cpus': [0, 1],
                        'arrival_origin_ns': origin, 'config': {'duration': 4}}
                (path / 'metadata.json').write_text(json.dumps(meta))
                (path / 'summary.json').write_text(json.dumps({'offered': 2, 'completed': 2, 'task_rows': 2, 'conservation_ok': True}))
                requests = [f'{i},{origin + plan},{origin + plan + 10},{origin + plan + 1000000},completed,1000'
                            for i, plan in [(0, 500000000), (1, 1500000000)]]
                if n:
                    requests.reverse()
                (path / 'requests.csv').write_text('request_id,scheduled_ns,offered_ns,finish_ns,status,planned_cpu_us\n' + '\n'.join(requests) + '\n')
                (path / 'tasks.csv').write_text('request_id,node_id,stage,work_target_us,predecessors,kind,rpc_delay_target_us\n0,0,0,1000,,operator,0\n1,0,0,1000,,operator,0\n')
                (path / 'pools.csv').write_text('time_ns,group,queue_depth\n')
                entry = {'case': str(n), 'seed': 1, 'repeat': 1, 'path': path, 'returncode': 0,
                         'binary_sha256': 'same', 'command': ['app', '--output', str(path)]}
                runs.append(c.summarize(entry, 'dag', 0, 1))
            self.assertEqual(runs[0]['completed'], 1)
            self.assertEqual(runs[0]['workload']['arrival_sha256'], c.digest([500000000, 1500000000]))
            self.assertEqual(c.contract_errors(*runs, {})[0], [])


if __name__ == '__main__':
    unittest.main()

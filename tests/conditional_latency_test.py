#!/usr/bin/env python3
"""Offline cohort, grouping and inference-boundary checks; no CPU workload."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import conditional_latency as c


class ConditionalLatencyTest(unittest.TestCase):
    def fixture(self, root, seed=1, threads=16, shift=0):
        path = root / f'run-{seed}'
        path.mkdir()
        meta = {'stage': 3, 'duration_seconds': 4, 'allowed_cpus': [0, 1],
                'effective_masks': [[0, 1]], 'same_cpu_union': True, 'seed': seed,
                'affinity': 'cluster', 'threads_per_pool': threads, 'instrumentation': 'full'}
        (path / 'metadata.json').write_text(json.dumps(meta))
        (path / 'summary.json').write_text(json.dumps({
            'planned_tasks': 4, 'completed_tasks': 4, 'conservation_ok': True}))
        rows = []
        for i, q in enumerate((0, 0, 64, -1)):
            plan = i * 1_000_000_000
            rows.append({'task': i, 'burst': i, 'pool': 0, 'status': 'completed',
                         'target_cpu_us': 1000, 'planned_ns': plan, 'submit_ns': plan + 1000,
                         'start_ns': plan + (200_000_000 if i == 2 else 1_000_000),
                         'end_ns': plan + (2_000_000_000 if i == 2 else 5_000_000) + shift,
                         'native_queue': q})
        with (path / 'tasks.csv').open('w') as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
        (path / 'pools.csv').write_text('time_ns,pool,native_queue\n1000000000,0,1\n')
        return {'case': 'local', 'seed': seed, 'repetition': seed, 'path': str(path),
                'returncode': 0, 'command': ['binary', '--threads', str(threads), '--seed', str(seed)]}

    def manifest(self, root, rows):
        path = root / 'manifest.json'
        path.write_text(json.dumps({'binary_sha256': 'same', 'runs': rows,
                                  'analysis': {'warmup_seconds': 1, 'steady_end_seconds': 3}}))
        return path

    def test_planned_cohort_retains_slow_completion_and_tail_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = c.analyze(self.manifest(root, [self.fixture(root)]))
            run = out['runs'][0]
            self.assertEqual(run['cohort_n'], 2)
            self.assertEqual(run['buckets']['all']['latency']['planned_ms']['p99'], 2000)
            self.assertEqual(run['buckets']['64+']['latency']['wait_ms']['p99'], 199.999)
            self.assertTrue(run['buckets']['64+']['low_tail'])
            self.assertEqual(run['buckets']['64+']['bursts'], 1)
            self.assertEqual(run['buckets']['all']['latency']['planned_ms']['p50'], 5)

    def test_range_is_per_seed_quantile_not_pooled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [self.fixture(root, 1), self.fixture(root, 2, shift=1_000_000)]
            out = c.analyze(self.manifest(root, rows))
            self.assertEqual(out['groups'][0]['buckets']['all']['latency']['planned_ms']['p99'], [2000, 2001])
            self.assertIn('not pooled', c.report(out))

    def test_same_case_different_config_never_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [self.fixture(root, 1), self.fixture(root, 2, threads=256)]
            out = c.analyze(self.manifest(root, rows))
            self.assertEqual(len(out['groups']), 2)
            self.assertTrue(all(len(g['seeds']) == 1 for g in out['groups']))

    def test_missing_queue_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = self.fixture(root)
            path = Path(row['path']) / 'tasks.csv'
            records = c.read_csv(path)
            with path.open('w') as f:
                fields = [k for k in records[0] if k != 'native_queue']
                w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
                w.writeheader(); w.writerows(records)
            out = c.analyze(self.manifest(root, [row]))
            bins = out['runs'][0]['buckets']
            self.assertNotIn('0', bins)
            self.assertEqual(bins['unobserved']['n'], 2)

    def test_failed_missing_and_duplicate_attempts_stay_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = self.fixture(root, 1)
            failed = self.fixture(root, 2); failed['returncode'] = 2
            missing = self.fixture(root, 3)
            (Path(missing['path']) / 'tasks.csv').unlink()
            duplicate = self.fixture(root, 4)
            out = c.analyze(self.manifest(root, [good, failed, missing, duplicate, duplicate]))
            self.assertEqual(len(out['runs']), 1)
            self.assertEqual(len(out['errors']), 4)
            self.assertEqual(out['recorded_runs'], 5)

    def test_successful_only_inference_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = self.fixture(root)
            path = Path(row['path'])
            (path / 'tasks.csv').write_text((path / 'tasks.csv').read_text().replace(',completed,', ',canceled,', 1))
            (path / 'summary.json').write_text(json.dumps({
                'planned_tasks': 4, 'completed_tasks': 3, 'conservation_ok': True}))
            out = c.analyze(self.manifest(root, [row]))
            self.assertFalse(out['runs'])
            self.assertIn('incomplete/rejected', out['errors'][0]['error'])

    def test_window_required_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'manifest.json'
            path.write_text('{"runs": []}')
            with self.assertRaises(KeyError):
                c.analyze(path)

    def test_numeric_and_string_seed_are_the_same_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = self.fixture(root)
            out = c.analyze(self.manifest(root, [row, {**row, 'seed': '1'}]))
            self.assertFalse(out['runs'])
            self.assertEqual(len(out['errors']), 2)
            self.assertTrue(all('duplicate' in e['error'] for e in out['errors']))

    def test_rare_nonzero_probability_is_visible(self):
        value = 1 / 12000
        self.assertAlmostEqual(float(c.display_probability([value, value])), value, places=9)

    def test_missing_bucket_does_not_invent_samples(self):
        a = {'case': 'x', 'config_id': 'same', 'config': {}, 'seed': 1,
             'buckets': {'0': c.bucket_stats([{'burst': 1, 'pool': 0, 'target_cpu_us': 1000,
                                              **{m: 2 for m in c.METRICS}}])}}
        b = {**a, 'seed': 2, 'buckets': {}}
        out = c.aggregate([a, b])[0]['buckets']['0']
        self.assertEqual(out['runs_with_bucket'], 1)
        self.assertEqual(out['runs_total'], 2)
        self.assertEqual(out['n'], [1, 1])


if __name__ == '__main__':
    unittest.main()

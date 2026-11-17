#!/usr/bin/env python3
"""CPU-service envelopes preserve uncertainty and request/pool attribution."""

import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'request_cpu_shape.py'
SPEC = importlib.util.spec_from_file_location('request_cpu_shape', TOOL)
shape = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shape)
FIXTURE_SPEC = importlib.util.spec_from_file_location('flow_fixtures', Path(__file__).with_name('flow_report_test.py'))
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)
MS = shape.MS


def task(cpu, start=0, end=10, pool=0, request=0, tag=1):
    return {'request': request, 'tag': tag, 'kind': 'operator',
            'chosen': pool, 'home': 0, 'source': 0, 'reason': 0,
            'submitted_ns': 0, 'start_ns': start * MS,
            'finish_ns': end * MS, 'cpu_ns': cpu * MS}


def simple_summary():
    return {'schema': 'affinity-v2', 'trace': True, 'widths': [2],
            'depths': [1], 'nodes_per_request': 4, 'affinity': 'cluster',
            'groups': [{'cpus': [0, 2], 'capacity': 1.5, 'numa': 0},
                       {'cpus': [3], 'capacity': 1, 'numa': 2}]}


class BoundsTest(unittest.TestCase):
    def test_partially_busy_callback_retains_uncertainty(self):
        row = task(4)
        # A 4 ms CPU total over 10 ms wall may all occur outside this 2 ms bin.
        self.assertEqual(shape.service_bounds(row, 2 * MS, 4 * MS), (0, 2 * MS, 2 * MS))
        self.assertEqual(shape.service_bounds(row, 0, 8 * MS), (2 * MS, 4 * MS, 8 * MS))
        self.assertEqual(shape.service_bounds(row, 0, 10 * MS), (4 * MS, 4 * MS, 10 * MS))
        self.assertEqual(shape.service_bounds(row, 10 * MS, 11 * MS), (0, 0, 0))

    def test_cpu_equals_wall_is_exact(self):
        self.assertEqual(shape.service_bounds(task(10), 2 * MS, 4 * MS), (2 * MS, 2 * MS, 2 * MS))

    def test_zero_cpu_has_wall_occupancy_but_no_cpu(self):
        self.assertEqual(shape.service_bounds(task(0), 0, MS), (0, 0, MS))

    def test_cpu_exceeding_wall_even_one_ns_is_rejected(self):
        row = task(10)
        row['cpu_ns'] += 1
        with self.assertRaisesRegex(ValueError, 'no clipping'):
            shape.service_bounds(row, 0, MS)
        row['cpu_ns'] = -1
        with self.assertRaises(ValueError):
            shape.service_bounds(row, 0, MS)

    def test_cumulative_crossings_bound_cpu_not_callback_completions(self):
        bounds = shape.cumulative_crossing_bounds([task(4)], 0, 10 * MS, .5)
        self.assertEqual(bounds, {'earliest_possible_ms': 2, 'latest_guaranteed_ms': 8})
        exact = shape.cumulative_crossing_bounds([task(10)], 0, 10 * MS, .9)
        self.assertEqual(exact, {'earliest_possible_ms': 9, 'latest_guaranteed_ms': 9})
        self.assertIsNone(shape.cumulative_crossing_bounds([task(0)], 0, 10 * MS, .5))

    def test_cross_pool_concurrency_and_other_request_isolation(self):
        rows = [task(10, pool=0, tag=1), task(10, pool=1, tag=2),
                task(10, request=1, tag=1)]
        request = fixtures.request(0, 0, 0, 10)
        profile = list(shape.profile_rows(simple_summary(), request, rows, MS))
        first = {row['pool']: row for row in profile if row['relative_start_ms'] == 0}
        self.assertEqual(first['0']['timed_cpu_lower_cores'], 1)
        self.assertEqual(first['1']['timed_cpu_lower_cores'], 1)
        self.assertEqual(first['all']['timed_cpu_lower_cores'], 2)
        self.assertEqual(first['all']['timed_cpu_upper_cores'], 2)
        self.assertEqual(first['all']['declared_logical_cpus'], 3)
        self.assertEqual(first['0']['declared_logical_cpus'], 2)  # NOT score capacity 1.5
        self.assertAlmostEqual(first['all']['timed_cpu_upper_pct_of_declared_cpus'], 200 / 3)

    def test_callback_tail_after_logical_completion_is_retained(self):
        request = fixtures.request(0, 0, 0, 8)
        result = shape.request_summary(simple_summary(), request, [task(4)])
        self.assertEqual(result['profile_span_ms'], 10)
        self.assertEqual(result['attempt_to_request_finish_ms'], 8)
        self.assertEqual(result['last_callback_after_request_finish_ms'], 2)

    def test_pool_attribution_not_claimed_as_cluster_when_unbound(self):
        summary = simple_summary()
        summary['affinity'] = 'none'
        self.assertFalse(shape.topology(summary)['pool_service_maps_to_declared_cluster'])
        summary['affinity'] = 'cluster'
        summary['groups'][1]['cpus'] = [2]
        result = shape.topology(summary)
        self.assertEqual(result['union_logical_cpu_count'], 2)
        self.assertFalse(result['pool_service_maps_to_declared_cluster'])

    def test_duplicate_and_invalid_task_timestamps_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            shape.validate_tasks([task(1), task(1)])
        with self.assertRaises(ValueError):
            shape.validate_tasks([task(1, start=5, end=4)])


class CliTest(unittest.TestCase):
    def invoke(self, run, output, *args):
        return subprocess.run([sys.executable, str(TOOL), '--run', str(run),
                               '--output', str(output), *args], capture_output=True, text=True)

    def test_audited_cli_exports_all_summaries_and_default_extreme_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, output = root / 'run', root / 'output'
            fixtures.fixture(run)
            result = self.invoke(run, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['selected_request_ids'], [4])
            self.assertIn('not representative', summary['selection'])
            self.assertEqual(len(summary['source_sha256']), 3)
            self.assertEqual(len(list(output.glob('request_*_profile.csv'))), 1)
            with (output / 'requests_shape.csv').open() as stream:
                self.assertEqual([row['request'] for row in csv.DictReader(stream)], ['0', '2', '4'])
            self.assertEqual(self.invoke(run, output).returncode, 2)  # refuse overwrite
            explicit = self.invoke(run, root / 'explicit', '--request', '0,2', '--bin-ms', '0.5')
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            self.assertEqual(len(list((root / 'explicit').glob('request_*_profile.csv'))), 2)

    def test_missing_cpu_field_and_unavailable_trace_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / 'trace0'
            fixtures.fixture(run, trace=False)
            result = self.invoke(run, root / 'out')
            self.assertEqual(result.returncode, 2)
            self.assertIn('trace=0', result.stderr)
            self.assertFalse((root / 'out').exists())
            run = root / 'missing'
            _, tasks, _ = fixtures.fixture(run)
            for row in tasks:
                del row['cpu_ns']
            fixtures.csv_write(run / 'tasks.csv', tasks)
            result = self.invoke(run, root / 'missing-out')
            self.assertEqual(result.returncode, 2)
            self.assertIn('cpu_ns', result.stderr)

    def test_invalid_cpu_missing_callbacks_failed_request_and_bad_bin_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / 'run'
            _, tasks, _ = fixtures.fixture(run)
            for args in (('--request', '1'), ('--request', '999'), ('--request', '0,0'),
                         ('--bin-ms', 'nan'), ('--bin-ms', '0')):
                self.assertEqual(self.invoke(run, root / 'out', *args).returncode, 2)
            tasks[0]['cpu_ns'] = tasks[0]['finish_ns'] - tasks[0]['start_ns'] + 1
            fixtures.csv_write(run / 'tasks.csv', tasks)
            result = self.invoke(run, root / 'bad-clock')
            self.assertEqual(result.returncode, 2)
            self.assertIn('no clipping', result.stderr)
            fixtures.csv_write(run / 'tasks.csv', tasks[1:])
            self.assertEqual(self.invoke(run, root / 'missing-task').returncode, 2)


if __name__ == '__main__':
    unittest.main()

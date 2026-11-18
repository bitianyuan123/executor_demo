#!/usr/bin/env python3
"""Exact offline phase intersections; no workload or performance assertions."""

import copy
import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'request_phase_overlap.py'
SPEC = importlib.util.spec_from_file_location('request_phase_overlap', TOOL)
phase = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phase)
MS = phase.MS


def callback(rid, tag, submitted, start, finish, pool=0):
    kind = ('init', 'operator', 'join', 'operator', 'join')[tag]
    return {'request': rid, 'tag': tag, 'kind': kind, 'home': 0,
            'source': 0, 'chosen': pool, 'hops': 0, 'reason': 0,
            'migrated': int(pool != 0), 'cross_numa': 0,
            'queue_before': 0, 'outstanding_before': 0,
            'submitted_ns': round(submitted * MS), 'start_ns': round(start * MS),
            'finish_ns': round(finish * MS), 'cpu_ns': 0,
            'iterations': int(kind == 'operator'), 'read_words': 0,
            'write_words': 0, 'checksum': tag}


def fixture(path, new_pool=0, new_numa=0):
    path.mkdir()
    requests = [
        {'request': 0, 'scheduled_ns': 0, 'submitted_ns': 0,
         'finished_ns': 10_500_000, 'status': 'completed', 'concurrent': 1, 'checksum': 1},
        {'request': 1, 'scheduled_ns': 2_500_000, 'submitted_ns': 2_500_000,
         'finished_ns': 10_500_000, 'status': 'completed', 'concurrent': 2, 'checksum': 2}]
    tasks = [callback(0, 0, 0, 0, 1), callback(0, 1, .5, 1, 2),
             callback(0, 2, 1.8, 2, 3), callback(0, 3, 2.5, 4, 10),
             callback(0, 4, 9.8, 10, 11), callback(1, 0, 2.5, 2.5, 3),
             callback(1, 1, 2.75, 3, 7, new_pool), callback(1, 2, 6.8, 7, 8),
             callback(1, 3, 7.5, 8, 10), callback(1, 4, 9.8, 10, 11)]
    arrival = checksum = 0
    for row in requests:
        arrival = phase.report.mix(arrival ^ row['scheduled_ns'])
        checksum = phase.report.mix(checksum ^ row['checksum'])
    summary = {'schema': 'affinity-v2', 'trace': True, 'widths': [1, 1],
               'depths': [1, 1], 'nodes_per_request': 5, 'origin_ns': 0,
               'seed': 1, 'work_signature': '{}', 'policy': 'home',
               'continuation': 'parent', 'affinity': 'cluster', 'model': 'none',
               'requests': 2, 'groups': [{'cpus': [0], 'capacity': 1, 'numa': 0},
                                        {'cpus': [1], 'capacity': 1, 'numa': new_numa}],
               'threads_per_pool': 1, 'arrival_hash': str(arrival), 'checksum': str(checksum),
               'completed': 2, 'failed_or_rejected': 0, 'observed_tasks': len(tasks),
               'migrations': sum(row['migrated'] for row in tasks), 'cross_numa': 0,
               'options': {}, 'process_cpu_s': 0, 'peak_inflight': 2, 'arrival': 'regular'}
    (path / 'summary.json').write_text(json.dumps(summary))
    for name, rows in (('requests.csv', requests), ('tasks.csv', tasks)):
        with (path / name).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return phase.report.load_run(path, .0025, .001)


class PhaseOverlapTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'run'
        self.run = fixture(self.path)

    def test_known_overlap_includes_old_request_outside_cohort(self):
        result = phase.analyze(self.run)
        self.assertEqual(result['cohort_requests'], 1)
        row = result['scopes']['pool']
        self.assertEqual(row['stage0_submissions'], 1)
        self.assertEqual(row['submissions_with_older_pending'], 1)
        self.assertEqual(row['submissions_with_older_executing'], 0)
        self.assertEqual(row['new_live_request_resource_ms'], 4.25)
        self.assertEqual(row['new_live_with_older_pending_request_resource_ms'], 1.25)
        self.assertEqual(row['new_live_with_older_executing_request_resource_ms'], 3)
        self.assertEqual(row['simultaneous_executing_request_resource_ms'], 3)
        self.assertEqual(row['new_executing_request_resource_ms'], 4)
        self.assertEqual(row['simultaneous_executing_fraction'], .75)
        self.assertEqual(row['max_combined_executing_callbacks_when_exposed'], 2)
        # Zero recorded CPU still permits wall overlap; never label it CPU use.
        self.assertTrue(all(task['cpu_ns'] == 0 for task in self.run['tasks']))

    def test_same_numa_different_pool_and_cross_numa_are_distinguished(self):
        for numa in (0, 2):
            with self.subTest(numa=numa):
                run = fixture(Path(self.temporary.name) / f'pool-{numa}', 1, numa)
                result = phase.analyze(run)['scopes']
                self.assertEqual(result['pool']['simultaneous_executing_request_resource_ms'], 0)
                self.assertEqual(result['numa']['simultaneous_executing_request_resource_ms'], 3 if numa == 0 else 0)

    def test_half_open_boundaries_and_zero_length_callbacks(self):
        old = [callback(0, 3, 0, 2, 4)]
        groups = self.run['summary']['groups']
        ranks = {0: 0, 1: 1}
        at_start = [callback(1, 1, 2, 2, 3)]
        row = phase.analyze_scope(old, at_start, ranks, groups, 'pool')
        self.assertEqual(row['submissions_with_older_pending'], 0)
        self.assertEqual(row['submissions_with_older_executing'], 1)
        self.assertEqual(row['simultaneous_executing_request_resource_ms'], 1)
        at_end = [callback(1, 1, 4, 4, 5)]
        row = phase.analyze_scope(old, at_end, ranks, groups, 'pool')
        self.assertEqual(row['submissions_with_either'], 0)
        self.assertEqual(row['simultaneous_executing_request_resource_ms'], 0)
        row = phase.analyze_scope([callback(0, 3, 2, 2, 2)], at_start, ranks, groups, 'pool')
        self.assertEqual(row['submissions_with_either'], 0)

    def test_request_union_not_task_pair_sum_and_no_self_or_equal_arrival(self):
        groups = self.run['summary']['groups']
        old = [callback(0, 3, 0, 0, 10), callback(0, 4, 0, 0, 10)]
        new = [callback(1, 1, 2, 2, 6), callback(1, 2, 3, 3, 7)]
        row = phase.analyze_scope(old, new, {0: 0, 1: 1}, groups, 'pool')
        self.assertEqual(row['new_executing_request_resource_ms'], 5)
        self.assertEqual(row['simultaneous_executing_request_resource_ms'], 5)
        self.assertEqual(row['max_combined_executing_callbacks_when_exposed'], 4)
        self.assertEqual(row['stage0_submissions'], 2)
        for ranks in ({0: 0, 1: 0}, {0: 1, 1: 0}):
            self.assertEqual(phase.analyze_scope(old, new, ranks, groups, 'pool')['submissions_with_either'], 0)
        own = [callback(1, 3, 0, 0, 10)]
        self.assertEqual(phase.analyze_scope(own, new, {1: 0}, groups, 'pool')['simultaneous_executing_request_resource_ms'], 0)

    def test_inverse_wait_includes_newer_request_outside_cohort(self):
        run = copy.deepcopy(self.run)
        run['completed_cohort'] = run['requests'][:1]
        run['cohort'] = run['requests'][:1]
        result = phase.analyze(run)
        self.assertEqual(result['later_wave_callbacks_cohort'], 1)
        self.assertEqual(result['initial_stage0_callbacks_whole'], 2)
        row = result['scopes']['pool']['inverse_later_wait']
        # Old later-wave wait [2.5,4) meets newer stage0 execution [3,7): 1 ms.
        self.assertEqual(row['later_wave_callbacks'], 1)
        self.assertEqual(row['callbacks_with_newer_stage0_execution_during_wait'], 1)
        self.assertEqual(row['covered_union_callback_wait_ms'], 1)
        self.assertEqual(row['total_callback_wait_ms'], 1.5)
        self.assertEqual(row['exposed_callbacks_wait_mean_ms'], 1.5)
        self.assertIsNone(row['nonexposed_callbacks_wait_mean_ms'])
        self.assertEqual(row['covered_fraction_of_total_wait'], 2 / 3)

    def test_inverse_wait_union_zero_wait_and_conditional_means(self):
        groups = self.run['summary']['groups']
        old = [callback(0, 3, 2.5, 4, 10), callback(0, 4, 0, 1, 10),
               callback(0, 1, 5, 5, 10)]
        new = [callback(1, 1, 2.5, 3, 7), callback(1, 2, 2.5, 3.5, 8)]
        row = phase.analyze_wait_exposure(old, new, {0: 0, 1: 1}, groups, 'pool')
        self.assertEqual(row['later_wave_callbacks'], 3)
        self.assertEqual(row['positive_wait_callbacks'], 2)
        self.assertEqual(row['zero_wait_callbacks'], 1)
        self.assertEqual(row['covered_union_callback_wait_ms'], 1)  # Not 1.5 pair-sum.
        self.assertEqual(row['total_callback_wait_ms'], 2.5)
        self.assertEqual(row['exposed_callbacks_wait_mean_ms'], 1.5)
        self.assertEqual(row['nonexposed_callbacks_wait_mean_ms'], .5)  # Includes zero wait.
        zero = phase.analyze_wait_exposure(old[-1:], new, {0: 0, 1: 1}, groups, 'pool')
        self.assertEqual(zero['covered_union_callback_wait_ms'], 0)
        self.assertIsNone(zero['covered_fraction_of_total_wait'])
        self.assertEqual(zero['nonexposed_callbacks_wait_mean_ms'], 0)

    def test_inverse_wait_halfopen_order_and_pool_numa(self):
        groups = self.run['summary']['groups']
        old = [callback(0, 3, 2.5, 4, 10)]
        for start, finish, expected in ((1, 2.5, 0), (4, 5, 0), (2.5, 4, 1.5), (3, 3, 0)):
            new = [callback(1, 1, start, start, finish)]
            row = phase.analyze_wait_exposure(old, new, {0: 0, 1: 1}, groups, 'pool')
            self.assertEqual(row['covered_union_callback_wait_ms'], expected)
        new = [callback(1, 1, 3, 3, 7)]
        for ranks in ({0: 0, 1: 0}, {0: 1, 1: 0}):
            self.assertEqual(phase.analyze_wait_exposure(old, new, ranks, groups, 'pool')['covered_union_callback_wait_ms'], 0)
        new[0]['chosen'] = 1
        self.assertEqual(phase.analyze_wait_exposure(old, new, {0: 0, 1: 1}, groups, 'pool')['covered_union_callback_wait_ms'], 0)
        self.assertEqual(phase.analyze_wait_exposure(old, new, {0: 0, 1: 1}, groups, 'numa')['covered_union_callback_wait_ms'], 1)
        separate = copy.deepcopy(groups)
        separate[1]['numa'] = 2
        self.assertEqual(phase.analyze_wait_exposure(old, new, {0: 0, 1: 1}, separate, 'numa')['covered_union_callback_wait_ms'], 0)

    def test_fail_closed_incomplete_graph_bad_parent_clock_and_unsupported_modes(self):
        cases = []
        run = copy.deepcopy(self.run)
        run['tasks'].pop()
        cases.append(run)
        run = copy.deepcopy(self.run)
        run['tasks'][6]['submitted_ns'] = 3_100_000
        run['tasks'][6]['start_ns'] = 3_200_000
        cases.append(run)
        run = copy.deepcopy(self.run)
        run['tasks'][0]['cpu_ns'] = MS + 1
        cases.append(run)
        for key, value in (('schema', 'affinity-v3-async'), ('trace', False),
                           ('sync_block_percent', 1), ('affinity', 'none')):
            run = copy.deepcopy(self.run)
            run['summary'][key] = value
            cases.append(run)
        run = copy.deepcopy(self.run)
        run['summary']['groups'][1]['cpus'] = [0]
        cases.append(run)
        for index, run in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                phase.analyze(run)

    def test_cli_json_hashes_and_validation_errors(self):
        command = [sys.executable, str(TOOL), str(self.path), '--warmup-seconds', '.0025',
                   '--window-seconds', '.001', '--json']
        done = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(len(result['source_sha256']), 3)
        self.assertEqual(result['scopes']['pool']['simultaneous_executing_request_resource_ms'], 3)
        for args in (['--window-seconds', 'nan'], ['--warmup-seconds', '-1']):
            done = subprocess.run(command + args, capture_output=True, text=True)
            self.assertEqual(done.returncode, 2)
        (self.path / 'tasks.csv').unlink()
        done = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn('affinity request phase overlap:', done.stderr)


if __name__ == '__main__':
    unittest.main()

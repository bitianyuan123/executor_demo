#!/usr/bin/env python3
"""Synthetic timelines; no pressure generation, real CPU load or external tools."""

import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'diagnose.py'
SPEC = importlib.util.spec_from_file_location('affinity_diagnose', TOOL)
diagnose = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnose)
MS = 1000000


def request(number, scheduled, submitted, finished, status='completed'):
    return {'request': number, 'scheduled_ns': scheduled * MS, 'submitted_ns': submitted * MS,
            'finished_ns': finished * MS, 'status': status, 'concurrent': 1, 'checksum': number + 1}


def task(number, tag, submitted, start, finish, *, kind='operator', chosen=0,
         queue=0, inflight=0, outstanding=0):
    return {'request': number, 'tag': tag, 'submitted_ns': submitted * MS,
            'start_ns': start * MS, 'finish_ns': finish * MS, 'kind': kind,
            'home': 0, 'chosen': chosen, 'queue_before': queue,
            'callback_inflight_before': inflight, 'outstanding_before': outstanding}


class TimelineTest(unittest.TestCase):
    def test_crossing_requests_late_submission_and_boundary_conservation(self):
        requests = [request(0, 0, 5, 25), request(1, 12, 35, 45),
                    request(2, 19, 19, 19, 'admission_rejected'),
                    request(3, 20, 21, 50), request(4, 30, 31, 40, 'failed')]
        tasks = [task(0, 1, 5, 8, 22), task(1, 1, 12, 35, 45)]
        rows = diagnose.request_trend(requests, tasks, 10 * MS, 40 * MS, 10 * MS)
        self.assertEqual([(r['arrivals'], r['completed'], r['failed_or_rejected']) for r in rows],
                         [(2, 0, 1), (1, 1, 0), (1, 0, 0)])
        self.assertEqual([(r['backlog_left'], r['backlog_right']) for r in rows], [(1, 2), (2, 2), (2, 3)])
        for row in rows:
            self.assertEqual(row['backlog_left'] + row['arrivals'] - row['completed'] - row['failed_or_rejected'],
                             row['backlog_right'])
        # Submission at 35 ms belongs to the final wall bin; its 23 ms lag is
        # retained in the 12 ms scheduled-arrival cohort even after window end.
        self.assertEqual([r['submission_attempts'] for r in rows], [1, 1, 2])
        self.assertEqual(rows[0]['lag']['p99'], 23)
        self.assertEqual(rows[0]['callbacks'], {'mean': 1, 'peak': 1})
        self.assertAlmostEqual(rows[1]['callbacks']['mean'], .2)
        self.assertAlmostEqual(rows[2]['callbacks']['mean'], .5)
        shorter = diagnose.request_trend(requests, tasks, 10 * MS, 20 * MS, 10 * MS)
        self.assertEqual(shorter[0]['lag']['p99'], 23, 'late submissions must not be censored')

    def test_callback_sweep_adjacent_zero_length_and_cross_window_intervals(self):
        tasks = [task(0, 1, 0, 0, 5), task(0, 2, 0, 5, 10),
                 task(0, 3, 0, 5, 5), task(1, 1, 0, 2, 8)]
        rows = diagnose.callback_bins(tasks, 0, 10 * MS, 5 * MS)
        self.assertEqual([r['peak'] for r in rows], [2, 2])
        self.assertEqual([r['mean'] for r in rows], [1.6, 1.6])
        self.assertEqual(diagnose.callback_bins(tasks[:2], 0, 10 * MS, 10 * MS),
                         [{'mean': 1, 'peak': 1}])
        self.assertEqual(diagnose.callback_bins(tasks, 3 * MS, 7 * MS, 4 * MS),
                         [{'mean': 2, 'peak': 2}])

    def test_first_fork_exact_tags_retains_late_finishes_and_excludes_later_stages(self):
        run = {'summary': {'schema': 'affinity-v2', 'trace': True, 'widths': [2],
                           'depths': [2], 'nodes_per_request': 6},
               'requests': [request(0, 0, 0, 50), request(1, 1, 1, 1, 'admission_rejected')],
               'tasks': [task(0, 0, 0, 0, 5, kind='init'),
                         task(0, 1, 4, 5, 25), task(0, 2, 6, 10, 30, chosen=1),
                         task(0, 3, 25, 25, 100)]}
        result = diagnose.first_fork(run, 0, 10 * MS)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['noncompleted_requests'], 1)
        self.assertEqual(len(result['rows']), 1)
        row = result['rows'][0]
        self.assertEqual(row['recorded_submit_span_ms'], 2)
        self.assertEqual(row['all_started_after_first_submit_ms'], 6)
        self.assertEqual(row['stage0_callback_span_ms'], 25)
        self.assertEqual(row['callback_peak'], 2)
        self.assertAlmostEqual(row['callback_mean'], 1.6)
        self.assertEqual(row['home_pool_callback_peak'], 1)
        run['tasks'] = [row for row in run['tasks'] if row['tag'] != 2]
        result = diagnose.first_fork(run, 0, 10 * MS)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['incomplete_requests'], 1)
        run['summary']['nodes_per_request'] = 100
        self.assertEqual(diagnose.first_fork(run, 0, 10 * MS)['status'], 'unavailable')
        run['summary']['trace'] = False
        self.assertEqual(diagnose.first_fork(run, 0, 10 * MS)['reason'], 'trace=0')

    def test_load_buckets_are_post_choice_operator_samples_not_time_samples(self):
        tasks = [task(0, 0, 12, 13, 15, kind='init', queue=100),
                 task(0, 1, 5, 15, 25, queue=100),  # crossing task, submitted before window
                 task(0, 2, 12, 14, 18, queue=1, inflight=2, outstanding=3),
                 task(1, 1, 19, 35, 55, chosen=1, queue=2, inflight=0, outstanding=4),
                 task(1, 2, 20, 25, 26, queue=100)]  # exact end is excluded
        groups = [{'capacity': 2}, {'capacity': 4}]
        conditions = diagnose.load_conditions(tasks, groups, 10 * MS, 20 * MS,
                                               set(diagnose.LOAD_FIELDS.values()))
        self.assertEqual(len(conditions['Q']), 1)
        q = conditions['Q'][0]
        self.assertEqual((q['bucket'], q['n'], q['requests']), ('(0,.5]', 2, 2))
        self.assertEqual(q['pools'], 2)
        self.assertEqual((q['wait_ms']['p50'], q['wait_ms']['p95'], q['wait_ms']['p99']), (2, 16, 16))
        self.assertEqual((q['wall_ms']['p50'], q['wall_ms']['p99']), (4, 20))
        self.assertEqual({row['bucket'] for row in conditions['I']}, {'0', '(.5,1]'})
        old = diagnose.load_conditions(tasks, groups, 10 * MS, 20 * MS,
                                       {'queue_before', 'outstanding_before'})
        self.assertNotIn('I', old, 'missing I must not be silently replaced with zero')


def write_trace_zero_fixture(directory):
    rows = [request(0, 0, 1, 20), request(1, 10, 12, 45),
            request(2, 20, 21, 21, 'admission_rejected')]
    arrival = checksum = 0
    for row in rows:
        arrival = diagnose.report.mix(arrival ^ row['scheduled_ns'])
        if row['status'] == 'completed':
            checksum = diagnose.report.mix(checksum ^ row['checksum'])
    summary = {'schema': 'affinity-v2', 'origin_ns': 0, 'seed': 1, 'work_signature': '{}',
               'policy': 'home', 'continuation': 'parent', 'affinity': 'cluster', 'model': 'none',
               'requests': 3, 'nodes_per_request': 3, 'widths': [1], 'depths': [1],
               'groups': [{'cpus': [0], 'capacity': 1, 'numa': 0}], 'threads_per_pool': 1,
               'trace': False, 'arrival_hash': str(arrival), 'checksum': str(checksum),
               'completed': 2, 'failed_or_rejected': 1, 'observed_tasks': 0, 'migrations': 0,
               'cross_numa': 0, 'options': {}, 'process_cpu_s': 0, 'peak_inflight': 2}
    directory.mkdir()
    (directory / 'summary.json').write_text(json.dumps(summary))
    with (directory / 'requests.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


class AuditedCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='affinity-diagnose-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / 'run'
        self.rows = write_trace_zero_fixture(self.run)

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(TOOL), str(self.run), *map(str, args)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_trace_zero_rejections_and_multiple_runs(self):
        process = self.invoke(str(self.run), '--window-seconds', '.04', '--bin-ms', '10', '--max-bins', '4')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.count('trace=0: task load samples'), 2)
        self.assertIn('Failed/rejected requests are present', process.stdout)
        self.assertIn('does NOT establish steady state', process.stdout)
        self.assertIn('0.040000', process.stdout)
        self.assertIn('cannot prove 16 CPUs on-CPU for 30 ms', process.stdout)
        self.assertIn('experiment_role=manual_or_unclassified', process.stdout)

    def test_nonterminal_or_missing_requests_are_rejected_by_shared_audit(self):
        for rows in (self.rows[:-1], [{**row, 'status': 'pending'} if row['request'] == 1 else row
                                    for row in self.rows]):
            with self.subTest(rows=len(rows)):
                with (self.run / 'requests.csv').open('w', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                process = self.invoke()
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('affinity diagnose:', process.stderr)

    def test_explicit_bin_limit_and_invalid_window_are_rejected(self):
        for args in (('--window-seconds', '1', '--bin-ms', '1', '--max-bins', '2'),
                     ('--window-seconds', 'nan'), ('--bin-ms', '0'), ('--max-bins', '1001')):
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(*args).returncode, 0)
        # Default output is bounded even when the terminal observation is late.
        process = self.invoke('--window-seconds', '1000')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertLessEqual(sum(line.startswith('| ') for line in process.stdout.splitlines()), 21)


if __name__ == '__main__':
    unittest.main()

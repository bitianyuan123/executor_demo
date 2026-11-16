#!/usr/bin/env python3
"""Arrival shifts, role conservation and audited CLI; no pressure generation."""

import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'flow_report.py'
SPEC = importlib.util.spec_from_file_location('affinity_flow_report', TOOL)
flow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flow)
MS = 1000000


def request(number, scheduled, submitted, finished, status='completed'):
    return {'request': number, 'scheduled_ns': scheduled * MS, 'submitted_ns': submitted * MS,
            'finished_ns': finished * MS, 'status': status, 'concurrent': 1, 'checksum': number + 1}


def graph_summary():
    return {'schema': 'affinity-v2', 'trace': True, 'widths': [2, 1],
            'depths': [2, 2], 'nodes_per_request': 9}


def graph_tasks(request_id=0, submitted=0):
    kinds = ['init', 'operator', 'operator', 'operator', 'operator',
             'join', 'operator', 'operator', 'join']
    routes = [(0, 0), (0, 1), (0, 0), (1, 1), (1, 0),
              (1, 0), (0, 1), (1, 0), (0, 0)]
    rows = []
    for tag, (kind, (source, chosen)) in enumerate(zip(kinds, routes)):
        rows.append({'request': request_id, 'tag': tag, 'kind': kind,
                     'home': 0, 'source': source, 'chosen': chosen, 'hops': 0,
                     'reason': 0, 'migrated': int(source != chosen), 'cross_numa': 0,
                     'queue_before': 0, 'outstanding_before': 0,
                     'submitted_ns': (submitted + tag) * MS,
                     'start_ns': (submitted + tag + 1) * MS,
                     'finish_ns': (submitted + tag + 3) * MS,
                     'cpu_ns': MS, 'iterations': 1, 'read_words': 0,
                     'write_words': 0, 'checksum': tag})
    return rows


def csv_write(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fixture(path, trace=True):
    rows = [request(0, 0, 5, 50), request(1, 10, 20, 90, 'failed'),
            request(2, 20, 25, 60), request(3, 30, 50, 50, 'admission_rejected'),
            request(4, 40, 60, 100)]
    arrival = checksum = 0
    for row in rows:
        arrival = flow.report.mix(arrival ^ row['scheduled_ns'])
        if row['status'] == 'completed':
            checksum = flow.report.mix(checksum ^ row['checksum'])
    tasks = [task for row in rows if row['status'] == 'completed'
             for task in graph_tasks(row['request'], row['submitted_ns'] // MS)] if trace else []
    summary = {**graph_summary(), 'trace': trace, 'origin_ns': 0, 'seed': 1,
               'work_signature': '{}', 'policy': 'outstanding', 'continuation': 'parent',
               'affinity': 'cluster', 'model': 'none', 'requests': len(rows),
               'groups': [{'cpus': [0], 'capacity': 1, 'numa': 0},
                          {'cpus': [1], 'capacity': 1, 'numa': 0}], 'threads_per_pool': 1,
               'arrival_hash': str(arrival), 'checksum': str(checksum),
               'completed': 3, 'failed_or_rejected': 2, 'observed_tasks': len(tasks),
               'migrations': sum(task['migrated'] for task in tasks), 'cross_numa': 0,
               'options': {}, 'process_cpu_s': 0, 'peak_inflight': 2,
               'arrival': 'burst', 'burst_size': 2, 'burst_spacing_us': 10000,
               'work_contract': {'arrival': 'burst'}}
    path.mkdir()
    (path / 'summary.json').write_text(json.dumps(summary))
    csv_write(path / 'requests.csv', rows)
    if tasks:
        csv_write(path / 'tasks.csv', tasks)
    return rows, tasks, summary


class FlowUnitTest(unittest.TestCase):
    def test_mean_decomposition_keeps_late_success_and_excludes_failure(self):
        rows = [request(0, 0, 5, 20), request(1, 1, 10, 40),
                request(2, 2, 100, 200, 'failed')]
        result = flow.latency_breakdown(rows)
        self.assertEqual(result['n'], 2)
        self.assertEqual(result['statuses'], {'completed': 2, 'failed': 1})
        self.assertEqual(result['planned_ms']['mean'], 29.5)
        self.assertEqual(result['lag_ms']['mean'] + result['after_attempt_ms']['mean'], 29.5)
        self.assertEqual(result['planned_ms']['p99'], 39)
        # Different requests dominate component tails: quantiles do not add.
        varied = flow.latency_breakdown([request(0, 0, 10, 11), request(1, 1, 2, 22)])
        self.assertNotEqual(varied['planned_ms']['p99'], varied['lag_ms']['p99'] + varied['after_attempt_ms']['p99'])
        self.assertIsNone(flow.latency_breakdown(rows[-1:])['planned_ms']['mean'])

    def test_common_shift_and_widening_are_separate_measurements(self):
        rows = [request(0, 0, 5, 30), request(1, 1, 6, 30),
                request(2, 10, 15, 30), request(3, 11, 25, 30)]
        result = flow.burst_summary(rows, 2, 0, 12 * MS)
        self.assertEqual(result['complete'], 2)
        self.assertEqual(result['planned_span_ms']['max'], 1)
        self.assertEqual(result['attempt_span_ms']['mean'], 5.5)
        self.assertEqual(result['attempt_span_ms']['max'], 10)
        self.assertEqual(result['first_attempt_lag_ms']['mean'], 5)
        # The second group's attempts are outside the planned window, retained.
        self.assertEqual(result['attempt_span_ms']['n'], 2)

    def test_boundary_cut_trailing_partial_and_failed_members(self):
        rows = [request(i, i, i + 20, i + 30,
                        'failed' if i == 4 else 'completed') for i in range(7)]
        result = flow.burst_summary(rows, 3, MS, 7 * MS)
        self.assertEqual((result['complete'], result['boundary_cut'], result['trailing_partial']), (1, 1, 1))
        self.assertEqual(result['failed_or_rejected_members'], 1)
        self.assertEqual(result['attempt_span_ms']['n'], 1)
        self.assertEqual(result['attempt_span_ms']['mean'], 2)
        # Exact upper boundary excludes request 5, turning that full group into
        # a boundary-cut group; the final partial group is entirely outside.
        result = flow.burst_summary(rows, 3, 0, 5 * MS)
        self.assertEqual((result['complete'], result['boundary_cut'], result['trailing_partial']), (1, 1, 0))

    def test_roles_partition_depths_and_distinguish_return_from_stay(self):
        tasks = graph_tasks()
        roles = flow.task_roles(graph_summary(), tasks)
        self.assertEqual([roles[role]['n'] for role in flow.ROLES], [1, 2, 1, 3, 2])
        self.assertEqual(sum(row['n'] for row in roles.values()), 9)
        self.assertEqual(roles['ordinary successors']['migrations'], 2)
        self.assertEqual(roles['ordinary successors']['return_home'], 2)
        self.assertEqual(roles['join']['return_home'], 1)
        self.assertEqual(roles['first-wave stage0']['return_home'], 0)
        self.assertEqual(roles['ordinary successors']['wait_ms'], 1)
        self.assertEqual(roles['ordinary successors']['cpu_ms'], 1)
        self.assertEqual(roles['ordinary successors']['wall_ms'], 2)
        tasks[3].pop('cpu_ns')
        self.assertIsNone(flow.task_roles(graph_summary(), tasks)['ordinary successors']['cpu_ms'])
        self.assertIsNone(flow.task_roles({**graph_summary(), 'trace': False}, tasks))
        for summary in ({**graph_summary(), 'schema': 'arbitrary-dag'},
                        {**graph_summary(), 'depths': [1, 1]},
                        {**graph_summary(), 'widths': [2.0, 1]}):
            with self.assertRaises(ValueError):
                flow.task_roles(summary, tasks)
        tasks[1]['kind'] = 'join'
        with self.assertRaises(ValueError):
            flow.task_roles(graph_summary(), tasks)


class AuditedCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='affinity-flow-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / 'run'
        self.rows, self.tasks, self.summary = fixture(self.run)

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(TOOL), str(self.run), *map(str, args)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_real_audited_cli_window_role_counts_and_late_attempts(self):
        process = self.invoke('--warmup-seconds', '.01', '--window-seconds', '.04')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('completed/offered=2/4', process.stdout)
        self.assertIn('complete=1, boundary-cut=1, trailing-partial=1', process.stdout)
        self.assertIn('attempt_span_ms mean/p50/p95/p99/max=25.000/', process.stdout)
        self.assertIn('Role conservation: 18 = 2 completed requests x 9 callbacks', process.stdout)
        self.assertIn('| ordinary successors | 6 | 4 | 4 |', process.stdout)
        self.assertIn("'admission_rejected': 1", process.stdout)
        self.assertIn('BEFORE request construction/root add', process.stdout)
        self.assertIn('quantiles must NOT be subtracted', process.stdout)
        self.assertLessEqual(len(process.stdout.splitlines()), 26)

    def test_trace_zero_and_multiple_runs(self):
        other = self.root / 'trace-zero'
        fixture(other, trace=False)
        process = self.invoke(other)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.count('RUN '), 2)
        self.assertIn('Callback roles unavailable: trace=0', process.stdout)
        self.assertIn('trailing-partial=1', process.stdout)

    def test_shared_audit_rejects_truncated_requests_bad_task_and_checksum(self):
        csv_write(self.run / 'requests.csv', self.rows[:-1])
        self.assertNotEqual(self.invoke().returncode, 0)
        csv_write(self.run / 'requests.csv', self.rows)
        self.tasks[1]['kind'] = 'join'
        csv_write(self.run / 'tasks.csv', self.tasks)
        process = self.invoke()
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('task kind does not match graph', process.stderr)
        self.tasks[1]['kind'] = 'operator'
        csv_write(self.run / 'tasks.csv', self.tasks)
        self.summary['checksum'] = 'invalid'
        (self.run / 'summary.json').write_text(json.dumps(self.summary))
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_invalid_and_empty_windows(self):
        for args in (('--window-seconds', 'nan'), ('--window-seconds', '-1'),
                     ('--window-seconds', '0'), ('--window-seconds', '1e-20'),
                     ('--warmup-seconds', '-1'), ('--warmup-seconds', 'inf')):
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(*args).returncode, 0)
        process = self.invoke('--warmup-seconds', '10', '--window-seconds', '1')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('completed/offered=0/0', process.stdout)
        self.assertIn('planned latency=unknown', process.stdout)
        self.assertIn('Role conservation: 0 = 0 completed requests x 9 callbacks', process.stdout)


if __name__ == '__main__':
    unittest.main()

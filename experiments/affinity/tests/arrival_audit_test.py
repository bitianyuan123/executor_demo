#!/usr/bin/env python3
"""Synthetic arrival boundaries and the shared CSV audit; no pressure runs."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'arrival_audit.py'
SPEC = importlib.util.spec_from_file_location('affinity_arrival_audit', TOOL)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
FIXTURE_SPEC = importlib.util.spec_from_file_location('flow_report_fixture', Path(__file__).with_name('flow_report_test.py'))
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)
request, csv_write, fixture = fixtures.request, fixtures.csv_write, fixtures.fixture
MS = 1_000_000


class ArrivalUnitTest(unittest.TestCase):
    def test_overlap_adjacent_zero_length_and_simultaneous_endpoints(self):
        rows = [request(0, 0, 0, 5), request(1, 5, 5, 10),
                request(2, 5, 5, 5, 'admission_rejected')]
        result = audit.request_overlap(rows, 'submitted_ns', 0, 10 * MS)
        self.assertEqual(result, {'intervals': 2, 'mean': 1, 'peak': 1, 'time_ge2_percent': 0})
        rows.append(request(3, 2, 2, 8, 'failed'))
        result = audit.request_overlap(rows, 'submitted_ns', 0, 10 * MS)
        self.assertEqual(result, {'intervals': 3, 'mean': 1.6, 'peak': 2, 'time_ge2_percent': 60})

    def test_overlap_uses_all_requests_and_clips_both_sides(self):
        rows = [request(0, 0, 12, 30), request(1, 15, 25, 35),
                request(2, 20, 20, 20, 'failed'), request(3, 40, 40, 50)]
        scheduled = audit.request_overlap(rows, 'scheduled_ns', 10 * MS, 20 * MS)
        attempted = audit.request_overlap(rows, 'submitted_ns', 10 * MS, 20 * MS)
        self.assertEqual(scheduled, {'intervals': 2, 'mean': 1.5, 'peak': 2, 'time_ge2_percent': 50})
        self.assertEqual(attempted, {'intervals': 1, 'mean': .8, 'peak': 1, 'time_ge2_percent': 0})
        self.assertEqual(audit.request_overlap(rows, 'scheduled_ns', 60 * MS, 70 * MS),
                         {'intervals': 0, 'mean': 0, 'peak': 0, 'time_ge2_percent': 0})
        with self.assertRaises(ValueError):
            audit.request_overlap(rows, 'scheduled_ns', 0, 0)

    def test_lag_threshold_exact_boundary_and_failures_retained(self):
        rows = [request(0, 0, 1, 11), request(1, 1, 4, 15, 'failed'),
                request(2, 2, 2, 2, 'admission_rejected')]
        group = audit.lag_group(rows, 1)
        self.assertEqual(group['above_threshold'], 1)
        self.assertAlmostEqual(group['above_percent'], 100 / 3)
        self.assertEqual(group['statuses'], {'completed': 1, 'failed': 1, 'admission_rejected': 1})
        latency = audit.latency_group(rows)
        self.assertEqual((latency['n'], latency['successful']), (3, 1))
        self.assertEqual(latency['planned_ms']['mean'], 11)
        self.assertEqual(latency['after_attempt_ms']['mean'], 10)
        self.assertIsNone(audit.latency_group(rows[1:])['planned_ms']['mean'])
        self.assertIsNone(audit.lag_group([], 1)['above_percent'])

    def test_quantiles_measured_separately_not_subtracted(self):
        rows = [request(0, 0, 10, 11), request(1, 1, 2, 22)]
        latency = audit.latency_group(rows)
        lag = audit.lag_group(rows, 1)['lag_ms']
        self.assertEqual(latency['planned_ms']['p99'], 21)
        self.assertEqual(latency['after_attempt_ms']['p99'], 20)
        self.assertNotEqual(latency['planned_ms']['p99'] - lag['p99'],
                            latency['after_attempt_ms']['p99'])

    def test_top_neighbors_cross_cohort_and_whole_edges_unavailable(self):
        rows = [request(0, 0, 1, 50), request(1, 10, 22, 50),
                request(2, 20, 33, 50), request(3, 30, 34, 50)]
        result = audit.top_lag_rows(rows, rows[1:3], 0, 2)
        self.assertEqual([row['request'] for row in result], [2, 1])
        self.assertEqual((result[0]['previous_attempt_gap_ms'], result[0]['next_attempt_gap_ms']), (11, 1))
        self.assertEqual(result[1]['previous_attempt_gap_ms'], 21)
        edges = audit.top_lag_rows(rows, [rows[0], rows[-1]], 0, 10)
        by_id = {row['request']: row for row in edges}
        self.assertIsNone(by_id[0]['previous_attempt_gap_ms'])
        self.assertIsNone(by_id[3]['next_attempt_gap_ms'])
        self.assertEqual(audit.top_lag_rows(rows, rows, 0, 0), [])

    def test_phase_is_id_residue_not_task_pool_and_no_cohort_loss(self):
        rows = [request(7, 10, 12, 50), request(8, 11, 30, 30, 'failed')]
        run = {'summary': {'origin_ns': 0}, 'requests': rows,
               'cohort': rows, 'tasks': [{'home': 99, 'chosen': 98}]}
        result = audit.analyze(run, .01, .005, 3, 2, 0)
        self.assertEqual([row['n'] for row in result['phases']], [0, 1, 1])
        self.assertEqual(sum(row['n'] for _, row in result['strata']), 2)
        self.assertEqual(result['strata'][0][1]['successful'], 1)
        self.assertEqual(result['strata'][1][1]['statuses'], {'failed': 1})
        self.assertEqual(result['cohort']['lag_ms']['p99'], 19)
        self.assertEqual(audit.analyze(run, .01, .005, None, 2, 0)['phases'], [])


class AuditedCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='affinity-arrival-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / 'run'
        self.rows, self.tasks, self.summary = fixture(self.run, trace=False)

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(TOOL), str(self.run), *map(str, args)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_cli_trace_zero_phase_strata_late_outcomes_and_bounded_output(self):
        result = self.invoke('--warmup-seconds', '.01', '--window-seconds', '.04',
                             '--phase-modulus', 3, '--lag-threshold-ms', 10, '--top', 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        self.assertIn('cohort [0.01,0.05)s: n=4', text)
        self.assertIn('lag>10ms count=2/4, percent=50.000', text)
        self.assertIn('| 0 | 1 | {"admission_rejected":1} |', text)
        self.assertIn('| lag<=threshold | 2 | {"completed":1,"failed":1} | 1 | 40.000/40.000 | 35.000/35.000 |', text)
        self.assertIn('| lag>threshold | 2 | {"admission_rejected":1,"completed":1} | 1 | 60.000/60.000 | 40.000/40.000 |', text)
        self.assertIn('| 3 | admission_rejected | 30.000/50.000/50.000 | 20.000 | 20.000/0.000 | 25.000/10.000 |', text)
        self.assertIn('residue is NOT measured home', text)
        self.assertIn('quantiles must NOT be subtracted', text)
        self.assertIn('No driver scheduling cause is inferred', text)
        self.assertIn('Neither is Folly Q, callback I, kernel runq', text)
        self.assertLessEqual(len(text.splitlines()), 32)

    def test_cli_both_trace_modes_default_phase_disabled_and_empty_cohort(self):
        other = self.root / 'trace-one'
        fixture(other, trace=True)
        result = self.invoke(other, '--warmup-seconds', 0, '--window-seconds', '.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('\nRUN '), 2)
        self.assertIn('Phase grouping disabled', result.stdout)
        self.assertIn('trace=0', result.stdout)
        self.assertIn('trace=1', result.stdout)
        empty = self.invoke('--warmup-seconds', 1, '--window-seconds', 1, '--top', 0)
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn('cohort [1,2)s: n=0, {}', empty.stdout)
        self.assertIn('percent=unavailable', empty.stdout)
        self.assertIn('Top rows: none', empty.stdout)
        self.assertIn('time-weighted mean=0.000, peak=0', empty.stdout)

    def test_all_runs_audited_before_any_stdout(self):
        other = self.root / 'broken'
        fixture(other, trace=False)
        (other / 'requests.csv').unlink()
        result = self.invoke(other)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_attempt_order_equal_allowed_reverse_in_second_run_rejected(self):
        other = self.root / 'attempt-order'
        rows, _, _ = fixture(other, trace=False)
        rows[1]['submitted_ns'] = rows[2]['submitted_ns']
        csv_write(other / 'requests.csv', rows)
        equal = self.invoke(other, '--warmup-seconds', 0, '--window-seconds', '.1')
        self.assertEqual(equal.returncode, 0, equal.stderr)
        # Every row remains individually legal; schedule/checksum hashes are
        # unchanged. Even an empty cohort must not hide this driver violation.
        rows[1]['submitted_ns'] = rows[2]['submitted_ns'] + 5 * MS
        csv_write(other / 'requests.csv', rows)
        reversed_attempt = self.invoke(other)
        self.assertNotEqual(reversed_attempt.returncode, 0)
        self.assertEqual(reversed_attempt.stdout, '')
        self.assertIn('request attempts are not monotonic by request ID', reversed_attempt.stderr)

    def test_shared_audit_rejects_truncated_checksum_and_bad_task(self):
        csv_write(self.run / 'requests.csv', self.rows[:-1])
        self.assertNotEqual(self.invoke().returncode, 0)
        csv_write(self.run / 'requests.csv', self.rows)
        self.summary['checksum'] = 'incorrect'
        (self.run / 'summary.json').write_text(json.dumps(self.summary))
        self.assertNotEqual(self.invoke().returncode, 0)
        other = self.root / 'bad-task'
        _, tasks, _ = fixture(other, trace=True)
        tasks[0]['kind'] = 'operator'
        csv_write(other / 'tasks.csv', tasks)
        result = subprocess.run([sys.executable, str(TOOL), str(other)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('task kind does not match graph', result.stderr)

    def test_cli_parameter_limits(self):
        invalid = [('--warmup-seconds', '-1'), ('--warmup-seconds', 'nan'),
                   ('--warmup-seconds', 'inf'), ('--window-seconds', '0'),
                   ('--window-seconds', '-1'), ('--window-seconds', 'nan'),
                   ('--window-seconds', 'inf'), ('--window-seconds', '1e-20'),
                   ('--lag-threshold-ms', '-1'), ('--lag-threshold-ms', 'nan'),
                   ('--lag-threshold-ms', 'inf'), ('--phase-modulus', '0'),
                   ('--phase-modulus', '65'), ('--top', '-1'), ('--top', '11')]
        for args in invalid:
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
        for modulus in (1, 64):
            result = self.invoke('--phase-modulus', modulus, '--warmup-seconds', 0,
                                 '--window-seconds', '1e-9', '--lag-threshold-ms', 0, '--top', 10)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

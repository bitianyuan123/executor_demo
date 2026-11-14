#!/usr/bin/env python3
"""Read-only reporting must preserve failures and out-of-window completions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import terminal_report as report
import comparison_test as fixtures


class TerminalReportTest(unittest.TestCase):
    def fixture(self, root, count=1):
        phase = root / 's3-calibration'
        phase.mkdir()
        rows = []
        for seed in range(count):
            row = fixtures.ComparisonTest().fixture(phase, seed=seed)
            path = row['path']
            meta = json.loads((path / 'metadata.json').read_text())
            meta.update(instrumentation='queue', effective_masks=[[0, 1]], affinity='cluster', threads_per_pool=2)
            (path / 'metadata.json').write_text(json.dumps(meta))
            summary = json.loads((path / 'summary.json').read_text())
            summary.update(work_cpu_mean_ms=None, process_user_cpu_seconds=1.25,
                           process_system_cpu_seconds=0.25, observed_seconds=4.1,
                           sampling_truncated=False, sampling_failed=False, sample_rows_dropped=0)
            (path / 'summary.json').write_text(json.dumps(summary))
            row['path'] = str(path)
            row['repetition'] = row.pop('repeat')
            rows.append(row)
        data = {'runs': rows, 'binary_sha256': 'fixedhash',
                'analysis': {'warmup_seconds': 1, 'steady_end_seconds': 3}}
        (phase / 'manifest.json').write_text(json.dumps(data))
        (root / 'plan').mkdir()
        (root / 'plan' / 'plan.json').write_text(json.dumps({'phases': {'s3-calibration': {'runs': count}}}))
        return data

    def test_real_summarizer_preserves_cohort_and_unknown_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            lines = []
            self.assertTrue(report.phase_report(root, 's3-calibration', emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('recorded=1 planned=1 window=[1,3)', output)
            self.assertIn('planned_mean/p99=1005.000/', output)
            self.assertIn('callback_cpu_mean_ms=?', output)
            self.assertIn('whole_run_user/system_s=1.250/0.250', output)
            self.assertIn('window_times=4', output)
            self.assertIn('successful_unfinished(start/mid/end)=0/0/1', output)
            self.assertIn('cohort_target_mean_us=1000.000', output)
            self.assertIn('nominal_offered_cpu_s/s=0.001', output)

    def test_failed_or_missing_run_does_not_hide_other_results(self):
        for failure in ('exit', 'missing'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                data = self.fixture(root, 2)
                if failure == 'exit':
                    data['runs'][0]['returncode'] = 137
                else:
                    (Path(data['runs'][0]['path']) / 'summary.json').unlink()
                (root / 's3-calibration' / 'manifest.json').write_text(json.dumps(data))
                lines = []
                self.assertFalse(report.phase_report(root, 's3-calibration', emit=lines.append))
                output = '\n'.join(lines)
                self.assertIn('INCOMPLETE', output)
                self.assertIn('seed=1 rc=0', output)
                self.assertIn('LAT planned_mean', output)

    def test_partial_manifest_cannot_confirm_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.fixture(root, 2)
            data['runs'].pop()
            (root / 's3-calibration' / 'manifest.json').write_text(json.dumps(data))
            lines = []
            self.assertFalse(report.phase_report(root, 's3-calibration', emit=lines.append))
            self.assertIn('recorded=1 planned=2', '\n'.join(lines))
            (root / 's3-calibration' / 'manifest.json').write_text('{')
            self.assertFalse(report.phase_report(root, 's3-calibration', emit=lines.append))
            self.assertIn('retry after completion', '\n'.join(lines))

    def test_unshown_failed_attempt_and_duplicate_seed_remain_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.fixture(root, 2)
            data['runs'][1]['returncode'] = 137
            data['runs'][1]['seed'] = data['runs'][0]['seed']
            (root / 's3-calibration' / 'manifest.json').write_text(json.dumps(data))
            lines = []
            self.assertFalse(report.phase_report(root, 's3-calibration', limit=1, emit=lines.append))
            output = '\n'.join(lines)
            self.assertIn('duplicate case/seed', output)
            self.assertIn('nonzero_or_missing_rc=1', output)
            self.assertIn('unshown artifacts have not been checked', output)

    def test_truncated_sampling_does_not_look_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.fixture(root)
            path = Path(data['runs'][0]['path']) / 'summary.json'
            summary = json.loads(path.read_text())
            summary['sampling_truncated'] = True
            path.write_text(json.dumps(summary))
            lines = []
            self.assertFalse(report.phase_report(root, 's3-calibration', emit=lines.append))
            self.assertIn('truncated=True', '\n'.join(lines))

    def test_empty_page_never_suggests_no_progress_next_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            lines = []
            self.assertFalse(report.phase_report(root, 's3-calibration', offset=4, emit=lines.append))
            self.assertIn('NO ROWS', '\n'.join(lines))
            self.assertNotIn('NEXT:', '\n'.join(lines))

    def test_missing_and_unreadable_quota_stay_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'topology-before.json').write_text(json.dumps({'allowed_cpus': [0, 1]}))
            (root / 'topology-after.json').write_text(json.dumps({
                'allowed_cpus': [0, 1], 'cgroup_version': 'v1', 'cgroup_hierarchies': [{
                    'version': 1, 'controllers': ['cpu'], 'status': 'resolved', 'hidden_ancestors': True,
                    'ancestors': [{'path': '/cgroup', 'cpu.cfs_quota_us': None,
                                   'cpu.cfs_period_us': '100000', 'cpu.stat': 'nr_throttled 9\nthrottled_time 10'}]}]}))
            lines = []
            report.environment(root, emit=lines.append)
            output = '\n'.join(lines)
            self.assertIn('quota UNKNOWN', output)
            self.assertIn('hidden_parents=True', output)
            self.assertIn('cpu.cfs_quota_us=[?]', output)
            self.assertIn('cpu.stat=[nr_throttled 9 throttled_time 10]', output)


if __name__ == '__main__':
    unittest.main()

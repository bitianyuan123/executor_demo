#!/usr/bin/env python3
"""Frozen workload identity, safe resume and /proc/stat accounting checks."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('request_wave', ROOT / 'experiments/independent/scripts/request_wave.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class RequestWaveRunnerTests(unittest.TestCase):
    def make_plan(self, path, *extra):
        args = M.parser().parse_args(['plan', '--deployment', 'dev', '--out', str(path), *extra])
        with contextlib.redirect_stdout(io.StringIO()):
            return M.create_plan(args)

    def test_three_policies_share_one_frozen_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.make_plan(Path(directory), '--cpu-us', '1000')
            self.assertEqual(3, len(plan['policy_order']))
            self.assertEqual({'home', 'source_only', 'guarded'}, set(plan['policy_order']))
            self.assertEqual(12, plan['base']['arrival-rate'])
            self.assertEqual(80, plan['base']['steady-tasks'])
            self.assertEqual(20, plan['base']['burst-window-ms'])
            self.assertEqual('exact', plan['base']['mean-normalization'])
            self.assertEqual('outstanding', plan['base']['load-metric'])
            self.assertEqual(-1, plan['policies']['source_only']['accept-threshold'])
            self.assertEqual(.5, plan['policies']['guarded']['accept-threshold'])
            M.validate_plan(plan)
            plan['base']['cpu-us'] += 1
            with self.assertRaisesRegex(ValueError, 'frozen plan changed'):
                M.validate_plan(plan)
            with self.assertRaises(FileExistsError):
                self.make_plan(Path(directory))

    def test_arm_plan_checks_actual_allowed_cpus(self):
        with tempfile.TemporaryDirectory() as directory:
            args = M.parser().parse_args(['plan', '--out', directory, '--numa', '0,0,0,2,2,2'])
            with mock.patch.object(M.os, 'sched_getaffinity', return_value={0, 1, 2, 3}):
                with self.assertRaisesRegex(ValueError, 'allowed CPUs'):
                    M.create_plan(args)
            self.assertFalse((Path(directory) / 'plan.json').exists())

    def test_proc_stat_guest_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stat'
            path.write_text('cpu 0 0 0 0 0 0 0 0 0 0\ncpu0 10 2 3 80 2 1 1 1 9 1\n')
            current = M.read_cpu_stat(path)
        rows = M.cpu_rows({0: [0] * 8}, current, [[0]], 0, 100000000, 0)
        self.assertEqual(100, rows[0]['total_delta'])
        self.assertEqual(17, rows[0]['execution_pct'])
        self.assertEqual(2, rows[0]['iowait_pct'])
        self.assertEqual(1, rows[0]['steal_pct'])
        self.assertEqual(['union', 'pool0'], [r['scope'] for r in rows])

    def test_missing_cpu_and_regression_are_visible(self):
        previous = {0: [10] * 8, 1: [10] * 8}
        current = {0: [11] * 8, 1: [11] * 8}
        current[1][4] = 9  # Linux documents that iowait can occasionally decrease.
        rows = M.cpu_rows(previous, current, [[0], [1]], 0, 100000000, 0)
        self.assertEqual([0, 1, 0], [r['valid'] for r in rows])
        self.assertEqual('counter_regression', rows[0]['reason'])
        self.assertEqual('', rows[0]['execution_pct'])
        rows = M.cpu_rows(previous, {0: current[0]}, [[0], [1]], 0, 100000000, 0)
        self.assertEqual('missing_selected_cpu', rows[0]['reason'])

    def test_resume_requires_same_binary_complete_artifacts_and_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {'plan_sha256': 'plan', 'binary_sha256': 'binary', 'policy': 'home', 'options': {'seed': 1}}
            record = dict(expected, status='completed', cpu_observation={'status': 'ok'})
            (root / 'manifest.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'missing artifacts'):
                M.completed_run(root, expected)
            for name in ['data/tasks.csv', 'data/summary.json', 'data/metadata.json', 'cpu_samples.csv', 'stdout.log']:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('test artifact')
            record['artifact_sha256'] = {name: M.file_digest(root / name) for name in
                ['data/tasks.csv', 'data/summary.json', 'data/metadata.json', 'cpu_samples.csv', 'stdout.log']}
            (root / 'manifest.json').write_text(json.dumps(record))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(record, M.completed_run(root, expected))
            with self.assertRaisesRegex(ValueError, 'identity differs'):
                M.completed_run(root, dict(expected, binary_sha256='newbinary'))
            record['status'] = 'failed'
            (root / 'manifest.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'prior run is failed'):
                M.completed_run(root, expected)

    def test_timeout_preserves_drain_after_a_long_initial_release_window(self):
        # A valid large override must not be mistaken for a stuck child while
        # its final request is still releasing tasks after the offered window.
        options = {'duration': 60, 'burst-window-ms': 90000,
                   'steady-window-ms': 400, 'drain-seconds': 120}
        final_request_start = 60 - 1 / 12
        last_task_release = final_request_start + 90 + .4
        deadline = M.run_timeout_seconds(options)
        self.assertGreater(deadline, last_task_release + 120 + 70)
        self.assertAlmostEqual(340.4, deadline)

    def test_nonzero_child_is_recorded_and_never_retried_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.make_plan(root / 'plan', '--duration', '1', '--warmup', '.1')
            binary = root / 'fail'
            binary.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(.25)\nraise SystemExit(7)\n')
            binary.chmod(0o755)
            run = M.execute_one(plan, binary, M.file_digest(binary), root / 'run', 'home')
            self.assertEqual('failed', run['status'])
            self.assertEqual(7, run['returncode'])
            self.assertTrue(any('exit code 7' in x for x in run['errors']))
            self.assertTrue((root / 'run/cpu_samples.csv').is_file())
            with self.assertRaisesRegex(ValueError, 'prior run is failed'):
                M.execute_one(plan, binary, M.file_digest(binary), root / 'run', 'home')


if __name__ == '__main__':
    unittest.main()

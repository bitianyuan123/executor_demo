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

    def make_arm_short_plan(self, path, *extra):
        args = M.parser().parse_args(['plan', '--deployment', 'arm', '--profile', 'short-peak',
                                     '--numa', '0,0,0,2,2,2', '--out', str(path), *extra])
        with mock.patch.object(M, 'validate_resources'), contextlib.redirect_stdout(io.StringIO()):
            return M.create_plan(args)

    def test_short_peak_freezes_phase_work_and_only_two_policies(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.make_arm_short_plan(Path(directory))
            self.assertEqual(['home', 'source_only'], plan['policy_order'])
            self.assertEqual({'home', 'source_only'}, set(plan['policies']))
            self.assertEqual(128, plan['base']['threads'])
            self.assertEqual(1, plan['base']['burst-window-ms'])
            self.assertEqual(500, plan['base']['steady-window-ms'])
            self.assertEqual(1020, plan['base']['initial-cpu-us'])
            self.assertEqual(48980, plan['base']['steady-cpu-us'])
            self.assertEqual(0, plan['base']['estimate-us'])
            self.assertEqual(1, plan['cpu_trace']['bin_ms'])
            self.assertEqual(4000000, plan['phase_workload']['cpu_us_per_request'])
            self.assertEqual(50, plan['nominal']['execution_percent'])
            self.assertAlmostEqual(10, plan['phase_workload']['estimated_fluid_peak_ms'], delta=.01)
            self.assertLess(plan['phase_workload']['steady_nominal_pool_utilization'], .5)
            M.validate_plan(plan)

    def test_short_peak_initial_override_rebalances_default_steady_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.make_arm_short_plan(Path(directory), '--initial-cpu-us', '1500')
            self.assertEqual(48500, plan['base']['steady-cpu-us'])
            self.assertEqual(50, plan['nominal']['execution_percent'])
            self.assertAlmostEqual(120 / (16 - 3.88 / .5), plan['phase_workload']['estimated_fluid_peak_ms'])
            self.assertEqual({'initial': True, 'steady': False}, plan['phase_workload']['phase_budget_overrides'])

    def test_short_peak_explicit_phase_budgets_report_actual_nominal_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.make_arm_short_plan(Path(directory), '--initial-cpu-us', '1200', '--steady-cpu-us', '45000')
            self.assertAlmostEqual(46.2, plan['nominal']['execution_percent'])
            self.assertEqual(.5, plan['phase_workload']['requested_target_utilization'])
            self.assertAlmostEqual(.462, plan['phase_workload']['actual_nominal_utilization'])

    def test_short_peak_rejects_incompatible_or_overloaded_models(self):
        cases = [(['--cpu-us', '25000'], 'incompatible'),
                 (['--target-utilization', '1'], 'target utilization'),
                 (['--peak-target-ms', '500'], 'peak target'),
                 (['--burst-window-ms', '20'], 'release window'),
                 (['--steady-window-ms', '400'], 'same-home'),
                 (['--initial-cpu-us', '0'], 'positive'),
                 (['--steady-cpu-us', '100000'], 'saturates'),
                 (['--capacities', '8,8,8,8,8,8'], 'equal CPU groups'),
                 (['--cpu-trace', '--cpu-bin-ms', '5'], '<=2ms')]
        with tempfile.TemporaryDirectory() as directory:
            for i, (options, error) in enumerate(cases):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, error):
                    self.make_arm_short_plan(Path(directory) / str(i), *options)

    def test_short_peak_rejects_parameters_incompatible_with_shape_contract(self):
        cases = [(['--target-utilization', '.4'], 'frozen 45-55%'),
                 (['--target-utilization', '.6'], 'frozen 45-55%'),
                 (['--peak-target-ms', '4'], 'frozen waveform contract'),
                 (['--peak-target-ms', '21'], 'frozen waveform contract'),
                 (['--request-rate', '200'], 'period must exceed 30ms'),
                 (['--threads', '15'], 'at least one worker'),
                 (['--steady-cpu-us', '30000'], 'rounded phase budgets'),
                 (['--steady-cpu-us', '60000'], 'rounded phase budgets')]
        with tempfile.TemporaryDirectory() as directory:
            for i, (options, error) in enumerate(cases):
                output = Path(directory) / str(i)
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, error):
                    self.make_arm_short_plan(output, *options)
                self.assertFalse((output / 'plan.json').exists())

    def test_short_peak_boundary_targets_preserve_actual_demand_and_background(self):
        with tempfile.TemporaryDirectory() as directory:
            for i, (rho, peak) in enumerate(((.45, 5), (.55, 20))):
                plan = self.make_arm_short_plan(Path(directory) / str(i),
                                                '--target-utilization', str(rho), '--peak-target-ms', str(peak))
                self.assertAlmostEqual(rho * 100, plan['nominal']['execution_percent'])
                self.assertGreater(plan['nominal']['per_pool_request_gap_ms'], 30)
                self.assertAlmostEqual(peak, plan['phase_workload']['estimated_fluid_peak_ms'], delta=.02)
                self.assertEqual(M.waveform_acceptance_contract()['union_mean_range_pct'],
                                 plan['waveform_acceptance']['union_mean_range_pct'])

    def test_waveform_gate_never_uses_coarse_cpu_or_missing_shape_as_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.make_arm_short_plan(Path(directory) / 'plain')
            self.assertFalse(M.home_waveform_gate(plan, None)['comparison_allowed'])
            dev = dict(plan, deployment='dev')
            gate = M.home_waveform_gate(dev, None)
            self.assertEqual('functional_only', gate['status'])
            self.assertFalse(gate['passed'])
            self.assertTrue(gate['comparison_allowed'])
            plan = self.make_arm_short_plan(Path(directory) / 'traced', '--cpu-trace')
            self.assertFalse(M.home_waveform_gate(plan, {'runs': [{'policy': 'home', 'status': 'ok'}]})['comparison_allowed'])
            result = {'runs': [{'policy': 'home', 'status': 'ok',
                               'request_waveform': {'passed': True, 'status': 'passed', 'reasons': []}}]}
            self.assertTrue(M.home_waveform_gate(plan, result)['comparison_allowed'])
            result['runs'][0]['status'] = 'partial'
            self.assertFalse(M.home_waveform_gate(plan, result)['comparison_allowed'])

    def test_failed_home_shape_preserves_results_and_blocks_arm_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.make_arm_short_plan(root / 'plan', '--cpu-trace')
            binary = root / 'binary'
            binary.write_text('placeholder')
            binary.chmod(0o755)
            args = M.parser().parse_args(['run', '--plan', str(root / 'plan/plan.json'), '--binary', str(binary)])
            run = {'policy': 'home', 'status': 'completed', 'cpu_observation': {'status': 'ok'}}
            curves = {'runs': [{'policy': 'home', 'status': 'ok',
                               'request_waveform': {'passed': False, 'status': 'failed', 'reasons': ['peak too wide']}}]}
            with mock.patch.object(M, 'validate_resources'), mock.patch.object(M, 'execute_one', return_value=run) as launch, \
                    mock.patch.object(M, 'analyze_saved', return_value=curves), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'source_only was not launched'):
                    M.execute(args)
            self.assertEqual(['home'], [call.args[4] for call in launch.call_args_list])
            manifest = json.loads((root / 'plan/run_manifest.json').read_text())
            self.assertEqual(plan['policy_order'], manifest['policy_order'])
            self.assertTrue(manifest['analysis_only'])
            self.assertFalse(manifest['home_waveform_gate']['passed'])

    def test_only_home_keeps_plan_order_and_resumes_remaining_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.make_arm_short_plan(root / 'plan', '--cpu-trace')
            binary = root / 'binary'
            binary.write_text('placeholder')
            binary.chmod(0o755)
            argv = ['run', '--plan', str(root / 'plan/plan.json'), '--binary', str(binary)]
            curves = {'runs': [{'policy': 'home', 'status': 'ok',
                               'request_waveform': {'passed': True, 'status': 'passed', 'reasons': []}}]}
            def result(_plan, _binary, _sha, _directory, policy, _calibration):
                return {'policy': policy, 'status': 'completed', 'cpu_observation': {'status': 'ok'}}
            with mock.patch.object(M, 'validate_resources'), mock.patch.object(M, 'execute_one', side_effect=result) as launch, \
                    mock.patch.object(M, 'analyze_saved', return_value=curves), contextlib.redirect_stdout(io.StringIO()):
                M.execute(M.parser().parse_args(argv + ['--only', 'home']))
                first = json.loads((root / 'plan/run_manifest.json').read_text())
                self.assertEqual(['home', 'source_only'], first['policy_order'])
                self.assertEqual(['home'], first['last_selection'])
                self.assertTrue(first['analysis_only'])
                M.execute(M.parser().parse_args(argv))
                final = json.loads((root / 'plan/run_manifest.json').read_text())
            # execute_one's verified completed_run path reuses home, tested below;
            # the top-level resume must still visit it to verify saved artifacts.
            self.assertEqual(['home', 'home', 'source_only'], [call.args[4] for call in launch.call_args_list])
            self.assertEqual(['home', 'source_only'], [run['policy'] for run in final['runs']])
            self.assertFalse(final['analysis_only'])
            self.assertEqual(plan['plan_sha256'], final['plan_sha256'])

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

    def test_cpu_trace_is_explicit_and_does_not_change_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = self.make_plan(root / 'plain')
            traced = self.make_plan(root / 'traced', '--cpu-trace', '--cpu-bin-ms', '5')
            self.assertEqual(plain['base'], traced['base'])
            self.assertFalse(M.trace_config(plain)['enabled'])
            self.assertTrue(M.trace_config(traced)['enabled'])
            self.assertEqual(100, traced['base']['sample-ms'])
            self.assertNotEqual(plain['plan_sha256'], traced['plan_sha256'])
            legacy = dict(plain)
            legacy.pop('cpu_trace')
            legacy['plan_sha256'] = M.digest({k: v for k, v in legacy.items() if k != 'plan_sha256'})
            M.validate_plan(legacy)
            self.assertFalse(M.trace_config(legacy)['enabled'])

    def test_trace_validation_rejects_overlapping_observer_and_invalid_bin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'bin'):
                self.make_plan(root / 'invalid', '--cpu-trace', '--cpu-bin-ms', '0')
            first_cpu = min(os.sched_getaffinity(0))
            with self.assertRaisesRegex(ValueError, 'outside'):
                self.make_plan(root / 'overlap', '--cpu-trace', '--observer-cpus', str(first_cpu))

    def test_trace_failure_stops_before_workload_and_records_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.make_plan(root / 'plan', '--cpu-trace')
            capture = mock.Mock()
            capture.start.side_effect = RuntimeError('tracefs denied')
            capture.stop.return_value = {'status': 'failed', 'errors': ['tracefs denied']}
            with mock.patch.object(M, 'new_trace_capture', return_value=capture), \
                    mock.patch.object(M.subprocess, 'Popen') as popen:
                run = M.execute_one(plan, Path('/not-launched'), 'binary', root / 'run', 'home')
            popen.assert_not_called()
            capture.stop.assert_called_once()
            self.assertEqual('failed', run['status'])
            self.assertTrue(any('tracefs denied' in e for e in run['errors']))
            self.assertEqual('failed', json.loads((root / 'run/manifest.json').read_text())['status'])

    def test_trace_resume_requires_trace_artifacts_and_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {'plan_sha256': 'plan', 'binary_sha256': 'binary', 'policy': 'home',
                        'options': {}, 'cpu_trace_enabled': True}
            record = dict(expected, status='completed', cpu_observation={'status': 'ok'},
                          cpu_trace={'status': 'failed'})
            (root / 'manifest.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'invalid high-resolution'):
                M.completed_run(root, expected)
            record['cpu_trace']['status'] = 'ok'
            (root / 'manifest.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'missing artifacts'):
                M.completed_run(root, expected)

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

#!/usr/bin/env python3
"""Bounded capture contract tests; no performance workload or perf privilege."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('request_cpu_capture', ROOT / 'scripts/run_request_cpu_capture.py')
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)
helper_spec = importlib.util.spec_from_file_location('request_cpu_capture_workload', cap.BOUNDARY_HELPER)
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


class CaptureTests(unittest.TestCase):
    def args(self, directory, **changes):
        args = cap.parse_args(['--output', str(Path(directory) / 'new'), '--binary', str(Path(directory) / 'demo')])
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def test_default_plan_does_not_probe_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'never-written'
            with mock.patch.object(cap.shutil, 'which', side_effect=AssertionError('dependency probe')), \
                 mock.patch.object(cap.subprocess, 'Popen', side_effect=AssertionError('workload')), \
                 contextlib.redirect_stdout(io.StringIO()) as printed:
                code = cap.main(['--output', str(output)])
            self.assertEqual(code, 0)
            self.assertFalse(output.exists())
            self.assertIn('PLAN ONLY', printed.getvalue())
            self.assertIn('--clockid mono -c 1', printed.getvalue())

    def test_only_one_round_and_every_event_no_pid_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, groups = cap.make_commands(self.args(directory))
            self.assertEqual(len(commands), 3)
            self.assertEqual(sum(map(len, groups)), 96)
            self.assertEqual(commands['record'].count('record'), 1)
            self.assertEqual(commands['record'][:4], ['taskset', '-c', '0-47,192-239', 'perf'])
            self.assertNotIn('--exclude-perf', commands['record'])
            self.assertNotIn('--pid', commands['record'])
            self.assertIn('0-47,192-239', commands['record'])
            self.assertEqual(commands['record'][commands['record'].index('--requests') + 1], '24')
            self.assertIn('trace:cpu,time,event,trace', commands['script'])
            self.assertIn('--show-lost-events', commands['script'])
            self.assertIn(str(cap.BOUNDARY_HELPER), commands['record'])
            self.assertIn('--boundary-output', commands['record'])
            helper_index = commands['record'].index(str(cap.BOUNDARY_HELPER))
            separator = commands['record'].index('--', helper_index)
            self.assertEqual(commands['record'][separator + 1:separator + 4],
                             ['taskset', '-c', '0-47,192-239'])

    def test_explicit_interior_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            args = cap.parse_args(['--output', str(Path(directory)/'new'), '--requests', '24', '--request-id', '6'])
            commands, _ = cap.make_commands(args)
            profile = commands['profile']
            self.assertEqual(profile[profile.index('--request') + 1], '6')
        for value in ('-1', '24'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cap.parse_args(['--output', 'unused', '--requests', '24', '--request-id', value])

    def test_invalid_partitions_refused(self):
        for value in ('', '0-1;1-2', '1-0', '0,0', '1024', 'a', '0;;1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cap.partitions(value)

    def test_no_perf_never_launches_or_writes(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(cap.shutil, 'which', return_value=None), \
             mock.patch.object(cap.subprocess, 'Popen', side_effect=AssertionError('workload')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            args = self.args(directory)
            code = cap.main(['--run', '--output', args.output, '--binary', args.binary])
            self.assertEqual(code, 1)
            self.assertFalse(Path(args.output).exists())
            self.assertIn('must already be installed', err.getvalue())

    def test_cpu_preflight_requires_exact_mask(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            Path(args.binary).write_text('fake')
            Path(args.binary).chmod(0o755)
            with mock.patch.object(cap.shutil, 'which', side_effect=lambda name: '/fake/' + name), \
                 mock.patch.object(cap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[0]', '')):
                with self.assertRaisesRegex(ValueError, 'mask differs'):
                    cap.preflight(args, cap.partitions(args.cpus))
            self.assertFalse(Path(args.output).exists())

    def simulate(self, directory, failure=None, loss=False, bad_boundaries=False):
        args = self.args(directory)
        Path(args.binary).write_text('fake binary')
        phases = []

        def run(command, stdout, stderr, timeout=120):
            phase = ('record' if 'record' in command else 'script' if 'script' in command else 'profile')
            phases.append(phase)
            Path(stderr).write_text('')
            if phase == 'record':
                Path(stdout).write_text('demo done\n')
                (Path(args.output) / 'perf.data').write_bytes(b'perf')
                (Path(args.output) / 'boundaries.json').write_text(json.dumps({
                    'schema': 'request-sched-boundaries-v1', 'clock': 'CLOCK_MONOTONIC',
                    'cpus': sorted(cpu for group in cap.partitions(args.cpus) for cpu in group),
                    'pre': {'completed': True}, 'post': {'completed': not bad_boundaries},
                    'demo_returncode': 0, 'error': None}))
            elif phase == 'script':
                Path(stdout).write_text('PERF_RECORD_LOST lost 9\n' if loss else '[000] 1.000000001: sched:sched_switch: sample\n')
            else:
                metadata = json.loads((Path(args.output) / 'capture.json').read_text())
                self.assertTrue(metadata['capture_completed'])
                (Path(args.output) / 'profile_capture.sha256').write_text(cap.sha256(Path(args.output) / 'capture.json'))
                Path(stdout).write_text('profile complete\n')
            return (9 if failure == phase else 0), False

        commands, groups = cap.make_commands(args)
        with mock.patch.object(cap, 'preflight', return_value=('/fake/perf', '/fake/taskset', 'perf test')), \
             mock.patch.object(cap, 'run_to_files', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            if failure or loss or bad_boundaries:
                with self.assertRaises(RuntimeError):
                    cap.execute(args, commands, groups)
            else:
                self.assertEqual(cap.execute(args, commands, groups), 0)
        metadata = json.loads((Path(args.output) / 'capture.json').read_text())
        return metadata, phases

    def test_success_writes_provenance_then_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, phases = self.simulate(directory)
            self.assertEqual(phases, ['record', 'script', 'profile'])
            self.assertTrue(metadata['capture_completed'])
            self.assertEqual(metadata['schema'], 'request-sched-capture-v1')
            self.assertEqual(metadata['clock'], 'CLOCK_MONOTONIC')
            self.assertIsNone(metadata['profile_returncode'])
            output = Path(directory) / 'new'
            execution = json.loads((output / 'execution.json').read_text())
            self.assertEqual(execution['profile_returncode'], 0)
            self.assertEqual((output / 'profile_capture.sha256').read_text(), cap.sha256(output / 'capture.json'))
            self.assertEqual(execution['capture_sha256'], cap.sha256(output / 'capture.json'))
            self.assertEqual(len(metadata['source_sha256']['sched_text']), 64)
            self.assertEqual(metadata['source_sha256']['boundaries'], cap.sha256(output / 'boundaries.json'))
            self.assertEqual(metadata['source_sha256']['boundary_helper'], cap.sha256(cap.BOUNDARY_HELPER))

    def test_incomplete_boundaries_retained_and_not_profiled(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, phases = self.simulate(directory, bad_boundaries=True)
            self.assertEqual(phases, ['record', 'script'])
            self.assertFalse(metadata['capture_completed'])
            self.assertIn('boundaries incomplete', metadata['error'])

    def test_profile_failure_preserves_frozen_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, phases = self.simulate(directory, failure='profile')
            self.assertEqual(phases, ['record', 'script', 'profile'])
            self.assertTrue(metadata['capture_completed'])
            self.assertIsNone(metadata['error'])
            output = Path(directory) / 'new'
            execution = json.loads((output / 'execution.json').read_text())
            self.assertEqual(execution['profile_returncode'], 9)
            self.assertIn('offline profile failed', execution['error'])
            self.assertEqual((output / 'profile_capture.sha256').read_text(), cap.sha256(output / 'capture.json'))

    def test_record_failure_stops_before_export(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, phases = self.simulate(directory, failure='record')
            self.assertEqual(phases, ['record'])
            self.assertFalse(metadata['capture_completed'])
            self.assertEqual(metadata['record_returncode'], 9)
            self.assertIsNone(metadata['script_returncode'])
            self.assertIn('record failed', metadata['error'])

    def test_script_failure_stops_before_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, phases = self.simulate(directory, failure='script')
            self.assertEqual(phases, ['record', 'script'])
            self.assertFalse(metadata['capture_completed'])

    def test_lost_events_retained_and_not_profiled(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata, phases = self.simulate(directory, loss=True)
            self.assertEqual(phases, ['record', 'script'])
            self.assertTrue(metadata['lost_events_detected'])
            self.assertFalse(metadata['capture_completed'])

    def test_loss_detection_does_not_flag_zero_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stderr'
            path.write_text('lost 0 samples\n')
            self.assertFalse(cap.lost_events([path]))
            path.write_text('Warning: lost 12 samples\n')
            self.assertTrue(cap.lost_events([path]))

    def test_timeout_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            process = mock.Mock(pid=12345)
            process.wait.side_effect = [subprocess.TimeoutExpired(['fake'], 120), None, None]
            with mock.patch.object(cap.subprocess, 'Popen', return_value=process) as launch, \
                 mock.patch.object(cap.os, 'killpg') as kill:
                result = cap.run_to_files(['fake'], Path(directory) / 'out', Path(directory) / 'err')
            self.assertEqual(result, (124, True))
            self.assertTrue(launch.call_args.kwargs['start_new_session'])
            self.assertEqual(kill.call_args_list, [mock.call(12345, cap.signal.SIGTERM), mock.call(12345, cap.signal.SIGKILL)])


class BoundaryTests(unittest.TestCase):
    def test_every_cpu_sleeps_and_original_union_restored(self):
        with mock.patch.object(helper.os, 'sched_getaffinity', return_value={2, 7}), \
             mock.patch.object(helper.os, 'sched_setaffinity') as affinity, \
             mock.patch.object(helper.time, 'sleep') as sleep:
            result = helper.boundary([2, 7])
        self.assertTrue(result['completed'])
        self.assertEqual([v['cpu'] for v in result['visits']], [2, 7])
        self.assertEqual(affinity.call_args_list, [mock.call(0, {2}), mock.call(0, {7}), mock.call(0, {2, 7})])
        self.assertEqual(sleep.call_args_list, [mock.call(.001), mock.call(.001)])
        self.assertLessEqual(result['started_monotonic_ns'], result['finished_monotonic_ns'])

    def test_boundary_failure_restores_union(self):
        with mock.patch.object(helper.os, 'sched_getaffinity', return_value={2, 7}), \
             mock.patch.object(helper.os, 'sched_setaffinity') as affinity, \
             mock.patch.object(helper.time, 'sleep', side_effect=OSError('sleep failed')):
            with self.assertRaisesRegex(OSError, 'sleep failed'):
                helper.boundary([2, 7])
        self.assertEqual(affinity.call_args_list[-1], mock.call(0, {2, 7}))

    def test_mismatched_union_refuses_boundary(self):
        with mock.patch.object(helper.os, 'sched_getaffinity', return_value={2}), \
             mock.patch.object(helper.os, 'sched_setaffinity') as affinity:
            with self.assertRaisesRegex(ValueError, 'differs'):
                helper.boundary([2, 7])
        affinity.assert_not_called()

    def test_demo_command_preserved_and_failure_still_closes_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'boundaries.json'
            order = []
            command = ['binary path', '--literal', 'a; $x', '--other', 'one two']
            def boundary(cpus):
                order.append('boundary')
                return {'completed': True, 'visits': [{'cpu': c} for c in cpus]}
            def demo(args):
                self.assertEqual(args, command)
                order.append('demo')
                return subprocess.CompletedProcess(args, 9)
            with mock.patch.object(helper, 'boundary', side_effect=boundary), \
                 mock.patch.object(helper.subprocess, 'run', side_effect=demo) as launch:
                self.assertEqual(helper.execute([2, 7], command, output), 9)
            self.assertEqual(order, ['boundary', 'demo', 'boundary'])
            self.assertEqual(launch.call_args.kwargs, {})  # Inherit outer process group.
            result = json.loads(output.read_text())
            self.assertEqual(result['demo_command'], command)
            self.assertEqual(result['demo_returncode'], 9)
            self.assertTrue(result['post']['completed'])

    def test_pre_boundary_failure_never_starts_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'boundaries.json'
            with mock.patch.object(helper, 'boundary', side_effect=OSError('cannot pin')), \
                 mock.patch.object(helper.subprocess, 'run') as launch:
                with self.assertRaisesRegex(OSError, 'cannot pin'):
                    helper.execute([2], ['demo'], output)
            launch.assert_not_called()
            self.assertIn('cannot pin', json.loads(output.read_text())['error'])

    def test_exec_failure_attempts_post_boundary_and_retains_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'boundaries.json'
            with mock.patch.object(helper, 'boundary', return_value={'completed': True}) as boundary, \
                 mock.patch.object(helper.subprocess, 'run', side_effect=OSError('exec failed')):
                with self.assertRaisesRegex(OSError, 'exec failed'):
                    helper.execute([2], ['demo'], output)
            self.assertEqual(boundary.call_count, 2)
            result = json.loads(output.read_text())
            self.assertTrue(result['post']['completed'])
            self.assertIn('exec failed', result['error'])

    def test_cli_preserves_command_after_separator(self):
        with mock.patch.object(helper, 'execute', return_value=0) as execute:
            self.assertEqual(helper.main(['--cpus', '0-1,7', '--boundary-output', 'boundaries.json',
                                          '--', 'taskset', '-c', '0-1,7', 'demo', '--requests', '24']), 0)
        execute.assert_called_once_with([0, 1, 7], ['taskset', '-c', '0-1,7', 'demo', '--requests', '24'],
                                        'boundaries.json')


if __name__ == '__main__':
    unittest.main()

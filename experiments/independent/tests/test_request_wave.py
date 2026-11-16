#!/usr/bin/env python3
"""Request-wave release/accounting contracts, not performance assertions."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()


class RequestWave(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, **overrides):
        self.case += 1
        output = self.root / str(self.case)
        args = {'output': output, 'workload': 'request-wave', 'pools': 2,
                'threads': 2, 'arrival-rate': 100, 'duration': .025,
                'burst-min': 4, 'burst-max': 4, 'burst-window-ms': 20,
                'steady-tasks': 3, 'steady-window-ms': 60, 'cpu-us': 100,
                'jitter': .5, 'long-prob': 0, 'mean-normalization': 'exact',
                'load-observation': 'basic', 'sample-ms': 2, 'drain-seconds': 1}
        args.update(overrides)
        result = subprocess.run([str(BINARY), *[v for key, value in args.items()
                                for v in ('--'+key, str(value))]], text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        meta = json.loads((output / 'metadata.json').read_text())
        summary = json.loads((output / 'summary.json').read_text())
        with (output / 'tasks.csv').open() as stream:
            tasks = list(csv.DictReader(stream))
        with (output / 'bursts.csv').open() as stream:
            bursts = list(csv.DictReader(stream))
        self.assertTrue(summary['conservation_ok'])
        self.assertTrue(summary['counters_balanced'])
        self.assertEqual(summary['completed_tasks'], len(tasks))
        self.assertTrue(all(t['status'] == 'completed' for t in tasks))
        self.assertEqual(meta['planned_cpu_us'], sum(int(t['target_cpu_us']) for t in tasks))
        self.assertEqual(meta['planned_cpu_us'], meta['planned_initial_cpu_us'] + meta['planned_steady_cpu_us'])
        self.assertGreater(meta['epoch_monotonic_ns'], 0)
        for task in tasks:
            self.assertLessEqual(int(task['request_planned_ns']), int(task['planned_ns']))
            self.assertLessEqual(int(task['planned_ns']), int(task['submit_ns']))
            self.assertLessEqual(int(task['submit_ns']), int(task['start_ns']))
            self.assertLessEqual(int(task['start_ns']), int(task['end_ns']))
        return meta, summary, tasks, bursts

    def test_overlapping_waves_are_globally_ordered_and_not_truncated(self):
        meta, summary, tasks, bursts = self.run_case()
        self.assertEqual(meta['planned_arrivals'], 3)
        self.assertEqual(len(tasks), 21)
        self.assertEqual(meta['planned_initial_tasks'], 12)
        self.assertEqual(meta['planned_steady_tasks'], 9)
        self.assertEqual(meta['last_task_release_ns'], 80_000_000)
        self.assertGreater(meta['last_task_release_ns'], meta['duration_seconds'] * 1e9)
        self.assertEqual(summary['complete_bursts'], 3)
        ordered = sorted(tasks, key=lambda t: (int(t['planned_ns']), int(t['task'])))
        observed = sorted(tasks, key=lambda t: (int(t['submit_ns']), int(t['task'])))
        self.assertEqual([t['task'] for t in ordered], [t['task'] for t in observed])
        # Request-contiguous storage cannot itself be used as submission order.
        self.assertNotEqual([t['task'] for t in tasks], [t['task'] for t in observed])
        for request in range(3):
            group = [t for t in tasks if int(t['burst']) == request]
            self.assertEqual(len(group), 7)
            self.assertEqual([int(t['burst_index']) for t in group], list(range(7)))
            self.assertEqual([t['phase'] for t in group], ['initial'] * 4 + ['steady'] * 3)
            self.assertEqual([int(t['planned_ns']) - int(t['request_planned_ns']) for t in group],
                             [0, 5_000_000, 10_000_000, 15_000_000, 20_000_000, 40_000_000, 60_000_000])
            self.assertTrue(all(int(t['home_pool']) == request % 2 for t in group))
            self.assertTrue(all(int(t['burst_index']) + int(t['burst_remaining']) == 6 for t in group))
            self.assertEqual(int(bursts[request]['tasks']), 7)
            self.assertEqual(int(bursts[request]['actual_ns']), int(group[0]['submit_ns']))

    def test_tail_gets_drain_after_final_release(self):
        # Last releases are later than duration+drain. They must still be offered.
        meta, summary, tasks, _ = self.run_case(**{'duration': .001, 'drain-seconds': .01})
        self.assertEqual(len(tasks), 7)
        self.assertGreater(int(tasks[-1]['submit_ns']), 11_000_000)
        self.assertEqual(summary['rejected_tasks'], 0)
        self.assertEqual(summary['canceled_tasks'], 0)

    def test_policy_reuses_exact_input_trace_and_acceptance_telemetry(self):
        cases = []
        for routing, acceptance in [('home', -1), ('threshold', -1), ('threshold', 0)]:
            meta, summary, tasks, _ = self.run_case(**{'routing': routing,
                'accept-threshold': acceptance, 'load-threshold': 0,
                'burst-window-ms': 0, 'cpu-us': 2000, 'capacities': '1,1'})
            cases.append(tasks)
            self.assertEqual(meta['accept_threshold'], acceptance)
            for field in ('source_over_threshold', 'no_eligible_target', 'no_improvement'):
                self.assertEqual(summary[field + '_tasks'], sum(int(t[field]) for t in tasks))
            if routing == 'home':
                self.assertEqual(summary['source_over_threshold_tasks'], 0)
            if acceptance == 0:
                self.assertGreater(summary['no_eligible_target_tasks'], 0)
                self.assertEqual(summary['spilled_tasks'], 0)
                self.assertEqual(summary['no_improvement_tasks'], 0)
                self.assertTrue(all(t['pool'] == t['home_pool'] for t in tasks))
        keys = ('task', 'burst', 'planned_ns', 'request_planned_ns', 'phase', 'target_cpu_us', 'home_pool')
        traces = [[[t[k] for k in keys] for t in tasks] for tasks in cases]
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(traces[0], traces[2])

    def test_flat_defaults_preserved_and_no_steady_wave(self):
        meta, _, tasks, _ = self.run_case(workload='flat', **{'max-planned-tasks': 12})
        self.assertEqual(len(tasks), 12)
        self.assertEqual(meta['planned_steady_tasks'], 0)
        self.assertTrue(all(t['request_planned_ns'] == t['planned_ns'] for t in tasks))
        self.assertTrue(all(t['phase'] == 'initial' for t in tasks))
        meta, _, tasks, _ = self.run_case(**{'steady-tasks': 0})
        self.assertEqual(len(tasks), 12)
        self.assertEqual(meta['planned_steady_cpu_us'], 0)

    def test_invalid_wave_and_acceptance_options(self):
        for args in (['--workload', 'bad'], ['--accept-threshold', '-2'],
                     ['--accept-threshold', 'nan'], ['--burst-window-ms', '-1'],
                     ['--steady-window-ms', '0'],
                     ['--workload', 'request-wave', '--burst-placement', 'spread'],
                     ['--workload', 'request-wave', '--max-planned-tasks', '80']):
            result = subprocess.run([str(BINARY), *args, '--output', str(self.root / 'invalid')],
                                    text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, args)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Validate exogenous work/arrival/RPC signatures and capacity-plan algebra."""
import csv
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import unittest

BINARY = str(Path(sys.argv.pop(1)).resolve()) if len(sys.argv) > 1 else None
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dag_graph_audit', HERE / 'analyze_graph.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def rows(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.cpu = str(min(os.sched_getaffinity(0)))

    def generate(self, *extra, success=True):
        output = self.path / f'cases-{len(list(self.path.iterdir()))}.json'
        result = subprocess.run([sys.executable, str(HERE / 'make_cases.py'), '--cpus', self.cpu,
                                 '--numa', '0', '--threads', '2', '--output', str(output), *extra],
                                capture_output=True, text=True)
        if not success:
            self.assertNotEqual(result.returncode, 0)
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output.read_text()), json.loads(output.with_suffix('.budget.json').read_text())

    def test_fixed_business_mean_and_budget(self):
        cases, budget = self.generate('--family', 'production', '--effective-cpus', '48')
        self.assertEqual(len(cases), 32)
        self.assertEqual({row['operators'] for row in budget['cases']}, {20, 60, 200, 400})
        for row in budget['cases']:
            self.assertAlmostEqual(row['mean_cpu_us'], 100000, delta=.5)
            self.assertAlmostEqual(row['expected_cpu_demand'], 12 * (row['operators'] * row['mean_cpu_us'] + 200) / 1e6)
            self.assertAlmostEqual(row['rho'], row['expected_cpu_demand'] / 48)
        for case in cases:
            values = dict(zip(case['args'][::2], case['args'][1::2]))
            self.assertEqual(values['--qps'], '12')
            self.assertEqual(sum(int(x) for x in values['--wave-widths'].split(',')), int(case['name'].split('-n')[1].split('-')[0]))

    def test_capacity_sweep_is_separate(self):
        cases, budget = self.generate('--family', 'capacity', '--effective-cpus', '2', '--load-ratios', '.5,1.05')
        self.assertEqual(len(cases), 16)
        for row in budget['cases']:
            self.assertAlmostEqual(row['rho'], row['requested_rho'], delta=.0002)
            self.assertEqual(row['operators'], 60)
            self.assertEqual(row['qps'], 12)
        self.generate('--family', 'capacity', success=False)

    def test_continuation_controls_match(self):
        cases, _ = self.generate('--family', 'continuation')
        self.assertEqual(len(cases), 10)
        for mechanism in ('chain', 'rpc'):
            group = [c for c in cases if f'continuation-{mechanism}-' in c['name']]
            variants = []
            for case in group:
                options = dict(zip(case['args'][::2], case['args'][1::2]))
                options.pop('--policy'); options.pop('--balance-forks')
                variants.append(options)
            self.assertTrue(all(item == variants[0] for item in variants))

    def test_invalid_target_and_impossible_mean(self):
        self.generate('--family', 'production', '--deployment', 'target', success=False)
        self.generate('--family', 'production', '--cpu-mean-us', '100', success=False)

    def test_locality_controls_hold_fixed_work(self):
        cases, _ = self.generate('--family', 'locality', '--iterations-per-us', '64', '--memory-sizes-kb', '0,256')
        self.assertEqual(len(cases), 12)
        for case in cases:
            values = dict(zip(case['args'][::2], case['args'][1::2]))
            self.assertEqual(values['--work-mode'], 'iterations')
            self.assertIn(values['--candidate-scope'], ('all', 'preferred-numa'))
            self.assertEqual(values['--cross-numa-penalty-us'], '0')
        self.generate('--family', 'locality', success=False)
        self.generate('--family', 'locality', '--iterations-per-us', '64', '--affinity', 'none', success=False)


@unittest.skipUnless(BINARY, 'pass compiled executor_demo for runtime tests')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.counter = 0

    def run_demo(self, *extra):
        self.counter += 1
        output = self.path / str(self.counter)
        result = subprocess.run([BINARY, '--output', str(output), '--pools', '2', '--threads', '2',
            '--affinity', 'none', '--qps', '1000', '--duration', '.25', '--requests', '30',
            '--graph', 'waves', '--wave-widths', '6,2', '--wave-depths', '2,1',
            '--cpu-us', '30', '--join-us', '5', '--jitter', '.2', '--long-prob', '.2',
            '--long-min-us', '200', '--long-max-us', '300', '--arrival', 'poisson',
            '--rpc-prob', '.35', '--rpc-delay-us', '1000', '--rpc-jitter', '.75',
            '--max-inflight', '512', '--drain-seconds', '10', *extra], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = audit.analyze(output)
        self.assertEqual(result['excluded_requests'], 0)
        return output

    def signature(self, path):
        meta = json.loads((path / 'metadata.json').read_text())
        requests = rows(path / 'requests.csv')
        origin = meta['arrival_origin_ns']
        arrival = sorted((int(r['request_id']), int(r['scheduled_ns']) - origin, int(r['planned_cpu_us'])) for r in requests)
        tasks = sorted((int(t['request_id']), int(t['node_id']), int(t['stage']), int(t['work_target_us']),
                        int(t['rpc_delay_target_us'])) for t in rows(path / 'tasks.csv'))
        return arrival, tasks

    def test_routing_preserves_arrivals_work_and_rpc(self):
        home = self.run_demo('--policy', 'home')
        sticky = self.run_demo('--policy', 'sticky')
        self.assertEqual(self.signature(home), self.signature(sticky))
        arrivals, tasks = self.signature(home)
        intervals = [b[1] - a[1] for a, b in zip(arrivals, arrivals[1:])]
        self.assertGreater(len(set(intervals)), 10)
        delays = [t[-1] for t in tasks if t[2] == 1]
        self.assertGreater(len(set(delays)), 10)
        self.assertTrue(all(250 <= d <= 1750 for d in delays))

    def test_absolute_tail_matches_mean_and_range(self):
        out = self.run_demo('--requests', '200', '--qps', '10000', '--duration', '.2',
                            '--wave-widths', '50', '--wave-depths', '1', '--cpu-us', '87',
                            '--long-prob', '.02', '--long-min-us', '700', '--long-max-us', '800',
                            '--rpc-prob', '0', '--work-mode', 'iterations', '--iterations-per-us', '1')
        work = [int(t['work_target_us']) for t in rows(out / 'tasks.csv') if t['kind'] == 'operator']
        self.assertEqual(len(work), 10000)
        self.assertAlmostEqual(statistics.mean(work), .98 * 87 + .02 * 750, delta=4)
        tail = [v for v in work if v > 105]
        self.assertGreater(len(tail), 100)
        self.assertTrue(all(700 <= v <= 800 for v in tail))

    def test_poisson_count_not_clipped_to_expected_count(self):
        # Fixed seed has a realization above E[N]=2; requests=0 must retain it.
        counts = []
        for seed in (1, 6, 9):
            out = self.run_demo('--requests', '0', '--duration', '.02', '--qps', '100', '--seed', str(seed),
                                '--rpc-prob', '0')
            counts.append(len(rows(out / 'requests.csv')))
        self.assertGreater(max(counts), 2)

    def test_candidate_scope_requires_real_bound_numa(self):
        cpu = min(os.sched_getaffinity(0))
        nodes = list(Path(f'/sys/devices/system/cpu/cpu{cpu}').glob('node[0-9]*'))
        if len(nodes) != 1:
            self.skipTest('host does not expose unique NUMA membership')
        node = int(nodes[0].name[4:])
        out = self.run_demo('--candidate-scope', 'preferred-numa', '--affinity', 'cluster',
                            '--cpus', f'{cpu};{cpu}', '--numa', f'{node},{node}', '--requests', '2')
        metadata = json.loads((out / 'metadata.json').read_text())
        self.assertTrue(metadata['numa_labels_verified'])
        for extra in [[], ['--affinity', 'cluster', '--cpus', f'{cpu};{cpu}', '--numa', f'{node+1},{node+1}']]:
            result = subprocess.run([BINARY, '--output', str(self.path / 'invalid'), '--pools', '2',
                                     '--candidate-scope', 'preferred-numa', *extra], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Paired instrumentation audits with synthetic affinity-v2 files; no load generation."""

import csv
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'trace_cost_report.py'
SPEC = importlib.util.spec_from_file_location('trace_cost_report', TOOL)
cost = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cost)
MS = 1000000


def write_csv(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_fixture(path, *, trace, strategy='home', seed=420, latencies=(10, 20, 30),
                  cpu_s=1, origin=100 * MS, failed_request=None):
    """Mirror resolved fields/options emitted by main.cpp's current write()."""
    policy = 'home' if strategy == 'home' else 'outstanding'
    balance = 'fork' if strategy == 'fork-parent' else 'all'
    options = {'trace': str(int(trace)), 'policy': policy, 'balance-at': balance,
               'continuation': 'parent', 'seed': str(seed), 'output': str(path),
               'busy-high': '1', 'cross-numa': '0', 'max-hops': '4',
               'home-policy': 'rr', 'iterations': '1', 'accesses': '0',
               'requests': '3', 'qps': '100', 'widths': '1', 'depths': '1',
               'model': 'none', 'cpus': '0;1',
               'affinity': 'cluster', 'threads': '1', 'cpu-sampling': '0'}
    contract = {'seed': seed, 'requests': 3, 'qps': 100, 'arrival': 'regular',
                'widths': [1], 'depths': [1], 'model': 'none', 'iterations': 1,
                'accesses': 0, 'jitter': 0, 'long_every': 0, 'long_factor': 8,
                'shared_bytes': 0, 'branch_bytes': 0, 'work_scale_scope': 'task',
                'sync_block_percent': 0, 'sync_block_us': 0}
    signature = json.dumps(contract, sort_keys=True)
    summary = {'schema': 'affinity-v2', 'origin_ns': origin, 'seed': seed,
               'options': options, 'work_signature': signature, 'work_contract': contract,
               'fixed_work_signature': json.dumps({key: value for key, value in contract.items()
                                                   if key not in ('arrival', 'qps', 'sync_block_percent', 'sync_block_us')}, sort_keys=True),
               'policy': policy, 'balance_at': balance, 'continuation': 'parent',
               'affinity': 'cluster', 'model': 'none', 'requests': 3,
               'nodes_per_request': 3, 'widths': [1], 'depths': [1],
               'groups': [{'cpus': [0], 'capacity': 1, 'numa': 0}, {'cpus': [1], 'capacity': 1, 'numa': 0}],
               'threads_per_pool': 1, 'pool_count': 2, 'qps': 100, 'iterations': 1, 'accesses': 0,
               'arrival': 'regular', 'burst_size': 4, 'burst_spacing_us': 0,
               'trace': trace, 'completed': 3 - int(failed_request is not None),
               'failed_or_rejected': int(failed_request is not None),
               'observed_tasks': 9 if trace else 0, 'migrations': 0, 'cross_numa': 0,
               'process_cpu_s': cpu_s, 'peak_inflight': 2, 'duration_with_drain_s': .05,
               'page_query_enabled': False, 'max_inflight': 16, 'memory_mb': 256,
               'sync_block_percent': 0, 'sync_block_us': 0, 'cpu_sampling': False,
               'cpu_sample_ms': 50, 'cpu_max_age_ms': 100,
               'routing': {'busy_high': 1, 'return_low': .5, 'min_gain': 0, 'cross_numa_penalty': 0,
                           'cross_numa': False, 'max_hops': 4, 'batch_chunk': 1, 'max_outstanding': 0,
                           'estimate_us': 1000, 'home_policy': 'rr', 'estimate_mode': 'constant'},
               'planned_total_iterations': 3, 'planned_total_accesses': 0,
               'planned_sync_block_us': 0, 'planned_blocked_operators': 0,
               'payload_bytes_per_request': 0, 'payload_budget_at_max_inflight': 0,
               'load_scorer': 'outstanding', 'load_score_units': 'outstanding_tasks_per_capacity'}
    requests, tasks = [], []
    arrival = checksum = 0
    for number, latency in enumerate(latencies):
        scheduled = origin + number * 10 * MS
        submitted = scheduled + MS
        status = 'failed' if number == failed_request else 'completed'
        requests.append({'request': number, 'scheduled_ns': scheduled, 'submitted_ns': submitted,
                         'finished_ns': scheduled + latency * MS, 'status': status,
                         'concurrent': 1, 'checksum': number + 1})
        arrival = cost.report.mix(arrival ^ (scheduled - origin))
        if status == 'completed':
            checksum = cost.report.mix(checksum ^ (number + 1))
        if trace:
            for tag, kind in enumerate(('init', 'operator', 'join')):
                tasks.append({'request': number, 'tag': tag, 'kind': kind,
                              'home': 0, 'source': 0, 'chosen': 0, 'hops': 0, 'reason': 0,
                              'migrated': 0, 'cross_numa': 0, 'queue_before': 0,
                              'outstanding_before': 1, 'callback_inflight_before': 0,
                              'submitted_ns': submitted + tag * MS,
                              'start_ns': submitted + (tag + 1) * MS,
                              'finish_ns': submitted + (tag + 2) * MS,
                              'cpu_ns': MS, 'iterations': int(kind == 'operator'),
                              'read_words': 0, 'write_words': 0, 'checksum': tag})
    summary.update(arrival_hash=str(arrival), checksum=str(checksum))
    path.mkdir()
    (path / 'summary.json').write_text(json.dumps(summary))
    write_csv(path / 'requests.csv', requests)
    if trace:
        write_csv(path / 'tasks.csv', tasks)
    return summary, requests


class TraceCostTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='affinity-trace-cost-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, name, **kwargs):
        path = self.root / name
        write_fixture(path, **kwargs)
        return path

    def load(self, paths, warmup=0, window=None):
        return [cost.report.load_run(path, warmup, window) for path in paths]

    def invoke(self, paths, *args):
        return subprocess.run([sys.executable, str(TOOL), *map(str, paths), *map(str, args)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def modify_summary(self, path, update):
        summary = json.loads((path / 'summary.json').read_text())
        update(summary)
        (path / 'summary.json').write_text(json.dumps(summary))

    def pair(self, prefix='pair', strategy='home'):
        return [self.fixture(prefix + '-off', trace=False, strategy=strategy),
                self.fixture(prefix + '-on', trace=True, strategy=strategy,
                             latencies=(11, 22, 35), cpu_s=1.1, origin=200 * MS)]

    def test_late_completions_match_relative_arrivals_and_whole_cpu(self):
        paths = self.pair()
        runs = self.load(paths, warmup=.01, window=.01)
        result = cost.analyze(runs)
        pair = result['pairs'][0]
        self.assertEqual(pair['off']['metrics']['n'], 1)
        self.assertEqual(pair['off']['metrics']['mean_ms'], 20)
        self.assertEqual(pair['on']['metrics']['mean_ms'], 22)
        self.assertEqual(pair['differences']['mean_ms'], {'absolute': 2, 'percent': 10})
        self.assertEqual(pair['differences']['p99_ms']['absolute'], 2)
        self.assertAlmostEqual(pair['differences']['cpu_per_request_ms']['absolute'], 100 / 3)
        self.assertAlmostEqual(pair['differences']['cpu_per_request_ms']['percent'], 10)
        self.assertEqual(runs[0]['tasks'], [])
        self.assertIsNone(runs[0]['metrics']['operator_cpu_mean_ms'])
        output = self.invoke(paths, '--warmup-seconds', '.01', '--window-seconds', '.01')
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn('unavailable at trace=0, not zero', output.stdout)
        self.assertIn('WHOLE-run execution', output.stdout)
        self.assertIn('whole_attempt_lag_p99_ms=1.000', output.stdout)
        self.assertIn('interaction unavailable', output.stdout)

    def test_full_six_cells_report_descriptive_difference_in_differences(self):
        paths = []
        for name, off_latency, on_latency in (('home', 30, 35), ('fork-parent', 20, 22), ('all-parent', 19, 24)):
            for trace, latency in ((False, off_latency), (True, on_latency)):
                # Directory names intentionally do not identify the strategy.
                paths.append(self.fixture(f'misleading-{len(paths)}', trace=trace, strategy=name,
                                          latencies=(latency,) * 3))
        result = cost.analyze(self.load(paths))
        self.assertEqual(len(result['pairs']), 3)
        self.assertEqual(result['interactions'], [{'seed': 420, 'trace0_all_minus_fork_ms': -1,
                                                  'trace1_all_minus_fork_ms': 2,
                                                  'difference_in_differences_ms': 3}])
        output = self.invoke(paths)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn('| 420 | -1.000 | 2.000 | 3.000 |', output.stdout)
        self.assertLess(len(output.stdout.splitlines()), 30)
        self.assertNotIn('interaction unavailable', output.stdout)

    def test_incomplete_six_cells_do_not_invent_interaction(self):
        result = cost.analyze(self.load(self.pair('fork', 'fork-parent') + self.pair('all', 'all-parent')))
        self.assertEqual(len(result['pairs']), 2)
        self.assertEqual(result['interactions'], [])
        self.assertEqual(result['incomplete_interaction_seeds'], [420])

    def test_missing_duplicate_and_empty_cohort_are_rejected(self):
        paths = self.pair()
        for selected, expected in ((paths[:1], 'missing trace pair'), (paths + paths[:1], 'duplicate trace=')):
            with self.subTest(expected=expected):
                output = self.invoke(selected)
                self.assertEqual(output.returncode, 2)
                self.assertIn(expected, output.stderr)
                self.assertNotIn('| seed / actual strategy', output.stdout)
        output = self.invoke(paths, '--warmup-seconds', '1')
        self.assertEqual(output.returncode, 2)
        self.assertIn('empty cohort', output.stderr)

    def test_wrong_options_and_resolved_configuration_are_rejected(self):
        mutations = [lambda s: s['options'].update({'min-gain': '0.5'}),
                     lambda s: s['routing'].update({'min_gain': .5}),
                     lambda s: s['groups'][0].update({'capacity': 2}),
                     lambda s: s.update(cpu_sampling=True),
                     lambda s: s['options'].update({'trace': '0'}),
                     lambda s: s.update(balance_at='fork'),
                     lambda s: s.update(work_signature='different-work')]
        for index, change in enumerate(mutations):
            with self.subTest(change=index):
                paths = self.pair(f'config-{index}')
                self.modify_summary(paths[1], change)
                output = self.invoke(paths)
                self.assertEqual(output.returncode, 2)
                self.assertIn('comparison refused', output.stderr)
                self.assertNotIn('| seed / actual strategy', output.stdout)

    def test_cross_strategy_mismatch_invalidates_six_cell_claim(self):
        paths = self.pair('home', 'home') + self.pair('fork', 'fork-parent') + self.pair('all', 'all-parent')
        # Internally legal trace pairs, but all-parent has an extra threshold
        # change. A strategy interaction must not silently pool these inputs.
        for path in paths[-2:]:
            self.modify_summary(path, lambda s: s['options'].update({'min-gain': '1'}))
        output = self.invoke(paths)
        self.assertEqual(output.returncode, 2)
        self.assertIn('min-gain', output.stderr)
        self.assertNotIn('| seed / actual strategy', output.stdout)

    def test_failure_outside_cohort_preserves_evidence_and_refuses_cost(self):
        paths = [self.fixture('failed-off', trace=False, failed_request=0),
                 self.fixture('failed-on', trace=True, failed_request=0)]
        output = self.invoke(paths, '--warmup-seconds', '.01', '--window-seconds', '.01')
        self.assertEqual(output.returncode, 2)
        self.assertIn('"failed": 1', output.stdout)
        self.assertIn('cohort_completed/offered=1/1', output.stdout)
        self.assertIn('whole-run failure/rejection', output.stderr)
        self.assertNotIn('| seed / actual strategy', output.stdout)

    def test_matching_aggregate_checksum_does_not_hide_request_mismatch(self):
        paths = self.pair()
        rows = cost.report.read_csv(paths[1] / 'requests.csv', ('checksum',))
        # mix is bijective; changing checksums a,b to a', mix(a)^b^mix(a')
        # preserves their aggregate fold. Later requests remain unchanged.
        a, b = int(rows[0]['checksum']), int(rows[1]['checksum'])
        rows[0]['checksum'] = 123
        rows[1]['checksum'] = cost.report.mix(a) ^ b ^ cost.report.mix(123)
        write_csv(paths[1] / 'requests.csv', rows)
        runs = self.load(paths)  # The shared aggregate audit still passes.
        self.assertEqual(runs[0]['summary']['checksum'], runs[1]['summary']['checksum'])
        with self.assertRaisesRegex(ValueError, 'per-request schedule/status/checksum differs'):
            cost.analyze(runs)

    def test_generic_policy_pair_zero_denominator_and_invalid_cli(self):
        paths = self.pair()
        for path in paths:
            def change(summary):
                summary.update(policy='active', load_scorer='active',
                               load_score_units='queue_plus_callback_inflight_per_capacity')
                summary['options']['policy'] = 'active'
                summary['process_cpu_s'] = 0
            self.modify_summary(path, change)
        result = cost.analyze(self.load(paths))
        self.assertEqual(result['pairs'][0]['strategy'], 'active/all/parent')
        self.assertIsNone(result['pairs'][0]['differences']['cpu_per_request_ms']['percent'])
        for args in (('--window-seconds', 'nan'), ('--window-seconds', '1e-20'),
                     ('--warmup-seconds', '-1')):
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(paths, *args).returncode, 0)


if __name__ == '__main__':
    unittest.main()

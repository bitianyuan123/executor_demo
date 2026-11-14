#!/usr/bin/env python3
"""Validate planning contracts and captured topology without running executors."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_target_validation', ROOT/'scripts/prepare_target_validation.py')
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


def evidence():
    groups = []
    records = []
    node_cpus = {0: set(), 1: set()}
    for group in range(6):
        mask = {100+group*2, 101+group*2, 200+group*2, 201+group*2}
        groups.append(p.compact(mask))
        node_cpus[group//3].update(mask)
        for cpu in mask:
            sibling = cpu+100 if cpu < 200 else cpu-100
            records.append({'cpu': cpu, 'topology': {'cluster_cpus_list': p.compact(mask),
                            'thread_siblings_list': p.compact({cpu, sibling})}})
    allowed = sorted(set().union(*node_cpus.values()) | {900})
    topology = {'allowed_cpus': allowed, 'cpus': records,
                'numa_nodes': {f'node{node}': p.compact(mask) for node, mask in node_cpus.items()}}
    return topology, ';'.join(groups), '0,0,0,1,1,1'


def args(tier='pilot', *extra):
    topology, cpus, numa = evidence()
    options = p.parser().parse_args(['--topology', 'unused.json', '--cpus', cpus, '--numa', numa,
                                    '--output', 'unused-output', '--tier', tier, *extra])
    return options, topology


def argdict(case):
    return dict(zip(case['args'][::2], case['args'][1::2]))


class TargetValidationTest(unittest.TestCase):
    def test_cross_host_full_clusters_and_resource_summary(self):
        opts, topology = args()
        with patch('os.sched_getaffinity', side_effect=AssertionError('must not inspect planner host')):
            bundle = p.build_bundle(opts, topology)
        resource = bundle['plan.json']['resource']
        self.assertEqual(resource['logical_cpus'], 24)
        self.assertEqual(resource['physical_cores_from_sibling_lists'], 12)
        self.assertEqual(resource['reported_smt_widths'], [2])
        self.assertNotIn(900, resource['allowed_union'])
        self.assertEqual(resource['numa_nodes_used'], [0, 1])
        self.assertEqual(resource['allowed_union_cpulist'], '100-111,200-211')

    def test_reject_partial_overlapping_outside_and_wrong_numa(self):
        topology, cpus, numa = evidence()
        groups = cpus.split(';')
        partial = list(groups); partial[0] = '100,200'
        overlapping = list(groups); overlapping[1] = groups[0]
        for selected, labels in [(';'.join(partial), numa), (';'.join(overlapping), numa),
                                 (cpus, '1,0,0,1,1,1'), (';'.join(groups[:5]), '0,0,0,1,1')]:
            with self.subTest(selected=selected, labels=labels), self.assertRaises(ValueError):
                p.validate_topology(topology, selected, labels)
        bad = copy.deepcopy(topology); bad['allowed_cpus'].remove(100)
        with self.assertRaisesRegex(ValueError, 'outside'): p.validate_topology(bad, cpus, numa)

    def test_reject_missing_or_inconsistent_cluster_evidence(self):
        topology, cpus, numa = evidence()
        for kind in ('missing_cpu', 'missing_cluster', 'inconsistent_cluster', 'missing_numa'):
            bad = copy.deepcopy(topology)
            if kind == 'missing_cpu': bad['cpus'].pop()
            elif kind == 'missing_cluster': bad['cpus'][0]['topology']['cluster_cpus_list'] = None
            elif kind == 'inconsistent_cluster': bad['cpus'][0]['topology']['cluster_cpus_list'] = '100-111,200-211'
            else: bad['numa_nodes'].pop('node0')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                p.validate_topology(bad, cpus, numa)

    def test_pilot_counts_runtime_and_no_calibration_contract(self):
        bundle = p.build_bundle(*args())
        plan = bundle['plan.json']
        self.assertEqual(len(plan['phases']), 7)
        self.assertEqual(plan['total_runs'], 65)
        self.assertAlmostEqual(plan['total_planned_offered_minutes'], 58.15)
        self.assertEqual(plan['measurement'], {'duration_seconds': 60, 'warmup_seconds': 10, 'repeats': 3})
        self.assertEqual(plan['seed'], 420)
        self.assertNotIn('s3-calibration-contrasts.json', bundle)
        self.assertNotIn('s3-smoke-contrasts.json', bundle)
        self.assertEqual(bundle['s3-smoke.json']['repeats'], 1)
        self.assertEqual(bundle['s3-smoke.json']['base']['duration'], 3)
        self.assertEqual(bundle['s3-calibration.json']['base']['instrumentation'], 'queue')
        self.assertEqual([c['options']['arrival-rate'] for c in bundle['s3-calibration.json']['cases']], [120, 240, 480, 720])

    def test_confirm_counts_but_smoke_and_calibration_stay_short(self):
        bundle = p.build_bundle(*args('confirm'))
        plan = bundle['plan.json']
        self.assertEqual(plan['total_runs'], 137)
        self.assertAlmostEqual(plan['total_planned_offered_minutes'], 634.15)
        self.assertEqual(plan['phases']['s3-burst']['duration_seconds'], 300)
        self.assertEqual(plan['phases']['s3-burst']['warmup_seconds'], 30)
        self.assertEqual(plan['phases']['s3-calibration']['duration_seconds'], 30)
        self.assertEqual(plan['phases']['s3-smoke']['duration_seconds'], 3)
        self.assertEqual(plan['phases']['s4-routing']['runner_arguments_required']['--repeats'], 7)
        self.assertEqual(plan['seed'], 1420)
        self.assertEqual(bundle['s3-burst.json']['seed'], 1420)
        self.assertEqual(plan['phases']['s4-routing']['runner_arguments_required']['--seed'], 1420)

    def test_seed_override_is_shared_by_all_runners_and_bounded(self):
        bundle = p.build_bundle(*args('confirm', '--seed', '92000'))
        self.assertEqual(bundle['plan.json']['seed'], 92000)
        self.assertEqual(bundle['s3-calibration.json']['seed'], 92000)
        self.assertEqual(bundle['plan.json']['phases']['s4-routing']['runner_arguments_required']['--seed'], 92000)
        for seed in ('-1', str((1 << 64)-1)):
            with self.assertRaisesRegex(ValueError, 'seed range'):
                p.build_bundle(*args('confirm', '--seed', seed))

    def test_equal_work_triangle_and_exact_contracts(self):
        bundle = p.build_bundle(*args())
        matrix = bundle['s3-burst.json']
        self.assertEqual(matrix['base']['mean-normalization'], 'exact')
        self.assertEqual([c['name'] for c in matrix['cases']], ['smooth', 'spread', 'local'])
        full = [dict(matrix['base'], **case['options']) for case in matrix['cases']]
        self.assertEqual([c['arrival-rate']*c['burst-max'] for c in full], [240]*3)
        spec = bundle['s3-burst-contrasts.json']
        self.assertEqual(spec['window'], {'start_s': 10, 'end_s': 60})
        self.assertEqual(spec['minimum_pairs'], 5)
        self.assertEqual(spec['minimum_tail_observations'], 20)
        first, second = spec['comparisons']
        self.assertEqual(first['workload']['arrival'], 'count')
        self.assertEqual(second['workload']['arrival'], 'exact')
        for a, b, contract in [(full[0], full[1], first), (full[1], full[2], second)]:
            changed = {key for key in set(a)|set(b) if a.get(key) != b.get(key)}
            self.assertEqual(changed, set(contract['allowed_changes']))
            self.assertTrue(contract['workload']['same_cohort_ids'])
            self.assertEqual(contract['workload']['cohort_total_relative_tolerance'], 0)

    def test_require_exact_burst_cohort_boundaries(self):
        with self.assertRaisesRegex(ValueError, 'divisible by 100'):
            p.build_bundle(*args('pilot', '--task-rate', '241'))
        bundle = p.build_bundle(*args('pilot', '--task-rate', '250'))
        self.assertEqual(bundle['s3-burst.json']['cases'][1]['options']['arrival-rate'], 2.5)
        for extra in [('--task-rate', 'nan'), ('--dag-qps', 'inf'), ('--threads', '0'), ('--cpu-us', '0')]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                p.build_bundle(*args('pilot', *extra))

    def test_float_division_and_nanosecond_boundary_safety(self):
        for tier in ('pilot', 'confirm'):
            for rate in (110, 270, 490):
                with self.subTest(tier=tier, rate=rate), self.assertRaisesRegex(ValueError, 'floating arrival boundary'):
                    p.build_bundle(*args(tier, '--task-rate', str(rate)))
            for rate in (120, 240):
                with self.subTest(tier=tier, rate=rate):
                    bundle = p.build_bundle(*args(tier, '--task-rate', str(rate)))
                    self.assertEqual(bundle['s3-burst.json']['cases'][0]['options']['arrival-rate'], rate)

    def test_dag_executable_bounds_fail_before_bundle_output(self):
        for tier, extra in [('pilot', ('--threads', '4097')),
                            ('pilot', ('--dag-cpu-us', '1250001')),
                            ('pilot', ('--dag-qps', '20000')),
                            ('confirm', ('--dag-qps', '4000'))]:
            with self.subTest(tier=tier, extra=extra), self.assertRaises(ValueError):
                p.build_bundle(*args(tier, *extra))
        bundle = p.build_bundle(*args('pilot', '--threads', '4096', '--dag-cpu-us', '1250000'))
        self.assertEqual(argdict(bundle['s4-routing.json'][0])['--threads'], '4096')
        self.assertEqual(argdict(bundle['s4-routing.json'][0])['--cpu-us'], '1250000')

    def test_independent_deployment_and_thread_controls(self):
        bundle = p.build_bundle(*args())
        self.assertEqual([c['name'] for c in bundle['s3-deployment.json']['cases']], ['none', 'numa', 'cluster'])
        self.assertEqual([c['options']['threads'] for c in bundle['s3-threads.json']['cases']], [8, 16, 64, 256])
        for name in ('s3-burst', 's3-deployment', 's3-threads'):
            common = bundle[name+'.json']['base']
            self.assertEqual(common['max-outstanding'], 8192)
            self.assertEqual(common['drain-seconds'], 60)
            self.assertEqual(common['instrumentation'], 'full')

    def test_dag_graph_budget_and_all_execution_parameters_explicit(self):
        bundle = p.build_bundle(*args())
        cases = bundle['s4-routing.json']
        self.assertEqual([case['name'] for case in cases], ['home', 'initial', 'all', 'sticky', 'borrow'])
        for case in cases:
            options = argdict(case)
            self.assertEqual(options['--wave-widths'], '40,10')
            self.assertEqual(options['--wave-depths'], '1,2')
            self.assertEqual(options['--cpu-us'], '20000')
            self.assertEqual(options['--duration'], '60')
            self.assertEqual(options['--qps'], '12')
            self.assertEqual(options['--arrival'], 'regular')
            self.assertEqual(options['--max-inflight'], '128')
            self.assertEqual(options['--drain-seconds'], '60')
            self.assertNotIn('--seed', options)
        self.assertAlmostEqual(bundle['plan.json']['phases']['s4-routing']['nominal_cpu_demand_seconds_per_second'], 14.4024)

    def test_dag_contracts_match_only_intended_option_differences(self):
        bundle = p.build_bundle(*args())
        for family in ('s4-baseline', 's4-routing'):
            cases = {case['name']: argdict(case) for case in bundle[family+'.json']}
            for contract in bundle[family+'-contrasts.json']['comparisons']:
                a, b = cases[contract['baseline']], cases[contract['candidate']]
                changed = {key[2:] for key in set(a)|set(b) if a.get(key) != b.get(key)}
                self.assertEqual(changed, set(contract['allowed_changes']))
                self.assertEqual(contract['workload'], p.strict_workload())
        contrasts = bundle['s4-routing-contrasts.json']['comparisons']
        self.assertEqual(contrasts[2]['name'], 'nonfork-submission-balancing-added')
        self.assertIn('joins', bundle['plan.json']['phases']['s4-routing']['question'])

    def test_cli_only_writes_fresh_bundle_and_records_source_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            topology, cpus, numa = evidence()
            source = root/'topology.json'; source.write_text(json.dumps(topology))
            output = root/'nested'/'bundle'
            command = [sys.executable, str(ROOT/'scripts/prepare_target_validation.py'), '--topology', str(source),
                       '--cpus', cpus, '--numa', numa, '--output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('No workload was executed', result.stdout)
            self.assertEqual(len(list(output.iterdir())), 13)
            plan = json.loads((output/'plan.json').read_text())
            self.assertEqual(len(plan['topology_source']['sha256']), 64)
            original = (output/'plan.json').read_bytes()
            again = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual((output/'plan.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()

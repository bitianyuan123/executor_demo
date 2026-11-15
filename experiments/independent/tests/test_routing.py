#!/usr/bin/env python3
"""Routing contract and counter lifecycle; no latency/throughput assertions."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = Path(sys.argv.pop(1)).resolve()


class Routing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case = 0

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, expected=0, **overrides):
        self.case += 1
        output = self.root / str(self.case)
        args = {'output': output, 'pools': 2, 'threads': 2, 'arrivals': 2,
                'duration': .03, 'arrival-rate': 1000, 'burst-min': 12,
                'burst-max': 12, 'cpu-us': 1000, 'jitter': .5, 'long-prob': 0,
                'sample-ms': 1, 'drain-seconds': 2, 'progress-us': 100}
        args.update(overrides)
        result = subprocess.run([str(BINARY), *[v for k, value in args.items()
                                for v in ('--'+k, str(value))]], text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        meta = json.loads((output / 'metadata.json').read_text())
        summary = json.loads((output / 'summary.json').read_text())
        with (output / 'tasks.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        with (output / 'pools.csv').open() as stream:
            samples = list(csv.DictReader(stream))
        self.assertTrue(summary['conservation_ok'])
        self.assertTrue(summary['counters_balanced'])
        self.assertEqual(summary['final_outstanding'], 0)
        for counter in summary['final_pool_counters']:
            self.assertTrue(all(value == 0 for key, value in counter.items() if key != 'pool'), counter)
        self.assertEqual(sum(summary[k] for k in ('completed_tasks', 'rejected_tasks',
                         'canceled_tasks', 'failed_tasks')), len(rows))
        self.assertEqual(summary['spilled_tasks'], sum(int(r['spilled']) for r in rows))
        for row in rows:
            if row['status'] == 'completed':
                self.assertLessEqual(int(row['planned_ns']), int(row['submit_ns']))
                self.assertLessEqual(int(row['submit_ns']), int(row['start_ns']))
                self.assertLessEqual(int(row['start_ns']), int(row['end_ns']))
            self.assertEqual(int(row['burst_index']) + int(row['burst_remaining']),
                             int(args['burst-max'])-1)
            self.assertEqual(float(row['capacity']), meta['capacities'][int(row['pool'])])
            if int(row['spilled']):
                self.assertNotEqual(row['home_pool'], row['pool'])
                self.assertNotIn(row['status'], ('rejected', 'deadline_rejected'))
        return meta, summary, rows, samples

    def test_old_default_off_home(self):
        meta, summary, rows, samples = self.run_case()
        self.assertEqual(meta['routing'], 'home')
        self.assertEqual(meta['effective_load_observation'], 'off')
        self.assertEqual(summary['spilled_tasks'], 0)
        self.assertIsNone(summary['route_ns_mean'])
        for row in rows:
            self.assertEqual(row['home_pool'], row['pool'])
            for key in ('home_native_queue', 'home_outstanding', 'home_work_estimate_us',
                        'home_remaining_work_us', 'selected_native_queue', 'selected_outstanding',
                        'selected_work_estimate_us', 'selected_remaining_work_us', 'home_score',
                        'selected_score', 'route_ns', 'route_candidates', 'home_inflight', 'selected_inflight'):
                self.assertEqual(float(row[key]), -1, key)
        self.assertTrue(all(int(s['outstanding']) == -1 and int(s['remaining_work_us']) == -1 for s in samples))

    def test_home_observes_without_spill(self):
        meta, summary, rows, _ = self.run_case(**{'load-observation': 'progress', 'capacities': '1,2'})
        self.assertEqual(meta['capacities'], [1, 2])
        self.assertEqual(summary['spilled_tasks'], 0)
        for row in rows:
            self.assertEqual(row['home_pool'], row['pool'])
            self.assertEqual(row['home_outstanding'], row['selected_outstanding'])
            self.assertGreaterEqual(int(row['route_ns']), 0)
            self.assertEqual(int(row['route_candidates']), 1)
            self.assertAlmostEqual(float(row['home_score']), int(row['home_outstanding']) / float(row['capacity']))

    def test_policy_changes_only_assignment(self):
        cases = []
        for routing in ('home', 'threshold', 'spread'):
            meta, summary, rows, _ = self.run_case(**{'routing': routing, 'load-observation': 'progress',
                                                     'load-threshold': 0, 'capacities': '1,1'})
            cases.append(rows)
            if routing == 'spread':
                self.assertTrue(all(int(r['pool']) == int(r['task']) % 2 for r in rows))
                self.assertGreater(summary['spilled_tasks'], 0)
            if routing == 'threshold':
                for r in rows:
                    if r['pool'] != r['home_pool']:
                        self.assertGreater(float(r['home_score']), 0)
                        self.assertLess(float(r['selected_score']), float(r['home_score']))
                        self.assertEqual(int(r['route_candidates']), 2)
                    else:
                        self.assertEqual(float(r['home_score']), float(r['selected_score']))
        keys = ('task', 'burst', 'planned_ns', 'target_cpu_us', 'estimate_us', 'home_pool')
        workloads = [[[r[k] for k in keys] for r in rows] for rows in cases]
        self.assertEqual(workloads[0], workloads[1])
        self.assertEqual(workloads[0], workloads[2])

    def test_metrics_and_auto_enable(self):
        for metric in ('queue', 'outstanding', 'work', 'remaining'):
            meta, _, rows, _ = self.run_case(**{'routing': 'threshold', 'load-metric': metric,
                                              'load-threshold': 0, 'capacities': '1,1', 'instrumentation': 'none'})
            self.assertEqual(meta['requested_load_observation'], 'off')
            self.assertEqual(meta['effective_load_observation'], 'progress' if metric == 'remaining' else 'basic')
            source = {'queue': 'native_queue', 'outstanding': 'outstanding',
                      'work': 'work_estimate_us', 'remaining': 'remaining_work_us'}[metric]
            for r in rows:
                divisor = 1000 if metric in ('work', 'remaining') else 1
                self.assertAlmostEqual(float(r['selected_score']), float(r['selected_'+source]) / divisor)
                self.assertEqual(r['home_inflight'], '-1')
                self.assertEqual(r['selected_inflight'], '-1')
                self.assertGreaterEqual(int(r['selected_outstanding']), 0)
                if metric != 'remaining':
                    self.assertEqual(int(r['selected_remaining_work_us']), -1)

    def test_minimal_reads_only_selected_metric(self):
        fields = {'queue': 'native_queue', 'outstanding': 'outstanding',
                  'work': 'work_estimate_us', 'remaining': 'remaining_work_us'}
        for metric, source in fields.items():
            meta, _, rows, samples = self.run_case(**{'routing': 'threshold', 'load-metric': metric,
                'load-threshold': 0, 'load-observation': 'minimal', 'instrumentation': 'none'})
            self.assertEqual(meta['effective_load_observation'], 'minimal')
            self.assertEqual(meta['maintain_outstanding'], metric == 'outstanding')
            self.assertEqual(meta['maintain_work'], metric == 'work')
            self.assertEqual(meta['publish_remaining'], metric == 'remaining')
            for r in rows:
                for candidate in fields.values():
                    self.assertEqual(float(r['selected_'+candidate]) >= 0, candidate == source)
                    self.assertEqual(float(r['home_'+candidate]) >= 0, candidate == source)
                self.assertEqual(r['selected_inflight'], '-1')
                self.assertEqual(r['home_inflight'], '-1')
                divisor = float(r['capacity']) * (1000 if metric in ('work', 'remaining') else 1)
                self.assertAlmostEqual(float(r['selected_score']), float(r['selected_'+source]) / divisor)
            for sample in samples:
                self.assertEqual(int(sample['outstanding']) >= 0, metric == 'outstanding')
                self.assertEqual(int(sample['remaining_work_us']) >= 0, metric == 'remaining')
                self.assertEqual(int(sample['native_queue']) >= 0, metric == 'queue')
                self.assertEqual(int(sample['work_estimate_us']) >= 0, metric == 'work')

    def test_minimal_preserves_legacy_full_and_cleanup(self):
        for metric in ('queue', 'outstanding', 'work', 'remaining'):
            meta, summary, rows, _ = self.run_case(expected=2, **{'load-observation': 'minimal',
                'load-metric': metric, 'fail-task': 0, 'fail-submit-task': 1})
            self.assertTrue(meta['maintain_work'])  # Legacy full instrumentation.
            self.assertEqual(summary['failed_tasks'], 1)
            self.assertEqual(summary['rejected_tasks'], 1)
            self.assertTrue(all(int(r['work_estimate_us']) >= 0 and int(r['native_queue']) >= 0 for r in rows))
            _, summary, _, _ = self.run_case(**{'load-observation': 'minimal', 'load-metric': metric,
                'instrumentation': 'none', 'arrivals': 1, 'threads': 1, 'duration': .002,
                'drain-seconds': .002, 'cpu-us': 1000000})
            self.assertGreater(summary['canceled_tasks'], 0)

    def test_progress_publishes_before_completion(self):
        for estimate in (1, 1000000):
            _, _, _, samples = self.run_case(**{'arrivals': 1, 'burst-min': 1, 'burst-max': 1,
                'pools': 1, 'threads': 1, 'cpu-us': 100000, 'jitter': 0, 'estimate-us': estimate,
                'load-observation': 'progress', 'progress-us': 1000})
            running = [s for s in samples if int(s['outstanding']) == 1]
            self.assertTrue(running, 'sampler never observed the 100ms CPU callback')
            if estimate == 1:
                self.assertTrue(any(int(s['remaining_work_us']) == 0 for s in running),
                                'underestimate should reach zero before completion')
            else:
                self.assertTrue(any(0 < int(s['remaining_work_us']) < estimate for s in running),
                                'remaining estimate did not publish consumed CPU')

    def test_home_remaining_enables_progress_and_iteration_work(self):
        meta, _, rows, _ = self.run_case(**{'load-metric': 'remaining', 'work-mode': 'iterations',
                                           'iterations-per-us': 1, 'estimate-us': 1})
        self.assertEqual(meta['effective_load_observation'], 'progress')
        self.assertTrue(all(int(r['selected_remaining_work_us']) >= 0 for r in rows))

    def test_lifecycle_cleanup_reject_fail_and_cancel(self):
        opts = {'load-observation': 'progress', 'routing': 'threshold', 'load-threshold': 0}
        _, summary, _, _ = self.run_case(**opts, **{'max-outstanding': 1, 'cpu-us': 20000})
        self.assertGreater(summary['rejected_tasks'], 0)
        _, summary, rows, _ = self.run_case(**opts, **{'fail-submit-task': 0})
        self.assertEqual(rows[0]['status'], 'rejected')
        self.assertEqual(summary['rejected_tasks'], 1)
        _, summary, rows, _ = self.run_case(expected=2, **opts, **{'fail-task': 0})
        self.assertEqual(summary['failed_tasks'], 1)
        self.assertEqual(rows[0]['status'], 'failed')
        _, summary, _, _ = self.run_case(**opts, **{'arrivals': 1, 'threads': 1, 'duration': .002,
                                                   'drain-seconds': .002, 'cpu-us': 1000000})
        self.assertGreater(summary['canceled_tasks'], 0)

    def test_estimate_under_over_and_noise_cleanup(self):
        targets = []
        for estimate, jitter in ((1, 0), (100000, 0), (1000, .75)):
            _, _, rows, _ = self.run_case(**{'load-observation': 'progress', 'estimate-us': estimate,
                                           'estimate-jitter': jitter, 'routing': 'threshold',
                                           'load-metric': 'remaining', 'load-threshold': 0})
            targets.append([r['target_cpu_us'] for r in rows])
            self.assertTrue(all(int(r['selected_remaining_work_us']) >= 0 for r in rows))
        self.assertTrue(all(targets[0] == work for work in targets))

    def test_no_improvement_and_singleton_fallback(self):
        _, summary, rows, _ = self.run_case(**{'routing': 'threshold', 'load-threshold': 0,
                                              'min-improvement': 1000000})
        self.assertEqual(summary['spilled_tasks'], 0)
        self.assertTrue(all(r['home_pool'] == r['pool'] for r in rows))
        _, summary, rows, _ = self.run_case(**{'routing': 'threshold', 'pools': 1, 'load-threshold': 0})
        self.assertEqual(summary['spilled_tasks'], 0)
        self.assertTrue(all(r['route_candidates'] == '1' for r in rows))

    def test_capacities_follow_effective_masks(self):
        cpus = sorted(os.sched_getaffinity(0))[:2]
        groups = ';'.join(map(str, cpus))
        meta, _, _, _ = self.run_case(pools=len(cpus), threads=4, cpus=groups, affinity='cluster')
        self.assertEqual(meta['capacities'], [1] * len(cpus))
        meta, _, _, _ = self.run_case(pools=len(cpus), threads=4, cpus=groups, affinity='none')
        self.assertEqual(meta['capacities'], [min(4, len(os.sched_getaffinity(0)))] * len(cpus))

    def test_invalid_options(self):
        for option, value in [('routing', 'bad'), ('load-metric', 'bad'), ('load-observation', 'bad'),
                              ('load-threshold', '-1'), ('load-threshold', 'nan'),
                              ('min-improvement', '-1'), ('progress-us', 0), ('capacities', '1'),
                              ('capacities', '1,0'), ('capacities', '1,-1'), ('capacities', '1,nan'),
                              ('capacities', '1,1,'), ('capacities', '1,,1'), ('capacities', '1,1e-300')]:
            result = subprocess.run([str(BINARY), '--'+option, str(value), '--output', str(self.root/'invalid')],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, (option, value))


if __name__ == '__main__':
    unittest.main()

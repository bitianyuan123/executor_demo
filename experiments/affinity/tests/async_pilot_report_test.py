#!/usr/bin/env python3
"""Bounded eight-cell contracts and terminal output; synthetic records only."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


pilot = module(HERE.parent / 'async_pilot_report.py', 'pilot_under_test')
async_fixture = module(HERE / 'async_analysis_test.py', 'pilot_async_fixture')
cost_fixture = module(HERE / 'trace_cost_report_test.py', 'pilot_cost_fixture')


class AsyncPilotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='affinity-async-pilot-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths, self.data = [], []
        for index, (phase, policy, trace) in enumerate(pilot.CELLS):
            path = self.root / f'arbitrary-name-{index}'
            summary, requests, tasks, events = async_fixture.write_fixture(
                path, percent=0 if phase == 'd0' else 50,
                delay_us=2000 if phase == 'a1' else 0, trace=bool(trace), policy=policy)
            template_path = self.root / f'template-{index}'
            template, _ = cost_fixture.write_fixture(template_path, trace=bool(trace), seed=421)
            summary = {**template, **summary}
            if phase != 'd0':
                summary['async_contract']['capacity'] = 4096
            summary['options'] = {
                'output': str(path), 'seed': '421', 'policy': policy,
                'trace': str(trace), 'balance-at': 'all', 'continuation': 'parent',
                'home-policy': 'rr', 'affinity': 'cluster', 'cpus': '0;1',
                'threads': '2', 'requests': '3', 'qps': '10', 'widths': '2,1',
                'depths': '2,2', 'iterations': '10', 'accesses': '2',
                'model': 'branch-rw', 'cross-numa': '0', 'cpu-sampling': '0',
                'page-sample': '0', 'sync-block-percent': '0', 'sync-block-us': '0',
                'async-percent': '0' if phase == 'd0' else '50',
                'async-delay-us': '2000' if phase == 'a1' else '0',
                'async-align-us': '0', 'async-capacity': '4096',
            }
            # Stable contrast values unrelated to the fixture callback CPU.
            summary['process_cpu_s'] = .2 + index / 100
            async_fixture.signatures(summary)
            self.paths.append(path)
            self.data.append([summary, requests, tasks, events])
            self.save(index)

    def save(self, index):
        async_fixture.save_fixture(self.paths[index], *self.data[index])

    def runs(self, paths=None):
        return [pilot.report.load_run(path) for path in (self.paths if paths is None else paths)]

    def invoke(self, paths=None, *args):
        return subprocess.run([sys.executable, str(HERE.parent / 'async_pilot_report.py'),
                               *map(str, self.paths if paths is None else paths), *map(str, args)],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_complete_cells_classified_by_resolved_contract_and_bounded_output(self):
        result = pilot.analyze(self.runs(list(reversed(self.paths))))
        self.assertEqual(set(result['cells']), set(pilot.CELLS))
        self.assertEqual(len(result['strategy']), 3)
        self.assertEqual(len(result['mechanism']), 4)
        self.assertEqual(len(result['traces']), 2)
        self.assertEqual(result['cells']['a0', 'home', 0]['summary']['planned_resumes'], 13)
        self.assertIsNone(result['provenance']['order'])
        self.assertAlmostEqual(result['strategy']['d0']['cpu_per_request_ms']['absolute'], 10 / 3)
        self.assertEqual(result['stages']['a0']['roles']['rpc_resume']['n'], 13)
        self.assertEqual(result['stages']['a0']['operators']['n'], 18)
        self.assertEqual(result['stages']['a0']['operators']['cpu_ms'], 1)
        self.assertEqual(result['rows']['d0', 'home', 0]['duration_s'], .05)
        output = self.invoke()
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn('AUDIT PASSED', output.stdout)
        self.assertIn('Plan order/source hashes unavailable', output.stdout)
        self.assertIn('trace=0)', output.stdout)
        self.assertIn('A0/A1 each add one rpc_resume', output.stdout)
        self.assertIn('home trace1-trace0 (instrumentation sensitivity)', output.stdout)
        self.assertIn('whole wall+drain s', output.stdout)
        self.assertIn('a1 business operators: n=18', output.stdout)
        self.assertIn('not identical execution contracts or pure overhead', output.stdout)
        self.assertLess(len(output.stdout.splitlines()), 65)

    def test_unknown_helper_cpu_is_not_reported_as_zero(self):
        self.data[4][0]['async_source']['helper_cpu_valid'] = False
        self.save(4)
        result = pilot.analyze(self.runs())
        self.assertIsNone(result['rows']['d0', 'home', 0]['helper_cpu_ms'])
        self.assertIsNone(result['rows']['a1', 'home', 0]['helper_cpu_ms'])
        self.assertIsNone(result['cells']['a1', 'home', 0]['async_events'])
        self.assertEqual(result['cells']['a1', 'home', 0]['tasks'], [])

    def test_missing_duplicate_extra_and_multiple_seed_refused_before_output(self):
        for paths in (self.paths[:-1], self.paths + self.paths[:1], self.paths[:-1] + self.paths[:1]):
            with self.subTest(paths=len(paths)):
                output = self.invoke(paths)
                self.assertEqual(output.returncode, 2)
                self.assertNotIn('AUDIT PASSED', output.stdout)
        # Changing a direct seed remains a valid raw v2 file, but not one batch.
        self.data[0][0]['seed'] = 422
        self.save(0)
        output = self.invoke()
        self.assertEqual(output.returncode, 2)
        self.assertIn('one seed', output.stderr)
        self.assertNotIn('AUDIT PASSED', output.stdout)

    def test_common_resource_or_routing_drift_rejected(self):
        mutations = (
            lambda s: s['groups'][0].update(cpus=[3]),
            lambda s: s.update(threads_per_pool=3),
            lambda s: s['routing'].update(min_gain=.25),
            lambda s: s['options'].update({'min-gain': '.25'}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                summary = self.data[3][0]
                before = json.loads(json.dumps(summary))
                mutation(summary)
                self.save(3)
                output = self.invoke()
                self.assertEqual(output.returncode, 2, output.stdout)
                self.assertNotIn('AUDIT PASSED', output.stdout)
                self.data[3][0] = before
                self.save(3)

    def test_disallowed_pilot_modes_and_async_cli_mismatch_rejected(self):
        mutations = (
            lambda s: s.update(affinity='none'),
            lambda s: (s.update(continuation='home'), s['options'].update(continuation='home')),
            lambda s: (s.update(balance_at='fork'), s['options'].update({'balance-at': 'fork'})),
            lambda s: s['routing'].update(cross_numa=True),
            lambda s: s.update(cpu_sampling=True),
            lambda s: s.update(page_query_enabled=True),
            lambda s: s.update(sync_block_percent=50, sync_block_us=1000),
            lambda s: s['groups'][1].update(cpus=[0]),
            lambda s: s.update(pool_count=4),
            lambda s: s['options'].update({'async-percent': '100'}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                before = json.loads(json.dumps(self.data[2][0]))
                mutation(self.data[2][0])
                self.save(2)
                output = self.invoke()
                self.assertEqual(output.returncode, 2, output.stdout)
                self.assertNotIn('AUDIT PASSED', output.stdout)
                self.data[2][0] = before
                self.save(2)

    def test_entire_batch_consistently_changing_frozen_routing_is_refused(self):
        for data in self.data:
            data[0]['routing']['busy_high'] = 2
            data[0]['options']['busy-high'] = '2'
        for index in range(8):
            self.save(index)
        output = self.invoke()
        self.assertEqual(output.returncode, 2)
        self.assertIn('outstanding default thresholds', output.stderr)
        self.assertNotIn('AUDIT PASSED', output.stdout)

    def test_legal_async_run_outside_pilot_contract_is_refused(self):
        # Capacity can change without changing selected callbacks or outcomes.
        summary = self.data[2][0]
        summary['async_contract']['capacity'] = 8192
        summary['options']['async-capacity'] = '8192'
        async_fixture.signatures(summary)
        self.save(2)
        pilot.report.load_run(self.paths[2])
        output = self.invoke()
        self.assertEqual(output.returncode, 2)
        self.assertIn('capacity=4096', output.stderr)
        self.assertNotIn('AUDIT PASSED', output.stdout)

    def test_cross_mechanism_request_mismatch_not_hidden_by_aggregate_checksum(self):
        summary, rows, _, _ = self.data[2]
        a, b = rows[0]['checksum'], rows[1]['checksum']
        rows[0]['checksum'] = 12345
        rows[1]['checksum'] = pilot.report.mix(a) ^ b ^ pilot.report.mix(12345)
        # Change the entire A0 family equally: same-family comparisons pass,
        # but D0/A0 business contract must still reject changed logical work.
        for index in (2, 3, 6):
            self.data[index][1][0]['checksum'] = rows[0]['checksum']
            self.data[index][1][1]['checksum'] = rows[1]['checksum']
            self.save(index)
        runs = self.runs()
        self.assertEqual(runs[0]['summary']['checksum'], runs[2]['summary']['checksum'])
        with self.assertRaisesRegex(ValueError, 'mechanism request schedule/status/checksum'):
            pilot.analyze(runs)

    def test_failure_outside_cohort_invalidates_batch(self):
        summary, rows, _, _ = self.data[0]
        rows[0]['status'] = 'failed'
        summary['completed'], summary['failed_or_rejected'] = 2, 1
        checksum = 0
        for row in rows[1:]:
            checksum = pilot.report.mix(checksum ^ row['checksum'])
        summary['checksum'] = str(checksum)
        self.save(0)
        output = self.invoke(None, '--warmup-seconds', '.1', '--window-seconds', '.1')
        self.assertEqual(output.returncode, 2)
        self.assertIn('whole-run failure/rejection', output.stderr)
        self.assertNotIn('AUDIT PASSED', output.stdout)

    def test_only_shared_matching_plan_supplies_order_and_hash_provenance(self):
        order = list(reversed(range(8)))
        plan = [{'directory': str(self.paths[index]), 'case': 'untrusted-label'} for index in order]
        metadata = {'schema': 'affinity-v2-matrix', 'suite': 'async-pilot',
                    'experiment_role': 'controlled_comparison', 'research_target': 'one_pool_per_cluster',
                    'binary_sha256': '1234', 'sources_sha256': {'include/executor_demo/AsyncCompletionSource.h': '5678'}}
        (self.root / 'plan.json').write_text(json.dumps(plan))
        (self.root / 'metadata.json').write_text(json.dumps(metadata))
        result = pilot.analyze(self.runs())
        self.assertEqual(result['provenance']['order'], [pilot.cell_name(pilot.CELLS[index]) for index in order])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pilot.print_result(result, 0, None)
        self.assertIn('Plan binary SHA256=1234', output.getvalue())
        self.assertIn('AsyncCompletionSource SHA256=5678', output.getvalue())
        plan[0]['directory'] = str(self.root / 'not-supplied')
        (self.root / 'plan.json').write_text(json.dumps(plan))
        self.assertIsNone(pilot.analyze(self.runs())['provenance']['order'])

    def test_trace_pair_uses_full_contract_and_rejects_changed_trace_selection(self):
        self.data[6][0]['options']['async-delay-us'] = '2000'
        self.save(6)
        output = self.invoke()
        self.assertEqual(output.returncode, 2)
        self.assertIn('disagrees with CLI', output.stderr)
        self.assertNotIn('AUDIT PASSED', output.stdout)

    def test_selected_plan_and_source_corruption_refused_before_partial_output(self):
        mutations = (
            lambda s: s['planned_resume_counts'].__setitem__(0, 0),
            lambda s: s['async_source'].update(pending=1),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                before = json.loads(json.dumps(self.data[7][0]))
                mutation(self.data[7][0])
                self.save(7)
                output = self.invoke()
                self.assertEqual(output.returncode, 2)
                self.assertEqual(output.stdout, '')
                self.data[7][0] = before
                self.save(7)

    def test_invalid_cli_window_and_empty_cohort_rejected(self):
        for args in (('--window-seconds', 'nan'), ('--window-seconds', '1e-20'),
                     ('--warmup-seconds', '-1'), ('--warmup-seconds', '2')):
            with self.subTest(args=args):
                output = self.invoke(None, *args)
                self.assertNotEqual(output.returncode, 0)
                self.assertNotIn('AUDIT PASSED', output.stdout)


if __name__ == '__main__':
    unittest.main()

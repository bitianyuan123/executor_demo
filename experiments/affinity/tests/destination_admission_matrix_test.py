#!/usr/bin/env python3
"""Bounded plan, provenance and paired evidence checks; no timing assertions."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[3] / 'scripts' / 'run_destination_admission_matrix.py'
SPEC = importlib.util.spec_from_file_location('admission_matrix', TOOL)
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


class MatrixTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='destination-matrix-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0

    def invoke(self, extra=(), success=True, output=None):
        self.serial += 1
        path = output or self.root / str(self.serial)
        result = subprocess.run([sys.executable, str(TOOL), '--output', str(path), *extra],
                                text=True, capture_output=True, timeout=10)
        if not success:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return result, path
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, json.loads((path / 'metadata.json').read_text()), json.loads((path / 'plan.json').read_text())

    def test_twelve_complete_cells_fixed_work_and_deterministic_interleaving(self):
        _, metadata, plan = self.invoke()
        self.assertEqual(metadata['rounds'], 12)
        self.assertFalse(metadata['development'])
        self.assertEqual(metadata['research_target'], 'one_pool_per_cluster')
        expected = []
        for seed in (421, 422):
            cells = [(shape, label) for shape in ('front80', 'tail4') for label in ('off', 'cap100', 'cap075')]
            random.Random(seed).shuffle(cells)
            expected.extend((seed, shape, label) for shape, label in cells)
        self.assertEqual([(row['seed'], row['shape'], row['label']) for row in plan], expected)
        for row in plan:
            options = row['options']
            self.assertEqual(dict(zip((key[2:] for key in row['command'][1::2]), row['command'][2::2])), options)
            self.assertEqual(options['iterations'], '3000000')
            self.assertEqual(options['accesses'], '100000')
            self.assertEqual(options['requests'], '240')
            self.assertEqual(options['threads'], '16')
            self.assertEqual(options['qps'], '12')
            self.assertEqual(options['policy'], 'outstanding')
            self.assertEqual(options['cross-numa'], '0')
            self.assertEqual(options['batch-chunk'], '1')
            self.assertEqual(options['jitter-percent'], '0')
            self.assertEqual(options['async-percent'], '0')
            self.assertEqual(options['sync-block-percent'], '0')
            self.assertEqual(options['trace'], '1')
            widths, depths = map(lambda value: list(map(int, value.split(','))), (options['widths'], options['depths']))
            self.assertEqual(sum(w * d for w, d in zip(widths, depths)), 168)
            self.assertEqual(sum(widths), 84)
            self.assertEqual(1 + sum(w * d + 1 for w, d in zip(widths, depths)), 171)
        self.assertEqual(metadata['work_contract']['iterations_per_request'], 504000000)
        self.assertEqual(metadata['work_contract']['read_and_write_words_each_per_request'], 16800000)
        self.assertEqual(metadata['work_contract']['branch_payload_bytes'], 5505024)
        self.assertIn('experiments/affinity/include/executor_demo/AdmissionPolicy.h', metadata['sources_sha256'])
        for seed in (421, 422):
            for shape in ('front80', 'tail4'):
                group = [row for row in plan if (row['seed'], row['shape']) == (seed, shape)]
                stripped = [{key: value for key, value in row['options'].items() if key not in ('target-limit', 'output')} for row in group]
                self.assertEqual(stripped[0], stripped[1])
                self.assertEqual(stripped[0], stripped[2])

    def test_no_existing_directory_can_be_reused_even_empty(self):
        path = self.root / 'existing'
        path.mkdir()
        result, _ = self.invoke(output=path, success=False)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(list(path.iterdir()), [])
        (path / 'important.txt').write_text('retain')
        self.invoke(output=path, success=False)
        self.assertEqual((path / 'important.txt').read_text(), 'retain')

    def test_target_configuration_cannot_silently_become_smoke(self):
        for extra in (('--threads', '1'), ('--requests', '4'), ('--iterations', '1000'),
                      ('--accesses', '32'), ('--qps', '50'), ('--cpus', '0;1')):
            with self.subTest(extra=extra):
                _, path = self.invoke(extra, success=False)
                self.assertFalse(path.exists())

    def test_development_requires_declared_cpu_groups_and_marks_contract(self):
        _, path = self.invoke(('--development',), success=False)
        self.assertFalse(path.exists())
        result, metadata, entries = self.invoke(('--development', '--cpus', '0;1'))
        self.assertIn('functional checks only', result.stdout)
        self.assertTrue(metadata['development'])
        self.assertEqual(len(entries), 12)
        for entry in entries:
            self.assertEqual(entry['options']['threads'], '1')
            self.assertEqual(entry['options']['requests'], '4')
            self.assertEqual(entry['options']['iterations'], '1000')
            self.assertEqual(entry['options']['accesses'], '32')
        _, metadata, _ = self.invoke(('--development', '--cpus', '2;3', '--threads', '2', '--requests', '3', '--qps', '20'))
        self.assertEqual(metadata['fixed_options']['requests'], '3')

    def test_invalid_cpu_sets_and_values_leave_no_plan(self):
        for cpus in ('', '0;', '0;;1', '0;0', '0-2;2-3', '-1;2', '0,a;2', '3-1;4', '0,0;1', '0'):
            with self.subTest(cpus=cpus):
                _, path = self.invoke(('--development', '--cpus', cpus), success=False)
                self.assertFalse(path.exists())
        for extra in (('--requests', '0'), ('--iterations', '-1'), ('--qps', 'nan'),
                      ('--qps', 'inf'), ('--timeout-seconds', '0')):
            _, path = self.invoke(('--development', '--cpus', '0;1', *extra), success=False)
            self.assertFalse(path.exists())

    def test_failed_execution_retains_log_and_receipt_and_stops(self):
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 2:
            self.skipTest('requires two allowed logical CPUs, no workload is launched')
        program = self.root / 'reject-demo'
        program.write_text('#!/bin/sh\necho expected-rejection\nexit 9\n')
        program.chmod(0o755)
        result, path = self.invoke(('--development', '--cpus', f'{cpus[0]};{cpus[1]}',
                                    '--binary', str(program), '--run'), success=False)
        self.assertIn('run failed', result.stderr)
        dirs = [directory for directory in path.iterdir() if directory.is_dir()]
        self.assertEqual(len(dirs), 1)
        receipt = json.loads((dirs[0] / 'execution.json').read_text())
        self.assertEqual(receipt['exit_code'], 9)
        self.assertFalse(receipt['timed_out'])
        self.assertIn('expected-rejection', (dirs[0] / 'run.log').read_text())

    def test_timeout_retains_partial_run_and_does_not_start_another_cell(self):
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 2:
            self.skipTest('requires two allowed logical CPUs, no workload is launched')
        program = self.root / 'timeout-demo'
        program.write_text('#!/bin/sh\nexec sleep 2\n')
        program.chmod(0o755)
        _, path = self.invoke(('--development', '--cpus', f'{cpus[0]};{cpus[1]}',
                               '--binary', str(program), '--timeout-seconds', '0.02', '--run'), success=False)
        dirs = [directory for directory in path.iterdir() if directory.is_dir()]
        self.assertEqual(len(dirs), 1)
        receipt = json.loads((dirs[0] / 'execution.json').read_text())
        self.assertIsNone(receipt['exit_code'])
        self.assertTrue(receipt['timed_out'])
        self.assertTrue((dirs[0] / 'run.log').is_file())

    def test_plan_identity_and_fixed_contract_changes_are_rejected(self):
        _, metadata, plan = self.invoke(('--binary', sys.executable))
        root = Path(plan[0]['directory']).parent
        matrix.validate_plan(root, metadata, plan)
        altered = copy.deepcopy(plan)
        altered[0]['command'][0] = '/some/other/binary'
        with self.assertRaisesRegex(ValueError, 'plan/configuration'):
            matrix.validate_plan(root, metadata, altered)
        altered_meta = copy.deepcopy(metadata)
        altered_meta['binary_sha256'] = None
        with self.assertRaisesRegex(ValueError, 'identity is missing'):
            matrix.validate_plan(root, altered_meta, plan)
        altered_meta = copy.deepcopy(metadata)
        altered_meta['fixed_options']['qps'] = '16'
        altered_plan = matrix.make_plan(Path(metadata['binary']), root, altered_meta['fixed_options'])
        altered_meta['plan_sha256'] = matrix.json_digest(altered_plan)
        with self.assertRaisesRegex(ValueError, 'fixed experiment contract'):
            matrix.validate_plan(root, altered_meta, altered_plan)

    @staticmethod
    def diagnostic(limit):
        stats = dict.fromkeys(('selections', 'evaluated_selections', 'candidate_checks',
                               'candidate_score_invalid', 'candidate_invalid', 'candidate_over_limit',
                               'candidate_insufficient_gain', 'return_home_rejected', 'no_eligible_target'), 0)
        reasons = dict.fromkeys(matrix.DECISION_REASONS, 0)
        if limit:
            stats.update(selections=3, evaluated_selections=2, candidate_checks=4,
                         candidate_over_limit=2, no_eligible_target=1)
            reasons.update(Home=1, LocalSpill=1, NoEligibleTarget=1)
        return {'enabled': bool(limit), 'limit': limit, 'stats': stats, 'decision_reasons': reasons,
                'policy': 'outstanding-after' if limit else 'disabled',
                'units': 'outstanding_reserved_incoming_tasks_per_capacity' if limit else 'none'}

    def test_admission_counters_preserve_selection_and_candidate_denominators(self):
        for limit in (0, 1, .75):
            matrix.validate_admission(self.diagnostic(limit), limit)
        value = self.diagnostic(1)
        self.assertGreater(value['stats']['candidate_checks'], value['stats']['selections'])
        for key, altered in (('selections', 4), ('no_eligible_target', 0), ('evaluated_selections', 4),
                             ('candidate_checks', -1), ('candidate_checks', False),
                             ('candidate_insufficient_gain', 3), ('return_home_rejected', 5)):
            modified = copy.deepcopy(value)
            modified['stats'][key] = altered
            with self.assertRaises(ValueError):
                matrix.validate_admission(modified, 1)
        value = self.diagnostic(0)
        value['stats']['candidate_checks'] = 1
        with self.assertRaises(ValueError):
            matrix.validate_admission(value, 0)
        for field, replacement in (('policy', 'queue'), ('units', 'CPU'), ('decision_reasons', {'Unknown': 3})):
            value = self.diagnostic(1)
            value[field] = replacement
            with self.assertRaises(ValueError):
                matrix.validate_admission(value, 1)

    @staticmethod
    def rows():
        return [{'seed': seed, 'shape': shape, 'target_limit': limit, 'case': f'{shape}-{limit}',
                 'development': True, 'pairing_sha256': f'{seed}-{shape}',
                 'cohort': {'mean_ms': 10 + limit, 'p95_ms': 12 + limit, 'p99_ms': 13 + limit, 'migrations': 10},
                 'whole_run': {'cpu_per_completed_request_ms': 20 + limit}}
                for seed in (421, 422) for shape in ('front80', 'tail4') for limit in (0, 1, .75)]

    def test_pairs_are_within_shape_and_seed_no_pooling_and_fail_closed(self):
        rows = self.rows()
        pairs = matrix.paired_deltas(rows)
        self.assertEqual(len(pairs), 8)
        for pair in pairs:
            self.assertEqual(pair['cohort_delta_ms']['p99_ms'], pair['target_limit'])
            self.assertEqual(pair['whole_run_cpu_per_request_delta_ms'], pair['target_limit'])
        for bad in (rows[:-1], rows + rows[:1], rows[:-1] + rows[:1]):
            with self.assertRaises(ValueError):
                matrix.paired_deltas(bad)
        rows[1]['pairing_sha256'] = 'changed-work-or-build-or-resource'
        with self.assertRaisesRegex(ValueError, 'resources/build/work/checksum/arrival'):
            matrix.paired_deltas(rows)


if __name__ == '__main__':
    unittest.main()

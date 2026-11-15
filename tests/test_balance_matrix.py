#!/usr/bin/env python3
"""Integrity checks for phase separation, equal-work comparisons and safe resume."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'experiments/independent/scripts/balance_matrix.py'
SPEC = importlib.util.spec_from_file_location('balance_matrix', SCRIPT)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class MatrixTests(unittest.TestCase):
    def args(self, *extra):
        return M.parser().parse_args(['plan', '--phase', 'observe', '--preset', 'dev', '--output', 'unused.json', *extra])

    def test_target_resource_domains(self):
        plan = M.generate(self.args('--preset', 'target', '--families', 'main,affinity'))
        self.assertEqual(5000, plan['base']['progress-us'])
        self.assertEqual(96, len(plan['launcher_cpu_union']))
        self.assertEqual(18, plan['planned_runs'])
        for case in plan['cases']:
            mode = case['options'].get('affinity', 'cluster')
            masks = case['layout']['pool_masks']
            self.assertTrue(all(len(mask) == {'cluster': 16, 'numa': 48, 'none': 96}[mode] for mask in masks))
            self.assertEqual(case['layout']['shared_cpu_domain'], mode != 'cluster')
        with self.assertRaises(ValueError):
            M.topology('0-2;2-3', '0,1', 2, 'cluster', 16)
        with self.assertRaises(ValueError):
            M.topology('0-1;2-3', '0', 2, 'numa', 16)
        with self.assertRaises(ValueError):
            M.topology('0-1;2-3', '0,1', 2, 'cluster', 16, '1,0')

    def test_splits_cannot_overlap(self):
        plan = M.generate(self.args())
        self.assertEqual(500, plan['base']['progress-us'])
        all_seeds = sum(plan['split_seeds'].values(), [])
        self.assertEqual(len(all_seeds), len(set(all_seeds)))
        with self.assertRaises(ValueError):
            M.generate(self.args('--seeds', '21000'))
        with self.assertRaises(ValueError):
            M.generate(self.args('--phase', 'evaluate'))

    def test_route_pairs_change_only_route(self):
        plan = M.generate(self.args('--phase', 'tune', '--families', 'main', '--metrics', 'queue,outstanding,remaining'))
        groups = {}
        for case in plan['cases']:
            opts = dict(plan['base'], **case['options'])
            group = groups.setdefault(case['comparison_group'], [])
            group.append({k: v for k, v in opts.items() if k not in M.ROUTE_KEYS})
            self.assertEqual('progress', opts['load-observation'])
            self.assertEqual(opts['cpu-us'], opts['estimate-us'])
        for rows in groups.values():
            self.assertTrue(all(row == rows[0] for row in rows))
        self.assertEqual(M.make_schedule(plan), M.make_schedule(plan))
        self.assertEqual(len(plan['cases']), len(M.make_schedule(plan)))

    def test_constant_budget_controls_and_tail(self):
        plan = M.generate(self.args('--preset', 'target', '--families', 'main,negative,distribution,burst,arrival,estimator'))
        for case in plan['cases']:
            opt = dict(plan['base'], **case['options'])
            self.assertAlmostEqual(240, opt['arrival-rate'] * opt['burst-min'])
            self.assertEqual('exact', opt['mean-normalization'])
            self.assertAlmostEqual(1440, case['sample_plan']['nominal_offered_cpu_seconds'])
        oracle = [c for c in plan['cases'] if 'oracle' in c['name']]
        self.assertTrue(oracle and all(c['options']['estimate-us'] == 0 for c in oracle))

    def test_cost_mode_fixed_work(self):
        plan = M.generate(self.args('--phase', 'cost'))
        for case in plan['cases']:
            opt = dict(plan['base'], **case['options'])
            self.assertEqual('iterations', opt['work-mode'])
            self.assertEqual('none', opt['instrumentation'])
            self.assertFalse(opt['routing'] == 'threshold' and opt['load-observation'] == 'off')
            self.assertIn('-obs-', case['comparison_group'])
        groups = {c['comparison_group'] for c in plan['cases']}
        for group in groups:
            self.assertEqual(1, sum(c['options']['routing'] == 'home' for c in plan['cases'] if c['comparison_group'] == group))

    def test_poisson_smooth_keeps_paired_workload(self):
        plan = M.generate(self.args('--phase', 'tune', '--families', 'negative',
                                    '--arrival', 'poisson', '--metrics', 'outstanding'))
        smooth = [c for c in plan['cases'] if '-smooth-' in c['name']]
        self.assertTrue(smooth)
        for case in smooth:
            options = dict(plan['base'], **case['options'])
            self.assertEqual('poisson', options['arrival'])
            self.assertEqual(1, options['burst-min'])
        for threads in {c['options']['threads'] for c in smooth}:
            self.assertEqual(1, len({c['pair_workload_sha256'] for c in smooth
                                    if c['options']['threads'] == threads}))

    def test_validation_rejects_leaking_edit(self):
        plan = M.generate(self.args())
        plan['split_seeds']['tune'] = plan['seeds']
        with self.assertRaises(ValueError):
            M.validate_spec(plan)

    def tiny(self, root, phase='observe', fail_once=False):
        plan = M.generate(self.args('--phase', phase, '--threads', '1', '--families', 'main', '--metrics', 'outstanding', '--thresholds', '1'))
        config = root / 'config.json'
        M.write_new(config, plan)
        binary = root / 'fake_load'
        marker = root / 'failed-once'
        binary.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n'
                          + (f'p=Path({str(marker)!r})\nif not p.exists():\n p.touch()\n sys.exit(4)\n' if fail_once else '')
                          + "o=dict(zip(sys.argv[1::2],sys.argv[2::2])); p=Path(o['--output']); p.mkdir(parents=True); (p/'summary.json').write_text(json.dumps({'conservation_ok':True,'counters_balanced':True,'final_outstanding':0,'sampling_truncated':False,'sampling_failed':False,'sample_rows_dropped':0,'planned_tasks':1,'completed_tasks':1,'rejected_tasks':0,'canceled_tasks':0,'failed_tasks':0}))\n")
        binary.chmod(0o755)
        output = root / 'runs'
        args = M.parser().parse_args(['run', '--config', str(config), '--binary', str(binary), '--output', str(output)])
        return plan, config, binary, output, args

    def quiet(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            M.execute(args)

    def test_resume_preserves_completed_and_refuses_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, config, binary, output, args = self.tiny(root)
            self.quiet(args)
            manifest = json.loads((output / 'manifest.json').read_text())
            path = Path(manifest['runs'][0]['path']) / 'summary.json'
            mtime = path.stat().st_mtime_ns
            args.resume = True
            self.quiet(args)
            self.assertEqual(mtime, path.stat().st_mtime_ns)
            self.assertEqual(1, len(json.loads((output / 'manifest.json').read_text())['runs'][0]['attempts']))
            changed = dict(plan, schedule_seed=plan['schedule_seed'] + 1)
            config.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.quiet(args)
            config.write_text(json.dumps(plan))
            binary.write_text(binary.read_text() + '\n# changed binary\n')
            with self.assertRaises(ValueError):
                self.quiet(args)

    def test_resume_keeps_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, config, binary, output, args = self.tiny(Path(tmp), fail_once=True)
            with self.assertRaises(RuntimeError):
                self.quiet(args)
            first = json.loads((output / 'manifest.json').read_text())['runs'][0]
            self.assertEqual(4, first['returncode'])
            args.resume = True
            self.quiet(args)
            row = json.loads((output / 'manifest.json').read_text())['runs'][0]
            self.assertEqual(2, len(row['attempts']))
            self.assertEqual('failed', row['attempts'][0]['status'])
            self.assertEqual('completed', row['attempts'][1]['status'])
            self.assertTrue(Path(row['attempts'][0]['log']).exists())

    def test_freeze_requires_complete_pairs_and_records_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, config, binary, output, args = self.tiny(root, phase='tune')
            self.quiet(args)
            manifest = output / 'manifest.json'
            selection_path = root / 'selection.json'
            freeze = M.parser().parse_args(['freeze', '--manifest', str(manifest), '--metric', 'outstanding', '--threshold', '1', '--output', str(selection_path)])
            with contextlib.redirect_stdout(io.StringIO()):
                M.freeze(freeze)
            selection = json.loads(selection_path.read_text())
            self.assertEqual('tune', selection['source_split'])
            self.assertTrue(selection['applicable_scopes'])
            evaluate = M.generate(self.args('--phase', 'evaluate', '--selection', str(selection_path), '--threads', '1'))
            self.assertTrue(all(c['evaluation_scope'] == 'seen-in-tuning' for c in evaluate['cases']))
            minimal = M.generate(self.args('--phase', 'evaluate', '--selection', str(selection_path), '--threads', '1', '--observation', 'minimal'))
            self.assertEqual('none', minimal['base']['instrumentation'])
            self.assertEqual('outstanding', minimal['base']['load-metric'])
            with self.assertRaises(ValueError):
                M.generate(self.args('--phase', 'evaluate', '--selection', str(selection_path), '--seeds', '21000'))
            data = json.loads(manifest.read_text())
            baseline = next(r for r in data['runs'] if r['options']['routing'] == 'home')
            baseline['status'] = 'failed'
            manifest.write_text(json.dumps(data))
            freeze.output = root / 'bad-selection.json'
            with self.assertRaises(ValueError):
                M.freeze(freeze)


if __name__ == '__main__':
    unittest.main()

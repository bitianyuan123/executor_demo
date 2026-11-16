#!/usr/bin/env python3
"""Cohort, pairing and evidence validity checks for the small wave report."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('request_wave_analysis',
    ROOT/'experiments/independent/scripts/analyze_request_wave.py')
a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(a)


def dump(path, value):
    path.write_text(json.dumps(value))


def csv_write(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def fixture(root):
    base = {'workload': 'request-wave', 'pools': 2, 'threads': 8, 'duration': 1.,
            'burst-min': 1, 'burst-max': 1, 'steady-tasks': 1,
            'load-observation': 'basic', 'load-metric': 'outstanding',
            'instrumentation': 'full', 'seed': 42}
    policies = {'home': {'routing': 'home', 'load-threshold': .8, 'accept-threshold': -1},
                'source_only': {'routing': 'threshold', 'load-threshold': .8, 'accept-threshold': -1},
                'guarded': {'routing': 'threshold', 'load-threshold': .8, 'accept-threshold': .5}}
    plan = {'schema_version': 1, 'family': 'request-wave', 'base': base,
            'analysis': {'warmup_seconds': .5, 'request_end_seconds': 1.},
            'capacities': [2, 2], 'pool_cpus': [[0, 1], [2, 3]], 'numa': [0, 0],
            'cpu_union': [0, 1, 2, 3], 'policies': policies}
    plan['plan_sha256'] = a.digest(plan)
    dump(root/'plan.json', plan)
    entries = []
    for index, policy in enumerate(a.POLICIES):
        directory = root/'runs'/policy
        data = directory/'data'; data.mkdir(parents=True)
        epoch = 10_000_000_000+index*10_000_000_000
        meta = {'schema': 1, 'workload': 'request-wave', 'pools': 2, 'threads_per_pool': 8,
                'duration_seconds': 1., 'burst_min': 1, 'burst_max': 1, 'steady_tasks': 1,
                'requested_load_observation': 'basic', 'effective_load_observation': 'basic',
                'load_metric': 'outstanding', 'instrumentation': 'full', 'seed': 42,
                'capacities': [2, 2], 'effective_masks': [[0, 1], [2, 3]], 'numa_labels': [0, 0],
                'allowed_cpus': [0, 1, 2, 3], 'planned_arrivals': 2, 'planned_tasks': 4,
                'epoch_monotonic_ns': epoch, 'pid': 100+index,
                **{key.replace('-', '_'): value for key, value in policies[policy].items()}}
        rows = []
        for task in range(4):
            burst = task//2
            request = (200_000_000, 800_000_000)[burst]
            planned = request+(400_000_000 if task % 2 else 0)
            submit = planned+1_000_000
            service = (50, 60, 100, 200)[task]*(1 if policy == 'home' else .5)
            home_n = (0, 2, 1, 4)[task]
            triggered = policy != 'home' and home_n/2 > .8
            pool = 1 if triggered else 0
            rows.append({'task': task, 'burst': burst, 'pool': pool, 'home_pool': 0,
                'status': 'completed', 'request_planned_ns': request, 'planned_ns': planned,
                'submit_ns': submit, 'start_ns': submit+int(service/2*1e6),
                'end_ns': submit+int(service*1e6), 'phase': 'steady' if task % 2 else 'initial',
                'target_cpu_us': 10000, 'home_outstanding': home_n, 'home_native_queue': 0,
                'selected_outstanding': 0 if triggered else home_n, 'selected_native_queue': 0,
                'capacity': 2, 'spilled': int(triggered), 'source_over_threshold': int(triggered),
                'no_eligible_target': 0, 'no_improvement': 0,
                'eligible_candidates': int(triggered)})
        summary = {'planned_tasks': 4, 'completed_tasks': 4, 'rejected_tasks': 0,
                   'canceled_tasks': 0, 'failed_tasks': 0, 'spilled_tasks': sum(r['spilled'] for r in rows),
                   'conservation_ok': True, 'counters_balanced': True, 'final_outstanding': 0,
                   'sampling_failed': False, 'process_user_cpu_seconds': .4,
                   'process_system_cpu_seconds': .01,
                   **{flag+'_tasks': sum(r[flag] for r in rows) for flag in
                      ('source_over_threshold', 'no_eligible_target', 'no_improvement')},
                   'final_pool_counters': [{'pool': p, 'pending': 0, 'inflight': 0,
                       'work_estimate_us': 0, 'outstanding': 0, 'remaining_work_us': 0} for p in range(2)]}
        csv_write(data/'tasks.csv', rows)
        dump(data/'metadata.json', meta); dump(data/'summary.json', summary)
        cpu = []
        for scope in ('union', 'pool0', 'pool1'):
            for start, end, total, pct in ((.4, .6, 30, 100), (.6, .8, 20, 50), (.8, 1., 40, 80), (1., 1.2, 20, 10)):
                cpu.append({'interval_start_monotonic_ns': epoch+round(start*1e9),
                            'monotonic_ns': epoch+round(end*1e9), 'elapsed_s': end, 'scope': scope,
                            'cpu_ids': '0,1,2,3' if scope == 'union' else '0,1' if scope == 'pool0' else '2,3',
                            'execution_pct': pct, 'iowait_pct': 0, 'steal_pct': 0,
                            'total_delta': total, 'valid': 1, 'reason': ''})
        csv_write(directory/'cpu_samples.csv', cpu)
        entry = {'policy': policy, 'path': str(directory.relative_to(root)),
                 'raw_dir': str(data.relative_to(root)), 'status': 'completed', 'returncode': 0,
                 'cpu_observation': {'status': 'ok'}, 'options': {**base, **policies[policy]},
                 'warmup_seconds': .5}
        dump(directory/'manifest.json', entry)
        entries.append(entry)
    manifest = {'schema_version': 1, 'plan_sha256': plan['plan_sha256'],
                'binary_sha256': 'same-binary', 'calibration': False, 'runs': entries}
    dump(root/'run_manifest.json', manifest)
    return root/'plan.json'


def modify_json(path, mutator):
    value = json.loads(path.read_text()); mutator(value); dump(path, value)


def modify_tasks(root, policy, mutator):
    path = root/'runs'/policy/'data/tasks.csv'
    with path.open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    mutator(rows); csv_write(path, rows)


class RequestWaveAnalysis(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_cohort_retains_late_release_and_drain_but_excludes_warmup_request(self):
        report = a.analyze(self.plan)
        self.assertTrue(report['comparison_valid'], report['runs'])
        home = report['runs'][0]['cohort']
        self.assertEqual((home['n'], home['requests']), (2, 1))
        self.assertEqual(home['S'], {'mean_ms': 150., 'p99_ms': 200.})
        self.assertEqual(home['L'], {'mean_ms': 151., 'p99_ms': 201.})
        self.assertEqual(home['D']['mean_ms'], 1.)
        self.assertEqual(home['S']['mean_ms'], home['W']['mean_ms']+home['X']['mean_ms'])
        self.assertAlmostEqual(report['comparisons'][0]['S_p99_ms_change_pct'], -50.)

    def test_fixed_buckets_pair_same_task_not_completion_window(self):
        report = a.analyze(self.plan)
        bucket = next(r for r in report['buckets'] if r['phase'] == 'all' and r['metric'] == 'N/C' and r['lower_inclusive'] == 2.)
        self.assertEqual(bucket['n'], 1)
        self.assertEqual(bucket['S_p99_ms'], 200.)
        self.assertEqual(bucket['tail_quality'], 'LOW_TAIL')
        zero_q = next(r for r in report['buckets'] if r['phase'] == 'all' and r['metric'] == 'Q/C' and r['lower_inclusive'] == 0.)
        self.assertEqual(zero_q['n'], 2)
        self.assertEqual(zero_q['S_p99_ms'], 200.)

    def test_cpu_uses_union_weighted_full_intervals_and_excludes_warmup_overlap(self):
        cpu = a.analyze(self.plan)['runs'][0]['cpu_arrival_window']['union']
        self.assertEqual(cpu['intervals'], 2)
        self.assertAlmostEqual(cpu['covered_seconds'], .4)
        self.assertAlmostEqual(cpu['execution_pct'], 70.)
        self.assertEqual(cpu['execution_sample_peak_pct'], 80.)

    def test_reordered_csv_is_not_a_workload_difference(self):
        modify_tasks(self.root, 'guarded', lambda rows: rows.reverse())
        self.assertTrue(a.analyze(self.plan)['comparison_valid'])

    def test_workload_trace_difference_prevents_comparison(self):
        modify_tasks(self.root, 'guarded', lambda rows: rows[0].update(target_cpu_us='10001'))
        report = a.analyze(self.plan)
        self.assertFalse(report['comparison_valid'])
        self.assertIn('task_workload_trace_mismatch', report['comparison_issues'])
        self.assertEqual(report['comparisons'], [])

    def test_observation_resource_difference_prevents_comparison(self):
        modify_json(self.root/'runs/guarded/data/metadata.json', lambda meta: meta.update(instrumentation='none'))
        report = a.analyze(self.plan)
        self.assertFalse(report['comparison_valid'])
        self.assertIn('resource_or_observation_or_workload_config_mismatch', report['comparison_issues'])

    def test_missing_task_is_invalid_even_if_remaining_ids_look_contiguous(self):
        modify_tasks(self.root, 'home', lambda rows: rows.pop())
        report = a.analyze(self.plan)
        self.assertIn('planned_task_count_mismatch', report['runs'][0]['issues'])
        self.assertIn('incomplete_request_tasks', report['runs'][0]['issues'])
        self.assertFalse(report['comparison_valid'])

    def test_failed_task_or_counter_imbalance_invalidates_run(self):
        modify_tasks(self.root, 'home', lambda rows: rows[0].update(status='failed'))
        modify_json(self.root/'runs/home/data/summary.json', lambda summary: summary['final_pool_counters'][0].update(outstanding=1))
        issues = a.analyze(self.plan)['runs'][0]['issues']
        self.assertIn('noncompleted_or_empty_tasks', issues)
        self.assertIn('nonzero_final_pool_counter', issues)

    def test_illegal_clock_and_guard_target_are_rejected(self):
        modify_tasks(self.root, 'guarded', lambda rows: rows[3].update(selected_outstanding='2', start_ns='0'))
        issues = a.analyze(self.plan)['runs'][2]['issues']
        self.assertIn('invalid_task_clock_order', issues)
        self.assertIn('guarded_target_limit_violated', issues)

    def test_manifest_hash_drift_prevents_comparison(self):
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest.update(plan_sha256='different'))
        self.assertFalse(a.analyze(self.plan)['comparison_valid'])

    def test_original_plan_tampering_is_detected_before_calibration_changes(self):
        modify_json(self.plan, lambda plan: plan.update(extra_tampered_value=1))
        report = a.analyze(self.plan)
        self.assertIn('original_plan_hash_mismatch', report['runs'][0]['issues'])

    def test_incomplete_noncalibration_manifest_returns_failure(self):
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest.update(runs=manifest['runs'][:1]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--plan', str(self.plan)]), 2)

    def test_sampling_truncation_is_invalid(self):
        modify_json(self.root/'runs/home/data/summary.json', lambda summary: summary.update(sampling_truncated=True))
        self.assertIn('sampling_truncated', a.analyze(self.plan)['runs'][0]['issues'])

    def test_guarded_vs_source_only_is_an_explicit_comparison(self):
        report = a.analyze(self.plan)
        row = next(r for r in report['comparisons'] if r['baseline'] == 'source_only')
        self.assertEqual(row['policy'], 'guarded')
        self.assertEqual(row['S_p99_ms_change_pct'], 0.)

    def test_two_planned_policies_are_a_complete_comparison(self):
        plan = a.read_json(self.plan)
        plan['profile'] = 'short-peak'
        plan['policies'].pop('guarded')
        plan['plan_sha256'] = a.digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
        dump(self.plan, plan)
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest.update(
            runs=manifest['runs'][:2], plan_sha256=plan['plan_sha256']))
        report = a.analyze(self.plan)
        self.assertTrue(report['comparison_valid'], report['comparison_issues'])
        self.assertEqual([(r['baseline'], r['policy']) for r in report['comparisons']], [('home', 'source_only')])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--plan', str(self.plan)]), 0)

    def test_explicit_home_analysis_only_never_claims_policy_comparison(self):
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest.update(
            runs=manifest['runs'][:1], analysis_only=True))
        report = a.analyze(self.plan)
        self.assertFalse(report['comparison_valid'])
        self.assertEqual(report['comparisons'], [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--plan', str(self.plan)]), 0)
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest['runs'][0].update(policy='source_only'))
        with self.assertRaisesRegex(ValueError, 'one home'):
            a.analyze(self.plan)

    def test_phase_budgets_are_printed_and_actual_targets_are_retained(self):
        plan = a.read_json(self.plan)
        plan['base'].update({'initial-cpu-us': 1000, 'steady-cpu-us': 49000})
        plan['plan_sha256'] = a.digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
        dump(self.plan, plan)
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest.update(plan_sha256=plan['plan_sha256']))
        for policy in a.POLICIES:
            modify_json(self.root/f'runs/{policy}/data/metadata.json', lambda meta: meta.update(initial_cpu_us=1000, steady_cpu_us=49000))
            modify_tasks(self.root, policy, lambda rows: [row.update(target_cpu_us='1000' if row['phase'] == 'initial' else '49000') for row in rows])
        report = a.analyze(self.plan)
        self.assertTrue(report['comparison_valid'], report['runs'])
        self.assertEqual(report['runs'][0]['phase_cpu_budgets_us']['initial']['target_mean'], 1000)
        self.assertEqual(report['runs'][0]['phase_cpu_budgets_us']['steady']['target_mean'], 49000)
        rendered = a.render(report)
        self.assertIn('initial=1000us; steady=49000us', rendered)
        self.assertIn('PHASE TARGET CPU home:', rendered)

    def test_functional_only_and_waveform_gate_are_visible_in_standalone_report(self):
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest.update(
            functional_only=True, home_waveform_gate={'status': 'functional_only',
                'passed': False, 'comparison_allowed': True, 'reasons': ['no CPU trace']}))
        report = a.analyze(self.plan)
        self.assertTrue(report['functional_only'])
        self.assertFalse(report['home_waveform_gate']['passed'])
        rendered = a.render(report)
        self.assertIn('FUNCTIONAL_ONLY:', rendered)
        self.assertIn('waveform is unvalidated', rendered)
        self.assertIn('HOME WAVEFORM GATE', rendered)

    def test_initial_submission_span_is_per_request_and_ignores_steady(self):
        rows = [dict(burst=0, phase='initial', planned_ns=0, submit_ns=1_000_000),
                dict(burst=0, phase='initial', planned_ns=20_000_000, submit_ns=41_000_000),
                dict(burst=0, phase='steady', planned_ns=400_000_000, submit_ns=500_000_000),
                dict(burst=1, phase='initial', planned_ns=1_000_000_000, submit_ns=1_001_000_000),
                dict(burst=1, phase='initial', planned_ns=1_020_000_000, submit_ns=1_021_000_000)]
        spans = a.initial_submission_spans(rows)
        self.assertEqual(spans['actual_span_ms'], {'mean_ms': 30., 'p99_ms': 40.})
        self.assertEqual(spans['over_30ms_fraction'], .5)
        self.assertEqual(spans['planned_span_ms']['mean_ms'], 20.)

    def test_cpu_duplicate_and_insufficient_coverage_are_not_ok(self):
        path = self.root/'runs/home/cpu_samples.csv'
        with path.open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        rows.append(dict(rows[1]))
        csv_write(path, rows)
        cpu = a.analyze(self.plan)['runs'][0]['cpu_arrival_window']['union']
        self.assertEqual(cpu['status'], 'partial')
        self.assertIn('overlapping_or_duplicate_intervals', cpu['issues'])
        self.assertAlmostEqual(cpu['covered_seconds'], .4)
        rows = [row for row in rows if row['scope'] != 'union']+[rows[1]]
        csv_write(path, rows)
        cpu = a.analyze(self.plan)['runs'][0]['cpu_arrival_window']['union']
        self.assertEqual(cpu['status'], 'partial')
        self.assertIn('fewer_than_two_complete_intervals', cpu['issues'])
        self.assertIn('insufficient_window_coverage', cpu['issues'])

    def test_saved_artifact_hash_mismatch_prevents_comparison(self):
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest['runs'][0].update(artifact_sha256={'data/tasks.csv': 'bad-hash'}))
        self.assertIn('artifact_hash_mismatch:data/tasks.csv', a.analyze(self.plan)['runs'][0]['issues'])

    def test_decision_summary_counter_must_match_task_flags(self):
        modify_json(self.root/'runs/source_only/data/summary.json', lambda summary: summary.update(source_over_threshold_tasks=999))
        self.assertIn('source_over_threshold_count_mismatch', a.analyze(self.plan)['runs'][1]['issues'])

    def test_cpu_scope_must_match_actual_resource_assignment(self):
        path = self.root/'runs/home/cpu_samples.csv'
        with path.open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        rows[1]['cpu_ids'] = '0,1'
        csv_write(path, rows)
        report = a.analyze(self.plan)
        self.assertFalse(report['runs'][0]['valid'])
        self.assertTrue(any('CPU sample scope' in issue for issue in report['runs'][0]['issues']))

    def test_calibration_uses_its_actual_window_and_does_not_require_three_runs(self):
        manifest = a.read_json(self.root/'run_manifest.json')
        manifest['runs'] = manifest['runs'][:1]
        manifest['calibration'] = True
        manifest['runs'][0]['warmup_seconds'] = .1
        dump(self.root/'calibration_manifest.json', manifest)
        with contextlib.redirect_stdout(io.StringIO()):
            code = a.main(['--plan', str(self.plan), '--calibration', '--output', str(self.root/'calibration-analysis')])
        self.assertEqual(code, 0)
        report = a.read_json(self.root/'calibration-analysis/report.json')
        self.assertEqual(report['cohort']['warmup_seconds'], .1)
        self.assertEqual(report['runs'][0]['cohort']['n'], 4)
        self.assertFalse(report['comparison_valid'])

    def test_cli_artifacts_and_invalid_exit_status(self):
        out = self.root/'analysis'
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(a.main(['--plan', str(self.plan), '--out', str(out)]), 0)
        self.assertIn('HOME ADMISSION BUCKETS', stdout.getvalue())
        self.assertIn('CPU_budget=', stdout.getvalue())
        self.assertIn('workers_per_pool=', stdout.getvalue())
        self.assertIn('baseline,policy', stdout.getvalue())
        self.assertTrue((out/'report.json').is_file())
        self.assertTrue((out/'report.txt').is_file())
        self.assertTrue((out/'buckets.csv').is_file())
        modify_json(self.root/'run_manifest.json', lambda manifest: manifest['runs'][0].update(returncode=1))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--plan', str(self.plan), '--out', str(out)]), 2)


if __name__ == '__main__':
    unittest.main()

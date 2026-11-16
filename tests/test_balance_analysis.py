#!/usr/bin/env python3
"""Adversarial analysis checks: censoring, plan mismatch and train/test leakage."""
import copy
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('balance_analysis', ROOT/'experiments/independent/scripts/analyze_balance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def fixture(root, name='home', seed=1, routing='home', split='observe', queues=None,
            rejected=False, cpu_us=1000, observation='progress', duration=1., completed_lag=0,
            mutate_metadata=None):
    path = root/f'{name}-{seed}-{split}'
    path.mkdir()
    queues = list(range(20)) if queues is None else list(queues)
    metadata = {'stage':3, 'duration_seconds':duration, 'planned_tasks':len(queues), 'seed':seed,
        'routing':routing, 'load_metric':'outstanding', 'load_threshold':1, 'min_improvement':.2,
        'effective_load_observation':observation, 'instrumentation':'full', 'progress_us':1000,
        'capacities':[2, 2], 'threads_per_pool':8, 'effective_masks':[[0, 1], [2, 3]],
        'pools':2, 'cpu_us':cpu_us, 'arrival_rate':10, 'burst_min':10, 'burst_max':10,
        'estimate_us':1000, 'work_mode':'cpu', 'hostname':'fixture', 'machine':'test'}
    if mutate_metadata:
        metadata.update(mutate_metadata)
    rows = []
    for index, q in enumerate(queues):
        planned = (index//10)*100_000_000
        submit = planned+1000
        start = submit+q*100_000
        end = start+cpu_us*1000+completed_lag
        bad = rejected and index == len(queues)-1
        rows.append(dict(task=index, burst=index//10, pool=0, status='rejected' if bad else 'completed',
            target_cpu_us=cpu_us, estimate_us=1000, planned_ns=planned, submit_ns=submit,
            start_ns=0 if bad else start, end_ns=0 if bad else end, native_queue=q,
            pending=q, inflight=q, work_estimate_us=q*2000, thread_cpu_ns=cpu_us*1000, worker_tid=10,
            home_pool=0, home_native_queue=q, home_outstanding=q*2, home_work_estimate_us=q*2000,
            home_remaining_work_us=q*1000, capacity=2, home_score=q, selected_score=q,
            selected_native_queue=q, selected_outstanding=q*2, selected_work_estimate_us=q*2000,
            selected_remaining_work_us=q*1000, route_ns=80 if observation != 'off' else -1,
            route_candidates=2, spilled=int(routing != 'home'), burst_index=index%10, burst_remaining=9-index%10))
    with (path/'tasks.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    summary = {'planned_tasks':len(rows), 'completed_tasks':len(rows)-int(rejected),
        'rejected_tasks':int(rejected), 'canceled_tasks':0, 'failed_tasks':0, 'conservation_ok':True,
        'observed_seconds':duration+completed_lag/1e9, 'process_user_cpu_seconds':.01*len(rows),
        'process_system_cpu_seconds':.001*len(rows), 'counters_balanced':True, 'final_outstanding':0}
    (path/'metadata.json').write_text(json.dumps(metadata))
    (path/'summary.json').write_text(json.dumps(summary))
    return {'run_id':path.name, 'case':name, 'seed':seed, 'split':split, 'comparison_group':'same-workload',
            'path':str(path), 'returncode':0, 'options':{}}, rows


def write_manifest(root, entries, **extra):
    manifest = root/'manifest.json'
    manifest.write_text(json.dumps({'runs':entries, 'binary_sha256':'same-binary', **extra}))
    return manifest


class Statistics(unittest.TestCase):
    def test_wave_requires_request_cohort_analyzer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            entry,_=fixture(root,mutate_metadata={'workload':'request-wave'})
            with self.assertRaisesRegex(ValueError,'analyze_request_wave'):
                a.load_run(entry['path'],entry,{})

    def test_flat_phase_csv_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            entry,rows=fixture(root)
            for row in rows:
                row.update(phase='initial',request_planned_ns=row['planned_ns'])
            with (Path(entry['path'])/'tasks.csv').open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
                writer.writeheader();writer.writerows(rows)
            loaded=a.load_run(entry['path'],entry,{})
            self.assertEqual(loaded['rows'][0]['phase'],'initial')

    def test_legacy_policy_id_cannot_hide_target_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            entry,_=fixture(Path(directory),mutate_metadata={'accept_threshold':.5})
            with self.assertRaisesRegex(ValueError,'legacy policy IDs'):
                a.load_run(entry['path'],entry,{})

    def test_constant_correlation_is_not_zero(self):
        self.assertIsNone(a.pearson([1, 1, 1], [2, 4, 8]))
        self.assertIsNone(a.pearson([], []))
        self.assertEqual(a.ranks([2, 1, 1]), [3, 1.5, 1.5])
        self.assertEqual(a.quantile(list(range(100))), 98)

    def test_p99_prediction_loss_and_coverage(self):
        result = a.prediction_error([1, 10], [(1, 5), (10, 5)])
        self.assertEqual(result['mean_mae_ms'], 0)
        self.assertEqual(result['p99_coverage'], .5)
        self.assertAlmostEqual(result['p99_pinball_ms'], (4*.01+5*.99)/2)

    def test_training_only_boundaries(self):
        training = [{'load':x, 'y':10*x} for x in range(1, 101)]
        fitted = a.fit_buckets(training, ('load',), 'y', bins=4)
        self.assertEqual(fitted['boundaries']['load'], [25, 50, 75])
        before = copy.deepcopy(fitted['boundaries'])
        predicted, _ = a.predict(fitted, [{'load':10000}])
        self.assertEqual(fitted['boundaries'], before)
        self.assertLess(predicted[0][0], 1000)

    def test_bootstrap_requires_independent_seed_count(self):
        self.assertIsNone(a.bootstrap_ci([1]*6))
        self.assertEqual(a.bootstrap_ci([1]*7), [1, 1])


class EvidenceGuards(unittest.TestCase):
    def test_slow_completion_retained_in_planned_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, _ = fixture(root, completed_lag=2_000_000_000)
            report = a.analyze([write_manifest(root, [entry])], start=0, end=.05)
            stats = report['runs'][0]['statistics']
            self.assertEqual(stats['cohort_completed'], 10)
            self.assertEqual(stats['cohort_complete_bursts'], 1)
            self.assertGreater(stats['planned_latency_ms_mean'], 2000)
            self.assertGreater(stats['burst_planned_p99_ms'], 2000)

    def test_rejection_cannot_look_like_speedup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune', completed_lag=500_000_000)
            candidate, _ = fixture(root, 'fast', routing='threshold', split='tune', rejected=True)
            report = a.analyze([write_manifest(root, [home, candidate])])
            pair = report['pairs'][0]
            self.assertLess(pair['deltas']['planned_latency_ms_mean'], 0)
            self.assertFalse(pair['valid'])
            self.assertIn('rejected', pair['reasons'])
            self.assertEqual(report['comparisons'][0]['n_pairs'], 0)
            self.assertIsNone(report['comparisons'][0]['planned_mean_delta_ms'])

    def test_identical_config_but_changed_planned_work_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune')
            candidate, rows = fixture(root, 'threshold', routing='threshold', split='tune')
            rows[0]['target_cpu_us'] += 1
            path = Path(candidate['path'])/'tasks.csv'
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
            report = a.analyze([write_manifest(root, [home, candidate])])
            self.assertIn('planned_task_budget_sequence_mismatch', report['pairs'][0]['reasons'])
            self.assertFalse(report['pairs'][0]['valid'])

    def test_observation_cost_cannot_masquerade_as_routing_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune')
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune', observation='basic')
            report = a.analyze([write_manifest(root, [home, candidate])])
            self.assertIn('missing_or_ambiguous_matched_home', report['pairs'][0]['reasons'])

    def test_tune_seed_not_reused_for_confirming_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='evaluate')
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='evaluate')
            manifest = write_manifest(root, [home, candidate], spec={'selection':{'source_seeds':[1, 2]}})
            report = a.analyze([manifest])
            self.assertIn('selection_seed_leakage', report['pairs'][0]['reasons'])
            self.assertEqual(report['quality_groups'], [])

    def test_missing_failed_candidate_visible_in_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='evaluate')
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='evaluate')
            missing = dict(candidate, path=str(root/'missing'), seed=2)
            report = a.analyze([write_manifest(root, [home, candidate, missing])])
            self.assertEqual(len(report['excluded_entries']), 1)
            self.assertFalse(report['comparisons'][0]['all_pairs_valid'])
            self.assertEqual(report['comparisons'][0]['confirmation'], 'incomplete_evidence')

    def test_whole_seed_holdout_and_redundant_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _ = fixture(root, seed=1, queues=range(20))
            second, _ = fixture(root, seed=2, queues=range(1000, 1020))
            report = a.analyze([write_manifest(root, [first, second])])
            self.assertEqual(len(report['quality_groups']), 1)
            self.assertTrue(report['quality_groups'][0]['redundant_signals'])
            held = [row for row in report['prediction'] if row['held_seed'] == 2 and row['model'] == 'queue']
            self.assertTrue(held)
            for row in held:
                self.assertEqual(row['training_seeds'], '1')
                self.assertLess(max(json.loads(row['training_boundaries'])['queue']), 20)
            self.assertTrue(all(row['tail_quality'] == 'LOW_TAIL' for row in report['buckets']))
            self.assertTrue(any(row['model_kind'] == 'context' for row in report['prediction']))

    def test_truncated_csv_is_not_zero_work_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, _ = fixture(root)
            path = Path(entry['path'])/'tasks.csv'
            path.write_text('\n'.join(path.read_text().splitlines()[:-1])+'\n')
            report = a.analyze([write_manifest(root, [entry])])
            self.assertEqual(report['runs'], [])
            self.assertIn('truncated task CSV', report['excluded_entries'][0]['reason'])

    def test_missing_metric_not_filled_with_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, rows = fixture(root)
            for row in rows:
                row['home_remaining_work_us'] = -1
            path = Path(entry['path'])/'tasks.csv'
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
            report = a.analyze([write_manifest(root, [entry])])
            missing = [r for r in report['correlations'] if r['metric'] == 'remaining_ms_per_capacity']
            self.assertEqual(len(missing), 4)
            self.assertTrue(all(r['n'] == 0 and r['pearson'] is None for r in missing))

    def test_minimal_different_metric_is_not_same_instrumentation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune', observation='minimal')
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune', observation='minimal',
                                   mutate_metadata={'load_metric':'remaining'})
            report = a.analyze([write_manifest(root, [home, candidate])])
            self.assertFalse(report['pairs'][0]['valid'])
            self.assertIn('missing_or_ambiguous_matched_home', report['pairs'][0]['reasons'])

    def test_cost_allows_only_intentional_observer_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            off, _ = fixture(root, 'off', split='cost', observation='off', mutate_metadata={
                'work_mode':'iterations', 'maintain_outstanding':False, 'maintain_work':False, 'publish_remaining':False})
            minimal, _ = fixture(root, 'minimal', split='cost', observation='minimal', mutate_metadata={
                'work_mode':'iterations', 'maintain_outstanding':True, 'maintain_work':False, 'publish_remaining':False})
            off['comparison_group'] = 'base-obs-off'
            minimal['comparison_group'] = 'base-obs-minimal-outstanding'
            report = a.analyze([write_manifest(root, [off, minimal])])
            self.assertEqual(report['pairs'], [])
            self.assertEqual(len(report['observer_cost_pairs']), 1)
            self.assertTrue(report['observer_cost_pairs'][0]['valid'])

    def test_cpu_budget_mode_cannot_hide_observer_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            off, _ = fixture(root, 'off', split='cost', observation='off')
            basic, _ = fixture(root, 'basic', split='cost', observation='basic')
            off['comparison_group'] = 'base-obs-off'
            basic['comparison_group'] = 'base-obs-basic'
            report = a.analyze([write_manifest(root, [off, basic])])
            self.assertFalse(report['observer_cost_pairs'][0]['valid'])
            self.assertIn('observer_cost_requires_fixed_iterations', report['observer_cost_pairs'][0]['reasons'])

    def test_clock_damage_outside_cohort_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry, rows = fixture(root)
            rows[-1]['start_ns'] = rows[-1]['submit_ns']-1
            path = Path(entry['path'])/'tasks.csv'
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
            report = a.analyze([write_manifest(root, [entry])], start=0, end=.05)
            self.assertEqual(report['runs'], [])
            self.assertIn('invalid task clock order', report['excluded_entries'][0]['reason'])

    def test_injection_config_must_match_even_if_no_failure_fired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune')
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune',
                                   mutate_metadata={'fail_submit_task':1000000})
            report = a.analyze([write_manifest(root, [home, candidate])])
            self.assertFalse(report['pairs'][0]['valid'])
            self.assertIn('missing_or_ambiguous_matched_home', report['pairs'][0]['reasons'])

    def test_evaluate_cannot_change_frozen_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='evaluate', seed=31)
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='evaluate', seed=31)
            manifest = write_manifest(root, [home, candidate], spec={'selection':{
                'source_split':'tune', 'source_seeds':[21, 22], 'metric':'outstanding', 'threshold':2}})
            report = a.analyze([manifest])
            self.assertFalse(report['pairs'][0]['valid'])
            self.assertIn('frozen_policy_mismatch', report['pairs'][0]['reasons'])

    def test_output_serializable_and_no_global_p99_average(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune', queues=[1]*19+[1000])
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune', queues=[1]*19+[500])
            report = a.analyze([write_manifest(root, [home, candidate])])
            json.dumps(report, allow_nan=False)
            self.assertAlmostEqual(report['pairs'][0]['deltas']['planned_latency_ms_p99'], -50)
            a.emit(report, root/'analysis')
            self.assertTrue((root/'analysis/report.json').is_file())
            self.assertTrue((root/'analysis/threshold_curve.csv').is_file())


class HostnameAliases(unittest.TestCase):
    ALIASES = {'localhost.localdomain':'localhost'}
    REASON = 'Continuous run on the same host; only hostname changed.'

    def test_56_run_hostname_change_is_strict_by_default_and_explicitly_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for seed in range(31000, 31007):
                for threads in (16, 256):
                    for policy in ('home', 'outstanding', 'queue', 'spread'):
                        hostname = 'localhost.localdomain' if seed < 31004 else 'localhost'
                        # A name change during this seed leaves exactly three
                        # existing home records in a different strict config.
                        if seed == 31004 and ((threads == 16 and policy in ('home', 'spread')) or
                                              (threads == 256 and policy == 'spread')):
                            hostname = 'localhost.localdomain'
                        entry, _ = fixture(root, f't{threads}-{policy}', seed=seed, split='evaluate',
                            routing=policy if policy in ('home', 'spread') else 'threshold',
                            mutate_metadata={'hostname':hostname, 'threads_per_pool':threads,
                                             'load_metric':'queue' if policy == 'queue' else 'outstanding'})
                        entry['comparison_group'] = f'main-t{threads}'
                        entries.append(entry)
            manifest = write_manifest(root, entries, selection={'source_split':'tune', 'source_seeds':[30000],
                'metric':'outstanding', 'threshold':1, 'queue_threshold':1})
            original_files = {Path(e['path'])/'metadata.json':(Path(e['path'])/'metadata.json').read_bytes()
                              for e in entries}
            strict = a.analyze([manifest])
            self.assertEqual(len(strict['runs']), 56)
            invalid = [p for p in strict['pairs'] if not p['valid']]
            self.assertEqual(len(invalid), 3)
            self.assertTrue(all(p['seed'] == 31004 for p in invalid))
            self.assertTrue(all('home_config_mismatch' in p['reasons'] for p in invalid))
            for pair in invalid:
                homes = pair['baseline_match']['same_seed_homes']
                self.assertEqual(len(homes), 1)
                self.assertEqual(set(homes[0]['config_differences']), {'hostname'})
            self.assertEqual(len(strict['comparisons']), 12)
            self.assertTrue(all(set(g['differences']) == {'hostname'} for g in strict['config_variants']))
            self.assertEqual(strict['normalization']['hostname_aliases'], {})
            normalized = a.analyze([manifest], hostname_aliases=self.ALIASES, hostname_alias_reason=self.REASON)
            self.assertEqual(len(normalized['comparisons']), 6)
            for comparison in normalized['comparisons']:
                self.assertEqual(comparison['n_pairs'], 7)
                self.assertEqual(comparison['n_invalid'], 0)
                self.assertEqual(comparison['status'], 'paired_seed_bootstrap')
                self.assertIsNotNone(comparison['planned_latency_ms_p99_delta_ci95'])
            self.assertTrue(all(not g['differences'] for g in normalized['config_variants']))
            self.assertEqual(normalized['normalization']['reason'], self.REASON)
            self.assertGreater(normalized['normalization']['applied_runs'], 0)
            self.assertEqual(normalized['manifests'][0]['sha256'], strict['manifests'][0]['sha256'])
            for raw, fixed in zip(strict['runs'], normalized['runs']):
                self.assertEqual(fixed['metadata'], raw['metadata'])
                self.assertEqual(fixed['raw_config_id'], raw['config_id'])
                self.assertEqual(fixed['config']['hostname'], 'localhost')
            self.assertTrue(all(path.read_bytes() == original for path, original in original_files.items()))
            rendered = a.report_markdown(normalized)
            self.assertIn('config_id', rendered)
            self.assertIn(self.REASON, rendered)
            json.dumps(normalized, allow_nan=False)
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                a.emit(normalized, root/'normalized-analysis')
            self.assertIn('PAIRING_SUMMARY comparison_groups=6 valid_pairs=42 invalid_pairs=0', printed.getvalue())
            self.assertIn('config_ids=2 raw_metadata_preserved=true', printed.getvalue())

    def test_alias_does_not_relax_resource_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, _ = fixture(root, split='tune', mutate_metadata={'hostname':'localhost.localdomain'})
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune',
                mutate_metadata={'hostname':'localhost', 'effective_masks':[[0, 1], [4, 5]]})
            report = a.analyze([write_manifest(root, [home, candidate])],
                hostname_aliases=self.ALIASES, hostname_alias_reason=self.REASON)
            pair = report['pairs'][0]
            self.assertFalse(pair['valid'])
            differences = pair['baseline_match']['same_seed_homes'][0]['config_differences']
            self.assertEqual(set(differences), {'effective_masks'})
            self.assertEqual(set(report['config_variants'][0]['differences']), {'effective_masks'})

    def test_missing_home_is_distinct_from_config_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune')
            report = a.analyze([write_manifest(root, [candidate])])
            self.assertIn('missing_home', report['pairs'][0]['reasons'])
            self.assertEqual(report['pairs'][0]['baseline_match']['same_seed_homes'], [])

    def test_ambiguous_home_remains_invalid_after_aliasing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _ = fixture(root, 'first-home', split='tune', mutate_metadata={'hostname':'localhost.localdomain'})
            second, _ = fixture(root, 'second-home', split='tune', mutate_metadata={'hostname':'localhost'})
            candidate, _ = fixture(root, 'threshold', routing='threshold', split='tune', mutate_metadata={'hostname':'localhost'})
            report = a.analyze([write_manifest(root, [first, second, candidate])],
                hostname_aliases=self.ALIASES, hostname_alias_reason=self.REASON)
            self.assertIn('ambiguous_matched_home', report['pairs'][0]['reasons'])
            self.assertEqual(report['comparisons'][0]['n_pairs'], 0)

    def test_alias_parser_rejects_malformed_conflicting_or_chained_names(self):
        for values in (['host'], ['=host'], ['host='], ['old=two words'], ['old=new=other'],
                       ['old=new', 'old=other'], ['old=new', 'new=canonical'], ['old=new', 'new=old']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                a.parse_hostname_aliases(values)
        self.assertEqual(a.parse_hostname_aliases(['first=canonical', 'second=canonical']),
                         {'first':'canonical', 'second':'canonical'})

    def test_alias_requires_explanation_in_api_and_cli(self):
        for reason in (None, '', '  '):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, 'hostname-alias-reason'):
                a.analyze([], hostname_aliases=self.ALIASES, hostname_alias_reason=reason)
        with self.assertRaisesRegex(ValueError, 'requires hostname-alias'):
            a.analyze([], hostname_alias_reason=self.REASON)
        result = subprocess.run([sys.executable, str(ROOT/'experiments/independent/scripts/analyze_balance.py'),
            '--manifest', 'unused.json', '--output', 'unused-output', '--hostname-alias', 'old=new'],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('hostname-alias-reason', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()

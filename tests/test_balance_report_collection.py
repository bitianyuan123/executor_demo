#!/usr/bin/env python3
"""Evidence collection guards: exact subset, provenance, missing data and no new CI."""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('collection', ROOT/'experiments/independent/scripts/collect_balance_report.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
aspec = importlib.util.spec_from_file_location('analysis_for_collection_test', ROOT/'experiments/independent/scripts/analyze_balance.py')
a = importlib.util.module_from_spec(aspec)
aspec.loader.exec_module(a)


def run(group, suffix, seed, phase):
    case = group+'-'+suffix
    minimal = phase == 'minimal'
    stats = {key: float(index+1) for index, key in enumerate(a.PAIR_FIELDS)}
    stats.update(cohort_planned=20, cohort_completed=20, cohort_bursts=2, spill_fraction=0 if suffix == 'home' else .67)
    return {'id':case+'-s'+str(seed), 'case':case,
        'group':group+'-minimal-outstanding' if minimal else group,
        'seed':seed, 'split':'evaluate' if minimal else phase,
        'routing':'home' if suffix == 'home' else 'spread' if suffix == 'spread-policy' else 'threshold',
        'metric':'outstanding', 'threshold':2, 'min_improvement':0,
        'config_id':group+('-minimal' if minimal else '-progress'),
        'config':{'threads_per_pool':16 if group == c.GROUPS[0] else 256, 'window':[10,60], 'hostname':'test'},
        'metadata':{'hostname':'test'}, 'summary':{'conservation_ok':True},
        'statistics':stats, 'valid':True, 'invalid_reasons':[], 'sequence_sha256':str(seed),
        'planned_time_quartiles':[{'quarter':1,'planned_mean_ms':123,'planned_p99_ms':234,'submit_lag_mean_ms':.1}]}


def fixtures():
    reports = {}
    for phase in c.PHASE_PATHS:
        suffixes = {'observe':('home',), 'tune':('home','spread-policy','queue-h0.5','queue-h2','outstanding-h0.5','outstanding-h2','remaining-h50','remaining-h200'),
            'evaluate':('home','spread-policy','queue-h2','outstanding-h2'), 'minimal':('home','spread-policy','outstanding-h2')}[phase]
        groups = (c.GROUPS[1],) if phase == 'minimal' else c.GROUPS
        runs = [run(g,s,seed,phase) for g in groups for s in suffixes for seed in c.SEEDS[phase]]
        reports[phase] = {'schema':1, 'script_sha256':'original-analysis-hash','runs':runs,
            'excluded_entries':[], 'config_variants':[], 'normalization':{'hostname_aliases':{}},
            'manifests':[{'sha256':'manifesthash','path':'/unchanged/source'}],
            'comparisons':[], 'quality_groups':[], 'correlations':[], 'prediction':[], 'buckets':[]}
        if phase in ('tune','evaluate'):
            for g in groups:
                for suffix in suffixes:
                    if suffix == 'home':
                        continue
                    reports[phase]['comparisons'].append({'group':g, 'case':g+'-'+suffix,'config_id':g+'-progress',
                        'seeds':list(c.SEEDS[phase]), 'n_pairs':len(c.SEEDS[phase]),'n_invalid':0,'all_pairs_valid':True,
                        'status':'paired_seed_bootstrap' if phase == 'evaluate' else 'descriptive_only',
                        'planned_latency_ms_p99_delta_ci95':[-42,-37] if phase == 'evaluate' else None})
    observe = reports['observe']
    for group in c.GROUPS:
        members = [r for r in observe['runs'] if r['group'] == group]
        observe['quality_groups'].append({'group':group,'config_id':group+'-progress', 'run_ids':[r['id'] for r in members],
            'seeds':list(c.SEEDS['observe']), 'status':'evaluated',
            'redundant_signals':[{'relation':'work = outstanding * constant estimate','constant_ms':100}]})
    for r in observe['runs']:
        for metric in c.METRICS:
            for target in c.TARGETS:
                constant = metric == 'queue_per_capacity'
                observe['correlations'].append({'group':r['group'],'config_id':r['config_id'],'run':r['id'], 'seed':r['seed'],
                    'metric':metric,'target':target,'n':20,'coverage':1,'distinct_values':1 if constant else 20,
                    'status':'constant' if constant else 'descriptive','pearson':None if constant else .8,'spearman':None if constant else .7})
                for model in ('global','own_budget_oracle',metric,metric+'+own_budget_oracle'):
                    observe['prediction'].append({'group':r['group'],'config_id':r['config_id'],'held_seed':r['seed'],
                        'training_seeds':','.join(str(s) for s in c.SEEDS['observe'] if s != r['seed']),
                        'metric':metric,'target':target,'model':model,'training_n':40,'test_available_n':20,'test_total_n':20,
                        'test_unique_bursts':2,'tail_quality':'LOW_TAIL','mean_mae_ms':13.1234567890123,
                        'p99_pinball_ms':.15,'p99_coverage':.98,'n':20,'fallback_n':0,'training_boundaries':'{}'})
            for index in range(8):
                for target in ('wait_ms','callback_ms','planned_latency_ms'):
                    observe['buckets'].append({'group':r['group'],'config_id':r['config_id'],'held_seed':r['seed'],
                        'metric':metric,'target':target,'bucket':index,
                        'lower_inclusive':None if index == 0 else index+(r['seed']-11000)*.01,
                        'upper_exclusive':None if index == 7 else index+1+(r['seed']-11000)*.01,
                        'boundary_source':'training_seeds_only','n':3 if index < 4 else 2,'unique_bursts':2,'mean_ms':float(index+1),
                        'p99_ms':float(index+3),'tail_quality':'LOW_TAIL'})
    # The source can contain 22 completed runs; only the predetermined nine count.
    for seed in range(32003,32007):
        for suffix in ('home','outstanding-h2','spread-policy'):
            reports['minimal']['runs'].append(run(c.GROUPS[1],suffix,seed,'minimal'))
    reports['minimal']['runs'].append(run(c.GROUPS[0],'home',32000,'minimal'))
    return reports


class Collection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reports = fixtures()
        self.save()

    def save(self):
        for phase, relative in c.PHASE_PATHS.items():
            path = self.root/relative
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(self.reports[phase]))

    def test_exact_nine_of_twenty_two_and_no_minimal_ci(self):
        result = c.collect(self.root)
        self.assertEqual(result['warnings'], [])
        self.assertEqual(len(result['runs']['minimal']),9)
        self.assertEqual(result['provenance']['minimal']['source_run_count'],22)
        self.assertEqual(len(result['minimal_pairs']),6)
        minimal = [r for r in result['comparisons'] if r['phase']=='minimal']
        self.assertEqual(len(minimal),2)
        self.assertTrue(all(r['n_pairs']==3 and r['status']=='descriptive_only' for r in minimal))
        self.assertTrue(all(r['planned_latency_ms_p99_delta_ci95'] is None for r in minimal))
        evaluate = [r for r in result['comparisons'] if r['phase']=='evaluate']
        self.assertEqual(evaluate[0]['planned_latency_ms_p99_delta_ci95'],[-42,-37])
        self.assertEqual(evaluate[0]['status'],'paired_seed_bootstrap')

    def test_missing_duplicate_invalid_and_excluded_visible(self):
        report = self.reports['minimal']
        report['runs'].pop(0)  # expected home seed missing
        report['runs'].append(copy.deepcopy(report['runs'][3]))
        report['runs'][1]['valid'] = False
        report['runs'][1]['invalid_reasons'] = ['drain_incomplete']
        report['excluded_entries'] = [{'case':'main-t256-local-home','reason':'bad CSV','seed':32000}]
        self.save()
        result = c.collect(self.root)
        kinds = {w['kind'] for w in result['warnings']}
        self.assertTrue({'missing_or_duplicate_expected_run','invalid_selected_run','source_excluded_entries','minimal_pair_invalid'} <= kinds)
        self.assertEqual(result['status'],'review_warnings')
        rendered = c.render(result)['summary.txt']
        self.assertIn('bad CSV',rendered)
        self.assertIn('drain_incomplete',rendered)
        self.assertIn('collector_duplicate_case_seed_or_id',rendered)
        self.assertTrue(any(not p['valid'] for p in result['minimal_pairs']))

    def test_per_seed_edges_and_constant_na_are_preserved(self):
        result = c.collect(self.root)
        self.assertEqual(len(result['buckets']),192)
        group = c.GROUPS[0]
        rows = [r for r in result['buckets'] if r['group']==group and r['metric']==c.METRICS[0] and r['bucket']==1]
        self.assertEqual({r['lower_inclusive'] for r in rows},{1,1.01,1.02})
        self.assertTrue(all(r['W_p99_ms']==4 and r['X_p99_ms']==4 for r in rows))
        constant = [r for r in result['correlations'] if r['metric']==c.METRICS[0]]
        self.assertTrue(all(r['pearson'] is None and r['status']=='constant' for r in constant))
        files = c.render(result)
        self.assertIn('constant,NA,NA',files['quality.txt'])
        self.assertIn('13.123456789',files['quality.txt'])
        self.assertIn('quartiles_planned_mean_ms',files['summary.txt'])
        self.assertIn('[123]',files['summary.txt'])

    def test_missing_prediction_model_is_na_and_warned(self):
        self.reports['observe']['prediction'].pop(2)
        self.save()
        result = c.collect(self.root)
        self.assertIn('missing_or_duplicate_prediction_model',{w['kind'] for w in result['warnings']})
        fold = next(f for f in result['prediction_folds'] if f['models']['metric'] is None)
        self.assertEqual(fold['metric'],c.METRICS[0])
        self.assertIn('NA', c.render(result)['quality.txt'])

    def test_no_mean_of_different_bucket_p99_and_run_p99_label(self):
        result = c.collect(self.root)
        self.assertEqual(len(result['prediction_folds']),48)
        self.assertEqual(len(result['correlations']),48)
        self.assertIn('p99_run_mean',c.render(result)['summary.txt'])
        self.assertTrue(all('held_seed' in r for r in result['buckets']))
        self.assertTrue(all(r['aggregation']=='equal_run_weight_not_pooled' for r in result['case_aggregates']))

    def test_read_only_sources_hashes_and_no_overwrite(self):
        before = {phase:hashlib.sha256((self.root/relative).read_bytes()).hexdigest() for phase,relative in c.PHASE_PATHS.items()}
        out = self.root/'collection'
        with contextlib.redirect_stdout(io.StringIO()):
            result = c.write_collection(self.root,out)
        self.assertEqual(set(p.name for p in out.iterdir()),{'summary.txt','quality.txt','buckets.txt','collection.json'})
        for phase,relative in c.PHASE_PATHS.items():
            self.assertEqual(before[phase],hashlib.sha256((self.root/relative).read_bytes()).hexdigest())
            self.assertEqual(before[phase],result['provenance'][phase]['sha256'])
        with self.assertRaisesRegex(ValueError,'already exists'):
            c.write_collection(self.root,out)

    def test_missing_input_aborts_before_output_creation(self):
        (self.root/c.PHASE_PATHS['minimal']).unlink()
        out = self.root/'collection'
        with self.assertRaises(FileNotFoundError):
            c.write_collection(self.root,out)
        self.assertFalse(out.exists())

    def test_configuration_drift_never_merged(self):
        altered = self.reports['minimal']['runs'][3]
        altered['config_id'] = 'different-resource'
        altered['config']['threads_per_pool'] = 128
        self.save()
        result = c.collect(self.root)
        self.assertIn('selected_config_drift',{w['kind'] for w in result['warnings']})
        bad = next(p for p in result['minimal_pairs'] if p['run']==altered['id'])
        self.assertFalse(bad['valid'])
        self.assertIn('home_config_mismatch',bad['reasons'])

    def test_old_reports_need_no_new_optional_metadata_fields(self):
        for phase in ('observe','tune'):
            self.reports[phase].pop('normalization')
            self.reports[phase].pop('config_variants')
        self.save()
        self.assertEqual(c.collect(self.root)['warnings'], [])

    def test_missing_buckets_and_duplicate_correlations_are_not_hidden(self):
        observed = self.reports['observe']
        observed['buckets'] = [b for b in observed['buckets'] if not (b['group']==c.GROUPS[0] and b['held_seed']==11000 and b['metric']==c.METRICS[0])]
        observed['correlations'][1] = copy.deepcopy(observed['correlations'][0])
        self.save()
        result = c.collect(self.root)
        kinds = {w['kind'] for w in result['warnings']}
        self.assertTrue({'missing_planned_buckets','bucket_mass_mismatch','missing_or_duplicate_correlation'} <= kinds)

    def test_conflicting_bucket_edges_and_invalid_source_comparison_warn(self):
        duplicate = copy.deepcopy(self.reports['observe']['buckets'][2])
        duplicate['upper_exclusive'] = 1.5
        self.reports['observe']['buckets'].append(duplicate)
        invalid = self.reports['evaluate']['runs'][0]
        invalid['valid'] = False
        self.save()
        result = c.collect(self.root)
        kinds = {w['kind'] for w in result['warnings']}
        self.assertTrue({'conflicting_bucket_bounds','source_comparison_collector_validity_discrepancy'} <= kinds)
        self.assertIn('source_invalid_without_reason', result['runs']['evaluate'][0]['collection_invalid_reasons'])

    def test_invalid_observe_member_cannot_enter_training_folds(self):
        self.reports['observe']['runs'][0]['valid'] = False
        self.reports['observe']['runs'][0]['invalid_reasons'] = ['failed']
        self.save()
        result = c.collect(self.root)
        self.assertIn('quality_group_incomplete_or_invalid_membership',{w['kind'] for w in result['warnings']})
        self.assertEqual(len(result['prediction_folds']),24)
        self.assertEqual(len(result['buckets']),96)


if __name__ == '__main__':
    unittest.main()

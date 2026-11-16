#!/usr/bin/env python3
"""Collect existing stage-3 ARM analysis as bounded terminal evidence; never run work."""
import argparse
from collections import Counter, defaultdict
import copy
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import statistics

PHASE_PATHS = {
    'observe': 'observe-analysis/report.json',
    'tune': 'tune-analysis/report.json',
    'evaluate': 'evaluate-alias-analysis/report.json',
    'minimal': 'minimal-analysis/report.json',
}
GROUPS = ('main-t16-local', 'main-t256-local')
MINIMAL_CASES = tuple('main-t256-local-'+s for s in ('home', 'outstanding-h2', 'spread-policy'))
SEEDS = {'observe': tuple(range(11000, 11003)), 'tune': tuple(range(21000, 21003)),
         'evaluate': tuple(range(31000, 31007)), 'minimal': tuple(range(32000, 32003))}
EXPECTED_RUNS = {'observe': 6, 'tune': 48, 'evaluate': 56, 'minimal': 9}
METRICS = ('queue_per_capacity', 'outstanding_per_capacity', 'work_ms_per_capacity',
           'remaining_ms_per_capacity')
TARGETS = ('wait_ms', 'planned_latency_ms')
STAT_FIELDS = ('cohort_planned', 'cohort_completed', 'cohort_bursts',
    'planned_latency_ms_mean', 'planned_latency_ms_p99', 'latency_ms_mean', 'latency_ms_p99', 'wait_ms_mean', 'wait_ms_p99',
    'callback_ms_mean', 'callback_ms_p99', 'submit_lag_ms_mean', 'submit_lag_ms_p99',
    'process_cpu_per_completed_task_ms', 'spill_fraction', 'route_mean_ns', 'route_p99_ns', 'quartiles_planned_mean_ms', 'quartiles_planned_p99_ms', 'quartiles_submit_lag_mean_ms')
AGG_FIELDS = ('planned_latency_ms_mean', 'planned_latency_ms_p99', 'wait_ms_mean',
              'callback_ms_mean', 'process_cpu_per_completed_task_ms', 'spill_fraction')
MODEL_FIELDS = ('mean_mae_ms', 'p99_pinball_ms', 'p99_coverage', 'n', 'fallback_n')
PAIR_FIELDS = ('planned_latency_ms_mean', 'planned_latency_ms_p99',
               'process_cpu_per_completed_task_ms')
NOTES = [
    'Scope is fixed: main T16/T256 only; minimal T256 home/N>2/spread, seeds 32000..32002. No new experiments.',
    'Q=Folly queue estimate; N=accepted unfinished callbacks; C=configured capacity. Q/C,N/C are counts/capacity; work/C,remaining/C are estimated CPU ms/capacity. None is Linux runq.',
    'Primary outcomes: W=start-submit and planned L=end-planned, milliseconds. X=end-start is callback wall time. Per-task sums hold; quantiles do not add.',
    'Run aggregates are equal-weight means of run means or run p99 values, NEVER pooled p99 or per-second p99 averages. Invalid/duplicate runs are not aggregated.',
    'Pair deltas=candidate-home; negative latency deltas indicate lower latency. Tune/evaluate summaries and CI are copied from the strict analyzer unchanged.',
    'Minimal pairs are recomputed only inside the predetermined nine-run subset; three seeds are descriptive, no CI is generated.',
    'Correlations describe per-task load versus latency, not correlation with a task-level p99. Constant/missing values are NA, not zero.',
    'Prediction folds hold out complete seeds. Models are global, synthetic own-budget oracle, metric, metric+oracle. MAE targets conditional mean, pinball loss targets conditional p99. Coverage is calibration, not a score to maximize.',
    'Every metric retains its own global/oracle baselines because availability can differ. Training bounds are never recomputed or averaged.',
    'Buckets are per held-out seed and original training bounds. Upper/lower NA means unbounded; other NA means unavailable. LOW_TAIL is not precise tail evidence.',
    'Whole-process CPU/completed task spans entire run and drain; it is not worker-only or analysis-window CPU. Spill is cross-home fraction.',
    'Source report hashes and metadata establish provenance; task CSV is not reread, so collection inherits analyzer validity and cannot independently revalidate raw tasks.',
]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def mean(values):
    return statistics.fmean(values) if values else None


def finite_values(values):
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]


def selected(phase, run):
    if phase == 'minimal':
        return run.get('case') in MINIMAL_CASES and run.get('seed') in SEEDS[phase]
    return run.get('group') in GROUPS


def warn(warnings, kind, **details):
    warnings.append({'kind': kind, **details})


def collect(root):
    """Read all inputs first. Return evidence; no filesystem writes occur here."""
    reports, provenance = {}, {}
    for phase, relative in PHASE_PATHS.items():
        path = root/relative
        raw = path.read_bytes()  # Missing files must abort before any output directory exists.
        report = json.loads(raw)
        if not isinstance(report, dict) or not isinstance(report.get('runs'), list):
            raise ValueError(f'{path}: expected an analyzer report with runs')
        reports[phase] = report
        provenance[phase] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
            'script_sha256': report.get('script_sha256'), 'schema': report.get('schema'),
            'manifests': report.get('manifests', []), 'normalization': report.get('normalization'),
            'source_run_count': len(report['runs']), 'excluded_entries': report.get('excluded_entries', []),
            'config_variants': report.get('config_variants', []), 'method': report.get('method')}
    warnings, runs, counts, configurations = [], {}, [], []
    for phase, report in reports.items():
        chosen = [copy.deepcopy(r) for r in report['runs'] if selected(phase, r)]
        runs[phase] = chosen
        keys = Counter((r.get('case'), r.get('seed')) for r in chosen)
        ids = Counter(r.get('id') for r in chosen)
        for run in chosen:
            reasons = list(run.get('invalid_reasons', []))
            if not run.get('valid') and not reasons:
                reasons.append('source_invalid_without_reason')
            if keys[(run.get('case'), run.get('seed'))] > 1 or ids[run.get('id')] > 1:
                reasons.append('collector_duplicate_case_seed_or_id')
            if run.get('seed') not in SEEDS[phase]:
                reasons.append('collector_unexpected_seed')
            run['collection_valid'] = bool(run.get('valid')) and not reasons
            run['collection_invalid_reasons'] = sorted(set(reasons))
            if not run['collection_valid']:
                warn(warnings, 'invalid_selected_run', phase=phase, run=run.get('id'),
                     reasons=run['collection_invalid_reasons'])
        if len(chosen) != EXPECTED_RUNS[phase]:
            warn(warnings, 'selected_count_mismatch', phase=phase, expected=EXPECTED_RUNS[phase], actual=len(chosen))
        cases = set(r.get('case') for r in chosen)
        if phase == 'minimal':
            expected_cases = set(MINIMAL_CASES)
        elif phase == 'observe':
            expected_cases = {g+'-home' for g in GROUPS}
        elif phase == 'evaluate':
            expected_cases = {g+'-'+s for g in GROUPS for s in ('home', 'outstanding-h2', 'queue-h2', 'spread-policy')}
        else:
            expected_cases = cases | {g+'-home' for g in GROUPS}
            for group in GROUPS:
                group_cases = {r['case'] for r in chosen if r.get('group') == group}
                if len(group_cases) != 8:
                    warn(warnings, 'tune_case_count_mismatch', group=group, expected=8, actual=len(group_cases))
        for case in sorted(expected_cases):
            for seed in SEEDS[phase]:
                n = keys[(case, seed)]
                if n != 1:
                    warn(warnings, 'missing_or_duplicate_expected_run', phase=phase, case=case, seed=seed, count=n)
        if cases-expected_cases:
            warn(warnings, 'unexpected_cases', phase=phase, cases=sorted(cases-expected_cases))
        if report.get('excluded_entries'):
            warn(warnings, 'source_excluded_entries', phase=phase, entries=report['excluded_entries'])
        by_config = defaultdict(list)
        for run in chosen:
            by_config[(run.get('group'), run.get('config_id'))].append(run)
        for (group, cid), members in sorted(by_config.items()):
            configs = {canonical(r.get('config')) for r in members}
            if len(configs) != 1:
                warn(warnings, 'config_id_collision', phase=phase, group=group, config_id=cid)
            configurations.append({'phase':phase, 'group':group, 'config_id':cid,
                'seeds': sorted({r['seed'] for r in members}), 'config':members[0].get('config'),
                'source_hostnames':sorted({r.get('metadata', {}).get('hostname') for r in members}, key=str)})
        for group in {r.get('group') for r in chosen}:
            ids_for_group = sorted({r['config_id'] for r in chosen if r.get('group') == group})
            if len(ids_for_group) > 1:
                warn(warnings, 'selected_config_drift', phase=phase, group=group, config_ids=ids_for_group)
        counts.append({'phase':phase, 'source_runs':len(report['runs']), 'selected_runs':len(chosen),
            'expected_selected_runs':EXPECTED_RUNS[phase], 'valid_selected_runs':sum(r['collection_valid'] for r in chosen),
            'source_excluded':len(report.get('excluded_entries', []))})
    aggregates = aggregate_runs(runs)
    comparisons = []
    for phase in ('tune', 'evaluate'):
        source_comparisons = [copy.deepcopy(c) for c in reports[phase].get('comparisons', []) if c.get('group') in GROUPS]
        for row in source_comparisons:
            row['phase'] = phase
            row['evidence_source'] = 'analyzer_unchanged'
            comparisons.append(row)
            inconsistent = [r['id'] for r in runs[phase] if r['group'] == row['group'] and r['config_id'] == row['config_id'] and (r['case'] == row['case'] or r['routing'] == 'home') and not r['collection_valid']]
            if inconsistent:
                warn(warnings, 'source_comparison_collector_validity_discrepancy', phase=phase, case=row['case'], runs=inconsistent)
            if not row.get('all_pairs_valid', False) or row.get('n_invalid', 0):
                warn(warnings, 'source_comparison_incomplete', phase=phase, case=row.get('case'),
                     config_id=row.get('config_id'), status=row.get('status'))
        expected_n = 14 if phase == 'tune' else 6
        if len(source_comparisons) != expected_n:
            warn(warnings, 'source_comparison_count_mismatch', phase=phase, expected=expected_n, actual=len(source_comparisons))
    # Reuse the strict analyzer's pairing logic, without loading CSV or running work.
    spec = importlib.util.spec_from_file_location('balance_collector_analysis', Path(__file__).with_name('analyze_balance.py'))
    analyzer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analyzer)
    minimal = copy.deepcopy(runs['minimal'])
    for run in minimal:
        run['valid'] = run['collection_valid']
        run['invalid_reasons'] = run['collection_invalid_reasons']
    minimal_pairs, minimal_comparisons = analyzer.compare_runs(minimal)
    for row in minimal_comparisons:
        row['phase'] = 'minimal'
        row['evidence_source'] = 'strict_recompute_predetermined_subset'
        comparisons.append(row)
    for pair in minimal_pairs:
        if not pair['valid']:
            warn(warnings, 'minimal_pair_invalid', case=pair['case'], seed=pair['seed'], reasons=pair['reasons'])
    if len(minimal_pairs) != 6:
        warn(warnings, 'minimal_pair_count_mismatch', expected=6, actual=len(minimal_pairs))
    quality_groups, correlations, predictions, buckets = collect_quality(reports['observe'], runs['observe'], warnings)
    return {'schema':1, 'collector_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'selection':{'groups':GROUPS, 'minimal_cases':MINIMAL_CASES, 'seeds':SEEDS,
                     'primary_targets':TARGETS, 'metrics':METRICS}, 'notes':NOTES,
        'provenance':provenance, 'counts':counts, 'warnings':warnings,
        'status':'complete_selected_evidence' if not warnings else 'review_warnings',
        'configurations':configurations, 'runs':runs, 'case_aggregates':aggregates,
        'comparisons':comparisons, 'minimal_pairs':minimal_pairs,
        'quality_groups':quality_groups, 'correlations':correlations,
        'prediction_folds':predictions, 'buckets':buckets}


def aggregate_runs(runs):
    result = []
    for phase, members in runs.items():
        groups = defaultdict(list)
        for run in members:
            groups[(run['case'], run['config_id'])].append(run)
        for (case, cid), group in sorted(groups.items()):
            good = [r for r in group if r['collection_valid']]
            row = {'phase':phase, 'case':case, 'config_id':cid, 'valid_runs':len(good),
                'invalid_runs':len(group)-len(good), 'seeds':[r['seed'] for r in good],
                'aggregation':'equal_run_weight_not_pooled'}
            for field in AGG_FIELDS:
                values = finite_values([r['statistics'].get(field) for r in good])
                for suffix, value in (('mean',mean(values)), ('min',min(values) if values else None),
                                      ('max',max(values) if values else None), ('n',len(values))):
                    row[field+'_run_'+suffix] = value
            result.append(row)
    return result


def collect_quality(report, selected_runs, warnings):
    good = [r for r in selected_runs if r['collection_valid'] and r.get('routing') == 'home']
    ids = {r['id'] for r in good}
    keys = {(r['group'], r['config_id']) for r in good}
    quality_groups = [copy.deepcopy(x) for x in report.get('quality_groups', []) if (x.get('group'), x.get('config_id')) in keys]
    # A fold may have used an invalid/duplicate run in training: keep only groups
    # whose entire analyzer source membership belongs to the accepted observations.
    accepted = set()
    for group in quality_groups:
        key = (group['group'], group['config_id'])
        if set(group.get('run_ids', [])) <= ids and set(group.get('seeds', [])) == set(SEEDS['observe']):
            accepted.add(key)
        else:
            warn(warnings, 'quality_group_incomplete_or_invalid_membership', group=group['group'], config_id=group['config_id'])
    correlations = [copy.deepcopy(x) for x in report.get('correlations', []) if x.get('run') in ids
                    and x.get('metric') in METRICS and x.get('target') in TARGETS]
    correlation_counts = Counter((r['run'], r['metric'], r['target']) for r in correlations)
    for run in good:
        for metric in METRICS:
            for target in TARGETS:
                count = correlation_counts[(run['id'], metric, target)]
                if count != 1:
                    warn(warnings, 'missing_or_duplicate_correlation', run=run['id'], metric=metric, target=target, count=count)
    prediction = [copy.deepcopy(x) for x in report.get('prediction', [])
        if (x.get('group'), x.get('config_id')) in accepted and x.get('metric') in METRICS
        and x.get('target') in TARGETS and x.get('held_seed') in SEEDS['observe']]
    by_prediction = defaultdict(list)
    for row in prediction:
        by_prediction[(row['group'], row['config_id'], row['held_seed'], row['metric'], row['target'])].append(row)
    folds = []
    for (group, cid, seed, metric, target), members in sorted(by_prediction.items()):
        row = {'group':group, 'config_id':cid, 'held_seed':seed, 'metric':metric, 'target':target, 'models':{}}
        models = (('global','global'), ('oracle','own_budget_oracle'), ('metric',metric), ('metric_oracle',metric+'+own_budget_oracle'))
        for label, model in models:
            found = [m for m in members if m['model'] == model]
            if len(found) != 1:
                warn(warnings, 'missing_or_duplicate_prediction_model', group=group, config_id=cid,
                     seed=seed, metric=metric, target=target, model=model, count=len(found))
                row['models'][label] = None
            else:
                row['models'][label] = found[0]
        folds.append(row)
    buckets_source = [copy.deepcopy(x) for x in report.get('buckets', [])
        if (x.get('group'), x.get('config_id')) in accepted and x.get('metric') in METRICS
        and x.get('held_seed') in SEEDS['observe']]
    by_bucket = defaultdict(list)
    for row in buckets_source:
        key = tuple(row.get(k) for k in ('group', 'config_id', 'held_seed', 'metric', 'bucket', 'lower_inclusive', 'upper_exclusive'))
        by_bucket[key].append(row)
    buckets = []
    for key, members in sorted(by_bucket.items(), key=lambda x:repr(x[0])):
        planned = [m for m in members if m['target'] == 'planned_latency_ms']
        if not planned:
            continue
        if len(planned) != 1:
            warn(warnings, 'duplicate_bucket', key=key)
            continue
        row = planned[0]
        for target, label in (('wait_ms','W'), ('callback_ms','X')):
            matching = [m for m in members if m['target'] == target]
            row[label+'_mean_ms'] = matching[0].get('mean_ms') if len(matching) == 1 else None
            row[label+'_p99_ms'] = matching[0].get('p99_ms') if len(matching) == 1 else None
        buckets.append(row)
    bucket_indices = defaultdict(set)
    bucket_cells = defaultdict(list)
    for bucket in buckets:
        key = tuple(bucket[k] for k in ('group', 'config_id', 'held_seed', 'metric'))
        bucket_cells[key].append(bucket)
        bucket_indices[key+(bucket['bucket'],)].add((bucket['lower_inclusive'], bucket['upper_exclusive']))
    for key, bounds in bucket_indices.items():
        if len(bounds) != 1:
            warn(warnings, 'conflicting_bucket_bounds', key=key, bounds=sorted(bounds, key=repr))
    folded = {(r['group'], r['config_id'], r['held_seed'], r['metric']):r for r in folds if r['target'] == 'planned_latency_ms'}
    for group, cid in sorted(accepted):
        for seed in SEEDS['observe']:
            for metric in METRICS:
                key = (group, cid, seed, metric)
                cells = bucket_cells.get(key, [])
                if not cells:
                    warn(warnings, 'missing_planned_buckets', group=group, config_id=cid, seed=seed, metric=metric)
                reference = (folded.get(key, {}).get('models', {}).get('global') or {})
                expected_n = reference.get('test_available_n')
                actual_n = sum(b.get('n', 0) for b in cells)
                if expected_n is not None and actual_n != expected_n:
                    warn(warnings, 'bucket_mass_mismatch', group=group, config_id=cid, seed=seed, metric=metric,
                         expected=expected_n, actual=actual_n)
    expected_corr = 2*3*4*2
    if len(correlations) != expected_corr:
        warn(warnings, 'correlation_count_mismatch', expected=expected_corr, actual=len(correlations))
    if len(folds) != expected_corr:
        warn(warnings, 'prediction_fold_count_mismatch', expected=expected_corr, actual=len(folds))
    return quality_groups, correlations, folds, buckets


def text_value(value):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return 'NA'
    if isinstance(value, (dict, list, tuple)):
        return canonical(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return format(value, '.12g')
    return str(value)


def table(title, rows, fields):
    out = io.StringIO()
    out.write('\n# '+title+'\n')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(fields)
    for row in rows:
        writer.writerow([text_value(row.get(k)) for k in fields])
    return out.getvalue()


def render(evidence):
    summary = '# Stage-3 bounded evidence collection\n# STATUS '+evidence['status']+'\n'
    summary += ''.join('# '+n+'\n' for n in NOTES)
    for phase, source in evidence['provenance'].items():
        summary += '# SOURCE '+phase+' '+canonical({k:source.get(k) for k in ('path','sha256','script_sha256','normalization','manifests')})+'\n'
    summary += table('COUNTS', evidence['counts'], ('phase','source_runs','selected_runs','expected_selected_runs','valid_selected_runs','source_excluded'))
    summary += table('WARNINGS (all must remain visible)', [{'kind':w['kind'], 'details':w} for w in evidence['warnings']], ('kind','details'))
    summary += table('CONFIGURATIONS (window seconds; cpu_us and estimate_us are microseconds)', evidence['configurations'],
                     ('phase','group','config_id','seeds','source_hostnames','config'))
    raw = []
    for phase in ('observe','minimal'):
        for run in evidence['runs'][phase]:
            raw.append({'phase':phase, **{k:run.get(k) for k in ('case','seed','config_id','collection_valid','collection_invalid_reasons')},
                        **run['statistics'], **{'quartiles_'+field:[q.get(field) for q in run.get('planned_time_quartiles', [])] for field in ('planned_mean_ms','planned_p99_ms','submit_lag_mean_ms')}})
    summary += table('OBSERVE AND MINIMAL RAW RUN STATISTICS', raw,
        ('phase','case','seed','config_id','collection_valid','collection_invalid_reasons')+STAT_FIELDS)
    aggregate_fields = ('phase','case','config_id','valid_runs','invalid_runs','seeds')
    aggregate_fields += tuple(f+'_run_'+s for f in AGG_FIELDS for s in ('mean','min','max','n'))
    summary += table('CASE AGGREGATES (p99_run_mean is average of run p99, never pooled p99)', evidence['case_aggregates'], aggregate_fields)
    comp_fields = ('phase','case','config_id','seeds','n_pairs','n_invalid','all_pairs_valid','status','confirmation','spill_fraction','evidence_source')
    comp_fields += tuple(f+s for f in PAIR_FIELDS for s in ('_delta','_delta_ci95'))
    comp_fields += ('planned_latency_ms_mean_improvement_percent','planned_latency_ms_p99_improvement_percent')
    summary += table('STRICT PAIRED AGGREGATES (source CI untouched; minimal no CI)', evidence['comparisons'], comp_fields)
    raw_pairs = []
    for pair in evidence['minimal_pairs']:
        raw_pairs.append({**pair, **{field+'_delta': pair['deltas'].get(field) for field in PAIR_FIELDS}})
    summary += table('MINIMAL SIX RAW PAIRED DELTAS', raw_pairs,
        ('case','config_id','seed','run','baseline_run','valid','reasons')+tuple(f+'_delta' for f in PAIR_FIELDS))
    quality = '# Metric quality: per-task association; seed-held-out prediction; no winner is selected.\n'
    quality += table('QUALITY GROUPS AND REDUNDANT SIGNALS', evidence['quality_groups'],
        ('group','config_id','seeds','run_ids','status','redundant_signals'))
    quality += table('CORRELATIONS (constant/missing = NA)', evidence['correlations'],
        ('group','config_id','run','seed','metric','target','n','coverage','distinct_values','status','pearson','spearman'))
    prediction_rows = []
    for fold in evidence['prediction_folds']:
        row = {k:v for k,v in fold.items() if k != 'models'}
        reference = fold['models'].get('global') or {}
        for key in ('training_seeds','training_n','test_available_n','test_total_n','test_unique_bursts','tail_quality'):
            row[key] = reference.get(key)
        for label in ('global','oracle','metric','metric_oracle'):
            source = fold['models'].get(label) or {}
            for key in MODEL_FIELDS:
                row[label+'_'+key] = source.get(key)
        prediction_rows.append(row)
    quality += table('PREDICTION FOLDS (oracle uses known synthetic CPU budget; pinball and coverage jointly)', prediction_rows,
        ('group','config_id','held_seed','metric','target','training_seeds','training_n','test_available_n','test_total_n','test_unique_bursts','tail_quality')+
        tuple(label+'_'+field for label in ('global','oracle','metric','metric_oracle') for field in MODEL_FIELDS))
    buckets = '# Planned L conditional buckets; preserve each held-seed training interval; do not average bucket p99.\n'
    buckets += '# Bounds use metric units: Q/C,N/C counts/capacity; work/C,remaining/C estimated CPU ms/capacity. Latencies milliseconds.\n'
    buckets += table('HELD-OUT BUCKETS (NA bounds unbounded; W/X quantiles cannot be added)', evidence['buckets'],
        ('group','config_id','held_seed','metric','bucket','lower_inclusive','upper_exclusive','boundary_source','n','unique_bursts','mean_ms','p99_ms','tail_quality','W_mean_ms','W_p99_ms','X_mean_ms','X_p99_ms'))
    return {'summary.txt':summary, 'quality.txt':quality, 'buckets.txt':buckets,
            'collection.json':json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False)+'\n'}


def write_collection(root, output):
    if output.exists():
        raise ValueError(f'output already exists; choose a new directory: {output}')
    evidence = collect(root)
    files = render(evidence)
    output.mkdir(parents=True, exist_ok=False)
    for name, contents in files.items():
        (output/name).write_text(contents, encoding='utf-8')
    print('collection_status='+evidence['status']+' warnings='+str(len(evidence['warnings'])))
    for row in evidence['counts']:
        print(f"{row['phase']}: selected={row['selected_runs']} valid={row['valid_selected_runs']} expected={row['expected_selected_runs']}")
    for name, contents in files.items():
        print(str(output/name)+' lines='+str(len(contents.splitlines())))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        write_collection(args.root, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, 'collection error: '+str(exc)+'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

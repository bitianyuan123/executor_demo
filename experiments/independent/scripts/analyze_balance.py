#!/usr/bin/env python3
"""Stage 3 load metrics and threshold routing: whole-seed validation, stdlib only.

No task is randomly split. No completed-only latency is advertised as a gain when
admission, failure or drain censoring differs. All percentiles use nearest rank.
"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

TARGETS = ('wait_ms', 'callback_ms', 'latency_ms', 'planned_latency_ms')
METRICS = ('queue', 'queue_per_capacity', 'outstanding_per_capacity', 'work_ms_per_capacity',
           'remaining_ms_per_capacity')
# A constant capacity is a normalization convention, not a measured CPU rate.
CONFIG_FIELDS = (
    'deployment', 'hostname', 'kernel', 'machine', 'executor', 'queue', 'affinity',
    'pools', 'threads_per_pool', 'allowed_cpus', 'effective_masks', 'numa_labels',
    'capacities', 'instrumentation', 'effective_load_observation', 'progress_us',
    'maintain_outstanding', 'maintain_work', 'publish_remaining',
    'sample_ms', 'max_samples', 'arrival', 'arrival_rate', 'duration_seconds',
    'arrival_limit', 'burst_min', 'burst_max', 'burst_placement', 'mean_normalization',
    'cpu_us', 'jitter', 'long_prob', 'long_factor', 'estimate_us', 'estimate_jitter',
    'work_mode', 'iterations_per_us', 'max_outstanding', 'max_planned_tasks',
    'drain_seconds', 'fail_task', 'fail_submit_task',
)
STATES = ('completed', 'rejected', 'deadline_rejected', 'canceled', 'failed')
NOTES = [
    'Only routing=home observations enter metric-quality comparisons; selected pools after routing are selection-biased.',
    'Queue is Folly telemetry, not Linux runq. Outstanding includes callbacks already started but not completed.',
    'Capacity normalizes by configured min(threads, allowed logical CPUs) or an explicit override; it is not measured CPU throughput. Overlapping masks do not provide independent capacities.',
    'Work retains estimated full budgets until completion. Remaining subtracts periodically published progress; neither is exact future service demand. Synthetic exact estimates are oracle information.',
    'W=start-submit; X=end-start; L=end-submit; planned=end-planned. Submit precedes routing so W/L include routing. End precedes callback cleanup, so X excludes counter-cleanup tail. Their per-task sums hold, but quantiles do not add.',
    'A cohort is selected by planned arrival in [start,end); slow completions after end remain included. Rejections/cancellations/failures remain in the denominators.',
    'Inspect planned-time quartiles and submission lag for growing overload before treating a correlation as steady behavior.',
    'Correlation is per-task observational association, not a task-level p99, linear law or causal proof. Constant/missing metrics produce NA.',
    'Buckets and own-budget boundaries are fitted on training seeds only. Test units are entire unseen seeds; all tasks of a burst remain together.',
    'Prediction reports mean-predictor MAE plus conditional-p99 pinball loss and coverage. Predicted conditional p99 is not a whole-run p99. The own-budget baseline uses known synthetic CPU budgets and is labelled oracle.',
    'LOW_TAIL means fewer than 1000 tasks or 100 distinct bursts; p99 is still printed descriptively, not treated as a precise tail estimate.',
    'Pairing checks full planned task/burst/budget/estimate sequences plus resources and instrumentation. Invalid runs never contribute to a claimed gain.',
    'Benefits use per-run means and per-run p99 differences, never an average of per-second p99 or a pooled task bootstrap. At least 7 independent seeds are needed for a paired seed bootstrap CI.',
    'Constant estimates make work proportional to outstanding; this is a rescaled signal, not an independent discovery. Batch ordinal/context is synthetic workload information, not a production pool-load counter.',
    'Threshold curves expose all candidates. No automatic winner is declared. Tune/observe and evaluate seeds must be disjoint to avoid selection leakage.',
    'Process CPU/completed task includes process work during the entire run and drain; it is not worker-only CPU or the analysis-window CPU.',
]


def avg(values):
    return statistics.fmean(values) if values else None


def quantile(values, p=.99):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p*len(ordered))-1)] if ordered else None


def pearson(xs, ys):
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx, my = avg(xs), avg(ys)
    vx = sum((x-mx)**2 for x in xs)
    vy = sum((y-my)**2 for y in ys)
    return sum((x-mx)*(y-my) for x, y in zip(xs, ys))/math.sqrt(vx*vy) if vx and vy else None


def ranks(values):
    result = [0.]*len(values)
    order = sorted(range(len(values)), key=values.__getitem__)
    i = 0
    while i < len(order):
        j = i+1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        for k in order[i:j]:
            result[k] = (i+j-1)/2+1
        i = j
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def get(metadata, entry, key, default=None):
    if key in metadata:
        return metadata[key]
    options = entry.get('options', {})
    return options.get(key, options.get(key.replace('_', '-'), default))


def numeric(value):
    try:
        return int(value)
    except ValueError:
        v = float(value)
        if not math.isfinite(v):
            raise ValueError('nonfinite CSV value')
        return v


def resolve_run(manifest, value):
    path = Path(value)
    candidates = [path] if path.is_absolute() else [manifest.parent/path, manifest.parent/path.name, Path.cwd()/path]
    for candidate in candidates:
        if (candidate/'metadata.json').is_file():
            return candidate.resolve()
    raise ValueError(f'cannot resolve run directory: {value}')


def load_run(path, entry, manifest, window_start=0., window_end=None):
    path = Path(path)
    meta = json.loads((path/'metadata.json').read_text())
    summary = json.loads((path/'summary.json').read_text())
    if meta.get('stage') != 3:
        raise ValueError(f'not stage 3: {path}')
    duration = float(meta['duration_seconds'])
    end = duration if window_end is None else float(window_end)
    if not 0 <= window_start < end <= duration:
        raise ValueError(f'analysis cohort outside run duration: {path}')
    with (path/'tasks.csv').open(newline='') as handle:
        raw = [{k: (v if k == 'status' else numeric(v)) for k, v in row.items()} for row in csv.DictReader(handle)]
    if len(raw) != meta.get('planned_tasks') or len(raw) != summary.get('planned_tasks'):
        raise ValueError(f'truncated task CSV: {path}')
    if len({r['task'] for r in raw}) != len(raw):
        raise ValueError(f'duplicate task IDs: {path}')
    counts = Counter(r['status'] for r in raw)
    if set(counts)-set(STATES):
        raise ValueError(f'unknown task status: {path}')
    for state in ('completed', 'canceled', 'failed'):
        if counts[state] != summary.get(state+'_tasks', 0):
            raise ValueError(f'task counts disagree for {state}: {path}')
    if counts['rejected']+counts['deadline_rejected'] != summary.get('rejected_tasks', 0):
        raise ValueError(f'rejected task counts disagree: {path}')
    if entry.get('seed') is not None and int(entry['seed']) != int(meta['seed']):
        raise ValueError(f'manifest/metadata seed mismatch: {path}')
    observation = get(meta, entry, 'effective_load_observation', get(meta, entry, 'observations', 'off'))
    config = {k: get(meta, entry, k) for k in CONFIG_FIELDS}
    config['effective_load_observation'] = observation
    config['minimal_load_metric'] = get(meta, entry, 'load_metric') if observation == 'minimal' else None
    config['binary_sha256'] = manifest.get('binary_sha256')
    config['window'] = [window_start, end]
    capacities = meta.get('capacities', [])
    if capacities and (len(capacities) != meta.get('pools') or
            any(not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0 for c in capacities)):
        raise ValueError(f'invalid metadata capacities: {path}')
    for row in raw:
        if row['planned_ns'] < 0 or row['planned_ns'] >= duration*1e9:
            raise ValueError(f'planned task outside offered duration: {path}')
        if row['status'] == 'completed' and not row['planned_ns'] <= row['submit_ns'] <= row['start_ns'] <= row['end_ns']:
            raise ValueError(f'invalid task clock order: {path}')
        home, selected_pool = int(row.get('home_pool', row['pool'])), int(row['pool'])
        if not 0 <= home < meta['pools'] or not 0 <= selected_pool < meta['pools']:
            raise ValueError(f'invalid home or selected pool: {path}')
        if capacities and 'capacity' in row and not math.isclose(row['capacity'], capacities[selected_pool], rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f'task selected capacity disagrees with metadata: {path}')
    selected = [r for r in raw if window_start*1e9 <= r['planned_ns'] < end*1e9]
    rows = []
    for r in selected:
        if r['status'] != 'completed':
            continue
        r.update(wait_ms=(r['start_ns']-r['submit_ns'])/1e6,
                 callback_ms=(r['end_ns']-r['start_ns'])/1e6,
                 latency_ms=(r['end_ns']-r['submit_ns'])/1e6,
                 planned_latency_ms=(r['end_ns']-r['planned_ns'])/1e6,
                 submit_lag_ms=(r['submit_ns']-r['planned_ns'])/1e6,
                 own_budget_ms=r['target_cpu_us']/1000)
        home = int(r.get('home_pool', r['pool']))
        capacity = capacities[home] if home < len(capacities) else r.get('capacity', -1)
        r['queue'] = r.get('home_native_queue', r.get('native_queue', -1))
        r['queue_per_capacity'] = r['queue']/capacity if r['queue'] >= 0 and capacity > 0 else -1
        for metric, field, scale in (
                ('outstanding_per_capacity', 'home_outstanding', 1),
                ('work_ms_per_capacity', 'home_work_estimate_us', 1000),
                ('remaining_ms_per_capacity', 'home_remaining_work_us', 1000)):
            value = r.get(field, -1)
            r[metric] = value/capacity/scale if value >= 0 and capacity > 0 else -1
        r['burst_context_remaining'] = r.get('burst_remaining', -1)
        rows.append(r)
    reasons = []
    split = entry.get('split', manifest.get('split', 'unspecified'))
    selection = manifest.get('selection') or manifest.get('frozen_selection') or manifest.get('spec', {}).get('selection')
    if split == 'evaluate' and (not isinstance(selection, dict) or selection.get('source_split') != 'tune' or not selection.get('source_seeds')):
        reasons.append('missing_frozen_tune_selection')
    if split == 'evaluate' and isinstance(selection, dict) and get(meta, entry, 'routing', 'home') == 'threshold' and 'metric' in selection:
        allowed_policies = [(selection.get('metric'), selection.get('threshold'))]
        if selection.get('queue_threshold') is not None:
            allowed_policies.append(('queue', selection['queue_threshold']))
        actual_policy = (get(meta, entry, 'load_metric'), get(meta, entry, 'load_threshold'))
        if actual_policy not in allowed_policies:
            reasons.append('frozen_policy_mismatch')
    if entry.get('returncode') != 0:
        reasons.append('nonzero_or_missing_returncode')
    if not summary.get('conservation_ok'):
        reasons.append('conservation_failure')
    for state in ('rejected', 'deadline_rejected', 'canceled', 'failed'):
        if counts[state]:
            reasons.append(state)
    if not rows:
        reasons.append('empty_cohort')
    if summary.get('sampling_failed') or summary.get('sampling_truncated'):
        reasons.append('sampling_incomplete')
    if summary.get('counters_balanced') is False or summary.get('final_outstanding', 0) != 0:
        reasons.append('unbalanced_final_counters')
    if summary.get('drain_complete') is False:
        reasons.append('drain_incomplete')
    seq = sorted(raw, key=lambda r: r['task'])
    sequence_hash = digest([[r[k] for k in ('task', 'burst', 'planned_ns', 'target_cpu_us', 'estimate_us')] for r in seq])
    cohort_counts = Counter(r['status'] for r in selected)
    # Derive burst completion from task records, including slow finishes outside the window.
    bursts = defaultdict(list)
    for r in selected:
        bursts[r['burst']].append(r)
    burst_latency = []
    for burst in bursts.values():
        if len({r['planned_ns'] for r in burst}) != 1:
            raise ValueError('one burst contains different planned arrival times')
        if all(r['status'] == 'completed' for r in burst):
            burst_latency.append((max(r['end_ns'] for r in burst)-burst[0]['planned_ns'])/1e6)
    observed_seconds = float(summary.get('observed_seconds', 0))
    completed = counts['completed']
    cpu_keys = ('process_user_cpu_seconds', 'process_system_cpu_seconds')
    cpu_seconds = sum(float(summary[k]) for k in cpu_keys) if all(k in summary for k in cpu_keys) else None
    stats = {'cohort_planned':len(selected), 'cohort_completed':len(rows),
             'cohort_rejected':cohort_counts['rejected']+cohort_counts['deadline_rejected'],
             'cohort_canceled':cohort_counts['canceled'], 'cohort_failed':cohort_counts['failed'],
             'cohort_bursts':len(bursts), 'cohort_complete_bursts':len(burst_latency),
             'burst_planned_mean_ms':avg(burst_latency), 'burst_planned_p99_ms':quantile(burst_latency),
             'whole_completed':completed, 'whole_planned':len(raw),
             'whole_rejected':counts['rejected']+counts['deadline_rejected'],
             'whole_canceled':counts['canceled'], 'whole_failed':counts['failed'],
             'process_cpu_per_completed_task_ms':cpu_seconds*1000/completed if completed and cpu_seconds is not None else None,
             'whole_completed_per_observed_second':completed/observed_seconds if observed_seconds else None,
             'whole_observed_seconds':observed_seconds,
             'drain_after_offer_seconds':max(0., observed_seconds-duration),
             'spill_fraction':avg([r['spilled'] for r in selected if r.get('spilled', -1) >= 0]),
             'route_mean_ns':avg([r['route_ns'] for r in selected if r.get('route_ns', -1) >= 0]),
             'route_p99_ns':quantile([r['route_ns'] for r in selected if r.get('route_ns', -1) >= 0]),
             'route_candidates_mean':avg([r['route_candidates'] for r in selected if r.get('route_candidates', -1) >= 0]),
             'mean_target_cpu_ms':avg([r['target_cpu_us']/1000 for r in selected]),
             'analysis_end_seconds':end, 'analysis_start_seconds':window_start}
    for target in TARGETS+('submit_lag_ms',):
        values = [r[target] for r in rows]
        stats[target+'_mean'] = avg(values)
        stats[target+'_p50'] = quantile(values, .50)
        stats[target+'_p99'] = quantile(values)
    trends = []
    for quarter in range(4):
        band = [r for r in rows if window_start+(end-window_start)*quarter/4 <= r['planned_ns']/1e9 < window_start+(end-window_start)*(quarter+1)/4]
        trends.append({'quarter':quarter+1, 'n':len(band), 'planned_mean_ms':avg([r['planned_latency_ms'] for r in band]),
            'planned_p99_ms':quantile([r['planned_latency_ms'] for r in band]), 'submit_lag_mean_ms':avg([r['submit_lag_ms'] for r in band]),
            'wait_mean_ms':avg([r['wait_ms'] for r in band]), 'callback_mean_ms':avg([r['callback_ms'] for r in band]),
            'outstanding_per_capacity_mean':avg([r['outstanding_per_capacity'] for r in band if r['outstanding_per_capacity'] >= 0])})
    return {'id':entry.get('run_id', path.name), 'path':str(path), 'case':entry.get('case', path.name),
            'group':entry.get('comparison_group', 'default'), 'split':entry.get('split', manifest.get('split', 'unspecified')),
            'seed':int(meta['seed']), 'routing':get(meta, entry, 'routing', 'home'),
            'evaluation_scope':entry.get('evaluation_scope'),
            'metric':get(meta, entry, 'load_metric', 'queue'), 'threshold':get(meta, entry, 'load_threshold'),
            'min_improvement':get(meta, entry, 'min_improvement'), 'observation':observation,
            'config':config, 'config_id':digest(config)[:12], 'sequence_sha256':sequence_hash,
            'valid':not reasons, 'invalid_reasons':reasons, 'statistics':stats,
            'metadata':meta, 'summary':summary, 'planned_time_quartiles':trends, 'rows':rows}


def read_manifests(paths, start=None, end=None):
    runs, excluded, manifests, seen = [], [], [], set()
    for source in paths:
        source = Path(source).resolve()
        manifest = json.loads(source.read_text())
        manifests.append({'path':str(source), 'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                          'split':manifest.get('split'), 'selection':manifest.get('selection') or manifest.get('frozen_selection') or manifest.get('spec', {}).get('selection')})
        window = manifest.get('analysis', {})
        begin = float(window.get('warmup_seconds', 0)) if start is None else start
        finish = window.get('steady_end_seconds') if end is None else end
        for entry in manifest.get('runs', []):
            try:
                path = resolve_run(source, entry['path'])
                if str(path) in seen:
                    raise ValueError('duplicate run path')
                seen.add(str(path))
                runs.append(load_run(path, entry, manifest, begin, finish))
            except (ValueError, KeyError, OSError, TypeError) as exc:
                excluded.append({'manifest':str(source), 'entry':entry.get('path'), 'case':entry.get('case'),
                                 'group':entry.get('comparison_group', 'default'),
                                 'split':entry.get('split', manifest.get('split', 'unspecified')),
                                 'seed':entry.get('seed'), 'reason':str(exc)})
    return runs, excluded, manifests


def available(row, features):
    return all(isinstance(row.get(k), (int, float)) and math.isfinite(row[k]) and row[k] >= 0 for k in features)


def edges(values, bins=8):
    """Quantile boundaries fitted solely on training data; tied values stay together."""
    unique = sorted(set(values))
    if len(unique) < 2:
        return []
    candidates = sorted(set(quantile(values, i/bins) for i in range(1, bins)))
    return [v for v in candidates if unique[0] < v <= unique[-1]]


def bucket_key(row, features, boundaries):
    return tuple(bisect_right(boundaries[k], row[k]) for k in features)


def fit_buckets(rows, features, target, bins=8):
    boundaries = {k:edges([r[k] for r in rows], bins if len(features) == 1 else 4) for k in features}
    cells = defaultdict(list)
    for r in rows:
        cells[bucket_key(r, features, boundaries)].append(r[target])
    estimates = {cell:(avg(values), quantile(values)) for cell, values in cells.items()}
    all_values = [r[target] for r in rows]
    return {'features':features, 'boundaries':boundaries, 'cells':estimates,
            'fallback':(avg(all_values), quantile(all_values))}


def predict(model, rows):
    predictions, fallbacks = [], 0
    for row in rows:
        key = bucket_key(row, model['features'], model['boundaries'])
        fallbacks += key not in model['cells']
        predictions.append(model['cells'].get(key, model['fallback']))
    return predictions, fallbacks


def prediction_error(actual, predicted):
    n = len(actual)
    if not n:
        return {'n':0, 'mean_mae_ms':None, 'p99_pinball_ms':None, 'p99_coverage':None}
    residuals = [y-p[1] for y, p in zip(actual, predicted)]
    return {'n':n, 'mean_mae_ms':avg([abs(y-p[0]) for y, p in zip(actual, predicted)]),
            'p99_pinball_ms':avg([(.99 if e >= 0 else -.01)*e for e in residuals]),
            'p99_coverage':avg([int(y <= p[1]) for y, p in zip(actual, predicted)]),
            'actual_mean_ms':avg(actual), 'mean_prediction_ms':avg([p[0] for p in predicted]),
            'actual_p99_ms':quantile(actual)}


def describe(values, bursts):
    return {'n':len(values), 'unique_bursts':len(set(bursts)), 'mean_ms':avg(values),
            'p50_ms':quantile(values, .50), 'p95_ms':quantile(values, .95), 'p99_ms':quantile(values),
            'tail_quality':'LOW_TAIL' if len(values) < 1000 or len(set(bursts)) < 100 else 'DESCRIPTIVE'}


def metric_analysis(runs, bins=8):
    correlations, buckets, predictions, groups_out = [], [], [], []
    grouped = defaultdict(list)
    for run in runs:
        if run['routing'] == 'home' and run['observation'] != 'off' and run['valid'] and run['split'] in ('observe', 'unspecified'):
            grouped[(run['group'], run['config_id'])].append(run)
    for (name, config_id), group in sorted(grouped.items()):
        seeds = sorted({r['seed'] for r in group})
        group_id = digest([name, config_id])[:12]
        groups_out.append({'group':name, 'config_id':config_id, 'quality_group_id':group_id,
                           'seeds':seeds, 'run_ids':[r['id'] for r in group],
                           'status':'evaluated' if len(seeds) >= 2 else 'insufficient_independent_seeds',
                           'config':group[0]['config'], 'redundant_signals':[]})
        common = [row for run in group for row in run['rows']
                  if available(row, ('outstanding_per_capacity', 'work_ms_per_capacity'))]
        nonzero = [r for r in common if r['outstanding_per_capacity'] > 0]
        ratios = [r['work_ms_per_capacity']/r['outstanding_per_capacity'] for r in nonzero]
        if ratios and max(ratios)-min(ratios) <= 1e-9*max(1., max(ratios)):
            groups_out[-1]['redundant_signals'].append({'metrics':['outstanding_per_capacity', 'work_ms_per_capacity'],
                'relation':'work = outstanding * constant estimate', 'constant_ms':avg(ratios),
                'note':'No extra signal in this experiment; async snapshots may make exact equality imperfect.'})
        # Context is deliberately separated from load metrics, never called pool load.
        metrics = METRICS+('burst_context_remaining',)
        for run in group:
            for metric in metrics:
                rows = [r for r in run['rows'] if available(r, (metric,))]
                xs = [r[metric] for r in rows]
                for target in TARGETS:
                    ys = [r[target] for r in rows]
                    correlations.append({'run':run['id'], 'seed':run['seed'], 'case':run['case'],
                        'group':name, 'config_id':config_id, 'metric':metric, 'target':target,
                        'kind':'context' if metric.startswith('burst_context') else 'pool_metric',
                        'n':len(rows), 'coverage':len(rows)/len(run['rows']) if run['rows'] else 0,
                        'distinct_values':len(set(xs)), 'pearson':pearson(xs, ys),
                        'spearman':pearson(ranks(xs), ranks(ys)),
                        'status':'missing' if not xs else 'constant' if len(set(xs)) == 1 else 'descriptive'})
        if len(seeds) < 2:
            continue
        for seed in seeds:
            train = [row for run in group if run['seed'] != seed for row in run['rows']]
            test = [dict(row, source_run=run['id']) for run in group if run['seed'] == seed for row in run['rows']]
            for metric in metrics:
                training = [r for r in train if available(r, (metric, 'own_budget_ms'))]
                testing = [r for r in test if available(r, (metric, 'own_budget_ms'))]
                if not training or not testing:
                    continue
                boundaries = edges([r[metric] for r in training], bins)
                cells = defaultdict(list)
                for row in testing:
                    cells[bisect_right(boundaries, row[metric])].append(row)
                for target in TARGETS:
                    for cell, rows in sorted(cells.items()):
                        buckets.append({'group':name, 'config_id':config_id, 'held_seed':seed,
                            'metric':metric, 'target':target, 'bucket':cell,
                            'lower_inclusive':boundaries[cell-1] if cell else None,
                            'upper_exclusive':boundaries[cell] if cell < len(boundaries) else None,
                            'boundary_source':'training_seeds_only',
                            **describe([r[target] for r in rows], [(r['source_run'], r['burst']) for r in rows])})
                    models = {'global':(), 'own_budget_oracle':('own_budget_ms',), metric:(metric,),
                              metric+'+own_budget_oracle':(metric, 'own_budget_ms')}
                    results = {}
                    for model_name, features in models.items():
                        fitted = fit_buckets(training, features, target, bins)
                        predicted, fallback = predict(fitted, testing)
                        results[model_name] = prediction_error([r[target] for r in testing], predicted)
                        predictions.append({'group':name, 'config_id':config_id, 'held_seed':seed,
                            'training_seeds':','.join(str(s) for s in seeds if s != seed),
                            'metric':metric, 'target':target, 'model':model_name,
                            'model_kind':'context' if metric.startswith('burst_context') else 'pool_metric',
                            'training_n':len(training), 'test_available_n':len(testing), 'test_total_n':len(test),
                            'test_unique_bursts':len({(r['source_run'], r['burst']) for r in testing}),
                            'tail_quality':'LOW_TAIL' if len(testing) < 1000 or len({(r['source_run'], r['burst']) for r in testing}) < 100 else 'DESCRIPTIVE',
                            'fallback_n':fallback, 'training_boundaries':canonical(fitted['boundaries']),
                            **results[model_name]})
                    for row in predictions[-len(models):]:
                        row['mean_mae_reduction_vs_global_ms'] = results['global']['mean_mae_ms']-row['mean_mae_ms']
                        row['mean_mae_reduction_vs_budget_ms'] = results['own_budget_oracle']['mean_mae_ms']-row['mean_mae_ms']
                        row['p99_pinball_reduction_vs_global_ms'] = results['global']['p99_pinball_ms']-row['p99_pinball_ms']
                        row['p99_pinball_reduction_vs_budget_ms'] = results['own_budget_oracle']['p99_pinball_ms']-row['p99_pinball_ms']
    return groups_out, correlations, buckets, predictions


def bootstrap_ci(values, samples=3000):
    if len(values) < 7:
        return None
    rng = random.Random(61337)
    means = [avg([values[rng.randrange(len(values))] for _ in values]) for _ in range(samples)]
    return [quantile(means, .025), quantile(means, .975)]


PAIR_FIELDS = ('planned_latency_ms_mean', 'planned_latency_ms_p99', 'latency_ms_mean', 'latency_ms_p99',
               'wait_ms_mean', 'wait_ms_p99', 'callback_ms_mean', 'callback_ms_p99',
               'submit_lag_ms_mean', 'submit_lag_ms_p99', 'burst_planned_mean_ms', 'burst_planned_p99_ms',
               'process_cpu_per_completed_task_ms', 'whole_completed_per_observed_second',
               'whole_observed_seconds', 'drain_after_offer_seconds', 'route_mean_ns', 'route_p99_ns')


def compare_runs(runs, selection_seeds=()):
    """Strict, per-seed paired differences. Negative latency deltas are improvements."""
    by_group = defaultdict(list)
    for run in runs:
        by_group[run['group']].append(run)
    pairs = []
    for group, group_runs in sorted(by_group.items()):
        discovery = {r['seed'] for r in group_runs if r['split'] in ('observe', 'tune')} | set(selection_seeds)
        homes = [r for r in group_runs if r['routing'] == 'home']
        for candidate in group_runs:
            if candidate['routing'] == 'home':
                continue
            baseline = [r for r in homes if r['seed'] == candidate['seed'] and r['split'] == candidate['split'] and r['config_id'] == candidate['config_id']]
            reasons = list(candidate['invalid_reasons'])
            if candidate['split'] == 'evaluate' and candidate['seed'] in discovery:
                reasons.append('selection_seed_leakage')
            if len(baseline) != 1:
                reasons.append('missing_or_ambiguous_matched_home')
                baseline = None
            else:
                baseline = baseline[0]
                reasons += ['baseline_'+x for x in baseline['invalid_reasons']]
                if baseline['sequence_sha256'] != candidate['sequence_sha256']:
                    reasons.append('planned_task_budget_sequence_mismatch')
            row = {'group':group, 'config_id':candidate['config_id'], 'case':candidate['case'],
                'baseline_case':baseline['case'] if baseline else None, 'run':candidate['id'],
                'baseline_run':baseline['id'] if baseline else None, 'seed':candidate['seed'],
                'split':candidate['split'], 'routing':candidate['routing'], 'metric':candidate['metric'],
                'threshold':candidate['threshold'], 'min_improvement':candidate['min_improvement'],
                'evaluation_scope':candidate.get('evaluation_scope'),
                'valid':not reasons, 'reasons':sorted(set(reasons)), 'deltas':{},
                'candidate':candidate['statistics'], 'baseline':baseline['statistics'] if baseline else None}
            if baseline:
                for field in PAIR_FIELDS:
                    x, y = candidate['statistics'][field], baseline['statistics'][field]
                    row['deltas'][field] = x-y if x is not None and y is not None else None
                for field in ('planned_latency_ms_mean', 'planned_latency_ms_p99'):
                    b = baseline['statistics'][field]
                    row[field+'_improvement_percent'] = 100*(b-candidate['statistics'][field])/b if b else None
            pairs.append(row)
    grouped = defaultdict(list)
    for row in pairs:
        key = tuple(row[k] for k in ('group', 'config_id', 'case', 'split', 'routing', 'metric', 'threshold', 'min_improvement'))
        grouped[key].append(row)
    comparisons = []
    for key, group in sorted(grouped.items(), key=lambda x:repr(x[0])):
        values = dict(zip(('group', 'config_id', 'case', 'split', 'routing', 'metric', 'threshold', 'min_improvement'), key))
        good = [r for r in group if r['valid']]
        if len({r['seed'] for r in good}) != len(good):
            values['duplicate_seed_warning'] = True
            good = []
        values.update(n_pairs=len(good), n_invalid=len(group)-len(good), seeds=sorted(r['seed'] for r in good),
                      status='paired_seed_bootstrap' if len(good) >= 7 else 'descriptive_only' if good else 'invalid',
                      confirmation=('extrapolation_evaluation' if any(r.get('evaluation_scope') == 'extrapolation' for r in good) else 'held_out_evaluation') if key[3] == 'evaluate' and good else 'exploratory_only',
                      all_pairs_valid=len(good) == len(group),
                      spill_fraction=avg([r['candidate']['spill_fraction'] for r in good if r['candidate']['spill_fraction'] is not None]))
        for field in PAIR_FIELDS:
            ds = [r['deltas'][field] for r in good if r['deltas'].get(field) is not None]
            values[field+'_delta'] = avg(ds)
            values[field+'_delta_ci95'] = bootstrap_ci(ds)
        for field in ('planned_latency_ms_mean', 'planned_latency_ms_p99'):
            values[field+'_improvement_percent'] = avg([r[field+'_improvement_percent'] for r in good if r.get(field+'_improvement_percent') is not None])
        if values['n_invalid']:
            values['confirmation'] = 'incomplete_evidence'
            values['status'] = 'incomplete_evidence_descriptive_only' if good else 'invalid'
            for field in PAIR_FIELDS:
                values[field+'_delta_ci95'] = None
        # Explicit convenience names for freezing scripts; full names remain authoritative.
        values['planned_mean_delta_ms'] = values['planned_latency_ms_mean_delta']
        values['planned_p99_delta_ms'] = values['planned_latency_ms_p99_delta']
        comparisons.append(values)
    return pairs, comparisons


def observer_cost(runs):
    """Fixed-iteration, home-only cost experiment; observation is the intervention."""
    normalized = []
    for run in runs:
        if run['split'] != 'cost' or run['routing'] != 'home':
            continue
        r = dict(run)
        config = dict(run['config'])
        # Explicitly allowed differences in this experiment only. Routing pairs
        # continue to require these fields to match exactly.
        for key in ('effective_load_observation', 'minimal_load_metric',
                    'maintain_outstanding', 'maintain_work', 'publish_remaining'):
            config.pop(key, None)
        r['config'] = config
        r['config_id'] = digest(config)[:12]
        r['group'] = run['group'].split('-obs-', 1)[0]
        r['routing'] = 'home' if run['observation'] == 'off' else 'observer_cost'
        r['metric'] = run['observation'] + ('/'+run['metric'] if run['observation'] == 'minimal' else '')
        r['threshold'] = None
        r['invalid_reasons'] = list(run['invalid_reasons'])
        if run['metadata'].get('work_mode') != 'iterations':
            r['invalid_reasons'].append('observer_cost_requires_fixed_iterations')
        r['valid'] = not r['invalid_reasons']
        normalized.append(r)
    return compare_runs(normalized)


def fmt(value, places=3):
    if value is None:
        return 'NA'
    if isinstance(value, float):
        return f'{value:.{places}f}'
    return str(value)


def report_markdown(report):
    out = ['# 阶段3：指标质量与阈值均衡', '',
        f"读取 {len(report['runs'])} 轮，文件缺失/损坏等排除 {len(report['excluded_entries'])} 条。"
        '原始 CSV 保留在各运行目录；本报告不重新解释失败为性能收益。', '',
        '## 逐轮原始摘要', '',
        '|case / seed / split|valid|完成/计划|planned均值 ms|planned p99 ms|W均值 ms|X均值 ms|spill|',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for run in report['runs']:
        s = run['statistics']
        out.append(f"|{run['case']} / {run['seed']} / {run['split']}|{run['valid']}|{s['cohort_completed']}/{s['cohort_planned']}|"
            f"{fmt(s['planned_latency_ms_mean'])}|{fmt(s['planned_latency_ms_p99'])}|{fmt(s['wait_ms_mean'])}|{fmt(s['callback_ms_mean'])}|{fmt(s['spill_fraction'])}|")
    out += ['', '## 同种子配对收益', '', '下表为候选减home的逐轮差值再取均值；负值表示时延下降。p99没有跨轮合并。', '',
        '|case / split|有效/无效对|planned均值差 ms|planned p99差 ms|95% CI（p99差）|说明|',
        '|---|---:|---:|---:|---|---|']
    for item in report['comparisons']:
        out.append(f"|{item['case']} / {item['split']}|{item['n_pairs']}/{item['n_invalid']}|{fmt(item['planned_mean_delta_ms'])}|"
                   f"{fmt(item['planned_p99_delta_ms'])}|{fmt(item['planned_latency_ms_p99_delta_ci95'])}|{item['status']}|")
    if not report['comparisons']:
        out += ['没有可比较的routing候选；观测阶段不产生均衡收益结论。']
    out += ['', '## 留出整轮的指标预测', '',
        '下表每行固定配置、指标、目标和模型，数值为留出seed各折等权均值；所有折明细见prediction.csv。'
        'MAE越小越好，p99 pinball越小越好，coverage需结合0.99目标和尾样本量判断。'
        'buckets.csv边界来自训练seed，p99及LOW_TAIL以独立burst数伴随报告。', '',
        '|配置 / 指标|目标|模型|折数|mean MAE ms|p99 pinball ms|p99 coverage|相对自身预算基线MAE减少 ms|',
        '|---|---|---|---:|---:|---:|---:|---:|']
    aggregate = defaultdict(list)
    for row in report['prediction']:
        if row['model'] in ('global', 'own_budget_oracle'):
            continue
        aggregate[(row['config_id'], row['metric'], row['target'], row['model'])].append(row)
    for (config, metric, target, model), rows in sorted(aggregate.items()):
        out.append(f"|{config} / {metric}|{target}|{model}|{len(rows)}|{fmt(avg([r['mean_mae_ms'] for r in rows]))}|"
                   f"{fmt(avg([r['p99_pinball_ms'] for r in rows]))}|{fmt(avg([r['p99_coverage'] for r in rows]), 4)}|"
                   f"{fmt(avg([r['mean_mae_reduction_vs_budget_ms'] for r in rows]))}|")
    out += ['', '## 固定工作量的观测成本', '',
        '仅cost、home、iterations模式比较；相同seed/预算序列/资源，允许观测开关变化。routing收益表仍要求观测配置一致。', '',
        '|case|有效对|CPU/completed task差 ms|planned均值差 ms|planned p99差 ms|',
        '|---|---:|---:|---:|---:|']
    for row in report['observer_cost_comparisons']:
        out.append(f"|{row['case']}|{row['n_pairs']}|{fmt(row['process_cpu_per_completed_task_ms_delta'])}|{fmt(row['planned_mean_delta_ms'])}|{fmt(row['planned_p99_delta_ms'])}|")
    redundant = [(g['config_id'], x) for g in report['quality_groups'] for x in g['redundant_signals']]
    if redundant:
        out += ['', '检测到work与outstanding仅为同一信号的常数缩放：'+', '.join(config for config, _ in redundant)+'。这不构成两个独立指标发现。']
    if report['excluded_entries']:
        out += ['', '## 未纳入记录', '', *[f"- {r['entry']}: {r['reason']}" for r in report['excluded_entries']]]
    bad = [r for r in report['pairs'] if not r['valid']]
    if bad:
        out += ['', '## 无效配对', '', *[f"- {r['case']} seed={r['seed']}: {', '.join(r['reasons'])}" for r in bad]]
    out += ['', '## 口径与限制', '', *['- '+note for note in NOTES], '']
    return '\n'.join(out)


def csv_write(path, rows):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k:canonical(v) if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()})


def analyze(paths, start=None, end=None, bins=8):
    runs, excluded, manifests = read_manifests(paths, start, end)
    quality_groups, correlations, buckets, prediction = metric_analysis(runs, bins)
    selection_seeds = set()
    for source in manifests:
        selection = source.get('selection') or {}
        selection_seeds.update(selection.get('source_seeds', selection.get('tuning_seeds', [])))
    pairs, comparisons = compare_runs(runs, selection_seeds)
    cost_pairs, cost_comparisons = observer_cost(runs)
    for comparison in comparisons:
        missing = [entry for entry in excluded if entry['group'] == comparison['group'] and entry['split'] == comparison['split']]
        comparison['missing_group_entries'] = len(missing)
        if missing:
            comparison['all_pairs_valid'] = False
            comparison['confirmation'] = 'incomplete_evidence'
            comparison['status'] = 'incomplete_evidence_descriptive_only'
            for field in PAIR_FIELDS:
                comparison[field+'_delta_ci95'] = None
    return {'schema':1, 'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'method':'whole-seed holdout, training-only bins; strict paired routing comparisons',
        'notes':NOTES, 'manifests':manifests, 'excluded_entries':excluded,
        'runs':[{k:v for k,v in r.items() if k != 'rows'} for r in runs],
        'quality_groups':quality_groups, 'correlations':correlations, 'buckets':buckets,
        'prediction':prediction, 'pairs':pairs, 'comparisons':comparisons,
        'observer_cost_pairs':cost_pairs, 'observer_cost_comparisons':cost_comparisons}


def emit(report, output):
    output.mkdir(parents=True, exist_ok=True)
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    (output/'report.md').write_text(report_markdown(report))
    csv_write(output/'runs.csv', [{**{k:r[k] for k in ('id', 'case', 'group', 'split', 'seed', 'routing', 'metric', 'threshold', 'config_id', 'valid', 'invalid_reasons', 'sequence_sha256')}, **r['statistics']} for r in report['runs']])
    for name in ('correlations', 'buckets', 'prediction'):
        csv_write(output/(name+'.csv'), report[name])
    csv_write(output/'paired.csv', [{**{k:v for k,v in row.items() if k not in ('candidate', 'baseline', 'deltas')}, **{k+'_delta':v for k,v in row['deltas'].items()}} for row in report['pairs']])
    csv_write(output/'threshold_curve.csv', report['comparisons'])
    csv_write(output/'observer_cost_pairs.csv', report['observer_cost_pairs'])
    csv_write(output/'observer_cost_comparisons.csv', report['observer_cost_comparisons'])
    print(f"runs={len(report['runs'])} invalid_runs={sum(not r['valid'] for r in report['runs'])} excluded={len(report['excluded_entries'])} quality_groups={len(report['quality_groups'])}")
    print('case split seed valid completed/planned planned_mean_ms planned_p99_ms W_mean_ms X_mean_ms spill')
    for run in report['runs']:
        s = run['statistics']
        print(run['case'], run['split'], run['seed'], run['valid'], f"{s['cohort_completed']}/{s['cohort_planned']}",
              *(fmt(s[k]) for k in ('planned_latency_ms_mean', 'planned_latency_ms_p99', 'wait_ms_mean', 'callback_ms_mean', 'spill_fraction')))
    print('PAIRED: case split valid_pairs invalid_pairs planned_mean_delta_ms planned_p99_delta_ms status')
    for row in report['comparisons']:
        print(row['case'], row['split'], row['n_pairs'], row['n_invalid'], fmt(row['planned_mean_delta_ms']), fmt(row['planned_p99_delta_ms']), row['status'])
    print('OBSERVER_COST: case pairs CPU_per_completed_delta_ms planned_mean_delta_ms planned_p99_delta_ms')
    for row in report['observer_cost_comparisons']:
        print(row['case'], row['n_pairs'], fmt(row['process_cpu_per_completed_task_ms_delta']), fmt(row['planned_mean_delta_ms']), fmt(row['planned_p99_delta_ms']))
    for group in report['quality_groups']:
        if group['redundant_signals']:
            print('REDUNDANT_SIGNAL', group['config_id'], 'constant work estimate rescales outstanding; not an independent signal')
    print('QUALITY: config metric target folds mean_MAE_ms p99_pinball_ms p99_coverage MAE_reduction_vs_budget_ms')
    grouped = defaultdict(list)
    for row in report['prediction']:
        if row['model'] == row['metric'] and row['target'] in ('wait_ms', 'planned_latency_ms'):
            grouped[(row['config_id'], row['metric'], row['target'])].append(row)
    for key, values in sorted(grouped.items()):
        print(*key, len(values), *(fmt(avg([r[k] for r in values]), 4) for k in ('mean_mae_ms', 'p99_pinball_ms', 'p99_coverage', 'mean_mae_reduction_vs_budget_ms')))
    for row in report['pairs']:
        if not row['valid']:
            print('INVALID_PAIR', row['case'], row['seed'], ','.join(row['reasons']))
    for row in report['excluded_entries']:
        print('EXCLUDED', row['entry'], row['reason'])
    print('No automatic winning metric/threshold. Positive correlation alone is insufficient; see held-out error, tail coverage, validity and cost.')
    print(output/'report.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', action='append', required=True, type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--window-start', type=float)
    parser.add_argument('--window-end', type=float)
    parser.add_argument('--bins', type=int, default=8)
    args = parser.parse_args()
    if args.window_start is not None and (not math.isfinite(args.window_start) or args.window_start < 0):
        parser.error('window-start must be finite and nonnegative')
    if args.window_end is not None and (not math.isfinite(args.window_end) or args.window_end <= (args.window_start or 0)):
        parser.error('window-end must be finite and after window-start')
    if not 2 <= args.bins <= 32:
        parser.error('bins must be between 2 and 32')
    report = analyze(args.manifest, args.window_start, args.window_end, args.bins)
    emit(report, args.output)
    # Exclusions remain explicit evidence, not a reason to hide other completed runs.
    return 0 if report['runs'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

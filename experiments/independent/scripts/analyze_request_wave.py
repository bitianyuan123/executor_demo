#!/usr/bin/env python3
"""Small, strict descriptive analysis of one request-wave run per policy.

No fitted prediction models or confidence intervals. Admission observations are
paired with the same task's subsequent latency; complete request cohorts retain
late task releases and drain completions.
"""
import argparse
import bisect
import copy
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


POLICIES = ('home', 'source_only', 'guarded')
EDGES = (0., .5, .8, 1., 2., 4.)
LATENCIES = ('S', 'W', 'X', 'L', 'D')
TRACE_FIELDS = ('task', 'burst', 'request_planned_ns', 'planned_ns', 'phase',
                'target_cpu_us', 'home_pool')
INT_FIELDS = ('task', 'burst', 'pool', 'home_pool', 'request_planned_ns',
              'planned_ns', 'submit_ns', 'start_ns', 'end_ns', 'target_cpu_us',
              'home_outstanding', 'home_native_queue', 'selected_outstanding',
              'selected_native_queue', 'spilled', 'source_over_threshold',
              'no_eligible_target', 'no_improvement', 'eligible_candidates')
BASE_MAPPING = {
    'duration': 'duration_seconds', 'arrival-rate': 'arrival_rate',
    'threads': 'threads_per_pool', 'arrivals': 'arrival_limit',
    'load-observation': 'requested_load_observation',
    'long-prob': 'long_prob', 'long-factor': 'long_factor',
}
# Only run identity, policy decisions and the origin of relative task clocks may
# differ. Every other metadata field must agree for a policy comparison.
META_DIFFERENCES = {'pid', 'epoch_monotonic_ns', 'routing', 'load_threshold',
                    'accept_threshold'}


def read_json(path):
    with Path(path).open() as handle:
        return json.load(handle)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve(root, value):
    path = Path(value)
    return path if path.is_absolute() else root/path


def percentile(values, p=.99):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p*len(ordered))-1)]


def stats(values):
    return {'mean_ms': sum(values)/len(values) if values else None,
            'p99_ms': percentile(values)}


def describe(rows):
    n = len(rows)
    requests = len({r['burst'] for r in rows})
    return {'n': n, 'requests': requests,
            'tail_quality': ('EMPTY' if not n else 'LOW_TAIL' if n < 1000 else 'DESCRIPTIVE'),
            'nominal_top_1pct_tasks': n*.01,
            **{key: stats([r[key] for r in rows]) for key in LATENCIES}}


def initial_submission_spans(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row['phase'] == 'initial':
            grouped[row['burst']].append(row)
    actual = [(max(r['submit_ns'] for r in items)-min(r['submit_ns'] for r in items))/1e6
              for items in grouped.values()]
    planned = [(max(r['planned_ns'] for r in items)-min(r['planned_ns'] for r in items))/1e6
               for items in grouped.values()]
    over_30 = sum(value > 30 for value in actual)
    return {'requests': len(actual), 'actual_span_ms': stats(actual), 'planned_span_ms': stats(planned),
            'over_30ms_requests': over_30, 'over_30ms_fraction': over_30/len(actual) if actual else None}


def finite_number(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name}: boolean is not a number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{name}: non-finite value')
    return number


def integer(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name}: boolean is not an integer')
    result = int(value)
    if str(result) != str(value) and float(value) != result:
        raise ValueError(f'{name}: noninteger value')
    return result


def close(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-9)
    return a == b


def load_tasks(path):
    with path.open(newline='') as handle:
        reader = csv.DictReader(handle)
        required = set(INT_FIELDS) | {'phase', 'status', 'capacity'}
        missing = required-set(reader.fieldnames or ())
        if missing:
            raise ValueError('tasks.csv missing fields: '+', '.join(sorted(missing)))
        rows = []
        for line, raw in enumerate(reader, 2):
            row = dict(raw)
            try:
                for field in INT_FIELDS:
                    row[field] = integer(raw[field], field)
                row['capacity'] = finite_number(raw['capacity'], 'capacity')
            except (TypeError, ValueError) as error:
                raise ValueError(f'tasks.csv line {line}: {error}') from error
            rows.append(row)
    return rows


def resource_config(meta):
    return {key: value for key, value in meta.items() if key not in META_DIFFERENCES}


def validate_tasks(rows, meta, summary, plan, policy):
    issues = []
    capacities = meta.get('capacities', [])
    pools = meta.get('pools', 0)
    if (not isinstance(pools, int) or pools < 1 or len(capacities) != pools or
            any(not isinstance(c, (int, float)) or not math.isfinite(c) or c <= 0 for c in capacities)):
        return ['invalid_capacities_or_pool_count']
    n = len(rows)
    ids = [r['task'] for r in rows]
    if sorted(ids) != list(range(n)):
        issues.append('duplicate_or_missing_task_ids')
    if n != meta.get('planned_tasks') or n != summary.get('planned_tasks'):
        issues.append('planned_task_count_mismatch')
    counts = Counter(r['status'] for r in rows)
    if counts != Counter({'completed': n}) or not n:
        issues.append('noncompleted_or_empty_tasks')
    for field, status in (('completed_tasks', 'completed'), ('rejected_tasks', 'rejected'),
                          ('canceled_tasks', 'canceled'), ('failed_tasks', 'failed')):
        if summary.get(field) != counts[status]:
            issues.append(f'summary_{field}_mismatch')
    if not (summary.get('conservation_ok') is True and summary.get('counters_balanced') is True
            and summary.get('final_outstanding') == 0):
        issues.append('counter_conservation_failed')
    final = summary.get('final_pool_counters')
    if not isinstance(final, list) or len(final) != pools:
        issues.append('missing_final_pool_counters')
    else:
        if {p.get('pool') for p in final} != set(range(pools)):
            issues.append('final_pool_counter_ids_mismatch')
        for pool in final:
            if any(pool.get(key) != 0 for key in ('pending', 'inflight', 'work_estimate_us',
                                                 'outstanding', 'remaining_work_us')):
                issues.append('nonzero_final_pool_counter')
    if summary.get('sampling_failed') is True:
        issues.append('sampling_failed')
    if summary.get('sampling_truncated') is True or summary.get('sample_rows_dropped', 0) != 0:
        issues.append('sampling_truncated')
    if summary.get('spilled_tasks') != sum(r['spilled'] for r in rows):
        issues.append('spill_count_mismatch')
    for flag in ('source_over_threshold', 'no_eligible_target', 'no_improvement'):
        if summary.get(flag+'_tasks') != sum(r[flag] for r in rows):
            issues.append(flag+'_count_mismatch')
    requests = defaultdict(list)
    duration = finite_number(meta.get('duration_seconds', 0), 'duration_seconds')
    for row in rows:
        requests[row['burst']].append(row)
        if not 0 <= row['home_pool'] < pools or not 0 <= row['pool'] < pools:
            issues.append('invalid_pool_id')
            continue
        if not close(row['capacity'], capacities[row['pool']]):
            issues.append('selected_capacity_mismatch')
        if not (0 <= row['request_planned_ns'] < duration*1e9 and
                row['request_planned_ns'] <= row['planned_ns'] <= row['submit_ns'] <=
                row['start_ns'] <= row['end_ns']):
            issues.append('invalid_task_clock_order')
        if row['target_cpu_us'] <= 0 or row['phase'] not in ('initial', 'steady'):
            issues.append('invalid_task_workload')
        if any(row[key] < 0 for key in ('home_outstanding', 'home_native_queue',
                                       'selected_outstanding', 'selected_native_queue', 'eligible_candidates')):
            issues.append('missing_or_negative_load_observation')
        for key in ('spilled', 'source_over_threshold', 'no_eligible_target', 'no_improvement'):
            if row[key] not in (0, 1):
                issues.append('invalid_decision_flag')
        if row['spilled'] != int(row['pool'] != row['home_pool']):
            issues.append('spill_flag_mismatch')
        if policy == 'home' and row['spilled']:
            issues.append('home_policy_spilled')
        if policy != 'home':
            expected_trigger = row['home_outstanding']/capacities[row['home_pool']] > meta['load_threshold']
            if row['source_over_threshold'] != int(expected_trigger):
                issues.append('source_trigger_mismatch')
            if row['spilled'] and (not row['source_over_threshold'] or row['no_eligible_target'] or row['no_improvement']):
                issues.append('inconsistent_spill_decision')
            if row['no_eligible_target'] and (not row['source_over_threshold'] or row['eligible_candidates']):
                issues.append('inconsistent_target_ineligible_decision')
            if policy == 'guarded' and row['spilled']:
                if not row['selected_outstanding']/capacities[row['pool']] < meta['accept_threshold']:
                    issues.append('guarded_target_limit_violated')
    if sorted(requests) != list(range(len(requests))) or len(requests) != meta.get('planned_arrivals'):
        issues.append('request_count_or_ids_mismatch')
    base = plan['base']
    for items in requests.values():
        if len({r['request_planned_ns'] for r in items}) != 1 or len({r['home_pool'] for r in items}) != 1:
            issues.append('inconsistent_request_identity')
        phases = Counter(r['phase'] for r in items)
        if (not base['burst-min'] <= phases['initial'] <= base['burst-max'] or
                phases['steady'] != base['steady-tasks']):
            issues.append('incomplete_request_tasks')
    return sorted(set(issues))


def validate_metadata(meta, plan, policy):
    issues = []
    if meta.get('workload') != 'request-wave':
        issues.append('not_request_wave')
    if meta.get('capacities') != plan['capacities']:
        issues.append('plan_capacities_mismatch')
    if meta.get('numa_labels') != plan['numa']:
        issues.append('plan_numa_mismatch')
    if meta.get('allowed_cpus') != plan['cpu_union']:
        issues.append('plan_cpu_union_mismatch')
    if meta.get('effective_masks') != plan['pool_cpus']:
        issues.append('plan_pool_masks_mismatch')
    options = {**plan['base'], **plan['policies'][policy]}
    for key, value in options.items():
        if key in ('cpus', 'numa', 'capacities', 'output'):
            continue
        field = BASE_MAPPING.get(key, key.replace('-', '_'))
        if field not in meta or not close(meta[field], value):
            issues.append(f'plan_option_mismatch:{key}')
    if meta.get('load_metric') != 'outstanding' or meta.get('effective_load_observation') not in ('basic', 'progress'):
        issues.append('both_N_and_Q_observations_required')
    if not isinstance(meta.get('epoch_monotonic_ns'), int) or meta['epoch_monotonic_ns'] <= 0:
        issues.append('missing_epoch_monotonic_ns')
    return issues


def cpu_report(path, meta, window):
    """Use only complete /proc/stat intervals inside the fixed arrival window."""
    begin = meta['epoch_monotonic_ns']+round(window[0]*1e9)
    end = meta['epoch_monotonic_ns']+round(window[1]*1e9)
    result = {}
    with path.open(newline='') as handle:
        samples = list(csv.DictReader(handle))
    scopes = ['union']+[f'pool{i}' for i in range(meta['pools'])]
    for scope in scopes:
        selected = []
        invalid = 0
        expected_ids = meta['allowed_cpus'] if scope == 'union' else meta['effective_masks'][int(scope[4:])]
        for raw in samples:
            if raw['scope'] != scope:
                continue
            start = integer(raw['interval_start_monotonic_ns'], 'cpu interval start')
            stop = integer(raw['monotonic_ns'], 'cpu interval end')
            if not begin <= start < stop <= end:
                continue
            if sorted(integer(value, 'cpu id') for value in raw['cpu_ids'].split(',')) != sorted(expected_ids):
                raise ValueError('CPU sample scope does not match selected CPUs: '+scope)
            if raw['valid'].lower() not in ('1', 'true'):
                invalid += 1
                continue
            weight = finite_number(raw['total_delta'], 'cpu total_delta')
            values = {key: finite_number(raw[key], key) for key in ('execution_pct', 'iowait_pct', 'steal_pct')}
            if weight <= 0 or any(not 0 <= v <= 100 for v in values.values()):
                invalid += 1
                continue
            selected.append((weight, (stop-start)/1e9, values, start, stop))
        total = sum(x[0] for x in selected)
        covered_ns, latest, overlap, gaps = 0, None, False, False
        for sample in sorted(selected, key=lambda item: item[3]):
            start, stop = sample[3:]
            if latest is not None:
                overlap |= start < latest
                gaps |= start > latest+1_000_000  # one millisecond accounting tolerance
            covered_ns += max(0, stop-max(start, latest if latest is not None else start))
            latest = max(stop, latest or stop)
        missing_seconds = (end-begin-covered_ns)/1e9
        allowed_edges = 2*meta.get('sample_ms', 100)/1000+0.001
        reasons = []
        if len(selected) < 2:
            reasons.append('fewer_than_two_complete_intervals')
        if invalid:
            reasons.append('invalid_intervals')
        if overlap:
            reasons.append('overlapping_or_duplicate_intervals')
        if gaps:
            reasons.append('interior_sampling_gap')
        if missing_seconds > allowed_edges:
            reasons.append('insufficient_window_coverage')
        result[scope] = {'status': 'unavailable' if not selected else 'partial' if reasons else 'ok',
                         'issues': reasons,
                         'intervals': len(selected), 'invalid_intervals': invalid,
                         'covered_seconds': covered_ns/1e9,
                         'coverage_fraction': covered_ns/(end-begin),
                         'execution_sample_peak_pct': max((x[2]['execution_pct'] for x in selected), default=None),
                         **{key: sum(x[0]*x[2][key] for x in selected)/total if total else None
                            for key in ('execution_pct', 'iowait_pct', 'steal_pct')}}
    return result


def analyze_run(entry, plan, root, window):
    policy = entry['policy']
    result = {'policy': policy, 'valid': False, 'issues': [], 'warnings': []}
    rows, meta = [], {}
    try:
        run_path = resolve(root, entry['path'])
        raw_path = resolve(root, entry.get('raw_dir', str(run_path/'data')))
        result['path'] = str(run_path)
        for name, expected in entry.get('artifact_sha256', {}).items():
            observed = hashlib.sha256((run_path/name).read_bytes()).hexdigest()
            if observed != expected:
                result['issues'].append('artifact_hash_mismatch:'+name)
        meta, summary = read_json(raw_path/'metadata.json'), read_json(raw_path/'summary.json')
        rows = load_tasks(raw_path/'tasks.csv')
        issues = result['issues']
        if entry.get('returncode') != 0 or entry.get('status') != 'completed':
            issues.append('runner_not_completed')
        if entry.get('cpu_observation', {}).get('status') != 'ok':
            issues.append('cpu_observation_failed')
        issues += validate_metadata(meta, plan, policy)
        issues += validate_tasks(rows, meta, summary, plan, policy)
        result['metadata_config'] = resource_config(meta)
        ordered = sorted(rows, key=lambda r: r['task'])
        trace = [[r[field] for field in TRACE_FIELDS] for r in ordered]
        result['trace_sha256'] = hashlib.sha256(json.dumps(trace, separators=(',', ':')).encode()).hexdigest()
        result['all_tasks'] = len(rows)
        cohort = [r for r in rows if window[0]*1e9 <= r['request_planned_ns'] < window[1]*1e9]
        if not cohort:
            issues.append('empty_request_cohort')
        for row in cohort:
            row.update(S=(row['end_ns']-row['submit_ns'])/1e6,
                       W=(row['start_ns']-row['submit_ns'])/1e6,
                       X=(row['end_ns']-row['start_ns'])/1e6,
                       L=(row['end_ns']-row['planned_ns'])/1e6,
                       D=(row['submit_ns']-row['planned_ns'])/1e6)
        result['cohort'] = describe(cohort)
        result['phases'] = {phase: describe([r for r in cohort if r['phase'] == phase])
                            for phase in ('initial', 'steady')}
        result['phase_cpu_budgets_us'] = {phase: {
            'configured': plan['base'].get(phase+'-cpu-us', plan['base'].get('cpu-us')),
            'resolved_nominal': meta.get('resolved_'+phase+'_cpu_us',
                plan['base'].get(phase+'-cpu-us') or plan['base'].get('cpu-us')),
            'target_mean': sum(r['target_cpu_us'] for r in cohort if r['phase'] == phase) /
                result['phases'][phase]['n'] if result['phases'][phase]['n'] else None,
        } for phase in ('initial', 'steady')}
        spans = initial_submission_spans(cohort)
        result['initial_submission_spans'] = spans
        if spans['over_30ms_requests']:
            result['warnings'].append(f"{spans['over_30ms_requests']}/{spans['requests']} requests took >30 ms to submit their initial tasks; workload release was stretched. Slow tasks remain included.")
        count = len(cohort)
        flags = {key: sum(r[key] for r in cohort) for key in
                 ('spilled', 'source_over_threshold', 'no_eligible_target', 'no_improvement')}
        result['decisions'] = {**{key+'_count': value for key, value in flags.items()},
            **{key+'_fraction': value/count if count else None for key, value in flags.items()},
            'no_eligible_fraction_of_triggered': flags['no_eligible_target']/flags['source_over_threshold']
            if flags['source_over_threshold'] else None}
        result['whole_run_process_cpu_seconds'] = sum(finite_number(summary.get(key, 0), key) for key in
                                                       ('process_user_cpu_seconds', 'process_system_cpu_seconds'))
        result['cpu_arrival_window'] = cpu_report(run_path/'cpu_samples.csv', meta, window)
        if any(scope['status'] != 'ok' for scope in result['cpu_arrival_window'].values()):
            result['warnings'].append('CPU window lacks complete valid intervals for one or more scopes; do not infer CPU utilization there.')
        result['valid'] = not issues
        result['issues'] = sorted(set(issues))
        return result, cohort, meta
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        result['issues'].append(f'unreadable_or_invalid_run:{error}')
        return result, [], meta


def bucket_rows(rows, capacities):
    result = []
    for phase in ('all', 'initial', 'steady'):
        selected = rows if phase == 'all' else [r for r in rows if r['phase'] == phase]
        for metric, field in (('N/C', 'home_outstanding'), ('Q/C', 'home_native_queue')):
            groups = defaultdict(list)
            for row in selected:
                groups[bisect.bisect_right(EDGES, row[field]/capacities[row['home_pool']])-1].append(row)
            for index, lower in enumerate(EDGES):
                description = describe(groups[index])
                result.append({'policy': 'home', 'phase': phase, 'metric': metric,
                               'lower_inclusive': lower,
                               'upper_exclusive': EDGES[index+1] if index+1 < len(EDGES) else None,
                               **{key: description[key] for key in ('n', 'requests', 'tail_quality', 'nominal_top_1pct_tasks')},
                               **{f'{latency}_{kind}': description[latency][kind]
                                  for latency in LATENCIES for kind in ('mean_ms', 'p99_ms')}})
    return result


def analyze(plan_path, manifest_path=None):
    plan_path = Path(plan_path).resolve()
    root = plan_path.parent
    plan = read_json(plan_path)
    manifest = read_json(manifest_path or root/'run_manifest.json')
    if plan.get('schema_version') != 1 or plan.get('family') != 'request-wave':
        raise ValueError('expected schema_version=1 request-wave plan')
    # Check the immutable original before calibration replaces only its analysis
    # duration in a working copy.
    original_plan = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    original_hash_matches = plan.get('plan_sha256') == digest(original_plan)
    calibration = manifest.get('calibration', False)
    if calibration:
        if len(manifest.get('runs', [])) != 1 or manifest['runs'][0].get('policy') != 'home':
            raise ValueError('calibration must contain exactly one home run')
        entry = manifest['runs'][0]
        plan = copy.deepcopy(plan)
        plan['base']['duration'] = entry['options']['duration']
        plan['analysis']['warmup_seconds'] = entry['warmup_seconds']
        plan['analysis']['request_end_seconds'] = entry['options']['duration']
    window = (finite_number(plan['analysis']['warmup_seconds'], 'warmup'),
              finite_number(plan['analysis']['request_end_seconds'], 'request end'))
    if not 0 <= window[0] < window[1] <= plan['base']['duration']:
        raise ValueError('invalid request cohort window')
    top_issues = []
    if not original_hash_matches:
        top_issues.append('original_plan_hash_mismatch')
    if manifest.get('plan_sha256') != plan.get('plan_sha256') or not plan.get('plan_sha256'):
        top_issues.append('manifest_plan_hash_mismatch')
    if not manifest.get('binary_sha256'):
        top_issues.append('missing_binary_identity')
    entries = manifest.get('runs', [])
    names = [entry.get('policy') for entry in entries]
    if not entries or len(names) != len(set(names)) or any(name not in POLICIES for name in names):
        raise ValueError('manifest must contain unique recognized policies')
    expected_policies = set(plan.get('policies', {}))
    if 'home' not in expected_policies or not expected_policies <= set(POLICIES):
        raise ValueError('plan must contain home and only recognized policies')
    analysis_only = manifest.get('analysis_only') is True
    if analysis_only and names != ['home']:
        raise ValueError('analysis_only is reserved for one home validation run')
    runs, cohorts, metas = [], {}, {}
    for entry in entries:
        run, rows, meta = analyze_run(entry, plan, root, window)
        if entry.get('binary_sha256', manifest.get('binary_sha256')) != manifest.get('binary_sha256'):
            run['issues'].append('binary_identity_mismatch')
        if entry.get('plan_sha256', manifest.get('plan_sha256')) != plan.get('plan_sha256'):
            run['issues'].append('run_plan_hash_mismatch')
        run['issues'].extend(top_issues)
        run['valid'] = not run['issues']
        runs.append(run)
        cohorts[entry['policy']], metas[entry['policy']] = rows, meta
    comparison_issues = []
    if set(names) != expected_policies:
        comparison_issues.append('not_all_planned_policy_runs')
    if len(names) < 2:
        comparison_issues.append('fewer_than_two_policy_runs')
    if not all(run['valid'] for run in runs):
        comparison_issues.append('invalid_run')
    if len({run.get('trace_sha256') for run in runs}) != 1:
        comparison_issues.append('task_workload_trace_mismatch')
    if len({json.dumps(run.get('metadata_config'), sort_keys=True) for run in runs}) != 1:
        comparison_issues.append('resource_or_observation_or_workload_config_mismatch')
    buckets = bucket_rows(cohorts['home'], metas['home']['capacities']) if any(r['policy'] == 'home' and r['valid'] for r in runs) else []
    comparisons = []
    if not comparison_issues:
        home = next(run for run in runs if run['policy'] == 'home')
        indexed = {run['policy']: run for run in runs}
        for baseline_name, policy in (('home', 'source_only'), ('home', 'guarded'), ('source_only', 'guarded')):
            if baseline_name not in indexed or policy not in indexed:
                continue
            baseline, run = indexed[baseline_name], indexed[policy]
            comparisons.append({'baseline': baseline_name, 'policy': policy, **{
                f'{latency}_{kind}_change_pct': 100*(run['cohort'][latency][kind]/baseline['cohort'][latency][kind]-1)
                if baseline['cohort'][latency][kind] else None
                for latency in ('S', 'L') for kind in ('mean_ms', 'p99_ms')}})
    report = {'schema_version': 1, 'family': 'request-wave', 'plan': str(plan_path), 'calibration': calibration,
              'analysis_only': analysis_only, 'profile': plan.get('profile', 'legacy'),
              'functional_only': manifest.get('functional_only') is True,
              'home_waveform_gate': manifest.get('home_waveform_gate'),
              'cohort': {'selection': 'request_planned_ns in [warmup_seconds, request_end_seconds); all tasks and drain completions retained',
                         'warmup_seconds': window[0], 'request_end_seconds': window[1]},
              'latency_definitions_ms': {'S': 'end-submit', 'W': 'start-submit', 'X': 'end-start',
                                         'L': 'end-task_planned', 'D': 'submit-task_planned'},
              'input': {'base': plan['base'], 'capacities': plan['capacities'],
                        'pool_cpus': plan['pool_cpus'], 'numa': plan['numa'],
                        'cpu_union': plan['cpu_union'], 'policies': plan['policies'],
                        'policy_order': manifest.get('policy_order', names)},
              'runs': runs, 'comparison_valid': not comparison_issues,
              'comparison_issues': comparison_issues, 'comparisons': comparisons,
              'buckets': buckets,
              'notes': [
                  'One run per policy: descriptive observed differences, no confidence intervals or significance claims.',
                  'Admission N/Q are read before this task reservation; each observation is paired with that same task latency.',
                  'N is unfinished accepted/reserved tasks; Q is the approximate Folly task queue, not Linux runq; C is configured logical accounting capacity.',
                  'Home-only fixed load buckets describe conditional task latency, not a causal or steady-load response curve.',
                  'S=W+X and L=D+S per task and for means; p99 values cannot be added.',
                  'No completion-window filtering and no averaging of per-second or per-bucket p99.',
                  'CPU percentages cover complete sampling intervals inside the fixed arrival window, not the request cohort lifetime; whole-run process CPU seconds include warmup and drain.',
                  'CPU union is measured directly, never averaged from pool percentages. Sampling peaks (100 ms by default) cannot resolve a 20 ms pulse.',
                  'Tasks execute independently (no DAG), but their timings are statistically correlated through shared request release/home and CPU competition. LOW_TAIL means fewer than 1000 tasks (fewer than about 10 nominal tail observations); larger samples remain descriptive.',
                  'Source-trigger fractions count actual routing decisions; home does not run threshold decisions, so its trigger fraction is zero by construction.',
              ]}
    return report


def fmt(value):
    return 'NA' if value is None else f'{value:.3f}' if isinstance(value, float) else str(value)


def render(report):
    inputs, base = report['input'], report['input']['base']
    lines = ['REQUEST-WAVE DESCRIPTIVE REPORT',
             f"INPUT request_rate={base.get('arrival-rate', 'NA')}/s; arrival={base.get('arrival', 'NA')}; seed={base.get('seed', 'NA')}; duration={base['duration']}s",
             f"INPUT initial_tasks={base['burst-min']}..{base['burst-max']} over {base.get('burst-window-ms', 'NA')}ms; steady_tasks={base['steady-tasks']} over {base.get('steady-window-ms', 'NA')}ms; CPU_budget={base.get('cpu-us', 'NA')}us; jitter={base.get('jitter', 'NA')}; long_prob={base.get('long-prob', 'NA')}; normalization={base.get('mean-normalization', 'NA')}",
             f"PHASE CPU BUDGET initial={base.get('initial-cpu-us', base.get('cpu-us', 'NA'))}us; steady={base.get('steady-cpu-us', base.get('cpu-us', 'NA'))}us; profile={report.get('profile', 'legacy')}; 0 means inherit CPU_budget; resolved nominal and actual cohort target means are printed below",
             f"RESOURCES pools={base['pools']}; workers_per_pool={base['threads']}; affinity={base.get('affinity', 'NA')}; C={inputs['capacities']}; NUMA={inputs['numa']}; pool_CPUs={inputs['pool_cpus']}",
             f"OBSERVATION instrumentation={base.get('instrumentation', 'NA')}; load={base.get('load-observation', 'NA')}; CPU_sample_ms={base.get('sample-ms', 'NA')}; order={inputs['policy_order']}; policies={json.dumps(inputs['policies'], sort_keys=True)}",
             f"request cohort: [{report['cohort']['warmup_seconds']}, {report['cohort']['request_end_seconds']}) seconds; retain entire requests and drain",
             'S=end-submit, W=start-submit, X=end-start, L=end-task_planned, D=submit-task_planned (milliseconds)',
             'comparison_valid='+str(report['comparison_valid']).lower(),
             'comparison_issues='+','.join(report['comparison_issues']), '',
             'policy,valid,tasks,requests,S_mean_ms,S_p99_ms,L_mean_ms,L_p99_ms,CPU_union_execution_pct,whole_run_process_CPU_s,spill_fraction,source_trigger_fraction,target_ineligible_fraction,target_ineligible_given_trigger']
    if report.get('functional_only'):
        lines.insert(1, 'FUNCTIONAL_ONLY: development exercise without high-resolution trace. Home 5-20ms waveform is unvalidated; numeric comparisons are not production-performance evidence.')
    gate = report.get('home_waveform_gate')
    if gate is not None:
        lines.insert(1, 'HOME WAVEFORM GATE '+json.dumps({key: gate.get(key) for key in ('status', 'passed', 'comparison_allowed', 'reasons')}, sort_keys=True))
    for run in report['runs']:
        cohort, decisions = run.get('cohort', {}), run.get('decisions', {})
        values = [run['policy'], run['valid'], cohort.get('n'), cohort.get('requests'),
                  *(cohort.get(latency, {}).get(kind) for latency in ('S', 'L') for kind in ('mean_ms', 'p99_ms')),
                  run.get('cpu_arrival_window', {}).get('union', {}).get('execution_pct'),
                  run.get('whole_run_process_cpu_seconds'),
                  *(decisions.get(key) for key in ('spilled_fraction', 'source_over_threshold_fraction',
                                                  'no_eligible_target_fraction', 'no_eligible_fraction_of_triggered'))]
        lines.append(','.join(map(fmt, values)))
        if run['issues']:
            lines.append('INVALID '+run['policy']+': '+'; '.join(run['issues']))
        for warning in run.get('warnings', []):
            lines.append('WARNING '+run['policy']+': '+warning)
        if run.get('phase_cpu_budgets_us'):
            lines.append('PHASE TARGET CPU '+run['policy']+': '+json.dumps(run['phase_cpu_budgets_us'], sort_keys=True))
    if report['comparisons']:
        lines += ['', 'baseline,policy,S_mean_change_pct,S_p99_change_pct,L_mean_change_pct,L_p99_change_pct (negative=lower latency)']
        for row in report['comparisons']:
            lines.append(','.join(fmt(row[key]) for key in ('baseline', 'policy', 'S_mean_ms_change_pct', 'S_p99_ms_change_pct',
                                                           'L_mean_ms_change_pct', 'L_p99_ms_change_pct')))
    lines += ['', 'CPU ARRIVAL WINDOW', 'policy,scope,execution_pct,execution_sample_peak_pct,iowait_pct,steal_pct,covered_seconds,intervals,status']
    for run in report['runs']:
        for scope, values in run.get('cpu_arrival_window', {}).items():
            lines.append(','.join(map(fmt, [run['policy'], scope, *(values[key] for key in
                ('execution_pct', 'execution_sample_peak_pct', 'iowait_pct', 'steal_pct', 'covered_seconds', 'intervals', 'status'))])))
            if values['issues']:
                lines.append('CPU WARNING '+run['policy']+'/'+scope+': '+','.join(values['issues']))
    lines += ['', 'INITIAL TASK SUBMISSION SPAN (per request, last submit minus first submit)',
              'policy,requests,actual_span_mean_ms,actual_span_p99_ms,planned_span_mean_ms,over_30ms_requests,over_30ms_fraction']
    for run in report['runs']:
        spans = run.get('initial_submission_spans')
        if spans:
            lines.append(','.join(map(fmt, [run['policy'], spans['requests'], spans['actual_span_ms']['mean_ms'],
                spans['actual_span_ms']['p99_ms'], spans['planned_span_ms']['mean_ms'],
                spans['over_30ms_requests'], spans['over_30ms_fraction']])))
    lines += ['', 'DECOMPOSITION (p99 components are not additive)', 'policy,phase,n,requests,S_mean,W_mean,X_mean,L_mean,D_mean,S_p99,W_p99,X_p99,L_p99,D_p99']
    for run in report['runs']:
        for phase, values in [('all', run.get('cohort', {})), *run.get('phases', {}).items()]:
            if values:
                lines.append(','.join(map(fmt, [run['policy'], phase, values['n'], values['requests'],
                    *(values[key]['mean_ms'] for key in LATENCIES), *(values[key]['p99_ms'] for key in LATENCIES)])))
    if not report.get('calibration'):
        lines += ['', 'HOME ADMISSION BUCKETS', 'phase,metric,lower_inclusive,upper_exclusive,n,requests,S_mean_ms,S_p99_ms,W_mean_ms,X_mean_ms,L_mean_ms,D_mean_ms,tail_quality']
        for row in report['buckets']:
            lines.append(','.join(fmt(row[key]) for key in ('phase', 'metric', 'lower_inclusive', 'upper_exclusive',
                'n', 'requests', 'S_mean_ms', 'S_p99_ms', 'W_mean_ms', 'X_mean_ms', 'L_mean_ms', 'D_mean_ms', 'tail_quality')))
    lines += ['', 'NOTES', *report['notes']]
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--out', '--output', dest='out', type=Path)
    parser.add_argument('--calibration', action='store_true', help='read calibration_manifest.json next to the plan')
    args = parser.parse_args(argv)
    try:
        manifest = args.manifest or (args.plan.parent/'calibration_manifest.json' if args.calibration else None)
        report = analyze(args.plan, manifest)
    except (OSError, KeyError, ValueError, TypeError) as error:
        parser.exit(2, f'request-wave analysis: {error}\n')
    out = args.out or args.plan.parent/'analysis'
    out.mkdir(parents=True, exist_ok=True)
    (out/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    text = render(report)
    (out/'report.txt').write_text(text)
    with (out/'buckets.csv').open('w', newline='') as handle:
        fields = list(report['buckets'][0]) if report['buckets'] else ['policy', 'phase', 'metric']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(report['buckets'])
    print(text, end='')
    print(f'Artifacts: {out}/report.json, {out}/report.txt, {out}/buckets.csv')
    return 0 if all(run['valid'] for run in report['runs']) and (report['comparison_valid'] or report['calibration'] or report['analysis_only']) else 2


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only tail detail for an existing complete destination-admission matrix.

Validate all twelve runs before printing one header plus twelve JSON lines. No
run is launched, censored, excluded or rewritten. Development input remains
explicitly development input; top-N details are not new performance samples.
"""

import argparse
from collections import Counter, defaultdict
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = ROOT / 'scripts' / 'run_destination_admission_matrix.py'
SPEC = importlib.util.spec_from_file_location('tail_admission_matrix', MATRIX_PATH)
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)
report, flow, phase = (matrix.module(name) for name in ('report', 'flow_report', 'request_phase_overlap'))
SCHEMA = 'affinity-admission-tail-audit-v1'
MS = 1_000_000
THRESHOLDS = (1, 5, 20)
ADMISSION_COUNTS = ('candidate_checks', 'candidate_score_invalid', 'candidate_invalid',
                    'candidate_over_limit', 'candidate_insufficient_gain', 'return_home_rejected')


def role_map(summary):
    """Versioned demo tags; flow.task_roles independently checks the same map."""
    tags = {0: {'role': 'init', 'wave': None, 'stage': None, 'branch': None}}
    offset = 1
    for wave, (width, depth) in enumerate(zip(summary['widths'], summary['depths'])):
        for stage in range(depth):
            role = 'ordinary successors' if stage else ('later-wave stage0' if wave else 'first-wave stage0')
            for branch in range(width):
                tags[offset + stage * width + branch] = {
                    'role': role, 'wave': wave, 'stage': stage, 'branch': branch}
        tags[offset + width * depth] = {'role': 'join', 'wave': wave, 'stage': None, 'branch': None}
        offset += width * depth + 1
    if offset != summary['nodes_per_request']:
        raise ValueError('tail audit: role/node count mismatch')
    return tags


def nonnegative_int(value, label):
    if isinstance(value, bool):
        raise ValueError(f'{label}: boolean is not an integer counter')
    if isinstance(value, str):
        if not value.isdecimal():
            raise ValueError(f'{label}: expected decimal nonnegative integer')
        value = int(value)
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}: expected nonnegative integer')
    return value


def finite_number(value, label):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'{label}: expected finite nonnegative value')
    return value


def admission_fields(row, limit):
    """Parse extra CSV columns not consumed by the general run loader."""
    required = ('selection_batch_tasks', 'admission_enabled', 'admission_evaluated',
                'admission_before', 'admission_after', *ADMISSION_COUNTS)
    report.require_fields(row, required, 'admission callback trace')
    values = {key: nonnegative_int(row[key], key) for key in required
              if key not in ('admission_before', 'admission_after')}
    if values['selection_batch_tasks'] != 1:
        raise ValueError('tail audit requires the recorded chunk=1 matrix')
    for key in ('admission_enabled', 'admission_evaluated', 'return_home_rejected'):
        if values[key] not in (0, 1):
            raise ValueError(f'{key}: expected 0 or 1')
    if values['admission_enabled'] != int(limit > 0):
        raise ValueError('callback admission_enabled differs from matrix')
    if values['admission_evaluated'] != int(limit > 0 and row['source'] != row['chosen']):
        raise ValueError('parent matrix selected-target admission validity disagrees with placement')
    for key in ('admission_before', 'admission_after'):
        if values['admission_evaluated']:
            values[key] = finite_number(row[key], key)
        elif row[key] != '':
            raise ValueError('unevaluated admission pressure must be blank, not a measured zero')
        else:
            values[key] = None
    if values['admission_evaluated']:
        if not values['admission_before'] <= values['admission_after'] <= limit + 1e-9:
            raise ValueError('selected-target projected pressure violates the recorded admission limit')
    rejected = sum(values[key] for key in ('candidate_score_invalid', 'candidate_invalid',
                                          'candidate_over_limit', 'candidate_insufficient_gain'))
    if rejected > values['candidate_checks'] or values['return_home_rejected'] > values['candidate_checks']:
        raise ValueError('callback candidate-counter conservation mismatch')
    if not limit and any(values[key] for key in ADMISSION_COUNTS):
        raise ValueError('disabled admission has active callback counters')
    return values


def validate_callback_diagnostics(run, limit):
    """Chunk=1 allows whole-trace counters to match whole-run selection totals."""
    diagnostics, totals, reasons = {}, Counter(), Counter()
    for row in run['tasks']:
        values = admission_fields(row, limit)
        diagnostics[row['request'], row['tag']] = values
        reason = flow.routing_reason(row)
        if reason not in matrix.DECISION_REASONS:
            raise ValueError('tail audit: unknown routing reason')
        if limit:
            reasons[reason] += 1
            totals['selections'] += 1
            totals['evaluated_selections'] += int(values['candidate_checks'] > values['candidate_score_invalid'])
            totals['no_eligible_target'] += int(reason == 'NoEligibleTarget')
        for key in ADMISSION_COUNTS:
            totals[key] += values[key]
    admission = run['summary']['target_admission']
    if any(totals[key] != value for key, value in admission['stats'].items()):
        raise ValueError('whole callback trace/admission summary counter mismatch')
    if any(reasons[key] != value for key, value in admission['decision_reasons'].items()):
        raise ValueError('whole callback trace/admission summary reason mismatch')
    return diagnostics


def wait_ns(task):
    return task['start_ns'] - task['submitted_ns']


def wall_ns(task):
    return task['finish_ns'] - task['start_ns']


def wait_counts(tasks):
    return {'callbacks': len(tasks), 'total_wait_ms': sum(map(wait_ns, tasks)) / MS,
            'thresholds': [{'wait_gt_ms': threshold,
                            'callbacks': sum(wait_ns(row) > threshold * MS for row in tasks),
                            'wait_ms': sum(wait_ns(row) for row in tasks if wait_ns(row) > threshold * MS) / MS}
                           for threshold in THRESHOLDS]}


def wait_overlap(task, all_tasks, tags):
    """Other callbacks on chosen pool, including requests outside the cohort.

    Clip callback [start,finish) to this wait [submitted,start). The union is
    elapsed wall time covered; the sum is callback-wall time and can exceed the
    wait. Neither establishes CPU usage, runnable pressure or delay causation.
    """
    intervals, roles, requests = [], defaultdict(lambda: [0, 0]), set()
    other_requests = same_request = 0
    left, right = task['submitted_ns'], task['start_ns']
    for other in all_tasks:
        if other['chosen'] != task['chosen'] or (other['request'], other['tag']) == (task['request'], task['tag']):
            continue
        start, end = max(left, other['start_ns']), min(right, other['finish_ns'])
        if end <= start:
            continue
        intervals.append((start, end))
        requests.add(other['request'])
        roles[tags[other['tag']]['role']][0] += 1
        roles[tags[other['tag']]['role']][1] += end - start
        same_request += int(other['request'] == task['request'])
        other_requests += int(other['request'] != task['request'])
    union, last_end = 0, left
    for start, end in sorted(intervals):
        union += max(0, end - max(last_end, start))
        last_end = max(last_end, end)
    active = peak = 0
    for _, delta in sorted(point for start, end in intervals for point in ((start, 1), (end, -1))):
        active += delta
        peak = max(peak, active)
    return {'overlapping_callbacks': len(intervals), 'same_request_callbacks': same_request,
            'other_request_callbacks': other_requests, 'distinct_requests': len(requests),
            'covered_union_ms': union / MS,
            'callback_wall_sum_ms': sum(end - start for start, end in intervals) / MS,
            'max_overlapping_callback_wall_intervals': peak,
            'by_role': {role: {'callbacks': values[0], 'callback_wall_sum_ms': values[1] / MS}
                        for role, values in sorted(roles.items())}}


def callback_detail(task, run, tags, diagnostics):
    origin = run['summary']['origin_ns']
    result = {key: task[key] for key in ('request', 'tag', 'kind', 'home', 'source', 'chosen',
                                        'hops', 'reason', 'migrated', 'cross_numa',
                                        'queue_before', 'outstanding_before', 'callback_inflight_before')}
    result.update(tags[task['tag']])
    result['reason_name'] = flow.routing_reason(task)
    result['worker_tid'] = nonnegative_int(task['worker_tid'], 'worker_tid') if 'worker_tid' in task else None
    result['preferred_score'] = finite_number(task['preferred_score'], 'preferred_score')
    result['chosen_score'] = finite_number(task['chosen_score'], 'chosen_score')
    result.update({key.removesuffix('_ns') + '_ms_from_origin': (task[key] - origin) / MS
                   for key in ('submitted_ns', 'start_ns', 'finish_ns')})
    result.update(wait_ms=wait_ns(task) / MS, wall_ms=wall_ns(task) / MS, cpu_ms=task['cpu_ns'] / MS)
    result['admission'] = diagnostics[task['request'], task['tag']]
    result['same_pool_other_callback_wall_during_wait'] = wait_overlap(task, run['tasks'], tags)
    return result


def analyze_tail(run, validated, top):
    summary = run['summary']
    if summary['schema'] != 'affinity-v2' or summary['continuation'] != 'parent':
        raise ValueError('tail audit currently requires the synchronous parent matrix contract')
    diagnostics = validate_callback_diagnostics(run, validated['target_limit'])
    tags = role_map(summary)
    cohort_ids = {row['request'] for row in run['completed_cohort']}
    tasks = [row for row in run['tasks'] if row['request'] in cohort_ids]
    groups = {role: [row for row in tasks if tags[row['tag']]['role'] == role] for role in flow.ROLES}
    roles = flow.task_roles(summary, tasks)
    if any(len(groups[role]) != roles[role]['n'] for role in flow.ROLES):
        raise ValueError('tail audit/flow callback-role disagreement')
    by_request = defaultdict(list)
    for task in tasks:
        by_request[task['request']].append(task)
    requests = []
    for request in sorted(run['completed_cohort'],
                          key=lambda row: (-(row['finished_ns'] - row['scheduled_ns']), row['request']))[:top]:
        selected = by_request[request['request']]
        max_wait = min(selected, key=lambda row: (-wait_ns(row), row['tag']))
        max_wall = min(selected, key=lambda row: (-wall_ns(row), row['tag']))
        requests.append({'request': request['request'],
                         **{key.removesuffix('_ns') + '_ms_from_origin': (request[key] - summary['origin_ns']) / MS
                            for key in ('scheduled_ns', 'submitted_ns', 'finished_ns')},
                         'planned_latency_ms': (request['finished_ns'] - request['scheduled_ns']) / MS,
                         'attempt_lag_ms': (request['submitted_ns'] - request['scheduled_ns']) / MS,
                         'after_attempt_ms': (request['finished_ns'] - request['submitted_ns']) / MS,
                         'callback_count': len(selected),
                         'wait_threshold_counts': [{'wait_gt_ms': threshold,
                                                    'callbacks': sum(wait_ns(row) > threshold * MS for row in selected)}
                                                   for threshold in THRESHOLDS],
                         'max_wait_callback': {'tag': max_wait['tag'], **tags[max_wait['tag']],
                                               'chosen': max_wait['chosen'], 'wait_ms': wait_ns(max_wait) / MS},
                         'max_wall_callback': {'tag': max_wall['tag'], **tags[max_wall['tag']],
                                               'chosen': max_wall['chosen'], 'wall_ms': wall_ns(max_wall) / MS}})
    detail = lambda task: callback_detail(task, run, tags, diagnostics)
    return {'schema': SCHEMA + '-run',
            **{key: validated[key] for key in ('case', 'seed', 'shape', 'target_limit', 'run', 'development',
                                               'resources', 'cohort_seconds', 'build_sha256', 'pairing_sha256',
                                               'work_sha256', 'request_checksums_sha256', 'source_sha256')},
            'origin_ns': summary['origin_ns'], 'cohort_requests': len(cohort_ids),
            'whole_completed_requests': summary['completed'],
            'latency_breakdown': validated['latency_breakdown'],
            'callback_waits': {'all': wait_counts(tasks),
                               'by_role': {role: wait_counts(rows) for role, rows in groups.items()}},
            'top_requests_by_planned_latency': requests,
            # Global top-N can be dominated by first-wave callbacks. Preserve
            # each role's maximum identity so a rare successor tail is not lost.
            'max_wait_callback_by_role': {role: detail(min(rows, key=lambda row: (-wait_ns(row), row['request'], row['tag'])))
                                          if rows else None for role, rows in groups.items()},
            'top_callbacks_by_wait': [detail(task) for task in sorted(tasks, key=lambda row: (-wait_ns(row), row['request'], row['tag']))[:top]],
            'top_callbacks_by_wall': [detail(task) for task in sorted(tasks, key=lambda row: (-wall_ns(row), row['request'], row['tag']))[:top]]}


def audit_matrix(root, warmup=2, window=16, top=3):
    if type(top) is not int or not 1 <= top <= 100:
        raise ValueError('top must be an integer from 1 to 100')
    if (not math.isfinite(warmup) or warmup < 0 or
            (window is not None and (not math.isfinite(window) or window <= 0 or round(window * 1e9) < 1))):
        raise ValueError('invalid planned-arrival window')
    root = Path(root).resolve()
    metadata = json.loads((root / 'metadata.json').read_text())
    plan = json.loads((root / 'plan.json').read_text())
    if type(metadata.get('development')) is not bool:
        raise ValueError('metadata development must be an explicit boolean')
    matrix.validate_plan(root, metadata, plan)
    if not metadata['development'] and (warmup != 2 or window != 16):
        raise ValueError('target audit requires the recorded planned-arrival [2,18) second cohort')
    validated, details = [], []
    for entry in plan:
        # Capture the same load_run result used by the complete matrix validator
        # instead of reading/parsing the task CSV a second time.
        loaded = []
        def load(path, start, duration):
            run = report.load_run(path, start, duration)
            loaded.append(run)
            return run
        row = matrix.summarize_run(entry, metadata, warmup, window,
                                   SimpleNamespace(load_run=load), flow, phase)
        validated.append(row)
        details.append(analyze_tail(loaded[0], row, top))
    pairs = matrix.paired_deltas(validated)
    header = {'schema': SCHEMA + '-report', 'runs': len(details), 'validated_pairs': len(pairs),
              'development': metadata['development'], 'top': top,
              'cohort_seconds': [warmup, None if window is None else warmup + window],
              'wait_thresholds_ms': list(THRESHOLDS), 'threshold_comparison': 'strictly greater than',
              'binary_sha256': metadata['binary_sha256'], 'git_head': metadata['git_head'],
              'metadata_sha256': matrix.digest(root / 'metadata.json'),
              'plan_sha256': matrix.digest(root / 'plan.json'),
              'fixed_options': metadata['fixed_options'],
              'analysis_sources_sha256': {str(path.relative_to(ROOT)): matrix.digest(path) for path in
                                         (Path(__file__).resolve(), MATRIX_PATH,
                                          Path(report.__file__), Path(flow.__file__), Path(phase.__file__))},
              'contract': 'Read-only detail from the existing twelve runs; no new performance samples or censoring. '
                          'Callbacks and top requests use complete planned-arrival cohort records, including late finishes. '
                          'Threshold wait_ms sums full waits of callbacks strictly above each threshold; thresholds are nested. '
                          'All timestamps are relative to each run origin. source is the submission-context pool, '
                          'chosen is actual placement; no inferred preferred-pool column. '
                          'Same-pool overlap includes all recorded requests and counts callback execution wall intervals, '
                          'not on-CPU/runnable time or causally attributed delay. Sum of overlapping callback wall time '
                          'can exceed the wait; union cannot. Callback cpu_ms is the existing callback CPU measurement, '
                          'not CPU time inside its waiting interval. No critical-path or summed-wait latency decomposition.'}
    return [header, *details]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix', type=Path)
    parser.add_argument('--top', type=int, default=3)
    parser.add_argument('--warmup-seconds', type=float, default=2)
    parser.add_argument('--window-seconds', default='16', help='positive duration, or all for development')
    args = parser.parse_args(argv)
    try:
        window = None if args.window_seconds == 'all' else float(args.window_seconds)
        rows = audit_matrix(args.matrix, args.warmup_seconds, window, args.top)
        # Serialize everything before emitting anything, including NaN checks.
        lines = [json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False) for row in rows]
        print('\n'.join(lines))
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'admission tail audit unavailable: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

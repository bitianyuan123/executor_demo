#!/usr/bin/env python3
"""Bound per-request timed CPU service from existing callback traces.

No CPU sampling, simulation or interpolation is performed. Each callback's
thread CPU clock total is located somewhere inside its observer wall interval.
The output preserves that uncertainty, including preemption and blocking.
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location(
    'affinity_shape_flow', Path(__file__).with_name('flow_report.py'))
flow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flow)
MS = 1_000_000
MAX_PROFILE_ROWS = 200_000


def overlap(start, end, task):
    return max(0, min(end, task['finish_ns']) - max(start, task['start_ns']))


def service_bounds(task, start, end):
    """CPU service in [start,end); a sequential callback uses at most one CPU.

    This is an envelope for the measured timed(fn) region, not all CPU used by
    the worker during its enclosing observer interval. Individual-bin extrema
    cannot generally be attained simultaneously across all bins.
    """
    wall = task['finish_ns'] - task['start_ns']
    cpu = task['cpu_ns']
    if not 0 <= cpu <= wall:
        raise ValueError('CPU total must be between zero and observer wall duration; no clipping is permitted')
    length = overlap(start, end, task)
    return max(0, cpu - (wall - length)), min(cpu, length), length


def topology(summary):
    groups = summary['groups']
    pools = []
    cpu_union = set()
    disjoint = True
    for index, group in enumerate(groups):
        cpus = group['cpus']
        if (not cpus or any(type(cpu) is not int or cpu < 0 for cpu in cpus)
                or len(set(cpus)) != len(cpus)):
            raise ValueError('invalid CPU set in summary.groups')
        disjoint &= not bool(cpu_union.intersection(cpus))
        cpu_union.update(cpus)
        pools.append({'pool': index, 'cpus': cpus, 'numa': group['numa'],
                      'logical_cpu_count': len(cpus), 'configured_score_capacity': group['capacity']})
    if not pools:
        raise ValueError('summary.groups is empty')
    physical = summary['affinity'] == 'cluster' and disjoint
    return {'pools': pools, 'union_logical_cpu_count': len(cpu_union),
            'union_cpus': sorted(cpu_union), 'disjoint_cpu_sets': disjoint,
            'pool_service_maps_to_declared_cluster': physical,
            'attribution': ('chosen pool maps to its declared CPU set; hardware cluster identity is not independently verified'
                            if physical else 'chosen-pool attribution only; not physical-cluster utilization')}


def validate_tasks(tasks):
    seen = set()
    for task in tasks:
        for key in ('request', 'tag', 'chosen', 'submitted_ns', 'start_ns', 'finish_ns', 'cpu_ns'):
            if type(task[key]) is not int or task[key] < 0:
                raise ValueError(f'invalid task {key}')
        if not task['submitted_ns'] <= task['start_ns'] <= task['finish_ns']:
            raise ValueError('invalid callback timestamps')
        if (task['request'], task['tag']) in seen:
            raise ValueError('duplicate callback identity')
        seen.add((task['request'], task['tag']))
        service_bounds(task, task['start_ns'], task['finish_ns'])


def role_totals(summary, tasks):
    # Reuse the versioned tag/kind contract, including detached RPC resumes.
    roles = flow.task_roles(summary, tasks)
    result = {}
    for role, values in roles.items():
        result[role] = {'callbacks': values['n'],
                        'cpu_ms': (values['cpu_ms'] or 0) * values['n']}
    return result


def cumulative_crossing_bounds(tasks, origin, end, fraction):
    """First crossing interval of cumulative measured CPU, never a uniform smear."""
    total = sum(task['cpu_ns'] for task in tasks)
    if total == 0:
        return None
    target = math.ceil(total * fraction)

    def first_crossing(bound_index):
        low, high = origin, end
        while low < high:
            mid = (low + high) // 2
            value = sum(service_bounds(task, origin, mid)[bound_index] for task in tasks)
            if value >= target:
                high = mid
            else:
                low = mid + 1
        return (low - origin) / MS

    return {'earliest_possible_ms': first_crossing(1),
            'latest_guaranteed_ms': first_crossing(0)}


def request_summary(summary, request, tasks):
    selected = [task for task in tasks if task['request'] == request['request']]
    if request['status'] != 'completed' or not selected:
        raise ValueError('request profile requires a completed request with callback records')
    validate_tasks(selected)
    roles = role_totals(summary, selected)
    start = request['submitted_ns']
    end = max(request['finished_ns'], max(task['finish_ns'] for task in selected))
    cpu = sum(task['cpu_ns'] for task in selected)
    operator_cpu = sum(task['cpu_ns'] for task in selected if task['kind'] == 'operator')
    first_wave_end_tag = 1 + summary['widths'][0] * summary['depths'][0]
    first_wave_cpu = sum(task['cpu_ns'] for task in selected
                         if task['kind'] == 'operator' and 1 <= task['tag'] < first_wave_end_tag)
    # Stage0 and all first-wave depths answer different questions.
    first_stage_cpu = roles['first-wave stage0']['cpu_ms'] * MS
    span = end - start
    return {'request': request['request'], 'status': request['status'],
            'scheduled_ns': request['scheduled_ns'], 'submitted_ns': start,
            'request_finished_ns': request['finished_ns'], 'profile_end_ns': end,
            'planned_latency_ms': (request['finished_ns'] - request['scheduled_ns']) / MS,
            'attempt_to_request_finish_ms': (request['finished_ns'] - start) / MS,
            'profile_span_ms': span / MS,
            'last_callback_after_request_finish_ms': (end - request['finished_ns']) / MS,
            'callbacks': len(selected), 'timed_cpu_ms': cpu / MS,
            'operator_timed_cpu_ms': operator_cpu / MS,
            'sum_callback_wall_ms': sum(task['finish_ns'] - task['start_ns'] for task in selected) / MS,
            'timed_cpu_average_cores_over_profile': cpu / span if span else None,
            'first_wave_stage0_cpu_fraction_of_all_callbacks': first_stage_cpu / cpu if cpu else None,
            'first_wave_all_depths_operator_cpu_fraction': first_wave_cpu / operator_cpu if operator_cpu else None,
            'roles': roles,
            'cumulative_timed_cpu_crossing': {
                str(percent): cumulative_crossing_bounds(selected, start, end, percent / 100)
                for percent in (50, 90, 99)}}


def profile_rows(summary, request, tasks, bin_ns):
    selected = [task for task in tasks if task['request'] == request['request']]
    info = request_summary(summary, request, selected)
    resource = topology(summary)
    start, end = info['submitted_ns'], info['profile_end_ns']
    if type(bin_ns) is not int or bin_ns <= 0:
        raise ValueError('bin duration must be a positive integer nanosecond count')
    expected = math.ceil((end - start) / bin_ns) * (len(resource['pools']) + 1)
    if expected > MAX_PROFILE_ROWS:
        raise ValueError(f'profile exceeds {MAX_PROFILE_ROWS} rows; increase bin-ms or select fewer requests')
    by_pool = {str(pool['pool']): [task for task in selected if task['chosen'] == pool['pool']]
               for pool in resource['pools']}
    by_pool['all'] = selected
    capacities = {str(pool['pool']): pool['logical_cpu_count'] for pool in resource['pools']}
    capacities['all'] = resource['union_logical_cpu_count']
    for left in range(start, end, bin_ns):
        right = min(end, left + bin_ns)
        duration = right - left
        for pool, pool_tasks in by_pool.items():
            bounds = [service_bounds(task, left, right) for task in pool_tasks]
            lower, upper, wall = (sum(row[index] for row in bounds) for index in range(3))
            yield {'request': request['request'], 'pool': pool,
                   'relative_start_ms': (left - start) / MS,
                   'relative_end_ms': (right - start) / MS,
                   'absolute_start_ns': left, 'absolute_end_ns': right,
                   'timed_cpu_lower_ns': lower, 'timed_cpu_upper_ns': upper,
                   'timed_cpu_lower_cores': lower / duration,
                   'timed_cpu_upper_cores': upper / duration,
                   'declared_logical_cpus': capacities[pool],
                   'timed_cpu_lower_pct_of_declared_cpus': 100 * lower / duration / capacities[pool],
                   'timed_cpu_upper_pct_of_declared_cpus': 100 * upper / duration / capacities[pool],
                   'callback_wall_occupancy_cores': wall / duration,
                   'overlapping_callbacks': sum(value[2] > 0 for value in bounds)}


def write_csv(path, rows):
    rows = iter(rows)
    first = next(rows, None)
    if first is None:
        raise ValueError('refusing to write an empty request profile')
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=first.keys(), lineterminator='\n')
        writer.writeheader()
        writer.writerow(first)
        writer.writerows(rows)


def analyze(run, selected_ids, bin_ns):
    summary = run['summary']
    if not summary['trace']:
        raise ValueError('trace=0 has no per-callback CPU totals; rerun is not performed by this tool')
    validate_tasks(run['tasks'])
    complete = [row for row in run['requests'] if row['status'] == 'completed']
    if not complete:
        raise ValueError('run has no completed requests')
    if selected_ids is None:
        selected_ids = [max(complete, key=lambda row: (row['finished_ns'] - row['scheduled_ns'], -row['request']))['request']]
        selection = 'largest planned latency among completed requests; deterministic extreme case, not representative sampling'
    else:
        selection = 'explicit request IDs; no representativeness claim'
    by_request = {row['request']: row for row in complete}
    if len(set(selected_ids)) != len(selected_ids) or any(value not in by_request for value in selected_ids):
        raise ValueError('request IDs must be unique and refer to completed requests')
    all_shapes = [request_summary(summary, row, run['tasks']) for row in complete]
    selected = [row for row in all_shapes if row['request'] in selected_ids]
    output = {'schema': 'affinity_request_cpu_shape_v1', 'selection': selection,
              'bin_ms': bin_ns / MS, 'selected_request_ids': selected_ids,
              'run': str(run['path']), 'resource': topology(summary),
              'completed_requests': len(complete),
              'other_request_statuses': {status: sum(row['status'] == status for row in run['requests'])
                                         for status in sorted({row['status'] for row in run['requests']} - {'completed'})},
              'completed_request_statistics': {
                  key: flow.distribution([row[key] for row in all_shapes if row[key] is not None])
                  for key in ('planned_latency_ms', 'profile_span_ms', 'timed_cpu_ms',
                              'timed_cpu_average_cores_over_profile',
                              'first_wave_stage0_cpu_fraction_of_all_callbacks',
                              'first_wave_all_depths_operator_cpu_fraction')},
              'measurement_contract': {
                  'quantity': 'thread CPU time of timed(fn), bounded inside observer start/finish wall interval',
                  'formula': 'overlap=l, callback wall=W, CPU=C: lower=max(0,C-(W-l)); upper=min(C,l)',
                  'uncertainty': 'no uniform CPU allocation; bounds are independently valid per bin and not necessarily jointly attainable',
                  'wall_occupancy': 'callbacks entered but not returned, including preemption and sleeping; NOT on-CPU or runnable count',
                  'exclusions': 'CPU outside timed(fn), executor bookkeeping, producer/AIO threads and unrelated processes are not measured',
                  'clock_assumption': '0<=C<=W and sequential callback CPU rate<=1; every C>W is rejected without clipping',
                  'percent': 'measured CPU service share of declared logical CPU count; NOT full machine/cluster CPU utilization',
                  'cumulative': '50/90/99% are earliest/latest cumulative timed CPU service crossings, not callback completion percentiles',
                  'profile_extent': 'request attempt through max(request finish,last callback finish); includes observer tail after logical completion'},
              'selected_requests': selected}
    return output, all_shapes, by_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--request', action='append', nargs='+', help='completed request IDs; spaces or commas; repeatable')
    parser.add_argument('--bin-ms', type=float, default=1)
    parser.add_argument('--output', required=True, type=Path, help='new output directory; existing directories are rejected')
    args = parser.parse_args()
    try:
        if not math.isfinite(args.bin_ms) or args.bin_ms <= 0 or round(args.bin_ms * MS) < 1:
            raise ValueError('bin-ms must be finite, positive and at least one nanosecond')
        ids = None if args.request is None else [int(item) for group in args.request for text in group for item in text.split(',')]
        run = flow.report.load_run(args.run)
        result, shapes, requests = analyze(run, ids, round(args.bin_ms * MS))
        result['source_sha256'] = {name: hashlib.sha256((args.run / name).read_bytes()).hexdigest()
                                   for name in ('summary.json', 'requests.csv', 'tasks.csv')}
        # Validate the selected profiles before creating output. Their row
        # budgets are bounded independently, and by a combined cap below.
        expected_rows = sum(math.ceil((row['profile_end_ns'] - row['submitted_ns']) / round(args.bin_ms * MS))
                            * (len(run['summary']['groups']) + 1) for row in result['selected_requests'])
        if expected_rows > MAX_PROFILE_ROWS:
            raise ValueError('combined profiles exceed row budget; select fewer requests')
        profiles = {request: list(profile_rows(run['summary'], requests[request], run['tasks'], round(args.bin_ms * MS)))
                    for request in result['selected_request_ids']}
        args.output.mkdir(parents=True, exist_ok=False)
        for request, rows in profiles.items():
            write_csv(args.output / f'request_{request}_profile.csv', rows)
        flattened = []
        for shape in shapes:
            row = {key: value for key, value in shape.items() if key not in ('roles', 'cumulative_timed_cpu_crossing')}
            for percent, bounds in shape['cumulative_timed_cpu_crossing'].items():
                for bound in ('earliest_possible_ms', 'latest_guaranteed_ms'):
                    row[f'cpu_{percent}_{bound}'] = None if bounds is None else bounds[bound]
            flattened.append(row)
        write_csv(args.output / 'requests_shape.csv', flattened)
        (args.output / 'summary.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
        print('REQUEST_CPU_SHAPE ' + json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        print('Per-bin files contain CPU service bounds, not measured scheduler on-CPU curves; no cpu_ns was spread uniformly.')
        print(f'Wrote {len(shapes)} completed-request summaries and {len(profiles)} detailed profiles to {args.output}')
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity request CPU shape: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

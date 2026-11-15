#!/usr/bin/env python3
"""Describe request backlog, first-fork callbacks and submitted-task load samples.

Uses report.load_run for the existing raw-data audit. This is descriptive
diagnosis, not a steady-state test, causal regression or on-CPU trace analysis.
"""

import argparse
import bisect
import csv
import importlib.util
import math
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location('affinity_report', Path(__file__).with_name('report.py'))
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)
LOAD_FIELDS = {'Q': 'queue_before', 'I': 'callback_inflight_before', 'O': 'outstanding_before'}
LOAD_BOUNDS = (0, .5, 1, 2, 4, 8)
LOAD_LABELS = ('0', '(0,.5]', '(.5,1]', '(1,2]', '(2,4]', '(4,8]', '(8,inf)')


def distribution(values):
    ordered = sorted(values)
    quantile = lambda fraction: ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None
    return {'n': len(values), 'mean': report.mean(values),
            'p50': quantile(.50), 'p95': quantile(.95), 'p99': quantile(.99)}


def callback_bins(tasks, start, end, width):
    """One interval sweep: O(T log T + bins), including crossing callbacks.

    Simultaneous ends/starts are applied together, so adjacent callbacks do not
    create a spurious instantaneous peak. Zero-length intervals contribute zero.
    """
    starts, ends = [], []
    for task in tasks:
        left, right = max(start, task['start_ns']), min(end, task['finish_ns'])
        if left < right:
            starts.append(left)
            ends.append(right)
    starts.sort()
    ends.sort()
    si = ei = active = 0
    result = []
    for left in range(start, end, width):
        right = min(end, left + width)
        now, area, peak = left, 0, 0
        while now < right:
            while ei < len(ends) and ends[ei] == now:
                active -= 1
                ei += 1
            while si < len(starts) and starts[si] == now:
                active += 1
                si += 1
            peak = max(peak, active)
            following = min(right, starts[si] if si < len(starts) else right,
                            ends[ei] if ei < len(ends) else right)
            area += active * (following - now)
            now = following
        result.append({'mean': area / (right - left), 'peak': peak})
    return result


def request_trend(requests, tasks, start, end, width):
    """Backlog at a boundary is the state immediately before its timestamp.

    Requests scheduled before the window and finishing within/after it remain
    in backlog. Lag is grouped by scheduled arrival, retaining late submissions.
    Completed/rejected counts are grouped by actual terminal timestamp.
    """
    scheduled = sorted(row['scheduled_ns'] for row in requests)
    submitted = sorted(row['submitted_ns'] for row in requests)
    finished = sorted(row['finished_ns'] for row in requests)
    completed = sorted(row['finished_ns'] for row in requests if row['status'] == 'completed')
    failed = sorted(row['finished_ns'] for row in requests if row['status'] != 'completed')
    size = (end - start + width - 1) // width
    lag = [[] for _ in range(size)]
    for row in requests:
        if start <= row['scheduled_ns'] < end:
            lag[(row['scheduled_ns'] - start) // width].append(
                (row['submitted_ns'] - row['scheduled_ns']) / 1e6)
    callbacks = callback_bins(tasks, start, end, width)
    count = lambda values, lo, hi: bisect.bisect_left(values, hi) - bisect.bisect_left(values, lo)
    backlog = lambda at: bisect.bisect_left(scheduled, at) - bisect.bisect_left(finished, at)
    rows = []
    for index, left in enumerate(range(start, end, width)):
        right = min(end, left + width)
        rows.append({'start_ns': left, 'end_ns': right,
                     'arrivals': count(scheduled, left, right),
                     'submission_attempts': count(submitted, left, right),
                     'completed': count(completed, left, right),
                     'failed_or_rejected': count(failed, left, right),
                     'backlog_left': backlog(left), 'backlog_right': backlog(right),
                     'lag': distribution(lag[index]), 'callbacks': callbacks[index]})
    return rows


def first_fork(run, start, end):
    """Only first-wave stage-0 tags (1..width[0]); never infer an arbitrary DAG."""
    summary = run['summary']
    unavailable = {'status': 'unavailable', 'reason': 'trace/tag/width contract unavailable',
                   'rows': [], 'incomplete_requests': 0, 'noncompleted_requests': 0}
    if not summary.get('trace'):
        return {**unavailable, 'reason': 'trace=0'}
    widths, depths = summary.get('widths'), summary.get('depths')
    if (summary.get('schema') != 'affinity-v2' or not isinstance(widths, list)
            or not isinstance(depths, list) or not widths or len(widths) != len(depths)
            or any(not isinstance(value, int) or value <= 0 for value in widths + depths)
            or summary.get('nodes_per_request') != 1 + sum(w * d + 1 for w, d in zip(widths, depths))):
        return unavailable
    selected = {row['request']: row for row in run['requests'] if start <= row['scheduled_ns'] < end}
    roots, operators = {}, {}
    width = widths[0]
    for task in run['tasks']:
        request = task['request']
        if request not in selected:
            continue
        if task['tag'] == 0 and task['kind'] == 'init':
            roots[request] = task
        elif 1 <= task['tag'] <= width:
            if task['kind'] != 'operator':
                return unavailable
            operators.setdefault(request, {})[task['tag']] = task
    rows, incomplete, noncompleted = [], 0, 0
    for request, record in selected.items():
        if record['status'] != 'completed':
            noncompleted += 1
            continue
        tasks = operators.get(request, {})
        if request not in roots or len(tasks) != width:
            incomplete += 1
            continue
        tasks = list(tasks.values())
        submitted = min(row['submitted_ns'] for row in tasks)
        first_start = min(row['start_ns'] for row in tasks)
        last_finish = max(row['finish_ns'] for row in tasks)
        if last_finish <= first_start:
            incomplete += 1
            continue
        occupancy = callback_bins(tasks, first_start, last_finish, last_finish - first_start)[0]
        home_tasks = [row for row in tasks if row['chosen'] == roots[request]['home']]
        home = callback_bins(home_tasks, first_start, last_finish, last_finish - first_start)[0]
        rows.append({'request': request,
                     'recorded_submit_span_ms': (max(row['submitted_ns'] for row in tasks) - submitted) / 1e6,
                     'all_started_after_first_submit_ms': (max(row['start_ns'] for row in tasks) - submitted) / 1e6,
                     'stage0_callback_span_ms': (last_finish - first_start) / 1e6,
                     'callback_mean': occupancy['mean'], 'callback_peak': occupancy['peak'],
                     'home_pool_callback_peak': home['peak']})
    return {'status': 'available' if rows else 'unavailable',
            'reason': '' if rows else 'no complete identifiable first fork in scheduled-request window',
            'rows': rows, 'incomplete_requests': incomplete, 'noncompleted_requests': noncompleted}


def load_conditions(tasks, groups, start, end, available_fields):
    """Operator samples in the post-choice submission window, not time samples."""
    buckets = {signal: [[] for _ in LOAD_LABELS] for signal, field in LOAD_FIELDS.items()
               if field in available_fields}
    for task in tasks:
        if task['kind'] != 'operator' or not start <= task['submitted_ns'] < end:
            continue
        capacity = float(groups[task['chosen']]['capacity'])
        for signal in buckets:
            normalized = task[LOAD_FIELDS[signal]] / capacity
            buckets[signal][bisect.bisect_left(LOAD_BOUNDS, normalized)].append(task)
    result = {}
    for signal, bins in buckets.items():
        result[signal] = []
        for label, rows in zip(LOAD_LABELS, bins):
            if not rows:
                continue
            result[signal].append({'bucket': label, 'n': len(rows),
                                  'requests': len({row['request'] for row in rows}),
                                  'pools': len({row['chosen'] for row in rows}),
                                  'wait_ms': distribution([(row['start_ns'] - row['submitted_ns']) / 1e6 for row in rows]),
                                  'wall_ms': distribution([(row['finish_ns'] - row['start_ns']) / 1e6 for row in rows])})
    return result


def fmt(value):
    return '-' if value is None else f'{value:.3f}'


def print_diagnosis(run, start, end, width, min_tail_samples, available_fields):
    summary = run['summary']
    origin = summary['origin_ns']
    print(f'\nRUN {run["path"]}: pool_count={len(summary["groups"])}, affinity={summary["affinity"]}, '
          f'threads/pool={summary["threads_per_pool"]}, policy={summary["policy"]}')
    context = run['matrix_context']
    print(f'Matrix suite={context["suite"]}, experiment_role={context["experiment_role"]}, '
          f'research_target={context["research_target"]}; declared provenance, not physical-cluster certification.')
    print('Chosen-pool capacities: ' + ', '.join(f'{i}={group["capacity"]}' for i, group in enumerate(summary['groups'])))
    print(f'Wall window relative to origin: [{(start-origin)/1e9:.6f}, {(end-origin)/1e9:.6f}) s; '
          f'bin={width/1e6:.3f} ms. Late submissions/completions are retained.')
    print('Request backlog = scheduled arrivals without a terminal outcome, measured immediately before each boundary; '
          'this includes generator lag and is NOT the Folly queue. Submission counts include rejected attempts.')
    print('| seconds [lo,hi) | scheduled | submit attempts | completed | failed/rejected | backlog left/right | '
          'scheduled-cohort lag p50/p99 ms | all-overlapping callback mean/peak |')
    for row in request_trend(run['requests'], run['tasks'], start, end, width):
        occupancy = (f'{row["callbacks"]["mean"]:.3f}/{row["callbacks"]["peak"]}' if summary['trace'] else 'unavailable')
        print(f'| {(row["start_ns"]-origin)/1e9:.6f},{(row["end_ns"]-origin)/1e9:.6f} | '
              f'{row["arrivals"]} | {row["submission_attempts"]} | {row["completed"]} | {row["failed_or_rejected"]} | '
              f'{row["backlog_left"]}/{row["backlog_right"]} | {fmt(row["lag"]["p50"])}/{fmt(row["lag"]["p99"])} | {occupancy} |')
    last_arrival = max((row['scheduled_ns'] for row in run['requests']), default=origin)
    last_finish = max((row['finished_ns'] for row in run['requests']), default=origin)
    print(f'Whole-record last scheduled arrival/terminal: {(last_arrival-origin)/1e9:.6f}/{(last_finish-origin)/1e9:.6f} s; '
          'a finite run draining to zero does NOT establish steady state. No automatic stability verdict.')
    if int(summary['failed_or_rejected']):
        print('Failed/rejected requests are present: retain them in arrival/backlog accounting; this is not an eligible performance comparison.')

    fork = first_fork(run, start, end)
    print('First fork = first-wave stage-0 operators, for requests scheduled in this window; '
          'full callback intervals are retained even after the window.')
    print('Submission points occur AFTER choose/reserve/diagnostic reads; their span is not total fork scheduling CPU overhead. '
          'Callback concurrency includes preemption/sleep and cannot prove 16 CPUs on-CPU for 30 ms.')
    if fork['status'] == 'unavailable':
        print('First fork unavailable: ' + fork['reason'])
    else:
        print(f'First forks n={len(fork["rows"])}, incomplete={fork["incomplete_requests"]}, '
              f'noncompleted skipped={fork["noncompleted_requests"]}; request-weighted descriptive distributions:')
        if len(fork['rows']) < min_tail_samples:
            print(f'  LOW tail samples: n<{min_tail_samples}; nominal top 1%={len(fork["rows"])*.01:.2f} requests.')
        print('  Request/burst dependence remains; no independence or confidence-interval guarantee.')
        for key in fork['rows'][0]:
            if key == 'request':
                continue
            values = distribution([row[key] for row in fork['rows']])
            print(f'  {key}: mean/p50/p95/p99={fmt(values["mean"])}/{fmt(values["p50"])}/{fmt(values["p95"])}/{fmt(values["p99"])}')

    if not summary['trace']:
        print('trace=0: task load samples and callback concurrency unavailable.')
        return
    print('Load conditions: Q=Folly queue guess, I=callback inflight, O=outstanding; divide by CHOSEN pool nominal capacity. '
          'Snapshots are post-selection, so routing creates selection bias; these are operator-submission samples, not time-weighted load.')
    print('Pools and operator costs are mixed within this run; normalized capacity does not remove those confounders. '
          'Tasks from one request are dependent. Do not regress these task samples against request p99 or interpret them causally.')
    conditions = load_conditions(run['tasks'], summary['groups'], start, end, available_fields)
    for signal in LOAD_FIELDS:
        if signal not in conditions:
            print(f'  {signal}: unavailable (field absent in original tasks.csv)')
    print('| load/capacity | tasks/unique requests/unique pools | post-choice submit-to-callback-start p50/p95/p99 ms | '
          'callback wall p50/p95/p99 ms | tail screen |')
    for signal, bins in conditions.items():
        for row in bins:
            waits, wall = row['wait_ms'], row['wall_ms']
            screen = f'LOW n<{min_tail_samples}' if row['n'] < min_tail_samples else 'not an independence/CI guarantee'
            print(f'| {signal} {row["bucket"]} | {row["n"]}/{row["requests"]}/{row["pools"]} | '
                  + '/'.join(fmt(waits[key]) for key in ('p50', 'p95', 'p99')) + ' | '
                  + '/'.join(fmt(wall[key]) for key in ('p50', 'p95', 'p99'))
                  + f' | {screen}; nominal top 1%={row["n"]*.01:.2f} |')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--warmup-seconds', type=float, default=0)
    parser.add_argument('--window-seconds', type=float)
    parser.add_argument('--bin-ms', type=float, help='explicit bin width; otherwise >=1000 ms, coarsened to max-bins')
    parser.add_argument('--max-bins', type=int, default=20)
    parser.add_argument('--min-tail-samples', type=int, default=1000)
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error('warmup-seconds must be finite and nonnegative')
    if not 1 <= args.max_bins <= 1000 or args.min_tail_samples < 1:
        parser.error('max-bins must be 1..1000 and min-tail-samples must be positive')
    for name, value in (('window-seconds', args.window_seconds), ('bin-ms', args.bin_ms)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f'{name} must be finite and positive')
    try:
        for path in args.runs:
            run = report.load_run(path, args.warmup_seconds, args.window_seconds)
            start = run['summary']['origin_ns'] + round(args.warmup_seconds * 1e9)
            if args.window_seconds is None:
                end = max([row['finished_ns'] for row in run['requests']]
                          + [row['finish_ns'] for row in run['tasks']] + [start]) + 1
            else:
                end = start + max(1, round(args.window_seconds * 1e9))
            width = (max(1, round(args.bin_ms * 1e6)) if args.bin_ms is not None
                     else max(1000000000, (end - start + args.max_bins - 1) // args.max_bins))
            if (end - start + width - 1) // width > args.max_bins:
                raise ValueError('explicit bins exceed max-bins; shorten the window or increase bin-ms/max-bins')
            fields = set()
            if run['summary']['trace']:
                with (path / 'tasks.csv').open(newline='') as stream:
                    fields = set(csv.DictReader(stream).fieldnames or [])
            print_diagnosis(run, start, end, width, args.min_tail_samples, fields)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity diagnose: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

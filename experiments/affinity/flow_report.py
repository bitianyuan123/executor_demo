#!/usr/bin/env python3
"""Read-only arrival and callback-role diagnosis for audited affinity-v2 runs.

No new hot-path instrumentation is needed. All cohorts use planned request
arrival, retaining late attempts/completions and their complete task records.
"""

import argparse
from collections import Counter
import importlib.util
import math
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location('affinity_report', Path(__file__).with_name('report.py'))
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)
ROLES = ('init', 'first-wave stage0', 'later-wave stage0', 'ordinary successors', 'join')
# affinity-v2 writes int(RequestExecutor::Reason) to tasks.csv. Preserve the
# enum order; the test checks this contract against RequestExecutor.h.
ROUTING_REASONS = ('Home', 'BelowBusyThreshold', 'ReturnHome', 'LocalSpill',
                   'CrossNumaSpill', 'InsufficientGain', 'HopBudget',
                   'PinnedHome', 'NonFork')


def routing_reason(row):
    value = row['reason']
    if type(value) is not int or value < 0:
        raise ValueError('routing reason must be a nonnegative integer')
    # choose() only enters ReturnHome when preferred != home, and preferred is
    # the submission source for a child. returnLow <= busyHigh means it then
    # returns that choice without another spill. A source already at home
    # cannot legitimately have this reason in the current v2 contract.
    if value == 2 and not (row['source'] != row['home'] and row['chosen'] == row['home']):
        raise ValueError('ReturnHome reason requires source!=home and chosen==home')
    return ROUTING_REASONS[value] if value < len(ROUTING_REASONS) else f'Unknown({value})'


def distribution(values):
    ordered = sorted(values)
    quantile = lambda fraction: ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None
    return {'n': len(ordered), 'mean': report.mean(ordered), 'p50': quantile(.5),
            'p95': quantile(.95), 'p99': quantile(.99), 'max': ordered[-1] if ordered else None}


def latency_breakdown(cohort):
    """Only arithmetic means decompose; quantiles are reported independently."""
    completed = [row for row in cohort if row['status'] == 'completed']
    return {'n': len(completed),
            'statuses': dict(sorted(Counter(row['status'] for row in cohort).items())),
            'planned_ms': distribution([(row['finished_ns'] - row['scheduled_ns']) / 1e6 for row in completed]),
            'lag_ms': distribution([(row['submitted_ns'] - row['scheduled_ns']) / 1e6 for row in completed]),
            'after_attempt_ms': distribution([(row['finished_ns'] - row['submitted_ns']) / 1e6 for row in completed])}


def burst_summary(requests, burst_size, start, end):
    """Classify only groups intersecting the planned-arrival window.

    An incomplete final group is counted separately, even if the window also
    cuts it. Full-size boundary-cut groups never contribute span statistics.
    Timing covers all attempted members, including failures/rejections.
    """
    if type(burst_size) is not int or burst_size < 1:
        raise ValueError('burst_size must be a positive integer')
    groups = {}
    for row in requests:
        groups.setdefault(row['request'] // burst_size, []).append(row)
    complete, boundary, trailing = [], 0, 0
    inside = lambda row: row['scheduled_ns'] >= start and (end is None or row['scheduled_ns'] < end)
    for rows in groups.values():
        selected = sum(inside(row) for row in rows)
        if not selected:
            continue
        if len(rows) != burst_size:
            trailing += 1
        elif selected != burst_size:
            boundary += 1
        else:
            complete.append(rows)
    return {'complete': len(complete), 'boundary_cut': boundary, 'trailing_partial': trailing,
            'failed_or_rejected_members': sum(row['status'] != 'completed' for rows in complete for row in rows),
            'planned_span_ms': distribution([(max(row['scheduled_ns'] for row in rows) - min(row['scheduled_ns'] for row in rows)) / 1e6 for rows in complete]),
            'attempt_span_ms': distribution([(max(row['submitted_ns'] for row in rows) - min(row['submitted_ns'] for row in rows)) / 1e6 for rows in complete]),
            'first_attempt_lag_ms': distribution([(min(row['submitted_ns'] for row in rows) - min(row['scheduled_ns'] for row in rows)) / 1e6 for rows in complete])}


def task_roles(summary, tasks):
    """Partition exact affinity-v2 tags; never infer arbitrary DAG semantics."""
    if not summary['trace']:
        return None
    widths, depths = summary['widths'], summary['depths']
    if (summary['schema'] != 'affinity-v2' or not isinstance(widths, list)
            or not isinstance(depths, list) or not widths or len(widths) != len(depths)
            or any(type(value) is not int or value <= 0 for value in widths + depths)):
        raise ValueError('unsupported affinity-v2 role/tag contract')
    tags, offset = {0: ('init', 'init')}, 1
    for wave, (width, depth) in enumerate(zip(widths, depths)):
        for stage in range(depth):
            role = 'ordinary successors' if stage else ('later-wave stage0' if wave else 'first-wave stage0')
            for branch in range(width):
                tags[offset + stage * width + branch] = (role, 'operator')
        tags[offset + width * depth] = ('join', 'join')
        offset += width * depth + 1
    if summary['nodes_per_request'] != offset:
        raise ValueError('affinity-v2 role node count mismatch')
    groups = {role: [] for role in ROLES}
    for row in tasks:
        if row['tag'] not in tags or tags[row['tag']][1] != row['kind']:
            raise ValueError('affinity-v2 task kind/tag mismatch')
        groups[tags[row['tag']][0]].append(row)
    output = {}
    for role, rows in groups.items():
        cpu = [row['cpu_ns'] / 1e6 for row in rows if row.get('cpu_ns') is not None]
        reasons = Counter(routing_reason(row) for row in rows)
        output[role] = {'n': len(rows),
                        'migrations': sum(row['source'] != row['chosen'] for row in rows),
                        'return_home': sum(row['source'] != row['home'] and row['chosen'] == row['home'] for row in rows),
                        'routing_reasons': dict(sorted(reasons.items())),
                        'wait_ms': report.mean([(row['start_ns'] - row['submitted_ns']) / 1e6 for row in rows]),
                        'cpu_ms': report.mean(cpu) if len(cpu) == len(rows) else None,
                        'wall_ms': report.mean([(row['finish_ns'] - row['start_ns']) / 1e6 for row in rows])}
    if sum(row['n'] for row in output.values()) != len(tasks):
        raise ValueError('task-role conservation mismatch')
    return output


def number(value):
    return 'unknown' if value is None else f'{value:.3f}'


def distribution_text(item):
    return '/'.join(number(item[key]) for key in ('mean', 'p50', 'p95', 'p99', 'max'))


def print_flow(run, warmup_seconds, window_seconds):
    summary = run['summary']
    start = summary['origin_ns'] + round(warmup_seconds * 1e9)
    end = None if window_seconds is None else start + round(window_seconds * 1e9)
    results = latency_breakdown(run['cohort'])
    ids = {row['request'] for row in run['completed_cohort']}
    tasks = [row for row in run['tasks'] if row['request'] in ids]
    roles = task_roles(summary, tasks)
    if roles is not None and len(tasks) != len(ids) * summary['nodes_per_request']:
        raise ValueError('completed-cohort role conservation mismatch')
    print(f'\nRUN {run["path"]}: policy={summary["policy"]}, continuation={summary["continuation"]}, model={summary["model"]}')
    print(f'Resources: pools={len(summary["groups"])}, affinity={summary["affinity"]}, threads/pool={summary["threads_per_pool"]}, groups={summary["groups"]}')
    print(f'Arrival={summary.get("arrival", "unknown")}; planned cohort [{warmup_seconds:g}, {"end" if window_seconds is None else f"{warmup_seconds + window_seconds:g}"}) s; completed/offered={results["n"]}/{len(run["cohort"])}; statuses={results["statuses"]}')
    print('submitted_ns is the driver attempt timestamp BEFORE request construction/root add; it is not root enqueue time. Late outcomes are retained.')
    print(f'Completed means ms: planned latency={number(results["planned_ms"]["mean"])} = attempt lag {number(results["lag_ms"]["mean"])} + attempt-to-finish {number(results["after_attempt_ms"]["mean"])} (rounding may differ).')
    print(f'Completed p99 ms, separately: planned={number(results["planned_ms"]["p99"])}, lag={number(results["lag_ms"]["p99"])}, attempt-to-finish={number(results["after_attempt_ms"]["p99"])}; quantiles must NOT be subtracted. n={results["n"]}; no tail-independence/CI guarantee.')
    print('Failed/rejected requests are listed in statuses and excluded from successful latency/role statistics; comparisons must retain their failure counts.')
    if summary.get('arrival') == 'burst':
        bursts = burst_summary(run['requests'], summary['burst_size'], start, end)
        print(f'Burst groups intersecting cohort: complete={bursts["complete"]}, boundary-cut={bursts["boundary_cut"]}, trailing-partial={bursts["trailing_partial"]}; B={summary["burst_size"]}; failed/rejected members in complete groups={bursts["failed_or_rejected_members"]}.')
        print('Complete groups only: all B planned arrivals inside window; all actual attempts retained, including failures/rejections and attempts after window end.')
        for key in ('planned_span_ms', 'attempt_span_ms', 'first_attempt_lag_ms'):
            print(f'  {key} mean/p50/p95/p99/max={distribution_text(bursts[key])}')
        print('A common shift increases first-attempt lag without widening the burst. Lag is NOT span; these observations do not identify delay causes.')
    else:
        print('Burst span analysis: not applicable (requires declared burst arrival contract).')
    if roles is None:
        print('Callback roles unavailable: trace=0.')
    else:
        print('Roles: completed planned-request cohort, all callback records including late ones. source is submission context; migration is routing, not a kernel CPU migration. ReturnHome is a chooser reason; routing back to home can also have other reasons.')
        print('| role | n | source!=chosen | source!=home && chosen==home | wait mean ms | CPU mean ms | wall mean ms | routing reasons |')
        for role, row in roles.items():
            reasons = ', '.join(f'{reason}:{count}' for reason, count in row['routing_reasons'].items()) or '-'
            print(f'| {role} | {row["n"]} | {row["migrations"]} | {row["return_home"]} | {number(row["wait_ms"])} | {number(row["cpu_ms"])} | {number(row["wall_ms"])} | {reasons} |')
        print(f'Role conservation: {sum(row["n"] for row in roles.values())} = {len(ids)} completed requests x {summary["nodes_per_request"]} callbacks; wait starts after routing selection, CPU/wall includes successor submission. Ordinary successors have depth>0; joins are separate.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--warmup-seconds', type=float, default=0)
    parser.add_argument('--window-seconds', type=float)
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error('warmup-seconds must be finite and nonnegative')
    if args.window_seconds is not None and (not math.isfinite(args.window_seconds) or args.window_seconds <= 0 or round(args.window_seconds * 1e9) < 1):
        parser.error('window-seconds must be finite, positive and at least one nanosecond')
    try:
        for path in args.runs:
            print_flow(report.load_run(path, args.warmup_seconds, args.window_seconds),
                       args.warmup_seconds, args.window_seconds)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity flow report: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

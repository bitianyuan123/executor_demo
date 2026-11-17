#!/usr/bin/env python3
"""Bounded, read-only request arrival/overlap audit for affinity-v2 CSV runs."""

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location('affinity_report', Path(__file__).with_name('report.py'))
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)
NS_PER_MS = 1_000_000
NS_PER_SECOND = 1_000_000_000


def statuses(rows):
    return dict(sorted(Counter(row['status'] for row in rows).items()))


def distribution(values):
    ordered = sorted(values)
    return {'n': len(ordered), 'mean': report.mean(ordered),
            'p50': report.quantile(ordered, .5), 'p95': report.quantile(ordered, .95),
            'p99': report.quantile(ordered, .99), 'max': ordered[-1] if ordered else None}


def lag_ms(row):
    return (row['submitted_ns'] - row['scheduled_ns']) / NS_PER_MS


def lag_group(rows, threshold_ms):
    values = [lag_ms(row) for row in rows]
    exceeded = sum(value > threshold_ms for value in values)
    return {'n': len(rows), 'statuses': statuses(rows), 'lag_ms': distribution(values),
            'above_threshold': exceeded,
            'above_percent': 100 * exceeded / len(rows) if rows else None}


def latency_group(rows):
    completed = [row for row in rows if row['status'] == 'completed']
    return {'n': len(rows), 'statuses': statuses(rows), 'successful': len(completed),
            'planned_ms': distribution([(row['finished_ns'] - row['scheduled_ns']) / NS_PER_MS
                                        for row in completed]),
            'after_attempt_ms': distribution([(row['finished_ns'] - row['submitted_ns']) / NS_PER_MS
                                              for row in completed])}


def request_overlap(requests, left_field, start, end):
    """Integrate all recorded half-open intervals clipped to a wall window.

    Aggregate coincident endpoints before considering the following positive
    time segment. Adjacent/zero-duration intervals cannot inflate the peak.
    Failed requests participate until their recorded terminal timestamp.
    """
    if end <= start:
        raise ValueError('overlap window must have positive duration')
    changes = defaultdict(int)
    intervals = 0
    for row in requests:
        left, right = max(start, row[left_field]), min(end, row['finished_ns'])
        if left < right:
            changes[left] += 1
            changes[right] -= 1
            intervals += 1
    count = peak = area = overlapping = 0
    previous = start
    for point, change in sorted(changes.items()):
        duration = point - previous
        if duration:
            area += count * duration
            overlapping += duration if count >= 2 else 0
            peak = max(peak, count)
        count += change
        previous = point
    duration = end - previous
    area += count * duration
    overlapping += duration if count >= 2 else 0
    if duration:
        peak = max(peak, count)
    return {'intervals': intervals, 'mean': area / (end - start), 'peak': peak,
            'time_ge2_percent': 100 * overlapping / (end - start)}


def top_lag_rows(requests, cohort, origin, top):
    # report.load_run audits unique contiguous request IDs and sorts by ID.
    # Keep those neighbors even when they fall outside the planned cohort.
    indices = {row['request']: index for index, row in enumerate(requests)}
    selected = sorted(cohort, key=lambda row: (-lag_ms(row), row['request']))[:top]
    output = []
    for row in selected:
        index = indices[row['request']]
        previous = requests[index - 1] if index else None
        following = requests[index + 1] if index + 1 < len(requests) else None
        output.append({'request': row['request'], 'status': row['status'],
                       'scheduled_ms': (row['scheduled_ns'] - origin) / NS_PER_MS,
                       'attempt_ms': (row['submitted_ns'] - origin) / NS_PER_MS,
                       'finish_ms': (row['finished_ns'] - origin) / NS_PER_MS,
                       'lag_ms': lag_ms(row),
                       'planned_ms': (row['finished_ns'] - row['scheduled_ns']) / NS_PER_MS,
                       'after_attempt_ms': (row['finished_ns'] - row['submitted_ns']) / NS_PER_MS,
                       'previous_attempt_gap_ms': (row['submitted_ns'] - previous['submitted_ns']) / NS_PER_MS
                       if previous is not None else None,
                       'next_attempt_gap_ms': (following['submitted_ns'] - row['submitted_ns']) / NS_PER_MS
                       if following is not None else None})
    return output


def analyze(run, warmup_seconds, window_seconds, phase_modulus, threshold_ms, top):
    summary, requests, cohort = run['summary'], run['requests'], run['cohort']
    # The v2 driver attempts requests sequentially in ID order. The shared
    # loader checks each row's timestamps, but not this cross-row contract.
    if any(later['submitted_ns'] < earlier['submitted_ns']
           for earlier, later in zip(requests, requests[1:])):
        raise ValueError(f'{run["path"]}: request attempts are not monotonic by request ID')
    origin = summary['origin_ns']
    start = origin + round(warmup_seconds * NS_PER_SECOND)
    end = start + round(window_seconds * NS_PER_SECOND)
    phases = []
    if phase_modulus is not None:
        groups = defaultdict(list)
        for row in cohort:
            groups[row['request'] % phase_modulus].append(row)
        phases = [{'residue': residue, **lag_group(groups[residue], threshold_ms)}
                  for residue in range(phase_modulus)]
    return {'run': run, 'start': start, 'end': end,
            'whole_statuses': statuses(requests), 'cohort': lag_group(cohort, threshold_ms),
            'phases': phases,
            'strata': [('lag<=threshold', latency_group([row for row in cohort if lag_ms(row) <= threshold_ms])),
                       ('lag>threshold', latency_group([row for row in cohort if lag_ms(row) > threshold_ms]))],
            'top': top_lag_rows(requests, cohort, origin, top),
            'overlap': [(name, request_overlap(requests, field, start, end))
                        for name, field in (('scheduled-to-terminal', 'scheduled_ns'),
                                            ('attempt-to-terminal', 'submitted_ns'))]}


def number(value):
    return 'unavailable' if value is None else f'{value:.3f}'


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def print_audit(audits, phase_modulus, threshold_ms):
    print('Arrival audit: every input was audited before output; runs are described separately, not paired or pooled.')
    print('Planned-arrival cohort retains late attempts/outcomes and all statuses. Nearest-rank quantiles; no tail-independence/CI guarantee.')
    print('submitted_ns is the driver attempt BEFORE request construction/root add, not enqueue or callback start. No driver scheduling cause is inferred.')
    print('Lag strata are descriptive selection, not causal effects or corrected performance; quantiles must NOT be subtracted.')
    print('Overlap uses ALL request intervals clipped to the wall window, including earlier arrivals/later outcomes and failures.')
    print('Scheduled overlap includes generator lag; attempt overlap includes request construction. Neither is Folly Q, callback I, kernel runq, or summary active/peak_inflight.')
    if phase_modulus is None:
        print('Phase grouping disabled; use --phase-modulus explicitly for request-ID residues.')
    else:
        print(f'Phase = request ID % {phase_modulus}: residue is NOT measured home, chosen pool, task pool or CPU; no routing attribution is inferred.')
    for audit in audits:
        run = audit['run']
        summary, cohort = run['summary'], audit['cohort']
        origin = summary['origin_ns']
        print(f'\nRUN {run["path"]}')
        print(f'Contract: seed={summary["seed"]}, model={summary["model"]}, policy={summary["policy"]}, '
              f'continuation={summary["continuation"]}, home-policy={summary["options"].get("home-policy", "rr")}, '
              f'arrival={summary.get("arrival", "unknown")}, qps={summary.get("qps", "unknown")}, trace={int(summary["trace"])}, '
              f'affinity={summary["affinity"]}, pools={len(summary["groups"])}, threads/pool={summary["threads_per_pool"]}, '
              f'max-inflight={summary.get("max_inflight", "unknown")}')
        print(f'Resources: {compact(summary["groups"])}')
        print(f'Work/arrival identity: work_signature_sha256_prefix={hashlib.sha256(summary["work_signature"].encode()).hexdigest()[:16]}, '
              f'arrival_hash={summary["arrival_hash"]}, checksum={summary["checksum"]}; identity alone does not certify cross-run equality.')
        print(f'Whole statuses: n={len(run["requests"])}, {compact(audit["whole_statuses"])}; '
              f'cohort [{(audit["start"] - origin) / NS_PER_SECOND:g},{(audit["end"] - origin) / NS_PER_SECOND:g})s: '
              f'n={cohort["n"]}, {compact(cohort["statuses"])}')
        lag = cohort['lag_ms']
        print('Cohort lag ms mean/p50/p95/p99/max=' + '/'.join(number(lag[key]) for key in ('mean', 'p50', 'p95', 'p99', 'max')) +
              f'; lag>{threshold_ms:g}ms count={cohort["above_threshold"]}/{cohort["n"]}, percent={number(cohort["above_percent"])}')
        if phase_modulus is not None:
            print('| ID residue | n | statuses | lag mean/p99 ms | above-threshold count |')
            print('|---:|---:|---|---:|---:|')
            for row in audit['phases']:
                print(f'| {row["residue"]} | {row["n"]} | {compact(row["statuses"])} | '
                      f'{number(row["lag_ms"]["mean"])}/{number(row["lag_ms"]["p99"])} | {row["above_threshold"]} |')
        print('| lag stratum | n | statuses | successful | planned mean/p99 ms | attempt-to-finish mean/p99 ms |')
        print('|---|---:|---|---:|---:|---:|')
        for label, row in audit['strata']:
            print(f'| {label} | {row["n"]} | {compact(row["statuses"])} | {row["successful"]} | '
                  f'{number(row["planned_ms"]["mean"])}/{number(row["planned_ms"]["p99"])} | '
                  f'{number(row["after_attempt_ms"]["mean"])}/{number(row["after_attempt_ms"]["p99"])} |')
        print('Top cohort lag rows; times relative to origin in ms, gaps use previous/next request ID in the WHOLE run. Noncompleted durations end at rejection/failure, not successful latency.')
        if audit['top']:
            print('| request | status | scheduled/attempt/finish ms | lag ms | planned/attempt-to-finish ms | previous/next attempt gap ms |')
            print('|---:|---|---:|---:|---:|---:|')
            for row in audit['top']:
                print(f'| {row["request"]} | {row["status"]} | '
                      f'{number(row["scheduled_ms"])}/{number(row["attempt_ms"])}/{number(row["finish_ms"])} | '
                      f'{number(row["lag_ms"])} | {number(row["planned_ms"])}/{number(row["after_attempt_ms"])} | '
                      f'{number(row["previous_attempt_gap_ms"])}/{number(row["next_attempt_gap_ms"])} |')
        else:
            print('Top rows: none (empty cohort or --top 0).')
        for label, row in audit['overlap']:
            print(f'Overlap {label}: clipped_intervals={row["intervals"]}, '
                  f'time-weighted mean={number(row["mean"])}, peak={row["peak"]}, '
                  f'time>=2 percent={number(row["time_ge2_percent"])}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--warmup-seconds', type=float, default=2)
    parser.add_argument('--window-seconds', type=float, default=16)
    parser.add_argument('--phase-modulus', type=int, help='optional request-ID residue grouping, 1..64; not pool identity')
    parser.add_argument('--lag-threshold-ms', type=float, default=1)
    parser.add_argument('--top', type=int, default=3, help='maximum largest-lag rows per run, 0..10')
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error('warmup-seconds must be finite and nonnegative')
    if (not math.isfinite(args.window_seconds) or args.window_seconds <= 0
            or not math.isfinite(args.window_seconds * NS_PER_SECOND)
            or round(args.window_seconds * NS_PER_SECOND) < 1):
        parser.error('window-seconds must be finite and positive, rounding to at least 1 ns')
    if not math.isfinite(args.lag_threshold_ms) or args.lag_threshold_ms < 0:
        parser.error('lag-threshold-ms must be finite and nonnegative')
    if args.phase_modulus is not None and not 1 <= args.phase_modulus <= 64:
        parser.error('phase-modulus must be in 1..64')
    if not 0 <= args.top <= 10:
        parser.error('top must be in 0..10')
    try:
        runs = [report.load_run(path, args.warmup_seconds, args.window_seconds) for path in args.runs]
        audits = [analyze(run, args.warmup_seconds, args.window_seconds,
                          args.phase_modulus, args.lag_threshold_ms, args.top) for run in runs]
        print_audit(audits, args.phase_modulus, args.lag_threshold_ms)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity arrival audit: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

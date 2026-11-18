#!/usr/bin/env python3
"""Audit old-request later-wave exposure during new-request initial fanout.

Reads existing CSV files only. The quantities are callback wall states, not CPU
service, Linux runnable counts, or exact Folly queue occupancy.
"""

import argparse
from collections import defaultdict
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location(
    'phase_shape', Path(__file__).with_name('request_cpu_shape.py'))
shape = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shape)
report = shape.flow.report
MS = 1_000_000


class Counts:
    """Fenwick counts; prefix(rank) excludes equal planned-arrival ranks."""
    def __init__(self, size):
        self.values = [0] * (size + 1)

    def add(self, rank, delta):
        index = rank + 1
        while index < len(self.values):
            self.values[index] += delta
            index += index & -index

    def prefix(self, rank):
        result = 0
        while rank:
            result += self.values[rank]
            rank -= rank & -rank
        return result


def graph_tags(summary):
    """Versioned wave/stage/branch identities, including control callbacks."""
    widths, depths = summary['widths'], summary['depths']
    if (len(widths) < 2 or len(widths) != len(depths)
            or any(type(value) is not int or value < 1 for value in widths + depths)):
        raise ValueError('phase overlap requires at least two valid waves')
    tags, parents, terminals, offset = {0: ('init', -1, -1)}, {}, {}, 1
    previous_join = 0
    for wave, (width, depth) in enumerate(zip(widths, depths)):
        for stage in range(depth):
            for branch in range(width):
                tag = offset + stage * width + branch
                tags[tag] = ('operator', wave, stage)
                parents[tag] = tag - width if stage else previous_join
        join = offset + width * depth
        tags[join] = ('join', wave, -1)
        terminals[join] = [offset + (depth - 1) * width + branch
                           for branch in range(width)]
        previous_join, offset = join, join + 1
    if offset != summary['nodes_per_request']:
        raise ValueError('graph tag/node conservation mismatch')
    return tags, parents, terminals


def validate(run):
    summary = run['summary']
    if summary['schema'] != 'affinity-v2' or not summary['trace']:
        raise ValueError('requires traced affinity-v2; async and trace=0 are unsupported')
    if summary.get('sync_block_percent', 0) or summary.get('planned_sync_block_us', 0):
        raise ValueError('this bounded analysis excludes synchronous waiting workloads')
    if any(row['status'] != 'completed' for row in run['requests']):
        raise ValueError('requires all requests completed; failed traces are not silently discarded')
    if not run['completed_cohort']:
        raise ValueError('empty planned-arrival cohort')
    resource = shape.topology(summary)
    if not resource['pool_service_maps_to_declared_cluster']:
        raise ValueError('requires cluster affinity with disjoint declared CPU sets')
    if any(type(group['numa']) is not int or group['numa'] < 0
           for group in summary['groups']):
        raise ValueError('requires known nonnegative integer NUMA IDs')
    shape.validate_tasks(run['tasks'])
    tags, parents, terminals = graph_tags(summary)
    rows = {(row['request'], row['tag']): row for row in run['tasks']}
    if len(rows) != len(run['requests']) * len(tags):
        raise ValueError('complete graph task conservation mismatch')
    for request in run['requests']:
        rid = request['request']
        for tag, (kind, _, _) in tags.items():
            if (rid, tag) not in rows or rows[rid, tag]['kind'] != kind:
                raise ValueError('missing callback or graph kind/tag mismatch')
        for tag, parent in parents.items():
            child, source = rows[rid, tag], rows[rid, parent]
            if not source['start_ns'] <= child['submitted_ns'] <= source['finish_ns']:
                raise ValueError('child submission is outside its parent callback interval')
        for tag, final_tags in terminals.items():
            submitted = rows[rid, tag]['submitted_ns']
            finals = [rows[rid, item] for item in final_tags]
            if (any(row['start_ns'] > submitted for row in finals)
                    or not any(row['start_ns'] <= submitted <= row['finish_ns'] for row in finals)):
                raise ValueError('join submission violates the terminal-branch timing contract')
    return tags, resource


def state_events(row, category, rank, rid):
    """Half-open intervals, with no transient state for zero-length intervals."""
    for state, left, right in (('pending', row['submitted_ns'], row['start_ns']),
                               ('executing', row['start_ns'], row['finish_ns'])):
        if left < right:
            yield (left, category, state, rank, rid, 1)
            yield (right, category, state, rank, rid, -1)


def analyze_scope(old, new, ranks, groups, scope):
    """Sweep each resource; preserve per-request exposure without pair sums."""
    events = defaultdict(list)
    def resource(row):
        return row['chosen'] if scope == 'pool' else groups[row['chosen']]['numa']
    for category, rows in (('old', old), ('new', new)):
        for row in rows:
            rid = row['request']
            events[resource(row)].extend(state_events(row, category, ranks[rid], rid))
            if category == 'new':
                events[resource(row)].append((row['submitted_ns'], 'query', '', ranks[rid], rid, 0))
    counters = {name: 0 for name in (
        'stage0_submissions', 'submissions_with_older_pending',
        'submissions_with_older_executing', 'submissions_with_either',
        'max_older_pending_at_submission', 'max_older_executing_at_submission',
        'max_combined_executing_callbacks_when_exposed')}
    durations = {name: 0 for name in (
        'new_live', 'new_executing', 'new_live_with_older_pending',
        'new_live_with_older_executing', 'simultaneous_executing')}
    exposed_submissions, exposed_intervals = set(), set()
    rank_count = max(ranks.values()) + 1
    for sequence in events.values():
        old_counts = {state: Counts(rank_count) for state in ('pending', 'executing')}
        active_new = {}
        previous = None
        for timestamp, grouped in itertools.groupby(sorted(sequence), key=lambda item: item[0]):
            if previous is not None and timestamp > previous:
                duration = timestamp - previous
                for rid, states in active_new.items():
                    pending = old_counts['pending'].prefix(ranks[rid])
                    executing = old_counts['executing'].prefix(ranks[rid])
                    durations['new_live'] += duration
                    if pending:
                        durations['new_live_with_older_pending'] += duration
                    if executing:
                        durations['new_live_with_older_executing'] += duration
                    if states['executing']:
                        durations['new_executing'] += duration
                        if executing:
                            durations['simultaneous_executing'] += duration
                            exposed_intervals.add(rid)
                            counters['max_combined_executing_callbacks_when_exposed'] = max(
                                counters['max_combined_executing_callbacks_when_exposed'],
                                executing + states['executing'])
            batch = list(grouped)
            # Apply all transitions at this timestamp before querying [t,...).
            for _, category, state, rank, rid, delta in batch:
                if category == 'old':
                    old_counts[state].add(rank, delta)
                elif category == 'new':
                    values = active_new.setdefault(rid, {'pending': 0, 'executing': 0})
                    values[state] += delta
            for rid in list(active_new):
                if any(value < 0 for value in active_new[rid].values()):
                    raise ValueError('negative callback state during interval sweep')
                if not any(active_new[rid].values()):
                    del active_new[rid]
            for _, category, _, rank, rid, _ in batch:
                if category != 'query':
                    continue
                pending = old_counts['pending'].prefix(rank)
                executing = old_counts['executing'].prefix(rank)
                counters['stage0_submissions'] += 1
                counters['submissions_with_older_pending'] += bool(pending)
                counters['submissions_with_older_executing'] += bool(executing)
                counters['submissions_with_either'] += bool(pending or executing)
                counters['max_older_pending_at_submission'] = max(
                    counters['max_older_pending_at_submission'], pending)
                counters['max_older_executing_at_submission'] = max(
                    counters['max_older_executing_at_submission'], executing)
                if pending or executing:
                    exposed_submissions.add(rid)
            previous = timestamp
        if active_new or any(count.prefix(rank_count) for count in old_counts.values()):
            raise ValueError('callback state did not drain after final timestamp')
    counters['new_requests_exposed_at_submission'] = len(exposed_submissions)
    counters['new_requests_with_simultaneous_executing'] = len(exposed_intervals)
    counters.update({f'{name}_request_resource_ms': value / MS
                     for name, value in durations.items()})
    counters['simultaneous_executing_fraction'] = (
        durations['simultaneous_executing'] / durations['new_executing']
        if durations['new_executing'] else None)
    if counters['stage0_submissions'] != len(new):
        raise ValueError('stage0 submission denominator mismatch')
    return counters


def analyze_wait_exposure(later_cohort, first_whole, ranks, groups, scope):
    """Intersect each old callback's wait with any strictly newer stage0 execution.

    Multiple newer callbacks do not multiply a waiting callback's covered time.
    Zero-wait callbacks are included in the nonexposed group and denominator.
    """
    events = defaultdict(list)
    def resource(row):
        return row['chosen'] if scope == 'pool' else groups[row['chosen']]['numa']
    waits = {(row['request'], row['tag']): row['start_ns'] - row['submitted_ns']
             for row in later_cohort}
    covered = {identity: 0 for identity in waits}
    for row in later_cohort:
        if row['submitted_ns'] < row['start_ns']:
            for timestamp, delta in ((row['submitted_ns'], 1), (row['start_ns'], -1)):
                events[resource(row)].append((timestamp, 'wait', ranks[row['request']],
                                               row['request'], row['tag'], delta))
    for row in first_whole:
        if row['start_ns'] < row['finish_ns']:
            for timestamp, delta in ((row['start_ns'], 1), (row['finish_ns'], -1)):
                events[resource(row)].append((timestamp, 'new', ranks[row['request']],
                                               row['request'], row['tag'], delta))
    rank_count = max(ranks.values()) + 1
    for sequence in events.values():
        newer = Counts(rank_count)
        active_waits = {}
        previous = None
        for timestamp, grouped in itertools.groupby(sorted(sequence), key=lambda item: item[0]):
            if previous is not None and timestamp > previous:
                duration = timestamp - previous
                total = newer.prefix(rank_count)
                for identity, rank in active_waits.items():
                    if total - newer.prefix(rank + 1) > 0:
                        covered[identity] += duration
            for _, category, rank, rid, tag, delta in grouped:
                if category == 'new':
                    newer.add(rank, delta)
                elif delta > 0:
                    active_waits[rid, tag] = rank
                else:
                    del active_waits[rid, tag]
            previous = timestamp
        if active_waits or newer.prefix(rank_count):
            raise ValueError('inverse wait exposure state did not drain')
    if any(not 0 <= covered[key] <= duration for key, duration in waits.items()):
        raise ValueError('covered wait exceeds callback waiting interval')
    exposed = [key for key, duration in covered.items() if duration > 0]
    nonexposed = [key for key, duration in covered.items() if duration == 0]
    exposed_wait = sum(waits[key] for key in exposed)
    nonexposed_wait = sum(waits[key] for key in nonexposed)
    return {
        'later_wave_callbacks': len(waits),
        'positive_wait_callbacks': sum(duration > 0 for duration in waits.values()),
        'zero_wait_callbacks': sum(duration == 0 for duration in waits.values()),
        'callbacks_with_newer_stage0_execution_during_wait': len(exposed),
        'callbacks_without_newer_stage0_execution_during_wait': len(nonexposed),
        'total_callback_wait_ms': sum(waits.values()) / MS,
        'covered_union_callback_wait_ms': sum(covered.values()) / MS,
        'exposed_callbacks_total_wait_ms': exposed_wait / MS,
        'nonexposed_callbacks_total_wait_ms': nonexposed_wait / MS,
        'exposed_callbacks_wait_mean_ms': exposed_wait / MS / len(exposed) if exposed else None,
        'nonexposed_callbacks_wait_mean_ms': nonexposed_wait / MS / len(nonexposed) if nonexposed else None,
        'covered_fraction_of_total_wait': (sum(covered.values()) / sum(waits.values())
                                           if sum(waits.values()) else None)}


def analyze(run):
    tags, resource = validate(run)
    selected = {row['request'] for row in run['completed_cohort']}
    planned = sorted({row['scheduled_ns'] for row in run['requests']})
    rank_by_time = {value: index for index, value in enumerate(planned)}
    ranks = {row['request']: rank_by_time[row['scheduled_ns']] for row in run['requests']}
    old = [row for row in run['tasks']
           if row['kind'] == 'operator' and tags[row['tag']][1] >= 1]
    new = [row for row in run['tasks'] if row['request'] in selected
           and tags[row['tag']] == ('operator', 0, 0)]
    first_whole = [row for row in run['tasks'] if tags[row['tag']] == ('operator', 0, 0)]
    later_cohort = [row for row in old if row['request'] in selected]
    if len(new) != len(selected) * run['summary']['widths'][0]:
        raise ValueError('cohort stage0 callback conservation mismatch')
    scopes = {}
    for scope in ('pool', 'numa'):
        scopes[scope] = analyze_scope(old, new, ranks, run['summary']['groups'], scope)
        scopes[scope]['inverse_later_wait'] = analyze_wait_exposure(
            later_cohort, first_whole, ranks, run['summary']['groups'], scope)
    return {'schema': 'affinity_request_phase_overlap_v1', 'run': str(run['path']),
            'policy': run['summary']['policy'], 'widths': run['summary']['widths'],
            'depths': run['summary']['depths'], 'completed_requests_whole': len(run['requests']),
            'cohort_requests': len(selected), 'first_stage0_callbacks': len(new),
            'later_wave_callbacks_whole': len(old),
            'initial_stage0_callbacks_whole': len(first_whole),
            'later_wave_callbacks_cohort': len(later_cohort),
            'equal_planned_arrival_extra_requests': len(run['requests']) - len(planned),
            'resource': resource,
            'contract': {
                'old': 'strictly earlier planned arrival; equal planned timestamps are not ordered by ID',
                'new': 'completed planned-arrival cohort, first-wave stage0 only, all late records retained',
                'other': 'all requests contribute later-wave operators, including requests outside the cohort',
                'pending': 'post-routing submitted_ns <= t < start_ns; not exact Folly queue occupancy',
                'executing': 'start_ns <= t < finish_ns; callback wall occupancy, not on-CPU or runnable',
                'submission': 'query at post-routing submitted_ns using chosen destination, not pre-choice pressure',
                'interval': 'union within each new-request/resource; sum across these pairs, never sum old/new task pairs',
                'simultaneous': 'new stage0 executing and at least one strictly older later-wave operator executing',
                'inverse_wait': 'each cohort later-wave callback [submitted,start) intersected with the union of strictly newer requests stage0 executing intervals; newer candidates include requests outside the cohort',
                'inverse_denominator': 'all cohort later-wave operators, including zero-wait callbacks in the nonexposed group; callback-ms sums covered unions per callback',
                'inverse_comparison': 'exposed/nonexposed means include each callback full wait, not only covered time; longer waits have more chance to overlap, so this comparison is descriptive and length-biased',
                'numa': 'same NUMA includes different clusters and does not establish shared-CPU contention',
                'causality': 'exposure demonstrates temporal/resource overlap only, not delay caused or production fidelity'},
            'scopes': scopes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--warmup-seconds', type=float, default=2)
    parser.add_argument('--window-seconds', type=float, default=16)
    parser.add_argument('--json', action='store_true', help='one complete JSON object per run')
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error('warmup-seconds must be finite and nonnegative')
    if (not math.isfinite(args.window_seconds) or args.window_seconds <= 0
            or round(args.window_seconds * 1e9) < 1):
        parser.error('window-seconds must be finite, positive and at least one nanosecond')
    try:
        for path in args.runs:
            result = analyze(report.load_run(path, args.warmup_seconds, args.window_seconds))
            result['planned_cohort_seconds'] = [args.warmup_seconds,
                                                args.warmup_seconds + args.window_seconds]
            result['source_sha256'] = {name: hashlib.sha256((path / name).read_bytes()).hexdigest()
                                       for name in ('summary.json', 'requests.csv', 'tasks.csv')}
            if args.json:
                print(json.dumps(result, sort_keys=True, allow_nan=False))
                continue
            print(f'RUN {path}: policy={result["policy"]}, widths={result["widths"]}, depths={result["depths"]}')
            print(f'Planned cohort [{args.warmup_seconds:g}, {args.warmup_seconds + args.window_seconds:g}) s: '
                  f'{result["cohort_requests"]} requests, {result["first_stage0_callbacks"]} initial stage0 submissions; '
                  f'older candidates use all {result["completed_requests_whole"]} requests.')
            print('Half-open callback states; pending is post-routing submitted-not-started, executing is wall occupancy. Neither is CPU service or exact Folly Q.')
            print('| scope | submissions with old pending / executing / either | new requests exposed at submit / while executing | simultaneous executing / new executing request-resource ms |')
            for scope, row in result['scopes'].items():
                print(f'| {scope} | {row["submissions_with_older_pending"]} / {row["submissions_with_older_executing"]} / '
                      f'{row["submissions_with_either"]} of {row["stage0_submissions"]} | '
                      f'{row["new_requests_exposed_at_submission"]} / {row["new_requests_with_simultaneous_executing"]} of {result["cohort_requests"]} | '
                      f'{row["simultaneous_executing_request_resource_ms"]:.6f} / '
                      f'{row["new_executing_request_resource_ms"]:.6f} |')
            print('| inverse: cohort later-wave wait | exposed / all callbacks | covered union / all wait callback-ms | exposed / nonexposed full-wait mean ms |')
            for scope, row in result['scopes'].items():
                wait = row['inverse_later_wait']
                means = [wait[key] for key in ('exposed_callbacks_wait_mean_ms', 'nonexposed_callbacks_wait_mean_ms')]
                mean_text = ' / '.join('unknown' if value is None else f'{value:.6f}' for value in means)
                print(f'| {scope} | {wait["callbacks_with_newer_stage0_execution_during_wait"]} / '
                      f'{wait["later_wave_callbacks"]} | {wait["covered_union_callback_wait_ms"]:.6f} / '
                      f'{wait["total_callback_wait_ms"]:.6f} | {mean_text} |')
            print('Older means strictly earlier planned arrival; equal-time arrivals are excluded. NUMA overlap may involve disjoint clusters. Exposure does not establish causal slowdown.')
            print('Inverse wait uses all newer requests, including outside the cohort; zero waits are nonexposed. Longer waits have more opportunity for exposure, so conditional wait means are not a causal comparison.')
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity request phase overlap: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

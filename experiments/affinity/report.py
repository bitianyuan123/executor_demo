#!/usr/bin/env python3
"""Audit affinity-v2 runs and print request-cohort statistics to the terminal."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


MASK64 = (1 << 64) - 1
TERMINAL = {'completed', 'admission_rejected', 'submit_rejected',
            'partial_batch_rejected', 'child_rejected', 'join_rejected', 'failed'}


def mix(value):
    value = (value + 0x9E3779B97F4A7C15) & MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
    return value ^ (value >> 31)


def require_fields(value, fields, label):
    missing = set(fields) - set(value)
    if missing:
        raise ValueError(f'{label}: missing fields {sorted(missing)}')


def read_csv(path, fields):
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream)
        require_fields(reader.fieldnames or [], fields, str(path))
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f'{path}: malformed/truncated CSV row')
    return rows


def integer(value, label):
    number = int(value)
    if number < 0:
        raise ValueError(f'{label}: expected nonnegative integer')
    return number


def quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def mean(values):
    return statistics.fmean(values) if values else None


def load_run(path, warmup_seconds=0.0, window_seconds=None):
    """Return audited data; do not silently discard failed or late completions."""
    path = Path(path)
    summary = json.loads((path / 'summary.json').read_text())
    require_fields(summary, ('schema', 'origin_ns', 'seed', 'work_signature', 'policy',
                            'continuation', 'affinity', 'model', 'requests',
                            'nodes_per_request', 'widths', 'depths', 'groups',
                            'threads_per_pool', 'trace', 'arrival_hash', 'checksum',
                            'completed', 'failed_or_rejected', 'observed_tasks',
                            'migrations', 'cross_numa', 'options', 'process_cpu_s',
                            'peak_inflight'), str(path / 'summary.json'))
    if summary['schema'] != 'affinity-v2':
        raise ValueError(f'{path}: unsupported schema')
    if not isinstance(summary['work_signature'], str) or not summary['work_signature']:
        raise ValueError(f'{path}: empty/non-string work signature')
    if not isinstance(summary['trace'], bool):
        raise ValueError(f'{path}: trace must be boolean')
    if not math.isfinite(float(summary['process_cpu_s'])) or float(summary['process_cpu_s']) < 0:
        raise ValueError(f'{path}: invalid process CPU time')
    origin = integer(summary['origin_ns'], 'origin_ns')
    requests = read_csv(path / 'requests.csv', ('request', 'scheduled_ns', 'submitted_ns',
                                              'finished_ns', 'status', 'concurrent', 'checksum'))
    numeric_request_fields = ('request', 'scheduled_ns', 'submitted_ns', 'finished_ns',
                              'concurrent', 'checksum')
    for row in requests:
        for field in numeric_request_fields:
            row[field] = integer(row[field], field)
        if row['status'] not in TERMINAL:
            raise ValueError(f'{path}: nonterminal/unknown request status {row["status"]!r}')
        if not origin <= row['scheduled_ns'] <= row['submitted_ns'] <= row['finished_ns']:
            raise ValueError(f'{path}: invalid request timestamps')
    expected_requests = integer(summary['requests'], 'requests')
    if len(requests) != expected_requests or sorted(row['request'] for row in requests) != list(range(expected_requests)):
        raise ValueError(f'{path}: missing or duplicated request rows')
    requests.sort(key=lambda row: row['request'])
    completed = sum(row['status'] == 'completed' for row in requests)
    if completed != int(summary['completed']) or len(requests) - completed != int(summary['failed_or_rejected']):
        raise ValueError(f'{path}: request conservation mismatch')
    arrival_hash = checksum = 0
    last_scheduled = origin
    for row in requests:
        if row['scheduled_ns'] < last_scheduled:
            raise ValueError(f'{path}: request schedule is not monotonic')
        last_scheduled = row['scheduled_ns']
        arrival_hash = mix(arrival_hash ^ (row['scheduled_ns'] - origin))
        if row['status'] == 'completed':
            checksum = mix(checksum ^ row['checksum'])
    if str(arrival_hash) != str(summary['arrival_hash']) or str(checksum) != str(summary['checksum']):
        raise ValueError(f'{path}: arrival/checksum audit mismatch')

    widths, depths = summary['widths'], summary['depths']
    if not widths or len(widths) != len(depths) or any(int(x) <= 0 for x in widths + depths):
        raise ValueError(f'{path}: invalid graph dimensions')
    expected_nodes = 1 + sum(int(w) * int(d) + 1 for w, d in zip(widths, depths))
    if expected_nodes != int(summary['nodes_per_request']):
        raise ValueError(f'{path}: graph node count mismatch')
    expected_kinds, offset = {0: 'init'}, 1
    for width, depth in zip(widths, depths):
        count = int(width) * int(depth)
        expected_kinds.update((tag, 'operator') for tag in range(offset, offset + count))
        expected_kinds[offset + count] = 'join'
        offset += count + 1

    tasks = []
    if summary['trace']:
        fields = ('request', 'tag', 'kind', 'home', 'source', 'chosen', 'hops', 'reason',
                  'migrated', 'cross_numa', 'queue_before', 'outstanding_before',
                  'submitted_ns', 'start_ns', 'finish_ns', 'cpu_ns', 'iterations',
                  'read_words', 'write_words', 'checksum')
        tasks = read_csv(path / 'tasks.csv', fields)
        seen = set()
        for row in tasks:
            for field in fields:
                if field != 'kind':
                    row[field] = integer(row[field], field)
            key = row['request'], row['tag']
            if key in seen or row['request'] >= len(requests) or row['tag'] >= expected_nodes:
                raise ValueError(f'{path}: duplicate/invalid task identity')
            seen.add(key)
            if row['kind'] != expected_kinds[row['tag']]:
                raise ValueError(f'{path}: task kind does not match graph')
            if not requests[row['request']]['submitted_ns'] <= row['submitted_ns'] <= row['start_ns'] <= row['finish_ns']:
                raise ValueError(f'{path}: invalid task timestamps')
            if any(row[field] >= len(summary['groups']) for field in ('home', 'source', 'chosen')):
                raise ValueError(f'{path}: unknown task pool')
            if row['migrated'] != int(row['source'] != row['chosen']):
                raise ValueError(f'{path}: migration flag mismatch')
        if len(tasks) != int(summary['observed_tasks']):
            raise ValueError(f'{path}: task trace count mismatch')
        for row in requests:
            if row['status'] == 'completed' and any((row['request'], tag) not in seen for tag in range(expected_nodes)):
                raise ValueError(f'{path}: completed request missing tasks')
        if sum(row['migrated'] for row in tasks) != int(summary['migrations']) or sum(row['cross_numa'] for row in tasks) != int(summary['cross_numa']):
            raise ValueError(f'{path}: migration counter mismatch')
    elif int(summary['observed_tasks']) != 0:
        raise ValueError(f'{path}: trace disabled but observed_tasks is nonzero')

    start = origin + round(warmup_seconds * 1e9)
    end = None if window_seconds is None else start + round(window_seconds * 1e9)
    cohort = [row for row in requests if row['scheduled_ns'] >= start and (end is None or row['scheduled_ns'] < end)]
    complete_cohort = [row for row in cohort if row['status'] == 'completed']
    ids = {row['request'] for row in complete_cohort}
    cohort_tasks = [row for row in tasks if row['request'] in ids]
    operators = [row for row in cohort_tasks if row['kind'] == 'operator']
    latencies = [(row['finished_ns'] - row['scheduled_ns']) / 1e6 for row in complete_cohort]
    waits = [(row['start_ns'] - row['submitted_ns']) / 1e6 for row in cohort_tasks]
    cpu = [row['cpu_ns'] / 1e6 for row in operators]
    wall = [(row['finish_ns'] - row['start_ns']) / 1e6 for row in operators]
    if not summary['groups']:
        raise ValueError(f'{path}: no CPU groups')
    for group in summary['groups']:
        require_fields(group, ('cpus', 'capacity', 'numa'), f'{path}: group')
        if not group['cpus'] or len(set(group['cpus'])) != len(group['cpus']) or not math.isfinite(float(group['capacity'])) or float(group['capacity']) <= 0:
            raise ValueError(f'{path}: invalid CPU resource description')
    resources = {'groups': summary['groups'], 'threads': summary['threads_per_pool'],
                 'affinity': summary['affinity'], 'trace': summary['trace'],
                 'page_query_enabled': summary.get('page_query_enabled', False),
                 'max_inflight': summary.get('max_inflight', 16),
                 'memory_mb': summary.get('memory_mb', 256),
                 'estimate_mode': summary.get('routing', {}).get('estimate_mode', 'constant'),
                 'home_policy': summary['options'].get('home-policy', 'rr')}
    pairing_key = (summary['seed'], summary['work_signature'], str(summary['arrival_hash']),
                   summary['model'], json.dumps(resources, sort_keys=True))
    return {'path': path, 'summary': summary, 'requests': requests, 'tasks': tasks,
            'cohort': cohort, 'completed_cohort': complete_cohort, 'pairing_key': pairing_key,
            'metrics': {'n': len(complete_cohort), 'offered': len(cohort),
                        'mean_ms': mean(latencies), 'p50_ms': quantile(latencies, .50),
                        'p95_ms': quantile(latencies, .95), 'p99_ms': quantile(latencies, .99),
                        'wait_p99_ms': quantile(waits, .99), 'operator_cpu_mean_ms': mean(cpu),
                        'operator_wall_mean_ms': mean(wall),
                        'operator_cpu_per_request_ms': sum(cpu) / len(ids) if cpu and ids else None,
                        'tasks': len(cohort_tasks), 'migrations': sum(row['migrated'] for row in cohort_tasks),
                        'cross_numa': sum(row['cross_numa'] for row in cohort_tasks)},
            'valid_for_comparison': completed == len(requests) and bool(cohort)}


def format_value(value):
    return '-' if value is None else f'{value:.3f}'


def print_report(runs, min_tail_samples, warmup_seconds, window_seconds):
    print('Affinity v2: scheduled-request cohort; late completions are retained.')
    print(f'Window: warmup={warmup_seconds:g}s, length={window_seconds if window_seconds is not None else "all remaining arrivals"}; nearest-rank quantiles.')
    print('Per-run descriptive statistics; no confidence interval or claim of independent tail samples.')
    print('Operator CPU/wall values cover the operator callback, including its successor submission; they are not a hardware stall decomposition.')
    printed_resources = set()
    for run in runs:
        summary = run['summary']
        resource = json.dumps(summary['groups'], sort_keys=True, separators=(',', ':'))
        if resource not in printed_resources:
            print(f'CPU groups ({summary["affinity"]}): {resource}')
            printed_resources.add(resource)
    print('| run | model/policy/continuation | completed/offered | mean ms | p95 ms | p99 ms | task wait p99 ms | operator CPU/wall mean ms | migrations/tasks | cross-NUMA |')
    print('|---|---|---:|---:|---:|---:|---:|---:|---:|---:|')
    for run in runs:
        summary, metric = run['summary'], run['metrics']
        print(f'| {run["path"].name} | {summary["model"]}/{summary["policy"]}/{summary["continuation"]} | '
              f'{metric["n"]}/{metric["offered"]} | {format_value(metric["mean_ms"])} | '
              f'{format_value(metric["p95_ms"])} | {format_value(metric["p99_ms"])} | '
              f'{format_value(metric["wait_p99_ms"])} | '
              f'{format_value(metric["operator_cpu_mean_ms"])}/{format_value(metric["operator_wall_mean_ms"])} | '
              f'{metric["migrations"]}/{metric["tasks"]} | {metric["cross_numa"]} |')
        flags = []
        if metric['n'] < min_tail_samples:
            flags.append(f'tail sample screen: n<{min_tail_samples} (nominal top 1%={metric["n"] * .01:.1f} requests)')
        if not run['valid_for_comparison']:
            flags.append('excluded from comparisons: failed/rejected requests or empty cohort')
        if not summary['trace']:
            flags.append('trace=0: task metrics unavailable; instrumentation differs from trace=1')
        if flags:
            print(f'  {run["path"].name}: ' + '; '.join(flags))
        print(f'  seed={summary["seed"]}, affinity={summary["affinity"]}, threads/pool={summary["threads_per_pool"]}, '
              f'whole completed/rejected-or-failed={summary["completed"]}/{summary["failed_or_rejected"]}, '
              f'operator CPU/request ms={format_value(metric["operator_cpu_per_request_ms"])}')
        signature = hashlib.sha256(summary['work_signature'].encode()).hexdigest()[:16]
        print(f'  work_signature_sha256_prefix={signature}, arrival_hash={summary["arrival_hash"]}, '
              f'checksum={summary["checksum"]}, pages={json.dumps(summary.get("pages", {}), sort_keys=True, separators=(",", ":"))}')
        whole_cpu_per_request = (float(summary['process_cpu_s']) * 1000 / len(run['requests'])
                                 if run['requests'] and int(summary['failed_or_rejected']) == 0 else None)
        whole_lag = quantile([(row['submitted_ns'] - row['scheduled_ns']) / 1e6
                             for row in run['requests']], .99)
        print(f'  WHOLE RUN (not cohort): process CPU s={float(summary["process_cpu_s"]):.6f}, '
              f'process CPU/completed-request ms={format_value(whole_cpu_per_request)}, '
              f'peak_inflight={summary["peak_inflight"]}, submission lag p99 ms={format_value(whole_lag)}')
        if summary['peak_inflight'] <= 1:
            print(f'  {run["path"].name}: no overlapping requests observed; this run cannot establish multi-request contention behavior.')

    print('\nMatched home deltas (candidate - home; negative means lower latency):')
    comparisons = 0
    for run in runs:
        if run['summary']['policy'] == 'home' or not run['valid_for_comparison']:
            continue
        homes = [other for other in runs if other['summary']['policy'] == 'home'
                 and other['valid_for_comparison'] and other['pairing_key'] == run['pairing_key']]
        if len(homes) != 1:
            print(f'  {run["path"].name}: not paired ({len(homes)} eligible home runs; need exactly one).')
            continue
        home = homes[0]
        first, second = run['completed_cohort'], home['completed_cohort']
        contract = lambda rows: [(row['request'], row['checksum']) for row in rows]
        if contract(first) != contract(second):
            raise ValueError(f'{run["path"]}: cohort request IDs/checksums differ from matched home')
        if run['summary']['trace']:
            task_contract = lambda item: sorted((row['request'], row['tag'], row['iterations'], row['read_words'], row['write_words'], row['checksum']) for row in item['tasks'])
            if task_contract(run) != task_contract(home):
                raise ValueError(f'{run["path"]}: actual task work/checksums differ from matched home')
        deltas = {key: run['metrics'][key] - home['metrics'][key] for key in ('mean_ms', 'p95_ms', 'p99_ms')}
        print(f'  {run["path"].name} vs {home["path"].name}: n={len(first)}, '
              f'delta mean/p95/p99 ms={deltas["mean_ms"]:+.3f}/{deltas["p95_ms"]:+.3f}/{deltas["p99_ms"]:+.3f}; descriptive only.')
        comparisons += 1
    if not comparisons:
        print('  No eligible unique pairs. Models, work, arrival schedules, seeds, CPU resources and tracing are never pooled.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--warmup-seconds', type=float, default=0)
    parser.add_argument('--window-seconds', type=float)
    parser.add_argument('--min-tail-samples', type=int, default=1000,
                        help='minimum completed request count for the tail-size screen; not a statistical guarantee')
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0 or args.min_tail_samples < 1:
        parser.error('finite nonnegative warmup and positive min-tail-samples required')
    if args.window_seconds is not None and (not math.isfinite(args.window_seconds) or args.window_seconds <= 0):
        parser.error('window-seconds must be positive and finite')
    try:
        runs = [load_run(path, args.warmup_seconds, args.window_seconds) for path in args.runs]
        print_report(runs, args.min_tail_samples, args.warmup_seconds, args.window_seconds)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity report: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

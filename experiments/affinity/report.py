#!/usr/bin/env python3
"""Audit affinity-v2 runs and print request-cohort statistics to the terminal."""

import argparse
import csv
import hashlib
import json
import math
import os
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


def callback_occupancy(tasks, group_count, start, end):
    """Observed callback intervals, including synchronous sleep/preemption.

    All tasks overlapping the wall-clock window participate, independently of
    the request-arrival cohort. This is not Linux runnable or on-CPU occupancy.
    """
    if end <= start:
        return None
    events = [[] for _ in range(group_count + 1)]
    area = [0] * len(events)
    for task in tasks:
        left, right = max(start, task['start_ns']), min(end, task['finish_ns'])
        if right <= left:
            continue
        for group in (task['chosen'], group_count):
            events[group].extend(((left, 1), (right, -1)))
            area[group] += right - left
    result = []
    for group, points in enumerate(events):
        active = peak = 0
        for _, delta in sorted(points):
            active += delta
            peak = max(peak, active)
        result.append({'group': group if group < group_count else 'all',
                       'mean': area[group] / (end - start), 'peak': peak})
    return result


def matrix_context(path):
    """Read declared experiment labels only from this run's parent matrix.

    A nearby metadata file alone is not evidence of membership. The matching
    plan must name this exact run. Relative plan paths are checked against the
    absolute run suffix, independently of the report caller's working directory.
    These labels do not certify physical cluster boundaries or affect pairing.
    """
    unclassified = {'suite': 'unknown', 'experiment_role': 'manual_or_unclassified',
                    'research_target': 'unspecified', 'source': 'no_parent_matrix'}
    absolute = path.resolve()
    metadata_path, plan_path = absolute.parent / 'metadata.json', absolute.parent / 'plan.json'
    if not metadata_path.is_file() or not plan_path.is_file():
        return unclassified
    try:
        metadata = json.loads(metadata_path.read_text())
        plan = json.loads(plan_path.read_text())
    except (OSError, ValueError):
        return {**unclassified, 'source': 'unreadable_parent_matrix'}
    if not isinstance(metadata, dict) or metadata.get('schema') != 'affinity-v2-matrix' or not isinstance(plan, list):
        return {**unclassified, 'source': 'unrecognized_parent_matrix'}
    matches = []
    for entry in plan:
        if not isinstance(entry, dict) or not isinstance(entry.get('directory'), str) or not entry['directory']:
            continue
        declared = Path(os.path.normpath(entry['directory']))
        if declared.is_absolute():
            matched = declared.resolve() == absolute
        else:
            parts = declared.parts
            matched = bool(parts) and '..' not in parts and absolute.parts[-len(parts):] == parts
        if matched:
            matches.append(entry)
    if not matches:
        return {**unclassified, 'source': 'not_listed_in_parent_matrix'}
    if len(matches) != 1:
        raise ValueError(f'{path}: duplicate run entries in parent matrix plan')
    labels = ('suite', 'experiment_role', 'research_target')
    if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in labels):
        return {**unclassified, 'source': 'parent_matrix_missing_role_labels'}
    return {**{key: metadata[key] for key in labels}, 'source': str(metadata_path)}


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
    if summary.get('arrival') == 'burst':
        require_fields(summary, ('burst_size', 'burst_spacing_us', 'work_contract'), str(path))
        if int(summary['burst_size']) < 1 or int(summary['burst_spacing_us']) < 0:
            raise ValueError(f'{path}: invalid burst contract')
        if summary['work_contract'].get('arrival') != 'burst':
            raise ValueError(f'{path}: burst arrival contract missing')

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
        if summary.get('sync_block_percent', 0):
            for row in tasks:
                require_fields(row, ('sync_block_target_us', 'sync_block_wall_ns'), str(path / 'tasks.csv'))
        if 'score_evaluations' in summary:
            for row in tasks:
                require_fields(row, ('score_evaluated', 'score_fallback', 'score_fallback_reason',
                                     'sampled_at_ns', 'sample_window_ns', 'cpu_valid', 'runqueue_valid'), str(path / 'tasks.csv'))
        seen = set()
        for row in tasks:
            for field in fields:
                if field != 'kind':
                    row[field] = integer(row[field], field)
            row['sync_block_target_us'] = integer(row.get('sync_block_target_us', 0), 'sync_block_target_us')
            row['sync_block_wall_ns'] = integer(row.get('sync_block_wall_ns', 0), 'sync_block_wall_ns')
            for field in ('callback_inflight_before', 'score_evaluated', 'score_fallback', 'sampled_at_ns',
                          'sample_window_ns', 'cpu_valid', 'runqueue_valid', 'runnable_valid'):
                row[field] = integer(row.get(field, 0), field)
            for field in ('sampled_cpu_cores', 'sampled_runqueue_wait_cores'):
                row[field] = float(row.get(field, 0))
                if not math.isfinite(row[field]) or row[field] < 0:
                    raise ValueError(f'{path}: invalid sampled load')
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
        if 'score_fallbacks' in summary and sum(row['score_fallback'] for row in tasks) != int(summary['score_fallbacks']):
            raise ValueError(f'{path}: score fallback counter mismatch')
        if 'score_evaluations' in summary and sum(row['score_evaluated'] for row in tasks) != int(summary['score_evaluations']):
            raise ValueError(f'{path}: score evaluation counter mismatch')
        if completed == len(requests) and 'planned_total_iterations' in summary:
            operators_all = [row for row in tasks if row['kind'] == 'operator']
            if sum(row['iterations'] for row in operators_all) != int(summary['planned_total_iterations']):
                raise ValueError(f'{path}: fixed compute work is not conserved')
            planes = {'none': 0, 'request-ro': 1, 'branch-rw': 1, 'mixed': 2}[summary['model']]
            accesses = int(summary['planned_total_accesses'])
            if sum(row['read_words'] for row in operators_all) != accesses * planes:
                raise ValueError(f'{path}: fixed read work is not conserved')
            if sum(row['write_words'] for row in operators_all) != (accesses if summary['model'] in ('branch-rw', 'mixed') else 0):
                raise ValueError(f'{path}: fixed write work is not conserved')
            if sum(row['sync_block_target_us'] for row in operators_all) != int(summary.get('planned_sync_block_us', 0)):
                raise ValueError(f'{path}: planned synchronous blocking is not conserved')
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
    occupancy_end = end if end is not None else max((row['finish_ns'] for row in tasks), default=start)
    return {'path': path, 'summary': summary, 'requests': requests, 'tasks': tasks,
            'matrix_context': matrix_context(path),
            'cohort': cohort, 'completed_cohort': complete_cohort, 'pairing_key': pairing_key,
            'callback_occupancy': callback_occupancy(tasks, len(summary['groups']), start, occupancy_end) if summary['trace'] else None,
            'metrics': {'n': len(complete_cohort), 'offered': len(cohort),
                        'mean_ms': mean(latencies), 'p50_ms': quantile(latencies, .50),
                        'p95_ms': quantile(latencies, .95), 'p99_ms': quantile(latencies, .99),
                        'wait_p99_ms': quantile(waits, .99), 'operator_cpu_mean_ms': mean(cpu),
                        'operator_wall_mean_ms': mean(wall),
                        'operator_cpu_per_request_ms': sum(cpu) / len(ids) if cpu and ids else None,
                        'operator_cpu_wall_ratio': sum(cpu) / sum(wall) if wall and sum(wall) > 0 else None,
                        'sync_block_target_ms': sum(row['sync_block_target_us'] for row in operators) / 1000,
                        'sync_block_wall_ms': sum(row['sync_block_wall_ns'] for row in operators) / 1e6,
                        'blocked_operators': sum(row['sync_block_target_us'] != 0 for row in operators),
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
    print('Matrix roles are declared experiment provenance, not certification of physical cluster boundaries.')
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
        print(f'  seed={summary["seed"]}, pool_count={len(summary["groups"])}, affinity={summary["affinity"]}, threads/pool={summary["threads_per_pool"]}, '
              f'whole completed/rejected-or-failed={summary["completed"]}/{summary["failed_or_rejected"]}, '
              f'operator CPU/request ms={format_value(metric["operator_cpu_per_request_ms"])}')
        context = run['matrix_context']
        print(f'  matrix suite={context["suite"]}, experiment_role={context["experiment_role"]}, '
              f'research_target={context["research_target"]}, provenance={context["source"]}')
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
        arrival = summary.get('arrival', summary.get('work_contract', {}).get('arrival', 'unknown'))
        burst_description = (f'{summary.get("burst_size", "-")}/{summary.get("burst_spacing_us", "-")}'
                             if arrival == 'burst' else 'not applicable')
        print(f'  arrival={arrival}, burst size/spacing us={burst_description}, '
              f'whole planned iterations/accesses={summary.get("planned_total_iterations", "unknown")}/{summary.get("planned_total_accesses", "unknown")}, '
              f'per-request mean={summary.get("planned_iterations_per_request", "unknown")}/{summary.get("planned_accesses_per_request", "unknown")}')
        if summary.get('fixed_work_signature'):
            fixed_digest = hashlib.sha256(summary['fixed_work_signature'].encode()).hexdigest()[:16]
            print(f'  fixed_work_sha256_prefix={fixed_digest}; equal fixed work does not imply equal arrival schedules or equal CPU service time.')
        if summary.get('sync_block_percent', 0) and summary['trace']:
            print(f'  COHORT synchronous callback blocking: operators={metric["blocked_operators"]}, '
                  f'target/wall sum ms={metric["sync_block_target_ms"]:.3f}/{metric["sync_block_wall_ms"]:.3f}, '
                  f'operator CPU/wall ratio={format_value(metric["operator_cpu_wall_ratio"])}; '
                  'controlled sleep retains a callback/thread, is not asynchronous RPC or evidence of kernel runnable count.')
        elif summary.get('sync_block_percent', 0):
            print('  Synchronous callback blocking configured; trace=0 has no measured per-operator blocking/occupancy data.')
        if run['callback_occupancy'] is not None:
            occupancy = ', '.join(f'{item["group"]}:mean={item["mean"]:.3f}/peak={item["peak"]}'
                                  for item in run['callback_occupancy'])
            print(f'  WALL-WINDOW callback occupancy (all overlapping tasks, includes sleeping/preempted): {occupancy}; not kernel runnable/on-CPU.')
        if summary.get('cpu_sampling'):
            print(f'  CPU sampling enabled: interval/max-age ms={summary.get("cpu_sample_ms")}/{summary.get("cpu_max_age_ms")}, '
                  f'whole-run sampler={json.dumps(summary.get("cpu_sampler", {}), sort_keys=True, separators=(",", ":"))}, '
                  f'fallbacks/evaluated={summary.get("score_fallbacks", "unknown")}/{summary.get("score_evaluations", "unknown")}, '
                  f'cpu-scored-without-fallback={summary.get("cpu_scored_without_fallback", "unknown")}, '
                  f'reasons={json.dumps(summary.get("score_fallback_reasons", {}), sort_keys=True, separators=(",", ":"))}')
            if not summary.get('cpu_sampler', {}).get('runqueue_accounting_enabled', False):
                print('  Runqueue accounting unavailable: CPU policy falls back to outstanding; this is not evidence that a CPU signal improved routing.')
            print('  Pinned/home/non-fork callbacks without score evaluation are excluded from the fallback denominator; published validity alone does not prove freshness.')
            if not summary['trace']:
                print('  trace=0: no task-level score/fallback evidence was collected; zero counters do not establish zero fallback.')
        scorer_suffix = ' (configured but unused: home bypasses L2 scoring)' if summary['policy'] == 'home' else ''
        print(f'  load scorer/units={summary.get("load_scorer", "legacy")}/{summary.get("load_score_units", "unknown")}{scorer_suffix}')
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
        if run['summary'].get('cpu_sampling', False) != home['summary'].get('cpu_sampling', False):
            print(f'  {run["path"].name}: CPU sampling differs from home; its measured overhead is included in this whole-policy comparison. Use sampling=1 on both for an overhead-controlled rerun.')
        first, second = run['completed_cohort'], home['completed_cohort']
        contract = lambda rows: [(row['request'], row['checksum']) for row in rows]
        if contract(first) != contract(second):
            raise ValueError(f'{run["path"]}: cohort request IDs/checksums differ from matched home')
        if run['summary']['trace']:
            task_contract = lambda item: sorted((row['request'], row['tag'], row['iterations'], row['read_words'], row['write_words'], row['checksum'], row['sync_block_target_us']) for row in item['tasks'])
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

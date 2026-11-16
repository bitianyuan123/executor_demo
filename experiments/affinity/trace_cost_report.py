#!/usr/bin/env python3
"""Audit paired trace=0/1 runs; report end-to-end instrumentation sensitivity.

This is an offline comparison of existing files, not a per-probe microbenchmark.
Task observations are unavailable at trace=0 and are never imputed as zero.
"""

import argparse
from collections import Counter
import importlib.util
import json
import math
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location('affinity_report', Path(__file__).with_name('report.py'))
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)

# Resolved fields written by affinity-v2, including defaults absent from options.
# Outcomes (CPU, timing, migrations, observations, sampler scans) are not inputs.
CONFIG_FIELDS = (
    'schema', 'seed', 'work_signature', 'work_contract', 'fixed_work_signature',
    'arrival_hash', 'affinity', 'model', 'requests', 'nodes_per_request', 'widths',
    'depths', 'groups', 'threads_per_pool', 'pool_count', 'qps', 'iterations',
    'accesses', 'arrival', 'burst_size', 'burst_spacing_us', 'page_query_enabled',
    'max_inflight', 'memory_mb', 'sync_block_percent', 'sync_block_us',
    'cpu_sampling', 'cpu_sample_ms', 'cpu_max_age_ms', 'routing',
    'planned_total_iterations', 'planned_total_accesses', 'planned_sync_block_us',
    'planned_blocked_operators', 'payload_bytes_per_request',
    'payload_budget_at_max_inflight', 'policy', 'balance_at', 'continuation',
    'load_scorer', 'load_score_units',
)
CANONICAL = ('home', 'fork-parent', 'all-parent')


def strategy_key(summary):
    return summary['policy'], summary['balance_at'], summary['continuation']


def strategy_name(summary):
    key = strategy_key(summary)
    names = {('home', 'all', 'parent'): 'home',
             ('outstanding', 'fork', 'parent'): 'fork-parent',
             ('outstanding', 'all', 'parent'): 'all-parent'}
    return names.get(key, '/'.join(key))


def validate_inputs(run):
    summary = run['summary']
    report.require_fields(summary, CONFIG_FIELDS, str(run['path']))
    options = summary['options']
    if not isinstance(options, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in options.items()):
        raise ValueError(f'{run["path"]}: options must be the original string-valued CLI arguments')
    for field, option, default in (('policy', 'policy', 'home'),
                                   ('balance_at', 'balance-at', 'all'),
                                   ('continuation', 'continuation', 'parent')):
        if not isinstance(summary[field], str) or summary[field] != options.get(option, default):
            raise ValueError(f'{run["path"]}: resolved {field} disagrees with options')
    if options.get('trace', '1') not in ('0', '1') or bool(int(options.get('trace', '1'))) != summary['trace']:
        raise ValueError(f'{run["path"]}: resolved trace disagrees with options')
    if int(summary['failed_or_rejected']) or not run['valid_for_comparison']:
        raise ValueError(f'{run["path"]}: whole-run failure/rejection or empty cohort; no valid trace-cost comparison')


def compare_contract(left, right, *, cross_strategy=False):
    a, b = left['summary'], right['summary']
    allowed_fields = {'policy', 'balance_at'} if cross_strategy else set()
    different = [key for key in CONFIG_FIELDS if key not in allowed_fields and a[key] != b[key]]
    if different:
        raise ValueError(f'{left["path"]} vs {right["path"]}: resolved configuration differs: {different}')
    ignored_options = {'trace', 'output'} | ({'policy', 'balance-at'} if cross_strategy else set())
    options_a = {key: value for key, value in a['options'].items() if key not in ignored_options}
    options_b = {key: value for key, value in b['options'].items() if key not in ignored_options}
    if options_a != options_b:
        keys = sorted(key for key in set(options_a) | set(options_b) if options_a.get(key) != options_b.get(key))
        raise ValueError(f'{left["path"]} vs {right["path"]}: CLI options differ beyond allowed changes: {keys}')
    # Aggregate hashes alone do not establish per-request equivalence. Compare
    # the complete run, including warmup and requests outside the report cohort.
    def contract(run):
        origin = int(run['summary']['origin_ns'])
        return [(row['request'], row['scheduled_ns'] - origin, row['status'], row['checksum'])
                for row in run['requests']]
    if contract(left) != contract(right):
        raise ValueError(f'{left["path"]} vs {right["path"]}: per-request schedule/status/checksum differs')
    if [row['request'] for row in left['cohort']] != [row['request'] for row in right['cohort']]:
        raise ValueError(f'{left["path"]} vs {right["path"]}: selected request cohorts differ')


def delta(candidate, baseline):
    return {'absolute': candidate - baseline,
            'percent': 100 * (candidate - baseline) / baseline if baseline else None}


def cpu_per_request(run):
    return float(run['summary']['process_cpu_s']) * 1000 / int(run['summary']['completed'])


def analyze(runs):
    """Validate every input before returning any valid comparison result."""
    grouped = {}
    for run in runs:
        validate_inputs(run)
        summary = run['summary']
        key = (summary['seed'], strategy_key(summary))
        trace = int(summary['trace'])
        pair = grouped.setdefault(key, {})
        if trace in pair:
            raise ValueError(f'duplicate trace={trace} for seed={summary["seed"]}, strategy={strategy_name(summary)}')
        pair[trace] = run
    if not grouped:
        raise ValueError('no runs supplied')
    pairs = []
    by_seed = {}
    for (seed, _), pair in sorted(grouped.items()):
        if set(pair) != {0, 1}:
            raise ValueError(f'missing trace pair for seed={seed}, strategy={strategy_name(next(iter(pair.values()))["summary"])}')
        off, on = pair[0], pair[1]
        compare_contract(off, on)
        name = strategy_name(off['summary'])
        differences = {key: delta(on['metrics'][key], off['metrics'][key])
                       for key in ('mean_ms', 'p95_ms', 'p99_ms')}
        differences['cpu_per_request_ms'] = delta(cpu_per_request(on), cpu_per_request(off))
        pairs.append({'seed': seed, 'strategy': name, 'off': off, 'on': on, 'differences': differences})
        by_seed.setdefault(seed, {})[name] = pair
    interactions = []
    incomplete = []
    for seed, strategies in sorted(by_seed.items()):
        if not all(name in strategies for name in CANONICAL):
            incomplete.append(seed)
            continue
        anchor = strategies['home'][0]
        for name in CANONICAL:
            for trace in (0, 1):
                compare_contract(anchor, strategies[name][trace], cross_strategy=True)
        effects = {trace: strategies['all-parent'][trace]['metrics']['mean_ms']
                   - strategies['fork-parent'][trace]['metrics']['mean_ms'] for trace in (0, 1)}
        interactions.append({'seed': seed, 'trace0_all_minus_fork_ms': effects[0],
                             'trace1_all_minus_fork_ms': effects[1],
                             'difference_in_differences_ms': effects[1] - effects[0]})
    return {'pairs': pairs, 'interactions': interactions, 'incomplete_interaction_seeds': incomplete}


def fmt(value):
    return 'n/a' if value is None else f'{value:.3f}'


def print_audit(runs):
    print('Whole-record evidence (before pairing); unsuccessful requests are never silently filtered:')
    for run in runs:
        summary = run['summary']
        statuses = dict(sorted(Counter(row['status'] for row in run['requests']).items()))
        lag_p99 = report.quantile([(row['submitted_ns'] - row['scheduled_ns']) / 1e6
                                  for row in run['requests']], .99)
        print(f'  {run["path"]}: seed={summary["seed"]} trace={int(summary["trace"])} '
              f'policy={summary["policy"]} balance_at={summary.get("balance_at", "missing")} '
              f'continuation={summary["continuation"]} whole_statuses={json.dumps(statuses, sort_keys=True)} '
              f'cohort_completed/offered={run["metrics"]["n"]}/{run["metrics"]["offered"]} '
              f'whole_attempt_lag_p99_ms={fmt(lag_p99)} peak_inflight={summary["peak_inflight"]}')


def print_result(result, warmup, window):
    print(f'Trace=1 minus trace=0; scheduled-request cohort warmup={warmup}s window={window}; late completions retained.')
    print('Latency values are differences of cohort means/quantiles, NOT quantiles of paired latency differences. '
          'CPU/request uses WHOLE-run execution, including warmup/drain; it is not cohort CPU. '
          'The execution timer excludes Driver construction/pool and trace-buffer allocation, and post-run CSV writing.')
    print('Task timing/routing observations are unavailable at trace=0, not zero. This measures end-to-end '
          'instrumentation sensitivity, including scheduling feedback; not pure probe nanoseconds, causal isolation or a confidence interval.')
    print('| seed / actual strategy | requests/trace | trace0 -> trace1 mean ms | '
          'delta mean ms/% | delta p95 ms/% | delta p99 ms/% | whole CPU/request delta ms/% |')
    for pair in result['pairs']:
        changes = pair['differences']
        entries = [f'{fmt(changes[key]["absolute"])}/{fmt(changes[key]["percent"])}%'
                   for key in ('mean_ms', 'p95_ms', 'p99_ms', 'cpu_per_request_ms')]
        print(f'| {pair["seed"]} / {pair["strategy"]} | {pair["off"]["metrics"]["n"]} | '
              f'{fmt(pair["off"]["metrics"]["mean_ms"])} -> {fmt(pair["on"]["metrics"]["mean_ms"])} | '
              + ' | '.join(entries) + ' |')
    print('Tail screen: each pair uses the printed request count; requests within bursts/DAGs are dependent. '
          'A nominal top 1% smaller than 10 requests (n<1000) is a low tail sample, not a ranking guarantee.')
    if result['interactions']:
        print('Complete, contract-matched six-cell comparison; negative all-fork means lower mean latency for all-parent:')
        print('| seed | trace0 all-fork mean ms | trace1 all-fork mean ms | (all-fork)trace1 - (all-fork)trace0 ms |')
        for row in result['interactions']:
            print(f'| {row["seed"]} | {fmt(row["trace0_all_minus_fork_ms"])} | '
                  f'{fmt(row["trace1_all_minus_fork_ms"])} | {fmt(row["difference_in_differences_ms"])} |')
        print('Difference in differences is descriptive strategy sensitivity to instrumentation, not a statistical interaction test or CI.')
    if result['incomplete_interaction_seeds']:
        print('All/fork interaction unavailable without the complete matched home/fork-parent/all-parent x trace0/1 six cells; '
              f'seeds={result["incomplete_interaction_seeds"]}. Valid individual trace pairs remain shown above.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--warmup-seconds', type=float, default=0)
    parser.add_argument('--window-seconds', type=float)
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error('warmup-seconds must be finite and nonnegative')
    if args.window_seconds is not None and (not math.isfinite(args.window_seconds) or args.window_seconds <= 0
                                            or round(args.window_seconds * 1e9) <= 0):
        parser.error('window-seconds must be finite and at least one rounded nanosecond')
    try:
        runs = [report.load_run(path, args.warmup_seconds, args.window_seconds) for path in args.runs]
        print_audit(runs)
        result = analyze(runs)
        print_result(result, args.warmup_seconds, args.window_seconds)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'affinity trace cost: comparison refused: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

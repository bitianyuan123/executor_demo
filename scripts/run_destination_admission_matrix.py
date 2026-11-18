#!/usr/bin/env python3
"""Plan/run the bounded destination-admission comparison; audit terminal results.

The normal experiment has twelve runs, not a general parameter sweep. Development
mode explicitly changes the work/resource contract and cannot yield target results.
"""

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import random
import shlex
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
AFFINITY = ROOT / 'experiments' / 'affinity'
SCHEMA = 'affinity-destination-admission-matrix-v1'
CPUS = '0-15;16-31;32-47;192-207;208-223;224-239'
SEEDS = (421, 422)
SHAPES = (('front80', '2,2'), ('tail4', '1,22'))
LIMITS = (('off', '0'), ('cap100', '1'), ('cap075', '0.75'))
DECISION_REASONS = {'Home', 'BelowBusyThreshold', 'ReturnHome', 'LocalSpill',
                    'CrossNumaSpill', 'InsufficientGain', 'HopBudget', 'PinnedHome',
                    'NonFork', 'NoEligibleTarget'}
FIXED = {
    'affinity': 'cluster', 'threads': '16', 'capacity-scale': '1',
    'requests': '240', 'max-inflight': '16', 'memory-mb': '512',
    'qps': '12', 'arrival': 'regular', 'widths': '80,4',
    'model': 'branch-rw', 'branch-kb': '64', 'shared-kb': '0',
    'iterations': '3000000', 'accesses': '100000',
    'jitter-percent': '0', 'long-every': '0', 'long-factor': '1',
    'work-scale-scope': 'task', 'estimate-mode': 'constant', 'home-policy': 'rr',
    'continuation': 'parent', 'balance-at': 'all', 'batch-chunk': '1',
    'policy': 'outstanding', 'busy-high': '1', 'return-low': '0.5', 'min-gain': '0',
    'cross-numa': '0', 'cross-numa-penalty': '0', 'max-hops': '4',
    'max-outstanding': '0', 'cpu-sampling': '0', 'trace': '1', 'page-sample': '0',
    'sync-block-percent': '0', 'sync-block-us': '0', 'async-percent': '0',
    'async-delay-us': '0', 'async-align-us': '0', 'async-capacity': '4096',
}
SOURCES = ('experiments/affinity/main.cpp',
           'experiments/affinity/include/executor_demo/RequestExecutor.h',
           'experiments/affinity/include/executor_demo/AdmissionPolicy.h',
           'experiments/affinity/include/executor_demo/LoadPolicy.h',
           'experiments/affinity/include/executor_demo/LocalityWorkload.h',
           'experiments/affinity/include/executor_demo/AsyncCompletionSource.h',
           'experiments/affinity/include/executor_demo/LinuxSchedstatLoadProvider.h',
           'experiments/dag/include/executor_demo/BusinessExecutor.h',
           'scripts/run_destination_admission_matrix.py')


def digest(path):
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def source_hashes():
    return {name: digest(ROOT / name) for name in SOURCES}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def cpu_partitions(value):
    groups, seen = [], set()
    for part in value.split(';'):
        cpus = []
        for item in part.split(','):
            endpoints = item.split('-')
            if not 1 <= len(endpoints) <= 2 or any(not x.isdecimal() for x in endpoints):
                raise ValueError('cpus requires nonempty integer/range partitions')
            first, last = int(endpoints[0]), int(endpoints[-1])
            if first > last or last > 1_000_000:
                raise ValueError('invalid CPU range')
            cpus.extend(range(first, last + 1))
        if len(cpus) != len(set(cpus)) or seen.intersection(cpus):
            raise ValueError('CPU partitions must be nonempty and disjoint')
        groups.append(sorted(cpus))
        seen.update(cpus)
    return groups


def make_plan(binary, output, options):
    plan = []
    for seed in SEEDS:
        cells = [(shape, depths, label, limit) for shape, depths in SHAPES
                 for label, limit in LIMITS]
        random.Random(seed).shuffle(cells)
        for shape, depths, label, limit in cells:
            case = f'{shape}-{label}'
            directory = output / f'seed-{seed}-{case}'
            values = {**options, 'depths': depths, 'target-limit': limit,
                      'seed': str(seed), 'output': str(directory)}
            command = [str(binary)] + [item for key, value in values.items()
                                      for item in ('--' + key, value)]
            plan.append({'seed': seed, 'case': case, 'shape': shape, 'label': label,
                         'target_limit': float(limit), 'directory': str(directory),
                         'options': values, 'command': command})
    return plan


def run_plan(plan, metadata, timeout):
    binary = Path(metadata['binary'])
    for ordinal, entry in enumerate(plan, 1):
        print(f'[{ordinal}/{len(plan)}] {shlex.join(entry["command"])}', flush=True)
        directory = Path(entry['directory'])
        before = {'binary_sha256': digest(binary), 'sources_sha256': source_hashes()}
        if any(before[key] != metadata[key] for key in before):
            raise ValueError('binary/source changed after planning; retain this matrix and use a fresh directory')
        directory.mkdir(exist_ok=False)
        receipt = {'schema': SCHEMA + '-receipt', 'ordinal': ordinal,
                   'command': entry['command'], **before, 'started_unix_s': time.time(),
                   'exit_code': None, 'timed_out': False}
        receipt_path = directory / 'execution.json'
        write_json(receipt_path, receipt)
        try:
            with (directory / 'run.log').open('w') as log:
                result = subprocess.run(entry['command'], stdout=log, stderr=subprocess.STDOUT,
                                        timeout=timeout, check=False)
            receipt['exit_code'] = result.returncode
        except subprocess.TimeoutExpired:
            receipt['timed_out'] = True
        except OSError as error:
            receipt['launch_error'] = str(error)
        receipt['finished_unix_s'] = time.time()
        receipt['after_binary_sha256'] = digest(binary)
        receipt['after_sources_sha256'] = source_hashes()
        write_json(receipt_path, receipt)
        if (receipt['exit_code'] != 0 or receipt['timed_out']
                or receipt.get('launch_error')):
            raise ValueError(f'run failed; no retry or overwrite: {directory}/run.log; '
                             f'exit={receipt["exit_code"]}, timeout={receipt["timed_out"]}')
        if receipt['after_binary_sha256'] != before['binary_sha256'] or receipt['after_sources_sha256'] != before['sources_sha256']:
            raise ValueError(f'build changed during run; excluded: {directory}')
        summary = json.loads((directory / 'summary.json').read_text())
        if summary.get('completed') != int(entry['options']['requests']) or summary.get('failed_or_rejected') != 0:
            raise ValueError(f'incomplete/failed requests; stop and retain {directory}')
        if summary.get('routing', {}).get('target_limit') != entry['target_limit']:
            raise ValueError(f'target_limit absent or inconsistent; stop and retain {directory}')
        print(f'  completed={summary["completed"]}; mean_ms={summary["request_mean_ms"]:.3f}; '
              f'p99_ms={summary["request_p99_ms"]:.3f}; CPU_s={summary["process_cpu_s"]:.6f}', flush=True)


def module(name):
    spec = importlib.util.spec_from_file_location('destination_' + name, AFFINITY / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def validate_plan(root, metadata, plan):
    if metadata.get('schema') != SCHEMA or len(plan) != 12:
        raise ValueError('requires a complete twelve-run destination-admission plan')
    expected = make_plan(Path(metadata['binary']), root, metadata['fixed_options'])
    if plan != expected or metadata.get('plan_sha256') != json_digest(plan):
        raise ValueError('plan/configuration/ordering differs from the recorded fixed contract')
    if not metadata.get('binary_sha256') or any(not value for value in metadata['sources_sha256'].values()):
        raise ValueError('recorded binary/source identity is missing')
    if not metadata['development'] and any(metadata['fixed_options'].get(key) != value
                                           for key, value in FIXED.items()):
        raise ValueError('target metadata changed the fixed experiment contract')


def validate_admission(admission, limit):
    if (not isinstance(admission, dict) or admission.get('enabled') != (limit > 0)
            or admission.get('limit') != limit or not isinstance(admission.get('stats'), dict)
            or not isinstance(admission.get('decision_reasons'), dict)):
        raise ValueError('missing/inconsistent target_admission diagnostics')
    stats, reasons = admission['stats'], admission['decision_reasons']
    required = ('selections', 'evaluated_selections', 'candidate_checks', 'candidate_score_invalid',
                'candidate_invalid', 'candidate_over_limit', 'candidate_insufficient_gain',
                'return_home_rejected', 'no_eligible_target')
    if set(stats) != set(required) or set(reasons) != DECISION_REASONS:
        raise ValueError('incomplete target admission counters')
    expected_policy = 'outstanding-after' if limit else 'disabled'
    expected_units = 'outstanding_reserved_incoming_tasks_per_capacity' if limit else 'none'
    if admission.get('policy') != expected_policy or admission.get('units') != expected_units:
        raise ValueError('admission policy/units differ from the O-count experiment')
    if any(type(value) is not int or value < 0 for value in (*stats.values(), *reasons.values())):
        raise ValueError('admission counters must be nonnegative integers')
    if (sum(reasons.values()) != stats['selections']
            or reasons['NoEligibleTarget'] != stats['no_eligible_target']
            or stats['evaluated_selections'] > min(stats['selections'], stats['candidate_checks'])
            or stats['return_home_rejected'] > stats['candidate_checks']
            or sum(stats[key] for key in ('candidate_score_invalid', 'candidate_invalid',
                                          'candidate_over_limit', 'candidate_insufficient_gain')) > stats['candidate_checks']):
        raise ValueError('admission selection/reason conservation mismatch')
    if limit == 0 and any((*stats.values(), *reasons.values())):
        raise ValueError('disabled admission must not report active diagnostics')


def role_wait_distributions(summary, tasks, flow):
    labels = {0: 'init'}
    offset = 1
    for wave, (width, depth) in enumerate(zip(summary['widths'], summary['depths'])):
        for stage in range(depth):
            label = 'ordinary successors' if stage else ('later-wave stage0' if wave else 'first-wave stage0')
            for branch in range(width):
                labels[offset + stage * width + branch] = label
        labels[offset + width * depth] = 'join'
        offset += width * depth + 1
    grouped = {name: [] for name in flow.ROLES}
    for task in tasks:
        grouped[labels[task['tag']]].append((task['start_ns'] - task['submitted_ns']) / 1e6)
    return {name: flow.distribution(values) for name, values in grouped.items()}


def summarize_run(entry, metadata, warmup, window, report, flow, phase):
    path = Path(entry['directory'])
    receipt = json.loads((path / 'execution.json').read_text())
    if (receipt.get('schema') != SCHEMA + '-receipt' or receipt.get('command') != entry['command']
            or receipt.get('exit_code') != 0 or receipt.get('timed_out') is not False):
        raise ValueError(f'{path}: missing or unsuccessful execution receipt')
    for key in ('binary_sha256', 'sources_sha256'):
        if receipt.get(key) != metadata[key] or receipt.get('after_' + key) != metadata[key]:
            raise ValueError(f'{path}: build identity differs from matrix')
    run = report.load_run(path, warmup, window)
    summary = run['summary']
    if summary['options'] != entry['options']:
        raise ValueError(f'{path}: actual options differ from plan; do not mix old/manual runs')
    if (not run['valid_for_comparison'] or summary['failed_or_rejected'] != 0
            or summary['schema'] != 'affinity-v2' or not summary['trace']):
        raise ValueError(f'{path}: failed/incomplete or incompatible run')
    if (summary['policy'] != 'outstanding' or summary['routing'].get('target_limit') != entry['target_limit']
            or summary['seed'] != entry['seed'] or summary['requests'] != int(entry['options']['requests'])
            or summary['threads_per_pool'] != int(entry['options']['threads'])
            or summary['affinity'] != 'cluster' or summary['continuation'] != 'parent'
            or summary.get('balance_at') != 'all'
            or summary['widths'] != [80, 4]
            or summary['depths'] != list(map(int, entry['options']['depths'].split(',')))):
        raise ValueError(f'{path}: actual workload/routing differs from plan')
    groups = cpu_partitions(entry['options']['cpus'])
    if [sorted(group['cpus']) for group in summary['groups']] != groups:
        raise ValueError(f'{path}: actual CPU groups differ from plan')
    if any(group['capacity'] != min(int(entry['options']['threads']), len(cpus))
           for group, cpus in zip(summary['groups'], groups)):
        raise ValueError(f'{path}: score capacity differs from fixed capacity-scale=1')
    if not metadata['development']:
        if (len(groups) != 6 or any(len(group) != 16 for group in groups)
                or any(type(group['numa']) is not int or group['numa'] < 0 for group in summary['groups'])
                or sorted(Counter(group['numa'] for group in summary['groups']).values()) != [3, 3]
                or len(run['completed_cohort']) != 192):
            raise ValueError(f'{path}: target requires six groups in two NUMAs and 192 cohort requests')
    admission = summary.get('target_admission')
    validate_admission(admission, entry['target_limit'])
    if entry['target_limit'] > 0 and admission['stats']['selections'] != summary['observed_tasks']:
        raise ValueError(f'{path}: this successful chunk=1 matrix requires selections == callbacks')
    cohort_ids = {row['request'] for row in run['completed_cohort']}
    tasks = [row for row in run['tasks'] if row['request'] in cohort_ids]
    roles = flow.task_roles(summary, tasks)
    waits = role_wait_distributions(summary, tasks, flow)
    for name, role in roles.items():
        role['wait_distribution_ms'] = waits[name]
    overlap = phase.analyze(run)
    task_identity = [(row['request'], row['tag'], row['iterations'], row['read_words'],
                      row['write_words'], row['checksum']) for row in run['tasks']]
    work_digest = json_digest(sorted(task_identity))
    request_digest = json_digest([(row['request'], row['checksum']) for row in run['requests']])
    normalized_options = {key: value for key, value in summary['options'].items()
                          if key not in ('target-limit', 'output')}
    pairing = {'seed': entry['seed'], 'shape': entry['shape'],
               'options': normalized_options, 'groups': summary['groups'],
               'threads_per_pool': summary['threads_per_pool'], 'affinity': summary['affinity'],
               'continuation': summary['continuation'], 'balance_at': summary['balance_at'],
               'routing': {key: value for key, value in summary['routing'].items() if key != 'target_limit'},
               'binary_sha256': receipt['binary_sha256'], 'sources_sha256': receipt['sources_sha256'],
               'work_signature': summary['work_signature'], 'arrival_hash': summary['arrival_hash'],
               'work_sha256': work_digest, 'request_checksums_sha256': request_digest,
               'cohort_ids': sorted(cohort_ids)}
    return {'schema': SCHEMA + '-run', 'case': entry['case'], 'seed': entry['seed'],
            'shape': entry['shape'], 'target_limit': entry['target_limit'],
            'run': str(path), 'development': metadata['development'],
            'resources': {'groups': summary['groups'], 'threads_per_pool': summary['threads_per_pool'],
                          'affinity': summary['affinity'], 'continuation': summary['continuation'],
                          'balance_at': summary['balance_at']},
            'cohort_seconds': [warmup, None if window is None else warmup + window],
            'cohort': run['metrics'], 'latency_breakdown': flow.latency_breakdown(run['cohort']),
            'whole_run': {'completed': summary['completed'], 'failed_or_rejected': summary['failed_or_rejected'],
                          'process_cpu_s': summary['process_cpu_s'],
                          'cpu_per_completed_request_ms': summary['process_cpu_s'] * 1000 / summary['completed'],
                          'peak_inflight': summary['peak_inflight'], 'target_admission': admission},
            'roles': roles, 'phase_overlap': overlap['scopes'],
            'build_sha256': receipt['binary_sha256'], 'pairing_sha256': json_digest(pairing),
            'work_sha256': work_digest, 'request_checksums_sha256': request_digest,
            'source_sha256': {name: digest(path / name) for name in ('summary.json', 'requests.csv', 'tasks.csv', 'execution.json')}}


def paired_deltas(rows):
    expected = {(seed, shape, float(limit)) for seed in SEEDS for shape, _ in SHAPES for _, limit in LIMITS}
    if {(row['seed'], row['shape'], row['target_limit']) for row in rows} != expected or len(rows) != 12:
        raise ValueError('incomplete or duplicate experiment cells')
    output = []
    for seed in SEEDS:
        for shape, _ in SHAPES:
            group = [row for row in rows if row['seed'] == seed and row['shape'] == shape]
            if len({row['pairing_sha256'] for row in group}) != 1:
                raise ValueError(f'{seed}/{shape}: resources/build/work/checksum/arrival contract differs')
            baseline = next(row for row in group if row['target_limit'] == 0)
            for row in group:
                if row['target_limit'] == 0:
                    continue
                output.append({'schema': SCHEMA + '-pair', 'seed': seed, 'shape': shape,
                               'target_limit': row['target_limit'], 'baseline': baseline['case'],
                               'candidate': row['case'], 'development': row['development'],
                               'cohort_delta_ms': {key: row['cohort'][key] - baseline['cohort'][key]
                                                   for key in ('mean_ms', 'p95_ms', 'p99_ms')},
                               'whole_run_cpu_per_request_delta_ms': row['whole_run']['cpu_per_completed_request_ms'] - baseline['whole_run']['cpu_per_completed_request_ms'],
                               'cohort_reroutes_delta': row['cohort']['migrations'] - baseline['cohort']['migrations'],
                               'scope': 'candidate minus same-seed/same-shape off; descriptive, no confidence interval or production claim'})
    return output


def print_report(root, warmup, window):
    metadata = json.loads((root / 'metadata.json').read_text())
    plan = json.loads((root / 'plan.json').read_text())
    validate_plan(root, metadata, plan)
    if not metadata['development'] and (warmup != 2 or window != 16):
        raise ValueError('target comparison requires the common planned-arrival [2,18) second cohort')
    report, flow, phase = module('report'), module('flow_report'), module('request_phase_overlap')
    rows = [summarize_run(entry, metadata, warmup, window, report, flow, phase) for entry in plan]
    pairs = paired_deltas(rows)
    # Nothing is printed as a usable comparison until the whole block passes.
    header = {'schema': SCHEMA + '-report', 'runs': len(rows), 'pairs': len(pairs),
              'development': metadata['development'], 'binary_sha256': metadata['binary_sha256'],
              'git_head': metadata['git_head'], 'metadata_sha256': digest(root / 'metadata.json'),
              'plan_sha256': digest(root / 'plan.json'),
              'fixed_options': metadata['fixed_options'], 'cpu_partitions': metadata['cpu_partitions'],
              'work_contract': metadata['work_contract'],
              'execution_sources_sha256': metadata['sources_sha256'],
              'analysis_sources_sha256': {str(path.relative_to(ROOT)): digest(path) for path in
                                         (Path(__file__).resolve(), AFFINITY / 'report.py',
                                          AFFINITY / 'flow_report.py', AFFINITY / 'request_phase_overlap.py')},
              'contract': 'latency/roles/overlap use planned-arrival cohort; CPU and admission counters use whole run; '
                          'reroutes are task placements, not kernel migrations; overlap is callback wall time, not CPU or causal delay; '
                          'two seeds are mechanism screening, not stable p99 validation'}
    for row in (header, *rows, *pairs):
        print(json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path, help='new directory; default action writes a plan only')
    mode.add_argument('--report', type=Path, help='read/audit an existing completed matrix; print JSON lines')
    parser.add_argument('--binary', type=Path, default=Path('build-affinity/affinity_demo'))
    parser.add_argument('--cpus', help='semicolon-separated disjoint CPU partitions; normal experiment requires six groups of 16')
    parser.add_argument('--run', action='store_true', help='execute the newly prepared plan')
    parser.add_argument('--development', action='store_true', help='explicit functional-only resource/work override')
    for name in ('threads', 'requests', 'iterations', 'accesses'):
        parser.add_argument('--' + name, type=int, help='development mode only')
    parser.add_argument('--qps', type=float, help='development mode only')
    parser.add_argument('--timeout-seconds', type=float, default=900)
    parser.add_argument('--warmup-seconds', type=float, default=2, help='report only')
    parser.add_argument('--window-seconds', default='16', help='report only; all for development full cohort')
    args = parser.parse_args()
    try:
        window = None if args.window_seconds == 'all' else float(args.window_seconds)
        if (not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0
                or (window is not None and (not math.isfinite(window) or window <= 0))):
            raise ValueError('report window must be finite, warmup nonnegative and duration positive')
        if args.report:
            if args.run or args.development or args.cpus is not None or any(getattr(args, key) is not None for key in ('threads', 'requests', 'iterations', 'accesses', 'qps')):
                raise ValueError('report is read-only; cannot combine with run/development/work overrides')
            print_report(args.report.resolve(), args.warmup_seconds, window)
            return 0
        if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
            raise ValueError('timeout must be finite and positive')
        if args.output.exists():
            raise ValueError('output already exists (even if empty); preserve it and choose a new directory')
        if not args.development and any(getattr(args, key) is not None for key in ('threads', 'requests', 'iterations', 'accesses', 'qps')):
            raise ValueError('work/resource overrides require --development; target contract is fixed')
        if args.development and args.cpus is None:
            raise ValueError('development requires explicit --cpus; artificial groups are functional only')
        cpus = CPUS if args.cpus is None else args.cpus
        groups = cpu_partitions(cpus)
        if not args.development and (len(groups) != 6 or any(len(group) != 16 for group in groups)):
            raise ValueError('target contract requires six disjoint groups of 16 CPUs; use --development for artificial groups')
        if len(groups) < 2:
            raise ValueError('at least two CPU groups are required to exercise destination selection')
        options = {**FIXED, 'cpus': cpus}
        if args.development:
            defaults = {'threads': 1, 'requests': 4, 'iterations': 1000, 'accesses': 32, 'qps': 50}
            maxima = {'threads': 100000, 'requests': 2_000_000, 'iterations': 10**12, 'accesses': 10**12, 'qps': 10**9}
            for key, default in defaults.items():
                value = getattr(args, key)
                value = default if value is None else value
                if not math.isfinite(value) or value <= 0 or value > maxima[key]:
                    raise ValueError('development values must be positive, finite and within demo bounds')
                options[key] = str(value)
        allowed = sorted(os.sched_getaffinity(0))
        if args.run and not set(sum(groups, [])).issubset(allowed):
            raise ValueError('requested CPUs are outside caller affinity; set taskset correctly before running')
        binary = args.binary.resolve()
        if args.run and (not binary.is_file() or not os.access(binary, os.X_OK)):
            raise ValueError('binary missing or not executable; build affinity_demo first')
        output = args.output.resolve()
        plan = make_plan(binary, output, options)
        git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True, check=False)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, text=True, capture_output=True, check=False)
        metadata = {'schema': SCHEMA, 'suite': 'destination-admission', 'experiment_role': 'controlled_comparison',
                    'research_target': 'one_pool_per_cluster', 'development': args.development,
                    'binary': str(binary), 'binary_sha256': digest(binary), 'sources_sha256': source_hashes(),
                    'git_head': git.stdout.strip(), 'git_status': status.stdout.splitlines(),
                    'fixed_options': options, 'seeds': list(SEEDS), 'rounds': 12,
                    'cpu_partitions': groups, 'caller_allowed_cpus': allowed,
                    'machine': platform.machine(), 'kernel': platform.release(),
                    'plan_sha256': json_digest(plan), 'argv': sys.argv[1:],
                    'work_contract': {'business_operators_per_request': 168, 'callbacks_per_request': 171,
                                      'branches': 84, 'branch_payload_bytes': 84 * 64 * 1024,
                                      'iterations_per_request': 168 * int(options['iterations']),
                                      'read_and_write_words_each_per_request': 168 * int(options['accesses']),
                                      'validation': 'mechanism experiment, not production replay or equal measured CPU work'}}
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / 'metadata.json', metadata)
        write_json(output / 'plan.json', plan)
        print('DEVELOPMENT: functional checks only; no target performance claims.' if args.development else
              'TARGET: 12 runs, two existing graphs, instantaneous O; target-limit off/1/0.75. No production threshold claim.', flush=True)
        if args.run:
            run_plan(plan, metadata, args.timeout_seconds)
            print('Report: ' + shlex.join([sys.executable, str(Path(__file__).resolve()), '--report', str(output)] +
                  (['--warmup-seconds', '0', '--window-seconds', 'all'] if args.development else [])))
        else:
            for ordinal, entry in enumerate(plan, 1):
                print(f'[{ordinal}/12] {shlex.join(entry["command"])}')
            print('Plan only. To execute, repeat with --run and a DIFFERENT new output directory; plans are never resumed/overwritten.')
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'destination admission matrix: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

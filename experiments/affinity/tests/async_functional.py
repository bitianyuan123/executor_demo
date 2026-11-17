#!/usr/bin/env python3
"""Bounded async integration checks on two allowed CPUs, never a benchmark."""

import argparse
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


AFFINITY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('affinity_async_report', AFFINITY / 'report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def selected_tags(seed, request, tags, percent):
    """Expected logical positions; selection must not depend on placement/time."""
    return {tag for tag in tags
            if report.mix(seed ^ report.mix(request) ^ report.mix(tag) ^ 0xa510c0de) % 100 < percent}


def business_rows(run):
    return sorted((row['request'], row['tag'], row['kind'], row['iterations'],
                   row['read_words'], row['write_words'], row['checksum'])
                  for row in run['tasks'] if row['kind'] != 'rpc_resume')


def run_checks(binary, root, cpus):
    # Artificial one-CPU partitions exercise API and lifetime contracts only.
    # Their relation to physical clusters/NUMA is not assumed.
    common = {
        'cpus': f'{cpus[0]};{cpus[1]}', 'affinity': 'cluster', 'threads': '1',
        'requests': '4', 'qps': '200', 'max-inflight': '4', 'memory-mb': '8',
        'widths': '3,2', 'depths': '2,2', 'model': 'branch-rw', 'branch-kb': '4',
        'shared-kb': '0', 'iterations': '2000', 'accesses': '20',
        'jitter-percent': '25', 'long-every': '5', 'long-factor': '3',
        'seed': '1234', 'home-policy': 'rr', 'policy': 'home',
        'continuation': 'parent', 'balance-at': 'all', 'cross-numa': '0',
        'trace': '1', 'cpu-sampling': '0', 'page-sample': '0',
        'async-percent': '0', 'async-delay-us': '0', 'async-align-us': '0',
        'async-capacity': '64',
    }

    def command_log(name, command, timeout=30):
        (root / f'{name}.command.txt').write_text(shlex.join(command) + '\n')
        try:
            process = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            for suffix, content in (('stdout', error.stdout), ('stderr', error.stderr)):
                if isinstance(content, bytes):
                    content = content.decode(errors='replace')
                (root / f'{name}.{suffix}.log').write_text(content or '')
            raise AssertionError(f'{name}: exceeded {timeout}s; possible stalled drain') from error
        (root / f'{name}.stdout.log').write_text(process.stdout)
        (root / f'{name}.stderr.log').write_text(process.stderr)
        return process

    def execute(name, updates, success=True):
        options = {**common, **updates, 'output': str(root / name)}
        command = [binary] + [part for key, value in options.items()
                              for part in (f'--{key}', str(value))]
        process = command_log(name, command)
        if success:
            check(process.returncode == 0,
                  f'{name}: exit={process.returncode}: {process.stderr[-4000:]}')
        else:
            check(process.returncode != 0, f'{name}: expected a recorded registration rejection')
        check((root / name / 'summary.json').is_file(), f'{name}: missing summary')
        run = report.load_run(root / name)
        summary = run['summary']
        check(summary['threads_per_pool'] == 1 and len(summary['groups']) == 2,
              f'{name}: changed artificial test resources')
        check([group['cpus'] for group in summary['groups']] == [[cpus[0]], [cpus[1]]],
              f'{name}: CPU partitions changed')
        check(len(run['requests']) == 4, f'{name}: missing request terminal records')
        if success:
            check(summary['completed'] == 4 and summary['failed_or_rejected'] == 0,
                  f'{name}: successful run did not complete all requests')
        return run

    variants = [
        ('d0-direct', {}),
        ('a0-zero-delay', {'async-percent': '100'}),
        ('a1-delayed', {'async-percent': '100', 'async-delay-us': '2000'}),
        ('aligned-parent-all', {'async-percent': '100', 'async-delay-us': '1000',
                                'async-align-us': '2000', 'policy': 'outstanding'}),
        ('half-home', {'async-percent': '50', 'async-delay-us': '2000'}),
        ('half-return-all', {'async-percent': '50', 'async-delay-us': '2000',
                             'policy': 'outstanding', 'continuation': 'home'}),
        ('half-parent-fork', {'async-percent': '50', 'async-delay-us': '2000',
                              'policy': 'outstanding', 'balance-at': 'fork'}),
        ('a1-trace0', {'async-percent': '100', 'async-delay-us': '2000', 'trace': '0'}),
    ]
    runs = [execute(name, changes) for name, changes in variants]
    baseline = runs[0]
    nodes = baseline['summary']['nodes_per_request']
    check(nodes == 13, 'functional graph unexpectedly changed')
    operators = {row['tag'] for row in baseline['tasks'] if row['kind'] == 'operator'}
    check(len(operators) == 10, 'functional graph operator count changed')
    base_work = business_rows(baseline)
    request_checksums = [(row['request'], row['checksum']) for row in baseline['requests']]
    half_positions = None
    for run in runs:
        name, summary = run['path'].name, run['summary']
        for field in ('fixed_work_signature', 'arrival_hash', 'checksum',
                      'planned_total_iterations', 'planned_total_accesses'):
            check(summary[field] == baseline['summary'][field], f'{name}: changed {field}')
        check([(row['request'], row['checksum']) for row in run['requests']] == request_checksums,
              f'{name}: per-request business checksums changed')
        if summary['trace']:
            check(business_rows(run) == base_work, f'{name}: actual business task work changed')
        else:
            check(not (run['path'] / 'tasks.csv').exists()
                  and not (run['path'] / 'async.csv').exists(),
                  f'{name}: trace0 unexpectedly wrote per-event files')
            check(run['async_events'] is None and not run['tasks'],
                  f'{name}: unavailable trace was fabricated')

        contract = summary.get('async_contract')
        if contract is None:
            check(summary['schema'] == 'affinity-v2', 'D0 must retain v2 compatibility')
            check(run['async_events'] is None and run['selected_resumes'] is None,
                  'D0 has an unexpected async contract')
            continue
        check(summary['schema'] == 'affinity-v3-async', f'{name}: missing async schema')
        selected = [selected_tags(1234, request, operators, contract['percent'])
                    for request in range(4)]
        counts = [len(tags) for tags in selected]
        expected = sum(counts)
        check(run['selected_resumes'] == selected, f'{name}: selection depends on execution')
        check(summary['planned_resume_counts'] == counts and summary['planned_resumes'] == expected,
              f'{name}: planned resume counts changed')
        source = summary['async_source']
        check(source['registered'] == source['fired'] == source['resume_succeeded'] == expected,
              f'{name}: registered/fired/resume counts do not match selected positions')
        check(all(source[field] == 0 for field in
                  ('pending', 'canceled', 'rejected', 'resume_failed',
                   'registration_rejected', 'callback_failures')),
              f'{name}: successful source did not drain cleanly')
        if contract['percent'] == 50:
            check(0 < expected < 4 * len(operators), '50% fixture selected all or no operators')
            if half_positions is None:
                half_positions = selected
            else:
                check(selected == half_positions, f'{name}: policy changed 50% selection')
        if not summary['trace']:
            continue
        check(len(run['tasks']) == 4 * nodes + expected,
              f'{name}: original callbacks plus resumes are not conserved')
        events = run['async_events']
        check(len(events) == expected and all(event['status'] == 'resumed' for event in events),
              f'{name}: event completion set differs from the resume plan')
        event_ids = {(event['request'], event['logical_tag']) for event in events}
        expected_ids = {(request, tag) for request, tags in enumerate(selected) for tag in tags}
        check(event_ids == expected_ids, f'{name}: event identities differ from deterministic selection')
        task_lookup = {(row['request'], row['tag']): row for row in run['tasks']}
        for row in run['tasks']:
            if summary['policy'] == 'home':
                check(row['chosen'] == row['home'], f'{name}: home policy moved a callback')
            if row['kind'] != 'rpc_resume':
                continue
            original = task_lookup[(row['request'], row['logical_tag'])]
            check(row['tag'] == nodes + row['logical_tag'], f'{name}: resume overwrote operator tag')
            check(row['source'] == original['chosen'] and row['home'] == original['home'],
                  f'{name}: resume lost captured source/home')
            check(all(row[field] == 0 for field in ('iterations', 'read_words', 'write_words')),
                  f'{name}: resume duplicated business work')
            if summary['balance_at'] == 'fork':
                check(row['chosen'] == row['source'], f'{name}: ordinary resume balanced in fork-only mode')
        for event in events:
            minimum = event['issue_ns'] + contract['delay_us'] * 1000
            grid = contract['align_us'] * 1000
            expected_due = (summary['origin_ns'] +
                            ((minimum - summary['origin_ns'] + grid - 1) // grid) * grid
                            if grid else minimum)
            check(event['due_ns'] == expected_due, f'{name}: changed fixed/aligned due contract')
            check(event['fired_ns'] >= event['due_ns'], f'{name}: completion fired before its due time')

    # A deliberately long pending event leaves room for a second branch to hit
    # capacity 1. Cancellation should then drain it without sleeping 10 seconds.
    # Exact capacity/cancellation races are covered by the component unit test;
    # this integration test asserts recorded failure/accounting, never latency.
    rejected = execute('capacity-rejected', {'async-percent': '100',
                        'async-delay-us': '10000000', 'async-capacity': '1'}, success=False)
    summary = rejected['summary']
    source = summary['async_source']
    check(summary['failed_or_rejected'] > 0 and not rejected['valid_for_comparison'],
          'capacity rejection was lost or became a performance sample')
    check(source['registration_rejected'] == source['rejected'] > 0,
          'capacity fixture did not exercise async registration rejection')
    check(source['pending'] == 0 and source['callback_failures'] == 0,
          'capacity rejection leaked pending work or source exception')
    check(source['registered'] == source['fired'] + source['canceled'],
          'capacity rejection lost an accepted source event')
    check(source['fired'] == source['resume_succeeded'] + source['resume_failed'],
          'capacity rejection lost a fired event')
    check(any(row['status'] == 'async_register_rejected' for row in rejected['requests']),
          'request terminal status did not identify async registration rejection')
    check(any(row['status'] == 'register_rejected' for row in rejected['async_events']),
          'event terminal status did not identify registration rejection')
    all_runs = [*runs, rejected]

    # Exercise command-line consumers as well as load_run. Keep their verbose
    # tiny-sample output in failure artifacts; this script publishes no ranking.
    paths = [str(run['path']) for run in all_runs]
    for tool, arguments in (
            ('report.py', []), ('flow_report.py', []),
            ('diagnose.py', ['--max-bins', '30']),
            ('arrival_audit.py', ['--warmup-seconds', '0', '--window-seconds', '1', '--top', '0'])):
        process = command_log('analyzer-' + tool.removesuffix('.py'),
                              [sys.executable, str(AFFINITY / tool), *paths, *arguments])
        check(process.returncode == 0, f'{tool}: rejected valid async/failure output: {process.stderr[-4000:]}')

    print(f'Artificial CPU partitions: {cpus[0]};{cpus[1]}; 1 worker/pool. Functional checks only.')
    for run in runs:
        summary = run['summary']
        callbacks = len(run['tasks']) if summary['trace'] else 'unavailable(trace0)'
        print(f'PASS {run["path"].name}: completed=4/4, '
              f'planned_resumes={summary.get("planned_resumes", 0)}, callbacks={callbacks}')
    print('PASS capacity-rejected: expected nonzero demo exit, failure retained, source drained, no timing comparison')
    print('PASS analyzers: report, flow_report, diagnose, arrival_audit accept all nine results')
    print('affinity async functional: 8 successful runs + 1 expected rejection; '
          'fixed business work/checksums, deterministic 50% selection, resume counts/context, '
          'due contracts and trace0 passed. No performance assertions.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--keep-output', action='store_true', help='also preserve successful test output')
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error(f'not an executable: {binary}')
    cpus = sorted(os.sched_getaffinity(0))[:2]
    if len(cpus) < 2:
        parser.error('two allowed logical CPUs are required; no hardware topology is assumed')
    root = Path(tempfile.mkdtemp(prefix='affinity-async-functional-'))
    try:
        run_checks(str(binary), root, cpus)
    except Exception as error:
        print(f'affinity async functional FAILED: {type(error).__name__}: {error}', file=sys.stderr)
        print(f'Artifacts retained: {root}', file=sys.stderr)
        return 1
    if args.keep_output:
        print(f'Artifacts retained: {root}')
    else:
        shutil.rmtree(root)
    return 0


if __name__ == '__main__':
    sys.exit(main())

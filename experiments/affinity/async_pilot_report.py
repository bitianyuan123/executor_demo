#!/usr/bin/env python3
"""Audit one complete eight-cell async pilot and print bounded terminal evidence.

This is a descriptive fixed-business-work experiment, not production replay,
a confidence interval, or a pure measurement-probe/RPC overhead benchmark.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys


def sibling(name):
    spec = importlib.util.spec_from_file_location('async_pilot_' + name,
                                                Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report, flow, cost = (sibling(name) for name in ('report', 'flow_report', 'trace_cost_report'))
PHASES = ('d0', 'a0', 'a1')
POLICIES = ('home', 'outstanding')
CELLS = tuple((phase, policy, 0) for phase in PHASES for policy in POLICIES) + (
    ('a0', 'home', 1), ('a1', 'home', 1))


def cell_name(cell):
    phase, policy, trace = cell
    return f'{phase}-{policy}-trace{trace}'


def selected_cell(run):
    """Classify audited resolved inputs, never directory names or matrix labels."""
    summary, options = run['summary'], run['summary']['options']
    report.require_fields(summary, ('duration_with_drain_s',), str(run['path']))
    if not math.isfinite(float(summary['duration_with_drain_s'])) or float(summary['duration_with_drain_s']) < 0:
        raise ValueError(f'{run["path"]}: invalid whole duration including drain')
    if summary['schema'] == 'affinity-v2':
        phase = 'd0'
        expected = {'percent': 0, 'delay_us': 0, 'align_us': 0, 'capacity': 4096}
        if any(field in summary for field in ('async_contract', 'async_source', 'planned_resumes')):
            raise ValueError(f'{run["path"]}: D0 unexpectedly has an async source contract')
    else:
        contract = summary['async_contract']
        phase = {0: 'a0', 2000: 'a1'}.get(contract['delay_us'])
        if phase is None:
            raise ValueError(f'{run["path"]}: pilot requires async delay 0 or 2000 us')
        expected = {'percent': 50, 'delay_us': 0 if phase == 'a0' else 2000,
                    'align_us': 0, 'capacity': 4096}
        if any(contract[key] != value for key, value in expected.items()):
            raise ValueError(f'{run["path"]}: pilot async contract must use 50%, align=0, capacity=4096')
    for field, default in (('percent', 0), ('delay_us', 0), ('align_us', 0), ('capacity', 4096)):
        option = 'async-' + field.replace('_', '-')
        if int(options.get(option, default)) != expected[field]:
            raise ValueError(f'{run["path"]}: resolved async {field} disagrees with CLI options')
    if (summary['affinity'] != 'cluster' or summary['balance_at'] != 'all'
            or summary['continuation'] != 'parent' or summary['routing']['cross_numa']
            or summary['cpu_sampling'] or summary['page_query_enabled']
            or summary['sync_block_percent'] or summary['sync_block_us']):
        raise ValueError(f'{run["path"]}: pilot requires cluster/all/parent, same NUMA, '
                         'CPU/page sampling off and synchronous blocking off')
    routing = summary['routing']
    frozen_routing = {'home_policy': 'rr', 'batch_chunk': 1, 'max_hops': 4,
                      'busy_high': 1, 'return_low': .5, 'min_gain': 0}
    if any(routing.get(key) != value for key, value in frozen_routing.items()):
        raise ValueError(f'{run["path"]}: pilot routing must retain rr/chunk=1/hops=4 and '
                         'outstanding default thresholds 1/.5/0')
    for option, value in (('batch-chunk', 1), ('max-hops', 4), ('busy-high', 1),
                          ('return-low', .5), ('min-gain', 0), ('cross-numa', 0),
                          ('cpu-sampling', 0), ('page-sample', 0),
                          ('sync-block-percent', 0), ('sync-block-us', 0), ('pool-count', 0)):
        if float(options.get(option, value)) != value:
            raise ValueError(f'{run["path"]}: pilot CLI changes frozen {option}')
    if options.get('home-policy', 'rr') != 'rr':
        raise ValueError(f'{run["path"]}: pilot CLI requires request home-policy=rr')
    cpus = [cpu for group in summary['groups'] for cpu in group['cpus']]
    if (len(cpus) != len(set(cpus)) or any(group['numa'] < 0 for group in summary['groups'])
            or summary['pool_count'] != len(summary['groups'])):
        raise ValueError(f'{run["path"]}: pilot requires disjoint cluster partitions with one pool each')
    cell = phase, summary['policy'], int(summary['trace'])
    if cell not in CELLS:
        raise ValueError(f'{run["path"]}: not one of the eight pilot cells: {cell}')
    return cell


def request_contract(run):
    origin = int(run['summary']['origin_ns'])
    return [(row['request'], row['scheduled_ns'] - origin, row['status'], row['checksum'])
            for row in run['requests']]


def compare_mechanism_contract(left, right):
    """Explicitly permit async mechanism changes; never reuse a timing-pair key."""
    a, b = left['summary'], right['summary']
    allowed_fields = {'schema', 'work_signature', 'work_contract'}
    different = [key for key in cost.CONFIG_FIELDS if key not in allowed_fields and a[key] != b[key]]
    if different:
        raise ValueError(f'{left["path"]} vs {right["path"]}: mechanism configuration differs: {different}')
    business = lambda summary: {key: value for key, value in summary['work_contract'].items()
                                if key != 'async_contract'}
    if business(a) != business(b):
        raise ValueError(f'{left["path"]} vs {right["path"]}: mechanism contrast changed business/arrival work')
    allowed_options = {'output', 'async-percent', 'async-delay-us'}
    cli = lambda summary: {key: value for key, value in summary['options'].items()
                           if key not in allowed_options}
    if cli(a) != cli(b):
        raise ValueError(f'{left["path"]} vs {right["path"]}: mechanism CLI differs beyond async selection/delay')
    if request_contract(left) != request_contract(right):
        raise ValueError(f'{left["path"]} vs {right["path"]}: mechanism request schedule/status/checksum differs')
    if [row['request'] for row in left['cohort']] != [row['request'] for row in right['cohort']]:
        raise ValueError(f'{left["path"]} vs {right["path"]}: mechanism selected cohorts differ')


def plan_provenance(runs, cells):
    """Only a shared matching matrix can supply a declared order/hash record."""
    parents = {run['path'].resolve().parent for run in runs}
    unknown = {'order': None, 'reason': 'no common matching async-pilot plan', 'metadata': None}
    if len(parents) != 1:
        return unknown
    parent = next(iter(parents))
    try:
        metadata = json.loads((parent / 'metadata.json').read_text())
        plan = json.loads((parent / 'plan.json').read_text())
    except (OSError, ValueError):
        return unknown
    if (not isinstance(metadata, dict) or metadata.get('schema') != 'affinity-v2-matrix'
            or metadata.get('suite') != 'async-pilot' or not isinstance(plan, list)
            or len(plan) != len(CELLS)):
        return unknown
    by_path = {run['path'].resolve(): cell_name(cell) for cell, run in cells.items()}
    order = []
    for entry in plan:
        if not isinstance(entry, dict) or not isinstance(entry.get('directory'), str):
            return unknown
        declared = Path(os.path.normpath(entry['directory']))
        matches = [name for actual, name in by_path.items()
                   if (declared.resolve() == actual if declared.is_absolute()
                       else bool(declared.parts) and '..' not in declared.parts
                       and actual.parts[-len(declared.parts):] == declared.parts)]
        if len(matches) != 1 or matches[0] in order:
            return unknown
        order.append(matches[0])
    return {'order': order, 'reason': None, 'metadata': metadata}


def contrast(candidate, baseline):
    differences = {key: cost.delta(candidate['metrics'][key], baseline['metrics'][key])
                   for key in ('mean_ms', 'p95_ms', 'p99_ms')}
    differences['cpu_per_request_ms'] = cost.delta(cost.cpu_per_request(candidate), cost.cpu_per_request(baseline))
    return differences


def business_operator_role(roles):
    selected = [roles[name] for name in ('first-wave stage0', 'later-wave stage0', 'ordinary successors')]
    size = sum(row['n'] for row in selected)
    result = {'n': size}
    for key in ('cpu_ms', 'wall_ms', 'wait_ms'):
        result[key] = (sum(row['n'] * row[key] for row in selected if row['n']) / size
                       if size and all(row[key] is not None for row in selected if row['n']) else None)
    return result


def analyze(runs):
    """Validate the complete experiment before returning any comparison result."""
    if len(runs) != len(CELLS):
        raise ValueError('async pilot requires exactly eight runs; incomplete/extra batches are refused')
    if len({run['summary']['seed'] for run in runs}) != 1:
        raise ValueError('async pilot accepts exactly one seed per bounded batch')
    cells = {}
    for run in runs:
        cost.validate_inputs(run)
        cell = selected_cell(run)
        if cell in cells:
            raise ValueError(f'duplicate pilot cell {cell_name(cell)}')
        cells[cell] = run
    if set(cells) != set(CELLS):
        raise ValueError('missing required async pilot cells')
    for phase in PHASES:
        cost.compare_contract(cells[phase, 'home', 0], cells[phase, 'outstanding', 0], cross_strategy=True)
    traces = {}
    for phase in ('a0', 'a1'):
        # Calling separately avoids treating A0/A1 home runs as duplicate
        # (seed,strategy) trace pairs in the generic trace-cost analyzer.
        traces[phase] = cost.analyze([cells[phase, 'home', 0], cells[phase, 'home', 1]])['pairs'][0]
    for policy in POLICIES:
        compare_mechanism_contract(cells['d0', policy, 0], cells['a0', policy, 0])
        compare_mechanism_contract(cells['a0', policy, 0], cells['a1', policy, 0])
    selected = cells['a0', 'home', 0]['selected_resumes']
    if any(run['selected_resumes'] != selected for cell, run in cells.items() if cell[0] != 'd0'):
        raise ValueError('async selected logical operators differ across pilot cells')
    if not any(selected):
        raise ValueError('pilot selected no async operators; no mechanism comparison is identifiable')
    report.fixed_business_checks(runs)
    rows, stages = {}, {}
    for cell in CELLS:
        run = cells[cell]
        summary = run['summary']
        breakdown = flow.latency_breakdown(run['cohort'])
        source = summary.get('async_source')
        helper_cpu = (float(source['helper_cpu_s']) * 1000 / summary['completed']
                      if source and source['helper_cpu_valid'] else None)
        rows[cell] = {'run': run, 'breakdown': breakdown, 'cpu_ms': cost.cpu_per_request(run),
                      'helper_cpu_ms': helper_cpu, 'duration_s': float(summary['duration_with_drain_s'])}
        if cell[2]:
            ids = {row['request'] for row in run['completed_cohort']}
            tasks = [task for task in run['tasks'] if task['request'] in ids]
            stages[cell[0]] = {'timing': flow.async_timing(run, ids),
                               'roles': flow.task_roles(summary, tasks)}
            stages[cell[0]]['operators'] = business_operator_role(stages[cell[0]]['roles'])
    return {'seed': runs[0]['summary']['seed'], 'cells': cells, 'rows': rows,
            'traces': traces, 'stages': stages, 'provenance': plan_provenance(runs, cells),
            'strategy': {phase: contrast(cells[phase, 'outstanding', 0], cells[phase, 'home', 0])
                         for phase in PHASES},
            'mechanism': {(policy, after, before): contrast(cells[after, policy, 0], cells[before, policy, 0])
                          for policy in POLICIES for after, before in (('a0', 'd0'), ('a1', 'a0'))}}


def fmt(value):
    return 'unknown' if value is None else f'{value:.3f}'


def delta_text(values):
    return ' | '.join(f'{fmt(values[key]["absolute"])}/{fmt(values[key]["percent"])}%'
                      for key in ('mean_ms', 'p95_ms', 'p99_ms', 'cpu_per_request_ms'))


def print_result(result, warmup, window):
    cells = result['cells']
    anchor = cells['d0', 'home', 0]['summary']
    print(f'Async pilot AUDIT PASSED: eight complete cells, one seed={result["seed"]}; '
          f'planned-arrival cohort warmup={warmup}s window={window}; late completions retained.')
    print('Contract: D0 direct; A0 50% selected operators + external source + zero configured delay; '
          'A1 same selected operators + 2000 us delay; align=0, capacity=4096. '
          'Cluster/all/parent; same-NUMA placement; CPU/page sampling and synchronous blocking off.')
    print(f'Resources: pools={anchor["pool_count"]}, threads/pool={anchor["threads_per_pool"]}, '
          f'groups={json.dumps(anchor["groups"], separators=(",", ":"))}; '
          'partition labels do not certify physical cluster boundaries.')
    print('Business/arrival contract: ' + json.dumps(anchor['work_contract'], sort_keys=True, separators=(',', ':')))
    print('Routing contract: ' + json.dumps(anchor['routing'], sort_keys=True, separators=(',', ':')))
    fixed_hash = hashlib.sha256(anchor['fixed_work_signature'].encode()).hexdigest()[:16]
    print(f'WHOLE-run evidence: requests/cell={anchor["requests"]}, business nodes/request={anchor["nodes_per_request"]}, '
          f'arrival_hash={anchor["arrival_hash"]}, checksum={anchor["checksum"]}, '
          f'fixed_work_sha256_prefix={fixed_hash}; iterations/accesses='
          f'{anchor["planned_total_iterations"]}/{anchor["planned_total_accesses"]}.')
    selected = cells['a0', 'home', 0]
    cohort_ids = [row['request'] for row in selected['cohort']]
    cohort_resumes = sum(selected['summary']['planned_resume_counts'][request] for request in cohort_ids)
    print(f'Async planned resumes whole/cohort={selected["summary"]["planned_resumes"]}/{cohort_resumes}; '
          'selected logical IDs, complete per-request schedules/checksums and fixed business work match. '
          'A0/A1 each add one rpc_resume callback per selected operator.')
    provenance = result['provenance']
    if provenance['order']:
        print('Declared common plan order: ' + ' -> '.join(provenance['order']) +
              '; plan provenance, not independent execution attestation.')
        metadata = provenance['metadata']
        print(f'Plan binary SHA256={metadata.get("binary_sha256") or "unknown"}; '
              f'AsyncCompletionSource SHA256={metadata.get("sources_sha256", {}).get("include/executor_demo/AsyncCompletionSource.h") or "unknown"}.')
    else:
        print('Plan order/source hashes unavailable: ' + provenance['reason'] + '; directory/argument order is not inferred.')
    print('| cell | cohort completed/offered | mean/p95/p99 ms | lag mean/p99 ms | '
          'attempt-to-finish mean/p99 ms | whole CPU/request ms | whole helper CPU/request ms | whole peak inflight | whole wall+drain s |')
    for cell in CELLS:
        item = result['rows'][cell]
        run, breakdown = item['run'], item['breakdown']
        metric, summary = run['metrics'], run['summary']
        print(f'| {cell_name(cell)} | {metric["n"]}/{metric["offered"]} | '
              + '/'.join(fmt(metric[key]) for key in ('mean_ms', 'p95_ms', 'p99_ms')) + ' | '
              + '/'.join(fmt(breakdown['lag_ms'][key]) for key in ('mean', 'p99')) + ' | '
              + '/'.join(fmt(breakdown['after_attempt_ms'][key]) for key in ('mean', 'p99')) +
              f' | {fmt(item["cpu_ms"])} | {fmt(item["helper_cpu_ms"])} | {summary["peak_inflight"]} | {fmt(item["duration_s"])} |')
    print('Helper CPU is already included in whole process CPU; D0 has no helper (shown unknown, not an observed zero). '
          'Whole CPU/request includes warmup/drain, not cohort CPU. Trace=0 task/event timing is unavailable. '
          'Peak inflight describes retained request state, not kernel runnable count or sustained saturation.')
    print('Whole wall+drain starts at arrival origin. Execution CPU excludes pool/trace-buffer construction and export; '
          'it includes async source creation, dispatch and shutdown.')
    print('Source conservation (whole run; successful registered=fired=resume_succeeded; pending/canceled/rejected/failures all zero):')
    for cell in CELLS:
        if cell[0] == 'd0':
            continue
        source = cells[cell]['summary']['async_source']
        print(f'  {cell_name(cell)}: registered/fired/resumed={source["registered"]}/{source["fired"]}/'
              f'{source["resume_succeeded"]}; pending peak/capacity={source["peak_pending"]}/4096; '
              f'observed callbacks={cells[cell]["summary"]["observed_tasks"] if cell[2] else "unknown (trace=0)"}.')
    print('Descriptive differences: candidate minus baseline; percentages use baseline. '
          'Quantile differences are not quantiles of per-request differences or an additive latency decomposition.')
    print('| comparison | delta mean ms/% | delta p95 ms/% | delta p99 ms/% | whole CPU/request delta ms/% |')
    for phase in PHASES:
        print(f'| {phase} O-home trace0 (same async contract) | {delta_text(result["strategy"][phase])} |')
    for (policy, after, before), values in result['mechanism'].items():
        print(f'| {policy} {after}-{before} trace0 (declared mechanism contrast) | {delta_text(values)} |')
    for phase in ('a0', 'a1'):
        print(f'| {phase} home trace1-trace0 (instrumentation sensitivity) | '
              f'{delta_text(result["traces"][phase]["differences"])} |')
    print('Mechanism contrasts permit declared callback/wait changes under equal business work; '
          'they are not identical execution contracts or pure overhead. Home trace sensitivity does not bound '
          'O instrumentation feedback. One run/cell yields no confidence interval or production p99 acceptance.')
    n = cells['d0', 'home', 0]['metrics']['n']
    print(f'Tail screen: cohort n={n}, nominal top 1%={n*.01:.2f}; '
          + ('LOW n<1000; ' if n < 1000 else '') + 'request/DAG dependence remains.')
    print('Trace1 home async stages from completed planned-request cohorts; distributions are mean/p50/p95/p99/max ms:')
    for phase in ('a0', 'a1'):
        stage = result['stages'][phase]
        for label, values in stage['timing'].items():
            print(f'  {phase} {label}: n={values["n"]}; {flow.distribution_text(values)}')
        role = stage['roles']['rpc_resume']
        operators = stage['operators']
        print(f'  {phase} business operators: n={operators["n"]}; mean CPU/wall/wait ms='
              f'{fmt(operators["cpu_ms"])}/{fmt(operators["wall_ms"])}/{fmt(operators["wait_ms"])}; '
              'excludes init/join/rpc_resume and detached external wait.')
        print(f'  {phase} rpc_resume control: n={role["n"]}; mean CPU/wall/wait ms='
              f'{fmt(role["cpu_ms"])}/{fmt(role["wall_ms"])}/{fmt(role["wait_ms"])}; '
              f'migrations/return_home={role["migrations"]}/{role["return_home"]}.')
    print('Issue-to-due is configured waiting; due-to-fire is source scheduling lag; fire-to-submit includes routing; '
          'executor wait starts after routing. Original callback/resume may overlap at zero delay; '
          'source acknowledgement may occur after resume execution. Detached waits are not executor Q/I/O.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--warmup-seconds', type=float, default=0)
    parser.add_argument('--window-seconds', type=float)
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error('warmup must be finite and nonnegative')
    if args.window_seconds is not None and (not math.isfinite(args.window_seconds)
                                           or args.window_seconds <= 0
                                           or round(args.window_seconds * 1e9) < 1):
        parser.error('window must be finite and at least one rounded nanosecond')
    try:
        runs = [report.load_run(path, args.warmup_seconds, args.window_seconds) for path in args.runs]
        result = analyze(runs)
        print_result(result, args.warmup_seconds, args.window_seconds)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f'async pilot: comparison refused: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

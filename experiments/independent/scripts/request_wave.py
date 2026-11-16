#!/usr/bin/env python3
"""Freeze and run one request-wave workload: three policies, once each.

The /proc/stat observer is out of the submission process. Its samples are never
used for routing or for changing the offered load. All times are CLOCK_MONOTONIC.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

SCHEMA = 1
ARM_CPUS = '0-15;16-31;32-47;192-207;208-223;224-239'
POLICIES = {
    'home': {'routing': 'home', 'load-threshold': .8, 'accept-threshold': -1},
    'source_only': {'routing': 'threshold', 'load-threshold': .8, 'accept-threshold': -1},
    'guarded': {'routing': 'threshold', 'load-threshold': .8, 'accept-threshold': .5},
}
CPU_FIELDS = ['interval_start_monotonic_ns', 'monotonic_ns', 'elapsed_s', 'scope',
              'cpu_ids', 'execution_pct', 'iowait_pct', 'steal_pct', 'total_delta',
              'valid', 'reason']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def parse_cpus(value):
    result = set()
    for piece in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', piece):
            raise ValueError(f'invalid CPU list: {value}')
        bounds = [int(x) for x in piece.split('-')]
        first, last = bounds[0], bounds[-1]
        if last < first or last >= 1024:
            raise ValueError('CPU ranges must increase and remain below CPU_SETSIZE=1024')
        added = set(range(first, last + 1))
        if result & added:
            raise ValueError('duplicate CPU within group')
        result |= added
    if not result:
        raise ValueError('empty CPU group')
    return sorted(result)


def cpu_node(cpu):
    links = list(Path(f'/sys/devices/system/cpu/cpu{cpu}').glob('node[0-9]*'))
    if len(links) == 1:
        return int(links[0].name[4:])
    if not links and not Path('/sys/devices/system/node').exists():
        return 0
    raise ValueError(f'cannot discover NUMA node for CPU {cpu}')


def validate_resources(plan):
    groups = plan['pool_cpus']
    union = [cpu for group in groups for cpu in group]
    if len(set(union)) != len(union):
        raise ValueError('pool CPU groups must be disjoint')
    unavailable = set(union) - os.sched_getaffinity(0)
    if unavailable:
        raise ValueError(f'CPU groups exceed this process allowed CPUs: {sorted(unavailable)}; generate/run ARM plan on the ARM test host')
    unavailable_observer = set(trace_config(plan)['observer_cpu_ids']) - os.sched_getaffinity(0)
    if unavailable_observer:
        raise ValueError(f'observer CPUs exceed current allowed mask: {sorted(unavailable_observer)}; launch parent without a restrictive taskset')
    for group, expected_node in zip(groups, plan['numa']):
        observed = {cpu_node(cpu) for cpu in group}
        if observed != {expected_node}:
            raise ValueError(f'CPU group {group} has NUMA IDs {sorted(observed)}, expected {expected_node}')
    missing = set(union) - read_cpu_stat().keys()
    if missing:
        raise ValueError(f'/proc/stat missing selected CPUs: {sorted(missing)}')


def validate_plan(plan):
    if plan.get('schema_version') != SCHEMA or plan.get('family') != 'request-wave':
        raise ValueError('not a supported request-wave plan')
    contents = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    if digest(contents) != plan.get('plan_sha256'):
        raise ValueError('frozen plan changed; generate a new plan in a new output directory')
    if digest(plan['base']) != plan['workload_sha256']:
        raise ValueError('workload fingerprint mismatch')
    if plan['policies'] != POLICIES or sorted(plan['policy_order']) != sorted(POLICIES):
        raise ValueError('expected exactly home/source_only/guarded once each')
    trace = trace_config(plan)
    if not isinstance(trace['enabled'], bool):
        raise ValueError('cpu_trace.enabled must be boolean')
    if not isinstance(trace['bin_ms'], int) or not 1 <= trace['bin_ms'] <= 100:
        raise ValueError('cpu trace bin must be an integer in [1,100] ms')
    if not isinstance(trace['buffer_kb'], int) or not 64 <= trace['buffer_kb'] <= 4096:
        raise ValueError('cpu trace buffer must be in [64,4096] KiB per CPU')
    observer = trace['observer_cpu_ids']
    if (not isinstance(observer, list) or any(type(cpu) is not int or cpu < 0 for cpu in observer)
            or len(set(observer)) != len(observer) or set(observer) & set(plan['cpu_union'])):
        raise ValueError('observer CPUs must be unique and outside the experiment CPU union')
    if observer and not trace['enabled']:
        raise ValueError('dedicated observer CPUs require --cpu-trace')


def trace_config(plan):
    # Old frozen plans retain their identity and their existing coarse observer.
    return {'enabled': False, 'bin_ms': 5, 'buffer_kb': 256, 'observer_cpu_ids': [],
            **plan.get('cpu_trace', {})}


def new_trace_capture(plan, directory):
    # Lazy loading keeps non-traced runs and import-based runner tests independent
    # of tracefs and optional runtime privileges.
    path = Path(__file__).with_name('cpu_trace.py')
    spec = importlib.util.spec_from_file_location('request_wave_cpu_trace', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.IdleTraceCapture(plan['pool_cpus'], directory,
                                  buffer_kb=trace_config(plan)['buffer_kb'])


def create_plan(args):
    arm = args.deployment == 'arm'
    allowed = sorted(os.sched_getaffinity(0))
    if args.cpus:
        groups = [parse_cpus(v) for v in args.cpus.split(';')]
    elif arm:
        groups = [parse_cpus(v) for v in ARM_CPUS.split(';')]
    else:
        # Small host defaults: at most four logical CPUs, at most two CPU/pool,
        # while never constructing a cluster group across NUMA nodes.
        by_node = {}
        for cpu in allowed[:4]:
            by_node.setdefault(cpu_node(cpu), []).append(cpu)
        groups = [cpulist[i:i+2] for cpulist in by_node.values() for i in range(0, len(cpulist), 2)]
    if arm and len(groups) != 6:
        raise ValueError('ARM plan requires six disjoint pool CPU groups')
    if args.pools is not None and args.pools != len(groups):
        raise ValueError('--pools must equal the number of --cpus groups')
    cpus = ';'.join(','.join(map(str, g)) for g in groups)
    if args.numa:
        nodes = [int(v) for v in args.numa.split(',')]
    else:
        nodes = []
        for group in groups:
            found = {cpu_node(cpu) for cpu in group}
            if len(found) != 1:
                raise ValueError('each pool CPU group must be contained in one NUMA node')
            nodes.append(found.pop())
    capacities = [float(v) for v in args.capacities.split(',')] if args.capacities else [float(len(g)) for g in groups]
    if len(nodes) != len(groups) or len(capacities) != len(groups):
        raise ValueError('NUMA and capacity lists must have one entry per pool')
    if any(v < 0 for v in nodes) or any(not math.isfinite(v) or v <= 0 for v in capacities):
        raise ValueError('invalid NUMA ID or capacity')
    union = sorted(cpu for group in groups for cpu in group)
    observer_cpus = parse_cpus(args.observer_cpus) if args.observer_cpus else []
    if set(observer_cpus) - set(allowed):
        raise ValueError('observer CPUs exceed current allowed mask; launch without restricting the parent with taskset')
    duration = args.duration if args.duration is not None else (60 if arm else 3)
    warmup = args.warmup if args.warmup is not None else (10 if arm else .5)
    budget = args.cpu_us if args.cpu_us is not None else (25000 if arm else max(1, round(len(union) * .5 * 1e6 / (args.request_rate * (args.initial_tasks + args.steady_tasks)))))
    threads = args.threads if args.threads is not None else (128 if arm else 16)
    positive = (duration, args.request_rate, budget, threads, args.initial_tasks,
                args.steady_tasks, args.burst_window_ms, args.steady_window_ms)
    if any(not math.isfinite(v) or v <= 0 for v in positive) or not 0 <= warmup < duration:
        raise ValueError('invalid positive workload values or analysis window')
    if not 0 <= args.jitter < 1 or args.sample_ms < 10:
        raise ValueError('jitter must be in [0,1), sample-ms must be >=10')
    base = {
        'deployment': 'target' if arm else 'dev', 'workload': 'request-wave',
        'pools': len(groups), 'threads': threads, 'cpus': cpus,
        'numa': ','.join(map(str, nodes)), 'capacities': ','.join(f'{v:g}' for v in capacities),
        'affinity': 'cluster', 'arrival-rate': args.request_rate, 'arrival': 'regular',
        'duration': duration, 'burst-min': args.initial_tasks, 'burst-max': args.initial_tasks,
        'burst-window-ms': args.burst_window_ms, 'steady-tasks': args.steady_tasks,
        'steady-window-ms': args.steady_window_ms, 'burst-placement': 'local',
        'cpu-us': budget, 'jitter': args.jitter, 'long-prob': 0, 'long-factor': 8,
        'mean-normalization': 'exact', 'work-mode': 'cpu', 'seed': args.seed,
        'instrumentation': 'full', 'load-observation': 'basic', 'load-metric': 'outstanding',
        'estimate-us': budget, 'estimate-jitter': 0, 'min-improvement': 0,
        'sample-ms': args.sample_ms, 'max-outstanding': 100000,
        'max-planned-tasks': 2000000, 'drain-seconds': 120 if arm else 20,
    }
    order = list(POLICIES)
    random.Random(args.seed ^ 0xA5A5).shuffle(order)
    task_rate = args.request_rate * (args.initial_tasks + args.steady_tasks)
    plan = {
        'schema_version': SCHEMA, 'family': 'request-wave', 'deployment': args.deployment,
        'base': base, 'analysis': {'warmup_seconds': warmup, 'request_end_seconds': duration,
                                 'cohort': 'request_planned_ns; include all later tasks belonging to selected requests'},
        'pool_cpus': groups, 'numa': nodes, 'capacities': capacities, 'cpu_union': union,
        'policies': POLICIES, 'policy_order': order, 'workload_sha256': digest(base),
        'cpu_observer': {'interval_ms': args.sample_ms, 'source': '/proc/stat',
                         'execution_fields': ['user', 'nice', 'system', 'irq', 'softirq'],
                         'guest_accounting': 'guest and guest_nice already included in user/nice; not added twice',
                         'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
                         'scope': 'selected logical CPUs, includes unrelated work and runner overhead',
                         'resolution_limit': '100 ms samples cannot resolve a <30 ms peak; counters are tick-accounted'},
        'cpu_trace': {'enabled': args.cpu_trace, 'bin_ms': args.cpu_bin_ms,
                      'buffer_kb': args.cpu_trace_buffer_kb, 'observer_cpu_ids': observer_cpus},
        'nominal': {'tasks_per_second': task_rate, 'cpu_seconds_per_second': task_rate * budget / 1e6,
                    'selected_logical_cpus': len(union),
                    'execution_percent': 100 * task_rate * budget / 1e6 / len(union),
                    'per_pool_request_gap_ms': 1000 * len(groups) / args.request_rate},
        'notes': ['Synthetic independent CPU tasks; not DAG and not a reproduction of 100 ms operator execution.',
                  '25 ms default on 96 logical CPUs gives ~50% nominal CPU-time demand; measure actual utilization.',
                  'SMT logical CPU-time is not equivalent to full physical-core throughput.',
                  'No CPU utilization input to routing, no feedback pacing and no per-policy workload adjustment.',
                  'One seed; each policy once; observed differences have no repeated-run confidence interval.',
                  'Request duration bounds request starts, not later steady task releases or drain completion.'],
    }
    plan['plan_sha256'] = digest(plan)
    validate_plan(plan)
    validate_resources(plan)
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / 'plan.json'
    with destination.open('x') as f:
        f.write(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    print(f'Frozen plan: {destination.resolve()}')
    print(f"{len(groups)} pools x {threads} threads; selected logical CPUs={len(union)}; NUMA={nodes}")
    print(f"{args.request_rate:g} requests/s; initial {args.initial_tasks} tasks/{args.burst_window_ms:g}ms + steady {args.steady_tasks} tasks/{args.steady_window_ms:g}ms")
    print(f"Synthetic mean CPU budget {budget}us; nominal CPU demand={plan['nominal']['cpu_seconds_per_second']:.3f} CPU-s/s, {plan['nominal']['execution_percent']:.2f}% of selected logical CPUs")
    print(f'Run order: {", ".join(order)}; one run each; duration={duration:g}s, warmup={warmup:g}s')
    if args.cpu_trace:
        print(f'CPU trace: idle-transition events, {args.cpu_bin_ms}ms reconstructed bins; observer CPUs={observer_cpus or "inherited"}; requires writable tracefs instances')
    print('Not a 100ms real-business reproduction. Calibrate once; keep the final workload identical for all policies.')
    return plan


def read_cpu_stat(path=Path('/proc/stat')):
    result = {}
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if fields and re.fullmatch(r'cpu\d+', fields[0]):
            values = [int(v) for v in fields[1:]]
            if len(values) < 8:
                raise ValueError('/proc/stat CPU line has fewer than eight counters')
            result[int(fields[0][3:])] = values[:8]  # guest counters overlap user/nice.
    return result


def cpu_rows(previous, current, groups, start_ns, end_ns, observer_start_ns):
    scopes = [('union', sorted({cpu for group in groups for cpu in group}))]
    scopes += [(f'pool{i}', group) for i, group in enumerate(groups)]
    rows = []
    for scope, group in scopes:
        reason = ''
        if end_ns <= start_ns:
            reason = 'nonpositive_sample_interval'
        if any(cpu not in previous or cpu not in current for cpu in group):
            reason = 'missing_selected_cpu'
        delta = [0] * 8
        if not reason:
            for cpu in group:
                values = [b - a for a, b in zip(previous[cpu], current[cpu])]
                if any(v < 0 for v in values):
                    reason = 'counter_regression'
                delta = [a + b for a, b in zip(delta, values)]
        total = sum(delta)
        if not reason and total <= 0:
            reason = 'zero_total_delta'
        valid = not reason
        rows.append({'interval_start_monotonic_ns': start_ns, 'monotonic_ns': end_ns,
                     'elapsed_s': (end_ns - observer_start_ns) / 1e9, 'scope': scope,
                     'cpu_ids': ','.join(map(str, group)),
                     'execution_pct': 100 * sum(delta[i] for i in (0, 1, 2, 5, 6)) / total if valid else '',
                     'iowait_pct': 100 * delta[4] / total if valid else '',
                     'steal_pct': 100 * delta[7] / total if valid else '',
                     'total_delta': total, 'valid': int(valid), 'reason': reason})
    return rows


def completed_run(path, expected):
    manifest_path = path / 'manifest.json'
    if not path.exists():
        return None
    if not manifest_path.exists():
        raise ValueError(f'{path} already exists without manifest; preserve it and use a new plan/output directory')
    manifest = json.loads(manifest_path.read_text())
    keys = ('plan_sha256', 'binary_sha256', 'policy', 'options')
    if any(manifest.get(k) != expected[k] for k in keys):
        raise ValueError(f'{path}: existing run identity differs; no overwrite')
    if manifest.get('status') != 'completed':
        raise ValueError(f'{path}: prior run is {manifest.get("status")}; preserve failure and use a new output directory')
    required = ['data/tasks.csv', 'data/summary.json', 'data/metadata.json', 'cpu_samples.csv', 'stdout.log']
    if expected.get('cpu_trace_enabled'):
        required += ['cpu_trace.txt', 'cpu_trace_status.json']
        if manifest.get('cpu_trace', {}).get('status') != 'ok':
            raise ValueError(f'{path}: completed run has invalid high-resolution CPU capture')
    if any(not (path / filename).is_file() for filename in required):
        raise ValueError(f'{path}: completed run has missing artifacts; no silent rerun')
    hashes = manifest.get('artifact_sha256', {})
    if any(hashes.get(name) != file_digest(path / name) for name in required):
        raise ValueError(f'{path}: saved artifacts changed; preserve run and investigate')
    if manifest.get('cpu_observation', {}).get('status') != 'ok':
        raise ValueError(f'{path}: completed run has invalid CPU observation')
    print(f'SKIP {manifest["policy"]}: complete matching run already exists', flush=True)
    return manifest


def run_timeout_seconds(options):
    # A final request starts before duration, then releases its initial wave and
    # subsequent steady tasks before the child begins its full drain allowance.
    release_tail = (options['burst-window-ms'] + options['steady-window-ms']) / 1000
    return options['duration'] + release_tail + options['drain-seconds'] + 70


def execute_one(plan, binary, binary_sha, directory, policy, calibration=False):
    options = dict(plan['base'], **plan['policies'][policy])
    warmup = plan['analysis']['warmup_seconds']
    if calibration:
        options['duration'] = min(20, options['duration'])
        warmup = min(5, options['duration'] / 4)
    expected = {'plan_sha256': plan['plan_sha256'], 'binary_sha256': binary_sha,
                'policy': policy, 'options': options,
                'cpu_trace_enabled': trace_config(plan)['enabled']}
    old = completed_run(directory, expected)
    if old:
        return old
    directory.mkdir(parents=True)
    raw_dir = directory / 'data'
    command = [str(binary), '--output', str(raw_dir)]
    for key, value in options.items():
        command += ['--' + key, str(value)]
    start = time.monotonic_ns()
    manifest = dict(expected, schema_version=SCHEMA, status='running', calibration=calibration,
                    path=str(directory), raw_dir=str(raw_dir), command=command,
                    start_monotonic_ns=start, warmup_seconds=warmup, returncode=None,
                    launcher_cpu_union=plan['cpu_union'],
                    observer_actual_cpu_ids=sorted(os.sched_getaffinity(0)),
                    cpu_observation={'status': 'pending', 'interval_ms': options['sample-ms']})
    atomic_json(directory / 'manifest.json', manifest)
    observation = {'status': 'pending', 'interval_ms': options['sample-ms'], 'sample_rows': 0,
                   'valid_rows': 0, 'invalid_rows': 0, 'valid_union_intervals': 0,
                   'error': None, 'scope': 'selected logical CPUs; unrelated work included',
                   'resolution_limit': '100ms defaults cannot resolve <30ms peak'}
    process = None
    capture = None
    errors = []
    returncode = None
    try:
        if trace_config(plan)['enabled']:
            capture = new_trace_capture(plan, directory)
            capture.start()  # No reader thread until the child has passed preexec.
        previous = read_cpu_stat()
        previous_ns = time.monotonic_ns()
        with (directory / 'cpu_samples.csv').open('w', newline='') as cpu_file, (directory / 'stdout.log').open('w') as logfile:
            writer = csv.DictWriter(cpu_file, fieldnames=CPU_FIELDS)
            writer.writeheader()
            # Restrict the child before exec so target mode sees exactly the
            # experiment CPU union. Observer itself remains outside this mask rule.
            process = subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT,
                                       preexec_fn=lambda: os.sched_setaffinity(0, plan['cpu_union']))
            if capture is not None:
                capture.start_reader()
            interval = options['sample-ms'] / 1000
            deadline = time.monotonic() + run_timeout_seconds(options)
            while True:
                try:
                    process.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
                now_ns = time.monotonic_ns()
                if process.poll() is not None and now_ns - previous_ns < min(10000000, int(interval * 1e9)):
                    observation['unsampled_final_interval_ns'] = now_ns - previous_ns
                    returncode = process.returncode
                    break
                current = read_cpu_stat()
                rows = cpu_rows(previous, current, plan['pool_cpus'], previous_ns, now_ns, start)
                writer.writerows(rows)
                cpu_file.flush()
                observation['sample_rows'] += len(rows)
                observation['valid_rows'] += sum(row['valid'] for row in rows)
                observation['invalid_rows'] += sum(not row['valid'] for row in rows)
                observation['valid_union_intervals'] += int(rows[0]['valid'])
                previous, previous_ns = current, now_ns
                if process.poll() is not None:
                    returncode = process.returncode
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('child exceeded workload + drain + startup allowance')
        if observation['valid_union_intervals'] < 2:
            errors.append('CPU observer collected fewer than two valid union intervals')
        # Counter regressions/missing CPUs make observations incomplete, rather than
        # silently letting an invalid curve look like a successful measurement.
        if observation['invalid_rows']:
            errors.append(f"CPU observer has {observation['invalid_rows']} invalid scope rows")
        observation['status'] = 'ok' if not errors else 'failed'
        if returncode != 0:
            errors.append(f'workload exit code {returncode}; see stdout.log')
        summary_file = raw_dir / 'summary.json'
        if summary_file.exists():
            summary = json.loads(summary_file.read_text())
            manifest['summary'] = summary
            if (not summary.get('conservation_ok') or summary.get('rejected_tasks', 0) or
                    summary.get('canceled_tasks', 0) or summary.get('failed_tasks', 0) or summary.get('sampling_failed') or
                    summary.get('completed_tasks') != summary.get('planned_tasks')):
                errors.append('workload incomplete, rejected, canceled, failed, or inconsistent')
        else:
            errors.append('missing summary.json')
        if any(not (raw_dir / name).is_file() for name in ('tasks.csv', 'metadata.json')):
            errors.append('missing raw task or metadata artifact')
    except (Exception, KeyboardInterrupt) as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
        observation['status'] = 'failed'
        observation['error'] = str(exc)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process is not None:
            returncode = process.returncode
    finally:
        if capture is not None:
            try:
                manifest['cpu_trace'] = capture.stop()
                if manifest['cpu_trace'].get('status') != 'ok':
                    errors.append('high-resolution CPU capture failed; see cpu_trace_status.json')
            except Exception as exc:
                manifest['cpu_trace'] = {'status': 'failed', 'error': str(exc)}
                errors.append(f'high-resolution CPU cleanup failed: {exc}')
    manifest.update(status='failed' if errors else 'completed', end_monotonic_ns=time.monotonic_ns(),
                    returncode=returncode, cpu_observation=observation, errors=errors)
    if not errors:
        artifacts = ['data/tasks.csv', 'data/summary.json', 'data/metadata.json', 'cpu_samples.csv', 'stdout.log']
        if trace_config(plan)['enabled']:
            artifacts += ['cpu_trace.txt', 'cpu_trace_status.json']
        manifest['artifact_sha256'] = {name: file_digest(directory / name) for name in artifacts}
    atomic_json(directory / 'manifest.json', manifest)
    return manifest


def execute(args):
    plan_path = args.plan.resolve()
    plan = json.loads(plan_path.read_text())
    validate_plan(plan)
    validate_resources(plan)
    binary = args.binary.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f'binary is not executable: {binary}')
    binary_sha = file_digest(binary)
    observer = trace_config(plan)['observer_cpu_ids']
    if observer:
        # The child's preexec above explicitly restores the experiment union.
        # The observer and its reader thread stay on the dedicated CPUs.
        os.sched_setaffinity(0, observer)
        if os.sched_getaffinity(0) != set(observer):
            raise ValueError('observer CPU affinity was not applied exactly; no workload launched')
    calibration = args.command == 'calibrate'
    order = ['home'] if calibration else plan['policy_order']
    root = plan_path.parent
    manifest_path = root / ('calibration_manifest.json' if calibration else 'run_manifest.json')
    manifest = {'schema_version': SCHEMA, 'family': 'request-wave', 'plan_sha256': plan['plan_sha256'],
                'binary_sha256': binary_sha, 'binary': str(binary), 'policy_order': order,
                'calibration': calibration, 'runs': []}
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if any(previous.get(k) != manifest[k] for k in ('plan_sha256', 'binary_sha256', 'policy_order', 'calibration')):
            raise ValueError('existing top-level manifest identity differs; no overwrite')
    for policy in order:
        directory = root / ('calibration' if calibration else 'runs') / policy
        run = execute_one(plan, binary, binary_sha, directory, policy, calibration)
        manifest['runs'].append(run)
        atomic_json(manifest_path, manifest)
        summary = run.get('summary', {})
        print(f"{policy}: {run['status']}; all-run task mean={summary.get('task_mean_ms', 'NA')}ms p99={summary.get('task_p99_ms', 'NA')}ms; CPU observer={run['cpu_observation']['status']}", flush=True)
        if run['status'] != 'completed':
            raise RuntimeError(f"{policy} failed; {directory / 'manifest.json'}; {'; '.join(run['errors'])}")
    print(f'Saved manifest: {manifest_path}')
    print('Above latency includes warmup. Use analyze_request_wave.py for request-cohort latency and selected-CPU observation.')
    if calibration:
        print('Calibration does not tune workload or launch the comparison. If changing assumptions, generate a new plan; never adjust one policy alone.')
    analyzer = Path(__file__).with_name('analyze_request_wave.py')
    analysis_command = [sys.executable, str(analyzer), '--plan', str(plan_path), '--manifest', str(manifest_path),
                        '--output', str(root / ('calibration-analysis' if calibration else 'analysis'))]
    print('Analyzing the saved samples (no additional workload run)...', flush=True)
    result = subprocess.run(analysis_command, check=False)
    if result.returncode:
        raise RuntimeError(f'analysis exited {result.returncode}; raw runs are preserved for inspection')
    if trace_config(plan)['enabled']:
        curve_analyzer = Path(__file__).with_name('analyze_cpu_trace.py')
        curve_command = [sys.executable, str(curve_analyzer), '--plan', str(plan_path),
                         '--manifest', str(manifest_path), '--output',
                         str(root / ('calibration-cpu-curves' if calibration else 'cpu-curves'))]
        print('Reconstructing CPU time curves from idle-transition events (no additional workload run)...', flush=True)
        curve_result = subprocess.run(curve_command, check=False)
        if curve_result.returncode:
            raise RuntimeError(f'CPU curve analysis exited {curve_result.returncode}; raw capture is preserved, do not treat missing curves as zero utilization')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    create = commands.add_parser('plan', help='freeze workload and randomize exactly three policies once')
    create.add_argument('--deployment', choices=('arm', 'dev'), default='arm')
    create.add_argument('--out', type=Path, required=True)
    create.add_argument('--cpus'); create.add_argument('--numa'); create.add_argument('--capacities')
    create.add_argument('--pools', type=int); create.add_argument('--threads', type=int)
    create.add_argument('--cpu-us', type=int); create.add_argument('--duration', type=float)
    create.add_argument('--warmup', type=float); create.add_argument('--request-rate', type=float, default=12)
    create.add_argument('--initial-tasks', type=int, default=80)
    create.add_argument('--steady-tasks', type=int, default=80)
    create.add_argument('--burst-window-ms', type=float, default=20)
    create.add_argument('--steady-window-ms', type=float, default=400)
    create.add_argument('--jitter', type=float, default=.5)
    create.add_argument('--sample-ms', type=int, default=100)
    create.add_argument('--cpu-trace', action='store_true', help='capture idle transitions and automatically reconstruct precise CPU occupancy curves')
    create.add_argument('--cpu-bin-ms', type=int, default=5, help='CPU trace reconstruction window, independent of --sample-ms (default 5)')
    create.add_argument('--cpu-trace-buffer-kb', type=int, default=256, help='trace ring buffer KiB per CPU (default 256)')
    create.add_argument('--observer-cpus', help='optional idle CPUs outside experiment union for the Python observer; launch parent without taskset')
    create.add_argument('--seed', type=int, default=52000)
    for name in ('calibrate', 'run'):
        execute_parser = commands.add_parser(name)
        execute_parser.add_argument('--plan', type=Path, required=True)
        execute_parser.add_argument('--binary', type=Path, default=Path('build/experiments/independent/independent_load'))
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == 'plan':
            create_plan(args)
        else:
            execute(args)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'request_wave: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

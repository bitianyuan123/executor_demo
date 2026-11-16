#!/usr/bin/env python3
"""Freeze and run one request-wave workload, once per selected policy.

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
SHORT_POLICIES = {name: POLICIES[name] for name in ('home', 'source_only')}
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
    profile = plan.get('profile', 'legacy')
    if profile not in ('legacy', 'short-peak'):
        raise ValueError('unsupported request-wave profile')
    expected_policies = SHORT_POLICIES if profile == 'short-peak' else POLICIES
    if plan['policies'] != expected_policies or sorted(plan['policy_order']) != sorted(expected_policies):
        raise ValueError('expected exactly one run of each profile policy')
    if profile == 'short-peak' and plan['policy_order'] != ['home', 'source_only']:
        raise ValueError('short-peak must validate home before launching source_only')
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
    if profile == 'short-peak' and trace['enabled'] and trace['bin_ms'] > 2:
        raise ValueError('short-peak requires CPU trace bins <=2ms (default 1ms)')


def short_peak_budgets(args, groups, capacities, contract):
    """Analytic starting guess, not feedback control or a measured peak promise."""
    if args.cpu_us is not None:
        raise ValueError('short-peak uses phase budgets; --cpu-us is incompatible')
    if len({len(group) for group in groups}) != 1 or any(capacity != len(group) for capacity, group in zip(capacities, groups)):
        raise ValueError('short-peak model requires equal CPU groups and capacities equal to their logical CPU counts')
    rho = args.target_utilization
    peak_ms = args.peak_target_ms
    utilization_low, utilization_high = [value / 100 for value in contract['union_mean_range_pct']]
    if not math.isfinite(rho) or not utilization_low <= rho <= utilization_high:
        raise ValueError('short-peak target utilization must be in [0.45,0.55] to match the frozen 45-55% waveform contract')
    peak_low, peak_high = contract['median_peak_width_range_ms']
    if not math.isfinite(peak_ms) or not peak_low <= peak_ms <= peak_high:
        raise ValueError('short-peak peak target must be in [5,20] ms to match the frozen waveform contract; it remains a model guess')
    period_s = len(groups) / args.request_rate
    tau_s = peak_ms / 1000
    if period_s * 1000 <= contract['onset_region_end_ms']:
        raise ValueError('short-peak same-home request period must exceed 30ms so the recovery/background window exists')
    if tau_s >= period_s:
        raise ValueError('peak target must be shorter than the same-home request period')
    burst_ms = args.burst_window_ms if args.burst_window_ms is not None else 1
    steady_ms = args.steady_window_ms if args.steady_window_ms is not None else period_s * 1000
    if not math.isfinite(burst_ms) or not 0 < burst_ms < peak_ms:
        raise ValueError('short-peak release window must be positive and shorter than the peak target')
    if not math.isclose(steady_ms, period_s * 1000, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError('short-peak steady-window-ms must equal the same-home request period; model assumes continuous steady demand')
    capacity = len(groups[0])
    total_us = sum(map(len, groups)) * rho * 1e6 / args.request_rate
    initial_work_us = capacity * (1 - rho) * tau_s / (1 - tau_s / period_s) * 1e6
    initial_us = args.initial_cpu_us if args.initial_cpu_us is not None else round(initial_work_us / args.initial_tasks)
    steady_us = args.steady_cpu_us if args.steady_cpu_us is not None else round((total_us - args.initial_tasks * initial_us) / args.steady_tasks)
    if initial_us <= 0 or steady_us <= 0:
        raise ValueError('phase CPU budgets must be positive; initial work must leave a positive steady budget')
    actual_initial_us = args.initial_tasks * initial_us
    actual_steady_us = args.steady_tasks * steady_us
    steady_cpu = actual_steady_us / 1e6 / period_s
    if steady_cpu >= capacity:
        raise ValueError('steady phase alone saturates the home pool; cannot model a recovering short peak')
    actual_rho = (actual_initial_us + actual_steady_us) / 1e6 / period_s / capacity
    if actual_rho >= 1:
        raise ValueError('phase budgets overload the resource domain on average')
    if not utilization_low - 1e-12 <= actual_rho <= utilization_high + 1e-12:
        raise ValueError(f'rounded phase budgets imply {actual_rho * 100:.6g}% nominal CPU demand outside the frozen 45-55% contract; use a compatible target or phase budgets')
    model = {
        'requested_target_utilization': rho, 'requested_peak_target_ms': peak_ms,
        'same_home_period_ms': period_s * 1000, 'logical_cpus_per_pool': capacity,
        'initial_task_cpu_us': initial_us, 'steady_task_cpu_us': steady_us,
        'initial_cpu_us_per_request': actual_initial_us,
        'steady_cpu_us_per_request': actual_steady_us,
        'cpu_us_per_request': actual_initial_us + actual_steady_us,
        'actual_nominal_utilization': actual_rho,
        'steady_nominal_pool_utilization': steady_cpu / capacity,
        'estimated_fluid_peak_ms': actual_initial_us / 1000 / (capacity - steady_cpu),
        'phase_budget_overrides': {'initial': args.initial_cpu_us is not None, 'steady': args.steady_cpu_us is not None},
        'formula': 'B=C*(1-rho)*tau/(1-tau/P); estimated peak=B/(C-steady_work/P)',
        'assumptions': ['equal disjoint logical CPU groups; C is CPU-time capacity, not physical-core throughput',
                        'initial CPU work is approximated as an instantaneous batch',
                        'steady work is approximated as continuous fluid demand with period P',
                        'ignores per-task scheduling, discrete long steady tasks, instrumentation and unrelated work',
                        'model is an initial guess; measured home waveform must pass before ARM policy comparison'],
    }
    return initial_us, steady_us, burst_ms, steady_ms, model


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


def waveform_acceptance_contract():
    # Keep one definition of the provisional acceptance thresholds, while
    # freezing their full values into each new short-peak plan for audit/resume.
    path = Path(__file__).with_name('analyze_cpu_trace.py')
    spec = importlib.util.spec_from_file_location('request_wave_cpu_analysis', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.WAVEFORM_CONTRACT)


def create_plan(args):
    arm = args.deployment == 'arm'
    short_peak = args.profile == 'short-peak'
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
    threads = args.threads if args.threads is not None else (128 if arm else 16)
    positive = (duration, args.request_rate, threads, args.initial_tasks, args.steady_tasks)
    if any(not math.isfinite(v) or v <= 0 for v in positive) or not 0 <= warmup < duration:
        raise ValueError('invalid positive workload values or analysis window')
    model = None
    if short_peak:
        shape_contract = waveform_acceptance_contract()
        if threads < max(map(len, groups)):
            raise ValueError('short-peak requires at least one worker per selected logical CPU in each pool; fewer workers invalidate the full-capacity peak model')
        initial_us, steady_us, burst_ms, steady_ms, model = short_peak_budgets(args, groups, capacities, shape_contract)
        # Common fallback is not used by either phase; explicit positive value
        # keeps the executable's generic argument validation straightforward.
        budget = initial_us
        actual_work_us = model['cpu_us_per_request']
    else:
        if args.initial_cpu_us is not None or args.steady_cpu_us is not None:
            raise ValueError('runner phase budgets require --profile short-peak')
        budget = args.cpu_us if args.cpu_us is not None else (25000 if arm else max(1, round(len(union) * .5 * 1e6 / (args.request_rate * (args.initial_tasks + args.steady_tasks)))))
        burst_ms = args.burst_window_ms if args.burst_window_ms is not None else 20
        steady_ms = args.steady_window_ms if args.steady_window_ms is not None else 400
        actual_work_us = (args.initial_tasks + args.steady_tasks) * budget
    if any(not math.isfinite(value) or value <= 0 for value in (budget, burst_ms, steady_ms)):
        raise ValueError('CPU budget and release windows must be positive')
    if not 0 <= args.jitter < 1 or args.sample_ms < 10:
        raise ValueError('jitter must be in [0,1), sample-ms must be >=10')
    bin_ms = args.cpu_bin_ms if args.cpu_bin_ms is not None else (1 if short_peak else 5)
    base = {
        'deployment': 'target' if arm else 'dev', 'workload': 'request-wave',
        'pools': len(groups), 'threads': threads, 'cpus': cpus,
        'numa': ','.join(map(str, nodes)), 'capacities': ','.join(f'{v:g}' for v in capacities),
        'affinity': 'cluster', 'arrival-rate': args.request_rate, 'arrival': 'regular',
        'duration': duration, 'burst-min': args.initial_tasks, 'burst-max': args.initial_tasks,
        'burst-window-ms': burst_ms, 'steady-tasks': args.steady_tasks,
        'steady-window-ms': steady_ms, 'burst-placement': 'local',
        'cpu-us': budget, 'jitter': args.jitter, 'long-prob': 0, 'long-factor': 8,
        'mean-normalization': 'exact', 'work-mode': 'cpu', 'seed': args.seed,
        'instrumentation': 'full', 'load-observation': 'basic', 'load-metric': 'outstanding',
        'estimate-us': budget, 'estimate-jitter': 0, 'min-improvement': 0,
        'sample-ms': args.sample_ms, 'max-outstanding': 100000,
        'max-planned-tasks': 2000000, 'drain-seconds': 120 if arm else 20,
    }
    if short_peak:
        base.update({'initial-cpu-us': initial_us, 'steady-cpu-us': steady_us, 'estimate-us': 0})
    policies = SHORT_POLICIES if short_peak else POLICIES
    order = list(policies)
    if not short_peak:
        random.Random(args.seed ^ 0xA5A5).shuffle(order)
    task_rate = args.request_rate * (args.initial_tasks + args.steady_tasks)
    plan = {
        'schema_version': SCHEMA, 'family': 'request-wave', 'deployment': args.deployment,
        'base': base, 'analysis': {'warmup_seconds': warmup, 'request_end_seconds': duration,
                                 'cohort': 'request_planned_ns; include all later tasks belonging to selected requests'},
        'pool_cpus': groups, 'numa': nodes, 'capacities': capacities, 'cpu_union': union,
        'policies': policies, 'policy_order': order, 'workload_sha256': digest(base),
        'cpu_observer': {'interval_ms': args.sample_ms, 'source': '/proc/stat',
                         'execution_fields': ['user', 'nice', 'system', 'irq', 'softirq'],
                         'guest_accounting': 'guest and guest_nice already included in user/nice; not added twice',
                         'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
                         'scope': 'selected logical CPUs, includes unrelated work and runner overhead',
                         'resolution_limit': '100 ms samples cannot resolve a <30 ms peak; counters are tick-accounted'},
        'cpu_trace': {'enabled': args.cpu_trace, 'bin_ms': bin_ms,
                      'buffer_kb': args.cpu_trace_buffer_kb, 'observer_cpu_ids': observer_cpus},
        'nominal': {'tasks_per_second': task_rate, 'cpu_seconds_per_second': args.request_rate * actual_work_us / 1e6,
                    'selected_logical_cpus': len(union),
                    'execution_percent': 100 * args.request_rate * actual_work_us / 1e6 / len(union),
                    'per_pool_request_gap_ms': 1000 * len(groups) / args.request_rate},
        'notes': ['Synthetic independent CPU tasks; not DAG and not a reproduction of 100 ms operator execution.',
                  '25 ms default on 96 logical CPUs gives ~50% nominal CPU-time demand; measure actual utilization.',
                  'SMT logical CPU-time is not equivalent to full physical-core throughput.',
                  'No CPU utilization input to routing, no feedback pacing and no per-policy workload adjustment.',
                  'One seed; each policy once; observed differences have no repeated-run confidence interval.',
                  'Request duration bounds request starts, not later steady task releases or drain completion.'],
    }
    if short_peak:
        plan['profile'] = 'short-peak'
        plan['phase_workload'] = model
        plan['waveform_acceptance'] = {
            **shape_contract,
            'scope': 'home only; source_only waveform is an outcome, not an acceptance condition',
            'requires_high_resolution_trace_on_arm': True,
            'status_without_trace': 'unvalidated; dev may exercise functional policy paths only',
        }
        plan['notes'][1] = 'Short initial CPU segments and longer steady CPU segments preserve nominal mean demand; each phase is normalized independently.'
        plan['notes'] += ['home runs first and is reused on resume; ARM source_only is gated by the measured home shape.',
                          'No automatic budget adjustment; a failed home waveform requires diagnosis and a newly frozen workload for both policies.',
                          'The business 5-20ms peak is an approximate observation with no established occupancy cutoff; >=90% is a provisional experiment contract, not a measured production threshold.']
    plan['plan_sha256'] = digest(plan)
    validate_plan(plan)
    validate_resources(plan)
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / 'plan.json'
    with destination.open('x') as f:
        f.write(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    print(f'Frozen plan: {destination.resolve()}')
    print(f"{len(groups)} pools x {threads} threads; selected logical CPUs={len(union)}; NUMA={nodes}")
    print(f"{args.request_rate:g} requests/s; initial {args.initial_tasks} tasks/{burst_ms:g}ms + steady {args.steady_tasks} tasks/{steady_ms:g}ms")
    budget_label = f'initial={initial_us}us, steady={steady_us}us (separately normalized)' if short_peak else f'mean={budget}us'
    print(f"Synthetic CPU budgets {budget_label}; nominal CPU demand={plan['nominal']['cpu_seconds_per_second']:.3f} CPU-s/s, {plan['nominal']['execution_percent']:.2f}% of selected logical CPUs")
    if short_peak:
        print(f"Fluid initial guess={model['estimated_fluid_peak_ms']:.3f}ms high occupancy; actual home shape must be measured, not inferred from this estimate")
    print(f'Run order: {", ".join(order)}; one run each; duration={duration:g}s, warmup={warmup:g}s')
    if args.cpu_trace:
        print(f'CPU trace: idle-transition events, {bin_ms}ms reconstructed bins; observer CPUs={observer_cpus or "inherited"}; requires writable tracefs instances')
    if short_peak:
        print('Not a 100ms real-business reproduction. The ordinary home run is the shape check; no additional calibration run is required.')
    else:
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


def analyze_saved(plan, plan_path, manifest_path, root, calibration=False):
    """Analyze persisted records; this never launches additional workload."""
    analyzer = Path(__file__).with_name('analyze_request_wave.py')
    analysis_command = [sys.executable, str(analyzer), '--plan', str(plan_path), '--manifest', str(manifest_path),
                        '--output', str(root / ('calibration-analysis' if calibration else 'analysis'))]
    print('Analyzing the saved samples (no additional workload run)...', flush=True)
    result = subprocess.run(analysis_command, check=False)
    if result.returncode:
        raise RuntimeError(f'analysis exited {result.returncode}; raw runs are preserved for inspection')
    if not trace_config(plan)['enabled']:
        return None
    curve_analyzer = Path(__file__).with_name('analyze_cpu_trace.py')
    output = root / ('calibration-cpu-curves' if calibration else 'cpu-curves')
    curve_command = [sys.executable, str(curve_analyzer), '--plan', str(plan_path),
                     '--manifest', str(manifest_path), '--output', str(output)]
    print('Reconstructing CPU time curves from idle-transition events (no additional workload run)...', flush=True)
    result = subprocess.run(curve_command, check=False)
    if result.returncode:
        raise RuntimeError(f'CPU curve analysis exited {result.returncode}; raw capture is preserved, do not treat missing curves as zero utilization')
    return json.loads((output / 'summary.json').read_text())


def home_waveform_gate(plan, curves):
    """Only measured home acceptance may unlock an ARM short-peak comparison."""
    if plan.get('profile') != 'short-peak':
        return {'status': 'not_applicable', 'passed': False, 'comparison_allowed': True}
    if not trace_config(plan)['enabled']:
        functional = plan['deployment'] == 'dev'
        return {'status': 'functional_only' if functional else 'insufficient_data',
                'passed': False, 'comparison_allowed': functional,
                'reasons': ['No high-resolution CPU trace: waveform unvalidated; /proc/stat cannot establish a 5-20ms peak.']}
    home = next((run for run in (curves or {}).get('runs', []) if run.get('policy') == 'home'), {})
    shape = home.get('request_waveform', {})
    passed = home.get('status') == 'ok' and shape.get('passed') is True
    return {'status': shape.get('status', 'insufficient_data'), 'passed': passed,
            'comparison_allowed': passed, 'reasons': shape.get('reasons', ['Missing measured home waveform acceptance']),
            'home_request_waveform': shape}


def reviewed_short_peak(plan, inspection_path, root, manifest, binary_sha):
    """Narrow post-result review of existing evidence, never a strict pass.

    Bind to immutable home artifacts rather than the top-level manifest digest:
    that manifest legitimately changes when source_only starts or completes.
    """
    def require(condition, message):
        if not condition:
            raise ValueError('short-peak review rejected: ' + message + '; no new workload launched')

    require(plan.get('profile') == 'short-peak' and trace_config(plan)['enabled'] and
            trace_config(plan)['bin_ms'] == 1, 'requires the frozen traced 1ms short-peak profile')
    inspection_path = Path(inspection_path).resolve()
    evidence = json.loads(inspection_path.read_text())
    evidence_sha = file_digest(inspection_path)
    require(evidence.get('schema_version') == 1 and
            evidence.get('kind') == 'diagnostic_only_no_acceptance_override' and
            evidence.get('status') == 'ok' and evidence.get('issues') == [] and
            evidence.get('strict_result_unchanged') is True, 'inspection is not complete and valid')
    require(evidence.get('plan_sha256') == plan['plan_sha256'] and
            Path(evidence.get('plan', '')).resolve() == root / 'plan.json' and
            Path(evidence.get('manifest', '')).resolve() == root / 'run_manifest.json',
            'inspection plan or manifest identity differs')
    require(evidence.get('bin_ms') == 1 and evidence.get('high_pct') == 90 and
            evidence.get('anchor') == 'actual_first_initial_submit' and
            evidence.get('window_ms') == [0, 30] and evidence.get('late_window_ms') == [20, 30],
            'inspection measurement definition differs')
    require(evidence.get('trace_validation') == {'status': 'ok', 'issues': []},
            'inspection trace validation is incomplete')
    gate = manifest.get('home_waveform_gate', {})
    reason = ['fragmented_onset_fraction_exceeds_10pct']
    shape = evidence.get('strict_original_recomputed', {})
    require(gate.get('status') == 'failed' and gate.get('passed') is False and
            gate.get('comparison_allowed') is False and gate.get('reasons') == reason and
            shape.get('status') == 'failed' and shape.get('passed') is False and
            shape.get('reasons') == reason and gate.get('home_request_waveform') == shape,
            'only the unchanged fragmentation-only strict failure is reviewable')
    count = evidence.get('fragment_count_check', {})
    n = evidence.get('requests', 0)
    require(type(n) is int and n >= plan['waveform_acceptance']['minimum_complete_requests'] and
            evidence.get('unavailable_requests') == 0 and shape.get('requests') == n and
            shape.get('complete_requests') == n and count.get('matches') is True and
            count.get('complete') == n and count.get('strict_complete') == n and
            type(count.get('fragmented')) is int and 0 < count['fragmented'] <= n and
            count.get('inspection_fraction') == count['fragmented'] / n and
            count.get('strict_fraction') == count['fragmented'] / n and
            shape.get('sensitivity', {}).get('90', {}).get('fragmented_fraction') == count['fragmented'] / n,
            'request coverage or fragmentation count differs')
    group = evidence.get('groups', {}).get('all_complete', {})
    metrics = group.get('metrics', {})
    last = metrics.get('last_high_end_ms', {})
    late = metrics.get('late_20_30_high_ms', {})
    require(group.get('n') == n and group.get('touches_30ms_boundary') == 0 and
            group.get('categories', {}).get('high_after_20ms') == 0 and
            isinstance(last.get('n'), int) and last['n'] >= .9 * n and
            isinstance(last.get('max'), (int, float)) and 0 < last['max'] <= 20 and
            late.get('n') == n and late.get('max') == 0,
            'short envelope is missing, censored, or extends after 20ms')
    background = evidence.get('background_90pct_original', {})
    require(all(background.get(key, {}).get('n') == n and
                background.get(key, {}).get('max') == 0 and
                background.get(key) == shape.get('sensitivity', {}).get('90', {}).get(key)
                for key in ('background_high_fraction', 'background_longest_high_ms')),
            'background is missing or contains high occupancy')
    checks = evidence.get('saved_checks', [])
    require(len(checks) == 3 and all(check.get('matches') is True for check in checks) and
            {check.get('source') for check in checks[:2]} ==
            {'manifest.home_waveform_gate', 'cpu-curves/summary.json'}, 'saved strict checks differ')
    waveform_path = root / 'cpu-curves/home/request_waveform.csv'
    require(Path(checks[2].get('source', '')).resolve() == waveform_path and
            checks[2].get('rows') == n and checks[2].get('sha256') == file_digest(waveform_path),
            'saved per-request waveform evidence changed')
    homes = [entry for entry in manifest.get('runs', []) if entry.get('policy') == 'home']
    require(len(homes) == 1, 'requires one existing completed home; home will not be launched')
    directory = root / 'runs/home'
    expected = {'plan_sha256': plan['plan_sha256'], 'binary_sha256': binary_sha,
                'policy': 'home', 'options': dict(plan['base'], **plan['policies']['home']),
                'cpu_trace_enabled': True}
    home = completed_run(directory, expected)
    require(home is not None and home == homes[0], 'saved home identity differs or is missing')
    require(evidence.get('trace_sha256') == home['artifact_sha256']['cpu_trace.txt'],
            'inspection trace hash differs from the verified home trace')
    home_identity = digest({key: home[key] for key in
                            ('plan_sha256', 'binary_sha256', 'options', 'artifact_sha256')})
    review = {'status': 'accepted_posthoc',
              'reason': 'Explicit post-result review: all observed >=90% segments end within 20ms; no late or background high occupancy. Fragmentation-only strict failure remains failed.',
              'inspection_path': str(inspection_path), 'inspection_sha256': evidence_sha,
              'plan_sha256': plan['plan_sha256'], 'home_identity_sha256': home_identity,
              'home_trace_sha256': evidence['trace_sha256'],
              'requests': n, 'fragmented_requests': count['fragmented'],
              'last_high_end_max_ms': last['max'], 'late_20_30_high_max_ms': late['max'],
              'background_high_fraction_max': background['background_high_fraction']['max']}
    prior = manifest.get('home_waveform_review')
    require(prior is None or prior == review, 'previous review evidence hash or home identity differs')
    return review


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
    review_path = getattr(args, 'reviewed_short_peak_inspection', None)
    if review_path is not None and (calibration or getattr(args, 'only', None)):
        raise ValueError('reviewed continuation requires run without --only; it reuses home and continues source_only')
    order = ['home'] if calibration else plan['policy_order']
    selection = ['home'] if getattr(args, 'only', None) == 'home' else order
    root = plan_path.parent
    manifest_path = root / ('calibration_manifest.json' if calibration else 'run_manifest.json')
    manifest = {'schema_version': SCHEMA, 'family': 'request-wave', 'plan_sha256': plan['plan_sha256'],
                'binary_sha256': binary_sha, 'binary': str(binary), 'policy_order': order,
                'calibration': calibration, 'runs': [], 'last_selection': selection,
                'functional_only': plan.get('profile') == 'short-peak' and plan['deployment'] == 'dev' and not trace_config(plan)['enabled']}
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if any(previous.get(k) != manifest[k] for k in ('plan_sha256', 'binary_sha256', 'policy_order', 'calibration')):
            raise ValueError('existing top-level manifest identity differs; no overwrite')
        previous_runs = previous.get('runs', [])
        if len({run['policy'] for run in previous_runs}) != len(previous_runs) or any(run['policy'] not in order for run in previous_runs):
            raise ValueError('existing top-level manifest contains unexpected or duplicate policies')
        # Preserve already completed policies when the user requests just home.
        manifest['runs'] = previous_runs
        for key in ('home_waveform_gate', 'home_waveform_review'):
            if key in previous:
                manifest[key] = previous[key]
    reviewed = review_path is not None
    if reviewed:
        manifest['home_waveform_review'] = reviewed_short_peak(plan, review_path, root, manifest, binary_sha)
        print('POST-RESULT REVIEW accepted_posthoc: reuse verified home; short envelope accepted for this comparison only. Original strict gate remains failed; comparison_allowed=False. This is a documented deviation, not a strict pass.', flush=True)
    analyzed_after_last_run = False
    for policy in selection:
        directory = root / ('calibration' if calibration else 'runs') / policy
        run = execute_one(plan, binary, binary_sha, directory, policy, calibration)
        by_policy = {entry['policy']: entry for entry in manifest['runs']}
        by_policy[policy] = run
        manifest['runs'] = [by_policy[name] for name in order if name in by_policy]
        manifest['analysis_only'] = not calibration and [entry['policy'] for entry in manifest['runs']] == ['home']
        atomic_json(manifest_path, manifest)
        analyzed_after_last_run = False
        summary = run.get('summary', {})
        print(f"{policy}: {run['status']}; all-run task mean={summary.get('task_mean_ms', 'NA')}ms p99={summary.get('task_p99_ms', 'NA')}ms; CPU observer={run['cpu_observation']['status']}", flush=True)
        if run['status'] != 'completed':
            raise RuntimeError(f"{policy} failed; {directory / 'manifest.json'}; {'; '.join(run['errors'])}")
        if plan.get('profile') == 'short-peak' and policy == 'home':
            if reviewed:
                # The inspector already recomputed and matched the saved strict
                # result; reusing it preserves that verdict byte-for-byte.
                gate = manifest['home_waveform_gate']
            else:
                curves = analyze_saved(plan, plan_path, manifest_path, root, calibration)
                analyzed_after_last_run = True
                gate = home_waveform_gate(plan, curves)
            manifest['home_waveform_gate'] = gate
            atomic_json(manifest_path, manifest)
            print(f"Home waveform gate: {gate['status']}; measured_shape_passed={gate['passed']}; comparison_allowed={gate['comparison_allowed']}", flush=True)
            if gate['status'] == 'functional_only':
                print('Development functional exercise only; these runs do not validate the short-peak waveform or production performance.', flush=True)
            if not gate['comparison_allowed'] and not reviewed and not calibration and selection != ['home']:
                raise RuntimeError('home waveform did not pass; source_only was not launched. Inspect saved analysis and cpu-curves. If changing workload, freeze a new plan for both policies; no automatic retuning.')
    print(f'Saved manifest: {manifest_path}')
    print('Above latency includes warmup. Use analyze_request_wave.py for request-cohort latency and selected-CPU observation.')
    if calibration:
        print('Calibration does not tune workload or launch the comparison. If changing assumptions, generate a new plan; never adjust one policy alone.')
    if not analyzed_after_last_run:
        analyze_saved(plan, plan_path, manifest_path, root, calibration)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    create = commands.add_parser('plan', help='freeze legacy three-policy or short-peak two-policy comparison')
    create.add_argument('--deployment', choices=('arm', 'dev'), default='arm')
    create.add_argument('--profile', choices=('legacy', 'short-peak'), default='legacy')
    create.add_argument('--out', type=Path, required=True)
    create.add_argument('--cpus'); create.add_argument('--numa'); create.add_argument('--capacities')
    create.add_argument('--pools', type=int); create.add_argument('--threads', type=int)
    create.add_argument('--cpu-us', type=int); create.add_argument('--duration', type=float)
    create.add_argument('--initial-cpu-us', type=int, help='short-peak initial phase mean; otherwise derived from the frozen fluid model')
    create.add_argument('--steady-cpu-us', type=int, help='short-peak steady phase mean; otherwise derived to preserve nominal target utilization')
    create.add_argument('--peak-target-ms', type=float, default=10, help='short-peak analytic initial guess in [5,20]ms; not a measured guarantee (default 10)')
    create.add_argument('--target-utilization', type=float, default=.5, help='short-peak nominal logical CPU-time demand fraction in [.45,.55], matching its frozen waveform contract (default .5)')
    create.add_argument('--warmup', type=float); create.add_argument('--request-rate', type=float, default=12)
    create.add_argument('--initial-tasks', type=int, default=80)
    create.add_argument('--steady-tasks', type=int, default=80)
    create.add_argument('--burst-window-ms', type=float, help='initial release window: legacy 20ms, short-peak 1ms')
    create.add_argument('--steady-window-ms', type=float, help='steady release window: legacy 400ms, short-peak same-home period')
    create.add_argument('--jitter', type=float, default=.5)
    create.add_argument('--sample-ms', type=int, default=100)
    create.add_argument('--cpu-trace', action='store_true', help='capture idle transitions and automatically reconstruct precise CPU occupancy curves')
    create.add_argument('--cpu-bin-ms', type=int, help='CPU trace reconstruction window, independent of --sample-ms (legacy 5ms; short-peak 1ms)')
    create.add_argument('--cpu-trace-buffer-kb', type=int, default=256, help='trace ring buffer KiB per CPU (default 256)')
    create.add_argument('--observer-cpus', help='optional idle CPUs outside experiment union for the Python observer; launch parent without taskset')
    create.add_argument('--seed', type=int, default=52000)
    for name in ('calibrate', 'run'):
        execute_parser = commands.add_parser(name)
        execute_parser.add_argument('--plan', type=Path, required=True)
        execute_parser.add_argument('--binary', type=Path, default=Path('build/experiments/independent/independent_load'))
        if name == 'run':
            execute_parser.add_argument('--only', choices=('home',), help='run/reuse the ordinary home baseline only; a later run resumes the same plan')
            execute_parser.add_argument('--reviewed-short-peak-inspection', type=Path,
                help='explicit post-result review of an existing home inspection.json; only fragmentation-only failure with short envelopes is reviewable; strict verdict remains failed')
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

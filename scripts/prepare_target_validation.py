#!/usr/bin/env python3
"""Prepare bounded target experiment plans from captured topology; never run load."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re


def cpu_list(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('CPU list is missing or empty')
    result = set()
    for item in text.split(','):
        match = re.fullmatch(r'\s*(\d+)(?:-(\d+))?\s*', item)
        if not match:
            raise ValueError(f'invalid CPU list: {text}')
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if first > last or last > 1048575:
            raise ValueError('invalid or excessively large CPU range')
        result.update(range(first, last+1))
    return result


def compact(cpus):
    values = sorted(cpus)
    chunks = []
    start = previous = values[0]
    for value in values[1:]:
        if value != previous+1:
            chunks.append(str(start) if start == previous else f'{start}-{previous}')
            start = value
        previous = value
    chunks.append(str(start) if start == previous else f'{start}-{previous}')
    return ','.join(chunks)


def validate_topology(topology, cpus, numa):
    groups = [cpu_list(mask) for mask in cpus.split(';')]
    if len(groups) != 6:
        raise ValueError('exactly six cluster masks are required')
    try:
        nodes = [int(value) for value in numa.split(',')]
    except ValueError as error:
        raise ValueError('NUMA labels must be integers') from error
    if len(nodes) != 6 or any(node < 0 for node in nodes):
        raise ValueError('six nonnegative NUMA labels are required')
    union = set().union(*groups)
    if sum(map(len, groups)) != len(union):
        raise ValueError('cluster masks must be disjoint')
    allowed = topology.get('allowed_cpus')
    if not isinstance(allowed, list) or any(type(cpu) is not int or cpu < 0 for cpu in allowed):
        raise ValueError('topology must contain an allowed_cpus integer list')
    if not union <= set(allowed):
        raise ValueError('selected CPUs are outside captured allowed_cpus')
    records = {}
    for row in topology.get('cpus', []):
        cpu = row.get('cpu')
        if cpu in records:
            raise ValueError('duplicate CPU record in topology')
        records[cpu] = row
    node_masks = topology.get('numa_nodes', {})
    sibling_groups = set()
    siblings_known = True
    cluster_details = []
    for index, (mask, node) in enumerate(zip(groups, nodes)):
        node_mask = cpu_list(node_masks.get(f'node{node}'))
        if not mask <= node_mask:
            raise ValueError(f'cluster {index} does not belong to NUMA node {node}')
        for cpu in sorted(mask):
            if cpu not in records:
                raise ValueError(f'missing captured CPU record: {cpu}')
            fields = records[cpu].get('topology', {})
            reported_cluster = cpu_list(fields.get('cluster_cpus_list'))
            if reported_cluster != mask:
                raise ValueError(f'CPU {cpu}: selected mask is not its complete reported cluster_cpus_list')
            siblings = fields.get('thread_siblings_list')
            if siblings is None:
                siblings_known = False
            else:
                group = cpu_list(siblings)
                if cpu not in group or not group <= mask:
                    raise ValueError(f'CPU {cpu}: inconsistent thread sibling evidence')
                sibling_groups.add(tuple(sorted(group)))
        cluster_details.append({'pool': index, 'numa_node': node, 'cpus': sorted(mask),
                                'cpulist': compact(mask), 'logical_cpus': len(mask)})
    return {'allowed_union': sorted(union), 'allowed_union_cpulist': compact(union),
            'captured_allowed_cpus': allowed, 'logical_cpus': len(union), 'clusters': cluster_details,
            'numa_labels': nodes, 'numa_nodes_used': sorted(set(nodes)),
            'physical_cores_from_sibling_lists': len(sibling_groups) if siblings_known else None,
            'reported_smt_widths': sorted({len(group) for group in sibling_groups}) if siblings_known else None,
            'cpus_option': ';'.join(compact(group) for group in groups),
            'numa_option': ','.join(map(str, nodes))}


def strict_workload(arrival='exact'):
    return {'service': 'exact', 'arrival': arrival, 'graph': 'exact',
            'same_cohort_ids': True, 'cohort_total_relative_tolerance': 0}


def contrast(name, baseline, candidate, changes, arrival='exact'):
    return {'name': name, 'baseline': baseline, 'candidate': candidate,
            'allowed_changes': changes, 'workload': strict_workload(arrival)}


def comparison_spec(stage, warmup, duration, comparisons):
    return {'stage': stage, 'window': {'start_s': warmup, 'end_s': duration},
            'minimum_pairs': 5, 'minimum_tail_observations': 20,
            'bootstrap_draws': 5000, 'comparisons': comparisons}


def validate_arrival_boundaries(task_rate, duration, warmup):
    """Match the executable's double division, duration test and ns truncation.

    Nominal integer counts alone are insufficient: e.g. 66/1.1 is below 60,
    causing the duration loop to admit an extra full burst. Keep the existing
    executable semantics and reject unsafe paired plans before writing files.
    """
    for rate in (task_rate, task_rate/100):
        last = round(rate*duration)
        if not last or not (last/rate >= duration and (last-1)/rate < duration):
            raise ValueError('floating arrival boundary is unsafe at duration; choose another task-rate')
        first = round(rate*warmup)
        boundary_ns = int(warmup*1e9)
        if not first or not (int((first/rate)*1e9) >= boundary_ns and
                             int(((first-1)/rate)*1e9) < boundary_ns):
            raise ValueError('floating arrival boundary is unsafe at warmup; choose another task-rate')


def build_bundle(args, topology):
    resource = validate_topology(topology, args.cpus, args.numa)
    for name in ('task_rate', 'dag_qps'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0 or value > 1000000:
            raise ValueError(f'{name} must be finite and in (0, 1000000]')
    if not 1 <= args.threads <= 4096 or not 1 <= args.cpu_us <= 10000000 or not 2 <= args.dag_cpu_us <= 1250000:
        raise ValueError('threads or CPU budget outside supported range')
    duration, warmup, repeats = (60, 10, 3) if args.tier == 'pilot' else (300, 30, 7)
    seed = args.seed if args.seed is not None else (420 if args.tier == 'pilot' else 1420)
    if seed < 0 or seed > (1 << 64)-max(repeats, 2):
        raise ValueError('seed range does not fit unsigned 64-bit independent repetitions')
    for seconds in (duration, warmup):
        batches = args.task_rate*seconds/100
        if not math.isclose(batches, round(batches), rel_tol=0, abs_tol=1e-9):
            raise ValueError('task-rate times duration and warmup must both be divisible by 100 for exact cohort pairing')
    validate_arrival_boundaries(args.task_rate, duration, warmup)
    if math.ceil(args.task_rate*duration) > 2000000:
        raise ValueError('planned independent task count exceeds the bundle limit of 2000000')
    if math.ceil(args.dag_qps*duration) > 1000000:
        raise ValueError('planned DAG request count exceeds the executable limit of 1000000')
    files, phases = {}, {}
    base = {'deployment': 'target', 'pools': 6, 'cpus': resource['cpus_option'],
            'numa': resource['numa_option'], 'affinity': 'cluster', 'threads': args.threads,
            'duration': duration, 'cpu-us': args.cpu_us, 'jitter': .5,
            'long-prob': 0, 'long-factor': 8, 'mean-normalization': 'exact',
            'arrival': 'regular', 'burst-placement': 'local', 'burst-min': 1, 'burst-max': 1,
            'instrumentation': 'full', 'sample-ms': 100, 'max-outstanding': 8192,
            'max-planned-tasks': 2000000, 'drain-seconds': 60, 'work-mode': 'cpu'}

    def add_phase(name, stage, config, count, seconds, warm, reps, comparisons=None,
                  demand=None, question=''):
        config_name = f'{name}.json'
        files[config_name] = config
        phase = {'stage': stage, 'config': config_name,
                 'runner': 'experiments/independent/scripts/run_matrix.py' if stage == 'independent' else 'scripts/run_matrix.py',
                 'cases': count, 'repeats': reps, 'runs': count*reps,
                 'duration_seconds': seconds, 'warmup_seconds': warm,
                 'planned_offered_seconds': count*reps*seconds,
                 'planned_offered_minutes': count*reps*seconds/60,
                 'planned_offered_hours': count*reps*seconds/3600,
                 'maximum_configured_drain_seconds_per_run': 60,
                 'nominal_cpu_demand_seconds_per_second': demand,
                 'question': question}
        if comparisons is not None:
            spec_name = f'{name}-contrasts.json'
            files[spec_name] = comparison_spec(stage, warm, seconds, comparisons)
            phase['comparison_spec'] = spec_name
        if stage == 'dag':
            phase['runner_arguments_required'] = {'--repeats': reps, '--duration': seconds,
                                                  '--proc-interval': 0, '--seed': seed}
        phases[name] = phase

    def independent(name, cases, overrides=None, seconds=duration, warm=warmup, reps=repeats,
                    comparisons=None, demand=None, question=''):
        common = dict(base)
        common.update(overrides or {})
        common['duration'] = seconds
        matrix = {'schema': 2, 'family': name, 'tier': args.tier, 'base': common,
                  'repeats': reps, 'seed': seed,
                  'analysis': {'warmup_seconds': warm, 'steady_end_seconds': seconds,
                               'must_check_steady_state': True}, 'cases': cases}
        add_phase(name, 'independent', matrix, len(cases), seconds, warm, reps,
                  comparisons, demand, question)

    def case(name, **options):
        return {'name': name, 'options': options}

    independent('s3-smoke', [case(affinity, affinity=affinity) for affinity in ('none', 'numa', 'cluster')],
                {'cpu-us': 1000, 'arrival-rate': 6, 'burst-min': 20, 'burst-max': 20},
                seconds=3, warm=0, reps=1, demand=.12,
                question='Verify six pools, masks, accounting and all three affinities; not a performance inference.')
    rates = [120, 240, 480, 720]
    independent('s3-calibration', [case(f'r{rate}', **{'arrival-rate': rate}) for rate in rates],
                {'instrumentation': 'queue'}, seconds=30, warm=10, reps=2,
                demand={f'r{rate}': rate*args.cpu_us/1e6 for rate in rates},
                question='Initial rate scan only; inspect backlog, completion and lag before claiming sustainable capacity.')
    burst_cases = [case('smooth', **{'arrival-rate': args.task_rate}),
                   case('spread', **{'arrival-rate': args.task_rate/100, 'burst-min': 100,
                                      'burst-max': 100, 'burst-placement': 'spread'}),
                   case('local', **{'arrival-rate': args.task_rate/100, 'burst-min': 100,
                                     'burst-max': 100, 'burst-placement': 'local'})]
    independent('s3-burst', burst_cases,
                comparisons=[contrast('time-concentration', 'smooth', 'spread',
                                      ['arrival-rate', 'burst-min', 'burst-max', 'burst-placement'], 'count'),
                             contrast('pool-concentration', 'spread', 'local', ['burst-placement'])],
                demand=args.task_rate*args.cpu_us/1e6,
                question='Same generated task budgets; separate time concentration from one-pool concentration.')
    burst_base = {'arrival-rate': args.task_rate/100, 'burst-min': 100, 'burst-max': 100}
    independent('s3-deployment', [case(affinity, affinity=affinity) for affinity in ('none', 'numa', 'cluster')],
                burst_base,
                comparisons=[contrast('none-vs-numa', 'none', 'numa', ['affinity']),
                             contrast('none-vs-cluster', 'none', 'cluster', ['affinity'])],
                demand=args.task_rate*args.cpu_us/1e6,
                question='Same whole-process CPU union and work; compare the three affinity modes.')
    independent('s3-threads', [case(f't{threads}', threads=threads) for threads in (8, 16, 64, 256)],
                burst_base,
                comparisons=[contrast(f't8-vs-t{threads}', 't8', f't{threads}', ['threads']) for threads in (16, 64, 256)],
                demand=args.task_rate*args.cpu_us/1e6,
                question='Same clustered work; distinguish Folly queue wait from callback wall-time growth.')

    dag_base = {'pools': 6, 'threads': args.threads, 'cpus': resource['cpus_option'],
                'numa': resource['numa_option'], 'affinity': 'cluster', 'graph': 'waves',
                'wave-widths': '40,10', 'wave-depths': '1,2', 'cpu-us': args.dag_cpu_us,
                'jitter': 0, 'long-prob': 0, 'long-factor': 8, 'rpc-prob': 0, 'join-us': 100,
                'metric': 'queue', 'queue': 'default', 'qps': args.dag_qps, 'arrival': 'regular',
                'sample-ms': 100, 'max-inflight': 128, 'drain-seconds': 60,
                'duration': duration, 'task-log': 1, 'work-mode': 'cpu', 'policy': 'home',
                'balance-forks': 'all', 'memory-kb': 0, 'candidate-scope': 'all',
                'cross-numa-penalty-us': 0, 'requests': 0}

    def dag_case(name, **overrides):
        options = dict(dag_base, **overrides)
        return {'name': name, 'args': [item for key, value in options.items() for item in ('--'+key, str(value))]}

    dag_demand = args.dag_qps*(60*args.dag_cpu_us+200)/1e6
    add_phase('s4-baseline', 'dag', [dag_case(affinity, affinity=affinity) for affinity in ('none', 'numa', 'cluster')],
              3, duration, warmup, repeats,
              [contrast('none-vs-numa', 'none', 'numa', ['affinity']),
               contrast('none-vs-cluster', 'none', 'cluster', ['affinity'])], dag_demand,
              'Same home policy and DAG; compare affinity before comparing migration policies.')
    routing = [dag_case('home'), dag_case('initial', **{'policy': 'fanout', 'balance-forks': 'initial'}),
               dag_case('all', policy='fanout'), dag_case('sticky', policy='sticky'), dag_case('borrow', policy='borrow')]
    add_phase('s4-routing', 'dag', routing, 5, duration, warmup, repeats,
              [contrast('initial-vs-home', 'home', 'initial', ['policy', 'balance-forks']),
               contrast('later-forks-added', 'initial', 'all', ['balance-forks']),
               contrast('nonfork-submission-balancing-added', 'all', 'sticky', ['policy']),
               contrast('borrow-vs-sticky', 'borrow', 'sticky', ['policy'])], dag_demand,
              'Identical graph with initial fork, later fork, ordinary successors and joins; all-to-sticky enables balancing nonfork submissions, not successors alone.')
    total_seconds = sum(phase['planned_offered_seconds'] for phase in phases.values())
    plan = {'schema': 1, 'kind': 'configuration_only_no_execution', 'tier': args.tier, 'seed': seed,
            'resource': resource, 'measurement': {'duration_seconds': duration, 'warmup_seconds': warmup,
                                               'repeats': repeats},
            'phases': phases, 'total_runs': sum(phase['runs'] for phase in phases.values()),
            'total_planned_offered_minutes': total_seconds/60,
            'total_planned_offered_hours': total_seconds/3600,
            'notes': [
                'This command only writes configuration files; no executors, subprocesses or workloads are started.',
                'Topology evidence comes only from the supplied capture, not the generator host live affinity.',
                'Run every phase on the target under taskset -c '+resource['allowed_union_cpulist']+'.',
                'Captured allowed CPUs and complete clusters do not prove exclusive allocation; verify ownership and current cgroup limits before execution.',
                'Missing or unreadable quota fields in an older capture are unknown, not unlimited; planning does not reject missing quota evidence.',
                'Run one phase at a time. Times are offered durations only; startup, drain, logging and analysis add wall time.',
                'Smoke and the 30-second initial calibration stay short in both tiers; they are not sustainable-capacity certification.',
                'Pilot has three independent repeats: no paired confidence interval under minimum_pairs=5.',
                'Confirm has seven repeats but does not guarantee statistical power, stationarity or sufficient independent tail observations.',
                'At 12 QPS, a 50-second pilot window has about 600 requests; request p99 intervals are withheld by the default 20-tail-observation gate.',
                'Logical CPUs and SMT width are topology facts, not measured CPU-service capacity; no rho is inferred.',
                'DAG queue selection uses the demo default capacity heuristic; these weights are not a measured throughput claim.',
                'The configured long-factor=8 still participates in DAG CLI budget bounds when long-prob=0; dag-cpu-us is limited to 1250000.',
                'If arrivals lag, backlog grows or work is rejected/canceled, report overload and do not infer successful-only latency gains.',
                'DAG runner --extra wins over case arguments; changing it can invalidate declared workload contracts.',
                'Independent runner reads analysis windows from its config; compare_runs and predict need explicit matching windows/specs.',
            ]}
    files['plan.json'] = plan
    return files


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--topology', type=Path, required=True)
    p.add_argument('--cpus', required=True)
    p.add_argument('--numa', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--tier', choices=['pilot', 'confirm'], default='pilot')
    p.add_argument('--seed', type=int, help='Base seed; defaults to 420 for pilot or 1420 for confirm')
    p.add_argument('--task-rate', type=float, default=240)
    p.add_argument('--threads', type=int, default=16)
    p.add_argument('--cpu-us', type=int, default=100000)
    p.add_argument('--dag-qps', type=float, default=12)
    p.add_argument('--dag-cpu-us', type=int, default=20000)
    return p


def main():
    args = parser().parse_args()
    if args.output.exists():
        raise ValueError('output directory already exists; choose a fresh plan directory')
    raw = args.topology.read_bytes()
    bundle = build_bundle(args, json.loads(raw))
    bundle['plan.json']['topology_source'] = {'path': str(args.topology.resolve()),
                                             'sha256': hashlib.sha256(raw).hexdigest()}
    args.output.mkdir(parents=True)
    for filename, value in bundle.items():
        (args.output/filename).write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')
    plan = bundle['plan.json']
    print(f'Prepared only: {len(plan["phases"])} phases, {plan["total_runs"]} planned runs, '
          f'{plan["total_planned_offered_minutes"]:.2f} offered minutes, excluding drain. '
          f'CPU union: {plan["resource"]["allowed_union_cpulist"]}. No workload was executed.')


if __name__ == '__main__':
    main()

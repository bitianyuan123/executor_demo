#!/usr/bin/env python3
"""Plan, freeze and serially run independent-task load-observation/routing trials."""
import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import socket
import subprocess
import sys
import time

SCHEMA = 1
TARGET_CPUS = '0-15;16-31;32-47;192-207;208-223;224-239'
TARGET_NUMA = '0,0,0,2,2,2'
METRICS = ('queue', 'outstanding', 'work', 'remaining')
SPLIT_START = {'observe': 11000, 'tune': 21000, 'evaluate': 31000, 'cost': 41000}
FAMILIES = ('main', 'negative', 'affinity', 'arrival', 'distribution', 'burst', 'estimator')
ROUTE_KEYS = {'routing', 'load-metric', 'load-threshold', 'min-improvement'}
SCOPE_KEYS = ('deployment', 'cpus', 'numa', 'pools', 'threads', 'affinity', 'capacities',
              'cpu-us', 'arrival-rate', 'arrival', 'burst-min', 'burst-max', 'burst-placement',
              'jitter', 'long-prob', 'long-factor', 'mean-normalization', 'estimate-us',
              'estimate-jitter', 'work-mode', 'load-observation', 'progress-us')


def workload_scope(options):
    return {key: options.get(key) for key in SCOPE_KEYS}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def cpus(value):
    result = set()
    for piece in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', piece):
            raise ValueError(f'invalid CPU list: {value}')
        bounds = [int(x) for x in piece.split('-')]
        first, last = bounds[0], bounds[-1]
        if last < first or last >= 1024:
            raise ValueError('CPU range must increase and remain below CPU_SETSIZE=1024')
        group = set(range(first, last + 1))
        if result & group:
            raise ValueError('duplicate CPU in a group')
        result |= group
    return sorted(result)


def topology(groups_string, numa_string, pools, affinity, threads, capacity_string=None):
    groups = [cpus(group) for group in groups_string.split(';')]
    if len(groups) != pools:
        raise ValueError('CPU groups must match pools')
    union = set()
    for group in groups:
        if union.intersection(group):
            raise ValueError('cluster CPU groups must be disjoint')
        union.update(group)
    nodes = [int(x) for x in numa_string.split(',')] if numa_string else list(range(pools))
    if len(nodes) != pools or min(nodes) < 0:
        raise ValueError('NUMA labels must match pools and be nonnegative')
    masks = []
    for index in range(pools):
        if affinity == 'cluster':
            masks.append(groups[index])
        elif affinity == 'none':
            masks.append(sorted(union))
        elif affinity == 'numa':
            masks.append(sorted(c for i, g in enumerate(groups) if nodes[i] == nodes[index] for c in g))
        else:
            raise ValueError('invalid affinity')
    capacities = ([float(x) for x in capacity_string.split(',')] if capacity_string else
                  [float(min(threads, len(mask))) for mask in masks])
    if len(capacities) != pools or any(not math.isfinite(x) or x <= 0 for x in capacities):
        raise ValueError('capacities must be positive finite values, one per pool')
    return {'cluster_groups': groups, 'numa_labels': nodes, 'cpu_union': sorted(union),
            'pool_masks': masks, 'capacities': capacities,
            'shared_cpu_domain': affinity != 'cluster',
            'capacity_interpretation': 'per-pool score normalization, not additive machine capacity'}



def cpu_node(cpu):
    links = list(Path(f'/sys/devices/system/cpu/cpu{cpu}').glob('node[0-9]*'))
    if len(links) == 1:
        return int(links[0].name[4:])
    if not links and not Path('/sys/devices/system/node').exists():
        return 0  # Linux UMA without CONFIG_NUMA.
    raise ValueError(f'cannot discover NUMA node for CPU {cpu}; pass explicit --numa after verification')


def infer_nodes(groups_string):
    labels = []
    for group in groups_string.split(';'):
        found = {cpu_node(cpu) for cpu in cpus(group)}
        if len(found) != 1:
            raise ValueError('a CPU group spans NUMA nodes; split --cpus into node-local groups')
        labels.append(str(found.pop()))
    return ','.join(labels)


def int_list(value):
    values = [int(x) for x in value.split(',')]
    if not values or len(set(values)) != len(values) or any(x < 0 for x in values):
        raise ValueError('expected unique nonnegative integers')
    return values


def seed_sets(repeats, phase, override=None, selection=None):
    out = {key: list(range(start, start + repeats)) for key, start in SPLIT_START.items()}
    if selection:
        for key, values in selection['split_seeds'].items():
            if key != 'evaluate':
                out[key] = list(values)
    if override:
        out[phase] = int_list(override)
    seen = set()
    for key, values in out.items():
        if not values or seen.intersection(values):
            raise ValueError('observe/tune/evaluate/cost seeds must be disjoint')
        seen.update(values)
    return out


def metric_observation(metric):
    return 'progress' if metric == 'remaining' else 'basic'


def generate(args):
    target = args.preset == 'target'
    selection = json.loads(args.selection.read_text()) if args.selection else None
    if args.phase == 'evaluate' and not selection:
        raise ValueError('evaluate requires --selection produced by freeze from actual tune results')
    if selection and selection.get('source_split') != 'tune':
        raise ValueError('selection must come from tune')
    if args.observation != 'progress' and args.phase != 'evaluate':
        raise ValueError('--observation minimal is reserved for frozen evaluate; cost creates its own modes')
    if args.phase != 'evaluate' and selection:
        raise ValueError('--selection only applies to evaluate')
    allowed = sorted(os.sched_getaffinity(0))[:4]
    if args.cpus:
        group_string = args.cpus
    elif target:
        group_string = TARGET_CPUS
    else:
        by_node = {}
        for cpu in allowed:
            by_node.setdefault(cpu_node(cpu), []).append(cpu)
        if len(by_node) == 1:
            midpoint = max(1, len(allowed) // 2)
            groups = [allowed[:midpoint], allowed[midpoint:]]
        else:
            groups = list(by_node.values())
        group_string = ';'.join(','.join(map(str, group)) for group in groups if group)
    pools = len(group_string.split(';'))
    if target and pools != 6:
        raise ValueError('target requires six explicit/disjoint cluster CPU groups')
    node_string = args.numa or (TARGET_NUMA if target else infer_nodes(group_string))
    first_capacity = len(cpus(group_string.split(';')[0]))
    thread_counts = int_list(args.threads) if args.threads else ([16, 256] if target else [first_capacity, first_capacity * 16])
    if any(t < 1 for t in thread_counts):
        raise ValueError('threads must be positive')
    duration = args.duration if args.duration is not None else (60.0 if target else 3.0)
    warmup = args.warmup if args.warmup is not None else (10.0 if target else .5)
    budget = args.cpu_us if args.cpu_us is not None else (100000 if target else 2000)
    task_rate = args.task_rate if args.task_rate is not None else (240.0 if target else 160.0)
    repeats = args.repeats if args.repeats is not None else (3 if target else 1)
    if repeats < 1 or not (duration > warmup >= 0) or budget < 1 or not math.isfinite(task_rate) or task_rate <= 0:
        raise ValueError('invalid repeats, window, CPU budget or task rate')
    seeds = seed_sets(repeats, args.phase, args.seeds, selection)
    metrics = args.metrics.split(',')
    if not metrics or len(set(metrics)) != len(metrics) or any(m not in METRICS for m in metrics):
        raise ValueError('invalid/duplicate metrics')
    families = args.families.split(',') if args.families else (['main', 'negative'] if args.phase == 'observe' else ['main'])
    if any(f not in FAMILIES for f in families) or len(set(families)) != len(families):
        raise ValueError('invalid/duplicate families')
    base = {'deployment': 'target' if target else 'dev', 'pools': pools, 'cpus': group_string,
            'numa': node_string, 'affinity': 'cluster', 'duration': duration, 'cpu-us': budget,
            'jitter': .5, 'long-prob': 0, 'long-factor': 8, 'mean-normalization': 'exact',
            'arrival': 'regular', 'burst-placement': 'local', 'work-mode': 'cpu',
            'estimate-us': budget, 'estimate-jitter': 0, 'instrumentation': 'full',
            'load-observation': args.observation, 'progress-us': args.progress_us if args.progress_us is not None else (5000 if target else 500),
            'routing': 'home', 'load-metric': 'queue', 'load-threshold': 0, 'min-improvement': 0,
            'sample-ms': 100 if target else 20, 'max-outstanding': 100000 if target else 10000,
            'max-planned-tasks': 2000000, 'drain-seconds': 120 if target else 20}
    if args.phase == 'evaluate' and args.observation == 'minimal':
        base['load-metric'] = selection['metric']
        base['instrumentation'] = 'none'
    width = 100 if target else 20
    cases = []

    def add(name, workload, route, group, question):
        options = dict(workload, **route)
        effective = dict(base, **options)
        layout = topology(group_string, node_string, pools, effective['affinity'], effective['threads'], args.capacities)
        effective['capacities'] = ','.join(f'{x:g}' for x in layout['capacities'])
        options['capacities'] = effective['capacities']
        expected_tasks = duration * effective['arrival-rate'] * effective['burst-min']
        scope = workload_scope(effective)
        known_scopes = [item['workload'] for item in selection.get('applicable_scopes', [])] if selection else []
        cases.append({'name': name, 'options': options, 'comparison_group': group, 'question': question,
                      'workload_scope': scope,
                      'evaluation_scope': ('seen-in-tuning' if scope in known_scopes else 'extrapolation') if selection else None,
                      'layout': layout, 'pair_workload_sha256': digest({k: v for k, v in effective.items() if k not in ROUTE_KEYS}),
                      'sample_plan': {'expected_total_tasks': expected_tasks,
                                      'expected_analysis_tasks': (duration - warmup) * effective['arrival-rate'] * effective['burst-min'],
                                      'nominal_offered_cpu_seconds': expected_tasks * budget / 1e6,
                                      'realized_count_may_differ': effective['arrival'] == 'poisson'}})

    for threads in thread_counts:
        for family in families:
            variants = [('local', {})]
            if family == 'negative':
                variants = [('smooth', {'burst-min': 1, 'burst-max': 1}), ('spread', {'burst-placement': 'spread'})]
            elif family == 'affinity':
                variants = [(a, {'affinity': a}) for a in ('none', 'numa')]
            elif family == 'arrival':
                variants = [('poisson', {'arrival': 'poisson'})]
            elif family == 'distribution':
                variants = [('fixed', {'jitter': 0}), ('tail', {'long-prob': .02})]
            elif family == 'burst':
                variants = [(f'k{k}', {'burst-min': k, 'burst-max': k}) for k in ([20, 400] if target else [4, 80])]
            elif family == 'estimator':
                variants = [('noisy', {'estimate-jitter': .75}), ('oracle', {'estimate-us': 0})]
            for label, updates in variants:
                workload = {'threads': threads, 'burst-min': width, 'burst-max': width}
                workload.update(updates)
                workload['arrival-rate'] = task_rate / workload['burst-min']
                group = f'{family}-t{threads}-{label}'
                question = f'{family}: same seed, workload and observation; compare routing only'
                if args.phase == 'observe':
                    routes = [('home', {'routing': 'home'})]
                elif args.phase == 'tune':
                    routes = [('home', {'routing': 'home'}), ('spread-policy', {'routing': 'spread'})]
                    for metric in metrics:
                        thresholds = [.5, 2.0] if metric in ('queue', 'outstanding') else [budget / 1000 * .5, budget / 1000 * 2]
                        if args.thresholds:
                            thresholds = [float(x) for x in args.thresholds.split(',')]
                        if any(not math.isfinite(x) or x < 0 for x in thresholds):
                            raise ValueError('thresholds must be finite nonnegative values')
                        routes += [(f'{metric}-h{h:g}', {'routing': 'threshold', 'load-metric': metric, 'load-threshold': h}) for h in thresholds]
                elif args.phase == 'evaluate':
                    metric, threshold = selection['metric'], selection['threshold']
                    routes = [('home', {'routing': 'home'}), ('spread-policy', {'routing': 'spread'}),
                              (f'{metric}-h{threshold:g}', {'routing': 'threshold', 'load-metric': metric, 'load-threshold': threshold})]
                    if selection.get('queue_threshold') is not None and metric != 'queue':
                        h = selection['queue_threshold']
                        if args.observation == 'minimal':
                            routes.append(('queue-home', {'routing': 'home', 'load-metric': 'queue'}))
                        routes.append((f'queue-h{h:g}', {'routing': 'threshold', 'load-metric': 'queue', 'load-threshold': h}))
                else:
                    # Useful loop work stays fixed; cpu-mode alone would hide observer cost inside budget.
                    workload.update({'work-mode': 'iterations', 'iterations-per-us': args.iterations_per_us})
                    routes = [(f'cost-{mode}-home', {'routing': 'home', 'load-observation': mode, 'instrumentation': 'none'})
                              for mode in ('off', 'basic', 'progress')]
                    for metric in metrics:
                        h = 1.0 if metric in ('queue', 'outstanding') else budget / 1000
                        routes.append((f'cost-minimal-{metric}-home', {'routing': 'home', 'load-metric': metric,
                                       'load-observation': 'minimal', 'instrumentation': 'none'}))
                        for mode in dict.fromkeys(['progress', metric_observation(metric), 'minimal']):
                            routes.append((f'cost-{mode}-{metric}', {'routing': 'threshold', 'load-metric': metric,
                                          'load-threshold': h, 'load-observation': mode, 'instrumentation': 'none'}))
                for suffix, route in routes:
                    pair_group = f"{group}-obs-{route['load-observation']}" if args.phase == 'cost' else group
                    if args.phase == 'cost' and route['load-observation'] == 'minimal':
                        pair_group += f"-{route['load-metric']}"
                    if args.phase == 'evaluate' and args.observation == 'minimal':
                        pair_group += f"-minimal-{route.get('load-metric', base['load-metric'])}"
                    add(f'{group}-{suffix}', workload, route, pair_group, question)
    union = topology(group_string, node_string, pools, 'cluster', thread_counts[0], args.capacities)['cpu_union']
    spec = {'schema_version': SCHEMA, 'family': 'stage3-balance', 'phase': args.phase, 'split': args.phase,
            'tier': 'target-pilot' if target else 'development-functional', 'base': base, 'cases': cases,
            'split_seeds': seeds, 'seeds': seeds[args.phase], 'schedule_seed': args.schedule_seed,
            'analysis': {'warmup_seconds': warmup, 'steady_end_seconds': duration, 'cohort': 'planned-arrival',
                         'minimum_independent_runs_for_confirmation': 7},
            'selection': selection, 'launcher_cpu_union': union,
            'planned_runs': len(cases) * len(seeds[args.phase]),
            'planned_offered_hours': duration * len(cases) * len(seeds[args.phase]) / 3600,
            'notes': ['Plan only; no binary is started until run is explicitly requested.',
                      'Logical CPU capacities normalize scores; SMT slots are not full physical cores.',
                      'none/numa pools share CPU domains; per-pool capacities must not be added.',
                      'Exact mean fixes CPU-budget sum to realized task count times cpu-us, not actual CPU overhead.',
                      'Poisson counts vary across seeds; same-seed routing pairs must have identical planned workload.',
                      'Development duration validates functionality, not steady-state performance.',
                      'Three target seeds are pilot evidence; confirm on >=7 independent seeds with adequate tail samples.',
                      'Oracle estimates are synthetic information upper bounds, not deployable production estimates.']}
    validate_spec(spec)
    return spec


def validate_spec(spec):
    if spec.get('schema_version') != SCHEMA or spec.get('split') not in SPLIT_START:
        raise ValueError('unknown balance spec schema or split')
    phase = spec['split']
    if not spec['seeds'] or spec['seeds'] != spec['split_seeds'][phase]:
        raise ValueError('split seed identity mismatch')
    seen = set()
    for key, values in spec['split_seeds'].items():
        if key not in SPLIT_START or not values or len(set(values)) != len(values) or seen.intersection(values):
            raise ValueError('seed splits must be nonempty, unique and disjoint')
        seen.update(values)
    names = [case['name'] for case in spec['cases']]
    if not names or len(set(names)) != len(names) or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', n) or n in ('.', '..') for n in names):
        raise ValueError('case names must be distinct safe directory names')
    for case in spec['cases']:
        options = dict(spec['base'], **case['options'])
        layout = topology(options['cpus'], options.get('numa'), options['pools'], options['affinity'], options['threads'], options.get('capacities'))
        if layout['cpu_union'] != spec['launcher_cpu_union']:
            raise ValueError('all cases must use the same CPU union')
        expected_hash = digest({k: v for k, v in options.items() if k not in ROUTE_KEYS})
        if expected_hash != case['pair_workload_sha256'] or layout != case['layout']:
            raise ValueError('case options/layout fingerprint changed; regenerate the plan')
        duration = float(options['duration'])
        warmup, end = spec['analysis']['warmup_seconds'], spec['analysis']['steady_end_seconds']
        if not (0 <= warmup < end <= duration):
            raise ValueError('analysis window outside offered duration')
        if options['routing'] == 'threshold' and options['load-metric'] == 'remaining' and options['load-observation'] not in ('progress', 'minimal'):
            raise ValueError('remaining routing requires progress or minimal observation')
        if options['routing'] == 'threshold' and options['load-metric'] in ('outstanding', 'work') and options['load-observation'] == 'off':
            raise ValueError('outstanding/work routing requires basic or progress observation')
    if phase == 'evaluate':
        selection = spec.get('selection')
        if not selection or selection.get('source_split') != 'tune' or set(spec['seeds']).intersection(selection['source_seeds']):
            raise ValueError('evaluation must use a frozen tuning selection and fresh seeds')


def make_schedule(spec):
    rng = random.Random(spec['schedule_seed'])
    result = []
    for repetition, seed in enumerate(spec['seeds']):
        block = list(spec['cases'])
        rng.shuffle(block)
        result += [(repetition, seed, case) for case in block]
    return result


def summary(spec):
    print(f"split={spec['split']}; cases={len(spec['cases'])}; seeds={spec['seeds']}; runs={spec['planned_runs']}")
    print(f"offered hours={spec['planned_offered_hours']:.3f}, excluding drain/startup; logical CPUs={len(spec['launcher_cpu_union'])}")
    print('CPU union=' + ','.join(map(str, spec['launcher_cpu_union'])))
    print('This is a plan/pilot, not an empirical performance conclusion.')


def freeze(args):
    manifest = json.loads(args.manifest.read_text())
    if manifest.get('split') != 'tune':
        raise ValueError('freeze requires an actual tune manifest')
    validate_spec(manifest['spec'])
    if digest(manifest['spec']) != manifest['config_sha256']:
        raise ValueError('tune config digest mismatch')
    rows = [r for r in manifest['runs'] if r.get('status') == 'completed' and r.get('returncode') == 0
            and r.get('summary', {}).get('conservation_ok') is True
            and r['summary'].get('counters_balanced') is True
            and r['summary'].get('final_outstanding', -1) == 0
            and r['summary'].get('sampling_truncated') is False
            and r['summary'].get('sampling_failed') is False
            and r['summary'].get('sample_rows_dropped', -1) == 0
            and r['summary'].get('planned_tasks', -1) == r['summary'].get('completed_tasks', -2)
            and all(r['summary'].get(k, 1) == 0 for k in ('rejected_tasks', 'canceled_tasks', 'failed_tasks'))]
    def candidate(metric, threshold):
        return [r for r in rows if r['options'].get('routing') == 'threshold' and
                r['options'].get('load-metric') == metric and r['options'].get('load-threshold') == threshold]
    def complete_pairs(metric, threshold):
        expected = [case for case in manifest['spec']['cases']
                    if dict(manifest['spec']['base'], **case['options']).get('routing') == 'threshold'
                    and dict(manifest['spec']['base'], **case['options']).get('load-metric') == metric
                    and dict(manifest['spec']['base'], **case['options']).get('load-threshold') == threshold]
        found = candidate(metric, threshold)
        expected_keys = {(case['name'], seed) for case in expected for seed in manifest['seeds']}
        if not expected_keys or {(r['case'], r['seed']) for r in found} != expected_keys:
            raise ValueError('chosen metric/threshold lacks complete successful tune cases for every seed')
        for row in found:
            baselines = [r for r in rows if r['seed'] == row['seed'] and
                         r['comparison_group'] == row['comparison_group'] and r['options'].get('routing') == 'home']
            if len(baselines) != 1 or row['pair_workload_sha256'] != baselines[0]['pair_workload_sha256']:
                raise ValueError('tune choice requires a successful home baseline with identical workload/observation')
        return found
    chosen = complete_pairs(args.metric, args.threshold)
    if args.queue_threshold is not None:
        complete_pairs('queue', args.queue_threshold)
    applicable = {r['comparison_group']: workload_scope(r['options']) for r in chosen}
    write_new(args.output, {'schema_version': SCHEMA, 'source_split': 'tune', 'source_manifest': str(args.manifest.resolve()),
                           'source_manifest_sha256': file_digest(args.manifest), 'source_config_sha256': manifest['config_sha256'],
                           'source_seeds': manifest['seeds'], 'split_seeds': manifest['spec']['split_seeds'],
                           'chosen_cases': sorted({r['case'] for r in chosen}),
                           'applicable_scopes': [{'comparison_group': key, 'workload': value} for key, value in sorted(applicable.items())],
                           'metric': args.metric,
                           'threshold': args.threshold, 'queue_threshold': args.queue_threshold,
                           'reason': args.reason, 'selected_at_unix': time.time(),
                           'note': 'Manual frozen decision before evaluation; no automatic claim of a winner.'})
    print(args.output)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@contextlib.contextmanager
def lock_output(output):
    lock = output / '.runner.lock'
    if lock.exists():
        old = json.loads(lock.read_text())
        if old.get('host') != socket.gethostname() or alive(int(old['pid'])):
            raise ValueError('another runner may be active; inspect .runner.lock before resuming')
        lock.unlink()
    with lock.open('x') as f:
        json.dump({'host': socket.gethostname(), 'pid': os.getpid()}, f)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def execute(args):
    spec = json.loads(args.config.read_text())
    validate_spec(spec)
    summary(spec)
    binary = args.binary.resolve()
    if args.action == 'dry-run':
        for repetition, seed, case in make_schedule(spec):
            options = dict(spec['base'], **case['options'], seed=seed)
            print(json.dumps({'case': case['name'], 'seed': seed, 'options': options}, sort_keys=True))
        return
    if not binary.is_file():
        raise ValueError(f'binary does not exist: {binary}')
    union = spec['launcher_cpu_union']
    if not set(union).issubset(os.sched_getaffinity(0)):
        raise ValueError('configured CPU union is not available in current process affinity')
    binary_hash, config_hash = file_digest(binary), digest(spec)
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise ValueError('output exists; choose a new directory or --resume')
    if not output.exists() and args.resume:
        raise ValueError('--resume requires an existing output directory')
    output.mkdir(parents=True, exist_ok=True)
    with lock_output(output):
        manifest_path = output / 'manifest.json'
        if args.resume:
            manifest = json.loads(manifest_path.read_text())
            if manifest['binary_sha256'] != binary_hash or manifest['config_sha256'] != config_hash:
                raise ValueError('resume refuses changed binary/config; use a new output directory')
            if manifest['binary'] != str(binary):
                raise ValueError('resume requires the original binary path')
        else:
            manifest = {'schema_version': SCHEMA, 'family': 'stage3-balance', 'split': spec['split'],
                        'binary': str(binary), 'binary_sha256': binary_hash, 'config_sha256': config_hash,
                        'spec': spec, 'seeds': spec['seeds'], 'analysis': spec['analysis'],
                        'launcher_cpu_union': union, 'schedule': 'randomized case order within seed blocks',
                        'created_at_unix': time.time(), 'host': socket.gethostname(), 'runs': []}
            write_new(output / 'config.json', spec)
            atomic_json(manifest_path, manifest)
        by_id = {r['run_id']: r for r in manifest['runs']}
        for repetition, seed, case in make_schedule(spec):
            if file_digest(binary) != binary_hash:
                raise ValueError('binary changed during the matrix; stop and use a new output directory')
            run_id = f"{case['name']}-s{seed}"
            entry = by_id.get(run_id)
            if entry and entry.get('status') == 'completed' and entry.get('returncode') == 0:
                if not (Path(entry['path']) / 'summary.json').exists():
                    raise ValueError(f'completed result is missing: {entry["path"]}')
                print(f'{run_id}: retained completed result', flush=True)
                continue
            if entry and entry.get('status') == 'running' and entry.get('child_pid') and alive(entry['child_pid']):
                raise ValueError(f'old child may still be running, PID={entry["child_pid"]}; inspect before resume')
            if entry and entry.get('status') == 'running':
                entry['attempts'][-1].update(status='interrupted', returncode='runner-lost', recovered_at_unix=time.time())
                entry.update(status='interrupted', returncode='runner-lost')
                entry.pop('child_pid', None)
            if not entry:
                entry = {'run_id': run_id, 'case': case['name'], 'seed': seed, 'repetition': repetition,
                         'split': spec['split'], 'comparison_group': case['comparison_group'], 'attempts': []}
                manifest['runs'].append(entry)
                by_id[run_id] = entry
            attempt_id = len(entry['attempts']) + 1
            attempt = output / run_id / f'attempt-{attempt_id:03d}'
            attempt.mkdir(parents=True, exist_ok=False)
            run_path = attempt / 'run'
            options = dict(spec['base'], **case['options'], seed=seed)
            command = [str(binary), '--output', str(run_path)]
            for key, value in options.items():
                command += ['--' + key, str(value)]
            timeout = float(options['duration']) + float(options['drain-seconds']) + args.timeout_slack
            record = {'attempt': attempt_id, 'path': str(run_path), 'log': str(attempt / 'stdout.log'),
                      'command': command, 'started_at_unix': time.time(), 'timeout_seconds': timeout,
                      'status': 'running', 'returncode': None}
            entry['attempts'].append(record)
            entry.update(record, options=options, layout=case['layout'], evaluation_scope=case.get('evaluation_scope'), pair_workload_sha256=case['pair_workload_sha256'])
            atomic_json(manifest_path, manifest)
            process = None
            interrupted = False
            try:
                with (attempt / 'stdout.log').open('x') as log:
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True, preexec_fn=lambda: os.sched_setaffinity(0, union))
                    entry['child_pid'] = process.pid
                    atomic_json(manifest_path, manifest)
                    code = process.wait(timeout=timeout)
                status = 'completed' if code == 0 else 'failed'
                if code == 0 and not (run_path / 'summary.json').is_file():
                    code, status = 'missing-summary', 'failed'
            except subprocess.TimeoutExpired:
                stop_process(process)
                code, status = 'timeout', 'timeout'
            except KeyboardInterrupt:
                if process:
                    stop_process(process)
                code, status, interrupted = 'interrupted', 'interrupted', True
            except OSError as error:
                code, status = f'launch-error: {error}', 'failed'
            record.update(returncode=code, status=status, wall_seconds=time.time() - record['started_at_unix'])
            entry.update(record)
            entry.pop('child_pid', None)
            if status == 'completed':
                entry['summary'] = json.loads((run_path / 'summary.json').read_text())
            atomic_json(manifest_path, manifest)
            print(f'{run_id}: {status}, {record["wall_seconds"]:.2f}s', flush=True)
            if interrupted:
                raise KeyboardInterrupt
            if status != 'completed' and not args.keep_going:
                raise RuntimeError('run failed; original attempt retained; inspect manifest/log then --resume')
        manifest['finished_at_unix'] = time.time()
        atomic_json(manifest_path, manifest)
        print(f'manifest={manifest_path}')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    plan = sub.add_parser('plan', help='write a reviewed plan only; never start a benchmark')
    plan.add_argument('--phase', choices=list(SPLIT_START), required=True)
    plan.add_argument('--preset', choices=['dev', 'target'], default='dev')
    plan.add_argument('--output', type=Path, required=True)
    plan.add_argument('--selection', type=Path)
    plan.add_argument('--observation', choices=['progress', 'minimal'], default='progress', help='minimal only for frozen evaluate; observe/tune compare all candidates with progress')
    plan.add_argument('--families', help='comma list: ' + ','.join(FAMILIES))
    plan.add_argument('--metrics', default=','.join(METRICS))
    plan.add_argument('--thresholds', help='same numeric grid for selected metric(s); units must be interpreted per metric')
    plan.add_argument('--cpus'); plan.add_argument('--numa'); plan.add_argument('--capacities')
    plan.add_argument('--threads'); plan.add_argument('--task-rate', type=float)
    plan.add_argument('--cpu-us', type=int); plan.add_argument('--duration', type=float); plan.add_argument('--warmup', type=float)
    plan.add_argument('--repeats', type=int); plan.add_argument('--seeds')
    plan.add_argument('--progress-us', type=int, help='CPU-us publication interval: target5000/dev500 by default')
    plan.add_argument('--iterations-per-us', type=int, default=64)
    plan.add_argument('--schedule-seed', type=int, default=7181)
    for action in ('dry-run', 'run'):
        run = sub.add_parser(action)
        run.add_argument('--config', type=Path, required=True)
        run.add_argument('--binary', type=Path, default=Path('build-independent-balance/independent_load'))
        if action == 'run':
            run.add_argument('--output', type=Path, required=True)
            run.add_argument('--resume', action='store_true')
            run.add_argument('--keep-going', action='store_true')
            run.add_argument('--timeout-slack', type=float, default=30)
    choice = sub.add_parser('freeze', help='record a manual metric/threshold choice using successful tune evidence')
    choice.add_argument('--manifest', type=Path, required=True)
    choice.add_argument('--metric', choices=METRICS, required=True)
    choice.add_argument('--threshold', type=float, required=True)
    choice.add_argument('--queue-threshold', type=float)
    choice.add_argument('--reason', default='Manual review of tune results; frozen before evaluation')
    choice.add_argument('--output', type=Path, required=True)
    return p


def main():
    args = parser().parse_args()
    if args.action == 'plan':
        spec = generate(args)
        write_new(args.output, spec)
        summary(spec)
        print(args.output)
    elif args.action == 'freeze':
        freeze(args)
    else:
        execute(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print('interrupted; completed runs and all attempts retained; use --resume', file=sys.stderr)
        sys.exit(130)

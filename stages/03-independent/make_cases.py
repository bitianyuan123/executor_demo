#!/usr/bin/env python3
"""Generate one independently interpretable stage-3 experiment family; never run it."""
import argparse
import json
import math
import os
from pathlib import Path

FAMILIES = ['calibration', 'burst', 'arrival', 'distribution', 'deployment',
            'scale', 'estimator', 'qps12', 'instrumentation']


def numbers(value, cast=float):
    result = [cast(x) for x in value.split(',')]
    if not result or any(not math.isfinite(x) or x <= 0 for x in result):
        raise ValueError('lists must contain positive finite numbers')
    return result


def generate(args):
    target = args.deployment == 'target'
    duration = args.duration if args.duration is not None else (300 if target else 8)
    warmup = args.warmup if args.warmup is not None else (30 if target else 2)
    repeats = args.repeats if args.repeats is not None else (7 if target else 2)
    cpu_us = args.cpu_us if args.cpu_us is not None else (100000 if target else 1000)
    threads = args.threads if args.threads is not None else (16 if target else 1)
    if duration <= 0 or warmup < 0 or warmup >= duration or repeats < 1 or cpu_us < 1 or threads < 1:
        raise ValueError('invalid duration, warmup, repeats, CPU budget or threads')
    if target:
        if not args.cpus or not args.numa or len(args.cpus.split(';')) != 6 or len(args.numa.split(',')) != 6:
            raise ValueError('target requires six explicit --cpus groups and six --numa labels')
        groups = args.cpus
        pools = 6
    else:
        allowed = sorted(os.sched_getaffinity(0))
        split = max(1, len(allowed)//2)
        masks = [allowed[:split], allowed[split:]]
        masks = [m for m in masks if m]
        groups = args.cpus or ';'.join(','.join(map(str, m)) for m in masks)
        pools = len(groups.split(';'))
    base = {'deployment': args.deployment, 'pools': pools, 'cpus': groups, 'threads': threads,
            'affinity': 'cluster', 'duration': duration, 'cpu-us': cpu_us,
            'jitter': .5, 'long-prob': 0, 'long-factor': 8, 'mean-normalization': 'exact',
            'arrival': 'regular', 'burst-placement': 'local', 'burst-min': 1, 'burst-max': 1,
            'instrumentation': 'full', 'sample-ms': 100 if target else 20,
            'max-outstanding': 100000, 'max-planned-tasks': 2000000,
            'drain-seconds': 120 if target else 30}
    if args.numa:
        base['numa'] = args.numa
    reference = args.capacity_task_rate
    if args.family == 'qps12' and (args.task_rate is not None or reference is not None or args.rates):
        raise ValueError('qps12 fixes arrivals=12 and derives task rate from width; do not pass rate controls')
    if args.family != 'calibration' and args.rates:
        raise ValueError('--rates is only used for calibration')
    if reference is not None and (not math.isfinite(reference) or reference <= 0):
        raise ValueError('capacity-task-rate must be positive and measured separately')
    if args.task_rate is not None and (not math.isfinite(args.task_rate) or args.task_rate <= 0):
        raise ValueError('task-rate must be positive')
    if args.task_rate is not None and reference is not None:
        raise ValueError('choose task-rate or capacity-task-rate, not both')
    if args.family == 'calibration':
        if not args.rates:
            if target:
                raise ValueError('target calibration requires explicit --rates after inspecting CPU quota')
            rates = [200, 800, 1600, 2400, 3200]
        else:
            rates = numbers(args.rates)
    elif reference is not None:
        rates = [reference * x for x in numbers(args.load_fractions)]
    elif args.task_rate is not None:
        rates = [args.task_rate]
    elif args.family == 'qps12':
        rates = [0]  # Rate belongs to each width and is deliberately not held constant.
    elif target:
        raise ValueError('target requires explicit --task-rate or measured --capacity-task-rate')
    else:
        rates = [1200]
    cases = []

    def add(name, rate, options, question, pair):
        opts = {'arrival-rate': rate}
        opts.update(options)
        config = dict(base, **opts)
        width = (config['burst-min'] + config['burst-max'])/2
        arrivals = rate * (duration-warmup)
        tasks = arrivals*width
        cases.append({'name': name, 'options': opts, 'comparison_group': pair,
                      'question': question, 'sample_plan': {
                          'expected_analysis_arrivals': arrivals,
                          'expected_analysis_tasks': tasks,
                          'nominal_analysis_cpu_seconds': tasks*config['cpu-us']/1e6,
                          'p99_task_tail_expected_count': tasks*.01,
                          'few_arrival_warning': arrivals < 1000,
                          'few_task_warning': tasks < 10000,
                          'reference_load_fraction': rate*width/reference if reference else None}})

    if args.family == 'qps12':
        for width in [20, 100, 400]:
            add(f'qps12-k{width}', 12, {'burst-min': width, 'burst-max': width},
                'Fixed arrivals; total CPU demand intentionally increases with width', 'qps12-demand')
    else:
        for rate in rates:
            prefix = f'r{rate:g}'
            if args.family == 'calibration':
                add(prefix, rate, {'instrumentation': 'queue'},
                    'Locate backlog growth and sustainable completion before assigning capacity', 'capacity')
            elif args.family == 'burst':
                for width in [1, 20, 100, 400]:
                    for placement in (['local'] if width == 1 else ['local', 'spread']):
                        add(f'{prefix}-k{width}-{placement}', rate/width,
                            {'burst-min': width, 'burst-max': width, 'burst-placement': placement},
                            'Equal expected task rate; time concentration and pool concentration separated', prefix)
            elif args.family == 'arrival':
                for arrival in ['regular', 'poisson']:
                    add(f'{prefix}-{arrival}', rate/20,
                        {'burst-min': 20, 'burst-max': 20, 'arrival': arrival},
                        'Equal expected rate; Poisson realized counts differ, retain that difference', prefix)
            elif args.family == 'distribution':
                for name, jitter, tail in [('fixed', 0, 0), ('uniform', .5, 0), ('tail', .5, .02)]:
                    add(f'{prefix}-{name}', rate/20,
                        {'burst-min': 20, 'burst-max': 20, 'jitter': jitter, 'long-prob': tail},
                        'Exact finite-sample equal CPU sum; service variance changes', prefix)
            elif args.family == 'deployment':
                affinities = ['none', 'numa', 'cluster'] if target or args.numa else ['none', 'cluster']
                sizes = numbers(args.thread_counts or ('8,16,64,256' if target else '1,2,8,32'), int)
                for affinity in affinities:
                    for count in sizes:
                        add(f'{prefix}-{affinity}-t{count}', rate/20,
                            {'burst-min': 20, 'burst-max': 20, 'affinity': affinity, 'threads': count},
                            'Same offered work; separate queue wait from callback wall time', prefix)
            elif args.family == 'scale':
                for scale in [.1, 1, 10]:
                    budget = max(1, round(cpu_us*scale))
                    add(f'{prefix}-cpu{budget}', rate*cpu_us/budget,
                        {'cpu-us': budget},
                        'Same nominal CPU demand; task overhead and service granularity change', prefix)
            elif args.family == 'estimator':
                for name, estimate, error in [('oracle', 0, 0), ('fixed', cpu_us, 0),
                                              ('noisy-oracle', 0, .75), ('noisy-fixed', cpu_us, .75)]:
                    add(f'{prefix}-{name}', rate/20,
                        {'burst-min': 20, 'burst-max': 20, 'long-prob': .02, 'estimate-us': estimate,
                         'estimate-jitter': error},
                        'Same work and scheduling; compare observational information, not policy gains', prefix)
            elif args.family == 'instrumentation':
                for mode in ['none', 'queue', 'counters', 'full']:
                    for sample in ([100] if mode == 'none' else [10, 100, 1000]):
                        add(f'{prefix}-{mode}-s{sample}', rate,
                            {'work-mode': 'iterations', 'iterations-per-us': args.iterations_per_us,
                             'instrumentation': mode, 'sample-ms': sample},
                            'Fixed useful loop work; observer CPU/wait effect with calibrated loop rate', prefix)
    notes = [
        'Generated plan only. Review CPU quota, masks, memory and estimated run hours before execution.',
        'dev default is functional/exploratory, not a formal p99 or sub-percent benefit estimate.',
        'Reference fractions reuse one measured workload capacity; affinity, threads or distribution changes can change capacity.',
        'Finite-sample exact means hold each run total at N*cpu-us; random Poisson N still differs.',
        'For equal-work regular burst comparisons ensure rate*duration and rate*warmup are divisible by every width; verify CSV hashes/counts.',
        'No built-in number of repetitions or task count guarantees enough power; pilot paired run variance determines duration/repetitions.',
        'Analysis window is a planned-arrival cohort; keep completions beyond its end and report rejection/cancellation.',
        'qps12 intentionally increases demand and may overload. It is not an equal-pressure fanout comparison.',
    ]
    return {'schema': 2, 'family': args.family, 'tier': 'target-candidate' if target else 'development-exploratory',
            'base': base, 'repeats': repeats, 'seed': args.seed,
            'analysis': {'warmup_seconds': warmup, 'steady_end_seconds': duration,
                         'must_check_steady_state': True, 'minimum_independent_runs_for_formal_comparison': 7},
            'reference_capacity_tasks_per_second': reference,
            'planned_offered_hours': len(cases)*repeats*duration/3600,
            'notes': notes, 'cases': cases}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--family', choices=FAMILIES, required=True)
    p.add_argument('--deployment', choices=['dev', 'target'], default='dev')
    p.add_argument('--cpus'); p.add_argument('--numa')
    p.add_argument('--task-rate', type=float)
    p.add_argument('--capacity-task-rate', type=float)
    p.add_argument('--load-fractions', default='.2,.5,.8,1.1')
    p.add_argument('--rates', help='explicit task/s calibration points')
    p.add_argument('--threads', type=int); p.add_argument('--thread-counts')
    p.add_argument('--cpu-us', type=int); p.add_argument('--iterations-per-us', type=int, default=64)
    p.add_argument('--duration', type=float); p.add_argument('--warmup', type=float)
    p.add_argument('--repeats', type=int); p.add_argument('--seed', type=int, default=420)
    p.add_argument('--output', type=Path, required=True)
    return p


def main():
    args = parser().parse_args()
    spec = generate(args)
    if args.output.exists():
        raise ValueError('output config already exists')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(spec, indent=2)+'\n')
    print(f'{len(spec["cases"])} cases; {spec["repeats"]} repeats; '
          f'{spec["planned_offered_hours"]:.2f} offered hours (excludes drain): {args.output}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Generate separate DAG experiment matrices, never infer CPU clusters from numbering."""
import argparse
import json
import math
import os
from pathlib import Path
import sys


def cpu_mask(text):
    values = set()
    for part in text.split(','):
        limits = [int(value) for value in part.split('-')]
        if len(limits) not in (1, 2) or limits[0] < 0 or limits[-1] < limits[0]:
            raise ValueError('invalid CPU mask')
        values.update(range(limits[0], limits[-1] + 1))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=('baseline', 'routing', 'shape', 'stress', 'capacity', 'production', 'continuation', 'locality'), required=True)
    parser.add_argument('--deployment', choices=('dev', 'target'), default='dev')
    parser.add_argument('--cpus', required=True, help='Verified CPU masks, one semicolon-separated mask per pool')
    parser.add_argument('--numa', required=True, help='Verified NUMA label per pool')
    parser.add_argument('--threads', default='2,16', help='Thread counts per pool, comma separated')
    parser.add_argument('--affinity', choices=('none', 'numa', 'cluster'), default='cluster', help='For routing/shape/stress; baseline always tests all three')
    parser.add_argument('--capacities', help='Optional capacity per pool; shared masks require calibration')
    parser.add_argument('--effective-cpus', type=float, help='Measured whole-executor CPU-second/second capacity, not sum of overlapping masks')
    parser.add_argument('--load-ratios', default='.25,.5,.75,.9,1.05', help='capacity family only: demand / measured executor capacity')
    parser.add_argument('--node-counts', default='20,60,200,400', help='production family: total operators, not fanout times depth')
    parser.add_argument('--qps', type=float, default=12, help='capacity/production/continuation request rate')
    parser.add_argument('--cpu-mean-us', type=float, default=100000, help='Production target mean INCLUDING the tail')
    parser.add_argument('--tail-prob', type=float, default=.02)
    parser.add_argument('--tail-min-us', type=int, default=700000)
    parser.add_argument('--tail-max-us', type=int, default=800000)
    parser.add_argument('--iterations-per-us', type=int, help='locality family requires an explicit, externally calibrated fixed-work multiplier')
    parser.add_argument('--memory-sizes-kb', default='0,256,8192', help='locality family: read-only request dataset sizes')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pools = len(args.cpus.split(';'))
    threads = [int(value) for value in args.threads.split(',')]
    if not all(threads) or any(value < 0 for value in threads) or len(args.numa.split(',')) != pools:
        parser.error('Positive threads and matching cpus/numa groups required')
    if args.capacities and len(args.capacities.split(',')) != pools:
        parser.error('capacities must match group count')
    try:
        masks = [cpu_mask(mask) for mask in args.cpus.split(';')]
        mask_union = set().union(*masks)
    except ValueError:
        parser.error('invalid CPU mask syntax')
    if sum(map(len, masks)) != len(mask_union):
        parser.error('explicit cluster masks must be disjoint; numa expansion may overlap later')
    if args.deployment == 'target' and pools != 6:
        parser.error('target deployment requires exactly six verified cluster masks')
    if not math.isfinite(args.qps) or args.qps <= 0 or not 0 <= args.tail_prob < 1:
        parser.error('positive finite qps and tail-prob in [0,1) required')
    if args.effective_cpus is not None and (not math.isfinite(args.effective_cpus) or args.effective_cpus <= 0):
        parser.error('effective-cpus must be a positive measured value')
    if args.tail_min_us < 2 or args.tail_max_us < args.tail_min_us or args.tail_max_us > 10000000:
        parser.error('tail range must be ordered and within 2..10000000 us')
    if args.family == 'capacity' and args.effective_cpus is None:
        parser.error('capacity family requires --effective-cpus from calibration')
    if args.family == 'locality' and (args.affinity == 'none' or not args.iterations_per_us or args.iterations_per_us < 1):
        parser.error('locality requires bound affinity and explicit positive --iterations-per-us')
    allowed = set(os.sched_getaffinity(0))
    if mask_union != allowed:
        print('WARNING: explicit CPU union differs from this process allowed CPUs. none inherits ALL allowed CPUs, '
              'while cluster/numa use the explicit union. Before comparing affinity modes, run the matrix under '
              'taskset with that exact union on the target host; otherwise total CPU capacity is confounded. '
              f'explicit={sorted(mask_union)} allowed={sorted(allowed)}', file=sys.stderr)
    base = ['--pools', str(pools), '--cpus', args.cpus, '--numa', args.numa, '--graph', 'waves',
            '--wave-widths', '40,10,10', '--wave-depths', '1,1,1', '--jitter', '0', '--long-prob', '0',
            '--rpc-prob', '0', '--join-us', '100', '--metric', 'queue']
    if args.capacities:
        base += ['--capacities', args.capacities]
    cases, budgets = [], []
    def add(name, count, extra, budget=None):
        cases.append({'name': name, 'args': [*base, '--threads', str(count), '--affinity', args.affinity, *extra]})
        if budget:
            budgets.append({'name': name, **budget})
    for count in threads:
        if args.family == 'baseline':
            for affinity in ('none', 'numa', 'cluster'):
                add(f'baseline-{affinity}-t{count}', count, ['--affinity', affinity, '--policy', 'home'])
        elif args.family == 'routing':
            for metric in ('queue', 'pending', 'work'):
                for policy in ('home', 'borrow', 'sticky', 'fanout', 'least-load'):
                    boundaries = ('initial', 'all') if policy == 'fanout' else ('all',)
                    for boundary in boundaries:
                        add(f'route-{metric}-{policy}-{boundary}-t{count}', count,
                            ['--metric', metric, '--policy', policy, '--balance-forks', boundary])
        elif args.family == 'shape':
            # Each case has 60 operators and 3 joins; work RNG depends on operator ID.
            for name, widths, depths in [('front', '40,10,10', '1,1,1'), ('even', '20,20,20', '1,1,1'),
                                         ('late', '10,10,40', '1,1,1'), ('deep', '5,5,5', '4,4,4')]:
                for policy, boundary in [('home', 'all'), ('borrow', 'all'), ('fanout', 'initial'), ('fanout', 'all')]:
                    add(f'shape-{name}-{policy}-{boundary}-t{count}', count,
                        ['--wave-widths', widths, '--wave-depths', depths, '--policy', policy, '--balance-forks', boundary])
        elif args.family == 'stress':
            # Change one mechanism at a time. Defaults have no tail or RPC.
            for name, extra in [('control', []), ('tail', ['--long-prob', '.02', '--long-factor', '8']),
                                ('jitter', ['--jitter', '.75']), ('rpc', ['--rpc-prob', '.35', '--rpc-delay-us', '20000']),
                                ('fixed-estimate', ['--estimate-us', '1000'])]:
                for policy in ('home', 'borrow', 'sticky'):
                    add(f'stress-{name}-{policy}-t{count}', count, ['--policy', policy, '--metric', 'work', *extra])
        elif args.family == 'locality':
            try:
                sizes = [int(value) for value in args.memory_sizes_kb.split(',')]
            except ValueError:
                parser.error('memory sizes must be integers')
            for memory in sizes:
                if not 0 <= memory <= 1048576:
                    parser.error('memory size outside supported 0..1048576 KiB')
                for policy in ('home', 'borrow', 'sticky'):
                    for scope in ('all', 'preferred-numa'):
                        add(f'locality-m{memory}-{policy}-{scope}-t{count}', count,
                            ['--policy', policy, '--candidate-scope', scope, '--memory-kb', str(memory),
                             '--work-mode', 'iterations', '--iterations-per-us', str(args.iterations_per_us),
                             '--arrival', 'poisson', '--qps', str(args.qps), '--cross-numa-penalty-us', '0'])
        elif args.family == 'continuation':
            # Identical graph/work across policies; sticky vs fanout/all changes
            # ordinary successor/RPC rebalancing while retaining parent affinity.
            for mechanism, rpc in [('chain', '0'), ('rpc', '.35')]:
                for policy, boundary in [('home', 'all'), ('borrow', 'all'), ('sticky', 'all'),
                                         ('fanout', 'initial'), ('fanout', 'all')]:
                    add(f'continuation-{mechanism}-{policy}-{boundary}-t{count}', count,
                        ['--wave-widths', '20,10', '--wave-depths', '2,2', '--policy', policy,
                         '--balance-forks', boundary, '--arrival', 'poisson', '--qps', str(args.qps),
                         '--rpc-prob', rpc, '--rpc-delay-us', '20000', '--rpc-jitter', '.75'])
        elif args.family == 'capacity':
            try:
                ratios = [float(value) for value in args.load_ratios.split(',')]
            except ValueError:
                parser.error('invalid load-ratios')
            for ratio in ratios:
                if not math.isfinite(ratio) or ratio <= 0:
                    parser.error('load ratios must be positive and finite')
                # Keep 12 QPS and 60 operators; scale per-op CPU only in this family.
                mean = (ratio * args.effective_cpus * 1e6 / args.qps - 300) / 60
                if not 2 <= mean <= 10000000:
                    parser.error('capacity-derived CPU mean outside supported 2..10000000 us')
                cpu = round(mean)
                demand = args.qps * (60 * cpu + 300) / 1e6
                for arrival in ('regular', 'poisson'):
                    for policy in ('home', 'borrow', 'sticky', 'fanout'):
                        add(f'capacity-rho{ratio:g}-{arrival}-{policy}-t{count}', count,
                            ['--cpu-us', str(cpu), '--qps', str(args.qps), '--arrival', arrival,
                             '--policy', policy, '--balance-forks', 'all'],
                            {'operators': 60, 'joins': 3, 'mean_cpu_us': cpu, 'qps': args.qps,
                             'expected_cpu_demand': demand, 'requested_rho': ratio,
                             'rho': demand / args.effective_cpus, 'regime': 'overload' if ratio > 1 else 'capacity_sweep'})
        elif args.family == 'production':
            mean_tail = (args.tail_min_us + args.tail_max_us) / 2
            body = (args.cpu_mean_us - args.tail_prob * mean_tail) / (1 - args.tail_prob)
            if not math.isfinite(body) or not 2 <= body <= 10000000 / 1.2:
                parser.error('requested mean is incompatible with tail; non-tail CPU must remain positive and bounded')
            body = round(body)
            expectation = (1 - args.tail_prob) * body + args.tail_prob * mean_tail
            try:
                counts = [int(value) for value in args.node_counts.split(',')]
            except ValueError:
                parser.error('node-counts must be integers')
            for nodes in counts:
                if not 20 <= nodes <= 400:
                    parser.error('production node-counts must be within the observed 20..400 range')
                front = max(1, 3 * nodes // 4)
                for distribution in ('constant', 'tail'):
                    for policy in ('home', 'borrow', 'sticky', 'fanout'):
                        extra = ['--wave-widths', f'{front},{nodes-front}', '--wave-depths', '1,1',
                                 '--cpu-us', str(round(args.cpu_mean_us) if distribution == 'constant' else body),
                                 '--jitter', '0' if distribution == 'constant' else '.2',
                                 '--long-prob', '0' if distribution == 'constant' else str(args.tail_prob),
                                 '--long-min-us', str(args.tail_min_us), '--long-max-us', str(args.tail_max_us),
                                 '--qps', str(args.qps), '--arrival', 'poisson', '--policy', policy,
                                 '--balance-forks', 'all']
                        actual_mean = round(args.cpu_mean_us) if distribution == 'constant' else expectation
                        actual_demand = args.qps * (nodes * actual_mean + 200) / 1e6
                        add(f'production-n{nodes}-{distribution}-{policy}-t{count}', count, extra,
                            {'operators': nodes, 'joins': 2, 'initial_fanout': front, 'mean_cpu_us': actual_mean,
                             'qps': args.qps, 'expected_cpu_demand': actual_demand,
                             'rho': actual_demand / args.effective_cpus if args.effective_cpus else None,
                             'regime': 'fixed_business_demand_not_capacity_normalized',
                             'mean_matched_only': distribution == 'tail'})
    budget_path = args.output.with_suffix('.budget.json')
    if args.output.exists() or budget_path.exists():
        parser.error('Refusing to overwrite output; choose a fresh case file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + '\n')
    budget_path.write_text(json.dumps({'family': args.family, 'deployment': args.deployment,
        'effective_executor_cpu_capacity': args.effective_cpus,
        'notes': ['CPU demand assumes cpu mode; iterations mode requires empirical calibration.',
                  'Global rho does not describe per-cluster transient saturation.',
                  'Constant/tail controls have equal theoretical means, not equal realized per-request work.',
                  'Runner --extra overrides case values; verify final manifest before interpreting this plan.'],
        'cases': budgets}, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(cases)} cases written to {args.output}')


if __name__ == '__main__':
    main()

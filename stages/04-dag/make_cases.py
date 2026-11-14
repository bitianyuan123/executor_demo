#!/usr/bin/env python3
"""Generate separate DAG experiment matrices, never infer CPU clusters from numbering."""
import argparse
import json
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
    parser.add_argument('--family', choices=('baseline', 'routing', 'shape', 'stress'), required=True)
    parser.add_argument('--cpus', required=True, help='Verified CPU masks, one semicolon-separated mask per pool')
    parser.add_argument('--numa', required=True, help='Verified NUMA label per pool')
    parser.add_argument('--threads', default='2,16', help='Thread counts per pool, comma separated')
    parser.add_argument('--affinity', choices=('none', 'numa', 'cluster'), default='cluster', help='For routing/shape/stress; baseline always tests all three')
    parser.add_argument('--capacities', help='Optional capacity per pool; shared masks require calibration')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    pools = len(args.cpus.split(';'))
    threads = [int(value) for value in args.threads.split(',')]
    if not all(threads) or any(value < 0 for value in threads) or len(args.numa.split(',')) != pools:
        parser.error('Positive threads and matching cpus/numa groups required')
    if args.capacities and len(args.capacities.split(',')) != pools:
        parser.error('capacities must match group count')
    try:
        mask_union = set().union(*(cpu_mask(mask) for mask in args.cpus.split(';')))
    except ValueError:
        parser.error('invalid CPU mask syntax')
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
    cases = []
    def add(name, count, extra):
        cases.append({'name': name, 'args': [*base, '--threads', str(count), '--affinity', args.affinity, *extra]})
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
        else:
            # Change one mechanism at a time. Defaults have no tail or RPC.
            for name, extra in [('control', []), ('tail', ['--long-prob', '.02', '--long-factor', '8']),
                                ('jitter', ['--jitter', '.75']), ('rpc', ['--rpc-prob', '.35', '--rpc-delay-us', '20000']),
                                ('fixed-estimate', ['--estimate-us', '1000'])]:
                for policy in ('home', 'borrow', 'sticky'):
                    add(f'stress-{name}-{policy}-t{count}', count, ['--policy', policy, '--metric', 'work', *extra])
    if args.output.exists():
        parser.error('Refusing to overwrite output; choose a fresh case file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(cases)} cases written to {args.output}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Snapshot live Linux worker TIDs; pool labels require explicit name rules."""
import argparse
from collections import Counter
import csv
import pathlib
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--pool', action='append', default=[], help='POOL=REGEX matched against comm; repeat for each pool')
    parser.add_argument('--output', help='tid,pool,comm,starttime CSV; requires explicit --pool rules')
    parser.add_argument('--proc-root', default='/proc')
    args = parser.parse_args()
    if args.pid <= 0 or (args.output and not args.pool):
        parser.error('positive PID required; mapping output requires explicit pool rules')
    rules = []
    for rule in args.pool:
        if '=' not in rule:
            parser.error('pool rule must be POOL=REGEX')
        pool, regex = rule.split('=', 1)
        if not pool or not regex:
            parser.error('empty pool or pattern')
        rules.append((pool, re.compile(regex)))
    rows, names, missing = [], Counter(), 0
    directory = pathlib.Path(args.proc_root) / str(args.pid) / 'task'
    for path in sorted(directory.iterdir(), key=lambda p: int(p.name)):
        try:
            comm = (path / 'comm').read_text().rstrip('\n')
            stat = (path / 'stat').read_text()
            closing = stat.rfind(') ')
            opening = stat.find(' (')
            if opening <= 0 or closing <= opening or int(stat[:opening]) != int(path.name):
                raise ValueError('invalid stat identity')
            birth = stat[closing + 2:].split()[19]
            if not birth.isascii() or not birth.isdecimal():
                raise ValueError('invalid stat starttime')
        except (OSError, IndexError, ValueError):
            missing += 1
            continue
        names[comm] += 1
        matches = [pool for pool, regex in rules if regex.search(comm)]
        if len(matches) > 1:
            parser.error(f'ambiguous pool rules for TID={path.name} comm={comm!r}')
        if matches:
            rows.append({'tid': int(path.name), 'pool': matches[0], 'comm': comm, 'starttime': birth})
    print(f'pid={args.pid} live_threads={sum(names.values())} missing_or_invalid_during_snapshot={missing} mapped={len(rows)}')
    for name, count in names.most_common(100):
        print(f'comm={name!r} threads={count}')
    if len(names) > 100:
        print(f'additional_comm_names_omitted={len(names)-100}')
    if args.output:
        if not rows:
            parser.error('no threads matched; refusing empty map')
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=('tid', 'pool', 'comm', 'starttime'))
            writer.writeheader(); writer.writerows(rows)
        print('pool_counts=' + str(dict(Counter(row['pool'] for row in rows))))
        print(f'map={output}; name-derived snapshot, verify against executor registration and trace lifetime')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Join 4024 report exports to the existing 36 runs; never count them as new runs."""
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
SOURCE_SHA = '702778049dc293ee6f2d2cc96f0a722a8db260d8a7247b49f9bf579192d781b9'
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise ValueError(message)


def cells(line):
    return [c.strip() for c in line.strip('|').split('|')]


def scale(path):
    match = re.search(r'shape-(46875|3m)-fixed-01', path)
    check(match is not None, 'unknown source directory')
    return 46875 if match[1] == '46875' else 3000000


def parse_log(path):
    raw = path.read_bytes()
    check(hashlib.sha256(raw).hexdigest() == SOURCE_SHA, 'wrong 4024 input')
    lines = raw.decode().splitlines()
    reports, flows = {}, {}
    current, flow = None, None
    for number, line in enumerate(lines, 1):
        if line.startswith('| seed-'):
            c = cells(line)
            completed, offered = map(int, c[2].split('/'))
            migrated, tasks = map(int, c[8].split('/'))
            cpu, wall = map(float, c[7].split('/'))
            current = dict(name=c[0], source_line=number, completed=completed,
                offered=offered, mean_ms=float(c[3]), p95_ms=float(c[4]), p99_ms=float(c[5]),
                wait_p99_ms=float(c[6]), operator_cpu_mean_ms=cpu, operator_wall_mean_ms=wall,
                migrations=migrated, tasks=tasks, cross_numa=int(c[9]))
        elif 'matrix suite=' in line:
            check(current is not None, 'provenance without cohort')
            check('research_target=one_pool_per_cluster' in line, 'wrong target')
            key = (scale(line), current['name'])
            check(key not in reports, 'duplicate report')
            reports[key] = current
            current['provenance_line'] = number
        elif line.strip().startswith('seed=') and 'threads/pool=' in line and current:
            check('pool_count=6, affinity=cluster, threads/pool=16' in line, 'wrong resources')
            check('whole completed/rejected-or-failed=240/0' in line, 'failed request')
        elif 'work_signature_sha256_prefix=' in line and current:
            current['signature_line'] = number
            current['signatures'] = dict(re.findall(r'(work_signature_sha256_prefix|arrival_hash|checksum)=([^, ]+)', line))
        elif 'fixed_work_sha256_prefix=' in line and current:
            current['fixed_work_prefix'] = re.search(r'fixed_work_sha256_prefix=([^; ]+)', line)[1]
        elif 'WHOLE RUN (not cohort):' in line and current:
            m = re.search(r'process CPU s=([\d.]+), process CPU/completed-request ms=([\d.]+), peak_inflight=(\d+)', line)
            check(m is not None, 'bad whole summary')
            current['whole_reprint'] = dict(source_line=number, process_cpu_s=float(m[1]),
                process_cpu_request_ms=float(m[2]), request_peak=int(m[3]))
        elif line.startswith('RUN '):
            name = Path(line.split(': policy=', 1)[0][4:]).name
            key = (scale(line), name)
            check(key not in flows, 'duplicate flow report')
            flow = flows[key] = {}
        elif flow is not None and re.match(r'^\| (init|first-wave stage0|later-wave stage0|ordinary successors|join) \|', line):
            c = cells(line)
            check(c[0] not in flow, 'duplicate role')
            flow[c[0]] = dict(source_line=number, n=int(c[1]), migrated=int(c[2]), returned_home=int(c[3]),
                wait_mean_ms=float(c[4]), cpu_mean_ms=float(c[5]), wall_mean_ms=float(c[6]), reasons=c[7])
    check(len(reports) == len(flows) == 24, 'missing report or flow')
    return reports, flows, len(lines)


def build(path):
    reports, flows, line_count = parse_log(path)
    previous_path = HERE.parent / '4023/evidence.json'
    previous = json.loads(previous_path.read_text())
    check(previous['source_log_sha256'] == '790427ed9172febde1c9cdcc538783dd789e5347bf298cec0ca7b523206ffd1c', 'wrong previous evidence')
    runs = copy.deepcopy(previous['runs'])
    indexed = {(int(r['options']['--iterations']), r['name']): r for r in runs}
    check(len(indexed) == 36, 'wrong original run count')
    for key, r in indexed.items():
        r['whole_source_log'] = '4023.log'
        r['cohort_source_log'] = '4023.log' if key[0] == 375000 else '4024.log'
        if key[0] != 375000:
            r['cohort'], r['roles'] = reports[key], flows[key]
            reprint = r['cohort']['whole_reprint']
            check(abs(reprint['process_cpu_s'] - r['whole']['process_cpu_s']) < 1e-9, 'whole CPU changed')
            check(reprint['request_peak'] == r['whole']['request_peak'], 'whole peak changed')
            check(abs(reprint['process_cpu_request_ms'] - r['whole']['process_cpu_per_completed_request_ms']) <= .000501, 'CPU/request rounding')
        c, roles = r['cohort'], r['roles']
        check(c['completed'] == c['offered'] == 192 and c['tasks'] == 32832, 'wrong cohort')
        check(c['cross_numa'] == 0, 'unexpected cross NUMA')
        check(set(c['signatures']) == {'work_signature_sha256_prefix', 'arrival_hash', 'checksum'}, 'missing fingerprints')
        expected_counts = {'init':192, 'first-wave stage0':15360, 'later-wave stage0':768, 'ordinary successors':16128, 'join':384}
        check(set(roles) == set(expected_counts), 'missing roles')
        for role, values in roles.items():
            check(values['n'] == expected_counts[role], 'role count')
            check(0 <= values['returned_home'] <= values['migrated'] <= values['n'], 'bad routing count')
            check(sum(int(v) for v in re.findall(r':(\d+)', values['reasons'])) == values['n'], 'reason count')
        check(sum(v['migrated'] for v in roles.values()) == c['migrations'], 'route conservation')
        r['derived'] = dict(migration_percent=100*c['migrations']/c['tasks'],
            successor_migration_percent=100*roles['ordinary successors']['migrated']/roles['ordinary successors']['n'])
    pairs = []
    for (iterations, name), r in indexed.items():
        opt, c = r['options'], r['cohort']
        if opt['--policy'] == 'home':
            continue
        home_name = re.sub(r'-(queue|outstanding)$', '-home', name)
        h = indexed[(iterations, home_name)]
        hc = h['cohort']
        check(c['signatures'] == hc['signatures'] and c['fixed_work_prefix'] == hc['fixed_work_prefix'], 'paired fingerprints differ')
        check(r['whole']['arrival_hash'] == h['whole']['arrival_hash'], 'arrival mismatch')
        pairs.append(dict(iterations=iterations, shape='front80' if opt['--depths']=='2,2' else 'tail4',
            seed=int(opt['--seed']), policy=opt['--policy'], candidate=name, home=home_name,
            cohort_mean_change_percent=100*(c['mean_ms']/hc['mean_ms']-1),
            cohort_p99_change_percent=100*(c['p99_ms']/hc['p99_ms']-1),
            whole_cpu_change_percent=100*(r['whole']['process_cpu_s']/h['whole']['process_cpu_s']-1)))
    check(len(pairs) == 24, 'wrong pair count')
    return dict(schema='review-4024-v1', source_log_sha256=SOURCE_SHA, source_lines=line_count,
        previous_evidence_sha256=hashlib.sha256(previous_path.read_bytes()).hexdigest(),
        scope='Same 36 executions as 4023; 24 new offline cohort/role exports, no new workload runs or CPU windows.',
        runs=runs, pairs=pairs, validation_checks=CHECKS,
        limits=['Terminal exports only; target CSV and build identifiers not available locally.',
            'Latency and routing use 192 planned arrivals in [2,18) seconds; process CPU uses all 240 completed requests.',
            'Two seed values are descriptive, not a confidence interval. No average of p99 across seeds.',
            'Shapes are uncalibrated hypotheses. No new historical production trace report or joint resource attribution.'])


def tables(evidence):
    index = {(int(r['options']['--iterations']), r['name']): r for r in evidence['runs']}
    absolute = ['| 每算子迭代 / 图 / 策略 | 请求mean ms | 请求p99 ms | 整轮CPU/请求 ms | callback改投率 |',
                '|---|---:|---:|---:|---:|']
    ranges = ['| 每算子迭代 / 图 | Q均值下降 | O均值下降 | Q CPU增加 | O CPU增加 |', '|---|---:|---:|---:|---:|']
    for iterations in (46875, 375000, 3000000):
        for shape in ('front80', 'tail4'):
            row = [f'{iterations} / {shape}']
            for field in ('cohort_mean_change_percent', 'whole_cpu_change_percent'):
                for policy in ('queue', 'outstanding'):
                    values = [p[field] * (-1 if field.startswith('cohort') else 1) for p in evidence['pairs']
                              if p['iterations']==iterations and p['shape']==shape and p['policy']==policy]
                    row.append(f'{min(values):.2f}%–{max(values):.2f}%')
            ranges.append('| ' + ' | '.join(row) + ' |')
            for policy, label in (('home', 'home'), ('queue', 'Q'), ('outstanding', 'O')):
                rs = [index[(iterations, f'seed-{seed}-{shape}-{policy}')] for seed in (421,422)]
                values = [' / '.join(f'{r["cohort"][field]:.3f}' for r in rs) for field in ('mean_ms', 'p99_ms')]
                values += [' / '.join(f'{r["whole"]["process_cpu_per_completed_request_ms"]:.3f}' for r in rs),
                           ' / '.join(f'{r["derived"]["migration_percent"]:.2f}%' for r in rs)]
                absolute.append('| ' + ' | '.join([f'{iterations} / {shape} / {label}']+values) + ' |')
    return '\n'.join(ranges) + '\n\n斜杠顺序为seed421 / seed422；不对p99取平均。\n\n' + '\n'.join(absolute) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-log', type=Path, required=True)
    args = parser.parse_args()
    evidence = build(args.source_log)
    (HERE/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    with (HERE/'paired_comparisons.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(evidence['pairs'][0]), lineterminator='\n')
        writer.writeheader(); writer.writerows(evidence['pairs'])
    (HERE/'tables.md').write_text(tables(evidence))
    print(f'PASS {CHECKS} checks: 24 exports joined; 36 existing runs, 24 paired comparisons, 0 new executions.')
    print(tables(evidence))


if __name__ == '__main__':
    main()

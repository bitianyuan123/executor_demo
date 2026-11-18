#!/usr/bin/env python3
"""Reconcile 4023 terminal evidence; does not execute a workload or read raw traces."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shlex

HERE = Path(__file__).resolve().parent
EXPECTED_SHA = '790427ed9172febde1c9cdcc538783dd789e5347bf298cec0ca7b523206ffd1c'
NAMES = ['request-cpu-3m-home', 'request-cpu-375k-home', 'request-cpu-375k-outstanding']
CHECKS = 0


def check(ok, message):
    global CHECKS
    CHECKS += 1
    if not ok:
        raise ValueError(message)


def close(a, b, tolerance=1e-7):
    return math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)


def spans(rows, predicate):
    result = []
    for row in rows:
        if predicate(row):
            a, b = row['relative_start_ms'], row['relative_end_ms']
            if result and close(result[-1][1], a):
                result[-1][1] = b
            else:
                result.append([a, b])
    return result


def extract(path):
    raw = path.read_bytes()
    check(hashlib.sha256(raw).hexdigest() == EXPECTED_SHA, 'wrong input log')
    lines = raw.decode().splitlines()
    profiles, windows, runs, cohorts, flows = [], [], [], {}, {}
    current, header, current_run, flow_name, cohort_name = None, None, None, None, None
    for lineno, line in enumerate(lines, 1):
        if line.startswith('REQUEST_SCHED_PROFILE '):
            current = {'label': NAMES[len(profiles)], 'summary_line': lineno,
                       'summary': json.loads(line.split(' ', 1)[1]), 'rows': []}
            profiles.append(current)
        elif line.startswith('REQUEST_CPU_WINDOWS_BEGIN '):
            check(current is not None and header is None, 'unexpected window header')
            header = line.split(' ', 1)[1].split(',')
        elif line == 'REQUEST_CPU_WINDOWS_END':
            check(header is not None, 'unmatched end')
            current['window_end_line'] = lineno
            header = None
        elif header is not None:
            cells = next(csv.reader([line]))
            check(len(cells) == len(header), f'bad CSV row {lineno}')
            row = dict(zip(header, cells))
            for key in row:
                if key == 'pool': continue
                row[key] = int(row[key]) if key in ('bin', 'cluster_window_fully_bracketed') else float(row[key])
            row['source_line'] = lineno
            current['rows'].append(row)
            windows.append({'profile': current['label'], **row})
        elif re.match(r'^\[\d+/12\] ', line):
            words = shlex.split(line)[2:]
            options = dict(zip(words[::2], words[1::2]))
            check(len(words) % 2 == 0, 'invalid command')
            current_run = {'command_line': lineno, 'options': options,
                           'name': Path(options['--output']).name}
        elif line.startswith('  completed='):
            check(current_run is not None, 'summary without command')
            fields = dict(token.split('=', 1) for token in line.split())
            integers = ('completed', 'migrations', 'request_peak', 'arrival_hash', 'fixed_iterations')
            current_run['whole'] = {k: int(v) if k in integers else float(v) for k, v in fields.items()}
            current_run['summary_line'] = lineno
            runs.append(current_run)
            current_run = None
        elif line.startswith('| seed-'):
            cells = [c.strip() for c in line.strip('|').split('|')]
            cohort_name = cells[0]
            completed, offered = map(int, cells[2].split('/'))
            migrated, tasks = map(int, cells[8].split('/'))
            cpu, wall = map(float, cells[7].split('/'))
            cohorts[cohort_name] = dict(source_line=lineno, completed=completed, offered=offered,
                mean_ms=float(cells[3]), p95_ms=float(cells[4]), p99_ms=float(cells[5]),
                wait_p99_ms=float(cells[6]), operator_cpu_mean_ms=cpu, operator_wall_mean_ms=wall,
                migrations=migrated, tasks=tasks, cross_numa=int(cells[9]))
        elif 'work_signature_sha256_prefix=' in line and cohort_name:
            cohorts[cohort_name]['signature_line'] = lineno
            cohorts[cohort_name]['signatures'] = dict(re.findall(r'(work_signature_sha256_prefix|arrival_hash|checksum)=([^, ]+)', line))
        elif 'fixed_work_sha256_prefix=' in line and cohort_name:
            cohorts[cohort_name]['fixed_work_prefix'] = re.search(r'fixed_work_sha256_prefix=([^; ]+)', line)[1]
        elif line.startswith('RUN '):
            flow_name = Path(line.split(': policy=', 1)[0][4:]).name
            flows[flow_name] = {}
        elif flow_name and re.match(r'^\| (init|first-wave stage0|later-wave stage0|ordinary successors|join) \|', line):
            cells = [c.strip() for c in line.strip('|').split('|')]
            flows[flow_name][cells[0]] = dict(source_line=lineno, n=int(cells[1]), migrated=int(cells[2]),
                returned_home=int(cells[3]), wait_mean_ms=float(cells[4]), cpu_mean_ms=float(cells[5]),
                wall_mean_ms=float(cells[6]), reasons=cells[7])
    check(header is None and len(profiles) == 3, 'incomplete profile blocks')
    for profile in profiles:
        rows, summary = profile.pop('rows'), profile['summary']
        totals = [r for r in rows if r['pool'] == 'all']
        check(len(rows) == 7 * len(totals), 'missing group rows')
        check(summary['request'] == 6 and summary['bin_ms'] == 1, 'unexpected profile contract')
        check(all(summary['group_full_extent_bracketed'].values()), 'unbracketed group')
        for idx, total in enumerate(totals):
            group = [r for r in rows if r['bin'] == idx and r['pool'] != 'all']
            check(len(group) == 6 and {r['pool'] for r in group} == set('012345'), 'group mismatch')
            start, end = float(idx), min(idx+1., summary['request_profile_span_ms'])
            for row in group + [total]:
                check(close(row['relative_start_ms'], start) and close(row['relative_end_ms'], end), 'window alignment')
                capacity = 96 if row['pool'] == 'all' else 16
                check(row['cluster_window_fully_bracketed'] == 1, 'missing window extent')
                check(close(row['request_scheduled_cpu_equivalent'] * (end-start), row['request_scheduled_cpu_ms']), 'area/rate mismatch')
                check(close(row['request_cluster_util_percent'], row['request_scheduled_cpu_equivalent'] / capacity * 100), 'util denominator')
                check(row['cluster_nonidle_residence_percent'] + 1e-7 >= row['request_cluster_util_percent'], 'request exceeds all nonidle')
            for key in ('request_scheduled_cpu_ms', 'request_scheduled_cpu_equivalent', 'request_submitted_not_started_mean', 'request_callback_wall_occupancy_mean'):
                check(close(sum(g[key] for g in group), total[key]), 'pool sum '+key)
            check(close(sum(g['cluster_nonidle_residence_percent'] for g in group)/6, total['cluster_nonidle_residence_percent']), 'nonidle denominator')
        area = sum(r['request_scheduled_cpu_ms'] for r in totals)
        check(close(area, summary['scheduled_callback_residence_ms']), 'profile CPU conservation')
        cumulative, crossings = 0., {}
        for row in totals:
            cumulative += row['request_scheduled_cpu_ms']
            for pct in (50, 90, 99):
                if str(pct) not in crossings and cumulative >= area*pct/100:
                    crossings[str(pct)] = row['relative_end_ms']
        check(crossings == summary['service_cumulative_percent_window_end_ms'], 'cumulative crossings')
        for threshold in (8, 16, 32, 48):
            segments = spans(totals, lambda r: r['request_scheduled_cpu_equivalent'] > threshold)
            lengths = [b-a for a,b in segments]
            reported = summary['above_cpu_equivalent'][str(threshold)]
            check(reported['segments'] == len(lengths) and close(reported['total_ms'], sum(lengths)) and close(reported['longest_binned_segment_ms'], max(lengths, default=0)), 'high segments')
        group_areas = {str(g): sum(r['request_scheduled_cpu_ms'] for r in rows if r['pool'] == str(g)) for g in range(6)}
        span = summary['request_profile_span_ms']
        outside = sum(r['cluster_nonidle_residence_percent']/100*96*(r['relative_end_ms']-r['relative_start_ms'])-r['request_scheduled_cpu_ms'] for r in totals)
        profile['derived'] = dict(window_rows=len(rows), bins=len(totals), group_request_cpu_ms=group_areas,
            request_cpu_equiv_peak=max(r['request_scheduled_cpu_equivalent'] for r in totals),
            request_cpu_equiv_mean=area/span, residence_minus_clock_percent=100*(area/summary['measured_callback_cpu_ms']-1),
            selected_cpus_nonidle_outside_request_callbacks_ms=outside,
            submitted_not_started_peak_bin_mean=max(r['request_submitted_not_started_mean'] for r in totals),
            home_at_least_95pct_intervals=spans([r for r in rows if r['pool']=='0'], lambda r:r['request_cluster_util_percent']>=95),
            above8_intervals=spans(totals, lambda r:r['request_scheduled_cpu_equivalent']>8))
    check(len(runs) == 36 and len(cohorts) == 12 and len(flows) == 12, 'wrong run/report counts')
    check(len({r['options']['--output'] for r in runs}) == 36, 'duplicate runs')
    indexed = {(int(r['options']['--iterations']), r['name']):r for r in runs}
    pairs = []
    for r in runs:
        opt, w = r['options'], r['whole']
        check(w['completed'] == 240, 'incomplete run')
        check(w['fixed_iterations'] == 240*168*int(opt['--iterations']), 'iteration total')
        check(opt['--threads']=='16' and opt['--affinity']=='cluster' and opt['--qps']=='12' and opt['--arrival']=='regular' and opt['--jitter-percent']=='0' and opt['--long-every']=='0', 'shape contract')
        check(opt['--trace']=='1' and opt['--cross-numa']=='0' and opt['--widths']=='80,4' and opt['--depths'] in ('2,2','1,22'), 'shape config')
        w['process_cpu_per_completed_request_ms'] = w['process_cpu_s']*1000/w['completed']
        w['migration_percent'] = 100*w['migrations']/(240*171)
        if int(opt['--iterations']) == 375000:
            r['cohort'] = cohorts[r['name']]
            check(r['cohort']['completed'] == r['cohort']['offered'] == 192, 'cohort completeness')
            r['roles'] = flows[r['name']]
            check(sum(v['n'] for v in r['roles'].values()) == r['cohort']['tasks'] == 192*171, 'role conservation')
            check(sum(v['migrated'] for v in r['roles'].values()) == r['cohort']['migrations'], 'route conservation')
        if opt['--policy']=='home': continue
        home_name = re.sub(r'-(queue|outstanding)$', '-home', r['name'])
        h = indexed[(int(opt['--iterations']), home_name)]
        check(w['arrival_hash']==h['whole']['arrival_hash'] and w['fixed_iterations']==h['whole']['fixed_iterations'], 'printed work/arrival summary mismatch')
        pair = dict(iterations=int(opt['--iterations']), shape='front80' if opt['--depths']=='2,2' else 'tail4', seed=int(opt['--seed']), policy=opt['--policy'],
            candidate=r['name'], home=home_name, scope='whole_240_rounded_console',
            mean_change_percent=100*(w['mean_ms']/h['whole']['mean_ms']-1),
            p99_change_percent=100*(w['p99_ms']/h['whole']['p99_ms']-1),
            cpu_change_percent=100*(w['process_cpu_s']/h['whole']['process_cpu_s']-1))
        if int(opt['--iterations'])==375000:
            c, hc = cohorts[r['name']], cohorts[home_name]
            check(c['signatures']==hc['signatures'] and c['fixed_work_prefix']==hc['fixed_work_prefix'], 'cohort pairing fingerprint')
            pair['cohort_192'] = dict(mean_change_percent=100*(c['mean_ms']/hc['mean_ms']-1), p99_change_percent=100*(c['p99_ms']/hc['p99_ms']-1))
        pairs.append(pair)
    return {'schema':'review-4023-v1', 'source_log_sha256': EXPECTED_SHA, 'source_lines':len(lines),
        'source_scope':'Terminal exports; raw target traces/CSV unavailable locally; no new production trace evidence.',
        'profiles':profiles, 'runs':runs, 'pairs':pairs, 'validation_checks':CHECKS,
        'limits':['Profiles are explicit request6 samples, not representative or p99 tests.',
        'Profile jitter/heavy-operator/24-request contract inherited from prior capture design; full metadata not reprinted in4023. Matrix commands directly printed, uniform fixed work. Do not pair them.',
        'Matrix provenance lacks build/git identifiers in returned text. Printed commands and source lines retained.',
        'Only375k includes cohort and work fingerprints; other scales are provisional whole-run descriptions.',
        'No new historical trace temporal report; shapes remain uncalibrated hypotheses.']}, windows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-log', type=Path, required=True)
    args=parser.parse_args()
    evidence, windows=extract(args.source_log)
    (HERE/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    with (HERE/'request_cpu_windows.csv').open('w', newline='') as stream:
        writer=csv.DictWriter(stream, fieldnames=list(windows[0]), lineterminator='\n'); writer.writeheader(); writer.writerows(windows)
    print(f'PASS {CHECKS} checks; 3 profiles, {len(windows)} CPU rows; 36 whole runs, 12 cohort+flow reports, 24 within-shape pairs')
    for p in evidence['profiles']: print(p['label'], json.dumps(p['derived']))
    for p in evidence['pairs']:
        if p['seed']==421: print(p['iterations'], p['shape'], p['policy'], 'mean/cpu/p99 %',*(round(p[k],3) for k in ('mean_change_percent','cpu_change_percent','p99_change_percent')))


if __name__=='__main__': main()

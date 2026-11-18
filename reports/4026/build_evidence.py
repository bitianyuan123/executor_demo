#!/usr/bin/env python3
"""Validate six offline phase reports and join the already reported 4024 runs."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
SOURCE_SHA = '9e3a8d4c3b7d0c3c29727cd3ba77a55aaaead5532d016bb2ad54cfdca1cbcdf9'
PREVIOUS_SHA = '0dfce6d19e8dc1b5cd7345aa63e929459a59fb613de5909954abbc1f98d2d2ee'
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise ValueError(message)


def close(left, right, message, tolerance=1e-8):
    check(math.isclose(left, right, rel_tol=1e-12, abs_tol=tolerance), message)


def validate_scope(scope, report, matched):
    check(scope['stage0_submissions'] == report['first_stage0_callbacks'], 'submission conservation')
    submissions = scope['stage0_submissions']
    pending, executing, either = (scope[f'submissions_with_{name}']
                                 for name in ('older_pending', 'older_executing', 'either'))
    check(0 <= max(pending, executing) <= either <= min(submissions, pending + executing),
          'submission intersection bounds')
    for key in ('new_requests_exposed_at_submission', 'new_requests_with_simultaneous_executing'):
        check(0 <= scope[key] <= report['cohort_requests'], 'request exposure bound')
    for key in ('max_older_pending_at_submission', 'max_older_executing_at_submission',
                'max_combined_executing_callbacks_when_exposed'):
        check(type(scope[key]) is int and scope[key] >= 0, 'invalid peak callback count')
    live = scope['new_live_request_resource_ms']
    active = scope['new_executing_request_resource_ms']
    simultaneous = scope['simultaneous_executing_request_resource_ms']
    check(0 <= simultaneous <= active <= live, 'executing union bounds')
    check(simultaneous <= scope['new_live_with_older_executing_request_resource_ms'] <= live,
          'older executing union bounds')
    check(0 <= scope['new_live_with_older_pending_request_resource_ms'] <= live,
          'older pending union bounds')
    close(simultaneous / active, scope['simultaneous_executing_fraction'], 'exposure ratio')
    wait = scope['inverse_later_wait']
    count = wait['later_wave_callbacks']
    exposed = wait['callbacks_with_newer_stage0_execution_during_wait']
    nonexposed = wait['callbacks_without_newer_stage0_execution_during_wait']
    total = wait['total_callback_wait_ms']
    covered = wait['covered_union_callback_wait_ms']
    exposed_wait = wait['exposed_callbacks_total_wait_ms']
    other_wait = wait['nonexposed_callbacks_total_wait_ms']
    check(count == report['later_wave_callbacks_cohort'], 'later-wave denominator')
    check(exposed + nonexposed == count and min(exposed, nonexposed) >= 0, 'exposed callback conservation')
    check(wait['positive_wait_callbacks'] + wait['zero_wait_callbacks'] == count,
          'positive/zero wait conservation')
    check(0 <= covered <= exposed_wait <= total, 'covered wait bounds')
    close(exposed_wait + other_wait, total, 'wait area conservation')
    close(covered / total, wait['covered_fraction_of_total_wait'], 'covered wait ratio')
    if exposed:
        close(exposed * wait['exposed_callbacks_wait_mean_ms'], exposed_wait, 'exposed full-wait mean')
    else:
        check(wait['exposed_callbacks_wait_mean_ms'] is None, 'undefined exposed mean')
    close(nonexposed * wait['nonexposed_callbacks_wait_mean_ms'], other_wait, 'nonexposed full-wait mean')
    roles = [matched['roles'][key] for key in ('later-wave stage0', 'ordinary successors')]
    check(sum(role['n'] for role in roles) == count, '4024 role denominator')
    rounded_total = sum(role['n'] * role['wait_mean_ms'] for role in roles)
    allowance = count * 0.0005
    check(abs(total - rounded_total) <= allowance + 1e-8, '4024 rounded role means disagree')
    return dict(submission_exposure_percent=100 * either / submissions,
                request_exposure_at_submission_percent=100 * scope['new_requests_exposed_at_submission'] / report['cohort_requests'],
                simultaneous_executing_percent=100 * simultaneous / active,
                later_wait_exposed_callbacks_percent=100 * exposed / count,
                later_wait_covered_percent=100 * covered / total,
                later_wait_mean_ms=total / count,
                later_wait_uncovered_callback_ms=total - covered,
                rounded_4024_role_wait_total_callback_ms=rounded_total,
                role_wait_rounding_allowance_callback_ms=allowance,
                role_wait_rounding_difference_callback_ms=total - rounded_total)


def build(path):
    global CHECKS
    CHECKS = 0
    raw = path.read_bytes()
    check(hashlib.sha256(raw).hexdigest() == SOURCE_SHA, 'wrong 4026 input')
    lines = raw.decode().splitlines()
    check('Ran 9 tests' in raw.decode() and '\nOK\n' in raw.decode().replace('\r\n', '\n'),
          'expected analyzer test transcript absent')
    previous_path = HERE.parent / '4024/evidence.json'
    previous_raw = previous_path.read_bytes()
    check(hashlib.sha256(previous_raw).hexdigest() == PREVIOUS_SHA, 'wrong 4024 evidence')
    previous = json.loads(previous_raw)
    index = {run['options']['--output']: run for run in previous['runs']}
    check(len(index) == len(previous['runs']) == 36, 'previous run inventory')
    extracted = [(number, json.loads(line)) for number, line in enumerate(lines, 1)
                 if line.startswith('{')]
    check(len(extracted) == 6, 'expected six reports')
    seen, runs = set(), []
    for number, report in extracted:
        path_key = report['run']
        check(path_key in index and path_key not in seen, 'unmatched or duplicate run')
        seen.add(path_key)
        matched = index[path_key]
        options, cohort = matched['options'], matched['cohort']
        check(report['schema'] == 'affinity_request_phase_overlap_v1', 'wrong overlap schema')
        check(report['widths'] == [80, 4] and report['depths'] == [1, 22], 'wrong graph')
        check(options['--iterations'] == '3000000' and options['--depths'] == '1,22', 'wrong 4024 cell')
        check(report['policy'] == options['--policy'], 'policy mismatch')
        check(options['--threads'] == '16' and options['--qps'] == '12', 'resource or arrival mismatch')
        check(report['planned_cohort_seconds'] == [2.0, 18.0], 'cohort bounds')
        check(report['cohort_requests'] == cohort['completed'] == cohort['offered'] == 192, 'cohort count')
        check(report['completed_requests_whole'] == matched['whole']['completed'] == 240, 'whole count')
        check(report['first_stage0_callbacks'] == 192 * 80, 'initial cohort callbacks')
        check(report['initial_stage0_callbacks_whole'] == 240 * 80, 'initial whole callbacks')
        check(report['later_wave_callbacks_cohort'] == 192 * 4 * 22, 'later cohort callbacks')
        check(report['later_wave_callbacks_whole'] == 240 * 4 * 22, 'later whole callbacks')
        check(report['equal_planned_arrival_extra_requests'] == 0, 'unexpected tied arrivals')
        check(set(report['source_sha256']) == {'requests.csv', 'tasks.csv', 'summary.json'}, 'source hashes missing')
        for digest in report['source_sha256'].values():
            check(re.fullmatch('[0-9a-f]{64}', digest) is not None, 'invalid reported hash format')
        resource = report['resource']
        check(resource['disjoint_cpu_sets'] and resource['pool_service_maps_to_declared_cluster'], 'resource contract')
        pools = resource['pools']
        check(len(pools) == 6 and [pool['pool'] for pool in pools] == list(range(6)), 'pool identity')
        cpu_sets = [pool['cpus'] for pool in pools]
        flattened = [cpu for group in cpu_sets for cpu in group]
        check(len(flattened) == len(set(flattened)) == resource['union_logical_cpu_count'] == 96, 'CPU set conservation')
        check(sorted(flattened) == resource['union_cpus'], 'CPU union mismatch')
        check([pool['numa'] for pool in pools] == [0, 0, 0, 2, 2, 2], 'NUMA grouping')
        for pool in pools:
            check(pool['logical_cpu_count'] == len(pool['cpus']) == pool['configured_score_capacity'] == 16,
                  'logical capacity mismatch')
        check(set(report['scopes']) == {'pool', 'numa'}, 'missing scopes')
        derived = {scope: validate_scope(values, report, matched)
                   for scope, values in report['scopes'].items()}
        pool, numa = (report['scopes'][name] for name in ('pool', 'numa'))
        p_wait, n_wait = pool['inverse_later_wait'], numa['inverse_later_wait']
        close(p_wait['total_callback_wait_ms'], n_wait['total_callback_wait_ms'], 'scope-invariant wait area')
        check(p_wait['callbacks_with_newer_stage0_execution_during_wait'] <= n_wait['callbacks_with_newer_stage0_execution_during_wait'], 'scope count inclusion')
        check(p_wait['covered_union_callback_wait_ms'] <= n_wait['covered_union_callback_wait_ms'], 'scope wait inclusion')
        check(pool['submissions_with_either'] <= numa['submissions_with_either'], 'scope submission inclusion')
        derived['numa_additional_covered_wait_callback_ms'] = n_wait['covered_union_callback_wait_ms'] - p_wait['covered_union_callback_wait_ms']
        derived['seed'] = int(options['--seed'])
        derived['policy'] = report['policy']
        runs.append(dict(source_line=number, overlap=report, matched_4024=matched, derived=derived))
    check({(run['derived']['seed'], run['derived']['policy']) for run in runs}
          == {(seed, policy) for seed in (421, 422) for policy in ('home', 'queue', 'outstanding')}, 'pair inventory')
    order = {'home': 0, 'queue': 1, 'outstanding': 2}
    runs.sort(key=lambda run: (run['derived']['seed'], order[run['derived']['policy']]))
    for run in runs:
        d, m = run['derived'], run['matched_4024']
        home = next(item['matched_4024'] for item in runs
                    if item['derived']['seed'] == d['seed'] and item['derived']['policy'] == 'home')
        check(m['cohort']['signatures'] == home['cohort']['signatures'], 'paired workload fingerprint mismatch')
        check(m['cohort']['fixed_work_prefix'] == home['cohort']['fixed_work_prefix'], 'paired fixed-work fingerprint mismatch')
        for field in ('mean_ms', 'p99_ms'):
            d[f'cohort_{field.removesuffix("_ms")}_change_vs_home_percent'] = 100 * (m['cohort'][field] / home['cohort'][field] - 1)
        d['whole_cpu_change_vs_home_percent'] = 100 * (m['whole']['process_cpu_s'] / home['whole']['process_cpu_s'] - 1)
    return dict(schema='review-4026-v1', source_log_sha256=SOURCE_SHA, source_lines=len(lines),
                previous_evidence_sha256=PREVIOUS_SHA,
                scope='Six offline reports of six existing 3M tail4 runs among the 36 executions already reported in 4023/4024. Zero new executions.',
                runs=runs, validation_checks=CHECKS,
                limitations=[
                    'Only the supplied terminal report is available locally. Target requests.csv, tasks.csv and summary.json hashes are recorded, not independently recomputed here.',
                    'Executing is callback wall occupancy; pending is post-routing submitted-not-started. Neither measures CPU service, kernel runnable demand or exact Folly queue occupancy.',
                    'Overlap establishes shared time and declared pool resources, not causal slowdown. Conditional exposed/nonexposed full-wait means are length-biased.',
                    'Pool and NUMA executing-union durations have different resource multiplicities and cannot be compared as CPU utilization.',
                    'Later-wave wait includes 768 later-wave stage0 and 16128 ordinary-successor callbacks per run. It is not the ordinary-successor-only mean.',
                    'Latency and overlap use 192 planned arrivals in [2,18) seconds; process CPU uses all 240 completed requests.',
                    'Two seeds are descriptive; no confidence interval or average of p99 is inferred. The 80x1 + 4x22 graph is an uncalibrated sensitivity model.',
                    'The 128/192 exposure frequency depends on regular 12 QPS and round-robin home ordering; it is not a production probability.',
                    'Declared CPU sets are disjoint, but hardware cluster identity is not independently verified by this analysis.',
                    'No new pressure time series, production request markers, gate experiment or threshold validation is supplied.'])


def comparison_rows(evidence):
    rows = []
    for run in evidence['runs']:
        report, matched, derived = run['overlap'], run['matched_4024'], run['derived']
        row = dict(run=report['run'], source_line=run['source_line'], seed=derived['seed'], policy=derived['policy'],
                   cohort_requests=report['cohort_requests'], cohort_mean_ms=matched['cohort']['mean_ms'],
                   cohort_p99_ms=matched['cohort']['p99_ms'],
                   whole_process_cpu_per_request_ms=matched['whole']['process_cpu_per_completed_request_ms'],
                   cohort_mean_change_vs_home_percent=derived['cohort_mean_change_vs_home_percent'],
                   cohort_p99_change_vs_home_percent=derived['cohort_p99_change_vs_home_percent'],
                   whole_cpu_change_vs_home_percent=derived['whole_cpu_change_vs_home_percent'])
        for scope in ('pool', 'numa'):
            values, wait = report['scopes'][scope], report['scopes'][scope]['inverse_later_wait']
            for key in ('new_requests_exposed_at_submission', 'new_requests_with_simultaneous_executing',
                        'submissions_with_either', 'new_executing_request_resource_ms',
                        'simultaneous_executing_request_resource_ms'):
                row[f'{scope}_{key}'] = values[key]
            for key in ('later_wave_callbacks', 'callbacks_with_newer_stage0_execution_during_wait',
                        'total_callback_wait_ms', 'covered_union_callback_wait_ms',
                        'exposed_callbacks_wait_mean_ms', 'nonexposed_callbacks_wait_mean_ms'):
                row[f'{scope}_{key}'] = wait[key]
            for key in ('submission_exposure_percent', 'simultaneous_executing_percent',
                        'later_wait_exposed_callbacks_percent', 'later_wait_covered_percent', 'later_wait_mean_ms'):
                row[f'{scope}_{key}'] = derived[scope][key]
        row['numa_additional_covered_wait_callback_ms'] = derived['numa_additional_covered_wait_callback_ms']
        rows.append(row)
    return rows


def tables(evidence):
    labels = {'home': 'home', 'queue': 'Q', 'outstanding': 'O'}
    exposure = ['| 策略 / seed | 新首波与旧后期同池相遇请求数 | 同池相遇提交占比 | 同池等待交集回调占比 | 同池交集覆盖后期等待 |',
                '|---|---:|---:|---:|---:|']
    waits = ['| 策略 / seed | 全后期等待 callback-ms | 被同池新首波覆盖 callback-ms | 全后期等待均值 ms | 相遇回调完整等待均值 ms | 未相遇回调完整等待均值 ms |',
             '|---|---:|---:|---:|---:|---:|']
    latency = ['| 策略 / seed | 请求均值 ms | 请求 p99 ms | 整轮 CPU/请求 ms | 均值较 home 变化 | p99 较 home 变化 | CPU 较 home 变化 |',
               '|---|---:|---:|---:|---:|---:|---:|']
    for run in evidence['runs']:
        report, matched, d = run['overlap'], run['matched_4024'], run['derived']
        label = f'{labels[d["policy"]]} / {d["seed"]}'
        scope, pd = report['scopes']['pool'], d['pool']
        wait = scope['inverse_later_wait']
        exposure.append(f'| {label} | {scope["new_requests_with_simultaneous_executing"]}/192 | {pd["submission_exposure_percent"]:.4f}% | {pd["later_wait_exposed_callbacks_percent"]:.4f}% | {pd["later_wait_covered_percent"]:.4f}% |')
        exposed_mean = wait['exposed_callbacks_wait_mean_ms']
        exposed_text = '无相遇回调' if exposed_mean is None else f'{exposed_mean:.6f}'
        waits.append(f'| {label} | {wait["total_callback_wait_ms"]:.5f} | {wait["covered_union_callback_wait_ms"]:.5f} | {pd["later_wait_mean_ms"]:.6f} | {exposed_text} | {wait["nonexposed_callbacks_wait_mean_ms"]:.6f} |')
        latency.append(f'| {label} | {matched["cohort"]["mean_ms"]:.3f} | {matched["cohort"]["p99_ms"]:.3f} | {matched["whole"]["process_cpu_per_completed_request_ms"]:.3f} | {d["cohort_mean_change_vs_home_percent"]:+.3f}% | {d["cohort_p99_change_vs_home_percent"]:+.3f}% | {d["whole_cpu_change_vs_home_percent"]:+.3f}% |')
    return ('六轮均为既有执行的离线输出；图为80×1 + 4×22，每算子3M迭代。'
            '时间交集代表回调墙钟状态，不能替代CPU占用或因果分析。\n\n'
            + '\n'.join(exposure) + '\n\n后期回调分母为16896（768个后期首层 + 16128个普通后继）。'
            '完整等待条件均值存在长度偏差；覆盖量按每回调等待区间取并集。\n\n'
            + '\n'.join(waits) + '\n\n以下时延、CPU数据来自4024对应执行；'
            '请求时延分母192，整轮CPU成本分母240。变化为候选/home−1；不平均两个seed的p99。\n\n'
            + '\n'.join(latency) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-log', type=Path, required=True)
    args = parser.parse_args()
    evidence = build(args.source_log)
    (HERE / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    rows = comparison_rows(evidence)
    with (HERE / 'comparison.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    (HERE / 'tables.md').write_text(tables(evidence))
    print(f'PASS {CHECKS} checks: 6 offline reports matched to existing runs; 0 new executions.')
    print(tables(evidence))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Audit 4028 against 4027 without treating offline detail as new trials."""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_SHA = 'd4339b77b2eec3d7ff5acf43ee3bbdedbd08e4c38b8ad179887837321300bc6f'
PREVIOUS_SHA = '94d41665929254eb2a7969f1428e8d8bc7f9ffd767ab3640adeb7257a0ae62af'
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise ValueError(message)


def close(left, right, message):
    check(math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-7), message)


def thresholds(data):
    check([r['wait_gt_ms'] for r in data['thresholds']] == [1, 5, 20], 'threshold contract')
    last_n, last_sum = data['callbacks'], data['total_wait_ms']
    for row in data['thresholds']:
        check(0 <= row['callbacks'] <= last_n, 'nested threshold counts')
        check(0 <= row['wait_ms'] <= last_sum + 1e-7, 'nested threshold full-wait sums')
        check(row['wait_ms'] >= row['callbacks'] * row['wait_gt_ms'], 'wait sum lower bound')
        last_n, last_sum = row['callbacks'], row['wait_ms']


def detail(row):
    close(row['start_ms_from_origin'] - row['submitted_ms_from_origin'], row['wait_ms'], 'callback wait')
    close(row['finish_ms_from_origin'] - row['start_ms_from_origin'], row['wall_ms'], 'callback wall')
    check(0 <= row['cpu_ms'] <= row['wall_ms'] + 1e-7, 'CPU inside wall measurement')
    check(row['worker_tid'] > 0, 'worker TID')
    overlap = row['same_pool_other_callback_wall_during_wait']
    check(0 <= overlap['covered_union_ms'] <= row['wait_ms'] + 1e-7, 'union bounded by wait')
    check(overlap['callback_wall_sum_ms'] + 1e-7 >= overlap['covered_union_ms'], 'sum at least union')
    check(0 <= overlap['max_overlapping_callback_wall_intervals'] <= 16, 'callback concurrency bound')
    check(overlap['same_request_callbacks'] + overlap['other_request_callbacks'] == overlap['overlapping_callbacks'], 'overlap counts')
    check(sum(r['callbacks'] for r in overlap['by_role'].values()) == overlap['overlapping_callbacks'], 'overlap role counts')
    close(sum(r['callback_wall_sum_ms'] for r in overlap['by_role'].values()), overlap['callback_wall_sum_ms'], 'overlap role sum')


def build(source):
    global CHECKS
    CHECKS = 0
    raw = source.read_bytes()
    check(hashlib.sha256(raw).hexdigest() == SOURCE_SHA, 'wrong 4028 source')
    records = [(n, json.loads(line)) for n, line in enumerate(raw.decode().splitlines(), 1) if line.startswith('{')]
    check(len(records) == 13, '13 complete records required')
    previous_raw = (HERE.parent / '4027/evidence.json').read_bytes()
    check(hashlib.sha256(previous_raw).hexdigest() == PREVIOUS_SHA, '4027 evidence changed')
    previous = json.loads(previous_raw)
    old_header = previous['source_records'][0]['data']
    old_runs = {(r['data']['seed'], r['data']['case']): r['data'] for r in previous['source_records'][1:13]}
    header, runs = records[0][1], [r for _, r in records[1:]]
    check(header['schema'] == 'affinity-admission-tail-audit-v1-report', 'header schema')
    check(header['runs'] == 12 and header['validated_pairs'] == 8 and header['development'] is False, 'matrix inventory')
    check(header['cohort_seconds'] == [2, 18] and header['top'] == 3, 'detail window/top')
    for key in ('binary_sha256', 'git_head', 'metadata_sha256', 'plan_sha256', 'fixed_options'):
        check(header[key] == old_header[key], 'header differs from 4027: ' + key)
    for path, digest in old_header['analysis_sources_sha256'].items():
        check(header['analysis_sources_sha256'][path] == digest, 'base analyzer differs from 4027')
    check(header['analysis_sources_sha256']['experiments/affinity/admission_tail_audit.py'] ==
          '9e5d354b6d7e871fb9e1720a301cfef22735293ead022ced20adb687c8708862', 'tail analyzer version')
    check(len({(r['seed'], r['case']) for r in runs}) == 12, 'no duplicate runs')
    extremes = []
    for run in runs:
        check(run['schema'] == 'affinity-admission-tail-audit-v1-run', 'run schema')
        old = old_runs[run['seed'], run['case']]
        for key in ('build_sha256', 'pairing_sha256', 'work_sha256', 'request_checksums_sha256',
                    'source_sha256', 'resources', 'latency_breakdown', 'cohort_seconds', 'development',
                    'run', 'shape', 'target_limit'):
            check(run[key] == old[key], 'run differs from 4027: ' + key)
        check(run['cohort_requests'] == 192 and run['whole_completed_requests'] == 240, 'request counts')
        total, roles = run['callback_waits']['all'], run['callback_waits']['by_role']
        check(total['callbacks'] == 192 * 171 == sum(r['callbacks'] for r in roles.values()), 'callback conservation')
        close(total['total_wait_ms'], sum(r['total_wait_ms'] for r in roles.values()), 'wait conservation')
        for values in (total, *roles.values()):
            thresholds(values)
        for i, threshold in enumerate(total['thresholds']):
            check(threshold['callbacks'] == sum(r['thresholds'][i]['callbacks'] for r in roles.values()), 'threshold count conservation')
            close(threshold['wait_ms'], sum(r['thresholds'][i]['wait_ms'] for r in roles.values()), 'threshold wait conservation')
        for role, values in roles.items():
            check(values['callbacks'] == old['roles'][role]['n'], '4027 role count')
            close(values['total_wait_ms'], old['roles'][role]['wait_ms'] * values['callbacks'], '4027 role wait')
            close(run['max_wait_callback_by_role'][role]['wait_ms'], old['roles'][role]['wait_distribution_ms']['max'], '4027 role max')
        check(total['thresholds'][2]['callbacks'] == 1, 'observed exactly one >20ms callback per run')
        check(len(run['top_requests_by_planned_latency']) == len(run['top_callbacks_by_wait']) == len(run['top_callbacks_by_wall']) == 3, 'top list size')
        check([r['wait_ms'] for r in run['top_callbacks_by_wait']] == sorted([r['wait_ms'] for r in run['top_callbacks_by_wait']], reverse=True), 'wait top order')
        check([r['wall_ms'] for r in run['top_callbacks_by_wall']] == sorted([r['wall_ms'] for r in run['top_callbacks_by_wall']], reverse=True), 'wall top order')
        identities = {}
        for row in [*run['top_callbacks_by_wait'], *run['top_callbacks_by_wall'], *run['max_wait_callback_by_role'].values()]:
            detail(row)
            identity = row['request'], row['tag']
            check(identity not in identities or identities[identity] == row, 'repeated callback detail differs')
            identities[identity] = row
        for request in run['top_requests_by_planned_latency']:
            close(request['planned_latency_ms'], request['finished_ms_from_origin'] - request['scheduled_ms_from_origin'], 'request latency')
            close(request['attempt_lag_ms'] + request['after_attempt_ms'], request['planned_latency_ms'], 'request mean components')
            check(request['callback_count'] == 171, 'complete request callback inventory')
        top_request, top = run['top_requests_by_planned_latency'][0], run['top_callbacks_by_wait'][0]
        check(top_request['request'] == top['request'], 'extreme wait belongs to slowest request')
        close(top_request['planned_latency_ms'], old['latency_breakdown']['planned_ms']['max'], '4027 request max')
        close(top['wait_ms'], total['thresholds'][2]['wait_ms'], 'unique >20ms wait sum')
        overlap = top['same_pool_other_callback_wall_during_wait']
        max_wall = run['top_callbacks_by_wall'][0]['wall_ms']
        check(top['kind'] == 'operator' and top['role'] in ('first-wave stage0', 'ordinary successors'), 'parent bound scope')
        check(header['fixed_options']['async-percent'] == '0', 'parent bound requires direct synchronous submission')
        check(top['wait_ms'] > max_wall, 'positive post-parent lower bound')
        extremes.append(dict(seed=run['seed'], case=run['case'], shape=run['shape'], target_limit=run['target_limit'],
                             **{k: top[k] for k in ('request', 'tag', 'role', 'wave', 'stage', 'source', 'chosen', 'worker_tid',
                                                  'queue_before', 'outstanding_before', 'callback_inflight_before', 'reason_name',
                                                  'submitted_ms_from_origin', 'start_ms_from_origin', 'wait_ms', 'wall_ms', 'cpu_ms')},
                             request_max_ms=top_request['planned_latency_ms'],
                             cohort_max_callback_wall_ms=max_wall,
                             after_parent_finish_lower_bound_ms=top['wait_ms'] - max_wall,
                             covered_union_ms=overlap['covered_union_ms'],
                             uncovered_wall_ms=top['wait_ms'] - overlap['covered_union_ms'],
                             max_other_callback_wall_intervals=overlap['max_overlapping_callback_wall_intervals'],
                             other_request_callbacks=overlap['other_request_callbacks']))
    terms = []
    for pair in previous['paired_deltas']:
        seed, shape, limit = pair['seed'], pair['shape'], pair['target_limit']
        old_group = [r for r in old_runs.values() if r['seed'] == seed and r['shape'] == shape]
        off = next(r for r in old_group if r['target_limit'] == 0)
        candidate = next(r for r in old_group if r['target_limit'] == limit)
        delta = candidate['cohort']['mean_ms'] - off['cohort']['mean_ms']
        max_term = (candidate['latency_breakdown']['planned_ms']['max'] - off['latency_breakdown']['planned_ms']['max']) / 192
        other_term = delta - max_term
        close(delta, pair['mean_ms_delta'], 'same full-cohort delta')
        close(delta, max_term + other_term, 'arithmetic decomposition')
        terms.append(dict(seed=seed, shape=shape, target_limit=limit, full_mean_delta_ms=delta,
                          max_value_difference_over_192_ms=max_term, other_values_sum_difference_over_192_ms=other_term))
    return dict(source_log_sha256=SOURCE_SHA, previous_evidence_sha256=PREVIOUS_SHA, checks=CHECKS,
                source_records=[dict(line=n, data=r) for n, r in records], extremes=extremes, mean_terms=terms,
                observed_counts=dict(runs=12, callbacks_per_cohort=32832, unique_over_20ms=12,
                                     request_42=sum(r['request'] == 42 for r in extremes),
                                     pool_0=sum(r['chosen'] == 0 for r in extremes),
                                     queue_before_zero=sum(r['queue_before'] == 0 for r in extremes),
                                     uncovered_waits=sum(r['uncovered_wall_ms'] > 1e-7 for r in extremes),
                                     reasons=dict(Counter(r['reason_name'] for r in extremes))),
                limitations=['Offline detail from the same twelve 4027 runs, not additional trials.',
                             'Original target CSVs are not available locally; their reported hashes match 4027.',
                             'Callback-wall coverage is neither CPU usage nor kernel runnable/queue state.',
                             'Post-parent bound uses this synchronous DAG: child submitted within parent callback, parent wall <= cohort max wall; parent finish follows child add return.',
                             'Each max term is a different run order statistic, not matched-request causal cost.',
                             'All 192 requests remain in the primary mean; other term is not a new mean over 191 requests.',
                             'No exclusion/censoring, warmup change, workload or policy change.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-log', type=Path, required=True)
    evidence = build(parser.parse_args().source_log)
    (HERE / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    for name, rows in [('extremes.csv', evidence['extremes']), ('mean_terms.csv', evidence['mean_terms'])]:
        with (HERE / name).open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
    print(f"4028 PASS: {evidence['checks']} checks; 12 existing runs, no new performance samples")


if __name__ == '__main__':
    main()

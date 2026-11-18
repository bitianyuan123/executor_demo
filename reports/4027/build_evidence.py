#!/usr/bin/env python3
"""Recheck the 4027 terminal contract; never claim to re-read target CSVs."""
import argparse
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE_SHA = 'dcbfb3fa9ed56de972cd02d96a0179efa9d0c56ccc2ce182c805eb7b24e19f5f'
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise ValueError(message)


def close(left, right, message):
    check(math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-7), message)


def build(path):
    global CHECKS
    CHECKS = 0
    raw = path.read_bytes()
    check(hashlib.sha256(raw).hexdigest() == SOURCE_SHA, 'wrong 4027 log')
    extracted = [(number, json.loads(line)) for number, line in enumerate(raw.decode().splitlines(), 1)
                 if line.startswith('{')]
    check(len(extracted) == 21, 'need 21 complete JSON records')
    header = extracted[0][1]
    runs = [value for _, value in extracted[1:13]]
    pairs = [value for _, value in extracted[13:]]
    spec = importlib.util.spec_from_file_location('admission_matrix', ROOT / 'scripts/run_destination_admission_matrix.py')
    matrix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matrix)
    check(header['schema'] == matrix.SCHEMA + '-report', 'header schema')
    check(header['development'] is False and header['runs'] == 12 and header['pairs'] == 8, 'matrix inventory')
    check(header['fixed_options'] == {**matrix.FIXED, 'cpus': matrix.CPUS}, 'fixed options')
    check(header['cpu_partitions'] == matrix.cpu_partitions(matrix.CPUS), 'CPU partitions')
    check(header['git_head'] == '5cc25bab6a3d636578a10e5cf5a52b2dd37d8d88', 'execution commit')
    for name, digest in header['execution_sources_sha256'].items():
        check(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, 'execution source changed: ' + name)
    for name, digest in header['analysis_sources_sha256'].items():
        check(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, 'analysis source changed: ' + name)
    rows = []
    for run in runs:
        check(run['schema'] == matrix.SCHEMA + '-run' and run['development'] is False, 'run schema/scope')
        check(run['build_sha256'] == header['binary_sha256'], 'one execution binary')
        check(run['cohort_seconds'] == [2, 18], 'cohort window')
        resources = run['resources']
        check(resources['threads_per_pool'] == 16 and resources['affinity'] == 'cluster', 'worker contract')
        check(resources['continuation'] == 'parent' and resources['balance_at'] == 'all', 'routing contract')
        check([g['cpus'] for g in resources['groups']] == header['cpu_partitions'], 'actual CPU sets')
        check([g['numa'] for g in resources['groups']] == [0, 0, 0, 2, 2, 2], 'actual NUMAs')
        check(all(g['capacity'] == 16 for g in resources['groups']), 'actual capacity')
        whole, cohort, roles = run['whole_run'], run['cohort'], run['roles']
        check(whole['completed'] == 240 and whole['failed_or_rejected'] == 0, 'whole-run completions')
        check(cohort['n'] == cohort['offered'] == 192 and cohort['tasks'] == 192 * 171, 'cohort conservation')
        check(cohort['cross_numa'] == 0 and cohort['blocked_operators'] == 0, 'no remote/blocking callbacks')
        close(whole['process_cpu_s'] * 1000 / 240, whole['cpu_per_completed_request_ms'], 'CPU denominator')
        check(run['latency_breakdown']['statuses'] == {'completed': 192}, 'cohort status')
        close(run['latency_breakdown']['after_attempt_ms']['mean'] + run['latency_breakdown']['lag_ms']['mean'],
              cohort['mean_ms'], 'mean decomposition')
        check(sum(r['n'] for r in roles.values()) == cohort['tasks'], 'role conservation')
        check(sum(r['migrations'] for r in roles.values()) == cohort['migrations'], 'placement conservation')
        counts = {'init': 192, 'join': 384, 'first-wave stage0': 15360, 'later-wave stage0': 768,
                  'ordinary successors': 16128}
        for name, role in roles.items():
            check(role['n'] == counts[name], 'role count')
            check(sum(role['routing_reasons'].values()) == role['n'], 'reason conservation')
            check(role['wait_distribution_ms']['n'] == role['n'], 'role wait count')
            close(role['wait_ms'], role['wait_distribution_ms']['mean'], 'role mean')
            check(0 <= role['wait_distribution_ms']['p50'] <= role['wait_distribution_ms']['p95']
                  <= role['wait_distribution_ms']['p99'] <= role['wait_distribution_ms']['max'], 'quantile ordering')
        matrix.validate_admission(whole['target_admission'], run['target_limit'])
        check(whole['target_admission']['stats']['selections'] == (41040 if run['target_limit'] else 0), 'selection conservation')
        for scope in run['phase_overlap'].values():
            x = scope['inverse_later_wait']
            check(x['later_wave_callbacks'] == 192 * (8 if run['shape'] == 'front80' else 88), 'later-wave denominator')
            check(x['callbacks_with_newer_stage0_execution_during_wait'] + x['callbacks_without_newer_stage0_execution_during_wait']
                  == x['later_wave_callbacks'], 'exposed callback conservation')
            check(0 <= x['covered_union_callback_wait_ms'] <= x['exposed_callbacks_total_wait_ms'] <= x['total_callback_wait_ms'], 'wait intersection bounds')
            close(x['exposed_callbacks_total_wait_ms'] + x['nonexposed_callbacks_total_wait_ms'], x['total_callback_wait_ms'], 'wait area conservation')
            close(x['covered_union_callback_wait_ms'] / x['total_callback_wait_ms'], x['covered_fraction_of_total_wait'], 'covered wait ratio')
            check(0 <= scope['new_requests_with_simultaneous_executing'] <= 192, 'exposure bounds')
            if run['shape'] == 'tail4':
                close(sum(roles[name]['wait_ms'] * roles[name]['n'] for name in ('ordinary successors', 'later-wave stage0')),
                      x['total_callback_wait_ms'], 'tail4 later-wave role total')
        pool = run['phase_overlap']['pool']['inverse_later_wait']
        numa = run['phase_overlap']['numa']['inverse_later_wait']
        check(pool['covered_union_callback_wait_ms'] <= numa['covered_union_callback_wait_ms'] + 1e-7, 'pool intersection subset of NUMA')
        row = dict(seed=run['seed'], case=run['case'], shape=run['shape'], target_limit=run['target_limit'],
                   **{k: cohort[k] for k in ('mean_ms', 'p95_ms', 'p99_ms')},
                   cpu_per_request_ms=whole['cpu_per_completed_request_ms'],
                   reroutes=cohort['migrations'], reroutes_percent=100 * cohort['migrations'] / cohort['tasks'],
                   first_wait_mean_ms=roles['first-wave stage0']['wait_ms'],
                   first_wait_p99_ms=roles['first-wave stage0']['wait_distribution_ms']['p99'],
                   successors_wait_mean_ms=roles['ordinary successors']['wait_ms'],
                   successors_wait_p99_ms=roles['ordinary successors']['wait_distribution_ms']['p99'],
                   successors_wait_max_ms=roles['ordinary successors']['wait_distribution_ms']['max'],
                   request_max_ms=run['latency_breakdown']['planned_ms']['max'],
                   later_wait_sum_callback_ms=pool['total_callback_wait_ms'],
                   later_wait_mean_ms=pool['total_callback_wait_ms'] / pool['later_wave_callbacks'],
                   later_wait_covered_percent=100 * pool['covered_fraction_of_total_wait'],
                   same_pool_exposed_requests=run['phase_overlap']['pool']['new_requests_with_simultaneous_executing'],
                   no_eligible_target=whole['target_admission']['stats']['no_eligible_target'],
                   candidate_over_limit=whole['target_admission']['stats']['candidate_over_limit'])
        rows.append(row)
    expected_pairs = matrix.paired_deltas(runs)
    check(len(expected_pairs) == len(pairs) == 8, 'pair inventory')
    for expected, actual in zip(expected_pairs, pairs):
        check(expected == actual, 'reported pair differs from recomputation')
    derived_pairs = []
    for pair in pairs:
        base = next(r for r in rows if r['seed'] == pair['seed'] and r['case'] == pair['baseline'])
        candidate = next(r for r in rows if r['seed'] == pair['seed'] and r['case'] == pair['candidate'])
        d = dict(seed=pair['seed'], shape=pair['shape'], target_limit=pair['target_limit'])
        for key in ('mean_ms', 'p95_ms', 'p99_ms', 'cpu_per_request_ms', 'reroutes', 'first_wait_mean_ms', 'successors_wait_mean_ms'):
            d[key + '_delta'] = candidate[key] - base[key]
            d[key + '_percent'] = 100 * (candidate[key] / base[key] - 1)
        derived_pairs.append(d)
    return dict(source_log_sha256=SOURCE_SHA, source_records=[dict(line=n, data=v) for n, v in extracted],
                checks=CHECKS, rows=rows, paired_deltas=derived_pairs,
                limitations=['Terminal JSON is available; original target CSV files are not available locally.',
                             'Reported raw-file hashes retained but not independently verified against target files.',
                             '192 requests per cohort; CPU/admission counters cover all 240 requests.',
                             'Two seeds, descriptive paired differences, no stable p99 or production claim.',
                             'Callback wall intervals are not on-CPU; overlap is not causal waiting.',
                             'No CPU utilization timeline was recorded by this matrix.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-log', type=Path, required=True)
    args = parser.parse_args()
    evidence = build(args.source_log)
    (HERE / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    for name, rows in [('comparison.csv', evidence['rows']), ('paired_deltas.csv', evidence['paired_deltas'])]:
        with (HERE / name).open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
    print(f"4027 PASS: {evidence['checks']} checks, 12 runs, 8 recomputed pairs; original target CSVs unavailable locally")


if __name__ == '__main__':
    main()

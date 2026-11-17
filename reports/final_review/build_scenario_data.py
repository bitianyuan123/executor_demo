#!/usr/bin/env python3
"""Rebuild scenario tables from repository-contained reviewed terminal evidence.

No target CSV, attachment directory, network connection, or native rerun is
required. 4011_routing_evidence.json retains only the relevant printed rows,
original line numbers and SHA256 of the source log.
"""
import copy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SELECTED_BATCHES = ("4011", "4012", "4013", "4014", "4015", "4016", "4018", "4020")
EXTRA_FILE = "reports/final_review/data/4011_routing_evidence.json"

COMMON = {'pools': 6,
 'cpus': ['0-15', '16-31', '32-47', '192-207', '208-223', '224-239'],
 'logical_cpus_per_pool': 16,
 'numa_nodes': [0, 0, 0, 2, 2, 2],
 'affinity': 'cluster',
 'home_policy': 'round-robin',
 'widths': [80, 4],
 'depths': [2, 2],
 'business_operators_per_request': 168,
 'base_callbacks_per_request': 171,
 'graph': 'init -> 80 independent chains of depth 2 -> join -> 4 independent chains of depth 2 -> '
          'join/finish',
 'branch_kib': 64,
 'shared_kib': 0,
 'jitter_percent': 25,
 'long_probability_approx': 0.125,
 'long_multiplier': 4,
 'work_scale_scope': 'task',
 'continuation': 'parent',
 'batch_chunk': 1,
 'cross_numa': False,
 'max_hops': 4,
 'cpu_sampling': False,
 'request_latency_scope': 'planned arrivals [2,18) s, late completions retained',
 'cpu_scope': 'whole process, initial/warmup/drain requests included; helper CPU included once',
 'real_time_cpu_available': False}

SCENARIOS = {'12qps_8request_burst_3m_100k': {'name': '平均12 QPS；每666.667 ms一批8请求，批内100 us间隔；每算子基准300万迭代/10万访存',
                                  'qps': 12,
                                  'arrival': 'burst',
                                  'batch_size': 8,
                                  'batch_period_ms': 666.6666666666666,
                                  'within_batch_spacing_us': 100,
                                  'nominal_batch_span_us': 700,
                                  'iterations_base': 3000000,
                                  'accesses_base': 100000,
                                  'whole_requests': 240,
                                  'cohort_requests': 192,
                                  'batches': ['4011', '4012', '4013'],
                                  'threads': [16, 128]},
 '12qps_regular_3m_100k': {'name': '12 QPS；每83.333 ms一请求；每算子基准300万迭代/10万访存',
                           'qps': 12,
                           'arrival': 'regular',
                           'request_interval_ms': 83.33333333333333,
                           'iterations_base': 3000000,
                           'accesses_base': 100000,
                           'whole_requests': 240,
                           'cohort_requests': 192,
                           'batches': ['4014'],
                           'threads': [16]},
 '12qps_regular_375k_12k5': {'name': '12 QPS；每83.333 ms一请求；每算子基准37.5万迭代/1.25万访存',
                             'qps': 12,
                             'arrival': 'regular',
                             'request_interval_ms': 83.33333333333333,
                             'iterations_base': 375000,
                             'accesses_base': 12500,
                             'whole_requests': 240,
                             'cohort_requests': 192,
                             'batches': ['4015'],
                             'threads': [16, 128]},
 '96qps_regular_375k_12k5_direct': {'name': '96 QPS；每10.4167 ms一请求；每算子基准37.5万迭代/1.25万访存；无外部异步等待',
                                    'qps': 96,
                                    'arrival': 'regular',
                                    'request_interval_ms': 10.416666666666666,
                                    'iterations_base': 375000,
                                    'accesses_base': 12500,
                                    'whole_requests': 1920,
                                    'cohort_requests': 1536,
                                    'async_percent': 0,
                                    'async_delay_us': 0,
                                    'batches': ['4016', '4018:D0'],
                                    'threads': [16, 128]},
 '96qps_regular_375k_12k5_async0': {'name': '96 QPS；每10.4167 '
                                            'ms一请求；每算子基准37.5万迭代/1.25万访存；约50%算子经helper以0 ms计划延迟恢复',
                                    'qps': 96,
                                    'arrival': 'regular',
                                    'request_interval_ms': 10.416666666666666,
                                    'iterations_base': 375000,
                                    'accesses_base': 12500,
                                    'whole_requests': 1920,
                                    'cohort_requests': 1536,
                                    'async_percent': 50,
                                    'async_delay_us': 0,
                                    'async_alignment_us': 0,
                                    'batches': ['4018:A0'],
                                    'threads': [16]},
 '96qps_regular_375k_12k5_async2ms': {'name': '96 QPS；每10.4167 '
                                              'ms一请求；每算子基准37.5万迭代/1.25万访存；约50%算子按issue+2 '
                                              'ms计划到期，回调释放线程后由helper触发恢复',
                                      'qps': 96,
                                      'arrival': 'regular',
                                      'request_interval_ms': 10.416666666666666,
                                      'iterations_base': 375000,
                                      'accesses_base': 12500,
                                      'whole_requests': 1920,
                                      'cohort_requests': 1536,
                                      'async_percent': 50,
                                      'async_delay_us': 2000,
                                      'async_alignment_us': 0,
                                      'batches': ['4018:A1', '4020'],
                                      'threads': [16]}}

CPU_AUDIT = {'selected_target_batches': '4011,4012,4013,4014,4015,4016,4018,4020',
 'cpu_sampling_enabled': False,
 'runtime_series_recoverable_from_existing_selected_target_csv': False,
 'callback_occupancy_recoverable_from_trace1_tasks_csv': True,
 'migration_series_recoverable_from_trace1_tasks_csv': True,
 'request_latency_series_recoverable_from_requests_csv': True,
 'operator_cpu_is_total_per_closed_callback_not_timeline': True,
 'local_proc_csv_found': 'older x86 developer runs only; cannot substitute for ARM target stage4 '
                         'evidence',
 'existing_summary_average_cpu_possible': False,
 'average_cpu_missing_reason': 'process CPU starts before helper/sampler startup; '
                               'duration_with_drain starts at origin set 10ms after startup. Exact '
                               'wall duration matching process CPU is not recorded.',
 'existing_sample_fields': 'tasks.csv sampled_cpu_cores, sampled_at_ns, sample_window_ns, '
                           'cpu_valid: invalid in selected batches because sampling disabled'}

def read_json(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cells(row):
    return [value.strip() for value in row['text'].strip().strip('|').split('|')]


def extra_roles(rows):
    result = []
    for source in rows:
        values = cells(source)
        result.append({
            'role': values[0], 'n': int(values[1]),
            'source_ne_chosen': int(values[2]),
            'source_not_home_chosen_home': int(values[3]),
            'wait_mean_ms': float(values[4]), 'cpu_mean_ms': float(values[5]),
            'wall_mean_ms': float(values[6]),
            'routing_reasons': {
                key: int(value)
                for key, value in (item.strip().split(':') for item in values[7].split(','))
            },
            'source_line': source['line'],
        })
    return result


def scenario_id(run):
    if run['batch'] in ('4018', '4020'):
        suffix = {'D0': 'direct', 'A0': 'async0', 'A1': 'async2ms'}[run['scenario']]
        return '96qps_regular_375k_12k5_' + suffix
    return next(key for key, value in SCENARIOS.items() if run['batch'] in value['batches'])


def build():
    base = read_json(HERE / 'data/comparisons.json')
    original = {
        batch: read_json(ROOT / f'results/review/{batch}_summary.json')
        for batch in SELECTED_BATCHES
    }
    extra = read_json(ROOT / EXTRA_FILE)
    if extra['source_sha256'] != original['4011']['source_sha256']['4011.log']:
        raise ValueError('4011 excerpt and reviewed summary refer to different log bytes')
    runs = [copy.deepcopy(run) for run in base['runs'] if run['batch'] in SELECTED_BATCHES]
    for run in runs:
        batch = run['batch']
        index = run['source_pointer'].split('/')[-1]
        raw_runs = original[batch]['runs']
        raw = raw_runs[index] if isinstance(raw_runs, dict) else raw_runs[int(index)]
        run.update({
            'scenario_id': scenario_id(run),
            'cohort_migrations': raw.get('cohort_migrations'),
            'cohort_callbacks': raw.get('cohort_callbacks'),
            'cohort_cross_numa': raw.get('cohort_cross_numa'),
            'cohort_migration_rate_percent': None,
            'roles': copy.deepcopy(raw.get('roles')),
            'roles_source_file': run['source_file'] if raw.get('roles') else None,
            'roles_source_pointer': run['source_pointer'] + '/roles' if raw.get('roles') else None,
            'migration_definition': 'source != chosen, user-space placement between executor child pools, not Linux sched_migrate_task',
            'real_time_cpu_utilization': None,
        })
        if batch == '4020' and raw.get('flow'):
            run['roles'] = copy.deepcopy(raw['flow']['roles'])
            run['roles_source_file'] = run['source_file']
            run['roles_source_pointer'] = run['source_pointer'] + '/flow/roles'
            run['cohort_callbacks'] = sum(row['n'] for row in run['roles'])
            run['cohort_migrations'] = sum(row['source_ne_chosen'] for row in run['roles'])
            run['cohort_cross_numa'] = None  # Not inferred from a policy switch when no explicit count was retained.
            run['task_wait_p99_ms'] = raw['cohort']['task_wait_p99_ms']
            business_roles = ('first-wave stage0', 'later-wave stage0', 'ordinary successors')
            cpu_total = sum(row['n'] * row['cpu_mean_ms'] for row in run['roles']
                            if row['role'] in business_roles)
            run['operator_cpu_mean_ms'] = cpu_total / (168 * run['cohort_completed'])
            run['operator_cpu_mean_note'] = 'derived from rounded role means; do not present as unrounded raw observation'
        if batch == '4018':
            run['whole_duration_with_drain_s'] = raw['whole_report']['wall_plus_drain_s']
            run['whole_process_average_cpu_percent_of_96'] = None
            run['whole_process_average_cpu_source'] = {
                'file': run['source_file'],
                'cpu_pointer': run['source_pointer'] + '/whole_execution_line/process_cpu_s',
                'wall_pointer': run['source_pointer'] + '/whole_report/wall_plus_drain_s',
                'formula': None,
                'note': 'DO NOT derive utilization: process CPU starts before helper/sampler startup; wall duration begins at origin set 10ms after startup. Start boundaries differ, and exact CPU interval wall duration is not recorded.',
            }
        if batch == '4011' and run['trace']:
            name = f"seed-{run['seed']}-{run['strategy']}-trace1"
            source = extra['runs'][name]
            values = cells(source['request_summary'])
            if float(values[3]) != run['mean_ms'] or float(values[5]) != run['p99_ms']:
                raise ValueError(f'{name}: request summary differs from reviewed comparisons')
            run['task_wait_p99_ms'] = float(values[6])
            run['operator_cpu_mean_ms'], run['operator_wall_mean_ms'] = map(float, values[7].split('/'))
            run['cohort_migrations'], run['cohort_callbacks'] = map(int, values[8].split('/'))
            run['cohort_cross_numa'] = int(values[9])
            run['roles'] = extra_roles(source['role_rows'])
            run['roles_source_file'] = EXTRA_FILE
            run['roles_source_pointer'] = f'/runs/{name}/role_rows'
            run['additional_metric_source'] = {
                'file': EXTRA_FILE,
                'pointer': f'/runs/{name}/request_summary',
                'original_log': extra['source_log'],
                'original_line': source['request_summary']['line'],
                'original_log_sha256': extra['source_sha256'],
                'columns': {'task_wait_p99_ms': 7, 'operator_cpu_wall_ms': 8, 'migration_callbacks': 9},
                'note': 'one-based Markdown table columns; rounded terminal values',
            }
        if run['cohort_callbacks']:
            run['cohort_migration_rate_percent'] = 100 * run['cohort_migrations'] / run['cohort_callbacks']
        if run['roles']:
            if sum(row['n'] for row in run['roles']) != run['cohort_callbacks']:
                raise ValueError(f"{run['id']}: callback denominator differs from role sum")
            if sum(row['source_ne_chosen'] for row in run['roles']) != run['cohort_migrations']:
                raise ValueError(f"{run['id']}: migration count differs from role sum")
            for role in run['roles']:
                role['migration_percent_derived'] = 100 * role['source_ne_chosen'] / role['n']
    sources = {
        batch: copy.deepcopy(base['sources'][batch]) for batch in SELECTED_BATCHES
    }
    sources['4011_routing_excerpt'] = {
        'file': EXTRA_FILE, 'sha256': digest(ROOT / EXTRA_FILE),
        'upstream_log_sha256': extra['source_sha256'],
    }
    sources['comparisons'] = {
        'file': 'reports/final_review/data/comparisons.json',
        'sha256': digest(HERE / 'data/comparisons.json'),
    }
    return {
        'schema': 'scenario-results-v1',
        'evidence': 'existing reviewed terminal summaries, source-linked; no new target execution or raw CSV reanalysis',
        'sources': sources, 'common': COMMON, 'scenarios': SCENARIOS, 'runs': runs,
        'cpu_time_series_audit': CPU_AUDIT,
    }


def main():
    output = HERE / 'data/scenario_results.json'
    data = build()
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(f'{output}: {len(data["runs"])} source-linked runs; no attachments required')


if __name__ == '__main__':
    main()

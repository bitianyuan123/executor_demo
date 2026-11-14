#!/usr/bin/env python3
"""Audit declared contrasts and estimate paired, whole-run latency differences.

Only the standard library is needed. Stage 3 tasks and stage 4 requests remain
separate. A confidence interval resamples entire seed pairs, never task rows.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics


def quantile(values, p=.99):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def digest(values):
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def read_csv(path):
    with path.open() as handle:
        return list(csv.DictReader(handle))


def options(command):
    result = {}
    for i in range(1, len(command), 2):
        if i + 1 >= len(command) or not command[i].startswith('--'):
            raise ValueError('expected --option value command format')
        key, value = command[i][2:], command[i + 1]
        if key in ('output', 'seed'):
            continue
        try:
            value = float(value)
        except ValueError:
            pass
        result[key] = value  # Last option wins, as in both binaries.
    return result


def resolve_run(path, manifest):
    path = Path(path)
    for candidate in (path, manifest.parent / path.name, manifest.parent / path):
        if (candidate / 'summary.json').exists():
            return candidate
    raise ValueError(f'run directory not found: {path}')


def entries(manifest):
    data = json.loads(manifest.read_text())
    rows = data['runs'] if isinstance(data, dict) else data
    result = {}
    for row in rows:
        name = row.get('case', row.get('name'))
        repeat = row.get('repetition', row.get('repeat'))
        key = (name, int(row['seed']))
        if key in result:
            raise ValueError(f'duplicate case/seed (not independent repetitions): {key}')
        result[key] = {**row, 'case': name, 'repeat': repeat,
                       'path': resolve_run(row.get('path', row.get('directory')), manifest),
                       'binary_sha256': row.get('binary_sha256', data.get('binary_sha256') if isinstance(data, dict) else None)}
    return result


def slope(points):
    if len(points) < 3:
        return None
    xs, ys = zip(*points)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in points) / denom if denom else None


def summarize(entry, stage, start_s, end_s):
    path = entry['path']
    meta = json.loads((path / 'metadata.json').read_text())
    summary = json.loads((path / 'summary.json').read_text())
    if entry['returncode'] != 0 or not summary.get('conservation_ok') or summary.get('io_failed'):
        raise ValueError(f'invalid or failed run: {path}')
    independent = stage == 'independent'
    metadata_seed = meta.get('seed') if independent else meta.get('config', {}).get('seed')
    if metadata_seed is not None and int(metadata_seed) != int(entry['seed']):
        raise ValueError('manifest seed differs from effective metadata seed')
    if independent:
        if meta.get('stage') != 3:
            raise ValueError('expected independent task data')
        rows = read_csv(path / 'tasks.csv')
        rows.sort(key=lambda r: int(r['task']))
        if [int(r['task']) for r in rows] != list(range(len(rows))):
            raise ValueError('duplicate, missing or noncontiguous task IDs')
        if len(rows) != summary['planned_tasks']:
            raise ValueError('truncated task data')
        plan_key, submit_key, finish_key = 'planned_ns', 'submit_ns', 'end_ns'
        service = [int(r['target_cpu_us']) for r in rows]
        graph = None
        duration = float(meta['duration_seconds'])
        start_offset = 0
        effective_union = sorted({cpu for mask in meta.get('effective_masks', [meta['allowed_cpus']]) for cpu in mask})
    else:
        if meta.get('phase') != 'completed' or meta.get('schema_version', 0) < 2:
            raise ValueError('expected complete DAG schema 2 data')
        rows = read_csv(path / 'requests.csv')
        rows.sort(key=lambda r: int(r['request_id']))
        if [int(r['request_id']) for r in rows] != list(range(len(rows))):
            raise ValueError('duplicate, missing or noncontiguous request IDs')
        tasks = read_csv(path / 'tasks.csv')
        if len(rows) != summary['offered'] or len(tasks) != summary['task_rows']:
            raise ValueError('truncated request/task data')
        if rows and not tasks:
            raise ValueError('DAG workload audits require task-log=1')
        plan_key, submit_key, finish_key = 'scheduled_ns', 'offered_ns', 'finish_ns'
        tasks.sort(key=lambda t: (int(t['request_id']), int(t['node_id']), int(t['stage'])))
        task_ids = [(t['request_id'], t['node_id'], t['stage']) for t in tasks]
        if len(set(task_ids)) != len(task_ids) or any(int(t['request_id']) >= len(rows) or int(t['request_id']) < 0 for t in tasks):
            raise ValueError('duplicate or orphan DAG CPU stage IDs')
        service = [[t['request_id'], t['node_id'], t['stage'], int(t['work_target_us'])] for t in tasks]
        graph = [[t['request_id'], t['node_id'], t['stage'], t['predecessors'], t['kind'], t.get('rpc_delay_target_us')] for t in tasks]
        duration = float(meta['config']['duration'])
        start_offset = int(meta.get('arrival_origin_ns', rows[0][plan_key] if rows else 0))
        effective_union = sorted({cpu for group in meta.get('groups', [{'effective_cpus': meta['allowed_cpus']}]) for cpu in group['effective_cpus']})
    end_s = duration if end_s is None else end_s
    if not 0 <= start_s < end_s <= duration:
        raise ValueError(f'invalid measurement window [{start_s}, {end_s}) for duration {duration}')
    # Preserve a Poisson first-arrival delay using schema 3's explicit origin.
    # Legacy schema 2 has regular arrivals and the first scheduled time is origin.
    plans = [int(r[plan_key]) - start_offset for r in rows]
    cohort = [r for r, planned in zip(rows, plans) if start_s * 1e9 <= planned < end_s * 1e9]
    statuses = dict(Counter(r['status'] for r in cohort))
    all_statuses = dict(Counter(r['status'] for r in rows))
    done = [r for r in cohort if r['status'] == 'completed']
    completed_all = [r for r in rows if r['status'] == 'completed']
    expected_done = summary['completed_tasks' if independent else 'completed']
    if len(completed_all) != expected_done:
        raise ValueError('completed row count differs from summary')
    for r in completed_all:
        if not int(r[plan_key]) <= int(r[submit_key]) <= int(r[finish_key]):
            raise ValueError('nonmonotonic lifecycle timestamps')
        if independent and not int(r[submit_key]) <= int(r['start_ns']) <= int(r[finish_key]):
            raise ValueError('nonmonotonic callback timestamps')
    metric_values = {
        'planned_latency_ms': [(int(r[finish_key]) - int(r[plan_key])) / 1e6 for r in done],
        'submitted_latency_ms': [(int(r[finish_key]) - int(r[submit_key])) / 1e6 for r in done],
        'admission_lag_ms': [(int(r[submit_key]) - int(r[plan_key])) / 1e6 for r in cohort],
    }
    if independent:
        metric_values.update({
            'submit_to_start_ms': [(int(r['start_ns']) - int(r['submit_ns'])) / 1e6 for r in done],
            'callback_wall_ms': [(int(r['end_ns']) - int(r['start_ns'])) / 1e6 for r in done],
        })
    metrics = {name + suffix: (statistics.fmean(values) if suffix == '_mean' else quantile(values)) if values else None
               for name, values in metric_values.items() for suffix in ('_mean', '_p99')}
    def intervals(lo, hi):
        admitted = [r for r in rows if lo * 1e9 <= int(r[submit_key]) - start_offset < hi * 1e9]
        arrivals = [r for r, planned in zip(rows, plans) if lo * 1e9 <= planned < hi * 1e9]
        finished = [r for r in completed_all if lo * 1e9 <= int(r[finish_key]) - start_offset < hi * 1e9]
        lats = [(int(r[finish_key]) - int(r[plan_key])) / 1e6 for r in arrivals if r['status'] == 'completed']
        return {'planned_rate': len(arrivals) / (hi - lo), 'submitted_rate': len(admitted) / (hi - lo),
                'completed_rate': len(finished) / (hi - lo),
                'cohort_mean_latency_ms': statistics.fmean(lats) if lats else None}
    mid = (start_s + end_s) / 2
    halves = [intervals(start_s, mid), intervals(mid, end_s)]
    def outstanding(at):
        t = start_offset + at * 1e9
        return sum(int(r[submit_key]) <= t < int(r[finish_key]) for r in completed_all)
    pool_rows = read_csv(path / 'pools.csv')
    sampled = {}
    queue_key = 'native_queue' if independent else 'queue_depth'
    for r in pool_rows:
        t = (int(r['time_ns']) - start_offset) / 1e9
        if start_s <= t < end_s and float(r[queue_key]) >= 0:
            sampled[t] = sampled.get(t, 0) + float(r[queue_key])
    queue_points = sorted(sampled.items())
    first_q = [q for t, q in queue_points if t < mid]
    last_q = [q for t, q in queue_points if t >= mid]
    last_finish = max([int(r[finish_key]) - start_offset for r in completed_all], default=0) / 1e9
    drain_s = max(0, last_finish - duration)
    failure_count = sum(n for state, n in all_statuses.items() if state != 'completed')
    warnings = []
    if not entry['binary_sha256']:
        warnings.append('legacy_manifest_has_no_executable_hash')
    if failure_count:
        warnings.append('rejected_canceled_or_failed: successful latency is survivor-only')
    if summary.get('sampling_truncated') or summary.get('sampling_failed'):
        warnings.append('incomplete_pool_sampling')
    if metrics['admission_lag_ms_p99'] is not None and metrics['admission_lag_ms_p99'] > 1:
        warnings.append('generator_or_submission_lag_p99_above_1ms')
    # A transparent diagnostic, NOT a statistical stationarity test. Raw values
    # are retained so long tails and burst phases can be assessed independently.
    early, late = (h['cohort_mean_latency_ms'] for h in halves)
    if early is not None and late is not None and late > early * 1.25 + 1:
        warnings.append('later_cohort_latency_growth: inspect overload_or_nonstationarity')
    if drain_s > (end_s - start_s) * .1:
        warnings.append('substantial_drain_after_planned_horizon')
    if end_s - start_s < 30:
        warnings.append('short_window: no_sustainable_capacity_claim')
    if independent and not meta.get('same_cpu_union', False):
        warnings.append('explicit_and_allowed_cpu_union_differ')
    return {
        'path': str(path), 'case': entry['case'], 'seed': int(entry['seed']), 'repeat': entry['repeat'],
        'binary_sha256': entry['binary_sha256'],
        'options': {**options(entry['command']), '_external_proc_interval_seconds': entry.get('proc_interval_seconds')},
        'allowed_cpus': meta['allowed_cpus'], 'window_s': [start_s, end_s],
        'effective_cpu_union': effective_union,
        'cohort_statuses': statuses, 'whole_run_statuses': all_statuses, 'completed': len(done),
        'expected_top_1pct_count': len(done) * .01, 'metrics': metrics, 'warnings': warnings,
        'diagnostics': {'halves': halves, 'completed_outstanding_at_start_mid_end': [outstanding(t) for t in (start_s, mid, end_s)],
                        'queue_sum_slope_per_second': slope(queue_points),
                        'queue_sum_half_means': [statistics.fmean(q) if q else None for q in (first_q, last_q)],
                        'pool_sample_times': len(queue_points), 'drain_after_horizon_s': drain_s,
                        'whole_run_incomplete': failure_count},
        'workload': {'units': len(rows), 'service_sha256': digest(service),
                     'total_target_cpu_us': sum(service) if independent else sum(t[-1] for t in service),
                     'arrival_sha256': digest(plans), 'graph_sha256': digest(graph),
                     'cohort_units': len(cohort),
                     'cohort_ids_sha256': digest([r['task' if independent else 'request_id'] for r in cohort]),
                     'cohort_target_cpu_us': sum(int(r['target_cpu_us' if independent else 'planned_cpu_us']) for r in cohort)},
    }


def contract_errors(a, b, contrast):
    errors = []
    keys = set(a['options']) | set(b['options'])
    changed = sorted(k for k in keys if a['options'].get(k) != b['options'].get(k))
    undeclared = set(changed) - set(contrast.get('allowed_changes', []))
    if undeclared:
        errors.append(f'undeclared option changes: {sorted(undeclared)}')
    if a['allowed_cpus'] != b['allowed_cpus']:
        errors.append('different allowed CPU resource sets')
    if a['effective_cpu_union'] != b['effective_cpu_union']:
        errors.append('different effective worker CPU resource unions')
    if a['binary_sha256'] and b['binary_sha256'] and a['binary_sha256'] != b['binary_sha256']:
        errors.append('different executable hashes')
    wa, wb = a['workload'], b['workload']
    contract = contrast.get('workload', {})
    service = contract.get('service', 'exact')
    arrival = contract.get('arrival', 'exact')
    graph = contract.get('graph', 'exact')
    if contract.get('same_cohort_ids', False) and wa['cohort_ids_sha256'] != wb['cohort_ids_sha256']:
        errors.append('measurement cohort IDs differ')
    if 'cohort_total_relative_tolerance' in contract:
        tolerance = float(contract['cohort_total_relative_tolerance'])
        if not 0 <= tolerance <= .1:
            raise ValueError('cohort_total_relative_tolerance must be in [0, .1]')
        if abs(wa['cohort_target_cpu_us'] - wb['cohort_target_cpu_us']) > max(1, wa['cohort_target_cpu_us']) * tolerance:
            errors.append('measurement cohort nominal work differs beyond declared tolerance')
    if service not in ('exact', 'equal_total', 'unrestricted') or arrival not in ('exact', 'count', 'unrestricted') or graph not in ('exact', 'unrestricted'):
        raise ValueError('unknown workload contract')
    if service == 'exact' and wa['service_sha256'] != wb['service_sha256']:
        errors.append('service targets or RPC stage layout differ')
    if service == 'equal_total':
        tolerance = float(contract.get('total_relative_tolerance', .001))
        if not 0 <= tolerance <= .1:
            raise ValueError('total_relative_tolerance must be in [0, .1]')
        if abs(wa['total_target_cpu_us'] - wb['total_target_cpu_us']) > max(1, wa['total_target_cpu_us']) * tolerance:
            errors.append('total nominal work differs beyond declared tolerance')
    if arrival in ('exact', 'count') and wa['units'] != wb['units']:
        errors.append('offered task/request counts differ')
    if arrival == 'exact' and wa['arrival_sha256'] != wb['arrival_sha256']:
        errors.append('planned arrival schedules differ')
    if graph == 'exact' and wa['graph_sha256'] != wb['graph_sha256']:
        errors.append('dependency/RPC graph differs')
    if a['diagnostics']['whole_run_incomplete'] or b['diagnostics']['whole_run_incomplete']:
        errors.append('incomplete/rejected work: no successful-only latency inference')
    if not a['completed'] or not b['completed']:
        errors.append('empty completed measurement cohort')
    return errors, changed


def paired_estimate(values, seed, draws, minimum_pairs):
    if not values:
        return {'mean': None, 'ci95': None, 'pairs': 0}
    ci = None
    if len(values) >= minimum_pairs:
        rng = random.Random(seed)
        means = [statistics.fmean(rng.choices(values, k=len(values))) for _ in range(draws)]
        ci = [quantile(means, .025), quantile(means, .975)]
    return {'mean': statistics.fmean(values), 'ci95': ci, 'pairs': len(values),
            'min': min(values), 'max': max(values), 'per_pair': values}


def compare(manifest, spec):
    if spec['stage'] not in ('independent', 'dag'):
        raise ValueError('stage must be independent or dag')
    minimum = int(spec.get('minimum_pairs', 5))
    tail = int(spec.get('minimum_tail_observations', 20))
    draws = int(spec.get('bootstrap_draws', 5000))
    if minimum < 5 or tail < 1 or draws < 1000:
        raise ValueError('minimum_pairs>=5, tail>=1 and bootstrap_draws>=1000 required')
    source = entries(manifest)
    start = float(spec.get('window', {}).get('start_s', 0))
    end = spec.get('window', {}).get('end_s')
    cache, comparisons = {}, []
    for contrast in spec['comparisons']:
        baseline, candidate = contrast['baseline'], contrast['candidate']
        if baseline == candidate:
            raise ValueError('baseline and candidate must differ')
        seeds_a = {seed for name, seed in source if name == baseline}
        seeds_b = {seed for name, seed in source if name == candidate}
        if not seeds_a or seeds_a != seeds_b:
            raise ValueError(f'missing/unpaired seeds: {baseline} {sorted(seeds_a)}, {candidate} {sorted(seeds_b)}')
        pairs = []
        for seed in sorted(seeds_a):
            for name in (baseline, candidate):
                key = (name, seed)
                if key not in cache:
                    cache[key] = summarize(source[key], spec['stage'], start, None if end is None else float(end))
            a, b = cache[(baseline, seed)], cache[(candidate, seed)]
            errors, changed = contract_errors(a, b, contrast)
            pairs.append({'seed': seed, 'baseline': a['path'], 'candidate': b['path'], 'errors': errors,
                          'changed_options': changed, 'tail_sample_adequate': min(a['completed'], b['completed']) * .01 >= tail,
                          'cohort_work_difference_pct': 100 * (b['workload']['cohort_target_cpu_us'] / a['workload']['cohort_target_cpu_us'] - 1) if a['workload']['cohort_target_cpu_us'] else None,
                          'differences': {key: b['metrics'][key] - value if value is not None and b['metrics'][key] is not None else None
                                          for key, value in a['metrics'].items()},
                          'relative_differences_pct': {key: 100 * (b['metrics'][key] / value - 1) if value and b['metrics'][key] is not None else None
                                                       for key, value in a['metrics'].items()}})
        invalid = any(pair['errors'] for pair in pairs)
        estimates = {}
        if not invalid:
            for key in pairs[0]['differences']:
                adequate = not key.endswith('_p99') or all(p['tail_sample_adequate'] for p in pairs)
                vals = [p['differences'][key] for p in pairs]
                rels = [p['relative_differences_pct'][key] for p in pairs]
                estimate = paired_estimate([v for v in vals if v is not None], 420, draws, minimum if adequate else len(pairs) + 1)
                estimate['relative_pct'] = paired_estimate([v for v in rels if v is not None], 420, draws, minimum if adequate else len(pairs) + 1)
                estimate['tail_sample_adequate'] = adequate
                estimates[key] = estimate
        comparisons.append({'name': contrast.get('name', f'{baseline} vs {candidate}'),
                            'baseline': baseline, 'candidate': candidate, 'contract': contrast,
                            'status': 'invalid_contract' if invalid else 'descriptive_only' if len(pairs) < minimum else 'paired_estimate',
                            'pairs': pairs, 'estimates': estimates})
    return {'schema': 1, 'stage': spec['stage'], 'specification': spec, 'comparisons': comparisons,
            'runs': list(cache.values()), 'interpretation': [
                'Difference is candidate minus baseline; negative latency difference is lower latency.',
                'Estimand: equally weighted mean of per-run means or per-run p99 differences, not pooled p99.',
                'CI: exploratory percentile bootstrap over independent seed pairs, not task/request rows.',
                'At least five pairs enables CI; more repeats and independent batches are needed for small improvements.',
                'Tail threshold is a sample sufficiency heuristic, not proof of p99 precision or independent observations.',
                'Short windows, queue trends, lag and drain are diagnostics; this tool does not certify stationarity.',
                'Multiple contrasts are exploratory without simultaneous inference or a separately confirmed primary contrast.',
                'Same nominal CPU budget is not identical useful work in CPU-clock mode; use fixed iterations for locality.',
            ]}


def markdown(result):
    lines = ['# 预声明对照与整轮配对分析', '',
             '差值为 candidate − baseline；负值表示时延降低。区间重采样单位是完整 seed 配对，不是任务或请求。', '',
             '|对照|有效性|配对数|planned 时延均值差 ms|探索性95%区间|p99区间可报告|',
             '|---|---|---:|---:|---|---|']
    for c in result['comparisons']:
        m = c['estimates'].get('planned_latency_ms_mean', {})
        p = c['estimates'].get('planned_latency_ms_p99', {})
        value = m.get('mean')
        lines.append(f"|{c['name']}|{c['status']}|{len(c['pairs'])}|{value if value is not None else 'n/a'}|{m.get('ci95')}|{'是（仍为探索性）' if p.get('ci95') else '否，见样本门槛'}|")
    lines += ['', '## 每轮有效性诊断', '', '|轮次|cohort完成数|顶端1%名义样本|前/后半均值ms|drain s|诊断|', '|---|---:|---:|---|---:|---|']
    for run in result['runs']:
        d = run['diagnostics']
        means = [round(h['cohort_mean_latency_ms'], 4) if h['cohort_mean_latency_ms'] is not None else None for h in d['halves']]
        lines.append(f"|{run['case']} seed={run['seed']}|{run['completed']}|{run['expected_top_1pct_count']:.2f}|{means}|{d['drain_after_horizon_s']:.4f}|{'; '.join(run['warnings']) or '无启发式告警；不等于证明稳态'}|")
    lines += ['', '## 解释限制', '', *['- ' + s for s in result['interpretation']], '',
              '完整配对差值、契约违规、工作量哈希、拒绝/删失、到达/完成率、队列趋势见同名 JSON。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.manifest, json.loads(args.spec.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    args.output.with_suffix('.md').write_text(markdown(result))
    return 2 if any(c['status'] == 'invalid_contract' for c in result['comparisons']) else 0


if __name__ == '__main__':
    raise SystemExit(main())

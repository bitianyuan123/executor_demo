#!/usr/bin/env python3
"""Print stage-4 evidence from existing files, without running any workload.

The window comes from a declared DAG comparison spec or two explicit CLI bounds.
Task statistics retain every logged CPU stage belonging to the request cohort,
including stages that finish after the window. They are not request path sums.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics

from compare_runs import quantile, read_csv, summarize
from prepare_target_validation import compact


def read_json(path):
    return json.loads(path.read_text())


def fmt(value):
    return '?' if value is None else f'{value:.3f}'


def mean(values):
    return statistics.fmean(values) if values else None


def pair(values):
    return f'{fmt(mean(values))}/{fmt(quantile(values))}'


def numeric(row, key):
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f'nonfinite {key}')
    return value


TRANSITIONS = ('initial_fork', 'later_fork', 'ordinary_successor', 'join', 'rpc_resume')


def transition(task):
    """Classify recorded CPU stages only; absent descriptors are not defaults."""
    kind, stage = task.get('kind'), task.get('stage')
    if kind == 'join' and stage == '0':
        return 'join'
    if kind != 'operator':
        return 'unknown'
    if stage == '1':
        return 'rpc_resume'
    if stage != '0':
        return 'unknown'
    depth = task.get('depth')
    if depth in (None, ''):
        return 'unknown'
    if int(depth) > 0:
        return 'ordinary_successor'
    if int(depth) != 0 or task.get('wave') in (None, ''):
        return 'unknown'
    wave = int(task['wave'])
    return 'initial_fork' if wave == 0 else 'later_fork' if wave > 0 else 'unknown'


def known_flag_count(tasks, key, reason=None):
    """Preserve unknown telemetry separately from known false flags."""
    known, count = 0, 0
    for task in tasks:
        value = task.get(key)
        if value in (None, ''):
            continue
        known += 1
        if reason is not None:
            count += value == reason
        else:
            if value not in ('0', '1'):
                raise ValueError(f'invalid {key} flag')
            count += int(value)
    return {'count': count, 'known': known, 'unknown': len(tasks)-known}


def flag_text(count):
    if not count['unknown']:
        return str(count['count'])
    return f'unknown[known={count["known"]},true={count["count"]},missing={count["unknown"]}]'


def routing_transitions(tasks):
    grouped = {kind: [] for kind in TRANSITIONS}
    for task in tasks:
        grouped.setdefault(transition(task), []).append(task)
    result = {}
    for kind, group in grouped.items():
        result[kind] = {
            'tasks': len(group),
            # The CSV stores Decision::migrated (chosen != source), not rerouted
            # (chosen != preferred). Borrow returning home may therefore migrate.
            'migrated_source': known_flag_count(group, 'migrated'),
            'cross_numa': known_flag_count(group, 'cross_numa'),
            'return_home_reason': known_flag_count(group, 'move_reason', 'return_home'),
            'chosen_groups': dict(Counter(t.get('chosen_group') or 'unknown' for t in group)),
            'move_reasons': dict(Counter(t.get('move_reason') or 'unknown' for t in group)),
            'waits': [(int(t['start_ns'])-int(t['submit_ns']))/1e6 for t in group],
        }
    return result


def config_value(config, key):
    value = config.get(key)
    return 'unknown' if value is None or value == '' else value


def window(root, phase, spec=None, start=None, end=None):
    if (start is None) != (end is None):
        raise ValueError('provide both --warmup-seconds and --steady-end-seconds')
    if start is not None:
        source = 'explicit CLI bounds'
    else:
        candidates = [spec] if spec else [root/'plan'/f'{phase}-contrasts.json', root/f'{phase}-contrasts.json']
        selected = next((path for path in candidates if path.is_file()), None)
        if selected is None:
            raise ValueError('measurement window absent; provide --spec or both explicit window bounds')
        data = read_json(selected)
        if data.get('stage') != 'dag':
            raise ValueError('comparison spec must have stage=dag')
        start, end = data['window']['start_s'], data['window']['end_s']
        source = str(selected)
    start, end = float(start), float(end)
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError('invalid measurement window')
    return start, end, source


def locate_run(manifest, row):
    raw = Path(row['directory'])
    candidates = (manifest.parent/raw.name, raw, manifest.parent/raw)
    return next((p for p in candidates if (p/'metadata.json').is_file()), candidates[0])


def details(path, metadata, summary, start, end):
    requests, tasks = read_csv(path/'requests.csv'), read_csv(path/'tasks.csv')
    origin = int(metadata.get('arrival_origin_ns', min((int(r['scheduled_ns']) for r in requests), default=0)))
    cohort = {r['request_id']: r for r in requests
              if start*1e9 <= int(r['scheduled_ns'])-origin < end*1e9}
    counts = Counter(r['status'] for r in requests)
    for status in ('completed', 'rejected', 'censored', 'failed'):
        if status in summary and counts[status] != summary[status]:
            raise ValueError(f'{status} request count differs from summary')
    selected = [t for t in tasks if t['request_id'] in cohort]
    done = [t for t in selected if t['status'] == 'completed']
    waits, walls, cpus, zero_waits, first_waits = [], [], [], [], []
    unknown_queue = 0
    for task in done:
        request = cohort[task['request_id']]
        submit, begin, finish = (int(task[k]) for k in ('submit_ns', 'start_ns', 'finish_ns'))
        if not int(request['scheduled_ns']) <= submit <= begin <= finish:
            raise ValueError('nonmonotonic task timestamps')
        if request['status'] == 'completed' and finish > int(request['finish_ns']):
            raise ValueError('task finishes after its completed request')
        wait, wall, cpu = (begin-submit)/1e6, (finish-begin)/1e6, numeric(task, 'cpu_us')/1000
        if cpu < 0:
            raise ValueError('negative logged task CPU')
        waits.append(wait); walls.append(wall); cpus.append(cpu)
        q = numeric(task, 'queue_before') if task.get('queue_before') not in (None, '') else -1
        if q < 0:
            unknown_queue += 1
        elif q == 0:
            zero_waits.append(wait)
        if transition(task) == 'initial_fork':
            first_waits.append(wait)
    groups = {int(g['group']) for g in metadata.get('groups', [])}
    snapshots = defaultdict(dict)
    for row in read_csv(path/'pools.csv'):
        when = (int(row['time_ns'])-origin)/1e9
        if start <= when < end:
            group = int(row['group'])
            if group in snapshots[when]:
                raise ValueError('duplicate pool/time sample')
            snapshots[when][group] = row
    load = {key: [] for key in ('queue_depth', 'pending', 'inflight')}
    incomplete = 0
    for snapshot in snapshots.values():
        if not groups or set(snapshot) != groups:
            incomplete += 1
            continue
        for key in load:
            values = [numeric(row, key) if row.get(key) not in (None, '') else -1 for row in snapshot.values()]
            if all(value >= 0 for value in values):
                load[key].append(sum(values))
    worker_file = path/'workers.csv'
    workers = Counter(int(r['group']) for r in read_csv(worker_file)) if worker_file.is_file() else None
    return {'statuses': dict(Counter(t['status'] for t in selected)), 'completed': len(done),
            'kinds': dict(Counter(t.get('kind', 'unknown') for t in done)), 'waits': waits,
            'walls': walls, 'cpus': cpus, 'zero_waits': zero_waits, 'unknown_queue': unknown_queue,
            'first_waits': first_waits, 'migrations': known_flag_count(done, 'migrated'),
            'cross_numa': known_flag_count(done, 'cross_numa'), 'routing': routing_transitions(done), 'load': load,
            'snapshot_times': len(snapshots), 'incomplete_snapshots': incomplete,
            'workers': workers, 'origin': origin}


def phase_report(root, phase, spec=None, start=None, end=None, offset=0, limit=16, emit=print):
    if not phase.startswith('s4-') or '/' in phase or phase in ('.', '..'):
        raise ValueError('phase must name one stage-4 directory, e.g. s4-baseline')
    manifest = root/phase/'manifest.json'
    emit(f'=== {phase} ===')
    try:
        rows = read_json(manifest)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('expected the DAG runner manifest array')
        start, end, source = window(root, phase, spec, start, end)
    except (OSError, ValueError, TypeError, KeyError) as error:
        emit(f'ERROR: {type(error).__name__}: {error}; if still running, retry after completion')
        return False
    planned = None
    try:
        planned = read_json(root/'plan'/'plan.json')['phases'][phase]['runs']
    except (OSError, ValueError, TypeError, KeyError):
        pass
    emit(f'MATRIX recorded={len(rows)} planned={planned if planned is not None else "unknown"} '
         f'window=[{start:g},{end:g})s source={source}')
    valid = True
    if planned is None:
        emit('NOTE: plan completion unconfirmed; only recorded attempts can be checked')
    elif planned != len(rows):
        emit('ERROR: recorded attempts differ from planned count; active or missing runs are not included')
        valid = False
    keys = [(r.get('name'), r.get('seed')) for r in rows]
    if len(keys) != len(set(keys)):
        emit('ERROR: duplicate case/seed entries')
        valid = False
    failed = sum(r.get('returncode') != 0 for r in rows)
    emit(f'ATTEMPTS nonzero_or_missing_rc={failed}; detailed artifact checks cover displayed rows only')
    valid = valid and not failed
    selected = rows[offset:offset+limit]
    if not selected:
        emit(f'NO ROWS at offset={offset}; recorded={len(rows)}; no detailed checks performed')
        return False
    emit(f'ROWS {offset+1}..{offset+len(selected)} of {len(rows)}; request cohort uses scheduled arrivals; '
         'task rows belong to that cohort even if they finish after the window')
    for row in selected:
        rc, name = row.get('returncode', 'missing'), row.get('name', '?')
        emit(f'RUN {name} repeat={row.get("repeat", "?")} seed={row.get("seed", "?")} rc={rc} '
             f'binary_sha256={row.get("binary_sha256", "unknown")}')
        if rc != 0:
            emit(f'  FAILED/INCOMPLETE; inspect {manifest.parent/Path(row.get("directory", "unknown")).name/"console.log"}')
            continue
        try:
            path = locate_run(manifest, row)
            entry = {**row, 'path': path, 'case': name, 'binary_sha256': row.get('binary_sha256')}
            result = summarize(entry, 'dag', start, end)
            meta, summary = read_json(path/'metadata.json'), read_json(path/'summary.json')
            extra = details(path, meta, summary, start, end)
            config, metrics, diagnostic = meta['config'], result['metrics'], result['diagnostics']
            masks = ';'.join(compact(g['effective_cpus']) if g['effective_cpus'] else '?' for g in meta.get('groups', []))
            same_union = result['effective_cpu_union'] == sorted(meta['allowed_cpus'])
            emit(f'  CONFIG backend={meta.get("backend", "?")} policy={config.get("policy")} '
                 f'affinity={config.get("affinity")} threads/pool={config.get("threads_per_pool")} '
                 f'metric={config.get("metric")} queue={config.get("queue")} wake_us={config.get("wake_us")} '
                 f'same_union={same_union} masks={masks} numa_verified={meta.get("numa_labels_verified", "?")}')
            emit('  ROUTING_CONFIG ' + ' '.join(f'{key}={config_value(config, key)}' for key in
                 ('balance_forks', 'candidate_scope', 'estimate_us', 'min_gain_us', 'relative_gain', 'cross_numa_penalty_us'))
                 + ' capacities=' + json.dumps({str(g['group']): config_value(g, 'capacity')
                                               for g in meta.get('groups', [])}, sort_keys=True))
            emit(f'  WORK graph={config.get("graph")} widths={config.get("wave_widths")} depths={config.get("wave_depths")} '
                 f'qps={config.get("qps")} arrival={config.get("arrival")} cpu_us={config.get("cpu_us")} '
                 f'join_us={config.get("join_us")} rpc_prob={config.get("rpc_prob")} mode={config.get("work_mode")} '
                 f'nominal_offered_cpu_s/s={fmt(result["workload"]["cohort_target_cpu_us"]/1e6/(end-start))}')
            emit(f'  REQUEST_STATUS whole={json.dumps(result["whole_run_statuses"], sort_keys=True)} '
                 f'cohort={json.dumps(result["cohort_statuses"], sort_keys=True)} '
                 f'conservation={summary.get("conservation_ok")} io_failed={summary.get("io_failed")} '
                 f'drain_timed_out={summary.get("drain_timed_out")} tail_1pct_nominal_n={fmt(result["expected_top_1pct_count"])}')
            emit('  REQUEST_LAT ms planned_mean/p99=' + '/'.join(fmt(metrics[k]) for k in ('planned_latency_ms_mean', 'planned_latency_ms_p99'))
                 + ' admitted_mean/p99=' + '/'.join(fmt(metrics[k]) for k in ('submitted_latency_ms_mean', 'submitted_latency_ms_p99'))
                 + ' admission_lag_mean/p99=' + '/'.join(fmt(metrics[k]) for k in ('admission_lag_ms_mean', 'admission_lag_ms_p99')))
            emit(f'  TASK_COHORT statuses={json.dumps(extra["statuses"], sort_keys=True)} kinds={json.dumps(extra["kinds"], sort_keys=True)} '
                 f'wait_mean/p99_ms={pair(extra["waits"])} work_wall_mean/p99_ms={pair(extra["walls"])} '
                 f'work_cpu_mean_ms={fmt(mean(extra["cpus"]))} migrated_source={flag_text(extra["migrations"])} '
                 f'cross_numa={flag_text(extra["cross_numa"])}')
            emit(f'  INITIAL_FORK task_n={len(extra["first_waits"])} wait_mean/p99_ms={pair(extra["first_waits"])} '
                 f'QUEUE_ZERO task_n={len(extra["zero_waits"])} wait_gt1ms={sum(v > 1 for v in extra["zero_waits"])} '
                 f'wait_mean/p99_ms={pair(extra["zero_waits"])} queue_unknown_task_n={extra["unknown_queue"]}')
            for kind, routing in extra['routing'].items():
                emit(f'  ROUTING_TRANSITION {kind} tasks={routing["tasks"]} '
                     f'migrated_source={flag_text(routing["migrated_source"])} '
                     f'cross_numa={flag_text(routing["cross_numa"])} '
                     f'return_home_reason={flag_text(routing["return_home_reason"])} '
                     f'chosen_groups={json.dumps(routing["chosen_groups"], sort_keys=True)} '
                     f'move_reasons={json.dumps(routing["move_reasons"], sort_keys=True)} '
                     f'wait_mean/p99_ms={pair(routing["waits"])}')
            early, late = diagnostic['halves']
            emit('  TREND early->late planned_req/s=' + f'{fmt(early["planned_rate"])}->{fmt(late["planned_rate"])}'
                 + ' completed_req/s=' + f'{fmt(early["completed_rate"])}->{fmt(late["completed_rate"])}'
                 + ' cohort_latency_ms=' + f'{fmt(early["cohort_mean_latency_ms"])}->{fmt(late["cohort_mean_latency_ms"])}'
                 + ' queue_sum=' + '->'.join(fmt(v) for v in diagnostic['queue_sum_half_means'])
                 + ' successful_unfinished(start/mid/end)=' + '/'.join(map(str, diagnostic['completed_outstanding_at_start_mid_end']))
                 + f' drain_after_horizon_s={fmt(diagnostic["drain_after_horizon_s"])}')
            emit(f'  POOL_SNAPSHOTS times={extra["snapshot_times"]} incomplete_group_sets={extra["incomplete_snapshots"]} '
                 + ' '.join(f'{key}_sum_mean/p99={pair(values)} known_times={len(values)}' for key, values in extra['load'].items()))
            emit(f'  ACCOUNTING logged_cohort_work_cpu_s={fmt(sum(extra["cpus"])/1000 if extra["cpus"] else None)} '
                 f'process_cpu_s=not_recorded workers/group={dict(sorted(extra["workers"].items())) if extra["workers"] is not None else "unknown"} '
                 f'proc_interval_s={row.get("proc_interval_seconds", "?")} FLAGS={json.dumps(result["warnings"])}')
            if (diagnostic['whole_run_incomplete'] or not same_union or extra['incomplete_snapshots']
                    or summary.get('drain_timed_out') or any(state != 'completed' for state in extra['statuses'])):
                valid = False
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            emit(f'  INVALID/INCOMPLETE: {type(error).__name__}: {error}')
            valid = False
    if offset or offset+limit < len(rows):
        emit('NOTE: partial display; unshown artifacts have not been checked')
    if offset+limit < len(rows):
        emit(f'NEXT: --offset {offset+len(selected)} --limit {limit}')
    return valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--phase', default='s4-baseline')
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--warmup-seconds', type=float)
    parser.add_argument('--steady-end-seconds', type=float)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=16)
    args = parser.parse_args()
    if args.offset < 0 or args.limit < 1:
        parser.error('offset must be nonnegative and limit positive')
    if args.spec and (args.warmup_seconds is not None or args.steady_end_seconds is not None):
        parser.error('choose --spec or explicit bounds, not both')
    print('DAG_TERMINAL_REPORT v2; existing files only; no workload, no settings changed')
    print('Request and task latency units are ms. Task work wall/CPU exclude successor routing and logging.')
    print('Task waits are not request path sums. Sampled queue != kernel runq; inflight != executing on CPU.')
    print('migrated_source is logged chosen != source (parent when present, otherwise home); '
          'it is not chosen != preferred. Routing transition totals cover completed cohort tasks only.')
    valid = phase_report(args.root, args.phase, args.spec, args.warmup_seconds, args.steady_end_seconds,
                         args.offset, args.limit)
    print('END: descriptive cohort evidence; no confidence intervals, stationarity proof or paired gains here')
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())

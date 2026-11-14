#!/usr/bin/env python3
"""Print a compact stage-3 report from existing results; never run a workload."""
import argparse
import json
from pathlib import Path

from compare_runs import summarize
from prepare_target_validation import compact


def read_json(path):
    return json.loads(path.read_text())


def fmt(value):
    return '?' if value is None else f'{value:.3f}'


def short(value):
    return '?' if value is None else ' '.join(str(value).split())


def environment(root, emit=print):
    emit('=== ENVIRONMENT (missing/unreadable = unknown, not unlimited) ===')
    for label in ('before', 'after'):
        path = root / f'topology-{label}.json'
        try:
            data = read_json(path)
        except (OSError, ValueError) as error:
            emit(f'ENV {label}: unavailable ({type(error).__name__})')
            continue
        cpus = data.get('allowed_cpus', [])
        emit(f'ENV {label}: allowed={compact(cpus) if cpus else "?"} n={len(cpus)} '
             f'cgroup={data.get("cgroup_version", "unknown/old capture")} '
             f'schedstats={data.get("sched_schedstats", "?")}')
        hierarchies = data.get('cgroup_hierarchies', [])
        if not hierarchies:
            emit('  CG: no hierarchy evidence; quota UNKNOWN')
        for h in hierarchies:
            label_h = ','.join(h.get('controllers', [])) or 'v2'
            emit(f'  CG {label_h}: {h.get("status", "?")} hidden_parents={h.get("hidden_ancestors", "?")}')
            controllers = set(h.get('controllers', []))
            if h.get('version') == 2:
                keys = ['cpu.max', 'cpu.stat', 'cpuset.cpus.effective', 'memory.max', 'pids.max']
            else:
                keys = []
                if 'cpu' in controllers:
                    keys += ['cpu.cfs_quota_us', 'cpu.cfs_period_us', 'cpu.stat']
                if 'cpuset' in controllers:
                    keys += ['cpuset.cpus', 'cpuset.effective_cpus', 'cpuset.mems']
                if 'memory' in controllers:
                    keys += ['memory.limit_in_bytes', 'memory.use_hierarchy']
                if 'pids' in controllers:
                    keys += ['pids.max', 'pids.current']
            for row in h.get('ancestors', []):
                emit('    ' + row['path'] + ' ' + ' '.join(f'{k}=[{short(row.get(k))}]' for k in keys))


def locate_run(manifest, row):
    raw = Path(row.get('path', ''))
    # Prefer the sibling run directory, so a relocated result tree is usable.
    candidates = (manifest.parent / raw.name, raw, manifest.parent / raw)
    return next((p for p in candidates if (p/'metadata.json').is_file()), candidates[0])


def phase_report(root, phase, offset=0, limit=16, emit=print):
    if not phase.startswith('s3-') or '/' in phase or phase in ('.', '..'):
        raise ValueError('phase must name one stage-3 directory, e.g. s3-calibration')
    manifest = root / phase / 'manifest.json'
    emit(f'=== {phase} ===')
    try:
        data = read_json(manifest)
    except (OSError, ValueError) as error:
        emit(f'MANIFEST unavailable ({type(error).__name__}); if still running, retry after completion')
        return False
    if not isinstance(data, dict) or not isinstance(data.get('runs'), list):
        emit('ERROR: expected a stage-3 manifest with runs')
        return False
    rows = data['runs']
    planned = None
    try:
        plan = read_json(root/'plan'/'plan.json')
        planned = plan['phases'][phase]['runs']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    analysis = data.get('analysis', {})
    start, end = analysis.get('warmup_seconds'), analysis.get('steady_end_seconds')
    emit(f'MATRIX recorded={len(rows)} planned={planned if planned is not None else "unknown"} '
         f'window=[{start},{end})s binary_sha256={data.get("binary_sha256", "unknown")}')
    valid = True
    if planned is None or len(rows) != planned:
        emit('NOTE: plan completion unconfirmed; manifest contains only finished attempts, not active runs')
        valid = False
    if start is None or end is None:
        emit('ERROR: measurement window absent; do not guess warmup from the phase name')
        return False
    keys = [(r.get('case'), r.get('seed')) for r in rows]
    if len(keys) != len(set(keys)):
        emit('ERROR: duplicate case/seed entries')
        valid = False
    failed_attempts = sum(r.get('returncode') != 0 for r in rows)
    emit(f'ATTEMPTS nonzero_or_missing_rc={failed_attempts}; detailed artifact checks cover displayed rows only')
    if failed_attempts:
        valid = False
    selected = rows[offset:offset+limit]
    if not selected:
        emit(f'NO ROWS at offset={offset}; recorded={len(rows)}; no detailed checks performed')
        return False
    emit(f'ROWS {offset+1}..{offset+len(selected)} of {len(rows)}; '
         'latency in ms, rates in tasks/s; early/late split the measurement window')
    for row in selected:
        name = row.get('case', '?')
        rc = row.get('returncode', 'missing')
        path = locate_run(manifest, row)
        emit(f'RUN {name} repeat={row.get("repetition", "?")} seed={row.get("seed", "?")} rc={rc}')
        if rc != 0:
            emit(f'  FAILED/INCOMPLETE; inspect {manifest.parent / (path.name+".log")}')
            valid = False
            continue
        try:
            entry = {**row, 'path': path, 'case': name, 'repeat': row['repetition'],
                     'binary_sha256': data.get('binary_sha256')}
            result = summarize(entry, 'independent', float(start), float(end))
            meta, summary = read_json(path/'metadata.json'), read_json(path/'summary.json')
            metrics, diagnostic = result['metrics'], result['diagnostics']
            masks = ';'.join(compact(mask) if mask else '?' for mask in meta.get('effective_masks', []))
            emit(f'  CONFIG affinity={meta.get("affinity")} threads/pool={meta.get("threads_per_pool")} '
                 f'instrumentation={meta.get("instrumentation")} same_union={meta.get("same_cpu_union")} masks={masks}')
            workload = result['workload']
            units = workload['cohort_units']
            target = workload['cohort_target_cpu_us']
            emit(f'  WORK mode={meta.get("work_mode", "?")} burst={meta.get("burst_min", "?")}..{meta.get("burst_max", "?")} '
                 f'cohort_target_mean_us={fmt(target / units if units else None)} '
                 f'nominal_offered_cpu_s/s={fmt(target / 1e6 / (float(end)-float(start)))} (target, not measured capacity)')
            emit(f'  STATUS whole={json.dumps(result["whole_run_statuses"], sort_keys=True)} '
                 f'cohort={json.dumps(result["cohort_statuses"], sort_keys=True)} '
                 f'planned={summary.get("planned_tasks")} conservation={summary.get("conservation_ok")}')
            emit('  LAT planned_mean/p99=' + '/'.join(fmt(metrics[k]) for k in ('planned_latency_ms_mean', 'planned_latency_ms_p99'))
                 + ' submitted_mean/p99=' + '/'.join(fmt(metrics[k]) for k in ('submitted_latency_ms_mean', 'submitted_latency_ms_p99'))
                 + ' wait_mean/p99=' + '/'.join(fmt(metrics[k]) for k in ('submit_to_start_ms_mean', 'submit_to_start_ms_p99'))
                 + ' callback_mean=' + fmt(metrics['callback_wall_ms_mean'])
                 + ' submit_lag_p99=' + fmt(metrics['admission_lag_ms_p99']))
            early, late = diagnostic['halves']
            emit('  TREND early->late planned_rate=' + f'{fmt(early["planned_rate"])}->{fmt(late["planned_rate"])}'
                 + ' completion_rate=' + f'{fmt(early["completed_rate"])}->{fmt(late["completed_rate"])}'
                 + ' cohort_latency=' + f'{fmt(early["cohort_mean_latency_ms"])}->{fmt(late["cohort_mean_latency_ms"])}'
                 + ' queue_sum=' + '->'.join(fmt(v) for v in diagnostic['queue_sum_half_means'])
                 + ' successful_unfinished(start/mid/end)=' + '/'.join(map(str, diagnostic['completed_outstanding_at_start_mid_end']))
                 + f' drain_s={fmt(diagnostic["drain_after_horizon_s"])}')
            emit(f'  SAMPLING window_times={diagnostic["pool_sample_times"]} '
                 f'queue_slope_tasks/s={fmt(diagnostic["queue_sum_slope_per_second"])} '
                 f'truncated={summary.get("sampling_truncated", "?")} '
                 f'failed={summary.get("sampling_failed", "?")} '
                 f'dropped={summary.get("sample_rows_dropped", "?")}')
            emit('  CPU whole_run_user/system_s=' + '/'.join(fmt(summary.get(k)) for k in ('process_user_cpu_seconds', 'process_system_cpu_seconds'))
                 + f' observed_s={fmt(summary.get("observed_seconds"))}'
                 + f' callback_cpu_mean_ms={fmt(summary.get("work_cpu_mean_ms"))}'
                 + f' FLAGS={json.dumps(result["warnings"])}')
            if diagnostic['whole_run_incomplete'] or not meta.get('same_cpu_union') or summary.get('sampling_truncated') or summary.get('sampling_failed'):
                valid = False
        except (OSError, ValueError, KeyError, TypeError) as error:
            emit(f'  INVALID/INCOMPLETE: {type(error).__name__}: {error}')
            valid = False
    if offset or offset+limit < len(rows):
        emit('NOTE: partial display; unshown artifacts have not been checked')
    if offset+limit < len(rows):
        emit(f'NEXT: --offset {offset+len(selected)} --limit {limit}')
    return valid


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--phase', nargs='+', default=['s3-smoke', 's3-calibration'])
    p.add_argument('--section', choices=['all', 'environment', 'runs'], default='all')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=16)
    args = p.parse_args()
    if args.offset < 0 or args.limit < 1:
        p.error('offset must be nonnegative and limit positive')
    print('TERMINAL_REPORT v1; existing files only; no workload, no settings changed')
    print('CPU totals cover the whole run incl drain; ? means unavailable. Queue != kernel runq.')
    valid = True
    if args.section != 'runs':
        environment(args.root)
    if args.section != 'environment':
        for phase in args.phase:
            valid = phase_report(args.root, phase, args.offset, args.limit) and valid
    print('END: inspect flags and raw artifacts; this digest does not certify steady state or paired gains')
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())

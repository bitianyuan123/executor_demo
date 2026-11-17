#!/usr/bin/env python3
"""Optional Linux CPU-window capture around an argv command (no shell).

Cluster values include every task running on selected CPUs; process values
include the launched process's threads, but not its child processes. Neither is
Folly queue length, callback occupancy, or a one-millisecond peak estimator.
"""

import argparse
import collections
import csv
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


COUNTERS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq',
            'steal', 'guest', 'guest_nice')
BUSY = ('user', 'nice', 'system', 'irq', 'softirq')
BASE = COUNTERS[:8]
SCHEMA = 'cluster-cpu-capture-v1'
CSV_FIELDS = (
    'window', 'cluster', 'cpus', 'cpu_count', 'window_start_ns', 'window_end_ns',
    'wall_seconds', 'valid', 'invalid_reason', 'busy_ticks', 'busy_cpu_seconds',
    'busy_cpu_equivalent', 'util_percent', 'idle_cpu_equivalent',
    'iowait_cpu_equivalent', 'steal_cpu_equivalent', 'total_cpu_equivalent',
    'util_outside_nominal_range', 'start_read_span_ns', 'end_read_span_ns',
    'start_read_begin_ns', 'start_read_end_ns', 'end_read_begin_ns', 'end_read_end_ns',
    'start_collection_span_ns', 'end_collection_span_ns',
    'sample_scheduled_ns', 'sample_lag_ns', 'skipped_deadlines',
    'process_valid', 'process_invalid_reason', 'process_window_start_ns',
    'process_window_end_ns', 'process_read_begin_ns', 'process_read_end_ns', 'process_cpu_seconds', 'process_cpu_equivalent',
    'collector_cpu_seconds',
)


def parse_partitions(text):
    groups, seen = [], set()
    for group in text.split(';'):
        cpus = set()
        if not group.strip():
            raise ValueError('empty CPU partition')
        for token in group.split(','):
            token = token.strip()
            parts = token.split('-')
            if len(parts) not in (1, 2) or any(not p.isdecimal() for p in parts):
                raise ValueError('invalid CPU token: ' + token)
            first, last = int(parts[0]), int(parts[-1])
            if last < first or last - first > 1_000_000:
                raise ValueError('invalid CPU range: ' + token)
            values = set(range(first, last + 1))
            if cpus & values:
                raise ValueError('duplicate CPU in partition: ' + token)
            cpus.update(values)
        if seen & cpus:
            raise ValueError('CPU partitions overlap')
        seen.update(cpus)
        groups.append(sorted(cpus))
    return groups


def parse_cpu_stat(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith('cpu') or not fields[0][3:].isdigit():
            continue
        cpu = int(fields[0][3:])
        if len(fields) < 9:
            raise ValueError('cpu%d lacks the eight base accounting fields' % cpu)
        values = [int(value) for value in fields[1:11]]
        if min(values) < 0 or cpu in result:
            raise ValueError('negative or duplicate CPU accounting')
        result[cpu] = dict(zip(COUNTERS, values + [0] * (10 - len(values))))
    if not result:
        raise ValueError('/proc/stat contains no per-CPU records')
    return result


def parse_process_stat(text):
    # comm may contain spaces and closing parentheses; fields follow its last ')'.
    left = text.find('(')
    right = text.rfind(')')
    if left < 1 or right <= left:
        raise ValueError('invalid /proc/PID/stat comm')
    pid = int(text[:left].strip())
    fields = text[right + 1:].split()
    if len(fields) < 20:
        raise ValueError('short /proc/PID/stat')
    values = {'pid': pid, 'state': fields[0], 'utime': int(fields[11]),
              'stime': int(fields[12]), 'starttime': int(fields[19])}
    if min(values['utime'], values['stime'], values['starttime']) < 0:
        raise ValueError('negative process counter')
    return values


def read_snapshot(pid, scheduled_ns=None, proc_root=Path('/proc')):
    result = {'collection_start_ns': time.monotonic_ns(),
              'collector_cpu_start_ns': time.process_time_ns(),
              'scheduled_ns': scheduled_ns, 'cpu_error': '', 'process_error': ''}
    result['cpu_read_start_ns'] = time.monotonic_ns()
    try:
        # One read for all selected CPUs. guest is already included in user/nice.
        result['cpus'] = parse_cpu_stat((proc_root / 'stat').read_text())
    except (OSError, ValueError) as exc:
        result['cpus'] = {}
        result['cpu_error'] = type(exc).__name__ + ': ' + str(exc)
    result['cpu_read_end_ns'] = time.monotonic_ns()
    result['at_ns'] = (result['cpu_read_start_ns'] + result['cpu_read_end_ns']) // 2
    result['process_read_start_ns'] = time.monotonic_ns()
    try:
        result['process'] = parse_process_stat((proc_root / str(pid) / 'stat').read_text())
    except (OSError, ValueError) as exc:
        result['process'] = None
        result['process_error'] = type(exc).__name__ + ': ' + str(exc)
    result['process_read_end_ns'] = time.monotonic_ns()
    result['process_at_ns'] = (result['process_read_start_ns'] + result['process_read_end_ns']) // 2
    result['collection_end_ns'] = time.monotonic_ns()
    result['collector_cpu_end_ns'] = time.process_time_ns()
    return result


def process_difference(before, after, hz):
    result = {'process_valid': False, 'process_invalid_reason': '',
              'process_window_start_ns': before['process_at_ns'],
              'process_window_end_ns': after['process_at_ns'],
              'process_read_begin_ns': before.get('process_read_start_ns', before['process_at_ns']),
              'process_read_end_ns': after.get('process_read_end_ns', after['process_at_ns']),
              'process_cpu_seconds': None, 'process_cpu_equivalent': None}
    a, b = before.get('process'), after.get('process')
    if a is None or b is None:
        result['process_invalid_reason'] = 'missing_process_endpoint'
    elif a['pid'] != b['pid'] or a['starttime'] != b['starttime']:
        result['process_invalid_reason'] = 'process_identity_changed'
    elif b['utime'] < a['utime'] or b['stime'] < a['stime']:
        result['process_invalid_reason'] = 'process_counter_decreased'
    elif after['process_at_ns'] <= before['process_at_ns']:
        result['process_invalid_reason'] = 'nonpositive_process_window'
    else:
        seconds = (b['utime'] + b['stime'] - a['utime'] - a['stime']) / hz
        result.update(process_valid=True, process_cpu_seconds=seconds,
                      process_cpu_equivalent=seconds / ((after['process_at_ns'] - before['process_at_ns']) / 1e9))
    return result


def difference(before, after, partitions, hz, window=0, skipped=0):
    wall = (after['at_ns'] - before['at_ns']) / 1e9
    process = process_difference(before, after, hz)
    common = dict(window=window, window_start_ns=before['at_ns'],
                  window_end_ns=after['at_ns'], wall_seconds=wall,
                  start_read_span_ns=before['cpu_read_end_ns'] - before['cpu_read_start_ns'],
                  start_read_begin_ns=before['cpu_read_start_ns'],
                  start_read_end_ns=before['cpu_read_end_ns'],
                  end_read_begin_ns=after['cpu_read_start_ns'],
                  end_read_end_ns=after['cpu_read_end_ns'],
                  end_read_span_ns=after['cpu_read_end_ns'] - after['cpu_read_start_ns'],
                  start_collection_span_ns=before['collection_end_ns'] - before['collection_start_ns'],
                  end_collection_span_ns=after['collection_end_ns'] - after['collection_start_ns'],
                  sample_scheduled_ns=after['scheduled_ns'],
                  sample_lag_ns=(max(0, after['collection_start_ns'] - after['scheduled_ns'])
                                 if after['scheduled_ns'] is not None else None),
                  skipped_deadlines=skipped,
                  collector_cpu_seconds=(after['collector_cpu_end_ns'] - before['collector_cpu_end_ns']) / 1e9,
                  **process)
    rows = []
    for cluster, cpus in enumerate(partitions):
        row = dict.fromkeys(CSV_FIELDS)
        row.update(common, cluster=cluster, cpus=','.join(map(str, cpus)),
                   cpu_count=len(cpus), valid=False, invalid_reason='')
        missing = [cpu for cpu in cpus if cpu not in before['cpus'] or cpu not in after['cpus']]
        if before.get('cpu_error') or after.get('cpu_error'):
            row['invalid_reason'] = 'cpu_snapshot_error'
        elif missing:
            row['invalid_reason'] = 'missing_cpu:' + ','.join(map(str, missing))
        elif wall <= 0:
            row['invalid_reason'] = 'nonpositive_window'
        else:
            backwards = [(cpu, name) for cpu in cpus for name in COUNTERS
                         if after['cpus'][cpu][name] < before['cpus'][cpu][name]]
            if backwards:
                row['invalid_reason'] = 'counter_decreased:' + ','.join('%d/%s' % x for x in backwards)
            else:
                delta = {name: sum(after['cpus'][cpu][name] - before['cpus'][cpu][name] for cpu in cpus)
                         for name in BASE}
                busy = sum(delta[name] for name in BUSY)
                equiv = busy / hz / wall
                row.update(valid=True, busy_ticks=busy, busy_cpu_seconds=busy / hz,
                           busy_cpu_equivalent=equiv, util_percent=100 * equiv / len(cpus),
                           idle_cpu_equivalent=delta['idle'] / hz / wall,
                           iowait_cpu_equivalent=delta['iowait'] / hz / wall,
                           steal_cpu_equivalent=delta['steal'] / hz / wall,
                           total_cpu_equivalent=sum(delta.values()) / hz / wall,
                           util_outside_nominal_range=not (0 <= equiv <= len(cpus)))
        rows.append(row)
    return rows


def quantile(values, p):
    return sorted(values)[max(0, math.ceil(p * len(values)) - 1)] if values else None


def summarize(rows, partitions, origin_ns=None, warmup_seconds=2, window_seconds=16):
    if origin_ns is None:
        return {'available': False, 'reason': 'summary_path_not_given_or_origin_unavailable',
                'scope': 'CSV still contains startup, warmup, measurement and drain windows'}
    start = origin_ns + round(warmup_seconds * 1e9)
    end = start + round(window_seconds * 1e9)
    groups = []
    selected = [r for r in rows if r['start_read_begin_ns'] >= start and r['end_read_end_ns'] <= end]
    for cluster, cpus in enumerate(partitions):
        observed = [r for r in selected if r['cluster'] == cluster]
        valid = [r for r in observed if r['valid']]
        seconds = sum(r['wall_seconds'] for r in valid)
        invalid = collections.Counter(r['invalid_reason'] for r in observed if not r['valid'])
        values = [r['util_percent'] for r in valid]
        groups.append({'cluster': cluster, 'cpus': cpus, 'complete_windows': len(observed),
                       'valid_windows': len(valid), 'valid_seconds': seconds,
                       'valid_coverage_percent': 100 * seconds / window_seconds,
                       'invalid_seconds': sum(r['wall_seconds'] for r in observed if not r['valid']),
                       'invalid_reasons': dict(invalid),
                       'util_time_weighted_mean_percent':
                           (100 * sum(r['busy_cpu_seconds'] for r in valid) / seconds / len(cpus) if seconds else None),
                       'window_util_p50_percent': quantile(values, .5),
                       'window_util_p95_percent': quantile(values, .95),
                       'window_util_p99_percent': quantile(values, .99),
                       'window_util_max_percent': max(values) if values else None,
                       'outside_nominal_windows': sum(bool(r['util_outside_nominal_range']) for r in valid)})
    # Process values are repeated on cluster rows; take exactly one per window.
    prows = [r for r in rows if r['cluster'] == 0 and
             r['process_read_begin_ns'] >= start and r['process_read_end_ns'] <= end]
    pv = [r for r in prows if r['process_valid']]
    pseconds = sum((r['process_window_end_ns'] - r['process_window_start_ns']) / 1e9 for r in pv)
    return {'available': True, 'origin_ns': origin_ns, 'scope_start_ns': start, 'scope_end_ns': end,
            'warmup_seconds': warmup_seconds, 'window_seconds': window_seconds,
            'selection': 'both complete read brackets contained in scope; midpoint-to-midpoint deltas; no clipping, interpolation or zero filling',
            'percentiles': 'nearest-rank over valid complete windows; mean is duration-weighted',
            'clusters': groups,
            'process': {'valid_windows': len(pv), 'valid_seconds': pseconds,
                        'valid_coverage_percent': 100 * pseconds / window_seconds,
                        'cpu_seconds': sum(r['process_cpu_seconds'] for r in pv),
                        'cpu_equivalent': (sum(r['process_cpu_seconds'] for r in pv) / pseconds if pseconds else None),
                        'invalid_reasons': dict(collections.Counter(r['process_invalid_reason'] for r in prows if not r['process_valid']))}}


def print_report(metadata, rows):
    print('CPU_CAPTURE ' + json.dumps({k: metadata.get(k) for k in
          ('status', 'child_returncode', 'exit_reason', 'interval_ms', 'clock_ticks_per_second',
           'collector_cpu_seconds', 'collector_sampling_cpu_seconds', 'controller_allowed_cpus',
           'capture_errors', 'summary_error')}, ensure_ascii=False, sort_keys=True))
    scope = metadata['formal_scope']
    print('CPU_SCOPE ' + json.dumps(scope, ensure_ascii=False, sort_keys=True))
    print('CPU_NOTES Cluster busy includes ALL work on selected CPUs (including collector/helper/interference); '
          'process CPU is launched-process threads only, excludes descendants. Windows are averages, '
          'not 1ms peaks. Missing endpoints are unavailable, never zero; no utilization cap.')
    if not scope['available']:
        print('CPU_SERIES unavailable: provide --summary-path for origin_ns alignment; full samples in cpu_windows.csv')
        return
    print('CPU_SERIES_BEGIN relative_start_ms,relative_end_ms,cluster,valid,busy_cpu_equivalent,util_percent,idle_cpu_equivalent,iowait_cpu_equivalent,steal_cpu_equivalent,process_cpu_equivalent,invalid_reason,sample_lag_ms')
    for row in rows:
        if row['start_read_begin_ns'] < scope['scope_start_ns'] or row['end_read_end_ns'] > scope['scope_end_ns']:
            continue
        def num(value):
            return '' if value is None else '%.6f' % value
        print(','.join([num((row['window_start_ns'] - scope['origin_ns']) / 1e6),
                        num((row['window_end_ns'] - scope['origin_ns']) / 1e6), str(row['cluster']),
                        str(int(row['valid'])), num(row['busy_cpu_equivalent']), num(row['util_percent']),
                        num(row['idle_cpu_equivalent']), num(row['iowait_cpu_equivalent']),
                        num(row['steal_cpu_equivalent']), num(row['process_cpu_equivalent']),
                        row['invalid_reason'].replace(',', ';'),
                        num(row['sample_lag_ns'] / 1e6 if row['sample_lag_ns'] is not None else None)]))
    print('CPU_SERIES_END')


def terminate_group(child, grace=1.0):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The group leader may exit while a descendant ignores SIGTERM. Always
    # address our own new process group again; do not leave descendants behind.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def execute(args):
    partitions = parse_partitions(args.partitions)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    hz = os.sysconf('SC_CLK_TCK')
    interval_ns = round(args.interval_ms * 1e6)
    start_cpu = time.process_time_ns()
    start_ns = time.monotonic_ns()
    interrupted = [None]
    prior_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        prior_handlers[signum] = signal.signal(signum, lambda sig, frame: interrupted.__setitem__(0, sig))
    rows, snapshots, errors = [], [], []
    child, reason, child_code = None, 'not_started', None
    metadata = {'schema': SCHEMA, 'command': command, 'cwd': str(Path.cwd()),
                'partitions': partitions, 'controller_allowed_cpus': sorted(os.sched_getaffinity(0)),
                'interval_ms': args.interval_ms, 'clock_ticks_per_second': hz,
                'clock': 'CLOCK_MONOTONIC via time.monotonic_ns; Linux steady_clock origin required',
                'started_ns': start_ns, 'timeout_seconds': args.timeout_seconds,
                'busy_definition': 'user+nice+system+irq+softirq; guest already included, iowait/steal excluded',
                'cluster_scope': 'all tasks on these CPUs; not executor-only or process-only',
                'process_scope': '/proc/PID/stat utime+stime of thread group; excludes descendant processes',
                'collector_affinity': 'inherited unchanged; no spare CPUs chosen automatically',
                'tick_warning': 'window averages have tick quantization and read timing error; values are not capped',
                'sampling_error_policy': 'invalid windows retained, no interpolation or zero fill',
                'collector_included_if_on_selected_cpu': True}
    if args.interval_ms < 100:
        print('WARNING: intervals below 100ms amplify tick quantization and sampler overhead; '
              '20ms windows still cannot resolve 1ms peaks.', file=sys.stderr)
    try:
        with (output / 'run.log').open('w') as log:
            try:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)  # argv only: no shell
            except (OSError, ValueError) as exc:
                reason = 'launch_failed'
                errors.append({'stage': 'launch', 'error': str(exc)})
            if child is not None:
                metadata['child_pid'] = child.pid
                reason = 'running'
                previous = read_snapshot(child.pid)
                snapshots.append(previous)
                deadline = time.monotonic_ns() + interval_ns
                timeout_at = start_ns + round(args.timeout_seconds * 1e9)
                skipped = 0
                while True:
                    if interrupted[0] is not None:
                        reason = 'signal_%d' % interrupted[0]
                        terminate_group(child)
                        break
                    now = time.monotonic_ns()
                    if now >= timeout_at:
                        reason = 'timeout'
                        terminate_group(child)
                        break
                    if child.poll() is not None:
                        reason = 'completed'
                        break
                    if now < deadline:
                        time.sleep(min((deadline - now) / 1e9, .02))
                        continue
                    current = read_snapshot(child.pid, deadline)
                    snapshots.append(current)
                    rows.extend(difference(previous, current, partitions, hz, len(snapshots) - 2, skipped))
                    previous = current
                    deadline += interval_ns
                    skipped = 0
                    while deadline <= time.monotonic_ns():
                        deadline += interval_ns
                        skipped += 1
                # Retain a final interval even if PID disappeared. Its process
                # delta is unavailable, not a fabricated zero CPU measurement.
                current = read_snapshot(child.pid)
                snapshots.append(current)
                rows.extend(difference(previous, current, partitions, hz, len(snapshots) - 2, skipped))
                child_code = child.wait()
    except Exception as exc:
        reason = 'collector_failed'
        errors.append({'stage': 'capture', 'error': type(exc).__name__ + ': ' + str(exc)})
        if child is not None:
            terminate_group(child)
            child_code = child.returncode
    finally:
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)
    for snapshot in snapshots:
        if snapshot['cpu_error']:
            errors.append({'stage': 'cpu_snapshot', 'at_ns': snapshot['at_ns'], 'error': snapshot['cpu_error']})
    origin, summary_error = None, None
    if args.summary_path:
        try:
            summary = json.loads(Path(args.summary_path).read_text())
            origin = summary['origin_ns']
            if isinstance(origin, bool) or not isinstance(origin, int) or origin < 0:
                raise ValueError('origin_ns must be a nonnegative integer')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            summary_error = type(exc).__name__ + ': ' + str(exc)
    scope = summarize(rows, partitions, origin, args.warmup_seconds, args.window_seconds)
    if args.summary_path and scope['available'] and not any(g['valid_windows'] for g in scope['clusters']):
        summary_error = 'no complete valid CPU window inside requested scope; check origin clock and duration'
    if reason == 'timeout':
        exit_code = 124
    elif reason.startswith('signal_'):
        exit_code = 128 + int(reason[7:])
    elif child_code is not None and child_code != 0:
        exit_code = child_code if child_code > 0 else 128 - child_code
    elif reason != 'completed' or errors or summary_error or not rows or any(not r['valid'] for r in rows):
        exit_code = 2
    else:
        exit_code = 0
    metadata.update(ended_ns=time.monotonic_ns(), exit_reason=reason, child_returncode=child_code,
                    capture_errors=errors, summary_path=args.summary_path, summary_error=summary_error,
                    status='complete' if exit_code == 0 else 'partial_or_failed', exit_code=exit_code,
                    sample_count=len(snapshots), window_count=max(0, len(snapshots) - 1),
                    formal_scope=scope,
                    collector_cpu_seconds=(time.process_time_ns() - start_cpu) / 1e9,
                    collector_sampling_cpu_seconds=sum((s['collector_cpu_end_ns'] - s['collector_cpu_start_ns']) / 1e9 for s in snapshots),
                    collector_cpu_scope='launch, sampling, loop, analysis before serialization; excludes child CPU and final serialization/terminal printing',
                    invalid_cluster_windows=sum(not r['valid'] for r in rows),
                    missing_process_windows=sum(not r['process_valid'] for r in rows if r['cluster'] == 0),
                    process_endpoint_errors=[{'at_ns': s['process_at_ns'], 'error': s['process_error']} for s in snapshots if s['process_error']])
    with (output / 'cpu_windows.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (output / 'capture.json').write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
    print_report(metadata, rows)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--partitions', required=True, help='nonoverlapping CPU sets, e.g. 0-15;16-31')
    parser.add_argument('--interval-ms', type=float, default=100)
    parser.add_argument('--output', required=True, help='new directory; never overwritten')
    parser.add_argument('--timeout-seconds', type=float, default=120)
    parser.add_argument('--summary-path', help='child-created summary.json with monotonic origin_ns')
    parser.add_argument('--warmup-seconds', type=float, default=2)
    parser.add_argument('--window-seconds', type=float, default=16)
    parser.add_argument('command', nargs=argparse.REMAINDER, help='-- executable arg ...; no shell')
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    for name in ('interval_ms', 'timeout_seconds', 'warmup_seconds', 'window_seconds'):
        if not math.isfinite(getattr(args, name)):
            parser.error(name + ' must be finite')
    if args.interval_ms < 20 or args.timeout_seconds <= 0 or args.warmup_seconds < 0 or args.window_seconds <= 0:
        parser.error('require interval >=20ms, timeout/window >0 and warmup >=0')
    if not command:
        parser.error('provide -- followed by a command argv')
    try:
        return execute(args)
    except (OSError, ValueError) as exc:
        print('capture error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

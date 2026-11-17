#!/usr/bin/env python3
"""Intersect scheduler switch intervals with one request's recorded callbacks.

No periodic CPU polling, CPU-time smearing, runq reconstruction or IRQ subtraction.
Only complete, continuous per-CPU switch chains can yield a complete profile.
"""
import argparse
import bisect
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import report

EVENT = re.compile(r'\[(\d+)\]\s+(\d+\.\d+):\s+sched:sched_switch:\s+(.*)')
SWITCH = re.compile(r'prev_comm=.*?\s+prev_pid=(\d+)\s+prev_prio=\S+\s+prev_state=\S+\s+==>\s+next_comm=.*?\s+next_pid=(\d+)\s+next_prio=\S+')
# perf can use either the tracepoint's named fields or its compact formatter.
# Greedy comm matching takes the final :PID before [priority], so names such as
# "worker 7:phase:42" retain their embedded spaces, digits and colons.
COMPACT_SWITCH = re.compile(r'.+:(\d+)\s*\[-?\d+\]\s+\S+\s+==>\s+.+:(\d+)\s*\[-?\d+\]')
LOST = re.compile(r'PERF_RECORD_LOST|\bLOST\s+\d+|\blost\s+\d+\s+events?', re.I)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def timestamp(value):
    seconds, fraction = value.split('.')
    require(len(fraction) == 9, 'perf script requires --ns (nine decimal timestamp digits)')
    return int(seconds) * 1_000_000_000 + int(fraction)


def switch_payload(payload, line_no):
    payload = payload.strip()
    for name, pattern in (('named', SWITCH), ('compact', COMPACT_SWITCH)):
        match = pattern.fullmatch(payload)
        if match is not None:
            return int(match[1]), int(match[2]), name
    # Preserve enough evidence to diagnose a different perf formatter without
    # flooding the terminal or printing control characters from task names.
    preview = json.dumps(payload[:240], ensure_ascii=True)
    suffix = ' (truncated)' if len(payload) > 240 else ''
    raise ValueError(f'line {line_no}: malformed switch payload: {preview}{suffix}')


def intervals(lines, cpus, tracked_tids, busy_out=None, extent=None):
    """Stream all CPUs' records, retaining only tracked TID intervals.

    The first switch only establishes state. The final open interval is not
    extended. A mismatch cannot be repaired by inventing an idle interval.
    """
    state, bounds, selected = {}, {}, collections.defaultdict(list)
    audit = collections.Counter(payload_format_named=0, payload_format_compact=0)
    for line_no, line in enumerate(lines, 1):
        require(not LOST.search(line), f'line {line_no}: lost event indication')
        if 'sched_switch:' not in line:
            continue
        match = EVENT.search(line)
        require(match is not None, f'line {line_no}: unsupported switch format')
        cpu, at, payload = int(match[1]), timestamp(match[2]), match[3]
        require(cpu in cpus, f'line {line_no}: CPU {cpu} outside declared capture')
        prev, nxt, payload_format = switch_payload(payload, line_no)
        audit['payload_format_' + payload_format] += 1
        if cpu in state:
            before, running = state[cpu]
            require(at >= before, f'line {line_no}: CPU clock moved backwards')
            require(prev == running, f'line {line_no}: broken CPU {cpu} switch chain')
            if running in tracked_tids and at > before:
                selected[running].append((before, at, cpu))
            if busy_out is not None and running != 0 and at > before:
                left, right = max(before, extent[0]), min(at, extent[1])
                if left < right:
                    busy_out.append((left, right, cpu))
        else:
            bounds[cpu] = [at, at]
            audit['left_open_cpu_intervals_excluded'] += 1
        bounds[cpu][1] = at
        state[cpu] = (at, nxt)
        audit['switch_events'] += 1
    require(audit['switch_events'] > 0, 'no scheduler switch events')
    for tid, spans in selected.items():
        spans.sort()
        require(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])),
                f'TID {tid}: overlapping CPU residence, clock/identity invalid')
    audit['right_open_cpu_intervals_excluded'] = len(state)
    return selected, bounds, dict(audit)


def integrate(callbacks, scheduled, finished, groups, spans, bounds, bin_ns, busy_spans=None):
    require(bin_ns > 0, 'bin must be positive')
    end = max([finished] + [t['finish_ns'] for t in callbacks])
    require(end > scheduled, 'empty request extent')
    count = (end - scheduled + bin_ns - 1) // bin_ns
    require(count <= 200000, 'profile exceeds 200000 windows; increase bin size')
    cpu_group = {}
    for i, group in enumerate(groups):
        require(group['cpus'], 'empty CPU group')
        for cpu in group['cpus']:
            require(cpu not in cpu_group, 'overlapping CPU groups')
            cpu_group[cpu] = i
    coverage = {i: all(cpu in bounds and bounds[cpu][0] <= scheduled and bounds[cpu][1] >= end
                       for cpu in group['cpus']) for i, group in enumerate(groups)}
    needed = {t['chosen'] for t in callbacks}
    require(all(coverage[i] for i in needed),
            'request not fully bracketed on its eligible CPUs; choose an interior request, retain partial evidence')
    areas = [[0] * len(groups) for _ in range(count)]
    busy_areas = [[0] * len(groups) for _ in range(count)]
    for s, f, cpu in busy_spans or []:
        cursor, stop = max(s, scheduled), min(f, end)
        while cursor < stop:
            index = (cursor - scheduled) // bin_ns
            right = min(stop, scheduled + (index+1)*bin_ns)
            busy_areas[index][cpu_group[cpu]] += right-cursor
            cursor = right
    scheduled_by_task = []
    per_tid = collections.defaultdict(list)
    for t in callbacks:
        per_tid[t['worker_tid']].append(t)
    for tid, tasks in per_tid.items():
        tasks.sort(key=lambda t: t['start_ns'])
        require(tid > 0, 'missing worker_tid; rebuild demo for future capture')
        require(all(a['finish_ns'] <= b['start_ns'] for a, b in zip(tasks, tasks[1:])),
                'same worker has overlapping callbacks')
        runs = spans.get(tid, [])
        run_ends = [v[1] for v in runs]
        for t in tasks:
            total = 0
            for s, f, cpu in runs[bisect.bisect_right(run_ends, t['start_ns']):]:
                if s >= t['finish_ns']:
                    break
                start, stop = max(s, t['start_ns']), min(f, t['finish_ns'])
                if start >= stop:
                    continue
                require(cpu in groups[t['chosen']]['cpus'], 'worker ran outside chosen cluster affinity')
                total += stop - start
                cursor = start
                while cursor < stop:
                    index = (cursor - scheduled) // bin_ns
                    right = min(stop, scheduled + (index + 1) * bin_ns)
                    areas[index][cpu_group[cpu]] += right - cursor
                    cursor = right
            # Different accounting scopes: retain both values and their residual.
            require(total > 0 or t['cpu_ns'] == 0,
                    'positive callback CPU but no matching scheduled residence; trace identity/clock incomplete')
            scheduled_by_task.append({'tag': t['tag'], 'kind': t['kind'], 'worker_tid': tid,
                                      'chosen': t['chosen'], 'measured_cpu_ns': t['cpu_ns'],
                                      'scheduled_residence_ns': total,
                                      'residence_minus_measured_ns': total-t['cpu_ns']})
    rows = []
    def overlap(task, left, right, begin, finish):
        return max(0, min(right, task[finish])-max(left, task[begin]))
    for index, area in enumerate(areas):
        start, stop = scheduled + index * bin_ns, min(end, scheduled + (index+1)*bin_ns)
        for pool, value in list(enumerate(area)) + [('all', sum(area))]:
            capacity = len(cpu_group) if pool == 'all' else len(groups[pool]['cpus'])
            members = callbacks if pool == 'all' else [t for t in callbacks if t['chosen'] == pool]
            physical_cpus = list(cpu_group) if pool == 'all' else groups[pool]['cpus']
            busy_valid = busy_spans is not None and all(cpu in bounds and bounds[cpu][0] <= start
                         and bounds[cpu][1] >= stop for cpu in physical_cpus)
            busy_value = sum(busy_areas[index]) if pool == 'all' else busy_areas[index][pool]
            require(value <= (stop-start)*capacity, 'residence exceeds physical logical-CPU window budget')
            rows.append({'bin': index, 'relative_start_ms': (start-scheduled)/1e6,
                         'relative_end_ms': (stop-scheduled)/1e6, 'pool': pool,
                         'request_scheduled_cpu_ms': value/1e6,
                         'request_scheduled_cpu_equivalent': value/(stop-start),
                         'request_cluster_util_percent': 100*value/(stop-start)/capacity,
                         'cluster_window_fully_bracketed': int(busy_valid),
                         'cluster_nonidle_residence_percent': 100*busy_value/(stop-start)/capacity if busy_valid else None,
                         'request_submitted_not_started_mean': sum(overlap(t,start,stop,'submitted_ns','start_ns') for t in members)/(stop-start),
                         'request_callback_wall_occupancy_mean': sum(overlap(t,start,stop,'start_ns','finish_ns') for t in members)/(stop-start)})
    totals = [sum(a) for a in areas]
    total_ns = sum(totals)
    cumulative = 0
    times = {}
    for index, value in enumerate(totals):
        cumulative += value
        for p in (50, 90, 99):
            if str(p) not in times and total_ns and cumulative >= p/100*total_ns:
                times[str(p)] = min(end-scheduled, (index+1)*bin_ns)/1e6
    high = {}
    for threshold in (8, 16, 32, 48):
        flags = [v/(min(end, scheduled+(i+1)*bin_ns)-(scheduled+i*bin_ns)) > threshold
                 for i, v in enumerate(totals)]
        lengths, current = [], 0
        for i, flag in enumerate(flags):
            duration = min(end, scheduled+(i+1)*bin_ns)-(scheduled+i*bin_ns)
            if flag:
                current += duration
            elif current:
                lengths.append(current); current = 0
        if current:
            lengths.append(current)
        high[str(threshold)] = {'segments': len(lengths), 'total_ms': sum(lengths)/1e6,
                                'longest_binned_segment_ms': max(lengths, default=0)/1e6}
    return rows, {'request_profile_span_ms': (end-scheduled)/1e6,
                  'request_finished_ms': (finished-scheduled)/1e6,
                  'last_callback_tail_ms': (end-finished)/1e6,
                  'measured_callback_cpu_ms': sum(t['cpu_ns'] for t in callbacks)/1e6,
                  'scheduled_callback_residence_ms': total_ns/1e6,
                  'service_cumulative_percent_window_end_ms': times,
                  'above_cpu_equivalent': high, 'group_full_extent_bracketed': coverage,
                  'task_residence_audit': scheduled_by_task}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--request', type=int, required=True)
    parser.add_argument('--bin-ms', type=float, default=1)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'output exists; choose a new directory')
    require(math.isfinite(args.bin_ms) and .05 <= args.bin_ms <= 1000, 'bin-ms must be 0.05..1000')
    capture = json.loads(args.capture.read_text())
    require(capture.get('schema') == 'request-sched-capture-v1' and capture.get('clock') == 'CLOCK_MONOTONIC', 'missing monotonic capture provenance')
    require(capture.get('capture_completed') is True and capture.get('record_returncode') == 0
            and capture.get('script_returncode') == 0 and capture.get('lost_events_detected') is False,
            'capture incomplete, failed, or lost events')
    require(digest(args.trace) == capture['source_sha256']['sched_text'], 'trace hash differs from capture')
    data = report.load_run(args.run)
    summary = data['summary']
    require(summary['trace'] and summary['affinity'] == 'cluster', 'requires trace1 and verified cluster partitions')
    require(data['valid_for_comparison'], 'run has failed/rejected requests')
    clock = summary.get('diagnostic_clock', {})
    require(clock.get('compatible') is True and clock.get('bracket_ns', 100001) <= 100000,
            'steady_clock/monotonic alignment missing or too uncertain; rebuild demo')
    requests = [r for r in data['requests'] if r['request'] == args.request and r['status'] == 'completed']
    require(len(requests) == 1, 'request is absent or incomplete')
    tasks = [dict(t, worker_tid=int(t.get('worker_tid', 0))) for t in data['tasks'] if t['request'] == args.request]
    require(tasks and all(t['worker_tid'] > 0 for t in tasks), 'tasks lack worker_tid; old CSV cannot be joined by request')
    cpus = {cpu for g in summary['groups'] for cpu in g['cpus']}
    require(cpus == set(capture['cpus']), 'demo and capture CPU sets differ')
    request = requests[0]
    extent = (request['submitted_ns'], max(request['finished_ns'], max(t['finish_ns'] for t in tasks)))
    busy = []
    with args.trace.open() as stream:
        spans, bounds, audit = intervals(stream, cpus, {t['worker_tid'] for t in tasks}, busy, extent)
    rows, derived = integrate(tasks, request['submitted_ns'], request['finished_ns'],
                              summary['groups'], spans, bounds, round(args.bin_ms*1e6), busy)
    result = {'schema': 'request-sched-profile-v1', 'request': args.request,
              'request_selection': 'explicit ID, not a representative sample', 'bin_ms': args.bin_ms,
              'profile_origin_ns': request['submitted_ns'],
              'attempt_lag_ms': (request['submitted_ns']-request['scheduled_ns'])/1e6,
              'groups': summary['groups'], 'audit': audit, **derived,
              'source_sha256': {p.name: digest(p) for p in [args.trace, args.capture,
                                  args.run/'summary.json', args.run/'tasks.csv', args.run/'requests.csv']},
              'scope': 'Time zero is request attempt/submitted_ns. Request columns intersect callback boundaries with sched_switch residence. Cluster nonidle column separately includes all non-idle tasks on those CPUs, with null for unbracketed windows. Both are scheduler residence, not precise IRQ-subtracted CPU accounting; include interruptions of nonidle tasks and omit IRQ during idle. Request columns exclude executor work outside callback boundaries and async helpers. Not CPU demand or runq. Submitted-not-started and wall occupancy are callback lifecycle counts, not native Folly Q or on-CPU. Compare residence with measured callback CPU; residual is not pure scheduler wait. Final callback tail shown separately.'}
    args.output.mkdir(parents=True)
    with (args.output/'request_cpu_windows.csv').open('w', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
    (args.output/'profile.json').write_text(json.dumps(result, indent=2)+'\n')
    public = {k: v for k, v in result.items() if k != 'task_residence_audit'}
    print('REQUEST_SCHED_PROFILE '+json.dumps(public, sort_keys=True))
    print('REQUEST_CPU_WINDOWS_BEGIN '+','.join(rows[0]))
    for row in rows:
        print(','.join('' if v is None else str(v) for v in row.values()))
    print('REQUEST_CPU_WINDOWS_END')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        print('request scheduler profile unavailable: '+str(exc), file=sys.stderr)
        sys.exit(2)

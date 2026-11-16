#!/usr/bin/env python3
"""Integrate tracefs mono idle transitions into fixed CPU-residency windows.

The metric is time with a non-idle task selected by the scheduler, including any
interrupt time charged while it is selected. It is NOT IRQ-exact CPU utilization.
Unknown CPUs never become zero utilization or a smaller normalization denominator.
Only the Python standard library is needed, including for SVG output.
"""
import argparse
import bisect
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import re
from collections import defaultdict

EVENT = re.compile(r'\[(\d+)\].*?\s(\d+\.\d+):\s+(?:sched:)?sched_switch:\s+(.*)')
PID = re.compile(r'\b(prev_pid|next_pid)=(\d+)\b')
COLORS = ['#111827', '#2563eb', '#ea580c', '#16a34a', '#9333ea', '#dc2626', '#0891b2']
LIMITATIONS = [
    'Scheduler non-idle task residency is not IRQ-exact CPU utilization; idle-context IRQ/softirq work is not reconstructed.',
    'The configured bin width is an integration window, not an independent /proc/stat accounting sample.',
    'No-event CPUs are unknown; every scope retains its original logical-CPU denominator.',
    'Aligned curves average overlapping request contexts, are descriptive, and are not independent repetitions or guaranteed steady state.',
    'The trace observes all tasks on selected CPUs, not only this process. CPU hotplug during capture is unsupported.',
]


def read_json(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def resolve(root, path):
    path = Path(path)
    return path if path.is_absolute() else root/path


def write_csv(path, rows, fields=None):
    with Path(path).open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]) if rows else fields or [])
        writer.writeheader()
        writer.writerows(rows)


def stamp_ns(value):
    whole, fraction = value.split('.')
    if len(fraction) > 9:
        raise ValueError('timestamp has greater than nanosecond precision')
    return int(whole)*1_000_000_000+int(fraction.ljust(9, '0'))


def parse_trace(path, cpu_ids, start_ns, end_ns):
    """Validate per-CPU ordering/idle state; global output need not be time sorted."""
    events = {cpu: [] for cpu in cpu_ids}
    issues = defaultdict(list)
    global_issues = []
    with Path(path).open(errors='replace') as handle:
        for line_number, line in enumerate(handle, 1):
            if re.search(r'\bLOST\s+\d+\s+EVENTS\b', line, re.I):
                global_issues.append('trace_reports_lost_events')
            if 'sched_switch:' not in line:
                continue
            match = EVENT.search(line)
            if not match:
                global_issues.append(f'unparseable_sched_switch_line:{line_number}')
                continue
            cpu = int(match.group(1))
            if cpu not in events:
                global_issues.append(f'unexpected_cpu:{cpu}')
                continue
            try:
                when = stamp_ns(match.group(2))
                pids = dict(PID.findall(match.group(3)))
                prev, nxt = int(pids['prev_pid']), int(pids['next_pid'])
            except (ValueError, KeyError):
                issues[cpu].append('malformed_event')
                continue
            events[cpu].append((when, prev != 0, nxt != 0))
    tracks = {}
    for cpu, items in events.items():
        if not items:
            tracks[cpu] = {'status': 'unknown', 'issues': ['no_idle_transition_observed'], 'intervals': [], 'events': 0}
            continue
        last = None
        state = None
        for when, before, after in items:
            if last is not None and when < last:
                issues[cpu].append('per_cpu_time_reversal')
            if state is not None and before != state:
                issues[cpu].append('idle_state_discontinuity')
            if before == after:
                issues[cpu].append('not_an_idle_transition')
            state, last = after, when
        if not any(start_ns <= event[0] <= end_ns for event in items):
            issues[cpu].append('no_event_in_capture_window')
        if issues[cpu]:
            tracks[cpu] = {'status': 'invalid', 'issues': sorted(set(issues[cpu])), 'intervals': [], 'events': len(items)}
            continue
        # First event's previous state identifies state back to capture start.
        # The last event's next state persists until capture end, absent loss.
        intervals = []
        cursor, state = start_ns, items[0][1]
        for when, before, after in items:
            if when < start_ns:
                state = after
                continue
            if when > end_ns:
                break
            if state and when > cursor:
                intervals.append((cursor, when))
            cursor, state = when, after
        if state and cursor < end_ns:
            intervals.append((cursor, end_ns))
        tracks[cpu] = {'status': 'ok', 'issues': [], 'intervals': intervals, 'events': len(items)}
    return tracks, sorted(set(global_issues))


class Integral:
    def __init__(self, intervals):
        self.starts = [a for a, _ in intervals]
        self.ends = [b for _, b in intervals]
        self.prefix = [0]
        for a, b in intervals:
            self.prefix.append(self.prefix[-1]+b-a)

    def at(self, time_ns):
        index = bisect.bisect_right(self.starts, time_ns)-1
        if index < 0:
            return 0
        return self.prefix[index]+min(time_ns, self.ends[index])-self.starts[index]

    def between(self, start, end):
        return self.at(end)-self.at(start)


def scope_value(cpu_ids, tracks, integrals, start, end):
    known = [cpu for cpu in cpu_ids if tracks[cpu]['status'] == 'ok']
    known_busy = sum(integrals[cpu].between(start, end) for cpu in known)
    capacity = len(cpu_ids)*(end-start)
    complete = len(known) == len(cpu_ids)
    return {'busy_ns': known_busy if complete else None, 'known_busy_ns': known_busy,
            'capacity_ns': capacity, 'known_capacity_ns': len(known)*(end-start),
            'known_cpus': len(known), 'total_cpus': len(cpu_ids),
            'coverage': len(known)/len(cpu_ids),
            'occupancy_pct': 100*known_busy/capacity if complete and capacity else None,
            'status': 'ok' if complete else 'no_data'}


def make_curve(tracks, scopes, start, end, epoch, bin_ns):
    integrals = {cpu: Integral(track['intervals']) for cpu, track in tracks.items()}
    rows = []
    for nominal_start in range(start//bin_ns*bin_ns, end, bin_ns):
        left, right = max(start, nominal_start), min(end, nominal_start+bin_ns)
        for scope, cpus in scopes.items():
            rows.append({'scope': scope, 'start_monotonic_ns': left, 'end_monotonic_ns': right,
                         'relative_start_ms': (left-epoch)/1e6, 'relative_end_ms': (right-epoch)/1e6,
                         'complete_bin': int(right-left == bin_ns),
                         **scope_value(cpus, tracks, integrals, left, right)})
    return rows, integrals



def curve_diagnostics(rows, scopes, start, end, bin_ns):
    """Describe full bins only; gaps break streaks and prevent an exact high fraction.

    Peak and longest streak are explicitly *observed* statistics if coverage is
    partial. They are never presented as proof that unobserved windows were low.
    90 percent is only a diagnostic convention, not a routing threshold.
    """
    first = -(-start//bin_ns)*bin_ns
    last = end//bin_ns*bin_ns
    expected = max(0, (last-first)//bin_ns)
    result = {}
    for scope in scopes:
        selected = sorted((row for row in rows if row['scope'] == scope and
                           row['complete_bin'] and row['start_monotonic_ns'] >= first and
                           row['end_monotonic_ns'] <= last and
                           row['end_monotonic_ns']-row['start_monotonic_ns'] == bin_ns),
                          key=lambda row: row['start_monotonic_ns'])
        values = []
        high_ns = longest_ns = streak_ns = 0
        previous_end = None
        for row in selected:
            value = row['occupancy_pct']
            if value is not None:
                values.append(value)
            if value is not None and value >= 90:
                streak_ns = streak_ns+bin_ns if row['start_monotonic_ns'] == previous_end else bin_ns
                high_ns += bin_ns
                longest_ns = max(longest_ns, streak_ns)
            else:
                streak_ns = 0
            previous_end = row['end_monotonic_ns']
        complete = expected > 0 and len(values) == expected
        result[scope] = {'expected_complete_bins': expected, 'observed_complete_bins': len(values),
                         'complete_bin_coverage': len(values)/expected if expected else 0,
                         'observed_complete_bin_peak_pct': max(values) if values else None,
                         'ge90_duration_fraction': high_ns/(expected*bin_ns) if complete else None,
                         'observed_ge90_ms': high_ns/1e6 if values else None,
                         'longest_observed_ge90_ms': longest_ns/1e6 if values else None,
                         'status': 'ok' if complete else 'partial_or_no_data'}
    return result


def load_requests(path, epoch, warmup, duration):
    requests = {}
    with Path(path).open(newline='') as handle:
        for row in csv.DictReader(handle):
            request_ns = int(row['request_planned_ns'])
            if not warmup*1e9 <= request_ns < duration*1e9:
                continue
            burst, home = int(row['burst']), int(row['home_pool'])
            request = requests.setdefault(burst, {'burst': burst, 'home_pool': home,
                'planned_monotonic_ns': epoch+request_ns, 'initial_submits': [], 'task_count': 0})
            if request['home_pool'] != home or request['planned_monotonic_ns'] != epoch+request_ns:
                raise ValueError('inconsistent_request_identity')
            request['task_count'] += 1
            if row['phase'] == 'initial':
                request['initial_submits'].append(epoch+int(row['submit_ns']))
    result = []
    for request in sorted(requests.values(), key=lambda r: r['planned_monotonic_ns']):
        submits = request.pop('initial_submits')
        request['initial_start_monotonic_ns'] = min(submits) if submits else None
        request['initial_end_monotonic_ns'] = max(submits) if submits else None
        result.append(request)
    return result


def align_requests(requests, scopes, tracks, integrals, capture_start, capture_end, bin_ns):
    """Retain all request tasks; use own home CPU group for descriptive alignment."""
    accum = defaultdict(list)
    windows = defaultdict(list)
    fixed = ((0, 30), (30, 100), (100, 200), (200, 300), (300, 420))
    for request in requests:
        home = f"pool{request['home_pool']}"
        cpus = scopes[home]
        for offset in range(-50_000_000, 450_000_000, bin_ns):
            left = request['planned_monotonic_ns']+offset
            right = left+bin_ns
            if capture_start <= left < right <= capture_end:
                value = scope_value(cpus, tracks, integrals, left, right)['occupancy_pct']
                if value is not None:
                    for scope in ('home_mean', home):
                        accum[(scope, offset)].append(value)
        for a, b in fixed:
            left = request['planned_monotonic_ns']+a*1_000_000
            right = request['planned_monotonic_ns']+b*1_000_000
            if capture_start <= left < right <= capture_end:
                value = scope_value(cpus, tracks, integrals, left, right)['occupancy_pct']
                if value is not None:
                    windows[(a, b)].append(value)
    rows = []
    for scope in ['home_mean']+[s for s in scopes if s != 'union']:
        expected = len(requests) if scope == 'home_mean' else sum(f"pool{r['home_pool']}" == scope for r in requests)
        for offset in range(-50_000_000, 450_000_000, bin_ns):
            values = accum[(scope, offset)]
            rows.append({'scope': scope, 'relative_start_ms': offset/1e6,
                         'relative_end_ms': (offset+bin_ns)/1e6,
                         'occupancy_pct': sum(values)/len(values) if values else None,
                         'requests': len(values), 'expected_requests': expected,
                         'coverage': len(values)/expected if expected else 0})
    summary = [{'start_ms': a, 'end_ms': b, 'requests': len(windows[(a, b)]),
                'expected_requests': len(requests),
                'mean_home_occupancy_pct': sum(windows[(a, b)])/len(windows[(a, b)]) if windows[(a, b)] else None}
               for a, b in fixed]
    return rows, summary


def svg_curve(path, rows, title, low_ms, high_ms, requests=(), epoch=0, aligned=False):
    width, height = 1250, 570
    left, top, plot_w, plot_h = 75, 100, 1135, 300
    scope_names = list(dict.fromkeys(row['scope'] for row in rows))
    def x(t):
        return left+(t-low_ms)/(high_ms-low_ms)*plot_w
    def y(p):
        return top+(100-p)/100*plot_h
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{left}" y="26" font-family="sans-serif" font-size="19">{html.escape(title)}</text>',
             f'<text x="{left}" y="48" font-family="sans-serif" font-size="12">Non-idle task residency (%), not IRQ-exact CPU utilization. Missing data is not zero.</text>']
    for pct in (0, 25, 50, 75, 100):
        parts.append(f'<path d="M {left},{y(pct)} H {left+plot_w}" stroke="#ddd"/><text x="{left-8}" y="{y(pct)+4}" text-anchor="end" font-family="sans-serif" font-size="12">{pct}</text>')
    for tick in range(6):
        time = low_ms+(high_ms-low_ms)*tick/5
        parts.append(f'<text x="{x(time)}" y="422" text-anchor="middle" font-family="sans-serif" font-size="12">{time:.1f}</text>')
    if aligned and low_ms < 0 < high_ms:
        parts.append(f'<path d="M {x(0)},{top} V {top+plot_h}" stroke="#888" stroke-dasharray="4 4"/>')
        parts.append(f'<text x="{x(0)}" y="422" text-anchor="middle" font-family="sans-serif" font-size="12">0</text>')
    for index, scope in enumerate(scope_names):
        color = COLORS[index % len(COLORS)]
        parts.append(f'<text x="{left+index*150}" y="78" fill="{color}" font-family="sans-serif" font-size="13">{html.escape(scope)}</text>')
        series = [r for r in rows if r['scope'] == scope and r['relative_end_ms'] > low_ms and r['relative_start_ms'] < high_ms]
        # At full-run scale preserve min/max per horizontal pixel, never average away peaks.
        groups = defaultdict(list)
        for row in series:
            if row['occupancy_pct'] is not None:
                groups[int(x(max(low_ms, row['relative_start_ms'])))].append(row)
        if series and any(r['occupancy_pct'] is None for r in series):
            parts.append(f'<text x="{left+index*150}" y="94" fill="{color}" font-family="sans-serif" font-size="10">incomplete / no data</text>')
        selected = []
        for group in groups.values():
            chosen = {id(r): r for r in (group[0], min(group, key=lambda r: r['occupancy_pct']), max(group, key=lambda r: r['occupancy_pct']), group[-1])}
            selected.extend(sorted(chosen.values(), key=lambda r: r['relative_start_ms']))
        segments, current, previous_end = [], [], None
        for row in selected:
            # A gap due to omitted unknown windows must never become a plotted interpolation.
            if previous_end is not None and row['relative_start_ms'] > previous_end+max(1e-6, (high_ms-low_ms)/plot_w*2):
                if current:
                    segments.append(current)
                current = []
            time = max(low_ms, min(high_ms, (row['relative_start_ms']+row['relative_end_ms'])/2))
            current.append(f'{x(time):.2f},{y(row["occupancy_pct"]):.2f}')
            previous_end = row['relative_end_ms']
        if current:
            segments.append(current)
        for points in segments:
            parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.2"/>')
    label = 'Milliseconds from each request planned time (averaged waveform)' if aligned else 'Milliseconds from workload epoch (individual run timeline)'
    parts.append(f'<text x="{left}" y="445" font-family="sans-serif" font-size="12">{label}</text>')
    if not aligned:
        for request in requests:
            begin, end = request['initial_start_monotonic_ns'], request['initial_end_monotonic_ns']
            if begin is None:
                continue
            a, b = (begin-epoch)/1e6, (end-epoch)/1e6
            if b < low_ms or a > high_ms:
                continue
            pool = request['home_pool']
            parts.append(f'<rect x="{x(max(a, low_ms)):.2f}" y="{465+pool*11}" width="{max(1, x(min(b, high_ms))-x(max(a, low_ms))):.2f}" height="8" fill="{COLORS[(pool+1)%len(COLORS)]}" opacity=".7"><title>home pool {pool}, request {request["burst"]}: actual initial submissions</title></rect>')
        parts.append(f'<text x="{left}" y="555" font-family="sans-serif" font-size="11">Bottom tracks: actual initial submission spans by home pool. Full-run lines retain per-pixel extrema.</text>')
    else:
        parts.append(f'<text x="{left}" y="480" font-family="sans-serif" font-size="12">All subsequent task activity is retained. Later intervals are not guaranteed steady state.</text>')
    parts.append('</svg>')
    Path(path).write_text('\n'.join(parts)+'\n')


def status_issues(status, cpus):
    issues = []
    if status.get('status') != 'ok':
        issues.append('capture_status_not_ok')
    if status.get('clock') != 'mono':
        issues.append('trace_clock_not_mono')
    if sorted(status.get('cpu_ids', [])) != sorted(cpus):
        issues.append('capture_cpu_ids_mismatch')
    for key in ('lost_events', 'overrun', 'dropped_events'):
        if status.get(key, 0):
            issues.append(f'capture_{key}')
    if status.get('cpu_hotplug_detected') or status.get('hotplug_detected'):
        issues.append('cpu_hotplug_unsupported')
    before, after = status.get('online_cpu_ids_start'), status.get('online_cpu_ids_end')
    if before is not None and after is not None and before != after:
        issues.append('cpu_hotplug_unsupported')
    if status.get('lost_event_marker'):
        issues.append('capture_lost_event_marker')
    for cpu in cpus:
        stats = status.get('stats', {}).get(str(cpu), {})
        for key in ('overrun', 'commit overrun', 'dropped events'):
            if key not in stats:
                issues.append(f'missing_loss_counter:{cpu}:{key}')
            elif stats[key] != 0:
                issues.append(f'capture_loss:{cpu}:{key}')
    if status.get('errors'):
        issues.extend('capture:'+str(value) for value in status['errors'])
    if status.get('issues'):
        issues.extend('capture:'+str(value) for value in status['issues'])
    return issues


def analyze_run(plan, entry, root, output, bin_ns, global_issues):
    policy = str(entry.get('policy', 'unknown'))
    if not re.fullmatch(r'[A-Za-z0-9_-]+', policy):
        raise ValueError('invalid_policy_path')
    destination = output/policy
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('cpu_curve.csv', 'aligned_home_curve.csv', 'aligned_windows.csv', 'request_markers.csv', 'cpu_full.svg', 'cpu_zoom.svg', 'cpu_aligned.svg'):
        (destination/name).unlink(missing_ok=True)
    issues = list(global_issues)
    if entry.get('status') != 'completed' or entry.get('returncode', 0) != 0:
        issues.append('workload_run_not_successful')
    directory = resolve(root, entry['path'])
    raw = resolve(root, entry.get('raw_dir', str(Path(entry['path'])/'data')))
    status = read_json(directory/'cpu_trace_status.json')
    meta = read_json(raw/'metadata.json')
    cpus = list(plan['cpu_union'])
    scopes = {'union': cpus, **{f'pool{i}': group for i, group in enumerate(plan['pool_cpus'])}}
    if sorted(cpu for group in plan['pool_cpus'] for cpu in group) != sorted(cpus) or len(set(cpus)) != len(cpus):
        issues.append('invalid_or_overlapping_plan_cpu_groups')
    if meta.get('effective_masks') != plan['pool_cpus']:
        issues.append('metadata_cpu_masks_mismatch')
    issues.extend(status_issues(status, cpus))
    if entry.get('cpu_trace') is not None and entry['cpu_trace'] != status:
        issues.append('manifest_capture_status_mismatch')
    for name, expected in entry.get('artifact_sha256', {}).items():
        path = directory/name
        if not path.is_file() or file_digest(path) != expected:
            issues.append('artifact_hash_mismatch:'+name)
    start, end = int(status.get('start_monotonic_ns', 0)), int(status.get('end_monotonic_ns', 0))
    epoch = int(meta['epoch_monotonic_ns'])
    if not 0 <= start < end:
        issues.append('invalid_capture_interval')
    result = {'policy': policy, 'status': 'invalid', 'issues': sorted(set(issues)),
              'metric': 'scheduler_nonidle_task_residency_pct', 'bin_ms': bin_ns/1e6,
              'capture_start_monotonic_ns': start, 'capture_end_monotonic_ns': end,
              'observer': {key: status.get(key) for key in ('observer_process_cpu_seconds',
                  'reader_thread_cpu_seconds', 'bytes_written', 'timestamp_precision', 'observer_cpu_scope')},
              'limitations': LIMITATIONS}
    if issues:
        return result
    trace_path = directory/'cpu_trace.txt'
    tracks, trace_issues = parse_trace(trace_path, cpus, start, end)
    if status.get('bytes_written') is not None and trace_path.stat().st_size != status['bytes_written']:
        trace_issues.append('trace_byte_count_mismatch')
    for cpu, track in tracks.items():
        stats = status.get('stats', {}).get(str(cpu), {})
        if stats.get('entries', 0) != 0:
            trace_issues.append(f'undrained_trace_buffer:{cpu}')
        if stats.get('read events') is not None and stats['read events'] != track['events']:
            trace_issues.append(f'trace_event_count_mismatch:{cpu}')
    result['issues'].extend(trace_issues)
    if trace_issues:
        return result
    curves, integrals = make_curve(tracks, scopes, start, end, epoch, bin_ns)
    warmup = float(entry.get('warmup_seconds', plan['analysis']['warmup_seconds']))
    duration = float(entry.get('options', {}).get('duration', plan['analysis']['request_end_seconds']))
    requests = load_requests(raw/'tasks.csv', epoch, warmup, duration)
    if any(f"pool{r['home_pool']}" not in scopes for r in requests):
        result['issues'].append('invalid_request_home_pool')
        return result
    aligned, windows = align_requests(requests, scopes, tracks, integrals, start, end, bin_ns)
    write_csv(destination/'cpu_curve.csv', curves)
    write_csv(destination/'aligned_home_curve.csv', aligned)
    write_csv(destination/'aligned_windows.csv', windows)
    write_csv(destination/'request_markers.csv', requests,
              ['burst', 'home_pool', 'planned_monotonic_ns', 'task_count', 'initial_start_monotonic_ns', 'initial_end_monotonic_ns'])
    result['observer']['events_parsed'] = sum(track['events'] for track in tracks.values())
    result['cpu_quality'] = {str(cpu): {key: value for key, value in track.items() if key != 'intervals'} for cpu, track in tracks.items()}
    result['status'] = 'ok' if all(track['status'] == 'ok' for track in tracks.values()) else 'partial'
    result['cohort_requests'] = len(requests)
    result['cohort_tasks'] = sum(r['task_count'] for r in requests)
    result['aligned_windows'] = windows
    begin, finish = max(start, epoch+round(warmup*1e9)), min(end, epoch+round(duration*1e9))
    result['measurement_window'] = {'start_monotonic_ns': begin, 'end_monotonic_ns': finish,
        'full_capture_coverage': start <= epoch+round(warmup*1e9) and end >= epoch+round(duration*1e9),
        'scopes': {scope: scope_value(ids, tracks, integrals, begin, finish) for scope, ids in scopes.items()} if begin < finish else {}}
    result['measurement_window']['bin_diagnostics'] = curve_diagnostics(curves, scopes, begin, finish, bin_ns)
    if not result['measurement_window']['full_capture_coverage']:
        result['issues'].append('capture_does_not_cover_measurement_window')
        result['status'] = 'partial'
    low, high = (start-epoch)/1e6, (end-epoch)/1e6
    svg_curve(destination/'cpu_full.svg', curves, f'{policy}: full captured timeline', low, high, requests, epoch)
    complete = next((r for r in requests if r['initial_start_monotonic_ns'] is not None and
                     start <= r['planned_monotonic_ns'] and r['planned_monotonic_ns']+1_000_000_000 <= end), None)
    zoom = (complete['planned_monotonic_ns']-epoch)/1e6 if complete else max(low, warmup*1000)
    if zoom < high:
        svg_curve(destination/'cpu_zoom.svg', curves, f'{policy}: first captured measurement request, 1 second', zoom, min(high, zoom+1000), requests, epoch)
    svg_curve(destination/'cpu_aligned.svg', aligned, f'{policy}: mean waveform aligned to home request', -50, 450, aligned=True)
    return result


def analyze(plan_path, manifest_path, output, bin_ms=None):
    plan_path, manifest_path, output = Path(plan_path), Path(manifest_path), Path(output)
    plan, manifest = read_json(plan_path), read_json(manifest_path)
    output.mkdir(parents=True, exist_ok=True)
    bin_ms = bin_ms if bin_ms is not None else plan.get('cpu_trace', {}).get('bin_ms', 5)
    if not math.isfinite(bin_ms) or bin_ms <= 0 or round(bin_ms*1e6) < 1:
        raise ValueError('bin-ms must be finite and positive')
    bin_ns = round(bin_ms*1e6)
    issues = []
    if digest({key: value for key, value in plan.items() if key != 'plan_sha256'}) != plan.get('plan_sha256'):
        issues.append('plan_hash_mismatch')
    if manifest.get('plan_sha256') != plan.get('plan_sha256'):
        issues.append('manifest_plan_hash_mismatch')
    policies = [entry.get('policy') for entry in manifest['runs']]
    if len(set(policies)) != len(policies):
        issues.append('duplicate_policy_runs_not_supported')
    runs = []
    for entry in manifest['runs']:
        try:
            result = analyze_run(plan, entry, manifest_path.parent, output, bin_ns, issues)
        except (OSError, KeyError, TypeError, ValueError) as error:
            result = {'policy': entry.get('policy'), 'status': 'invalid', 'issues': [f'{type(error).__name__}: {error}']}
        runs.append(result)
    report = {'schema_version': 1, 'metric': 'scheduler_nonidle_task_residency_pct',
              'status': 'ok' if runs and all(r['status'] == 'ok' for r in runs) else 'partial_or_invalid',
              'bin_ms': bin_ns/1e6, 'runs': runs, 'limitations': LIMITATIONS}
    (output/'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    lines = ['CPU TRACE: scheduler non-idle task residency; not IRQ-exact utilization', f'bin_ms={bin_ns/1e6:g} status={report["status"]}']
    for run in runs:
        lines.append(f'\nPOLICY {run["policy"]}: {run["status"]}; issues={",".join(run["issues"]) or "none"}')
        if run.get('observer'):
            lines.append('OBSERVER '+json.dumps(run['observer'], sort_keys=True))
        if 'cpu_quality' in run:
            bad = {cpu: quality for cpu, quality in run['cpu_quality'].items() if quality['status'] != 'ok'}
            lines.append(f'CPU coverage: {len(run["cpu_quality"])-len(bad)}/{len(run["cpu_quality"])}; unknown/invalid={json.dumps(bad, sort_keys=True)}')
            lines.append('MEASUREMENT WINDOW full_capture_coverage='+str(run['measurement_window']['full_capture_coverage']))
            lines.append('scope,covered_window_mean_pct,observed_complete_bin_peak_pct,ge90_duration_fraction,longest_observed_ge90_ms,complete_bins,bin_coverage,cpu_coverage')
            def number(value):
                return 'NA' if value is None else f'{value:.3f}'
            for scope, row in run['measurement_window']['bin_diagnostics'].items():
                mean = run['measurement_window']['scopes'].get(scope, {})
                lines.append(','.join((scope, number(mean.get('occupancy_pct')), number(row['observed_complete_bin_peak_pct']),
                    number(row['ge90_duration_fraction']), number(row['longest_observed_ge90_ms']),
                    f'{row["observed_complete_bins"]}/{row["expected_complete_bins"]}', number(row['complete_bin_coverage']), number(mean.get('coverage')))))
            lines.append('90% is a descriptive bin threshold, not a routing threshold. Observed peaks/streaks do not bound missing windows.')
            lines.append('relative_window_ms,requests,mean_home_nonidle_residency_pct')
            for row in run['aligned_windows']:
                value = row['mean_home_occupancy_pct']
                lines.append(f'{row["start_ms"]}-{row["end_ms"]},{row["requests"]}/{row["expected_requests"]},{value:.3f}' if value is not None else f'{row["start_ms"]}-{row["end_ms"]},0/{row["expected_requests"]},NA')
        safe_policy = str(run['policy'])
        if re.fullmatch(r'[A-Za-z0-9_-]+', safe_policy):
            directory = output/safe_policy
            directory.mkdir(exist_ok=True)
            (directory/'quality.json').write_text(json.dumps(run, ensure_ascii=False, indent=2)+'\n')
            (directory/'report.txt').write_text(f'{safe_policy}: {run["status"]}\n'+json.dumps(run.get('issues', []))+'\n'+'\n'.join(LIMITATIONS)+'\n')
    lines += ['\nLIMITATIONS']+LIMITATIONS
    (output/'report.txt').write_text('\n'.join(lines)+'\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--bin-ms', type=float)
    args = parser.parse_args(argv)
    report = analyze(args.plan, args.manifest, args.output, args.bin_ms)
    print((args.output/'report.txt').read_text(), end='')
    return 0 if report['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())

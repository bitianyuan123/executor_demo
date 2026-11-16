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
WAVEFORM_CONTRACT = {
    'version': 1, 'kind': 'provisional_synthetic_profile_acceptance_not_business_calibration',
    'high_pct': 90., 'sensitivity_high_pct': [80., 90., 95.],
    'maximum_bin_ms': 2., 'minimum_complete_requests': 30,
    'minimum_complete_fraction': .95, 'minimum_onset_fraction': .90,
    'near_onset_start_by_ms': 5., 'onset_region_end_ms': 30.,
    'median_peak_width_range_ms': [5., 20.], 'p90_peak_width_max_ms': 25.,
    'extra_onset_high_tolerance_ms': 2., 'maximum_fragmented_fraction': .10,
    'background_median_range_pct': [35., 65.],
    'background_high_fraction_p90_max': .10, 'background_longest_high_p90_max_ms': 10.,
    'union_mean_range_pct': [45., 55.],
    'minimum_background_window_fraction': .90,
}


def quantile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered)*fraction)-1)] if ordered else None


def distribution(values):
    return {'n': len(values), 'median': quantile(values, .5), 'p90': quantile(values, .9),
            'max': max(values) if values else None}


def waveform_unavailable(plan, policy, reason):
    applicable = plan.get('profile') == 'short-peak' and policy == 'home'
    return {'status': 'insufficient_data' if applicable else 'not_applicable',
            'passed': False, 'issues': [reason] if applicable else [],
            'reasons': [reason] if applicable else [], 'contract': WAVEFORM_CONTRACT,
            'applicable': applicable}


def high_runs(bins, threshold, left_ms=0., right_ms=math.inf):
    result = []
    for start, end, value in bins:
        start, end = max(start, left_ms), min(end, right_ms)
        if end <= start or value < threshold:
            continue
        if result and abs(result[-1][1]-start) < 1e-8:
            result[-1] = (result[-1][0], end)
        else:
            result.append((start, end))
    return result


def home_period_ms(plan):
    value = plan.get('nominal', {}).get('per_pool_request_gap_ms')
    if value is None:
        rate = plan.get('base', {}).get('arrival-rate', 0)
        value = 1000*len(plan['pool_cpus'])/rate if rate else 0
    return value


def next_home_starts(requests):
    result, following = {}, {}
    for request in sorted(requests, key=lambda r: r['planned_monotonic_ns'], reverse=True):
        home = request['home_pool']
        result[request['burst']] = following.get(home)
        if request['initial_start_monotonic_ns'] is not None:
            following[home] = request['initial_start_monotonic_ns']
    return result


def request_waveform(plan, policy, requests, scopes, tracks, integrals, capture_start,
                     capture_end, bin_ns, measurement):
    """A fixed provisional home-shape contract, applied to individual waves.

    Values at 80/95% are sensitivity descriptions from the identical trace; they
    never replace the predeclared 90% acceptance threshold. Missing/shortened
    waves remain in the denominator, and a subsequent home request truncates the
    background region so delayed submission cannot include the next burst.
    """
    if plan.get('profile') != 'short-peak':
        return waveform_unavailable(plan, policy, ''), []
    contract = WAVEFORM_CONTRACT
    frozen = plan.get('waveform_acceptance')
    if frozen is not None and (not isinstance(frozen, dict) or
            any(frozen.get(key) != value for key, value in contract.items())):
        return waveform_unavailable(plan, policy, 'frozen_waveform_contract_mismatch'), []
    period_ms = home_period_ms(plan)
    if not math.isfinite(period_ms) or period_ms <= 30:
        return waveform_unavailable(plan, policy, 'home_period_must_exceed_30ms'), []
    next_start = next_home_starts(requests)
    rows = []
    thresholds = contract['sensitivity_high_pct']
    for request in requests:
        anchor = request['initial_start_monotonic_ns']
        row = {'burst': request['burst'], 'home_pool': request['home_pool'],
               'planned_monotonic_ns': request['planned_monotonic_ns'],
               'actual_initial_start_monotonic_ns': anchor,
               'anchor_lag_ms': (anchor-request['planned_monotonic_ns'])/1e6 if anchor is not None else None,
               'period_ms': period_ms, 'next_home_actual_start_monotonic_ns': next_start[request['burst']],
               'window_end_monotonic_ns': None, 'background_window_ms': None,
               'background_window_fraction': None, 'clipped_by_next_request': False,
               'complete': False, 'issues': '', 'background_mean_pct': None}
        for threshold in thresholds:
            prefix = f'high{threshold:g}_'
            row.update({prefix+key: None for key in ('onset_start_ms', 'onset_width_ms',
                'extra_onset_high_ms', 'background_high_fraction', 'background_longest_high_ms')})
        issues = []
        cpus = scopes[f"pool{request['home_pool']}"]
        if anchor is None:
            issues.append('missing_initial_submit')
        elif any(tracks[cpu]['status'] != 'ok' for cpu in cpus):
            issues.append('incomplete_home_cpu_coverage')
        else:
            nominal_end = anchor+round(period_ms*1e6)
            end = min(nominal_end, next_start[request['burst']] or nominal_end)
            background_ms = (end-anchor)/1e6-30
            row.update(window_end_monotonic_ns=end, background_window_ms=max(0., background_ms),
                       background_window_fraction=max(0., background_ms)/(period_ms-30),
                       clipped_by_next_request=end < nominal_end)
            if not capture_start <= anchor < end <= capture_end:
                issues.append('capture_does_not_cover_request_window')
            if background_ms < (period_ms-30)*contract['minimum_background_window_fraction']:
                issues.append('next_home_request_shortens_background')
            if not issues:
                bins = []
                for left in range(anchor, end, bin_ns):
                    right = min(left+bin_ns, end)
                    value = scope_value(cpus, tracks, integrals, left, right)['occupancy_pct']
                    bins.append(((left-anchor)/1e6, (right-anchor)/1e6, value))
                background_start = anchor+30_000_000
                row['background_mean_pct'] = scope_value(cpus, tracks, integrals, background_start, end)['occupancy_pct']
                row['complete'] = True
                for threshold in thresholds:
                    prefix = f'high{threshold:g}_'
                    runs = high_runs(bins, threshold)
                    onset = next((run for run in runs if run[0] <= contract['near_onset_start_by_ms']), None)
                    early = high_runs(bins, threshold, 0, 30)
                    extra = sum(b-a for a, b in early)
                    if onset:
                        extra -= max(0., min(30., onset[1])-onset[0])
                    background = high_runs(bins, threshold, 30, (end-anchor)/1e6)
                    row.update({prefix+'onset_start_ms': onset[0] if onset else None,
                        prefix+'onset_width_ms': onset[1]-onset[0] if onset else None,
                        prefix+'extra_onset_high_ms': max(0., extra),
                        prefix+'background_high_fraction': sum(b-a for a, b in background)/background_ms,
                        prefix+'background_longest_high_ms': max((b-a for a, b in background), default=0.)})
        row['issues'] = ';'.join(issues)
        rows.append(row)
    complete = [row for row in rows if row['complete']]
    coverage = len(complete)/len(rows) if rows else 0.
    sensitivity = {}
    for threshold in thresholds:
        prefix = f'high{threshold:g}_'
        widths = [row[prefix+'onset_width_ms'] for row in complete if row[prefix+'onset_width_ms'] is not None]
        sensitivity[f'{threshold:g}'] = {
            'onset_fraction': len(widths)/len(complete) if complete else 0.,
            'onset_width_ms': distribution(widths),
            'fragmented_fraction': sum(row[prefix+'extra_onset_high_ms'] > contract['extra_onset_high_tolerance_ms'] for row in complete)/len(complete) if complete else None,
            'background_high_fraction': distribution([row[prefix+'background_high_fraction'] for row in complete]),
            'background_longest_high_ms': distribution([row[prefix+'background_longest_high_ms'] for row in complete]),
        }
    summary = {'applicable': policy == 'home', 'status': 'not_applicable', 'passed': False,
        'issues': [], 'reasons': [], 'contract': contract, 'alignment': 'actual_first_initial_submit',
        'background': '30ms after actual first initial submit until min(anchor+home_period,next_same_home_actual_submit)',
        'period_ms': period_ms, 'requests': len(rows), 'complete_requests': len(complete),
        'complete_fraction': coverage, 'anchor_lag_ms': distribution([r['anchor_lag_ms'] for r in rows if r['anchor_lag_ms'] is not None]),
        'clipped_by_next_request': sum(r['clipped_by_next_request'] for r in rows),
        'background_mean_pct': distribution([r['background_mean_pct'] for r in complete]),
        'sensitivity': sensitivity, 'union_mean_pct': measurement.get('scopes', {}).get('union', {}).get('occupancy_pct')}
    if policy != 'home':
        return summary, rows
    insufficient = []
    if any(track['status'] != 'ok' for track in tracks.values()) or not measurement.get('full_capture_coverage'):
        insufficient.append('complete_trace_required')
    if bin_ns/1e6 > contract['maximum_bin_ms']:
        insufficient.append('integration_bin_exceeds_2ms')
    if len(complete) < contract['minimum_complete_requests']:
        insufficient.append('fewer_than_30_complete_requests')
    if coverage < contract['minimum_complete_fraction']:
        insufficient.append('complete_request_coverage_below_95pct')
    gate = sensitivity['90']
    issues = list(insufficient)
    union = summary['union_mean_pct']
    if union is None or not 45 <= union <= 55:
        issues.append('union_mean_outside_45_55pct')
    if gate['onset_fraction'] < contract['minimum_onset_fraction']:
        issues.append('near_onset_high_run_fraction_below_90pct')
    widths = gate['onset_width_ms']
    if widths['median'] is None or not 5 <= widths['median'] <= 20:
        issues.append('median_onset_high_width_outside_5_20ms')
    if widths['p90'] is None or widths['p90'] > 25:
        issues.append('p90_onset_high_width_exceeds_25ms')
    if gate['fragmented_fraction'] is None or gate['fragmented_fraction'] > .10:
        issues.append('fragmented_onset_fraction_exceeds_10pct')
    background = summary['background_mean_pct']['median']
    if background is None or not 35 <= background <= 65:
        issues.append('background_median_outside_35_65pct')
    if gate['background_high_fraction']['p90'] is None or gate['background_high_fraction']['p90'] > .10:
        issues.append('background_p90_high_fraction_exceeds_10pct')
    if gate['background_longest_high_ms']['p90'] is None or gate['background_longest_high_ms']['p90'] > 10:
        issues.append('background_p90_longest_high_exceeds_10ms')
    summary.update(status='insufficient_data' if insufficient else 'failed' if issues else 'passed',
                   passed=not issues, issues=issues, reasons=list(issues))
    return summary, rows


def waveform_text(waveform):
    """Compact terminal summary; full contract and statistics remain in JSON."""
    def number(value, multiplier=1):
        return 'NA' if value is None else f'{value*multiplier:.3f}'
    lines = [f"REQUEST WAVEFORM status={waveform.get('status')}; passed={str(waveform.get('passed') is True).lower()}; applicable={str(waveform.get('applicable') is True).lower()}",
             'reasons='+(','.join(waveform.get('reasons', [])) or 'none')]
    if waveform.get('sensitivity'):
        lag = waveform.get('anchor_lag_ms', {})
        background = waveform.get('background_mean_pct', {})
        lines += [f"complete_waves={waveform['complete_requests']}/{waveform['requests']} ({number(waveform['complete_fraction'], 100)}%); anchor=actual_first_initial_submit; P={number(waveform['period_ms'])}ms; clipped_by_next_home={waveform['clipped_by_next_request']}",
                  f"union_mean={number(waveform['union_mean_pct'])}%; background_mean_median/p90={number(background.get('median'))}/{number(background.get('p90'))}%; anchor_lag_median/p90={number(lag.get('median'))}/{number(lag.get('p90'))}ms",
                  'high_pct,onset_wave_pct,peak_width_median_ms,peak_width_p90_ms,peak_width_max_ms,fragmented_wave_pct,background_high_p90_pct,background_longest_high_p90_ms']
        for threshold, row in waveform['sensitivity'].items():
            width = row['onset_width_ms']
            lines.append(','.join((threshold, number(row['onset_fraction'], 100), number(width['median']),
                number(width['p90']), number(width['max']), number(row['fragmented_fraction'], 100),
                number(row['background_high_fraction']['p90'], 100), number(row['background_longest_high_ms']['p90']))))
    lines += ['Provisional synthetic home-only contract, NOT business calibration: fixed high=90%; >=30 complete waves/95% coverage; onset>=90%; width median=5..20ms, p90<=25ms; fragmented<=10%; union=45..55%; background median=35..65%, p90 high-time<=10% and longest<=10ms.',
              '80/95% are sensitivity from the SAME trace, never automatically selected. Individual peak widths come from request_waveform.csv, not the aligned mean curve.']
    return lines


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


def align_requests(requests, scopes, tracks, integrals, capture_start, capture_end, bin_ns,
                   short_period_ms=None):
    """Retain all request tasks; use own home CPU group for descriptive alignment."""
    accum = defaultdict(list)
    windows = defaultdict(list)
    fixed = ((0, 30), (30, 100), (100, 200), (200, 300), (300, 420))
    low_ns, high_ns = -50_000_000, 450_000_000
    next_start = {}
    if short_period_ms is not None:
        if not math.isfinite(short_period_ms) or short_period_ms <= 30:
            raise ValueError('short-peak alignment requires a home period greater than 30 ms')
        low_ns, high_ns = -10_000_000, round(short_period_ms*1e6)
        edges = sorted({float(edge) for edge in (0, 5, 10, 20, 30, 100, 200, 300, short_period_ms)
                        if edge <= short_period_ms})
        fixed = tuple(zip(edges, edges[1:]))
        next_start = next_home_starts(requests)
    for request in requests:
        home = f"pool{request['home_pool']}"
        cpus = scopes[home]
        anchor = request['planned_monotonic_ns'] if short_period_ms is None else request['initial_start_monotonic_ns']
        if anchor is None:
            continue
        limit = capture_end if short_period_ms is None else min(capture_end, anchor+high_ns,
            next_start[request['burst']] or anchor+high_ns)
        for offset in range(low_ns, high_ns, bin_ns):
            left = anchor+offset
            right = anchor+min(offset+bin_ns, high_ns)
            observed_left, observed_right = ((left, right) if short_period_ms is None else
                (max(left, capture_start), min(right, limit)))
            if capture_start <= observed_left < observed_right <= limit:
                value = scope_value(cpus, tracks, integrals, observed_left, observed_right)['occupancy_pct']
                if value is not None:
                    for scope in ('home_mean', home):
                        accum[(scope, offset)].append((value, observed_right-observed_left))
        for a, b in fixed:
            left = anchor+round(a*1_000_000)
            right = anchor+round(b*1_000_000)
            observed_left, observed_right = ((left, right) if short_period_ms is None else
                (max(left, capture_start), min(right, limit)))
            if capture_start <= observed_left < observed_right <= limit:
                value = scope_value(cpus, tracks, integrals, observed_left, observed_right)['occupancy_pct']
                if value is not None:
                    windows[(a, b)].append((value, observed_right-observed_left))
    rows = []
    for scope in ['home_mean']+[s for s in scopes if s != 'union']:
        expected = len(requests) if scope == 'home_mean' else sum(f"pool{r['home_pool']}" == scope for r in requests)
        for offset in range(low_ns, high_ns, bin_ns):
            values = accum[(scope, offset)]
            observed_ns = sum(length for _, length in values)
            expected_ns = min(offset+bin_ns, high_ns)-offset
            row = {'scope': scope, 'relative_start_ms': offset/1e6,
                         'relative_end_ms': min(offset+bin_ns, high_ns)/1e6,
                         'occupancy_pct': sum(value*length for value, length in values)/observed_ns if observed_ns else None,
                         'requests': len(values), 'expected_requests': expected,
                         'coverage': len(values)/expected if expected else 0}
            if short_period_ms is not None:
                row.update(complete_requests=sum(length == expected_ns for _, length in values),
                    observed_duration_ms=observed_ns/1e6,
                    duration_coverage=observed_ns/(expected*expected_ns) if expected else 0.)
            rows.append(row)
    summary = []
    for a, b in fixed:
        values = windows[(a, b)]
        observed_ns = sum(length for _, length in values)
        expected_ns = round((b-a)*1e6)
        row = {'start_ms': a, 'end_ms': b, 'requests': len(values),
               'expected_requests': len(requests),
               'mean_home_occupancy_pct': sum(value*length for value, length in values)/observed_ns if observed_ns else None}
        if short_period_ms is not None:
            complete = sum(length == expected_ns for _, length in values)
            row.update(complete_requests=complete, truncated_requests=len(values)-complete,
                observed_duration_ms=observed_ns/1e6, expected_duration_ms=len(requests)*expected_ns/1e6,
                duration_coverage=observed_ns/(len(requests)*expected_ns) if requests else 0.,
                max_missing_window_ms=max([expected_ns-length for _, length in values]+
                    ([expected_ns] if len(values) < len(requests) else [0]))/1e6,
                aggregation='observed_duration_weighted_mean_not_zero_filled')
        summary.append(row)
    return rows, summary


def svg_curve(path, rows, title, low_ms, high_ms, requests=(), epoch=0, aligned=False,
              actual_alignment=False):
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
        if series and any(r['occupancy_pct'] is None or actual_alignment and
                (r.get('coverage', 1) < 1 or r.get('duration_coverage', 1) < 1) for r in series):
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
    label = ('Milliseconds from each request actual first initial submit (averaged waveform)' if actual_alignment else
             'Milliseconds from each request planned time (averaged waveform)') if aligned else 'Milliseconds from workload epoch (individual run timeline)'
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
        if actual_alignment:
            parts.append(f'<text x="{left}" y="502" font-family="sans-serif" font-size="12">Mean waveform width is NOT the distribution of individual request peak widths; use request_waveform.csv.</text>')
            parts.append(f'<text x="{left}" y="524" font-family="sans-serif" font-size="12">Actual observed durations weight partial windows. CSV records request/time coverage; missing time is not zero-filled.</text>')
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
    for name in ('cpu_curve.csv', 'aligned_home_curve.csv', 'aligned_windows.csv', 'request_markers.csv', 'request_waveform.csv', 'cpu_full.svg', 'cpu_zoom.svg', 'cpu_aligned.svg', 'cpu_aligned_peak.svg', 'cpu_peak.svg'):
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
              'request_waveform': waveform_unavailable(plan, policy, 'trace_not_validated'),
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
    short_period = home_period_ms(plan) if plan.get('profile') == 'short-peak' else None
    aligned, windows = align_requests(requests, scopes, tracks, integrals, start, end, bin_ns, short_period)
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
    if short_period is not None:
        result['aligned_curve'] = {'anchor': 'actual_first_initial_submit',
            'range_ms': [-10, short_period], 'peak_zoom_range_ms': [-5, 35],
            'next_same_home_request_clips_background': True,
            'aggregation': 'observed_duration_weighted_mean; partial windows retain request/time coverage',
            'warning': 'Phase-aligned averages do not measure the distribution of individual wave peak widths. Use request_waveform.csv; absent time is not zero-filled.'}
    begin, finish = max(start, epoch+round(warmup*1e9)), min(end, epoch+round(duration*1e9))
    result['measurement_window'] = {'start_monotonic_ns': begin, 'end_monotonic_ns': finish,
        'full_capture_coverage': start <= epoch+round(warmup*1e9) and end >= epoch+round(duration*1e9),
        'scopes': {scope: scope_value(ids, tracks, integrals, begin, finish) for scope, ids in scopes.items()} if begin < finish else {}}
    result['measurement_window']['bin_diagnostics'] = curve_diagnostics(curves, scopes, begin, finish, bin_ns)
    if not result['measurement_window']['full_capture_coverage']:
        result['issues'].append('capture_does_not_cover_measurement_window')
        result['status'] = 'partial'
    waveform, wave_rows = request_waveform(plan, policy, requests, scopes, tracks, integrals,
        start, end, bin_ns, result['measurement_window'])
    result['request_waveform'] = waveform
    if wave_rows:
        write_csv(destination/'request_waveform.csv', wave_rows)
    low, high = (start-epoch)/1e6, (end-epoch)/1e6
    svg_curve(destination/'cpu_full.svg', curves, f'{policy}: full captured timeline', low, high, requests, epoch)
    complete = next((r for r in requests if r['initial_start_monotonic_ns'] is not None and
                     start <= r['planned_monotonic_ns'] and r['planned_monotonic_ns']+1_000_000_000 <= end), None)
    zoom = (complete['planned_monotonic_ns']-epoch)/1e6 if complete else max(low, warmup*1000)
    if zoom < high:
        svg_curve(destination/'cpu_zoom.svg', curves, f'{policy}: first captured measurement request, 1 second', zoom, min(high, zoom+1000), requests, epoch)
    if short_period is None:
        svg_curve(destination/'cpu_aligned.svg', aligned, f'{policy}: mean waveform aligned to home request', -50, 450, aligned=True)
    else:
        svg_curve(destination/'cpu_aligned.svg', aligned, f'{policy}: mean home waveform, full home-request period',
                  -10, short_period, aligned=True, actual_alignment=True)
        svg_curve(destination/'cpu_aligned_peak.svg', aligned, f'{policy}: mean home waveform, short-peak zoom',
                  -5, min(35, short_period), aligned=True, actual_alignment=True)
        peak_request = next((r for r in requests if r['initial_start_monotonic_ns'] is not None and
            start <= r['initial_start_monotonic_ns']-5_000_000 and r['initial_start_monotonic_ns']+35_000_000 <= end), None)
        if peak_request:
            anchor_ms = (peak_request['initial_start_monotonic_ns']-epoch)/1e6
            svg_curve(destination/'cpu_peak.svg', curves,
                f"{policy}: actual request {peak_request['burst']} short-peak timeline (home pool{peak_request['home_pool']})",
                anchor_ms-5, anchor_ms+35, requests, epoch)
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
            result = {'policy': entry.get('policy'), 'status': 'invalid', 'issues': [f'{type(error).__name__}: {error}'],
                      'request_waveform': waveform_unavailable(plan, entry.get('policy'), 'trace_not_validated')}
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
        waveform = run.get('request_waveform', {})
        if waveform.get('status') != 'not_applicable' or waveform.get('sensitivity'):
            lines += waveform_text(waveform)
        if run.get('aligned_curve'):
            lines.append('ALIGNED CURVE '+json.dumps(run['aligned_curve'], sort_keys=True))
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
            short_alignment = bool(run.get('aligned_curve'))
            lines.append('relative_window_ms,observed_requests,complete_requests,truncated_requests,time_coverage_pct,max_missing_window_ms,observed_duration_weighted_home_residency_pct' if short_alignment else
                         'relative_window_ms,requests,mean_home_nonidle_residency_pct')
            for row in run['aligned_windows']:
                value = row['mean_home_occupancy_pct']
                if short_alignment:
                    lines.append(','.join((f'{row["start_ms"]:g}-{row["end_ms"]:g}',
                        f'{row["requests"]}/{row["expected_requests"]}', str(row['complete_requests']),
                        str(row['truncated_requests']), number(100*row['duration_coverage']),
                        number(row['max_missing_window_ms']), number(value))))
                else:
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

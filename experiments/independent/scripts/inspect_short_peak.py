#!/usr/bin/env python3
"""Inspect saved short-peak home traces without changing acceptance or running work.

The fixed 90% / 1 ms / first 30 ms view distinguishes a notch inside a short
episode from high residency after 20 ms. This is diagnostic evidence only:
the original strict waveform result, frozen plan and manifest are unchanged.
"""
import argparse
import csv
import importlib.util
import json
import math
from pathlib import Path
import tempfile

_SPEC = importlib.util.spec_from_file_location(
    'short_peak_cpu_analysis', Path(__file__).with_name('analyze_cpu_trace.py'))
a = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(a)

BIN_NS = 1_000_000
HIGH_PCT = 90.
EARLY_MS = 30.
LATE_MS = 20.
FIELDS = ['burst', 'home_pool', 'actual_initial_start_monotonic_ns',
          'initial_submit_span_ms', 'strict_complete', 'status', 'issues',
          'strict_fragmented', 'strict_extra_onset_high_ms', 'segment_count',
          'first_high_start_ms', 'last_high_end_ms', 'envelope_ms',
          'total_high_ms', 'late_20_30_high_ms', 'max_gap_ms',
          'all_gaps_at_most_1ms', 'high_at_30ms_boundary', 'category',
          'segments_ms']
MEASURES = ['initial_submit_span_ms', 'segment_count', 'envelope_ms',
            'last_high_end_ms', 'total_high_ms', 'late_20_30_high_ms',
            'max_gap_ms', 'strict_extra_onset_high_ms']


def stats(values):
    values = [value for value in values if value is not None]
    return {'n': len(values), 'median': a.quantile(values, .5),
            'p90': a.quantile(values, .9), 'p99': a.quantile(values, .99),
            'max': max(values) if values else None}


def describe_bins(bins):
    """Require complete coverage; unknown intervals must never become zero."""
    previous = 0.
    for left, right, value in bins:
        if (abs(left-previous) > 1e-8 or right <= left or right > EARLY_MS or
                value is None or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError('incomplete_or_invalid_onset_bins')
        previous = right
    if abs(previous-EARLY_MS) > 1e-8:
        raise ValueError('incomplete_or_invalid_onset_bins')
    runs = a.high_runs(bins, HIGH_PCT, 0., EARLY_MS)
    onset = next((run for run in runs if run[0] <= 5.), None)
    total = sum(end-start for start, end in runs)
    extra = total-(onset[1]-onset[0] if onset else 0.)
    gaps = [right[0]-left[1] for left, right in zip(runs, runs[1:])]
    last = runs[-1][1] if runs else None
    category = ('no_high_segment' if not runs else 'high_after_20ms' if last > LATE_MS
                else 'split_within_20ms' if len(runs) > 1 else 'single_within_20ms')
    return {'strict_fragmented': extra > 2., 'strict_extra_onset_high_ms': extra,
            'segment_count': len(runs), 'first_high_start_ms': runs[0][0] if runs else None,
            'last_high_end_ms': last, 'envelope_ms': last-runs[0][0] if runs else None,
            'total_high_ms': total,
            'late_20_30_high_ms': sum(max(0., end-max(start, LATE_MS)) for start, end in runs),
            'max_gap_ms': max(gaps) if gaps else None,
            'all_gaps_at_most_1ms': max(gaps) <= 1. if gaps else None,
            'high_at_30ms_boundary': bool(runs and abs(last-EARLY_MS) < 1e-8),
            'category': category, 'segments_ms': [list(run) for run in runs]}


def strict_signature(shape):
    gate = shape.get('sensitivity', {}).get('90', {})
    return {key: shape.get(key) for key in
            ('status', 'passed', 'reasons', 'requests', 'complete_requests')} | {
                'fragmented_fraction': gate.get('fragmented_fraction'),
                'onset_fraction': gate.get('onset_fraction')}


def group_summary(rows):
    return {'n': len(rows), 'categories': {category: sum(row['category'] == category for row in rows)
            for category in ('no_high_segment', 'single_within_20ms', 'split_within_20ms', 'high_after_20ms')},
            'touches_30ms_boundary': sum(row['high_at_30ms_boundary'] for row in rows),
            'split_with_all_gaps_at_most_1ms': sum(row['all_gaps_at_most_1ms'] is True for row in rows),
            'metrics': {key: stats([row[key] for row in rows]) for key in MEASURES}}


def compare_saved_rows(path, fresh):
    with path.open(newline='') as handle:
        saved = list(csv.DictReader(handle))
    old = {int(row['burst']): row for row in saved}
    current = {int(row['burst']): row for row in fresh}
    if len(old) != len(saved) or old.keys() != current.keys():
        return ['saved_waveform_request_ids_mismatch']
    issues = []
    for burst, row in current.items():
        for key in ('home_pool', 'actual_initial_start_monotonic_ns', 'complete',
                    'high90_onset_start_ms', 'high90_onset_width_ms', 'high90_extra_onset_high_ms'):
            left, right = old[burst].get(key, ''), row.get(key, '')
            if key == 'complete':
                same = left == right
            elif left == '' or right == '':
                same = left == right
            elif key in ('home_pool', 'actual_initial_start_monotonic_ns'):
                same = int(left) == int(right)
            else:
                same = math.isclose(float(left), float(right), rel_tol=0., abs_tol=1e-8)
            if not same:
                issues.append(f'saved_waveform_value_mismatch:{burst}:{key}')
    return issues


def inspect(plan_path, manifest_path):
    """Read inputs, validate using the authoritative analyzer, retain its result."""
    plan_path, manifest_path = Path(plan_path).resolve(), Path(manifest_path).resolve()
    root = manifest_path.parent
    result = {'schema_version': 1, 'kind': 'diagnostic_only_no_acceptance_override',
              'status': 'invalid', 'issues': [], 'plan': str(plan_path),
              'manifest': str(manifest_path), 'bin_ms': 1., 'high_pct': HIGH_PCT,
              'window_ms': [0., EARLY_MS], 'late_window_ms': [LATE_MS, EARLY_MS],
              'anchor': 'actual_first_initial_submit', 'strict_result_unchanged': True,
              'saved_checks': [], 'groups': {}, 'worst_fragmented_examples': [],
              'limitations': [
                  'Diagnostic categories do not accept the workload or unlock a policy run.',
                  'A first-30ms envelope may be right-censored; high_at_30ms_boundary flags this.',
                  'One-ms gaps are observed threshold dips, not proof of their cause.',
                  'Submission-span association is descriptive, not a causal attribution.',
                  *a.LIMITATIONS]}
    rows = []
    try:
        plan, manifest = a.read_json(plan_path), a.read_json(manifest_path)
        result.update(plan_sha256=plan.get('plan_sha256'), manifest_sha256=a.file_digest(manifest_path))
        issues = result['issues']
        if plan.get('profile') != 'short-peak':
            issues.append('short_peak_profile_required')
        if plan.get('cpu_trace', {}).get('bin_ms') != 1:
            issues.append('frozen_one_ms_bins_required_no_rebinning')
        if a.digest({key: value for key, value in plan.items() if key != 'plan_sha256'}) != plan.get('plan_sha256'):
            issues.append('plan_hash_mismatch')
        if manifest.get('plan_sha256') != plan.get('plan_sha256'):
            issues.append('manifest_plan_hash_mismatch')
        entries = manifest.get('runs', [])
        if len({entry.get('policy') for entry in entries}) != len(entries):
            issues.append('duplicate_policy_runs_not_supported')
        homes = [entry for entry in entries if entry.get('policy') == 'home']
        if len(homes) != 1:
            issues.append('exactly_one_saved_home_required')
        if issues:
            return result, rows
        entry = homes[0]
        if entry.get('plan_sha256', plan['plan_sha256']) != plan['plan_sha256']:
            issues.append('home_plan_hash_mismatch')
        if (entry.get('binary_sha256') is not None and
                entry['binary_sha256'] != manifest.get('binary_sha256')):
            issues.append('home_binary_identity_mismatch')
        # Reuse all authoritative capture, artifact, CPU-mask, clock and coverage
        # checks. Temporary derived files never replace the user's saved analysis.
        with tempfile.TemporaryDirectory(prefix='short-peak-inspection-') as directory:
            validated = a.analyze_run(plan, entry, root, Path(directory), BIN_NS, issues)
            result['trace_validation'] = {'status': validated['status'], 'issues': validated['issues']}
            result['strict_original_recomputed'] = validated['request_waveform']
            if validated['status'] != 'ok':
                issues.extend(validated['issues'] or ['incomplete_cpu_trace'])
                return result, rows
            shape = validated['request_waveform']
            if 'sensitivity' not in shape:
                issues.extend(shape.get('issues') or ['strict_waveform_unavailable'])
                return result, rows
            with (Path(directory)/'home/request_waveform.csv').open(newline='') as handle:
                fresh = list(csv.DictReader(handle))
            strict_rows = {int(row['burst']): row for row in fresh}

        saved_sources = []
        saved = manifest.get('home_waveform_gate', {}).get('home_request_waveform')
        if saved:
            saved_sources.append(('manifest.home_waveform_gate', saved))
        summary_path = root/'cpu-curves/summary.json'
        if summary_path.is_file():
            summaries = a.read_json(summary_path).get('runs', [])
            saved_sources += [('cpu-curves/summary.json', run['request_waveform']) for run in summaries
                              if run.get('policy') == 'home' and run.get('request_waveform')]
        for source, saved in saved_sources:
            matches = strict_signature(saved) == strict_signature(shape)
            result['saved_checks'].append({'source': source, 'matches': matches,
                                           'saved_strict': strict_signature(saved)})
            if not matches:
                issues.append('saved_strict_result_mismatch:'+source)
        prior_rows = root/'cpu-curves/home/request_waveform.csv'
        if prior_rows.is_file():
            row_issues = compare_saved_rows(prior_rows, fresh)
            issues.extend(row_issues)
            result['saved_checks'].append({'source': str(prior_rows), 'matches': not row_issues,
                                           'sha256': a.file_digest(prior_rows), 'rows': len(fresh)})
        if not saved_sources:
            issues.append('missing_saved_strict_result_cannot_verify_original_count')

        directory = a.resolve(root, entry['path'])
        raw = a.resolve(root, entry.get('raw_dir', str(Path(entry['path'])/'data')))
        result['trace_sha256'] = a.file_digest(directory/'cpu_trace.txt')
        capture = a.read_json(directory/'cpu_trace_status.json')
        meta = a.read_json(raw/'metadata.json')
        start, end = int(capture['start_monotonic_ns']), int(capture['end_monotonic_ns'])
        tracks, trace_issues = a.parse_trace(directory/'cpu_trace.txt', plan['cpu_union'], start, end)
        issues.extend(trace_issues)
        if any(track['status'] != 'ok' for track in tracks.values()):
            issues.append('incomplete_cpu_trace')
        if issues:
            return result, rows
        integrals = {cpu: a.Integral(track['intervals']) for cpu, track in tracks.items()}
        epoch = int(meta['epoch_monotonic_ns'])
        requests = a.load_requests(raw/'tasks.csv', epoch,
            float(entry.get('warmup_seconds', plan['analysis']['warmup_seconds'])),
            float(entry.get('options', {}).get('duration', plan['analysis']['request_end_seconds'])))
        for request in requests:
            strict = strict_rows[request['burst']]
            anchor = request['initial_start_monotonic_ns']
            last_submit = request['initial_end_monotonic_ns']
            row = {key: None for key in FIELDS}
            row.update(burst=request['burst'], home_pool=request['home_pool'],
                actual_initial_start_monotonic_ns=anchor,
                initial_submit_span_ms=(last_submit-anchor)/1e6 if anchor is not None else None,
                strict_complete=strict['complete'] == 'True', status='unavailable',
                issues=strict['issues'], segments_ms=[])
            if row['strict_complete'] and anchor is not None and start <= anchor < anchor+30_000_000 <= end:
                cpus = plan['pool_cpus'][request['home_pool']]
                bins = [(offset/1e6, (offset+BIN_NS)/1e6,
                    a.scope_value(cpus, tracks, integrals, anchor+offset, anchor+offset+BIN_NS)['occupancy_pct'])
                    for offset in range(0, 30_000_000, BIN_NS)]
                row.update(describe_bins(bins), status='ok', issues='')
                if not math.isclose(row['strict_extra_onset_high_ms'],
                                    float(strict['high90_extra_onset_high_ms']), rel_tol=0., abs_tol=1e-8):
                    issues.append(f'recomputed_fragment_measure_mismatch:{request["burst"]}')
            rows.append(row)
        complete = [row for row in rows if row['status'] == 'ok']
        fragmented = [row for row in complete if row['strict_fragmented']]
        expected = shape['sensitivity']['90']['fragmented_fraction']
        actual = len(fragmented)/len(complete) if complete else None
        result['fragment_count_check'] = {'complete': len(complete), 'fragmented': len(fragmented),
            'strict_complete': shape['complete_requests'], 'strict_fraction': expected,
            'inspection_fraction': actual,
            'matches': len(complete) == shape['complete_requests'] and actual == expected}
        if not result['fragment_count_check']['matches']:
            issues.append('fragment_count_does_not_match_strict_analysis')
        result['groups'] = {'all_complete': group_summary(complete),
            'fragmented': group_summary(fragmented),
            'not_fragmented': group_summary([row for row in complete if not row['strict_fragmented']])}
        result['requests'] = len(rows)
        result['unavailable_requests'] = len(rows)-len(complete)
        result['worst_fragmented_examples'] = sorted(fragmented, key=lambda row:
            (row['last_high_end_ms'] or -1, row['late_20_30_high_ms'], row['envelope_ms'] or -1,
             row['strict_extra_onset_high_ms']), reverse=True)[:5]
        result['background_90pct_original'] = {
            key: shape['sensitivity']['90'][key]
            for key in ('background_high_fraction', 'background_longest_high_ms')}
        result['status'] = 'invalid' if issues else 'ok'
    except (OSError, KeyError, TypeError, ValueError) as error:
        result['issues'].append(f'{type(error).__name__}: {error}')
    return result, rows


def render(result):
    def number(value):
        return 'NA' if value is None else f'{value:.3f}'
    lines = ['SHORT-PEAK OFFLINE INSPECTION — diagnostic only; no workload launched',
             f"status={result['status']}; issues={','.join(result['issues']) or 'none'}",
             'Same actual first-submit anchor, 1ms bins, >=90%; inspect [0,30)ms; late=[20,30)ms.']
    shape = result.get('strict_original_recomputed', {})
    lines += [f"ORIGINAL STRICT status={shape.get('status', 'NA')}; passed={shape.get('passed', 'NA')}; reasons={','.join(shape.get('reasons', [])) or 'none'}",
              'Original strict acceptance is unchanged; this report does not authorize source_only.']
    for check in result['saved_checks']:
        lines.append(f"SAVED CHECK {check['source']}: matches={check['matches']}")
    if 'fragment_count_check' in result:
        count = result['fragment_count_check']
        lines.append(f"FRAGMENT COUNT {count['fragmented']}/{count['complete']}; matches_strict={count['matches']}")
        lines.append(f"REQUEST COVERAGE total={result['requests']}; unavailable={result['unavailable_requests']}")
    lines.append('group,metric,n,median,p90,p99,max')
    for name, group in result['groups'].items():
        lines.append('CATEGORIES '+name+' '+json.dumps(group['categories'], sort_keys=True)+
                     f"; touches30={group['touches_30ms_boundary']}; split_with_all_gaps_le1ms={group['split_with_all_gaps_at_most_1ms']}")
        for key, values in group['metrics'].items():
            lines.append(','.join([name, key, str(values['n']),
                                  *[number(values[k]) for k in ('median', 'p90', 'p99', 'max')]]))
    lines.append('WORST FRAGMENTED (latest end first; maximum five): burst,home,submit_span_ms,last_end_ms,envelope_ms,late_high_ms,max_gap_ms,segments_ms')
    for row in result['worst_fragmented_examples']:
        lines.append(','.join([str(row['burst']), str(row['home_pool']),
            *[number(row[key]) for key in ('initial_submit_span_ms', 'last_high_end_ms',
                                         'envelope_ms', 'late_20_30_high_ms', 'max_gap_ms')],
            json.dumps(row['segments_ms'], separators=(',', ':'))]))
    if result.get('background_90pct_original'):
        lines.append('ORIGINAL BACKGROUND '+json.dumps(result['background_90pct_original'], sort_keys=True))
    lines += ['Envelope ends at the last observed high bin; touching30 is censored, not evidence of recovery.',
              'All/non-fragmented/fragmented counts retain unknown coverage as unavailable, never as zero.',
              'Submission-span differences and one-ms gaps are descriptive; causes are unproven.']
    return '\n'.join(lines)+'\n'


def guard_output(output, plan, manifest):
    """Only a matching prior inspection owns files with our output names."""
    output, plan, manifest = Path(output), Path(plan), Path(manifest)
    protected = ('plan.json', 'run_manifest.json', 'manifest.json', 'metadata.json',
                 'tasks.csv', 'cpu_trace.txt', 'summary.json', 'quality.json',
                 'request_waveform.csv', 'cpu_curve.csv', 'aligned_home_curve.csv')
    if (output.exists() and not output.is_dir()) or output.resolve() in (
            plan.parent.resolve(), manifest.parent.resolve()) or any(
            (output/name).exists() or (output/name).is_symlink() for name in protected):
        raise ValueError('--output must be a separate inspection directory, not a saved input/analysis directory')
    targets = [output/name for name in ('inspection.json', 'report.txt', 'waves.csv')]
    existing = [path for path in targets if path.exists() or path.is_symlink()]
    if not existing:
        return
    if any(path.is_symlink() or not path.is_file() for path in existing):
        raise ValueError('--output contains non-regular inspection targets; refusing to replace them')
    owner = output/'inspection.json'
    try:
        previous = a.read_json(owner)
        owned = (isinstance(previous, dict) and
                 previous.get('kind') == 'diagnostic_only_no_acceptance_override' and
                 previous.get('schema_version') == 1 and
                 Path(previous['plan']).resolve() == plan.resolve() and
                 Path(previous['manifest']).resolve() == manifest.resolve())
    except (OSError, KeyError, TypeError, ValueError):
        owned = False
    if not owned:
        raise ValueError('--output contains existing files not owned by this inspection; choose a new directory')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, help='default: run_manifest.json next to plan')
    parser.add_argument('--output', type=Path, help='default: short-peak-inspection next to manifest')
    args = parser.parse_args(argv)
    manifest = args.manifest or args.plan.parent/'run_manifest.json'
    output = args.output or manifest.parent/'short-peak-inspection'
    try:
        guard_output(output, args.plan, manifest)
    except ValueError as error:
        parser.error(str(error))
    result, rows = inspect(args.plan, manifest)
    # Inspection can take time; reject a destination populated meanwhile too.
    try:
        guard_output(output, args.plan, manifest)
    except ValueError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    (output/'inspection.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    serial = [{**row, 'segments_ms': json.dumps(row['segments_ms'], separators=(',', ':'))} for row in rows]
    a.write_csv(output/'waves.csv', serial, FIELDS)
    report = render(result)
    (output/'report.txt').write_text(report)
    print(report, end='')
    print(f'Artifacts: {output}/report.txt, inspection.json, waves.csv')
    return 0 if result['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())

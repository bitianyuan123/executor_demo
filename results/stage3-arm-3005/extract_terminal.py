#!/usr/bin/env python3
"""Archive printed aggregates faithfully; stdlib only, no raw-task reconstruction."""
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLICIES = ('home', 'guarded', 'source_only')
PHASES = ('all', 'initial', 'steady')
D = Decimal
ROUND = D('.0005')
PREFIXES = {
    'policy_summary': 'policy,valid,tasks,requests,',
    'comparisons': 'baseline,policy,S_mean_change_pct,',
    'cpu_procstat': 'policy,scope,execution_pct,',
    'initial_spans': 'policy,requests,actual_span_mean_ms,',
    'decomposition': 'policy,phase,n,requests,',
    'buckets': 'phase,metric,lower_inclusive,',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def blocks(lines, marker, end):
    result = []
    for i, line in enumerate(lines):
        if line == marker:
            stop = next(j for j in range(i, len(lines)) if lines[j].startswith(end))
            result.append({'line': i+1, 'lines': lines[i:stop+1]})
    return result


def table(lines, prefix):
    indices = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    require(len(indices) == 1, 'missing/duplicate header '+prefix)
    i = indices[0]
    header = lines[i].removesuffix(' (negative=lower latency)')
    body = []
    for line in lines[i+1:]:
        if not line or line.startswith('90% is a descriptive'):
            break
        if not line.startswith('WARNING '):
            body.append(line)
    rows = list(csv.DictReader(io.StringIO('\n'.join([header, *body]))))
    require(all(None not in r and None not in r.values() for r in rows), 'malformed table '+prefix)
    return {'fields': next(csv.reader([header])), 'rows': rows}


def write_table(name, value):
    with (ROOT/(name+'.csv')).open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=value['fields'])
        writer.writeheader()
        writer.writerows(value['rows'])


def validate_latency(tables):
    summary = {r['policy']: r for r in tables['policy_summary']['rows']}
    require(tuple(summary) == POLICIES, 'unexpected policy order')
    checks = []
    for policy, main in summary.items():
        require(main['valid'] == 'True' and main['tasks'] == '96000' and main['requests'] == '600', 'invalid counts/status')
        phases = {r['phase']: r for r in tables['decomposition']['rows'] if r['policy'] == policy}
        require(set(phases) == set(PHASES), 'unexpected phase set')
        for phase, row in phases.items():
            require(int(row['n']) == (96000 if phase == 'all' else 48000) and row['requests'] == '600', 'phase counts')
            for result, left, right in [('S_mean', 'W_mean', 'X_mean'), ('L_mean', 'S_mean', 'D_mean')]:
                require(abs(D(row[result])-D(row[left])-D(row[right])) <= 3*ROUND, 'mean decomposition exceeds rounding')
        for interval in ('S', 'W', 'X', 'L', 'D'):
            mean = (D(phases['initial'][interval+'_mean'])+D(phases['steady'][interval+'_mean']))/2
            require(abs(mean-D(phases['all'][interval+'_mean'])) <= 2*ROUND, 'phase mean consistency')
        for interval in ('S', 'L'):
            for stat in ('mean', 'p99'):
                require(main[f'{interval}_{stat}_ms'] == phases['all'][f'{interval}_{stat}'], 'summary/decomposition differ')
        cpu = [r for r in tables['cpu_procstat']['rows'] if r['policy'] == policy]
        require({r['scope'] for r in cpu} == {'union', *[f'pool{i}' for i in range(6)]}, 'CPU scope missing')
        require(all(r['status'] == 'ok' for r in cpu), 'CPU status failed')
        union = next(r for r in cpu if r['scope'] == 'union')
        require(main['CPU_union_execution_pct'] == union['execution_pct'], 'CPU summary differs')
        span = next(r for r in tables['initial_spans']['rows'] if r['policy'] == policy)
        require(span['requests'] == '600', 'span requests')
        require(abs(D(span['over_30ms_requests'])/600-D(span['over_30ms_fraction'])) <= ROUND, 'span fraction rounding')
    checks.append({'check': 'policy_phase_cpu_span_consistency', 'passed': True})
    buckets = tables['buckets']['rows']
    require(len(buckets) == 36, 'expected 36 bucket rows')
    home = {r['phase']: r for r in tables['decomposition']['rows'] if r['policy'] == 'home'}
    bounds = [('0.000', '0.500'), ('0.500', '0.800'), ('0.800', '1.000'), ('1.000', '2.000'), ('2.000', '4.000'), ('4.000', 'NA')]
    deviations = []
    for phase in PHASES:
        for metric in ('N/C', 'Q/C'):
            rows = [r for r in buckets if (r['phase'], r['metric']) == (phase, metric)]
            require([(r['lower_inclusive'], r['upper_exclusive']) for r in rows] == bounds, 'unexpected bucket bounds')
            count = sum(int(r['n']) for r in rows)
            require(count == int(home[phase]['n']), 'bucket count sum')
            for r in rows:
                require(0 <= int(r['requests']) <= min(600, int(r['n'])), 'bucket requests')
                if not int(r['n']):
                    require(r['tail_quality'] == 'EMPTY' and all(r[k] == 'NA' for k in
                            ['S_mean_ms', 'S_p99_ms', 'W_mean_ms', 'X_mean_ms', 'L_mean_ms', 'D_mean_ms']), 'empty bucket populated')
            for interval in ('S', 'W', 'X', 'L', 'D'):
                mean = sum(int(r['n'])*D(r[interval+'_mean_ms']) for r in rows if int(r['n']))/count
                difference = mean-D(home[phase][interval+'_mean'])
                require(abs(difference) <= 2*ROUND, 'bucket weighted mean exceeds rounding')
                deviations.append({'phase': phase, 'metric': metric, 'interval': interval, 'difference_ms': str(difference)})
    for metric in ('N/C', 'Q/C'):
        for lower, upper in bounds:
            rows = {r['phase']: r for r in buckets if r['metric'] == metric and r['lower_inclusive'] == lower}
            require(int(rows['all']['n']) == int(rows['initial']['n'])+int(rows['steady']['n']), 'phase bucket counts')
    checks.append({'check': '36_bucket_counts_and_weighted_means', 'passed': True, 'details': deviations})
    ratios = []
    require(len(tables['comparisons']['rows']) == 3, 'expected three pair comparisons')
    for row in tables['comparisons']['rows']:
        for interval in ('S', 'L'):
            for stat in ('mean', 'p99'):
                field = f'{interval}_{stat}_ms'
                baseline = D(summary[row['baseline']][field])
                other = D(summary[row['policy']][field])
                printed = D(row[f'{interval}_{stat}_change_pct'])
                lower = 100*((other-ROUND)/(baseline+ROUND)-1)-ROUND
                upper = 100*((other+ROUND)/(baseline-ROUND)-1)+ROUND
                require(lower <= printed <= upper, 'comparison ratio exceeds rounding bounds')
                ratios.append({'baseline': row['baseline'], 'policy': row['policy'], 'field': field,
                               'printed_change_pct': str(printed), 'allowed_lower_pct': str(lower), 'allowed_upper_pct': str(upper)})
    checks.append({'check': 'comparison_percentages_with_rounding_bounds', 'passed': True, 'details': ratios})
    return checks


def parse_trace(lines):
    require(lines[1] == 'bin_ms=5 status=ok', 'trace bin/status')
    starts = [i for i, line in enumerate(lines) if line.startswith('POLICY ')]
    require(len(starts) == 3, 'trace policy count')
    observer, scopes, aligned, coverage = [], [], [], []
    for k, (policy, start) in enumerate(zip(POLICIES, starts)):
        block = lines[start:starts[k+1] if k+1 < len(starts) else len(lines)]
        require(block[0] == f'POLICY {policy}: ok; issues=none', 'trace policy status')
        observation = json.loads(next(line[9:] for line in block if line.startswith('OBSERVER ')))
        observer.append({'policy': policy, **observation})
        cpu_coverage = next(line for line in block if line.startswith('CPU coverage:'))
        require(cpu_coverage == 'CPU coverage: 96/96; unknown/invalid={}', 'trace CPU coverage')
        require('MEASUREMENT WINDOW full_capture_coverage=True' in block, 'capture window incomplete')
        coverage.append({'policy': policy, 'valid_cpus': 96, 'expected_cpus': 96, 'full_capture_coverage': True})
        scope_table = table(block, 'scope,covered_window_mean_pct,')
        require({r['scope'] for r in scope_table['rows']} == {'union', *[f'pool{i}' for i in range(6)]}, 'trace scope set')
        for r in scope_table['rows']:
            require(r['complete_bins'] == '9999/9999' and r['bin_coverage'] == r['cpu_coverage'] == '1.000', 'incomplete trace bins')
            require(0 <= D(r['covered_window_mean_pct']) <= D(r['observed_complete_bin_peak_pct']) <= 100, 'CPU range')
            require(0 <= D(r['ge90_duration_fraction']) <= 1, 'CPU duration fraction range')
            require(D(r['longest_observed_ge90_ms']) % 5 == 0, 'streak not 5ms multiple')
            scopes.append({'policy': policy, **r})
        aligned_table = table(block, 'relative_window_ms,requests,')
        require([r['relative_window_ms'] for r in aligned_table['rows']] == ['0-30', '30-100', '100-200', '200-300', '300-420'], 'aligned windows')
        for r in aligned_table['rows']:
            require(r['requests'] == '600/600', 'aligned request coverage')
            require(0 <= D(r['mean_home_nonidle_residency_pct']) <= 100, 'aligned CPU range')
            aligned.append({'policy': policy, **r})
    return {'cpu_trace_scopes': {'fields': ['policy', *scope_table['fields']], 'rows': scopes},
            'cpu_trace_aligned_windows': {'fields': ['policy', *aligned_table['fields']], 'rows': aligned},
            'cpu_trace_observer': {'fields': list(observer[0]), 'rows': observer}}, coverage


def main():
    data = (ROOT/'3005.log').read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    require(digest == (ROOT/'source.sha256').read_text().split()[0], 'source SHA mismatch')
    text = data.decode('utf-8')
    lines = text.splitlines()
    latency = blocks(lines, 'REQUEST-WAVE DESCRIPTIVE REPORT', 'Source-trigger fractions count actual routing decisions;')
    trace = blocks(lines, 'CPU TRACE: scheduler non-idle task residency; not IRQ-exact utilization',
                   'The trace observes all tasks on selected CPUs, not only this process. CPU hotplug during capture is unsupported.')
    checks = []
    for kind, copies, count in [('latency', latency, 2), ('cpu_trace', trace, 3)]:
        require(len(copies) == count, 'unexpected report print count')
        require(all(r['lines'] == copies[0]['lines'] for r in copies), 'repeated report body differs')
        checks.append({'check': 'identical_duplicate_report_bodies', 'kind': kind, 'copies': count,
                       'first_lines': [r['line'] for r in copies], 'passed': True})
    first = latency[0]['lines']
    require('comparison_valid=true' in first, 'remote comparison validity false')
    require('request cohort: [10.0, 60.0) seconds; retain entire requests and drain' in first, 'request cohort changed')
    tables = {name: table(first, prefix) for name, prefix in PREFIXES.items()}
    checks.extend(validate_latency(tables))
    trace_tables, coverage = parse_trace(trace[0]['lines'])
    tables.update(trace_tables)
    checks.append({'check': 'trace_coverage_ranges_and_aligned_windows', 'passed': True, 'coverage': coverage,
                   'bins_per_scope': '9999/9999', 'aligned_requests_per_window': '600/600'})
    preflight = json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
    require(preflight['status'] == 'ok' and preflight['cleanup_complete'] and not preflight['errors']
            and not preflight['lost_event_marker'], 'preflight unsuccessful')
    (ROOT/'preflight_status.json').write_text(json.dumps(preflight, ensure_ascii=False, indent=2)+'\n')
    for name, value in tables.items():
        write_table(name, value)
    evidence = {
        'schema': 1,
        'source': {'file': '3005.log', 'bytes': len(data), 'sha256': digest, 'original_line_endings_preserved': True},
        'provenance': 'User supplied ARM terminal output. No raw workload tasks, trace, plan, manifest or sample CSVs received.',
        'printed_latency_reports': 2, 'printed_cpu_trace_reports': 3,
        'underlying_workload_runs_reported': {'home': 1, 'guarded': 1, 'source_only': 1},
        'duplicate_prints_are_independent_repetitions': False,
        'numeric_precision': 'All CSV aggregate values retain original printed strings; observer JSON retains original JSON numbers.',
        'context': [line for line in first if line.startswith(('INPUT ', 'RESOURCES ', 'OBSERVATION ', 'request cohort:', 'S=end-', 'comparison_'))],
        'warnings': [line for line in first if line.startswith('WARNING ')],
        'tables': {name: value['rows'] for name, value in tables.items()}, 'validation': checks,
        'preflight': {'status': preflight['status'], 'cleanup_complete': preflight['cleanup_complete'],
                      'bytes_written': preflight['bytes_written'], 'cpu_count': len(preflight['cpu_ids']), 'full_status': 'preflight_status.json'},
        'limits': [
            'Checks verify printed evidence consistency, not the underlying ARM artifact hashes, raw task p99, trace loss or reconstructed waveform.',
            'Remote analyzer reports full coverage, status=ok and no issues for all policies. Formal capture status JSON is not in this attachment.',
            'No raw events: cannot reconstruct continuous 5ms curves or independently inspect individual burst peaks/timing.',
            'Five aligned windows are descriptive averages across overlapping request contexts, not a continuous waveform or guaranteed steady state.',
            'Each scope longest_ge90 is its longest observed streak across the full measurement; min/max across six pools is not a request-duration distribution.',
            'One run per policy; no repeat-run uncertainty or statistical significance claim.',
            'No averaging or pooling p99 from buckets, phases, policies or repeated terminal prints.',
            'Source summary and component means may differ in the last decimal due to rounding.',
            'Observer CPU seconds cover parent runner including proc/stat; reader is a subset, not additive; kernel tracing overhead excluded.',
        ],
    }
    (ROOT/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    print(f'PASS sha256={digest}; latency 2 identical bodies, CPU 3 identical bodies; 3x96000 tasks / 600 requests; 36 buckets; CPU 96/96, 9999/9999 bins, 15 windows 600/600. No workload run.')


if __name__ == '__main__':
    main()

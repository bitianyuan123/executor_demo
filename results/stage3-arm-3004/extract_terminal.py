#!/usr/bin/env python3
"""Extract/validate printed aggregates; stdlib only, never reconstruct raw tasks.

Run this script without arguments from any working directory. Original decimal
strings remain unchanged in the extracted CSVs and evidence.json tables.
"""
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = 'REQUEST-WAVE DESCRIPTIVE REPORT'
PREFIXES = {
    'policy_summary': 'policy,valid,tasks,requests,',
    'comparisons': 'baseline,policy,S_mean_change_pct,',
    'cpu': 'policy,scope,execution_pct,',
    'initial_spans': 'policy,requests,actual_span_mean_ms,',
    'decomposition': 'policy,phase,n,requests,',
    'buckets': 'phase,metric,lower_inclusive,',
}
POLICIES = ('home', 'guarded', 'source_only')
PHASES = ('all', 'initial', 'steady')
METRICS = ('N/C', 'Q/C')
ROUND = Decimal('.0005')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decimal(row, field):
    return Decimal(row[field])


def table(block, prefix):
    matches = [i for i, line in enumerate(block) if line.startswith(prefix)]
    require(len(matches) <= 1, 'duplicate table header inside one report: '+prefix)
    if not matches:
        return None
    index = matches[0]
    header = block[index].removesuffix(' (negative=lower latency)')
    records = []
    for line in block[index+1:]:
        if not line:
            break
        if line.startswith('WARNING '):
            continue
        records.append(line)
    parsed = list(csv.DictReader(io.StringIO('\n'.join([header, *records]))))
    require(all(None not in row and None not in row.values() for row in parsed), 'malformed '+header)
    return {'fields': next(csv.reader([header])), 'rows': parsed}


def parse_reports(lines):
    starts = [i for i, line in enumerate(lines) if line == MARKER]
    require(len(starts) == 4, 'expected four printed reports, not four independent experiment repetitions')
    reports = []
    for pos, start in enumerate(starts):
        stop = starts[pos+1] if pos+1 < len(starts) else len(lines)
        block = lines[start:stop]
        cohort = next(line for line in block if line.startswith('request cohort:'))
        name = 'calibration' if '[5.0, 20.0)' in cohort else 'formal' if '[10.0, 60.0)' in cohort else None
        require(name is not None, 'unexpected report cohort: '+cohort)
        tables = {name: value for name, prefix in PREFIXES.items() if (value := table(block, prefix)) is not None}
        reports.append({'kind': name, 'line': start+1, 'cohort': cohort, 'tables': tables,
                        'input': [line for line in block if line.startswith(('INPUT ', 'RESOURCES ', 'OBSERVATION '))],
                        'comparison_valid': next(line.split('=', 1)[1] for line in block if line.startswith('comparison_valid=')),
                        'warnings': [line for line in block if line.startswith('WARNING ')]})
    return reports


def validate(grouped):
    checks = []
    for kind, reports in grouped.items():
        require(len(reports) == 2, 'expected exactly two printed copies of '+kind)
        for field in ('tables', 'input', 'cohort', 'comparison_valid', 'warnings'):
            require(reports[0][field] == reports[1][field], f'{kind} repeated print differs: {field}')
        checks.append({'check': 'identical_duplicate_prints', 'kind': kind, 'copies': 2,
                       'first_lines': [report['line'] for report in reports], 'passed': True})

    formal = grouped['formal'][0]['tables']
    calibration = grouped['calibration'][0]['tables']
    require(set(formal) == set(PREFIXES), 'formal report missing a table')
    require(set(calibration) == set(PREFIXES)-{'comparisons', 'buckets'}, 'unexpected calibration table set')
    require(grouped['formal'][0]['comparison_valid'] == 'true', 'formal report did not claim a valid comparison')
    require(grouped['calibration'][0]['comparison_valid'] == 'false', 'calibration must not claim a policy comparison')

    for kind, tables, policies, tasks, requests in (
            ('calibration', calibration, ('home',), 28800, 180),
            ('formal', formal, POLICIES, 96000, 600)):
        summary = tables['policy_summary']['rows']
        require([row['policy'] for row in summary] == list(policies), kind+' unexpected policy list/order')
        require(all(row['valid'] == 'True' and int(row['tasks']) == tasks and int(row['requests']) == requests
                    for row in summary), kind+' task/request count mismatch')
        require(len(tables['decomposition']['rows']) == len(policies)*3, kind+' phase table count mismatch')
        require(len(tables['cpu']['rows']) == len(policies)*7, kind+' CPU table count mismatch')
        for policy in policies:
            main = next(row for row in summary if row['policy'] == policy)
            phases = {row['phase']: row for row in tables['decomposition']['rows'] if row['policy'] == policy}
            require(set(phases) == set(PHASES), 'missing phase')
            require(int(phases['all']['n']) == tasks and int(phases['initial']['n']) == tasks//2
                    and int(phases['steady']['n']) == tasks//2, 'incorrect phase counts')
            for phase in PHASES:
                require(int(phases[phase]['requests']) == requests, 'incorrect phase request count')
                row = phases[phase]
                for result, left, right in (('S_mean', 'W_mean', 'X_mean'), ('L_mean', 'S_mean', 'D_mean')):
                    require(abs(decimal(row, result)-decimal(row, left)-decimal(row, right)) <= 3*ROUND,
                            kind+' mean decomposition beyond rounding: '+policy+'/'+phase)
            for interval in ('S', 'W', 'X', 'L', 'D'):
                average = (decimal(phases['initial'], interval+'_mean')+decimal(phases['steady'], interval+'_mean'))/2
                require(abs(average-decimal(phases['all'], interval+'_mean')) <= 2*ROUND,
                        kind+' phase mean mismatch: '+policy+'/'+interval)
            for latency in ('S', 'L'):
                for stat in ('mean', 'p99'):
                    require(main[f'{latency}_{stat}_ms'] == phases['all'][f'{latency}_{stat}'], 'summary/decomposition disagreement')
            cpu = [row for row in tables['cpu']['rows'] if row['policy'] == policy]
            require({row['scope'] for row in cpu} == {'union', *[f'pool{i}' for i in range(6)]}, 'CPU scope missing')
            require(all(row['status'] == 'ok' for row in cpu), 'CPU observation not ok')
            union = next(row for row in cpu if row['scope'] == 'union')
            require(main['CPU_union_execution_pct'] == union['execution_pct'], 'CPU union summary mismatch')
            span = next(row for row in tables['initial_spans']['rows'] if row['policy'] == policy)
            require(int(span['requests']) == requests, 'span request count mismatch')
            require(abs(Decimal(span['over_30ms_requests'])/requests-decimal(span, 'over_30ms_fraction')) <= ROUND,
                    'span proportion rounding mismatch')
        checks.append({'check': 'policy_phase_cpu_span_consistency', 'kind': kind, 'passed': True})

    buckets = formal['buckets']['rows']
    require(len(buckets) == 36, 'expected exactly 36 formal bucket rows')
    home = {row['phase']: row for row in formal['decomposition']['rows'] if row['policy'] == 'home'}
    expected_bounds = [('0.000', '0.500'), ('0.500', '0.800'), ('0.800', '1.000'),
                       ('1.000', '2.000'), ('2.000', '4.000'), ('4.000', 'NA')]
    deviations = []
    for phase in PHASES:
        for metric in METRICS:
            rows = [row for row in buckets if (row['phase'], row['metric']) == (phase, metric)]
            require([(row['lower_inclusive'], row['upper_exclusive']) for row in rows] == expected_bounds, 'bucket bounds/order mismatch')
            count = sum(int(row['n']) for row in rows)
            require(count == int(home[phase]['n']), f'{phase}/{metric} bucket count mismatch')
            for row in rows:
                require(0 <= int(row['requests']) <= int(row['n']) and int(row['requests']) <= 600, 'invalid bucket requests')
                if int(row['n']) == 0:
                    require(row['tail_quality'] == 'EMPTY' and all(row[k] == 'NA' for k in
                            ('S_mean_ms', 'S_p99_ms', 'W_mean_ms', 'X_mean_ms', 'L_mean_ms', 'D_mean_ms')), 'empty bucket is populated')
            for interval in ('S', 'W', 'X', 'L', 'D'):
                weighted = sum(int(row['n'])*decimal(row, interval+'_mean_ms') for row in rows if int(row['n']))/count
                difference = weighted-decimal(home[phase], interval+'_mean')
                require(abs(difference) <= 2*ROUND, f'{phase}/{metric}/{interval}: weighted mean exceeds rounding tolerance')
                deviations.append({'phase': phase, 'metric': metric, 'interval': interval,
                                   'weighted_minus_phase_mean_ms': str(difference)})
    for metric in METRICS:
        for lower, upper in expected_bounds:
            rowset = {r['phase']: r for r in buckets if r['metric'] == metric and r['lower_inclusive'] == lower}
            require(int(rowset['all']['n']) == int(rowset['initial']['n'])+int(rowset['steady']['n']), 'phase bucket counts do not add')
    checks.append({'check': '36_buckets_counts_and_weighted_means', 'passed': True,
                   'rounding_tolerance_ms': '.001', 'mean_deviations': deviations,
                   'p99_reconstructed_or_averaged': False})

    summary = {row['policy']: row for row in formal['policy_summary']['rows']}
    require(len(formal['comparisons']['rows']) == 3, 'expected three printed pair comparisons')
    ratio_checks = []
    for row in formal['comparisons']['rows']:
        for interval in ('S', 'L'):
            for stat in ('mean', 'p99'):
                field = f'{interval}_{stat}_ms'
                base, other = decimal(summary[row['baseline']], field), decimal(summary[row['policy']], field)
                printed = decimal(row, f'{interval}_{stat}_change_pct')
                lower = 100*((other-ROUND)/(base+ROUND)-1)-ROUND
                upper = 100*((other+ROUND)/(base-ROUND)-1)+ROUND
                require(lower <= printed <= upper, 'printed comparison exceeds rounding bounds: '+str(row))
                ratio_checks.append({'baseline': row['baseline'], 'policy': row['policy'], 'field': field,
                                     'printed_change_pct': str(printed), 'permitted_lower_pct': str(lower),
                                     'permitted_upper_pct': str(upper)})
    checks.append({'check': 'comparison_percentages_with_rounding_bounds', 'passed': True, 'details': ratio_checks})
    return checks


def main():
    source = ROOT/'3004.log'
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    recorded = (ROOT/'source.sha256').read_text().split()[0]
    require(digest == recorded, 'original attachment checksum mismatch')
    reports = parse_reports(data.decode('utf-8').splitlines())
    grouped = {kind: [r for r in reports if r['kind'] == kind] for kind in ('calibration', 'formal')}
    checks = validate(grouped)
    tables = {}
    for kind, copies in grouped.items():
        tables[kind] = {}
        for name in PREFIXES:
            value = copies[0]['tables'].get(name)
            # Missing calibration tables remain explicitly empty, not copied from formal data.
            fields = value['fields'] if value else grouped['formal'][0]['tables'][name]['fields']
            rows = value['rows'] if value else []
            with (ROOT/f'{kind}_{name}.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            tables[kind][name] = rows
    evidence = {
        'schema': 1, 'source': {'file': '3004.log', 'bytes': len(data), 'sha256': digest,
                              'original_line_endings_preserved': True},
        'provenance': 'User-supplied terminal output; no ARM raw tasks, CPU samples, plan or manifest available locally.',
        'printed_reports': 4, 'unique_report_sets': 2,
        'underlying_workload_runs_reported': {'calibration_home': 1, 'formal_home': 1, 'formal_guarded': 1, 'formal_source_only': 1},
        'duplicate_prints_are_independent_repetitions': False,
        'numeric_precision': 'CSV/table numeric values retain the original printed strings (normally three decimal places); NA remains NA.',
        'report_context': {kind: {key: copies[0][key] for key in ('cohort', 'input', 'comparison_valid', 'warnings')}
                           for kind, copies in grouped.items()},
        'tables': tables, 'validation': checks,
        'limits': ['No raw-task or CPU-sample recomputation; comparison_valid is the remote analyzer reported result.',
                   'Checks establish printed-table consistency, not independent verification of remote manifest or task trace hashes.',
                   'No p99 pooling across bins, phases, policies or repeated report prints.',
                   'N/C and Q/C bucket membership differs even when numeric boundaries match.',
                   'Calibration has no printed comparisons or bucket rows; its CSVs are header-only.',
                   'One formal run per policy; no repeat-run confidence interval or significance claim.'],
    }
    (ROOT/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    print(f'PASS source SHA256 {digest}; 4 prints -> 2 unique report sets; formal 3 x 96000 tasks / 600 requests; 36 buckets; no workload executed.')


if __name__ == '__main__':
    main()

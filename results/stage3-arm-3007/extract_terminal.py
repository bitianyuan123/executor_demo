#!/usr/bin/env python3
"""Extract and cross-check printed summaries; never infer raw-task statistics."""
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEADERS = {
    'policy_summary': 'policy,valid,tasks,requests,S_mean_ms',
    'comparisons': 'baseline,policy,S_mean_change_pct',
    'decomposition': 'policy,phase,n,requests,S_mean',
    'buckets': 'phase,metric,lower_inclusive',
    'cpu_procstat': 'policy,scope,execution_pct',
    'initial_spans': 'policy,requests,actual_span_mean_ms',
    'cpu_thresholds': 'high_pct,onset_wave_pct',
    'cpu_scopes': 'scope,covered_window_mean_pct',
    'cpu_aligned_windows': 'relative_window_ms,observed_requests',
}
PER_POLICY = {'cpu_thresholds', 'cpu_scopes', 'cpu_aligned_windows'}


def read_csv(name):
    with (ROOT/name).open(newline='') as handle:
        return list(csv.DictReader(handle))


def extract_request_completion():
    """Supplementary offline output from the same two ARM runs, not a repeat."""
    source = ROOT/'request_completion.log'
    if not source.exists():
        return
    raw = source.read_bytes()
    lines = raw.decode().splitlines()
    assert lines.count('comparison_valid=true') == 1
    assert lines.count('comparison_issues=') == 1
    assert 'functional_only=false' in lines
    assert 'request cohort: [10.0, 60.0) seconds by request_planned_ns; retain all releases and drain completions' in lines
    tables = {}
    for name, prefix in (
        ('request_completion_summary', 'policy,status,observed_requests,'),
        ('request_completion_comparisons', 'baseline,policy,metric,'),
    ):
        starts = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert len(starts) == 1, name
        start = starts[0]
        block = [lines[start].split(' (negative=')[0]]
        for line in lines[start+1:]:
            if not line:
                break
            block.append(line)
        tables[name] = list(csv.DictReader(block))
        (ROOT/(name+'.csv')).write_text('\n'.join(block)+'\n')
    rows = tables['request_completion_summary']
    metrics = ('request_planned_to_end', 'request_submit_to_end',
               'initial_planned_to_end', 'last_planned_release_offset')
    indexed = {(r['policy'], r['metric']): r for r in rows}
    assert len(rows) == 8 and set(indexed) == {
        (p, m) for p in ('home', 'source_only') for m in metrics}
    assert all(r['status'] == 'ok' and r['observed_requests'] == r['request_samples'] == '600'
               and r['tail_quality'] == 'LOW_TAIL' and float(r['nominal_top_1pct_requests']) == 6 for r in rows)
    changes = tables['request_completion_comparisons']
    assert len(changes) == 3 and {r['metric'] for r in changes} == set(metrics[:3])
    derived = []
    for row in changes:
        assert row['baseline'] == 'home' and row['policy'] == 'source_only'
        for kind in ('mean', 'p99'):
            before = float(indexed['home', row['metric']][kind+'_ms'])
            after = float(indexed['source_only', row['metric']][kind+'_ms'])
            pct = float(row[kind+'_change_pct'])
            # Both latency inputs and the printed percentage have been rounded.
            lower = 100*((after-.0005)/(before+.0005)-1)
            upper = 100*((after+.0005)/(before-.0005)-1)
            assert lower-.0005 <= pct <= upper+.0005
            derived.append({'metric': row['metric'], 'statistic': kind,
                'change_ms_from_printed_values': round(after-before, 3),
                'change_pct_reported': pct})
    review = json.loads(next(line.split('=', 1)[1] for line in lines if line.startswith('home_waveform_review=')))
    assert review == json.loads((ROOT/'review.json').read_text())
    gate = json.loads(next(line.split('=', 1)[1] for line in lines if line.startswith('home_waveform_gate=')))
    assert gate['passed'] is False and gate['status'] == 'failed'
    assert gate['reasons'] == ['fragmented_onset_fraction_exceeds_10pct']
    for policy in ('home', 'source_only'):
        row = indexed[policy, 'last_planned_release_offset']
        assert float(row['mean_ms']) == float(row['p99_ms']) == 494.75
    metadata = {'source': source.name, 'source_sha256': hashlib.sha256(raw).hexdigest(),
        'source_bytes': len(raw), 'new_workload_runs': 0,
        'request_samples_per_policy': 600, 'comparison_reported_valid': True,
        'review_matches_3007': True, 'strict_passed': False,
        'summary_rows': len(rows), 'comparison_rows': len(changes),
        'derived_changes': derived,
        'evidence_scope': 'Printed offline request summaries from the existing ARM runs. No local raw-task p99 recomputation; differences from rounded values are approximate. No new replication.'}
    (ROOT/'request_completion_evidence.json').write_text(json.dumps(metadata, indent=2)+'\n')


def main():
    raw = (ROOT/'3007.log').read_bytes()
    lines = raw.decode().splitlines()
    tables = defaultdict(list)
    observers = defaultdict(list)
    review = []
    gates = []
    scope = None
    for index, line in enumerate(lines):
        if line.startswith('POLICY '):
            scope = line.split()[1].rstrip(':')
        if line.startswith('OBSERVER '):
            observers[scope].append(json.loads(line[len('OBSERVER '):]))
        if line.startswith('HOME WAVEFORM REVIEW '):
            review.append(json.loads(line[len('HOME WAVEFORM REVIEW '):]))
        if line.startswith('HOME WAVEFORM GATE '):
            gates.append(json.loads(line[len('HOME WAVEFORM GATE '):]))
        for name, prefix in HEADERS.items():
            if not line.startswith(prefix):
                continue
            width = len(next(csv.reader([line])))
            block = [line]
            for candidate in lines[index+1:]:
                if name == 'policy_summary' and candidate.startswith('PHASE TARGET CPU '):
                    continue
                if not candidate or len(next(csv.reader([candidate]))) != width:
                    break
                block.append(candidate)
            key = f'{scope}_{name}' if name in PER_POLICY else name
            tables[key].append(block)
    expected = (set(HEADERS)-PER_POLICY) | {
        f'{policy}_{name}' for policy in ('home', 'source_only') for name in PER_POLICY}
    assert set(tables) == expected, 'Missing/unknown table or CPU-policy identity'
    counts = {}
    for name, copies in tables.items():
        assert len(copies) == 2 and all(block == copies[0] for block in copies), name
        (ROOT/(name+'.csv')).write_text('\n'.join(copies[0])+'\n')
        counts[name] = {'printed_copies': len(copies), 'data_rows': len(copies[0])-1}
    assert len(review) == 2 and review[0] == review[1]
    assert len(gates) == 2 and gates[0] == gates[1] and gates[0]['passed'] is False
    assert gates[0]['reasons'] == ['fragmented_onset_fraction_exceeds_10pct']
    assert review[0]['status'] == 'accepted_posthoc'
    assert lines.count('comparison_valid=true') == 2
    for policy, copies in observers.items():
        assert len(copies) == 2 and copies[0] == copies[1], policy
    assert set(observers) == {'home', 'source_only'}
    (ROOT/'observers.json').write_text(json.dumps({k:v[0] for k,v in observers.items()}, indent=2)+'\n')
    (ROOT/'review.json').write_text(json.dumps(review[0], indent=2)+'\n')
    summary = read_csv('policy_summary.csv')
    assert [row['policy'] for row in summary] == ['home', 'source_only']
    assert all(row['valid'] == 'True' and row['tasks'] == '96000' and row['requests'] == '600' for row in summary)
    phases = read_csv('decomposition.csv')
    assert len(phases) == 6
    # Existing 3006 baseline must be reused, not silently replaced.
    with (ROOT.parent/'stage3-arm-3006/decomposition.csv').open(newline='') as handle:
        assert [row for row in phases if row['policy'] == 'home'] == list(csv.DictReader(handle))
    assert (ROOT/'buckets.csv').read_bytes() == (ROOT.parent/'stage3-arm-3006/buckets.csv').read_bytes()
    for name in PER_POLICY:
        assert (ROOT/('home_'+name+'.csv')).read_bytes() == (ROOT.parent/'stage3-arm-3006'/(name+'.csv')).read_bytes()
    by_phase = {(row['policy'], row['phase']): row for row in phases}
    deltas = []
    for phase in ('initial', 'steady', 'all'):
        for metric in ('S_mean', 'S_p99', 'W_mean', 'W_p99', 'X_mean', 'X_p99', 'L_mean', 'L_p99', 'D_mean', 'D_p99'):
            before = float(by_phase['home', phase][metric])
            after = float(by_phase['source_only', phase][metric])
            deltas.append({'phase': phase, 'metric': metric, 'home_ms': before,
                'source_only_ms': after, 'change_ms': after-before,
                'change_pct': 100*(after/before-1), 'input_precision': 'printed_ms_3_decimals'})
    with (ROOT/'derived_changes.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(deltas[0]), lineterminator='\n')
        writer.writeheader(); writer.writerows(deltas)
    metadata = {'source': '3007.log', 'source_sha256': hashlib.sha256(raw).hexdigest(),
        'new_workload_runs_observed': {'home': 0, 'source_only': 1},
        'comparison_runs': {'home': 'reused from 3006', 'source_only': 'new in 3007'},
        'home_skip_messages': lines.count('SKIP home: complete matching run already exists'),
        'source_completed_messages': sum(line.startswith('source_only: completed;') for line in lines),
        'home_summary_matches_3006': True, 'strict_passed': False,
        'comparison_reported_valid': True,
        'review_status': review[0]['status'], 'extracted_tables': counts,
        'evidence_scope': 'Terminal summaries only; raw tasks and CPU traces remain on ARM. Changes derived from rounded values are approximate, not recomputed p99.',
        'repeat_note': 'Two identical report prints and two home precheck/skip messages are not repeated workload trials.'}
    assert metadata['home_skip_messages'] == 2 and metadata['source_completed_messages'] == 1
    (ROOT/'extraction.json').write_text(json.dumps(metadata, indent=2)+'\n')
    extract_request_completion()
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Extract identical printed tables once; never count repeated prints as runs."""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEADERS = {
    'policy_summary.csv': 'policy,valid,tasks,requests,S_mean_ms',
    'decomposition.csv': 'policy,phase,n,requests,S_mean',
    'buckets.csv': 'phase,metric,lower_inclusive',
    'cpu_procstat.csv': 'policy,scope,execution_pct',
    'initial_spans.csv': 'policy,requests,actual_span_mean_ms',
    'cpu_thresholds.csv': 'high_pct,onset_wave_pct',
    'cpu_scopes.csv': 'scope,covered_window_mean_pct',
    'cpu_aligned_windows.csv': 'relative_window_ms,observed_requests',
}


def main():
    source = ROOT / '3006.log'
    raw = source.read_bytes()
    lines = raw.decode().splitlines()
    tables = {}
    for name, prefix in HEADERS.items():
        copies = []
        for i, line in enumerate(lines):
            if not line.startswith(prefix):
                continue
            width = len(next(csv.reader([line])))
            block = [line]
            for row in lines[i+1:]:
                if not row or len(next(csv.reader([row]))) != width:
                    break
                block.append(row)
            copies.append(block)
        if not copies or any(block != copies[0] for block in copies):
            raise ValueError(f'{name}: missing or inconsistent repeated table')
        (ROOT / name).write_text('\n'.join(copies[0]) + '\n')
        tables[name] = {'printed_copies': len(copies), 'data_rows': len(copies[0])-1}
    policy = list(csv.DictReader((ROOT / 'policy_summary.csv').open()))
    if len(policy) != 1 or policy[0]['policy'] != 'home' or policy[0]['tasks'] != '96000':
        raise ValueError('unexpected run/cohort; review source before extracting')
    metadata = {
        'source': '3006.log', 'source_sha256': hashlib.sha256(raw).hexdigest(),
        'workload_runs_observed': {'home': 1, 'source_only': 0},
        'resume_evidence': 'SKIP home: complete matching run already exists' in lines,
        'candidate_block_evidence': any('source_only was not launched' in line for line in lines),
        'repeat_note': 'Repeated terminal prints and a verified SKIP are not repeated workload runs.',
        'extracted_tables': tables,
        'evidence_scope': 'Terminal report only; original per-task CSV and CPU trace remain on test host.',
    }
    (ROOT / 'extraction.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()

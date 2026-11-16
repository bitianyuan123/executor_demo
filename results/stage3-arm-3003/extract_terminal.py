#!/usr/bin/env python3
"""Extract printed ARM evidence without inventing unprinted JSON or bucket data.

Run in this directory or elsewhere: python3 /path/to/extract_terminal.py
CSV cell strings retain every digit printed by the collector (which formats most
float cells with 12 significant digits). This cannot restore unprinted precision.
"""
import csv
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECTIONS = {
    'COUNTS': ('counts.csv', 4),
    'WARNINGS (all must remain visible)': ('warnings.csv', 1),
    'CONFIGURATIONS (window seconds; cpu_us and estimate_us are microseconds)': ('configurations.csv', 7),
    'OBSERVE AND MINIMAL RAW RUN STATISTICS': ('runs.csv', 15),
    'CASE AGGREGATES (p99_run_mean is average of run p99, never pooled p99)': ('cases.csv', 29),
    'STRICT PAIRED AGGREGATES (source CI untouched; minimal no CI)': ('comparisons.csv', 22),
    'MINIMAL SIX RAW PAIRED DELTAS': ('minimal_pairs.csv', 6),
    'QUALITY GROUPS AND REDUNDANT SIGNALS': ('quality_groups.csv', 2),
    'CORRELATIONS (constant/missing = NA)': ('correlations.csv', 48),
    'PREDICTION FOLDS (oracle uses known synthetic CPU budget; pinball and coverage jointly)': ('prediction_folds.csv', 48),
}


def extract():
    raw = (ROOT / '3003.log').read_bytes()
    lines = raw.decode('utf-8').splitlines()
    found, provenance, duplicates = {}, {}, {}
    for index, line in enumerate(lines):
        if line.startswith('# SOURCE '):
            phase, encoded = line[len('# SOURCE '):].split(' ', 1)
            provenance[phase] = json.loads(encoded)
        if line[2:] not in SECTIONS or not line.startswith('# '):
            continue
        name, expected = SECTIONS[line[2:]]
        end = index + 1
        while end < len(lines) and lines[end] and not lines[end].startswith(('# ', '[root@')):
            end += 1
        text = '\n'.join(lines[index + 1:end]) + '\n'
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) != expected:
            raise ValueError(f'{name}: expected {expected} rows, got {len(rows)}')
        if any(None in row or None in row.values() for row in rows):
            raise ValueError(f'{name}: malformed/truncated CSV')
        if name in found:
            if found[name]['text'] != text:
                raise ValueError(f'{name}: repeated section differs')
            duplicates[name] = duplicates.get(name, 0) + 1
        else:
            found[name] = {'text': text, 'rows': rows, 'source_header_line': index + 1}
    if len(found) != len(SECTIONS):
        raise ValueError('Missing required summary/quality sections')
    if set(provenance) != {'observe', 'tune', 'evaluate', 'minimal'}:
        raise ValueError('Missing source provenance')
    if any(line.startswith('# HELD-OUT BUCKETS') for line in lines):
        raise ValueError('New bucket evidence present: extend extractor rather than silently ignoring it')
    for name, evidence in found.items():
        (ROOT / name).write_text(evidence['text'], encoding='utf-8')
    # Additional user message, archived independently from the original attachment.
    bucket_raw = (ROOT / 'buckets-terminal.txt').read_bytes()
    bucket_lines = bucket_raw.decode('utf-8').splitlines()
    start = next(i for i, line in enumerate(bucket_lines) if line.startswith('group,config_id,held_seed,metric,bucket,'))
    bucket_text = '\n'.join(line for line in bucket_lines[start:] if not line.startswith('[root@')) + '\n'
    buckets = list(csv.DictReader(io.StringIO(bucket_text)))
    if len(buckets) != 192 or any(None in row or None in row.values() for row in buckets):
        raise ValueError('Missing or malformed bucket rows')
    groups = {}
    for row in buckets:
        key = (row['group'], row['config_id'], row['held_seed'], row['metric'])
        groups.setdefault(key, []).append(row)
        if row['boundary_source'] != 'training_seeds_only':
            raise ValueError('Bucket boundary provenance differs')
    if len(groups) != 24:
        raise ValueError('Expected 2 configurations x 3 seeds x 4 metrics')
    for key, rows in groups.items():
        rows.sort(key=lambda r: int(r['bucket']))
        if [int(r['bucket']) for r in rows] != list(range(8)) or sum(int(r['n']) for r in rows) != 12000:
            raise ValueError(f'Bucket coverage mismatch: {key}')
        if rows[0]['lower_inclusive'] != 'NA' or rows[-1]['upper_exclusive'] != 'NA':
            raise ValueError(f'Missing open intervals: {key}')
        if any(a['upper_exclusive'] != b['lower_inclusive'] for a, b in zip(rows, rows[1:])):
            raise ValueError(f'Bucket intervals not contiguous: {key}')
    (ROOT / 'buckets.csv').write_text(bucket_text, encoding='utf-8')
    sha = hashlib.sha256(raw).hexdigest()
    bucket_sha = hashlib.sha256(bucket_raw).hexdigest()
    (ROOT / 'source.sha256').write_text(sha + '  3003.log\n' + bucket_sha + '  buckets-terminal.txt\n')
    result = {
        'schema': 1,
        'source': '3003.log',
        'source_sha256': sha,
        'source_bytes': len(raw),
        'source_line_count': len(lines),
        'additional_source': {'path': 'buckets-terminal.txt', 'sha256': bucket_sha,
            'bytes': len(bucket_raw), 'origin': 'Subsequent user terminal text, archived separately; not inside original 3003.log',
            'rows': len(buckets), 'groups': len(groups), 'tasks_per_group': 12000,
            'low_tail_rows': [r for r in buckets if r['tail_quality'] == 'LOW_TAIL']},
        'provenance': provenance,
        'section_counts': {name: len(value['rows']) for name, value in found.items()},
        'source_header_lines': {name: value['source_header_line'] for name, value in found.items()},
        'exact_duplicate_sections_removed': duplicates,
        'available': ['summary.txt (complete)', 'quality.txt (complete; identical duplicate removed)',
            'buckets.txt (192 rows, complete in subsequent user terminal text)'],
        'missing': ['collection.json (not printed)', 'ARM task CSV (not provided)'],
        'precision': 'CSV cell strings preserve printed digits. Collector usually formats floats as .12g; no claim of source-report precision.',
        'validation': 'Extracted table counts and duplicate consistency checked. Source run validity inherited from ARM analyzer, not revalidated from raw tasks.',
    }
    (ROOT / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('source_sha256', 'section_counts', 'exact_duplicate_sections_removed', 'missing')}, indent=2))


if __name__ == '__main__':
    extract()

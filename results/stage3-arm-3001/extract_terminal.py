#!/usr/bin/env python3
"""Reproduce descriptive summaries from the user's terminal log; no task data or CI.

The same rows appear in both stdout and Markdown. Deduplicate on case/split/seed
and retain all source line numbers. Quality stdout has four decimal places while
Markdown has three; keep stdout's greater precision and check rounded agreement.
"""
import argparse
import csv
import hashlib
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

RUN_FIELDS = ('planned_mean_ms', 'planned_p99_ms', 'wait_mean_ms',
              'callback_mean_ms', 'spill_fraction')
QUALITY_FIELDS = ('mean_mae_ms', 'p99_pinball_ms', 'p99_coverage',
                  'mae_reduction_vs_budget_ms')
NUMBER = r'-?\d+(?:\.\d+)?'
RUN_RE = re.compile(r'^(\S+) (observe|tune|evaluate) (\d+) (True|False) '
                    r'(\d+)/(\d+) (' + NUMBER + r') (' + NUMBER + r') (' +
                    NUMBER + r') (' + NUMBER + r') (' + NUMBER + r')$')
QUALITY_RE = re.compile(r'^([a-f0-9]{12}) (\S+) (\S+) (\d+) (' + NUMBER +
                        r') (' + NUMBER + r') (' + NUMBER + r') (' + NUMBER + r')$')


def save_csv(path, rows):
    if not rows:
        raise ValueError(f'No rows for {path}')
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def retain(records, key, row, fields, line, representation):
    if key not in records:
        row['source_lines'] = [line]
        row['representations'] = [representation]
        records[key] = row
        return
    old = records[key]
    for field in fields:
        # All source fields are printed to 3 or 4 decimal places.
        if abs(old[field] - row[field]) > 0.001001:
            raise ValueError(f'Conflicting duplicate {key}: {field} at line {line}')
    old['source_lines'].append(line)
    if representation not in old['representations']:
        old['representations'].append(representation)
    if representation == 'stdout':
        for field in fields:
            old[field] = row[field]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path(__file__).with_name('3001.log'))
    parser.add_argument('--output', type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    runs, quality = {}, {}
    for line_number, line in enumerate(raw.decode().splitlines(), 1):
        representation = 'stdout'
        run_match = RUN_RE.fullmatch(line)
        quality_match = QUALITY_RE.fullmatch(line)
        if line.startswith('|'):
            columns = line.strip('|').split('|')
            # Markdown copies of the per-run terminal table.
            if len(columns) == 8 and ' / ' in columns[0]:
                identity = columns[0].split(' / ')
                if len(identity) == 3 and identity[1].isdigit() and columns[1] in ('True', 'False'):
                    case, seed, split = identity
                    run_match = RUN_RE.fullmatch(' '.join((case, split, seed, *columns[1:])))
                    representation = 'markdown'
            # Predictor rows additionally contain the model name.
            if len(columns) == 8 and ' / ' in columns[0] and columns[3].isdigit():
                config, metric = columns[0].split(' / ', 1)
                if re.fullmatch(r'[a-f0-9]{12}', config):
                    model = columns[2]
                    row = dict(config_id=config, metric=metric, target=columns[1], model=model,
                               folds=int(columns[3]), **dict(zip(QUALITY_FIELDS, map(float, columns[4:]))))
                    retain(quality, (config, metric, columns[1], model), row,
                           QUALITY_FIELDS, line_number, 'markdown')
        if run_match:
            case, split, seed, valid, completed, planned, *values = run_match.groups()
            row = dict(case=case, split=split, seed=int(seed), valid=valid == 'True',
                       completed=int(completed), planned=int(planned),
                       environment='ARM-target' if re.search(r'-t(?:16|256)-', case) else 'dev-preset',
                       **dict(zip(RUN_FIELDS, map(float, values))))
            retain(runs, (case, split, int(seed)), row, (*RUN_FIELDS, 'completed', 'planned'),
                   line_number, representation)
        if quality_match:
            config, metric, target, folds, *values = quality_match.groups()
            row = dict(config_id=config, metric=metric, target=target, model=metric, folds=int(folds),
                       **dict(zip(QUALITY_FIELDS, map(float, values))))
            retain(quality, (config, metric, target, metric), row, QUALITY_FIELDS, line_number, 'stdout')

    grouped = defaultdict(list)
    for row in runs.values():
        grouped[(row['environment'], row['split'], row['case'])].append(row)
    summaries = []
    for (environment, split, case), items in sorted(grouped.items()):
        summaries.append(dict(environment=environment, split=split, case=case, runs=len(items),
                              all_valid=all(r['valid'] for r in items),
                              seeds=','.join(str(r['seed']) for r in sorted(items, key=lambda r: r['seed'])),
                              **{field: statistics.fmean(r[field] for r in items) for field in RUN_FIELDS}))
    counts = {split: sum(r['environment'] == 'ARM-target' and r['split'] == split for r in runs.values())
              for split in ('observe', 'tune', 'evaluate')}
    # Deliberately no bootstrap or inferred pooled percentiles: the original
    # config mismatch must be handled against metadata and task CSV in the lab.
    result = {
        'schema': 1,
        'source': args.input.name,
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'scope': 'terminal summaries only; no ARM metadata.json or task CSV supplied',
        'arm_analyzed_runs': counts,
        'arm_analyzed_total': sum(counts.values()),
        'dev_analyzed_runs': sum(r['environment'] == 'dev-preset' for r in runs.values()),
        'limitations': [
            'Case means average printed per-run summaries; p99 means average per-run p99, not a pooled p99.',
            'No formal CI or causal pairing is reconstructed from rounded terminal rows.',
            '3001.log ends after one completed run of the 70-run minimal plan; it does not establish later status.',
            'User later confirmed hostname is the only differing config field; explicit alias reanalysis needs ARM raw files.',
            'The later hostname confirmation is separate evidence, not part of the original 3001.log.',
        ],
        'quality_config_mapping_from_later_user_confirmation': {
            '79fbbec9139e': 'main-t16-local', 'c383f86cfb2e': 'main-t256-local',
        },
        'case_summaries': summaries,
        'quality_rows': list(quality.values()),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in (('runs.csv', runs.values()), ('quality.csv', quality.values())):
        flat = [{**r, 'source_lines': ','.join(map(str, r['source_lines'])),
                 'representations': ','.join(r['representations'])} for r in rows]
        save_csv(args.output/name, flat)
    save_csv(args.output/'cases.csv', summaries)
    (args.output/'summary.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    (args.output/'source.sha256').write_text(result['source_sha256'] + '  3001.log\n')
    print(f'ARM analyzed runs: {counts}; total={sum(counts.values())}; dev={result["dev_analyzed_runs"]}')
    print(f'Unique run rows={len(runs)}; quality rows={len(quality)}; duplicate printed rows deduplicated.')


if __name__ == '__main__':
    main()

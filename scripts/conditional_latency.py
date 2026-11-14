#!/usr/bin/env python3
"""Read existing stage-3 results: conditional latency quantiles, never a workload.

Keep configurations and seeds separate. Ranges summarize per-run statistics;
they are neither pooled percentiles nor confidence intervals.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

from compare_runs import digest, read_csv, summarize
from terminal_report import locate_run


BUCKETS = ('all', '0', '1-3', '4-15', '16-63', '64+', 'unobserved')
METRICS = ('wait_ms', 'submitted_ms', 'planned_ms', 'callback_ms')
NOTES = [
    'Native queue is the submitting thread\'s pre-add Folly snapshot, not kernel runq or a reservation.',
    'Cohort selection uses planned arrival in [start,end); keep completions after end.',
    'Each run uses nearest-rank p50/p95/p99. Ranges are min..max of run statistics, not pooled quantiles or confidence intervals.',
    'Tasks within a burst/time interval are dependent; burst counts are descriptive, not independent sample counts.',
    'LOW_TAIL means n*0.01<20 in at least one run. This heuristic is not a precision guarantee, even when absent.',
    'Association within these workload/configuration strata is not a causal curve or a portable queue threshold.',
    'Do not average per-second p99. Failed/incomplete runs stay visible and are excluded from conditional summaries.',
]


def bucket(q):
    if q < 0:
        return 'unobserved'
    return '0' if q == 0 else '1-3' if q < 4 else '4-15' if q < 16 else '16-63' if q < 64 else '64+'


def distribution(values):
    values = sorted(values)
    return {'mean': statistics.fmean(values), **{
        f'p{p}': values[max(0, math.ceil(len(values) * p / 100) - 1)]
        for p in (50, 95, 99)}}


def bucket_stats(rows):
    return {
        'n': len(rows), 'bursts': len({r['burst'] for r in rows}),
        'pools': len({r['pool'] for r in rows}), 'nominal_top_1pct_count': len(rows) * .01,
        'low_tail': len(rows) < 2000,
        'wait_over_1ms_count': sum(r['wait_ms'] > 1 for r in rows),
        'wait_over_5ms_count': sum(r['wait_ms'] > 5 for r in rows),
        'wait_over_1ms_fraction': sum(r['wait_ms'] > 1 for r in rows) / len(rows),
        'wait_over_5ms_fraction': sum(r['wait_ms'] > 5 for r in rows) / len(rows),
        'target_cpu_mean_ms': statistics.fmean(r['target_cpu_us'] / 1000 for r in rows),
        'latency': {m: distribution([r[m] for r in rows]) for m in METRICS},
    }


def analyze_run(entry, start, end):
    audit = summarize(entry, 'independent', start, end)
    if audit['diagnostics']['whole_run_incomplete'] or not audit['completed']:
        raise ValueError('incomplete/rejected work or empty cohort; successful-only inference excluded')
    meta = json.loads((entry['path'] / 'metadata.json').read_text())
    if meta.get('same_cpu_union') is not True:
        raise ValueError('same CPU resource union is not confirmed')
    rows = []
    for r in read_csv(entry['path'] / 'tasks.csv'):
        if not start * 1e9 <= int(r['planned_ns']) < end * 1e9:
            continue
        row = {k: int(r[k]) for k in ('burst', 'pool', 'target_cpu_us')}
        # Missing instrumentation must not be treated as queue=0.
        row['native_queue'] = int(r.get('native_queue', '-1'))
        row.update({
            'wait_ms': (int(r['start_ns']) - int(r['submit_ns'])) / 1e6,
            'submitted_ms': (int(r['end_ns']) - int(r['submit_ns'])) / 1e6,
            'planned_ms': (int(r['end_ns']) - int(r['planned_ns'])) / 1e6,
            'callback_ms': (int(r['end_ns']) - int(r['start_ns'])) / 1e6,
        })
        rows.append(row)
    config = {
        'options': audit['options'], 'binary_sha256': audit['binary_sha256'],
        'allowed_cpus': audit['allowed_cpus'],
        'effective_masks': meta.get('effective_masks'),
        'affinity': meta.get('affinity'), 'threads_per_pool': meta.get('threads_per_pool'),
        'instrumentation': meta.get('instrumentation'), 'work_mode': meta.get('work_mode'),
    }
    groups = {'all': rows}
    for r in rows:
        groups.setdefault(bucket(r['native_queue']), []).append(r)
    return {
        'case': entry['case'], 'seed': int(entry['seed']), 'repeat': entry['repeat'],
        'path': str(entry['path']), 'config': config, 'config_id': digest(config)[:12],
        'cohort_n': len(rows), 'warnings': audit['warnings'],
        'buckets': {b: bucket_stats(groups[b]) for b in BUCKETS if groups.get(b)},
    }


def bounds(values):
    return [min(values), max(values)]


def aggregate(runs):
    groups = {}
    for r in runs:
        groups.setdefault((r['case'], r['config_id']), []).append(r)
    result = []
    for (case, cid), rs in sorted(groups.items()):
        bins = {}
        for b in BUCKETS:
            bs = [r['buckets'][b] for r in rs if b in r['buckets']]
            if not bs:
                continue
            bins[b] = {
                'runs_with_bucket': len(bs), 'runs_total': len(rs),
                'n': bounds([v['n'] for v in bs]), 'bursts': bounds([v['bursts'] for v in bs]),
                'low_tail': any(v['low_tail'] for v in bs),
                'wait_over_1ms_count': bounds([v['wait_over_1ms_count'] for v in bs]),
                'wait_over_5ms_count': bounds([v['wait_over_5ms_count'] for v in bs]),
                'wait_over_1ms_fraction': bounds([v['wait_over_1ms_fraction'] for v in bs]),
                'wait_over_5ms_fraction': bounds([v['wait_over_5ms_fraction'] for v in bs]),
                'target_cpu_mean_ms': bounds([v['target_cpu_mean_ms'] for v in bs]),
                'latency': {m: {s: bounds([v['latency'][m][s] for v in bs])
                                for s in ('mean', 'p50', 'p95', 'p99')} for m in METRICS},
            }
        result.append({'case': case, 'config_id': cid, 'config': rs[0]['config'],
                       'seeds': [r['seed'] for r in rs], 'buckets': bins})
    return result


def analyze(manifest):
    data = json.loads(manifest.read_text())
    window = data['analysis']
    start, end = float(window['warmup_seconds']), float(window['steady_end_seconds'])
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError('invalid analysis window')
    rows = data['runs']
    planned = None
    try:
        plan = json.loads((manifest.parent.parent / 'plan/plan.json').read_text())
        planned = plan['phases'][manifest.parent.name]['runs']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    canonical_keys = []
    for row in rows:
        try:
            canonical_keys.append((row.get('case'), int(str(row.get('seed')))))
        except (ValueError, TypeError):
            pass  # The per-row audit below records malformed seed values.
    keys = Counter(canonical_keys)
    runs, errors = [], []
    for row in rows:
        try:
            seed = int(str(row.get('seed')))
            if keys[(row.get('case'), seed)] != 1:
                raise ValueError('duplicate case/seed is not an independent repetition')
            if not row.get('case') or row.get('seed') is None or not row.get('command'):
                raise ValueError('case/seed/command missing; configuration cannot be audited')
            entry = {**row, 'seed': seed, 'path': locate_run(manifest, row),
                     'repeat': row.get('repetition', row.get('repeat')),
                     'binary_sha256': row.get('binary_sha256', data.get('binary_sha256'))}
            runs.append(analyze_run(entry, start, end))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append({'case': row.get('case'), 'seed': row.get('seed'), 'error': str(error)})
    return {
        'manifest': str(manifest), 'window_s': [start, end], 'recorded_runs': len(rows),
        'planned_runs': planned, 'plan_count_matches': planned == len(rows) if planned is not None else None,
        'notes': NOTES, 'errors': errors, 'runs': runs, 'groups': aggregate(runs),
    }


def display_range(values):
    a, b = values
    return f'{a:.3f}' if a == b else f'{a:.3f}..{b:.3f}'


def display_probability(values):
    a, b = values
    # Significant digits preserve small nonzero rates in large task cohorts.
    return f'{a:.6g}' if a == b else f'{a:.6g}..{b:.6g}'


def report(result):
    lines = ['# Stage-3 conditional latency (ms)',
             f"Manifest: {result['manifest']}; window={result['window_s']}; "
             f"recorded/planned={result['recorded_runs']}/{result['planned_runs']}; "
             f"usable={len(result['runs'])}; excluded={len(result['errors'])}.", '']
    lines += [f'- {note}' for note in NOTES]
    if result['plan_count_matches'] is not True:
        lines += ['', 'PLAN INCOMPLETE/UNKNOWN: manifest records finished attempts only; check the original plan.']
    for error in result['errors']:
        lines.append('EXCLUDED: ' + json.dumps(error, ensure_ascii=False))
    for r in result['runs']:
        if r['warnings']:
            lines.append(f"FLAGS {r['case']} seed={r['seed']}: {json.dumps(r['warnings'])}")
    for g in result['groups']:
        c = g['config']
        lines += ['', f"## {g['case']} config={g['config_id']} seeds={g['seeds']}",
                  f"affinity={c['affinity']} threads/pool={c['threads_per_pool']} "
                  f"instrumentation={c['instrumentation']} work_mode={c['work_mode']} "
                  f"binary={c['binary_sha256']}", '',
                  '|pre-add queue|runs|n/run|bursts/run|latency|p50 range|p95 range|p99 range|note|',
                  '|---|---:|---:|---:|---|---:|---:|---:|---|']
        for b, v in g['buckets'].items():
            for m in METRICS:
                parts = [b, f"{v['runs_with_bucket']}/{v['runs_total']}",
                         '..'.join(map(str, v['n'])), '..'.join(map(str, v['bursts'])), m]
                parts += [display_range(v['latency'][m][p]) for p in ('p50', 'p95', 'p99')]
                lines.append('|' + '|'.join(parts + ['LOW_TAIL' if v['low_tail'] else '']) + '|')
        if '0' in g['buckets']:
            z = g['buckets']['0']
            lines += [f"queue=0: P(wait>1ms)={display_probability(z['wait_over_1ms_fraction'])}; "
                      f"P(wait>5ms)={display_probability(z['wait_over_5ms_fraction'])}; "
                      f"event counts/run={z['wait_over_1ms_count']}/{z['wait_over_5ms_count']} (per-run fractions and count ranges)."]
    lines += ['', 'JSON retains per-seed quantiles, counts, config, target means and flags. '
              'No pooled p99, fitted causal model, precision guarantee or production threshold is inferred.']
    return '\n'.join(lines) + '\n'


def compact_report(result):
    lines = [f"CONDITIONAL_LATENCY window={result['window_s']} recorded/planned="
             f"{result['recorded_runs']}/{result['planned_runs']} usable={len(result['runs'])} "
             f"excluded={len(result['errors'])}"]
    lines += NOTES
    if result['plan_count_matches'] is not True:
        lines.append('PLAN INCOMPLETE/UNKNOWN: check original plan for unrecorded attempts.')
    for e in result['errors']:
        lines.append('EXCLUDED ' + json.dumps(e, ensure_ascii=False))
    for r in result['runs']:
        if r['warnings']:
            lines.append(f"FLAGS {r['case']} seed={r['seed']}: {json.dumps(r['warnings'])}")
    for g in result['groups']:
        cfg = g['config']
        lines += [f"CASE {g['case']} config={g['config_id']} seeds={g['seeds']} "
                  f"affinity={cfg['affinity']} threads/pool={cfg['threads_per_pool']}",
                  '  All latency triplets below = p50 / p95 / p99, in ms; each value is a per-run range.']
        for b, v in g['buckets'].items():
            text = (f"  q={b} runs={v['runs_with_bucket']}/{v['runs_total']} "
                    f"n={v['n']} bursts={v['bursts']}")
            for key, label in [('wait_ms', 'wait'), ('submitted_ms', 'submitted'), ('planned_ms', 'planned')]:
                text += ' ' + label + '=' + '/'.join(display_range(v['latency'][key][p]) for p in ('p50', 'p95', 'p99'))
            if v['low_tail']:
                text += ' LOW_TAIL'
            lines.append(text)
        if '0' in g['buckets']:
            z = g['buckets']['0']
            lines.append(f"  queue=0 P(wait>1ms)={display_probability(z['wait_over_1ms_fraction'])} "
                         f"P(wait>5ms)={display_probability(z['wait_over_5ms_fraction'])} "
                         f"event_counts={z['wait_over_1ms_count']}/{z['wait_over_5ms_count']}")
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='output prefix for .json and .md; report also printed')
    p.add_argument('--compact', action='store_true', help='short terminal output; still save full JSON/Markdown')
    args = p.parse_args()
    try:
        result = analyze(args.manifest)
    except (OSError, ValueError, KeyError, TypeError) as error:
        p.exit(1, f'Cannot audit manifest: {error}\n')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    Path(str(args.output) + '.json').write_text(json.dumps(result, indent=2) + '\n')
    text = report(result)
    Path(str(args.output) + '.md').write_text(text)
    print(compact_report(result) if args.compact else text, end='')
    return 1 if result['errors'] or not result['runs'] or result['plan_count_matches'] is False else 0


if __name__ == '__main__':
    raise SystemExit(main())

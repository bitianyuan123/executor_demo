#!/usr/bin/env python3
"""Reproduce figures from extracted terminal evidence; requires matplotlib.

No raw-task reconstruction, p99 averaging across buckets, or fitted unseen points.
Run: python3 results/stage3-arm-3003/plot_report.py
"""
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'figures'
OUT.mkdir(exist_ok=True)
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'svg.fonttype': 'none'})
COLORS = ['#2166ac', '#d6604d']


def read(name):
    with (ROOT / name).open() as source:
        return list(csv.DictReader(source))


def save(fig, name):
    fig.savefig(OUT / (name + '.png'), dpi=170, bbox_inches='tight')
    fig.savefig(OUT / (name + '.svg'), bbox_inches='tight')
    svg = OUT / (name + '.svg')
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    plt.close(fig)


cases = read('cases.csv')
runs = read('runs.csv')
folds = read('prediction_folds.csv')
correlations = read('correlations.csv')

# Each row summarizes three separate seed-level diagnostics, not pooled tasks.
groups = defaultdict(list)
for row in folds:
    groups[(row['group'], row['metric'], row['target'])].append(row)
summary = []
numeric = [k for k in folds[0] if k.endswith(('_mae_ms', '_pinball_ms', '_coverage'))]
for key, rows in sorted(groups.items()):
    item = dict(zip(('group', 'metric', 'target'), key))
    item['folds'] = len(rows)
    for field in numeric:
        item[field + '_fold_mean'] = mean(float(row[field]) for row in rows)
        item[field + '_fold_min'] = min(float(row[field]) for row in rows)
        item[field + '_fold_max'] = max(float(row[field]) for row in rows)
    related = [r for r in correlations if (r['group'], r['metric'], r['target']) == key]
    for field in ('pearson', 'spearman'):
        item[field + '_run_mean'] = mean(float(row[field]) for row in related)
    summary.append(item)
with (ROOT / 'quality_derived_summary.csv').open('w') as target:
    writer = csv.DictWriter(target, fieldnames=list(summary[0]))
    writer.writeheader()
    writer.writerows(summary)

fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))
observed = [next(r for r in cases if r['phase'] == 'observe' and r['case'] == f'main-t{t}-local-home') for t in (16, 256)]
for i, row in enumerate(observed):
    w = float(row['wait_ms_mean_run_mean'])
    x = float(row['callback_ms_mean_run_mean'])
    lag = float(row['planned_latency_ms_mean_run_mean']) - w - x
    bottom = 0
    for value, color, label in ((lag, '#888888', 'planned -> submit'), (w, '#2166ac', 'submit -> callback start (W)'), (x, '#f4a582', 'callback start -> end (X)')):
        axes[0].bar(i, value, bottom=bottom, width=.5, color=color, label=label if i == 0 else None)
        if value > 5:
            axes[0].text(i, bottom + value / 2, f'{value:.1f}', ha='center', va='center')
        bottom += value
    own = [r for r in runs if r['phase'] == 'observe' and r['case'] == row['case']]
    axes[1].scatter([i-.08, i, i+.08], [float(r['planned_latency_ms_p99']) for r in own], color=COLORS[i], s=38)
    axes[1].hlines(float(row['planned_latency_ms_p99_run_mean']), i-.25, i+.25, color=COLORS[i], linewidth=2)
for ax in axes:
    ax.set_xticks([0, 1], ['T=16', 'T=256'])
    ax.set_ylim(bottom=0)
    ax.set_ylabel('Milliseconds')
    ax.grid(axis='y', alpha=.2)
axes[0].set_title('Home: average L decomposed into mean intervals')
axes[0].legend(fontsize=8)
axes[1].set_title('Home: each observed run p99 (3 seeds)')
fig.suptitle('B=100, six cluster-bound pools, 16 logical CPUs per pool')
fig.tight_layout()
save(fig, 'observed_latency_decomposition')

fig, axes = plt.subplots(2, 2, figsize=(12, 8))
metrics = [('queue_per_capacity', 'Q/C'), ('outstanding_per_capacity', 'N/C'), ('work_ms_per_capacity', 'Work/C'), ('remaining_ms_per_capacity', 'Remaining/C')]
plots = [('wait_ms', 'metric_mean_mae_ms', 'W prediction: conditional-mean MAE (ms)'),
         ('planned_latency_ms', 'metric_mean_mae_ms', 'L prediction: conditional-mean MAE (ms)'),
         ('planned_latency_ms', 'metric_p99_pinball_ms', 'L prediction: p99 pinball loss (ms; lower is better)'),
         ('planned_latency_ms', 'metric_p99_coverage', 'L prediction: p99 coverage (target 0.99)')]
for ax, (target, field, title) in zip(axes.flat, plots):
    for ti, threads in enumerate((16, 256)):
        for mi, (metric, label) in enumerate(metrics):
            own = groups[(f'main-t{threads}-local', metric, target)]
            values = [float(r[field]) for r in own]
            pos = mi + (-.18 if ti == 0 else .18)
            ax.bar(pos, mean(values), width=.32, color=COLORS[ti], alpha=.75, label=f'T={threads}' if mi == 0 else None)
            ax.scatter([pos-.06, pos, pos+.06], values, color=COLORS[ti], edgecolor='white', linewidth=.3, s=23, zorder=3)
    ax.set_xticks(range(4), [label for _, label in metrics])
    ax.set_title(title)
    ax.grid(axis='y', alpha=.2)
    if field.endswith('coverage'):
        ax.set_ylim(.97, 1.0)
        ax.axhline(.99, linestyle='--', color='black', linewidth=1)
        ax.text(.01, .02, 'Axis cropped to 0.97–1.00', transform=ax.transAxes, fontsize=8)
    else:
        ax.set_ylim(bottom=0)
    ax.legend(fontsize=9)
fig.suptitle('Metric quality on home observations only: mean of 3 held-seed folds + all fold points', fontsize=12)
fig.tight_layout()
save(fig, 'metric_quality')

fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
policies = [('home', 'Home'), ('queue-h2', 'Q/C > 2'), ('outstanding-h2', 'N/C > 2'), ('spread-policy', 'Spread')]
for ti, threads in enumerate((16, 256)):
    for pi, (policy, label) in enumerate(policies):
        row = next(r for r in cases if r['phase'] == 'evaluate' and r['case'] == f'main-t{threads}-local-{policy}')
        avg = float(row['planned_latency_ms_p99_run_mean'])
        lower = float(row['planned_latency_ms_p99_run_min'])
        upper = float(row['planned_latency_ms_p99_run_max'])
        pos = pi + (-.18 if ti == 0 else .18)
        axes[0].bar(pos, avg, width=.32, color=COLORS[ti], label=f'T={threads}' if pi == 0 else None)
        axes[0].errorbar(pos, avg, yerr=[[avg-lower], [upper-avg]], color='black', capsize=3, linewidth=1)
        axes[0].text(pos, avg+12, f'{avg:.1f}', ha='center', fontsize=8)
axes[0].set_xticks(range(4), [label for _, label in policies])
axes[0].set_title('Formal evaluation: 7 paired seeds\nBars = mean of run p99; whiskers = run min–max')
axes[0].legend()
for seed in (32000, 32001, 32002):
    rows = [next(r for r in runs if r['phase'] == 'minimal' and r['seed'] == str(seed) and r['case'] == f'main-t256-local-{policy}') for policy in ('home', 'outstanding-h2', 'spread-policy')]
    axes[1].plot(range(3), [float(r['planned_latency_ms_p99']) for r in rows], 'o-', label=str(seed), alpha=.8)
axes[1].set_xticks(range(3), ['Home', 'N/C > 2', 'Spread'])
axes[1].set_title('Minimal observation: T=256, 3 paired seeds\nEach line is a seed; descriptive, no confidence interval')
axes[1].legend(title='Seed')
for ax in axes:
    ax.set_ylabel('Task planned-to-end p99 (ms)')
    ax.set_ylim(0, 760)
    ax.grid(axis='y', alpha=.2)
fig.suptitle('Policy effects at B=100; different observation modes are shown separately')
fig.tight_layout()
save(fig, 'policy_effects')

# Plot each held seed with its original training intervals, including open ends.
# The x-axis is categorical (equal spacing), never implying a numeric tail bound.
buckets = read('buckets.csv')
for threads in (16, 256):
    fig, axes = plt.subplots(2, 3, figsize=(16, 8.5), sharey=True)
    for row_index, (metric, label) in enumerate(metrics[:2]):
        for column, seed in enumerate((11000, 11001, 11002)):
            ax = axes[row_index, column]
            rows = sorted([r for r in buckets if r['group'] == f'main-t{threads}-local' and r['metric'] == metric and r['held_seed'] == str(seed)], key=lambda r: int(r['bucket']))
            for field, name, color, marker in (('p99_ms', 'L conditional p99', '#b2182b', 'o'), ('mean_ms', 'L conditional mean', '#2166ac', 'o'), ('W_mean_ms', 'W mean', '#4d9221', '^'), ('X_mean_ms', 'X mean', '#984ea3', 's')):
                ax.plot(range(8), [float(r[field]) for r in rows], marker=marker, markersize=3.5, linewidth=1.4, color=color, label=name)
            for i, row in enumerate(rows):
                if row['tail_quality'] == 'LOW_TAIL':
                    ax.scatter(i, float(row['p99_ms']), marker='D', s=75, facecolors='none', edgecolors='black', zorder=5)
                    ax.annotate('LOW_TAIL\nn=964', (i, float(row['p99_ms'])), xytext=(-55, -35), textcoords='offset points', fontsize=8, arrowprops={'arrowstyle': '-', 'color': 'black'})
            labels = []
            for r in rows:
                lo, hi = r['lower_inclusive'], r['upper_exclusive']
                interval = '<' + hi if lo == 'NA' else '≥' + lo if hi == 'NA' else '[' + lo + ', ' + hi + ')'
                labels.append(interval + '\nn=' + r['n'])
            ax.set_xticks(range(8), labels, rotation=45, ha='right', fontsize=7)
            ax.set_title(f'{label}, held seed {seed}; 120 bursts per bucket', fontsize=10)
            ax.set_ylim(0, 800)
            ax.grid(axis='y', alpha=.2)
            if column == 0:
                ax.set_ylabel('Milliseconds')
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .955), ncol=4)
    fig.suptitle(f'T={threads}: load-conditioned latency, each seed shown separately\nX-axis uses equal-width categories with exact training intervals; quantiles are not additive', fontsize=12, y=1.01)
    fig.tight_layout(rect=(0, 0, 1, .92))
    save(fig, f'conditional_latency_t{threads}')
print('Wrote quality_derived_summary.csv and 5 PNG/SVG figure pairs')

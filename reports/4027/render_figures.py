#!/usr/bin/env python3
"""Plot paired tradeoffs, without pooling quantiles or inventing CPU timelines."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
evidence = json.loads((HERE / 'evidence.json').read_text())
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                     'axes.spines.top': False, 'axes.spines.right': False,
                     'svg.hashsalt': 'executor-demo-4027'})
fig, axes = plt.subplots(1, 3, figsize=(13, 6.3))
fig.subplots_adjust(left=.07, right=.98, top=.74, bottom=.27, wspace=.28)
fig.suptitle('Destination admission: lower CPU cost, higher mean request latency',
             x=.05, y=.97, ha='left', weight='bold', fontsize=15)
fig.text(.05, .90, '12 new runs; two DAGs, off / 1.0 / 0.75; regular 12 QPS; six cluster pools, 16 workers each.\n'
         'Each point is one candidate versus its same-seed, same-shape off baseline.', va='top')
cells = [('front80', 1), ('front80', .75), ('tail4', 1), ('tail4', .75)]
for ax, field, title in zip(axes, ['mean_ms_percent', 'p99_ms_percent', 'cpu_per_request_ms_percent'],
                           ['Request mean change (%)', 'Request p99 change (%)', 'Process CPU / request change (%)']):
    for seed, offset, marker, color in [(421, -.09, 'o', '#2463a6'), (422, .09, 'D', '#d86e21')]:
        values = [next(r[field] for r in evidence['paired_deltas']
                       if r['shape'] == shape and r['target_limit'] == limit and r['seed'] == seed)
                  for shape, limit in cells]
        ax.scatter(np.arange(4) + offset, values, s=48, marker=marker, color=color, label=f'seed {seed}', zorder=3)
    ax.axhline(0, color='black', lw=.8)
    ax.set_xticks(range(4), ['front80\n1.0', 'front80\n0.75', 'tail4\n1.0', 'tail4\n0.75'])
    ax.set_ylabel(title)
    ax.grid(axis='y', alpha=.2)
axes[0].legend(loc='upper left', bbox_to_anchor=(0, -.20), ncol=2, frameon=False)
fig.text(.05, .06, 'Latency: 192 planned-arrival cohort requests per run. CPU: whole-run process CPU / 240 completions.\n'
         'Two seeds are screening evidence, not a confidence interval. CPU cost is not instantaneous utilization.\n'
         'Target limits are approximate (outstanding + reservations + incoming) / configured capacity, not CPU percentages.', fontsize=9)
for suffix in ('png', 'svg'):
    fig.savefig(HERE / f'request_tradeoff.{suffix}', dpi=180, metadata={'Date': None} if suffix == 'svg' else None)
plt.close(fig)

fig, axes = plt.subplots(1, 3, figsize=(13, 6.3))
fig.subplots_adjust(left=.07, right=.98, top=.74, bottom=.27, wspace=.30)
fig.suptitle('tail4: later successors wait less; the first wave waits longer',
             x=.05, y=.97, ha='left', weight='bold', fontsize=15)
fig.text(.05, .90, '80 independent operators, barrier, then 4 serial chains of 22 operators each; 3M iterations per operator.\n'
         'All settings still have 128 / 192 new requests overlapping older later-phase execution in the same pool.', va='top')
fields = [('first_wait_p99_ms', 'First-wave waiting p99 (ms)'),
          ('successors_wait_p99_ms', 'Later successors waiting p99 (ms)'),
          ('successors_wait_max_ms', 'Later successors maximum waiting (ms)')]
for ax, (field, label) in zip(axes, fields):
    for seed, offset, marker, color in [(421, -.07, 'o', '#2463a6'), (422, .07, 'D', '#d86e21')]:
        values = [next(r[field] for r in evidence['rows']
                       if r['shape'] == 'tail4' and r['target_limit'] == limit and r['seed'] == seed)
                  for limit in (0, 1, .75)]
        ax.scatter(np.arange(3) + offset, values, s=45, marker=marker, color=color, label=f'seed {seed}', zorder=3)
    ax.set_xticks(range(3), ['off', '1.0', '0.75'])
    ax.set_ylabel(label)
    ax.set_ylim(bottom=0)
    ax.grid(axis='y', alpha=.2)
axes[0].legend(loc='upper left', bbox_to_anchor=(0, -.20), ncol=2, frameon=False)
fig.text(.05, .06, 'First-wave: 15,360 callbacks/run. Later ordinary successors: 16,128 callbacks/run; excludes 768 later stage0.\n'
         'Waiting = after-routing submission to callback start, not pure Folly queue or kernel runqueue time.\n'
         'A very low callback p99 can coexist with isolated 57-72 ms waits. Do not subtract quantiles to split request latency.', fontsize=9)
for suffix in ('png', 'svg'):
    fig.savefig(HERE / f'phase_tradeoff.{suffix}', dpi=180, metadata={'Date': None} if suffix == 'svg' else None)
plt.close(fig)
for path in HERE.glob('*_tradeoff.svg'):
    path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()) + '\n')
print('4027: two figures, PNG + SVG; per-seed observations retained')

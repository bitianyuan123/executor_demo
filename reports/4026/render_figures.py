#!/usr/bin/env python3
"""Draw the six existing runs, with callback-wall and cohort boundaries explicit."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                     'axes.spines.top': False, 'axes.spines.right': False,
                     'svg.hashsalt': 'executor-demo-4026'})
e = json.loads((HERE / 'evidence.json').read_text())
rows = sorted(e['runs'], key=lambda r: (r['derived']['seed'], {'home':0, 'queue':1, 'outstanding':2}[r['derived']['policy']]))
labels = [f"{ {'home':'home', 'queue':'Q', 'outstanding':'O'}[r['derived']['policy']]}\n{r['derived']['seed']}" for r in rows]
x = np.arange(6)
fig, axes = plt.subplots(1, 2, figsize=(13, 6.5))
fig.subplots_adjust(left=.07, right=.98, bottom=.29, top=.74, wspace=.24)
fig.suptitle('3M tail4: a small callback subset contains most later-phase waiting overlap',
             x=.05, y=.97, ha='left', fontsize=14, weight='bold')
fig.text(.05, .90, 'Six existing runs, not new trials. Regular 12 QPS; 80 x 1 then 4 x 22 operators; six cluster pools, 16 workers each.\n'
         '192-request cohort / run; 16,896 later-wave callbacks = 768 later stage0 + 16,128 ordinary successors.', va='top')
for dx, key, name, color in [(-.19, 'later_wait_exposed_callbacks_percent', 'Callbacks with a same-pool wait intersection', '#2463a6'),
                             (.19, 'later_wait_covered_percent', 'Share of total later-wave wait covered', '#d86e21')]:
    vals = [r['derived']['pool'][key] for r in rows]
    axes[0].bar(x + dx, vals, .36, color=color, label=name)
    for xi, val in zip(x + dx, vals):
        axes[0].text(xi, val + 1.5, f'{val:.1f}', ha='center', fontsize=8)
axes[0].set_ylabel('Share (%) — different, labeled denominators')
axes[0].set_ylim(0, 110)
axes[0].legend(loc='upper left', bbox_to_anchor=(0, -.20), fontsize=8, frameon=False)
covered = [r['overlap']['scopes']['pool']['inverse_later_wait']['covered_union_callback_wait_ms'] for r in rows]
uncovered = [r['derived']['pool']['later_wait_uncovered_callback_ms'] for r in rows]
axes[1].bar(x, covered, .65, color='#d86e21', label='Covered by newer first-wave execution in same pool')
axes[1].bar(x, uncovered, .65, bottom=covered, color='#a5b6c9', label='Remaining later-wave wait')
for xi, a, b in zip(x, covered, uncovered):
    axes[1].text(xi, a + b + 50, f'{a+b:.1f}', ha='center', fontsize=9)
axes[1].set_ylim(0, 2800)
axes[1].set_ylabel('Sum of callback waiting (callback-ms)')
axes[1].legend(loc='upper left', bbox_to_anchor=(0, -.20), fontsize=8, frameon=False)
for ax in axes:
    ax.set_xticks(x, labels)
    ax.grid(axis='y', alpha=.15)
    ax.set_axisbelow(True)
fig.text(.05, .045, 'Execution = callback [start, finish), including preemption, not on-CPU. Covered waits are unions per callback.\n'
         'Overlap establishes temporal/resource association, not causal delay; it is neither CPU service nor request critical-path cost.', fontsize=9)
fig.savefig(HERE / 'wait_localization.png', dpi=180)
fig.savefig(HERE / 'wait_localization.svg', metadata={'Date': None})
plt.close(fig)

fig, axes = plt.subplots(1, 3, figsize=(13, 6.5))
fig.subplots_adjust(left=.06, right=.98, bottom=.27, top=.72, wspace=.32)
fig.suptitle('Request latency still improves while total CPU cost rises', x=.05, y=.97,
             ha='left', fontsize=15, weight='bold')
fig.text(.05, .90, 'Same six runs as the overlap analysis; latency and CPU values were already returned in 4024.\n'
         'Two seed observations are shown separately. No averaged p99, confidence interval, or newly measured CPU timeline.', va='top')
fields = [('Request mean (ms)', lambda r:r['matched_4024']['cohort']['mean_ms'], (138,160)),
          ('Request p99 (ms)', lambda r:r['matched_4024']['cohort']['p99_ms'], (150,170)),
          ('Process CPU / request (ms)', lambda r:r['matched_4024']['whole']['process_cpu_per_completed_request_ms'], (958,990))]
for ax, (label, getter, ylim) in zip(axes, fields):
    for seed, dx, marker, color in [(421,-.09,'o','#2463a6'),(422,.09,'D','#d86e21')]:
        rr = [r for r in rows if r['derived']['seed'] == seed]
        vals = [getter(r) for r in rr]
        xx = np.arange(3) + dx
        ax.scatter(xx, vals, s=45, marker=marker, color=color, label=f'seed {seed}', zorder=3)
        for xi, val in zip(xx, vals):
            ax.annotate(f'{val:.3f}', (xi,val), textcoords='offset points',
                        xytext=(-4 if seed == 421 else 4, -16 if seed == 421 else 8),
                        ha='center', fontsize=8, color=color)
    ax.set_xticks(range(3), ['home', 'Q', 'O']); ax.set_xlim(-.5,2.5)
    ax.set_ylim(*ylim); ax.set_ylabel(label)
    ax.grid(axis='y', alpha=.2); ax.set_axisbelow(True)
    ax.set_title('192-request cohort' if ax is not axes[2] else 'Whole run: 240 completions', fontsize=10)
axes[0].legend(loc='upper left', bbox_to_anchor=(0,-.19), ncol=2, fontsize=9, frameon=False)
fig.text(.05,.065, 'Point-plot axes show restricted ranges. CPU / request is a cost, not CPU utilization.\n'
         'This graph is a structural sensitivity case, not a reconstructed production DAG. Two seeds do not establish a stable winner.', fontsize=9)
fig.savefig(HERE/'request_tradeoff.png',dpi=180)
fig.savefig(HERE/'request_tradeoff.svg',metadata={'Date':None})

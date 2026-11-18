#!/usr/bin/env python3
"""Show the twelve existing extremes, without inferring CPU occupancy."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
e = json.loads((HERE / 'evidence.json').read_text())
rows = sorted(e['extremes'], key=lambda r: (r['seed'], r['shape'], {0: 0, 1: 1, .75: 2}[r['target_limit']]))
labels = [f"{r['seed']} {r['case']}  r{r['request']}" for r in rows]
y = np.arange(len(rows))
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9,
                     'axes.spines.top': False, 'axes.spines.right': False,
                     'svg.hashsalt': 'executor-demo-4028'})
fig, axes = plt.subplots(1, 2, figsize=(13, 7.6), gridspec_kw={'width_ratios': [1, 1.35]}, sharey=True)
fig.subplots_adjust(left=.22, right=.98, top=.78, bottom=.25, wspace=.16)
fig.suptitle('4028: one callback wait above 20 ms in every existing run',
             x=.03, y=.97, ha='left', fontsize=16, weight='bold')
fig.text(.03, .90, 'Same twelve 4027 runs; offline detail, no new trials. Each extreme belongs to its run\'s slowest request.\n'
         'Eight are request 42; nine use pool 0. Fixed 12 QPS cannot separate request index from time since run origin.', va='top')
axes[0].hlines(y, [r['submitted_ms_from_origin'] / 1000 for r in rows],
               [r['start_ms_from_origin'] / 1000 for r in rows], color='#2463a6', lw=3)
axes[0].scatter([r['submitted_ms_from_origin'] / 1000 for r in rows], y, color='#2463a6', s=18, zorder=3)
axes[0].set_xlabel('Submit-to-start position (seconds from run origin)')
axes[0].set_xlim(2.35, 3.8)
axes[0].set_yticks(y, labels)
axes[0].invert_yaxis()
axes[0].grid(axis='x', alpha=.2)
covered = [r['covered_union_ms'] for r in rows]
uncovered = [r['uncovered_wall_ms'] for r in rows]
axes[1].barh(y, covered, color='#92b2d4', label='Covered by other recorded same-pool callback wall intervals')
axes[1].barh(y, uncovered, left=covered, color='#d86e21', label='No such recorded interval covers this portion')
for yi, row in zip(y, rows):
    axes[1].text(row['wait_ms'] + .7, yi, f"{row['wait_ms']:.1f}", va='center', fontsize=8)
axes[1].set_xlim(0, 80)
axes[1].set_xlabel('Waiting interval length (ms), partitioned by wall coverage')
axes[1].grid(axis='x', alpha=.2)
handles, names = axes[1].get_legend_handles_labels()
fig.legend(handles, names, loc='lower left', bbox_to_anchor=(.03, .12), frameon=False, fontsize=9)
fig.text(.03, .045, 'Uncovered does NOT mean CPU idle. start/finish omit wrapper prefixes, observers and cleanup; the wait also includes submission.\n'
         'Covered intervals include preemption and are not on-CPU service. No attribution to the kernel, Folly or destination admission is established.\n'
         'All samples remain in the original 192-request means; no warmup changes, censoring or invented CPU timeline.', fontsize=9)
fig.savefig(HERE / 'extreme_waits.png', dpi=180)
fig.savefig(HERE / 'extreme_waits.svg', metadata={'Date': None})
plt.close(fig)
p = HERE / 'extreme_waits.svg'
p.write_text('\n'.join(line.rstrip() for line in p.read_text().splitlines()) + '\n')
print('4028: one two-panel figure, PNG + SVG')

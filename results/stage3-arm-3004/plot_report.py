#!/usr/bin/env python3
"""Plot reported three-decimal aggregates; requires matplotlib, no raw data.

Run after extract_terminal.py. Empty buckets are omitted; p99 is never pooled.
"""
import csv
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUT = ROOT/'figures'
OUT.mkdir(exist_ok=True)
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                     'svg.fonttype': 'none', 'svg.hashsalt': 'stage3-arm-3004'})


def read(name):
    with (ROOT/name).open() as stream:
        return list(csv.DictReader(stream))


def save(fig, name):
    fig.savefig(OUT/(name+'.png'), dpi=170, bbox_inches='tight')
    fig.savefig(OUT/(name+'.svg'), bbox_inches='tight', metadata={'Date': None})
    svg = OUT/(name+'.svg')
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines())+'\n')
    plt.close(fig)


summary = {row['policy']: row for row in read('formal_policy_summary.csv')}
order = ['home', 'source_only', 'guarded']
labels = ['Home', 'Source > 0.8', 'Source > 0.8\nTarget < 0.5']
fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.7), gridspec_kw={'width_ratios': [1.55, 1]})
for index, (field, label, color) in enumerate([('S_mean_ms', 'Task S mean', '#2166ac'),
                                             ('S_p99_ms', 'Task S p99', '#d6604d')]):
    xs = [x+(index-.5)*.33 for x in range(3)]
    ys = [float(summary[p][field]) for p in order]
    axes[0].bar(xs, ys, width=.31, color=color, label=label)
    for x, y in zip(xs, ys):
        axes[0].text(x, y+3, f'{y:.3f}', ha='center', va='bottom', fontsize=9)
axes[0].set_ylim(0, 180)
axes[0].set_ylabel('S = end - submit (ms)')
axes[0].set_title('Same request cohort: 96,000 tasks / 600 requests')
axes[0].legend(loc='upper right', fontsize=9)
cpu = [float(summary[p]['CPU_union_execution_pct']) for p in order]
axes[1].bar(range(3), cpu, color='#738996', width=.55)
for x, value in enumerate(cpu):
    axes[1].text(x, value+2.5, f'{value:.3f}%', ha='center', va='bottom', fontsize=10)
axes[1].set_ylim(0, 100)
axes[1].set_ylabel('Selected 96 logical CPUs: execution (%)')
axes[1].set_title('CPU use over the arrival window [10, 60) s')
for ax in axes:
    ax.set_xticks(range(3), labels)
    ax.grid(axis='y', alpha=.2)
    ax.set_axisbelow(True)
fig.suptitle('Request wave: 12/s, 80 initial + 80 steady tasks, 25 ms mean CPU budget', fontsize=12)
fig.text(.5, -.035, 'Thresholds use pre-submit N/C. Each policy ran once; no confidence intervals.\n'
         'Terminal aggregates retain 3 decimals. CPU uses a separate scale.',
         ha='center', fontsize=9)
fig.tight_layout()
save(fig, 'policy_effects')

buckets = read('formal_buckets.csv')
fig, axes = plt.subplots(1, 3, figsize=(14, 4.8), sharey=True)
ranges = ['[0, .5)', '[.5, .8)', '[.8, 1)', '[1, 2)', '[2, 4)', '[4, inf)']
for ax, phase in zip(axes, ['all', 'initial', 'steady']):
    for metric, color, marker, style in [('N/C', '#2166ac', 'o', '-'), ('Q/C', '#d6604d', 's', '--')]:
        rows = [row for row in buckets if row['phase'] == phase and row['metric'] == metric]
        # NaN prevents any line/marker for the explicitly empty Q/C tail bucket.
        values = [float(row['S_p99_ms']) if int(row['n']) else math.nan for row in rows]
        ax.plot(range(6), values, label=metric, color=color, marker=marker, linestyle=style, linewidth=1.8)
        for x, (row, y) in enumerate(zip(rows, values)):
            if row['tail_quality'] == 'LOW_TAIL':
                ax.annotate(f"n={row['n']}*", (x, y), xytext=(4, 8), textcoords='offset points', fontsize=8, color=color)
    ax.set_title({'all': 'All tasks (96,000)', 'initial': 'Initial tasks (48,000)', 'steady': 'Steady tasks (48,000)'}[phase])
    ax.set_xticks(range(6), ranges, rotation=35, ha='right')
    ax.set_xlabel('Pre-submit load bucket (categorical intervals)')
    ax.set_ylim(0, 180)
    ax.grid(alpha=.2)
    ax.set_axisbelow(True)
    ax.text(5, 8, 'Q/C: empty', ha='right', fontsize=8, color='#d6604d')
axes[0].set_ylabel('Same-task S p99 (ms)')
axes[0].legend(loc='lower left', fontsize=9)
fig.suptitle('Home policy: conditional task p99 by pre-submit N/C or Q/C', fontsize=13)
fig.text(.5, -.025, 'Equal bounds do not mean equal task membership for N/C and Q/C. No pooled p99 or empty-bucket extrapolation.\n'
         '* Fewer than 1,000 tasks: limited tail observations. One run; rounded terminal evidence, not raw-task recomputation.',
         ha='center', fontsize=9)
fig.tight_layout()
save(fig, 'conditional_p99')
print('Saved figures/policy_effects.{png,svg} and figures/conditional_p99.{png,svg}')

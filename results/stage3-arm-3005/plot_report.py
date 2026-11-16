#!/usr/bin/env python3
"""Plot only terminal aggregates; requires matplotlib, never invent a 5ms curve."""
import csv
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
OUT = ROOT/'figures'
OUT.mkdir(exist_ok=True)
POLICIES = ['home', 'guarded', 'source_only']
COLORS = ['#777777', '#d88b16', '#2474b5']
LABELS = ['Home only', 'Source > 0.8 + target < 0.5', 'Source > 0.8']
SHORT = ['Home', 'Guarded', 'Source only']
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                     'svg.fonttype': 'none', 'svg.hashsalt': 'stage3-arm-3005'})


def read(name):
    with (ROOT/(name+'.csv')).open() as stream:
        return list(csv.DictReader(stream))


def save(fig, name):
    fig.savefig(OUT/(name+'.png'), dpi=180, bbox_inches='tight')
    fig.savefig(OUT/(name+'.svg'), bbox_inches='tight', metadata={'Date': None})
    target = OUT/(name+'.svg')
    target.write_text('\n'.join(line.rstrip() for line in target.read_text().splitlines())+'\n')
    plt.close(fig)


windows = read('cpu_trace_aligned_windows')
summary = {r['policy']: r for r in read('policy_summary')}
fig, axes = plt.subplots(1, 2, figsize=(12.7, 5.8), gridspec_kw={'width_ratios': [2.05, 1]})
for k, (policy, label, color) in enumerate(zip(POLICIES, LABELS, COLORS)):
    rows = [r for r in windows if r['policy'] == policy]
    xs = [i+(k-1)*.25 for i in range(5)]
    ys = [float(r['mean_home_nonidle_residency_pct']) for r in rows]
    axes[0].bar(xs, ys, width=.235, color=color, label=label)
    for x, y in zip(xs, ys):
        axes[0].text(x, y+1.6, f'{y:.1f}', ha='center', va='bottom', fontsize=8)
axes[0].set_xticks(range(5), ['0–30', '30–100', '100–200', '200–300', '300–420'])
axes[0].set_xlabel('Time after request planned start (ms; categorical windows)')
axes[0].set_ylabel('Mean home-cluster non-idle task residency (%)')
axes[0].set_ylim(0, 112)
axes[0].set_yticks(range(0,101,20))
axes[0].set_title('600 request contexts per policy / window', pad=13)
axes[0].legend(loc='upper center', bbox_to_anchor=(.5, -.21), ncol=1, frameon=False, fontsize=9)
latencies = [float(summary[p]['S_p99_ms']) for p in POLICIES]
axes[1].bar(range(3), latencies, color=COLORS, width=.63)
for i, value in enumerate(latencies):
    axes[1].text(i, value+4, f'{value:.3f}', ha='center', fontsize=10)
axes[1].set_xticks(range(3), SHORT)
axes[1].set_ylabel('S = task end − submit (ms)')
axes[1].set_ylim(0, 185)
axes[1].set_title('Task completion p99', pad=13)
for ax in axes:
    ax.grid(axis='y', alpha=.2)
    ax.set_axisbelow(True)
fig.suptitle('ARM 3005: short release burst, prolonged local saturation, policy differences', fontsize=13, y=.98)
fig.subplots_adjust(left=.065, right=.98, top=.85, bottom=.30, wspace=.28)
fig.text(.5, .025, 'Bars use five printed window averages, not a reconstructed continuous 5 ms curve.\n'
         'Aligned home contexts overlap and include other requests / migrated work. One run per policy; descriptive only.',
         ha='center', fontsize=9)
save(fig, 'cpu_phase_summary')

scopes = read('cpu_trace_scopes')
fig, axes = plt.subplots(1, 2, figsize=(10.6, 4.9))
for i, (policy, color) in enumerate(zip(POLICIES, COLORS)):
    pools = [r for r in scopes if r['policy'] == policy and r['scope'].startswith('pool')]
    values = [float(r['longest_observed_ge90_ms']) for r in pools]
    # Six observations are per-cluster maxima, not six independent workload runs.
    axes[0].vlines(i, min(values), max(values), color=color, linewidth=4, alpha=.6)
    axes[0].scatter([i+(j-2.5)*.035 for j in range(6)], values, color=color, s=30, zorder=3)
    axes[0].text(i, max(values)+9, f'{min(values):.0f}–{max(values):.0f} ms', ha='center', fontsize=10)
    fractions = [100*float(r['ge90_duration_fraction']) for r in pools]
    axes[1].vlines(i, min(fractions), max(fractions), color=color, linewidth=4, alpha=.6)
    axes[1].scatter([i+(j-2.5)*.035 for j in range(6)], fractions, color=color, s=30, zorder=3)
    axes[1].text(i, max(fractions)+1.8, f'{min(fractions):.1f}–{max(fractions):.1f}%', ha='center', fontsize=10)
axes[0].set_ylabel('Longest observed streak per cluster (ms)')
axes[0].set_ylim(0, 220)
axes[0].set_title('Cluster occupancy ≥90%, consecutive 5 ms bins')
axes[1].set_ylabel('Share of observed time with occupancy ≥90%')
axes[1].set_ylim(0, 40)
axes[1].set_title('Time fraction per cluster during [10, 60) s')
for ax in axes:
    ax.set_xlim(-.5,2.5)
    ax.set_xticks(range(3), SHORT)
    ax.grid(axis='y', alpha=.2)
    ax.set_axisbelow(True)
fig.suptitle('ARM 3005: CPU saturation duration across six clusters', fontsize=13)
fig.text(.5,.025, 'Each dot is one cluster; annotated ranges are min–max across six clusters, not uncertainty or request-duration percentiles.\n'
         '90% is a reporting threshold only. One workload run per policy; all 9,999 complete bins and 96 CPUs reported covered.',
         ha='center', fontsize=9)
fig.subplots_adjust(left=.075, right=.98, top=.82, bottom=.22, wspace=.27)
save(fig, 'cpu_saturation_duration')
print('Saved figures/cpu_phase_summary.{png,svg} and figures/cpu_saturation_duration.{png,svg}')

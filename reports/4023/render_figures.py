#!/usr/bin/env python3
"""Render 4023 evidence; keep selected-request profiles and matrix contracts separate."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np

HERE = Path(__file__).resolve().parent
POOL_COLORS = ['#2463a6', '#e28b27', '#31966a', '#ac66a3', '#d34e52', '#73aab0']
POLICY_COLORS = {'queue': '#2463a6', 'outstanding': '#d86e21'}
plt.rcParams.update({
    'font.family': 'DejaVu Sans', 'font.size': 10,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.titlesize': 12, 'axes.labelsize': 10,
    'svg.hashsalt': 'executor-demo-4023', 'savefig.facecolor': 'white',
})


def save(fig: plt.Figure, name: str) -> None:
    for extension in ('png', 'svg'):
        kwargs = {'dpi': 180} if extension == 'png' else {'metadata': {'Date': None}}
        fig.savefig(HERE / f'{name}.{extension}', **kwargs)
    plt.close(fig)


def request_profiles(evidence: dict, rows: list[dict]) -> None:
    fig, axes = plt.subplots(3, 3, figsize=(15.8, 12.1), sharex='col')
    fig.subplots_adjust(left=.07, right=.985, top=.825, bottom=.27,
                        hspace=.35, wspace=.24)
    fig.suptitle('Selected request 6: CPU residence and callback lifecycle',
                 x=.07, y=.965, ha='left', fontsize=17, weight='bold')
    fig.text(.07, .925,
             'Three separate captures; 1 ms bins with a partial final bin. Each column uses its own time axis.\n'
             'Request CPU residence is sched_switch time intersected with callback scope, not CPU demand or runnable count.',
             fontsize=10.5, va='top')
    titles = ['3,000,000 iterations | home', '375,000 iterations | home',
              '375,000 iterations | outstanding (O)']
    for col, (profile, title) in enumerate(zip(evidence['profiles'], titles)):
        selected = [row for row in rows if row['profile'] == profile['label']]
        totals = sorted((row for row in selected if row['pool'] == 'all'),
                        key=lambda row: int(row['bin']))
        edges = np.array([float(row['relative_start_ms']) for row in totals] +
                         [float(totals[-1]['relative_end_ms'])])
        n = len(totals)
        request = np.zeros((6, n))
        outside = np.zeros((6, n))
        for pool in range(6):
            group = sorted((row for row in selected if row['pool'] == str(pool)),
                           key=lambda row: int(row['bin']))
            if len(group) != n:
                raise ValueError('Missing pool bins')
            logical_cpus = len(profile['summary']['groups'][pool]['cpus'])
            for index, row in enumerate(group):
                if row['cluster_window_fully_bracketed'] != '1':
                    raise ValueError('Cannot plot unbracketed CPU window as measured zero')
                request[pool, index] = float(row['request_scheduled_cpu_equivalent'])
                nonidle = float(row['cluster_nonidle_residence_percent']) * logical_cpus / 100
                residual = nonidle - request[pool, index]
                if residual < -1e-8:
                    raise ValueError('Selected request residence exceeds group nonidle residence')
                outside[pool, index] = max(0, residual)
        for row_index, values in [(0, request), (1, outside)]:
            ax = axes[row_index, col]
            # Duplicate the final value solely to draw a final partial-bin step.
            expanded = np.column_stack((values, values[:, -1]))
            ax.stackplot(edges, expanded, colors=POOL_COLORS, step='post', alpha=.87)
            ax.set_xlim(0, edges[-1])
            ax.grid(axis='y', alpha=.20, zorder=0)
            ax.set_axisbelow(True)
            if row_index == 0:
                ax.set_ylim(0, 50)
                ax.axhline(16, color='#59616c', linestyle=':', linewidth=1)
                ax.text(.98, .34, '16 = one group', transform=ax.transAxes,
                        ha='right', fontsize=8, color='#59616c')
            else:
                ax.set_ylim(0, max(.1, float(outside.sum(axis=0).max()) * 1.15))
                area = float((outside.sum(axis=0) * np.diff(edges)).sum())
                ax.text(.97, .93, f'Area: {area:.3f} CPU-ms', transform=ax.transAxes,
                        ha='right', va='top', fontsize=9,
                        bbox={'facecolor': 'white', 'alpha': .82, 'edgecolor': 'none'})
        axes[0, col].set_title(title + f'\nspan = {edges[-1]:.3f} ms', pad=12)
        bottom = axes[2, col]
        for field, color, label in [
            ('request_submitted_not_started_mean', '#2463a6', 'Submitted, not started'),
            ('request_callback_wall_occupancy_mean', '#c35b34', 'Inside callback (wall)'),
        ]:
            values = np.array([float(row[field]) for row in totals])
            bottom.stairs(values, edges, color=color, linewidth=1.7, label=label)
        bottom.set_ylim(0, 70)
        bottom.set_xlabel('Time since request attempt (ms)')
        bottom.grid(axis='y', alpha=.20)
        bottom.set_axisbelow(True)
    axes[0, 0].set_ylabel('Selected request callbacks\nCPU equivalents, by group')
    axes[1, 0].set_ylabel('Outside selected callback scope\nNonidle CPU equivalents, by group')
    axes[2, 0].set_ylabel('Selected request lifecycle\nPer-bin mean callback count')
    fig.text(.07, .205, 'Middle row uses native y scales. Outside scope does not identify other requests; '
             'it may include any nonidle execution outside the selected callbacks.', fontsize=9.5)
    handles = [Patch(color=color, label=f'Group {i} ({"NUMA 0" if i < 3 else "NUMA 2"})')
               for i, color in enumerate(POOL_COLORS)]
    fig.legend(handles=handles, ncol=6, loc='lower left', bbox_to_anchor=(.065, .155),
               frameon=False, fontsize=9)
    fig.legend(handles=[Line2D([0], [0], color='#2463a6', lw=2, label='Submitted, not started'),
                        Line2D([0], [0], color='#c35b34', lw=2, label='Inside callback (wall)')],
               ncol=2, loc='lower left', bbox_to_anchor=(.065, .124), frameon=False, fontsize=9)
    sha = evidence['source_log_sha256'][:16]
    fig.text(.07, .105,
             'Lifecycle curves are wrapper timestamps: neither is native Folly Q or kernel runq. '
             'Wall occupancy includes preemption and waiting.\n'
             f'Source: 4023.log lines 17-1152; SHA256 {sha}...; exported request_cpu_windows.csv.\n'
             'Explicit request 6 samples, not representative or p99 tests. These captures use the original jitter/heavy-operator contract\n'
             'and perf; they are not CPU curves for the uniform-work shape matrix in the companion figure.',
             fontsize=9, va='top', linespacing=1.5)
    save(fig, 'request_cpu_profiles')


def matrix_pairs(evidence: dict) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(14.2, 11.8), sharex=True)
    fig.subplots_adjust(left=.11, right=.975, top=.855, bottom=.245, hspace=.23)
    fig.suptitle('Uncalibrated shape hypotheses: paired policy changes',
                 x=.11, y=.965, ha='left', fontsize=17, weight='bold')
    fig.text(.11, .925,
             'Each point = one seed, one candidate vs its same-seed / same-shape / same-work home run.\n'
             'Whole-run n = 240 each. Mean and p99 changes are separate summaries; no pooled quantiles or confidence intervals.',
             fontsize=10.5, va='top')
    cells = [(iterations, shape) for iterations in (46875, 375000, 3000000)
             for shape in ('front80', 'tail4')]
    offsets = {('queue', 421): -.21, ('queue', 422): -.07,
               ('outstanding', 421): .07, ('outstanding', 422): .21}
    markers = {421: 'o', 422: '^'}
    specs = [('mean_change_percent', -1, 'Mean latency improvement (%)\nPositive = lower latency'),
             ('p99_change_percent', -1, 'p99 improvement (%)\nPositive = lower p99'),
             ('cpu_change_percent', 1, 'CPU / request increase (%)\nPositive = more CPU cost')]
    for ax, (field, sign, label) in zip(axes, specs):
        for index, cell in enumerate(cells):
            if index % 2:
                ax.axvspan(index-.48, index+.48, color='#f0f2f5', zorder=0)
            points = [pair for pair in evidence['pairs']
                      if (pair['iterations'], pair['shape']) == cell]
            if len(points) != 4:
                raise ValueError('Expected two policies x two seeds per workload cell')
            for point in points:
                key = point['policy'], point['seed']
                ax.scatter(index+offsets[key], sign*point[field], s=67,
                           color=POLICY_COLORS[point['policy']], marker=markers[point['seed']],
                           edgecolor='white', linewidth=.6, zorder=4)
        ax.axhline(0, color='#555b65', linewidth=1)
        for boundary in (1.5, 3.5):
            ax.axvline(boundary, color='#a4a9b2', linestyle=':', linewidth=1)
        ax.grid(axis='y', alpha=.20)
        ax.set_axisbelow(True)
        ax.set_ylabel(label)
        ax.set_xlim(-.55, 5.55)
    axes[0].set_ylim(-2, 55)
    axes[1].set_ylim(-15, 55)
    axes[2].set_ylim(-1, 32)
    axes[2].set_xticks(range(6),
        [f'{iterations:,} iterations\n{shape}' for iterations, shape in cells])
    axes[2].set_xlabel('Fixed work per operator and dependency shape (not production-calibrated)', labelpad=10)
    handles = [Line2D([0], [0], marker=markers[seed], linestyle='none',
                      color=POLICY_COLORS[policy], markersize=8,
                      label=f'{"Q" if policy == "queue" else "O"}, seed {seed}')
               for policy in ('queue', 'outstanding') for seed in (421, 422)]
    fig.legend(handles=handles, loc='lower left', ncol=4, frameon=False,
               bbox_to_anchor=(.105, .145), fontsize=10)
    sha = evidence['source_log_sha256'][:16]
    fig.text(.11, .125,
             'Q = queue scorer; O = outstanding scorer. All 36 runs completed 240 requests, so total process-CPU ratios equal CPU/request ratios.\n'
             'Six explicit CPU groups, 16 workers/group; regular 12 QPS; RR home; parent/all; same NUMA only; trace=1, no perf.\n'
             'front80 = 80x2 + 4x2; tail4 = 80x1 + 4x22; both 168 operators. Equal work counts do not imply equal CPU service.\n'
             f'Source: 4023.log lines 1176-1626; SHA256 {sha}...; evidence.json paired whole-run console summaries.\n'
             'Only 375k also has cohort/fingerprint reports; other scales are provisional whole-run descriptions. Two seeds do not establish stable p99\n'
             'or production fidelity. This figure has a uniform-work contract; selected-request perf profiles above are a different experiment.',
             fontsize=9, va='top', linespacing=1.5)
    save(fig, 'shape_policy_pairs')


def main() -> None:
    evidence = json.loads((HERE / 'evidence.json').read_text())
    with (HERE / 'request_cpu_windows.csv').open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    request_profiles(evidence, rows)
    matrix_pairs(evidence)
    print('Rendered request_cpu_profiles.{png,svg} and shape_policy_pairs.{png,svg}')


if __name__ == '__main__':
    main()

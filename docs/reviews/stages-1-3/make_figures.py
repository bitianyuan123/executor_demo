#!/usr/bin/env python3
"""Rebuild review figures from archived terminal-table extracts, not task CSVs.

Requires matplotlib. No experiment execution or target access.
"""
import csv
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties, fontManager
import numpy as np


ROOT = Path(__file__).resolve().parent
DATA, OUT = ROOT / 'data', ROOT / 'figures'
FONT = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
if FONT.is_file():
    fontManager.addfont(str(FONT))
    plt.rcParams['font.family'] = FontProperties(fname=FONT).get_name()
plt.rcParams.update({'font.size': 10, 'axes.spines.top': False,
                     'axes.spines.right': False, 'axes.unicode_minus': False,
                     'svg.hashsalt': 'stage123-review', 'savefig.facecolor': '#ffffff'})


def read(name):
    with (DATA / name).open() as file:
        return list(csv.DictReader(file))


def save(fig, name):
    fig.savefig(OUT / (name + '.png'), dpi=170, bbox_inches='tight')
    fig.savefig(OUT / (name + '.svg'), bbox_inches='tight', metadata={'Date': None})
    svg = OUT / (name + '.svg')
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    plt.close(fig)


def mean_components():
    runs = read('arm_runs.csv')
    families = [
        ('s3-burst', ['smooth', 'spread', 'local'], ['平滑', '突发分散', '突发集中'], '改变投递集中程度｜每池16线程'),
        ('s3-threads', ['t8', 't16', 't64', 't256'], ['8', '16', '64', '256'], '改变每池线程数｜cluster亲和'),
        ('s3-deployment', ['none', 'numa', 'cluster'], ['none', 'numa', 'cluster'], '改变亲和范围｜每池256线程'),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(14, 8), gridspec_kw={'height_ratios': [1.1, 1]})
    colors = ['#387a99', '#ebaa54', '#955a96']
    labels = ['提交→回调开始', '回调开始→结束', '计划到达→提交']
    for col, (phase, cases, names, title) in enumerate(families):
        groups = [[r for r in runs if r['phase'] == phase and r['case'] == case] for case in cases]
        assert all(len(rows) == 3 for rows in groups), phase
        for rows in groups:
            assert {int(r['seed']) for r in rows} == {420, 421, 422}
        x = np.arange(len(cases))
        parts = []
        for key in ('wait_mean_ms', 'callback_mean_ms'):
            parts.append([statistics.fmean(float(r[key]) for r in rows) for rows in groups])
        residuals = [[float(r['planned_mean_ms']) - float(r['wait_mean_ms']) - float(r['callback_mean_ms']) for r in rows] for rows in groups]
        assert min(v for vs in residuals for v in vs) >= -.002  # log rounding only
        parts.append([max(0, statistics.fmean(vs)) for vs in residuals])
        bottom = np.zeros(len(cases))
        for values, color, label in zip(parts, colors, labels):
            axes[0, col].bar(x, values, bottom=bottom, width=.55, color=color, label=label)
            bottom += values
        for xpos, total in zip(x, bottom):
            axes[0, col].text(xpos, total + 12, f'{total:.1f}', ha='center', fontsize=10)
        for seed, marker, offset, color in zip((420, 421, 422), ('o', 's', '^'), (-.12, 0, .12), ('#24627b', '#8e4d89', '#bd6a25')):
            vals = [float(next(r for r in rows if int(r['seed']) == seed)['planned_p99_ms']) for rows in groups]
            axes[1, col].scatter(x + offset, vals, marker=marker, s=30, color=color, label=f'seed {seed}', zorder=3)
        axes[0, col].set_title(title, pad=14, fontsize=11)
        for ax in axes[:, col]:
            ax.set_xticks(x, names)
            ax.set_axisbelow(True)
            ax.grid(axis='y', alpha=.2)
        axes[0, col].set_ylim(0, 760)
        axes[1, col].set_ylim(0, 1450)
    axes[0, 0].set_ylabel('planned完成均值（ms）')
    axes[1, 0].set_ylabel('每轮planned完成p99（ms）')
    axes[0, 2].legend(frameon=False, fontsize=9, loc='upper right')
    axes[1, 2].legend(frameon=False, fontsize=9, loc='upper right')
    fig.suptitle('阶段3：平均压力相同，集中度、线程数和亲和范围改变时延', fontsize=17, y=.98)
    fig.text(.5, .035, '共同条件：6池，240 task/s，目标平均100ms线程CPU；窗口[10,60)秒。\n上排为三轮均值的分量；下排为每轮p99原始摘要点，非合并p99、非置信区间。', ha='center', fontsize=10)
    fig.subplots_adjust(top=.88, bottom=.15, hspace=.3, wspace=.18)
    save(fig, 'latency-components')


def conditional_ranges():
    rows = read('arm_conditional_ranges.csv')
    bins = ['0', '1-3', '4-15', '16-63', '64+']
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.7))
    for case, color, offset in [('none', '#267c9c', -.20), ('numa', '#b3791f', 0), ('cluster', '#945e9a', .20)]:
        selected = [next(r for r in rows if r['phase'] == 's3-deployment' and r['case'] == case and r['queue_bucket'] == b) for b in bins]
        for ax, key in zip(axes, ['wait', 'submitted']):
            lo = np.array([float(r[f'{key}_p99_min_ms']) for r in selected])
            hi = np.array([float(r[f'{key}_p99_max_ms']) for r in selected])
            x = np.arange(len(bins)) + offset
            ax.vlines(x, lo, hi, color=color, linewidth=2.3, label=case)
            for pos, low, high in zip(x, lo, hi):
                ax.hlines([low, high], pos-.065, pos+.065, color=color, linewidth=2.3)
    for ax in axes:
        ax.set_xticks(np.arange(len(bins)), ['0*', '1–3*', '4–15*', '16–63', '64+'])
        ax.set_xlabel('当前任务提交前的Folly queue桶')
        ax.grid(axis='y', alpha=.2)
    axes[0].set_yscale('log')
    axes[0].set_ylim(.02, 700)
    axes[0].set_ylabel('提交→回调开始 p99（ms，对数刻度）')
    axes[1].set_ylim(0, 750)
    axes[1].set_ylabel('提交→完成 p99（ms）')
    fig.suptitle('同为256线程，相同queue桶不能共用一个时延阈值', fontsize=16, y=.98)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .90),
               ncol=3, frameon=False, title='每池CPU允许范围', fontsize=9)
    fig.text(.5, .035, '竖线与端点：三个seed各自分位数的最小–最大范围，非置信区间。\n* 每轮桶内任务数不足2,000，尾样本较少；burst内任务相关。', ha='center', fontsize=10)
    fig.subplots_adjust(top=.77, bottom=.21, wspace=.28)
    save(fig, 'conditional-affinity')


def conditional_same_scale():
    """Compare waiting and completion with identical linear time axes.

    Plot range endpoints only: these are not matched quantiles of the same task.
    """
    rows = read('arm_conditional_ranges.csv')
    bins = ['0', '1-3', '4-15', '16-63', '64+']
    configs = [('s3-burst', 'local', '每池16线程｜cluster'),
               ('s3-deployment', 'cluster', '每池256线程｜cluster')]
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8), sharex=True, sharey=True)
    for col, (phase, case, title) in enumerate(configs):
        selected = [next(r for r in rows if r['phase'] == phase
                         and r['case'] == case and r['queue_bucket'] == b)
                    for b in bins]
        for row, quantile in enumerate(('p50', 'p99')):
            ax = axes[row, col]
            for key, label, color, offset in (
                    ('wait', '提交→回调开始 W', '#267c9c', -.12),
                    ('submitted', '提交→完成 L', '#c27623', .12)):
                lo = [float(r[f'{key}_{quantile}_min_ms']) for r in selected]
                hi = [float(r[f'{key}_{quantile}_max_ms']) for r in selected]
                x = np.arange(len(bins)) + offset
                ax.vlines(x, lo, hi, color=color, linewidth=2.5, label=label)
                for pos, low, high in zip(x, lo, hi):
                    ax.hlines([low, high], pos-.065, pos+.065,
                              color=color, linewidth=2.5)
            ax.set_title(f'{title}｜{quantile}', fontsize=12, pad=10)
            ax.set_xticks(np.arange(len(bins)), ['0*', '1–3*', '4–15*', '16–63', '64+'])
            ax.set_ylim(-12, 800)
            ax.set_yticks(np.arange(0, 801, 200))
            ax.grid(axis='y', alpha=.2)
            ax.set_axisbelow(True)
        axes[1, col].set_xlabel('提交前 queue 分桶（离散类别，非等间距队列值）')
    for ax, quantile in zip(axes[:, 0], ('p50', 'p99')):
        ax.set_ylabel(f'条件{quantile}（ms，统一线性刻度）')
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(.5, .94),
               ncol=2, frameon=False, fontsize=10)
    fig.suptitle('统一坐标后：256线程下完成p99变化较小，但完成p50仍明显上升',
                 fontsize=16, y=.99)
    fig.text(.5, .035,
             '相同cluster范围、local100、240 task/s、100ms平均CPU预算；两列来自不同实验组。\n'
             '竖线为三轮各自分位数的最小–最大范围，非置信区间；* 尾样本较少。\n'
             'W与L的同名分位数不对应同一任务，不能用橙色减蓝色求回调墙钟分位数。',
             ha='center', fontsize=10)
    fig.subplots_adjust(top=.84, bottom=.17, hspace=.29, wspace=.12)
    save(fig, 'queue-latency-same-scale')


def main():
    OUT.mkdir(exist_ok=True)
    mean_components()
    conditional_ranges()
    conditional_same_scale()
    (OUT / 'provenance.json').write_text(json.dumps({
        'input_kind': 'terminal summaries, no per-task CSV or new measurements',
        'input_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (DATA/'arm_runs.csv', DATA/'arm_conditional_ranges.csv')},
        'matplotlib': matplotlib.__version__,
        'font': str(FONT),
        'statistics': 'means of per-run means; individual per-run p99; min/max ranges, never CI',
        'conditional_range_quantiles': ['p50', 'p99'],
        'axis_note': 'conditional-affinity: wait log, completion linear; queue-latency-same-scale: all time axes linear and shared',
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Reproduce concise tables from the reviewed, separately paired experiments."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import statistics


def number(value):
    return 'n/a' if value is None else f'{value:.3f}'


def mechanism(root, batch='dag'):
    audits = json.loads((root / 'analysis' / f'{batch}-paths.json').read_text())
    paired = json.loads((root / 'analysis' / f'{batch}-paired.json').read_text())
    by_path = {r['path']: r for r in paired['runs']}
    result = []
    for audit in audits:
        r = by_path[audit['directory']]
        path = Path(r['path'])
        meta = json.loads((path / 'metadata.json').read_text())
        with (path / 'requests.csv').open() as f:
            requests = list(csv.DictReader(f))
        begin, end = [meta['arrival_origin_ns'] + t * 1e9 for t in r['window_s']]
        selected = {q['request_id'] for q in requests if begin <= int(q['scheduled_ns']) < end}
        paths = [p for p in audit['requests'] if p['request_id'] in selected]
        fields = ['latency_ms', 'path_dispatch_rpc_ms', 'path_submit_to_start_ms', 'path_execution_wall_ms', 'request_tail_ms']
        means = {k: statistics.fmean(p[k] for p in paths) for k in fields}
        if abs(means['latency_ms'] - r['metrics']['planned_latency_ms_mean']) > .000001:
            raise ValueError('path analysis and paired cohort differ')
        targets, queues, transitions = Counter(), Counter(), {}
        with (path / 'tasks.csv').open() as f:
            for t in csv.DictReader(f):
                if t['request_id'] not in selected:
                    continue
                targets[t['chosen_group']] += int(t['work_target_us'])
                queues['total'] += 1
                queues['zero'] += int(float(t['queue_before']) == 0)
                kind = ('join' if t['kind'] == 'join' else 'rpc_resume' if int(t['stage']) else
                        'ordinary_successor' if int(t['depth']) else 'later_fork' if int(t['wave']) else 'initial_fork')
                count = transitions.setdefault(kind, Counter())
                count['callbacks'] += 1; count['migrated'] += int(t['migrated'])
                count['cross_numa'] += int(t['cross_numa'])
        area = 0; events = []
        for q in requests:
            lo, hi = max(begin, int(q['offered_ns'])), min(end, int(q['finish_ns']))
            if lo < hi:
                area += hi - lo; events.extend([(lo, 1), (hi, -1)])
        live = peak = 0
        for _, delta in sorted(events):
            live += delta; peak = max(peak, live)
        result.append({'batch': batch, 'case': r['case'], 'seed': r['seed'], 'cohort_requests': len(paths),
                       'path_means_ms': means, 'target_cpu_by_pool_us': dict(targets),
                       'largest_pool_work_fraction': max(targets.values()) / sum(targets.values()),
                       'zero_queue_task_fraction': queues['zero'] / queues['total'],
                       'routing_by_transition': transitions, 'peak_live_requests': peak,
                       'mean_live_requests': area / (end - begin),
                       'audited_edges_whole_run': audit['edges_checked']})
    return result


def aggregate(root):
    batches = []
    for name in ('independent', 'independent-lower', 'dag', 'dag-threads'):
        if not (root / 'analysis' / f'{name}-paired.json').exists():
            continue
        data = json.loads((root / 'analysis' / f'{name}-paired.json').read_text())
        runs = data['runs']
        batches.append({'name': name, 'stage': data['stage'], 'runs': len(runs),
                        'offered': sum(r['workload']['units'] for r in runs),
                        'cohort_completed': sum(r['completed'] for r in runs),
                        'whole_run_states': dict(sum((Counter(r['whole_run_statuses']) for r in runs), Counter())),
                        'warnings': dict(Counter(w for r in runs for w in r['warnings'])),
                        'comparisons': data['comparisons'],
                        'per_run': [{k: r[k] for k in ('case', 'seed', 'completed', 'metrics', 'diagnostics', 'workload')} for r in runs]})
    return batches


def markdown(batches):
    lines = ['# 本轮修订的实际配对结果', '',
             '全部为4逻辑CPU开发VM、每轮8秒、6个独立seed配对，测量窗口为计划到达1～7秒。不是ARM结果或稳定容量认证。', '',
             '独立任务第一批1600 task/s；第二批800 task/s是看到第一批波动后增加的独立seed验证，保留第一批所有轮次。DAG为Poisson期望12 QPS、60个算子与3个join、5×4深链的3波图。', '',
             'dag-threads是发现DAG完成路径主要消耗在回调墙钟后追加的独立seed诊断，比较home策略每池2与16线程；不是原主矩阵的预注册确认结果。', '',
             '|批次|运行数|整轮offered|测量cohort完成|整轮状态|', '|---|---:|---:|---:|---|']
    for b in batches:
        lines.append(f"|{b['name']}|{b['runs']}|{b['offered']}|{b['cohort_completed']}|{b['whole_run_states']}|")
    lines += ['', '## 预声明对照', '',
              '均值差是candidate − baseline的整轮配对差；区间是探索性95% percentile bootstrap，抽样单位为seed配对。', '',
              '|批次 / 对照|均值差ms|95%区间ms|各pair差值范围ms|窗口预算差范围%|p99差值区间|',
              '|---|---:|---|---|---|---|']
    for b in batches:
        for c in b['comparisons']:
            e = c['estimates'].get('planned_latency_ms_mean', {})
            p99 = c['estimates'].get('planned_latency_ms_p99', {})
            ci = e.get('ci95')
            budgets = [p['cohort_work_difference_pct'] for p in c['pairs'] if p['cohort_work_difference_pct'] is not None]
            lines.append(f"|{b['name']} / {c['name']}|{number(e.get('mean'))}|{', '.join(map(number, ci)) if ci else '不输出'}|"
                         f"{number(e.get('min'))} ～ {number(e.get('max'))}|{number(min(budgets)) if budgets else 'n/a'} ～ "
                         f"{number(max(budgets)) if budgets else 'n/a'}|{'可作探索描述' if p99.get('ci95') else '样本不足，不输出'}|")
    lines += ['', '## 有效性与解释边界', '']
    for b in batches:
        lines.append(f"- {b['name']}：告警计数 `{b['warnings']}`。详细每轮值在同名paired JSON/Markdown，告警并未被删除。")
    lines += ['', '- 所有计划内对照均保留；不能把看到最优结果后新选的策略称为预先确认的最优策略。',
              '- 运行数少、窗口短，即使区间不跨0，也不能宣称0.5%～1%收益可重现或生产配置最优。',
              '- 同均值分布对照精确匹配的是整轮预算，窗口预算仍有有限样本波动；表中同时报告其大小。',
              '- p99门槛只检查每轮样本量，不证明任务独立或尾部精度；DAG本轮明确不足，区间为null。',
              '- 未执行ARM NUMA内存放置或缓存归因实验；候选域功能通过测试不代表已测得跨NUMA收益。', '']
    return '\n'.join(lines)


def plot(batches, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout='constrained')
    for ax, batch_name, title in zip(axes, ('independent-lower', 'dag'), ('Independent tasks: 800 tasks/s', 'DAG: Poisson 12 requests/s')):
        batch = next(b for b in batches if b['name'] == batch_name)
        comparisons = batch['comparisons']
        for i, comparison in enumerate(comparisons):
            e = comparison['estimates'].get('planned_latency_ms_mean', {})
            if e.get('ci95') is None:
                continue
            lo, hi = e['ci95']; mean = e['mean']
            ax.errorbar(mean, i, xerr=[[max(0, mean-lo)], [max(0, hi-mean)]], fmt='o', capsize=5, color='#246a90')
        ax.axvline(0, color='#777777', linewidth=.8)
        ax.set_yticks(range(len(comparisons)), [c['name'].replace('-', ' ') for c in comparisons], fontsize=8)
        ax.invert_yaxis(); ax.grid(axis='x', alpha=.2)
        ax.set_title(title)
        ax.set_xlabel('Candidate - baseline mean latency (ms)\nNegative means lower latency')
    fig.suptitle('Exploratory paired whole-run differences, 95% percentile bootstrap\n6 seeds x 8 seconds; planned-arrival cohort 1-7 seconds; local VM, not ARM', fontsize=11)
    fig.savefig(path, dpi=170)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('results/review'))
    parser.add_argument('--plot', action='store_true')
    args = parser.parse_args()
    batches = aggregate(args.root)
    paths = mechanism(args.root)
    if (args.root / 'analysis' / 'dag-threads-paths.json').exists():
        paths += mechanism(args.root, 'dag-threads')
    (args.root / 'analysis' / 'summary.json').write_text(json.dumps(batches, indent=2) + '\n')
    (args.root / 'analysis' / 'summary.md').write_text(markdown(batches))
    (args.root / 'analysis' / 'dag-mechanism.json').write_text(json.dumps(paths, indent=2) + '\n')
    lines = ['# 相同测量cohort的DAG完成路径与路由', '',
             '按每轮分别取均值，再对6个seed等权平均；路径各均值可加，CPU服务已包含在执行墙钟中。', '',
             '|策略|请求均值ms|路径dispatch/RPC ms|路径submit→start ms|路径执行墙钟ms|最大池名义工作占比均值|queue=0回调比例均值|平均/峰值并发request|',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    for case in sorted({r['case'] for r in paths}):
        rs = [r for r in paths if r['case'] == case]
        values = [statistics.fmean(r['path_means_ms'][k] for r in rs) for k in
                  ('latency_ms', 'path_dispatch_rpc_ms', 'path_submit_to_start_ms', 'path_execution_wall_ms')]
        lines.append('|'+case+'|'+'|'.join(map(number, values))+'|'+number(statistics.fmean(r['largest_pool_work_fraction'] for r in rs))+'|'+
                     number(statistics.fmean(r['zero_queue_task_fraction'] for r in rs))+'|'+
                     number(statistics.fmean(r['mean_live_requests'] for r in rs))+' / '+str(max(r['peak_live_requests'] for r in rs))+'|')
    lines += ['', '完整每轮路由分型、CPU预算池分布和并发见JSON。queue是用户态队列；路径执行墙钟变长不能仅凭本表归因于内核runq或缓存。',
              '最大池工作占比是整段测量的汇总，不能排除较短时间尺度的集中。并发按整个窗口内存活区间计算，包括窗口前到达但尚未完成的request。', '']
    (args.root / 'analysis' / 'dag-mechanism.md').write_text('\n'.join(lines))
    if args.plot:
        plot(batches, args.root / 'paired-mean-differences.png')


if __name__ == '__main__':
    main()

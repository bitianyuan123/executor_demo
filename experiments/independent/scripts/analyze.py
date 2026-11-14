#!/usr/bin/env python3
"""Stage-3 analysis. Stdlib only. Native Folly queue is never labelled kernel runq."""
import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def quantile(values, p=.99):
    values = sorted(values)
    return values[max(0, math.ceil(p * len(values)) - 1)] if values else None


def average(values):
    return statistics.fmean(values) if values else None


def pearson(x, y):
    if len(x) < 3 or len(x) != len(y):
        return None
    mx, my = average(x), average(y)
    vx, vy = sum((a-mx)**2 for a in x), sum((b-my)**2 for b in y)
    return sum((a-mx)*(b-my) for a,b in zip(x,y)) / math.sqrt(vx*vy) if vx and vy else None


def ranks(xs):
    out = [0.] * len(xs)
    order = sorted(range(len(xs)), key=xs.__getitem__)
    i = 0
    while i < len(order):
        j = i+1
        while j < len(order) and xs[order[j]] == xs[order[i]]:
            j += 1
        rank = (i+j-1)/2 + 1
        for k in order[i:j]:
            out[k] = rank
        i = j
    return out


def correlation(rows, field, target):
    rs = [r for r in rows if r[field] >= 0]
    x, y = [r[field] for r in rs], [r[target] for r in rs]
    return {'n': len(rs), 'distinct_load_values': len(set(x)),
            'pearson': pearson(x,y), 'spearman': pearson(ranks(x), ranks(y))}


def stats(rows):
    return {'n': len(rows), 'task_mean_ms': average([r['latency_ms'] for r in rows]),
            'task_p99_ms': quantile([r['latency_ms'] for r in rows]),
            'submit_to_start_mean_ms': average([r['wait_ms'] for r in rows]),
            'callback_wall_mean_ms': average([r['wall_ms'] for r in rows]),
            'planned_to_end_mean_ms': average([r['planned_latency_ms'] for r in rows]),
            'target_cpu_mean_ms': average([r['target_cpu_us']/1000 for r in rows])}


def load_run(path, warmup=0., end=None):
    path = Path(path)
    metadata = json.loads((path/'metadata.json').read_text())
    summary = json.loads((path/'summary.json').read_text())
    if metadata.get('stage') != 3 or not summary.get('conservation_ok'):
        raise ValueError(f'not a complete valid stage-3 run: {path}')
    with (path/'tasks.csv').open() as f:
        raw = list(csv.DictReader(f))
    if len(raw) != metadata['planned_tasks'] or len(raw) != summary['planned_tasks']:
        raise ValueError(f'truncated task CSV: {path}')
    statuses = {s:sum(r['status']==s for r in raw) for s in ['completed','rejected','deadline_rejected','canceled','failed']}
    if statuses['failed'] != summary.get('failed_tasks',0) or statuses['completed'] != summary['completed_tasks'] or statuses['canceled'] != summary['canceled_tasks'] or statuses['rejected']+statuses['deadline_rejected'] != summary['rejected_tasks']:
        raise ValueError(f'task counts disagree: {path}')
    rows = []
    for rawrow in raw:
        if rawrow['status'] != 'completed':
            continue
        r = {k:int(v) for k,v in rawrow.items() if k != 'status'}
        if r['planned_ns'] < warmup*1e9 or (end is not None and r['planned_ns'] >= end*1e9):
            continue
        if not r['planned_ns'] <= r['submit_ns'] <= r['start_ns'] <= r['end_ns']:
            raise ValueError('invalid clock ordering')
        r['latency_ms'] = (r['end_ns']-r['submit_ns'])/1e6
        r['wait_ms'] = (r['start_ns']-r['submit_ns'])/1e6
        r['wall_ms'] = (r['end_ns']-r['start_ns'])/1e6
        r['planned_latency_ms'] = (r['end_ns']-r['planned_ns'])/1e6
        r['outstanding_estimate'] = r['pending']+r['inflight'] if r['pending'] >= 0 else -1
        rows.append(r)
    return metadata, summary, rows


def analyze(path, warmup=0., end=None):
    metadata, summary, rows = load_run(path,warmup,end)
    metrics = ['native_queue','pending','inflight','outstanding_estimate','work_estimate_us']
    result = {'path':str(path),'metadata':metadata,'overall_summary':summary,
              'analysis_window':{'planned_start_seconds':warmup,'planned_end_seconds':end},
              'selected_completed':stats(rows),
              'correlations':{m:{t:correlation(rows,m,t) for t in ['wait_ms','latency_ms','wall_ms']} for m in metrics}}
    bins = defaultdict(list)
    strata = defaultdict(list)
    seconds = defaultdict(list)
    for r in rows:
        q = r['native_queue']
        if q >= 0:
            label = '0' if q == 0 else '1-3' if q < 4 else '4-15' if q < 16 else '16-63' if q < 64 else '64+'
            bins[label].append(r)
        # Target CPU is known before execution; use narrow-ish logarithmic strata
        # to avoid calling a mix of long tasks and short tasks evidence of queue causality.
        band = int(math.floor(math.log2(max(1,r['target_cpu_us']))))
        strata[(r['pool'],band)].append(r)
        seconds[r['planned_ns']//1_000_000_000].append(r)
    result['native_queue_bins'] = {k:stats(v) for k,v in sorted(bins.items())}
    result['pool_and_target_strata'] = [
        {'pool':pool,'target_us_range':[2**band,2**(band+1)-1], **stats(rs),
         'native_queue_vs_wait':correlation(rs,'native_queue','wait_ms'),
         'native_queue_vs_latency':correlation(rs,'native_queue','latency_ms')}
        for (pool,band),rs in sorted(strata.items())]
    result['planned_second_cohorts'] = [{'second':s,**stats(rs)} for s,rs in sorted(seconds.items())]
    z = [r for r in rows if r['native_queue']==0]
    result['queue_zero'] = {'n':len(z),'wait_over_1ms':sum(r['wait_ms']>1 for r in z),
                            'wait_over_5ms':sum(r['wait_ms']>5 for r in z),
                            'max_wait_ms':max((r['wait_ms'] for r in z),default=None)}
    result['warnings'] = [
        'Native queue is Folly semaphore/queue telemetry; it is not the kernel runqueue.',
        'Submit-to-start includes add/queue/wakeup/kernel scheduling/callback-entry delay; it is not pure queue residence.',
        'Correlations are observational, not causal. Shared burst/time and service-time mixtures confound them.',
        'Low variance produces null correlation, not proof that load is unrelated to latency.',
        'p99 uses nearest rank over completed tasks; do not average per-second p99.',
        'Censored/rejected tasks remain in denominators and must accompany completed-only latency.',
        'CPU-budget mode equalizes thread CPU duration; use fixed-iteration mode for observer-cost comparisons.',
        'Pool/target strata are descriptive controls, not a randomized causal identification method.',
    ]
    if summary.get('sampling_truncated') or summary.get('sampling_failed'):
        result['warnings'].append('Periodic samples are truncated or failed; use the explicit quality flags, never assume a complete time series.')
    if summary.get('failed_tasks'):
        result['warnings'].append('Task callbacks failed; this run is diagnostic and must not enter successful-performance comparisons.')
    if summary['rejected_tasks'] or summary['canceled_tasks']:
        result['warnings'].append('This run has rejection/censoring: completed-only p99 can be biased downward.')
    return result


def fmt(x):
    return '—' if x is None else f'{x:.3f}'


def markdown(results):
    out = ['# 阶段3：独立任务实验分析','',
           '每个任务独立。arrival 是一次投递批次；task 是批次中的独立 CPU 任务。下表时延单位为 task，未混用 burst 完成时延。','',
           '|运行|部署/亲和|每池线程|arrival/s × burst|完成/拒绝/取消/失败|task 平均 ms|task p99 ms|提交至开始均值 ms|queue→等待 Pearson|',
           '|---|---|---:|---|---|---:|---:|---:|---:|']
    for r in results:
        m,s,v=r['metadata'],r['overall_summary'],r['selected_completed']
        out.append(f"|{Path(r['path']).name}|{m['deployment']}/{m['affinity']}|{m['threads_per_pool']}|{m['arrival_rate']} × {m['burst_min']}–{m['burst_max']}|{s['completed_tasks']}/{s['rejected_tasks']}/{s['canceled_tasks']}/{s.get('failed_tasks',0)}|{fmt(v['task_mean_ms'])}|{fmt(v['task_p99_ms'])}|{fmt(v['submit_to_start_mean_ms'])}|{fmt(r['correlations']['native_queue']['wait_ms']['pearson'])}|")
    out += ['', '## 如何解释', '',
            '1. 先检查拒绝、取消、到达延迟、CPU 配额和时间趋势；持续增长的等待意味着暂态过载，不能称为稳态服务能力。',
            '2. 优先比较同一随机种子、相同目标工作量的不同线程数/亲和形态；多次运行随机顺序，展示各次结果。',
            '3. 分别检查 submit→start、callback wall、task 完成时延。线程增加可能减少 Folly 等待而增加回调期间的内核等待。',
            '4. JSON 含 queue 分桶、同 pool/目标执行时间分层和按计划到达秒分组。队列相关性先看等待，再看端到端；不把随机服务时间混成队列因果效应。',
            '5. 缺少内核追踪时不把 callback wall−thread CPU 全部归因于内核 runq；也可能有缺页、阻塞或探针影响。',
            '6. 开发机两分区结果不能预测六 cluster 的性能；不同 affinity 下共享 CPU 的线程池不具备独占容量。', '',
            '## 每次运行的队列分桶', '']
    for r in results:
        out += [f"### {Path(r['path']).name}", '', '|Folly queue|任务数|等待平均 ms|task 平均 ms|task p99 ms|', '|---|---:|---:|---:|---:|']
        for k,v in r['native_queue_bins'].items():
            out.append(f"|{k}|{v['n']}|{fmt(v['submit_to_start_mean_ms'])}|{fmt(v['task_mean_ms'])}|{fmt(v['task_p99_ms'])}|")
    return '\n'.join(out)+'\n'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('runs',nargs='+',type=Path)
    p.add_argument('--output',type=Path,default=Path('independent-analysis'))
    p.add_argument('--warmup-seconds',type=float,default=0.)
    p.add_argument('--steady-end-seconds',type=float)
    args=p.parse_args()
    results=[analyze(path,args.warmup_seconds,args.steady_end_seconds) for path in args.runs]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    args.output.with_suffix('.md').write_text(markdown(results))
    print(args.output.with_suffix('.md'))

if __name__=='__main__':
    main()

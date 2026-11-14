#!/usr/bin/env python3
"""Summarize separately measured stage-3 and stage-4 datasets; no mixed task units."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
from collections import defaultdict


def quantile(values,p=.99):
    return sorted(values)[math.ceil(p*len(values))-1] if values else None


def fmt(value):
    return '—' if value is None else f'{value:.3f}'


def read(path):
    with path.open() as f:return list(csv.DictReader(f))


def stage3(root,output):
    manifest=json.loads((root/'manifest.json').read_text())
    groups=defaultdict(list); runs=[]
    for entry in manifest['runs']:
        if entry['returncode']!=0:raise ValueError('failed stage3 run')
        path=Path(entry['path']);meta=json.loads((path/'metadata.json').read_text());summary=json.loads((path/'summary.json').read_text())
        rows=read(path/'tasks.csv'); complete=[r for r in rows if r['status']=='completed']
        if not summary['conservation_ok'] or len(rows)!=summary['planned_tasks'] or len(complete)!=summary['completed_tasks']:
            raise ValueError('invalid stage3 run')
        sig=hashlib.sha256(','.join(r['target_cpu_us'] for r in rows).encode()).hexdigest()
        data={'case':entry['case'],'repetition':entry['repetition'],'seed':entry['seed'],'metadata':meta,'summary':summary,'target_sha256':sig,
              'latencies_ms':[(int(r['end_ns'])-int(r['submit_ns']))/1e6 for r in complete],
              'walls_ms':[(int(r['end_ns'])-int(r['start_ns']))/1e6 for r in complete]}
        groups[entry['case']].append(data);runs.append(data)
    # At each offered task count/repetition, changing burst/placement must not change CPU targets.
    expected={}
    for r in runs:
        key=(r['seed'],r['summary']['planned_tasks'])
        if key in expected and expected[key]!=r['target_sha256']:raise ValueError('unpaired workload budgets')
        expected[key]=r['target_sha256']
    aggregates=[]
    for case,rs in sorted(groups.items()):
        latencies=[v for r in rs for v in r.pop('latencies_ms')]
        walls=[v for r in rs for v in r.pop('walls_ms')]
        sums=[r['summary'] for r in rs]
        aggregates.append({'case':case,'metadata':rs[0]['metadata'],'repeats':len(rs),'completed':len(latencies),
            'rejected':sum(s['rejected_tasks'] for s in sums),'canceled':sum(s['canceled_tasks'] for s in sums),'failed':sum(s['failed_tasks'] for s in sums),
            'pooled_mean_ms':statistics.fmean(latencies),'pooled_p99_ms':quantile(latencies),'wall_mean_ms':statistics.fmean(walls),
            'run_mean_range_ms':[min(s['task_mean_ms'] for s in sums),max(s['task_mean_ms'] for s in sums)],
            'run_p99_range_ms':[min(s['task_p99_ms'] for s in sums),max(s['task_p99_ms'] for s in sums)],
            'run_wait_range_ms':[min(s['submit_to_start_mean_ms'] for s in sums),max(s['submit_to_start_mean_ms'] for s in sums)],
            'process_cpu_per_task_us':[(s['process_user_cpu_seconds']+s['process_system_cpu_seconds'])*1e6/s['completed_tasks'] for s in sums]})
    output.with_suffix('.json').write_text(json.dumps({'aggregates':aggregates,'runs':runs},indent=2)+'\n')
    lines=['# 阶段3实测汇总','', '任务单位为独立task；p99合并原始任务重算，范围为各独立轮次的min–max，不是置信区间。','',
           '|场景|完成task|平均ms|p99 ms|各轮平均范围ms|各轮p99范围ms|拒绝/取消/失败|', '|---|---:|---:|---:|---|---|---|']
    for g in aggregates:
        lines.append(f"|{g['case']}|{g['completed']}|{fmt(g['pooled_mean_ms'])}|{fmt(g['pooled_p99_ms'])}|{'–'.join(map(fmt,g['run_mean_range_ms']))}|{'–'.join(map(fmt,g['run_p99_range_ms']))}|{g['rejected']}/{g['canceled']}/{g['failed']}|")
    lines+=['','每种服务量/seed的目标CPU序列哈希一致性已检查。队列/服务分层见对应analysis，预测结果另用整轮留出验证；不得用这些短轮次声称稳态容量或精确微小收益。']
    output.with_suffix('.md').write_text('\n'.join(lines)+'\n')
    return aggregates


def stage4(root,output):
    manifest=json.loads((root/'manifest.json').read_text());groups=defaultdict(list);concurrency=[]
    for entry in manifest:
        if entry['returncode']!=0:raise ValueError('failed DAG run')
        path=Path(entry['directory']);summary=json.loads((path/'summary.json').read_text());meta=json.loads((path/'metadata.json').read_text())
        requests=read(path/'requests.csv')
        if meta['phase']!='completed' or not summary['conservation_ok'] or summary['io_failed'] or len(requests)!=summary['offered']:
            raise ValueError('invalid DAG run')
        lat=[float(r['latency_us'])/1000 for r in requests if r['status']=='completed']
        # Intervals from accepted request arrival to terminal completion. Rejected
        # requests are not live graphs; this diagnostic is only exact for complete runs.
        if summary['rejected']==0 and summary['censored']==0 and summary['failed']==0:
            events=[];area=0
            for r in requests:
                begin,end=int(r['offered_ns']),int(r['finish_ns'])
                events.extend([(begin,1),(end,-1)])
                area+=max(0,min(end,summary['admission_end_ns'])-begin)
            live=peak=0
            for _,change in sorted(events):live+=change;peak=max(peak,live)
            concurrency.append({'case':entry['name'],'repeat':entry['repeat'],'peak_open_requests':peak,
                                'mean_open_during_admission':area/summary['admission_end_ns']})
        groups[entry['name']].append({'latencies_ms':lat,'summary':summary,'metadata':meta})
    aggregates=[]
    for name,rs in sorted(groups.items()):
        xs=[x for r in rs for x in r['latencies_ms']]
        aggregates.append({'case':name,'repeats':len(rs),'requests':len(xs),'mean_ms':statistics.fmean(xs),'p99_ms':quantile(xs),
            'run_means_ms':[statistics.fmean(r['latencies_ms']) for r in rs],'run_p99_ms':[quantile(r['latencies_ms']) for r in rs],
            'rejected':sum(r['summary']['rejected'] for r in rs),'censored':sum(r['summary']['censored'] for r in rs),'failed':sum(r['summary']['failed'] for r in rs)})
    output.with_suffix('.json').write_text(json.dumps(aggregates,indent=2)+'\n')
    output.with_name(output.stem+'-concurrency.json').write_text(json.dumps(concurrency,indent=2)+'\n')
    lines=['# 阶段4实测汇总','', '这里的时延单位为整个DAG request，与阶段3的task不可混合。p99从每配置全部原始请求重算；仅两轮，尾部估计粗糙。','',
           '|场景|完成request|平均ms|p99 ms|各轮平均ms|拒绝/取消/失败|','|---|---:|---:|---:|---|---|']
    for g in aggregates:
        lines.append(f"|{g['case']}|{g['requests']}|{fmt(g['mean_ms'])}|{fmt(g['p99_ms'])}|{' / '.join(map(fmt,g['run_means_ms']))}|{g['rejected']}/{g['censored']}/{g['failed']}|")
    output.with_suffix('.md').write_text('\n'.join(lines)+'\n')
    return aggregates


def plot(groups,path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axs=plt.subplots(2,2,figsize=(11,7),sharex=True,layout='constrained')
    for col,aff in enumerate(['cluster','none']):
        for threads in [1,8]:
            for burst in [1,20]:
                gs=sorted([g for g in groups if g['metadata']['affinity']==aff and g['metadata']['threads_per_pool']==threads and g['metadata']['burst_min']==burst],key=lambda g:g['metadata']['arrival_rate'])
                x=[g['metadata']['arrival_rate']*burst for g in gs]
                for row,key in enumerate(['pooled_mean_ms','pooled_p99_ms']):
                    axs[row,col].plot(x,[g[key] for g in gs],marker='o' if burst==1 else 's',linestyle='-' if threads==1 else '--',label=f'{threads} threads/pool, burst {burst}')
                    axs[row,col].set_yscale('log');axs[row,col].grid(alpha=.25)
        axs[0,col].set_title('Two logical CPU partitions' if aff=='cluster' else 'Two pools, shared allowed CPUs')
        axs[1,col].set_xlabel('Offered independent tasks / second')
    axs[0,0].set_ylabel('Task mean latency (ms, log scale)');axs[1,0].set_ylabel('Task p99 latency (ms, log scale)')
    axs[0,0].legend(fontsize=8)
    fig.suptitle('Stage 3: independent random CPU tasks; 3 seconds/run, 2 repeats\nPooled task quantiles; overload cases included; not ARM NUMA evidence',fontsize=11)
    fig.savefig(path,dpi=170);plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path('results/staged'))
    p.add_argument('--no-plot',action='store_true',help='Write stdlib-only tables without the optional matplotlib figure')
    a=p.parse_args()
    output=a.root/'analysis';output.mkdir(parents=True,exist_ok=True)
    groups=stage3(a.root/'runs/independent-main',output/'independent-summary')
    stage3(a.root/'runs/independent-observer',output/'observer-summary')
    stage4(a.root/'runs/dag',output/'dag-summary')
    if (a.root/'runs/dag-overlap').exists():stage4(a.root/'runs/dag-overlap',output/'dag-overlap-summary')
    if not a.no_plot:plot(groups,a.root/'independent-load-latency.png')
    print('Stage 3/4 reports written'+('' if a.no_plot else ' with plot'))

if __name__=='__main__':main()

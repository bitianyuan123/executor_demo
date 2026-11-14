#!/usr/bin/env python3
"""Lossless-provenance extraction of stage-3 terminal evidence (not task CSVs).

Usage: python3 extract_stage3.py --logs-dir evidence --output data
Input 0005.log may be the original log or its stage-3 prefix; DAG is ignored.
Numeric precision is exactly what was printed. No missing field is imputed.
"""
import argparse
import ast
import csv
import hashlib
import json
import re
from pathlib import Path

NUM = r'-?\d+(?:\.\d+)?'


def write_csv(path, rows):
    keys = list(dict.fromkeys(k for row in rows for k in row if k != 'raw_lines'))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore', lineterminator='\n')
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k,v in row.items() if k != 'raw_lines'})


def number(v):
    return None if v == '?' else float(v)


def parse_runs(lines, source):
    rows = []
    phase, window, binary = None, None, None
    for i, line in enumerate(lines):
        m = re.match(r'=== (s3-[\w-]+) ===', line)
        if m: phase = m[1]
        if line.startswith('=== s4-'): phase = None
        m = re.match(r'MATRIX recorded=\d+ planned=\d+ window=\[([^,]+),([^\)]+)\)s binary_sha256=(\w+)', line)
        if m: window = (float(m[1]), float(m[2])); binary = m[3]
        m = re.match(r'RUN (\S+) repeat=(\d+) seed=(\d+) rc=(\d+)$', line)
        if not m or not phase: continue
        block = []
        for j in range(i + 1, len(lines)):
            if lines[j].startswith('  '): block.append((j+1, lines[j]))
            elif not lines[j].strip(): continue
            else: break
        row = dict(source_log=source, source_line=i+1, phase=phase, case=m[1], repeat=int(m[2]), seed=int(m[3]), returncode=int(m[4]), window_start_s=window[0], window_end_s=window[1], binary_sha256=binary)
        for lineno, raw in block:
            x = re.match(r'  CONFIG affinity=(\S+) threads/pool=(\d+) instrumentation=(\S+) same_union=(\S+) masks=(.*)', raw)
            if x: row.update(affinity=x[1], threads_per_pool=int(x[2]), instrumentation=x[3], same_cpu_union=x[4]=='True', masks=x[5])
            x = re.match(r'  WORK mode=(\S+) burst=(\d+)\.\.(\d+) cohort_target_mean_us=(\S+) nominal_offered_cpu_s/s=(\S+)', raw)
            if x: row.update(work_mode=x[1], burst_min=int(x[2]), burst_max=int(x[3]), cohort_target_mean_us=number(x[4]), nominal_offered_cpu_s_per_s=number(x[5]))
            x = re.match(r'  STATUS whole=(\{.*?\}) cohort=(\{.*?\}) planned=(\d+) conservation=(\S+)', raw)
            if x: row.update(whole_status=json.loads(x[1]), cohort_status=json.loads(x[2]), whole_completed=json.loads(x[1]).get('completed',0), cohort_completed=json.loads(x[2]).get('completed',0), planned_tasks=int(x[3]), conservation=x[4]=='True')
            x = re.match(r'  LAT planned_mean/p99=(\S+)/(\S+) submitted_mean/p99=(\S+)/(\S+) wait_mean/p99=(\S+)/(\S+) callback_mean=(\S+) submit_lag_p99=(\S+)',raw)
            if x:
                for key,value in zip(('planned_mean_ms','planned_p99_ms','submitted_mean_ms','submitted_p99_ms','wait_mean_ms','wait_p99_ms','callback_mean_ms','submit_lag_p99_ms'), x.groups()): row[key]=number(value)
            x = re.match(r'  TREND early->late planned_rate=(\S+)->(\S+) completion_rate=(\S+)->(\S+) cohort_latency=(\S+)->(\S+) queue_sum=(\S+)->(\S+) successful_unfinished\(start/mid/end\)=(\d+)/(\d+)/(\d+) drain_s=(\S+)',raw)
            if x:
                keys=('planned_rate_early','planned_rate_late','completion_rate_early','completion_rate_late','planned_mean_early_ms','planned_mean_late_ms','queue_sum_early','queue_sum_late','unfinished_start','unfinished_mid','unfinished_end','drain_s')
                for key,value in zip(keys,x.groups()): row[key]=number(value)
            x = re.match(r'  SAMPLING window_times=(\d+) queue_slope_tasks/s=(\S+) truncated=(\S+) failed=(\S+) dropped=(\d+)', raw)
            if x: row.update(sample_window_times=int(x[1]),queue_slope_tasks_per_s=number(x[2]),sampling_truncated=x[3]=='True',sampling_failed=x[4]=='True',samples_dropped=int(x[5]))
            x = re.match(r'  CPU whole_run_user/system_s=(\S+)/(\S+) observed_s=(\S+) callback_cpu_mean_ms=(\S+) FLAGS=(.*)',raw)
            if x: row.update(whole_user_cpu_s=number(x[1]),whole_system_cpu_s=number(x[2]),observed_s=number(x[3]),whole_callback_cpu_mean_ms=number(x[4]),flags=json.loads(x[5]))
        row['source_end_line']=block[-1][0]
        row['raw_lines']=[line]+[raw for _,raw in block]
        required=('planned_p99_ms','wait_mean_ms','cohort_completed','whole_user_cpu_s','masks')
        if any(k not in row for k in required): raise ValueError(f'incomplete RUN {source}:{i+1}')
        rows.append(row)
    return rows


def parse_tables(lines, source):
    buckets, predictions, contrasts = [], [], []
    if source == '0005.log':
        return buckets, predictions, contrasts
    phase = None
    if source == '0001.log': phase = 's3-calibration'
    if source == '0002.log': phase = 's3-burst'
    if source == '0003.log': phase = 's3-threads'
    if source == '0004.log': phase = 's3-deployment'
    run = None; predcase = None; predtarget = None
    for i,line in enumerate(lines):
        m = re.match(r'### (\S+)-r(\d+)$',line)
        if m: run=(m[1],int(m[2]))
        if source == '0003.log':
            m = re.match(r'## (local|smooth|spread) · (\w+)',line)
            if m: predcase=m[1]
            if line == '### 提交至开始等待': predtarget='wait_ms'
            if line == '### 任务提交至完成': predtarget='submitted_ms'
        if not line.startswith('|'): continue
        cells = line.strip('|').split('|')
        cells = [v.strip() for v in cells]
        base=dict(source_log=source,source_line=i+1,raw=line)
        if len(cells)==5 and cells[0] in ('0','1-3','4-15','16-63','64+') and run:
            buckets.append(dict(base,phase=phase,case=run[0],repeat=run[1],seed=420+run[1],queue_bucket=cells[0],n=int(cells[1]),wait_mean_ms=float(cells[2]),submitted_mean_ms=float(cells[3]),submitted_p99_ms=float(cells[4])))
        if len(cells)==7 and predcase and re.fullmatch(r'\d+/\d+',cells[1]):
            predictions.append(dict(base,phase='s3-burst',case=predcase,target=predtarget,model=cells[0],folds=cells[1],held_out_tasks=int(cells[2]),mae_ms=float(cells[3]),p95_absolute_error_ms=float(cells[4]),paired_baseline_mae_ms=float(cells[5]),mae_reduction_ms=float(cells[6])))
        if len(cells)==6 and cells[1]=='descriptive_only':
            contrasts.append(dict(base,phase=phase,contrast=cells[0],validity=cells[1],pairs=int(cells[2]),planned_mean_delta_ms=float(cells[3]),ci=cells[4],p99_interval=cells[5]))
    return buckets,predictions,contrasts


def parse_conditions(lines,source):
    rows=[]; group=-1; case=None
    phases=['s3-burst','s3-threads','s3-deployment']
    for i,line in enumerate(lines):
        if line.startswith('CONDITIONAL_LATENCY '): group+=1
        if group<0: continue
        m=re.match(r'CASE (\S+) config=(\w+) seeds=(\[.*?\]) affinity=(\S+) threads/pool=(\d+)',line)
        if m: case=dict(case=m[1],config=m[2],seeds=json.loads(m[3]),affinity=m[4],threads_per_pool=int(m[5]))
        m=re.match(r'  q=(\S+) runs=(\d+)/(\d+) n=(\[.*?\]) bursts=(\[.*?\]) wait=(\S+) submitted=(\S+) planned=(\S+)( LOW_TAIL)?',line)
        if not m: continue
        row=dict(source_log=source,source_line=i+1,phase=phases[group],**case,queue_bucket=m[1],runs_present=int(m[2]),runs_total=int(m[3]),n_range=json.loads(m[4]),bursts_range=json.loads(m[5]),low_tail=bool(m[9]),raw=line)
        for prefix,text in zip(('wait','submitted','planned'),(m[6],m[7],m[8])):
            for q,v in zip(('p50','p95','p99'),text.split('/')):
                endpoints=[float(x) for x in v.split('..')]
                row[f'{prefix}_{q}_min_ms']=endpoints[0];row[f'{prefix}_{q}_max_ms']=endpoints[-1]
        rows.append(row)
    return rows


def parse_metrics(lines,source):
    reads=[];updates=[];kind=None;fields=None
    for i,line in enumerate(lines):
        if line.startswith('state,threads,metric,'): kind='reads';fields=line.split(',');continue
        if line.startswith('repeat,mode,producers,'):kind='updates';fields=line.split(',');continue
        if not fields:continue
        cells=next(csv.reader([line]))
        if len(cells)!=len(fields) or not (line.startswith(('idle,','cpu_busy_with_backlog,')) or re.match(r'\d+,(shared_fetch_add|padded_fetch_add|padded_store_every_64),',line)):
            kind=None;fields=None;continue
        row=dict(source_log=source,source_line=i+1,**dict(zip(fields,cells)))
        (reads if kind=='reads' else updates).append(row)
    return reads,updates


def write_appendix(path, rows):
    lines = ['# ARM阶段3逐轮原始终端值', '',
             '本表直接整理终端RUN段，不合并seed，不从舍入值补造额外精度。时延单位ms；等待=submit→start，回调wall=start→end。planned完成包含发生器/提交迟到，submitted完成不包含。原始逐任务CSV未传回。', '',
             '共同参数、完整字段、窗口与解释见[阶段3回顾](../03-experiments.md)和[CSV](arm_runs.csv)。每个来源链接锚点为日志原行号；若查看器不支持锚点，可按显示的行号查找。', '']
    phases = ['s3-smoke','s3-calibration','s3-burst','s3-threads','s3-deployment']
    for phase in phases:
        rs = sorted((r for r in rows if r['phase']==phase), key=lambda r:(r['case'],r['seed']))
        lines += [f'## {phase}', '',
                  '|case|seed|线程/池|planned均值/p99|submitted均值/p99|等待均值/p99|回调wall均值|迟到p99|来源|',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---|']
        for r in rs:
            pairs = [f"{r[k+'_mean_ms']:.3f}/{r[k+'_p99_ms']:.3f}" for k in ('planned','submitted','wait')]
            source = f"[{r['source_log']}:{r['source_line']}]" + f"(../evidence/{r['source_log']}#L{r['source_line']})"
            lines.append(f"|{r['case']}|{r['seed']}|{r['threads_per_pool']}|{'|'.join(pairs)}|{r['callback_mean_ms']:.3f}|{r['submit_lag_p99_ms']:.3f}|{source}|")
        lines += ['', '完整计量与趋势：前/后半窗口均值、进程CPU均为终端打印口径。进程CPU是整轮含排空，不能与cohort墙钟直接相减。', '',
                  '|case/seed|cohort完成数|前/后半planned均值ms|前/后半queue和|进程user/system CPU秒|drain秒|flags|',
                  '|---|---:|---:|---:|---:|---:|---|']
        for r in rs:
            lines.append(f"|{r['case']}/{r['seed']}|{r['cohort_completed']}|{r['planned_mean_early_ms']:.3f}/{r['planned_mean_late_ms']:.3f}|{r['queue_sum_early']:.3f}/{r['queue_sum_late']:.3f}|{r['whole_user_cpu_s']:.3f}/{r['whole_system_cpu_s']:.3f}|{r['drain_s']:.3f}|{'; '.join(r['flags']) or '无启发式告警'}|")
        lines.append('')
    path.write_text('\n'.join(lines).rstrip()+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--logs-dir',type=Path,default=Path(__file__).parent/'evidence')
    p.add_argument('--output',type=Path,default=Path(__file__).parent/'data')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    tables={k:[] for k in ('runs','queue_buckets','prediction','contrasts','conditional_ranges','metric_reads','metric_updates','queue_zero')}
    sources=[]
    for name in ('0001.log','0002.log','0003.log','0004.log','0005.log'):
        path=args.logs_dir/name;raw=path.read_bytes();lines=raw.decode().splitlines()
        sources.append(dict(name=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),lines=len(lines)))
        tables['runs']+=parse_runs(lines,name)
        buckets,predictions,contrasts=parse_tables(lines,name)
        tables['queue_buckets']+=buckets;tables['prediction']+=predictions;tables['contrasts']+=contrasts
        if name=='0005.log':tables['conditional_ranges']+=parse_conditions(lines,name)
        if name=='0004.log':
            reads,updates=parse_metrics(lines,name);tables['metric_reads']+=reads;tables['metric_updates']+=updates
        for i,line in enumerate(lines):
            m=re.match(r'(\S+)-r(\d+) queue_zero: (\{.*\})$',line)
            if m:tables['queue_zero'].append(dict(source_log=name,source_line=i+1,phase='s3-burst',case=m[1],repeat=int(m[2]),seed=420+int(m[2]),**ast.literal_eval(m[3])))
    counts={k:len(v) for k,v in tables.items()}
    expected = dict(runs=41, queue_buckets=139, prediction=30, contrasts=7,
                    conditional_ranges=54, metric_reads=16, metric_updates=18, queue_zero=9)
    if counts != expected:
        raise ValueError(f'incomplete extraction: {counts}')
    keys=[(r['phase'],r['case'],r['seed']) for r in tables['runs']]
    assert len(keys)==len(set(keys))
    assert all(r['returncode']==0 and r['conservation'] and r['whole_completed']==r['planned_tasks'] for r in tables['runs'])
    # Independent terminal views must agree: bucket populations equal the RUN cohort.
    for row in tables['runs']:
        if row['phase']=='s3-smoke': continue
        bucket_n = sum(b['n'] for b in tables['queue_buckets']
                       if (b['phase'],b['case'],b['seed']) == (row['phase'],row['case'],row['seed']))
        if bucket_n != row['cohort_completed']:
            raise ValueError(f'queue bucket population mismatch: {row["phase"]}/{row["case"]}/{row["seed"]}')
    for key,rows in tables.items():write_csv(args.output/f'arm_{key}.csv',rows)
    write_appendix(args.output/'arm-runs.md',tables['runs'])
    payload={'schema':1,'provenance':'Printed terminal evidence only; no ARM raw per-task CSV supplied. Values retain printed precision. Conditional ranges are not per-seed values.','sources':sources,'counts':counts,**tables}
    (args.output/'arm_stage3_evidence.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(counts,ensure_ascii=False))

if __name__=='__main__':main()

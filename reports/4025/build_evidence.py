#!/usr/bin/env python3
"""Extract labeled 4025 terminal tables; do not reconstruct absent series or N(t)."""
import argparse
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
EXPECTED = '21cf4d7d8a9c5fffec298b4a96946fb67c4dab725cbcdd92a38ea7c99722c6ff'


def build(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError('unexpected 4025 source')
    pools = {}
    for number, line in enumerate(raw.decode().splitlines(), 1):
        if line.startswith('1.0 pool_'):
            m = re.fullmatch(r'1\.0 (pool_\w) mu ([\d.]+) busy10% ([\d.]+) \| m2: hiT% ([\d.]+) hiA% ([\d.]+) non ([\d./]+) nonA% ([\d.]+) ivl ([\d./]+) c(\d+)', line)
            if not m: raise ValueError('bad background row')
            pools[m[1]] = dict(background_source_line=number, mean_cpu_equiv=float(m[2]),
                top10_service_percent=float(m[3]), high_time_percent=float(m[4]), high_area_percent=float(m[5]),
                nonhigh_cpu_equiv=dict(zip(('mean','p50','p90','p99'),map(float,m[6].split('/')))),
                nonhigh_area_percent=float(m[7]), nonhigh_interval_ms=list(map(float,m[8].split('/'))),
                nonhigh_censored_segments=int(m[9]))
        elif line.startswith('1.0 m2 pool_') and 'nComp=' in line:
            name = line.split(':',1)[0].split()[-1]
            p = pools[name]
            m = re.search(r'nComp=(\d+) nCens=(\d+) censArea=([\d.]+)',line)
            p.update(segments_source_line=number, complete_segments=int(m[1]), censored_segments=int(m[2]), censored_area_cpu_ms=float(m[3]))
            values = re.findall(r'\(([^]]+)\]: n=(\d+) dur=([\d.]+) area=([\d.]+) pk=([\d.]+)/([\d.]+)',line)
            if len(values)!=4: raise ValueError('bad duration groups')
            p['groups']=[dict(duration_bucket_ms=v[0], count=int(v[1]), duration_ms=float(v[2]),
                              area_cpu_ms=float(v[3]), peak_p50=float(v[4]), peak_p95=float(v[5])) for v in values]
        elif re.match(r'^1.0 m2 pool_\w: \d+ \|',line):
            name=line.split(':',1)[0].split()[-1]
            c=line.split('|')
            pools[name]['gaps']=dict(source_line=number, pairs=int(c[0].split()[-1]),
                gap_ms=list(map(float,c[1].strip().split('/'))), start_gap_ms=list(map(float,c[2].strip().split('/'))),
                at_most={m[0]:[int(m[1]),int(m[2])] for m in re.findall(r'(\d+)ms:(\d+)/(\d+)',c[3])})
        elif re.match(r'^pool_\w (high|nonHigh) ',line):
            name,state,count=line.split('|')[0].split()
            c=[v.strip() for v in line.split('|')]
            # The extra triple has no column label in the returned header. Retain
            # raw text only: do not interpret it as waiting or runnable evidence.
            pools[name].setdefault('concurrent',{})[state]=dict(source_line=number,n_bins=int(count),
                other_service_cpu_equiv=list(map(float,c[1].split('/'))),
                unlabeled_triple=c[2], other_high_counts=list(map(int,c[3].split('/'))),
                source_service_cpu_equiv=list(map(float,c[4].split('/'))))
    if len(pools)!=4: raise ValueError('missing pools')
    for p in pools.values():
        if sum(g['count'] for g in p['groups']) != p['complete_segments']: raise ValueError('segment count mismatch')
        if abs(sum(g['duration_ms'] for g in p['groups']) - p['high_time_percent']*100)>1e-7: raise ValueError('duration mismatch')
        # mu and area percentages were rounded independently in the source.
        reconstructed=sum(g['area_cpu_ms'] for g in p['groups'])
        expected=p['mean_cpu_equiv']*10000*p['high_area_percent']/100
        if abs(reconstructed-expected)>4: raise ValueError('area beyond printed rounding tolerance')
        if abs(p['high_area_percent']+p['nonhigh_area_percent']-100)>1e-8: raise ValueError('area complement mismatch')
        if p['gaps']['pairs']!=p['complete_segments']-1: raise ValueError('gap denominator')
        for row in p['concurrent'].values():
            if sum(row['other_high_counts'])!=row['n_bins']: raise ValueError('concurrent count mismatch')
    groups=[]
    for i,label in enumerate(('(0,2]','(2,5]','(5,20]','(20,infinity)')):
        row=dict(duration_bucket_ms=label)
        for field in ('count','duration_ms','area_cpu_ms'):
            row[field]=sum(p['groups'][i][field] for p in pools.values())
        groups.append(row)
    for row in groups:
        for field in ('count','duration_ms','area_cpu_ms'):
            row[field+'_percent']=100*row[field]/sum(g[field] for g in groups)
    multiplicities=[]
    for i in range(4):
        count=sum(p['concurrent']['high']['other_high_counts'][i] for p in pools.values())
        if count%(i+1): raise ValueError('high multiplicity mismatch')
        multiplicities.append(count//(i+1))
    return dict(schema='review-4025-v1', source_log_sha256=EXPECTED, source_lines=len(raw.decode().splitlines()),
        input_series_reported_sha256='2a9a9adc00250cd49b3e2be05990ac92515e62c8b02eb10995340ff6dc3cc04d',
        window_ms=10000, bin_ms=1, coverage='unknown', mapping='comm-name inference, not verified',
        definition='high-service S > 2 * own full-window mean; S is matched CPU residence / bin width, not O, Q, runnable N or overload',
        pools=pools, pooled_duration_groups=groups,
        common_bins_by_number_high_pools={str(i+1):n for i,n in enumerate(multiplicities)},
        limits=['Only terminal summaries available locally; no raw series or parser implementation revalidated.',
            'Durations/areas are pooled across historical pool-segments; not request lifetimes, not independent samples.',
            'S19 unlabeled triple excluded. S17 v/side/boundary denominators ambiguous; no statistics derived from them.',
            'Reported complete segment censoring is not evidence of complete trace coverage.',
            'No online residual-duration prediction, full N(t), request p99, idle destination or calibrated pressure threshold.'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-log',type=Path,required=True)
    data=build(parser.parse_args().source_log)
    (HERE/'evidence.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print('PASS labeled counts, duration/area rounding, complements and simultaneous-bin conservation; 1115 segments, no new raw scan.')
    for row in data['pooled_duration_groups']: print(json.dumps(row))
    print(data['common_bins_by_number_high_pools'])

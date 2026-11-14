#!/usr/bin/env python3
"""Sequential randomized stage-3 experiments; no cross-pool routing or DAG flags."""
import argparse
import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path


def cpulist(cpus):
    return ','.join(map(str,cpus))


def generate(preset, cpus=None, numa=None):
    allowed=sorted(os.sched_getaffinity(0))
    if preset=='target':
        if not cpus or not numa or len(cpus.split(';'))!=6 or len(numa.split(','))!=6:
            raise ValueError('target requires six explicit --cpus groups and --numa labels')
        base={'deployment':'target','pools':6,'cpus':cpus,'numa':numa,'duration':120,'cpu-us':100000,
              'jitter':.5,'long-prob':.02,'long-factor':8,'instrumentation':'full','sample-ms':100,
              'max-outstanding':100000,'max-planned-tasks':2000000,'drain-seconds':120}
        # Arrival rate is deliberately not inferred from 96 logical CPUs: calibrate sustainable throughput first.
        threads=[8,16,64,256]
        cases=[]
        for affinity in ['none','numa','cluster']:
            for t in threads:
                for label,rate,burst in [('smooth-low',120,1),('smooth-mid',300,1),('smooth-high',600,1),
                                         ('burst20',12,20),('burst100',12,100),('burst400',12,400)]:
                    cases.append({'name':f'{affinity}-t{t}-{label}',
                                  'options':{'affinity':affinity,'threads':t,'arrival-rate':rate,
                                             'burst-min':burst,'burst-max':burst}})
        return {'base':base,'repeats':5,'seed':420,'cases':cases,
                'note':'Review capacity/quotas first; 100/400 bursts are deliberate overload probes, not defaults to run blindly. Use --only.'}
    groups=[allowed[:max(1,len(allowed)//2)],allowed[max(1,len(allowed)//2):]]
    groups=[g for g in groups if g]
    base={'deployment':'dev','pools':len(groups),'cpus':';'.join(cpulist(g) for g in groups),
          'affinity':'cluster','duration':4,'cpu-us':1000,'jitter':.5,'long-prob':.02,'long-factor':8,
          'instrumentation':'full','sample-ms':50,'max-outstanding':10000,'drain-seconds':20}
    cases=[]
    if preset=='dev-quick':
        for threads in [1,8]:
            for label,rate,burst in [('smooth',200,1),('burst',10,20)]:
                cases.append({'name':f't{threads}-{label}','options':{'threads':threads,'arrival-rate':rate,'burst-min':burst,'burst-max':burst}})
        return {'base':base,'repeats':1,'seed':420,'cases':cases}
    for affinity in ['none','cluster']:
        for threads in [1,2,8,32]:
            for label,rate,burst in [('smooth-low',200,1),('smooth-mid',1200,1),('smooth-high',2400,1),
                                     ('burst20-low',10,20),('burst20-mid',60,20),('burst20-high',120,20)]:
                cases.append({'name':f'{affinity}-t{threads}-{label}','options':{'affinity':affinity,'threads':threads,
                             'arrival-rate':rate,'burst-min':burst,'burst-max':burst}})
    return {'base':base,'repeats':3,'seed':420,'cases':cases}


def execute(args):
    spec=json.loads(args.config.read_text()) if args.config else generate(args.preset,args.cpus,args.numa)
    if args.write_config:
        args.write_config.parent.mkdir(parents=True,exist_ok=True)
        args.write_config.write_text(json.dumps(spec,indent=2)+'\n')
        print(args.write_config)
        return
    if args.output.exists():
        raise ValueError('matrix output already exists')
    args.output.mkdir(parents=True)
    (args.output/'matrix.json').write_text(json.dumps(spec,indent=2)+'\n')
    cases=spec['cases']
    if args.only:
        wanted=set(args.only.split(','));cases=[c for c in cases if c['name'] in wanted]
        missing=wanted-{c['name'] for c in cases}
        if missing:raise ValueError(f'unknown case names: {sorted(missing)}')
    if not cases:raise ValueError('empty case selection')
    names=[c['name'] for c in cases]
    if len(set(names))!=len(names) or any(not n or '/' in n or n in {'.','..'} for n in names):
        raise ValueError('case names must be distinct single directory names')
    repeats=args.repeats if args.repeats is not None else spec.get('repeats',1)
    if repeats<1:raise ValueError('repeats must be positive')
    seed=int(spec.get('seed',420));rng=random.Random(seed)
    schedule=[(r,c) for r in range(repeats) for c in cases]
    rng.shuffle(schedule)
    binary=args.binary.resolve()
    manifest={'binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
              'matrix_seed':seed,'runs':[]}
    for repetition,case in schedule:
        options=dict(spec.get('base',{}));options.update(case.get('options',{}))
        options['seed']=seed+repetition
        options.update(dict(v.split('=',1) for v in args.override))
        run=args.output/f"{case['name']}-r{repetition}"
        command=[str(binary),'--output',str(run)]
        for key,value in options.items():command.extend(['--'+key,str(value)])
        timeout=float(options.get('duration',5))+float(options.get('drain-seconds',30))+70
        begin=time.time()
        try:
            with (args.output/f'{run.name}.log').open('w') as log:
                process=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
            code=process.returncode
        except subprocess.TimeoutExpired:
            code='timeout'
        entry={'case':case['name'],'repetition':repetition,'seed':options['seed'],'path':str(run),
               'command':command,'returncode':code,'wall_seconds':time.time()-begin}
        if code==0 and (run/'summary.json').exists():entry['summary']=json.loads((run/'summary.json').read_text())
        manifest['runs'].append(entry)
        (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print(f'{run.name}: {code}',flush=True)
        if code!=0 and not args.keep_going:raise RuntimeError('run failed; see log and manifest')
    good=[r['path'] for r in manifest['runs'] if r['returncode']==0 and 'summary' in r]
    if good:
        subprocess.run([os.sys.executable,str(Path(__file__).with_name('analyze.py')),*good,
                        '--output',str(args.output/'analysis')],check=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary',type=Path,default=Path('build-independent/independent_load'))
    p.add_argument('--config',type=Path)
    p.add_argument('--preset',choices=['dev-quick','dev-sweep','target'],default='dev-quick')
    p.add_argument('--cpus');p.add_argument('--numa')
    p.add_argument('--output',type=Path,default=Path('results/runs/independent'))
    p.add_argument('--write-config',type=Path)
    p.add_argument('--only',help='comma-separated exact case names')
    p.add_argument('--repeats',type=int)
    p.add_argument('--override',action='append',default=[],metavar='KEY=VALUE')
    p.add_argument('--keep-going',action='store_true')
    execute(p.parse_args())

if __name__=='__main__':main()

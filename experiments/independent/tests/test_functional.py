#!/usr/bin/env python3
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY=Path(sys.argv.pop(1)).resolve()

class Functional(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.index=0
    def tearDown(self):
        self.temp.cleanup()
    def run_case(self,expected_returncode=0,**options):
        self.index+=1
        output=self.root/str(self.index)
        args={'output':str(output),'pools':1,'threads':2,'arrival-rate':1000,'duration':.03,
              'arrivals':12,'cpu-us':100,'burst-min':2,'burst-max':4,'sample-ms':2,'drain-seconds':1}
        args.update(options)
        result=subprocess.run([str(BINARY),*[x for k,v in args.items() for x in ['--'+k,str(v)]]],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,expected_returncode,result.stderr)
        metadata=json.loads((output/'metadata.json').read_text())
        summary=json.loads((output/'summary.json').read_text())
        with (output/'tasks.csv').open() as f:rows=list(csv.DictReader(f))
        with (output/'bursts.csv').open() as f:bursts=list(csv.DictReader(f))
        self.assertTrue(summary['conservation_ok'])
        self.assertEqual(len(rows),summary['planned_tasks'])
        self.assertEqual(len(rows),sum(summary[k] for k in ['completed_tasks','rejected_tasks','canceled_tasks','failed_tasks']))
        for r in rows:
            if r['status']=='completed':
                p,s,a,e=(int(r[k]) for k in ['planned_ns','submit_ns','start_ns','end_ns'])
                self.assertLessEqual(p,s);self.assertLessEqual(s,a);self.assertLessEqual(a,e)
        for b in bursts:
            self.assertEqual(int(b['tasks']),sum(int(b[k]) for k in ['completed','rejected','canceled','failed']))
        return metadata,summary,rows,output
    def test_real_folly_independent_jobs(self):
        m,s,rs,out=self.run_case()
        self.assertEqual(m['executor'],'original folly::CPUThreadPoolExecutor')
        self.assertEqual(s['planned_tasks'],s['completed_tasks'])
        self.assertEqual(m['planned_arrivals'],12)
        self.assertTrue(all(int(r['thread_cpu_ns'])>=int(r['target_cpu_us'])*1000 for r in rs))
        self.assertTrue(all(int(r['worker_tid'])>0 for r in rs))
        self.assertGreater(len((out/'pools.csv').read_text().splitlines()),1)
    def test_instrumentation_levels_and_seed(self):
        targets=[]
        for mode in ['none','queue','counters','full']:
            m,s,rs,out=self.run_case(instrumentation=mode,seed=123)
            targets.append([(r['burst'],r['target_cpu_us']) for r in rs])
            self.assertTrue(all((int(r['native_queue'])>=0)==(mode!='none') for r in rs))
            self.assertTrue(all((int(r['pending'])>=0)==(mode in ['counters','full']) for r in rs))
            self.assertTrue(all((int(r['work_estimate_us'])>=0)==(mode=='full') for r in rs))
            self.assertTrue(all((int(r['thread_cpu_ns'])>=0)==(mode=='full') for r in rs))
        self.assertTrue(all(t==targets[0] for t in targets))
    def test_affinity_and_round_robin(self):
        cpus=sorted(os.sched_getaffinity(0))
        n=min(len(cpus),2)
        masks=';'.join(str(c) for c in cpus[:n])
        m,s,rs,out=self.run_case(pools=n,cpus=masks,affinity='cluster')
        self.assertEqual(m['effective_masks'],[[c] for c in cpus[:n]])
        self.assertEqual(m['same_cpu_union'],n==len(cpus))
        self.assertEqual(m['extra_allowed_cpus'],cpus[n:])
        self.assertTrue(all(int(r['pool'])==int(r['burst'])%n for r in rs))
        with (out/'workers.csv').open() as f:workers=list(csv.DictReader(f))
        self.assertEqual(len(workers),n*2)
    def test_rejection_not_success(self):
        m,s,rs,_=self.run_case(**{'threads':1,'arrivals':1,'burst-min':20,'burst-max':20,'cpu-us':20000,'max-outstanding':1})
        self.assertGreater(s['rejected_tasks'],0)
        self.assertEqual(s['complete_bursts'],0)
        self.assertIsNone(s['burst_p99_ms'])
    def test_cutoff_and_cancel(self):
        m,s,rs,_=self.run_case(**{'duration':.002,'drain-seconds':.002,'arrivals':1,'cpu-us':1000000,'threads':1,
                                 'burst-min':10,'burst-max':10})
        self.assertGreater(s['canceled_tasks'],0)
        self.assertEqual(s['completed_tasks'],0)
        self.assertIsNone(s['task_p99_ms'])
    def test_poisson_fixed_iterations(self):
        a=self.run_case(**{'arrival':'poisson','duration':.05,'work-mode':'iterations','iterations-per-us':1,'seed':33})
        b=self.run_case(**{'arrival':'poisson','duration':.05,'work-mode':'iterations','iterations-per-us':1,'seed':33})
        self.assertEqual([(r['planned_ns'],r['target_cpu_us']) for r in a[2]],[(r['planned_ns'],r['target_cpu_us']) for r in b[2]])
    def test_failure_cleanup_and_sampling_limit(self):
        m,s,rs,out=self.run_case(expected_returncode=2,**{'fail-task':0,'max-samples':1})
        self.assertEqual(s['failed_tasks'],1)
        self.assertTrue(s['conservation_ok'])
        self.assertTrue(s['sampling_truncated'])
        self.assertEqual(s['sample_rows'],1)
        self.assertGreater(s['sample_rows_dropped'],0)
        self.assertEqual(rs[0]['status'],'failed')
    def test_same_task_work_across_burst_widths(self):
        a=self.run_case(**{'arrivals':20,'burst-min':1,'burst-max':1})
        b=self.run_case(**{'arrivals':2,'burst-min':10,'burst-max':10})
        self.assertEqual([r['target_cpu_us'] for r in a[2]],[r['target_cpu_us'] for r in b[2]])
    def test_exact_mean_distribution_and_estimate_controls(self):
        for jitter,prob,factor in [(0,0,1),(.5,0,1),(.5,.2,8)]:
            m,s,rows,_=self.run_case(**{'mean-normalization':'exact','cpu-us':137,'arrivals':10,
                'burst-min':5,'burst-max':5,'jitter':jitter,'long-prob':prob,'long-factor':factor,
                'work-mode':'iterations','iterations-per-us':1})
            self.assertEqual(sum(int(r['target_cpu_us']) for r in rows),len(rows)*137)
            self.assertTrue(all(int(r['target_cpu_us'])>=1 for r in rows))
            self.assertEqual(m['mean_normalization'],'exact')
            self.assertTrue(all(r['estimate_us']==r['target_cpu_us'] for r in rows))
        _,_,rows,_=self.run_case(**{'mean-normalization':'exact','cpu-us':1,'estimate-us':9,
                                    'work-mode':'iterations','iterations-per-us':1})
        self.assertTrue(all(r['target_cpu_us']=='1' and r['estimate_us']=='9' for r in rows))
    def test_spread_is_only_pool_assignment_change(self):
        args={'pools':2,'arrivals':5,'burst-min':4,'burst-max':4,'mean-normalization':'exact'}
        local=self.run_case(**args)[2]
        m,_,spread,_=self.run_case(**args,**{'burst-placement':'spread'})
        self.assertEqual(m['burst_placement'],'spread')
        fields=['task','burst','planned_ns','target_cpu_us','estimate_us']
        self.assertEqual([[r[k] for k in fields] for r in local],[[r[k] for k in fields] for r in spread])
        self.assertTrue(all(int(r['pool'])==int(r['task'])%2 for r in spread))
        self.assertTrue(all(int(r['pool'])==int(r['burst'])%2 for r in local))
    def test_estimation_noise_does_not_change_workload(self):
        a=self.run_case(seed=56)[2]
        b=self.run_case(seed=56,**{'estimate-jitter':.75})[2]
        c=self.run_case(seed=56,**{'estimate-jitter':.75})[2]
        keys=['task','pool','planned_ns','target_cpu_us']
        self.assertEqual([[r[k] for k in keys] for r in a],[[r[k] for k in keys] for r in b])
        self.assertEqual([r['estimate_us'] for r in b],[r['estimate_us'] for r in c])
        self.assertNotEqual([r['estimate_us'] for r in a],[r['estimate_us'] for r in b])
        self.assertTrue(all(.25*int(r['target_cpu_us'])-1<=int(r['estimate_us'])<=1.75*int(r['target_cpu_us']) for r in b))
    def test_invalid_inputs_and_existing_output(self):
        bad=[['--pools','0'],['--affinity','cluster'],['--deployment','target'],['--instrumentation','wat'],
             ['--jitter','1'],['--cpu-us','-1'],['--max-planned-tasks','1','--burst-min','2','--burst-max','2'],
             ['--cpus','9999'],['--pools','2','--cpus','0;0'],['--numa','999999'],['--arrival-rate','nan'],
             ['--mean-normalization','bad'],['--burst-placement','bad'],['--estimate-jitter','1']]
        for opts in bad:
            r=subprocess.run([str(BINARY),*opts,'--output',str(self.root/'bad')],capture_output=True,text=True,timeout=5)
            self.assertNotEqual(r.returncode,0,opts)
        self.run_case()
        r=subprocess.run([str(BINARY),'--output',str(self.root/'1')],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(r.returncode,0)

if __name__=='__main__':unittest.main()

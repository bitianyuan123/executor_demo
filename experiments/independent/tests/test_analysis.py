#!/usr/bin/env python3
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('analyze',Path(__file__).resolve().parents[1]/'scripts/analyze.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
spec2=importlib.util.spec_from_file_location('runner',Path(__file__).resolve().parents[1]/'scripts/run_matrix.py')
r=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(r)
spec3=importlib.util.spec_from_file_location('design',Path(__file__).resolve().parents[3]/'stages/03-independent/make_cases.py')
d=importlib.util.module_from_spec(spec3);spec3.loader.exec_module(d)

class Analysis(unittest.TestCase):
    def test_quantile_and_empty(self):
        self.assertIsNone(a.quantile([]));self.assertEqual(a.quantile(list(range(100))),98)
        self.assertEqual(a.quantile([5]),5)
    def test_correlations_and_ties(self):
        self.assertIsNone(a.pearson([1,1,1],[2,3,4]))
        self.assertEqual(a.ranks([2,1,1,3]),[3,1.5,1.5,4])
        self.assertAlmostEqual(a.pearson([1,2,3],[2,4,6]),1)
        self.assertAlmostEqual(a.pearson([1,2,3],[6,4,2]),-1)
    def test_incomplete_run_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'metadata.json').write_text(json.dumps({'stage':3,'planned_tasks':2}))
            (p/'summary.json').write_text(json.dumps({'conservation_ok':True,'planned_tasks':2}))
            (p/'tasks.csv').write_text('status\ncompleted\n')
            with self.assertRaisesRegex(ValueError,'truncated'):a.load_run(p)
    def test_dev_config_is_independent(self):
        m=r.generate('dev-quick')
        self.assertEqual(len(m['cases']),4)
        self.assertNotIn('policy',m['base'])
        self.assertEqual(m['cases'][0]['options']['arrival-rate']*m['cases'][0]['options']['burst-max'],
                         m['cases'][1]['options']['arrival-rate']*m['cases'][1]['options']['burst-max'])
    def test_target_requires_explicit_topology(self):
        with self.assertRaises(ValueError):r.generate('target')
        m=r.generate('target','0;1;2;3;4;5','0,0,0,1,1,1')
        self.assertEqual(m['base']['pools'],6)
        self.assertEqual({c['options']['affinity'] for c in m['cases']},{'none','numa','cluster'})
        self.assertEqual({c['options']['threads'] for c in m['cases']},{8,16,64,256})
    def design(self,family,*args):
        return d.generate(d.parser().parse_args(['--family',family,'--output','unused.json',*args]))
    def test_equal_demand_burst_factorial(self):
        m=self.design('burst','--task-rate','1600')
        self.assertEqual(len(m['cases']),7)
        for case in m['cases']:
            o=case['options']
            self.assertEqual(o['arrival-rate']*o['burst-max'],1600)
        self.assertEqual({c['options']['burst-placement'] for c in m['cases']},{'local','spread'})
        self.assertEqual(m['base']['mean-normalization'],'exact')
    def test_distribution_same_mean_and_qps_growth_separate(self):
        m=self.design('distribution')
        self.assertEqual(len(m['cases']),3)
        self.assertEqual({c['options']['arrival-rate'] for c in m['cases']},{60})
        q=self.design('qps12')
        self.assertEqual([c['options']['arrival-rate'] for c in q['cases']],[12]*3)
        self.assertEqual([c['options']['burst-max'] for c in q['cases']],[20,100,400])
        with self.assertRaisesRegex(ValueError,'fixes arrivals'):self.design('qps12','--task-rate','1')
    def test_target_never_invents_measured_capacity(self):
        topo=['--deployment','target','--cpus','0;1;2;3;4;5','--numa','0,0,0,1,1,1']
        with self.assertRaisesRegex(ValueError,'explicit --task-rate'):self.design('burst',*topo)
        with self.assertRaisesRegex(ValueError,'explicit --rates'):self.design('calibration',*topo)
        m=self.design('deployment',*topo,'--capacity-task-rate','100','--load-fractions','.5')
        self.assertEqual(len(m['cases']),12)
        self.assertEqual(m['analysis']['warmup_seconds'],30)
        self.assertEqual(m['repeats'],7)
        self.assertEqual({c['sample_plan']['reference_load_fraction'] for c in m['cases']},{.5})
    def test_scale_equal_cpu_demand_and_estimator_real_noise(self):
        m=self.design('scale','--task-rate','1600')
        self.assertEqual({c['options']['arrival-rate']*c['options']['cpu-us'] for c in m['cases']},{1600000})
        e=self.design('estimator')
        self.assertEqual([c['options']['estimate-jitter'] for c in e['cases']],[0,0,.75,.75])
    def test_design_tier_and_sampling_warnings(self):
        m=self.design('burst')
        self.assertEqual(m['tier'],'development-exploratory')
        self.assertTrue(any(c['sample_plan']['few_arrival_warning'] for c in m['cases']))
        for family in d.FAMILIES:
            plan=self.design(family)
            self.assertTrue(plan['cases'])
            self.assertEqual(len({c['name'] for c in plan['cases']}),len(plan['cases']))
    def test_randomized_repetition_blocks(self):
        cases=[{'name':str(i)} for i in range(8)]
        a=r.make_schedule(cases,3,47)
        self.assertEqual(a,r.make_schedule(cases,3,47))
        self.assertEqual([rep for rep,case in a],[rep for rep in range(3) for _ in cases])
        for start in range(0,len(a),8):
            self.assertEqual({case['name'] for _,case in a[start:start+8]},{str(i) for i in range(8)})
        self.assertNotEqual([c['name'] for _,c in a[:8]],[c['name'] for _,c in a[8:16]])

if __name__=='__main__':unittest.main()

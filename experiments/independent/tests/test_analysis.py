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

if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import sys
import unittest

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
import request_sched_profile as profile


def event(at, prev, nxt, cpu=0):
    return f' [{cpu:03d}] {at/1e9:.9f}: sched:sched_switch: prev_comm=worker prev_pid={prev} prev_prio=120 prev_state=S ==> next_comm=worker next_pid={nxt} next_prio=120\n'


def task(tid=42, start=1_000_000, finish=3_000_000, cpu=1_000_000, chosen=0, tag=1):
    return dict(worker_tid=tid,submitted_ns=start,start_ns=start,finish_ns=finish,cpu_ns=cpu,chosen=chosen,tag=tag,kind='operator')


class ProfileTests(unittest.TestCase):
    def test_preempted_callback_not_smeared(self):
        spans,bounds,audit=profile.intervals([
            event(0,0,0),event(1_000_000,0,42),event(1_400_000,42,0),
            event(2_400_000,0,42),event(3_000_000,42,0),event(4_000_000,0,0)],{0},{42})
        rows,out=profile.integrate([task()],1_000_000,3_000_000,[{'cpus':[0]}],spans,bounds,1_000_000)
        self.assertEqual([r['request_scheduled_cpu_ms'] for r in rows if r['pool'] != 'all'],[.4,.6])
        self.assertEqual(out['scheduled_callback_residence_ms'],1)
        self.assertEqual(out['service_cumulative_percent_window_end_ms']['50'],2)

    def test_cpu_migration_and_request_identity(self):
        lines=[event(0,0,0),event(0,0,0,1),event(1_000_000,0,42),
               event(2_000_000,42,0),event(2_000_000,0,42,1),event(3_000_000,42,0,1),
               event(4_000_000,0,0),event(4_000_000,0,0,1)]
        spans,bounds,_=profile.intervals(lines,{0,1},{42})
        rows,out=profile.integrate([task(cpu=2_000_000)],1_000_000,3_000_000,[{'cpus':[0,1]}],spans,bounds,1_000_000)
        self.assertEqual(out['scheduled_callback_residence_ms'],2)
        self.assertEqual([r['request_cluster_util_percent'] for r in rows if r['pool'] != 'all'],[50,50])

    def test_coverage_never_fills_unseen_prefix(self):
        spans,bounds,_=profile.intervals([event(1_500_000,42,0),event(4_000_000,0,0)],{0},{42})
        with self.assertRaisesRegex(ValueError,'bracketed'):
            profile.integrate([task()],1_000_000,3_000_000,[{'cpus':[0]}],spans,bounds,1_000_000)

    def test_lost_and_broken_chains_rejected(self):
        for lines in [[event(0,0,0),'PERF_RECORD_LOST: 10'],[event(0,0,42),event(1_000_000,7,0)]]:
            with self.assertRaises(ValueError):profile.intervals(lines,{0},{42})

    def test_per_cpu_backward_clock_rejected(self):
        with self.assertRaisesRegex(ValueError,'backwards'):
            profile.intervals([event(2_000_000,0,0),event(1_000_000,0,0)],{0},{42})

    def test_unclosed_final_interval_not_extended(self):
        spans,bounds,_=profile.intervals([event(0,0,42),event(1_000_000,42,42)],{0},{42})
        self.assertEqual(spans[42],[(0,1_000_000,0)])
        self.assertEqual(bounds[0],[0,1_000_000])

    def test_overlapping_tid_residence_rejected(self):
        with self.assertRaisesRegex(ValueError,'overlapping CPU'):
            profile.intervals([event(0,0,42),event(0,0,42,1),event(2_000_000,42,0),event(2_000_000,42,0,1)],{0,1},{42})

    def test_unexpected_affinity_rejected(self):
        with self.assertRaisesRegex(ValueError,'outside chosen'):
            profile.integrate([task()],1_000_000,3_000_000,[{'cpus':[0]},{'cpus':[1]}],
                              {42:[(1_000_000,3_000_000,1)]},{0:[0,4_000_000],1:[0,4_000_000]},1_000_000)

    def test_final_callback_tail_and_partial_bin(self):
        rows,out=profile.integrate([task(finish=3_500_000,cpu=2_500_000)],1_000_000,3_000_000,[{'cpus':[0]}],
            {42:[(1_000_000,3_500_000,0)]},{0:[0,4_000_000]},1_000_000)
        self.assertEqual(out['last_callback_tail_ms'],.5)
        self.assertEqual(rows[-1]['relative_end_ms'],2.5)
        self.assertEqual(rows[-1]['request_cluster_util_percent'],100)

    def test_total_cluster_busy_kept_separate_from_request(self):
        busy=[]
        spans,bounds,_=profile.intervals([event(0,0,0),event(1_000_000,0,42),
            event(2_000_000,42,99),event(3_000_000,99,0),event(4_000_000,0,0)],
            {0},{42},busy,(1_000_000,3_000_000))
        rows,out=profile.integrate([task(finish=2_000_000)],1_000_000,3_000_000,
            [{'cpus':[0]}],spans,bounds,1_000_000,busy)
        self.assertEqual(rows[2]['request_cluster_util_percent'],0)
        self.assertEqual(rows[2]['cluster_nonidle_residence_percent'],100)

    def test_positive_cpu_without_tid_intervals_rejected(self):
        with self.assertRaisesRegex(ValueError,'no matching scheduled'):
            profile.integrate([task()],1_000_000,3_000_000,[{'cpus':[0]}],{},
                              {0:[0,4_000_000]},1_000_000)

    def test_no_format_guess(self):
        with self.assertRaisesRegex(ValueError,'nine decimal'):
            profile.intervals(['[000] 1.123456: sched:sched_switch: test'],{0},{42})


if __name__=='__main__':unittest.main()

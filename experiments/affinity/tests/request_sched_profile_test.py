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

    def test_compact_openeuler_perf_fixture(self):
        lines = [
            '[000] 128173.903316950: sched:sched_switch: perf:824058 [120] D ==> migration/0:17 [0]\n',
            '[000] 128173.903326950: sched:sched_switch: migration/0:17 [0] S ==> swapper/0:0 [120]\n',
            '[001] 128173.903336950: sched:sched_switch: perf:824058 [120] D ==> migration/1:20 [0]\n',
            '[001] 128173.903346950: sched:sched_switch: migration/1:20 [0] S ==> swapper/1:0 [120]\n',
        ]
        spans, bounds, audit = profile.intervals(lines, {0, 1}, {17, 20})
        self.assertEqual(spans[17], [(128173903316950, 128173903326950, 0)])
        self.assertEqual(spans[20], [(128173903336950, 128173903346950, 1)])
        self.assertEqual(audit['payload_format_compact'], 4)
        self.assertEqual(audit.get('payload_format_named', 0), 0)

    def test_names_with_spaces_and_colons_do_not_become_pids(self):
        for payload, name in [
            ('worker 7:phase:42 [120] R+ ==> other:8 pool:99 [0]', 'compact'),
            ('worker 7:phase:42[120] R+ ==> other:8 pool:99[0]', 'compact'),
            ('prev_comm=worker 7:phase prev_pid=42 prev_prio=120 prev_state=R+ '
             '==> next_comm=other:8 pool next_pid=99 next_prio=0', 'named'),
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(profile.switch_payload(payload, 1), (42, 99, name))

    def test_mixed_payload_formats_preserve_chain_and_count(self):
        lines = [event(0, 0, 42),
                 '[000] 0.001000000: sched:sched_switch: worker:42 [120] S ==> idle:0 [120]\n',
                 event(2_000_000, 0, 0)]
        spans, _, audit = profile.intervals(lines, {0}, {42})
        self.assertEqual(spans[42], [(0, 1_000_000, 0)])
        self.assertEqual(audit['payload_format_named'], 2)
        self.assertEqual(audit['payload_format_compact'], 1)

    def test_compact_format_retains_chain_loss_and_clock_guards(self):
        first = '[000] 1.000000000: sched:sched_switch: idle:0 [120] R ==> worker:42 [120]\n'
        for second, expected in [
            ('[000] 1.001000000: sched:sched_switch: worker:7 [120] S ==> idle:0 [120]\n', 'broken CPU'),
            ('[000] 0.999000000: sched:sched_switch: worker:42 [120] S ==> idle:0 [120]\n', 'backwards'),
            ('PERF_RECORD_LOST: 2\n', 'lost event'),
        ]:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                profile.intervals([first, second], {0}, {42})

    def test_compact_payload_requires_complete_structure(self):
        for payload in (
            'worker:42 [bad] S ==> idle:0 [120]',
            'worker:no_pid [120] S ==> idle:0 [120]',
            'worker:42 [120] ==> idle:0 [120]',
            'worker:42 [120] S ==> idle:0',
            'worker:42 [120] S ==> idle:0 [120] unexpected',
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'malformed switch payload'):
                profile.switch_payload(payload, 5)

    def test_unknown_payload_diagnostic_bounded_and_escaped(self):
        payload = 'unknown\x1b[31m ' + 'x' * 10000
        with self.assertRaises(ValueError) as result:
            profile.switch_payload(payload, 7)
        diagnostic = str(result.exception)
        self.assertIn('line 7: malformed switch payload', diagnostic)
        self.assertIn('unknown\\u001b[31m', diagnostic)
        self.assertNotIn('\x1b', diagnostic)
        self.assertIn('(truncated)', diagnostic)
        self.assertLess(len(diagnostic), 400)


if __name__=='__main__':unittest.main()

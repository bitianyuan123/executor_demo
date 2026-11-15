#!/usr/bin/env python3
"""Synthetic scheduler timelines, no ftrace privileges or third-party modules."""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sched_trace", ROOT / "scripts/analyze_sched_trace.py")
trace = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trace
SPEC.loader.exec_module(trace)


def line(ms, event, body, cpu=0):
    return f" sender name-900 ( 900) [{cpu:03d}] d..2 {100 + ms / 1000:.9f}: {event}: {body}\n"


def switch(ms, prev, nxt, state="S", cpu=0, prev_name="worker one", next_name="worker one"):
    return line(ms, "sched_switch", f"prev_comm={prev_name} prev_pid={prev} prev_prio=120 prev_state={state} ==> next_comm={next_name} next_pid={nxt} next_prio=120", cpu)


def wake(ms, tid=10, cpu=0, target=0, kind="sched_wakeup", name="worker one"):
    return line(ms, kind, f"comm={name} pid={tid} prio=120 target_cpu={target:03d}", cpu)


def analyze(lines, **kwargs):
    analyzer = trace.Analyzer(mapping={10: "poolA", 20: "poolB"}, **kwargs)
    for text in lines:
        analyzer.consume(text)
    return analyzer.finish(True)


class SchedulerTraceTest(unittest.TestCase):
    def test_waking_is_not_wait_anchor_and_space_names_parse(self):
        report = analyze([wake(0, kind="sched_waking"), wake(1), switch(4, 0, 10), switch(7, 10, 0)])
        pool = report["pools"]["poolA"]
        self.assertEqual(pool["wake_to_run"]["n"], 1)
        self.assertAlmostEqual(pool["wake_to_run"]["mean_us"], 3000)
        self.assertAlmostEqual(pool["matched_oncpu_ms"], 3)
        self.assertEqual(report["thread_candidates"][0]["name"], "worker one")
        self.assertEqual(pool["events"]["sched_waking"], 1)

    def test_preemption_wait_does_not_include_sleep(self):
        report = analyze([switch(0, 0, 10), switch(2, 10, 20, "R+"),
                          switch(5, 20, 10), switch(9, 10, 0),
                          wake(20), switch(22, 0, 10), switch(25, 10, 0)])
        pool = report["pools"]["poolA"]
        self.assertEqual(pool["runnable_switch_to_run"]["mean_us"], 3000)
        self.assertEqual(pool["wake_to_run"]["mean_us"], 2000)
        self.assertEqual(pool["matched_waiting_ms"], 5)
        self.assertEqual(pool["matched_oncpu_ms"], 9)
        self.assertEqual(pool["events"]["explicit_preemption_R_plus"], 1)

    def test_record_cpu_is_distinct_from_target_and_migration(self):
        report = analyze([wake(0, cpu=5, target=1),
                          line(1, "sched_migrate_task", "comm=worker one pid=10 prio=120 orig_cpu=1 dest_cpu=2", 5),
                          switch(4, 0, 10, cpu=2), switch(7, 10, 0, cpu=2)], cpu_mask={1, 2, 5}, coverage="full")
        self.assertEqual(report["event_record_cpus"], [2, 5])
        self.assertEqual(report["destination_cpus"], [1, 2])
        self.assertEqual(report["pools"]["poolA"]["wake_to_run"]["mean_us"], 4000)

    def test_partial_mask_censors_unseen_execution(self):
        report = analyze([wake(0, target=1), switch(4, 0, 10), switch(7, 10, 0)], cpu_mask={0}, coverage="partial")
        self.assertEqual(report["pools"]["poolA"]["wake_to_run"]["n"], 0)
        self.assertEqual(report["audit"]["wake_target_outside_mask_censored"], 1)
        self.assertEqual(report["pools"]["poolA"]["matched_oncpu_ms"], 3)

    def test_unknown_migration_coverage_censors_wait(self):
        report = analyze([wake(0), line(1, "sched_migrate_task", "comm=worker one pid=10 prio=120 orig_cpu=0 dest_cpu=1"),
                          switch(5, 0, 10, cpu=1), switch(7, 10, 0, cpu=1)])
        self.assertEqual(report["pools"]["poolA"]["wake_to_run"]["n"], 0)
        self.assertEqual(report["audit"]["migration_wait_coverage_censored"], 1)

    def test_duplicate_wake_excluded_not_double_counted(self):
        report = analyze([wake(0), wake(1), switch(4, 0, 10), switch(7, 10, 0)])
        self.assertEqual(report["pools"]["poolA"]["wake_to_run"]["n"], 0)
        self.assertEqual(report["audit"]["ambiguous_wait_samples_excluded"], 1)

    def test_unmatched_boundaries_not_filled(self):
        report = analyze([switch(0, 10, 20), wake(1), switch(4, 20, 10), wake(6, tid=20)])
        self.assertEqual(report["pools"]["poolA"]["matched_oncpu_ms"], 0)
        self.assertEqual(report["audit"]["unmatched_switch_out"], 1)
        self.assertEqual(report["audit"]["right_censored_oncpu_anchors"], 1)
        self.assertEqual(report["audit"]["right_censored_wait_anchors"], 1)

    def test_loss_malformed_and_reordering_invalidate_anchors(self):
        for broken, audit in [("CPU:0 [LOST 25 EVENTS]\n", "lost_event_markers"),
                              (line(1, "sched_switch", "bad payload"), "malformed_scheduler_lines"),
                              (wake(-1), "out_of_order_events_dropped")]:
            with self.subTest(audit=audit):
                report = analyze([wake(0), broken, switch(4, 0, 10), switch(7, 10, 0)])
                self.assertEqual(report["audit"][audit], 1)
                self.assertEqual(report["pools"]["poolA"]["wake_to_run"]["n"], 0)

    def test_cpu_switch_chain_mismatch_discards_oncpu(self):
        report = analyze([switch(0, 0, 10), switch(5, 20, 0)])
        self.assertEqual(report["pools"]["poolA"]["matched_oncpu_ms"], 0)
        self.assertEqual(report["audit"]["cpu_switch_chain_mismatch"], 1)

    def test_window_clipping_and_time_weighted_demand(self):
        report = analyze([wake(0), switch(4, 0, 10), switch(10, 10, 0)], start=.002, duration=.006)
        pool = report["pools"]["poolA"]
        # Wake interval crosses left edge; no complete wake latency sample.
        self.assertEqual(pool["wake_to_run"]["n"], 0)
        self.assertEqual(pool["matched_waiting_ms"], 2)
        # Later text closes the interval; clip to the selected right boundary.
        self.assertEqual(pool["matched_oncpu_ms"], 4)
        self.assertEqual(report["duration_ms"], 6)
        self.assertEqual(pool["windows"]["1.0"]["series"][0]["observed_demand"], 1)
        self.assertEqual(report["audit"]["right_censored_oncpu_anchors"], 0)

    def test_no_mapping_produces_candidates_not_invented_groups(self):
        a = trace.Analyzer()
        a.consume(wake(0))
        a.consume(switch(2, 0, 10))
        report = a.finish()
        self.assertEqual(list(report["pools"]), [trace.UNMAPPED])
        self.assertEqual(report["thread_candidates"][0]["mapping"], "unmapped")
        b = trace.Analyzer(patterns=[("named", r"worker one")])
        b.consume(wake(0))
        self.assertEqual(b.finish()["thread_candidates"][0]["mapping"], "heuristic_name")

    def test_stream_storage_bounds_and_quantiles(self):
        a = trace.Analyzer(max_threads=3, max_bins=10, max_cells=12)
        for i in range(30000):
            a.consume(wake(i / 1000, tid=i % 7 + 1))
        report = a.finish(True)
        self.assertLessEqual(report["storage"]["tracked_threads"], 3)
        self.assertLessEqual(report["storage"]["window_cells"], 12)
        self.assertGreater(report["audit"]["thread_limit_events_skipped"], 0)
        self.assertGreater(report["audit"]["events_after_window"], 0)
        h = trace.Histogram()
        for i in range(1, 10001):
            h.add(i)
        self.assertGreaterEqual(h.report()["p99_us"], 9.9)
        self.assertLessEqual(h.report()["p99_us"], 9.9 * 1.022)

    def test_stdin_and_csv_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping = pathlib.Path(directory) / "map.csv"
            output = pathlib.Path(directory) / "new" / "nested" / "report.json"
            mapping.write_text("tid,pool\n10,executor-A\n")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/analyze_sched_trace.py"), "-",
                                     "--tid-map", str(mapping), "--json", str(output)],
                                    input=wake(0) + switch(2, 0, 10) + switch(5, 10, 0), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("executor-A", json.loads(output.read_text())["pools"])

    def test_small_correlations_are_not_certified(self):
        self.assertIsNone(trace.correlation([(1, 2), (2, 4)])["pearson_r"])
        self.assertAlmostEqual(trace.correlation([(i, 2 * i) for i in range(30)])["pearson_r"], 1)

    def test_waking_unique_and_cpu_correlations_with_idle_windows(self):
        # 30 active bins: attempted wakes and distinct running TIDs vary together,
        # while successful wakeups stay at one. Ten idle bins change the latter's
        # full-window correlation without making its active series informative.
        a = trace.Analyzer(mapping={tid: "pool" for tid in range(100, 105)},
                           widths=[10], duration=.4)
        for index in range(30):
            base, count = index * 10, index % 5 + 1
            for member in range(count):
                a.consume(wake(base, tid=100 + member, kind="sched_waking"))
            a.consume(wake(base + .01, tid=100))
            for member in range(count):
                begin = base + .1 + member * .3
                a.consume(switch(begin, 0, 100 + member))
                a.consume(switch(begin + .2, 100 + member, 0))
        a.consume(switch(400, 0, 0))
        report = a.finish(True)
        window = report["pools"]["pool"]["windows"]["10.0"]
        for key in ("waking_to_unique_oncpu_tids_lag_0_bins", "waking_to_oncpu_cores_lag_0_bins",
                    "unique_oncpu_tids_to_oncpu_cores_lag_0_bins"):
            for selection, n in (("correlations", 40), ("activity_correlations", 30)):
                self.assertEqual(window[selection][key]["n"], n)
                self.assertAlmostEqual(window[selection][key]["pearson_r"], 1)
        old_key = "wakeup_to_oncpu_cores_lag_0_bins"
        self.assertGreater(window["correlations"][old_key]["pearson_r"], 0)
        self.assertIsNone(window["activity_correlations"][old_key]["pearson_r"])
        self.assertEqual(window["activity_correlations"][old_key]["reason"], "constant_series")
        self.assertTrue(any("selection can bias r" in warning for warning in report["warnings"]))
        output = io.StringIO()
        with redirect_stdout(output):
            trace.print_report(report)
        text = output.getvalue()
        self.assertIn("correlation lag=0bins full: waking->unique_oncpu: r=1.000 n=40", text)
        self.assertIn("correlation lag=0bins activity: waking->unique_oncpu: r=1.000 n=30", text)
        self.assertIn("waking->oncpu: r=1.000 n=30", text)
        self.assertIn("unique_oncpu->oncpu: r=1.000 n=30", text)
        self.assertIn("wakeup->oncpu: r=undefined n=30 (constant_series)", text)

    def test_activity_pairs_keep_lag_time_axis_and_minimum_samples(self):
        source = [(i * 11) % 7 if i % 3 else 0 for i in range(45)]
        rows = [{"waking": x, "wakeup": 0, "unique_oncpu_tids": 2 * source[i - 1] if i else 0,
                 "oncpu_cores": source[i - 1] / 10 if i else 0,
                 "observed_demand": 0, "completed_wake_wait_mean_us": None}
                for i, x in enumerate(source)]
        full, activity = trace.window_correlations(rows)
        for metric in ("unique_oncpu_tids", "oncpu_cores"):
            for lag in (0, 1, 2, 5):
                key = f"waking_to_{metric}_lag_{lag}_bins"
                pairs = [(source[i], rows[i + lag][metric]) for i in range(len(rows) - lag)]
                self.assertEqual(full[key], trace.correlation(pairs))
                self.assertEqual(activity[key], trace.correlation([(x, y) for x, y in pairs if x or y]))
            self.assertAlmostEqual(activity[f"waking_to_{metric}_lag_1_bins"]["pearson_r"], 1)
        self.assertEqual(full["waking_to_oncpu_cores_lag_1_bins"]["n"], 44)
        self.assertEqual(activity["waking_to_oncpu_cores_lag_1_bins"]["n"], sum(x != 0 for x in source[:-1]))
        # Filtering leaves too few windows even though the full series is long.
        full, activity = trace.window_correlations([
            {"waking": i if i < 11 else 0, "wakeup": 0,
             "unique_oncpu_tids": i if i < 11 else 0, "oncpu_cores": 0,
             "observed_demand": 0, "completed_wake_wait_mean_us": None}
            for i in range(30)])
        key = "waking_to_unique_oncpu_tids_lag_0_bins"
        self.assertAlmostEqual(full[key]["pearson_r"], 1)
        self.assertEqual(activity[key], {"n": 10, "pearson_r": None,
                                         "reason": "fewer_than_20_paired_windows"})

    def test_exact_event_unique_is_separate_from_oncpu_unique(self):
        a = trace.Analyzer(mapping={tid: "pool" for tid in range(1, 81)})
        for tid in range(1, 81):
            a.consume(wake(0, tid=tid, kind="sched_waking"))
        a.consume(switch(.2, 0, 1))
        a.consume(switch(.7, 1, 0))
        report = a.finish(True)
        window = report["pools"]["pool"]["windows"]["1.0"]
        self.assertEqual(window["peak_unique_event_tids"], 80)
        self.assertEqual(window["peak_unique_oncpu_tids"], 1)
        self.assertEqual(window["unique_event_methods"], ["exact_csv_bitset"])
        self.assertEqual(window["unique_oncpu_methods"], ["exact_csv_bitset"])

    def test_background_threads_cannot_evict_mapped_state(self):
        a = trace.Analyzer(mapping={10: "pool"}, max_threads=3)
        for tid in (100, 200, 300, 400, 10):
            a.consume(wake(0, tid=tid))
        self.assertIn(10, a.threads)
        self.assertEqual(len(a.threads), 3)

    def test_wake_and_migration_invalidate_inconsistent_running_anchor(self):
        for event in (wake(2), line(2, "sched_migrate_task", "comm=worker one pid=10 prio=120 orig_cpu=0 dest_cpu=1")):
            report = analyze([switch(0, 0, 10), event, switch(7, 10, 0)])
            self.assertEqual(report["pools"]["poolA"]["matched_oncpu_ms"], 0)


if __name__ == "__main__":
    unittest.main()

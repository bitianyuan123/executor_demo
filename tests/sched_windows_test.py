#!/usr/bin/env python3
import copy
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
SCRIPT = ROOT / "scripts/analyze_sched_windows.py"
spec = importlib.util.spec_from_file_location("sched_windows", SCRIPT)
windows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(windows)


def fixture(values, demands=None, partial=False):
    count = len(next(iter(values.values())))
    elapsed_ns = count * 1000000 - (500000 if partial else 0)
    report = {"schema": "sched-text-v1", "origin_timestamp_ns": 1000000000,
              "analysis_start_timestamp_ns": 1001000000,
              "analysis_end_timestamp_ns": 1001000000 + elapsed_ns,
              "duration_ms": elapsed_ns / 1000000, "coverage_declared": "unknown",
              "audit": {}, "pools": {}}
    for name, cpus in values.items():
        rows = [{"start_ms": i, "span_ms": .5 if partial and i == count - 1 else 1,
                 "oncpu_cores": cpu, "observed_demand": demands[name][i] if demands else cpu}
                for i, cpu in enumerate(cpus)]
        report["pools"][name] = {"windows": {"1.0": {"n": count, "incomplete_storage": False, "series": rows}}}
    return report


class WindowAnalysisTest(unittest.TestCase):
    def test_joint_peaks_are_synchronous_not_sum_of_individual_maxima(self):
        alternating = windows.analyze(fixture({"alpha": [10, 0, 0, 0], "beta": [0, 10, 0, 0]}))
        concurrent = windows.analyze(fixture({"alpha": [10, 0, 0, 0], "beta": [10, 0, 0, 0]}))
        self.assertEqual(alternating["joint"]["oncpu"]["max"], 10)
        self.assertEqual(concurrent["joint"]["oncpu"]["max"], 20)
        self.assertEqual(alternating["joint"]["high"][2]["simultaneously_high_pool_counts"], {0: 2, 1: 2})
        self.assertEqual(concurrent["joint"]["high"][2]["simultaneously_high_pool_counts"], {0: 3, 2: 1})
        self.assertEqual(alternating["joint"]["high"][2]["conditional"]["alpha"]["targets"]["beta"]["fraction"], 1)

    def test_low_service_does_not_hide_high_observed_demand(self):
        result = windows.analyze(fixture({"a": [10, 0, 0, 0], "b": [0, 1, 1, 1]},
                                        {"a": [10, 0, 0, 0], "b": [100, 1, 1, 1]}))
        self.assertEqual(result["joint"]["demand"]["max"], 110)
        self.assertEqual(result["joint"]["high"][2]["conditional"]["a"]["targets"]["b"]["fraction"], 1)
        output = io.StringIO()
        with redirect_stdout(output):
            windows.print_report(result)
        self.assertIn("not idle/borrowable", output.getvalue())
        self.assertIn("not hardware capacity", output.getvalue())

    def test_constant_zero_and_strict_threshold_have_no_high_bins(self):
        result = windows.analyze(fixture({"constant": [3] * 10, "zero": [0] * 10}), multiples=(1, 2))
        for pool in result["pools"].values():
            self.assertEqual(pool["high"][1]["n"], 0)
            self.assertIsNone(pool["high"][1]["segments"]["p50_ms"])
        self.assertIsNone(result["pools"]["zero"]["top_service_fraction"])
        self.assertEqual(result["pools"]["constant"]["top_service_fraction"], .1)
        self.assertIsNone(result["joint"]["high"][1]["conditional"]["zero"]["targets"]["constant"]["fraction"])

    def test_partial_window_excluded_and_edge_segments_counted(self):
        result = windows.analyze(fixture({"only": [10, 0, 0, 0, 10, 999]}, partial=True), multiples=(1,))
        self.assertEqual(result["complete_windows"], 5)
        self.assertEqual(result["partial_windows_excluded"], 1)
        self.assertEqual(result["pools"]["only"]["oncpu"]["max"], 10)
        self.assertEqual(result["pools"]["only"]["high"][1]["segments"],
                         {"n": 2, "p50_ms": 1, "p95_ms": 1, "max_ms": 1, "edge_censored_segments": 2})
        self.assertEqual(result["complete_end_ns"], result["end_ns"] - 500000)

    def test_segment_lengths_and_top_decile_service(self):
        result = windows.analyze(fixture({"x": [0, 8, 8, 0, 0, 0, 0, 0, 4, 0]}), multiples=(1,))
        pool = result["pools"]["x"]
        self.assertEqual(pool["top_service_fraction"], .4)
        self.assertEqual(pool["high"][1]["segments"],
                         {"n": 2, "p50_ms": 1, "p95_ms": 2, "max_ms": 2, "edge_censored_segments": 0})

    def test_dynamic_selection_excludes_only_unmapped_by_default(self):
        report = fixture({"x": [1, 2], "sixteen": [2, 3], "[unmapped]": [999, 999]})
        result = windows.analyze(report)
        self.assertEqual(result["selected_pools"], ["sixteen", "x"])
        self.assertEqual(result["joint"]["oncpu"]["max"], 5)
        self.assertEqual(windows.analyze(report, pools=["[unmapped]"])["joint"]["oncpu"]["max"], 999)
        for names in (["missing"], ["x", "x"], []):
            with self.assertRaises(windows.InvalidReport):
                windows.analyze(report, pools=names)

    def test_truncated_missing_misaligned_and_incomplete_are_rejected(self):
        base = fixture({"a": [1, 2, 3], "b": [3, 2, 1]})
        changes = [lambda w: w["series"].pop(), lambda w: w.update(n=2),
                   lambda w: w["series"][1].update(start_ms=1.5),
                   lambda w: w["series"][2].update(span_ms=.5),
                   lambda w: w.update(incomplete_storage=True), lambda w: w.pop("series")]
        for change in changes:
            report = copy.deepcopy(base)
            change(report["pools"]["b"]["windows"]["1.0"])
            with self.subTest(change=change), self.assertRaises(windows.InvalidReport):
                windows.analyze(report)
        base["audit"]["window_storage_limit_updates_skipped"] = 1
        with self.assertRaises(windows.UnavailableReport):
            windows.analyze(base)

    def test_range_and_values_are_strict(self):
        for field, value in (("oncpu_cores", float("nan")), ("oncpu_cores", float("inf")),
                             ("observed_demand", -1), ("oncpu_cores", True),
                             ("observed_demand", .5), ("start_ms", "0")):
            report = fixture({"x": [1, 2]})
            report["pools"]["x"]["windows"]["1.0"]["series"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(windows.InvalidReport):
                windows.analyze(report)
        for key, value in (("duration_ms", 3), ("origin_timestamp_ns", 999999999999),
                           ("analysis_start_timestamp_ns", True), ("coverage_declared", "guessed"),
                           ("audit", None)):
            report = fixture({"x": [1, 2]})
            report[key] = value
            with self.subTest(key=key), self.assertRaises(windows.InvalidReport):
                windows.analyze(report)

    def test_bin_options_and_only_partial_are_rejected(self):
        report = fixture({"x": [1, 2]})
        for bin_ms, multiples in ((0, (2,)), (float("nan"), (2,)), (1, (0,)),
                                  (1, (float("inf"),)), (1, (2, 2)), (1, ())):
            with self.subTest(bin_ms=bin_ms, multiples=multiples), self.assertRaises(windows.InvalidReport):
                windows.analyze(report, bin_ms, multiples)
        with self.assertRaises(windows.UnavailableReport):
            windows.analyze(fixture({"x": [1]}, partial=True))

    def test_actual_analyzer_schema_integration(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            from analyze_sched_trace import Analyzer
            analyzer = Analyzer(mapping={11: "one", 12: "two"}, duration=.005, widths=(1,))
            for line in (
                "x [000] 1.000000: sched_wakeup: comm=one pid=11 prio=120 target_cpu=0",
                "x [000] 1.000100: sched_switch: prev_comm=idle prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=one next_pid=11 next_prio=120",
                "x [000] 1.003000: sched_switch: prev_comm=one prev_pid=11 prev_prio=120 prev_state=S ==> next_comm=idle next_pid=0 next_prio=120",
                "x [000] 1.005000: sched_wakeup: comm=two pid=12 prio=120 target_cpu=0",
            ):
                analyzer.consume(line)
            report = analyzer.finish(include_windows=True)
            result = windows.analyze(report)
            self.assertEqual(result["complete_windows"], 5)
            self.assertAlmostEqual(result["pools"]["one"]["oncpu"]["mean"], .58)
        finally:
            sys.path.pop(0)

    def test_cli_success_missing_series_schema_and_file_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "summary.json"
            report = fixture({"arbitrary-name": [1, 9, 0, 0]})
            path.write_text(json.dumps(report))
            def run(*args):
                return subprocess.run([sys.executable, str(SCRIPT), str(path), *args],
                                      capture_output=True, text=True, timeout=10)
            good = run()
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertIn("complete_n=4", good.stdout)
            self.assertIn("absolute_ns=", good.stdout)
            limited = run("--max-input-mb", ".00001")
            self.assertEqual(limited.returncode, 2)
            self.assertIn("size limit", limited.stderr)
            self.assertEqual(run("--max-input-mb", "129").returncode, 2)
            report["pools"]["arbitrary-name"]["windows"]["1.0"].pop("series")
            path.write_text(json.dumps(report))
            missing = run()
            self.assertEqual(missing.returncode, 2)
            self.assertIn("unavailable:", missing.stderr)
            self.assertIn("no series", missing.stderr)
            report["schema"] = "not-a-scheduler-report"
            path.write_text(json.dumps(report))
            self.assertIn("schema", run().stderr)
            path.write_text('{"bad": NaN}')
            self.assertIn("non-finite", run().stderr)


if __name__ == "__main__":
    unittest.main()

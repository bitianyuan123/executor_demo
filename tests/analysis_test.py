#!/usr/bin/env python3
"""Check statistical/report semantics without confusing fixture data with runs."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("analyze", pathlib.Path(__file__).parents[1]/"scripts/analyze.py")
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)


class AnalysisTest(unittest.TestCase):
    def test_nearest_rank_small_p99_is_max(self):
        self.assertEqual(analyze.percentile(list(range(12)), .99), 11)
        self.assertIsNone(analyze.percentile([], .99))

    def test_ties_and_constant_load(self):
        self.assertEqual(analyze.ranks([4, 2, 2, 8]), [2, .5, .5, 3])
        self.assertIsNone(analyze.pearson([0, 0, 0], [1, 2, 3]))
        self.assertAlmostEqual(analyze.correlations([1, 2, 3], [3, 2, 1])["spearman"], -1)

    def test_failure_is_not_successful_latency(self):
        for status in ("rejected", "censored", "failed", "submission_rejected"):
            self.assertFalse(analyze.completed({"status": status}))
        self.assertTrue(analyze.completed({"status": "completed"}))

    def test_incomplete_run_is_not_analyzed_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "metadata.json").write_text(json.dumps({"phase": "running"}))
            (path / "summary.json").write_text(json.dumps({"conservation_ok": True}))
            with self.assertRaises(ValueError):
                analyze.summarize(path)

    def test_no_task_samples_are_unknown_not_zero_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "metadata.json").write_text(json.dumps({"phase": "completed"}))
            (path / "summary.json").write_text(json.dumps({"conservation_ok": True, "task_rows": 0, "offered": 0}))
            (path / "tasks.csv").write_text("status,request_id\n")
            (path / "requests.csv").write_text("status,request_id\n")
            summary = analyze.summarize(path)
            self.assertIsNone(summary["cpu_total_s"])
            self.assertIsNone(summary["migrations"])


if __name__ == "__main__":
    unittest.main()

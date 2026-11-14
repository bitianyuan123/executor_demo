#!/usr/bin/env python3
"""Deterministic data fixtures only; does not execute Folly or a load generator."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("stage3_predict", Path(__file__).with_name("predict.py"))
predictor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(predictor)


def observation(wait, target=1000, queue=0):
    return {"target_cpu_us": target, "native_queue": queue, "pending": queue,
            "inflight": 1, "work_estimate_us": queue * target,
            "wait_ms": wait, "latency_ms": wait + target / 1000}


def run(name, repetition, rows, threads=2):
    return {"id": name, "case": "same-case", "repetition": repetition, "seed": 42 + repetition,
            "config": {"threads_per_pool": threads, "affinity": "cluster"},
            "summary": {"completed_tasks": len(rows)}, "rows": rows}


class PredictionTests(unittest.TestCase):
    def test_absolute_error_and_nearest_rank(self):
        values = predictor.errors([1., 3.], [2., 1.])
        self.assertEqual(values["mae_ms"], 1.5)
        self.assertEqual(values["p95_absolute_error_ms"], 2.)
        self.assertIsNone(predictor.errors([], [])["mae_ms"])

    def test_whole_run_holdout_not_task_split(self):
        # Both tasks in a run belong to the same constant burst. The held-out
        # run must never influence the mean fitted from the other run.
        runs = [run("run-a", 0, [observation(10), observation(10)]),
                run("run-b", 1, [observation(30), observation(30)])]
        model = predictor.evaluate(runs)[0]["models"]["target_only"]
        self.assertEqual(model["pooled_held_out"]["wait_ms"]["mae_ms"], 20.)
        expected = {"run-a": 30., "run-b": 10.}
        for fold in model["folds"]:
            self.assertNotIn(fold["held_out_run"], fold["training_runs"])
            self.assertEqual(fold["training_tasks"], 2)
            self.assertEqual(fold["test_tasks"], 2)
            self.assertEqual(fold["targets"]["wait_ms"]["prediction_mean_ms"], expected[fold["held_out_run"]])

    def test_missing_load_is_not_imputed_as_zero(self):
        runs = [run("a", 0, [observation(1, queue=-1)]),
                run("b", 1, [observation(2, queue=-1)])]
        groups = predictor.evaluate(runs)
        model = groups[0]["models"]["queue_target"]
        self.assertEqual(model["status"], "unavailable")
        self.assertEqual(model["pooled_held_out"]["wait_ms"]["n"], 0)
        self.assertIsNone(model["pooled_held_out"]["wait_ms"]["mae_ms"])
        self.assertEqual(groups[0]["models"]["target_only"]["status"], "evaluated")
        del runs[0]["rows"][0]["native_queue"]
        self.assertEqual(predictor.evaluate(runs)[0]["models"]["queue_target"]["status"], "unavailable")

    def test_constant_features_and_training_only_standardization(self):
        rows = [observation(2, target=1000), observation(4, target=1000)]
        model = predictor.fit(rows, ("target_cpu_us",), "wait_ms")
        self.assertEqual(model["means"], [1000])
        self.assertEqual(model["scales"], [1.])
        self.assertEqual(predictor.predict(model, [observation(999, target=999999)]), [3.])

    def test_linear_relationship_and_collinear_features(self):
        rows = [observation(2 + 3*q, queue=q) for q in range(20)]
        model = predictor.fit(rows, ("target_cpu_us", "native_queue", "pending"), "wait_ms")
        self.assertAlmostEqual(predictor.predict(model, [observation(0, queue=25)])[0], 77., places=4)

    def test_different_configurations_and_single_repetition_do_not_mix(self):
        groups = predictor.evaluate([run("a", 0, [observation(1)], threads=1),
                                     run("b", 1, [observation(2)], threads=8)])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(g["status"] == "insufficient_repetitions" for g in groups))
        one_repetition = predictor.evaluate([run("a", 0, [observation(1)]), run("b", 0, [observation(2)])])
        self.assertEqual(one_repetition[0]["status"], "insufficient_repetitions")

    def test_partial_coverage_uses_paired_baseline(self):
        runs = [run("a", 0, [observation(1, queue=0), observation(100, queue=-1)]),
                run("b", 1, [observation(2, queue=0), observation(200, queue=-1)])]
        model = predictor.evaluate(runs)[0]["models"]["queue_target"]
        result = model["pooled_held_out"]["wait_ms"]
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["paired_target_only"]["n"], 2)
        self.assertEqual(result["mae_ms"], 1.)
        self.assertEqual(result["paired_target_only"]["mae_ms"], 1.)

    def test_manifest_integration_uses_validated_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = []
            for repetition in range(2):
                directory = root / f"run-{repetition}"
                directory.mkdir()
                metadata = {"stage": 3, "planned_tasks": 1, "seed": 42 + repetition,
                            "threads_per_pool": 2, "affinity": "cluster"}
                summary = {"planned_tasks": 1, "completed_tasks": 1, "rejected_tasks": 0,
                           "canceled_tasks": 0, "conservation_ok": True}
                (directory / "metadata.json").write_text(json.dumps(metadata))
                (directory / "summary.json").write_text(json.dumps(summary))
                row = {"status": "completed", "task": 0, "burst": 0, "pool": 0,
                       "planned_ns": 0, "submit_ns": 0, "start_ns": 1000000, "end_ns": 2000000,
                       "target_cpu_us": 1000, "native_queue": 0, "pending": 0,
                       "inflight": 0, "work_estimate_us": 0}
                with (directory / "tasks.csv").open("w") as out:
                    writer = csv.DictWriter(out, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerow(row)
                entries.append({"case": "fixture", "repetition": repetition,
                                "path": str(directory), "returncode": 0})
            entries.append(dict(entries[0]))  # A duplicate path must not become another fold.
            entries.append({"path": "not-created", "returncode": "timeout"})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"binary_sha256": "fixture", "runs": entries}))
            runs, excluded = predictor.read_runs([manifest])
            self.assertEqual(len(runs), 2)
            self.assertEqual(len(excluded), 2)
            group = predictor.evaluate(runs)[0]
            self.assertEqual(group["models"]["queue_target"]["pooled_held_out"]["wait_ms"]["mae_ms"], 0.)
            self.assertIn("缺测", predictor.markdown({"groups": [group], "excluded_entries": excluded}))


if __name__ == "__main__":
    unittest.main()

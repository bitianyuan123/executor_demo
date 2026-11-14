#!/usr/bin/env python3
"""Black-box data integrity tests against actual Folly; no timing speedup assertions."""
import csv
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = str(Path(sys.argv.pop(1)).resolve())


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="executor-demo-test-")
        self.addCleanup(self.temp.cleanup)
        self.sequence = 0

    def run_demo(self, *extra):
        self.sequence += 1
        out = Path(self.temp.name) / str(self.sequence)
        command = [BINARY, "--output", str(out), "--pools", "2", "--threads", "2",
                   "--duration", "1", "--requests", "6", "--qps", "100",
                   "--fanout-min", "3", "--fanout-max", "3", "--chain-length", "2",
                   "--cpu-us", "100", "--join-us", "30", "--jitter", "0.4",
                   "--long-prob", "0.2", "--long-factor", "3", "--sample-ms", "5",
                   "--drain-seconds", "5", "--max-inflight", "32", *extra]
        result = subprocess.run(command, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return out

    def validate(self, out, complete=True, task_log=True):
        summary = json.loads((out / "summary.json").read_text())
        metadata = json.loads((out / "metadata.json").read_text())
        requests = rows(out / "requests.csv")
        tasks = rows(out / "tasks.csv")
        self.assertTrue(summary["conservation_ok"])
        self.assertFalse(summary["io_failed"])
        self.assertEqual(len(requests), summary["offered"])
        self.assertEqual(summary["offered"], summary["accepted"] + summary["rejected"])
        self.assertEqual(summary["accepted"], summary["completed"] + summary["censored"] + summary["failed"])
        self.assertEqual(len(tasks), summary["task_rows"])
        self.assertEqual(metadata["phase"], "completed")
        self.assertEqual(metadata["cpu_clock"], "CLOCK_THREAD_CPUTIME_ID")
        if complete:
            self.assertEqual(summary["offered"], summary["completed"])
        final_pools = {}
        for row in rows(out / "pools.csv"):
            final_pools[row["group"]] = row
        self.assertEqual(len(final_pools), metadata["config"]["pools"])
        for row in final_pools.values():
            for field in ("pending", "inflight", "outstanding", "estimated_outstanding_cpu_us"):
                self.assertEqual(int(row[field]), 0, row)
            self.assertEqual(row["submitted"], row["completed"])
        workers = rows(out / "workers.csv")
        self.assertEqual(len(workers), metadata["config"]["pools"] * metadata["config"]["threads_per_pool"])
        self.assertEqual(len({r["tid"] for r in workers}), len(workers))
        by_request = {}
        for t in tasks:
            by_request.setdefault(t["request_id"], []).append(t)
            if t["start_ns"] != "0":
                self.assertGreaterEqual(int(t["start_ns"]), int(t["submit_ns"]))
                self.assertGreaterEqual(int(t["finish_ns"]), int(t["start_ns"]))
                self.assertAlmostEqual(float(t["enqueue_us"]), (int(t["start_ns"]) - int(t["submit_ns"])) / 1000, delta=1)
            self.assertLess(int(t["chosen_group"]), metadata["config"]["pools"])
            if t["status"] == "completed":
                self.assertIn(t["worker_tid"], {r["tid"] for r in workers})
                if metadata["config"]["work_mode"] == "cpu":
                    self.assertGreaterEqual(float(t["cpu_us"]) + 0.01, float(t["work_target_us"]))
        chain = metadata["config"]["chain_length"]
        for r in requests:
            self.assertGreaterEqual(int(r["offered_ns"]), int(r["scheduled_ns"]))
            if r["status"] == "rejected":
                self.assertEqual(r["latency_us"], "")
                self.assertNotIn(r["request_id"], by_request)
            elif r["status"] == "completed":
                self.assertGreaterEqual(int(r["finish_ns"]), int(r["first_start_ns"]))
                self.assertAlmostEqual(float(r["latency_us"]), (int(r["finish_ns"]) - int(r["scheduled_ns"])) / 1000, delta=2)
                if not task_log:
                    continue
                task_map = {(int(t["node_id"]), int(t["stage"])): t for t in by_request[r["request_id"]]}
                node_ids = {node for node, _ in task_map}
                self.assertEqual(len(task_map), len(by_request[r["request_id"]]), "duplicate node stage")
                self.assertEqual(node_ids, set(range(int(r["cpu_nodes"]))))
                self.assertEqual(sum(t["kind"] == "join" for t in task_map.values()), int(r["waves"]))
                target_cpu = sum(float(t["work_target_us"]) for t in task_map.values())
                self.assertEqual(target_cpu, int(r["planned_cpu_us"]))
                for (node, stage), task in task_map.items():
                    dependencies = [int(x) for x in task["predecessors"].split(";") if x]
                    for dependency in dependencies:
                        # RPC continuation depends on stage 0 of itself. Graph edges
                        # depend on the predecessor's final stage, after its RPC.
                        previous = task_map[(dependency, 0)] if dependency == node else task_map.get((dependency, 1), task_map[(dependency, 0)])
                        self.assertGreaterEqual(int(task["submit_ns"]), int(previous["finish_ns"]))
                    if stage == 1:
                        first = task_map[(node, 0)]
                        self.assertGreaterEqual(int(task["submit_ns"]), int(first["finish_ns"]) + metadata["config"]["rpc_delay_us"] * 1000)
                    if task["kind"] == "join":
                        wave = int(task["wave"])
                        width = int(r["fanout"]) if metadata["config"]["graph"] == "chains" else int(metadata["config"]["wave_widths"].split(",")[wave])
                        self.assertEqual(len(dependencies), width)
                self.assertTrue(all(t["status"] == "completed" for t in task_map.values()))
        seconds = rows(out / "seconds.csv")
        self.assertEqual(sum(int(r["completed"]) for r in seconds), summary["completed"])
        self.assertEqual(sum(int(r["offered"]) for r in seconds), summary["offered"])
        self.assertEqual(sum(int(r["rejected"]) for r in seconds), summary["rejected"])
        return requests, tasks, metadata

    def test_policies_same_seed_and_home_invariant(self):
        reference = None
        for policy in ("home", "borrow", "sticky", "fanout", "least-load"):
            out = self.run_demo("--policy", policy)
            requests, tasks, _ = self.validate(out)
            signature = sorted((t["request_id"], t["node_id"], t["stage"], t["work_target_us"]) for t in tasks)
            if reference is None:
                reference = signature
            else:
                self.assertEqual(signature, reference)
            if policy == "home":
                self.assertTrue(all(t["chosen_group"] == t["home_group"] for t in tasks))
            self.assertEqual(len(tasks), 6 * (3 * 2 + 1))
            scheduled = sorted(int(r["scheduled_ns"]) for r in requests)
            self.assertEqual([b - a for a, b in zip(scheduled, scheduled[1:])], [10_000_000] * 5)

    def test_rpc_and_fixed_estimator(self):
        out = self.run_demo("--rpc-prob", "1", "--rpc-delay-us", "600", "--estimate-us", "77", "--policy", "sticky")
        _, tasks, _ = self.validate(out)
        self.assertEqual(len(tasks), 6 * (3 * 2 * 2 + 1))
        self.assertTrue(all(t["work_estimate_us"] == "77" for t in tasks))

    def test_waves_multiple_dependencies_and_repeated_forks(self):
        for rpc in ("0", "1"):
            out = self.run_demo("--graph", "waves", "--wave-widths", "5,1,3", "--wave-depths", "2,3,1",
                                "--rpc-prob", rpc, "--rpc-delay-us", "100", "--policy", "sticky")
            requests, tasks, _ = self.validate(out)
            self.assertTrue(all(r["operator_nodes"] == "16" and r["cpu_nodes"] == "19" for r in requests))
            self.assertEqual(len(tasks), 6 * (16 * (2 if rpc == "1" else 1) + 3))
            self.assertEqual({t["wave"] for t in tasks}, {"0", "1", "2"})

    def test_fanout_balance_boundary(self):
        for boundary in ("initial", "all"):
            out = self.run_demo("--graph", "waves", "--wave-widths", "8,2,6", "--wave-depths", "1,1,2",
                                "--policy", "fanout", "--balance-forks", boundary, "--rpc-prob", "1",
                                "--rpc-delay-us", "100")
            _, tasks, _ = self.validate(out)
            eligible = [t for t in tasks if t["balance_eligible"] == "1"]
            self.assertEqual(len(eligible), 6 * (8 if boundary == "initial" else 16))
            for task in tasks:
                should_balance = task["is_fanout"] == "1" and (boundary == "all" or task["wave"] == "0")
                self.assertEqual(task["balance_eligible"] == "1", should_balance)
                if not should_balance:
                    self.assertEqual(task["chosen_group"], task["parent_group"])

    def test_waves_cutoff(self):
        out = self.run_demo("--graph", "waves", "--wave-widths", "8,4,2", "--wave-depths", "1,2,1",
                            "--requests", "1", "--cpu-us", "200000", "--drain-seconds", "0")
        self.validate(out, complete=False)
        self.assertEqual(json.loads((out / "summary.json").read_text())["censored"], 1)

    def test_fixed_iteration_work(self):
        out = self.run_demo("--work-mode", "iterations", "--iterations-per-us", "20", "--memory-kb", "8")
        _, tasks, metadata = self.validate(out)
        self.assertEqual(metadata["config"]["work_mode"], "iterations")
        self.assertTrue(all(float(t["cpu_us"]) > 0 for t in tasks))

    def test_queue_kinds_and_metrics(self):
        for queue, metric in (("lifo", "queue"), ("throttled", "pending"), ("default", "work")):
            out = self.run_demo("--queue", queue, "--metric", metric, "--policy", "borrow", "--wake-us", "25" if queue == "throttled" else "0")
            self.validate(out)

    def test_bounded_overload(self):
        out = self.run_demo("--pools", "1", "--threads", "1", "--qps", "2000", "--requests", "30",
                            "--max-inflight", "1", "--cpu-us", "4000", "--jitter", "0", "--long-prob", "0")
        self.validate(out, complete=False)
        summary = json.loads((out / "summary.json").read_text())
        self.assertGreater(summary["rejected"], 0)
        self.assertGreater(summary["completed"], 0)

    def test_cancellation_and_rpc_flush(self):
        for rpc in ("0", "1"):
            out = self.run_demo("--pools", "1", "--threads", "1", "--requests", "1", "--cpu-us", "200000",
                                "--rpc-prob", rpc, "--rpc-delay-us", "2000000", "--drain-seconds", "0")
            self.validate(out, complete=False)
            summary = json.loads((out / "summary.json").read_text())
            self.assertTrue(summary["drain_timed_out"])
            self.assertEqual(summary["censored"], 1)

        # Let CPU stages actually finish and suspend on long timers, then flush.
        out = self.run_demo("--requests", "1", "--rpc-prob", "1", "--rpc-delay-us", "2000000", "--drain-seconds", "0.02")
        self.validate(out, complete=False)
        self.assertEqual(json.loads((out / "summary.json").read_text())["censored"], 1)

    def test_topology_and_disabled_task_log(self):
        allowed = sorted(os.sched_getaffinity(0))
        lists = f"{allowed[0]};{allowed[-1]}"
        out = self.run_demo("--affinity", "cluster", "--cpus", lists, "--task-log", "0")
        _, tasks, metadata = self.validate(out, task_log=False)
        self.assertEqual(tasks, [])
        self.assertEqual(metadata["groups"][0]["effective_cpus"], [allowed[0]])
        out = self.run_demo("--affinity", "numa", "--cpus", lists, "--numa", "0,0")
        _, _, metadata = self.validate(out)
        self.assertEqual(metadata["groups"][0]["effective_cpus"], sorted(set((allowed[0], allowed[-1]))))
        self.assertTrue(metadata["effective_cpuset_overlap"])

    def test_invalid_and_overwrite_refused(self):
        for args in (("--graph", "waves"), ("--wave-widths", "2,3"), ("--graph", "waves", "--wave-widths", "2,3", "--wave-depths", "1"), ("--balance-forks", "bad"), ("--affinity", "cluster"), ("--cpu-us", "-1"), ("--qps", "nan"), ("--policy", "bad"), ("--wake-us", "1"), ("--queue", "throttled", "--wake-us", "18446744073709551615")):
            result = subprocess.run([BINARY, "--output", str(Path(self.temp.name) / "invalid"), *args], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        out = self.run_demo()
        result = subprocess.run([BINARY, "--output", str(out)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overwrite", result.stderr)


if __name__ == "__main__":
    unittest.main()

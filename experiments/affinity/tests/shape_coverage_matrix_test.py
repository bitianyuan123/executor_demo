#!/usr/bin/env python3
"""Shape comparisons must preserve resources and work counts before execution.

Only generate plans. Artificial CPU partitions never launch the demo, and this
test makes no claims about target-machine performance or physical topology.
"""
import importlib.util
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest


MATRIX = Path(__file__).resolve().parents[1] / "run_matrix.py"
spec = importlib.util.spec_from_file_location("shape_matrix", MATRIX)
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class ShapeCoverageMatrixTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="affinity-shape-matrix-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0

    def invoke(self, updates=None, remove=(), seeds=None, extra=(), suite="shape-coverage",
               success=True):
        self.serial += 1
        output = self.root / str(self.serial)
        base = {"--cpus": "0;1", "--iterations": "375000", "--accesses": "12500"}
        base.update(updates or {})
        for key in remove:
            base.pop(key, None)
        command = [sys.executable, str(MATRIX), "--suite", suite, "--output", str(output)]
        if seeds is not None:
            command += ["--seeds", seeds]
        command += list(extra) + ["--base-args"]
        command += [part for pair in base.items() for part in pair]
        result = subprocess.run(command, text=True, capture_output=True, timeout=10)
        if not success:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(output.exists(), "invalid contract left a runnable plan")
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, json.loads((output / "metadata.json").read_text()), json.loads(
            (output / "plan.json").read_text())

    @staticmethod
    def options(entry):
        command = entry["command"]
        return dict(zip(command[1::2], command[2::2]))

    def test_default_complete_twelve_round_contract_and_paired_order(self):
        result, metadata, entries = self.invoke()
        names = [f"{shape}-{policy}" for shape in ("front80", "tail4")
                 for policy in ("home", "queue", "outstanding")]
        self.assertEqual(metadata["selected_cases"], names)
        self.assertEqual(len(entries), 12)
        expected = []
        for seed in (421, 422):
            shuffled = list(names)
            random.Random(seed).shuffle(shuffled)
            expected.extend((seed, name) for name in shuffled)
        self.assertEqual([(row["seed"], row["case"]) for row in entries], expected)
        for entry in entries:
            options = self.options(entry)
            self.assertEqual(options["--cpus"], "0;1")
            self.assertNotIn("--pool-count", options)
            self.assertEqual(options["--threads"], "16")
            self.assertEqual(options["--affinity"], "cluster")
            self.assertEqual(options["--qps"], "12")
            self.assertEqual(options["--requests"], "240")
            self.assertEqual(options["--trace"], "1")
            self.assertEqual(options["--policy"], entry["case"].split("-", 1)[1])
            self.assertEqual(options["--depths"], "2,2" if entry["case"].startswith("front80-") else "1,22")
        self.assertIn("not production replay", metadata["shape_coverage"]["scope"])
        self.assertIn("NOT zero-overhead", result.stdout)
        self.assertIn("six cells x 2 seed blocks = 12 rounds", result.stdout)
        self.assertEqual(metadata["research_target"], "one_pool_per_cluster")

    def test_shapes_preserve_business_work_branch_memory_and_callback_counts(self):
        _, metadata, entries = self.invoke()
        ratios = set()
        for entry in entries:
            options = self.options(entry)
            widths = list(map(int, options["--widths"].split(",")))
            depths = list(map(int, options["--depths"].split(",")))
            operators = sum(width * depth for width, depth in zip(widths, depths))
            self.assertEqual(operators, 168)
            self.assertEqual(sum(widths), 84)
            self.assertEqual(operators + 1 + len(widths), 171)
            self.assertEqual(sum(widths) * int(options["--branch-kb"]) * 1024, 5505024)
            self.assertEqual(operators - sum(widths), 84, "ordinary successor count drifted")
            self.assertEqual(options["--jitter-percent"], "0")
            self.assertEqual(options["--long-every"], "0")
            self.assertEqual(options["--long-factor"], "1")
            ratios.add((widths[0] * depths[0], operators))
        self.assertEqual(ratios, {(160, 168), (80, 168)})
        self.assertEqual(metadata["shape_coverage"]["iterations_per_request"], 63000000)
        self.assertEqual(metadata["shape_coverage"]["read_words_per_request"], 2100000)
        self.assertEqual(metadata["shape_coverage"]["write_words_per_request"], 2100000)

    def test_three_scales_are_explicit_and_rounding_is_not_hidden(self):
        for iterations, accesses in ((375000, 12500), (46875, 1563), (3000000, 100000)):
            with self.subTest(iterations=iterations):
                _, metadata, entries = self.invoke({"--iterations": str(iterations), "--accesses": str(accesses)})
                self.assertEqual({self.options(row)["--iterations"] for row in entries}, {str(iterations)})
                self.assertEqual({self.options(row)["--accesses"] for row in entries}, {str(accesses)})
                contract = metadata["shape_coverage"]
                self.assertEqual(contract["iterations_per_request"], 168 * iterations)
                self.assertEqual(contract["read_words_per_request"], 168 * accesses)
        self.assertNotEqual(1563 * 8, 12500)

    def test_any_distinct_nonnegative_seeds_extend_full_blocks(self):
        result, metadata, entries = self.invoke(seeds="0,500,900")
        self.assertEqual(len(entries), 18)
        self.assertEqual(metadata["shape_coverage"]["seeds"], [0, 500, 900])
        self.assertEqual(metadata["shape_coverage"]["rounds"], 18)
        self.assertIn("six cells x 3 seed blocks = 18 rounds", result.stdout)
        for seed in (0, 500, 900):
            self.assertEqual(len({row["case"] for row in entries if row["seed"] == seed}), 6)

    def test_conflicting_fixed_fields_are_rejected_before_output(self):
        for key in matrix.SHAPE_COVERAGE_FIXED:
            with self.subTest(key=key):
                self.invoke({"--" + key: "conflict"}, success=False)

    def test_matching_explicit_fixed_fields_are_accepted(self):
        _, _, entries = self.invoke({"--" + key: value for key, value in matrix.SHAPE_COVERAGE_FIXED.items()})
        self.assertEqual(len(entries), 12)

    def test_cpu_partitions_must_be_explicit_and_pool_count_derived(self):
        self.invoke(remove=("--cpus",), success=False)
        for cpus in ("", "0;;1", "0;", ";1"):
            self.invoke({"--cpus": cpus}, success=False)
        for pools in ("4", "6", "invalid"):
            self.invoke({"--pool-count": pools}, success=False)
        _, _, entries = self.invoke({"--pool-count": "0"})
        self.assertTrue(all(self.options(row)["--pool-count"] == "0" for row in entries))

    def test_missing_or_invalid_work_scale_is_rejected(self):
        for key in ("--iterations", "--accesses"):
            self.invoke(remove=(key,), success=False)
            for value in ("0", "-1", "nan", "1.5", "1000000000001"):
                with self.subTest(key=key, value=value):
                    self.invoke({key: value}, success=False)

    def test_case_filter_and_managed_shape_or_policy_are_rejected(self):
        self.invoke(extra=("--cases", "front80-home"), success=False)
        self.invoke({"--policy": "home"}, success=False)
        self.invoke({"--depths": "2,2"}, success=False)
        self.invoke(extra=("--thread-counts", "16,64"), success=False)

    def test_invalid_seeds_are_rejected_before_output(self):
        for seeds in ("", "421,421", "-1", "421,bad", "421,"):
            self.invoke(seeds=seeds, success=False)

    def test_existing_suite_default_seeds_are_unchanged(self):
        _, metadata, entries = self.invoke(suite="routing")
        self.assertEqual({entry["seed"] for entry in entries}, {101, 102, 103})
        self.assertEqual(len(entries), 12)
        self.assertNotIn("shape_coverage", metadata)


if __name__ == "__main__":
    unittest.main()

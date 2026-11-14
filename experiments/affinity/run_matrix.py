#!/usr/bin/env python3
"""Prepare or run bounded, shuffled, paired stage-4-v2 experiments."""
import argparse
import hashlib
import json
import os
import pathlib
import platform
import random
import shlex
import subprocess
import sys


def cases(suite):
    if suite == "routing":
        return [("home", {"policy": "home"}), ("queue", {"policy": "queue"}),
                ("outstanding", {"policy": "outstanding"}), ("work", {"policy": "work"})]
    if suite == "follow":
        return [("home", {"policy": "home"})] + [
            (f"{when}-{continuation}", {"policy": "outstanding", "balance-at": when,
                                        "continuation": continuation})
            for when in ("all", "fork") for continuation in ("parent", "home")]
    if suite == "granularity":
        return [("home", {"policy": "home"})] + [
            (f"chunk-{chunk}", {"policy": "outstanding", "batch-chunk": str(chunk)})
            for chunk in (1, 4, 8)]
    if suite == "deployment":
        return [(affinity, {"policy": "home", "affinity": affinity})
                for affinity in ("none", "numa", "cluster")]
    if suite == "threads":
        return [(f"threads-{n}", {"policy": "home", "threads": str(n)})
                for n in (8, 16, 64, 256)]
    if suite == "overhead":
        return [(f"{policy}-trace{trace}", {"policy": policy, "trace": str(trace)})
                for policy in ("home", "outstanding") for trace in (0, 1)]
    raise ValueError(suite)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="build-affinity/affinity_demo")
    parser.add_argument("--output", required=True)
    parser.add_argument("--suite", choices=["routing", "follow", "granularity", "deployment", "threads", "overhead"], default="routing")
    parser.add_argument("--seeds", default="101,102,103")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--run", action="store_true", help="without this flag only write a plan")
    parser.add_argument("--base-args", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    if len(args.base_args) % 2:
        parser.error("base-args must be --key value pairs")
    base = {}
    for key, value in zip(args.base_args[::2], args.base_args[1::2]):
        if not key.startswith("--") or key in base:
            parser.error("invalid or duplicate base argument")
        base[key] = value
    if "--output" in base or "--seed" in base:
        parser.error("output and seed are managed by the matrix")
    seeds = [int(x) for x in args.seeds.split(",")]
    if not seeds or len(set(seeds)) != len(seeds) or any(x < 0 for x in seeds):
        parser.error("seeds must be distinct nonnegative integers")
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    root = pathlib.Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "plan.json").exists():
        parser.error("output already has a plan; choose a new directory")
    def digest(path):
        if not path.is_file():
            return None
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()
    source_root = pathlib.Path(__file__).resolve().parent
    sources = ["main.cpp", "include/executor_demo/RequestExecutor.h",
               "include/executor_demo/LocalityWorkload.h",
               "../dag/include/executor_demo/BusinessExecutor.h"]
    metadata = {"schema": "affinity-v2-matrix", "binary": str(pathlib.Path(args.binary).resolve()),
                "binary_sha256": digest(pathlib.Path(args.binary)),
                "sources_sha256": {name: digest(source_root / name) for name in sources},
                "machine": platform.machine(), "kernel": platform.release(),
                "caller_allowed_cpus": sorted(os.sched_getaffinity(0)), "argv": sys.argv[1:]}
    if args.run and metadata["binary_sha256"] is None:
        parser.error("binary is missing; build the affinity stage first")
    (root / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    plan = []
    for seed in seeds:
        order = cases(args.suite)
        random.Random(seed).shuffle(order)
        for case, changes in order:
            directory = root / f"seed-{seed}-{case}"
            options = dict(base)
            # A work score uses CPU-us/capacity, while count scores use tasks/
            # capacity. Leave policy defaults in effect for the routing sweep.
            if args.suite == "routing" and ("--busy-high" in base or "--return-low" in base or "--min-gain" in base):
                parser.error("routing suite uses policy-specific threshold defaults; tune thresholds in separate single-policy runs")
            options.update({"--" + key: str(value) for key, value in changes.items()})
            options.update({"--seed": str(seed), "--output": str(directory)})
            command = [args.binary] + [part for pair in options.items() for part in pair]
            plan.append({"seed": seed, "case": case, "directory": str(directory), "command": command})
    (root / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    for i, entry in enumerate(plan, 1):
        print(f"[{i}/{len(plan)}] {shlex.join(entry['command'])}", flush=True)
        if args.run:
            directory = pathlib.Path(entry["directory"])
            directory.mkdir(parents=True, exist_ok=False)
            with (directory / "run.log").open("w") as log:
                try:
                    result = subprocess.run(entry["command"], stdout=log, stderr=subprocess.STDOUT,
                                            timeout=args.timeout_seconds)
                except subprocess.TimeoutExpired:
                    print(f"FAILED timeout: {directory}; partial run excluded", file=sys.stderr)
                    return 2
            if result.returncode:
                print(f"FAILED rc={result.returncode}: {directory}/run.log", file=sys.stderr)
                return result.returncode
            summary = json.loads((directory / "summary.json").read_text())
            print(f"  completed={summary['completed']} mean_ms={summary['request_mean_ms']:.3f} "
                  f"p99_ms={summary['request_p99_ms']:.3f} migrations={summary['migrations']}", flush=True)
    if args.run:
        print("Report: " + shlex.join([sys.executable, str(pathlib.Path(__file__).with_name("report.py"))] + [p["directory"] for p in plan]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

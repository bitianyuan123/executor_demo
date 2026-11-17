#!/usr/bin/env python3
"""Prepare or run bounded, shuffled, paired stage-4-v2 experiments."""
import argparse
import hashlib
import json
import math
import os
import pathlib
import platform
import random
import shlex
import subprocess
import sys


SUITES = ("routing", "signals", "arrival", "cluster-baseline", "real-baseline",
          "legacy-four-pool-replay", "blocking", "follow", "granularity",
          "deployment", "threads", "overhead", "follow-overhead", "async-pilot")
HISTORICAL_SUITES = ("real-baseline", "legacy-four-pool-replay")
ASYNC_PILOT_FIXED = {"async-align-us": "0", "async-capacity": "4096",
                     "balance-at": "all", "continuation": "parent",
                     "home-policy": "rr", "batch-chunk": "1", "max-hops": "4",
                     "cross-numa": "0", "cpu-sampling": "0", "page-sample": "0",
                     "sync-block-percent": "0", "sync-block-us": "0"}


def cases(suite):
    if suite == "async-pilot":
        cells = [(mode, policy, 0) for mode in ("d0", "a0", "a1")
                 for policy in ("home", "outstanding")]
        cells += [(mode, "home", 1) for mode in ("a0", "a1")]
        return [(f"{mode}-{policy}-trace{trace}", {
            **ASYNC_PILOT_FIXED, "policy": policy, "trace": str(trace),
            "async-percent": "0" if mode == "d0" else "50",
            "async-delay-us": "2000" if mode == "a1" else "0"})
            for mode, policy, trace in cells]
    if suite == "routing":
        return [("home", {"policy": "home"}), ("queue", {"policy": "queue"}),
                ("outstanding", {"policy": "outstanding"}), ("work", {"policy": "work"})]
    if suite == "signals":
        return [(policy, {"policy": policy})
                for policy in ("home", "queue", "active", "outstanding", "cpu", "work")]
    if suite in ("arrival", "cluster-baseline", *HISTORICAL_SUITES):
        deployment = ({"pool-count": "4", "threads": "128", "affinity": "none", "qps": "16"}
                      if suite in HISTORICAL_SUITES else {})
        return [(name, {**deployment, "policy": "home", **arrival}) for name, arrival in (
            ("regular", {"arrival": "regular"}), ("poisson", {"arrival": "poisson"}),
            ("burst4", {"arrival": "burst", "burst-size": "4", "burst-spacing-us": "100"}),
            ("burst8", {"arrival": "burst", "burst-size": "8", "burst-spacing-us": "100"}))]
    if suite == "blocking":
        return [(f"{policy}-block{percent}", {"policy": policy, "sync-block-percent": str(percent),
                                               "sync-block-us": "10000" if percent else "0"})
                for percent in (0, 50) for policy in ("home", "active", "outstanding", "cpu")]
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
                for n in (8, 16, 64, 128, 256)]
    if suite == "overhead":
        return [(f"{policy}-trace{trace}", {"policy": policy, "trace": str(trace)})
                for policy in ("home", "outstanding") for trace in (0, 1)]
    if suite == "follow-overhead":
        # Keep all six cells in the same seed block. Explicit continuation and
        # balance-at prevent caller defaults from changing the three strategies.
        return [(f"{name}-trace{trace}", {"policy": policy, "balance-at": when,
                                           "continuation": "parent", "trace": str(trace)})
                for name, policy, when in (("home", "home", "all"),
                                          ("fork-parent", "outstanding", "fork"),
                                          ("all-parent", "outstanding", "all"))
                for trace in (0, 1)]
    raise ValueError(suite)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="build-affinity/affinity_demo")
    parser.add_argument("--output", required=True)
    parser.add_argument("--suite", choices=SUITES, default="routing")
    parser.add_argument("--cases", help="comma-separated case names from the selected suite; default: all cases")
    parser.add_argument("--thread-counts", help="signals only: comma-separated distinct positive thread counts per pool; expand selected policies in each seed block")
    parser.add_argument("--seeds", default="101,102,103")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--run", action="store_true", help="without this flag only write a plan")
    parser.add_argument("--base-args", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    selected = cases(args.suite)
    if args.cases is not None:
        requested = [name.strip() for name in args.cases.split(",")]
        if not all(requested) or len(set(requested)) != len(requested):
            parser.error("cases must be distinct nonempty case names")
        available = {name for name, _ in selected}
        unknown = set(requested) - available
        if unknown:
            parser.error(f"unknown cases for {args.suite}: {', '.join(sorted(unknown))}; "
                         f"available: {', '.join(name for name, _ in selected)}")
        selected = [(name, changes) for name, changes in selected if name in requested]
    if len(args.base_args) % 2:
        parser.error("base-args must be --key value pairs")
    base = {}
    for key, value in zip(args.base_args[::2], args.base_args[1::2]):
        if not key.startswith("--") or key in base:
            parser.error("invalid or duplicate base argument")
        base[key] = value
    if "--output" in base or "--seed" in base:
        parser.error("output and seed are managed by the matrix")
    thread_counts = None
    if args.thread_counts is not None:
        if args.suite != "signals":
            parser.error("thread-counts is supported only by the signals suite")
        if "--threads" in base:
            parser.error("thread-counts and base --threads cannot be combined")
        try:
            thread_counts = [int(value.strip()) for value in args.thread_counts.split(",")]
        except ValueError:
            parser.error("thread-counts must be distinct positive integers")
        if any(value <= 0 for value in thread_counts) or len(set(thread_counts)) != len(thread_counts):
            parser.error("thread-counts must be distinct positive integers")
        selected = [(f"threads-{count}-{name}", {**changes, "threads": str(count)})
                    for count in thread_counts for name, changes in selected]
    # Validate before creating output, including when a signals thread sweep is
    # requested. Count and work scores deliberately retain their own defaults.
    if args.suite in ("routing", "signals", "blocking", "async-pilot") and any(
            key in base for key in ("--busy-high", "--return-low", "--min-gain")):
        parser.error("routing/signals/blocking/async-pilot suites use policy-specific threshold defaults; tune thresholds in separate single-policy runs")
    if args.suite == "async-pilot":
        if args.cases is not None:
            parser.error("async-pilot requires all eight cells; case filtering is not supported")
        for key in ("policy", "trace", "async-percent", "async-delay-us"):
            if "--" + key in base:
                parser.error(f"async-pilot manages --{key}; omit it from base-args")
        for key, value in ASYNC_PILOT_FIXED.items():
            if "--" + key in base and base["--" + key] != value:
                parser.error(f"async-pilot requires --{key} {value}")
    if args.suite in ("cluster-baseline", "async-pilot"):
        if base.get("--affinity") != "cluster":
            parser.error(f"{args.suite} requires explicit --base-args --affinity cluster; affinity is not silently overridden")
        partitions = base.get("--cpus", "").split(";")
        if not all(partition.strip() for partition in partitions):
            parser.error(f"{args.suite} requires explicit nonempty --cpus hardware-cluster partitions without empty segments")
        try:
            pool_count = int(base.get("--pool-count", "0"))
        except ValueError:
            parser.error(f"{args.suite} pool-count must be omitted or 0")
        if pool_count != 0:
            parser.error(f"{args.suite} pool-count must be omitted or 0; one pool is derived from each --cpus partition")
    if args.suite == "real-baseline":
        print("WARNING: real-baseline is deprecated and is only a historical capture replay "
              "(4 pools, 128 threads, affinity=none, 16 QPS), not the one-pool-per-cluster target. "
              "Use legacy-four-pool-replay for that replay or cluster-baseline for the target.",
              file=sys.stderr)
    seeds = [int(x) for x in args.seeds.split(",")]
    if not seeds or len(set(seeds)) != len(seeds) or any(x < 0 for x in seeds):
        parser.error("seeds must be distinct nonnegative integers")
    if args.suite == "async-pilot" and len(seeds) != 1:
        parser.error("async-pilot is one bounded eight-run block; specify exactly one seed")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("timeout must be finite and positive")
    root = pathlib.Path(args.output)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        parser.error("output is not an empty directory; preserve prior results and choose a new directory")
    root.mkdir(parents=True, exist_ok=True)
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
               "include/executor_demo/AsyncCompletionSource.h",
               "include/executor_demo/LocalityWorkload.h",
               "include/executor_demo/LoadPolicy.h", "include/executor_demo/LinuxSchedstatLoadProvider.h",
               "../dag/include/executor_demo/BusinessExecutor.h"]
    metadata = {"schema": "affinity-v2-matrix", "binary": str(pathlib.Path(args.binary).resolve()),
                "suite": args.suite,
                "selected_cases": [name for name, _ in selected],
                "experiment_role": ("target_cluster_baseline" if args.suite == "cluster-baseline"
                                    else "historical_capture_replay" if args.suite in HISTORICAL_SUITES
                                    else "controlled_comparison"),
                "research_target": "one_pool_per_cluster",
                "binary_sha256": digest(pathlib.Path(args.binary)),
                "sources_sha256": {name: digest(source_root / name) for name in sources},
                "machine": platform.machine(), "kernel": platform.release(),
                "caller_allowed_cpus": sorted(os.sched_getaffinity(0)), "argv": sys.argv[1:]}
    if thread_counts is not None:
        metadata["thread_counts"] = thread_counts
    if args.run and metadata["binary_sha256"] is None:
        parser.error("binary is missing; build the affinity stage first")
    (root / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    plan = []
    for seed in seeds:
        order = list(selected)
        random.Random(seed).shuffle(order)
        for case, changes in order:
            directory = root / f"seed-{seed}-{case}"
            options = dict(base)
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
            migrations = summary['migrations'] if summary['trace'] else 'unavailable'
            print(f"  completed={summary['completed']} mean_ms={summary['request_mean_ms']:.3f} "
                  f"p99_ms={summary['request_p99_ms']:.3f} migrations={migrations} "
                  f"request_peak={summary['peak_inflight']} process_cpu_s={summary['process_cpu_s']:.6f} "
                  f"arrival_hash={summary['arrival_hash']} fixed_iterations={summary.get('planned_total_iterations', 'unknown')}", flush=True)
    if args.run:
        report_name = "async_pilot_report.py" if args.suite == "async-pilot" else "report.py"
        print("Report: " + shlex.join([sys.executable, str(pathlib.Path(__file__).with_name(report_name))] + [p["directory"] for p in plan]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

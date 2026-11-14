#!/usr/bin/env python3
"""Sequential, randomized-order paired experiments. Never runs workloads in parallel."""
import argparse
import hashlib
import json
import os
import pathlib
import random
import subprocess
import sys
import time

from topology import capture


def local_cases(cpus):
    # These are explicitly logical CPU partitions, NOT discovered physical clusters.
    half = max(1, len(cpus)//2)
    two = ";".join(",".join(map(str, part)) for part in (cpus[:half], cpus[half:] or cpus[:half]))
    cases = []
    for threads in (2, 16):
        for policy in ("home", "borrow", "sticky", "fanout", "least-load"):
            cases.append((f"split-t{threads}-{policy}", ["--pools", "2", "--threads", str(threads),
                "--cpus", two, "--numa", "0,0", "--affinity", "cluster", "--policy", policy]))
    cases += [("six-unbound-home", ["--pools", "6", "--threads", "8", "--affinity", "none", "--policy", "home"]),
              ("split-t16-queue", ["--pools", "2", "--threads", "16", "--cpus", two, "--numa", "0,0",
                                  "--affinity", "cluster", "--policy", "borrow", "--metric", "queue"])]
    return cases


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", default="build/executor_demo")
    p.add_argument("--output", default="results/runs")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--duration", type=float, default=6)
    p.add_argument("--qps", type=float, default=10)
    p.add_argument("--cpu-us", type=int, default=3000)
    p.add_argument("--fanout", type=int, default=20)
    p.add_argument("--seed", type=int, default=420)
    p.add_argument("--only", help="comma-separated case names or substring filters")
    p.add_argument("--cases-json", help="JSON array of {name,args} overrides local partitions for production")
    p.add_argument("--timeout-seconds", type=float, help="explicit process timeout; default effective duration+drain+40")
    p.add_argument("--proc-interval", type=float, default=.5, help="/proc sampling seconds; 0 disables external sampling")
    p.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="extra demo arguments, put last")
    a = p.parse_args()
    if a.repeats < 1 or a.duration <= 0 or a.proc_interval < 0:
        p.error("positive repeats and duration required")
    binary = str(pathlib.Path(a.binary).resolve())
    binary_hash = hashlib.sha256(pathlib.Path(binary).read_bytes()).hexdigest()
    root = pathlib.Path(a.output)
    if root.exists():
        p.error('matrix output already exists; choose a fresh directory')
    root.mkdir(parents=True)
    topology = capture()
    (root / "environment.json").write_text(json.dumps(topology, indent=2) + "\n")
    cases = local_cases(sorted(os.sched_getaffinity(0)))
    if a.cases_json:
        entries = json.loads(pathlib.Path(a.cases_json).read_text())
        cases = [(entry["name"], entry["args"]) for entry in entries]
    if a.only:
        patterns = a.only.split(",")
        cases = [(n, v) for n, v in cases if any(pattern in n for pattern in patterns)]
    if not cases:
        p.error("no matching cases")
    names = [name for name, _ in cases]
    if len(names) != len(set(names)) or any(not name or '/' in name or name in ('.', '..') for name in names):
        p.error('case names must be distinct single directory names')
    jobs = []
    for repeat in range(a.repeats):
        ordered = list(cases)
        random.Random(a.seed + repeat).shuffle(ordered)
        jobs += [(repeat, name, extra) for name, extra in ordered]
    manifest = []
    failures = 0
    for repeat, name, extra in jobs:
        destination = root / f"r{repeat}-{name}"
        if destination.exists():
            raise SystemExit(f"Refusing to overwrite {destination}; use a fresh output directory")
        destination.mkdir()
        command = [binary, "--output", str(destination), "--duration", str(a.duration),
                   "--qps", str(a.qps), "--cpu-us", str(a.cpu_us), "--fanout-min", str(a.fanout),
                   "--fanout-max", str(a.fanout), "--chain-length", "2", "--seed", str(a.seed + repeat),
                   "--max-inflight", "64", "--drain-seconds", "60", *extra, *a.extra]
        print("RUN", destination, flush=True)
        started = time.monotonic()
        effective = {"--duration": str(a.duration), "--drain-seconds": "60"}
        for index, option in enumerate(command[:-1]):
            if option in effective:
                effective[option] = command[index+1]
        timeout = a.timeout_seconds if a.timeout_seconds is not None else (
            float(effective["--duration"]) + float(effective["--drain-seconds"]) + 40)
        with (destination / "console.log").open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            sampler = subprocess.Popen([sys.executable, str(pathlib.Path(__file__).with_name("proc_sample.py")),
                "--pid", str(process.pid), "--workers", str(destination / "workers.csv"),
                "--output", str(destination / "proc.csv"), "--interval", str(a.proc_interval)]) if a.proc_interval else None
            try:
                rc = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                rc = process.wait()
            finally:
                if sampler:
                    try:
                        sampler.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        sampler.terminate()
                        sampler.wait()
        failures += int(rc != 0)
        manifest.append({"name": name, "repeat": repeat, "seed": a.seed+repeat,
                         "binary_sha256": binary_hash, "proc_interval_seconds": a.proc_interval,
                         "command": command, "returncode": rc,
                         "elapsed_s": time.monotonic()-started, "directory": str(destination)})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if rc:
            print(f"FAILED {destination}; see console.log", file=sys.stderr)
    valid = []
    for row in manifest:
        directory = pathlib.Path(row["directory"])
        if row["returncode"] != 0:
            continue
        try:
            summary = json.loads((directory/"summary.json").read_text())
            metadata = json.loads((directory/"metadata.json").read_text())
            complete = metadata.get("phase") == "completed" and summary.get("conservation_ok") and not summary.get("io_failed")
        except (OSError, ValueError):
            complete = False
        if complete:
            valid.append(str(directory))
        else:
            failures += 1
            print(f"INCOMPLETE {directory}; excluded from latency analysis", file=sys.stderr)
    if valid:
        subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("analyze.py")),
                        *valid, "--output", str(root/"analysis")], check=True)
        with (root/"analysis.md").open("a") as report:
            report.write(f"\n矩阵计划 {len(manifest)} 轮；有效完整运行 {len(valid)} 轮；异常/不完整 {len(manifest)-len(valid)} 轮。所有退出码与命令见 manifest.json。\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

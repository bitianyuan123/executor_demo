#!/usr/bin/env python3
"""Aggregate worker-only perf sched timehist --state output, including exited workers.

Input rows are rounded to 0.001 ms by perf. `wait_ms` includes sleeping;
`sched_delay_ms` is ready-to-run delay. Summed thread delay is NOT request latency.
This script reads existing observations only; it never runs or traces a workload.
"""
import argparse
import csv
from decimal import Decimal
import json
import math
import pathlib
import re
import statistics

ROW = re.compile(
    r"^\s*([0-9.]+)\s+\[(\d+)\]\s+\S+\[(\d+)/(\d+)\]\s+"
    r"([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+(\S+)"
)


def aggregate(run, timehist):
    with (run / "workers.csv").open() as stream:
        workers = {row["tid"]: {
            "tid": int(row["tid"]), "group": int(row["group"]),
            "sched_intervals": 0, "wait_ms": Decimal(0),
            "sched_delay_ms": Decimal(0), "runtime_ms": Decimal(0),
            "max_sched_delay_ms": Decimal(0),
        } for row in csv.DictReader(stream)}
    for line in timehist.read_text().splitlines():
        match = ROW.match(line)
        if not match:
            continue
        _, _, tid, _, wait, delay, runtime, _ = match.groups()
        if tid not in workers:
            raise ValueError(f"unrelated TID {tid} in supposedly worker-filtered input")
        row = workers[tid]
        row["sched_intervals"] += 1
        for name, value in (("wait_ms", wait), ("sched_delay_ms", delay), ("runtime_ms", runtime)):
            row[name] += Decimal(value)
        row["max_sched_delay_ms"] = max(row["max_sched_delay_ms"], Decimal(delay))
    missing = [tid for tid, row in workers.items() if not row["sched_intervals"]]
    if missing:
        raise ValueError(f"workers missing from timehist: {missing}; do not interpret missing as zero")
    with (run / "requests.csv").open() as stream:
        requests = list(csv.DictReader(stream))
    latencies = [float(row["latency_us"]) / 1000 for row in requests if row["status"] == "completed"]
    if not latencies:
        raise ValueError("no completed requests for normalization")
    with (run / "tasks.csv").open() as stream:
        tasks = list(csv.DictReader(stream))
    totals = {
        name: sum(row[name] for row in workers.values())
        for name in ("sched_intervals", "wait_ms", "sched_delay_ms", "runtime_ms")
    }
    totals.update(
        requests=len(latencies),
        latency_mean_ms=statistics.mean(latencies),
        latency_p99_ms=sorted(latencies)[math.ceil(0.99 * len(latencies)) - 1],
        sched_delay_ms_per_request=totals["sched_delay_ms"] / len(latencies),
        runtime_ms_per_request=totals["runtime_ms"] / len(latencies),
        logged_kernel_cpu_ms=sum(Decimal(row["cpu_us"]) for row in tasks) / 1000,
        max_sched_delay_ms=max(row["max_sched_delay_ms"] for row in workers.values()),
    )
    return {
        "run": str(run), "timehist": str(timehist), "workers": list(workers.values()), "totals": totals,
        "scope": "Worker scheduler intervals including startup/shutdown. Parallel thread delays overlap; sums per completed request are not critical-path latency. Input display resolution is 0.001 ms.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=pathlib.Path)
    parser.add_argument("timehist", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    result = aggregate(args.run, args.timehist)
    encoded = json.dumps(result, indent=2, default=lambda value: float(value) if isinstance(value, Decimal) else str(value))
    args.output.write_text(encoded + "\n")
    print(json.dumps(result["totals"], indent=2, default=float))

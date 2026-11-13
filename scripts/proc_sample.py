#!/usr/bin/env python3
"""Low-frequency thread schedstat sampler; never changes kernel settings.

runqueue_ns is kernel reported waiting-to-run time, not task queue delay.
A zero is NOT proof of no delay when schedstats is disabled/unavailable.
"""
import argparse
import csv
import os
import pathlib
import time


def read_thread(pid, tid):
    base = pathlib.Path(f"/proc/{pid}/task/{tid}")
    try:
        raw = (base / "schedstat").read_text().split()
        status = {}
        for line in (base / "status").read_text().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                status[key] = value.strip()
        return [int(raw[0]), int(raw[1]), int(raw[2]),
                int(status.get("voluntary_ctxt_switches", 0)),
                int(status.get("nonvoluntary_ctxt_switches", 0)),
                status.get("State", "?").split()[0], status.get("Cpus_allowed_list", "")]
    except (OSError, ValueError, IndexError):
        return None


def sample(pid, output, workers=None, interval=1.0):
    previous = {}
    started = time.monotonic_ns()
    with open(output, "w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["elapsed_s", "interval_s", "group", "tid", "cpu_ns", "runqueue_ns", "timeslices",
                         "voluntary_cs", "involuntary_cs", "state", "cpus_allowed", "schedstats_enabled"])
        last = started
        while pathlib.Path(f"/proc/{pid}").exists():
            now = time.monotonic_ns()
            mapping = {}
            if workers:
                try:
                    with open(workers) as f:
                        mapping = {int(row["tid"]): row["group"] for row in csv.DictReader(f)}
                except (OSError, ValueError, KeyError):
                    pass
            try:
                enabled = pathlib.Path("/proc/sys/kernel/sched_schedstats").read_text().strip()
            except OSError:
                enabled = "unknown"
            try:
                tids = os.listdir(f"/proc/{pid}/task")
            except FileNotFoundError:
                break
            for tid_text in tids:
                tid = int(tid_text)
                values = read_thread(pid, tid)
                if values is None:
                    continue
                prior = previous.get(tid)
                if prior:
                    deltas = [max(0, values[i] - prior[i]) for i in range(5)]
                    writer.writerow([(now-started)/1e9, (now-last)/1e9, mapping.get(tid, "other"), tid,
                                     *deltas, values[5], values[6], enabled])
                previous[tid] = values
            stream.flush()
            last = now
            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--workers")
    p.add_argument("--interval", type=float, default=1.0)
    a = p.parse_args()
    if a.pid <= 0 or a.interval < 0.01:
        p.error("positive PID and interval >= 0.01 seconds required")
    sample(a.pid, a.output, a.workers, a.interval)

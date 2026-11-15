#!/usr/bin/env python3
"""Stream ftrace/trace-cmd scheduler text; never reconstruct an unobserved runq."""
import argparse
import csv
import json
import math
import pathlib
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

EVENT = re.compile(r"\[(\d+)\].*?\s(\d+\.\d+):\s+(?:sched:)?(sched_\w+):\s*(.*)")
SWITCH = re.compile(r"prev_comm=(.*?)\s+prev_pid=(\d+)\s+prev_prio=\S+\s+prev_state=(\S+)\s+==>\s+next_comm=(.*?)\s+next_pid=(\d+)\s+next_prio=\S+")
WAKE = re.compile(r"comm=(.*?)\s+pid=(\d+)\s+prio=\S+(?:\s+success=(\d+))?\s+target_cpu=(\d+)")
MIGRATE = re.compile(r"comm=(.*?)\s+pid=(\d+)\s+prio=\S+\s+orig_cpu=(\d+)\s+dest_cpu=(\d+)")
SUPPORTED = {"sched_switch", "sched_waking", "sched_wakeup", "sched_wakeup_new", "sched_migrate_task"}
UNMAPPED = "[unmapped]"


def timestamp(value):
    whole, fraction = value.split(".")
    return int(whole) * 1_000_000_000 + int((fraction + "000000000")[:9])


def cpu_list(value):
    result = set()
    for part in value.split(","):
        limits = part.split("-")
        if len(limits) not in (1, 2):
            raise ValueError("CPU mask must be a decimal list, e.g. 0-47,192-239")
        low, high = int(limits[0]), int(limits[-1])
        if low < 0 or high < low or high > 65535:
            raise ValueError("invalid CPU range")
        result.update(range(low, high + 1))
    return result


def load_mapping(path, max_threads=100000, max_pools=64):
    result = {}
    if not path:
        return result
    with open(path, newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not {"tid", "pool"} <= set(reader.fieldnames or []):
            raise ValueError("TID map requires columns tid,pool")
        pools = set()
        for row in reader:
            tid, pool = int(row["tid"]), row["pool"].strip()
            if tid <= 0 or not pool or pool == UNMAPPED or tid in result:
                raise ValueError("invalid/duplicate TID or reserved pool name in map")
            result[tid] = pool
            pools.add(pool)
            if len(result) > max_threads or len(pools) > max_pools - 1:
                raise ValueError("mapping exceeds configured thread/pool bounds")
    return result


class Histogram:
    """Fixed 2049 counters; quantiles are upper edges, <= 2.2% bins."""
    def __init__(self):
        self.bins = [0] * 2049
        self.n = self.total = self.maximum = 0

    def add(self, value):
        index = 0 if value <= 1 else min(2048, math.ceil(math.log2(value) * 32))
        self.bins[index] += 1
        self.n += 1
        self.total += value
        self.maximum = max(self.maximum, value)

    def report(self):
        result = {"n": self.n, "mean_us": self.total / self.n / 1000 if self.n else None,
                  "max_us": self.maximum / 1000 if self.n else None,
                  "quantiles_are_histogram_upper_edges": True}
        for name, p in (("p50_us", .5), ("p95_us", .95), ("p99_us", .99)):
            rank, seen = math.ceil(self.n * p), 0
            result[name] = None
            if not rank:
                continue
            for i, count in enumerate(self.bins):
                seen += count
                if seen >= rank:
                    result[name] = min(self.maximum, 2 ** (i / 32)) / 1000
                    break
        return result


def hashed(tid):
    x = (tid + 0x9e3779b97f4a7c15) & ((1 << 64) - 1)
    x = ((x ^ (x >> 30)) * 0xbf58476d1ce4e5b9) & ((1 << 64) - 1)
    x = ((x ^ (x >> 27)) * 0x94d049bb133111eb) & ((1 << 64) - 1)
    return x ^ (x >> 31)


@dataclass
class Bin:
    waking: int = 0
    wakeup: int = 0
    oncpu_ns: int = 0
    waiting_ns: int = 0
    wake_wait_sum: int = 0
    wake_wait_n: int = 0
    registers: bytearray = field(default_factory=lambda: bytearray(64))
    exact_bits: int = 0
    exact_known: bool = True
    cpu_registers: bytearray = field(default_factory=lambda: bytearray(64))
    cpu_exact_bits: int = 0
    cpu_exact_known: bool = True

    def touch(self, tid, exact_index=None, oncpu=False):
        if oncpu:
            if exact_index is None:
                self.cpu_exact_known = False
            else:
                self.cpu_exact_bits |= 1 << exact_index
        elif exact_index is None:
            self.exact_known = False
        else:
            self.exact_bits |= 1 << exact_index
        x = hashed(tid)
        index, rest = x & 63, x >> 6
        registers = self.cpu_registers if oncpu else self.registers
        registers[index] = max(registers[index], 59 - rest.bit_length())

    def unique(self, oncpu=False):
        if self.cpu_exact_known if oncpu else self.exact_known:
            return (self.cpu_exact_bits if oncpu else self.exact_bits).bit_count()
        registers = self.cpu_registers if oncpu else self.registers
        zeros = registers.count(0)
        raw = .709 * 64 * 64 / sum(2.0 ** -x for x in registers)
        return 64 * math.log(64 / zeros) if zeros and raw <= 160 else raw


@dataclass
class Thread:
    tid: int
    name: str
    pool: str
    mapping: str
    events: int = 0
    pending: object = None  # (time, wake/runnable, target_cpu, ambiguous)
    on_cpu: object = None


class Pool:
    def __init__(self, widths):
        self.counts = Counter()
        self.oncpu_ns = self.waiting_ns = 0
        self.wake_wait = Histogram()
        self.runnable_wait = Histogram()
        self.oncpu = Histogram()
        self.bins = {width: {} for width in widths}


def correlation(pairs):
    n = len(pairs)
    if n < 20:
        return {"n": n, "pearson_r": None, "reason": "fewer_than_20_paired_windows"}
    sx = sum(x for x, _ in pairs) / n
    sy = sum(y for _, y in pairs) / n
    xx = sum((x - sx) ** 2 for x, _ in pairs)
    yy = sum((y - sy) ** 2 for _, y in pairs)
    return {"n": n, "pearson_r": sum((x - sx) * (y - sy) for x, y in pairs) / math.sqrt(xx * yy)
            if xx and yy else None, "reason": "descriptive_autocorrelated_windows" if xx and yy else "constant_series"}


def window_correlations(rows):
    """Compare existing complete windows; positive lag means source precedes target.

    Activity filtering is applied after pairing, without compressing the time axis.
    It conditions on either measurement being nonzero and can bias correlation;
    neither selection supplies independent samples or a causal estimate.
    """
    full, activity = {}, {}
    relationships = [("wakeup", target, (0, 1, 2, 5)) for target in
                     ("oncpu_cores", "unique_oncpu_tids", "observed_demand", "completed_wake_wait_mean_us")]
    relationships.extend(("waking", target, (0, 1, 2, 5))
                         for target in ("unique_oncpu_tids", "oncpu_cores"))
    relationships.append(("unique_oncpu_tids", "oncpu_cores", (0,)))
    for source, target, lags in relationships:
        for lag in lags:
            pairs = [(rows[i][source], rows[i + lag][target])
                     for i in range(max(0, len(rows) - lag))
                     if rows[i][source] is not None and rows[i + lag][target] is not None]
            key = f"{source}_to_{target}_lag_{lag}_bins"
            full[key] = correlation(pairs)
            activity[key] = correlation([(x, y) for x, y in pairs if x != 0 or y != 0])
    return full, activity


class Analyzer:
    def __init__(self, mapping=None, patterns=(), widths=(1, 5, 10, 30), start=0,
                 duration=None, cpu_mask=None, coverage="unknown", max_threads=100000,
                 max_bins=20000, max_pools=64, max_cells=400000):
        self.mapping = mapping or {}
        self.exact_indexes = {tid: i for i, tid in enumerate(sorted(self.mapping))} if len(self.mapping) <= 8192 else {}
        self.mapped_seen = 0
        self.patterns = [(name, re.compile(pattern)) for name, pattern in patterns]
        self.widths = tuple(sorted(set(round(float(x) * 1_000_000) for x in widths)))
        if not self.widths or min(self.widths) <= 0 or start < 0 or duration is not None and duration <= 0:
            raise ValueError("positive bin widths/duration and nonnegative start required")
        if min(max_threads, max_bins, max_pools, max_cells) < 1:
            raise ValueError("positive storage limits required")
        if len(self.mapping) > max_threads:
            raise ValueError("mapping exceeds max-threads")
        if len(set(self.mapping.values()) | {x[0] for x in patterns} | {UNMAPPED}) > max_pools:
            raise ValueError("too many pool names")
        self.start_offset = round(start * 1e9)
        self.duration = round(duration * 1e9) if duration is not None else None
        self.cpu_mask, self.coverage = cpu_mask, coverage
        self.max_threads, self.max_bins = max_threads, max_bins
        self.max_pools, self.max_cells = max_pools, max_cells
        self.threads, self.pools, self.running = {}, {}, {}
        self.audit, self.events = Counter(), Counter()
        self.record_cpus, self.target_cpus = set(), set()
        self.origin = self.start = self.limit = self.last = None
        self.last_in_window = self.cells = 0

    def get_thread(self, tid, name):
        if not tid:
            return None
        if tid in self.threads:
            t = self.threads[tid]
            if t.name != name:
                self.audit["tid_name_changes_or_reuse"] += 1
                # Pool membership is never silently remapped by a rename.
                t.name = name
            return t
        if len(self.threads) >= self.max_threads or (tid not in self.mapping and
                len(self.threads) - self.mapped_seen >= self.max_threads - len(self.mapping)):
            self.audit["thread_limit_events_skipped"] += 1
            return None
        pool, source = self.mapping.get(tid, UNMAPPED), "csv" if tid in self.mapping else "unmapped"
        if source == "unmapped":
            matches = [label for label, pattern in self.patterns if pattern.search(name)]
            if len(matches) == 1:
                pool, source = matches[0], "heuristic_name"
            elif len(matches) > 1:
                self.audit["ambiguous_name_matches"] += 1
        t = self.threads[tid] = Thread(tid, name, pool, source)
        self.mapped_seen += source == "csv"
        if pool not in self.pools:
            self.pools[pool] = Pool(self.widths)
        return t

    def bin(self, pool, width, index):
        if index < 0 or index >= self.max_bins:
            return None
        bins = self.pools[pool].bins[width]
        if index not in bins:
            if self.cells >= self.max_cells:
                self.audit["window_storage_limit_updates_skipped"] += 1
                return None
            bins[index] = Bin()
            self.cells += 1
        return bins[index]

    def event(self, t, kind, at):
        if t is None:
            return
        t.events += 1
        if at < self.start or at >= self.limit:
            return
        self.pools[t.pool].counts[kind] += 1
        for width in self.widths:
            cell = self.bin(t.pool, width, (at - self.start) // width)
            if cell:
                cell.touch(t.tid, self.exact_indexes.get(t.tid))
                if kind == "sched_waking":
                    cell.waking += 1
                if kind in ("sched_wakeup", "sched_wakeup_new"):
                    cell.wakeup += 1

    def interval(self, t, begin, end, kind):
        left, right = max(begin, self.start), min(end, self.limit)
        if right <= left:
            return
        pool = self.pools[t.pool]
        if kind == "oncpu":
            pool.oncpu_ns += right - left
        else:
            pool.waiting_ns += right - left
        if begin >= self.start and end <= self.limit:
            hist = pool.oncpu if kind == "oncpu" else pool.wake_wait if kind == "wake" else pool.runnable_wait
            hist.add(end - begin)
        else:
            self.audit[kind + "_window_clipped_intervals"] += 1
        for width in self.widths:
            for index in range((left - self.start) // width, (right - 1 - self.start) // width + 1):
                cell = self.bin(t.pool, width, index)
                if not cell:
                    continue
                overlap = min(right, self.start + (index + 1) * width) - max(left, self.start + index * width)
                if kind == "oncpu":
                    cell.oncpu_ns += overlap
                else:
                    cell.waiting_ns += overlap
                if kind == "oncpu":
                    cell.touch(t.tid, self.exact_indexes.get(t.tid), oncpu=True)
            if kind == "wake" and begin >= self.start and end < self.limit:
                cell = self.bin(t.pool, width, (end - self.start) // width)
                if cell:
                    cell.wake_wait_sum += end - begin
                    cell.wake_wait_n += 1

    def invalidate(self, reason):
        self.audit[reason] += 1
        self.audit["oncpu_anchors_invalidated"] += len(self.running)
        self.running.clear()
        for t in self.threads.values():
            if t.pending:
                self.audit["wait_anchors_invalidated"] += 1
            t.pending = t.on_cpu = None

    def consume(self, line):
        self.audit["lines"] += 1
        if re.search(r"\bLOST\b.*\bEVENTS?\b", line, re.I):
            self.invalidate("lost_event_markers")
            return
        header = re.search(r"entries-in-buffer/entries-written:\s*(\d+)/(\d+)", line)
        if header and int(header[2]) > int(header[1]):
            self.audit["header_overwritten_entries"] += int(header[2]) - int(header[1])
        match = EVENT.search(line)
        if not match:
            if any(event + ":" in line for event in SUPPORTED):
                self.invalidate("malformed_scheduler_lines")
            return
        cpu, at, kind, body = int(match[1]), timestamp(match[2]), match[3], match[4]
        if kind not in SUPPORTED:
            return
        if cpu > 65535:
            self.invalidate("invalid_cpu_records")
            return
        if self.origin is None:
            self.origin, self.start = at, at + self.start_offset
            horizon = self.max_bins * min(self.widths)
            self.limit = self.start + min(horizon, self.duration if self.duration is not None else horizon)
        if self.last is not None and at < self.last:
            self.invalidate("out_of_order_events_dropped")
            return
        self.last = at
        if at >= self.limit:
            self.audit["events_after_window"] += 1
        self.record_cpus.add(cpu)
        if self.cpu_mask is not None and cpu not in self.cpu_mask:
            self.audit["record_cpu_outside_declared_mask"] += 1
        self.last_in_window = max(at, self.last_in_window)
        if self.start <= at < self.limit:
            self.events[kind] += 1
        if kind == "sched_switch":
            fields = SWITCH.search(body)
            if not fields:
                self.invalidate("malformed_scheduler_lines")
                return
            prev_name, prev_tid, state, next_name, next_tid = fields.groups()
            previous = self.get_thread(int(prev_tid), prev_name)
            following = self.get_thread(int(next_tid), next_name)
            anchor = self.running.pop(cpu, None)
            if anchor and anchor[0] == int(prev_tid):
                t = self.threads.get(anchor[0])
                if t:
                    self.interval(t, anchor[1], at, "oncpu")
                    t.on_cpu = None
            else:
                self.audit["unmatched_switch_out"] += int(bool(int(prev_tid)))
                if anchor:
                    self.audit["cpu_switch_chain_mismatch"] += 1
                    t = self.threads.get(anchor[0])
                    if t:
                        t.on_cpu = None
            if previous:
                self.event(previous, "switch_out", at)
                if previous.pending:
                    self.audit["switch_out_with_missing_switch_in"] += 1
                previous.pending = None
                if state in ("R", "R+", "0"):
                    previous.pending = (at, "runnable", cpu, False)
                    if self.start <= at < self.limit:
                        self.pools[previous.pool].counts["runnable_switch_out"] += 1
                    if "+" in state and self.start <= at < self.limit:
                        self.pools[previous.pool].counts["explicit_preemption_R_plus"] += 1
                elif self.start <= at < self.limit:
                    self.pools[previous.pool].counts["nonrunnable_switch_out"] += 1
            if following:
                self.event(following, "switch_in", at)
                if following.on_cpu is not None:
                    self.running.pop(following.on_cpu, None)
                    self.audit["double_cpu_anchor_invalidated"] += 1
                if following.pending:
                    begin, reason, target, ambiguous = following.pending
                    if target != cpu:
                        self.audit["wait_target_cpu_mismatch_censored"] += 1
                    elif not ambiguous:
                        self.interval(following, begin, at, reason)
                    else:
                        self.audit["ambiguous_wait_samples_excluded"] += 1
                    following.pending = None
                else:
                    self.audit["switch_in_without_wait_anchor"] += 1
                following.on_cpu = cpu
                self.running[cpu] = (following.tid, at)
            return
        fields = (MIGRATE if kind == "sched_migrate_task" else WAKE).search(body)
        if not fields:
            self.invalidate("malformed_scheduler_lines")
            return
        if kind == "sched_migrate_task":
            name, tid, origin, destination = fields.groups()
            origin, target = int(origin), int(destination)
            if max(origin, target) > 65535:
                self.invalidate("invalid_cpu_records")
                return
            t = self.get_thread(int(tid), name)
            self.event(t, kind, at)
            self.target_cpus.add(target)
            if t and t.on_cpu is not None:
                self.running.pop(t.on_cpu, None)
                t.on_cpu = None
                self.audit["migration_invalidated_running_anchor"] += 1
            if t and t.pending:
                begin, reason, _, ambiguous = t.pending
                if self.cpu_mask is None or origin not in self.cpu_mask or target not in self.cpu_mask:
                    t.pending = None
                    self.audit["migration_wait_coverage_censored"] += 1
                else:
                    t.pending = (begin, reason, target, ambiguous)
            return
        name, tid, success, target = fields.groups()
        target = int(target)
        if target > 65535:
            self.invalidate("invalid_cpu_records")
            return
        t = self.get_thread(int(tid), name)
        self.target_cpus.add(target)
        if success == "0":
            self.audit["explicit_unsuccessful_wake"] += 1
            return
        self.event(t, kind, at)
        if kind == "sched_waking" or t is None:
            return
        if self.cpu_mask is not None and target not in self.cpu_mask:
            t.pending = None
            self.audit["wake_target_outside_mask_censored"] += 1
        elif t.on_cpu is not None:
            self.audit["wake_while_observed_running"] += 1
            self.running.pop(t.on_cpu, None)
            t.on_cpu = None
            t.pending = (at, "wake", target, True)
        else:
            ambiguous = bool(t.pending)
            if ambiguous:
                self.audit["duplicate_wake_or_missing_execution"] += 1
            t.pending = (at, "wake", target, ambiguous)

    def finish(self, include_windows=False):
        end = min(self.limit, self.last + 1) if self.origin is not None else 0
        start = self.start or 0
        elapsed = max(0, end - start)
        self.audit["right_censored_oncpu_anchors"] = sum(anchor[1] < self.limit for anchor in self.running.values()) if self.limit else 0
        self.audit["right_censored_wait_anchors"] = sum(bool(t.pending and t.pending[0] < self.limit) for t in self.threads.values()) if self.limit else 0
        reports = {}
        incomplete_windows = bool(self.audit["window_storage_limit_updates_skipped"])
        output_cells = len(self.pools) * sum(min(self.max_bins, math.ceil(elapsed / width)) for width in self.widths)
        omit_series = include_windows and output_cells > self.max_cells
        for label, pool in sorted(self.pools.items()):
            result = {"events": dict(pool.counts), "matched_oncpu_ms": pool.oncpu_ns / 1e6,
                      "matched_waiting_ms": pool.waiting_ns / 1e6,
                      "oncpu_core_equivalents": pool.oncpu_ns / elapsed if elapsed else None,
                      "observed_demand_equivalents": (pool.oncpu_ns + pool.waiting_ns) / elapsed if elapsed else None,
                      "wake_to_run": pool.wake_wait.report(), "runnable_switch_to_run": pool.runnable_wait.report(),
                      "oncpu_intervals": pool.oncpu.report(), "windows": {}}
            for width in self.widths:
                n = min(self.max_bins, math.ceil(elapsed / width))
                rows = []
                for index in range(n):
                    b = pool.bins[width].get(index)
                    span = min(width, elapsed - index * width)
                    rows.append({"start_ms": index * width / 1e6, "span_ms": span / 1e6,
                                 "waking": b.waking if b else 0, "wakeup": b.wakeup if b else 0,
                                 "unique_event_tids": b.unique() if b else 0,
                                 "unique_oncpu_tids": b.unique(True) if b else 0,
                                 "unique_event_method": "exact_csv_bitset" if b and b.exact_known else "hll_64" if b else "empty",
                                 "unique_oncpu_method": "exact_csv_bitset" if b and b.cpu_exact_known else "hll_64" if b else "empty",
                                 "oncpu_cores": b.oncpu_ns / span if b else 0,
                                 "observed_demand": (b.oncpu_ns + b.waiting_ns) / span if b else 0,
                                 "completed_wake_wait_mean_us": b.wake_wait_sum / b.wake_wait_n / 1000 if b and b.wake_wait_n else None})
                # Partial last windows and storage-truncated runs are excluded.
                full = [r for r in rows if r["span_ms"] == width / 1e6]
                corrs, activity_corrs = {}, {}
                if not incomplete_windows:
                    corrs, activity_corrs = window_correlations(full)
                window = {"n": n, "incomplete_storage": incomplete_windows,
                          "peak_waking": max((r["waking"] for r in rows), default=0),
                          "peak_wakeup": max((r["wakeup"] for r in rows), default=0),
                          "peak_oncpu_cores": max((r["oncpu_cores"] for r in rows), default=0),
                          "peak_observed_demand": max((r["observed_demand"] for r in rows), default=0),
                          "peak_unique_event_tids": max((r["unique_event_tids"] for r in rows), default=0),
                          "peak_unique_oncpu_tids": max((r["unique_oncpu_tids"] for r in rows), default=0),
                          "unique_event_methods": sorted({r["unique_event_method"] for r in rows} - {"empty"}),
                          "unique_oncpu_methods": sorted({r["unique_oncpu_method"] for r in rows} - {"empty"}),
                          "top_wakeup_windows": sorted(rows, key=lambda x: x["wakeup"], reverse=True)[:3],
                          "correlations": corrs, "activity_correlations": activity_corrs}
                window["distributions"] = {}
                for metric in ("waking", "wakeup", "unique_event_tids", "unique_oncpu_tids", "oncpu_cores", "observed_demand"):
                    values = sorted(r[metric] for r in full)
                    window["distributions"][metric] = {
                        f"p{round(p * 100)}": values[math.ceil(len(values) * p) - 1] if values else None
                        for p in (.5, .95, .99)}
                if include_windows and not omit_series:
                    window["series"] = rows
                result["windows"][str(width / 1e6)] = window
            reports[label] = result
        warnings = ["Matched intervals only; unclosed/invalidated spans are omitted, never filled as a complete runq.",
                    "sched_waking counts attempts, not successful wakes, unique threads, tasks, or requests.",
                    "CSV maps <=8192 TIDs use exact TID bitsets; other bins use HLL estimates (~13% standard error). Event and matched-onCPU unique sets are separate.",
                    "Fixed windows are trace-anchored, not sliding maxima; correlations have dependent samples and are not causal.",
                    "Activity correlations keep paired windows where either variable is nonzero; this selection can bias r and does not create independent samples. Lag pairs retain the original time axis.",
                    "TID maps must belong to this capture; exits/TID reuse and missing filters cannot be ruled out from these five events."]
        if self.coverage != "full":
            warnings.append("CPU/event coverage not asserted complete: waits and demand may miss execution or wake episodes.")
        if self.patterns:
            warnings.append("Thread-name mappings are heuristic; names may be truncated, reused, or changed.")
        if omit_series:
            warnings.append("JSON window series omitted because expanded zero-filled series exceeds max-window-cells.")
        if self.target_cpus - self.record_cpus:
            warnings.append("Some destination CPUs have no records; silence does not prove those CPUs were captured.")
        if self.audit["header_overwritten_entries"]:
            warnings.append("Ring-buffer header reports overwritten history; retained text may be shorter than the intended capture.")
        if self.audit["record_cpu_outside_declared_mask"]:
            warnings.append("Declared mask conflicts with recorded CPUs; records were retained, not filtered.")
        if self.last is not None and self.last >= self.limit and (self.duration is None or self.duration > self.max_bins * min(self.widths)):
            warnings.append("Analysis horizon reached max-bins limit; later events were streamed but excluded.")
        if any(self.audit[x] for x in ("lost_event_markers", "malformed_scheduler_lines", "out_of_order_events_dropped", "cpu_switch_chain_mismatch")):
            warnings.append("Event integrity gaps found: interval anchors were conservatively discarded; paired waits are selected survivors.")
        return {"schema": "sched-text-v1", "coverage_declared": self.coverage,
                "origin_timestamp_ns": self.origin, "analysis_start_timestamp_ns": start,
                "analysis_end_timestamp_ns": end, "duration_ms": elapsed / 1e6,
                "declared_cpu_mask": sorted(self.cpu_mask) if self.cpu_mask is not None else None,
                "event_record_cpus": sorted(self.record_cpus), "destination_cpus": sorted(self.target_cpus),
                "events_in_window": dict(self.events), "audit": dict(self.audit), "warnings": warnings,
                "storage": {"tracked_threads": len(self.threads), "window_cells": self.cells,
                            "max_threads": self.max_threads, "max_bins_per_width": self.max_bins,
                            "max_window_cells": self.max_cells},
                "pools": reports,
                "thread_candidates": [{"tid": t.tid, "name": t.name, "pool": t.pool, "mapping": t.mapping, "events": t.events}
                                      for t in sorted(self.threads.values(), key=lambda t: (-t.events, t.tid))]}


def print_report(report, candidates=20):
    print("Scheduler text analysis; matched/observed intervals only")
    print(f"window_ms={report['duration_ms']:.3f} coverage={report['coverage_declared']} event_record_cpus={report['event_record_cpus']}")
    print("events=" + json.dumps(report["events_in_window"], sort_keys=True))
    print("audit=" + json.dumps({k: v for k, v in report["audit"].items() if v}, sort_keys=True))
    for name, p in report["pools"].items():
        print(f"POOL {name}: oncpu_ms={p['matched_oncpu_ms']:.3f} oncpu_cores={p['oncpu_core_equivalents']} observed_demand={p['observed_demand_equivalents']}")
        for label in ("wake_to_run", "runnable_switch_to_run"):
            h = p[label]
            print(f"  {label}: n={h['n']} mean_us={h['mean_us']} p50/p95/p99_us~={h['p50_us']}/{h['p95_us']}/{h['p99_us']} max_us={h['max_us']}")
        for width, window in p["windows"].items():
            print(f"  bin={width}ms peak waking/wakeup={window['peak_waking']}/{window['peak_wakeup']} unique_event={window['peak_unique_event_tids']:.1f} unique_oncpu={window['peak_unique_oncpu_tids']:.1f} unique_oncpu_methods={window['unique_oncpu_methods']} oncpu={window['peak_oncpu_cores']:.3f} demand={window['peak_observed_demand']:.3f}")
            print("    p50/p95/p99 wakeup=" + str(window["distributions"]["wakeup"]) + " demand=" + str(window["distributions"]["observed_demand"]))
            print("    top_wakeup_start_ms=" + str([(r["start_ms"], r["wakeup"]) for r in window["top_wakeup_windows"]]))
            for selection, key in (("full", "correlations"), ("activity", "activity_correlations")):
                parts = []
                for source, target, label in (("waking", "unique_oncpu_tids", "waking->unique_oncpu"),
                                              ("waking", "oncpu_cores", "waking->oncpu"),
                                              ("unique_oncpu_tids", "oncpu_cores", "unique_oncpu->oncpu"),
                                              ("wakeup", "oncpu_cores", "wakeup->oncpu")):
                    corr = window[key].get(f"{source}_to_{target}_lag_0_bins")
                    if corr is None:
                        parts.append(f"{label}: unavailable(incomplete_storage)")
                    elif corr["pearson_r"] is None:
                        parts.append(f"{label}: r=undefined n={corr['n']} ({corr['reason']})")
                    else:
                        parts.append(f"{label}: r={corr['pearson_r']:.3f} n={corr['n']}")
                print(f"    correlation lag=0bins {selection}: " + "; ".join(parts))
            for lag in (0, 1, 2, 5):
                corr = window["correlations"].get(f"wakeup_to_completed_wake_wait_mean_us_lag_{lag}_bins")
                if corr and corr["pearson_r"] is not None:
                    print(f"    wakeup->completed_wait lag={lag}bins r={corr['pearson_r']:.3f} n={corr['n']} descriptive")
    print("THREAD CANDIDATES (not inferred pools):")
    for t in report["thread_candidates"][:candidates]:
        print(f"  tid={t['tid']} pool={t['pool']} mapping={t['mapping']} events={t['events']} name={t['name']!r}")
    for warning in report["warnings"]:
        print("NOTE: " + warning)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", help="ftrace text path, or - for trace-cmd report stdin")
    parser.add_argument("--tid-map")
    parser.add_argument("--name-pattern", action="append", default=[], metavar="POOL=REGEX")
    parser.add_argument("--start-seconds", type=float, default=0)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--bin-ms", default="1,5,10,30")
    parser.add_argument("--cpu-mask", help="known capture CPU list; not a hex tracing_cpumask")
    parser.add_argument("--coverage", choices=("unknown", "partial", "full"), default="unknown")
    parser.add_argument("--max-threads", type=int, default=100000)
    parser.add_argument("--max-bins", type=int, default=20000)
    parser.add_argument("--max-window-cells", type=int, default=400000)
    parser.add_argument("--candidate-limit", type=int, default=20)
    parser.add_argument("--json", metavar="OUTPUT")
    parser.add_argument("--include-windows", action="store_true", help="include bounded window series in JSON")
    args = parser.parse_args(argv)
    try:
        patterns = [item.split("=", 1) for item in args.name_pattern]
        if any(len(p) != 2 or not p[0] or p[0] == UNMAPPED for p in patterns):
            raise ValueError("name-pattern must be POOL=REGEX")
        analyzer = Analyzer(load_mapping(args.tid_map, args.max_threads), patterns,
                            [float(x) for x in args.bin_ms.split(",")], args.start_seconds,
                            args.duration_seconds, cpu_list(args.cpu_mask) if args.cpu_mask else None,
                            args.coverage, args.max_threads, args.max_bins, max_cells=args.max_window_cells)
        stream = sys.stdin if args.trace == "-" else open(args.trace, encoding="utf-8", errors="replace")
        try:
            for line in stream:
                analyzer.consume(line)
        finally:
            if stream is not sys.stdin:
                stream.close()
        report = analyzer.finish(args.include_windows)
        print_report(report, args.candidate_limit)
        if args.json:
            output = pathlib.Path(args.json)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", encoding="utf-8") as out:
                json.dump(report, out, ensure_ascii=False, indent=2)
                out.write("\n")
        if not report["events_in_window"]:
            print("No supported scheduler events in analysis window", file=sys.stderr)
            return 2
    except (OSError, ValueError, re.error) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())

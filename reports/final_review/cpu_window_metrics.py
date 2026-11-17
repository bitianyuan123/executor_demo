"""Recompute the 4021 CPU-window comparisons with the standard library.

``load_rounds(path)`` reads a CSV containing ``round_id`` and CPU_SERIES fields,
returning ``{round_id: [row, ...]}`` without silently dropping empty cells.
``derive(rows, cpu_count_per_cluster=16, numa_groups=((0,1,2),(3,4,5)))``
returns:

* row_count/window_count/valid_window_count/invalid_window_count, invalid_reasons;
* first_start_ms/last_end_ms, observed_seconds/valid_seconds;
* global_mean_percent (all selected logical CPUs) and global_mean_cpu_equiv;
* max_cluster_util_percent: duration-weighted mean plus nearest-rank
  p50/p95/p99/max of each window's maximum cluster utilization;
* mean_cluster_span_pp, mean_cluster_std_pp and mean_local_span_pp: duration-
  weighted inter-cluster range, population standard deviation, and the mean
  of the ranges within the explicitly supplied NUMA groups;
* interval_ms and sample_lag_ms: min/p50/p95/p99/max over distinct windows;
* any_cluster_above_percent: fraction of valid windows whose largest cluster
  utilization exceeds 50, 70 or 80 percent (unweighted window counts).

The default topology describes this six-cluster experiment, not the scheduler
architecture. Callers can supply other equal-capacity clusters and NUMA groups.
A missing/duplicate cluster, invalid row or nonfinite CPU value invalidates the
whole simultaneous window. No interpolation, capping, clipping or zero filling
is performed. Process CPU is deliberately not aggregated here: its read bounds
are different and are unavailable in printed CPU_SERIES; use CPU_SCOPE.process.
Percentiles are window percentiles, not request latency or 1 ms CPU peaks.
"""
from __future__ import annotations

import collections
import csv
import math
import statistics


def _number(value):
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid(value):
    return value is True or value == 1 or str(value).strip().lower() in ("1", "true")


def _quantile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def _distribution(values):
    return {"min": min(values) if values else None,
            "p50": _quantile(values, .5), "p95": _quantile(values, .95),
            "p99": _quantile(values, .99), "max": max(values) if values else None}


def load_rounds(csv_path):
    """Read a CSV once and retain rows grouped by the explicit round_id."""
    result = {}
    with open(csv_path, newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "round_id" not in reader.fieldnames:
            raise ValueError("CPU series CSV requires round_id")
        for row in reader:
            if not row["round_id"]:
                raise ValueError("CPU series row has no round_id")
            result.setdefault(row["round_id"], []).append(row)
    return result


def derive(rows, cpu_count_per_cluster=16, numa_groups=((0, 1, 2), (3, 4, 5))):
    """Aggregate simultaneous complete CPU windows; see module field contract."""
    rows = list(rows)
    groups = [tuple(group) for group in numa_groups]
    clusters = [cluster for group in groups for cluster in group]
    if (not groups or any(not group for group in groups) or
            len(clusters) != len(set(clusters)) or cpu_count_per_cluster <= 0):
        raise ValueError("NUMA groups must contain distinct clusters with positive capacity")
    expected = set(clusters)
    windows = collections.defaultdict(list)
    for row in rows:
        start = _number(row.get("relative_start_ms"))
        end = _number(row.get("relative_end_ms"))
        if start is None or end is None:
            raise ValueError("CPU row lacks finite window bounds")
        windows[(start, end)].append(row)
    invalid = collections.Counter()
    values = []
    intervals, lags = [], []
    observed_seconds = 0.0
    last_end = None
    for (start, end), members in sorted(windows.items()):
        if end <= start:
            invalid["nonpositive_window"] += 1
            continue
        if last_end is not None and start < last_end - 1e-6:
            raise ValueError("CPU windows overlap; cannot time-weight them")
        last_end = end
        seconds = (end - start) / 1000
        observed_seconds += seconds
        intervals.append(end - start)
        window_lags = {_number(row.get("sample_lag_ms")) for row in members}
        if len(window_lags) == 1 and None not in window_lags:
            lags.append(window_lags.pop())
        members_by_cluster = {}
        duplicate = False
        for row in members:
            cluster = _number(row.get("cluster"))
            if cluster in members_by_cluster:
                duplicate = True
            members_by_cluster[cluster] = row
        if duplicate or set(members_by_cluster) != expected:
            invalid["missing_or_duplicate_cluster"] += 1
            continue
        if not all(_valid(row.get("valid")) for row in members):
            invalid["invalid_cpu_row"] += 1
            continue
        util = {cluster: _number(row.get("util_percent"))
                for cluster, row in members_by_cluster.items()}
        busy = [_number(row.get("busy_cpu_equivalent")) for row in members]
        if any(value is None or value < 0 for value in list(util.values()) + busy):
            invalid["missing_or_invalid_cpu_value"] += 1
            continue
        u = list(util.values())
        values.append({"seconds": seconds, "busy": sum(busy), "maximum": max(u),
                       "span": max(u) - min(u), "std": statistics.pstdev(u),
                       "local_span": statistics.mean(
                           max(util[c] for c in group) - min(util[c] for c in group)
                           for group in groups)})
    valid_seconds = sum(value["seconds"] for value in values)

    def weighted(field):
        return (sum(value[field] * value["seconds"] for value in values) / valid_seconds
                if valid_seconds else None)

    maximum = [value["maximum"] for value in values]
    maximum_stats = _distribution(maximum)
    maximum_stats.pop("min")
    maximum_stats["mean"] = weighted("maximum")
    global_busy = weighted("busy")
    return {
        "row_count": len(rows), "window_count": len(windows),
        "valid_window_count": len(values), "invalid_window_count": sum(invalid.values()),
        "invalid_reasons": dict(invalid),
        "first_start_ms": min((key[0] for key in windows), default=None),
        "last_end_ms": max((key[1] for key in windows), default=None),
        "observed_seconds": observed_seconds, "valid_seconds": valid_seconds,
        "global_mean_cpu_equiv": global_busy,
        "global_mean_percent": (global_busy * 100 / (len(clusters) * cpu_count_per_cluster)
                                if global_busy is not None else None),
        "max_cluster_util_percent": maximum_stats,
        "mean_cluster_span_pp": weighted("span"),
        "mean_local_span_pp": weighted("local_span"),
        "mean_cluster_std_pp": weighted("std"),
        "interval_ms": _distribution(intervals), "sample_lag_ms": _distribution(lags),
        "any_cluster_above_percent": {
            str(threshold): 100 * sum(value > threshold for value in maximum) / len(maximum)
            if maximum else None for threshold in (50, 70, 80)},
    }

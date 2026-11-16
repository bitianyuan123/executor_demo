#!/usr/bin/env python3
"""Summarize existing sched-text-v1 window JSON without reading the raw trace."""
import argparse
import json
import math
import pathlib
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation


MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_POOLS = 64
MAX_ROWS = 400000


class InvalidReport(ValueError):
    pass


class UnavailableReport(InvalidReport):
    pass


def finite_number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidReport(f"{label}: expected a finite number")
    try:
        valid = math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        valid = False
    if not valid:
        raise InvalidReport(f"{label}: expected a finite {'positive' if positive else 'nonnegative'} number")
    return value


def milliseconds_ns(value, label, positive=False):
    finite_number(value, label, positive)
    scaled = Decimal(str(value)) * 1000000
    if scaled != scaled.to_integral_value():
        raise InvalidReport(f"{label}: must resolve to integer nanoseconds")
    return int(scaled)


def integer(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise InvalidReport(f"{label}: expected a {'positive' if positive else 'nonnegative'} integer")
    return value


def load_report(path, max_bytes=MAX_INPUT_BYTES):
    if max_bytes <= 0 or max_bytes > MAX_INPUT_BYTES:
        raise InvalidReport("input size limit must be positive and at most 128 MiB")
    path = pathlib.Path(path)
    if path.stat().st_size > max_bytes:
        raise InvalidReport(f"input exceeds size limit ({max_bytes} bytes)")
    # Bound the read too: the file may grow after stat().
    with path.open("rb") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise InvalidReport(f"input exceeds size limit ({max_bytes} bytes)")
    try:
        def reject_constant(value):
            raise ValueError(f"non-finite JSON constant {value}")
        return json.loads(raw, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidReport(f"invalid JSON: {error}") from error


def quantile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def distribution(values):
    return {"mean": math.fsum(values) / len(values), "p50": quantile(values, .5),
            "p90": quantile(values, .9), "p99": quantile(values, .99), "max": max(values)}


def high_segments(flags, width_ms):
    """Consecutive high *bins*, including censored edge runs; not request bursts."""
    lengths, edge_count, begin = [], 0, None
    for index in range(len(flags) + 1):
        if index < len(flags) and flags[index]:
            if begin is None:
                begin = index
        elif begin is not None:
            lengths.append((index - begin) * width_ms)
            edge_count += begin == 0 or index == len(flags)
            begin = None
    return {"n": len(lengths), "p50_ms": quantile(lengths, .5),
            "p95_ms": quantile(lengths, .95), "max_ms": max(lengths) if lengths else None,
            "edge_censored_segments": edge_count}


def analyze(report, bin_ms=1, multiples=(2, 4), pools=None):
    if not isinstance(report, dict) or report.get("schema") != "sched-text-v1":
        raise InvalidReport("expected schema=sched-text-v1")
    width_ns = milliseconds_ns(bin_ms, "bin-ms", positive=True)
    if not multiples or len(multiples) > 8 or len(set(multiples)) != len(multiples):
        raise InvalidReport("high-multiples requires 1..8 distinct positive values")
    for multiple in multiples:
        finite_number(multiple, "high-multiple", positive=True)
    start = integer(report.get("analysis_start_timestamp_ns"), "analysis_start_timestamp_ns")
    end = integer(report.get("analysis_end_timestamp_ns"), "analysis_end_timestamp_ns")
    origin = integer(report.get("origin_timestamp_ns"), "origin_timestamp_ns")
    if origin > start or end <= start:
        raise InvalidReport("invalid absolute analysis range/origin")
    elapsed = end - start
    if milliseconds_ns(report.get("duration_ms"), "duration_ms", positive=True) != elapsed:
        raise InvalidReport("duration_ms disagrees with absolute analysis range")
    full_count, remainder = divmod(elapsed, width_ns)
    expected_rows = full_count + bool(remainder)
    if not full_count:
        raise UnavailableReport("no complete windows at the requested bin width")
    if expected_rows > MAX_ROWS:
        raise InvalidReport("window count exceeds bounded analysis limit")
    if report.get("coverage_declared") not in ("unknown", "partial", "full"):
        raise InvalidReport("invalid or missing coverage_declared")
    audit = report.get("audit", {})
    if not isinstance(audit, dict):
        raise InvalidReport("audit must be an object")
    if audit.get("window_storage_limit_updates_skipped", 0):
        raise UnavailableReport("window storage updates were skipped")
    available = report.get("pools")
    if not isinstance(available, dict):
        raise InvalidReport("missing pools object")
    selected = sorted(name for name in available if name != "[unmapped]") if pools is None else list(pools)
    if not selected or len(selected) > MAX_POOLS or len(set(selected)) != len(selected):
        raise InvalidReport("select 1..64 distinct pools")
    if any(not isinstance(name, str) or not name or name not in available for name in selected):
        raise InvalidReport("unknown or invalid selected pool")
    if expected_rows * len(selected) > MAX_ROWS:
        raise InvalidReport("selected pool windows exceed bounded analysis limit")
    series = {}
    for name in selected:
        pool = available[name]
        if not isinstance(pool, dict) or not isinstance(pool.get("windows"), dict):
            raise InvalidReport(f"pool {name!r}: missing windows")
        matches = []
        for key, window in pool["windows"].items():
            try:
                candidate = Decimal(key) * 1000000
            except (InvalidOperation, TypeError):
                raise InvalidReport(f"pool {name!r}: invalid window width key") from None
            if not candidate.is_finite() or candidate <= 0 or candidate != candidate.to_integral_value():
                raise InvalidReport(f"pool {name!r}: invalid window width key")
            if candidate == width_ns:
                matches.append(window)
        if len(matches) != 1:
            raise UnavailableReport(f"pool {name!r}: expected one {bin_ms:g} ms window series")
        window = matches[0]
        if not isinstance(window, dict) or window.get("incomplete_storage") is not False:
            raise UnavailableReport(f"pool {name!r}: incomplete/unknown window storage")
        rows = window.get("series")
        if not isinstance(rows, list):
            raise UnavailableReport(f"pool {name!r}: no series; original JSON needs --include-windows")
        if integer(window.get("n"), f"pool {name!r} window n") != expected_rows or len(rows) != expected_rows:
            raise UnavailableReport(f"pool {name!r}: truncated/missing windows; refusing to fill as idle")
        oncpu, demand = [], []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise InvalidReport(f"pool {name!r}, window {index}: expected object")
            label = f"pool {name!r}, window {index}"
            expected_span = min(width_ns, elapsed - index * width_ns)
            if (milliseconds_ns(row.get("start_ms"), label + " start_ms") != index * width_ns or
                    milliseconds_ns(row.get("span_ms"), label + " span_ms", positive=True) != expected_span):
                raise InvalidReport(f"{label}: misaligned start/span or absolute range mismatch")
            cpu = finite_number(row.get("oncpu_cores"), label + " oncpu_cores")
            observed = finite_number(row.get("observed_demand"), label + " observed_demand")
            if observed < cpu:
                raise InvalidReport(f"{label}: observed_demand is below oncpu_cores")
            if index < full_count:
                oncpu.append(cpu)
                demand.append(observed)
        series[name] = {"oncpu": oncpu, "demand": demand}
    # Validation above establishes the complete common time axis before any totals.
    result = {"coverage": report["coverage_declared"], "start_ns": start,
              "end_ns": end, "complete_end_ns": start + full_count * width_ns,
              "bin_ms": width_ns / 1e6, "complete_windows": full_count,
              "partial_windows_excluded": int(bool(remainder)), "selected_pools": selected,
              "pools": {}, "joint": {}}
    top_count = math.ceil(full_count / 10)
    for name in selected:
        values = series[name]["oncpu"]
        stats = distribution(values)
        total = math.fsum(values)
        per_pool = {"oncpu": stats, "top_window_count": top_count,
                    "top_service_fraction": math.fsum(sorted(values, reverse=True)[:top_count]) / total if total else None,
                    "high": {}}
        for multiple in multiples:
            threshold = multiple * stats["mean"]
            finite_number(threshold, "computed high threshold")
            flags = [value > threshold for value in values]
            per_pool["high"][multiple] = {"threshold": threshold, "n": sum(flags),
                                          "fraction": sum(flags) / full_count,
                                          "segments": high_segments(flags, width_ns / 1e6)}
        result["pools"][name] = per_pool
    for metric in ("oncpu", "demand"):
        totals = [math.fsum(series[name][metric][i] for name in selected) for i in range(full_count)]
        result["joint"][metric] = distribution(totals)
    result["joint"]["high"] = {}
    for multiple in multiples:
        high = {name: [value > result["pools"][name]["high"][multiple]["threshold"]
                       for value in series[name]["oncpu"]] for name in selected}
        counts = Counter(sum(high[name][i] for name in selected) for i in range(full_count))
        conditional = {}
        for source in selected:
            denominator = sum(high[source])
            conditional[source] = {"source_high_n": denominator, "targets": {}}
            for target in selected:
                below = result["pools"][target]["oncpu"]["mean"]
                numerator = sum(high[source][i] and series[target]["oncpu"][i] <= below
                                for i in range(full_count))
                conditional[source]["targets"][target] = {
                    "n": numerator, "fraction": numerator / denominator if denominator else None}
        result["joint"]["high"][multiple] = {
            "simultaneously_high_pool_counts": dict(sorted(counts.items())), "conditional": conditional}
    return result


def number(value):
    return "NA" if value is None else f"{value:.3f}"


def print_report(result):
    print("Existing scheduler windows only; no raw trace scan")
    print(f"coverage_declared={result['coverage']} absolute_ns=[{result['start_ns']},{result['end_ns']}) "
          f"complete_end_ns={result['complete_end_ns']} bin_ms={result['bin_ms']:g} "
          f"complete_n={result['complete_windows']} partial_excluded={result['partial_windows_excluded']}")
    print("selected_pools=" + json.dumps(result["selected_pools"], ensure_ascii=False))
    for name, pool in result["pools"].items():
        label = json.dumps(name, ensure_ascii=False)
        stats = pool["oncpu"]
        fraction = pool["top_service_fraction"]
        print(f"POOL {label} onCPU mean/p50/p90/p99/max=" +
              "/".join(number(stats[key]) for key in ("mean", "p50", "p90", "p99", "max")) +
              f" top10%_bins={pool['top_window_count']} CPU_service_pct={number(None if fraction is None else 100 * fraction)}")
        for multiple, high in pool["high"].items():
            segments = high["segments"]
            print(f"  high>{multiple:g}xmean threshold={number(high['threshold'])} n={high['n']} "
                  f"pct={100 * high['fraction']:.3f} segments={segments['n']} "
                  "length_ms_p50/p95/max=" + "/".join(number(segments[key]) for key in ("p50_ms", "p95_ms", "max_ms")) +
                  f" edge_censored_segments={segments['edge_censored_segments']}")
    joint = result["joint"]
    print("JOINT synchronous onCPU mean/max=" + "/".join(number(joint['oncpu'][key]) for key in ('mean', 'max')) +
          " observed_demand mean/max=" + "/".join(number(joint['demand'][key]) for key in ('mean', 'max')))
    for multiple, data in joint["high"].items():
        counts = ",".join(f"{count}:{n}" for count, n in data["simultaneously_high_pool_counts"].items())
        print(f"  {multiple:g}xmean simultaneously_high_pools:bin_count={counts}")
        print("  conditional P(target onCPU<=its own mean | source high), target order=" +
              json.dumps(result["selected_pools"], ensure_ascii=False))
        for source, conditional in data["conditional"].items():
            percentages = [number(None if item["fraction"] is None else 100 * item["fraction"])
                           for item in conditional["targets"].values()]
            print(f"    source={json.dumps(source, ensure_ascii=False)} high_n={conditional['source_high_n']} pct=" + "/".join(percentages))
    print("NOTE: low observed CPU service is not idle/borrowable capacity; observed demand may still be high.")
    print("NOTE: bucket segments are not request bursts; onCPU is delivered logical CPU time, not hardware capacity.")
    print("NOTE: CSV/exact TID counts do not certify pool identity; capture registration/coverage still require verification.")
    print("NOTE: these dependent windows do not identify Folly Q/I/O, a complete kernel runq, operator duration, data locality or request p99.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", help="existing sched-text-v1 JSON with --include-windows")
    parser.add_argument("--bin-ms", type=float, default=1)
    parser.add_argument("--high-multiples", default="2,4", help="comma-separated multiples of each selected pool's mean onCPU")
    parser.add_argument("--pools", help="comma-separated pool names; default all except [unmapped]")
    parser.add_argument("--max-input-mb", type=float, default=128, help="positive input cap, at most 128 MiB")
    args = parser.parse_args(argv)
    try:
        finite_number(args.max_input_mb, "max-input-mb", positive=True)
        limit = int(args.max_input_mb * 1024 * 1024)
        multiples = tuple(float(part) for part in args.high_multiples.split(","))
        pools = [part.strip() for part in args.pools.split(",")] if args.pools is not None else None
        report = load_report(args.summary, limit)
        result = analyze(report, args.bin_ms, multiples, pools)
        print_report(result)
    except (InvalidReport, OSError, ValueError, OverflowError) as error:
        prefix = "unavailable" if isinstance(error, UnavailableReport) else "error"
        print(f"{prefix}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

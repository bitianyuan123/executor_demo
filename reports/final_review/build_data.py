#!/usr/bin/env python3
"""Normalize reviewed terminal evidence; never reanalyse unavailable target CSVs."""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "data"
BATCHES = ["4011", "4012", "4013", "4014", "4015", "4016", "4018", "4020"]
sources = {}
runs = []
mechanisms = []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_row(batch, index, raw, contract, **kw):
    """Each numeric metric maps to its JSON pointer below, not invented raw precision."""
    row = {
        "id": f"{batch}:{index}", "batch": batch,
        "source_file": f"results/review/{batch}_summary.json",
        "source_pointer": f"/runs/{index}", "source_log": f"{batch}.log",
        "evidence": "reviewed_terminal_rounded_summary",
        "printed_rounding": True, "raw_target_csv_available": False,
        "request_latency_scope": "planned arrivals [2,18) s; late completions retained",
        "cpu_scope": "whole execution incl. warmup/drain, excluding pool/buffer construction and export",
        "metric_sources": {}, "contract": contract,
        "cpu_utilization_percent": None,
        "cpu_utilization_missing_reason": "no aligned CPU-time/wall-time utilization measurement in selected summary",
        "independent_repetitions_per_cell": 1,
        "cross_batch_causal_comparison": False,
    }
    row.update(kw)
    runs.append(row)
    return row


def map_metric(row, name, value, pointer):
    row[name] = value
    row["metric_sources"][name] = row["source_pointer"] + "/" + pointer if value is not None else None


def common_contract(d, batch):
    c = d.get("common_cli_contract", {}).copy()
    if batch == "4011":
        c = {"--arrival": "burst", "--qps": "12", "--requests": "240",
             "--threads": "128", "--iterations": "3000000", "--accesses": "100000",
             "--widths": "80,4", "--depths": "2,2", "--branch-kb": "64",
             "--work-scale-scope": "task", "--continuation": "parent"}
    return c


for batch in BATCHES:
    path = ROOT / f"results/review/{batch}_summary.json"
    d = json.loads(path.read_text())
    sources[batch] = {
        "file": str(path.relative_to(ROOT)), "sha256": sha(path),
        "upstream_log_sha256": d.get("source_sha256"),
        "original_evidence_note": d.get("evidence"),
        "common_contract": common_contract(d, batch),
    }
    items = d["runs"].items() if isinstance(d["runs"], dict) else enumerate(d["runs"])
    for index, r in items:
        contract = common_contract(d, batch)
        if batch in ["4011", "4012", "4013", "4014", "4015", "4016"]:
            contract.update(r.get("cli_options", {}))
            policy = r.get("policy", "home" if r.get("strategy") == "home" else "outstanding")
            trace = r.get("trace", int(contract.get("--trace", "0")))
            threads = r.get("threads", int(contract.get("--threads", "128")))
            scenario = "S" if batch == "4015" else "SD" if batch == "4016" else "L"
            row = add_row(batch, index, r, contract, seed=r["seed"],
                scenario=scenario, arrival=contract["--arrival"],
                qps=float(contract["--qps"]), threads=threads, trace=trace,
                policy=policy, strategy=r.get("strategy", policy),
                balance_at="fork" if r.get("strategy") == "fork-parent" else "all",
                continuation="parent", run_name=r.get("run", f"seed-{r['seed']}-{r['strategy']}-trace{trace}" if "strategy" in r else None),
                whole_completed=r.get("whole_completed", 240),
                whole_failed_or_rejected=r.get("whole_failed_or_rejected", 0))
            for name in ["cohort_completed", "cohort_offered", "mean_ms", "p95_ms", "p99_ms", "peak_inflight"]:
                map_metric(row, name, r[name], name)
            cpu_key = "process_cpu_per_request_ms" if batch == "4011" else "whole_cpu_per_request_ms"
            cpu_s_key = "process_cpu_s" if batch == "4011" else "whole_process_cpu_s"
            map_metric(row, "cpu_per_request_ms", r[cpu_key], cpu_key)
            map_metric(row, "process_cpu_s", r[cpu_s_key], cpu_s_key)
            for name in ["cohort_lag_mean_ms", "cohort_lag_p99_ms", "cohort_attempt_to_finish_mean_ms", "cohort_attempt_to_finish_p99_ms", "operator_cpu_mean_ms", "operator_wall_mean_ms", "task_wait_p99_ms"]:
                map_metric(row, name, r.get(name), name)
            for name in ["roles", "first_fork", "joint_load", "pool_callback_occupancy", "all_callback_occupancy_mean", "all_callback_occupancy_peak"]:
                if r.get(name) is not None:
                    mechanisms.append({"run_id": row["id"], "kind": name,
                        "source_file": row["source_file"], "source_pointer": f"/runs/{index}/{name}",
                        "values": r[name], "printed_rounding": True})
        elif batch == "4018":
            o = r["overrides"]
            contract.update(d["business_arrival_contract"])
            contract.update(d["routing_contract"])
            contract.update(o)
            row = add_row(batch, index, r, contract, seed=421,
                scenario=r["cell"].split("-")[0].upper(), arrival="regular", qps=96.0,
                threads=16, trace=int(o["trace"]), policy=o["policy"], strategy=o["policy"],
                balance_at="all", continuation="parent", run_name=r["run"],
                whole_completed=r["whole_execution_line"]["completed"], whole_failed_or_rejected=0)
            for field in ["mean", "p95", "p99"]:
                map_metric(row, field + "_ms", r["cohort"]["scheduled_to_finish_ms"][field], f"cohort/scheduled_to_finish_ms/{field}")
            for field in ["offered", "completed"]:
                map_metric(row, "cohort_" + field, r["cohort"][field], f"cohort/{field}")
            map_metric(row, "cpu_per_request_ms", r["whole_report"]["cpu_per_request_ms"], "whole_report/cpu_per_request_ms")
            map_metric(row, "process_cpu_s", r["whole_execution_line"]["process_cpu_s"], "whole_execution_line/process_cpu_s")
            map_metric(row, "peak_inflight", r["whole_report"]["retained_request_state_peak"], "whole_report/retained_request_state_peak")
            map_metric(row, "cohort_lag_mean_ms", r["cohort"]["arrival_lag_ms"]["mean"], "cohort/arrival_lag_ms/mean")
            map_metric(row, "cohort_lag_p99_ms", r["cohort"]["arrival_lag_ms"]["p99"], "cohort/arrival_lag_ms/p99")
            map_metric(row, "cohort_attempt_to_finish_mean_ms", r["cohort"]["attempt_to_finish_ms"]["mean"], "cohort/attempt_to_finish_ms/mean")
            map_metric(row, "cohort_attempt_to_finish_p99_ms", r["cohort"]["attempt_to_finish_ms"]["p99"], "cohort/attempt_to_finish_ms/p99")
            for name in ["trace1_cohort_async_stages", "trace1_cohort_business_operators", "trace1_cohort_rpc_resume", "async_source_whole"]:
                if name in r:
                    mechanisms.append({"run_id": row["id"], "kind": name,
                        "source_file": row["source_file"], "source_pointer": f"/runs/{index}/{name}",
                        "values": r[name], "printed_rounding": True})
        else:
            contract.update(r["options"])
            row = add_row(batch, index, r, contract, seed=421,
                scenario="A1", arrival="regular", qps=96.0, threads=16,
                trace=int(contract["--trace"]), policy=contract["--policy"], strategy=contract["--policy"],
                balance_at="all", continuation="parent", run_name=contract["--output"],
                whole_completed=r["whole"]["completed"], whole_failed_or_rejected=r["whole"]["failed_or_rejected"])
            for name in ["mean_ms", "p95_ms", "p99_ms", "task_wait_p99_ms"]:
                map_metric(row, name, r["cohort"].get(name), f"cohort/{name}")
            for field in ["offered", "completed"]:
                map_metric(row, "cohort_" + field, r["cohort"][field], f"cohort/{field}")
            for name in ["cpu_per_request_ms", "process_cpu_s", "peak_inflight"]:
                map_metric(row, name, r["whole"][name], f"whole/{name}")
            for name in ["flow", "source", "callback_occupancy_printed"]:
                if name in r:
                    mechanisms.append({"run_id": row["id"], "kind": name,
                        "source_file": row["source_file"], "source_pointer": f"/runs/{index}/{name}",
                        "values": r[name], "printed_rounding": True})

# Earlier follow/fork blocks are included from published review tables. Missing
# individual CPU values stay null, rather than inferred from aggregate ranges.
historical = {
    "4008": [
        (420, "home", 121.817, 148.732, None), (420, "fork-parent", 76.413, 107.875, None), (420, "all-parent", 76.472, 113.669, None),
        (421, "home", 123.002, 156.351, None), (421, "fork-parent", 76.565, 108.439, None), (421, "all-parent", 76.579, 111.726, None),
        (422, "home", 123.483, 154.629, None), (422, "fork-parent", 77.686, 106.019, None), (422, "all-parent", 77.384, 120.913, None)],
    "4009": [
        (420, "home", 158.287, 229.761, 325.430626), (420, "fork-parent", 151.661, 196.430, 326.149138), (420, "all-parent", 149.612, 192.428, 326.005275),
        (421, "home", 160.029, 225.660, 330.281406), (421, "fork-parent", 152.733, 210.335, 330.716071), (421, "all-parent", 151.054, 202.413, 330.858696),
        (422, "home", 159.594, 222.538, 327.240043), (422, "fork-parent", 152.734, 198.713, 327.509539), (422, "all-parent", 151.190, 187.664, 327.606195)],
}
for batch, values in historical.items():
    path = ROOT / f"docs/TARGET_V2_{batch}_REVIEW.md"
    sources[batch] = {"file": str(path.relative_to(ROOT)), "sha256": sha(path), "manual_extraction": True}
    for i, (seed, strategy, mean, p99, cpu_s) in enumerate(values):
        section = "2. 4008新实验" if batch == "4008" else "3. 新burst8"
        row = add_row(batch, i, {}, {}, seed=seed, scenario="L",
            arrival="regular" if batch == "4008" else "burst", qps=12.0, threads=128,
            trace=1, policy="home" if strategy == "home" else "outstanding", strategy=strategy,
            balance_at="fork" if strategy == "fork-parent" else "all", continuation="parent",
            mean_ms=mean, p99_ms=p99, p95_ms=None, process_cpu_s=cpu_s,
            cpu_per_request_ms=cpu_s * 1000 / 240 if cpu_s is not None else None,
            cohort_completed=192, cohort_offered=192, whole_completed=240, whole_failed_or_rejected=0,
            peak_inflight=8 if batch == "4009" else None,
            run_name=f"seed-{seed}-{strategy}")
        row["source_file"] = str(path.relative_to(ROOT))
        row["source_pointer"] = section
        row["metric_sources"] = {"mean_ms": f"{section}: seed {seed}, {strategy} mean/p99 table",
            "p99_ms": f"{section}: seed {seed}, {strategy} mean/p99 table",
            "process_cpu_s": f"{section}: seed {seed}, {strategy} CPU table" if cpu_s is not None else None,
            "cpu_per_request_ms": "process_cpu_s * 1000 / 240" if cpu_s is not None else None}
        row["evidence"] = "review_table_extracted_from_terminal"

# Keep all seed-level pairs. Ranges in prose are descriptive min/max, never CI.
metrics = ["mean_ms", "p95_ms", "p99_ms", "cpu_per_request_ms"]
pairs = []


def make_pair(candidate, baseline, kind):
    pair = {"kind": kind, "candidate_id": candidate["id"], "baseline_id": baseline["id"],
        "batch": candidate["batch"], "scenario": candidate["scenario"], "seed": candidate["seed"],
        "candidate_strategy": candidate["strategy"], "baseline_strategy": baseline["strategy"],
        "threads": candidate["threads"], "trace": candidate["trace"],
        "precision": "derived from printed rounded endpoints; not the original tool unrounded delta",
        "independent_repetitions_per_cell": 1, "metrics": {}}
    for name in metrics:
        c, b = candidate.get(name), baseline.get(name)
        pair["metrics"][name] = {"candidate": c, "baseline": b,
            "delta": c - b if c is not None and b is not None else None,
            "delta_percent": 100 * (c / b - 1) if c is not None and b not in [None, 0] else None}
    pairs.append(pair)


for r in runs:
    same = [s for s in runs if (s["batch"], s["scenario"], s["seed"], s["threads"], s["trace"]) ==
            (r["batch"], r["scenario"], r["seed"], r["threads"], r["trace"])]
    if r["policy"] != "home":
        home = [s for s in same if s["policy"] == "home"]
        assert len(home) == 1
        make_pair(r, home[0], "same_batch_policy_minus_home")
    if r["strategy"] == "all-parent":
        fork = [s for s in same if s["strategy"] == "fork-parent"]
        if fork:
            make_pair(r, fork[0], "same_batch_all_minus_fork")
    if r["trace"] == 1:
        off = [s for s in runs if (s["batch"], s["scenario"], s["seed"], s["threads"], s["strategy"], s["trace"]) ==
               (r["batch"], r["scenario"], r["seed"], r["threads"], r["strategy"], 0)]
        if off:
            assert len(off) == 1
            make_pair(r, off[0], "same_batch_trace1_minus_trace0")
    if r["threads"] == 16:
        more = [s for s in runs if (s["batch"], s["scenario"], s["seed"], s["strategy"], s["trace"], s["threads"]) ==
                (r["batch"], r["scenario"], r["seed"], r["strategy"], r["trace"], 128)]
        if more:
            assert len(more) == 1
            make_pair(r, more[0], "same_batch_16_minus_128_threads")

out = {
    "schema": "affinity-final-evidence-v1", "sources": sources,
    "scenario_labels": {
        "L": "较长 CPU 算子；request 内 80/4 扇出，规则或 burst8 到达",
        "S": "短 CPU 算子，12 QPS，观察到孤立请求",
        "SD": "短 CPU 算子，96 QPS，观察到有限请求重叠",
        "D0": "短密度业务工作，直接继续，不增加异步恢复",
        "A0": "短密度业务工作，约 50% 位置经零延迟异步恢复",
        "A1": "短密度业务工作，约 50% 位置经 2 ms 异步等待恢复"},
    "scope": {"deployment": "one pool per verified cluster, six selected clusters, nominal capacity 16 each",
        "cpu_union": "0-47,192-239", "numa_ids": [0, 2], "cross_numa": False,
        "latency_unit": "ms", "cpu_per_request_unit": "process CPU milliseconds per completed whole-run request",
        "latency_window": "planned arrivals [2,18) seconds; late outcomes retained",
        "cpu_scope": "whole execution including warmup/drain, excluding pool/trace-buffer construction and export; async helper included where enabled",
        "cpu_utilization": "unavailable; do not label CPU/request or callback occupancy as utilization",
        "rounded": True, "target_raw_csv_available_locally": False,
        "quantile_policy": "nearest rank within each run; never average per-run p99 into pooled p99",
        "seed_policy": "seed changes business work; three seeds are not three independent repetitions of identical workload",
        "matching_policy": "paired only within same batch/scenario/seed and intended controlled axis",
        "statistical_claim": "descriptive evidence only; no CIs or production tail acceptance",
        "missing_policy": "null means unavailable, never zero"},
    "runs": runs, "paired_comparisons": pairs, "mechanism_observations": mechanisms,
}
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "comparisons.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
columns = ["id", "batch", "scenario", "arrival", "qps", "threads", "trace", "seed", "policy", "strategy", "balance_at", "mean_ms", "p95_ms", "p99_ms", "cpu_per_request_ms", "process_cpu_s", "cohort_completed", "whole_completed", "peak_inflight", "source_file", "source_pointer"]
with (OUT / "runs.csv").open("w", newline="") as fp:
    w = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(runs)
with (OUT / "paired_comparisons.csv").open("w", newline="") as fp:
    columns = ["kind", "batch", "scenario", "seed", "threads", "trace", "candidate_id", "baseline_id", "candidate_strategy", "baseline_strategy", "metric", "candidate", "baseline", "delta", "delta_percent"]
    w = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for pair in pairs:
        for name, values in pair["metrics"].items():
            w.writerow({**pair, "metric": name, **values})
assert len({r["id"] for r in runs}) == len(runs)
assert all(r["mean_ms"] <= r["p99_ms"] for r in runs)
assert all(r["cohort_completed"] == r["cohort_offered"] for r in runs)
print(f"Wrote {len(runs)} runs, {len(pairs)} matched comparisons, {len(mechanisms)} mechanism records")

#!/usr/bin/env python3
"""Analyze real CSV observations; no external Python dependencies required."""
import argparse
import csv
import json
import math
import pathlib
import random
import statistics
from collections import defaultdict


def percentile(values, q):
    if not values:
        return None
    data = sorted(values)
    return data[max(0, math.ceil(q * len(data)) - 1)]


def pearson(x, y):
    if len(x) < 3 or len(x) != len(y):
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    dx, dy = [v-mx for v in x], [v-my for v in y]
    denom = math.sqrt(sum(v*v for v in dx) * sum(v*v for v in dy))
    return sum(a*b for a, b in zip(dx, dy))/denom if denom else None


def ranks(data):
    result = [0.0] * len(data)
    order = sorted(range(len(data)), key=lambda i: data[i])
    lo = 0
    while lo < len(order):
        hi = lo + 1
        while hi < len(order) and data[order[hi]] == data[order[lo]]:
            hi += 1
        for j in range(lo, hi):
            result[order[j]] = (lo+hi-1)/2
        lo = hi
    return result


def correlations(x, y):
    return {"n": len(x), "pearson": pearson(x, y), "spearman": pearson(ranks(x), ranks(y))}


def bootstrap_mean(values, seed=42, repetitions=400):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(repetitions)]
    return [percentile(means, .025), percentile(means, .975)]


def load_csv(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def numeric(row, key, default=0):
    try:
        return float(row.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def completed(row):
    return row.get("status") in ("completed", "complete", "ok", "success")


def summarize(path):
    metadata = json.loads((path / "metadata.json").read_text())
    summary = json.loads((path / "summary.json").read_text())
    if metadata.get("phase") != "completed" or not summary.get("conservation_ok") or summary.get("io_failed"):
        raise ValueError(f"Incomplete or invalid run {path}; inspect metadata/summary/console before analysis")
    tasks_all = load_csv(path / "tasks.csv")
    requests_all = load_csv(path / "requests.csv")
    if len(tasks_all) != summary.get("task_rows") or len(requests_all) != summary.get("offered"):
        raise ValueError(f"Truncated CSV in {path}; row counts differ from summary")
    tasks = [t for t in tasks_all if completed(t)]
    requests = [r for r in requests_all if completed(r)]
    latencies = [numeric(r, "latency_us")/1000 for r in requests]
    states = defaultdict(int)
    for r in requests_all:
        states[r["status"]] += 1
    metrics = {}
    wait = [numeric(t, "enqueue_us") for t in tasks]
    for key in ("queue_before", "pending_before", "inflight_before", "outstanding_before",
                "outstanding_cpu_us_before", "chosen_load_us"):
        metrics[key] = correlations([numeric(t, key) for t in tasks], wait)
    first_tasks = {}
    for task in tasks:
        rid = task["request_id"]
        if rid not in first_tasks or numeric(task, "submit_ns") < numeric(first_tasks[rid], "submit_ns"):
            first_tasks[rid] = task
    pairs = [(r, first_tasks[r["request_id"]]) for r in requests if r["request_id"] in first_tasks]
    req_corr = {key: correlations([numeric(t, key) for r, t in pairs],
                                  [numeric(r, "latency_us") for r, t in pairs])
                for key in ("queue_before", "inflight_before", "outstanding_before", "chosen_load_us")}
    work_corr = correlations([numeric(r, "planned_cpu_us") for r in requests],
                             [numeric(r, "latency_us") for r in requests])
    # Fixed-width workload strata reduce one confound; they are not a causal test.
    strata = defaultdict(list)
    for r, t in pairs:
        strata[(int(numeric(r, "fanout")), int(numeric(r, "planned_cpu_us")//10000))].append((r, t))
    conditioned = []
    for (fanout, work_bucket), rows in sorted(strata.items()):
        if len(rows) >= 5:
            conditioned.append({"fanout": fanout, "planned_work_10ms_bucket": work_bucket,
                **correlations([numeric(t, "chosen_load_us") for r, t in rows],
                               [numeric(r, "latency_us") for r, t in rows])})
    # Fixed logarithmic queue bins are comparable between runs.
    bins = defaultdict(list)
    for t in tasks:
        q = numeric(t, "queue_before")
        label = "0" if q == 0 else "1-3" if q < 4 else "4-15" if q < 16 else "16-63" if q < 64 else "64+"
        bins[label].append(numeric(t, "enqueue_us")/1000)
    proc = load_csv(path / "proc.csv") if (path / "proc.csv").exists() else []
    worker_samples = [row for row in proc if row.get("group", "other") != "other"]
    schedstats_enabled = bool(worker_samples) and all(row.get("schedstats_enabled") == "1" for row in worker_samples)
    capacities = {int(group["group"]): float(group["capacity"]) for group in metadata.get("groups", [])}
    hidden = [task for task in tasks if numeric(task, "queue_before") == 0 and
              numeric(task, "inflight_before") > capacities.get(int(task["chosen_group"]), float("inf"))]
    hidden_waits = [numeric(task, "enqueue_us")/1000 for task in hidden]
    return {"run": str(path), "requests": dict(states), "completed_tasks": len(tasks),
            "latency_mean_ms": statistics.mean(latencies) if latencies else None,
            "latency_p50_ms": percentile(latencies, .5), "latency_p99_ms": percentile(latencies, .99),
            "request_resample_mean_ci95_ms": bootstrap_mean(latencies),
            "enqueue_mean_ms": statistics.mean(wait)/1000 if wait else None,
            "enqueue_p99_ms": percentile(wait, .99)/1000 if wait else None,
            "cpu_total_s": sum(numeric(t, "cpu_us") for t in tasks_all)/1e6 if tasks_all else None,
            "cpu_measurement_scope": "logged operator kernels only; excludes child routing/Folly/logging/AIO; null means no task samples",
            "cpu_completed_tasks_s": sum(numeric(t, "cpu_us") for t in tasks)/1e6 if tasks else None,
            "sampled_executor_cpu_s": sum(numeric(t, "cpu_ns") for t in worker_samples)/1e9 if worker_samples else None,
            "sampled_executor_runqueue_s": sum(numeric(t, "runqueue_ns") for t in worker_samples)/1e9 if schedstats_enabled else None,
            "schedstats_enabled": schedstats_enabled,
            "zero_queue_with_inflight_over_capacity": {"n": len(hidden),
                "fraction_of_completed_tasks": len(hidden)/len(tasks) if tasks else None,
                "enqueue_mean_ms": statistics.mean(hidden_waits) if hidden_waits else None,
                "enqueue_p99_ms": percentile(hidden_waits, .99)},
            "execution_offcpu_or_overhead_s": sum(max(0, numeric(t, "wall_us")-numeric(t, "cpu_us")) for t in tasks_all)/1e6 if tasks_all else None,
            "migrations": sum(numeric(t, "migrated") for t in tasks_all) if tasks_all else None,
            "cross_numa": sum(numeric(t, "cross_numa") for t in tasks_all) if tasks_all else None,
            "task_load_vs_enqueue": metrics, "request_initial_load_vs_latency": req_corr,
            "request_planned_work_vs_latency": work_corr,
            "request_workload_strata": conditioned,
            "queue_bins": {key: {"n": len(v), "wait_mean_ms": statistics.mean(v), "wait_p99_ms": percentile(v, .99)}
                           for key, v in bins.items()}}


def fmt(value):
    return "n/a" if value is None else f"{value:.3f}"


def report(summaries):
    lines = ["# 实测汇总", "", "所有时延来自实际输出；CPU 工作以线程 CPU 时钟计量。", "",
             "| run | 完成请求 | 状态计数 | 均值 ms | p99 ms | 入队等待均值 ms | 算子 kernel CPU s | 迁移次数 |", "|---|---:|---|---:|---:|---:|---:|---:|"]
    for s in summaries:
        lines.append(f"| {pathlib.Path(s['run']).name} | {sum(v for k,v in s['requests'].items() if k in ('completed','complete','ok','success'))} | {s['requests']} | {fmt(s['latency_mean_ms'])} | {fmt(s['latency_p99_ms'])} | {fmt(s['enqueue_mean_ms'])} | {fmt(s['cpu_total_s'])} | {fmt(s['migrations'])} |")
    lines += ["", "## 负载与任务入队等待的相关性", "", "| run | 指标 | 样本数 | Pearson | Spearman |", "|---|---|---:|---:|---:|"]
    for s in summaries:
        for key, value in s["task_load_vs_enqueue"].items():
            lines.append(f"| {pathlib.Path(s['run']).name} | {key} | {value['n']} | {fmt(value['pearson'])} | {fmt(value['spearman'])} |")
    lines += ["", "## 解释边界", "",
        "- 每秒 p99 在低 QPS 下通常接近当秒最大值；跨运行比较请使用多轮独立重复，保留每轮结果。",
        "- 任务样本共享 request、池和时间段，不能当作独立重复；本报告没有对任务相关系数提供虚假的独立样本显著性。",
        "- JSON 中 request 重采样均值区间只供描述；请求仍可能时间相关，生产推断宜按完整重复运行或时间块 bootstrap。",
        "- 任务工作量、扇出、策略选池和过载拒绝均会混杂相关性；JSON 提供请求初始负载、计划工作量相关性及少量同工作量分层。",
        "- wall-cpu 包含抢占、阻塞及计时开销，不能直接命名为 runqueue 时间；请配合 proc.csv 或 perf sched。",
        "- 算子 kernel CPU 秒不包含后继路由、Folly 队列/唤醒、日志和 AIO；JSON 另有外部采样的 executor CPU，采样首尾可能漏计。关闭 task log 时无样本标为 n/a。",
        "- 零队列深度不表示 CPU 空闲。queue depth 的语义取决于 Folly 队列和信号量；本实验还记录 submitted-not-started。",
        "- 拒绝或删失请求必须一起报告；仅比较完成请求 p99 可能奖励主动丢弃慢请求的策略。",
        "- 本地 VM 的结果不证明 ARM 的 NUMA/缓存迁移成本或生产收益。", ""]
    return "\n".join(lines)


def plot(summaries, output):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise SystemExit("Plotting requires: python3 -m pip install matplotlib (prefer a virtual environment)")
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), layout="constrained")
    names = [pathlib.Path(s["run"]).name for s in summaries]
    axes[0].bar(range(len(names)), [s["latency_mean_ms"] or 0 for s in summaries], label="mean")
    axes[0].scatter(range(len(names)), [s["latency_p99_ms"] or 0 for s in summaries], color="crimson", label="p99", zorder=3)
    axes[0].set_xticks(range(len(names)), names, rotation=65, ha="right", fontsize=7)
    axes[0].set_ylabel("Request latency (ms)")
    axes[0].legend()
    labels = ["0", "1-3", "4-15", "16-63", "64+"]
    for s in summaries:
        axes[1].plot(labels, [s["queue_bins"].get(k, {}).get("wait_mean_ms", float("nan")) for k in labels], marker="o", label=pathlib.Path(s["run"]).name)
    axes[1].set_xlabel("Observed queue depth before submission")
    axes[1].set_ylabel("Mean enqueue-to-start (ms)")
    axes[1].legend(fontsize=6)
    fig.suptitle("Measured synthetic workload; local topology is not production ARM")
    fig.savefig(output, dpi=160)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("runs", nargs="+")
    p.add_argument("--output", default="results/analysis")
    p.add_argument("--plot", action="store_true")
    a = p.parse_args()
    data = [summarize(pathlib.Path(x)) for x in a.runs]
    out = pathlib.Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    out.with_suffix(".md").write_text(report(data))
    if a.plot:
        plot(data, out.with_suffix(".png"))
    print(report(data))

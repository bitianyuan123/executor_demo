#!/usr/bin/env python3
"""Summarize independent run means/ranges and optionally plot measured matrices."""
import argparse
import json
import pathlib
import statistics
from collections import defaultdict


def aggregate(root):
    root = pathlib.Path(root)
    manifest = json.loads((root/"manifest.json").read_text())
    analysis = json.loads((root/"analysis.json").read_text())
    names = {row["directory"]: row["name"] for row in manifest}
    groups = defaultdict(list)
    for row in analysis:
        groups[names[row["run"]]].append(row)
    result = {}
    for name, runs in sorted(groups.items()):
        means = [r["latency_mean_ms"] for r in runs]
        p99s = [r["latency_p99_ms"] for r in runs]
        queue_correlations = [r["task_load_vs_enqueue"]["queue_before"]["spearman"] for r in runs
                              if r["task_load_vs_enqueue"]["queue_before"]["spearman"] is not None]
        completed = sum(r["requests"].get("completed", 0) for r in runs)
        statuses = defaultdict(int)
        for r in runs:
            for k, v in r["requests"].items():
                statuses[k] += v
        result[name] = {"repetitions": len(runs), "requests": dict(statuses),
            "run_means_ms": means, "mean_of_run_means_ms": statistics.mean(means),
            "run_p99_ms": p99s, "migrations_per_completed_request": sum(r["migrations"] or 0 for r in runs)/completed if completed else None,
            "queue_zero_inflight_gt_capacity_tasks": sum(r["zero_queue_with_inflight_over_capacity"]["n"] for r in runs),
            "task_count": sum(r["completed_tasks"] for r in runs),
            "queue_spearman_range": [min(queue_correlations), max(queue_correlations)] if queue_correlations else None}
    return result


def table(groups):
    lines = ["| case | 重复 | 完成请求 | 各轮均值范围 ms | 各轮 p99 范围 ms | 迁移/请求 | Q=0 且 inflight>capacity 的任务 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, row in groups.items():
        means, p99s = row["run_means_ms"], row["run_p99_ms"]
        lines.append(f"| {name} | {row['repetitions']} | {row['requests'].get('completed',0)} | {min(means):.2f}–{max(means):.2f} | {min(p99s):.2f}–{max(p99s):.2f} | {row['migrations_per_completed_request']:.2f} | {row['queue_zero_inflight_gt_capacity_tasks']}/{row['task_count']} |")
    return "\n".join(lines)


def figures(data, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    policies = ["home", "borrow", "sticky", "fanout", "least-load"]
    for threads, color in ((2, "#24788a"), (16, "#c86f35")):
        for ax, field, title in ((axes[0,0], "run_means_ms", "CPU scenario: request mean"),
                                  (axes[0,1], "run_p99_ms", "CPU scenario: per-run request p99")):
            values = [data["main"][f"split-t{threads}-{p}"][field] for p in policies]
            ys = [statistics.mean(v) for v in values]
            ax.errorbar(policies, ys, yerr=[[y-min(v) for y,v in zip(ys,values)],
                        [max(v)-y for y,v in zip(ys,values)]], marker="o", capsize=4,
                        label=f"{threads} threads/group", color=color)
            ax.set_title(title)
            ax.set_ylabel("ms (range across runs)")
            ax.legend(fontsize=8)
            ax.grid(axis="y", alpha=.2)
        qps = [4, 12, 28, 36]
        vals = [data["load"][f"load-t{threads}-q{q}"]["run_means_ms"] for q in qps]
        ys = [statistics.mean(v) for v in vals]
        axes[1,0].errorbar(qps, ys, yerr=[[y-min(v) for y,v in zip(ys,vals)],
            [max(v)-y for y,v in zip(ys,vals)]], marker="o", capsize=4, color=color, label=f"{threads} threads/group")
    axes[1,0].set_title("Home policy: offered-load sweep")
    axes[1,0].set_xlabel("Offered requests/second")
    axes[1,0].set_ylabel("Mean request latency (ms)")
    axes[1,0].legend(fontsize=8)
    axes[1,0].grid(alpha=.2)
    vals = [data["rpc"][f"split-t16-{p}"]["run_means_ms"] for p in policies]
    ys = [statistics.mean(v) for v in vals]
    axes[1,1].bar(policies, ys, color="#24788a", yerr=[[y-min(v) for y,v in zip(ys,vals)],
        [max(v)-y for y,v in zip(ys,vals)]], capsize=4)
    axes[1,1].set_title("RPC + fixed estimate: 16 threads/group")
    axes[1,1].set_ylabel("Mean request latency (ms)")
    axes[1,1].grid(axis="y", alpha=.2)
    fig.suptitle("Local x86 VM: measured synthetic workload; ranges are NOT confidence intervals")
    fig.savefig(output, dpi=170)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--main", required=True)
    p.add_argument("--rpc", required=True)
    p.add_argument("--load", required=True)
    p.add_argument("--iterations", help="optional fixed-iteration control matrix")
    p.add_argument("--output", default="results/experiment-summary")
    p.add_argument("--plot", action="store_true")
    args = p.parse_args()
    data = {label: aggregate(getattr(args,label)) for label in ("main","rpc","load")}
    if args.iterations:
        data["iterations"] = aggregate(args.iterations)
    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False)+"\n")
    text = "# 本地独立重复的测量范围\n\n"
    text += "范围是完整运行间的观测范围，不是置信区间；没有把每秒 p99 再平均。表中 p99 为每轮全部完成请求的 nearest-rank p99。\n\n"
    for label, rows in data.items():
        text += f"## {label}\n\n{table(rows)}\n\n"
    text += "每轮原始数据、命令、状态和详细相关性见原始数据归档。拒绝/删失须同时检查；本地结果不能外推 ARM NUMA 收益。\n"
    out.with_suffix(".md").write_text(text)
    if args.plot:
        figures(data, out.with_suffix(".png"))


if __name__ == "__main__":
    main()

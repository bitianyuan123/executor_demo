#!/usr/bin/env python3
"""Whole-run held-out prediction of stage-3 task waiting and latency; stdlib only."""
import argparse
import hashlib
import importlib.util
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "stage3_observations", ROOT / "experiments/independent/scripts/analyze.py")
_observations = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_observations)

MODELS = {
    "target_only": ("target_cpu_us",),
    "queue_target": ("target_cpu_us", "native_queue"),
    "pending_target": ("target_cpu_us", "pending"),
    "queue_inflight_target": ("target_cpu_us", "native_queue", "inflight"),
    "work_inflight_target": ("target_cpu_us", "work_estimate_us", "inflight"),
}
LABELS = {
    "target_only": "自身 target",
    "queue_target": "queue + 自身 target",
    "pending_target": "pending + 自身 target",
    "queue_inflight_target": "queue + inflight + 自身 target",
    "work_inflight_target": "work + inflight + 自身 target",
}
TARGETS = {"wait_ms": "提交至开始等待", "latency_ms": "任务提交至完成"}
# A seed/repetition changes the realized workload, not its configured distribution.
# All other relevant execution and load conditions remain in the grouping key.
CONFIG_FIELDS = (
    "executor", "queue", "deployment", "affinity", "instrumentation", "pools",
    "threads_per_pool", "allowed_cpus", "effective_masks", "numa_labels",
    "arrival", "arrival_rate", "duration_seconds", "arrival_limit", "burst_min",
    "burst_max", "cpu_us", "jitter", "long_prob", "long_factor", "estimate_us",
    "work_mode", "iterations_per_us", "max_outstanding", "max_planned_tasks",
    "sample_ms", "drain_seconds", "hostname", "kernel", "machine",
)


def quantile(values, p=.95):
    values = sorted(values)
    return values[max(0, math.ceil(p * len(values)) - 1)] if values else None


def errors(actual, predicted):
    if len(actual) != len(predicted):
        raise ValueError("actual and predicted lengths differ")
    absolute = [abs(a - p) for a, p in zip(actual, predicted)]
    return {
        "n": len(absolute),
        "mae_ms": statistics.fmean(absolute) if absolute else None,
        "p95_absolute_error_ms": quantile(absolute),
        "actual_mean_ms": statistics.fmean(actual) if actual else None,
        "prediction_mean_ms": statistics.fmean(predicted) if predicted else None,
    }


def eligible(rows, features, target=None):
    fields = tuple(features) + ((target,) if target else ())
    return [r for r in rows if all(
        k in r and isinstance(r[k], (int, float)) and math.isfinite(r[k]) and r[k] >= 0
        for k in fields)]


def solve(matrix, rhs):
    """Small dense system, Gaussian elimination with partial pivoting."""
    n = len(rhs)
    augmented = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-15:
            raise ValueError("singular ridge system")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [x / scale for x in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[col])]
    return [row[-1] for row in augmented]


def fit(rows, features, target, ridge=1e-8):
    """Fit using training rows only. Constant features have zero standardized value."""
    if not rows:
        raise ValueError("empty training set")
    n, width = len(rows), len(features)
    means = [statistics.fmean(r[k] for r in rows) for k in features]
    scales = [math.sqrt(statistics.fmean((r[k] - mean)**2 for r in rows))
              for k, mean in zip(features, means)]
    scales = [s if s > 0 else 1. for s in scales]
    center = statistics.fmean(r[target] for r in rows)
    gram = [[0.] * width for _ in range(width)]
    rhs = [0.] * width
    for r in rows:
        x = [(r[k] - m) / s for k, m, s in zip(features, means, scales)]
        y = r[target] - center
        for i in range(width):
            rhs[i] += x[i] * y / n
            for j in range(width):
                gram[i][j] += x[i] * x[j] / n
    for i in range(width):
        gram[i][i] += ridge
    return {"features": tuple(features), "means": means, "scales": scales,
            "center": center, "coefficients": solve(gram, rhs)}


def predict(model, rows):
    # Both physical targets are nonnegative. This rule is identical for all models.
    return [max(0., model["center"] + sum(
        coef * (r[k] - mean) / scale
        for k, mean, scale, coef in zip(model["features"], model["means"],
                                       model["scales"], model["coefficients"])))
        for r in rows]


def resolve_run(manifest, value):
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [
        Path.cwd() / raw, ROOT / raw, manifest.parent / raw, manifest.parent / raw.name]
    for candidate in candidates:
        if (candidate / "metadata.json").is_file() and (candidate / "summary.json").is_file():
            return candidate.resolve()
    raise ValueError(f"run path cannot be resolved: {value}")


def read_runs(manifests, warmup=0., end=None):
    runs, excluded, seen = [], [], set()
    for source in manifests:
        source = Path(source).resolve()
        manifest = json.loads(source.read_text())
        for entry in manifest.get("runs", []):
            if entry.get("returncode") != 0:
                excluded.append({"manifest": str(source), "entry": entry.get("path"),
                                 "reason": "nonzero or missing returncode"})
                continue
            path = resolve_run(source, entry["path"])
            if str(path) in seen:
                excluded.append({"manifest": str(source), "entry": str(path), "reason": "duplicate run path"})
                continue
            seen.add(str(path))
            metadata, summary, rows = _observations.load_run(path, warmup, end)
            if "case" not in entry or "repetition" not in entry:
                raise ValueError(f"manifest case/repetition missing for {path}")
            config = {k: metadata.get(k) for k in CONFIG_FIELDS}
            config["binary_sha256"] = manifest.get("binary_sha256")
            runs.append({"id": str(path), "manifest": str(source), "case": entry["case"],
                         "repetition": entry["repetition"], "seed": metadata.get("seed"),
                         "config": config, "summary": summary, "rows": rows})
    return runs, excluded


def evaluate_group(runs, ridge=1e-8):
    first = runs[0]
    group = {"case": first["case"], "config": first["config"],
             "runs": [{"id": r["id"], "repetition": r["repetition"], "seed": r["seed"],
                       "selected_completed_tasks": len(r["rows"]),
                       "all_completed_tasks": r["summary"].get("completed_tasks"),
                       "rejected_tasks": r["summary"].get("rejected_tasks", 0),
                       "canceled_tasks": r["summary"].get("canceled_tasks", 0),
                       "failed_tasks": r["summary"].get("failed_tasks", 0)} for r in runs],
             "models": {}}
    group["group_id"] = hashlib.sha256(json.dumps(
        [first["case"], first["config"]], sort_keys=True).encode()).hexdigest()[:12]
    if len(runs) < 2 or len({r["repetition"] for r in runs}) < 2:
        group.update(status="insufficient_repetitions", reason="need at least two entire runs with distinct repetitions")
        return group
    group["status"] = "evaluated"
    for name, features in MODELS.items():
        folds = []
        combined = {target: {"actual": [], "prediction": [], "baseline": []} for target in TARGETS}
        for held in runs:
            train_runs = [r for r in runs if r["id"] != held["id"]]
            # Never randomly split tasks/bursts. An entire held-out run is absent from fitting.
            train_rows = [row for r in train_runs for row in r["rows"]]
            usable_train = eligible(train_rows, features)
            usable_test = eligible(held["rows"], features)
            fold = {"held_out_run": held["id"], "held_out_repetition": held["repetition"],
                    "training_runs": [r["id"] for r in train_runs],
                    "training_tasks_before_filter": len(train_rows), "training_tasks": len(usable_train),
                    "test_tasks_before_filter": len(held["rows"]), "test_tasks": len(usable_test)}
            if not usable_train or not usable_test:
                fold.update(status="unavailable", reason="missing observed features or no selected completed tasks")
                folds.append(fold)
                continue
            fold["status"], fold["targets"] = "evaluated", {}
            for target in TARGETS:
                training = eligible(usable_train, (), target)
                testing = eligible(usable_test, (), target)
                if not training or not testing:
                    raise ValueError(f"valid feature row lacks target {target}")
                fitted = fit(training, features, target, ridge)
                baseline = fit(training, MODELS["target_only"], target, ridge)
                actual = [r[target] for r in testing]
                predicted, base_predicted = predict(fitted, testing), predict(baseline, testing)
                report = errors(actual, predicted)
                report["paired_target_only"] = errors(actual, base_predicted)
                report["mae_reduction_vs_paired_target_only_ms"] = report["paired_target_only"]["mae_ms"] - report["mae_ms"]
                fold["targets"][target] = report
                combined[target]["actual"].extend(actual)
                combined[target]["prediction"].extend(predicted)
                combined[target]["baseline"].extend(base_predicted)
            folds.append(fold)
        total = {}
        evaluated = [f for f in folds if f["status"] == "evaluated"]
        for target, data in combined.items():
            total[target] = errors(data["actual"], data["prediction"])
            total[target]["paired_target_only"] = errors(data["actual"], data["baseline"])
            total[target]["mae_reduction_vs_paired_target_only_ms"] = (
                total[target]["paired_target_only"]["mae_ms"] - total[target]["mae_ms"] if evaluated else None)
            total[target]["mean_run_mae_ms"] = (
                statistics.fmean(f["targets"][target]["mae_ms"] for f in evaluated) if evaluated else None)
        group["models"][name] = {"features": list(features), "status": "evaluated" if evaluated else "unavailable",
                                 "evaluated_folds": len(evaluated), "total_folds": len(folds),
                                 "folds": folds, "pooled_held_out": total}
    return group


def evaluate(runs, ridge=1e-8):
    grouped = defaultdict(list)
    for run in runs:
        key = json.dumps([run["case"], run["config"]], sort_keys=True)
        grouped[key].append(run)
    return [evaluate_group(grouped[k], ridge) for k in sorted(grouped)]


NOTES = [
    "训练/验证按整轮运行分开；同一burst、同一请求/任务不会随机分散到训练和验证。标准化参数只由训练轮次估计。",
    "相同case还按亲和、线程数、到达/服务分布、计量模式、硬件和二进制等配置分组；不足两个不同repetition不训练。",
    "这是带微小ridge的标准化线性回归，所有预测下限为0。模型只是描述性基线，非最优调度或因果模型。",
    "负载缺测（-1/字段缺失）不补0。模型只用其全部特征可用的样本，并报告覆盖率；配对自身target基线使用同样训练/测试样本。",
    "自身target是demo在提交前已知的工作预算。estimate_us=0时work利用其他任务的已知合成预算，是理想信息，不能声称生产同样可得。work仍保留执行中任务的完整预算，并非准确剩余CPU。",
    "MAE和p95绝对误差单位为ms；p95按留出样本的绝对误差取nearest-rank，不是任务时延p99，也不是置信区间。",
    "合并误差按任务加权，mean_run_mae另给每轮等权均值；同轮/同burst样本相关，不据样本数作独立性或显著性声称。",
    "只评价完成任务；拒绝、取消、失败和未完成可能造成选择偏差，须同时查看各轮计数。过载趋势可能提高预测表现但不证明稳态或因果。",
    "误差下降为配对target-only的MAE减去当前模型MAE，正值表示在这批留出数据上误差较小；负值保留。",
    "此处未随机拆分调参，默认ridge固定为1e-8；不跨配置外推，不把跨种子的预测误差当成ARM生产性能结论。",
]


def markdown(report):
    def fmt(value):
        return "—" if value is None else f"{value:.4f}"
    out = ["# 阶段3：按整轮留出的负载预测检验", "", *[f"- {note}" for note in NOTES], ""]
    for group in report["groups"]:
        config = group["config"]
        out += [f"## {group['case']} · {group['group_id']}", "",
                f"亲和 `{config.get('affinity')}`，每池 `{config.get('threads_per_pool')}` 线程，"
                f"arrival/s `{config.get('arrival_rate')}`，计量 `{config.get('instrumentation')}`，"
                f"共 {len(group['runs'])} 轮。", ""]
        if group["status"] != "evaluated":
            out += ["不足两次不同 repetition，未拟合模型。", ""]
            continue
        for target, label in TARGETS.items():
            out += [f"### {label}", "",
                    "|模型|有效折/总折|留出任务数|MAE ms|p95绝对误差 ms|配对基线 MAE ms|MAE减少 ms|",
                    "|---|---:|---:|---:|---:|---:|---:|"]
            for name, model in group["models"].items():
                values = model["pooled_held_out"][target]
                out.append(f"|{LABELS[name]}|{model['evaluated_folds']}/{model['total_folds']}|{values['n']}|"
                           f"{fmt(values['mae_ms'])}|{fmt(values['p95_absolute_error_ms'])}|"
                           f"{fmt(values['paired_target_only']['mae_ms'])}|"
                           f"{fmt(values['mae_reduction_vs_paired_target_only_ms'])}|")
            out.append("")
        out += ["每折训练轮次、测试轮次、缺测过滤数量、逐轮误差和拒绝/取消计数保存在同名 JSON。", ""]
    if report["excluded_entries"]:
        out += [f"排除 {len(report['excluded_entries'])} 条失败或重复 manifest 记录，原因见 JSON。", ""]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("independent-prediction"))
    parser.add_argument("--warmup-seconds", type=float, default=0.)
    parser.add_argument("--steady-end-seconds", type=float)
    parser.add_argument("--ridge", type=float, default=1e-8)
    args = parser.parse_args()
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error("warmup must be finite and nonnegative")
    if args.steady_end_seconds is not None and (
            not math.isfinite(args.steady_end_seconds) or args.steady_end_seconds <= args.warmup_seconds):
        parser.error("steady end must be finite and after warmup")
    if not math.isfinite(args.ridge) or args.ridge < 1e-12:
        parser.error("ridge must be finite and >= 1e-12")
    runs, excluded = read_runs(args.manifests, args.warmup_seconds, args.steady_end_seconds)
    if not runs:
        parser.error("no successful validated runs")
    report = {"schema": 1, "method": "leave-one-entire-run-out, standardized ridge linear regression",
              "ridge": args.ridge, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "analysis_window": {"planned_start_seconds": args.warmup_seconds,
                                  "planned_end_seconds": args.steady_end_seconds},
              "notes": NOTES, "excluded_entries": excluded, "groups": evaluate(runs, args.ridge)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report).rstrip() + "\n")
    print(args.output.with_suffix(".md"))


if __name__ == "__main__":
    main()

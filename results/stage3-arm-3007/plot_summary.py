#!/usr/bin/env python3
"""Plot reported summaries; never synthesize raw samples or CPU time curves.

Requires matplotlib. Example:
  python3 results/stage3-arm-3007/plot_summary.py

The default input is the byte-preserved terminal log beside this script.
Repeated terminal tables must agree, so repeated printing is not replication.
"""

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch


POLICIES = ("home", "source_only")
COLORS = ("#526780", "#078778")
CPU_HEADER = (
    "scope,covered_window_mean_pct,observed_complete_bin_peak_pct,"
    "ge90_duration_fraction,longest_observed_ge90_ms,complete_bins,"
    "bin_coverage,cpu_coverage"
)


def store_identical(target, key, row):
    if key in target and target[key] != row:
        raise ValueError(f"Repeated terminal rows disagree: {key}")
    target[key] = row


def read_summaries(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    phases, cpu = {}, {}
    policy = None
    for index, line in enumerate(lines):
        if line.startswith("POLICY "):
            policy = line.split(":", 1)[0].split()[1]
        is_phase = line.startswith("policy,phase,n,requests,S_mean,")
        is_cpu = line == CPU_HEADER
        if not (is_phase or is_cpu):
            continue
        header = next(csv.reader([line]))
        for body in lines[index + 1 :]:
            cells = next(csv.reader([body]))
            if len(cells) != len(header):
                break
            row = dict(zip(header, cells))
            if is_phase:
                if row["policy"] not in POLICIES:
                    break
                store_identical(phases, (row["policy"], row["phase"]), row)
            else:
                if policy not in POLICIES:
                    raise ValueError("CPU table has no recognized policy context")
                if not (row["scope"] == "union" or row["scope"].startswith("pool")):
                    break
                store_identical(cpu, (policy, row["scope"]), row)
    required_phases = {(p, phase) for p in POLICIES for phase in ("all", "initial", "steady")}
    required_cpu = {(p, scope) for p in POLICIES for scope in ("union", *(f"pool{i}" for i in range(6)))}
    if not required_phases <= phases.keys() or not required_cpu <= cpu.keys():
        raise ValueError("Missing expected phase or CPU summaries")
    for row in cpu.values():
        if float(row["bin_coverage"]) != 1 or float(row["cpu_coverage"]) != 1:
            raise ValueError("This figure requires the complete CPU coverage reported in 3007")
    return phases, cpu


def draw_bars(ax, labels, values, ymax, ylabel):
    width = 0.34
    for policy_index, policy in enumerate(POLICIES):
        positions = [i + (policy_index - 0.5) * width for i in range(len(labels))]
        bars = ax.bar(positions, values[policy], width, color=COLORS[policy_index], zorder=3)
        for bar in bars:
            value = bar.get_height()
            # Keep the full 0--100% CPU scale: annotate near-full bars inside.
            inside = value > 0.95 * ymax
            y = value - 0.026 * ymax if inside else value + 0.014 * ymax
            ax.text(bar.get_x() + bar.get_width() / 2, y, f"{value:.3f}",
                    ha="center", va="top" if inside else "bottom", fontsize=9,
                    color="white" if inside else "#202934", fontweight="normal")
    ax.set_xticks(range(len(labels)), labels)
    ax.set_ylim(0, ymax)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", color="#DDE3EA", linewidth=0.7, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="x", length=0, pad=9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path(__file__).with_name("3007.log"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("figures"))
    args = parser.parse_args()
    phases, cpu = read_summaries(args.log)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "svg.fonttype": "none", "svg.hashsalt": "stage3-arm-3007",
                         "axes.titleweight": "bold"})
    fig, axes = plt.subplots(1, 3, figsize=(15.8, 6.2))
    fig.subplots_adjust(left=0.058, right=0.984, top=0.73, bottom=0.28, wspace=0.32)
    fig.suptitle("3007 | Short-peak task routing: observed policy comparison",
                 x=0.058, y=0.965, ha="left", fontsize=17, fontweight="bold")
    fig.text(0.058, 0.913,
             "6 cluster-bound pools × 128 workers; 16 logical CPUs/pool; 12 requests/s; 600 measured requests per policy",
             ha="left", fontsize=10, color="#465364")
    fig.legend(handles=[Patch(facecolor=COLORS[i], label=p) for i, p in enumerate(POLICIES)],
               loc="upper left", bbox_to_anchor=(0.052, 0.887), ncol=2, frameon=False)

    initial = {p: [float(phases[p, "initial"][m]) for m in ("W_p99", "S_p99", "L_p99")]
               for p in POLICIES}
    draw_bars(axes[0], ["W p99\nsubmit → start", "S p99\nsubmit → end", "L p99\nplanned → end"],
              initial, 12, "Task latency (ms)")
    axes[0].set_title("A  Initial short tasks", loc="left", pad=14, fontsize=12)

    completion = {p: [float(phases[p, phase]["S_p99"]) for phase in ("steady", "all")]
                  for p in POLICIES}
    draw_bars(axes[1], ["Steady tasks\nS p99", "All tasks\nS p99"], completion, 80, "Task latency (ms)")
    axes[1].set_title("B  Completion tails: full scale", loc="left", pad=14, fontsize=12)

    occupancy = {p: [float(cpu[p, "union"][m]) for m in
                    ("covered_window_mean_pct", "observed_complete_bin_peak_pct")]
                 for p in POLICIES}
    draw_bars(axes[2], ["96-CPU union\nwindow mean", "96-CPU union\n1 ms bin peak"],
              occupancy, 100, "Scheduler non-idle residency (%)")
    axes[2].set_title("C  CPU: 96-CPU union scope", loc="left", pad=14, fontsize=12)

    local_streaks = {p: [float(cpu[p, f"pool{i}"]["longest_observed_ge90_ms"])
                        for i in range(6)] for p in POLICIES}
    range_text = lambda values: (f"{min(values):.3f}" if min(values) == max(values)
                                  else f"{min(values):.3f}–{max(values):.3f}")
    fig.text(0.058, 0.177,
             "Different scope: longest continuous ≥90% per-pool residency, "
             f"home {range_text(local_streaks['home'])} ms → source_only {range_text(local_streaks['source_only'])} ms.",
             fontsize=10, color="#283644")
    fig.text(0.058, 0.129,
             "One run/policy; reported summary values (3 decimals), not confidence intervals. Task p99 is not request/DAG completion p99.",
             fontsize=9.5, color="#465364")
    fig.text(0.058, 0.087,
             "CPU uses scheduler event reconstruction, not IRQ-exact utilization. No synthetic CPU time series; local and union peaks have different scopes.",
             fontsize=9.5, color="#465364")
    fig.text(0.058, 0.045,
             "source_only: pre-add N/C > 0.8 triggers routing to a strictly less-loaded pool; no absolute target cap. Home strict gate failed; reviewed exception retained.",
             fontsize=9, color="#465364")
    args.output.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg"):
        target = args.output / f"summary.{extension}"
        metadata = {"Creator": "plot_summary.py"}
        if extension == "svg":
            metadata["Date"] = None
        fig.savefig(target, dpi=180, facecolor="white", metadata=metadata)
        if extension == "svg":
            target.write_text("\n".join(line.rstrip() for line in target.read_text().splitlines()) + "\n")
        print(target)
    plt.close(fig)


if __name__ == "__main__":
    main()

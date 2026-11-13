# 实测汇总

所有时延来自实际输出；CPU 工作以线程 CPU 时钟计量。

| run | 完成请求 | 状态计数 | 均值 ms | p99 ms | 入队等待均值 ms | 算子 kernel CPU s | 迁移次数 |
|---|---:|---|---:|---:|---:|---:|---:|
| r0-split-t16-fanout | 40 | {'completed': 40} | 64.569 | 93.340 | 1.838 | 5.783 | 400.000 |
| r0-split-t16-queue | 40 | {'completed': 40} | 66.604 | 123.224 | 1.906 | 5.860 | 958.000 |
| r0-split-t16-borrow | 40 | {'completed': 40} | 63.038 | 91.544 | 1.908 | 5.781 | 1155.000 |
| r0-split-t16-least-load | 40 | {'completed': 40} | 63.778 | 119.704 | 1.992 | 5.901 | 1159.000 |
| r0-split-t16-sticky | 40 | {'completed': 40} | 62.751 | 115.255 | 1.767 | 5.735 | 883.000 |
| r0-split-t16-home | 40 | {'completed': 40} | 88.093 | 129.190 | 5.215 | 5.675 | 0.000 |
| r1-split-t16-home | 40 | {'completed': 40} | 89.037 | 120.974 | 4.674 | 5.621 | 0.000 |
| r1-split-t16-fanout | 40 | {'completed': 40} | 63.504 | 86.812 | 1.781 | 5.671 | 399.000 |
| r1-split-t16-borrow | 40 | {'completed': 40} | 61.358 | 84.891 | 1.744 | 5.733 | 1175.000 |
| r1-split-t16-sticky | 40 | {'completed': 40} | 61.619 | 94.546 | 1.741 | 5.726 | 916.000 |
| r1-split-t16-least-load | 40 | {'completed': 40} | 62.263 | 83.159 | 1.759 | 5.831 | 1208.000 |
| r1-split-t16-queue | 40 | {'completed': 40} | 65.745 | 90.582 | 1.767 | 5.727 | 1007.000 |

## 负载与任务入队等待的相关性

| run | 指标 | 样本数 | Pearson | Spearman |
|---|---|---:|---:|---:|
| r0-split-t16-fanout | queue_before | 2170 | 0.906 | 0.888 |
| r0-split-t16-fanout | pending_before | 2170 | 0.912 | 0.897 |
| r0-split-t16-fanout | inflight_before | 2170 | -0.258 | -0.481 |
| r0-split-t16-fanout | outstanding_before | 2170 | 0.615 | 0.419 |
| r0-split-t16-fanout | outstanding_cpu_us_before | 2170 | 0.615 | 0.419 |
| r0-split-t16-fanout | chosen_load_us | 2170 | 0.615 | 0.419 |
| r0-split-t16-queue | queue_before | 2170 | 0.868 | 0.831 |
| r0-split-t16-queue | pending_before | 2170 | 0.873 | 0.836 |
| r0-split-t16-queue | inflight_before | 2170 | -0.301 | -0.411 |
| r0-split-t16-queue | outstanding_before | 2170 | 0.560 | 0.447 |
| r0-split-t16-queue | outstanding_cpu_us_before | 2170 | 0.560 | 0.447 |
| r0-split-t16-queue | chosen_load_us | 2170 | 0.868 | 0.831 |
| r0-split-t16-borrow | queue_before | 2170 | 0.849 | 0.853 |
| r0-split-t16-borrow | pending_before | 2170 | 0.847 | 0.857 |
| r0-split-t16-borrow | inflight_before | 2170 | -0.180 | -0.289 |
| r0-split-t16-borrow | outstanding_before | 2170 | 0.658 | 0.610 |
| r0-split-t16-borrow | outstanding_cpu_us_before | 2170 | 0.658 | 0.610 |
| r0-split-t16-borrow | chosen_load_us | 2170 | 0.658 | 0.610 |
| r0-split-t16-least-load | queue_before | 2170 | 0.872 | 0.858 |
| r0-split-t16-least-load | pending_before | 2170 | 0.878 | 0.862 |
| r0-split-t16-least-load | inflight_before | 2170 | -0.153 | -0.320 |
| r0-split-t16-least-load | outstanding_before | 2170 | 0.702 | 0.589 |
| r0-split-t16-least-load | outstanding_cpu_us_before | 2170 | 0.702 | 0.589 |
| r0-split-t16-least-load | chosen_load_us | 2170 | 0.702 | 0.589 |
| r0-split-t16-sticky | queue_before | 2170 | 0.911 | 0.868 |
| r0-split-t16-sticky | pending_before | 2170 | 0.915 | 0.872 |
| r0-split-t16-sticky | inflight_before | 2170 | -0.191 | -0.365 |
| r0-split-t16-sticky | outstanding_before | 2170 | 0.692 | 0.540 |
| r0-split-t16-sticky | outstanding_cpu_us_before | 2170 | 0.692 | 0.540 |
| r0-split-t16-sticky | chosen_load_us | 2170 | 0.692 | 0.540 |
| r0-split-t16-home | queue_before | 2170 | 0.949 | 0.953 |
| r0-split-t16-home | pending_before | 2170 | 0.949 | 0.953 |
| r0-split-t16-home | inflight_before | 2170 | -0.361 | -0.347 |
| r0-split-t16-home | outstanding_before | 2170 | 0.629 | 0.655 |
| r0-split-t16-home | outstanding_cpu_us_before | 2170 | 0.629 | 0.655 |
| r0-split-t16-home | chosen_load_us | 2170 | 0.629 | 0.655 |
| r1-split-t16-home | queue_before | 2233 | 0.967 | 0.950 |
| r1-split-t16-home | pending_before | 2233 | 0.968 | 0.950 |
| r1-split-t16-home | inflight_before | 2233 | -0.406 | -0.395 |
| r1-split-t16-home | outstanding_before | 2233 | 0.623 | 0.614 |
| r1-split-t16-home | outstanding_cpu_us_before | 2233 | 0.623 | 0.614 |
| r1-split-t16-home | chosen_load_us | 2233 | 0.623 | 0.614 |
| r1-split-t16-fanout | queue_before | 2233 | 0.906 | 0.878 |
| r1-split-t16-fanout | pending_before | 2233 | 0.908 | 0.881 |
| r1-split-t16-fanout | inflight_before | 2233 | -0.251 | -0.493 |
| r1-split-t16-fanout | outstanding_before | 2233 | 0.644 | 0.420 |
| r1-split-t16-fanout | outstanding_cpu_us_before | 2233 | 0.644 | 0.420 |
| r1-split-t16-fanout | chosen_load_us | 2233 | 0.644 | 0.420 |
| r1-split-t16-borrow | queue_before | 2233 | 0.895 | 0.842 |
| r1-split-t16-borrow | pending_before | 2233 | 0.898 | 0.851 |
| r1-split-t16-borrow | inflight_before | 2233 | -0.166 | -0.309 |
| r1-split-t16-borrow | outstanding_before | 2233 | 0.727 | 0.591 |
| r1-split-t16-borrow | outstanding_cpu_us_before | 2233 | 0.727 | 0.591 |
| r1-split-t16-borrow | chosen_load_us | 2233 | 0.727 | 0.591 |
| r1-split-t16-sticky | queue_before | 2233 | 0.874 | 0.857 |
| r1-split-t16-sticky | pending_before | 2233 | 0.877 | 0.863 |
| r1-split-t16-sticky | inflight_before | 2233 | -0.181 | -0.388 |
| r1-split-t16-sticky | outstanding_before | 2233 | 0.690 | 0.523 |
| r1-split-t16-sticky | outstanding_cpu_us_before | 2233 | 0.690 | 0.523 |
| r1-split-t16-sticky | chosen_load_us | 2233 | 0.690 | 0.523 |
| r1-split-t16-least-load | queue_before | 2233 | 0.873 | 0.842 |
| r1-split-t16-least-load | pending_before | 2233 | 0.877 | 0.844 |
| r1-split-t16-least-load | inflight_before | 2233 | -0.159 | -0.301 |
| r1-split-t16-least-load | outstanding_before | 2233 | 0.708 | 0.587 |
| r1-split-t16-least-load | outstanding_cpu_us_before | 2233 | 0.708 | 0.587 |
| r1-split-t16-least-load | chosen_load_us | 2233 | 0.708 | 0.587 |
| r1-split-t16-queue | queue_before | 2233 | 0.888 | 0.825 |
| r1-split-t16-queue | pending_before | 2233 | 0.890 | 0.825 |
| r1-split-t16-queue | inflight_before | 2233 | -0.328 | -0.419 |
| r1-split-t16-queue | outstanding_before | 2233 | 0.549 | 0.426 |
| r1-split-t16-queue | outstanding_cpu_us_before | 2233 | 0.549 | 0.426 |
| r1-split-t16-queue | chosen_load_us | 2233 | 0.888 | 0.825 |

## 解释边界

- 每秒 p99 在低 QPS 下通常接近当秒最大值；跨运行比较请使用多轮独立重复，保留每轮结果。
- 任务样本共享 request、池和时间段，不能当作独立重复；本报告没有对任务相关系数提供虚假的独立样本显著性。
- JSON 中 request 重采样均值区间只供描述；请求仍可能时间相关，生产推断宜按完整重复运行或时间块 bootstrap。
- 任务工作量、扇出、策略选池和过载拒绝均会混杂相关性；JSON 提供请求初始负载、计划工作量相关性及少量同工作量分层。
- wall-cpu 包含抢占、阻塞及计时开销，不能直接命名为 runqueue 时间；请配合 proc.csv 或 perf sched。
- 算子 kernel CPU 秒不包含后继路由、Folly 队列/唤醒、日志和 AIO；JSON 另有外部采样的 executor CPU，采样首尾可能漏计。关闭 task log 时无样本标为 n/a。
- 零队列深度不表示 CPU 空闲。queue depth 的语义取决于 Folly 队列和信号量；本实验还记录 submitted-not-started。
- 拒绝或删失请求必须一起报告；仅比较完成请求 p99 可能奖励主动丢弃慢请求的策略。
- 本地 VM 的结果不证明 ARM 的 NUMA/缓存迁移成本或生产收益。

矩阵计划 12 轮；有效完整运行 12 轮；异常/不完整 0 轮。所有退出码与命令见 manifest.json。

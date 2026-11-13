# 实测汇总

所有时延来自实际输出；CPU 工作以线程 CPU 时钟计量。

| run | 完成请求 | 状态计数 | 均值 ms | p99 ms | 入队等待均值 ms | 算子 kernel CPU s | 迁移次数 |
|---|---:|---|---:|---:|---:|---:|---:|
| r0-split-t16-home | 40 | {'completed': 40} | 76.367 | 105.224 | 13.491 | 5.665 | 0.000 |
| r0-split-t16-fanout | 40 | {'completed': 40} | 45.199 | 74.831 | 5.020 | 5.789 | 398.000 |
| r0-split-t2-borrow | 40 | {'completed': 40} | 42.862 | 68.701 | 9.893 | 5.675 | 919.000 |
| r0-split-t16-borrow | 40 | {'completed': 40} | 48.369 | 99.159 | 5.435 | 6.193 | 853.000 |
| r0-split-t2-fanout | 40 | {'completed': 40} | 44.777 | 89.868 | 10.663 | 5.937 | 398.000 |
| r0-split-t2-home | 40 | {'completed': 40} | 74.335 | 112.441 | 22.823 | 5.662 | 0.000 |
| r1-split-t2-home | 40 | {'completed': 40} | 72.585 | 103.740 | 22.568 | 5.609 | 0.000 |
| r1-split-t16-home | 40 | {'completed': 40} | 77.050 | 97.612 | 13.485 | 5.633 | 0.000 |
| r1-split-t2-borrow | 40 | {'completed': 40} | 47.213 | 126.252 | 10.962 | 5.983 | 901.000 |
| r1-split-t2-fanout | 40 | {'completed': 40} | 46.655 | 108.790 | 10.954 | 5.893 | 379.000 |
| r1-split-t16-borrow | 40 | {'completed': 40} | 46.326 | 85.975 | 5.316 | 5.942 | 835.000 |
| r1-split-t16-fanout | 40 | {'completed': 40} | 47.185 | 86.865 | 5.363 | 5.831 | 374.000 |

## 负载与任务入队等待的相关性

| run | 指标 | 样本数 | Pearson | Spearman |
|---|---|---:|---:|---:|
| r0-split-t16-home | queue_before | 1640 | 0.958 | 0.963 |
| r0-split-t16-home | pending_before | 1640 | 0.959 | 0.963 |
| r0-split-t16-home | inflight_before | 1640 | -0.125 | -0.098 |
| r0-split-t16-home | outstanding_before | 1640 | 0.692 | 0.640 |
| r0-split-t16-home | outstanding_cpu_us_before | 1640 | 0.505 | 0.503 |
| r0-split-t16-home | chosen_load_us | 1640 | 0.500 | 0.499 |
| r0-split-t16-fanout | queue_before | 1640 | 0.906 | 0.937 |
| r0-split-t16-fanout | pending_before | 1640 | 0.916 | 0.948 |
| r0-split-t16-fanout | inflight_before | 1640 | -0.250 | -0.320 |
| r0-split-t16-fanout | outstanding_before | 1640 | 0.546 | 0.468 |
| r0-split-t16-fanout | outstanding_cpu_us_before | 1640 | 0.297 | 0.234 |
| r0-split-t16-fanout | chosen_load_us | 1640 | 0.291 | 0.235 |
| r0-split-t2-borrow | queue_before | 1640 | 0.847 | 0.824 |
| r0-split-t2-borrow | pending_before | 1640 | 0.847 | 0.824 |
| r0-split-t2-borrow | inflight_before | 1640 | 0.639 | 0.645 |
| r0-split-t2-borrow | outstanding_before | 1640 | 0.879 | 0.856 |
| r0-split-t2-borrow | outstanding_cpu_us_before | 1640 | 0.904 | 0.875 |
| r0-split-t2-borrow | chosen_load_us | 1640 | 0.882 | 0.859 |
| r0-split-t16-borrow | queue_before | 1640 | 0.868 | 0.915 |
| r0-split-t16-borrow | pending_before | 1640 | 0.872 | 0.920 |
| r0-split-t16-borrow | inflight_before | 1640 | -0.162 | -0.191 |
| r0-split-t16-borrow | outstanding_before | 1640 | 0.608 | 0.573 |
| r0-split-t16-borrow | outstanding_cpu_us_before | 1640 | 0.371 | 0.341 |
| r0-split-t16-borrow | chosen_load_us | 1640 | 0.362 | 0.333 |
| r0-split-t2-fanout | queue_before | 1640 | 0.770 | 0.847 |
| r0-split-t2-fanout | pending_before | 1640 | 0.770 | 0.847 |
| r0-split-t2-fanout | inflight_before | 1640 | 0.543 | 0.601 |
| r0-split-t2-fanout | outstanding_before | 1640 | 0.784 | 0.858 |
| r0-split-t2-fanout | outstanding_cpu_us_before | 1640 | 0.850 | 0.861 |
| r0-split-t2-fanout | chosen_load_us | 1640 | 0.838 | 0.851 |
| r0-split-t2-home | queue_before | 1640 | 0.887 | 0.822 |
| r0-split-t2-home | pending_before | 1640 | 0.889 | 0.825 |
| r0-split-t2-home | inflight_before | 1640 | 0.626 | 0.644 |
| r0-split-t2-home | outstanding_before | 1640 | 0.887 | 0.811 |
| r0-split-t2-home | outstanding_cpu_us_before | 1640 | 0.959 | 0.957 |
| r0-split-t2-home | chosen_load_us | 1640 | 0.954 | 0.952 |
| r1-split-t2-home | queue_before | 1640 | 0.914 | 0.835 |
| r1-split-t2-home | pending_before | 1640 | 0.914 | 0.834 |
| r1-split-t2-home | inflight_before | 1640 | 0.626 | 0.634 |
| r1-split-t2-home | outstanding_before | 1640 | 0.910 | 0.819 |
| r1-split-t2-home | outstanding_cpu_us_before | 1640 | 0.959 | 0.948 |
| r1-split-t2-home | chosen_load_us | 1640 | 0.950 | 0.941 |
| r1-split-t16-home | queue_before | 1640 | 0.967 | 0.967 |
| r1-split-t16-home | pending_before | 1640 | 0.968 | 0.968 |
| r1-split-t16-home | inflight_before | 1640 | -0.140 | -0.111 |
| r1-split-t16-home | outstanding_before | 1640 | 0.700 | 0.658 |
| r1-split-t16-home | outstanding_cpu_us_before | 1640 | 0.542 | 0.508 |
| r1-split-t16-home | chosen_load_us | 1640 | 0.538 | 0.506 |
| r1-split-t2-borrow | queue_before | 1640 | 0.783 | 0.803 |
| r1-split-t2-borrow | pending_before | 1640 | 0.783 | 0.804 |
| r1-split-t2-borrow | inflight_before | 1640 | 0.577 | 0.622 |
| r1-split-t2-borrow | outstanding_before | 1640 | 0.809 | 0.833 |
| r1-split-t2-borrow | outstanding_cpu_us_before | 1640 | 0.783 | 0.807 |
| r1-split-t2-borrow | chosen_load_us | 1640 | 0.757 | 0.789 |
| r1-split-t2-fanout | queue_before | 1640 | 0.829 | 0.859 |
| r1-split-t2-fanout | pending_before | 1640 | 0.827 | 0.857 |
| r1-split-t2-fanout | inflight_before | 1640 | 0.556 | 0.584 |
| r1-split-t2-fanout | outstanding_before | 1640 | 0.841 | 0.867 |
| r1-split-t2-fanout | outstanding_cpu_us_before | 1640 | 0.823 | 0.845 |
| r1-split-t2-fanout | chosen_load_us | 1640 | 0.807 | 0.833 |
| r1-split-t16-borrow | queue_before | 1640 | 0.848 | 0.915 |
| r1-split-t16-borrow | pending_before | 1640 | 0.849 | 0.923 |
| r1-split-t16-borrow | inflight_before | 1640 | -0.160 | -0.178 |
| r1-split-t16-borrow | outstanding_before | 1640 | 0.588 | 0.579 |
| r1-split-t16-borrow | outstanding_cpu_us_before | 1640 | 0.389 | 0.343 |
| r1-split-t16-borrow | chosen_load_us | 1640 | 0.371 | 0.331 |
| r1-split-t16-fanout | queue_before | 1640 | 0.909 | 0.940 |
| r1-split-t16-fanout | pending_before | 1640 | 0.915 | 0.947 |
| r1-split-t16-fanout | inflight_before | 1640 | -0.226 | -0.295 |
| r1-split-t16-fanout | outstanding_before | 1640 | 0.574 | 0.509 |
| r1-split-t16-fanout | outstanding_cpu_us_before | 1640 | 0.322 | 0.256 |
| r1-split-t16-fanout | chosen_load_us | 1640 | 0.314 | 0.255 |

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

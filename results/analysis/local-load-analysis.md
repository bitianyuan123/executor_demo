# 实测汇总

所有时延来自实际输出；CPU 工作以线程 CPU 时钟计量。

| run | 完成请求 | 状态计数 | 均值 ms | p99 ms | 入队等待均值 ms | 算子 kernel CPU s | 迁移次数 |
|---|---:|---|---:|---:|---:|---:|---:|
| r0-load-t2-q12 | 36 | {'completed': 36} | 75.389 | 114.393 | 23.153 | 5.160 | 0.000 |
| r0-load-t16-q4 | 12 | {'completed': 12} | 80.003 | 105.082 | 10.737 | 1.817 | 0.000 |
| r0-load-t16-q36 | 108 | {'completed': 108} | 1162.465 | 1961.780 | 333.190 | 15.071 | 0.000 |
| r0-load-t2-q36 | 108 | {'completed': 108} | 1076.509 | 1785.050 | 353.410 | 15.070 | 0.000 |
| r0-load-t16-q28 | 84 | {'completed': 84} | 243.935 | 607.721 | 32.468 | 11.832 | 0.000 |
| r0-load-t2-q28 | 84 | {'completed': 84} | 215.257 | 473.743 | 62.895 | 11.832 | 0.000 |
| r0-load-t16-q12 | 36 | {'completed': 36} | 76.794 | 108.103 | 10.947 | 5.153 | 0.000 |
| r0-load-t2-q4 | 12 | {'completed': 12} | 79.659 | 112.980 | 24.073 | 1.817 | 0.000 |
| r1-load-t2-q4 | 12 | {'completed': 12} | 74.352 | 102.924 | 22.911 | 1.723 | 0.000 |
| r1-load-t16-q36 | 108 | {'completed': 108} | 1073.845 | 1659.490 | 300.936 | 15.016 | 0.000 |
| r1-load-t2-q12 | 36 | {'completed': 36} | 75.062 | 119.283 | 23.314 | 5.109 | 0.000 |
| r1-load-t2-q28 | 84 | {'completed': 84} | 160.107 | 256.784 | 42.255 | 11.706 | 0.000 |
| r1-load-t16-q12 | 36 | {'completed': 36} | 76.751 | 100.866 | 10.934 | 5.109 | 0.000 |
| r1-load-t2-q36 | 108 | {'completed': 108} | 997.140 | 1502.310 | 324.687 | 15.016 | 0.000 |
| r1-load-t16-q28 | 84 | {'completed': 84} | 229.105 | 439.799 | 19.689 | 11.706 | 0.000 |
| r1-load-t16-q4 | 12 | {'completed': 12} | 75.584 | 99.252 | 11.050 | 1.723 | 0.000 |

## 负载与任务入队等待的相关性

| run | 指标 | 样本数 | Pearson | Spearman |
|---|---|---:|---:|---:|
| r0-load-t2-q12 | queue_before | 1476 | 0.888 | 0.833 |
| r0-load-t2-q12 | pending_before | 1476 | 0.888 | 0.832 |
| r0-load-t2-q12 | inflight_before | 1476 | 0.619 | 0.644 |
| r0-load-t2-q12 | outstanding_before | 1476 | 0.885 | 0.820 |
| r0-load-t2-q12 | outstanding_cpu_us_before | 1476 | 0.958 | 0.959 |
| r0-load-t2-q12 | chosen_load_us | 1476 | 0.954 | 0.955 |
| r0-load-t16-q4 | queue_before | 492 | 0.941 | 0.959 |
| r0-load-t16-q4 | pending_before | 492 | 0.940 | 0.959 |
| r0-load-t16-q4 | inflight_before | 492 | -0.137 | -0.126 |
| r0-load-t16-q4 | outstanding_before | 492 | 0.651 | 0.603 |
| r0-load-t16-q4 | outstanding_cpu_us_before | 492 | 0.482 | 0.439 |
| r0-load-t16-q4 | chosen_load_us | 492 | 0.474 | 0.434 |
| r0-load-t16-q36 | queue_before | 4428 | 0.989 | 0.993 |
| r0-load-t16-q36 | pending_before | 4428 | 0.989 | 0.993 |
| r0-load-t16-q36 | inflight_before | 4428 | 0.265 | 0.291 |
| r0-load-t16-q36 | outstanding_before | 4428 | 0.988 | 0.993 |
| r0-load-t16-q36 | outstanding_cpu_us_before | 4428 | 0.993 | 0.994 |
| r0-load-t16-q36 | chosen_load_us | 4428 | 0.993 | 0.994 |
| r0-load-t2-q36 | queue_before | 4428 | 0.988 | 0.993 |
| r0-load-t2-q36 | pending_before | 4428 | 0.988 | 0.993 |
| r0-load-t2-q36 | inflight_before | 4428 | 0.186 | 0.167 |
| r0-load-t2-q36 | outstanding_before | 4428 | 0.988 | 0.993 |
| r0-load-t2-q36 | outstanding_cpu_us_before | 4428 | 0.998 | 0.999 |
| r0-load-t2-q36 | chosen_load_us | 4428 | 0.998 | 0.998 |
| r0-load-t16-q28 | queue_before | 3444 | 0.976 | 0.968 |
| r0-load-t16-q28 | pending_before | 3444 | 0.976 | 0.968 |
| r0-load-t16-q28 | inflight_before | 3444 | 0.473 | 0.574 |
| r0-load-t16-q28 | outstanding_before | 3444 | 0.958 | 0.907 |
| r0-load-t16-q28 | outstanding_cpu_us_before | 3444 | 0.902 | 0.866 |
| r0-load-t16-q28 | chosen_load_us | 3444 | 0.901 | 0.865 |
| r0-load-t2-q28 | queue_before | 3444 | 0.959 | 0.950 |
| r0-load-t2-q28 | pending_before | 3444 | 0.959 | 0.950 |
| r0-load-t2-q28 | inflight_before | 3444 | 0.403 | 0.488 |
| r0-load-t2-q28 | outstanding_before | 3444 | 0.959 | 0.949 |
| r0-load-t2-q28 | outstanding_cpu_us_before | 3444 | 0.989 | 0.989 |
| r0-load-t2-q28 | chosen_load_us | 3444 | 0.988 | 0.987 |
| r0-load-t16-q12 | queue_before | 1476 | 0.948 | 0.951 |
| r0-load-t16-q12 | pending_before | 1476 | 0.949 | 0.951 |
| r0-load-t16-q12 | inflight_before | 1476 | -0.046 | -0.061 |
| r0-load-t16-q12 | outstanding_before | 1476 | 0.708 | 0.633 |
| r0-load-t16-q12 | outstanding_cpu_us_before | 1476 | 0.549 | 0.521 |
| r0-load-t16-q12 | chosen_load_us | 1476 | 0.545 | 0.518 |
| r0-load-t2-q4 | queue_before | 492 | 0.911 | 0.837 |
| r0-load-t2-q4 | pending_before | 492 | 0.910 | 0.834 |
| r0-load-t2-q4 | inflight_before | 492 | 0.632 | 0.638 |
| r0-load-t2-q4 | outstanding_before | 492 | 0.908 | 0.821 |
| r0-load-t2-q4 | outstanding_cpu_us_before | 492 | 0.952 | 0.957 |
| r0-load-t2-q4 | chosen_load_us | 492 | 0.947 | 0.953 |
| r1-load-t2-q4 | queue_before | 492 | 0.896 | 0.815 |
| r1-load-t2-q4 | pending_before | 492 | 0.894 | 0.812 |
| r1-load-t2-q4 | inflight_before | 492 | 0.621 | 0.629 |
| r1-load-t2-q4 | outstanding_before | 492 | 0.892 | 0.803 |
| r1-load-t2-q4 | outstanding_cpu_us_before | 492 | 0.947 | 0.941 |
| r1-load-t2-q4 | chosen_load_us | 492 | 0.936 | 0.932 |
| r1-load-t16-q36 | queue_before | 4428 | 0.993 | 0.994 |
| r1-load-t16-q36 | pending_before | 4428 | 0.993 | 0.994 |
| r1-load-t16-q36 | inflight_before | 4428 | 0.320 | 0.348 |
| r1-load-t16-q36 | outstanding_before | 4428 | 0.993 | 0.994 |
| r1-load-t16-q36 | outstanding_cpu_us_before | 4428 | 0.993 | 0.994 |
| r1-load-t16-q36 | chosen_load_us | 4428 | 0.993 | 0.994 |
| r1-load-t2-q12 | queue_before | 1476 | 0.879 | 0.816 |
| r1-load-t2-q12 | pending_before | 1476 | 0.879 | 0.816 |
| r1-load-t2-q12 | inflight_before | 1476 | 0.599 | 0.619 |
| r1-load-t2-q12 | outstanding_before | 1476 | 0.874 | 0.804 |
| r1-load-t2-q12 | outstanding_cpu_us_before | 1476 | 0.910 | 0.925 |
| r1-load-t2-q12 | chosen_load_us | 1476 | 0.901 | 0.918 |
| r1-load-t2-q28 | queue_before | 3444 | 0.917 | 0.874 |
| r1-load-t2-q28 | pending_before | 3444 | 0.917 | 0.874 |
| r1-load-t2-q28 | inflight_before | 3444 | 0.409 | 0.419 |
| r1-load-t2-q28 | outstanding_before | 3444 | 0.917 | 0.875 |
| r1-load-t2-q28 | outstanding_cpu_us_before | 3444 | 0.975 | 0.966 |
| r1-load-t2-q28 | chosen_load_us | 3444 | 0.972 | 0.961 |
| r1-load-t16-q12 | queue_before | 1476 | 0.945 | 0.942 |
| r1-load-t16-q12 | pending_before | 1476 | 0.945 | 0.942 |
| r1-load-t16-q12 | inflight_before | 1476 | -0.043 | -0.051 |
| r1-load-t16-q12 | outstanding_before | 1476 | 0.719 | 0.666 |
| r1-load-t16-q12 | outstanding_cpu_us_before | 1476 | 0.555 | 0.503 |
| r1-load-t16-q12 | chosen_load_us | 1476 | 0.547 | 0.499 |
| r1-load-t2-q36 | queue_before | 4428 | 0.993 | 0.993 |
| r1-load-t2-q36 | pending_before | 4428 | 0.993 | 0.993 |
| r1-load-t2-q36 | inflight_before | 4428 | 0.182 | 0.162 |
| r1-load-t2-q36 | outstanding_before | 4428 | 0.993 | 0.993 |
| r1-load-t2-q36 | outstanding_cpu_us_before | 4428 | 0.998 | 0.998 |
| r1-load-t2-q36 | chosen_load_us | 4428 | 0.998 | 0.997 |
| r1-load-t16-q28 | queue_before | 3444 | 0.927 | 0.915 |
| r1-load-t16-q28 | pending_before | 3444 | 0.926 | 0.915 |
| r1-load-t16-q28 | inflight_before | 3444 | 0.271 | 0.309 |
| r1-load-t16-q28 | outstanding_before | 3444 | 0.859 | 0.843 |
| r1-load-t16-q28 | outstanding_cpu_us_before | 3444 | 0.677 | 0.653 |
| r1-load-t16-q28 | chosen_load_us | 3444 | 0.674 | 0.649 |
| r1-load-t16-q4 | queue_before | 492 | 0.937 | 0.935 |
| r1-load-t16-q4 | pending_before | 492 | 0.936 | 0.935 |
| r1-load-t16-q4 | inflight_before | 492 | 0.007 | 0.015 |
| r1-load-t16-q4 | outstanding_before | 492 | 0.741 | 0.687 |
| r1-load-t16-q4 | outstanding_cpu_us_before | 492 | 0.554 | 0.550 |
| r1-load-t16-q4 | chosen_load_us | 492 | 0.546 | 0.550 |

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

矩阵计划 16 轮；有效完整运行 16 轮；异常/不完整 0 轮。所有退出码与命令见 manifest.json。

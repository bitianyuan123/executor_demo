# 实测汇总

所有时延来自实际输出；CPU 工作以线程 CPU 时钟计量。

| run | 完成请求 | 状态计数 | 均值 ms | p99 ms | 入队等待均值 ms | 算子 kernel CPU s | 迁移次数 |
|---|---:|---|---:|---:|---:|---:|---:|
| r0-split-t2-fanout | 40 | {'completed': 40} | 44.429 | 65.141 | 10.640 | 5.791 | 399.000 |
| r0-split-t2-borrow | 40 | {'completed': 40} | 44.489 | 72.136 | 10.657 | 5.948 | 896.000 |
| r0-split-t2-sticky | 40 | {'completed': 40} | 45.343 | 71.442 | 10.914 | 6.073 | 902.000 |
| r0-split-t16-fanout | 40 | {'completed': 40} | 47.584 | 74.743 | 4.402 | 5.956 | 400.000 |
| r0-split-t16-sticky | 40 | {'completed': 40} | 45.842 | 66.148 | 4.296 | 5.885 | 858.000 |
| r0-split-t16-least-load | 40 | {'completed': 40} | 43.794 | 70.181 | 3.758 | 5.737 | 869.000 |
| r0-split-t16-queue | 40 | {'completed': 40} | 49.658 | 77.190 | 4.017 | 5.918 | 823.000 |
| r0-split-t16-borrow | 40 | {'completed': 40} | 43.341 | 62.341 | 3.799 | 5.732 | 863.000 |
| r0-split-t2-least-load | 40 | {'completed': 40} | 45.586 | 84.441 | 10.906 | 6.001 | 898.000 |
| r0-split-t16-home | 40 | {'completed': 40} | 76.499 | 105.504 | 10.883 | 5.674 | 0.000 |
| r0-six-unbound-home | 40 | {'completed': 40} | 44.989 | 70.680 | 7.662 | 5.867 | 0.000 |
| r0-split-t2-home | 40 | {'completed': 40} | 74.531 | 113.134 | 22.865 | 5.674 | 0.000 |
| r1-split-t2-home | 40 | {'completed': 40} | 72.883 | 102.932 | 22.698 | 5.620 | 0.000 |
| r1-split-t2-borrow | 40 | {'completed': 40} | 44.364 | 83.994 | 10.384 | 5.808 | 874.000 |
| r1-split-t16-borrow | 40 | {'completed': 40} | 43.588 | 65.701 | 3.754 | 5.665 | 854.000 |
| r1-split-t16-fanout | 40 | {'completed': 40} | 48.516 | 83.771 | 4.404 | 5.946 | 374.000 |
| r1-split-t2-sticky | 40 | {'completed': 40} | 44.874 | 70.170 | 10.464 | 5.899 | 882.000 |
| r1-split-t2-fanout | 40 | {'completed': 40} | 44.213 | 71.007 | 10.255 | 5.620 | 374.000 |
| r1-split-t16-home | 40 | {'completed': 40} | 76.108 | 100.855 | 10.852 | 5.620 | 0.000 |
| r1-split-t16-sticky | 40 | {'completed': 40} | 46.607 | 81.929 | 4.304 | 5.924 | 837.000 |
| r1-split-t2-least-load | 40 | {'completed': 40} | 44.673 | 69.608 | 10.535 | 5.834 | 870.000 |
| r1-split-t16-queue | 40 | {'completed': 40} | 47.602 | 68.344 | 3.386 | 5.675 | 849.000 |
| r1-split-t16-least-load | 40 | {'completed': 40} | 45.758 | 73.633 | 4.060 | 5.775 | 837.000 |
| r1-six-unbound-home | 40 | {'completed': 40} | 44.788 | 61.961 | 7.037 | 5.621 | 0.000 |
| r2-split-t2-fanout | 40 | {'completed': 40} | 42.590 | 69.946 | 10.489 | 5.631 | 378.000 |
| r2-split-t16-queue | 40 | {'completed': 40} | 48.010 | 78.186 | 3.865 | 5.761 | 822.000 |
| r2-split-t16-fanout | 40 | {'completed': 40} | 44.383 | 75.809 | 3.770 | 5.612 | 378.000 |
| r2-split-t16-least-load | 40 | {'completed': 40} | 42.480 | 76.009 | 3.943 | 5.684 | 872.000 |
| r2-split-t2-sticky | 40 | {'completed': 40} | 40.602 | 61.059 | 10.024 | 5.554 | 886.000 |
| r2-split-t16-sticky | 40 | {'completed': 40} | 43.301 | 72.438 | 4.144 | 5.777 | 857.000 |
| r2-six-unbound-home | 40 | {'completed': 40} | 43.033 | 85.995 | 7.517 | 5.698 | 0.000 |
| r2-split-t2-borrow | 40 | {'completed': 40} | 42.048 | 70.704 | 10.572 | 5.784 | 868.000 |
| r2-split-t16-home | 40 | {'completed': 40} | 74.352 | 110.838 | 11.005 | 5.554 | 0.000 |
| r2-split-t2-home | 40 | {'completed': 40} | 71.137 | 104.665 | 22.918 | 5.554 | 0.000 |
| r2-split-t2-least-load | 40 | {'completed': 40} | 41.153 | 59.723 | 10.341 | 5.619 | 899.000 |
| r2-split-t16-borrow | 40 | {'completed': 40} | 43.301 | 70.162 | 4.184 | 5.746 | 845.000 |

## 负载与任务入队等待的相关性

| run | 指标 | 样本数 | Pearson | Spearman |
|---|---|---:|---:|---:|
| r0-split-t2-fanout | queue_before | 1640 | 0.808 | 0.846 |
| r0-split-t2-fanout | pending_before | 1640 | 0.808 | 0.846 |
| r0-split-t2-fanout | inflight_before | 1640 | 0.564 | 0.604 |
| r0-split-t2-fanout | outstanding_before | 1640 | 0.821 | 0.856 |
| r0-split-t2-fanout | outstanding_cpu_us_before | 1640 | 0.864 | 0.859 |
| r0-split-t2-fanout | chosen_load_us | 1640 | 0.844 | 0.845 |
| r0-split-t2-borrow | queue_before | 1640 | 0.841 | 0.820 |
| r0-split-t2-borrow | pending_before | 1640 | 0.840 | 0.818 |
| r0-split-t2-borrow | inflight_before | 1640 | 0.627 | 0.639 |
| r0-split-t2-borrow | outstanding_before | 1640 | 0.870 | 0.850 |
| r0-split-t2-borrow | outstanding_cpu_us_before | 1640 | 0.848 | 0.836 |
| r0-split-t2-borrow | chosen_load_us | 1640 | 0.823 | 0.817 |
| r0-split-t2-sticky | queue_before | 1640 | 0.778 | 0.786 |
| r0-split-t2-sticky | pending_before | 1640 | 0.777 | 0.783 |
| r0-split-t2-sticky | inflight_before | 1640 | 0.579 | 0.614 |
| r0-split-t2-sticky | outstanding_before | 1640 | 0.805 | 0.813 |
| r0-split-t2-sticky | outstanding_cpu_us_before | 1640 | 0.833 | 0.848 |
| r0-split-t2-sticky | chosen_load_us | 1640 | 0.811 | 0.831 |
| r0-split-t16-fanout | queue_before | 1640 | 0.862 | 0.932 |
| r0-split-t16-fanout | pending_before | 1640 | 0.863 | 0.936 |
| r0-split-t16-fanout | inflight_before | 1640 | -0.231 | -0.310 |
| r0-split-t16-fanout | outstanding_before | 1640 | 0.487 | 0.439 |
| r0-split-t16-fanout | outstanding_cpu_us_before | 1640 | 0.272 | 0.208 |
| r0-split-t16-fanout | chosen_load_us | 1640 | 0.270 | 0.211 |
| r0-split-t16-sticky | queue_before | 1640 | 0.823 | 0.909 |
| r0-split-t16-sticky | pending_before | 1640 | 0.825 | 0.912 |
| r0-split-t16-sticky | inflight_before | 1640 | -0.195 | -0.211 |
| r0-split-t16-sticky | outstanding_before | 1640 | 0.511 | 0.522 |
| r0-split-t16-sticky | outstanding_cpu_us_before | 1640 | 0.289 | 0.268 |
| r0-split-t16-sticky | chosen_load_us | 1640 | 0.272 | 0.257 |
| r0-split-t16-least-load | queue_before | 1640 | 0.896 | 0.922 |
| r0-split-t16-least-load | pending_before | 1640 | 0.901 | 0.926 |
| r0-split-t16-least-load | inflight_before | 1640 | -0.213 | -0.206 |
| r0-split-t16-least-load | outstanding_before | 1640 | 0.552 | 0.516 |
| r0-split-t16-least-load | outstanding_cpu_us_before | 1640 | 0.334 | 0.287 |
| r0-split-t16-least-load | chosen_load_us | 1640 | 0.327 | 0.284 |
| r0-split-t16-queue | queue_before | 1640 | 0.790 | 0.879 |
| r0-split-t16-queue | pending_before | 1640 | 0.792 | 0.888 |
| r0-split-t16-queue | inflight_before | 1640 | -0.274 | -0.316 |
| r0-split-t16-queue | outstanding_before | 1640 | 0.373 | 0.333 |
| r0-split-t16-queue | outstanding_cpu_us_before | 1640 | 0.229 | 0.243 |
| r0-split-t16-queue | chosen_load_us | 1640 | 0.470 | 0.830 |
| r0-split-t16-borrow | queue_before | 1640 | 0.879 | 0.922 |
| r0-split-t16-borrow | pending_before | 1640 | 0.880 | 0.925 |
| r0-split-t16-borrow | inflight_before | 1640 | -0.206 | -0.244 |
| r0-split-t16-borrow | outstanding_before | 1640 | 0.570 | 0.533 |
| r0-split-t16-borrow | outstanding_cpu_us_before | 1640 | 0.307 | 0.223 |
| r0-split-t16-borrow | chosen_load_us | 1640 | 0.301 | 0.223 |
| r0-split-t2-least-load | queue_before | 1640 | 0.791 | 0.786 |
| r0-split-t2-least-load | pending_before | 1640 | 0.790 | 0.784 |
| r0-split-t2-least-load | inflight_before | 1640 | 0.553 | 0.591 |
| r0-split-t2-least-load | outstanding_before | 1640 | 0.811 | 0.809 |
| r0-split-t2-least-load | outstanding_cpu_us_before | 1640 | 0.821 | 0.826 |
| r0-split-t2-least-load | chosen_load_us | 1640 | 0.802 | 0.809 |
| r0-split-t16-home | queue_before | 1640 | 0.936 | 0.941 |
| r0-split-t16-home | pending_before | 1640 | 0.936 | 0.941 |
| r0-split-t16-home | inflight_before | 1640 | -0.060 | -0.060 |
| r0-split-t16-home | outstanding_before | 1640 | 0.696 | 0.647 |
| r0-split-t16-home | outstanding_cpu_us_before | 1640 | 0.548 | 0.521 |
| r0-split-t16-home | chosen_load_us | 1640 | 0.543 | 0.518 |
| r0-six-unbound-home | queue_before | 1640 | 0.849 | 0.778 |
| r0-six-unbound-home | pending_before | 1640 | 0.844 | 0.770 |
| r0-six-unbound-home | inflight_before | 1640 | 0.511 | 0.474 |
| r0-six-unbound-home | outstanding_before | 1640 | 0.877 | 0.811 |
| r0-six-unbound-home | outstanding_cpu_us_before | 1640 | 0.820 | 0.820 |
| r0-six-unbound-home | chosen_load_us | 1640 | 0.815 | 0.817 |
| r0-split-t2-home | queue_before | 1640 | 0.886 | 0.824 |
| r0-split-t2-home | pending_before | 1640 | 0.886 | 0.824 |
| r0-split-t2-home | inflight_before | 1640 | 0.621 | 0.642 |
| r0-split-t2-home | outstanding_before | 1640 | 0.885 | 0.812 |
| r0-split-t2-home | outstanding_cpu_us_before | 1640 | 0.961 | 0.963 |
| r0-split-t2-home | chosen_load_us | 1640 | 0.957 | 0.958 |
| r1-split-t2-home | queue_before | 1640 | 0.914 | 0.831 |
| r1-split-t2-home | pending_before | 1640 | 0.914 | 0.830 |
| r1-split-t2-home | inflight_before | 1640 | 0.635 | 0.643 |
| r1-split-t2-home | outstanding_before | 1640 | 0.911 | 0.817 |
| r1-split-t2-home | outstanding_cpu_us_before | 1640 | 0.953 | 0.940 |
| r1-split-t2-home | chosen_load_us | 1640 | 0.944 | 0.933 |
| r1-split-t2-borrow | queue_before | 1640 | 0.843 | 0.831 |
| r1-split-t2-borrow | pending_before | 1640 | 0.842 | 0.830 |
| r1-split-t2-borrow | inflight_before | 1640 | 0.626 | 0.651 |
| r1-split-t2-borrow | outstanding_before | 1640 | 0.872 | 0.865 |
| r1-split-t2-borrow | outstanding_cpu_us_before | 1640 | 0.859 | 0.869 |
| r1-split-t2-borrow | chosen_load_us | 1640 | 0.831 | 0.853 |
| r1-split-t16-borrow | queue_before | 1640 | 0.892 | 0.922 |
| r1-split-t16-borrow | pending_before | 1640 | 0.893 | 0.925 |
| r1-split-t16-borrow | inflight_before | 1640 | -0.207 | -0.217 |
| r1-split-t16-borrow | outstanding_before | 1640 | 0.571 | 0.533 |
| r1-split-t16-borrow | outstanding_cpu_us_before | 1640 | 0.333 | 0.300 |
| r1-split-t16-borrow | chosen_load_us | 1640 | 0.315 | 0.289 |
| r1-split-t16-fanout | queue_before | 1640 | 0.849 | 0.930 |
| r1-split-t16-fanout | pending_before | 1640 | 0.848 | 0.933 |
| r1-split-t16-fanout | inflight_before | 1640 | -0.227 | -0.296 |
| r1-split-t16-fanout | outstanding_before | 1640 | 0.481 | 0.449 |
| r1-split-t16-fanout | outstanding_cpu_us_before | 1640 | 0.258 | 0.193 |
| r1-split-t16-fanout | chosen_load_us | 1640 | 0.251 | 0.193 |
| r1-split-t2-sticky | queue_before | 1640 | 0.852 | 0.832 |
| r1-split-t2-sticky | pending_before | 1640 | 0.850 | 0.828 |
| r1-split-t2-sticky | inflight_before | 1640 | 0.619 | 0.634 |
| r1-split-t2-sticky | outstanding_before | 1640 | 0.879 | 0.859 |
| r1-split-t2-sticky | outstanding_cpu_us_before | 1640 | 0.828 | 0.826 |
| r1-split-t2-sticky | chosen_load_us | 1640 | 0.801 | 0.810 |
| r1-split-t2-fanout | queue_before | 1640 | 0.889 | 0.884 |
| r1-split-t2-fanout | pending_before | 1640 | 0.889 | 0.884 |
| r1-split-t2-fanout | inflight_before | 1640 | 0.610 | 0.622 |
| r1-split-t2-fanout | outstanding_before | 1640 | 0.904 | 0.898 |
| r1-split-t2-fanout | outstanding_cpu_us_before | 1640 | 0.875 | 0.865 |
| r1-split-t2-fanout | chosen_load_us | 1640 | 0.854 | 0.852 |
| r1-split-t16-home | queue_before | 1640 | 0.945 | 0.950 |
| r1-split-t16-home | pending_before | 1640 | 0.945 | 0.950 |
| r1-split-t16-home | inflight_before | 1640 | -0.081 | -0.078 |
| r1-split-t16-home | outstanding_before | 1640 | 0.686 | 0.628 |
| r1-split-t16-home | outstanding_cpu_us_before | 1640 | 0.517 | 0.472 |
| r1-split-t16-home | chosen_load_us | 1640 | 0.509 | 0.467 |
| r1-split-t16-sticky | queue_before | 1640 | 0.829 | 0.903 |
| r1-split-t16-sticky | pending_before | 1640 | 0.833 | 0.910 |
| r1-split-t16-sticky | inflight_before | 1640 | -0.189 | -0.220 |
| r1-split-t16-sticky | outstanding_before | 1640 | 0.554 | 0.546 |
| r1-split-t16-sticky | outstanding_cpu_us_before | 1640 | 0.312 | 0.289 |
| r1-split-t16-sticky | chosen_load_us | 1640 | 0.295 | 0.281 |
| r1-split-t2-least-load | queue_before | 1640 | 0.846 | 0.834 |
| r1-split-t2-least-load | pending_before | 1640 | 0.842 | 0.828 |
| r1-split-t2-least-load | inflight_before | 1640 | 0.621 | 0.631 |
| r1-split-t2-least-load | outstanding_before | 1640 | 0.871 | 0.858 |
| r1-split-t2-least-load | outstanding_cpu_us_before | 1640 | 0.835 | 0.825 |
| r1-split-t2-least-load | chosen_load_us | 1640 | 0.805 | 0.807 |
| r1-split-t16-queue | queue_before | 1640 | 0.881 | 0.903 |
| r1-split-t16-queue | pending_before | 1640 | 0.888 | 0.909 |
| r1-split-t16-queue | inflight_before | 1640 | -0.334 | -0.350 |
| r1-split-t16-queue | outstanding_before | 1640 | 0.388 | 0.321 |
| r1-split-t16-queue | outstanding_cpu_us_before | 1640 | 0.206 | 0.195 |
| r1-split-t16-queue | chosen_load_us | 1640 | 0.555 | 0.859 |
| r1-split-t16-least-load | queue_before | 1640 | 0.865 | 0.915 |
| r1-split-t16-least-load | pending_before | 1640 | 0.868 | 0.918 |
| r1-split-t16-least-load | inflight_before | 1640 | -0.222 | -0.250 |
| r1-split-t16-least-load | outstanding_before | 1640 | 0.546 | 0.518 |
| r1-split-t16-least-load | outstanding_cpu_us_before | 1640 | 0.302 | 0.247 |
| r1-split-t16-least-load | chosen_load_us | 1640 | 0.289 | 0.237 |
| r1-six-unbound-home | queue_before | 1640 | 0.887 | 0.798 |
| r1-six-unbound-home | pending_before | 1640 | 0.886 | 0.796 |
| r1-six-unbound-home | inflight_before | 1640 | 0.558 | 0.477 |
| r1-six-unbound-home | outstanding_before | 1640 | 0.928 | 0.829 |
| r1-six-unbound-home | outstanding_cpu_us_before | 1640 | 0.851 | 0.808 |
| r1-six-unbound-home | chosen_load_us | 1640 | 0.845 | 0.805 |
| r2-split-t2-fanout | queue_before | 1640 | 0.837 | 0.865 |
| r2-split-t2-fanout | pending_before | 1640 | 0.836 | 0.864 |
| r2-split-t2-fanout | inflight_before | 1640 | 0.565 | 0.609 |
| r2-split-t2-fanout | outstanding_before | 1640 | 0.845 | 0.875 |
| r2-split-t2-fanout | outstanding_cpu_us_before | 1640 | 0.895 | 0.859 |
| r2-split-t2-fanout | chosen_load_us | 1640 | 0.880 | 0.844 |
| r2-split-t16-queue | queue_before | 1640 | 0.835 | 0.896 |
| r2-split-t16-queue | pending_before | 1640 | 0.844 | 0.903 |
| r2-split-t16-queue | inflight_before | 1640 | -0.326 | -0.365 |
| r2-split-t16-queue | outstanding_before | 1640 | 0.358 | 0.280 |
| r2-split-t16-queue | outstanding_cpu_us_before | 1640 | 0.225 | 0.185 |
| r2-split-t16-queue | chosen_load_us | 1640 | 0.489 | 0.855 |
| r2-split-t16-fanout | queue_before | 1640 | 0.920 | 0.946 |
| r2-split-t16-fanout | pending_before | 1640 | 0.924 | 0.951 |
| r2-split-t16-fanout | inflight_before | 1640 | -0.282 | -0.361 |
| r2-split-t16-fanout | outstanding_before | 1640 | 0.478 | 0.393 |
| r2-split-t16-fanout | outstanding_cpu_us_before | 1640 | 0.249 | 0.171 |
| r2-split-t16-fanout | chosen_load_us | 1640 | 0.250 | 0.177 |
| r2-split-t16-least-load | queue_before | 1640 | 0.869 | 0.913 |
| r2-split-t16-least-load | pending_before | 1640 | 0.872 | 0.917 |
| r2-split-t16-least-load | inflight_before | 1640 | -0.201 | -0.245 |
| r2-split-t16-least-load | outstanding_before | 1640 | 0.559 | 0.508 |
| r2-split-t16-least-load | outstanding_cpu_us_before | 1640 | 0.358 | 0.286 |
| r2-split-t16-least-load | chosen_load_us | 1640 | 0.357 | 0.289 |
| r2-split-t2-sticky | queue_before | 1640 | 0.860 | 0.845 |
| r2-split-t2-sticky | pending_before | 1640 | 0.859 | 0.843 |
| r2-split-t2-sticky | inflight_before | 1640 | 0.642 | 0.661 |
| r2-split-t2-sticky | outstanding_before | 1640 | 0.887 | 0.872 |
| r2-split-t2-sticky | outstanding_cpu_us_before | 1640 | 0.906 | 0.877 |
| r2-split-t2-sticky | chosen_load_us | 1640 | 0.883 | 0.860 |
| r2-split-t16-sticky | queue_before | 1640 | 0.838 | 0.907 |
| r2-split-t16-sticky | pending_before | 1640 | 0.839 | 0.914 |
| r2-split-t16-sticky | inflight_before | 1640 | -0.177 | -0.196 |
| r2-split-t16-sticky | outstanding_before | 1640 | 0.523 | 0.518 |
| r2-split-t16-sticky | outstanding_cpu_us_before | 1640 | 0.346 | 0.303 |
| r2-split-t16-sticky | chosen_load_us | 1640 | 0.337 | 0.301 |
| r2-six-unbound-home | queue_before | 1640 | 0.836 | 0.785 |
| r2-six-unbound-home | pending_before | 1640 | 0.839 | 0.786 |
| r2-six-unbound-home | inflight_before | 1640 | 0.486 | 0.465 |
| r2-six-unbound-home | outstanding_before | 1640 | 0.855 | 0.798 |
| r2-six-unbound-home | outstanding_cpu_us_before | 1640 | 0.808 | 0.787 |
| r2-six-unbound-home | chosen_load_us | 1640 | 0.806 | 0.781 |
| r2-split-t2-borrow | queue_before | 1640 | 0.798 | 0.815 |
| r2-split-t2-borrow | pending_before | 1640 | 0.796 | 0.811 |
| r2-split-t2-borrow | inflight_before | 1640 | 0.581 | 0.623 |
| r2-split-t2-borrow | outstanding_before | 1640 | 0.820 | 0.837 |
| r2-split-t2-borrow | outstanding_cpu_us_before | 1640 | 0.874 | 0.864 |
| r2-split-t2-borrow | chosen_load_us | 1640 | 0.859 | 0.854 |
| r2-split-t16-home | queue_before | 1640 | 0.945 | 0.943 |
| r2-split-t16-home | pending_before | 1640 | 0.945 | 0.943 |
| r2-split-t16-home | inflight_before | 1640 | -0.062 | -0.067 |
| r2-split-t16-home | outstanding_before | 1640 | 0.684 | 0.599 |
| r2-split-t16-home | outstanding_cpu_us_before | 1640 | 0.507 | 0.498 |
| r2-split-t16-home | chosen_load_us | 1640 | 0.507 | 0.496 |
| r2-split-t2-home | queue_before | 1640 | 0.886 | 0.856 |
| r2-split-t2-home | pending_before | 1640 | 0.885 | 0.852 |
| r2-split-t2-home | inflight_before | 1640 | 0.648 | 0.676 |
| r2-split-t2-home | outstanding_before | 1640 | 0.887 | 0.841 |
| r2-split-t2-home | outstanding_cpu_us_before | 1640 | 0.969 | 0.963 |
| r2-split-t2-home | chosen_load_us | 1640 | 0.963 | 0.957 |
| r2-split-t2-least-load | queue_before | 1640 | 0.870 | 0.852 |
| r2-split-t2-least-load | pending_before | 1640 | 0.868 | 0.850 |
| r2-split-t2-least-load | inflight_before | 1640 | 0.607 | 0.619 |
| r2-split-t2-least-load | outstanding_before | 1640 | 0.889 | 0.871 |
| r2-split-t2-least-load | outstanding_cpu_us_before | 1640 | 0.880 | 0.857 |
| r2-split-t2-least-load | chosen_load_us | 1640 | 0.855 | 0.839 |
| r2-split-t16-borrow | queue_before | 1640 | 0.853 | 0.919 |
| r2-split-t16-borrow | pending_before | 1640 | 0.851 | 0.920 |
| r2-split-t16-borrow | inflight_before | 1640 | -0.182 | -0.228 |
| r2-split-t16-borrow | outstanding_before | 1640 | 0.550 | 0.519 |
| r2-split-t16-borrow | outstanding_cpu_us_before | 1640 | 0.354 | 0.307 |
| r2-split-t16-borrow | chosen_load_us | 1640 | 0.350 | 0.308 |

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

矩阵计划 36 轮；有效完整运行 36 轮；异常/不完整 0 轮。所有退出码与命令见 manifest.json。

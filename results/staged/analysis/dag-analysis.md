# 实测汇总

所有时延来自实际输出；CPU 工作以线程 CPU 时钟计量。

| run | 完成请求 | 状态计数 | 均值 ms | p99 ms | 入队等待均值 ms | 算子 kernel CPU s | 迁移次数 |
|---|---:|---|---:|---:|---:|---:|---:|
| r0-base-none-t16 | 36 | {'completed': 36} | 38.695 | 56.846 | 5.748 | 4.655 | 0.000 |
| r0-shape-late-borrow | 36 | {'completed': 36} | 36.692 | 51.152 | 4.351 | 4.410 | 1101.000 |
| r0-base-cluster-t16 | 36 | {'completed': 36} | 64.274 | 77.276 | 10.498 | 4.401 | 0.000 |
| r0-base-none-t2 | 36 | {'completed': 36} | 61.726 | 93.654 | 13.681 | 4.408 | 0.000 |
| r0-shape-even-home | 36 | {'completed': 36} | 64.054 | 72.847 | 6.019 | 4.334 | 0.000 |
| r0-base-numa-t2 | 36 | {'completed': 36} | 60.636 | 61.280 | 13.383 | 4.333 | 0.000 |
| r0-base-numa-t16 | 36 | {'completed': 36} | 36.105 | 51.154 | 5.304 | 4.503 | 0.000 |
| r0-shape-late-home | 36 | {'completed': 36} | 63.116 | 74.659 | 9.371 | 4.333 | 0.000 |
| r0-stress-tail-sticky | 36 | {'completed': 36} | 48.184 | 74.329 | 4.690 | 5.088 | 1188.000 |
| r0-shape-deep-borrow | 36 | {'completed': 36} | 50.722 | 61.758 | 0.148 | 4.482 | 812.000 |
| r0-stress-rpc-sticky | 36 | {'completed': 36} | 99.498 | 114.455 | 3.420 | 4.486 | 1363.000 |
| r0-shape-even-borrow | 36 | {'completed': 36} | 35.681 | 44.941 | 2.692 | 4.415 | 1095.000 |
| r0-route-sticky-all | 36 | {'completed': 36} | 37.428 | 56.528 | 4.872 | 4.580 | 1026.000 |
| r0-route-borrow-all | 36 | {'completed': 36} | 35.839 | 50.002 | 4.580 | 4.439 | 1066.000 |
| r0-base-cluster-t2 | 36 | {'completed': 36} | 61.392 | 73.343 | 13.751 | 4.333 | 0.000 |
| r0-route-fanout-initial | 36 | {'completed': 36} | 45.649 | 63.069 | 5.291 | 4.503 | 706.000 |
| r0-shape-deep-home | 36 | {'completed': 36} | 65.459 | 81.512 | 0.433 | 4.345 | 0.000 |
| r0-stress-tail-home | 36 | {'completed': 36} | 75.897 | 104.742 | 10.414 | 4.949 | 0.000 |
| r0-stress-rpc-home | 36 | {'completed': 36} | 110.251 | 118.480 | 6.662 | 4.341 | 0.000 |
| r0-route-fanout-all | 36 | {'completed': 36} | 37.216 | 61.290 | 4.862 | 4.508 | 1031.000 |
| r1-base-none-t16 | 36 | {'completed': 36} | 37.571 | 51.767 | 5.806 | 4.634 | 0.000 |
| r1-shape-even-borrow | 36 | {'completed': 36} | 36.204 | 46.297 | 2.677 | 4.392 | 1094.000 |
| r1-stress-rpc-sticky | 36 | {'completed': 36} | 101.974 | 119.923 | 3.555 | 4.474 | 1350.000 |
| r1-route-fanout-all | 36 | {'completed': 36} | 36.111 | 52.016 | 4.581 | 4.446 | 1027.000 |
| r1-stress-rpc-home | 36 | {'completed': 36} | 110.743 | 121.042 | 6.951 | 4.334 | 0.000 |
| r1-base-numa-t2 | 36 | {'completed': 36} | 61.518 | 83.520 | 13.761 | 4.393 | 0.000 |
| r1-stress-tail-sticky | 36 | {'completed': 36} | 42.887 | 68.841 | 4.488 | 4.843 | 1187.000 |
| r1-stress-tail-home | 36 | {'completed': 36} | 71.625 | 98.774 | 10.443 | 4.771 | 0.000 |
| r1-base-none-t2 | 36 | {'completed': 36} | 61.693 | 91.665 | 13.648 | 4.405 | 0.000 |
| r1-shape-late-home | 36 | {'completed': 36} | 62.393 | 68.038 | 9.428 | 4.338 | 0.000 |
| r1-shape-even-home | 36 | {'completed': 36} | 63.089 | 76.207 | 6.021 | 4.346 | 0.000 |
| r1-route-fanout-initial | 36 | {'completed': 36} | 45.514 | 62.595 | 5.391 | 4.509 | 706.000 |
| r1-shape-deep-borrow | 36 | {'completed': 36} | 49.247 | 57.005 | 0.126 | 4.334 | 844.000 |
| r1-base-cluster-t2 | 36 | {'completed': 36} | 60.838 | 62.844 | 13.487 | 4.333 | 0.000 |
| r1-shape-late-borrow | 36 | {'completed': 36} | 35.298 | 43.506 | 4.201 | 4.369 | 1103.000 |
| r1-route-borrow-all | 36 | {'completed': 36} | 37.362 | 54.664 | 4.908 | 4.570 | 1075.000 |
| r1-base-cluster-t16 | 36 | {'completed': 36} | 63.385 | 66.665 | 9.955 | 4.333 | 0.000 |
| r1-shape-deep-home | 36 | {'completed': 36} | 65.406 | 67.995 | 0.433 | 4.333 | 0.000 |
| r1-base-numa-t16 | 36 | {'completed': 36} | 35.496 | 49.669 | 4.992 | 4.409 | 0.000 |
| r1-route-sticky-all | 36 | {'completed': 36} | 36.728 | 53.968 | 4.639 | 4.452 | 1026.000 |

## 负载与任务入队等待的相关性

| run | 指标 | 样本数 | Pearson | Spearman |
|---|---|---:|---:|---:|
| r0-base-none-t16 | queue_before | 2268 | 0.892 | 0.971 |
| r0-base-none-t16 | pending_before | 2268 | 0.890 | 0.971 |
| r0-base-none-t16 | inflight_before | 2268 | -0.165 | -0.319 |
| r0-base-none-t16 | outstanding_before | 2268 | 0.892 | 0.964 |
| r0-base-none-t16 | outstanding_cpu_us_before | 2268 | 0.892 | 0.966 |
| r0-base-none-t16 | chosen_load_us | 2268 | 0.892 | 0.971 |
| r0-shape-late-borrow | queue_before | 2268 | 0.941 | 0.975 |
| r0-shape-late-borrow | pending_before | 2268 | 0.940 | 0.973 |
| r0-shape-late-borrow | inflight_before | 2268 | 0.476 | 0.507 |
| r0-shape-late-borrow | outstanding_before | 2268 | 0.935 | 0.954 |
| r0-shape-late-borrow | outstanding_cpu_us_before | 2268 | 0.937 | 0.960 |
| r0-shape-late-borrow | chosen_load_us | 2268 | 0.941 | 0.975 |
| r0-base-cluster-t16 | queue_before | 2268 | 0.972 | 0.988 |
| r0-base-cluster-t16 | pending_before | 2268 | 0.972 | 0.988 |
| r0-base-cluster-t16 | inflight_before | 2268 | -0.143 | -0.225 |
| r0-base-cluster-t16 | outstanding_before | 2268 | 0.972 | 0.982 |
| r0-base-cluster-t16 | outstanding_cpu_us_before | 2268 | 0.973 | 0.984 |
| r0-base-cluster-t16 | chosen_load_us | 2268 | 0.972 | 0.988 |
| r0-base-none-t2 | queue_before | 2268 | 0.978 | 0.992 |
| r0-base-none-t2 | pending_before | 2268 | 0.979 | 0.992 |
| r0-base-none-t2 | inflight_before | 2268 | -0.267 | -0.379 |
| r0-base-none-t2 | outstanding_before | 2268 | 0.979 | 0.987 |
| r0-base-none-t2 | outstanding_cpu_us_before | 2268 | 0.979 | 0.989 |
| r0-base-none-t2 | chosen_load_us | 2268 | 0.978 | 0.992 |
| r0-shape-even-home | queue_before | 2268 | 0.978 | 0.989 |
| r0-shape-even-home | pending_before | 2268 | 0.976 | 0.989 |
| r0-shape-even-home | inflight_before | 2268 | 0.241 | 0.237 |
| r0-shape-even-home | outstanding_before | 2268 | 0.970 | 0.980 |
| r0-shape-even-home | outstanding_cpu_us_before | 2268 | 0.976 | 0.984 |
| r0-shape-even-home | chosen_load_us | 2268 | 0.978 | 0.989 |
| r0-base-numa-t2 | queue_before | 2268 | 0.999 | 0.995 |
| r0-base-numa-t2 | pending_before | 2268 | 0.999 | 0.995 |
| r0-base-numa-t2 | inflight_before | 2268 | -0.355 | -0.447 |
| r0-base-numa-t2 | outstanding_before | 2268 | 0.999 | 0.990 |
| r0-base-numa-t2 | outstanding_cpu_us_before | 2268 | 0.999 | 0.992 |
| r0-base-numa-t2 | chosen_load_us | 2268 | 0.999 | 0.995 |
| r0-base-numa-t16 | queue_before | 2268 | 0.925 | 0.979 |
| r0-base-numa-t16 | pending_before | 2268 | 0.923 | 0.978 |
| r0-base-numa-t16 | inflight_before | 2268 | -0.220 | -0.350 |
| r0-base-numa-t16 | outstanding_before | 2268 | 0.923 | 0.972 |
| r0-base-numa-t16 | outstanding_cpu_us_before | 2268 | 0.924 | 0.974 |
| r0-base-numa-t16 | chosen_load_us | 2268 | 0.925 | 0.979 |
| r0-shape-late-home | queue_before | 2268 | 0.994 | 0.992 |
| r0-shape-late-home | pending_before | 2268 | 0.993 | 0.993 |
| r0-shape-late-home | inflight_before | 2268 | 0.574 | 0.649 |
| r0-shape-late-home | outstanding_before | 2268 | 0.991 | 0.987 |
| r0-shape-late-home | outstanding_cpu_us_before | 2268 | 0.993 | 0.989 |
| r0-shape-late-home | chosen_load_us | 2268 | 0.994 | 0.992 |
| r0-stress-tail-sticky | queue_before | 2268 | 0.909 | 0.969 |
| r0-stress-tail-sticky | pending_before | 2268 | 0.908 | 0.970 |
| r0-stress-tail-sticky | inflight_before | 2268 | 0.093 | 0.044 |
| r0-stress-tail-sticky | outstanding_before | 2268 | 0.903 | 0.954 |
| r0-stress-tail-sticky | outstanding_cpu_us_before | 2268 | 0.903 | 0.954 |
| r0-stress-tail-sticky | chosen_load_us | 2268 | 0.903 | 0.954 |
| r0-shape-deep-borrow | queue_before | 2268 | 0.372 | 0.332 |
| r0-shape-deep-borrow | pending_before | 2268 | 0.404 | 0.353 |
| r0-shape-deep-borrow | inflight_before | 2268 | -0.082 | -0.419 |
| r0-shape-deep-borrow | outstanding_before | 2268 | 0.024 | -0.336 |
| r0-shape-deep-borrow | outstanding_cpu_us_before | 2268 | 0.008 | -0.341 |
| r0-shape-deep-borrow | chosen_load_us | 2268 | 0.370 | 0.331 |
| r0-stress-rpc-sticky | queue_before | 3034 | 0.915 | 0.950 |
| r0-stress-rpc-sticky | pending_before | 3034 | 0.914 | 0.950 |
| r0-stress-rpc-sticky | inflight_before | 3034 | 0.179 | 0.246 |
| r0-stress-rpc-sticky | outstanding_before | 3034 | 0.902 | 0.926 |
| r0-stress-rpc-sticky | outstanding_cpu_us_before | 3034 | 0.902 | 0.926 |
| r0-stress-rpc-sticky | chosen_load_us | 3034 | 0.902 | 0.926 |
| r0-shape-even-borrow | queue_before | 2268 | 0.886 | 0.959 |
| r0-shape-even-borrow | pending_before | 2268 | 0.889 | 0.959 |
| r0-shape-even-borrow | inflight_before | 2268 | 0.103 | 0.048 |
| r0-shape-even-borrow | outstanding_before | 2268 | 0.862 | 0.921 |
| r0-shape-even-borrow | outstanding_cpu_us_before | 2268 | 0.882 | 0.935 |
| r0-shape-even-borrow | chosen_load_us | 2268 | 0.886 | 0.959 |
| r0-route-sticky-all | queue_before | 2268 | 0.865 | 0.964 |
| r0-route-sticky-all | pending_before | 2268 | 0.866 | 0.963 |
| r0-route-sticky-all | inflight_before | 2268 | -0.132 | -0.264 |
| r0-route-sticky-all | outstanding_before | 2268 | 0.866 | 0.940 |
| r0-route-sticky-all | outstanding_cpu_us_before | 2268 | 0.868 | 0.946 |
| r0-route-sticky-all | chosen_load_us | 2268 | 0.866 | 0.964 |
| r0-route-borrow-all | queue_before | 2268 | 0.918 | 0.972 |
| r0-route-borrow-all | pending_before | 2268 | 0.917 | 0.971 |
| r0-route-borrow-all | inflight_before | 2268 | -0.089 | -0.167 |
| r0-route-borrow-all | outstanding_before | 2268 | 0.914 | 0.950 |
| r0-route-borrow-all | outstanding_cpu_us_before | 2268 | 0.915 | 0.957 |
| r0-route-borrow-all | chosen_load_us | 2268 | 0.918 | 0.972 |
| r0-base-cluster-t2 | queue_before | 2268 | 0.992 | 0.992 |
| r0-base-cluster-t2 | pending_before | 2268 | 0.992 | 0.992 |
| r0-base-cluster-t2 | inflight_before | 2268 | -0.177 | -0.253 |
| r0-base-cluster-t2 | outstanding_before | 2268 | 0.991 | 0.986 |
| r0-base-cluster-t2 | outstanding_cpu_us_before | 2268 | 0.992 | 0.988 |
| r0-base-cluster-t2 | chosen_load_us | 2268 | 0.992 | 0.992 |
| r0-route-fanout-initial | queue_before | 2268 | 0.888 | 0.969 |
| r0-route-fanout-initial | pending_before | 2268 | 0.885 | 0.969 |
| r0-route-fanout-initial | inflight_before | 2268 | -0.103 | -0.190 |
| r0-route-fanout-initial | outstanding_before | 2268 | 0.880 | 0.954 |
| r0-route-fanout-initial | outstanding_cpu_us_before | 2268 | 0.887 | 0.960 |
| r0-route-fanout-initial | chosen_load_us | 2268 | 0.888 | 0.969 |
| r0-shape-deep-home | queue_before | 2268 | 0.831 | 0.816 |
| r0-shape-deep-home | pending_before | 2268 | 0.836 | 0.842 |
| r0-shape-deep-home | inflight_before | 2268 | -0.485 | -0.608 |
| r0-shape-deep-home | outstanding_before | 2268 | 0.071 | -0.112 |
| r0-shape-deep-home | outstanding_cpu_us_before | 2268 | -0.004 | -0.162 |
| r0-shape-deep-home | chosen_load_us | 2268 | 0.831 | 0.816 |
| r0-stress-tail-home | queue_before | 2268 | 0.982 | 0.989 |
| r0-stress-tail-home | pending_before | 2268 | 0.981 | 0.989 |
| r0-stress-tail-home | inflight_before | 2268 | -0.331 | -0.420 |
| r0-stress-tail-home | outstanding_before | 2268 | 0.981 | 0.984 |
| r0-stress-tail-home | outstanding_cpu_us_before | 2268 | 0.981 | 0.984 |
| r0-stress-tail-home | chosen_load_us | 2268 | 0.981 | 0.984 |
| r0-stress-rpc-home | queue_before | 3034 | 0.994 | 0.972 |
| r0-stress-rpc-home | pending_before | 3034 | 0.994 | 0.972 |
| r0-stress-rpc-home | inflight_before | 3034 | -0.187 | -0.226 |
| r0-stress-rpc-home | outstanding_before | 3034 | 0.979 | 0.935 |
| r0-stress-rpc-home | outstanding_cpu_us_before | 3034 | 0.979 | 0.935 |
| r0-stress-rpc-home | chosen_load_us | 3034 | 0.979 | 0.935 |
| r0-route-fanout-all | queue_before | 2268 | 0.876 | 0.967 |
| r0-route-fanout-all | pending_before | 2268 | 0.875 | 0.965 |
| r0-route-fanout-all | inflight_before | 2268 | -0.129 | -0.204 |
| r0-route-fanout-all | outstanding_before | 2268 | 0.869 | 0.940 |
| r0-route-fanout-all | outstanding_cpu_us_before | 2268 | 0.871 | 0.947 |
| r0-route-fanout-all | chosen_load_us | 2268 | 0.876 | 0.967 |
| r1-base-none-t16 | queue_before | 2268 | 0.887 | 0.972 |
| r1-base-none-t16 | pending_before | 2268 | 0.886 | 0.972 |
| r1-base-none-t16 | inflight_before | 2268 | -0.214 | -0.374 |
| r1-base-none-t16 | outstanding_before | 2268 | 0.888 | 0.966 |
| r1-base-none-t16 | outstanding_cpu_us_before | 2268 | 0.889 | 0.968 |
| r1-base-none-t16 | chosen_load_us | 2268 | 0.887 | 0.972 |
| r1-shape-even-borrow | queue_before | 2268 | 0.892 | 0.956 |
| r1-shape-even-borrow | pending_before | 2268 | 0.898 | 0.956 |
| r1-shape-even-borrow | inflight_before | 2268 | 0.124 | 0.065 |
| r1-shape-even-borrow | outstanding_before | 2268 | 0.873 | 0.916 |
| r1-shape-even-borrow | outstanding_cpu_us_before | 2268 | 0.896 | 0.932 |
| r1-shape-even-borrow | chosen_load_us | 2268 | 0.893 | 0.956 |
| r1-stress-rpc-sticky | queue_before | 2990 | 0.925 | 0.952 |
| r1-stress-rpc-sticky | pending_before | 2990 | 0.925 | 0.952 |
| r1-stress-rpc-sticky | inflight_before | 2990 | 0.248 | 0.219 |
| r1-stress-rpc-sticky | outstanding_before | 2990 | 0.923 | 0.933 |
| r1-stress-rpc-sticky | outstanding_cpu_us_before | 2990 | 0.923 | 0.933 |
| r1-stress-rpc-sticky | chosen_load_us | 2990 | 0.923 | 0.933 |
| r1-route-fanout-all | queue_before | 2268 | 0.929 | 0.976 |
| r1-route-fanout-all | pending_before | 2268 | 0.929 | 0.975 |
| r1-route-fanout-all | inflight_before | 2268 | -0.170 | -0.299 |
| r1-route-fanout-all | outstanding_before | 2268 | 0.928 | 0.951 |
| r1-route-fanout-all | outstanding_cpu_us_before | 2268 | 0.930 | 0.957 |
| r1-route-fanout-all | chosen_load_us | 2268 | 0.930 | 0.976 |
| r1-stress-rpc-home | queue_before | 2990 | 0.987 | 0.974 |
| r1-stress-rpc-home | pending_before | 2990 | 0.987 | 0.974 |
| r1-stress-rpc-home | inflight_before | 2990 | -0.169 | -0.193 |
| r1-stress-rpc-home | outstanding_before | 2990 | 0.972 | 0.941 |
| r1-stress-rpc-home | outstanding_cpu_us_before | 2990 | 0.972 | 0.941 |
| r1-stress-rpc-home | chosen_load_us | 2990 | 0.972 | 0.941 |
| r1-base-numa-t2 | queue_before | 2268 | 0.981 | 0.991 |
| r1-base-numa-t2 | pending_before | 2268 | 0.982 | 0.991 |
| r1-base-numa-t2 | inflight_before | 2268 | -0.293 | -0.384 |
| r1-base-numa-t2 | outstanding_before | 2268 | 0.981 | 0.986 |
| r1-base-numa-t2 | outstanding_cpu_us_before | 2268 | 0.982 | 0.988 |
| r1-base-numa-t2 | chosen_load_us | 2268 | 0.981 | 0.991 |
| r1-stress-tail-sticky | queue_before | 2268 | 0.924 | 0.970 |
| r1-stress-tail-sticky | pending_before | 2268 | 0.923 | 0.971 |
| r1-stress-tail-sticky | inflight_before | 2268 | 0.163 | 0.092 |
| r1-stress-tail-sticky | outstanding_before | 2268 | 0.923 | 0.959 |
| r1-stress-tail-sticky | outstanding_cpu_us_before | 2268 | 0.923 | 0.959 |
| r1-stress-tail-sticky | chosen_load_us | 2268 | 0.923 | 0.959 |
| r1-stress-tail-home | queue_before | 2268 | 0.980 | 0.989 |
| r1-stress-tail-home | pending_before | 2268 | 0.979 | 0.989 |
| r1-stress-tail-home | inflight_before | 2268 | -0.230 | -0.314 |
| r1-stress-tail-home | outstanding_before | 2268 | 0.979 | 0.984 |
| r1-stress-tail-home | outstanding_cpu_us_before | 2268 | 0.979 | 0.984 |
| r1-stress-tail-home | chosen_load_us | 2268 | 0.979 | 0.984 |
| r1-base-none-t2 | queue_before | 2268 | 0.984 | 0.992 |
| r1-base-none-t2 | pending_before | 2268 | 0.984 | 0.992 |
| r1-base-none-t2 | inflight_before | 2268 | -0.366 | -0.468 |
| r1-base-none-t2 | outstanding_before | 2268 | 0.984 | 0.987 |
| r1-base-none-t2 | outstanding_cpu_us_before | 2268 | 0.985 | 0.989 |
| r1-base-none-t2 | chosen_load_us | 2268 | 0.984 | 0.992 |
| r1-shape-late-home | queue_before | 2268 | 0.995 | 0.994 |
| r1-shape-late-home | pending_before | 2268 | 0.995 | 0.995 |
| r1-shape-late-home | inflight_before | 2268 | 0.633 | 0.691 |
| r1-shape-late-home | outstanding_before | 2268 | 0.993 | 0.990 |
| r1-shape-late-home | outstanding_cpu_us_before | 2268 | 0.995 | 0.991 |
| r1-shape-late-home | chosen_load_us | 2268 | 0.995 | 0.994 |
| r1-shape-even-home | queue_before | 2268 | 0.971 | 0.988 |
| r1-shape-even-home | pending_before | 2268 | 0.970 | 0.988 |
| r1-shape-even-home | inflight_before | 2268 | 0.251 | 0.232 |
| r1-shape-even-home | outstanding_before | 2268 | 0.963 | 0.979 |
| r1-shape-even-home | outstanding_cpu_us_before | 2268 | 0.969 | 0.983 |
| r1-shape-even-home | chosen_load_us | 2268 | 0.971 | 0.988 |
| r1-route-fanout-initial | queue_before | 2268 | 0.879 | 0.966 |
| r1-route-fanout-initial | pending_before | 2268 | 0.878 | 0.966 |
| r1-route-fanout-initial | inflight_before | 2268 | -0.146 | -0.221 |
| r1-route-fanout-initial | outstanding_before | 2268 | 0.872 | 0.950 |
| r1-route-fanout-initial | outstanding_cpu_us_before | 2268 | 0.879 | 0.956 |
| r1-route-fanout-initial | chosen_load_us | 2268 | 0.879 | 0.966 |
| r1-shape-deep-borrow | queue_before | 2268 | 0.430 | 0.342 |
| r1-shape-deep-borrow | pending_before | 2268 | 0.443 | 0.374 |
| r1-shape-deep-borrow | inflight_before | 2268 | -0.064 | -0.416 |
| r1-shape-deep-borrow | outstanding_before | 2268 | 0.063 | -0.319 |
| r1-shape-deep-borrow | outstanding_cpu_us_before | 2268 | 0.031 | -0.326 |
| r1-shape-deep-borrow | chosen_load_us | 2268 | 0.430 | 0.341 |
| r1-base-cluster-t2 | queue_before | 2268 | 0.998 | 0.994 |
| r1-base-cluster-t2 | pending_before | 2268 | 0.998 | 0.994 |
| r1-base-cluster-t2 | inflight_before | 2268 | -0.193 | -0.275 |
| r1-base-cluster-t2 | outstanding_before | 2268 | 0.998 | 0.989 |
| r1-base-cluster-t2 | outstanding_cpu_us_before | 2268 | 0.999 | 0.991 |
| r1-base-cluster-t2 | chosen_load_us | 2268 | 0.998 | 0.994 |
| r1-shape-late-borrow | queue_before | 2268 | 0.971 | 0.977 |
| r1-shape-late-borrow | pending_before | 2268 | 0.971 | 0.976 |
| r1-shape-late-borrow | inflight_before | 2268 | 0.520 | 0.503 |
| r1-shape-late-borrow | outstanding_before | 2268 | 0.968 | 0.958 |
| r1-shape-late-borrow | outstanding_cpu_us_before | 2268 | 0.973 | 0.965 |
| r1-shape-late-borrow | chosen_load_us | 2268 | 0.971 | 0.977 |
| r1-route-borrow-all | queue_before | 2268 | 0.875 | 0.962 |
| r1-route-borrow-all | pending_before | 2268 | 0.874 | 0.962 |
| r1-route-borrow-all | inflight_before | 2268 | -0.084 | -0.149 |
| r1-route-borrow-all | outstanding_before | 2268 | 0.866 | 0.937 |
| r1-route-borrow-all | outstanding_cpu_us_before | 2268 | 0.868 | 0.944 |
| r1-route-borrow-all | chosen_load_us | 2268 | 0.875 | 0.962 |
| r1-base-cluster-t16 | queue_before | 2268 | 0.995 | 0.993 |
| r1-base-cluster-t16 | pending_before | 2268 | 0.995 | 0.994 |
| r1-base-cluster-t16 | inflight_before | 2268 | -0.255 | -0.327 |
| r1-base-cluster-t16 | outstanding_before | 2268 | 0.995 | 0.989 |
| r1-base-cluster-t16 | outstanding_cpu_us_before | 2268 | 0.995 | 0.991 |
| r1-base-cluster-t16 | chosen_load_us | 2268 | 0.995 | 0.993 |
| r1-shape-deep-home | queue_before | 2268 | 0.855 | 0.818 |
| r1-shape-deep-home | pending_before | 2268 | 0.857 | 0.844 |
| r1-shape-deep-home | inflight_before | 2268 | -0.498 | -0.619 |
| r1-shape-deep-home | outstanding_before | 2268 | 0.058 | -0.156 |
| r1-shape-deep-home | outstanding_cpu_us_before | 2268 | -0.016 | -0.205 |
| r1-shape-deep-home | chosen_load_us | 2268 | 0.855 | 0.818 |
| r1-base-numa-t16 | queue_before | 2268 | 0.946 | 0.982 |
| r1-base-numa-t16 | pending_before | 2268 | 0.948 | 0.982 |
| r1-base-numa-t16 | inflight_before | 2268 | -0.266 | -0.366 |
| r1-base-numa-t16 | outstanding_before | 2268 | 0.946 | 0.975 |
| r1-base-numa-t16 | outstanding_cpu_us_before | 2268 | 0.947 | 0.977 |
| r1-base-numa-t16 | chosen_load_us | 2268 | 0.946 | 0.982 |
| r1-route-sticky-all | queue_before | 2268 | 0.914 | 0.969 |
| r1-route-sticky-all | pending_before | 2268 | 0.915 | 0.968 |
| r1-route-sticky-all | inflight_before | 2268 | -0.066 | -0.196 |
| r1-route-sticky-all | outstanding_before | 2268 | 0.915 | 0.944 |
| r1-route-sticky-all | outstanding_cpu_us_before | 2268 | 0.918 | 0.951 |
| r1-route-sticky-all | chosen_load_us | 2268 | 0.914 | 0.969 |

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

矩阵计划 40 轮；有效完整运行 40 轮；异常/不完整 0 轮。所有退出码与命令见 manifest.json。

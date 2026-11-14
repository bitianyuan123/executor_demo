# DAG 依赖与观测完成路径分析

逐请求沿各 join 最晚完成的前驱回溯。下表为同一批请求的平均值，分量可以相加；各分量 p99 不可相加。

| 运行 | 完成请求 | 已验证依赖边 | 请求 ms | 路径调度/RPC间隙 ms | 路径提交至开始 ms | 路径执行墙钟 ms | 请求尾部 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| results/staged/runs/dag/r0-base-cluster-t16 | 36 | 2880 | 64.274 | 0.151 | 40.571 | 23.552 | 0.000 |
| results/staged/runs/dag/r0-base-cluster-t2 | 36 | 2880 | 61.392 | 0.147 | 54.937 | 6.308 | 0.000 |
| results/staged/runs/dag/r0-base-none-t16 | 36 | 2880 | 38.695 | 0.247 | 21.274 | 17.174 | 0.000 |
| results/staged/runs/dag/r0-base-none-t2 | 36 | 2880 | 61.726 | 0.147 | 55.123 | 6.455 | 0.000 |
| results/staged/runs/dag/r0-base-numa-t16 | 36 | 2880 | 36.105 | 0.153 | 20.205 | 15.747 | 0.000 |
| results/staged/runs/dag/r0-base-numa-t2 | 36 | 2880 | 60.636 | 0.141 | 54.184 | 6.311 | 0.000 |
| results/staged/runs/dag/r0-route-borrow-all | 36 | 2880 | 35.839 | 0.835 | 17.253 | 17.751 | 0.000 |
| results/staged/runs/dag/r0-route-fanout-all | 36 | 2880 | 37.216 | 0.900 | 18.389 | 17.927 | 0.000 |
| results/staged/runs/dag/r0-route-fanout-initial | 36 | 2880 | 45.649 | 0.273 | 24.056 | 21.319 | 0.000 |
| results/staged/runs/dag/r0-route-sticky-all | 36 | 2880 | 37.428 | 1.494 | 18.815 | 17.118 | 0.000 |
| results/staged/runs/dag/r0-shape-deep-borrow | 36 | 2520 | 50.722 | 0.868 | 2.613 | 47.241 | 0.000 |
| results/staged/runs/dag/r0-shape-deep-home | 36 | 2520 | 65.459 | 0.105 | 5.795 | 59.559 | 0.000 |
| results/staged/runs/dag/r0-shape-even-borrow | 36 | 3600 | 35.681 | 0.786 | 16.622 | 18.272 | 0.000 |
| results/staged/runs/dag/r0-shape-even-home | 36 | 3600 | 64.054 | 0.696 | 37.343 | 26.015 | 0.000 |
| results/staged/runs/dag/r0-shape-late-borrow | 36 | 3960 | 36.692 | 1.268 | 17.023 | 18.401 | 0.000 |
| results/staged/runs/dag/r0-shape-late-home | 36 | 3960 | 63.116 | 1.425 | 37.681 | 24.010 | 0.000 |
| results/staged/runs/dag/r0-stress-rpc-home | 36 | 3646 | 110.251 | 60.619 | 30.104 | 19.528 | 0.000 |
| results/staged/runs/dag/r0-stress-rpc-sticky | 36 | 3646 | 99.498 | 63.208 | 21.911 | 14.379 | 0.000 |
| results/staged/runs/dag/r0-stress-tail-home | 36 | 2880 | 75.897 | 0.195 | 31.307 | 44.395 | 0.000 |
| results/staged/runs/dag/r0-stress-tail-sticky | 36 | 2880 | 48.184 | 0.742 | 14.188 | 33.254 | 0.000 |
| results/staged/runs/dag/r1-base-cluster-t16 | 36 | 2880 | 63.385 | 0.136 | 39.874 | 23.375 | 0.000 |
| results/staged/runs/dag/r1-base-cluster-t2 | 36 | 2880 | 60.838 | 0.143 | 54.379 | 6.315 | 0.000 |
| results/staged/runs/dag/r1-base-none-t16 | 36 | 2880 | 37.571 | 0.151 | 20.888 | 16.531 | 0.000 |
| results/staged/runs/dag/r1-base-none-t2 | 36 | 2880 | 61.693 | 0.131 | 55.141 | 6.421 | 0.000 |
| results/staged/runs/dag/r1-base-numa-t16 | 36 | 2880 | 35.496 | 0.141 | 18.809 | 16.546 | 0.000 |
| results/staged/runs/dag/r1-base-numa-t2 | 36 | 2880 | 61.518 | 0.141 | 55.063 | 6.314 | 0.000 |
| results/staged/runs/dag/r1-route-borrow-all | 36 | 2880 | 37.362 | 1.165 | 17.931 | 18.266 | 0.000 |
| results/staged/runs/dag/r1-route-fanout-all | 36 | 2880 | 36.111 | 1.291 | 16.643 | 18.177 | 0.000 |
| results/staged/runs/dag/r1-route-fanout-initial | 36 | 2880 | 45.514 | 0.383 | 24.159 | 20.972 | 0.000 |
| results/staged/runs/dag/r1-route-sticky-all | 36 | 2880 | 36.728 | 1.075 | 18.233 | 17.419 | 0.000 |
| results/staged/runs/dag/r1-shape-deep-borrow | 36 | 2520 | 49.247 | 0.585 | 2.922 | 45.740 | 0.000 |
| results/staged/runs/dag/r1-shape-deep-home | 36 | 2520 | 65.406 | 0.100 | 5.502 | 59.804 | 0.000 |
| results/staged/runs/dag/r1-shape-even-borrow | 36 | 3600 | 36.204 | 0.788 | 15.796 | 19.620 | 0.000 |
| results/staged/runs/dag/r1-shape-even-home | 36 | 3600 | 63.089 | 0.318 | 36.987 | 25.784 | 0.000 |
| results/staged/runs/dag/r1-shape-late-borrow | 36 | 3960 | 35.298 | 1.201 | 16.982 | 17.115 | 0.000 |
| results/staged/runs/dag/r1-shape-late-home | 36 | 3960 | 62.393 | 0.970 | 37.701 | 23.722 | 0.000 |
| results/staged/runs/dag/r1-stress-rpc-home | 36 | 3602 | 110.743 | 59.983 | 31.376 | 19.385 | 0.000 |
| results/staged/runs/dag/r1-stress-rpc-sticky | 36 | 3602 | 101.974 | 62.442 | 21.845 | 17.686 | 0.000 |
| results/staged/runs/dag/r1-stress-tail-home | 36 | 2880 | 71.625 | 0.195 | 33.033 | 38.397 | 0.000 |
| results/staged/runs/dag/r1-stress-tail-sticky | 36 | 2880 | 42.887 | 0.744 | 15.152 | 26.991 | 0.000 |
| results/staged/runs/dag-overlap/r0-base-cluster-t16 | 72 | 5760 | 146.904 | 1.246 | 90.788 | 54.871 | 0.000 |
| results/staged/runs/dag-overlap/r0-route-borrow-all | 72 | 5760 | 70.585 | 0.863 | 33.371 | 36.351 | 0.000 |
| results/staged/runs/dag-overlap/r0-route-fanout-all | 72 | 5760 | 77.197 | 1.143 | 35.817 | 40.236 | 0.000 |
| results/staged/runs/dag-overlap/r0-route-sticky-all | 72 | 5760 | 72.567 | 1.061 | 33.008 | 38.497 | 0.000 |
| results/staged/runs/dag-overlap/r1-base-cluster-t16 | 72 | 5760 | 124.201 | 0.257 | 78.854 | 45.090 | 0.000 |
| results/staged/runs/dag-overlap/r1-route-borrow-all | 72 | 5760 | 70.798 | 1.155 | 33.196 | 36.447 | 0.000 |
| results/staged/runs/dag-overlap/r1-route-fanout-all | 72 | 5760 | 68.935 | 0.895 | 32.360 | 35.679 | 0.000 |
| results/staged/runs/dag-overlap/r1-route-sticky-all | 72 | 5760 | 69.979 | 1.045 | 32.163 | 36.770 | 0.000 |

提交至开始不是内核 runq 时间；执行墙钟减去算子 CPU 时间也不能精确指认为 runq。并发分支的累计等待不能直接相加解释请求时延。

JSON 保留每请求的路径节点、分量、完整性检查与排除的未完成请求数量。存在拒绝或截断时，完成请求 p99 不是全部请求的无偏估计。

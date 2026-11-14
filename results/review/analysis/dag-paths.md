# DAG 依赖与观测完成路径分析

逐请求沿各 join 最晚完成的前驱回溯。下表为同一批请求的平均值，分量可以相加；各分量 p99 不可相加。

| 运行 | 完成请求 | 已验证依赖边 | 请求 ms | 路径调度/RPC间隙 ms | 路径提交至开始 ms | 路径执行墙钟 ms | 请求尾部 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| results/review/runs/dag/r0-all-forks | 84 | 5880 | 214.246 | 1.269 | 8.270 | 204.706 | 0.000 |
| results/review/runs/dag/r0-home | 84 | 5880 | 215.645 | 0.719 | 14.360 | 200.565 | 0.000 |
| results/review/runs/dag/r0-initial | 84 | 5880 | 330.585 | 0.817 | 51.558 | 278.209 | 0.000 |
| results/review/runs/dag/r0-sticky | 84 | 5880 | 222.280 | 1.635 | 9.898 | 210.747 | 0.000 |
| results/review/runs/dag/r1-all-forks | 106 | 7420 | 255.354 | 1.074 | 12.590 | 241.690 | 0.000 |
| results/review/runs/dag/r1-home | 106 | 7420 | 231.642 | 0.837 | 15.865 | 214.940 | 0.000 |
| results/review/runs/dag/r1-initial | 106 | 7420 | 425.065 | 0.977 | 95.334 | 328.754 | 0.000 |
| results/review/runs/dag/r1-sticky | 106 | 7420 | 238.452 | 1.203 | 7.566 | 229.683 | 0.000 |
| results/review/runs/dag/r2-all-forks | 105 | 7350 | 324.886 | 0.979 | 42.288 | 281.618 | 0.000 |
| results/review/runs/dag/r2-home | 105 | 7350 | 334.335 | 0.742 | 53.147 | 280.446 | 0.000 |
| results/review/runs/dag/r2-initial | 105 | 7350 | 484.161 | 0.857 | 141.398 | 341.907 | 0.000 |
| results/review/runs/dag/r2-sticky | 105 | 7350 | 316.595 | 1.051 | 36.965 | 278.580 | 0.000 |
| results/review/runs/dag/r3-all-forks | 92 | 6440 | 230.285 | 1.015 | 9.625 | 219.645 | 0.000 |
| results/review/runs/dag/r3-home | 92 | 6440 | 221.971 | 0.814 | 14.126 | 207.030 | 0.000 |
| results/review/runs/dag/r3-initial | 92 | 6440 | 358.374 | 0.838 | 58.289 | 299.247 | 0.000 |
| results/review/runs/dag/r3-sticky | 92 | 6440 | 206.295 | 1.277 | 6.053 | 198.965 | 0.000 |
| results/review/runs/dag/r4-all-forks | 99 | 6930 | 325.103 | 1.475 | 92.924 | 230.704 | 0.000 |
| results/review/runs/dag/r4-home | 99 | 6930 | 351.383 | 0.807 | 109.421 | 241.155 | 0.000 |
| results/review/runs/dag/r4-initial | 99 | 6930 | 464.124 | 0.690 | 143.828 | 319.606 | 0.000 |
| results/review/runs/dag/r4-sticky | 99 | 6930 | 325.277 | 0.886 | 96.100 | 228.291 | 0.000 |
| results/review/runs/dag/r5-all-forks | 91 | 6370 | 222.746 | 1.325 | 9.977 | 211.444 | 0.000 |
| results/review/runs/dag/r5-home | 91 | 6370 | 208.796 | 0.742 | 13.414 | 194.640 | 0.000 |
| results/review/runs/dag/r5-initial | 91 | 6370 | 372.469 | 0.559 | 63.364 | 308.546 | 0.000 |
| results/review/runs/dag/r5-sticky | 91 | 6370 | 195.721 | 1.148 | 5.633 | 188.940 | 0.000 |

提交至开始不是内核 runq 时间；执行墙钟减去算子 CPU 时间也不能精确指认为 runq。并发分支的累计等待不能直接相加解释请求时延。

JSON 保留每请求的路径节点、分量、完整性检查与排除的未完成请求数量。存在拒绝或截断时，完成请求 p99 不是全部请求的无偏估计。

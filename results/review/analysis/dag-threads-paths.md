# DAG 依赖与观测完成路径分析

逐请求沿各 join 最晚完成的前驱回溯。下表为同一批请求的平均值，分量可以相加；各分量 p99 不可相加。

| 运行 | 完成请求 | 已验证依赖边 | 请求 ms | 路径调度/RPC间隙 ms | 路径提交至开始 ms | 路径执行墙钟 ms | 请求尾部 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| results/review/runs/dag-threads/r0-home-t16 | 93 | 6510 | 212.624 | 0.565 | 20.636 | 191.423 | 0.000 |
| results/review/runs/dag-threads/r0-home-t2 | 93 | 6510 | 204.783 | 0.129 | 155.677 | 48.976 | 0.000 |
| results/review/runs/dag-threads/r1-home-t16 | 95 | 6650 | 321.622 | 0.818 | 65.683 | 255.121 | 0.000 |
| results/review/runs/dag-threads/r1-home-t2 | 95 | 6650 | 316.832 | 0.116 | 267.886 | 48.830 | 0.000 |
| results/review/runs/dag-threads/r2-home-t16 | 80 | 5600 | 191.710 | 0.784 | 13.766 | 177.160 | 0.000 |
| results/review/runs/dag-threads/r2-home-t2 | 80 | 5600 | 184.950 | 0.147 | 136.222 | 48.580 | 0.000 |
| results/review/runs/dag-threads/r3-home-t16 | 103 | 7210 | 262.469 | 0.769 | 25.159 | 236.541 | 0.000 |
| results/review/runs/dag-threads/r3-home-t2 | 103 | 7210 | 252.062 | 0.164 | 203.080 | 48.817 | 0.000 |
| results/review/runs/dag-threads/r4-home-t16 | 87 | 6090 | 203.844 | 0.666 | 13.969 | 189.209 | 0.000 |
| results/review/runs/dag-threads/r4-home-t2 | 87 | 6090 | 197.403 | 0.145 | 148.218 | 49.040 | 0.000 |
| results/review/runs/dag-threads/r5-home-t16 | 106 | 7420 | 296.944 | 0.754 | 79.968 | 216.223 | 0.000 |
| results/review/runs/dag-threads/r5-home-t2 | 106 | 7420 | 286.840 | 0.198 | 237.613 | 49.029 | 0.000 |

提交至开始不是内核 runq 时间；执行墙钟减去算子 CPU 时间也不能精确指认为 runq。并发分支的累计等待不能直接相加解释请求时延。

JSON 保留每请求的路径节点、分量、完整性检查与排除的未完成请求数量。存在拒绝或截断时，完成请求 p99 不是全部请求的无偏估计。

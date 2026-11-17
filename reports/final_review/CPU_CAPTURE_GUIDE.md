# 已完成的4021补采：逐 cluster CPU 时间序列与复现指南

**4021已回传本指南的全部六轮结果，目标机执行、工作一致性和CPU窗口校验均通过。无需为同一目的再次运行六轮。** 结果与结论见[4021报告](../../docs/TARGET_V2_4021_REVIEW.md)。下面保留完整合同、命令和失效处理作为复现参考，不是新的待执行任务。此前仅有的CPU/完成请求和callback占用数不能替代CPU时间窗；此次确实新增了该负载的100ms逐cluster观测。

本次只补一个量化合同、三种策略各两轮，共六轮。其目的是把 CPU 窗口变化、request 时延、各角色改投放在同一轮解释；不重开所有负载矩阵，不决定生产最优阈值。

## 固定六轮合同

| 维度 | 固定值 |
|---|---|
| 获准 CPU | `0-47,192-239`，96 个逻辑 CPU |
| cluster 子池 | `0-15;16-31;32-47;192-207;208-223;224-239`；每 cluster 一池；每池 16 worker |
| 请求到达 | regular 12 QPS，相邻计划到达间隔 83.333 ms；240 请求，约 20 秒投递 |
| 每请求 DAG | init → 第一波 80 条分支、每分支两级 → join → 第二波 4 条分支、每分支两级 → join；168 个业务算子、171 个基础 callback |
| 计算与数据 | 每算子基准 3,000,000 次迭代及 100,000 次访问；task 级 ±25% 扰动、约 1/8 四倍工作；branch-rw 64 KiB/分支 |
| 重复工作 | seed 421；每轮核对到达、逐 request checksum、实际算子工作、CPU 资源与第一轮 home 一致 |
| 路由 | request RR home；parent、all、chunk=1、high=1、gain=0、hop=4；仅同 NUMA；return-low=.5 在 parent 模式不负责主动回 home |
| 其他控制 | max-inflight=16、memory cap=512 MiB；无异步等待、无同步阻塞；CPU 后台评分和页采样关闭 |
| 观测 | 六轮统一 `trace=1`，用于同轮角色等待及改投；另加独立 CPU 采集器，100 ms 采样 |
| 正式范围 | 计划到达 `[2,18)` 秒的 192 个请求，晚完成仍计；CPU 只纳入读取区间完全位于同一时间范围内的窗口 |
| 上限 | 每轮被测命令 120 秒；最多六轮；任何一轮执行、采集或分析失败就停止，不自动重试或补格 |

固定顺序：`home → queue → outstanding → outstanding → queue → home`。前后三格各有一个 home，分别形成两个可核对工作一致性的比较块。反序降低单方向时间漂移的风险，但两次重复仍不足以保证稳定 p99；不要平均两轮 p99。

六轮都保留同一详细记录档位。它回答“在这个观测合同下发生什么”，不替代此前 trace=0 的净性能结果，也没有测量采集器开/关的纯成本。

## 复现操作：先查看计划，再显式启动

仅在另有复现需要时使用本节；已返回4021结果的目标环境无需重复执行。确认 96 个 CPU 仍可用于独立实验，并已构建当前 `build-affinity/affinity_demo`。新增工具为标准库 Python，不需要因本次采集重新编译 C++ 或 Folly。

```bash
cd /home/bty/rank_demo/executor_demo
scripts/run_cluster_cpu_comparison.sh \
  --output results/local/cluster-cpu-regular12-3m-t16-seed421-01
```

默认只打印六条完整命令，**不运行负载、不创建结果目录**。查看无误后，以同一条命令加 `--run`：

```bash
cd /home/bty/rank_demo/executor_demo
scripts/run_cluster_cpu_comparison.sh --run \
  --output results/local/cluster-cpu-regular12-3m-t16-seed421-01
```

输出目录必须不存在，已有目录直接拒绝。脚本启动前验证实际 CPU 允许集合恰为批准的 96 CPU，避免 `taskset` 被 cpuset 限制后只保留部分 CPU 却继续比较。采集器和子命令都明确限定到这组 CPU，不会自动选用其他业务 CPU。每轮串行执行，只有前一轮的采集和分析成功，才会进入下一轮。

脚本固定为六轮，不支持临时把 QPS、图、seed 或线程数混入这次对照；要改合同应另立实验。`--binary` 可指定其他已核实构建路径，`--python` 可指定 Python 可执行文件；脚本会记录仓库提交和二进制 SHA256。`--help` 不执行任何负载。

每轮结束会打印：CPU 正式窗口统计及时间序列、request 时延/整轮 CPU、`flow_report.py` 的角色等待/改投、以及各角色 `source!=chosen / n` 的百分比。这里改投是总池将新任务分配到另一个池，**不是内核线程迁移**。最后分别打印前后三格的同 home 比较，不把两个 home 混成唯一基线。

## 如何从终端回传

运行过程中已打印每轮全部摘要，目标机也保留文本。不能传文件时，可以分轮打印并复制，例如第一轮：

```bash
cd /home/bty/rank_demo/executor_demo
CPU_RESULT='results/local/cluster-cpu-regular12-3m-t16-seed421-01'
cat "$CPU_RESULT/batch.txt"
cat "$CPU_RESULT/01-home/cpu-terminal.txt"
cat "$CPU_RESULT/01-home/request-report.txt"
cat "$CPU_RESULT/01-home/flow-report.txt"
cat "$CPU_RESULT/01-home/role-rates.txt"
```

其余目录依次是 `02-queue`、`03-outstanding`、`04-outstanding`、`05-queue`、`06-home`。最后回传 `block-1-comparison.txt` 与 `block-2-comparison.txt`。如果失败，保留该轮 `cpu-terminal.txt`、`capture/capture.json` 和 `capture/run.log`；先分析失败，不跳过该格继续执行。

每轮 CPU 标准输出包含 `CPU_CAPTURE`、`CPU_SCOPE`、`CPU_SERIES_BEGIN…END`，时间序列每 cluster 一行。每组约 160 个 100 ms 窗口、六 cluster，正文较长，建议逐轮复制，避免终端历史截断。完整 CSV 留在测试机，缺失文本不能补成零。

## CPU 计量含义和时间边界

`experiments/affinity/cluster_cpu_capture.py` 用 argv 直接启动命令，**不调用 shell**。每次读取一份 `/proc/stat` 得到所有选定 CPU，不扫描上百个 worker；另读取主 PID 的 `/proc/PID/stat`，记录整个进程线程组的 `utime+stime`。工具不修改内核配置。

```text
Δbusy = Δ(user + nice + system + irq + softirq)
cluster_busy_cpu_equivalent = Δbusy / SC_CLK_TCK / 实际窗口秒数
cluster_util_percent = 100 × cluster_busy_cpu_equivalent / 该分区CPU数
```

user/nice 已包含 guest，不能再加 guest。idle、iowait、steal 分开输出。这里是逻辑 CPU 时间，不是物理核吞吐率或 SMT 有效容量。

每次 CPU 读取记录 MONOTONIC 起止和中点；增量除以相邻读取中点之间的实际时间，不假设调度器恰好每 100 ms 唤醒采集器。`/proc/stat` 本身不是所有 CPU 的事务快照，读取跨度会作为计量不确定性保留。正式统计只使用前后两次读取括号都位于 `summary.origin_ns+[2,18)` 内的窗口，不裁剪边缘、不插值。进程 CPU 使用它自己的前后读取中点和边界。

默认 **100 ms 窗口平均**；其 p99/max 是“窗口平均 CPU 利用率”的尾部，绝不是 request p99，也不能证明 1 ms 峰值或 30 ms 峰值持续时间。单独工具允许降低至 20 ms，但会提示 tick 量化及开销；本次六轮全部固定 100 ms，不能中途改采样间隔。出现略高于 100% 的原始值时保留并标记，不裁剪以掩盖量化、读取偏差或计账异常。

**cluster CPU 包含这些 CPU 上的全部活动**：executor、driver/helper、采集器和其他干扰。进程 CPU 是被启动进程的全部线程，不包括另行 fork 的后代；不能按其总数分配出每个 executor 池的 CPU。采集器 CPU 成本另列，scope 是启动、采样、循环和序列化前分析，不含最后写 CSV/JSON 和打印终端。采集器在选定 CPU 上运行时，其 CPU 也已计入 cluster 值；不能再重复相加。

每个 CSV 时间窗的进程 CPU 值在六个 cluster 行重复，**不得跨 cluster 累加**。被测命令应直接启动真正被测进程；示例 `taskset` 最终 exec 后 PID 不变，不能换成一个常驻 shell 或 runner 再把它的 CPU 当成 executor CPU。

## 产物、失效与停止

每轮 `capture` 子目录中：

- `run.log`：被测命令 stdout/stderr。
- `cpu_windows.csv`：所有采样窗，包含启动、预热、正式段和排空；每 cluster 记录实际窗口、CPU 分类增量、读取跨度、采样迟到、跳过的计划采样点以及 invalid 原因。
- `capture.json`：实际 argv、资源、SC_CLK_TCK、计量定义、子进程退出状态、采集器 CPU、错误和正式窗口统计。

CPU 读取失败、任一选定 CPU 缺失或计数回退，会保留 invalid，不用邻近值或零填补；窗口没有全部有效则工具非零退出，六轮脚本停止。PID 起始时间变化或计数回退使进程差分失效。进程退出后最后一个 PID 端点缺失是正常可能，标为 unavailable，不伪造尾段 CPU=0；已采到的 cluster 窗口仍保留。

工具按 `summary.origin_ns` 对齐正式时间，缺失/非法 origin 或正式范围内没有完整有效 CPU 窗口会非零退出。打印有效秒数、覆盖率、无效原因、时间加权均值，以及完整有效窗口的 nearest-rank p50/p95/p99/max。正式时间范围与 request cohort 对齐，不代表“这些 CPU 时间只归属于 cohort 的 192 个请求”：窗口边界上可有更早请求仍在执行、或 cohort 请求晚于结束才完成。

被测命令非零退出保留其状态；超时返回 124，SIGINT 返回 130。中断/超时终止本工具创建的进程组，并保存 partial。六轮脚本转发中断给当前采集器，等待其清理，不继续下一格。若物理断电、SIGKILL 或磁盘不可写，不能保证最后的内存样本完整落盘。

## 本机验证范围

```bash
python3 experiments/affinity/tests/cluster_cpu_capture_test.py
bash -n scripts/run_cluster_cpu_comparison.sh
scripts/run_cluster_cpu_comparison.sh --help
scripts/run_cluster_cpu_comparison.sh --output /tmp/cluster-cpu-plan-only-check
```

19 项短时功能测试覆盖解析、guest 不重复、分类计账、真实时间差、回退/缺失、PID 复用、读取边界、正式覆盖率、加权均值、真实短 CPU 子进程、失败退出、超时进程组清理、SIGINT、拒绝覆盖和缺失 summary。计划检查核实恰好六轮、固定参数与顺序，且不会创建结果目录。开发机只运行上述功能检查，没有用开发机生成目标性能数据；4021的CPU曲线来自用户返回的目标机六轮实测。

## 当前完成状态

4021中六轮各完成240/240请求，正式cohort为192/192；资源、到达、checksum和算子工作相同。每cluster 159个完整有效CPU窗口，约99.37%正式范围覆盖，采集退出码均为0。现已具备同一轮的CPU窗口、请求时延、首波/后继改投和等待数据；不需要为填补这一项再次采集。

该结果只覆盖本页量化合同。其他线程数、请求成组、异步或跨NUMA等负载不能借用这条曲线；也不因此自动要求逐项补采。只有新的明确决策需要这些数据时才单独设计有限对照。本次不修改负载、调度算法或参数初值，既有复现命令保留。

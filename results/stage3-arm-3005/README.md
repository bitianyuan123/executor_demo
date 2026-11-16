# ARM 3005 原始终端证据归档

本目录保存用户提供的 `3005.log` 原始字节，以及从终端打印内容提取的结构化表格和图。没有重新运行负载，也没有收到测试机上的 `tasks.csv`、`cpu_trace.txt`、正式采集状态、plan 或 manifest。

完整负载定义、N/Q/C与S/L含义、三种策略流程和结果解读见[阶段3最终分析报告](../../stages/03-independent/ARM_3005_CPU_CURVE_REPORT.md)。

原文件 SHA256：`eb394e50586933ad252e5021837368c56521206cd1c74ffea70c30f28d5deccc`。`source.sha256` 可独立校验；原日志的 CRLF 换行未更改。

## 数据及口径

同一套微突发负载：6 池、每池 128 worker、每池 16 个逻辑 CPU、cluster 亲和；12 request/s，每个 request 初始 80 个独立任务在 20ms 内投递，后续 80 个任务在 400ms 内投递；任务平均 CPU 预算 25ms，jitter=0.5。三个策略各运行一次，正式 request cohort 为 `[10,60)` 秒，每策略 600 个 request、96,000 个 task。

- `home`：任务保留在请求亲和池。
- `guarded`：原池 N/C > 0.8 时，选择严格更轻且 N/C < 0.5 的最轻目标；无合格目标则留原池。
- `source_only`：原池 N/C > 0.8 时，选择严格更轻的最轻目标，不设置目标绝对上限；无更轻目标则留原池。两种分流都只选择尚未提交的新任务目的池，不搬运已排队或执行中的任务。

N是每池预留或接纳后尚未完成的任务数，C=16为逻辑CPU容量刻度，N/C不是CPU百分比。S为任务进入提交路径到计算结束，submit早于负载读取、选池和Folly add；L从该项自己的计划投递时刻计时，包含发压迟到。它们都不是整个request的完成时延。以下仅复述本次打印结果：

| 策略 | S 平均 ms | S p99 ms | 100ms /proc/stat 并集 CPU % | 5ms 调度口径并集 CPU 平均 % | 每池最长 ≥90% 时长在六池间的范围 |
|---|---:|---:|---:|---:|---:|
| home | 64.052 | 155.828 | 50.474 | 50.481 | 175–185ms |
| guarded | 39.920 | 98.604 | 50.322 | 50.362 | 105–110ms |
| source_only | 25.101 | 37.381 | 50.380 | 50.219 | 20ms（六池相同） |

CPU trace 的口径是非 idle 任务驻留率，不能当作包含精确 IRQ 核算的 CPU 利用率。上述最长时长是每个池在整段观测内的最长连续高占用，取六池最小/最大值；不是 request 时长范围，也不是 p99。

## 文件

| 文件 | 内容 |
|---|---|
| `3005.log`、`source.sha256` | 原始终端字节及哈希 |
| `evidence.json` | 来源、上下文、提取表、检查结果、适用限制 |
| `preflight_status.json` | 日志开始的采集能力检查 JSON；不替代正式三轮采集状态 |
| `policy_summary.csv`、`comparisons.csv` | task 时延与策略比较 |
| `decomposition.csv`、`initial_spans.csv`、`buckets.csv` | 分阶段时延、初始投递跨度、home 的条件负载分桶 |
| `cpu_procstat.csv` | 原 100ms CPU 统计，21 行 |
| `cpu_trace_scopes.csv` | 5ms 重建统计，每策略并集和六池，共 21 行 |
| `cpu_trace_aligned_windows.csv` | 已打印的五个对齐窗口，每策略各五行，共 15 行 |
| `cpu_trace_observer.csv` | 每策略采集量、事件数、观察进程和 reader CPU 秒 |
| `figures/cpu_phase_summary.{png,svg}` | 五个离散窗口分组柱图与 task S p99 |
| `figures/cpu_saturation_duration.{png,svg}` | 六池各自的最长高占用及高占用时间比例，点和范围不表示重复实验 |

图只使用收到的汇总数值；**没有把五个窗口的均值画成连续 5ms 曲线**。window 对齐原点为 request planned time，观测的是该请求 home cluster 的全部运行任务，因此会包含其他 request 和迁入任务，不能分离单请求专属 CPU 时间，也不能把后几个窗口直接命名为稳态。

## 已执行的完整性检查

`extract_terminal.py` 使用十进制运算验证以下内容：

1. 原字节 SHA256 一致；两份时延报告正文逐行相同，三份 CPU TRACE 报告正文逐行相同。第一份时延报告另外打印了生成器的 Artifacts 路径；不作为数据差异。
2. 三策略各 96,000 task / 600 request；initial、steady 各 48,000 task；时延总表与分解表一致。
3. S=W+X、L=D+S 的均值分解，阶段均值与总均值，36 行条件分桶的计数/加权均值，在三位小数舍入误差范围内一致。
4. 三组策略差值百分比与原表的三位小数舍入区间兼容；没有平均、相加或重构 p99。
5. 远端输出的三策略 CPU 状态均为 ok / issues=none；96/96 CPU、每个 scope 9999/9999 个完整窗口、五个对齐窗口各 600/600 request；数值范围与 5ms 连续时长单位一致。

重复打印不等于重复实验。上述检查验证的是**打印证据自洽**；正式 trace 的无丢失、原任务 p99 和完整 5ms 曲线仍依赖远端分析器，不能在没有原始任务/事件文件的情况下重新独立验证。Observer 的进程 CPU 时间包含 reader，二者不能相加；这些数值也不包含内核跟踪开销。

## 重现提取与图表

在任意目录执行（提取只需 Python 标准库；绘图需 matplotlib）：

```bash
python3 /path/to/executor_demo/results/stage3-arm-3005/extract_terminal.py
python3 /path/to/executor_demo/results/stage3-arm-3005/plot_report.py
```

本次已执行脚本并检查两幅 PNG 排版。没有补跑测试，不计算重复实验置信区间，也不据此宣称 0.8 是最优阈值。

# 两次短时 perf 调度诊断

在主矩阵和负载扫描之后，另做**恰好两次**带跟踪的诊断：每池 2 个线程和每池 16 个线程，其余参数相同。两池分别允许 CPU `0-1`、`2-3`，NUMA label 都为 0；使用 home 策略，10 QPS、3 秒、fanout 20、链长 2、名义 CPU 3000 µs、seed 420、抖动 0.5、长任务概率 0.02/倍率 8。每轮均完成 30 请求、1230 段，没有拒绝、删失或失败；目标 CPU 预算之和均为 4.365779 秒。

它们位于 `results/runs/diagnostic-t2`、`diagnostic-t16`，与此前未带 perf 的结果分开保存。原始 perf 二进制只保留在 `/tmp/executor-diagnostic-t2.data`、`/tmp/executor-diagnostic-t16.data`，**没有提交到 Git**。仓库只保留本 demo 的 CSV、按 workers.csv TID 过滤的 timehist、计数统计与聚合结果，不保存其他进程的原始调度轨迹。

## 1. 直接观察到的内核调度延迟

本机 `kernel.sched_schedstats=0`，这使此前 `/proc/.../schedstat` 等待字段不能用于证明 runqueue 延迟，但独立的 scheduler tracepoints 可读。本次使用 perf 6.12.107，采集内核为 6.12.95+deb13-amd64。以下是**带跟踪的两轮**结果：

| 指标 | 每池 2 线程 | 每池 16 线程 |
|---|---:|---:|
| 完成请求数 | 30 | 30 |
| 请求平均时延 ms | 77.072 | 77.308 |
| 请求 p99 ms（每轮仅 30 样本） | 115.492 | 104.919 |
| 记录到的 worker 调度运行区间数 | 265 | 2359 |
| worker `sch delay` 总和 ms | 28.077 | 17091.480 |
| 总和 / 完成请求数，ms/request | 0.936 | 569.716 |
| 单次最大 `sch delay` ms | 3.581 | 23.101 |
| worker `run time` 总和 ms | 4401.981 | 4382.159 |
| 任务 submit-to-start 均值 ms | 23.204 | 10.725 |
| 任务开始至完成墙钟均值 ms | 3.593 | 16.203 |

**569.716 ms/request 是所有 worker 可运行等待的累加记账，不是一个请求关键路径增加了 569.716 ms。** 各分支、各线程的等待可以重叠，也包含工作线程启动/退出的区间。它完全可以大于约 77 ms 的请求时延。

这次跟踪直接显示：在相同 CPU mask 下增加消费者线程，worker 的可运行调度等待明显增多，同时提交至回调开始的等待降低。它为“等待从用户队列转移到已开始/可运行线程”提供了内核事件证据。两次小样本诊断不能给生产最优线程数、稳定 p99 收益或精确回归幅度；也不能把它们与未跟踪矩阵当作同一采样条件直接合并。

## 2. 三个时间字段不能混用

`perf sched timehist` 中：

- `wait time` 是上次切出到下次切入的离 CPU 时间，包含睡眠/阻塞和被抢占等待；本次总和分别为 7417.068 ms、88925.820 ms，**不是 runqueue 时间**。
- `sch delay` 从可运行时间到真正切入。perf 对唤醒记录 ready 时间，也在以 R 状态切出时把切出时刻作为下一次 ready 时间，因此覆盖已可运行线程被抢占后的等待。
- `run time` 是对应 CPU 运行区间。这里按 worker 累加，包含线程池及部分初始化/退出开销，与只包合成算子 kernel 的任务 `cpu_us` 不同。

这些语义可对照 [perf sched 官方文档](https://github.com/torvalds/linux/blob/v6.12/tools/perf/Documentation/perf-sched.txt) 和 [perf 对 R 状态切出的处理](https://github.com/torvalds/linux/blob/v6.12/tools/perf/builtin-sched.c#L2842-L2853)。本次事件源包含 `sched_waking`，其时间在唤醒处理路径中，因此该指标也包含就绪入队周边的小段处理时间，不等同于一个理想的“已完成入队后纯排队”时钟。

## 3. 跟踪质量与扰动

`perf sched record` 默认选中的事件包含 `sched_stat_runtime`。本实验的 CPU 预算循环频繁读取线程 CPU 时钟，实际记录中该事件数量极高：

| 记录质量 | t2 | t16 |
|---|---:|---:|
| 全部 sample 数 | 3827608 | 3868341 |
| 其中 sched_stat_runtime 数 | 3814948 | 3851134 |
| perf 输出文件大小（其报告口径） | 380.243 MB | 384.331 MB |
| 报告的乱序事件数 | 4 | 1 |
| `perf report --stats` 中 LOST/LOST_SAMPLES 记录 | 未出现 | 未出现 |

录制和 `perf script --debug-mode` 都报告了上述乱序事件；未逐条定位它们是否涉及目标 worker。检查没有报告丢事件，不能据此宣称轨迹完美、时序毫无误差或没有其他观测盲区。`Samples of sched_switch event do not have callchains` 表示未录调用栈，不意味着没有调度时间事件。

尤其不能用“跟踪前后请求均值看起来接近”推断跟踪扰动很低。默认 CPU 模式固定的是线程 CPU 预算；跟踪和内核开销也占用这份预算，可能减少实际完成的有用循环迭代。约数百万 runtime 事件说明本次跟踪绝不是经过校准的低开销采样。本次不追加第三轮负载、不改事件集合重跑，不改内核开关。后续现场诊断可独立评估只记录所需 switch/wakeup 事件的方案。

运行环境另有 `/etc/ld.so.preload` 配置的 `libonion.so`；本次未修改、禁用或分析它，影响未知，不能把 CPU 过冲或跟踪现象归因给它。

## 4. 保存文件与汇总方式

- [t2 汇总及每 worker 统计](../results/logs/perf-t2.txt)、[t16 汇总及每 worker 统计](../results/logs/perf-t16.txt)。开头保留原样 `--summary --tid` 输出，末尾附从完整 timehist 行得到的聚合。
- [t2 过滤 timehist](../results/logs/perf-timehist-t2.txt)、[t16 过滤 timehist](../results/logs/perf-timehist-t16.txt)，仅含 workers.csv 中的 TID。
- [t2 机器可读聚合](../results/logs/perf-t2-summary.json)、[t16 机器可读聚合](../results/logs/perf-t16-summary.json)。
- `results/logs/perf-record-t*.txt` 保存录制输出，`perf-check-t*.txt` 保存时间顺序检查，`perf-events-t*.txt` 保存事件数量，不含其他进程的轨迹正文。

这版 perf 的 `timehist --summary --tid ...` 在本次线程均已退出后打印 `<no still running tasks>`。这不是“没有调度延迟”。完整 `timehist --tid ... --state` 仍输出了这些 worker 的运行区间，因此使用 [解析脚本](../scripts/analyze_perf_timehist.py) 按 TID 累加这些行。脚本拒绝混入非目标 TID，也拒绝将缺少记录的 worker 当成零。timehist 文本每项显示到 0.001 ms，累加具有相应舍入误差。

已有过滤文本足以复核本页数字，不依赖仍保留 `/tmp` 二进制：

```bash
python3 scripts/analyze_perf_timehist.py \
  results/runs/diagnostic-t2 results/logs/perf-timehist-t2.txt \
  results/logs/perf-t2-summary.json
python3 scripts/analyze_perf_timehist.py \
  results/runs/diagnostic-t16 results/logs/perf-timehist-t16.txt \
  results/logs/perf-t16-summary.json
```

## 5. 精确录制与导出命令

下面是这两次运行的命令模板；循环只取 2 和 16。若再次执行，须改为新的 demo 输出目录，程序不会覆盖既有结果。这是复现说明，本次任务未额外运行第三次负载。

```bash
for n in 2 16; do
  perf sched record -o "/tmp/executor-diagnostic-t${n}.data" -- \
    ./build/executor_demo --output "results/runs/diagnostic-t${n}" \
    --pools 2 --threads "$n" --cpus '0-1;2-3' --numa '0,0' \
    --affinity cluster --policy home --metric work --qps 10 --duration 3 \
    --fanout-min 20 --fanout-max 20 --chain-length 2 --cpu-us 3000 \
    --seed 420 --jitter 0.5 --long-prob 0.02 --long-factor 8 --join-us 100 \
    --max-inflight 64 --drain-seconds 30 --sample-ms 100 \
    > "results/logs/perf-record-t${n}.txt" 2>&1
done
```

导出与检查只读已有 trace：

```bash
for n in 2 16; do
  PERF_WORKER_TIDS=$(python3 -c \
    'import csv,sys; print(",".join(r["tid"] for r in csv.DictReader(open(sys.argv[1]))))' \
    "results/runs/diagnostic-t${n}/workers.csv")
  perf sched timehist -i "/tmp/executor-diagnostic-t${n}.data" \
    --summary --tid "$PERF_WORKER_TIDS"
  perf sched timehist -i "/tmp/executor-diagnostic-t${n}.data" \
    --tid "$PERF_WORKER_TIDS" --state \
    > "results/logs/perf-timehist-t${n}.txt" \
    2> "results/logs/perf-timehist-t${n}-stderr.txt"
  perf script --debug-mode -i "/tmp/executor-diagnostic-t${n}.data" \
    > /dev/null 2> "results/logs/perf-check-t${n}.txt"
  perf report --stdio --stats -i "/tmp/executor-diagnostic-t${n}.data" \
    > "results/logs/perf-events-t${n}.txt"
done
```

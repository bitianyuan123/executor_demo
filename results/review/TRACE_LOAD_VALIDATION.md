# 调度轨迹与四池负载复核：实际验证记录

日期：2026-11-15。开发机x86_64、Linux `6.12.95+deb13-amd64`、4核/4GB，原版兼容Folly安装。目标ARM业务trace未取得，不将以下功能数据视为生产性能证据。本轮只修改demo仓库，不修改Folly核心。

## 构建、测试和运行链路

- `cmake --build build-affinity -j1`成功；affinity独立CTest **7/7**通过，包括19项门面、7项局部性、10项评分、5项Linux采样、2项stock Folly适配测试，以及22个完整短运行的功能组和成本探针。
- `cmake --build build -j1`成功；默认工程CTest **17/17**通过，保留阶段2/3/4旧路径并增加调度分析与辅助工具测试。安装版Folly的CMake Boost策略警告没有阻止构建。
- Python分析器使用合成时间线测试，含丢失/乱序/边界/部分CPU/迁移、独立unique集合、内存上限及相关性；辅助工具11项测试包含proc映射、坏stat、SQL转义/裁剪/生命周期。最终测试项数以测试终端为准。
- 额外执行一次真实的matrix→demo→report流水线：六策略、每例32请求、4软件池×2线程、共享开发机两个CPU、burst4/100us、同seed420、request级异质工作。六轮均32完成/0拒绝、480闭包、29,652,000计划迭代，arrival hash及最终checksum相同。报告能识别固定工作量、并发占用与合法home配对。
- 本机`sched_schedstats=0`。上述CPU策略携带评分的448个任务全部回退：98个missing_cpu、350个missing_runqueue，没有把退化成outstanding的运行称为CPU信号的收益。真CPU/runqueue有效值路径由fake proc差分测试及纯评分/门面测试覆盖；尚未在目标内核启用计账后压测。

重放流水线（使用新的输出目录）：

```bash
python3 experiments/affinity/run_matrix.py --suite signals --seeds 420 \
  --output results/local/trace-load-signals-01 --run --base-args \
  --pool-count 4 --threads 2 --affinity none --requests 32 --qps 100 \
  --arrival burst --burst-size 4 --burst-spacing-us 100 \
  --widths 8,2 --depths 1,2 --model none --accesses 0 --iterations 50000 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope request --max-inflight 32 --memory-mb 32
python3 experiments/affinity/report.py results/local/trace-load-signals-01/seed-*
```

这组运行仅用于验收接口和合同。32请求的p99接近最大值，单seed和低压力不能排名策略；默认none的0.5容量/池是两个共享CPU的名义份额，不是独占资源。

## 本地空闲线程读成本探针

命令：`build-affinity/observation_cost_bench --threads N --iterations 2000 --scans 10`，N分别4和128，串行执行。下表保留批量平均ns/操作，没有减去循环基线。没有进行目标ARM性能测量。

| 操作 | 4线程 | 128线程 |
|---|---:|---:|
| loop baseline | 3.237 ns | 3.237 ns |
| native queue | 6.330 ns | 6.453 ns |
| Folly stats light扫描 | 8.729 ns | 60.110 ns |
| stock active provider读取 | 6.160 ns | 6.160 ns |
| active读取+评分 | 11.513 ns | 11.290 ns |
| schedstat缓存读取 | 3.236 ns | 3.325 ns |
| 每轮proc扫描全部注册线程 | 33,556.5 ns | 982,310.9 ns |

128线程共11轮（含建基线），累计扫描CPU 11.185ms、墙钟11.310ms、最大单轮墙钟1.492ms。4线程分别0.442ms CPU、0.442ms墙钟。它们说明后台采集和热路径读是两种成本，不能把缓存读的几ns说成全部观测开销。热缓存、空闲worker与编译器优化都影响数值；此探针没有证明高并发observer RMW、跨NUMA路由和整个总池的低扰动。批平均p99也不是单次API p99。

## Perfetto真实CLI与自写分析交叉验证

本机从官方launcher取得`Perfetto v59.1-1e9c61e98`，实际运行`query --format csv -f SQL TRACE`。没有运行内核trace采集，也没有本地验证trace-cmd录制；其命令按官方手册核对。

仓库保留两个**人工构造**文本fixture：[仅wakeup](trace-load/wakeup-only.trace)、[waking与wakeup分开](trace-load/waking-and-wakeup.trace)，映射为[tid=10→pool0](trace-load/tids.csv)。它们不是用户真实trace。

第二个fixture的时间线：waking=100.000000s，wakeup=100.000100s，switch-in=100.002000s，switch-out=100.005000s。相同绝对窗口`[100000000000,100005000000)`：

- 自写分析：配对onCPU=3ms，成功wake→run=1.9ms。
- Perfetto：sched_slice=3ms；thread_state的R=2ms，从waking阶段开始重建。
- Perfetto的`ftrace_event`只有两条switch generic rows，但R区间已使用waking信息。因此**该表不列某事件，不等于该事件未被处理**。SQL用`stored_generic_ftrace_events`名称，不能将它当全部原始事件库存。
- 仅wakeup且带空格名称的第一个fixture，在此版本只重建出3ms Running，没有R。它只能证明此输入/版本没有得到相应等待状态，不能泛化成所有文本wake均不受支持。没有waking和名称差异是两个变化因素，也不能据此单独归因于空格。

重放第二个fixture：

```bash
python3 scripts/analyze_sched_trace.py \
  results/review/trace-load/waking-and-wakeup.trace \
  --tid-map results/review/trace-load/tids.csv \
  --coverage full --duration-seconds 0.005
python3 scripts/trace_processor_report_sql.py \
  --tid-map results/review/trace-load/tids.csv \
  --start-ns 100000000000 --end-ns 100005000000 \
  --output /tmp/executor-synthetic-report.sql
# 本地已安装launcher的路径；目标机先准备对应工具，勿假定文件存在。
python3 /tmp/executor-trace-processor-launcher query --format csv \
  -f /tmp/executor-synthetic-report.sql \
  results/review/trace-load/waking-and-wakeup.trace
```

自写事件计数采用半开窗口，末端switch-out不计为窗口内事件但可闭合前面3ms运行；SQL的generic表计数目前是全导入范围，不能直接拿这两个数字互判丢事件。小fixture无足够窗口时相关性明确输出undefined，不伪造r=0。

## 人工复核项与下一步

1. 真实每池active 20/80的定义、窗口、数据来源以及这次trace对应的CPU掩码/时钟/丢失情况。
2. 实际Folly版本、队列和线程min/max；目标worker是否稳定、TID映射是否属于捕获时刻。
3. 目标机同CPU资源、同冻结工作量的负载/时延对照；分开四池none和六池cluster实验。
4. ARM上128/512线程后台采样成本、有效样本覆盖、trace与observer维护的扰动；结合微突发决定是否需要更细粒度采样后端。
5. 真实RPC和多父DAG数据归属仍由v1/业务补充，不能据v2波次图宣称全部场景复现；生产Future/KeepAlive接入也仍需单独验收。

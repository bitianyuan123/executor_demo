# 实验、部署与对比分析

## 环境与单位

先执行 `python3 scripts/topology.py --output results/topology.json`，保存 allowed cpuset、NUMA、core、SMT sibling、cluster、缓存共享列表、cgroup 祖先配额和内核版本。CPU 编号不保证相邻同 cluster；cluster 不保证等于 LLC；48 物理核 / 96 逻辑 CPU 不能按 96 个独立满速核计算容量。

`--cpus` 用分号分隔线程组，组内使用 CPU 编号或范围。`--numa` 与组数一致。`--affinity cluster` 按每组输入掩码绑定；`numa` 合并同一 NUMA 标签的掩码；`none` 保留当前 allowed mask。以下占位值必须根据现场拓扑填写：

```bash
export EXECUTOR_CPUS='实际cluster0的cpulist;实际cluster1的cpulist;实际cluster2的cpulist;实际cluster3的cpulist;实际cluster4的cpulist;实际cluster5的cpulist'
./build/executor_demo --output results/runs/arm-home \
  --pools 6 --threads 16 --cpus "$EXECUTOR_CPUS" --numa '0,0,0,1,1,1' \
  --affinity cluster --policy home --qps 12 --duration 120 \
  --fanout-min 20 --fanout-max 200 --chain-length 2 --cpu-us 100000 \
  --jitter 0.5 --long-prob 0.02 --long-factor 8 --max-inflight 64 \
  --sample-ms 100 --seed 42
```

这组参数每请求可能超过 40 CPU 秒，可能主动进入过载。先降 QPS / CPU 时间并计算需求，再逐级升高。`chain-length=2`、扇出 20–200 对应 40–400 个链节点，另有一个汇合节点。业务的 100ms 若是墙钟时间，必须先分离 CPU 与 RPC 等待，不能直接当 CPU 需求输入。

`--capacities` 可指定每组有效 CPU 容量。默认取线程数与有效掩码逻辑 CPU 数的较小值，只是粗估；先测物理核/SMT吞吐再填写。多个线程组共享相同 NUMA 掩码时，容量不能简单相加；当前打分器不建立共享资源域的竞争模型，这种部署只能作局限明确的对照。

## 第一阶段：负载怎样进入时延

固定 seed、请求数和工作量，先使用 `home`。线程数分别设为每组有效 CPU 数附近、2 倍、16 倍，并增加业务当前几百线程的配置；比较 none / numa / cluster 亲和。每个实验顺序执行，记录其他进程、频率、温度、IRQ、quota、NUMA 内存策略。

按预计容量的 20%、40%、60%、80%、接近饱和及过载扫 QPS。另固定扇出 20、80、200、400，并调整 QPS 保持总 CPU 工作量大致相同，以区分 burst 集中程度与平均负载。

需要的直接证据：

1. `queue_before`、`pending_before` 与 `enqueue_us` 的关系。
2. 队列很浅时，`inflight_before` 和内核 runqueue 等待是否已经很大。
3. 请求初始负载与请求完成时延，按 fanout、planned CPU 分层。
4. 增加线程数是否只是把池内等待转移到了内核，而没有改善请求 p99。

`enqueue_us` 从调用路由层前到 callback 开始，包含路由、入队、唤醒及首次获得 CPU 的延迟，不能单独拆分 semaphore handoff 与 OS 调度。`wall_us-cpu_us` 包含抢占、阻塞和计量误差，不能直接命名为 runqueue latency。

## 第二阶段：图与迁移策略

固定亲和、线程数、工作量和 seed，仅改变策略：

| 策略 | 行为 | 关注代价 |
|---|---|---|
| `home` | 每请求 round-robin 选 home，全部节点回 home | 局部 burst 与等待 |
| `borrow` | 每次优先 request home，必要时借用其他组 | 借出和回迁的全部跨组边 |
| `sticky` | 以实际父节点组为偏好，后续仍可再次均衡 | 延续迁移、远离 home 的时间 |
| `fanout` | 初始扇出均衡，之后沿父节点组执行 | 后续变化时不再均衡的损失 |
| `least-load` | 按负载选池，忽略亲和惩罚 | 排队收益与迁移成本 |

加 `--rpc-prob 0.2 --rpc-delay-us 5000` 可模拟算子结束 CPU 段后由独立定时线程恢复 continuation。RPC 等待不占 executor 线程。它模拟生命周期和唤醒来源，不模拟网络协议、连接池、下游排队或重试。图是“初始扇出 → 各链 → 汇合”，不覆盖全部业务拓扑；汇合节点的 parent 取最后满足依赖的分支，不能表达所有输入的内存位置。

Demo 的定时回调显式携带 request 对象、home 和实际父池；它没有模拟真实 RPC 库的 RequestContext 传播。业务若还依赖 RequestContext/TLS，需要在异步边界显式保存并在回调作用域恢复，不能指望 CPUThreadPoolExecutor 从 AIO 线程自动找回原请求上下文。

`--memory-kb` 增加 request 工作集，但不是远程 NUMA 访问校准器。首触位置、数据依赖和真实业务不同，也不自动 mbind。配合 `numastat -p PID`、PMU 或业务内存测量后，才能判断远程访问代价。

默认 `--work-mode cpu` 以线程 CPU 时间截止，若访存变慢只会少执行一些迭代，因此不能用它证明缓存/NUMA 导致服务时间变化。要研究局部性成本，请改为 `--work-mode iterations --iterations-per-us 64`，对每段执行相同 `work_target_us × iterations_per_us` 次迭代；64 只是默认换算值，应先独立校准，并在各策略中保持相同。此时 planned CPU/estimated CPU 都是名义预算，真实服务时间以测得的 `cpu_us` 为准。

`--cross-numa-penalty-us`、`--min-gain-us`、`--relative-gain` 是选池分数和门槛，不会通过 sleep 伪造物理迁移成本。先扫 0，再扫实测代价的若干倍。`--estimate-us` 可使用固定 CPU 估计，避免只测试“调度器提前知道每段精确工作量”的理想情况；不设置时使用合成负载的计划值。

## 输出与系统采样

| 文件 | 内容 |
|---|---|
| `metadata.json` | 参数、PID、时钟、亲和和局限 |
| `workers.csv` | group 到 Linux TID 的映射 |
| `tasks.csv` | 每段任务提交/开始/完成、CPU、路由和负载 |
| `requests.csv` | 计划到达、实际投递、完成、工作量与状态 |
| `seconds.csv` | 每秒完成 cohort 的平均/p99及到达、拒绝、删失 |
| `pools.csv` | 周期性负载与计数 |
| `summary.json` | 全运行聚合及失败/拒绝/删失 |
| `proc.csv` | 可选的线程 CPU、runqueue、上下文切换增量 |

时延从计划到达时间计算，主线程迟到也计入。工作量按 request/node 固定生成，不随策略和线程交错改变。过载限制同时存活请求并记录拒绝。停止投递后等待完整 DAG（包括 RPC）；拒绝和超时删失不能从对比报告中隐去。

```bash
# 手动运行时填真实 PID；run_matrix.py 自动启动采样器。
python3 scripts/proc_sample.py --pid PID --workers results/runs/arm-home/workers.csv \
  --output results/runs/arm-home/proc.csv --interval 1
python3 scripts/analyze.py results/runs/arm-home results/runs/arm-borrow \
  --output results/runs/comparison
```

`/proc/PID/task/TID/schedstat` 第二项为内核记录的 runqueue 等待纳秒数，须核实内核配置及 `kernel.sched_schedstats`；未启用时的零不能解释为无等待。采样器只读、不改内核设置。线程退出前最后一段可能漏采，不能强求其总量与逐任务 CPU 完全相等。[内核 schedstat 文档](https://www.kernel.org/doc/html/latest/scheduler/sched-stats.html)

独立诊断轮次可使用现场允许的 `perf sched record -p PID`、`perf sched timehist`，或 sched_wakeup/sched_switch 追踪。唤醒到切入只覆盖新唤醒后的 runnable 等待；抢占后的等待需结合切出状态。追踪与启用 schedstats 均有扰动，应与关闭时成对测量。

## 自动矩阵与统计

`run_matrix.py` 默认是本地两个逻辑 CPU 分区以及六池无绑定对照，不声称发现了真实 cluster。ARM 使用 `--cases-json`，每项为 `{"name":"arm-home","args":["--pools","6","--threads","16","--cpus","实际cpulist","--numa","0,0,0,1,1,1","--affinity","cluster","--policy","home"]}`。`--extra` 放最后，可追加 RPC、内存和惩罚参数。

每轮相同 seed、随机案例顺序，下一轮使用新 seed，同名目录不覆盖。保存完整命令、退出码与原始 CSV。分析器提供 nearest-rank p99、Pearson / Spearman、队列桶等待、请求初始负载与计划工作量的相关性和初步分层。可选 `--plot` 需要 matplotlib。

生产每点建议至少 5 次完整重复，预热单独运行，正式采样数千到数万请求。12 QPS 下每秒 p99 通常接近最大值，不能据此判断 0.5% 收益。以独立运行或时间块为统计单位给差值区间，同一 DAG 的数百任务不是数百次独立重复。比较同 offered load 下的吞吐、拒绝率、CPU-seconds/request、p50/p95/p99 和 runqueue 时间。

相关性不是因果证明。长任务同时推高负载与时延、动态选池、不同 fanout、只保留成功请求均会混杂结果。先做固定工作量的受控扫点，再做随机业务形态并分层，避免只展示一组最佳参数。

## 低扰动边界

原型有时间戳、线程 CPU 时钟、原子计数、预分配节点记录和每请求一次的日志交付，不是未经验证的“零开销”方案。`--task-log 0` 关闭任务 CSV 写出，但保留计时及记录，因此只是日志输出的对照。共享缓存行上的 RMW、跨 NUMA 写入、扫描和日志都需要单独测量。

近似读取允许旧值，不允许 C++ 普通变量的数据竞争。生产可按 worker 分片累积 CPU、低频发布原子快照，按 request 抽样详细时序；严格 admission 计数与近似选池指标应分开。当前全扫描 6 组，未验证数十或数百组的扩展性。

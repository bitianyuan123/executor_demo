# 阶段3：独立任务的 Folly 负载—时延定量实验

本阶段回答：在给定 CPU 资源、线程数和到达过程下，原版 Folly 的队列观测值在多大程度上解释任务等待和完成时延；补充 pending/inflight/工作量估计是否有价值，额外计量带来多少扰动。

工程是 [`experiments/independent`](../../experiments/independent/README.md)，独立 CMake 项目和 `independent_load` 可执行文件。它只链接原版 `folly::CPUThreadPoolExecutor`，没有 DAG、RPC 或实验性 `AffinityExecutor`。本页描述默认 home 投递的原始实验；新增的可选阈值投递实验单独见[负载指标与均衡设计](BALANCE_DESIGN.md)、[运行指南](BALANCE_GUIDE.md)，不会混入历史结果。阶段4、5的策略不会进入本阶段的运行路径。

## 1. 实验单位与时间边界

每次 arrival 由开放环发生器按预先生成的时间到达。默认 `--burst-placement local` 通过 round-robin 为每次 arrival 选择一个池；新增 `spread` 按 task ID 跨池 round-robin，在完全相同的到达和任务预算下单独改变空间集中程度。一次 arrival 可以提交 K 个独立 CPU 任务；这些任务之间没有依赖，没有汇合任务，也不会继续投递任务。记录该批任务全部完成的最晚时刻，仅用于另算 burst 完成时延。

- **arrival/s**：批次到达率。K=1 时等于任务到达率；K>1 时任务到达率约为 `arrival/s × E[K]`。
- **task**：一个独立 CPU 回调。主分析单位是 task；平均值和 p99 不与 burst 指标混用。
- **burst**：同一次 arrival 的 K 个任务。只有全部成功，才有完整 burst 完成时延；发生拒绝、取消或失败的 burst 单独计数。
- **执行时间随机性**：任务 CPU 目标为 `cpu-us × U(1−jitter,1+jitter) × M`，M 以 `long-prob` 概率等于 `long-factor`，否则为1。目标在投递前全部生成，完成顺序不影响工作量。默认 `--mean-normalization none` 保持原始分布；`exact` 将有限样本总预算精确归一化到 `任务数 × cpu-us`，用于控制同均值分布对照。归一化后各任务不再严格独立同分布，且绝对长尾时长会变化，不能把该对照当作线上原始服务时间分布。
- **开放环**：regular 为固定间隔；poisson 为指数到达间隔。发生器使用绝对单调时间，不能因前一个任务慢而降低后续计划到达率。

同种子使用分开的到达时间、burst 宽度、任务 CPU 预算随机流。同任务数量、同种子和相同服务时间参数的 smooth/burst 配置具有相同的逐任务 CPU 预算序列。默认 local 下，任务被分配到哪个池会随 burst 宽度改变。因此新增三角对照：smooth local → burst spread 主要改变时间集中；同一 burst spread → local 单独改变池内集中。相同任务数和服务参数下，两种 placement 的预算序列完全相同。跨编译器/标准库不承诺随机分布逐位一致，应核对 `tasks.csv` 的目标序列或总预算。

四个时间戳来自 `CLOCK_MONOTONIC`：

|时间戳|位置|由此得到的指标|
|---|---|---|
|planned|预生成计划到达|planned→submit 为发生器迟到及逐任务投递耗时|
|submit|读取负载前、准入和 `pool.add` 前|submit→start 为用户侧调度等待|
|start|Folly 调用用户回调的入口|start→end 为回调墙钟时间|
|end|CPU 工作结束，完成统计清理前|submit→end 为 task 时延|

`submit→start` 包括本次负载读取、准入、Folly add/排队/通知、内核调度和回调入口路径，**不是严格的 Folly 容器驻留时间**。`start→end` 可能包含回调执行期间的抢占；不能仅凭该数值区分 runq 等待、缺页或其他因素。`planned→end` 保留发生器迟到，避免只报告实际提交后的时延而掩盖投递端跟不上压力。

每秒输出按**完成秒**分组的 task 均值/p99；分析脚本另外给出按**计划到达秒**分组的趋势。总体 p99 从原始任务计算，禁止平均各秒 p99。

## 2. 负载观测及插桩层次

所有值在当前任务入队之前读取，所以同一 burst 后提交的任务通常看见之前已提交的任务。多个字段不是同一原子快照。

|模式|每次提交读取|回调统计|额外周期采样|
|---|---|---|---|
|none|无负载字段|基本时间戳；必要的准入/排空计数|无|
|queue|原版 `getTaskQueueSize()`|同 none|只读原版 queue|
|counters|queue、pending、inflight|每池 relaxed 计数更新|以上三项|
|full|以上三项、work estimate|再记录工作量估计、线程 CPU 时间和 worker TID|所有指标|

所有模式都保留安全所需的全局 outstanding 原子计数、取消检查、基本 task 时间戳；CPU 预算模式还必须读取线程 CPU 时钟。`none` 是低插桩基线，**不是完全无开销基线**。

- `native_queue` 是当前固定版本 Folly 默认队列暴露的近似信号量/队列指标。它属于用户态 Folly 任务管理，**不是 Linux runq**。queue=0 不代表没有执行中的任务，也不保证新任务立刻获得 CPU。
- `pending` 是已通过本 demo 准入、尚未进入回调的数量，包括 `pool.add` 之前的一小段提交时间和被通知但未开始回调的时间。
- `inflight` 是回调已开始、尚未完成的数量。被抢占或等待内存的回调仍包含在其中，不能当作当前正在 CPU 上执行的线程数。
- `work_estimate_us` 是未完成任务的**完整预估预算之和**，直到任务结束才减去，并不随任务进度递减。`--estimate-us 0` 使用合成实验已知预算，是一种理想信息对照；生产可用性应另测固定或类别估计。`--estimate-jitter F` 在独立随机流上为目标或固定估计乘上 `U(1−F,1+F)`，不改变实际工作量，用于估计误差灵敏度。

本阶段不在每个 task 上读取 `/proc`，也不把 `inflight−CPU数` 直接标成内核等待。需要内核归因时，另做低频 `/proc/TID/schedstat` 或独立追踪对照，避免把高频追踪的性能当作主实验结果。

## 3. 部署变量必须保持可比较

### 开发环境

默认按本进程允许 CPU 列表分成两个 disjoint 分区；只有一个 CPU 时使用一个池。`cluster` 在开发机上表示**人工 CPU 分区**，不声称是物理 cluster。开发机验证计量正确性、过载形态和相关性分析方法。

开发机可比较 none 与人工分区，但必须查看 `metadata.json` 中 `same_cpu_union`。若为 false，none 的 allowed CPU 比显式分区并集更大，容量不同，不能当作纯亲和性实验；`extra_allowed_cpus` 列出多出来的 CPU。

### 六池测试环境

用户提供六个真实 cluster 的 CPU 列表及其真实 NUMA node ID。程序检查数量、CPU 是否在 allowed mask 内、显式组之间是否重叠，以及每个 CPU 是否属于指定 NUMA node。程序不凭连续 CPU 编号猜测物理 cluster；cluster 是否等于 LLC 域、SMT sibling 分布仍需结合 sysfs 和硬件资料核实。

`--deployment target` 强制六组 CPU 并集等于进程 allowed mask。因此：

- none：六池均可在相同总 CPU 集合内运行。
- numa：同 NUMA 的三个池共用该 NUMA 的 cluster CPU 并集。
- cluster：每池只用对应 cluster 的 CPU。

若专门保留部分 CPU 给 IO/AIO/发生器，先用 `taskset` 将整个实验进程约束到六组并集再启动。该发生器和采样线程仍会使用该集合；这类控制线程的开销必须保留或单独标定，不应悄悄给 none 更多 CPU。

线程数量固定为 min=max。建议测试每池 `8、16、32/64、256`，覆盖物理核附近、逻辑核附近和过量线程；不要把96逻辑 CPU 当作96物理核或双倍吞吐。NUMA 形态中三个池共享资源，单池不能各自按整个 NUMA 的容量重复计算。

## 4. 有顺序的实验设计

|轮次|保持一致|改变什么|主要回答|
|---|---|---|---|
|A 功能验收|小工作量|各插桩、CPU mask、拒绝/取消/失败|输出守恒、计量含义是否正确|
|B 服务能力标定|K=1、固定任务种子、低扰动|线程数、逐级增加 arrival/s|每种部署的可持续吞吐及队列稳定区间|
|C 负载扫描|亲和、线程数、服务分布|约0.2/0.5/0.8/1.1倍标定能力|queue/pending/work 与等待如何变化|
|D 同平均压力的 burst 对照|任务数和逐任务预算序列、总任务率|K=1/20/100/400，arrival/s 相应反比调整|平均负载相同，局部突发如何改变尾延迟|
|E 线程和部署对照|同种子、同 offered workload|none/numa/cluster × 线程数|减少队列等待是否转移为回调内等待|
|F 服务时间异质性|尽量保持平均 CPU 预算相同|固定时长、均匀随机、少量长尾|队列“个数”和预估工作量何时分离|
|G 插桩成本|固定迭代数、同种子、同任务数|none/queue/counters/full，采样周期|指标是否值得其测量开销|

每轮先固定其余变量。不要一次全扫所有组合后仅比较最优一行。旧 `run_matrix.py --preset target` 仅保留为历史粗筛配置，**不是完整正式对照矩阵**。本轮新增 [`make_cases.py`](make_cases.py)，按九个可解释的问题分别生成实验族，完整覆盖、命令和样本门槛见[设计复核](DESIGN_REVIEW.md)。其中12 arrival/s ×100/400 tasks ×约100ms 是单独的需求增长/过载探针，不代表预计可以稳态运行。

### 容量归一化：如何使用 ρ

对固定部署/线程数，用成功任务的 CPU 服务需求 `E[S_cpu]` 和该配置可持续提供的 CPU 服务率 `C_eff` 定义诊断量：

`ρ_cpu = λ_task × E[S_cpu] / C_eff`，单位为 `(task/s × CPU秒/task) / (CPU秒/s)`。

对于 burst，`λ_task = λ_arrival × E[K]`；混合长尾分布的在 `mean-normalization=none` 下名义均值约为 `cpu-us × [1 + long-prob × (long-factor−1)] / 10^6`，均匀 jitter 的理论均值为1；`exact` 下有限样本均值就是 `cpu-us / 10^6`。实际应从生成目标总预算/任务数核对，并从 full 模式线程 CPU 时间检查预算超出和估计偏差。

`C_eff` 不能直接取线程数、逻辑 CPU 数或每池重复计入共享 NUMA CPU。先排查 cgroup 配额，使用固定工作、逐步加压，在队列无持续上升、完成率追得上到达率的区间测量稳态 CPU 服务率；或者先得到该工作负载的可持续任务吞吐 `λ_sustainable`，用 `ρ_empirical = λ_offered / λ_sustainable` 作为经验归一化。后者只适用于相同任务分布、亲和方式和线程配置。若没有足够长的稳定运行，报告“名义 offered CPU demand”，不要给出伪精确的ρ。

D轮同平均压力对照与“固定12 arrival/s、改变K”的对照是不同问题：前者隔离突发集中效应，后者同时增加总 CPU 需求。两种都可以做，但结论必须写清。

### 启动、稳态、排空与重复

线程池在计时前建好，初始队列为空。开发 quick 默认4秒，仅用于功能和现象观察。测试环境建议每轮至少120–300秒，先从20–30秒预热和足够长的中间窗口开始，结合趋势决定窗口是否真的稳态；持续上升的队列不能通过简单删除前20秒变成稳态。

分析可用 `--warmup-seconds 20 --steady-end-seconds 100` 按**计划到达时间**选择同一 cohort。入选任务可以在100秒后完成，不能按完成时间截断掉慢任务；但若超过排空期限被取消，必须报告删失。终止投递后的 drain 区间不计入稳态吞吐，整轮平均可另报。

正式候选配置默认7个种子/重复；数量是否足够仍由 pilot 的配对差波动和目标效应决定，不能用“7轮”自动证明0.5%差异。runner 以重复号为时间块，每个块内随机打乱 case，同重复号使用相同种子，以减小配对运行的时间漂移。主比较采用配对差值，展示逐次差值及其离散程度。不能将同一 burst 内的任务视为完全独立样本来制造极窄置信区间。p99需要足够完成样本；几十/几百个任务只支持粗略描述，不能验证0.5%的业务收益。

## 5. 如何分析，哪些结论成立

1. **先审查有效性。** 检查 `conservation_ok`、failed/rejected/canceled、采样截断、发生器迟到、允许 CPU 集、cgroup 配额、预算一致性，以及按计划到达秒的时延趋势。失败或持续过载不能与成功稳态结果混排。
2. **分解时延。** 先比较 submit→start，再比较 callback wall 和最终 task 时延。更多线程可能使 Folly 队列更短，却让更多回调在内核中竞争 CPU；本阶段只测得其外部表现，内核原因需要额外证据。
3. **看相关性与条件分布。** 分别计算 queue/pending/inflight/work 对等待、回调时间和最终时延的 Pearson/Spearman，画或查看 queue 分桶、同 pool/目标时长分层。随机执行时长会直接影响最终时延，所以“队列→等待”通常比“队列→最终时延”更贴近问题。
4. **检查 queue=0。** 统计 queue=0 但等待超过1/5ms的比例和最大值。它说明queue单指标存在盲区，不意味着所有queue=0都失效；也不能据此断言具体等待全部发生在Linux runq。
5. **没有方差时不强求相关性。** 例如低负载下所有入口queue=0，相关系数应为null。需要提高压力或采用burst，而不是把null解释成“没有关系”。
6. **观测相关不等于因果。** 时序过载会使队列和时延一起增长；服务时间分层也不能完全消除burst共享和时间趋势。优先使用随机顺序、同工作量配对实验，验证改变线程/亲和后整个分布如何变化。

插桩成本轮次优先使用 `--work-mode iterations`：CPU预算模式主动把工作循环调整到指定线程CPU时间，可能隐藏观察器导致的有效工作量变化。比较总进程 user/system CPU、完成吞吐、延迟和配对差值；`(CPU_full−CPU_none)/完成数` 只能作为该工作负载下的整轮额外CPU估计，包含共享计数竞争、周期采样和缓存效应，不是某个原子指令的纳秒成本。差值小于运行波动时应报告“不足以分辨”，允许测到负差值，不强行截成正数。

## 6. 新增指标是否提高预测准确性

[`predict.py`](predict.py) 用整轮留出验证比较“自身目标服务量”、queue、pending、queue+inflight、work+inflight。同一配置至少两次独立重复；每次拿一整轮测试，其余轮训练，不能随机拆同一burst的任务到训练和测试两边。报告等待和完成时延的MAE、p95绝对误差、逐轮误差和相同覆盖样本上的基线。缺少观测的字段不补零。

```bash
python3 stages/03-independent/predict.py \
  results/runs/independent-thread-sweep/manifest.json \
  --output results/local/independent-prediction
```

只有一轮的quick实验不会被强行拟合。模型使用简单线性关系，比较的是这种模型在相同case里的描述性预测，不是因果识别或对线上任意工作负载的泛化保证。默认work使用已知合成预算，属于比生产更理想的信息；实际选型须增加 `--estimate-us` 固定/误差对照。准确性应与[指标读取及维护成本](../../experiments/metrics/RESULTS.md)、四档完整观测成本一起判断。

本次短轮次的实际结果另见[RESULTS](RESULTS.md)，不混入历史DAG结果。

## 7. 数据与安全边界

每个任务有独占预分配记录，工作线程不写CSV，不争夺逐任务日志锁。排空并 join 后统一序列化，避免把磁盘输出放进回调路径。全局 outstanding 是非阻塞准入保护，不是池选择策略；超限的任务明确拒绝。超过 `duration + drain-seconds` 后任务协作取消，排队回调也会进入后立即退出，仍能释放计数。

回调异常有 scope guard 释放全部计数，写 failed 状态并使进程返回非零；`--fail-task` 仅用于故障测试。计划任务数由 `--max-planned-tasks` 限制，累计工作量估计检查溢出。周期采样由 `--max-samples` 限制；超出后写出丢弃数和截断标志，不能把不完整序列用于完整时间段分析。进程被强制杀死或输出失败可能没有完整 summary，分析脚本拒绝缺失/截断数据。

这里没有实现任务抢占、CPU配额控制、NUMA内存绑定，也没有模拟真实算子内存/RPC行为。阶段3通过后，再进入阶段4讨论依赖图与request亲和；不能用阶段3成功替代阶段4的验证。

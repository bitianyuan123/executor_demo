# 阶段 4 实验设计复审：可回答的问题与生产外推边界

本文件是对首轮设计的修订，保留 [RESULTS.md](RESULTS.md) 的原始实验与历史结论。先前 60 算子、2～4 ms、几秒窗口的实验支持功能和局部机制判断，不能独立支持生产参数选择、稳定 p99、真实 NUMA/缓存收益或“迁移策略普遍更优”的结论。

## 1. 首先满足真实业务的工作量守恒

已知 QPS 12，20～400 是取值范围，并未给出各图大小的概率。不能把 `fanout-min=20,fanout-max=400` 的均匀分布直接当成真实业务；其均值是 210，若每算子真的消耗 100 ms 线程 CPU，则需要约 **252 CPU 秒/秒**，已经与机器规模及约 50% 总利用率矛盾。

如果“50%”确实按 96 个逻辑 CPU 的 CPU 时间定义，则全应用约使用 48 CPU 秒/秒；12 QPS 对应全应用每 request 约 4 CPU 秒，executor 只占其中一部分。在算子平均线程 CPU 为 100 ms 的前提下，平均每 request 算子数应不超过约 40。这个条件推导不意味着确认了真实利用率口径；也不把 SMT 吞吐视为翻倍。上线实验前应核实利用率分母、executor 占比、算子 CPU/墙钟、实际 N 分布及 RPC 等待是否包含在 100 ms 内。

因此必须分开两类实验：

- **固定业务需求**：12 QPS、理论平均算子 CPU 100 ms、20/60/200/400 算子分别扫描，明确高 N 是压力端点，非假造的生产分布。不能为了让所有配置完成而降低其中某个策略的 QPS 或工作量。
- **容量归一试验**：先测整个 executor 的有效 CPU 容量 `C_eff`，固定 12 QPS、60 个算子，调小/调大 CPU 预算得到 rho≈0.25/0.5/0.75/0.9/1.05。它隔离平均压力与策略，不声称保留 100 ms 业务尺度。CPU 时间模式下的 C_eff 应来自可持续 CPU 秒/秒；固定迭代模式则需重新按实测服务成本校准。

`rho = QPS × E[request CPU 秒] / C_eff` 是整体平均需求，不是某个 cluster 的瞬时繁忙程度。按 cluster 绑定仍可能在 rho<1 时遇到局部扇出排队。对 pool mask 重叠的 none/numa，不能把每池标注 capacity 相加当全机容量。

## 2. 本轮补齐的负载能力

### 开放到达：regular 与 poisson

新增 `--arrival regular|poisson`，默认 regular 保持历史命令语义。Poisson 每个间隔独立取指数分布，平均速率仍为 `--qps`；首个请求也等待随机间隔。计划在计时前生成，随机流独立于图、算子预算、RPC 和路由，同 seed 的策略对照具有相同外生到达。

`--duration` 定义计划到达窗口，`--requests` 是可选上限，不是需要凑满的数量。Poisson 的实现**不会把请求数截成 ceil(QPS×duration)**；这样才保留繁忙种子的请求数波动。最多一百万个计划请求，超界报错。定时器、CPU 或日志变慢都不会修改计划时间；发生器实际滞后单独记录。

Poisson 能避免规则 12 QPS 与轮转 home 的周期对齐被误当成一般规律，但它也不是生产流量的断言。流量相关性、microburst 和真实回放仍需另测；先比较 regular/poisson，再决定是否有必要增加更复杂到达模型。

### 真实长尾范围与总体均值

新增 `--long-min-us`、`--long-max-us`；两者一起指定时，尾部从这个绝对范围均匀抽样，覆盖 `--long-factor`。尾部不会再被普通 `--jitter` 放大；未进入尾部的算子保留普通抖动。默认历史倍数模式继续可用。

100 ms 基础预算 × 2% 的 8 倍尾部，使总体均值变成 114 ms，不能用于“仅改变长尾”的等均值对照。生产家族用 `p=0.02`、尾部均值 750 ms、目标总体均值 100 ms，计算：

`E[非尾部 CPU] = (100000 - 0.02 × 750000) / 0.98 ≈ 86735 μs`。

生成器分别产生固定 100 ms 与“普通均值 86.735 ms ±20%，2% 尾部 700～800 ms”的对照。两者理论均值相同（整数取整误差小于 1 μs），**不保证有限样本逐 request 总 CPU 完全相同**；要报告实际 `planned_cpu_us`、跨种子区间和分层结果，不能把有限样本工作量偏差解释为路由收益。同一分布下不同路由则保证逐算子名义工作量相同。

### RPC 随机延迟和普通后继

新增 `--rpc-jitter F`：每个 RPC 的计划延迟为 `rpc-delay-us × Uniform(1-F,1+F)`，独立于是否发生 RPC、CPU 预算和调度顺序；默认 0 保持原定时器模型。stage 1 的 `rpc_delay_target_us` 记录该次真实配置的延迟，依赖审计验证其没有提前恢复。该值不包括 timer 线程实际调度滞后；观察到的间隙可以更长。

新 `continuation` 家族使用 60 个算子、两波 `20,10`、深度 `2,2`，所有策略保持图和工作量相同。比较 `home / borrow / sticky / fanout-initial / fanout-all`，分别在无 RPC 和 35% RPC、20 ms±75% 延迟下运行：

- `fanout-initial` 对 `fanout-all`：允许后续 fork 再均衡的价值；普通后继都跟随 parent。
- `fanout-all` 对 `sticky`：二者偏好实际 parent，区别是普通链后继、join、RPC 恢复是否也允许重均衡。不能仅用初始 fanout 数判断这项差异。
- `sticky` 对 `borrow`：每次决策偏好 parent 还是固定 request home，观察回迁与再次迁移。

`analyze_graph.py` 新增 `routing_by_transition`：分别报告 initial_fork、later_fork、ordinary_successor、rpc_resume、join 的回调数、迁移、跨 NUMA、回迁以及提交到开始时延。这样不会用“总迁移率下降”掩盖某类后继排队上升。迁移是就绪闭包改投池，并未把已入队任务移动到别池。

### 候选域：同 NUMA 硬约束与全机借用

新增 `--candidate-scope all|preferred-numa`，默认 all 兼容历史行为。preferred-numa 只允许进入与本次 preferred 池同 NUMA 的池；borrow 的 preferred 是 request home，sticky/fanout 则跟随实际 parent，least-load 也遵守域约束。与跨 NUMA 软惩罚不同，该选项不会因为负载足够大就跨域借用。

DAG 运行器要求受限候选域必须使用 cluster 或 numa 绑定，以及显式 CPU/NUMA；逐 CPU 核对 sysfs 的实际 NUMA 节点。none 模式不能靠一个标签证明 worker 实际在哪个 NUMA 执行，因而拒绝。metadata 记录 candidate_scope 与 numa_labels_verified；默认 all 兼容历史标签，不宣称已做物理校验。

locality 家族固定迭代数，对每个数据大小/策略配对 all 与 preferred-numa，软迁移惩罚固定为 0，先隔离候选范围的作用。生成时要求显式提供经外部校准的 iterations-per-us。当前 first-touch 仍由发生器控制，故这完成了“是否允许跨 NUMA 借用”的机制对照，尚未完成“真实数据归属与缓存收益”的生产模拟。单 NUMA 开发机的两个候选域应退化为相同集合，不允许伪造 NUMA 标签制造区别。

## 3. 修订后的覆盖矩阵与执行顺序

| 层次 | 控制条件 | 变量与最小对照 | 足以支持的结论 |
|---|---|---|---|
| 机制与完整性 | 小预算、小请求数 | chains/waves、RPC、超载取消、五策略 | 节点/预算守恒、依赖和收尾正确 |
| 部署 | home、同到达/图/预算、同 CPU 并集 | 三亲和 × C/2、C、2C、8C、当前业务数百线程 | 绑定/线程数对等待和服务成本的局部影响 |
| 压力 | 同部署、60 算子、12 QPS | capacity 五 rho × regular/poisson，先 home 后三路由 | 可用余量和局部扇出压力如何影响收益；rho>1 单列过载 |
| 图形 | 60 算子、相同 seed、相同 join 数和预算 | 前重/均匀/后重/深链 | 初始扇出、后继依赖形状的影响 |
| 决策时点 | 相同深链/到达/预算 | continuation 五策略，先无 RPC、再随机 RPC | 仅首 fork、各 fork、全部后继均衡的增量价值 |
| 实际尺度 | 12 QPS、总体平均 100 ms | production 的 N=20/60/200/400，固定值/绝对尾部 | 在给定 CPU 假设下哪些负载可持续，哪些必然过载 |
| 估计健壮性 | 同一图/工作量/压力 | queue、pending、work；固定错误 estimate 与完美预算分别 | 负载观测选择是否依赖理想估计 |
| 成本与局部性 | 同迭代数、同数据集、真实服务器 | 不同数据大小、all/preferred-numa 候选域，另测路由惩罚阈值 | 只有完成下述局部性前置条件后才可作生产亲和收益判断 |

先用部署基线筛选 1～2 个线程数，再围绕它们跑压力和决策时点；不要盲跑整个笛卡尔积。目标机六个 cluster，若每 cluster 8 物理核/16 逻辑 CPU，经验证后可从每池 `8,16,32,128,256` 取值；这不是推荐 256 线程。必须包含现有配置，才能判断过量线程是否掩盖用户态队列或增加内核等待。

三亲和使用相同 CPU 并集。none 和 numa 下共享硬件的池容量不是独占份额，跨池路由收益可能主要来自队列分散。六池跨两个 NUMA 的真实测试不可用开发机两个逻辑分区代替。

## 4. 新生成器用法与预算侧车

以下开发机示例的 CPU 编号必须与本机 allowed mask 对齐。生成配置不执行负载；每份 JSON 同时产生 `.budget.json`，写出期望 request CPU、全局需求与 rho，缺少校准容量时 rho 为 null，不猜测容量。

```bash
# 容量示例仅表示 C_eff=2 已由前置校准获得，不应直接拿逻辑 CPU 数填写。
python3 stages/04-dag/make_cases.py --family capacity \
  --cpus '0,1;2,3' --numa '0,0' --threads 2,16 \
  --effective-cpus 2 --load-ratios .25,.5,.75,.9,1.05 \
  --output results/local/dag-capacity.json

python3 stages/04-dag/make_cases.py --family continuation \
  --cpus '0,1;2,3' --numa '0,0' --threads 16 \
  --output results/local/dag-continuation.json

# 仅生成真实尺度计划；100 ms 不适合在开发机直接跑整个矩阵。
python3 stages/04-dag/make_cases.py --family production \
  --cpus '0,1;2,3' --numa '0,0' --threads 16 \
  --qps 12 --cpu-mean-us 100000 --tail-prob .02 \
  --tail-min-us 700000 --tail-max-us 800000 \
  --output results/local/dag-production-plan.json
```

固定迭代候选域对照另用 `--family locality --iterations-per-us N --memory-sizes-kb 0,256,8192`；N 应先校准，各候选域保持同一 N，不能分别按运行速度重调。正式运行应同时保存 NUMA 页位置和算子服务时间证据，否则只能解释候选域/排队效果。

测试机加 `--deployment target`，要求六份不重叠的已验证 cluster masks。`--effective-cpus` 是预算计算用的全 executor 容量；它不修改路由器的每池 `--capacities`，后者有另一层语义。运行方法仍使用 [README](README.md) 的 `scripts/run_matrix.py`，以 `--only` 分批选子集；`--extra` 会覆盖 case，任何覆盖都须在最终 manifest 中复核，避免预算文件失效。

先用 30～60 s 容量筛选排除明显过载，再针对候选做多 seed、随机顺序、长窗口对照。12 QPS 下 8 s 只有约 96 request，单轮 p99 接近最大值；不能把本地几秒补测升级为生产 p99 结论。正式尾延迟分析需要足够请求数、预热/测量分段和运行级置信区间。请求到达很稀疏时尤其应同时报告并发 request 分布，避免一个几乎没有重叠的实验声称研究并发。

## 5. 仍未被工程补齐的关键边界

- **数据亲和成本**：目前数据仍由发生器 first-touch、只读共享，没有控制每页 NUMA 位置和算子读写所有权。CPU-budget 模式可能在远端内存变慢时少做迭代，必须用固定迭代量、对等工作量和服务成本分析；即便如此，当前代码也不能代表真实缓存复用。已实现 preferred-numa 候选硬约束并验证 CPU 标签，跨 NUMA 惩罚则是软阈值，两者不可混称。该项是现场生产决策前的明确缺口，不借助合成迁移惩罚伪造收益。
- **真实到达/图分布**：目前固定 N 扫描和形状对照用于机制研究，没有测得的混合概率、任意 DAG 导入、RPC 下游拥塞和相关长尾。生产 trace 是这些结论外推的前提。
- **日志 admission 耦合**：finished request 等待 CSV 写出期间仍占 `max-inflight`，所以 rejected 可能由遥测积压触发；不是所有 rejected 都代表 executor 算力耗尽。由 `offered_ns/finish_ns` 重建未完成 request 并发，结合磁盘/日志负载；补做 task-log=0 对照。该模式仍有时钟和统计成本，不能叫无观测开销。
- **窗口与截断**：完成样本 p99 必须和 offered/accepted/rejected/censored、发生器滞后和排空时长同时报告；超载运行的所有已完成请求不能自动成为稳态样本。不同种子的 Poisson 请求数可能不同，同 seed 策略配对必须一致。
- **完美 work 指标**：合成预算是未来工作的先验信息，生产未必知道；queue 是合理首选基线，不能仅凭 work 在某组中更好就否定队列。

这次修改提高了对照的辨识度，并把无法支持的结论明确留在边界之外。当前能力足以在测试机定量检验“何种压力/图形/后继类型下路由降低等待”，尚不足以单凭 demo 证明生产 NUMA 亲和策略最优。

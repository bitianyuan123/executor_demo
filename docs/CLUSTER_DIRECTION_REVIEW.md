# 研究方向复核：每硬件cluster一个池

用户明确的业务改造目标是：所选CPU资源内，每个硬件cluster创建一个Folly子池；请求先确定home，算子在局部繁忙时按策略借用其他cluster。4池×128线程、16 QPS、无CPU绑定仅是此前trace的临时采集条件。

## 1. 审查结论：实验入口发生偏移，核心实现保持通用

| 审查项 | 修订前的事实 | 判断与处理 |
|---|---|---|
| `real-baseline`矩阵 | 强制4池/128线程/none/16QPS | 名称和优先级误导；明确标为历史重放，旧名保留兼容并发出弃用说明 |
| 最新实验指南 | 把四池列为第一条“真实部署路线”，主要命令使用4池none | 确有研究方向偏移；主线重写为cluster home校准、过配、L2与数据亲和 |
| 请求/算子负载 | 新增入口burst，但旧指南先集中验证四池入口 | 单request内部扇出恢复为核心；入口burst保留为补充变量 |
| 默认可执行程序 | affinity=cluster，QPS=12；未给CPU分区时用两个CPU做开发smoke | 没有默认转成四池；生产矩阵必须显式传已验证的cluster列表 |
| 总池与路由 | home对`groups.size()`取模；候选扫描与状态按实际组数分配 | 不依赖4或6；无需重写核心算法 |
| CPU池构造 | 每个GroupConfig创建一个Folly池；线程数由配置决定 | 没有将128设为必需线程数 |
| 负载与容量 | cluster按自身partition容量；none显式池数才使用CPU并集平均份额 | 没有把四池的24逻辑CPU名义份额套用cluster |
| NUMA、provider | 从实际标签/组数构造，不要求每NUMA恰好3池 | 同NUMA优先及跨NUMA策略可继续用于cluster主线 |
| 历史20/80 | 临时配置的单池统计，定义仍待核对 | 只能作为旧负载证据，不能成为目标cluster的阈值或校准目标 |

源码依据：[主程序配置与分区](../experiments/affinity/main.cpp)、[RequestExecutor](../experiments/affinity/include/executor_demo/RequestExecutor.h)、[BusinessExecutor](../experiments/dag/include/executor_demo/BusinessExecutor.h)、[负载评分](../experiments/affinity/include/executor_demo/LoadPolicy.h)。`cluster/numa`下pool-count必须与partition数一致；六个分区配四池会报错。当前CLI最多64池，采样器按实际组数分配；不是无限制规模承诺。

## 2. 恢复的主研究结构

当前独立子集为6个cluster、96逻辑CPU、48物理核，因此采用6池。扩展到其他资源范围时，池数由其中实际cluster数量决定。

```text
请求到达 → 一个RequestExecutor总池
  L1：为request选择一个cluster home
  L2：每次就绪算子保留home/parent，必要时借出
      本NUMA其他cluster优先 → 按开关考虑跨NUMA
      每个cluster内部是一个Folly CPUThreadPoolExecutor
```

主线围绕相同目标部署比较：

1. home固定路由下校准单request扇出与固定工作，再增加request重叠。
2. 扫每池worker数量，区分用户队列等待与内核竞争；128只是过配档位。
3. 固定线程档和逐request工作，比较queue、Q+I、outstanding、CPU慢反馈、work估计。
4. 使用真实分支数据依赖，比较本NUMA借出、跨NUMA、parent/home、fork/all、chunk与阈值，检查等待收益是否抵消执行变慢。
5. 保持池数/线程数/CPU全集相同，none/numa作为亲和控制组。四池历史配置不参与该单变量对照。

12和16 QPS均可作为压力点，不能因临时trace恰好16 QPS就固定目标值。初始20～400扇出与入口每批几个request是两个维度；改变宽度也改变总工作时，要明确是容量压力对照。具体命令见[cluster实验矩阵](REAL_WORKLOAD_MATRIX.md)。

## 3. 本轮工程防误用措施

- 新增`cluster-baseline`：显式要求`--affinity cluster`和非空`--cpus`，拒绝空分区；pool-count仅能省略或0，由分区数量导出。线程数/QPS来自调用者。
- 新增`legacy-four-pool-replay`；旧`real-baseline`行为保持不变并发出警告，避免悄悄改变历史命令的语义。
- 矩阵metadata记录`suite`、`experiment_role`、`research_target=one_pool_per_cluster`。角色标签表达研究目的，不代替真实拓扑证明。
- `threads`套件补入128档，保留8/16/64/256；其他通用suite不注入四池配置。
- README、业务分析、观测报告和模型提示词统一区分目标与历史条件；旧数据不重新贴目标部署标签。

## 4. 仍需明确的边界

**硬件拓扑没有自动发现/强校验。** `--affinity cluster`目前表示将池绑定到用户给定分区，只检查CPU可用、不重叠及同NUMA。任意一个同NUMA分区不一定是硬件cluster。主实验必须使用已确认的topology，将每个硬件cluster作为一个分区，并核对实际池CPU mask和NUMA标签。开发机人工分区测试仅验证机制。本次未把文档纠偏扩展为另一套拓扑发现实现。

当前CLI的threads与capacity-scale对所有池统一，核心GroupConfig支持逐组参数；scale=1仍是逻辑CPU计数，不是标定后的SMT吞吐。none下不等大分区还存在“显式同数pool-count改用均分容量”的配置差异；当前等大六cluster控制组不受影响，扩展异构分区时需固定并审查容量配置，不能自动外推。

旧trace继续用于事件定义、任务突发和等待机制分析，按其真实四池映射处理；不能事后将TID划成六个虚构cluster池。没有旧trace报告也可以推进cluster实验，二者相互补充而非前置阻塞。

## 5. 首轮纠偏验证（751a465）

本次未修改C++调度或Folly源码，已有二进制可使用新矩阵。计划测试覆盖2/3/6个分区、保留用户线程数/QPS、错误cluster参数拒绝、历史别名等价/警告、实验角色元数据与通用套件不注入四池。完整affinity功能测试22个短运行通过，包括已有cluster放置与非法池数拒绝。

另用目标六cluster CPU列表生成4组计划，确认每组保留6分区/cluster绑定/16线程/12QPS，没有注入四池；在开发机两个CPU人工分区上实际运行新入口的4个到达case，各8请求全部完成、0迁移，报告链路通过。人工分区运行不代表目标硬件cluster验证。修改文档的32个Shell代码块及本地链接也已检查。

这些验证证明配置没有被固定为四池，不证明ARM上哪种线程数、负载指标或迁移策略最优。目标机下一步从cluster校准和固定资源的对照继续；不需要为了完成本次纠偏重跑四池实验。

## 6. 最终贯通审查（2026-11-15）

再次沿设计、默认参数、建池与候选搜索、实验计划、终端分析和指南入口检查。核心没有固定4池或6池的路由分支；发现的剩余问题在入口表述、报告来源标签和实际测试覆盖，已针对修订：

| 环节 | 发现与修订 | 验收边界 |
|---|---|---|
| 当前测试入口 | README/TARGET_TESTING残留“先0～3”“接着旧s4-routing”的指令；改为已有阶段3结果直接进入cluster v2 | 旧数据和旧命令保留用途，不作为v2前置门槛 |
| 负载顺序 | 入口burst排列容易被当成必跑；明确可跳过，先固定regular研究单request扇出和L2 | burst仍是稳健性变量；不替代算子内部突发 |
| 数据亲和 | 计算基线初始分支depth=1没有普通后继；follow实验明确使用depth>=2并重新冻结工作 | 不把改变图深度或数据模型后的数值当同工作量策略收益 |
| 算法设计报告 | 补齐已实现的active/cpu及可注册provider/scorer；不把历史active20/80变为阈值 | active仍不是runnable，CPU慢反馈保留有效性/回退与成本要求 |
| 建池和实际路由 | 原3/6组主要覆盖计划生成；新增3/6逻辑组的真实callback执行测试 | 每组home可达、最后一组参与容量归一选择、非连续NUMA标签下本地优先与跨域开关；不声称模拟真实硬件cluster |
| 终端报告 | 原来只显示资源，未保留矩阵研究角色；新增实际pool_count、suite、experiment_role及来源 | 标签仅来自直接父矩阵且plan确认该run；缺失/旧元数据明确未分类，标签不是拓扑认证 |
| 性能配对 | 既有完整groups/affinity/threads配对条件阻止四池none与cluster主线混配；补拒绝混配回归 | 角色不替代资源/工作合同，也不阻断相同合同下baseline与signals的合法配对 |
| 第五阶段 | 把v1底层Hint/Decision契约与v2业务总池接口分开说明 | 业务不维护池编号；Folly侧仍是底层分组器，未宣称高层门面已迁入库 |

本轮没有改变调度算法、默认线程数或Folly代码。RequestExecutor新增3项测试，每项同时覆盖3/6逻辑组；该目标22项测试全部通过。报告回归复用22组短功能运行，验证资源不同不配对、角色可见、缺失元数据仍可分析、不同角色但同合同可以配对；矩阵计划检查同样通过。

后续仍应按实际拓扑验收绑定，使用相同工作和到达轨迹，在同一线程档内比较home与候选策略，再单独改变NUMA、延续方式或其他变量。跨线程档、跨绑定、跨图形是各自的实验因素，不能混入“只改负载均衡策略”的收益数字。

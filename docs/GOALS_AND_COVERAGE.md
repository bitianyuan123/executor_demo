# 总目标、实现难点与验收覆盖

本文是当前研究的总览。目标部署为**所选资源内，每个已核实的硬件cluster一个Folly子池**，上层业务只使用一个总池。当前96逻辑CPU子集包含六个cluster，因此使用六池；历史4×128线程、16 QPS、无CPU绑定仅是旧trace采集条件。

目标不是单独压低Folly队列长度或增加唤醒数，而是在相同工作与资源下，找到**请求尾时延、CPU/请求和数据亲和成本**之间可重复的取舍，并交付可复用的观测和调度组件。当前没有生产最优线程数、迁移阈值或算法的结论。

## 1. 要解决的问题与实现难点

| 难点 | 当前处理 | 还需要什么证据 |
|---|---|---|
| 局部微突发被全机平均CPU掩盖 | 区分每请求总工作、初始宽度、后续fork和请求重叠；按cluster配置容量 | 真实图分布、100ms是CPU还是墙钟、executor占比；不能令400个100ms纯CPU算子持续跑12QPS来强拟合 |
| 几百worker把等待移出Folly队列 | 保留Q基线，再比较I/O/work与后台CPU/可运行等待 | 同一线程档下的盲区和增益；Q=0或I=80均不直接定义忙闲 |
| 低成本观测仍可能干扰被测负载 | 评分读缓存；分开原子读、竞争RMW、后台扫描、全追踪和总池生命周期成本 | ARM多生产者、跨cluster读写和真实callback的整路径开销 |
| 任务借出降低等待，却破坏数据复用 | 固定计算/访问次数，用真实分支读写链比较parent/home与同/跨NUMA | 实际页位置、工作集与cache范围、CPU/请求及执行墙钟变化；不能仅用迁移计数证明cache miss |
| 无锁近似负载下并发选池会失效 | chunk预占、容量归一、同NUMA搜索、阈值/gain/hop | 选池与预占不是事务；没有严格最小负载、零惊群或公平性保证 |
| 总池隐藏池编号，但不能隐藏异步生命周期 | request上下文、分支独立parent、登记式continuation、部分批拒绝和drain | 真实RPC完成/超时/取消、Future/KeepAlive接入；普通add无法推断fork、多父数据归属和deadline |
| 因果对照与运行波动 | 固定逐请求工作/到达、同资源、交错seed、计划到达cohort、保留晚完成 | 足够尾部样本及留出复验；完整排空不等于稳态，相关性不等于迁移收益 |

## 2. 负载观测：先分清测量对象

| 信号 | 当前实现及获取成本 | 不能解释的事情 |
|---|---|---|
| Q | Folly原生`getTaskQueueSize()`，O(1)近似读，无新增统计写入 | 已领取的任务、内核runq、未来同一fork尚未提交的工作 |
| I | 总池callback计数；既有stock池可单独注册`FollyActiveLoadProvider`，出队/处理结束各一次relaxed RMW | 计数窗口不完全相同；均包含被抢占/同步阻塞，不等于onCPU或runnable |
| O | 总池已接纳未完成数；提交失败回滚，读取O(1) | 未就绪DAG节点、精确剩余CPU工作；不能将O再与I相加 |
| work | 未完成任务的完整估计预算之和，读O(1) | 不随任务执行递减；常数估计可能退化为O，不能把合成先验当生产预测器 |
| CPU慢反馈 | 后台按稳定TID集合读stat/schedstat，热路径读发布缓存；采集成本O(worker总数) | 50ms窗口不能可靠反映30ms内新突发；CPU占用仅是已获得服务，不能单独表示需求 |
| 调度trace | 独立诊断，按1/5/10/30ms观察waking、unique-onCPU、CPU及闭合等待 | 无request/算子标记时不能还原Folly队列、图关键路径或request p99 |

Q、I、O均是非事务快照；允许误差不意味着可以用普通共享整数制造C++数据竞争。CPU评分的无效、过期或worker全占用回退必须同时报告，不能把回退到O的结果解释为CPU信号收益。

`LoadProvider`与`LoadScorer`可替换，不依赖demo图或CSV。stock观察器可为既有池取样，**当前RequestExecutor仍自行创建子池，不是直接接管已有池的构造接口**。后台采样器也不是动态worker发现器。可抽取的头文件不等于已经完成全部生产接入验收。完整语义、计量成本及回退见[观测报告](LOAD_OBSERVATION_REVIEW.md)和[阶段2源码分析](../stages/02-folly/README.md)。

## 3. 两层均衡与线程亲和

```text
request进入总池
  L1：RR或颜色哈希确定固定home
  图运行时产生就绪算子/fork
  L2：偏好home或实际parent，必要时借出
      先同NUMA其他cluster，再按开关/罚项考虑远端
  目标cluster内Folly队列 → worker → 内核按CPU mask调度
```

- 业务通过同一组`add`接口提交；外部root显式带request，池内后继继承上下文，外部异步回调使用登记式token。不同分支不争用一个“request当前池”。
- 指标策略为home、queue、active、outstanding、work、cpu；候选搜索是参数化的同NUMA优先全扫描。没有实现LALB反馈式L1、P2C、工作窃取或内核调度器的复刻。
- `busy-high/min-gain/remote penalty`控制借出；`return-low`控制返回home。回迁受fork/hop/跨域许可限制，但不经过借出的gain/罚项：home仍低于low时，即使比parent略忙也可回迁。两种决策不能误用同一收益解释。
- `parent/home`决定后继偏好；`fork/all`决定所有fork点或每次就绪是否评估。v2的fork不是“仅初始fork”；v1另有initial/all对照。
- `batch-chunk`是一组任务共用一次选池与预占，每个任务仍独立入队。只放置新就绪任务，不迁走已排队/正在执行的任务。
- cluster绑定输入分区；numa和none作为保持同池数、同线程数、同CPU并集的控制组。容量不等于worker数量，SMT逻辑CPU数也不是已标定物理吞吐。

高层门面、数据上下文和参数细节见[两层设计](../stages/04-dag/TWO_LEVEL_DESIGN.md)。`--affinity cluster`检查CPU可用、不重叠和同NUMA，**不自动认证硬件cluster边界**；仍需核对topology与实际mask。扩大池数会增加扫描和采样成本，当前六池实验不能直接证明大规模扩展性。

## 4. Demo负载覆盖什么

| 变量 | 已实现的对照 | 必须避免的混淆 |
|---|---|---|
| 请求并发 | 开放式regular/Poisson；固定QPS、多request重叠，最大在途准入 | QPS不是并发数；拒绝不算完成，不能让发生器因压力自动降低计划QPS |
| 内部微突发 | 初始宽分支链、多波fork/join；20～400宽度可配置 | 宽度不是总节点数；同每算子工作增加宽度会增加需求 |
| 入口微突发 | 同批多个request、批内间距和平均QPS独立设置 | 可选稳健性变量，不代替单request内部扇出 |
| 算子异质性 | 固定seed的task/request抖动与长尾；常数/请求类别估计对照 | 长尾改变均值需重新校准；不同shape/分布不自动成为同工作策略配对 |
| 数据亲和 | none、request-ro、branch-rw、mixed；home初始化，链消费前驱修改的数据 | follow重点分支depth≥2；初始化位置不保证永久NUMA页归属，真实worker构图的first-touch可能不同 |
| CPU亲和 | 每cluster一池主线；相同CPU并集下none/numa控制 | none的逻辑迁移不是可证明的物理NUMA迁移 |
| 阻塞/异步 | v2同步sleep反例；v1模拟异步RPC；总池token生命周期单测 | sleep不是CPU工作或异步RPC；v2驱动未做真实网络压力 |

v2使用固定迭代/访问次数，使迁移变慢时仍完成同样工作；v1/阶段3的CPU预算模型用于排队机制，不拿它证明cache收益。当前图为波次分支链与标量join，不是任意生产DAG导入、多父大块数据融合或共享写争用模型。

## 5. 从负载关系走到策略结论

现有ARM阶段3日志已得到指定配置下的条件等待/完成分位数和queue=0反例，见[第五批审阅](TARGET_PILOT_05_DAG_BASELINE_REVIEW.md)。这些是独立task的条件关系；不能搬成DAG请求p99公式或跨配置阈值。无需重跑已完成阶段3。

阶段4依次做以下检查：

1. **home下重测关系**：在目标cluster部署固定线程数、图和工作，查看Q/I/O条件等待、callback墙钟及初始fork形态。线程数变化后重新分层，不混合所有配置回归。
2. **解释时序**：检查计划/实际投递、完成、积压和lag趋势；使用所有跨窗口区间，保留窗口后完成。首波callback占用不等于CPU饱和，30ms onCPU证据仍需调度trace或独立CPU测量。
3. **冻结候选做干预**：相同工作/到达/资源下比较home与候选策略；报告整request尾部、CPU/请求、执行墙钟和迁移。只比较被迁移与未迁移任务存在选择偏差。
4. **留出验证**：调参后用未参与选型的seed/批次及邻近压力、足迹复验。检查无借出、回退、持续过载等失败条件；结果范围不支持0.5%差异时如实保留不确定性。

当前分析工具的职责如下：

| 工具 | 数据与输出 | 推断边界 |
|---|---|---|
| `scripts/conditional_latency.py`、stage3 `predict.py` | 独立任务条件分位、整轮留出预测 | 不能直接读取v2 schema或预测其request关键路径 |
| `experiments/affinity/report.py` | 审计v2合同、请求mean/p95/p99、callback占用、同seed合法home差值及实验角色 | 每轮描述性配对；没有自动跨seed置信区间或生产最优排名 |
| `experiments/affinity/diagnose.py` | v2请求时序/积压、首波投递/启动/callback跨度、Q/I/O条件task分位 | 离线诊断；不是kernel runq测量、稳态认证或负载→request p99模型 |
| `experiments/affinity/flow_report.py` | 成功请求均值分解、完整burst实际跨度、init/普通后继等角色及选池原因 | 复用原CSV审计；attempt不等于root入队，路由回home与ReturnHome原因分别统计 |
| `scripts/compare_runs.py` | 阶段3/v1的声明式合同与整轮差值区间 | 不兼容v2 schema，不能把目录直接替换后声称已复用 |
| `scripts/analyze_sched_trace.py`及Perfetto SQL | 调度事件/线程状态与质量审计 | 没有业务marker，不输出request因果结论 |

v2的`submitted_ns`在**选池、预占和诊断读取之后，底层pool.add之前**；task等待不含完整L2选池耗时。request计划到达→完成和整进程CPU保留这些总体成本。条件分桶采用投递任务的采样，队列值来自已选目标池，带有选池及批内顺序偏差，不是时间加权负载分布。CSV没有完整保存每个候选与预占状态，不能精确重演所有选池决策。正式策略结论仍以相同合同的整轮干预为单位。

诊断的三个样本口径分别是：时间趋势纳入所有穿过墙钟窗口的请求/callback；首fork选择计划到达落在窗口内的request并保留其完整首波；Q/I/O条件分桶选择记录提交点在窗口内的operator并保留其后续等待/执行。这些总体不同，不能将条件桶和`report.py`按request cohort汇总的算子指标直接混加。

## 6. 设计—工程—验证覆盖

| 交付 | 工程/指南 | 已有验证 | 待目标环境或后续实现 |
|---|---|---|---|
| 业务模型 | [阶段1](../stages/01-business/README.md) | 量纲检查、假设与未知项分离 | 图分布、CPU时长口径、数据所有权 |
| Folly调用链/成本 | [阶段2](../stages/02-folly/README.md)、metrics | 固定版本源码核对、读/维护探针 | 目标业务版本核对、ARM整路径成本 |
| 独立负载关系 | [阶段3](../stages/03-independent/README.md) | 本地测试、ARM终端结果及条件分析 | 新配置的外推边界，不重新冒充未知 |
| 总池/L2/局部性 | [affinity工程](../experiments/affinity/README.md) | 生命周期、拒绝、3/6逻辑组、负载回退、数据依赖和固定工作合同 | ARM收益及阈值，真实共享写/大join/异步压力 |
| 观测适配器 | [观测报告](LOAD_OBSERVATION_REVIEW.md) | stock观察器异常/阻塞/过期、provider析构后继续回调；fake proc差分、失效及评分 | 动态worker、多优先级等真实接入路径；CPU有效样本的目标内核成本 |
| 结果分析 | 本文第5节及[cluster指南](REAL_WORKLOAD_MATRIX.md) | 合同拒配、时间/分桶边界合成测试 | v2自动多轮区间未实现；目标机足量重复与独立确认仍需执行 |
| 库归属 | [阶段5](../stages/05-placement/README.md) | v1底层业务/Folly两位置同算法验证 | 高层RequestExecutor未迁入Folly；Future/KeepAlive及生产关闭协议 |

历史验证记录保留当时测试数与配置，不能把旧四池功能运行或人工CPU分区描述为目标六硬件cluster性能实验。最新修订与验证事实见[方向复核](CLUSTER_DIRECTION_REVIEW.md)及对应提交测试记录。

## 7. 当前执行入口

[4001](TARGET_V2_4001_REVIEW.md)至[4004](TARGET_V2_4004_REVIEW.md)完成初筛、竞争与尺度校准；[4006](TARGET_V2_4006_REVIEW.md)完成24轮后继策略，[4008](TARGET_V2_4008_REVIEW.md)完成task异质性，[4009](TARGET_V2_4009_REVIEW.md)完成联合诊断与burst8。regular与burst下策略收益不同，不能自动外推生产；这些轮次不再列为待办。

4010有界补证与4011的18轮观测对照均已完成，见[阶段结论](TARGET_V2_4010_4011_REVIEW.md)。旧trace缺少的业务字段保持unknown，不继续以扫描作为工程前置。外部新增状态解析存在缺陷，0.85ms与3ms不能当已标定参数。

4012已完成[24轮信号/线程对照](TARGET_V2_4012_REVIEW.md)：16线程的借出策略均值比128线程低2.93%～4.03%，Q/active/O间未分出稳定赢家，p99也未证明全面改善。[4013](TARGET_V2_4013_REVIEW.md)已完成六轮机制诊断，当前入口为[9轮regular守护与覆盖计划](TARGET_V2_COVERAGE_NEXT.md)，不再重复原初筛或扩阈值；[异步恢复设计](ASYNC_CONTINUATION_WORKLOAD_DESIGN.md)明确已知结构性缺口与后续功能验收。模型仍保持固定工作与依赖，不为拟合旧四池改变负载。

真实业务接入另按[分阶段生产agent手册](PRODUCTION_AGENT_PLAYBOOK.md)推进；若只能复制文字，使用[纯prompt交接版](PRODUCTION_AGENT_PROMPTS_ONLY.md)。当地agent先确认源码/授权/资源，再设计、实现、验收、有限迭代。给候选初值与有界梯度，不直接指定生产阈值；不要求环境网络直通。阶段5继续负责组件归属与Folly最小扩展的必要性，当前无需修改Folly核心。

## 8. 本次覆盖审查的验证事实

2026-11-15在开发机完成affinity独立CTest **8/8**，包括23项RequestExecutor、4项stock观察器测试、新增7项诊断合成测试、22组短功能运行及其余既有局部性/评分/采样/成本检查。新增回归覆盖回迁不经过借出gain/罚项、provider析构后继续回调、正常/异常过期回调，以及诊断的跨窗区间、首fork识别、缺失字段和终态审计。

新诊断还读取了4个已有完整短运行目录，确认真实schema、元数据角色和终端报告链路；没有为此新增压力实验。修改后的文档命令语法与本地链接已检查，并修正96请求/12QPS初筛却使用超过投递期窗口的示例。新增分析只读已有结果，不增加业务热路径插桩；本轮没有修改调度算法、Folly实现或默认负载。

这些是功能和分析正确性验证；没有新增ARM性能数据、真实业务trace结论或生产策略优劣结论。

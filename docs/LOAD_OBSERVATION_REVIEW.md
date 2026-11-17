# cluster池过配场景：负载观测与两层调度复核

目标为每硬件cluster一个Folly子池，当前选定六cluster对应六池；负载观测和算法按实际池数复用。已有轨迹的临时采集条件为16 QPS、4个CPU池、每池约128线程、只有request→池亲和，没有池→CPU亲和；**单池**“活跃”均值约20、峰值约80，活跃定义仍待核实。不能把这组数据当作“绑定16个逻辑CPU后仍有相同运行行为”的事实，也不能将四个池各自的峰值直接相加。原始trace留在测试机，已收到[4005终端证据并完成审阅](TRACE_4005_REVIEW.md)；下文设计仍需目标配置验收，不是生产最优结论。历史四池不决定实验主线，修订见[方向复核](CLUSTER_DIRECTION_REVIEW.md)。

## 1. 先统一名词，再判断哪些信息足够

| 指标 | 准确定义 | 主要盲区 |
|---|---|---|
| Q：native queue | Folly队列接口报告的未消费permit近似数 | 非物理队列精确长度；不含已领取/执行的工作；经典LifoSem还有handoff窗口 |
| I：callback inflight | 进入计数回调但尚未返回的任务数 | 包括运行、被抢占和同步阻塞；不是瞬时runnable |
| O：outstanding | 包装器已预留/接纳而未完成的任务数，失败提交需回滚 | 不区分任务成本/状态；不是包括未就绪DAG节点的整请求剩余工作 |
| unique-onCPU | 指定时间窗内至少有一段已观察到onCPU时间的不同TID数 | 与窗口大小强相关；80个轮流执行不等于同时运行80个 |
| waking/wakeup count | 两种不同调度事件的条数，按目标TID归属池 | 不是不同线程数、在运行线程数或算子数；一次算子可能多次睡眠/恢复 |
| onCPU等价核数 | 同窗池内线程运行时间之和 / 墙钟时间 | 只表示获得的服务，不含未获得CPU的需求；SMT CPU秒不等于物理核吞吐 |
| runqueue-wait等价线程数 | 同窗各线程可运行等待时间之和 / 墙钟时间 | 依赖内核计账启用；计数更新边界和采样时间不完全同步 |

Folly固定版本的`numActiveThreads()`是线程生命周期计数，不能用来取得I。`getPoolStatsLight().activeThreadCount`更接近I，但仍持共享锁扫描所有worker，而且runTask之前的唤醒/领取阶段存在盲区。完整源码流程与两种信号量的区别仍以[阶段2报告](../stages/02-folly/README.md)为准。

观察到“active、waking、CPU强相关”可以支持**在已测负载区间**使用廉价active代理的假设，不能单独证明普遍成立。若active本来由waking事件集合计算，两者甚至不是独立证据。应在相同窗口同时保留事件条数、unique-onCPU、onCPU时间和闭合等待区间，并分别看含空窗/非空窗结果。同步阻塞、长短任务混合、CPU配额/外部竞争、线程数变化是需要保留的反例。

4005的最终精确映射复算进一步收窄了这个假设：1ms窗口unique-onCPU→onCPU的r约0.926～0.933，但waking→onCPU仅0.501～0.546；前两者还共享运行区间作为输入。没有Folly callback开始/结束记录，不能声称已证明I与onCPU同样相关。保留Q/I/O和慢CPU后端，不把trace窗口unique作为新增生产热路径计数，也不由历史峰约80直接设定cluster阈值。

## 2. 生产可复用的观测边界与成本

本轮增加的头文件位于`experiments/affinity/include/executor_demo/`，不依赖demo随机图、任务标签或CSV。它们依赖已锁定的原版Folly接口，无需修改Folly默认执行器。

| 实现/方法 | 热路径读取 | 新增维护成本 | 适用范围及限制 |
|---|---|---|---|
| 原生`getTaskQueueSize()` | O(1)，现成原子状态读取/位操作 | 无新增统计写入 | 最廉价基线；过配线程把等待转移到内核时单用不够 |
| 总池的O/I/work计数 | 少量relaxed原子读 | 各计数更新、提交失败补偿、生命周期管理；共享缓存行可能竞争 | 当前总池原有计数；O覆盖回调接力窗口，I可解释阻塞 |
| `FollyActiveLoadProvider` | 原生Q + 一个I原子读；workerCount读取配置上限 | `TaskObserver::taskDequeued/Processed`各一次relaxed RMW，共2次/任务，另有observer调用成本 | 给既有stock CPU池注册；包含过期处理和异常；不伪造O/work |
| `getPoolStatsLight()` | O(线程数)扫描+共享锁 | Folly本来维护idle标记 | 低频监控/对照，不用于每次L2路由 |
| `LinuxSchedstatLoadProvider::read()` | O(1)，读取缓存发布槽，版本变化则返回无效 | **后台**每轮每TID读取stat+ schedstat；不是免费，O(总线程数)文件操作 | 注册稳定TID集合；50ms默认只是慢反馈试验值 |
| 每算子CPU时钟和完整trace | 需开始/结束时钟、写追踪行 | demo trace=1会增加实际任务路径成本 | 校准/解释时延；trace=0用于扰动对照，非生产默认建议 |

“允许读数有误差”不意味着允许C++数据竞争：多线程并发读写普通整数仍是未定义行为。`atomic.load(relaxed)`与`fetch_add`的缓存一致性成本不同，不能把它们一律称为昂贵原子操作。若共享RMW成为瓶颈，可以增加分片实现，代价是O(分片数)读或后台汇总及其陈旧性；本轮没有未经实测就把分片宣称为更优。

stock适配器须在**停止准入且尚无任务开始**时安装，每池生命周期只安装一次。构造时检查不是排空屏障；不能在有任务运行时临时附加，否则可能只见结束事件而漏掉开始。池必须活到最后一次provider读取结束；Folly拥有observer，不能通过销毁provider撤销计数成本。它利用正常TaskObserver回调，独立测试也使用原版CPUThreadPoolExecutor，不依赖BusinessExecutor。

Linux provider的set/register/start/sampleOnce/stop由单一管理线程串行调用，`read`可并发。注册必须来自真实worker TID（线程启动工厂/observer或一次低频线程注册快照），不能每次add遍历线程。首次有效采样固定TID的birth；之后TID复用永久使对应组无效，扩缩容/替换worker应重建provider。它不能证明首次采样之前的TID没有被复用。任何worker缺失、计数倒退、未建立差分或发布冲突均不解释为空闲。

后台采样读取`/proc/<pid>/task/<tid>/schedstat`的运行与等待增量，核对`sched_schedstats`启用状态；未启用时仍可观测CPU，但不能把等待零值当真实零。路由不做proc I/O，不等待采样线程。多个TID顺序扫描，因此是近似同窗历史观测；长扫描与计数更新延迟会影响精度。输出总扫描CPU/墙钟、最大单次扫描时间及失败组数；初始建基线也计入失败组。若一次扫描接近采样间隔，先降低频率/评估其他采集后端，不把陈旧缓存当实时状态。[Linux计账定义](https://docs.kernel.org/scheduler/sched-stats.html)

## 3. 观测、评分、路由分别负责什么

```text
业务 beginRequest(color) → 一次确定request home
业务 add/addBatch/continuation → RequestExecutor
  LoadProvider::read(group)       只读已发布信号，含有效性/时间窗
  LoadScorer::score(sample,...)   把信号变为有明确单位的score
  路由规则                        high/low、NUMA、gain、hop、fork、chunk
  BusinessExecutor → stock Folly CPUThreadPoolExecutor
```

`Config.loadProvider`、`Config.loadScorer`支持注册实现类。provider只提供CPU样本也可以，缺失字段保留本总池的即时计数；CPU策略回退始终使用本总池当前O，避免错误使用外部历史O。自定义scorer需声明所需输入、单位、无效值处理，不能在`score/read`中扫描线程、锁等待、分配内存或读proc。现有几种评分只是可对比假设，没有通用场景最优的宣称。

| CLI policy | 评分（C为配置容量，R为批内待提交预占） | 解释 |
|---|---|---|
| home | 不评估L2 | 请求home基线 |
| queue | `(Q+R)/C` | 观察队列单指标的盲区 |
| active | `(Q+I+R)/C` | 廉价占用代理；不再额外加O，以免重复计量 |
| outstanding | `(O+R)/C` | 默认优先验证的快路径补充，包含更多生命周期窗口 |
| work | `(估计未完成CPU us+预占估计)/C` | 依赖估计质量；仍保留整份任务估计，不是精确剩余工作 |
| cpu | `max(Q+R, 历史CPU等价数+历史runqueue-wait等价数, 可选runnable快照)/C` | 近似历史需求；此proc实现不提供瞬时runnable，不能把可选接口误写成已采集 |

cpu样本缺失、过旧、非有限数或runqueue统计不可用时回退O。即使CPU很低，若I达到配置worker数量，也以`worker_saturated`回退O，避免将全阻塞池看成空闲；**是否借出仍受high阈值控制**，不是强制拒绝该池。80/128不是全worker占满，不触发这条特殊退路。半数阻塞、采样滞后、任务异质仍可能导致错误排序，必须测反例。

active和outstanding很可能在CPU密集长任务时近似，这正是有意义的简化对照。CPU模型不能保证感知新一轮微突发：当很多worker很快取走工作，Q归零而历史CPU尚未上升，它也可能低估；因此保留O基线，不能因CPU“更底层”就预设更准确。

`tasks.csv`保留选池样本/score、样本窗口、是否评估、回退原因。summary的`score_evaluations`按**携带评分结果的任务**计数，batch chunk内多个任务可共用一次选池；它不是虚函数调用次数。pin-home/home/non-fork跳过评分的任务不进入回退分母。trace=0没有逐任务回退证据，应结合trace=1诊断轮解释，不能据零计数认定没有回退。任务等待起点位于选池、预占及诊断计数读取之后、底层提交之前，终点为包装闭包开始；它包含后续提交、排队及调度等待，**不包含完整L2选池成本**，不能用它替代整条`add`路径的开销测量。

[4008的既有CSV诊断](TARGET_V2_4008_REVIEW.md#3-4008前半q有信息但任何单个计数都未验收)说明Q有描述性信息、I与等待不稳定单调，不能预先淘汰某信号。[4009](TARGET_V2_4009_REVIEW.md)联合桶确认Q为零而I高与I不高却有积压两类样本均存在；尚未证明某个信号生产最优。4010有界补证、4011观测开关和[4012信号/线程对照](TARGET_V2_4012_REVIEW.md)均已完成。4012中Q在128线程下仍有收益，Q/active/O未分出稳定赢家；不能把过配时Q有盲区改写成Q必然无用。[4013](TARGET_V2_4013_REVIEW.md)六轮机制诊断显示128档Q零且I/O高仅占0.310%～0.487%的提交样本，不能证明盲区主导尾部；16档大量Q非零但I不超过容量。当前按[覆盖计划](TARGET_V2_COVERAGE_NEXT.md)做9轮regular守护，未知业务量仍是敏感性假设，不要求生产访问。`diagnose.py --joint-load`可离线检查同一行的Q/I/O联合条件；没有新增热路径探针，仍受选池后采样、成本混合和非原子快照限制。[持续压力门控](PERSISTENT_PRESSURE_REVIEW.md)是独立于Provider/Scorer的候选设计，目前没有实现、没有将3ms设为默认；`return-low`也不是门控释放阈值。

## 4. 已保留的调度开关与未实现的能力

L1仍是RR/颜色哈希确定一次home。L2只决定**新就绪算子**投向哪里，不能抢占或搬运已排队任务。先选当前偏好池；ReturnHome可先按low阈值回home。其余情况下超high后扫描同NUMA候选，满足min-gain才借出；同域没有合适候选且允许跨NUMA时，才加remote罚项搜索远端。此处“空闲”是低score的相对判断，不承诺找到没有任务的池。

| 参数 | 作用及边界 |
|---|---|
| `--busy-high / --return-low` | 借出与回home阈值，单位依policy；不是从80峰值直接推导 |
| `--min-gain` | 普通借出候选必须有足够评分改善；不约束ReturnHome回迁 |
| `--cross-numa 0/1 / --cross-numa-penalty` | 普通借出先同NUMA，再按开关和罚项跨域；ReturnHome遵守跨域开关，但不加该罚项 |
| `--continuation parent/home` | 跟随实际父位置，或home评分不高于low时回迁；回迁遵守fork/hop/跨域开关，不比较min-gain或remote罚项 |
| `--balance-at fork/all` | 仅fork批量点，或每次就绪都重新考虑 |
| `--batch-chunk 1/4/8` | 每次选池预占一组；仍逐任务入队执行，不把任务合成串行大闭包 |
| `--max-hops` | 沿上下文路径的迁移预算，0禁止借出；不是最短驻留时间 |
| `--cpu-sample-ms / --cpu-max-age-ms / --cpu-sampling` | 采样频率、陈旧阈值与独立开销对照 |

ReturnHome是单独的亲和策略：即使parent评分为0、home为1，只要low允许且上述限制满足，也可回home。它不要求home比parent更空闲，不用`cross-numa-penalty`证明回迁划算；因此提高min-gain或remote罚项不能替代对回迁策略本身的对照。

当前六cluster子集采用全扫描候选，便于审计；扫描成本随池数增长，扩展至更多cluster需重新测量，而不是假定池数永远为4或6。因此本轮仍未引入P2C或复杂负载树。各并发提交者选择与预占不是事务，仍可能同时选中同池；chunk/保守gain是可测参数，不是严格避免惊群证明。借鉴内核的分层搜索、容量归一和迁移抑制，不照搬内核runqueue或任务抢占机制；LALB面向服务调用/反馈延迟的权重也不能直接当作本地CPU算子可用容量。原有[算法依据与二层设计](../stages/04-dag/TWO_LEVEL_DESIGN.md)继续适用。

none模式所有池共享同一CPU集合，NUMA标签为未知；逻辑home不意味着物理缓存/内存归属。同NUMA优先只有固定cluster/numa部署时才有稳定意义。4池共享96逻辑CPU时，默认每池C=24的名义份额（再受线程数/scale限制），不是每池独占24 CPU，更不是测得物理吞吐。目标六cluster池使用本分区容量；none/numa控制组保持六池及相同线程数。四池none仅为可选历史重放，不能混入单变量亲和或策略对照。

## 5. 负载覆盖、测试顺序与第五阶段

[cluster负载矩阵](REAL_WORKLOAD_MATRIX.md)给出主线命令：每cluster一池下校准单request扇出，扫描线程过配、QPS、regular/Poisson/入口微突发与request/task异质性，并保留同步阻塞反例。原有v1保留异步RPC；v2保留request只读、branch真实读写依赖、mixed足迹、初始fork和后续fork/链。当前是参数化波次图，不是生产任意DAG回放；没有请求标记的调度轨迹不能自动还原20～400算子的图形或请求p99。

按顺序验证：

1. 目标部署核对真实cluster分区、每池CPU mask与NUMA；旧trace的四池映射与active定义可并行核实，不作为cluster实验的前置条件。
2. 每cluster一池、home固定路由下校准CPU/请求、初始尖峰和请求重叠，再冻结迭代/访存工作；分别看Q、I、O，避免改策略同时改需求。
3. 相同工作和到达轨迹比较queue/active/O/cpu/work，检查确实发生决策、迁移以及CPU信息有效覆盖。低压力下都无借出不能判算法等价。
4. 对候选策略单变量扫线程数、QPS、扇出形状、足迹、NUMA、parent/home、fork/all、chunk与阈值，找收益转折点。工作量不同的形状对照明确标注，不自动混为等需求。
5. 追踪、采样、observer与总池封装分别评估扰动；正式p99使用足够请求、多个交错seed/整轮重复及完整排空，不用每秒p99的平均。

第五阶段仍有意义：它决定经过验证的策略/上下文/lifecycle应留在应用通用组件、独立Folly适配层还是上游库。**当前先实现高层总池及原版Folly适配器**，业务只持有一个对象；不需要为了证明这一接口强行修改Folly核心。未来只有稳定需求、明确统计成本及Future/KeepAlive/取消语义都验证后，再讨论公共库API。既有实验Folly分支保留，本轮未改默认CPU池或声称可直接替换所有业务Executor接口。

## 6. 在目标机测观测成本

```bash
BUILD_JOBS=1 ./scripts/build_stage.sh affinity
build-affinity/observation_cost_bench --threads 16 --iterations 2000 --scans 10
build-affinity/observation_cost_bench --threads 128 --iterations 2000 --scans 10
# 六cluster各128线程时共768 TID；下例仅探测同数量TID扫描成本。
# 单池探针不等同于六个绑核池；实际分组采样成本仍用home采样0/1测。
build-affinity/observation_cost_bench --threads 768 --iterations 2000 --scans 10
```

该探针输出native queue、stats扫描、注册observer的缓存读取/评分、后台采样缓存读取和proc全扫描。是空闲线程、热缓存、批量平均；p99列是批平均的p99，不是每次读取p99。不能据其数值推导高并发跨NUMA的RMW开销。后台全扫描CPU毫秒/实际采样周期毫秒约为占用一个逻辑CPU的份额，需同worker数量在目标ARM实测。

完整维护/应用扰动继续用[阶段2探针](../experiments/metrics/README.md)和v2 `overhead`、`home --cpu-sampling 0/1`配对；stock observer的端到端接入还应在真实回调和取消路径上验收。已执行验证与本地读成本见[本轮记录](../results/review/TRACE_LOAD_VALIDATION.md)，不得外推为目标机数字。

# 第四阶段 v2：请求亲和与算子溢出的两层调度

本轮工作的目标是验证：在图开始时高度扇出、后续仍有并行且存在数据复用的条件下，如何用一个总池封装保留数据亲和，同时在局部资源不足时借用其他池。业务创建请求上下文并提交就绪任务，不管理各个 Folly 子池，也不自行选择池编号。

池的物理边界按用户确认的改造方向固定为**一个硬件cluster一个池**，六池仅是当前选定六cluster子集的实例。4池无CPU绑定属于旧trace的临时配置，不是另一条生产改造路线；主实验安排与纠偏见[cluster矩阵](../../docs/REAL_WORKLOAD_MATRIX.md)及[方向复核](../../docs/CLUSTER_DIRECTION_REVIEW.md)。

这是新增的第四阶段实验。原有 `experiments/dag` 和已经完成的 v1 数据继续保留；v1 的独立任务、图依赖、亲和范围和路由结果不会因为新增模型而失效，也不能直接与 v2 的原始时延拼接。v2 使用 `experiments/affinity`，加入真实内存访问、固定工作量及 `RequestExecutor` 门面。具体构建、功能测试和运行参数以该工程的 [README](../../experiments/affinity/README.md) 为准，本文不另造尚未验证的命令。

## 1. 回顾：已回答的问题和仍缺失的问题

真实业务的两层分工应保持清楚：

1. 请求进入 executor 时选择 request 的 home。已有业务用 round-robin；同一请求的图运算初始具有共同亲和范围。
2. 每次产生可执行的子图或算子时，再决定留在亲和位置还是借池。只有依赖已满足的任务进入这个决策，未就绪节点不是可分派负载。

先前实验支持“较窄 CPU 范围可能限制突发并行度”，也显示队列深度相同而 CPU 范围不同时，等待分布可以差异很大。但原有缩放基线中的 request 重叠不强，缺乏能复用的真实数据，不能据此选定生产中的亲和收益、回迁阈值或最优均衡算法。

v1 中明确传递 home、parent 和估计工作量的 `BusinessExecutor` 已能做池间放置和记账。它仍把池编号和部分调度提示暴露给调用者；也没有把 request 生命周期、外部异步 continuation 和“不让业务挑池”的接口完整收敛。v2 在它上面增加门面，保留底层 Folly CPU 池和现有记账，不改 Folly 的任务领取或内核线程调度。

必须区分三种操作：

| 操作 | 当前 v2 |
|---|---|
| 就绪任务尚未入队时选择目标池 | 支持，是本轮二级均衡 |
| 从另一个池已入队的任务中取出任务执行 | 不支持，不称为 work stealing |
| 迁走已经运行的 callback，或抢占其计算 | 不支持；线程如何上 CPU 仍由内核决定 |

“初始16个CPU占满30ms”是生产观测，不是可以由 QPS、算子个数直接推算出的所有实验都必须精确复现的常量。应先复现前置高扇出和资源饱和形态，再用目标机轨迹检查突发持续时间。

## 2. 门面与上下文：同一个总池，两种提交边界

实现见 [RequestExecutor.h](../../experiments/affinity/include/executor_demo/RequestExecutor.h)。门面内部创建各组 `BusinessExecutor`；其底层放置策略固定为 Home，实际二级选池由门面完成，避免两个独立均衡器重复选池。

```cpp
auto request = executor.beginRequest(requestColor);  // 一级：确定 home
executor.add(request, [&] {
  // 当前 callback 已安装这个请求和本分支的执行上下文。
  executor.add([] { /* 同一请求的就绪后继 */ });  // 二级：自动继承
});
```

两次都使用 `add`，但含义不能依靠猜测：外部根任务显式带 `Request`，池内后继使用当前上下文。不带上下文的池外 `add` 明确失败；外部 RPC 完成线程不会天然拥有原线程的 TLS。

`captureContinuation()`捕获一次性、可移动的 token，保存分支上下文和 Folly `RequestContext`。RPC完成方用 `resume()`提交后继，取消方用 `cancel()`或销毁 token 释放占用。token不是RPC实现本身；v2数据计算demo不因有此接口就等于覆盖了真实网络行为。

`Request`包含固定颜色和 home。每个执行分支携带独立的 source 与 hops，而不是所有分支争用一个可变“request当前位置”。子任务继承的是其实际执行parent的位置；不同分支的迁移互不覆盖。`TaskOptions`提供诊断tag、CPU估计和显式 `pinHome`，不要求业务填池编号。

生命周期边界如下：

- `join()`停止新的request和外部根提交，继续允许已接受callback及已登记continuation产生后继，全部清空后才关闭底层池。
- 复制一个普通request handle不会无限延长drain；登记的continuation会。RPC的完成、超时、取消路径必须确保token最终被处理。
- `add`可能拒绝；`addBatch`返回接受的前缀长度，调用方必须把未提交后缀对应的依赖显式取消或完成。部分提交不是全有或全无事务。
- 不允许在自己的callback中join；门面对象必须比正在调用其成员的线程及continuation存活更久。

这些属于可复用执行器契约，不能隐藏成“调用一个函数就一定能执行”。业务无需感知负载均衡，但仍必须遵守异步任务的错误和生命周期约定。

## 3. 数据亲和：用实际工作建立可检验的收益

实现见 [LocalityWorkload.h](../../experiments/affinity/include/executor_demo/LocalityWorkload.h)。固定工作量包括计算迭代数、数据访问次数、分支与stage、种子；不能在每个目标池上持续运行到相同CPU毫秒后停止。后者会把较差的数据位置变成较少的有效工作，削弱对cache和NUMA代价的比较。

| 模型 | 数据和依赖 | 能检验什么 |
|---|---|---|
| None | 无大块payload；保留分支标量状态及计算迭代 | 计算与排队基线；不是绝对没有缓存访问 |
| RequestReadOnly | 一个request的分支读同一只读buffer | 共享数据的复用、缓存容量及远端读代价 |
| BranchReadWrite | 每个分支独占buffer，后继读取并修改前驱的数据 | 跟随parent与回home对连续数据复用的影响 |
| Mixed | request只读共享区加分支独占读写区 | 两种亲和倾向的竞争 |

分支读写区彼此分离、按页排布；标量状态做保守隔离。模型有意避免多个并行分支同时写同一cache line，因为那会把数据争用、锁和正确性机制混入本轮调度问题。它没有覆盖所有生产中的共享写和coherence流量，后续需要时再独立加入。

同一分支按stage顺序执行，其后继消费前驱标量与变更后的buffer；最终join按逻辑分支顺序读取最终标量并形成checksum。**当前join不是读取所有大块分支输出的多输入融合算子**，也不是任意DAG生成器。不能仅凭标量join结果推导生产中多输入算子的最佳亲和位置。

四种模型之间的工作不相同。主要因果对照应是同一模型、同一工作清单下改变策略；跨模型用于观察策略收益如何随内存特征变化。不能把Mixed比None慢的全部差额叫“迁移成本”。

### 内存位置与足迹

payload采用匿名映射，初始化时才实际写页。初始化任务固定在request home，建立可记录的first-touch基线；这不是 `mbind`，不是保证页面永久留在home NUMA。对象、容器和少量分支元数据的分配也不等于全部在home first-touch。

可用 `move_pages`查询模式抽样页面位置；不移动页面。无权限、接口不可用或部分查询失败时必须输出unknown/partial，不能把“初始化在哪运行”代替已验证的物理页位置。自动NUMA平衡、透明大页和内存策略应记入运行环境，且配对实验保持一致。

单request的主要足迹约为：

\[
M_r=\operatorname{pageRound}(S_{shared})+
B\operatorname{pageRound}(S_{branch})+B\operatorname{sizeof}(BranchState)
\]

总峰值还取决于同时存活的request数、任务对象、指标记录和allocator开销。实现中的预算包含页取整payload和分支状态，不是进程RSS总上限。压力增大导致request驻留更多时，总工作集也会增加，不能把由此引起的cache容量变化误解成单纯的队列效应。

建议按目标机实测cache共享范围设置“小于共享cache、接近共享cache、明显超出”的足迹档位，同时记录所有并行request的合计驻留工作集。不能只依据CPU编号或cluster名称假定某一级cache恰好由16个逻辑CPU共享。

## 4. 负载信号：语义、单位、成本和盲区

设组的有效计算容量为 `C`。它是对当前计算/内存模式测量得到的CPU等效容量或明确说明的初始近似，不是worker线程数量；16个逻辑CPU也不保证等于16个独立物理核的吞吐。

| 策略 | 当前评分 | 读取及维护 | 局限 |
|---|---|---|---|
| QueueSpill | `(Folly queue guess + 未入队预占数) / C` | `readQueueDepth()`读原有队列；另读预占计数 | 看不到已被领取的任务和运行中的callback |
| ActiveSpill | `(Q + callback inflight + 未入队预占数) / C` | 原生Q和已有callback计数；可注册stock Folly observer适配器 | inflight包含抢占与同步阻塞，不是kernel runnable |
| OutstandingSpill | `(outstanding + 未入队预占数) / C` | `readLoad()`读现有relaxed计数和队列；任务完成释放outstanding | 长短任务等权；RPC睡眠/阻塞可能被当作CPU压力 |
| WorkSpill | `(估计未完成CPU微秒 + 预占CPU微秒) / C` | 读取已有工作量计数；每任务提供估计 | 估计保留到完成而不扣除已执行部分；估计误差和长尾会影响选择 |
| CpuSpill | `max(Q+预占数, 历史CPU等价数+历史runq等待等价数, 可选runnable快照) / C` | 后台按TID采样，热路径只读已发布缓存 | 慢反馈会遗漏短突发；当前proc实现不提供瞬时runnable，缺失/过期回退outstanding |

Q/active/outstanding使用计数除以容量，work使用估计CPU微秒除以容量；cpu混合当前队列与历史平均需求。**它们都不是已校准的精确等待时长，也不能假定数值相同就有相同预测能力。** `busyHigh`、`returnLow`、`minGain`、`crossNumaPenalty`跟随当前评分单位，不能把Q策略的阈值原封不动交给Work策略。可替换的`LoadProvider/LoadScorer`、有效性回退与计量成本详见[观测复核](../../docs/LOAD_OBSERVATION_REVIEW.md)。

当前驱动明确区分实际工作变动和调度器已知的信息：

- `--work-scale-scope task`默认按算子生成固定的jitter/长尾倍率；`request`让同一request的算子使用同一倍率，模拟不同成本的请求类型。
- `--estimate-mode constant`默认给各operator同一估计，不知道其实际长尾。在这个模式下，WorkSpill近似为OutstandingSpill乘常数，主要差异来自join等特殊任务的估计，不能据此声称已经识别长短算子。
- `--estimate-mode request-scale`只允许与 `work-scale-scope=request`配合，operator和批次估计按同一request倍率缩放，模拟已知请求类型成本。它不是在线CPU预测器，也不是准确的剩余时间预测。

比较估计器时，固定 `work-scale-scope=request`及完整工作清单，仅切换constant/request-scale；改变scale scope会改变任务分布，不能冒充只改算法。真实生产的类型估计误差、反馈更新及异质批次估计仍需另外验证。

`readLoad()`不取worker TID，不锁诊断列表，不分配；字段是非事务的近似快照。`snapshots()`仍会收集TID、锁列表和分配，仅用于诊断。`readQueueDepth()`避免读取其他统计，但当前包装的提交、生命周期和底层已有计数仍会执行原子操作；Q-only评分不等于整个执行器零计数开销。

已完成的指标热读微基准说明现有队列快照可作为低成本候选，不足以代表多生产者、跨NUMA写入和整条调度路径成本。应同时测量观察器关闭/开启、路由决策耗时、整请求CPU与吞吐。原子load与竞争中的fetch_add不是同一成本；允许指标不精确也不允许用存在C++数据竞争的普通变量替代共享原子。

尤其在每池几百worker、仅16个逻辑CPU时，需要同时区分：

- Folly任务队列：尚未被worker领取的任务近似数量；不是内核runq。
- outstanding：已接受而未完成任务；覆盖排队、被领取、callback执行等阶段。
- inflight：callback已经进入；可以正在CPU上、被抢占、阻塞或休眠，不等于实际占用CPU数。
- 内核runq/调度等待：可运行线程的等待，由调度追踪或相应内核统计解释，不在每次选池时遍历 `/proc`。

因此，queue为0只能说明看不到该层队列积压，不能据此称一个池“空闲”。二级选择找到的是评分较低且满足迁移条件的候选池，不是证明它的CPU完全idle。

## 5. 二级策略：先保留亲和，再判断溢出

当前门面提供 `Home / QueueSpill / ActiveSpill / OutstandingSpill / WorkSpill / CpuSpill`；Home是必要的基线。一级提供RoundRobin和ColorHash，尚未实现LALB式完成反馈。先固定一级RR，单独评价二级，避免两个控制器同时变化。

`Config.balanceOnlyAtFork`控制决策发生的位置。开启时，仅 `TaskOptions.isFork` 标识的提交可借池或考虑回home，普通后继继续沿实际source执行；`addBatch`自动将各成员标记为fork。当前驱动将每一波的分支启动作为fork，因此这表示“各波扇出时决策”，不等于“仅request最开始那一次扇出”。若要专门研究仅初始扇出，应另设明确定义的模式，不能把现有fork模式误读为initial-only。单任务 `add`也可显式标记fork，但业务图运行时应统一负责此语义。

当前二级流程是：

1. 根任务以home为preferred；有parent的后继以实际source为preferred。
2. `FollowParent`继续以source为优先位置；`ReturnHome`在home评分不高于 `returnLow` 时尝试回home。回home是显式亲和选择，不代表已测出迁移收益。
3. preferred评分不超过 `busyHigh` 时直接使用；超过才寻找溢出目标。
4. 先在preferred所在NUMA找满足 `minGain` 的较低评分组；有合格本地目标即使用。仅当没有合格本地目标且允许跨NUMA时，检查远端并加 `crossNumaPenalty`。
5. 分支达到 `maxHops` 后停止普通位置变化；`pinHome`是初始化等任务的显式home约束。
6. 选定目标后预占即将提交的任务/估计工作，再逐一交给底层池。

它是NUMA优先的分层搜索，不是“把所有组加不同惩罚然后全局最小化”的同义词：一个合格的同NUMA目标可以先于收益更大的远端目标被选中。对于六池，扫描是清晰且可测的基线；没有必要先引入大规模权重树。未来若测出扫描跨NUMA cacheline成本显著，可以比较有限随机候选，但不能预先宣称P2C一定更快或更优。

高低水位、最小收益、跨NUMA惩罚和hop预算用于减少无意义来回移动；尚不包含按时间的冷却窗口、最近一次数据接触时间或实测cache热度。阈值应作为参数扫描，不由单次队列—时延回归直接宣布生产值。

所有快照都可能过时。预占减少连续扇出对尚未入队工作完全不知情的问题，但多个生产者仍可能在各自发布预占之前选中同一池；计数交接期间也可能短暂保守地重复计入。当前算法没有全局选池锁，也不承诺严格最小负载或完全消除羊群效应。

### 单个还是批量

`addBatch`每 `batchChunk` 个就绪任务选一次目标，整块先预占，随后每个任务仍是独立Folly callback。它不把一批任务串行塞进一个callback，也不从已经入队的池搬走整批任务。

小chunk提高调度精度但增加选池次数；大chunk减少选择开销，却会放大超调、将更多同批任务固定到一个目标。它不天然等于“同一数据亲和组”：生产接入应由图运行时把共享数据特征接近的就绪任务组织成批，而不能假定任意批次都有数据复用。

回迁与跟随parent需要独立比较。分支读写模型倾向于检验跟随parent的价值；request共享只读模型可能给回home不同收益；跨NUMA还涉及原始页面位置。多输入join不能简单继承“最后完成的parent”并宣称亲和最优，当前标量join只作为有限模型。

## 6. 成熟实现借鉴及不适用边界

brpc LALB按完成吞吐和调用延迟更新权重，并用未完成调用年龄及最小权重处理反馈滞后、慢目标和恢复探测。当前源码和旧说明中的部分公式不同，本文按核对的源码理解；它的locality是服务调用所体现的延迟差异，并不直接知道算子cache或CPU队列。[brpc LALB实现](https://github.com/apache/brpc/blob/master/src/brpc/policy/locality_aware_load_balancer.cpp)、[权重与未完成调用计算](https://github.com/apache/brpc/blob/master/src/brpc/policy/locality_aware_load_balancer.h)

本场景可以借鉴“不能只观察完成任务”和“保留变化后恢复的机会”。但12 QPS分六池后，request级反馈较稀疏；某request又可能跨池执行和等待RPC，把总RTT全部归因home会混淆工作量及下游差异。LALB式一级反馈应作为之后单独的候选，不作为本轮二级突发控制器。

Linux调度器提供了局部唤醒快路径、容量比较、缓存热度和NUMA迁移取舍的具体实例。可借鉴分级搜索与亲和收益门槛，不能把内核可运行线程直接映射成Folly队列任务，更不应照搬完整内核调度器。[Linux v6.6 fair.c](https://github.com/torvalds/linux/blob/v6.6/kernel/sched/fair.c)

oneTBB在本地较深任务执行和偷取其他线程较早任务之间平衡局部性与并行性；其cache亲和说明也强调数据复用及工作集大小。这支持把真实数据模型和分支延续纳入实验，而不是保证任意DAG在某种策略下最优。[oneTBB任务调度](https://uxlfoundation.github.io/oneTBB/main/tbb_userguide/How_Task_Scheduler_Works.html)、[oneTBB缓存亲和](https://uxlfoundation.github.io/oneTBB/main/tbb_userguide/Bandwidth_and_Cache_Affinity_os.html)

当前工程是基于这些思想的可对照实现，不是Linux调度器、brpc LALB或oneTBB调度器的复刻，也没有业界公认的适用于本场景所有参数的最优策略。

## 7. 有顺序的试验矩阵

不要一次展开所有维度的笛卡尔积。每一步先通过功能与工作量守恒，再增加一个机制；每轮参数、二进制、CPU范围、种子和分析窗口必须留档。

| 顺序 | 固定项 | 主要变量 | 本步问题与进入下一步条件 |
|---|---|---|---|
| A 功能 | 开发机小CPU范围、少request、小足迹 | 四种数据模型、单/批提交、continuation及拒绝路径 | checksum及依赖一致、无丢任务/重复执行、join能正确结束；不能评价ARM性能 |
| B 固定工作校准 | home、低负载、目标机实际CPU范围 | 计算迭代/访问次数、足迹、线程数 | 获得服务CPU、墙钟、有效吞吐和容量估计；保存工作清单，不对每种策略重新校准工作量 |
| C 压力与部署 | home、同一工作清单 | 无绑定/NUMA/cluster；低负载至接近饱和；扇出与重叠 | 建立无均衡基线，找到局部瓶颈、全局饱和和内存瓶颈；配对资源必须相同 |
| D 二级算法 | 一级RR、cluster、固定线程数/足迹/延续方式 | home/Q/active/outstanding/cpu/work；相应单位的阈值 | 量化何处值得借池、何种信号漏报；检查真实触发、CPU样本有效性和观测成本 |
| E 局部性及粒度 | D筛出的少数参数 | None/RO/BranchRW/Mixed；足迹；all/fork决策；chunk；FollowParent/ReturnHome | 找等待收益超过数据位置代价的拐点，排除仅靠减少工作量的假收益 |
| F NUMA与压力稳健性 | 优选的两三种策略 | 禁止/允许跨NUMA、远端惩罚、长尾/偏斜/重叠突发 | 判断何时值得跨NUMA及何时策略开始振荡或过载 |
| G 一级及库集成 | 已验证二级配置 | RR/ColorHash；之后才考虑反馈候选；stage5等价实现 | 分清一级分配、二级救济及实现安放位置各自贡献 |

建议在C中分开扫描：

- **QPS扫描**：保持单request图、固定工作量及足迹不变，覆盖12 QPS及其上下档；以实测资源余量定档，不假定12必然是中负载。
- **扇出扫描**：覆盖20、40、100、400等代表规模，但明确是保持总算子数/工作量还是保持每算子工作量。两者是不同问题，不能混为“只改并发度”。
- **图形扫描**：前置宽扇出、后续较窄并行和串行链；分别观察同一池request重叠。当前驱动若只覆盖分支链，就把其他图形列为扩展，不能将其报告为已验证。
- **线程数扫描**：按CPU范围相邻档及生产的每池数百worker设置；同一亲和对照保持总线程数。线程数改变会影响Folly排队和内核等待的相对占比。

固定工作校准后，可以把典型算子设为接近目标机100ms的固定工作，长尾档接近700–800ms；这些只是校准参照，运行时不再用计时循环强制维持时长。长尾比例、算子类别和分支位置应由固定种子产生并跨策略相同。若生产的100ms包含阻塞或实际CPU时间未知，必须先标注这种差异，不能把它全当CPU需求输入。

工作量检查可用：

\[
D=\lambda\cdot E[\text{每request的总CPU秒}],\qquad
\rho_{nominal}=D/C_{total}
\]

这是容量核对，不是时延公式。举例：12 QPS、每request平均60个100ms纯CPU算子，需要约72 CPU秒/秒，尚未计初始化、join和调度成本；不同SMT效率及内存访问下能否稳定完成必须测量。若把400个算子都设为100ms，则需求会完全不同，不能在不可持续的总负载下把所有尾延迟归咎于亲和。

## 8. 怎样给出定量结论

每个配对必须同时满足有效工作相同、资源范围相同、没有未解释的拒绝或截断。至少保存：

- request计划到达、实际提交、完成及状态；task就绪/提交/开始/完成；二者的时延不要混称。
- 每个request的图形、固定计算和访问预算、checksum；初始化及join是否计入请求口径。
- 分组queue、outstanding、估计work、迁移原因、跨NUMA、hops和实际提交比例；选择了策略名称不等于策略真实触发。
- 完成吞吐、未完成积压趋势、排空时间、进程CPU、观测器开关；页面位置和总驻留足迹。

优先使用相同seed与工作清单配对，并随机化或交错运行顺序。固定计划到达窗口取request cohort，保留这些request在窗口结束后的完成，不能只挑窗口内已经完成的请求。依赖任务和同一burst内任务不是独立样本，不能将几万个task当作同样数量的独立request来给p99窄置信区间。

12 QPS下50秒窗口约600request，p99尾部名义上只有约6个request；这适合发现明显差异，不足以精确评价小幅p99收益。比如希望约100个名义尾部观测，需要约10000个request，12 QPS单轮约14分钟有效窗口，但相关性和非平稳仍影响可信度。先短试点排错，再为少量候选延长运行、多轮重复；不要要求全矩阵都长跑。

报告至少包含每轮均值与p50/p95/p99、配对差、吞吐及积压。只有吞吐/积压稳定且资源没有变化，才讨论稳态性能。过载点仍有价值，但结论是有限时间窗下积压增长和恢复能力，不能宣称存在稳定p99。

队列分桶的条件时延能回答“在这个配置下，观测到某种负载时，之后通常多慢”。它受算子长度、已在执行的任务和同批位置影响，不是队列深度单独导致时延的因果证明。策略配对能估计整个调度干预的效应；若没有CPU调度时间与硬件计数器证据，不能把时延差全部命名为runq等待、cache miss或远端内存成本。

最终应输出一个适用区域表，而不是一个全局获胜算法：在何种request压力、扇出、足迹、线程数及NUMA范围下，均衡带来多大时延/CPU变化，代价和失效点是什么。阈值应在未参与调参的seed和邻近压力档复验。

## 9. 第五阶段仍有意义，但不重复比较算法

“业务只看到一个总池”与“必须修改Folly核心库”是不同决定。当前门面已经能隐藏池的创建和选池；优先在应用基础库验证接口与策略，业务只依赖门面。是否把成熟实现放入Folly，应由复用、接口稳定、生命周期与维护成本决定，不由一次性能实验决定。

第五阶段保留为同算法、同参数、同工作量在不同安放位置下的等价性和工程检验：业务基础库包装与Folly experimental组件是否具有相同放置、记账、异常处理、RequestContext、拒绝及drain行为；是否值得维护上游API或fork。

现有同步脚本保证 `BusinessExecutor`与canonical `AffinityExecutor` 的底层算法镜像；它不自动证明新 `RequestExecutor` 门面也已成为Folly公共API。若将来移入库，应保持门面契约并增加相同测试，不能一边更换算法一边声称测到的是“业务侧与库内”的差异。

真正需要修改调度内部的扩展——例如跨组共享ready队列、已入队任务偷取、严格的全局容量预留——可作为后续独立方案。当前v2的价值是先用可审查、可测试的放置层量化是否有必要承担这些复杂度。

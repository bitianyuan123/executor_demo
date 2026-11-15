# 阶段5：业务侧路由，还是 Folly 原生分组支持

本阶段只讨论**能力放在哪里、接口边界和集成代价**。先读完阶段3的独立负载结果、阶段4的DAG策略对照，再决定是否需要库级能力。把同一段代码移入Folly不应被解释为自动提高性能。

实现归属不改变每硬件cluster一个子池的目标；当前六池来自所选六cluster，四池无绑定仅是历史trace条件。底层两位置的开发机验收也不能代替目标机cluster绑定和数据亲和对照。

阶段4新增的[RequestExecutor门面](../../experiments/affinity/README.md)把多池、home/parent传递和登记式异步续接藏在总池内部，业务只负责请求标识、依赖就绪、fork及已知工作估计。它目前是业务可复用适配库，不是Folly默认行为；本阶段仍有意义：决定成熟后是否将通用机制放入opt-in库、如何兼容Future/KeepAlive、拒绝/取消/关闭和不同业务的上下文。当前两位置等价测试覆盖底层分组器，**尚未把新增高层门面移入Folly**，不能把该测试说成高层门面的双位置验收。

底层新增`readLoad(group)`、`readQueueDepth(group)`诊断/评分入口，不获取TID列表、不加线程列表锁；已同步业务生成版。完整快照仍单独取TID。本轮没有修改默认CPUThreadPoolExecutor队列或唤醒协议。

## 1. 判断与推荐

当前推荐先将总池接入业务基础库：业务提交请求标识、就绪依赖、fork、已知代价，以及RPC完成/取消和失败传播语义；`RequestExecutor`负责创建子池、选择home、继承实际parent位置和执行L2策略，业务不维护这些池编号。多父数据归属仍需图运行时提供语义，不能仅凭requestID推断哪400个任务属于同一次fork或哪个cluster最适合join。普通 `Executor::add(Func)` 不足以表达这些边界。

现有Folly CPUThreadPoolExecutor原生支持工作队列、线程生命周期、通知/等待、上下文和统计；本次基线**没有**按业务request进行跨独立池负载均衡的默认能力。Linux调度器能在允许CPU间调度线程，但不会从一个Folly池的队列偷取业务闭包放进另一个池。

| 放置方式 | 可复用能力 | 业务仍负责 | 引入成本与边界 |
|---|---|---|---|
| 业务基础库总池 + 原版Folly子池 | 隐藏建池、home/parent传递和L2选择；无需修改公共Folly | 请求上下文、DAG就绪/fork、预估代价、依赖失败与异步取消 | 基础库维护适配层，业务只用总池；便于灰度/回滚 |
| Folly可选组件承载成熟总池契约 | 复用相同绑定、计量、admission、上下文与drain能力 | 同上；库不能替图运行时猜多父数据亲和语义 | 当前库版仅有底层分组器；高层门面迁入及兼容测试仍是后续工作 |
| 改默认CPUThreadPoolExecutor | 所有使用者隐式受影响 | 业务语义仍无法凭空获得 | 风险最大，当前没有证据支持这样做 |

因此交付业务版和**opt-in实验扩展**，不改Folly默认队列或调度行为。是否将API提交上游是后续独立决策，本交付没有宣称它已经是Folly官方原生能力。

## 2. 当前可构建对照：v1底层分组器的两个位置

- 阶段4业务版：`experiments/dag/include/executor_demo/BusinessExecutor.h`；只依赖原版Folly公共接口。
- 阶段5库版：Folly分支中的 `folly/executors/experimental/AffinityExecutor.h`。
- 同一个DAG驱动通过编译选项分别生成 `executor_demo`（业务版）和 `dag_folly`（库版），metadata中的 `backend` 标识分别为 `business` / `folly_experimental`。

两版**故意使用相同算法**，区别为命名空间、类名和代码归属。业务版由 `scripts/sync_business_executor.py` 从实验header生成，已提交生成产物及两套各24项单元测试（覆盖候选域及单池负载读取接口）；普通业务版构建不运行生成器、不读取Folly实验源码。这样阶段4不依赖阶段5分支，且对比不会把算法差异错误归因于“放在库里”。`business-source.json`记录哈希，检查器防止静默漂移。长期维护宜选择一个归属，不建议永久维护两份独立演进的逻辑。

```bash
# 阶段4：仅已安装的原版Folly即可
BUILD_JOBS=1 ./scripts/build_stage.sh dag

# 阶段5：额外需要包含实验header的Folly源码分支
FOLLY_SOURCE_DIR=../folly BUILD_JOBS=1 ./scripts/build_stage.sh placement
python3 scripts/sync_business_executor.py --check --folly-dir ../folly
python3 stages/05-placement/compare_backends.py \
  --business build-placement/executor_demo --library build-placement/dag_folly \
  --output results/local/placement-compare
```

`compare_backends.py`串行执行5种策略×2个后端，再加受限候选域的Poisson/随机RPC场景×2个后端，共12轮。核对同一seed的计划到达、任务/CPU阶段预算、RPC目标、全部请求完成、home规则与守恒。并发快照与线程时序可能导致不同placement，因此不要求borrow/sticky路线逐项相同，也不以某一次时延更低宣称打包位置更快。完整自动化还分别运行两种后端的DAG依赖/RPC/取消测试和两套执行器测试。

## 3. v1底层适配器契约与目标业务接口

以下`Hint`、`Decision`及borrow/sticky选项描述现有v1底层分组器和其DAG适配器，不是要求目标业务绕过总池直接选池。v2高层接口和自动上下文契约以[RequestExecutor指南](../../experiments/affinity/README.md)为准。

底层`Hint{home,parent,hasParent,isFanout,estimatedCpuUs}`描述就绪任务；每次接收返回不可变 `Decision`，由适配器保存实际chosen池并传给后继，而不是共享一个可变“request当前池”。v1的多父join parent约定由DAG适配器提供；v2当前沿最后完成分支上下文，不代表多输入数据亲和最优。两层`add`都可能拒绝，图运行时必须传播失败或取消，不可静默丢失子节点后永远等join。

负载只影响**新就绪任务的放置**。当前没有偷取已入队任务、抢占运行中算子或动态修改正在执行线程的亲和mask。Borrow会在后继提交时重新偏好home；sticky跟随实际父池；fanout是否允许二次fork参与由阶段4的 `--balance-forks initial|all`决定。库只使用isFanout，不认识图层次。

本轮增加 `CandidateScope::All/PreferredNuma`，默认All保持兼容。PreferredNuma先按本次preferred池的NUMA标签限制候选，再比较负载；LeastLoad也遵守。Borrow的preferred为home，Sticky/Fanout为实际parent（初始为home）。库要求标签已知，但标签不等于真实物理绑定；DAG前端额外验证sysfs CPU归属并禁止none绑定配合受限域。来自外部的跨域parent仍可被Borrow回迁至home，因此不能将该选项描述为“任何source到chosen都禁止跨NUMA”。两版同步实现这一可选机制，实际内存局部性仍需目标机证据。

v1分组封装不是普通 `folly::Executor` 子类：它要求适配器传显式hint并支持拒绝，不能把这种语义伪装成无条件的 `void add(Func)`。v2总池虽隐藏池编号，也仍需专门处理拒绝与KeepAlive适配，不能直接当成已兼容Future的普通Executor。两者都组合原版CPU池，未改写底层futex协议。

## 4. 推进到生产前需要的证据

1. 结合阶段3结果选择足够准确且低扰动的负载基线；增加pending/inflight/work等指标时，分别证明增益并测量增量成本，不预设一定需要更多计量。
2. 在阶段4分别证明借出、跟随、回迁及决策时机的收益，记录CPU/请求、尾时延、拒绝和迁移。v1的initial/all区分初始与所有fork；v2的fork/all区分所有fork与每次就绪提交，不能把v2的fork说成仅初始扇出。首先同NUMA借用，再考虑跨NUMA。
3. 固定CPU预算适合排队研究；局部性成本应使用固定工作量和真实数据访问、真实NUMA内存放置。v1只读数据由发生器first-touch；v2已改为home回调初始化，并加入分支读写链，但仍需核对实际页位置与服务成本，不能只凭初始化位置证明home回迁收益。
4. 标定有效容量、SMT收益、共享NUMA mask重叠、cgroup quota。capacity不能让同一NUMA的3个池各自声称拥有该NUMA全部独占容量。
5. 校准收益阈值和估计误差，比较已实现的同NUMA候选硬约束与全机候选，再考虑驻留时间、每分支迁移预算和公平性。当前没有独立冷却机制，默认零阈值不是生产推荐值。
6. v1底层库仅自动追踪自身回调内的后继，DAG适配器应先排空完整图和外部RPC回调再join。v2总池通过登记式continuation追踪异步续接，join等待其resume/cancel/析构；生产接入仍需保证所有完成、超时、取消路径正确释放token。高层契约迁入Folly时应保留同样验收。
7. 当前满池拒绝不自动改选另一池；若要fallback，必须有界、保留deadline、计入拒绝/尝试，不能无限重试。

本阶段验收是：业务版不依赖实验Folly头文件；库版单独可构建；两版功能及工作量契约一致；新增库能力没有侵入默认线程池。是否上线哪个位置，要由目标环境数据和维护范围决定。

本次独立构建、测试和后端接入验证见[RESULTS](RESULTS.md)。

# 阶段5：业务侧路由，还是 Folly 原生分组支持

本阶段只讨论**能力放在哪里、接口边界和集成代价**。先读完阶段3的独立负载结果、阶段4的DAG策略对照，再决定是否需要库级能力。把同一段代码移入Folly不应被解释为自动提高性能。

## 1. 判断与推荐

当前推荐先在业务侧接入：业务知道request home、依赖何时满足、哪里是fork、多个父节点的数据归属、RPC continuation的来源、截止时间和失败传播；普通 `Executor::add(Func)` 看不到这些语义。只有某个requestID无法推断哪400个任务属于同一次fork，也不能推断join应回哪个cluster。

现有Folly CPUThreadPoolExecutor原生支持工作队列、线程生命周期、通知/等待、上下文和统计；本次基线**没有**按业务request进行跨独立池负载均衡的默认能力。Linux调度器能在允许CPU间调度线程，但不会从一个Folly池的队列偷取业务闭包放进另一个池。

| 放置方式 | 可复用能力 | 业务仍负责 | 引入成本与边界 |
|---|---|---|---|
| 业务路由器 + 原版Folly多个池 | 策略可以快速试验；无需升级或修改公共Folly | DAG就绪、home/parent/fork、预估代价、请求生命周期 | 业务维护小型适配层；便于灰度/回滚 |
| Folly可选分组执行器 | 统一绑定、计量、admission、候选选择与join行为 | 同上；库不能替业务猜亲和语义 | 扩大公共API、兼容性和测试责任；适合多个服务共享成熟策略 |
| 改默认CPUThreadPoolExecutor | 所有使用者隐式受影响 | 业务语义仍无法凭空获得 | 风险最大，当前没有证据支持这样做 |

因此交付业务版和**opt-in实验扩展**，不改Folly默认队列或调度行为。是否将API提交上游是后续独立决策，本交付没有宣称它已经是Folly官方原生能力。

## 2. 两个独立可构建位置

- 阶段4业务版：`experiments/dag/include/executor_demo/BusinessExecutor.h`；只依赖原版Folly公共接口。
- 阶段5库版：Folly分支中的 `folly/executors/experimental/AffinityExecutor.h`。
- 同一个DAG驱动通过编译选项分别生成 `executor_demo`（业务版）和 `dag_folly`（库版），metadata中的 `backend` 标识分别为 `business` / `folly_experimental`。

两版**故意使用相同算法**，区别为命名空间、类名和代码归属。业务版由 `scripts/sync_business_executor.py` 从实验header生成，已提交生成产物及17项等价的单元测试；普通业务版构建不运行生成器、不读取Folly实验源码。这样阶段4不依赖阶段5分支，且对比不会把算法差异错误归因于“放在库里”。`business-source.json`记录哈希，检查器防止静默漂移。长期维护宜选择一个归属，不建议永久维护两份独立演进的逻辑。

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

`compare_backends.py`串行执行5种策略×2个后端，核对同一seed的任务/CPU阶段预算、全部请求完成、home规则与守恒。并发快照与线程时序可能导致不同placement，因此不要求borrow/sticky路线逐项相同，也不以某一次时延更低宣称打包位置更快。完整自动化还分别运行两种后端的DAG依赖/RPC/取消测试和两套执行器测试。

## 3. API职责

`Hint{home,parent,hasParent,isFanout,estimatedCpuUs}`描述就绪任务；每次接收返回不可变 `Decision`，后继保存实际chosen池，而不是共享一个可变“request当前池”。多父join的parent约定必须由业务给出。`add`可以拒绝，业务必须将拒绝传播到图，不可静默丢失子节点后永远等join。

负载只影响**新就绪任务的放置**。当前没有偷取已入队任务、抢占运行中算子或动态修改正在执行线程的亲和mask。Borrow会在后继提交时重新偏好home；sticky跟随实际父池；fanout是否允许二次fork参与由阶段4的 `--balance-forks initial|all`决定。库只使用isFanout，不认识图层次。

分组封装不是普通 `folly::Executor` 子类：它要求显式hint并支持拒绝，不能把这种语义伪装成无条件的 `void add(Func)`。它仍组合原版CPU池，实现的是库中可选分组能力，未改写底层futex协议。

## 4. 推进到生产前需要的证据

1. 在阶段3证明需要额外的pending/inflight/work指标，并测量它们的增量成本；不要默认全量计量一定最佳。
2. 在阶段4分别证明借出、跟随、回迁、仅初始fork或所有fork的收益，记录CPU/请求、尾时延、拒绝和迁移。首先同NUMA借用，再考虑跨NUMA。
3. 固定CPU预算适合排队研究；局部性成本应使用固定工作量和真实数据访问、真实NUMA内存放置。当前只读数据由发生器first-touch，尚不能证明home回迁改善远端内存代价。
4. 标定有效容量、SMT收益、共享NUMA mask重叠、cgroup quota。capacity不能让同一NUMA的3个池各自声称拥有该NUMA全部独占容量。
5. 校准收益阈值和估计误差，再考虑驻留时间、每分支迁移预算、公平性及同NUMA优先候选。当前没有独立冷却机制，默认零阈值不是生产推荐值。
6. 异步RPC期间请求仍存活，关闭流程先排空完整DAG和外部回调再join。当前库只自动追踪自身回调内的后继提交。
7. 当前满池拒绝不自动改选另一池；若要fallback，必须有界、保留deadline、计入拒绝/尝试，不能无限重试。

本阶段验收是：业务版不依赖实验Folly头文件；库版单独可构建；两版功能及工作量契约一致；新增库能力没有侵入默认线程池。是否上线哪个位置，要由目标环境数据和维护范围决定。

本次独立构建、测试和后端接入验证见[RESULTS](RESULTS.md)。

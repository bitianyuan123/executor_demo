# 业务落地判断与人工复查清单

当前建议从业务 DAG 调度层接入“显式亲和提示 + 可观测计数 + 提交时选池”，先跑 shadow decision，再进行小流量受控实验。本工程提供的 Folly experimental 封装可复用机制，但不应立即替换所有 CPUThreadPoolExecutor 的默认调度。

## 1. 为什么第一层应在业务侧

通用线程池看见的是独立 `Func`，无法推断哪个算子读取同一图数据、谁是关键路径、一个 fork 将产生多少工作、RPC 后还会不会回来、哪个 merge 输入最重。业务层拥有 requestID、DAG、deadline、算子类型、取消状态和数据生命周期，能够准确填写 home/parent/fanout/estimatedCpuUs。

Folly 适合提供线程工厂、队列/唤醒、统计钩子和一个显式 opt-in 的分组调度器。若后续多个业务已验证相同策略，可以把稳定接口沉淀到库；库仍需要调用者提供染色键/拓扑/工作代价，不能自动从 Linux cluster ID 推断业务亲和性。不要把“同一个 request”默认等同于“所有算子共享同一小块缓存数据”。

本次已创建 Folly 开发目录并实现 `folly::experimental::AffinityExecutor`，由多个原版 CPU 池组成；它保留底层 RequestContext 传递，提供五种策略、三种负载指标、指定 CPU 范围与显式 NUMA label、任务 admission 上限、线程 ID 和生命周期计量。它是工程可验证的探索接口，不是新的 upstream 稳定 ABI，也没有修改 Folly 原池的默认配置。

## 2. 必须补齐的事实

以下问题不阻塞本次 demo，但会直接改变生产参数和结论。

| 待确认项 | 为什么重要 | 获取方式 |
|---|---|---|
| 实际 Folly commit、CPU 池构造函数、队列类型、gflags | wakeup/size/default 因版本不同 | 构建锁定版本、启动配置、源码调用点 |
| 100 ms 是线程 CPU 时间还是墙钟 | 决定资源需求量纲 | 同一 CPU 回调记录 wall + thread CPU；抽样检查锁/RPC |
| 20–400 是总算子数还是初始宽度 | 决定总工作量和峰值 | 每请求总提交数、各 fork 宽度、最大就绪数 |
| 700–800 ms 算子比例与类别 | 尾延迟与剩余工作预测 | 按算子类型的 CPU/墙钟直方图 |
| 50% 的分母、窗口和进程范围 | 防止把 quota 或整机平均当可借容量 | 指标定义、每 CPU/cluster、cpu.stat |
| ARM CPU 型号、SMT 是否实际启用 | 96 逻辑 CPU 不保证双倍吞吐 | 型号、thread_siblings_list、lscpu 与固件信息 |
| cluster 与缓存共享关系 | cluster 不必等于 L3/LLC 域 | sysfs topology + cache shared_cpu_list |
| 内存由哪个线程首次触及、在哪个 NUMA | CPU 绑定不自动搬迁已分配内存 | 分配路径、numa_maps、numastat、应用 first-touch |
| executor 是否同步阻塞 | 影响线程数与 work 估计 | 栈采样、调度 trace、RPC/Future 使用审计 |
| 目前 min/max 线程与动态伸缩 | 空闲线程数不等于真正活跃数 | 实例配置和线程创建/退出统计 |
| IO/worker/AIO 与 executor 是否共享 CPU/quota | 路由可能挤压接入或 RPC | 每 TID affinity/cgroup 映射 |
| 真实取消、超时、shutdown 协议 | 拒绝不能把 DAG 留在永远等待状态 | 人工代码审查和故障注入 |

ARM64 架构本身不保证任一具体产品支持 SMT，也不规定 cluster 必然对应某级共享 cache。Linux 导出的 cluster 属性依赖架构支持，可能缺失/使用默认值；应在允许 CPU 集内根据实际 `core_id`、siblings、NUMA 与 cache 信息建组，而不是假设 CPU 编号连续代表一个 cluster。[CPU topology 官方说明](https://www.kernel.org/doc/html/latest/admin-guide/cputopology.html)

NUMA 内存策略决定新页分配位置，改变 CPU affinity 不会自动把旧页搬到新节点；即使设置内存策略，已 fault-in 页也不自然随之迁移。需明确 first-touch、内存策略、automatic NUMA balancing 和迁页状态。[NUMA memory policy](https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html)

## 3. 建议的生产验证顺序

1. **先统一口径。** 采集请求工作量、任务 CPU/wall、submit-to-start、pool/TID 映射、runqueue 等待、完成/拒绝和各 cluster CPU。修复业务若存在的同步 RPC 阻塞问题，至少将其单独标记。
2. **线程数扫描。** 以每池允许的物理/逻辑核规模为参照，比较多个倍率与当前几百线程。不要先把复杂路由收益与改变线程数收益混在一起。预热线程并控制动态池参数。
3. **绑定范围扫描。** 不绑定、NUMA、cluster；同时验证数据内存位置。为 IO/worker/AIO 留足资源，记录 IRQ、其他进程与 quota 干扰。
4. **影子路由。** 实际仍投 home，只计算 borrow/sticky/fanout 的候选、预测收益和预计迁移；检查估计是否稳定、业务 hints 是否正确。影子阶段不能证明策略时延收益，但可发现配置与工作量错误。
5. **同 NUMA 借用。** 先限制候选在同 NUMA（需要在业务侧过滤或扩展原型），比较 tail、CPU/请求和 cache 变化。当前原型扫描所有组，以 penalty 表达偏好，没有硬性的 same-NUMA-only 开关。
6. **必要时跨 NUMA。** 只有节省等待预计超过实测搬动/远端访问成本才尝试；按数据集规模、访问模式、deadline 和负载分层。不能只看全体平均值。
7. **加入稳定性边界。** deadline、admission、取消传播、每请求/分支迁移预算、冷却时间、突发限额、可回滚开关；对照原策略和无流量阶段开销。

这里的顺序是减少混杂变量的实验建议，不是部署前必须逐项申请批准的流程。实际生产变更仍由业务拥有者按本地发布制度操作。

## 4. 缓存和迁移成本怎样计量

一次逻辑算子换池可能涉及：提交与共享队列原子成本、waiter 唤醒、内核 wake-to-run、冷 cache/TLB、远端 NUMA 内存、共享可变数据的 cacheline 争用以及被挤出的其他任务。前几项可用本工程时间戳和调度事件观察；后几项需要真实硬件与代表性数据访问。

建议比较同一数据/算子在“同线程”“同 cluster 不同线程”“同 NUMA 不同 cluster”“跨 NUMA”四种放置下的 CPU 时间与墙钟；同时保留 throughput、cycles/instructions、支持的 cache/memory/互连 PMU、CPU migration 和上下文切换。ARM PMU 事件随 CPU 与内核暴露不同，应以机器 `perf list` 和厂商文档为准，不能套用 x86 remote/LLC 事件名。

`wall` 的减少而 `cpu` 增加，可能表示用更多执行开销换取更短排队；两者都减少可能表示局部性或争用改善；两者都增大则更需检查迁移和 shared capacity 模型。所有解释需要对照组。给路由器配置 `crossNumaPenaltyUs=100` 只表达假设成本为 100 µs，不是测得跨 NUMA 每次恰好多 100 µs。

## 5. 已知原型限制

- 图是 fanout 多链加末端 join，并可带 RPC continuation；不是任意图调度引擎。真实动态多级 fork、循环执行、共享节点、业务取消与多输入 merge 要补测试。
- `work` 使用提交方提供的完整估计，执行中不减少；当前没有算子类型在线学习、预测置信区间或剩余工作模型。合成实验已知预算，生产通常未知，因此合成 work 优势可能偏乐观；可用 `--estimate-us` 固定估计作对照。
- NUMA ID 是调用方 label；CPU affinity 不绑定内存。跨组 mask 允许重叠，必须识别共享容量，不能将 6 个逻辑池写成 6 个真实 cluster。
- 原型只在提交点选池，不移动已有队列积压，不抢占长回调，不自动注册 request 染色。若要 `RequestContext` 染色路由，应显式接入并保留默认无 hint 行为。
- 收益阈值不是完整防振荡控制；没有冷却/驻留/迁移次数限制。负载高频抖动和多个生产者同时选池仍可能影响 placement。
- 任务级 admission 满时不自动改投另一组，不提供 deadline-aware 排序、公平队列或租户隔离。底层无界队列需要业务外层 admission 防止内存无限增长。
- 任务异常只释放执行器计数；业务 DAG 的失败完成需自己负责。外部 AIO 回调排空与 executor join 的顺序需要业务生命周期管理。
- 全量任务日志、计时与计数有开销；`--task-log 0` 只关闭行输出，仍有全部计时。任务 CPU 之和不包含全部路由/线程池/日志成本。当前验证目标是可解释、可复现，不能宣称是生产零扰动实现。默认 CPU 时间预算模式也不能证明局部性服务时间收益，应使用经过校准的固定迭代模式或真实算子。
- 本机没有真实 ARM 双 NUMA 六 cluster 条件，不能由本机结果确定生产迁移阈值、SMT 最优线程数或缓存收益。

## 6. 什么结果才值得推广到库

若多组业务负载都能在保持吞吐和错误率的同时稳定改善尾延迟，且明确了 hints、取消/关闭协议、统计成本与公平性，再考虑提交为公共 Folly 能力。建议保持以下接口边界：拓扑由使用方提供；home/父池/算子类别为可选显式 hint；策略可插拔；默认行为保持传统 Executor 语义；指标不冒充内核 runnable 信息；高成本统计可关闭。

若收益仅来自减少几百线程到合理线程数，或仅来自放宽 cluster affinity，则不必为了“更像内核”引入复杂库内调度。若复杂图需要 deadline、租户公平、内存归属或关键路径优先，则业务图调度器继续拥有策略，Folly 只提供可靠执行机制通常更容易维护。

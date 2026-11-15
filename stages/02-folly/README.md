# 阶段 2：Folly 任务管理、唤醒与低扰动负载信息报告

**原生任务队列长度是合理且读取成本低的第一项负载信号，不应因为它不能表示内核 runqueue 就否定它。** 应分别回答：它对“尚未被消费者领取的工作”有多准确，以及它能否独立预测“新任务的等待/完成时延”。这两个问题的答案不同；本报告给出代码依据和选择方法。

本报告固定分析 Folly `v2026.10.26.00`，基线提交 `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。源码链接均锁定该提交。阶段 3 的简单实验和下面的计量微基准不依赖实验性分组执行器。实际业务若使用其他版本、定制队列或 gflags，必须重新确认相应路径。

前置报告：[阶段 1：真实业务分析](../01-business/README.md)。负载读取与计数写入的独立实测工程：[指标成本微基准](../../experiments/metrics/README.md)，结果见 [RESULTS](../../experiments/metrics/RESULTS.md)。本报告不预设 ARM 上每次调用耗时多少 ns。

针对新确认的4×128线程、单池active均值20/峰值80（定义待核），追加[负载观测复核与生产适配器](../../docs/LOAD_OBSERVATION_REVIEW.md)及[调度轨迹工作流](../../docs/TRACE_WORKFLOW.md)。保留Q基线，同时比较O/I和后台CPU/runqueue信号；不把“active与CPU相关”当作所有负载成立的先验。

## 1. 必须分开的三个队列/状态集合

| 对象 | 管理者 | 其中是什么 | 何时离开 | 是否等于另一项 |
|---|---|---|---|---|
| 业务 DAG 未就绪集合 | 业务图调度器 | 前驱尚未完成的逻辑节点 | 依赖满足后提交 | 不在 Folly 任务队列中 |
| Folly 任务队列 | CPUThreadPoolExecutor/BlockingQueue | `CPUTask`，包含闭包、上下文和元数据 | 消费者 `dequeue` | 不等于 Linux runqueue |
| Linux runqueue | 内核调度器 | 可运行的 Linux 线程/task | 被调度运行、阻塞或迁移等状态变化 | 不知道线程内当前对应哪个业务节点 |

Folly 信号量还维护“可消费工作数量/通知交接”的状态，它不是第四份闭包队列。默认 `size()` 读取这个状态，因此必须先理解它与物理任务队列之间的协议。

一个线程从 futex 等待被唤醒，通常先变成 runnable；它可能先在内核等待 CPU，之后才继续领取 permit、取闭包和执行。一个已经进入闭包的线程也可能被抢占，再次经历 runnable 等待。**唤醒、取出闭包、进入回调、在 CPU 上运行是四个不同事件。**

## 2. 首先核对构造函数与默认值

本提交的默认 gflag `folly_cputhreadpoolexecutor_use_throttled_lifo_sem=true`。默认普通队列为 `UnboundedBlockingQueue<CPUTask, ThrottledLifoSem>`；`makeLifoSemQueue()` 才显式选经典 `LifoSem`。[默认配置与队列工厂](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L33-L137)

`makeThrottledLifoSemQueue()` 默认 `wakeUpInterval={}`，即 0 ns。不能由类名 Throttled 推断默认存在一个非零的人工唤醒延迟。[工厂参数](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.h#L82-L97)

另一个容易影响实验的默认值是动态线程：单个 `size_t numThreads` 构造在动态功能开启时会转换为 `max=numThreads,min=0`。为了比较“每池 N 个消费者”的稳定行为，实验应固定 `std::pair{N,N}` 并预热；生产仍需记录其实际 min/max，不能仅看传入的最大线程数。[构造转换](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L140-L155)

优先级队列在队列内选择不同优先级的闭包，不等于改变 Linux 线程调度优先级。有界队列、自定义 `BlockingQueue` 可以具有不同的 `size()` 语义；本文关于默认无界队列的结论不能直接套用到它们。

## 3. 从提交到执行的完整调用关系

```text
业务：节点依赖满足，选择 CPU 池，构造 Func
  CPUThreadPoolExecutor::add(Func)
    add(Func, expiration, expireCallback)
      构造 CPUTask
        保存 Func
        RequestContext::saveContext()
        Task 构造中记录 enqueueTime、priority、taskId
      addImpl
        QueueObserver::onEnqueued（如果注册）
        registerTaskEnqueue -> TaskObserver::taskEnqueued
        判断是否可能需要增加工作线程/持有 KeepAlive
        taskQueue_->add(move(task))
          queue_.enqueue(move(task))       // 闭包一定先进入队列
          sem_.post()                     // 发布可消费数量/通知等待者
        必要时 ensureActiveThreads()

工作线程 CPUThreadPoolExecutor::threadRun
  启动握手 initBaton / readyBaton（不是每任务路径）
  循环
    taskQueue_->take() 或 try_take_for(timeout)
      sem_.wait() / try_wait_for()
      queue_.dequeue()                     // 有 permit 后才取任务
    若停止标记/超时，处理线程生命周期
    QueueObserver::onDequeued（如果注册）
    ThreadPoolExecutor::runTask
      idle=false
      计算 waitTime
      恢复该任务 RequestContext
      TaskObserver::taskDequeued
      检查过期；执行 Func 或 expireCallback
      记录 runTime；TaskObserver::taskProcessed
      恢复先前 RequestContext；idle=true
    下一次 take
```

源码依据：[任务构造与 add](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L257-L295)、[addImpl](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.h#L208-L237)、[queue enqueue/post 与 wait/dequeue](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/task_queue/UnboundedBlockingQueue.h#L32-L49)、[worker 循环](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L341-L392)、[任务计时与执行](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.cpp#L84-L196)。

### 3.1 精准唤醒是否绕过入队

**本路径不绕过。** “精准”意味着在等待集合里选择某个 waiter，并通知它的等待对象；闭包仍由共享任务队列传递。通知中不携带需要执行的那个 `Func`，permit 也不永久绑定某个闭包。

捕获到闭包中的值、智能指针或句柄负责传递业务数据；队列同步负责安全发布闭包对象。智能指针保证相关生命周期，不自动保证其指向的可变数据没有竞争。`RequestContext` 保存/恢复的是 Folly 上下文，既不是复制整个线程栈，也不是自动传播任意自定义 TLS。业务 requestID、取消状态和数据所有权必须有明确携带方式。

提交方得到 `BlockingQueueAddResult::reusedThread`，用途是是否需要扩线程。它不包含“由哪个 TID 执行哪个闭包”的绑定，也不证明已经获得 CPU。[返回值定义](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/task_queue/BlockingQueue.h#L39-L53)

## 4. 两种信号量的唤醒与 queue size 不能混讲

### 4.1 经典 LifoSem：交接 permit

`post()` 内部 `incrOrPop(1)` 对共享状态进行原子操作：存在等待节点时弹出一个 waiter，并直接 `handoff.post()`；没有 waiter 时增加公共 value。前一种情况下，permit 已交接给特定 waiter，公共 value 不增加。[post 与交接](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L372-L409)

消费者先尝试快路径取 permit；失败后再原子检查并注册等待节点，解决注册等待和生产者 post 的竞态。超时等待还要处理“刚超时又收到通知”的竞争。[等待路径](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L494-L553)

其 `valueGuess()` 用一次 acquire atomic load，若当前状态是 waiter 链表则返回 0，否则返回公共 value。源码明确不承诺 `valueGuess()` 的通用线性化契约，并称其为估计值。[size 对应的实现](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L556-L564)

因此 **经典 LifoSem 中，一次 post 已通知 waiter、但 waiter 还在内核等待 CPU 时，native queue 可以为 0，尽管闭包还留在物理队列。** 这是其公共 permit 计数的语义，不是未入队。

### 4.2 默认 ThrottledLifoSem：先增加共享 value，再推进唤醒链

`postImpl()` 先增加共享 value，再结合 waiter 数和 `kWakingBit` 判断是否启动 waking chain。通知某个 waiter 本身不先为其扣掉一个专属 permit。被唤醒者成功扣减共享 value 后，如果还有工作，再推进下一次通知；已经活跃的消费者也可以先消费共享 value。[共享 value 与 post](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L138-L219)、[waking chain](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L226-L327)

`valueGuess()` 是 `state_.load() & kValueMask`；没有显式 memory order，故该处为默认 sequentially consistent load。它是一个原子读和位运算，不是遍历物理队列，不是原子 fetch-add，也没有为统计额外更新队列计数。

所以 **默认 ThrottledLifoSem 中，waiter 被通知但尚未拿到 CPU、也没有其他消费者领取这份工作时，shared value 仍在，native queue 能看到这份积压。** 不能用经典 LifoSem 的 handoff 例子论证默认队列在这个时点必然漏计。

### 4.3 默认 size 为什么仍不是精确物理队列长度

默认 `size()` 读取尚未消费的 shared permit，协议中仍有以下窗口：

| 时刻/窗口 | 闭包是否在物理队列 | 默认 shared value 是否已代表它 | 包装器“尚未开始”计数是否包含它 |
|---|---|---|---|
| 预留记录完成、真正 enqueue 前 | 否 | 否 | 若采用先预留方案，则包含 |
| `queue.enqueue` 完成、`sem.post` 前 | 是 | 否 | 包含 |
| `post` 后，waiter 仅被通知、尚未成功领取 | 是 | 是；其他消费者仍可能先领取 | 包含 |
| 消费者已扣 permit，`dequeue` 前 | 是 | 否 | 包含 |
| 已 dequeue、尚未进入包装回调 | 否 | 否 | 包含 |
| 包装回调开始后 | 否 | 否 | 不包含；应由 executing/outstanding 表示 |

这些事件并非跨字段的事务快照；读数还可能在返回后立刻过时。表格解释单项工作的生命周期，不能用于构造并发时必须成立的计数等式。对于经典 LifoSem，“post 后直接交接 waiter”行的 native value 就已经不包含那份 permit。

这说明 native queue 是廉价、语义清楚的**未领取工作量代理**；通常值得先测。是否需要更严格的“尚未进入回调”计数，要看这些窗口对实际任务时长、唤醒压力和预测误差的影响。

## 5. 是否主动轮询，是否会与被唤醒线程抢任务

worker 执行完一个回调便立即进入下一次 `take()`。有 permit 时直接领取，无需先睡眠。因此存在积极消费队列的线程，但正常路径不是每隔一个固定周期扫描队列，也不是永久忙轮询。

没有工作时会经历快路径尝试、有界 spin，之后才阻塞。本版本 `WaitOptions` 默认 spin 上限为 2 µs；ThrottledLifoSem 慢路径等待专属通知时显式将 spin 设为 0。超时主要用于动态缩线程，不应解释成发现新任务所必需的轮询周期。[默认 spin 值](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/WaitOptions.h#L33-L71)、[Throttled 快慢路径](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L113-L135)、[慢路径通知等待](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L241-L259)

多个消费者可以并发从同一 MPMC 队列取任务，包括刚完成前一个回调的线程和刚被唤醒的线程。但有两种不同的竞争：

- 经典 LifoSem 已交接给 A 的唯一 permit，B 不能再凭空领取。若 B 获得另一份 permit，A、B 仍可并发 dequeue，具体闭包不与 permit 绑定。
- ThrottledLifoSem 使用共享 value，活跃 B 可以在 waiter A 继续运行前先领取，A 之后发现无工作并继续等待。这是算法允许的减少无谓唤醒方式。

LIFO 指等待线程的选取顺序，不表示业务任务必然按 LIFO 顺序执行；任务顺序还取决于底层队列和优先级。选最近等待的线程可能保留热状态，但不等于知道某个 request 的缓存归属。

## 6. 从用户态通知到内核调度等待

本版本常规等待对象是 `SaturatingSemaphore`。LifoSem 默认 Handoff 和 Throttled waiter 都使用它；worker 启动时的 Baton 不应误画成每次任务的通知对象。[LifoSem 默认模板](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L39-L42)、[Throttled waiter](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L154-L157)

```text
SaturatingSemaphore::post
  状态尚未真正阻塞：发布 READY，可能无需系统调用
  状态已 BLOCKED：发布 READY -> futexWake
    Linux FUTEX_WAKE_BITSET | FUTEX_PRIVATE_FLAG
      内核匹配这个等待地址上的线程，使其 runnable
        调度器在允许 CPU 中放置/入队
          获得 CPU，线程继续 wait / 取 permit / dequeue / 回调
```

[SaturatingSemaphore 通知与等待](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/SaturatingSemaphore.h#L148-L329)、[Linux futex wake 实现](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/detail/Futex.cpp#L61-L78)。通知先于阻塞、或者等待者还在 spin 时，可能完全没有 futex wake 系统调用。不能用一次 post 对应一次系统调用或一次 context switch 的模型估算成本。

affinity 限制内核可选择的 CPU。唤醒方是同池工作线程，并不承诺被唤醒者立即执行，也不承诺运行在唤醒方所在 CPU。Linux 的线程放置策略无法理解业务 requestID；本报告不把某个内核版本的 fair 调度启发式当成所有 ARM 厂商内核的固定行为。

## 7. API 的真实含义与容易误读的名字

| API/字段 | 实际含义 | 对已唤醒但没得到 CPU 的线程能说明什么 |
|---|---|---|
| `CPUThreadPoolExecutor::getTaskQueueSize()` | 直接调实际 BlockingQueue 的 `size()`；默认是上述 semaphore value | 默认版本在 permit 未被领取时可继续计数；不能完整识别全部 runnable 等待 |
| `getPendingTaskCount()` | 持线程列表共享锁，再调用同一个队列 size | 不比上一接口多识别“未开始”状态 |
| `numThreads()` | 配置最大线程数 | 不表示执行中的线程数 |
| `numActiveThreads()` | 动态线程管理里的实际活动/存活 worker 数 | 不是忙线程数，也不是 on-CPU 数 |
| `getPoolStats[Light]().activeThreadCount` | 根据各线程 `idle` 标记估算回调占用线程数 | waiter 还没进 runTask 时可能仍显示 idle；runTask 内被抢占/阻塞又一直显示 active |
| `getUsedCpuTime()` | 所有池线程累计 CPU 时间，含已退出线程的记账 | 差分反映历史实际获得的 CPU；不反映尚未获得 CPU 的未来需求 |
| `TaskObserver::waitTime` | Task 构造时间至 runTask 开始 | 多阶段 submit-to-start，不是纯物理队列驻留 |
| `TaskObserver::runTime` | runTask 内的墙钟差 | 包含被抢占/阻塞，不是线程 CPU 时间 |

[queue size 与 pending 实现](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L302-L304)、[pool stats 扫描](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.cpp#L420-L479)、[公开接口与 CPU 时间契约](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.h#L100-L164)、[TaskObserver 定义](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.h#L199-L237)。

过期任务和停止标记也影响边界：过期闭包可能不进入用户包装回调，却会走 Folly 的过期处理；线程关闭时队列里还可能有内部 poison 任务。正式负载测量窗口应避开启动/关闭过程，或明确把这些事件分开记录。不能仅在正常回调内减计数而遗漏过期/取消路径。

## 8. 不同负载方法：读取成本、额外写入与准确性

以下是代码层面的成本结构，**不是未经实测的纳秒排名**。CPU 架构、缓存行归属、活跃消费者数量、读取频率和线程迁移都影响常数项。

| 信号/方法 | 每次读取成本结构 | 为获取它新增的任务路径写入 | 准确表示的对象/优点 | 盲区或误差 | 推荐位置 |
|---|---|---|---|---|---|
| 原生 `getTaskQueueSize()` | O(1)，接口转发+虚调用+一个 atomic load/位解码；默认 seq_cst，经典 acquire | 无新增统计 RMW；队列本来就维护 semaphore | 廉价可用的未领取工作计数；很适合首先验证 | 协议窗口、经典 handoff；不含已开始长任务；不是工作量 | 提交点观测/候选池快照 |
| `getPendingTaskCount()` | O(1) 主体，另有共享锁 | 无新增统计写入 | 同上 | 额外锁；含义并未变成严格未开始数 | 兼容通用接口时使用 |
| 包装器 `pending` | 一个 relaxed atomic load | 每任务至少预留+开始两次计数更新，提交失败/取消补偿 | 预留至包装回调开始的任务数；覆盖领取后未开始窗口 | 仍不包含已开始但在内核等待的回调；预留窗口不是物理队列 | 阶段 3 验证 native 的遗漏是否重要 |
| 包装器 `executing` | 一个 relaxed atomic load | 每回调开始+结束两次更新 | 已进入回调、尚未返回的任务数 | 包括 on-CPU、runnable、阻塞；不区分长短任务 | 与 queue 联合判断过量并发 |
| 包装器 `outstanding` | 一个 relaxed atomic load | 每任务预留+完成两次更新，失败补偿 | 尚未完成的所有已接纳 CPU 回调 | 不能直接区分排队/执行/阻塞；一个长任务和短任务等价 | 最小的 queue 补充项/容量控制 |
| `getPoolStatsLight()` | O(T) 扫线程 idle，持共享锁；T=池线程数 | 利用 Folly 已有 idle 更新 | 无需自己加执行中计数 | 扫描几百线程会读取多条缓存行；idle 状态盲区如上 | 低频监控，不建议每次路由扫描 |
| `getPoolStats()` | O(T)+共享锁+更多统计与时钟 | 同上 | 丰富监控快照 | 不是事务快照，通常比 Light 做更多工作 | 低频监控 |
| 估计未完成 CPU 工作量 | 若维护总量，O(1) atomic load；若临时遍历则不适合热路径 | 提交/完成时加减估计；估计学习另计成本 | 随机时长/长尾任务下比计数更有信息 | 估计可能错误；完整预算保留到完成会高估已做部分；同步阻塞混入也会误判 | 计数预测不足后再比较 |
| `getUsedCpuTime()` 差分 | 共享锁、遍历线程并获取 CPU 时钟，O(T) | 无需每任务 CPU 计时 | 池实际消耗的历史 CPU，含线程池自身开销 | CPU 高不区分正在积压还是刚好满载；系统支持依赖 | 100 ms–1 s 起步的低频采样 |
| 每回调 thread CPU/wall 计时 | 每次被测回调多个时钟读取+记录 | owner 独占记录可免共享 RMW；汇总/日志另计 | 算子 CPU 分布、wall 与 CPU 区别 | 对短任务扰动大；CPU 时钟不保证都是廉价 vDSO；wall−CPU 不是纯 runqueue | 抽样或独立实验 |
| `/proc/.../schedstat` | O(T) 文件读取/解析，用户内核切换 | 无应用任务级插桩；内核计量本身有成本 | 每 TID 累计运行和 runnable 等待 | 需配置/启用；线程创建退出；无 request 归属 | 外部低频诊断 |
| `sched_wakeup`/`sched_switch` 追踪 | 按事件产生、缓冲和后处理 | 调度路径追踪开销；事件量可很大 | 可区分唤醒后等待与执行中再次排队 | 丢事件、权限、扰动；必须映射 TID/任务时间 | 独立短诊断窗口 |

“低读成本”与“低总成本”必须分开：维护一个 O(1) counter 的读很便宜，但所有生产者/消费者共同更新同一缓存行的 RMW 可能引起竞争。另一方面，native queue 没有新增计数写入，也不等于读取它完全没有缓存一致性成本。

所有共享计数都要处理成功接纳、回调开始、完成、异常、过期、取消和提交失败的完整生命周期。用于容量上限和对象存活的计数必须有相应正确性保证；不能拿若干近似读数拼成一个必须精确的 admission 条件。

## 9. 最小接入方式和原子操作问题

### 9.1 首先直接用公开 API，不改 Folly 队列

```cpp
// pool 是实际业务使用的 folly::CPUThreadPoolExecutor。
// 在 add 前读取：这是新任务进入前的 native queue 观测值。
const size_t nativeQueueBefore = pool.getTaskQueueSize();
pool.add([nativeQueueBefore, task = std::move(task)]() mutable {
  // 将样本写入预分配的任务记录或线程独占统计；不要每任务同步刷日志。
  task();
});
```

上例展示读取位置和数据携带；执行期间 other producers 可以立刻改变队列，因此它是一次路由前观测，而不是保证该任务前面恰有这么多项。在生产者高并发时，不同提交者可同时看到同样的最小值，导致集中选池；队列读数准确也不能自动解决路由振荡。

仅采样时可减少到每 N 次提交或后台每隔 Δ 发布，但要记录采样周期；后台快照可能漏掉 30 ms burst。路由使用旧快照和观测使用同一任务提交点快照，应标为不同实验条件。

### 9.2 能否为了允许误差而不用原子

不能在一个线程修改普通 C++ 整数时，让另一个线程无同步读取；这是数据竞争，而不是可控的误差。`volatile` 也不提供所需同步。合理选择包括：

1. 直接读 Folly 的合法公开接口；底层已经有原子状态，**不需要再添加一份队列计数**。
2. 自建统计使用 `atomic.load(memory_order_relaxed)`；只要求观察一个计数而不发布其他数据时，不必施加更强的跨对象顺序。
3. 高频写入采用每线程独占计数，再批量发布到各自原子槽；收集端低频汇总并发布每池快照。成本由“每任务集中 RMW”变成“分片写+周期汇总”，代价是新鲜度。

Relaxed 不等于没有一致性开销；尤其多个核写同一缓存行仍然可能昂贵。跨核读写时，不能用“普通字段前后读版本号”自动消除数据竞争；需要真正的安全发布/生命周期方案。填充缓存行也应基于目标平台，不能假定所有 ARM cache line 都是 64 字节。

### 9.3 总执行时间怎么获取

若目的是监控“池在最近一秒用了多少 CPU”，优先低频读 `getUsedCpuTime()` 做差；它包含 worker 的其他 CPU 成本。若目的是预测不同算子的服务时间，在同一回调内抽样读取线程 CPU 时钟并按算子类别聚合。业务自己的 wall 计时只能得到墙钟，Folly `runTime` 也是墙钟。

若要区分 runnable 等待，需要系统信号。`schedstat` 的三个字段分别为 CPU 运行时间、runqueue 等待时间、运行时间片计数；应做差并核对统计是否支持/启用，不可将不可用的零值解释为没有等待。[内核 schedstat 定义](https://www.kernel.org/doc/html/latest/scheduler/sched-stats.html#proc-pid-schedstat)

## 10. 综合选择：先检验 queue，再按证据增加信息

**建议基线为原生 queue，辅以低频实际 CPU 与时延观测；不要一开始就要求路由热路径查询内核或扫描所有线程。** 阶段 3 固定路由，让不同负载指标在同一批任务上同时接受预测检验，避免“改指标同时改策略”造成混杂。

| 观测到的情况 | 解释候选 | 下一步 |
|---|---|---|
| 线程数接近可用计算并行度；queue 与 submit-to-start 单调相关 | 主要积压留在用户队列 | 保留 queue；只在不同任务时长下检查误差 |
| queue=0 时，pending 仍常较高且 submit-to-start 长 | 经典 handoff、领取后抢占或包装边界影响较大 | 比较 pending 增量预测力与计数写入开销 |
| queue/pending 都低，executing 远超容量、CPU 与 runnable 等待高 | 已开始任务在争取有限 CPU | 先扫线程数；如仍需高线程数，考虑 outstanding/work 辅助 |
| 同一 queue 深度下时延差异主要由长任务决定 | 计数没有区分任务 CPU 代价 | 按时长类别分层，比较 EWMA/固定估计与已知预算上界 |
| executing 高、CPU 与 runnable 等待低 | 回调同步阻塞或其他非 CPU 等待候选 | 查阻塞路径；不要直接把它当 CPU 饱和 |
| 任务等待相关，但请求级 admission queue 几乎总为 0 | 请求自己的 burst 创造了压力 | 阶段 4 增加自身宽度/工作量；不能把无方差的相关系数写成 0 |

原生队列是否“够用”的判断标准，是在目标线程数、任务分布和负载区间内，能否以可接受的预测误差区分拥塞和非拥塞，而不是它能否覆盖所有系统状态。额外指标应报告加入后误差减少多少，以及付出了多少开销；没有可见增益时保留更简单的 queue 是合理结论。

## 11. 成本测量与准确性测量必须分成两项实验

### 11.1 成本微基准

[独立指标工程](../../experiments/metrics/README.md)比较空闲/活跃池中 `getTaskQueueSize()`、`getPendingTaskCount()`、完整/Light stats、CPU 时间接口和原子快照读取；另比较集中 relaxed 更新与填充分片更新。其 [结果](../../experiments/metrics/RESULTS.md)须保留 CPU 架构、线程数、调用次数、测量口径和环境。

重复调用得到的批量平均 ns/call 不是线上每次调用的 p99，也不完整代表频繁跨 NUMA 读取的成本。孤立原子计数基准也不等于实际闭包生命周期的全部统计开销。应另外以相同工作集合比较关/开观测模式的吞吐、CPU/任务和整体时延，并避免把文件输出开销计入某一个 queue API。

### 11.2 预测准确性实验

阶段 3 使用独立随机时长任务，扫描线程数、任务到达率、burst 与服务时间分布。至少保留：提交前 native queue/pending/outstanding、任务开始/完成、线程 CPU/墙钟、每秒与全运行平均/p99、拒绝和未完成数。

应分别检验：

- `queue at submit → submit-to-start`：队列信号是否预测等待。
- `queue + service class → end-to-end task latency`：剔除自身服务时间差异后是否仍有信息。
- queue=0 且高时延样本的比例与成因：区分队列边界、已开始并发和自身长任务。
- 加入 pending/outstanding/work 后的预测误差变化与运行成本变化。

按运行、线程数、到达率、任务时长分层，保留负载分桶的样本数/均值/p99。相关系数不是因果关系；过载期间队列与时延共同随时间增长可能造成很高相关，稳定段和积压增长段应分开解释。每秒样本少时 p99 接近最大值，正式比较用全体任务样本和多次独立重复，不能平均每秒 p99。

## 12. 本阶段交付与结论边界

本报告给出实际源码调用关系、默认/经典信号量差异、Folly 队列与内核 runqueue 的严格区分、接口含义和计量成本结构。成本工程独立于简单任务实验和复杂 DAG 工程；复杂分组执行器不是获取原生 queue 的前提。

最终建议是：**首先用 `getTaskQueueSize()` 做低成本基线，明确它是当前队列实现的 native size；在真实线程数和随机时长负载中验证预测力。只有数据证明它漏掉了重要拥塞信息，才引入最小的补充计数/工作量估计；CPU 与内核调度观测放在低频或独立诊断路径。** 这既保留队列方案的低成本优势，也避免把它误称为全部 CPU 负载或内核 runnable 数。

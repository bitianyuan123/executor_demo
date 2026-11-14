# Folly CPUThreadPoolExecutor：实际代码、唤醒与计量

> 历史版说明：本文对应首轮工程与结果。重新分阶段后的独立报告和当前实现以[阶段文档](../stages/02-folly/README.md)为准；旧实验数据仍保留用于复核。

分析固定到 `v2026.10.26.00`，提交 `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。下文链接采用该提交，避免主干更新改变语义。上线前必须对照业务实际版本、构造函数、传入队列、gflags 和 wakeUpInterval；“Folly CPU 线程池都如此”的泛化不成立。该版本 upstream 链接已核验，仓库现重定向到 `meta/folly`。

## 1. 先回答三个关键问题

1. 标准 CPUThreadPoolExecutor 的任务先写入队列，再 post semaphore。所谓精准唤醒是通知某个 waiter，**不是把任务绕过队列直接塞进该线程**。任务闭包及 RequestContext 随队列元素传递。
2. 唤醒与获得 CPU 是两个阶段。waiter 从 futex 等待变成 runnable 后，可以在内核 runqueue 上等待；Folly 不会因为唤醒完成就知道它是否正在运行。
3. 默认队列的 `size()` 实际返回 semaphore 的 `valueGuess()`。经典 LifoSem 已把 permit 交给 waiter 时，任务可能仍未从物理队列取走，但值已经为零。因而 native queue depth 不能单独衡量“尚未开始工作”。

这些结论直接来自 [UnboundedBlockingQueue.h:32–49](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/task_queue/UnboundedBlockingQueue.h#L32-L49)。自定义 BlockingQueue 可以有别的实现，本结论不能未经检查推广到任何第三方队列。

## 2. 提交调用链及上下文

```text
业务 DAG: 某节点依赖计数降为 0
  -> 选择 executor；构造捕获请求/算子数据的 Func
  -> CPUThreadPoolExecutor::add / addWithPriority
  -> CPUTask(Func, RequestContext::saveContext(), expiration, ...)
  -> addImpl
       queue observer onEnqueued + registerTaskEnqueue
       必要时持 KeepAlive，以安全处理后续动态线程创建
       enqueueTask
         -> taskQueue_->add / addWithPriority
         -> 底层并发队列 enqueue(move(task))
         -> semaphore.post()
       若可能扩线程且 !reusedThread -> ensureActiveThreads()
```

[add 的任务构造](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L257-L295)、[addImpl 与动态线程处理](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.h#L208-L237)、[ensureActiveThreads](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.cpp#L659-L686)。

队列负责发布 `Func` 对象，`Func` 捕获值、智能指针或业务数据句柄；semaphore 负责可消费工作的计数/交接和等待通知。共享指针只保证相应对象的存活，不自动保证其中可变业务数据的线程安全。`RequestContext` 的保存/恢复也不复制一个线程的全部 TLS、栈或寄存器。若业务 requestID 仅在自定义 TLS 中，必须显式携带或接入 RequestContext，不能假定跨线程自然传播。

任务创建时记录 `enqueueTime_`，因此 Folly 的 waitTime 从构造任务开始，稍早于真正 queue.enqueue。进入 `runTask` 后用 `RequestContextScopeGuard` 恢复上下文，执行 observer 与闭包，结束后恢复原上下文。该版本 taskDequeued/taskProcessed observer 也在任务 RequestContext 下执行。[Task 与 runTask](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.cpp#L84-L196)

`BlockingQueueAddResult::reusedThread` 用于动态线程数量判断：它并不承诺任务立即运行，也不携带哪个 TID 执行哪个 task 的关系。[返回值定义](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/task_queue/BlockingQueue.h#L39-L53)

## 3. 队列默认值有版本差异

在本提交，`folly_cputhreadpoolexecutor_use_throttled_lifo_sem` 默认 `true`，默认队列为 `UnboundedBlockingQueue<CPUTask, ThrottledLifoSem>`。显式 `makeLifoSemQueue()` 则使用经典 LifoSem。优先级版本使用多优先级并发队列加同一个 semaphore，worker 优先取高优先级任务，线程自身的 Linux priority 并未因此改变。[默认队列工厂](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L33-L137)、[优先级队列](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/task_queue/PriorityUnboundedBlockingQueue.h#L48-L96)

**该版本工厂的 `wakeUpInterval` 默认参数为 `{}`，即 0 ns。** 不应因类名 ThrottledLifoSem 就断言生产默认每次人为等待若干微秒。需要显式配置非零间隔才有相应间隔节流；即使为零，其 waking chain 与经典 LifoSem 的交接算法仍不同。[工厂声明](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.h#L82-L97)

## 4. 经典 LifoSem：给 waiter 一个 permit

生产者写入任务后调用 `LifoSemBase::post()`，内部 `incrOrPop(1)` 用原子状态判断：

- 有等待节点：从等待栈弹出一个节点，对它的 handoff 对象 `post()`，返回 true。该 permit 已交给该 waiter，公共 value 不增加。
- 没有等待节点：增加公共 value，返回 false。之后已有活跃消费者调用 wait 时可以直接扣减该 value，无需睡眠/唤醒。

消费者 `wait()` -> `try_wait_until(infinite)` 先 `tryWait()` 快速拿 permit；失败则分配等待节点，`tryWaitOrPush` 再原子地检查/入栈以避免丢唤醒，必要时等节点的 handoff。定时等待还处理超时与 post 同时发生的删除竞态。[post](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L372-L409)、[wait](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L494-L564)、[incrOrPop](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L676-L714)

“精准”指用户态已选定一个 waiter，通知它专属等待地址；不是内核按 requestID 搜索最亲和线程，也不是任务与 waiter 永久绑定。LIFO 主要复用最近进入等待的线程，以减少无谓唤醒和保留热状态；它不保证请求数据恰好在该线程的 L1/L2，也不保证线程公平性。LIFO 描述等待线程顺序，任务元素顺序由并发队列、优先级和入队顺序决定；不能据此推断任务也按 LIFO 执行。

## 5. ThrottledLifoSem：waking chain 与竞争消费

`postImpl` 总是先把 value 加入共享状态，并检查等待数和 `kWakingBit`；有必要时 `maybeStartWakingChain` 从 waiter 链尾取一个节点并通知。在同一时刻最多一个 waiter 负责推进 waking chain。

被唤醒的 waiter 按 `lastWakeup + wakeUpInterval` 检查是否需要短暂睡眠，然后尝试扣减 value；若仍有工作，交接 waking 状态并通知下一个 waiter；若活跃消费者已经取尽 value，当前 waiter 可能重新等待。这是有意允许活跃线程先处理短任务，以避免为微小 burst 唤醒过多线程。[状态与 postImpl](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L113-L219)、[等待与链式唤醒](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L226-L350)

这里 `post()` 的 true 表示等待者数量足以消费更新后的值，甚至不保证已经立即通知某一个线程。不要把它按经典 LifoSem 的“已直接 handoff 一个 permit”解释。对 100 ms 级 CPU 任务，非零 wakeUpInterval 通常不是主要服务量，但大量线程链式唤醒、内核调度和 burst 首段仍应实测；不能凭量级比较排除 p99 影响。

## 6. 到 Linux futex 的路径：本版常规任务不是 Baton

本版本 `LifoSemImpl` 默认 Handoff 是 `SaturatingSemaphore<true, Atom>`；ThrottledLifoSem::Waiter 也持有 SaturatingSemaphore。网上较旧文章的 `LifoSem -> Baton` 不能原样套到本提交。`CPUThreadPoolExecutor::threadRun` 开头确实有 initBaton/readyBaton，但它们是线程启动握手，不是每个任务的常规唤醒通路。[LifoSem 默认模板](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/LifoSem.h#L39-L42)、[Throttled waiter](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L154-L157)

```text
单个 waiter 的 SaturatingSemaphore::post
  -> 状态 NOTREADY -> READY：无需 futex syscall
  -> 若已 BLOCKED：设 READY -> detail::futexWake
  -> futexWakeImpl -> nativeFutexWake
  -> Linux futex(FUTEX_WAKE_BITSET | FUTEX_PRIVATE_FLAG, waiter 地址, ...)
  -> 内核使等待 task 可运行 -> 调度器择机让其执行

waiter 等待
  -> ready 快路径 / 有界 spin
  -> 状态登记 BLOCKED
  -> MemoryIdler::futexWaitUntil -> detail futex 等待实现
  -> 返回后重新检查状态，处理超时/虚假唤醒
```

[semaphore post/wait 实现](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/SaturatingSemaphore.h#L148-L329)、[Linux futex wake](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/detail/Futex.cpp#L61-L78)、[Linux 与 emulation 分支](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/detail/Futex.cpp#L232-L258)

专属等待对象通常只有对应的那个 waiter，所以即使底层 SaturatingSemaphore 的 API 可以唤醒同一地址上的多个等待者，也不意味着线程池每次广播给所有工作线程。线程尚在 spin 或通知先于阻塞时，完全可能没有 futex wake 系统调用。

### 6.1 内核中还有一次 CPU 选择和调度

作为可核查示例，Linux **v6.12** 的普通非 PI futex 唤醒可沿以下路径阅读；生产内核及厂商补丁须另行核验，并非所有分支每次都会经过：

```text
futex_wake: 匹配 futex 地址/bitset、摘等待者并建立 wake_q
  -> wake_up_q -> wake_up_process -> try_to_wake_up
  -> select_task_rq -> 调度类 select_task_rq_fair（普通 fair 线程）
  -> ttwu_queue -> 本地入队或远端 wakelist
  -> ttwu_do_activate -> activate_task + wakeup_preempt
  -> 调度器后来选中该 task，才发生真正 switch-in
```

[futex 等待者匹配和 wake_q](https://github.com/torvalds/linux/blob/v6.12/kernel/futex/waitwake.c#L155-L200)、[wake_up_q](https://github.com/torvalds/linux/blob/v6.12/kernel/sched/core.c#L1051-L1069)、[try_to_wake_up 的选 CPU 分支](https://github.com/torvalds/linux/blob/v6.12/kernel/sched/core.c#L4264-L4281)、[activate 与抢占检查](https://github.com/torvalds/linux/blob/v6.12/kernel/sched/core.c#L3658-L3683)。

fair 类的唤醒放置会考虑允许的 CPU、此前运行 CPU、唤醒方与调度域、idle sibling 等信息；EAS 是否参与取决于配置与状态。`wake_affine` 是内核的线程唤醒局部性启发式，不知道业务 home/requestID。亲和 mask 缩小后，选择范围随之缩小；远处 CPU 空闲并不能让被硬绑线程在那里执行。唤醒成功及抢占检查都不承诺立即运行。[v6.12 select_task_rq_fair](https://github.com/torvalds/linux/blob/v6.12/kernel/sched/fair.c#L8589-L8655)

## 7. 主动取任务、spin 和“抢任务”

worker 每次运行完闭包就回到 `while` 循环，调用 `take()` 或 `try_take_for(threadTimeout)`。队列 `take()` 先等 permit，再 `dequeue()`；有 permit 时直接进入下一任务，没有则有界 spin/休眠。它不是“每隔固定周期扫描所有池”，也不是永久忙轮询任务队列。threadTimeout 主要用于动态缩池，不能当作正常任务发现周期。[threadRun](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/CPUThreadPoolExecutor.cpp#L341-L392)

本版 `WaitOptions` 默认 spin 最大值为 2 µs；SaturatingSemaphore 文件的旧注释仍写 10 µs，应以实际常量为准。ThrottledLifoSem 的入口先 spin 消费共享 value，但进入 waiter 专属通知等待时显式设 spin 为零，少量超时竞态恢复路径又可用默认 spin。[实际默认值](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/WaitOptions.h#L33-L71)、[Throttled 等待选项](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/synchronization/ThrottledLifoSem.h#L113-L135)

两个消费者可以并发从同一个 MPMC 队列取任务，包含“刚完成任务的线程”和“刚被唤醒的线程”。但必须区分两种竞争：

- **经典 LifoSem permit**：如果唯一一次 post 已 handoff 给 waiter A，活跃线程 B 不能凭空消费这个已分配 permit。若 B 有另一个 permit，A/B 才可能并发 dequeue；谁取得哪一个具体任务由队列协议决定，permit 不绑定某个闭包。
- **ThrottledLifoSem value**：活跃 B 可以先扣减共享 value，A 起来后发现没有 value 再次等待，这是算法允许的正常行为。

有界 `LifoSemMPMCQueue::take()` 在拿到 permit 后还会循环 `readIfNotEmpty`，默认 unbounded 版本使用自身 dequeue 协议。不能看到一个内部重试循环就宣称所有池都在持续轮询。所有合法消费者都受队列与 semaphore 协议约束，任务不应重复执行。[有界队列实现](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/task_queue/LifoSemMPMCQueue.h#L37-L71)

## 8. API 到底测到了什么

| API/字段 | 本提交实现 | 路由时注意 |
|---|---|---|
| `getTaskQueueSize()` | 直接调用 taskQueue_->size | 廉价但按实际 queue 定义；默认为 permit guess |
| `getPendingTaskCount()` | 拿 threadList shared lock 后调用队列 size | 仍不是全部未开始任务 |
| `getPoolStats()` | shared lock、扫描线程 idle 标志、CPU 池 queue size 等 | O(线程数)，不是热路径免费查询 |
| `getPoolStatsLight()` | 不算 maxIdleTime 等，但仍拿锁并扫描线程 | “Light”不代表 O(1) 或无锁 |
| `PoolStats.activeThreadCount` | 存活 activeThreads 扣 idleAlive | busy callback 近似值，不是 runnable/on-CPU |
| `numActiveThreads()` | 动态线程管理中的实际存活线程数 | 名称容易误读成正在做计算的线程数 |
| `getUsedCpuTime()` | 汇总池线程 CPU 时间，含已退出线程记账 | 可能昂贵，适合低频采样；平台不支持时为 0 |
| `TaskObserver` | enqueue/dequeue/processed 回调 | 回调发生在提交/执行路径，必须短且不能阻塞 |

[getPoolStatsImpl 与 pending](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.cpp#L420-L479)、[CPU 时间和公开接口](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.h#L100-L164)、[TaskObserver API](https://github.com/meta/folly/blob/c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c/folly/executors/ThreadPoolExecutor.h#L199-L237)

`thread->idle` 在 `runTask` 开头置 false，结尾置 true。线程已被 futex 唤醒、尚未得到 CPU 或尚未进入 runTask 时仍可能显示 idle；进入 runTask 后被抢占、卡锁或同步 RPC 时又一直显示 active。这两种情况说明 active 不能解决内核 runqueue 的观测问题。

Folly `TaskStats::runTime`/`ProcessedTaskInfo::runTime` 是 steady_clock 墙钟差，含调用中被抢占/阻塞，**不是线程 CPU 时间**；waitTime 也包含提交到起跑的多阶段开销。无需修改 Folly 即可用 Func 包装器或 TaskObserver 做低频/抽样计量；要识别内核时间则必须补系统信号。

## 9. 用户态与内核联合观测

建议常态每池 100 ms–1 s 采样计数、任务/请求直方图与 CPU 时间；只在短诊断窗口打开调度事件，记录 TID 到 pool 的映射。

`/proc/PID/task/TID/schedstat` 的三个字段是 CPU 运行时间、runqueue 等待时间、运行时间片次数。使用相邻采样差值并处理线程创建/退出；缺少内核配置或运行时 schedstats 未启用时，等待字段可能不可用或为零，不能据零值断言没有调度延迟。该统计是 TID 生命周期计量，不自动带 requestID；可以与包装器 task 开始/结束的 TID、时间戳关联。[内核 schedstat 文档](https://www.kernel.org/doc/html/latest/scheduler/sched-stats.html#proc-pid-schedstat)

更细分时可采集 `sched_wakeup`/`sched_wakeup_new` 和 `sched_switch`，计算唤醒到首次 switch-in 以及执行中被抢占后再运行的等待。只看 wakeup-to-switch 会遗漏已 runnable 后的再次排队。还需区分睡眠阻塞、cgroup throttle、CPU 迁移，核对 trace 丢事件和 perf 权限。内核可能使用 CFS、EEVDF 或厂商改版，应记录版本，不能默认所有 ARM 机器调度细节一致。[内核 EEVDF 说明](https://docs.kernel.org/scheduler/sched-eevdf.html)

测试时重点找三种形态：`pending 高 + inflight≈容量` 是用户队列积压候选；`pending 低 + inflight 远高于容量 + runqueue wait 高` 是过量 runnable 候选；`inflight 高 + CPU 低 + runqueue wait 低` 更需要查锁、同步下游、内存/page fault 或任务计量边界。这些是诊断线索，不是仅凭三个数作最终归因。

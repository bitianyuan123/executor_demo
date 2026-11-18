# CPU worker busy 计数：现场 AI 的独立实施 Prompt

下面的文本可以单独复制给现场 AI，不要求传递补丁、访问外网或读取此前会话。若只希望先调查，可在文本前追加：**“本次仅执行第1阶段，输出完整文字交接卡。”** 若现场版本已有等价 O(1) 接口，应先核实并复用，不为照搬样例重复实现。

本方案统计的是 **CPU worker 已进入任务处理区间、尚未离开的数量 B**。它不是操作系统的 running＋runnable 线程计数。两者的差异是设计边界，不是后续通过更名或调整阈值就能消除的误差。

```text
你是在真实应用环境工作的工程 AI。请基于现场 Folly 代码，为 CPUThreadPoolExecutor 增加可选、O(1) 读取的 busy 任务计数 B。现场无法连接开发环境，你没有之前的会话，也不能假设可以下载代码或依赖。本文给出完整参考语义和实施步骤；不要因为缺少外部补丁停止调查。

默认完成第1～4阶段；人类若指定某阶段，则只执行该阶段。第5阶段仅修正已发现的问题。每阶段输出完整文字交接卡，在已授权范围内继续工作，不因阶段切换反复请求确认。没有授权不得生产发压、重启或发布。

【业务背景与本次范围】
应用使用多个 Folly CPUThreadPoolExecutor。短时任务扇出较大，worker 数可能远高于绑定的 CPU 数。任务出队后仍可能等待 CPU、被抢占或同步阻塞，因此 Folly 队列深度 Q 不能表示池中全部压力。原有 PoolStats 通常需要锁和线程扫描，若每次选池都读取多个池，读取成本值得审视。

此次只新增 busy 观测接口、默认关闭的配置、必要功能测试和使用说明。不调整池数、worker 数、亲和性、调度策略、阈值、队列类型、负载或 DAG。不要侵入其他 agent 的工作。不开发新的 OS 采集器，不追加性能矩阵，不要求三次重复。不要据增加观测就宣称请求 p99 会改善。

【必须先说明的语义：B 不等于 OS running＋runnable】
B[p] = 池 p 的 worker 当前处在普通任务 runTask 处理区间内的数量。
入队、领取许可、出队以及出队观察器不计入 B；在出队观察器返回后、进入 runTask 前加一，在 runTask 返回或异常退出后减一。排队任务和内部 poison/停止标记不计。回调、过期处理、runTask 内的任务观察器和上下文清理都计入。不要把它缩为只包住 task.func()。

单个 worker 执行普通任务时保持一个 B，无论它此刻正在 CPU 执行、在 OS runq 等待 CPU、被抢占，还是在回调内部同步等待锁/RPC/sleep。B 的单位是这个处理区间，不是 request 数，也不是所有 Future/协程的逻辑未完成量。

两个反例必须出现在设计报告：
1. 一个 worker 在用户回调内阻塞于条件变量：B=1；该线程阻塞期间不是 running/runnable。
2. 一个 worker 被唤醒、已可运行但尚未到 B++ 的代码：B=0；它可能已经是 running/runnable。
因此 B 相对 OS 指标既可能多算，也可能漏算，不能声称固定上界或下界。纯 CPU 回调、前后调度开销较小且同步阻塞很少时，B 才可能成为近似参考；不是普遍等式。线程池内维护的用户态计数无法在线程被 OS 切换为阻塞状态时自动获知该变化。

若需求坚持“真实 OS running＋runnable”，应另列诊断需求：明确仅统计哪些 worker TID、采样/事件时间、线程新建退出和事件丢失如何处理。逐个读取 /proc 的 R 状态也是不同时间的采样，不能声称原子快照；调度事件追踪需额外权限和完整性验证。本轮不扩大为这个工程，也不把 B 命名成 osRunnableCount。

若目标允许“用户态低成本、尽量接近 running＋runnable 的代理指标”，按证据选择：
1. 回调以纯 CPU 计算为主、同步阻塞少：先使用本补丁的 B。它不扫描线程、不读 /proc，但仍保留上面两类差异。
2. 回调存在已知且能统一标记的同步阻塞：后续可评估 B-K。K 表示已经进入 B 区间、此刻位于业务显式标记的同步等待区域内的 worker 数。可设计线程本地 TLS 记录当前池和等待嵌套深度：最外层等待进入时 K++，最外层离开时 K--，嵌套等待不重复扣除。只在已经计入 B 的线程区间使用；等待 guard 必须先于 busy 区间结束而退出。这里是设计选项，本补丁不实现 K，也不自动修改业务等待代码。
3. 采用 K 前先审计等待入口、异常退出、取消、嵌套、线程/协程迁移和 TLS 归属，测算等待边界原子操作与接入成本。未标记的锁等待、同步 I/O、缺页等仍无法识别；显式等待区间也可能包含实际尚未阻塞或已被唤醒的瞬间。因此 B-K 只能称“扣除已知等待后的用户态 busy 代理”，不能称 OS 精确统计。
4. B、K 分别读取不构成一致快照，不能直接以无符号减法制造下溢。若未来确实实现扣除等待的代理 Rhat，可优先设计单原子状态机：进入 runTask 区间 Rhat++；最外层已知等待进入 Rhat--、离开 Rhat++；离开 runTask 区间 Rhat--。TLS 记录当前池、busy 区间和等待嵌套状态，保证一次 worker 最多贡献1，所有正常/异常路径保持平衡，并要求所有等待 guard 先于 runTask 区间退出。Rhat 单次读取消除了 B-K 两个独立快照相减的问题，并未消除与 OS 状态的差异。例如等待线程已被 OS 唤醒、正在 runq 等待恢复执行，但等待 guard 还没有退出，此时 Rhat 仍漏计它。B 必须保留用于观察占用 worker 的总数；若继续导出 K，可将其作为诊断值，不宣称跨计数读取是事务。本轮只说明该后续方案，不实现 Rhat/K，也不引入全局锁。
5. Rhat=0 可能意味着全部 worker 都在同步阻塞，线程池仍没有空闲 worker 领取新任务。因此不能仅凭 Rhat/C 接近0就认定该池可接纳迁入任务；需要保留 B 和 Q/N，以及线程容量/可用性的观察，并在未来单独审计接纳策略。本轮不新增该策略，也不提供默认阈值。
6. 不要误用 Folly 的 ExecutorBlockingGuard：它表示执行器上下文中的阻塞许可/限制或相关上下文信息，并不是“线程现在正在阻塞”的状态探针。必须查现场版本定义；不能通过安装这个 guard 就声称采集到了 OS 阻塞变化，也不能用它自动扣减 K 或 Rhat。

Q+B 包含仍在用户队列内、没有对应 runnable worker 的任务，不能用作 running＋runnable 指标。另行统计“未停放的 worker”也可能把出队/管理工作算入，并仍无法排除其中的同步阻塞；不要未经生命周期与成本审计自动实现。此次仍只交付最小 B 计数。

与其他计数的区别：
- Q：Folly 等待领取/消费的任务或许可积压，具体 size() 语义需查实际队列与信号量；不是内核 runq。
- N：如果现场已有，通常是提交预留后到终结的全部未结束普通任务数，包含排队任务。B 不替代 N；不要重复实现 N 或把两者相加。
- numActiveThreads：在参考版本表示已激活的 worker 数，包含空闲线程，不是 busy。
- PoolStats 中非 idle 线程数：稳定状态下接近 B，但扫描读取不一致且代码边界略有差异。
- Q+B：在稳定状态下接近现有 totalTaskCount 的含义，仍可能漏掉出队到进入 runTask 等窗口，两个独立读取也不是一致快照，不能宣称它严格等于 N。
- B/C：C 必须说明是逻辑 CPU 数还是另行标定的有效容量；该比值不是 CPU 利用率，可大于1。此次优先提供原始 B，不硬编码容量和调度阈值。

【参考版本、接口与代价】
参考基线：Folly tag v2026.10.26.00，commit c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c。现场版本可以不同，不要求升级或下载这个版本。应按真实函数职责适配，不能按行号粘贴。

拟新增接口，不能假装 stock Folly 已经提供：
  CPUThreadPoolExecutor::Options().setBusyCounting(true)
  folly::Optional<size_t> CPUThreadPoolExecutor::getBusyTaskCount() const noexcept

默认未启用时 getter 返回空；启用而当前无 busy 工作时返回0。可用现场适配的 std::optional 代替 folly::Optional，但所有调用方必须区分“不可用”和0。禁止使用 value_or(0) 把不可用解释为空闲。仅在构造时设置，不实现在线任意切换，否则可能在已进入和退出任务之间产生不配对计数。

实现限制：仅 CPUThreadPoolExecutor 每池一个 atomic<size_t> 计数和构造时固定的启用状态；不加每任务字段，不改 CPUTask，不引入 shared_ptr、不分配每任务状态、不改通用父类的所有 executor。一次读取为一次原子 load，启用时每个处理区间加、减各一次原子 RMW；关闭路径保留必要分支成本。数值只作观测，使用 memory_order_relaxed；这不等于无竞争或无缓存行成本。多核共享计数可造成缓存行争用，需要有测量才能评价。普通非原子变量的并发访问不能因“容许误差”而使用。

即使不改 CPUTask，新增 Options 字段和 executor 成员也会影响公开类型布局、ABI 或构造方式；必须协调重编 Folly 及相关消费者，不能把新头文件与旧库混用。

【第1阶段：探索实际实现，先不推断版本相同】
1. 读取本地仓库指导文件与 git 状态，记录其他人的变更并隔离本次工作。确定应用/Folly 源码版本、应用编译时的真实 include 路径、链接库位置及构建配置。不能仅从当前源码目录的 tag 推断运行版本。
2. 找出 CPUThreadPoolExecutor 的所有构造函数、Options 传递链和可能的 aggregate 初始化。确认新增选项在默认与自定义队列构造路径均可传递，不改变旧默认行为。
3. 沿 threadRun 或现场等价 worker 循环，画出：取许可→出队→poison 分支→QueueObserver::onDequeued→runTask→停止判断→下一次取任务。查清 runTask 定义与所有调用点，确认是否存在另一条普通任务处理路径。
4. 查 runTask 内 idle 标志更新、TaskObserver 回调、用户回调、异常处理、expiration/expireCallback、RequestContext 恢复和局部对象析构。明确每一步属于 runTask 内还是外，不因函数同名就假定职责相同。
5. 查 stop/join/缩容/销毁与 worker 生命周期，确认计数成员在所有 worker 的 RAII 退出前仍存活。沿 KeepAlive 和 add 生命周期检查，不修改它们。检查异常路径是否可能绕过预计退出点；遵守现有 noexcept 和异常策略。
6. 查队列实际类型、size()、信号量、getPoolStats/getPoolStatsLight/getPendingTaskCount/numActiveThreads 的具体实现，记录 Q、idle、active 的含义与成本。若已有满足语义的 O(1) busy 接口，优先复用并说明覆盖差异。
7. 列出所有覆盖/不覆盖情况，尤其是同步阻塞、内联执行、自定义 worker 循环、协程/Future 是否真正经该 CPU 池排队。审计已知阻塞入口与潜在隐式阻塞，说明纯 B 是否足够、B-K 是否仅值得后续评估；本轮不接入 K 或“未停放 worker”计数。不要把一个 CPU worker 内部 inline 执行的多项函数额外记作多个 busy worker。

交付：真实来源与路径、调用流程、版本差异、计数生命周期、最小改动文件清单、尚未核实的问题。只执行本阶段时，给出足够完整的文字交接卡，后续 AI 无需前文即可实施。

【第2阶段：设计最小改动】
参考修改位置：
  folly/executors/CPUThreadPoolExecutor.h
    Options 增加默认 false 的 busyCounting 及 fluent setter；
    声明可选 getter；新增构造后不变的启用状态和每池 atomic<size_t>{0}。
  folly/executors/CPUThreadPoolExecutor.cpp
    统一主构造函数接收启用状态；实现 getter；
    只在普通任务分支、出队观察器返回后的 runTask 外围增加作用域计数。
  对应构建元数据
    若公开头文件新增 Optional 依赖，核对实际构建系统的公开/导出依赖声明。
  必要定向测试与说明文件
    不修改通用 ThreadPoolExecutor 语义，不修改 CPUTask、queue/sem 行为。

以下伪代码只表达位置和异常安全，需转换为现场可编译代码：

class CPUThreadPoolExecutor:
    const bool busyCountingEnabled
    atomic<size_t> busyCount = 0

getBusyTaskCount() noexcept:
    if not busyCountingEnabled: return unavailable
    return busyCount.load(relaxed)

threadRun(...):
    while existing_worker_loop_condition:
        task = existing_wait_and_take()
        if existing_poison_test(task):
            existing_poison_path()
            continue_or_return_as_before

        existing_queue_observer_onDequeued(task)
        {
            counted = busyCountingEnabled
            if counted:
                busyCount.fetch_add(1, relaxed)
            guard = scope_exit(noexcept {
                if counted:
                    busyCount.fetch_sub(1, relaxed)
            })
            existing_runTask(thread, move(task))
        }  // guard 在这里执行；不可拖到下一次阻塞取任务
        existing_stop_or_shrink_check()

约束：
- guard 必须在本版本保证不抛异常的方式下构造，且在 B++ 后到 runTask 调用间不能插入会抛异常却未被 guard 覆盖的操作。
- 不仅在正常 return 后手动 B--，要由 RAII 覆盖原有异常退出。
- 统计范围包含 runTask 的所有执行和清理，不包含 QueueObserver::onDequeued 及后续取任务等待。如果现场职责位置不同，说明精确适配位置和额外覆盖区间。
- 不改 idle 标志；B 与扫描 idle 只要求稳定检查点一致，不能要求任意并发瞬间一致。
- 不把取消请求、超时标记本身当作减一事件。只有已进入本计数区间的任务离开时减一；从未进入的排队丢弃、拒绝、停止 poison 一律不加不减。
- getter 只是观测。B=0 不证明队列为空、无新提交或所有业务数据已可见，也不能代替 join/latch/内存同步。
- 如果多个线程同时读 B 再决策选池，读数不是接纳预留。本轮不新增路由、CAS 接纳或容量限制。

明确成本与名称：getter O(1) 不代表整体开销为零。统计 B 比扫描 idle 更便于频繁读取，但不能据此断言它比 N 或 Q 更能预测请求 p99。B 的上限取决于实际同时进入该区间的 worker，不可机械用配置 maxThreads 的某个瞬间值断言缩容过程。

【第3阶段：实施与接入】
基于已确认版本实施最小修改，补齐必要 include 和构建公开依赖。新增接口文档必须包含默认关闭/不可用、同步阻塞仍计数、OS 指标不等价、精确进入退出边界及观测非同步保证。
将启用选项显式接到测试或已有业务配置；默认不启用。接入现有低频指标出口时同时导出 available/enabled 状态，避免空值被监控系统转换为0。热路径不新增日志、时钟读取、线程扫描、/proc 或任务分配。
全量重编相关 Folly 和消费者，确认应用实际加载新版本，不能只让独立测试链接正确。保持原有 worker、优先级、过期、RequestContext、KeepAlive、stop/join 行为。

【第4阶段：最小验收】
用 latch/baton/条件变量等可控同步制造稳定检查点，等待设置合理超时并确保失败时释放 worker；不要依赖 sleep 猜测内部时序。只做必要测试及已有相关回归：
1. 默认关闭返回空，显式启用且空闲为0；既有构造方式和默认运行行为不变。
2. 一个 worker 的回调被测试闸门阻塞，另有多个任务排队：B=1。放行并 join 后 B=0。这也直接证明同步阻塞仍计 B，不能称其为 OS runnable。
3. 多个 worker 同时在回调闸门内：稳定检查点 B 等于已进入的 worker 数；队列长度变化不直接改变 B。
4. 正常回调、抛异常回调、快速回调、多生产者交错：结束并同步后 B=0，无下溢泄漏；原异常处理行为不变。
5. 过期任务：验证用户主体未执行，过期回调被闸门阻塞时 B 仍为1，过期处理结束后归零；无过期回调也能结束。原版本不支持的路径明确标“不适用”，不伪称验证。
6. 如现有测试接口允许，验证 runTask 内任务观察器阻塞时仍计 B，QueueObserver::onDequeued 阻塞时尚未计 B；不要仅为了测试扩大公共接口。
7. stop/join/缩容：内部 poison 不加 B；未执行的拒绝或排队丢弃不改 B；已进入区间的工作退出后归零。不得把 stop 发出请求的时点当作工作已经全部结束。
8. 多构造路径、优先级或自定义队列按现场实际使用范围验证；不重写整个 Folly 测试套件。执行现有相关线程池回归，记录命令、退出码、通过/失败/未执行项。

若已有授权且需要了解成本，最多先做一组固定现有工作负载的 busy 观测 off/on 对照。除该选项外，资源、线程数、亲和、策略和输入保持一致。记录吞吐、失败率、请求完成 mean/p99 与现有 CPU 指标；将这称为观测成本检查，不是负载均衡收益实验，不要求三次重复。无授权或无运行条件时只提供命令与待执行项，不能声称成本已验证。

【第5阶段：只修正已发现的问题】
仅在版本不兼容、生命周期/计数测试失败、真实入口覆盖不足或实际开销不满足已有预算时修正。修复后重跑受影响检查。没有既定开销预算就报告数据与不确定性，不虚构验收门槛。不借此探索均衡阈值或追加 OS 采集工程。

【每阶段都输出完整文字交接卡】
请逐项填写实际信息，禁止只写“见前文”“见文件”或依赖外网链接：
目标与边界：仅新增 CPU worker busy 观测 B，不改调度/阈值/线程/亲和/负载。
语义：在出队观察器后、runTask 前进入；runTask 完整退出后离开；同步阻塞计入；排队和 poison 不计；B不等于OS running+runnable。
真实版本与来源：应用/Folly commit或包版本、实际头文件和链接库路径、构建方式。
工作位置：目录、分支、已完成阶段、本次修改文件、其他人的变更归属。
实现摘要：Options/getter名称、启用方式、构造传递、计数位置、RAII和生命周期证明、公开依赖与ABI处理。
现有状态对照：Q/idle/active/totalTaskCount在现场版本的真实含义、成本及与B的差别。
验证证据：实际命令、退出码、通过/失败/未执行项；若有off/on结果，列原始数据和条件，不推断为策略收益。
未决问题：已证实事实与待核实假设分开，明确哪项会阻塞后续修改。
下一步：具体阶段、文件、检查和可运行命令，以及已获授权范围。
复现与回滚：如何重编、启用/关闭、恢复本次变更且不覆盖他人工作；不可将新头文件留给旧二进制。
```

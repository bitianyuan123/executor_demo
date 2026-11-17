# 异步等待与恢复：实现合同及验收边界

4017结束了既有CPU工作族的到达核查。本实现补上“算子释放worker、外部完成后重新进入ready调度”的结构，使用新的`affinity-v3-async`结果合同。原有直接推进仍输出`affinity-v2`，旧结果可继续分析。当前目标是功能验收，尚未建立ARM异步性能排名或生产RPC代表性。执行入口见[异步功能指南](TARGET_V2_ASYNC_FUNCTIONAL_GUIDE.md)。

目标仍为每个已核实硬件cluster一个子池，业务持有总池；request选择home，后续ready callback由总池按策略选池。六池仅属于现有目标机资源，代码不写死四池或六池。

## 1. 已实现的执行与所有权

[AsyncCompletionSource](../experiments/affinity/include/executor_demo/AsyncCompletionSource.h)使用一个辅助线程、单调时钟有序队列、按request索引的取消记录及容量上限。它不执行网络RPC，不为每事件创建线程。插入/移除为O(log pending)，取消设置request标记后由辅助线程逐个消费；已取消request以后登记也会被拒绝。辅助线程继承与driver、worker相同的CPU并集，不额外借用实验外CPU。

[驱动](../experiments/affinity/main.cpp)在选中业务operator完成原有计算/访问后、推进依赖前登记等待：

1. 记录issue与计划due，捕获总池的move-only Continuation。其保存request home、实际source、hops与Folly RequestContext，并维持drain lease。
2. 把token及request引用移交外部完成事件，然后原callback返回。等待期间不占worker，不增加子池的ready任务Q/I/O；request仍存活。
3. 到期时辅助线程使用token恢复一个`rpc_resume`控制callback。它按原有parent/home、fork/all、同NUMA及hop规则选池，执行原本的后继提交或branch完成通知；不重复业务计算。
4. 恢复callback的后继继承其实际执行池。多个完成碰巧同时到期不自动标记为fork；恢复callback为普通后继，真正addBatch时才进入fork规则。

零配置延迟仍经过完成线程与额外callback；它不保证零实际等待。源线程串行提交多个完成，会使实际释放跨度不同于计划网格。源与driver的成本均计入本轮进程CPU，另列辅助线程CPU；详细事件/任务记录受trace开关控制，基础生命周期和统计维护不会因此消失。

## 2. 参数与三个独立合同

| 参数 | 含义与界限 |
|---|---|
| `--async-percent` | 0关闭，1～100按逻辑operator确定性选择；不根据池状态或运行时间选择 |
| `--async-delay-us` | 0～10000000，issue之后的固定最小延迟 |
| `--async-align-us` | 0为独立due；正值至10000000，将最小due向上对齐到run origin的网格 |
| `--async-capacity` | 1～2000000，默认4096；包含待到期及辅助线程正在处理的事件 |

async-percent为0时不能设置非零delay/align。参数是敏感性变量，不是生产默认值；当前没有随机延迟分布、重试、超时竞速或真实服务调用。需要新分布时另立合同，不把固定延迟解释成真实RPC分布。

选择函数为`mix(seed ^ mix(request_id) ^ mix(operator_tag) ^ 0xa510c0de) % 100 < percent`；mix为已有64位SplitMix形式。业务工作缩放使用原有独立公式，等待不改变compute迭代、访问次数、数据依赖或checksum。

| 合同 | 配置 | 可以比较什么 |
|---|---|---|
| D0 | async-percent=0 | 原有直接推进 |
| A0 | percent>0，delay=align=0 | 外部完成机制与额外恢复callback |
| A1 | 相同percent，固定非零delay，align=0 | 在同恢复位置/数量下增加等待 |
| 网格恢复 | 相同percent/delay，align>0 | 单独研究集中恢复，实际源释放跨度另测 |

D0/A0的callback数量不同，A0/A1的实际绝对完成时间也不必相同。完整work_signature包含async合同，普通策略配对只接受相同合同；fixed_work_signature排除async参数，仅用于证明业务计算相同，不能据此自动比较请求时延。

## 3. 任务、事件与计量边界

业务图仍有K个节点，tag为0～K−1。被选operator额外产生`tag=K+operator_tag`的`rpc_resume`，logical_tag指回业务operator。成功request实际callback数为K+其选中位置数，不再假定每request都是K。任务槽位保留2K，逻辑tag稀疏；初始化、join不被选为等待位置。

新schema增加async_contract、planned_resume_counts及planned_resumes，分析器独立重算选择集合。成功request必须完成完整业务tag和计划恢复tag，失败request只允许计划内子集，不能强求成功集合或删除失败。

trace1新增`async.csv`：

| 字段 | 记录位置 |
|---|---|
| request / logical_tag | 逻辑request与发起等待的operator |
| issue_ns | 业务计算/同步阻塞完成后，捕获token前 |
| due_ns | issue加配置延迟，再按可选网格向上取整 |
| fired_ns | 辅助线程进入fire或cancel闭包；登记拒绝时为0 |
| terminal_ns | token.resume返回或token.cancel完成后的事件记录时间 |
| status | resumed、canceled、resume_failed、register_rejected |

恢复任务本身的submitted/start/finish继续在tasks.csv。submitted位于总池选择/预留之后，因此它不包含完整恢复路由成本。source-fired减due仅对到期fire有意义；取消可早于due。terminal可能晚于恢复callback开始、完成甚至request终态，不能要求相反；零延迟恢复也可能与原operator callback收尾交叠。

trace0不产生逐事件/逐任务记录，但保留来源统计与失败守恒。Q/I/O仍是CPU子池中的ready生命周期；外部pending数量是独立指标，不加入CPU繁忙评分。单看request在途数或端到端时长会把外部等待也算进去，不能替代池负载。

## 4. 取消、失败和停止次序

完成源独占已注册事件的fire/cancel执行，回调不持源锁。cancelRequest与登记/dispatch在锁内仲裁；未dispatch时取消获胜，已经dispatch的fire继续执行，绝不重复回调。同一个Continuation不被多个线程并发访问。

源容量满、已关闭或request已取消时，登记返回false，闭包在调用线程销毁；驱动取消token并把request记为async_register_rejected。token.resume返回false或抛异常时记async_resume_failed。request失败通过一次terminal仲裁，并取消其他pending事件；已经恢复的callback检查terminal后可以不再推进图。取消不伪造branch成功或request成功。

正常顺序：停止生成request → 源继续接纳已获准callback新登记的等待 → pool.join → source.stopAfterDrain → 停采样 → 检查存活及守恒 → 写结果。源暂时为空不代表后续callback不会再登记。源callback仍可能持有request引用，因此必须等源线程结束后检查active归零。

异常清理：source.abort关闭登记、取消已有等待并等待当前fire结束 → pool.join → 停采样；已经运行的executor callback再登记会被拒绝并正常终止request。池和driver状态必须活到这些操作完成。源callback自身禁止调用join/stop/abort；析构也必须在外部线程。

最终核对registered=fired+canceled、pending=0、fired=resume_succeeded+resume_failed、rejected=registration_rejected；源未知回调异常、observer异常或request存活不归零均使运行报错。合法故障轮保留请求/事件失败并返回非零，不能进入性能配对。

## 5. 验证范围与剩余工作

组件测试覆盖到期排序、同期限顺序、提前新期限唤醒、禁止提前fire、容量、取消先/完成先、重复取消、取消空request后再登记、异常回调、abort中断长等待及停止并发。与真实RequestExecutor联验等待期间Q/I/O归零但join仍等待、Draining中恢复RequestContext、恢复后再次登记等待及取消释放引用。

小型端到端脚本覆盖D0/A0/A1、网格恢复、trace0/1、50%选择跨策略、parent/home与fork/all、容量拒绝，并核对固定工作、checksum、恢复数量和各分析入口。开发机人工CPU分区只验证功能，不代表ARM cluster、缓存或NUMA性能。

目前没有按目标机生产规模验收异步性能、尾延迟或观测扰动，也未实现网络服务、随机下游分布、重试及独立超时竞争。下一步先在测试机编译并回传小型功能验收；通过后再声明一组固定资源、同NUMA、有界的异步性能合同。CPU族到达核查已由4017完成，不作为继续等待生产trace或扩大原矩阵的理由。

生产接入复用的是总池Continuation契约：实际RPC完成线程恢复token。AsyncCompletionSource是实验中的外部完成模拟器，无需用它替换真实RPC运行时。开发机验证记录见[功能验收摘录](../results/review/async_v3_functional_validation.json)，不包含ARM性能结论。

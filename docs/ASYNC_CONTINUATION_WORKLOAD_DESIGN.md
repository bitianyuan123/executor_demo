# v2异步等待与恢复负载：待实现设计

状态：**本页仅为实现设计，当前affinity demo尚无异步RPC负载或对应CLI。** 不修改现有实验合同，不把同步sleep当异步RPC。目标仍按实际核实的cluster动态建池，池数、CPU编号和线程数均不写死。

## 1. 为什么现在补，而不等待旧trace还原RPC

现有v2在算子内可执行同步sleep，期间callback和worker仍被占用；这只能测阻塞反例。真实RPC可能释放executor worker，随后多个完成事件集中恢复，改变Q/I/O、ready形状和亲和继承。旧sched事件无法识别这些业务边界，不应因此无限推迟该机制覆盖。先实现明确标为敏感性假设的受控模型，生产RPC比例/延迟未知就不称真实回放。

## 2. 已核对的接口与缺口

代码依据：[RequestExecutor.h](../experiments/affinity/include/executor_demo/RequestExecutor.h)、[main.cpp](../experiments/affinity/main.cpp)、[request_executor_test.cpp](../experiments/affinity/tests/request_executor_test.cpp)。

- `captureContinuation()`仅可在本总池callback内调用；保存request、home、实际source、hops及`folly::RequestContext`，并增加排空lease。
- `Continuation`是move-only、一次性token；`resume(task, options)`恢复上下文并通过同一选择/提交路径运行，允许在Draining期间恢复；重复resume返回false。`cancel()`和析构释放lease。**同一个token不能被多个线程并发访问。**
- `join()`关闭新request/外部root提交，等待callback、提交操作和已注册continuation排空；没有独立`stopDrain()`接口。token最终必须resume、cancel或销毁，不能遗留。
- 现有测试`RegisteredRpcRestoresContextAndCompletesDuringDrain`覆盖外部线程恢复、上下文、恢复后子任务及join等待；`CancelAndThrowingCallbacksReleaseDrainLeases`覆盖取消/析构与异常。它们没有验证v2定时源、图完成守恒或生产RPC性能。
- 当前main在发完请求后直接`pool_->join()`，随后停止CPU采样并检查request计数；trace按固定节点数预分配，旧分析器也检查固定tag/kind。新增恢复callback需要连同driver/schema/分析器一起演进。

## 3. 最小实现：外部完成源，worker立即返回

新增独立的`AsyncCompletionSource`（设计名），由一个辅助线程管理单调时钟定时队列及取消命令。它只产生完成事件，不在executor worker上sleep、等待future或忙轮询；不模拟真实网络、序列化和下游计算。先设容量上限、每事件固定小记录和可统计的辅助线程CPU，不能为每个RPC创建一个线程。

初版将等待插在选定业务算子计算/访存完成之后、后继或branch完成通知之前；选择由seed与逻辑request/operator ID决定，与执行池和时间无关。

1. 原算子完成原有固定计算、访问和结果写入，调用`captureContinuation()`；把token、`shared_ptr<RequestState>`、逻辑位置、到期时间和结果类型移交完成源，然后返回。
2. 完成源到期后取出唯一记录，调用`token.resume(resume_control, options)`；该控制callback执行原来的“提交下一stage或递减branch剩余数/发join”逻辑。只有RPC成功后才推进依赖。
3. 源线程不直接执行图计算，不直接使用业务层root `add(request,...)`代替token恢复；后者会丢失实际父池继承及排空能力。
4. 捕获时source是发起等待的实际池；恢复callback按现有parent/home、fork/all、hop及NUMA规则选池，后续子任务继承恢复callback的实际执行池。home仍为原request的home。
5. `resume_control`标为普通后继，**多个完成事件碰巧同时到期不自动等于业务fork**。fork-only下恢复不借出；all下可重新选池。真实恢复会再扇出时，控制callback显式调用addBatch，才进入fork规则。

等待token仅维持总池drain lease及request存活，不能加进某个CPU子池Q/I/O来假装CPU占用。源记录、request、token的所有权必须可追踪；完成后及时移除重复超时记录，避免已完成request仍被辅助结构长期持有。

## 4. 固定业务工作不等于固定callback合同

原业务图的宽/深、依赖、compute迭代、访存次数、数据布局和校验结果保持；初版每个选定等待位置额外产生一个`rpc_resume`控制callback。若原请求K个callback、选中S个位置，全部成功时应为K+S，而非继续宣称旧K不变。控制callback参与路由、计数、CPU开销和hop，可能改变下一次选池；不能将差异全部归因于等待时间。

必须有三个合同：D0原有直接推进；A0相同选择位置＋外部源＋零配置延迟；A1相同选择位置和额外callback＋非零延迟。D0/A0测机制引入差异，A0/A1研究等待/恢复，均记录实际投递延迟；零配置延迟不保证零墙钟延迟。

业务operator tag和工作量hash保持稳定；恢复callback使用独立且唯一的执行标识与`kind=rpc_resume`，记录其所属逻辑operator。不要覆盖原operator追踪行。每request保存计划恢复位置/预期callback集合；选择数不同时不能套用统一固定节点数。

新增明确的schema版本或扩展标记，并同步更新writer、report/diagnose/flow及合同检查：旧文件继续按旧合同读取；新文件未经支持应拒绝而非静默忽略恢复节点。业务operator与控制callback的CPU/wait分别统计，整request仍含两者；故障轮按实际状态守恒，不能强求成功完整tag集合。

## 5. 两类独立恢复形态

| 工作族 | 到期规则与用途 | 必须保留的边界 |
|---|---|---|
| 独立延迟 | 固定、离散长短或有界分布；每位置延迟由独立hash确定，到期为实际issue时间加该延迟 | issue时间受调度影响；相同延迟样本不等于不同策略有相同绝对完成时间 |
| 对齐完成批次 | 在最小等待后对齐到相对run origin的释放网格；同格集中提交恢复 | 模拟外部完成相关性，不声称复现真实RPC；源线程串行投递会拉宽实际恢复跨度 |

RPC选择率、延迟、长尾、释放网格和每批上限均为待实现配置概念，不提供不存在的命令。初期只选少量档，禁止同时改变选择率、延迟形态和路由策略；先独立延迟，再保持业务工作研究对齐恢复。配置由未来小规模校准确定，不继承0.85ms线程周期或3ms门控。

每事件至少记录逻辑ID、issue、planned-due、source-fired、resume-submit及callback-start/finish；定义时钟与标记位置，分开源调度lag、恢复投递跨度及executor等待。记录计划延迟、实际等待、源队列峰值/拒绝、恢复数/取消数、辅助CPU和观测开关；trace关闭时没有逐事件时序不能补0。

## 6. 完成、失败、取消与停止顺序

完成源单线程拥有token；外部取消只投递控制命令，不能与resume并发操作同一token。每事件只允许`pending→resumed`或`pending→canceled/failed`之一；先决出唯一终态再移走token。`resume`返回false或抛异常均结束该事件并终止/标错request，不能丢掉失败继续发join。

注册容量不足或抛异常时，未转移成功的token在原调用方取消/析构，request标为注册失败。失败request通过现有terminal恰好一次仲裁后，请求完成源取消其余pending事件；已恢复callback也检查terminal。取消路径释放token而不递减成功branch计数，不伪造成功完成。

正常排空顺序：停止外部request生产 → **完成源保持运行且仍接纳已获准callback产生的新等待** → 非executor线程调用`pool.join()` → 源quiesce/stop并清理记录 → 停CPU采样 → 检查request存活和守恒 → 结束CPU计时/写报告。不能先停源再join，否则持有token可永久阻塞排空；也不能仅等源暂时为空就认为未来不会再注册。

异常停止采用单独abort模式：先禁止新request，令源拒绝后续注册并取消全部pending token；每个受影响request必须恰好一次写入取消/失败终态，不能等待已不可能发生的成功join。运行中的callback注册失败走上述终止路径，然后join总池并quiesce源。池对象必须活到所有token消费/取消及源停止之后；当前API无超时强制销毁池能力，不能用析构掩盖卡住的lease。

排空后核对：每事件注册成功数=恢复成功数+取消/失败数；无pending事件或token；所有已接纳callback恰好完成一次；每成功request恰好一次终态、branch/join守恒；子池O/I/pending归零；request存活计数归零。把注册失败、resume失败及控制callback异常分别计数，避免用总失败数掩盖泄漏。

## 7. 低资源功能验收与后续范围

先用可注入单调时钟/人工完成的源测试到期排序、同期限稳定处理、取消先/完成先、重复完成、容量拒绝及异常回滚；再在获准CPU上以极少request/分支验证真实辅助线程，无网络服务依赖、不跑生产规模。

必要联验：没有其他工作时，等待期间子池Q/I/O可降为0而join仍被token保持；恢复在Draining继续并恢复RequestContext/home/source；parent/home与fork/all路径真实触发；request终止竞态不重复join；长等待取消可排空；固定计算/访问/checksum在D0/A0/A1成功轮一致；额外callback及trace开关按新合同计数。

至少验证“队列暂空但仍有执行callback会注册新等待”和“源发出resume后尚未清理request引用”两类停止竞态；对齐批次打印实际释放跨度，不只检查计划时间。源线程、计时与诊断均计入执行CPU，资源预算相同，不悄悄借用额外CPU来改善某策略。

功能完成后仅追加一组有界同NUMA性能对照，固定线程和业务工作；再分别研究RPC比例、独立/对齐恢复及亲和策略。同步sleep反例保留为不同工作族。真实RPC的服务依赖、取消网络请求、重试、限流和回调线程语义仍未模拟；将这些列为适用边界，而不阻塞本设计先覆盖“释放worker后再集中恢复”的基本机制。

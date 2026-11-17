# 阶段4补充：透明请求总池与固定工作量的数据亲和实验（v2）

这是独立、可选的阶段4工程。保留 `experiments/dag`、此前ARM日志和原有命令；v1适合固定CPU预算下研究排队、图依赖和模拟RPC，v2专门检验“借用并行资源的收益能否覆盖数据迁移的代价”。详细审查、算法依据及试验顺序见 [两层调度设计](../../stages/04-dag/TWO_LEVEL_DESIGN.md)。

**主线为每硬件cluster一池**：当前选定六cluster即六池，分区来自已核实拓扑。最新[cluster负载矩阵](../../docs/REAL_WORKLOAD_MATRIX.md)和[观测/算法复核](../../docs/LOAD_OBSERVATION_REVIEW.md)补充`active/cpu`、入口burst、同步阻塞及可注册`LoadProvider/LoadScorer`。四池128线程只属于历史trace与可选重放，none模式的独立软件池数不改变目标设计。CPU采样默认关闭，仅cpu策略或显式`--cpu-sampling 1`开启。

## 接口与实现边界

业务只持有一个 `RequestExecutor`，不创建各个Folly子池、不传home/parent/chosen编号：

```cpp
auto request = executor.beginRequest(requestId); // RR或ColorHash，只决定一次home
executor.add(request, [&] {
  executor.add([] { /* 自动继承请求和实际父位置 */ });
  // fork：vector<folly::Func>，每个闭包仍独立并行执行
  executor.addBatch(makeReadyChildren());
  auto next = executor.captureContinuation();
  // 把move-only的next交给RPC完成/取消所有者：
  // next.resume([] { ... }); 或 next.cancel();
});
executor.join(); // 关闭新root，排空已登记的任务和continuation
```

以上是接口示意；RPC库需要能保存move-only回调，或者自行提供所有权适配。`Continuation`保存调度上下文与Folly RequestContext，只能使用一次。`join()`会等待登记的token最终resume/cancel/析构；没有隐式超时。不能在本池回调调用join，也不能让未登记的外部回调在总池析构后调用成员函数。门面不是 `folly::Executor` 子类，暂不能直接 `.via(&executor)`；拒绝/KeepAlive语义需要专门适配，不能把bool拒绝吞掉。

所有 `add` 使用同一调度入口。`add(request, ...)`是外部root，`add(...)`继承当前上下文，`addBatch(...)`表达fork并逐chunk选池。选池结果仅通过诊断observer输出。API支持完整token生命周期；**本驱动没有模拟RPC延迟**，异步上下文/关闭由单元测试验证，RPC压力仍用v1。

两层逻辑：

1. L1：RR或颜色哈希固定请求home。没有实现LALB、反馈式L1，也没有假定L1与L2应共享评分。
2. L2：`home/queue/active/outstanding/work/cpu`。先查偏好池；超过高阈值后先搜索同NUMA、有足够收益的候选，找不到才按配置考虑远端。无队列偷取、不抢占已运行算子。
3. `continuation=parent`沿实际父池；`home`在home低于低阈值时回迁。这是策略开关，不是已测得的缓存收益模型。join沿最后完成分支的上下文；不是多父数据归属最优化。
4. `balance-at=fork`只在batch做L2；普通链后继保持source，下一次fork才重新评估回home/借出。`all`在每次就绪时评估。
5. chunk预占待提交任务，降低一次批量提交过程中反复把任务投向同一空池的偏差；每个任务仍独立执行。并发选池与预占不是事务，可能短暂同选或重复计量；没有完全避免惊群。

## 工作量与测量

每请求：home上的初始化 → 第1波并行分支链 → join → 第2波分支链 → join。`--widths 20,10 --depths 2,2`为60个算子、2个join、1个初始化，共63个闭包；第一波有20条并发链，每条2个算子。任意数量的波可配置，但不等于任意DAG。

| model | 数据与依赖 | 参数 |
|---|---|---|
| none | 固定计算迭代；无数据数组 | `--accesses 0 --shared-kb 0 --branch-kb 0` |
| request-ro | 同请求只读数组；分支标量仍有前后依赖 | `--shared-kb N --branch-kb 0` |
| branch-rw | 分支独占数组；后继消费前驱改写的数据与标量 | `--shared-kb 0 --branch-kb N` |
| mixed | 请求只读数组+分支读写数组 | 两种大小均大于0 |

`iterations/accesses`固定工作次数，不是在某个CPU时间到期后提前结束。Mixed一次access读两平面并写分支；不同模型之间的访存工作不同，不能把横向差异全归因于调度。各策略同seed、同模型和工作合同必须有相同迭代/读写次数、节点及最终checksum。

匿名mmap不由发生器预触payload；home回调负责第一次写满，再fork。初始化计入请求时延及独立init行。`cluster`下home CPU mask有物理意义；`none`下home逻辑池没有固定内存节点。`--page-sample 1`只对第0请求初始化后查询最多64页，不迁移；权限不足显示unknown/errno，不假装确认NUMA归属。不能据first-touch断言长期page placement，也不把小样本页查询当所有请求的物理验证。

请求按预定时间开放式投递；regular或Poisson到达与策略无关。超过max-inflight拒绝，完整记录并返回非零；不静默等待空槽而降低实际压力。时延从**计划到达**算起，包括发生器落后；排空时不截掉晚完成请求。`requests.csv`可重算不同窗口。

`--jitter-percent 20`对固定工作做80%～120%确定性扰动；`--long-every 20 --long-factor 8`约1/20工作具有8倍倍率。默认 `--work-scale-scope task`按节点扰动。`request`使同请求所有算子共用倍率，模拟不同请求类型。`--estimate-mode constant`给算子同一估计，work在同成本任务下接近outstanding乘常数；**这不是按长短任务准确估计**。只有scope=request可选择`--estimate-mode request-scale`：把已知请求倍率应用于估计；这是受控的估计质量对照，不是在线训练或真实剩余CPU预测。批内共用估计，尚不支持任意不同成本批成员。

详细追踪在任务完成后写预分配行，整轮结束后落盘，无每任务打印/全局日志锁。默认trace=1有两个线程CPU时钟读取、observer/时间戳等开销；`--trace 0`关闭逐任务追踪，只保留请求结果和整进程CPU。底层计数、生命周期及reservation仍有原子更新。**低成本读取不等于已证明整个封装低扰动**；短算子必须做overhead对照。

## 编译和小规模功能验收

在仓库根目录执行，原版Folly安装即可，无需重编Folly：

```bash
BUILD_JOBS=1 ./scripts/build_stage.sh affinity
# 或与根工程一起显式启用：cmake ... -DDEMO_BUILD_AFFINITY=ON

# 默认只选当前allowed mask中前两个CPU，各1个CPU的两个池；不是伪造六cluster
build-affinity/affinity_demo --requests 12 --qps 20 \
  --output results/affinity/local-smoke-01
python3 experiments/affinity/report.py results/affinity/local-smoke-01
```

输出目录不得已有summary/requests；使用新目录避免覆盖证据。`--help`列出所有参数，均需显式值。参数名误拼会报错。线程池析构先drain，再销毁回调状态；内存构造/拒绝等失败返回非零，报告排除失败运行。

## ARM测试第一步：固定工作校准

使用已确认可独占的96个逻辑CPU：`0-15;16-31;32-47;192-207;208-223;224-239`。实际节点从sysfs读取，预期`0,0,0,2,2,2`；不能改成猜测的NUMA 0/1。

驱动会把发生器也限定在这组CPU的并集，新建辅助线程继承该范围；none表示各池共享同一并集，不是放开整机384CPU。每个分区必须在allowed mask中、互不重叠且位于同一真实NUMA；程序不会凭连续编号证明分区是物理cluster，物理cluster划分使用已验证的拓扑。

```bash
build-affinity/affinity_demo \
  --cpus '0-15;16-31;32-47;192-207;208-223;224-239' \
  --affinity cluster --threads 16 --capacity-scale 1 \
  --requests 12 --qps 1 --widths 20,10 --depths 2,2 \
  --model branch-rw --branch-kb 256 --iterations 2000000 --accesses 20000 \
  --policy home --seed 101 --max-inflight 16 --memory-mb 512 \
  --page-sample 1 --output results/affinity/arm-calibration-01

python3 experiments/affinity/report.py results/affinity/arm-calibration-01
```

这不是生产复现参数，2,000,000次迭代不代表固定毫秒数。先回传报告、summary及pages字段；按实测operator CPU/wall调iterations/accesses，使初始fork的CPU需求与目标尖峰在合理数量级，再**冻结工作量**比较所有策略。不要让每个策略自己校准到相同CPU耗时。一次请求60×100ms=6 CPU秒，12QPS即72 CPU秒/秒，必须结合48物理核、SMT实际吞吐与原业务测量口径重新核对，不能盲设。

四种大小先按单变量逐级测：none → request-ro的256KiB/8MiB/64MiB → branch-rw的每分支64KiB/256KiB/4MiB → mixed。默认请求30个branch，256KiB即7.5MiB payload，请求并发16约120MiB；4MiB则每请求120MiB、16请求约1.875GiB。**每请求分支总量、最大驻留请求和共享LLC竞争一起决定局部性**，不能只看单数组能否放入约22.75MiB的cluster LLC。

`memory-mb`是payload+branch状态的预算，不是RSS限制；还需给Folly线程栈、trace、库及OS留余量。trace最多允许200万节点，本身可占数百MiB；4GB开发机只跑小图/少请求/少线程。

## 成对矩阵：先计划，再串行运行

生成器默认只写计划、打印命令。加`--run`才执行；每seed随机交错case顺序、逐个运行并保存run.log。单次超时/非零立即停止，不把缺结果继续纳入对比。使用不同输出根目录重跑。

矩阵metadata保存二进制和核心源码的SHA256、内核、架构及调用者allowed mask；计划记录每轮完整命令。实验期间不要重新编译或替换二进制。

信号/线程24轮已由[4012](../../docs/TARGET_V2_4012_REVIEW.md)完成，4013六轮诊断也已完成，当前入口为[9轮regular守护及覆盖计划](../../docs/TARGET_V2_COVERAGE_NEXT.md)。`signals --thread-counts 16,128`可在策略筛选后展开线程维度，每seed全部case统一打乱；不能同时在base-args指定threads。未使用此选项时原矩阵行为保持不变。

可在`--base-args`之前用`--cases home,outstanding`仅选所选suite中的两个case，例如`--suite routing --cases home,outstanding`。筛选后再逐seed随机排列；未指定时保持完整套件。空、重复或不属于该suite的case会在创建输出前报错，metadata记录`selected_cases`。这适合[4002校准后的有限竞争对照](../../docs/TARGET_V2_4002_REVIEW.md)，不必为验证两个策略执行整个信号矩阵。

```bash
# 将校准后的固定工作量替换下例iterations/accesses；先不要一次跑所有套件。
python3 experiments/affinity/run_matrix.py --suite routing --seeds 101,102,103 \
  --output results/affinity/routing-plan-01 --base-args \
  --cpus '0-15;16-31;32-47;192-207;208-223;224-239' \
  --affinity cluster --threads 16 --requests 240 --qps 12 \
  --widths 20,10 --depths 2,2 --model branch-rw --branch-kb 256 \
  --iterations 2000000 --accesses 20000 --estimate-us 5000 \
  --max-inflight 32 --memory-mb 1024

# 阅读计划确认资源/工作量后，用新output根、在--base-args之前加--run。
# 例如 --output results/affinity/routing-run-01 --run --base-args ...
# 跑完只需打印：
python3 experiments/affinity/report.py results/affinity/routing-run-01/seed-* \
  --warmup-seconds 5 --window-seconds 10
```

| suite | 对照 | 阶段目的 |
|---|---|---|
| deployment | 固定home，none/numa/cluster | 验证同CPU全集下限制并行资源的代价 |
| threads | 固定home，8/16/64/128/256线程/池 | 明确线程过配如何把等待转移到内核；仅目标机跑 |
| routing | home/queue/outstanding/work | 同图、同工作量、同资源下比较指标盲区；默认constant成本下work是退化基线 |
| follow | home；all/fork × parent/home | 二次就绪是否重平衡、链是否回迁 |
| granularity | home；chunk 1/4/8 | 单次决策成本、峰值并行度、预占误差 |
| overhead | home/outstanding × trace 0/1 | 测量详细追踪的增量扰动；非纯门面相对原版Folly成本 |

先在cluster、同NUMA借出下完成routing；再选择有价值策略逐级扫描QPS（如3/6/12，过载时不继续加压）、线程数、分支宽度与深度、足迹。更宽图需同步控制总工作量，区分“相同CPU需求更尖峰”与“总需求也增加”。`--widths 40,10 --depths 1,2`与`20,10/2,2`均60算子，但链复用不同；不是完全相同的局部性负载。

异质成本单独一轮设 `--work-scale-scope request --jitter-percent 20 --long-every 10 --long-factor 8 --estimate-mode request-scale`；再固定同负载改constant验证错误估计是否消除work优势。`--estimate-us`是基准任务估计，不自动从当轮完成时间自适应。

阈值扫描用直接命令或同policy分别运行，单位必须一致：

- queue：`Q/capacity`；outstanding：`未完成数/capacity`。high=1、low=.5只是起点。
- work：`估计CPU us/capacity`，默认high=estimate-us、low=high/2。不是绝对等待时间保证；刚出队与快结束任务仍保留完整估计。
- `--min-gain`、`--cross-numa-penalty`沿用所选score单位。同一数值不能不换单位就跨指标比较。
- `--cross-numa 0`默认只借同NUMA；对比`1`时另行增加罚项。`--max-hops 4`限制每条上下文路径迁移次数，0禁止初始借出。它不等于最短驻留时间。
- `capacity-scale`是每partition逻辑CPU数的缩放，再受线程数上限限制；1不代表SMT两线程等于两独立物理核。none/numa下是**名义公平份额**，各池mask重叠，不是独占容量。none的NUMA标签未知，cross_numa=0不代表物理无跨NUMA。

## 如何从终端得出定量结论

目录包含summary.json、requests.csv、trace=1时tasks.csv；报告按计划到达窗口选request，保留窗口外完成值。自动核对到达/工作合同、节点、校验值、拒绝和CSV完整性。只有同seed、同工作合同、同资源/线程/观测口径的唯一home才能成对；不会把不同模型、CPU绑定或threads混成调度收益。

请求级看mean/p95/p99、生成器落后、peak-inflight；算子级看wait、CPU、wall、迁移、跨NUMA。报告的CPU/wall是闭包口径，含内部提交子任务的时间；并不等于纯内存访问内核的CPU耗时。逐任务`submitted_ns`在选池、预占和诊断读之后、底层`pool.add`之前，至闭包开始的等待不包含完整L2选池耗时，也不能拆解为内核runq时间。request计划到达→完成与进程CPU保留总体开销；需要内核等待归因时另采目标池TID的schedstat/perf，仍按阶段2定义核对。

用独立只读诊断检查home下的负载关系与时间趋势：

```bash
python3 experiments/affinity/diagnose.py results/affinity/routing-run-01/seed-* \
  --warmup-seconds 5 --window-seconds 10 --bin-ms 1000
```

可加`--joint-load`输出同一operator行的Q零/非零 × I/C≤1/>1 × O/C≤1/>1联合分桶；最多八格，缺原始字段不补零，不增加热路径记录。相同行仍不是原子快照，样本比例不是时间繁忙比例。该工具复用报告的输入审计，输出Q/I/O条件task分位、首波投递/启动/callback跨度和请求积压/lag；`stage0_callback_span_ms`从最早callback开始到最晚完成，不从提交起算。它不按每秒p99求总体p99，不将callback并发当成onCPU，也不把排空视为稳态。分桶是投递task采样且目标池已经选定，受选择、批内顺序与任务成本影响；先在home基线解释，再做整轮策略干预。阶段3与v1的`compare_runs.py`不兼容v2 schema；当前v2跨seed仍是逐轮描述性比较，没有自动置信区间。完整承接与边界见[验收覆盖](../../docs/GOALS_AND_COVERAGE.md)。

入口微突发与连续分支诊断使用只读工具，无需增加热路径记录或重编：

```bash
python3 experiments/affinity/flow_report.py results/affinity/routing-run-01/seed-* \
  --warmup-seconds 2 --window-seconds 8
```

它复用完整审计，将成功请求均值分为投递lag与attempt→完成，单列失败/拒绝；p99不能相减。对完整burst计算实际attempt跨度与首attempt lag，区分整批后移和批内拉宽；对成功计划到达cohort保留全部晚完成回调，分类init、首波stage0、后续波stage0、普通后继和join。request的attempt记录在准入检查/构造/root add之前，不等于入队；回home列为source非home且chosen等于home，不等于特定阈值分支命中。`routing reasons`列另统计已有CSV的Home/ReturnHome/LocalSpill/NonFork等原因；未知值显示Unknown(n)，当前契约下矛盾的ReturnHome记录拒绝。24轮、9轮task异质性及9轮burst均已完成，最新[4009审阅](../../docs/TARGET_V2_4009_REVIEW.md)说明收益对到达形态敏感。4010～4013的后续审计已完成，当前执行[9轮regular守护](../../docs/TARGET_V2_COVERAGE_NEXT.md)，不重跑历史命令；无需重编C++。

同seed成对delta是整轮统计量之差，不把请求当独立重复。默认少于1000请求提示尾部样本不足；1000也仅约10个名义尾样本，不是自动可信。初筛240请求/12QPS只看机制、稳定性和数量级；正式p99建议每配置至少数千到上万请求，≥5次交错重复，并检查队列/提交lag是否随时间增长。共享负载下有相关性，不能拿每秒p99平均当总体p99。

收益判断同时检查：整请求尾时延下降、相同工作量、无拒绝、CPU/请求和算子wall是否恶化、迁移是否过多。若排队下降但执行变慢，这可能就是并行度收益与缓存/内存代价的拐点；不能只筛选“被迁移任务”同未迁移任务比较后宣称因果。真正的对照单位是冻结负载后的整轮策略。

当前局部x86验收只证明编译、工作合同和生命周期正确；不证明ARM上的最优阈值/批量/迁移策略。v2暂不包含真实RPC压力、任意多父数据图、在线服务类型EWMA、队列偷取或生产Future适配。第五阶段仍负责成熟策略的API归属/ABI/生命周期/复用决策；高层门面目前留在demo，并未移入或改变Folly默认执行器。

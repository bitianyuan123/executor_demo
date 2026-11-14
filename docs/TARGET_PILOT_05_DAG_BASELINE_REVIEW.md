# ARM pilot：条件分位数收束、DAG基线与路由对照

依据：用户回传的`0005.log`，包含三组既有独立任务CSV的条件分位数，以及高线程DAG home基线。本文核对终端文本和对应源码，未取得ARM原始CSV，未在开发机重新执行ARM性能实验。

## 1. 当前阶段结论

独立任务初测已经得到按部署、线程数、到达形态分层的queue→等待/完成p50、p95、p99，能够收束。DAG基线也已经完成：cluster绑定的request平均时延约98.95ms，none/numa约60.7ms。下一步运行既有`s4-routing`，验证保持cluster绑定、仅改变任务选池能否恢复初始扇出的并行能力。

这一步尚不能选择生产最终策略。当前20ms同质算子、规则12 QPS、无RPC和数据局部性模型，request重叠很少；它适合先隔离初始扇出机制，之后再逐项增加重叠和异质性。

## 2. 独立任务：现在可以给出哪些定量关系

三组离线汇总分别为burst 9/9、threads 12/12、256线程部署9/9，全部usable，未排除运行。窗口仍为`[10,60)`，每轮12,000个任务，保留窗口后完成的慢任务。下面数值均为三个seed各自分位数的范围，不是合并分位数或置信区间。

### 固定256线程、cluster、local burst100

|提交前queue|等待p50 ms|等待p95 ms|等待p99 ms|提交→完成p50 ms|提交→完成p99 ms|每轮桶内任务数|
|---|---:|---:|---:|---:|---:|---:|
|0|0.020～0.021|0.025～0.028|0.033～0.035|283.589～300.120|581.142～624.703|120|
|4～15|0.086～0.092|2.872～2.904|2.940～3.018|305.866～322.713|603.284～609.160|1556～1619|
|16～63|56.065～61.683|148.608～159.835|167.625～176.667|485.568～490.636|643.763～647.688|6124～6346|
|64+|229.364～232.695|297.951～300.791|313.930～316.825|582.240～583.437|676.422～679.841|3549～3831|

完整完成p95及其他桶保存在已打印的conditional报告中。0和4～15桶带LOW_TAIL；即使样本更多，burst内任务也不是独立重复。因此这里是此配置下的经验条件分布，不是“队列单变量导致时延”的因果公式。

### 固定256线程、queue≥64，改变CPU允许范围

|亲和|等待p50 ms|等待p95 ms|等待p99 ms|提交→完成p99 ms|
|---|---:|---:|---:|---:|
|none|0.908～0.937|4.668～6.998|8.596～13.572|176.099～181.262|
|numa|33.913～36.551|59.664～61.718|67.400～67.546|272.067～274.417|
|cluster|229.364～232.695|297.951～300.791|313.930～316.825|676.422～679.841|

相同原始queue阈值不能跨配置直接对应同一时延。还不能把这个阶段的桶阈值直接搬给DAG：后者算子预算、图依赖、扇出宽度和提交者并发都不同。

零队列有两种不同盲点：

- spread、16线程时，queue=0任务的等待p99为56.547～57.226ms，约3.31%～3.40%等待超过1ms。快照为0不保证新任务立即获得worker或CPU。
- local、256线程时，queue=0任务几乎立即开始，但完成p99约581～625ms。这个桶每burst恰好一条，符合批前部任务；随后扇出的工作会在快照之后增加竞争。不能只归因于queue没记录inflight，任何单次当前负载快照也不能直接包含未来整个扇出。该桶每轮仅120条，尾部分位数不要作精细差值比较。

线程数也存在不同分位数的取舍：cluster的t16→t256，全体等待p99从约573～575ms降到300～306ms，planned完成p99只从约691ms降到666～668ms，完成p50反而从358～360ms升到510～512ms。不能以等待变短代替业务完成时延改善。

保留高线程的投递迟到告警、planned/submitted两种口径。burst/local与threads/t16的配置及seed相同，不能把它们合并成六个不同seed的独立重复。当前已有表格与此前整轮留出预测足以完成阶段3初测；生产阈值和干预收益由后续路由实验检验。

## 3. DAG基线：亲和效应已经体现在整个request

九轮全部rc0，每轮720请求全部完成；主窗口每轮600请求、37,200个CPU任务（36,000普通算子+1,200个join）。同二进制SHA256 `02bdf66bd23f1aaa74b9051546dc193dc71e8b4eee9c7f686dca3fa8eabb07c6`、相同96逻辑CPU并集、每池256线程。无拒绝、排空超时、日志失败或配额限流；每轮cohort记录的工作CPU为720.161～720.177秒，预算一致。

下表均值为三轮均值等权平均，p99列为各轮范围，均基于预声明的`[10,60)`窗口：

|指标|none|numa|cluster|
|---|---:|---:|---:|
|request planned完成均值 ms|60.713|60.731|98.951|
|request planned完成p99范围 ms|60.818～60.967|60.830～61.312|103.892～113.252|
|所有CPU任务开始前等待均值 ms|0.094|0.093|4.365|
|所有CPU任务工作墙钟均值 ms|19.380|19.381|29.827|
|第一波根算子等待均值 ms|0.139|0.139|6.761|

cluster−none的request均值差逐seed为约38.093、38.547、38.074ms，精确配对平均+38.238457ms，约增加63%。numa−none逐seed为+0.086、+0.055、−0.088ms，配对平均仅+0.017872ms，当前不能据此宣称numa有收益、退化或与none严格等效。

20ms算子加100μs join时，无资源等待的路径预算为`20+0.1+20+20+0.1=60.2ms`，none/numa约60.7ms与此接近。所有CPU任务的平均CPU约19.359ms，是60个20ms算子与2个0.1ms join的加权均值，不表示算子少做了工作。

cluster前后半窗口均值相近，投递迟到p99约0.06ms，排空仅约14～16ms；没有出现此前高线程独立burst的明显投递迟到。这里仍只是60秒初测，不认证长期稳态。每轮600请求，p99名义尾部仅约6条，三个seed不能支持精细p99区间或0.5%～1%的收益判断。

### 路径证据必须保持同一统计口径

图分析每轮验证50,400条依赖边（720×70），路径分解守恒。它当前使用整轮720请求，包含前10秒；不能与上述600请求窗口直接混加。

只在这套**整轮口径内部**比较cluster−none：

|分量|三轮平均差 ms|
|---|---:|
|request总时延|+37.497|
|路径提交至开始|+17.108|
|路径工作墙钟|+20.377|
|路径调度/提交间隙|约+0.011|

舍入后分量和可能差0.001ms。开始前和执行期间都增长，不能只归因于Folly队列；这里没有RPC，路径表列名中的“调度/RPC”主要是投递与后继提交间隙。墙钟增量不是直接测得的内核runq时间。并行分支累计等待、各分量p99都不能相加解释request p99。

自动打印的整轮none/numa p99约77～88ms，而窗口p99约61ms，来自包含前10秒与否的统计口径差异。窗口预先确定，没有为了更好数值临时裁剪。冷启动或早期变动是否属于业务目标，需另行定义。

### 其他容易误读的字段

- 全轮任务等待的Spearman约0.97，none/numa也很高，但其绝对等待很小。这可能反映批内顺序等结构，不表示queue已能准确预测request p99，亦不是独立样本显著性结论。
- 少量周期快照有非零queue，可以同时出现queue均值大于0、p99等于0；不要据后者判定整个过程没有积压。周期快照与任务提交前快照不是同一采样方式。
- `numa_verified=False`表示没有启用`preferred-numa`候选范围才执行的额外sysfs核验。生成计划时已经根据拓扑验证本CPU子集，当前有效mask也匹配；这个字段不表示亲和设置失败。
- `cross_numa`比较的是来源池与目的池的NUMA标签，不是内核线程迁移次数或远端内存访问次数。none策略下为0并不能说明线程没有在不同NUMA CPU上运行。
- 没有进程总CPU统计，记录的工作CPU不含完整路由、Folly与日志开销，不能据这批结果宣称某部署更省总CPU。

## 4. 为什么下一轮先做策略机制对照

当前12 QPS为规则到达，request间隔83.33ms；六池轮询，同一home池每500ms才接一个request。none/numa约60.7ms，稳态绝大多数request不重叠；cluster约99ms主要只有相邻不同home请求的短暂重叠，尚未形成同池多request持续竞争。

第一波40个算子超过cluster的16逻辑CPU范围，第二波仅10条链，未超过这个范围。下一轮可以有力检验初始分散，但后续均衡可能很少触发。若initial、all、sticky、borrow相近，只能说明当前低重叠、同质短任务下没有明显额外收益，不能宣布后续均衡或回迁没有价值。

## 5. 下一轮的实际路由逻辑

继续用已有`s4-routing.json`：所有case都绑定cluster，每池256线程、相同图/预算/QPS；只改变选池策略。

|case|首选池|允许寻找更轻池的提交|其他提交|
|---|---|---|---|
|home|request.home|无|全部回home|
|initial|第一波为home，此后为parent|第一波40个根算子|跟随各自parent，包括第二波、普通后继和join|
|all|同initial|第一波和第二波根算子|普通后继和join跟随parent|
|sticky|初始home，此后parent|所有提交|每次可再次选择更轻池|
|borrow|始终request.home|所有提交|每次重新比较home，有利时返回home|

这里的parent是提交当前任务的算子所在池；join使用最后完成依赖计数递减的分支池，不保证等于路径分析选出的“记录finish最晚”分支池。request.home不因迁移而改变。

默认queue评分为`队列快照 × 当前任务估计CPU预算 / capacity`，本六个cluster的capacity均为16，是逻辑CPU启发式容量，不是实测容量。默认估计采用demo已知任务预算；queue评分不计已经开始的回调，也不预留尚未提交的整批扇出。

当前没有一个绝对“忙队列长度”门槛。候选为所有六池，只有严格更低分才选走；min-gain、relative-gain和跨NUMA惩罚均为0。首选池并列最小则留下，其他池并列按池编号扫描顺序选择。这是提交时的启发式选池，非原子全局决策；多个提交者可能同时选到同一个较轻池。

本轮各池capacity相同、迁移惩罚和门槛为0，同一次提交的任务估计又是各候选共用的正因子，所以queue策略实际按队列深度排序。它尚未验证跨容量或加入迁移成本后的评分校准。

选定后任务进入该Folly池，没有已入队任务搬迁、后台窃取或运行中任务抢占迁移。`max-inflight=128`是request准入上限，不是忙闲阈值。这轮用于研究业务层选池机制，不是验证修改Folly内部调度的收益。

主对照分别是home→initial、initial→all、all→sticky、borrow→sticky。第三组同时开放普通后继和join等非fork提交，不能单独归因于普通链后继。

## 6. 执行与终端回传

新增终端机制统计只读取已有CSV，不改变C++负载或Folly；更新后无需重新编译。下轮15轮×60秒，约15分钟投递，加启动、排空与分析；只运行routing阶段，不重复前面阶段。

```bash
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
TARGET_HIGH='results/local/arm-pilot-t256-01'

taskset -c 0-47,192-239 python3 scripts/run_matrix.py \
  --binary build/executor_demo \
  --cases-json "$TARGET_HIGH/plan/s4-routing.json" \
  --output "$TARGET_HIGH/s4-routing" \
  --duration 60 --repeats 3 --seed 420 \
  --qps 12 --cpu-us 20000 --proc-interval 0

python3 scripts/compare_runs.py \
  --manifest "$TARGET_HIGH/s4-routing/manifest.json" \
  --spec "$TARGET_HIGH/plan/s4-routing-contrasts.json" \
  --output "$TARGET_HIGH/reports/s4-routing-paired"

python3 stages/04-dag/analyze_graph.py "$TARGET_HIGH/s4-routing"/r*/ \
  --output "$TARGET_HIGH/reports/s4-routing-paths"

cp "$TARGET_HIGH/s4-routing/environment.json" "$TARGET_HIGH/s4-routing/topology-before.json"
taskset -c 0-47,192-239 python3 scripts/topology.py \
  --output "$TARGET_HIGH/s4-routing/topology-after.json"

python3 scripts/terminal_report.py --root "$TARGET_HIGH/s4-routing" --section environment
python3 scripts/dag_terminal_report.py --root "$TARGET_HIGH" --phase s4-routing
cat "$TARGET_HIGH/reports/s4-routing-paired.md"
cat "$TARGET_HIGH/reports/s4-routing-paths.md"
```

输出目录存在时runner拒绝覆盖，应保留已有结果先核查。较长摘要可将dag_terminal_report拆成`--offset 0 --limit 5`、`--offset 5 --limit 5`、`--offset 10 --limit 5`三段复制；仍保留所有轮次和告警。

新摘要在同request cohort内按initial_fork/later_fork/ordinary_successor/join/rpc_resume打印实际选池、迁移、回home与等待，缺失字段标unknown。当前每轮cohort预期分别为24,000、6,000、6,000、1,200、0个任务，总数37,200。这样可以核对策略是否真正触发，并观察按池编号扫描是否造成集中。

迁移需要区分：日志`migrated`是chosen与source比较（有parent时source=parent，初始为home），`rerouted`才是chosen与preferred比较。因此borrow的回home可能算source迁移但不是偏离首选；跟随已迁移parent也不必产生新的migration。不能只用总迁移次数判断请求是否离开home或后继是否继续均衡。

本批接收后，先检查合同/完成/图守恒，再看request时延、路径分量和各类实际改投次数。若策略差异因未触发而难以判断，下一批优先逐级提高算子预算（例如50ms，再视稳定性到100ms），或单独改变后续fork宽度/到达抖动。12 QPS、60个100ms算子会产生约72 CPU秒/秒名义需求，是本96逻辑CPU子集时间上限的约75%，但不是有效算力保证；不能直接把它等同原业务50%利用率，也不要一次同时加入所有复杂因素。

## 7. 本地增量验证

本次只改文档和Python终端摘要，没有改变C++负载、工作预算或Folly调度。DAG摘要10项测试通过，覆盖分类、同cohort统计、窗口外慢完成、未完成任务排除、未知字段与计数守恒；根15组CTest全部通过。另读取既有缩放baseline 6轮、routing 10轮，验证实际CSV兼容和路由分类总数。这些是工具验证，不是新的ARM性能数据。

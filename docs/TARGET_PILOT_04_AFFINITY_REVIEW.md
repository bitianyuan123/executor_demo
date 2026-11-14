# ARM pilot：256线程亲和对照、指标成本与进入DAG阶段

依据：用户回传的`0004.log`，含`arm-pilot-t256-01/s3-deployment`九轮终端摘要、分桶、配对结果，以及首批`metrics-t16`探针。本文核对这些文本和本地对应实现，未收到ARM原始CSV，也没有在开发机代替执行ARM性能实验。

## 1. 本轮已经回答的问题

当前独立任务初测可以收束，进入DAG。九轮均退出0，每轮14,400个任务全部完成；`[10,60)`窗口每轮12,000个任务，工作量与资源配对合同通过。同一二进制、96逻辑CPU并集，未观察到cgroup CPU配额限制或throttling增加。三轮重复仍只提供探索性描述，不报告置信区间。

本轮固定：6个池、每池256个worker；每批100个独立task集中到一个池，批次按round-robin选池；2.4批/秒，即240 task/s；随机线程CPU预算均值100ms（jitter=0.5、无额外长尾，有限样本预算归一化）。批内按顺序调用add，没有图依赖、RPC、回迁或动态负载均衡。线程由Folly通知/领取工作，再由内核在允许CPU范围内调度；用户态queue与内核runq不同。

整个实验都限制在`0-47,192-239`，共48物理核、96逻辑CPU。none每池允许96个逻辑CPU；numa前三池共享`0-47`，后三池共享`192-239`，每池48个逻辑CPU；cluster每池固定16个逻辑CPU。发生器与辅助线程也在同一个96CPU集合内，不绑定不表示放开整机384CPU。

下表均值为三轮均值的等权平均，p99为三轮各自值的范围，单位ms；不是合并p99：

|亲和|planned→完成均值|各轮planned p99范围|提交→开始均值|回调墙钟均值|
|---|---:|---:|---:|---:|
|none|105.106|173.234～175.026|0.895|104.116|
|numa|171.797|263.828～264.807|13.573|158.126|
|cluster|472.097|664.734～668.246|105.894|365.159|

cluster相对none平均增加366.991ms，其中提交至开始约增加105.0ms，回调墙钟约增加261.0ms，投递迟到约增加0.95ms。相同cohort的平均分量可相加；分量p99不能相加。大部分增量落在回调开始之后，单看队列等待会漏掉执行期间竞争。

这支持“本合成突发模型中，cluster绑定限制可借用CPU范围、降低峰值并发”的机制解释。它不证明真实业务应全部解绑，也没有测出真实中间数据的缓存收益或迁移代价。回调墙钟增量不是直接测得的runq等待，整轮callback CPU均值也不能与窗口内callback墙钟均值直接相减。

cluster的投递迟到p99为8.630～10.087ms，必须保留；planned时延已包含迟到。none的后半窗口均值比前半低约2.8～5.5ms，存在时间变化。两者不妨碍本次366ms级的机制判断，但不足以认证长期稳态或生产0.5%～1%的收益。CPU-budget模式包含CPU时钟查询；后续研究实际计算吞吐、局部性或插桩开销时，需要固定迭代工作量对照。

## 2. queue与时延已有定量关系，但不能共用一个原始阈值

同为每池256线程、提交前Folly queue≥64的任务：

|亲和|该桶各轮等待均值 ms|该桶各轮提交→完成p99 ms|
|---|---:|---:|
|none|1.774～2.219|176.099～181.262|
|numa|35.772～37.333|272.067～274.417|
|cluster|228.182～233.608|676.422～679.841|

这已经是条件分桶的定量证据。同样的queue值对应的等待差异很大，阈值需要按CPU允许范围、线程数、工作分布和调度方式标定；不能直接给出全局“queue≥64就忙”。即使按逻辑CPU数量归一化，也不能自动消除SMT效率、唤醒节奏、当前执行任务或未来burst的影响。

none的100ms周期采样queue_sum为0，而提交前采样仍有大量queue≥64任务，两者并不矛盾：短暂积压可发生在两个周期采样点之间。周期采样适合趋势，投递点快照适合关联该次任务的后续时延。

本轮cluster的queue=0任务平均开始前只等约0.02ms，但提交至完成均值仍约300～317ms；queue=0不能表明CPU空闲，也不包含随后到达的同批任务。此前线程扫描观察到，线程数增加后开始前等待减少、回调墙钟增加；提交至开始包含Folly排队、唤醒及内核调度，不能全部称为队列驻留时间。优化目标必须看任务完成和后续request完成时延。

已有报告提供桶内平均值和完成p99，但还没有完整打印桶内等待/完成p50、p95、p99。新脚本`conditional_latency.py`只读现存CSV，补齐这些分位数，不增加压测：

```bash
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
TARGET_RUN='results/local/arm-pilot-01'
TARGET_HIGH='results/local/arm-pilot-t256-01'

python3 scripts/conditional_latency.py \
  --manifest "$TARGET_RUN/s3-burst/manifest.json" \
  --output "$TARGET_RUN/reports/s3-burst-conditional" --compact

python3 scripts/conditional_latency.py \
  --manifest "$TARGET_RUN/s3-threads/manifest.json" \
  --output "$TARGET_RUN/reports/s3-threads-conditional" --compact

python3 scripts/conditional_latency.py \
  --manifest "$TARGET_HIGH/s3-deployment/manifest.json" \
  --output "$TARGET_HIGH/reports/s3-deployment-conditional" --compact
```

三段摘要可分别复制。每个case内按同配置分组，各seed独立计算nearest-rank分位数，终端显示每轮值的范围，并列任务数、burst数、缺失桶、零队列慢任务比例。窗口从manifest读取，窗口后才完成的慢任务仍保留；失败或不完整运行显示EXCLUDED，不悄悄仅保留成功任务。相同case配置不同会拆组，重复seed拒绝计为独立轮次。JSON保留每seed结果、完整配置与事件数，Markdown保留回调墙钟分位数等完整表格。

`LOW_TAIL`表示至少一轮该桶n×1%<20；它是样本提示，不是显著性检验。burst内任务相关，即便去掉该标志也不代表p99精度达标。范围不是置信区间，不能合并不同亲和/线程数拟合一个通用关系。此前整轮留出预测已经说明queue在16线程local下有较强等待预测价值；这些新表补充条件尾时延，不宣称新模型已经通过独立验证。

## 3. 指标成本：queue是合理起点

16线程探针的批量热读均值如下，单位ns/调用：

|读取方式|idle|busy with backlog|
|---|---:|---:|
|循环基线|0.600|1.275|
|native_queue|1.940|3.220|
|单个relaxed load|0.635|1.270|
|三个relaxed load|0.960|1.825|
|pending API|24.495|27.500|
|light pool stats|30.345|53.595|
|完整pool stats|63.615|127.175|
|used CPU time|3948.150|8243.050|

这些是缓存较热、短批量的端点测量，不扣循环基线推“纯指令成本”；p99实际是40个batch均摊值的最大值，不是单次调用p99。忙态worker在持续占CPU，但读阶段计数基本不变，没有覆盖高频修改同一cacheline、跨NUMA读写或256线程情形。因此支持优先使用已有native_queue快照，不足以认定全量生产插桩扰动可忽略。

更新探针的4生产者聚合成本约为：共享fetch_add 8.64～8.79ns/event、分片填充fetch_add约1.65ns/event、每64次发布约0.51～0.55ns/event。这是总墙钟/总事件的吞吐口径，不是单次操作延迟；每64次发布还引入滞后。不能由此推导“所有原子操作都很昂贵”。允许估计误差不意味着可以把并发读写的普通C++变量当安全快照；应区分relaxed原子读取、共享RMW和分片发布。

当前选择：保留native_queue作为低成本候选输入，按配置标定，并用完成时延验证；只有在新增计数能改善留出误差或调度结果时，才增加pending/inflight等维护。CPU时间或内核调度统计用于低频诊断，不放在每次任务投递的热路径里。此结论尚未决定DAG最终路由阈值。

## 4. 下一步直接跑DAG基线

无需重编Folly或C++，本次新增的是Python只读分析工具。保持每池256线程，使用已经生成的`s4-baseline.json`，仅运行这个阶段：

|项目|DAG初测设置|
|---|---|
|请求到达|12 QPS，规则到达，request按round-robin选home池|
|图结构|第一波40条单节点分支，join后第二波10条两节点链，再join|
|CPU工作|60个普通算子×20ms，2个join×100μs；无长尾、RPC或额外数据集|
|需求|约14.4024 CPU秒/秒；20ms是初测缩放，不冒充真实100ms|
|调度|policy=home，全部后继回原request池；依赖满足才提交到executor|
|负载均衡|本轮不跨池借用、回迁或工作窃取；none由内核在更宽CPU范围调度|
|部署对照|none / numa / cluster，同96逻辑CPU并集|
|统计|每种3轮×60秒，窗口[10,60)，按request而非单task计算主时延|

```bash
cd /home/bty/rank_demo/executor_demo
TARGET_HIGH='results/local/arm-pilot-t256-01'

taskset -c 0-47,192-239 python3 scripts/run_matrix.py \
  --binary build/executor_demo \
  --cases-json "$TARGET_HIGH/plan/s4-baseline.json" \
  --output "$TARGET_HIGH/s4-baseline" \
  --duration 60 --repeats 3 --seed 420 \
  --qps 12 --cpu-us 20000 --proc-interval 0

python3 scripts/compare_runs.py \
  --manifest "$TARGET_HIGH/s4-baseline/manifest.json" \
  --spec "$TARGET_HIGH/plan/s4-baseline-contrasts.json" \
  --output "$TARGET_HIGH/reports/s4-baseline-paired"

python3 stages/04-dag/analyze_graph.py "$TARGET_HIGH/s4-baseline"/r*/ \
  --output "$TARGET_HIGH/reports/s4-baseline-paths"

# runner开始时已经自动采集environment.json；保留为本阶段before证据。
cp "$TARGET_HIGH/s4-baseline/environment.json" "$TARGET_HIGH/s4-baseline/topology-before.json"
taskset -c 0-47,192-239 python3 scripts/topology.py \
  --output "$TARGET_HIGH/s4-baseline/topology-after.json"

python3 scripts/terminal_report.py --root "$TARGET_HIGH/s4-baseline" --section environment
python3 scripts/dag_terminal_report.py --root "$TARGET_HIGH" --phase s4-baseline
cat "$TARGET_HIGH/reports/s4-baseline-paired.md"
cat "$TARGET_HIGH/reports/s4-baseline-paths.md"
```

约9分钟投递，加启动、排空与分析。输出目录已存在时runner拒绝覆盖，保留已有结果先核查。case参数优先于runner较早的同名参数，不要仅修改runner的cpu-us误以为改了case预算。当前计划明确为20ms。

摘要约100行，可用`--offset 0 --limit 3`、`--offset 3 --limit 3`、`--offset 6 --limit 3`分页打印。它从对应contrasts.json取分析窗口；若处理其他独立目录，可用`--spec`或同时提供`--warmup-seconds`和`--steady-end-seconds`，不猜窗口。DAG没有记录进程总CPU秒，输出`process_cpu_s=not_recorded`；task工作段CPU不能替代进程CPU。队列或inflight也不等于正在CPU执行的线程数。

下一批重点核查：720请求/轮是否全部完成、cohort约600请求、home策略迁移应为0、图依赖/路径分解是否守恒；亲和效应在request平均/p99上是否仍出现，落在初始扇出等待、后继调度还是工作段墙钟。不要把全图分支累计等待加成request时延，也不要把各分量p99相加。路径工具当前使用全轮请求，新终端摘要和配对工具使用10～60秒cohort，两套均值不能逐项混加。

初测每轮约600个request，p99名义尾部只有约6个请求，足以先观察明显趋势，不足以给稳定的p99收益区间。下一步确认home部署后，再运行现有`s4-routing`比较initial/all/sticky/borrow；之后按结果逐项加100ms时间尺度、扇出宽度、RPC、长尾和数据局部性。现在不要求先跑完可选独立任务矩阵。

## 5. 本地交付验证

本次新增条件分位数工具及10项测试、DAG终端摘要及8项测试，注册到根CTest。验证包含慢任务保留、不同配置分开、重复seed、坏运行、未知队列、缺失桶与低频概率；DAG还验证请求/任务口径、窗口来源、失败后继续、分页和缺失负载。用此前保存的缩放实验产物核对实际CSV兼容，未新增ARM压力。这些检查证明工具流程，不构成额外ARM性能证据。

根工程重新配置后15组CTest全部通过（约3.4秒）；另读取既有20轮独立任务和16轮DAG产物，所有只读分析完成。未修改C++负载或Folly调度实现，本轮ARM数据与下一轮仍可使用既有二进制。

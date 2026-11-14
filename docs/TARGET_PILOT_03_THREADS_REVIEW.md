# ARM pilot-01：预测检验与线程数扫描审阅

依据：用户提供的 `0003.log`，包含既有s3-burst的整轮留出预测、queue_zero计数，以及12轮s3-threads摘要、分桶和配对报告。本文复核终端数字和本地对应源码，没有收到ARM原始CSV，也没有重跑目标机负载。线程扫描仍使用二进制SHA256 `f4efe69dbd41f053324f25b5b8080ddf8afe4950c1357bbbf7a6b4677c7c0fc9`。

## 核心判断

1. queue对本次16线程单池集中负载具有很强的等待预测价值；pending并未明显优于queue。加入inflight在local改善，在spread反而恶化，不能把更多指标等同于普遍更准确。
2. cluster范围固定时，从16线程增加到64/256线程，提交至开始等待减少，但回调墙钟大幅增加，总体平均时延恶化。队列缩短不能单独作为优化成功的标准。
3. 高线程的p99略低，平均时延却更高，是取舍。当前仅三个seed且实际投递出现迟到，不据此选择生产最优线程数。
4. 下一步固定256线程，比较none/numa/cluster，检验较大的CPU允许范围能否恢复峰值并发；保留同一发生器、CPU集合和插桩口径。16线程部署基线、固定迭代工作量复验和DAG仍在后续计划中。

## 预测检验：三种场景需分别解释

所有模型每组3/3有效折、累计36,000个留出任务；每次用两整轮训练、另一整轮测试，未把同一个burst拆入训练与测试。数据来自s3-burst、每池16线程，不能直接当成64/256线程下已经验证的模型。

下表是**等待预测的MAE，单位ms**；各模型都含当前任务的已知合成target预算：

|模型|local|spread|smooth|
|---|---:|---:|---:|
|仅自身target|155.5971|4.2873|0.0040|
|queue|15.0648|3.9781|0.0040|
|pending|15.0864|3.9716|0.0040|
|queue + inflight|11.8472|4.2029|0.0040|
|work + inflight|7.8767|4.4800|0.0040|

local中queue相比target-only降低MAE约90.3%；加入inflight再比queue降低约21.4%，p95绝对误差从41.8446降到33.6166ms。pending的MAE与queue基本相当，没有足够证据用它替换queue。work的改善使用了demo已知工作预算，不能宣称生产中可以同样获得。

spread中queue相比target-only只降低约7.2%；queue+inflight相对queue的MAE增加约5.7%，p95绝对误差从2.0842增至7.3335ms。更多特征在当前线性模型上没有统一收益，也不能由此证明inflight本身无用：当前拟合是固定ridge线性最小二乘，评价指标是留出MAE和误差分位数，忙闲阈值、剩余服务时间和后续突发影响未必线性。

spread按等待超过5ms定义慢任务，即使把所有281个非零队列任务也算入，最多1472/36000≈4.09%；p95绝对误差可能遗漏这部分困难样本。MAE大于p95并非数学矛盾。指标选择还应看尾部和条件错误，不能只挑一个较小的p95。smooth几乎没有排队，约4μs的等待MAE不用于证明指标优劣。三个seed未提供足以精确评价微小差异的独立重复数，当前不调整模型再把同一批验证数据当独立确认。

## queue=0的两个不同盲区

**回调开始前：** spread零队列桶共35,719个任务，其中1,199个等待超过1ms（3.357%），1,191个超过5ms（3.334%），最大等待95.70772ms。百分比的分母是零队列桶，不是全部任务。零队列不等于有空闲worker；入队后的任务仍可能等待执行中任务释放worker或等待通知/调度。

**回调开始后：** 新线程扫描中，t256零队列桶的平均提交至开始只有约0.019～0.020ms，但提交至完成均值为303.890～340.754ms。这些是各轮的同一零队列桶统计，不是所有任务的均值。已进入回调的任务仍会受到随后同批任务和共享CPU的影响；提交时的queue甚至queue+inflight都不包含尚未投递的未来工作。

因此可把queue保留为简单的积压候选指标，但不能把queue=0当作“CPU不忙”，也不能跨线程数、不同CPU允许范围直接套用一个阈值。最终选型仍需要读取/维护成本，以及固定工作量下的端到端扰动数据。已有metrics-t16探针产物可先补齐，不为获取这部分数据重新压测。

## 线程数扫描结果

12轮全部退出0；每轮14,400任务全部完成，合计172,800个任务，无拒绝/取消/失败，守恒成立。分析窗口 `[10,60)`，每轮12,000任务；same_union、二进制与工作预算一致，目标机三个预声明合同通过，均为 `descriptive_only`。所有轮采样500个时刻，无失败或截断。

下表均值是三轮均值的等权平均，p99列是三轮各自值的范围，不是合并p99：

|每池线程|planned→完成均值 ms|提交→开始均值 ms|回调墙钟均值 ms|各轮planned p99范围 ms|
|---:|---:|---:|---:|---:|
|8|673.163|572.980|100.082|1305.256～1311.969|
|16|360.456|260.264|100.100|690.779～691.533|
|64|456.684|132.081|323.674|671.457～673.550|
|256|471.242|106.038|364.119|666.091～668.057|

原合同以t8为baseline，均值差分别为t16 −312.707ms、t64 −216.479ms、t256 −201.921ms。下面的16→64/256是同一批完整seed上的**追加探索性描述**，不伪装成预声明主比较，也不补报置信区间：

- 16→64：planned均值约增加96.229ms（26.7%）；提交至开始减少128.183ms，回调墙钟增加223.574ms，投递迟到均值增加0.836ms。每轮p99差的均值约−18.415ms，约下降2.7%。
- 16→256：planned均值约增加110.786ms（30.7%）；提交至开始减少154.226ms，回调墙钟增加264.020ms，投递迟到均值增加0.991ms。每轮p99差的均值约−23.988ms，约下降3.5%。

均值变化可以用同cohort时间分量相加核对；p99分量不能相加。16线程在已测试四档中平均时延最低，但不是证明它是生产最优值，甚至不是证明它在所有时延目标上都最优。

这里全部是单个task的时延，不能把task p99改善直接叫作burst或request完成时延改善；一次fanout的完成取决于分支及依赖路径。已有bursts.csv与后续DAG实验用于回答这类业务目标。

前后半窗口完成速率基本跟随240 task/s，时延没有持续共同上升，排空均小于0.9秒。当前未见持续过载，但不认证长期稳态；12轮不是12个相同配置的独立重复，每个线程配置仍只有3轮。

## 为什么256个线程、100个任务仍可能等待

线程数是已创建worker数量，不是此刻已经获CPU执行的worker数量。程序使用固定min=max线程配置，在实验epoch前建池；单池worker即使有256个，允许执行范围仍是16逻辑CPU。

所检查Folly源码的默认队列选择ThrottledLifoSem路径，取任务前必须获得permit；通知睡眠worker不等于worker已运行和取走任务。该信号量的后续唤醒链推进依赖先被唤醒的worker实际执行。尚未领取的任务可以继续体现在queue计数中，而已经进入回调的任务又要竞争CPU。这些机制解释了“线程很多”不保证“回调立即开始”，但日志没有每次唤醒/调度轨迹，不能把约106ms等待精确归因于某一条唤醒链。

回调墙钟增长与受限CPU下的竞争相符，但现有证据不能将全部增量叫内核runq时间；线程CPU时钟查询、缺页、运行中的抢占等仍需独立诊断。summary中的100.001ms CPU均值属于整轮，不能与cohort回调均值直接相减。CPU-budget模式还包含频繁CPU时钟查询，生产有效计算量结论需用固定迭代工作量复验。

## 投递迟到告警如何处理

t64投递迟到p99为6.838～9.517ms，t256为9.699～12.428ms；t16为0.120～0.162ms。不能隐藏或删除这六轮告警。

planned时延已经计入迟到，所以这不是遗漏等待造成的虚假成功。平均迟到增量约0.8～1.0ms，远小于96～111ms的总均值恶化；但实际burst也被摊开，会改变后续竞争和尾部，不能减掉一个迟到数就“校正”为纯executor效应，更不能把约3%的p99改善全部归因于线程数。

此处task提交lag同时包含整批到达延迟与批内顺序投递扩散，并非专门测量主线程睡醒延迟。需要进一步归因时，可离线从bursts.csv的actual_ns−planned_ns以及每批任务max(submit_ns)−min(submit_ns)分别计算，无需增加压力；现有终端文本还不足以区分两者。

本轮发生器与worker共享约定96CPU；下一轮保持此口径，回答整个配置的端到端差异。若之后要分离发生器干扰，单独设计保持worker资源一致的辅助线程隔离对照，并取得其CPU资源安排；当前不临时占用约定集合外的CPU，也不把隔离与未隔离结果混配。

## 下一步：256线程三亲和对照

复用既有工具，不需要重新编译。新目录只运行s3-deployment，**不要执行生成计划中的其他阶段**。固定每池256线程、240 task/s、K100 local、100ms均值CPU预算、full计量、相同三个seed及相同CPU并集；三种亲和分别允许每池使用96/48/16个逻辑CPU。

```bash
cd /home/bty/rank_demo/executor_demo
TARGET_HIGH='results/local/arm-pilot-t256-01'

taskset -c 0-47,192-239 python3 scripts/topology.py \
  --output "$TARGET_HIGH/topology-before.json"

python3 scripts/prepare_target_validation.py \
  --topology "$TARGET_HIGH/topology-before.json" \
  --cpus '0-15;16-31;32-47;192-207;208-223;224-239' \
  --numa '0,0,0,2,2,2' \
  --threads 256 --task-rate 240 --cpu-us 100000 \
  --tier pilot --seed 420 --output "$TARGET_HIGH/plan"

taskset -c 0-47,192-239 python3 experiments/independent/scripts/run_matrix.py \
  --binary build/experiments/independent/independent_load \
  --config "$TARGET_HIGH/plan/s3-deployment.json" \
  --output "$TARGET_HIGH/s3-deployment"

python3 scripts/compare_runs.py \
  --manifest "$TARGET_HIGH/s3-deployment/manifest.json" \
  --spec "$TARGET_HIGH/plan/s3-deployment-contrasts.json" \
  --output "$TARGET_HIGH/reports/s3-deployment-paired"

taskset -c 0-47,192-239 python3 scripts/topology.py \
  --output "$TARGET_HIGH/topology-after.json"

python3 scripts/terminal_report.py \
  --root "$TARGET_HIGH" --section environment
python3 scripts/terminal_report.py \
  --root "$TARGET_HIGH" --section runs --phase s3-deployment
cat "$TARGET_HIGH/s3-deployment/analysis.md"
cat "$TARGET_HIGH/reports/s3-deployment-paired.md"
```

9轮×60秒投递，约9分钟加启动、排空和分析。即使已有旧批t256-cluster，也要保留新批cluster重复，主要亲和效应使用本批内部配对。目录已经存在时保留，不覆盖；先查看已有结果。

如果放宽CPU范围后回调墙钟与总时延都明显下降，将支持本合成模型下峰值并发受cluster约束；如果没有，继续检查实际CPU分布、唤醒和投递，而不是先改亲和阈值。无论结果如何，保留迟到和平均/p99取舍，且不将这个缺少真实数据局部性的CPU预算实验直接外推到生产缓存收益。

同时打印首轮已经生成的成本探针，只有读取文件，不启动新压力：

```bash
cat results/local/arm-pilot-01/metrics-t16/metadata.json
cat results/local/arm-pilot-01/metrics-t16/reads.csv
cat results/local/arm-pilot-01/metrics-t16/updates.csv
```

读取p99是批量均摊成本的分位数，更新成本是聚合吞吐倒数，都不是单次调用p99或完整业务插桩开销；16线程探针也不能替代高线程验证。此轮先将这些证据补齐，之后再决定需要哪一种低扰动指标与确认实验。

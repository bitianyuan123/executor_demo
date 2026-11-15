# ARM测试机：按已确认CPU集合分步验证

## 新增四池真实调度轨迹与阶段4路线

最新输入为16 QPS、4×128线程、无CPU绑定，单池active约20/80且定义待核。原始trace留在目标机，先按[轨迹工作流](TRACE_WORKFLOW.md)打印摘要；需要模型辅助时用[分析/审计提示词](TRACE_ANALYSIS_PROMPTS.md)。新的[四池微突发实验](REAL_WORKLOAD_MATRIX.md)与本文六池亲和实验是两条对照路线，不把池数和CPU绑定同时改变后的差值归因于单个因素。负载信号选择和成本见[复核报告](LOAD_OBSERVATION_REVIEW.md)。

本指南基于2026-11-14用户上传的`topology.json`，以及用户确认可以独立使用的`0-47,192-239`。这里提供运行方案；本仓库开发机没有执行这些ARM性能实验。先完成第0～3步并检查结果，再扩展压力和策略，避免一次运行所有矩阵。

当前回传进度已完成独立任务条件p50/p95/p99及256线程DAG home基线。按[第五批审阅与执行步骤](TARGET_PILOT_05_DAG_BASELINE_REVIEW.md)进入`s4-routing`五策略对照；无需重复前面阶段。`conditional_latency.py`与`dag_terminal_report.py`均为只读Python分析，新增路由终端统计不需要重编Folly或C++。本文后续的大矩阵和确认阶段保留为按需扩展，当前不要一次全部执行。

针对随后明确的“透明总池、二层均衡、子图真实数据亲和”目标，新增[阶段4 v2独立工程及校准步骤](../experiments/affinity/README.md)。下一次这类研究先编译`affinity`并做低负载固定工作校准；上段`s4-routing`仍可用于旧DAG路由控制实验，但不替代v2的缓存/回迁验证。此前0001～0005结果继续有效，无需重跑阶段3。

## 0. 固定实际资源与解释口径

附件实际报告2个socket、4个NUMA、24个cluster、192个物理核、384个逻辑CPU；每个cluster为8核×SMT2，其L3共享范围与cluster一致（23296 KiB）。这与最初描述的整机规模不同，因此本轮取一个明确的六cluster子集：

|池|socket|NUMA|cluster CPU集合|
|---|---:|---:|---|
|0|0|0|0–15|
|1|0|0|16–31|
|2|0|0|32–47|
|3|1|2|192–207|
|4|1|2|208–223|
|5|1|2|224–239|

三种部署共享**48物理核、96逻辑CPU**：none的每池允许范围都是`0-47,192-239`；numa的前三池共享`0-47`、后三池共享`192-239`；cluster分别使用表内范围。numa不是放开整个node0/node2，none也不是放开整机384CPU。下面所有runner都放在相同`taskset`中，发生器、计量线程和DAG timer/logger同样占用这个集合。

本子集的跨NUMA也同时跨socket，后续若测到跨域服务代价，它包含socket互连影响；不能独立解释成同socket跨NUMA的代价，也不能外推到全部四个NUMA节点组合。

进程CPU时间的统一口径为`进程CPU秒 / 墙钟秒`；若展示该96CPU集合的百分比，再除以96。不要混用整机384CPU的利用率分母，也不要将96条逻辑CPU时间线解释成96个物理核的有效吞吐。SMT收益需要固定工作量另测。

先更新代码，沿用已经构建成功的根工程产物：

```bash
cd /home/bty/rank_demo/executor_demo
git pull --ff-only

# 以下变量仅影响当前shell。后续所有步骤在这个仓库目录执行。
TARGET_MASK='0-47,192-239'
TARGET_GROUPS='0-15;16-31;32-47;192-207;208-223;224-239'
TARGET_NUMA='0,0,0,2,2,2'
TARGET_RUN='results/local/arm-pilot-01'
TARGET_INDEP='build/experiments/independent/independent_load'
TARGET_DAG='build/executor_demo'
TARGET_METRICS='build/experiments/metrics/metric_cost_bench'

# 增量构建并运行检查，确保记录的源码与产物对应；复用已安装Folly。
SKIP_FOLLY_BUILD=1 BUILD_JOBS=1 ./scripts/build.sh

taskset -c "$TARGET_MASK" python3 scripts/topology.py \
  --output "$TARGET_RUN/topology-before.json"
git rev-parse HEAD > "$TARGET_RUN/demo-commit.txt"
git -C ../folly rev-parse HEAD > "$TARGET_RUN/folly-commit.txt"
ulimit -a > "$TARGET_RUN/shell-limits.txt"
```

旧附件是cgroup v1，旧采集器的`cgroup_ancestors=[]`不代表没有配额。新采集器增加`cgroup_version`与`cgroup_hierarchies`，按controller分别读取自身及可见父级；旧v2字段仍保留。检查CPU quota/period、cpuset、memory、pids：quota为正时`quota/period`是该层CPU带宽上限，父级也可能限制它；`-1`仅表示当前层未设置限制，缺失值仍为未知。限额和throttling定义见[内核CFS带宽文档](https://docs.kernel.org/scheduler/sched-bwc.html)。

```bash
python3 - "$TARGET_RUN/topology-before.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print('allowed CPUs:', len(d['allowed_cpus']), 'cgroup:', d['cgroup_version'])
for h in d['cgroup_hierarchies']:
    print(h['controllers'], h['status'], 'hidden ancestors:', h['hidden_ancestors'])
    for a in h['ancestors']:
        print({k: v for k, v in a.items() if k in {
            'path', 'cpu.cfs_quota_us', 'cpu.cfs_period_us', 'cpu.max', 'cpu.stat',
            'cpuset.cpus', 'cpuset.cpus.effective', 'memory.limit_in_bytes',
            'memory.max', 'pids.max', 'pids.current'}})
PY
```

如果关键限额不可确认、CPU集合有后台业务占用或发生明显throttling，先保留证据并调整负载，不能把相应时延归因于Folly。整段实验期间避免同时编译或运行其他压测。原附件`sched_schedstats=0`；本轮先不改变内核设置，不将runq计数0解释为没有调度等待。

## 1. 生成分阶段计划，不自动执行压力

```bash
python3 scripts/prepare_target_validation.py \
  --topology "$TARGET_RUN/topology-before.json" \
  --cpus "$TARGET_GROUPS" --numa "$TARGET_NUMA" \
  --tier pilot --output "$TARGET_RUN/plan"
```

生成器校验六个完整cluster、NUMA归属和CPU并集；仅生成配置与比较合同。`plan.json`记录时长、窗口、重复数和投递时间预算。输出目录必须不存在，重做时把`arm-pilot-01`改为新的名字。运行器保存命令、二进制哈希、seed和退出状态，禁止覆盖旧轮次。

为了保持smooth与K=100的整轮及测量窗口任务ID一致，生成器要求批次边界精确对齐，并拒绝当前二进制浮点到达计算会多生成一批或错分边界的任务率。默认240和示例120已经检查；出现此类配置错误时换一个可配对的近邻压力点，不放宽工作量合同。

默认pilot计划如下；**逐行运行和验收，不是一次启动全部**：

|阶段|对照|轮数×每轮投递时间|投递总时间，不含排空/分析|
|---|---|---:|---:|
|s3-smoke|none/numa/cluster，1ms小任务|3×3秒|9秒|
|s3-calibration|120/240/480/720 task/s，K=1、100ms均值、每池16线程|8×30秒|4分钟|
|s3-burst|平滑 / K=100分散 / K=100集中；同为240 task/s|9×60秒|9分钟|
|s3-deployment|K=100集中，none/numa/cluster，每池16线程|9×60秒|9分钟|
|s3-threads|K=100集中、cluster，每池8/16/64/256线程|12×60秒|12分钟|
|s4-baseline|同图home策略，none/numa/cluster|9×60秒|9分钟|
|s4-routing|cluster，home/初始fork/所有fork/sticky/borrow|15×60秒|15分钟|

pilot的主要比较窗口为计划到达后10～60秒，重复3次，只提供探索性描述，配对区间应为空。calibration窗口为10～30秒，smoke不评价性能。240 task/s与DAG的20ms预算都是初始探测值，**没有被宣称为机器容量的某个比例**。

## 2. 先做六池绑定验收

```bash
taskset -c "$TARGET_MASK" python3 experiments/independent/scripts/run_matrix.py \
  --binary "$TARGET_INDEP" --config "$TARGET_RUN/plan/s3-smoke.json" \
  --output "$TARGET_RUN/s3-smoke"
```

检查`manifest.json`的三轮退出码、各轮`summary.json`的守恒和失败计数，并核对`metadata.json`中的`same_cpu_union`、`effective_masks`。这里应分别看到全96CPU、两组48CPU、六组16CPU。程序本身会校验target集合与NUMA，但仍保留实际worker允许范围作为证据。没有通过绑定与守恒检查时，不进入性能对比。

可单独运行一次原生队列读取成本探针；它只使用第一个cluster，且与其他压力串行：

```bash
taskset -c 0-15 "$TARGET_METRICS" \
  --output "$TARGET_RUN/metrics-t16" --threads 16 \
  --samples 40 --batch 50 --operations 10000 --repeats 2 --queue default
```

`reads.csv`是批量读取的平均成本分布，`updates.csv`是计数更新的聚合成本；不能称为单次调用p99或完整业务插桩开销。后续才补8/64/256线程，以及固定工作量下的端到端插桩对照。

## 3. 找稳定压力区间

```bash
taskset -c "$TARGET_MASK" python3 experiments/independent/scripts/run_matrix.py \
  --binary "$TARGET_INDEP" --config "$TARGET_RUN/plan/s3-calibration.json" \
  --output "$TARGET_RUN/s3-calibration"
```

先看自动生成的`analysis.md/json`，再检查每轮`pools.csv`、`tasks.csv`和`summary.json`：是否全部完成、有无拒绝/取消/截断，后半窗口的等待是否继续增大，停流后是否还需要很久排空。30秒只是筛查；边界附近需要120～300秒复验。若720仍稳定，应增加更高压力点继续找区间；如果120已经积压，先查限额/后台负载并降低起点。不要将扫描最大点自动写成容量。

四点的名义CPU需求依次是12/24/48/72 CPU秒/秒（另有调度开销）。CPU预算模式固定线程CPU时间，它适合本轮排队研究，不能用来证明SMT或缓存改善了固定工作量吞吐。低负载稳定区、临界区和有界过载应分别报告；达到准入上限后成功任务的p99可能反而偏低。

**建议第一次先到这里。** 保存第0～3步结果后审阅，再决定240 task/s是否适合作为初始扇出对照。如果需要改任务率，生成新计划，例如加`--task-rate 120`并换输出目录；不要覆盖既有配置或只改其中一侧。

## 4. 独立任务：先集中程度，再部署，再线程数

以下命令先只跑`burst`。其结果无明显过载且工作量合同通过后，把`TARGET_PHASE`分别改为`deployment`、`threads`继续，一次一个阶段：

```bash
TARGET_PHASE='burst'
taskset -c "$TARGET_MASK" python3 experiments/independent/scripts/run_matrix.py \
  --binary "$TARGET_INDEP" --config "$TARGET_RUN/plan/s3-$TARGET_PHASE.json" \
  --output "$TARGET_RUN/s3-$TARGET_PHASE"

python3 scripts/compare_runs.py \
  --manifest "$TARGET_RUN/s3-$TARGET_PHASE/manifest.json" \
  --spec "$TARGET_RUN/plan/s3-$TARGET_PHASE-contrasts.json" \
  --output "$TARGET_RUN/reports/s3-$TARGET_PHASE-paired"

python3 stages/03-independent/predict.py \
  "$TARGET_RUN/s3-$TARGET_PHASE/manifest.json" \
  --warmup-seconds 10 --steady-end-seconds 60 \
  --output "$TARGET_RUN/reports/s3-$TARGET_PHASE-prediction"
```

burst三角对照的含义：smooth→spread主要改变到达时间集中程度；spread→local保持同一次burst和预算，单独把任务集中到同一池。默认K=100时arrival-rate为2.4批/秒，仍是240 task/s；**不是12批/秒**。随机CPU预算均值为100ms，有限样本总预算归一化，逐任务预算和cohort ID由合同核验。

读结果时依次回答：

1. `paired`中工作量/资源合同是否通过、完成率是否一致、每个seed的差值方向是否一致？`candidate − baseline`为负表示下降。
2. task的planned→finish、submit→start、callback wall分别怎样变化？线程增多后队列变短、callback wall变长，说明等待位置可能发生转移，不能只看队列深度。
3. `analysis.json`中的`native_queue_bins`是否随queue增大出现更长等待？同时检查`queue_zero`里等待超过1ms/5ms的比例、同pool/CPU目标分层与时间趋势。
4. 整轮留出预测中，pending或queue+inflight相对queue+自身预算是否降低MAE及高误差？work是合成预算信息优势，还要用误差估计对照才可判断生产可用性。

分析器中的queue是Folly用户态指标，submit→start混合排队、通知、内核调度与入口开销；callback wall−CPU也不能直接叫runq时间。原始任务之间存在burst相关性，不能将上万task当成上万独立重复。不要平均每秒p99，也不要合并不同部署计算一个相关系数。

## 5. DAG：先home部署，再改变后继放置

默认图为两波：第一波40条单节点分支，第二波10条两节点链，共60个CPU算子和2个join；每request先扇出40，再扇出10，且包含普通后继。12 QPS、每算子20ms、无RPC/长尾，名义需求约14.4 CPU秒/秒。它保留多请求重叠、初始集中、中途fork和链式唤醒，用于先隔离调度机制；20ms是初测缩放值，不冒充真实100ms。

先令`TARGET_PHASE=baseline`；完成后再改为`routing`：

```bash
TARGET_PHASE='baseline'
taskset -c "$TARGET_MASK" python3 scripts/run_matrix.py \
  --binary "$TARGET_DAG" --cases-json "$TARGET_RUN/plan/s4-$TARGET_PHASE.json" \
  --output "$TARGET_RUN/s4-$TARGET_PHASE" \
  --duration 60 --repeats 3 --seed 420 --qps 12 --cpu-us 20000 \
  --proc-interval 0

python3 scripts/compare_runs.py \
  --manifest "$TARGET_RUN/s4-$TARGET_PHASE/manifest.json" \
  --spec "$TARGET_RUN/plan/s4-$TARGET_PHASE-contrasts.json" \
  --output "$TARGET_RUN/reports/s4-$TARGET_PHASE-paired"

python3 stages/04-dag/analyze_graph.py "$TARGET_RUN/s4-$TARGET_PHASE"/r*/ \
  --output "$TARGET_RUN/reports/s4-$TARGET_PHASE-paths"
```

DAG runner自带的`analysis.md`是整轮描述，正式窗口用`paired`报告；路径报告目前覆盖完整运行，不能拿它的全轮均值与10～60秒cohort均值逐项相加。路径守恒应逐request成立。

主比较为home→initial、initial→all、all→sticky、borrow→sticky，分别回答初始fork是否值得分散、后续fork是否还需均衡、普通后继是否也需要均衡、后继优先回home还是跟随parent。观察request完成时延与完成路径上的等待，不把全图并行分支累计等待直接当成request时延。迁移次数是机制证据，不能仅用迁移任务和未迁移任务的平均差估计迁移成本，因为任务是策略选择出来的。

准确地说，all→sticky打开的是**所有非fork提交**的均衡，本图同时包含普通链后继和join；引入RPC后还包含continuation。因此该差值不能单独归因于普通链，要结合任务类型与路径报告解释，必要时再设计移除普通链的独立对照。

首轮`--proc-interval 0`固定关闭外部逐线程采样。需要内核归因时，单独增加低频采样对照，核实schedstats状态并评估扰动；不能把有采样与无采样轮次随意混成同一对照。[内核schedstat字段与计数说明](https://docs.kernel.org/scheduler/sched-stats.html)可用于区分运行CPU时间与等待运行时间。

### 必做补测：数百线程下的亲和限制

前面每池16线程的对照用于先观察机制。**16线程的none也最多并行执行16个该池回调**，它不完整复现原业务数百线程在不绑时借用更大CPU范围的能力。因此，即使16线程下三种亲和差异很小，也不能排除你提出的峰值并发限制。至少补以下每池256线程的同工作量对照；若真实值不是256，再按实际值另跑。

```bash
TARGET_HIGH='results/local/arm-pilot-t256-01'
taskset -c "$TARGET_MASK" python3 scripts/topology.py \
  --output "$TARGET_HIGH/topology-before.json"
python3 scripts/prepare_target_validation.py \
  --topology "$TARGET_HIGH/topology-before.json" \
  --cpus "$TARGET_GROUPS" --numa "$TARGET_NUMA" \
  --threads 256 --tier pilot --output "$TARGET_HIGH/plan"

taskset -c "$TARGET_MASK" python3 experiments/independent/scripts/run_matrix.py \
  --binary "$TARGET_INDEP" --config "$TARGET_HIGH/plan/s3-deployment.json" \
  --output "$TARGET_HIGH/s3-deployment"
python3 scripts/compare_runs.py \
  --manifest "$TARGET_HIGH/s3-deployment/manifest.json" \
  --spec "$TARGET_HIGH/plan/s3-deployment-contrasts.json" \
  --output "$TARGET_HIGH/reports/s3-deployment-paired"

taskset -c "$TARGET_MASK" python3 scripts/run_matrix.py \
  --binary "$TARGET_DAG" --cases-json "$TARGET_HIGH/plan/s4-baseline.json" \
  --output "$TARGET_HIGH/s4-baseline" --duration 60 --repeats 3 \
  --seed 420 --qps 12 --cpu-us 20000 --proc-interval 0
python3 scripts/compare_runs.py \
  --manifest "$TARGET_HIGH/s4-baseline/manifest.json" \
  --spec "$TARGET_HIGH/plan/s4-baseline-contrasts.json" \
  --output "$TARGET_HIGH/reports/s4-baseline-paired"
```

此处仅执行新计划的两个阶段，合计约18分钟投递时间。先核对1536个worker加辅助线程所需的pids/内存限额。分别比较16与256线程时“cluster相对none的时延差”：差异是否主要在初始扇出等待？numa是否居中？回调wall与CPU是否改变？这才直接对应原始假设。若要量化跨线程数的差值或交互，需要另声明合同并合并完整seed配对，不能直接减两个区间端点。

再用选定线程数的同一新计划执行`s4-routing`，避免只在16线程下评估路由却把结果用于数百线程业务。高线程初测保持20ms算子，后续还需在确认稳定的前提下复验100ms时间尺度。

## 6. 提高业务接近程度与确认结论

前面的默认计划是第一批实验，不是全部结论。按顺序扩展，每次只新增一个主要变量：

|新增问题|固定项与变化|现有生成器入口|
|---|---|---|
|不同稳定压力下结论是否保持|固定任务分布，按已测容量区间选低/中/高负载|阶段3`--family calibration`、`burst`；计划生成器`--task-rate`|
|20～400扇出是否改变趋势|同总task/s，K=20/100/400的local/spread对照|阶段3`--family burst`|
|三种部署是否需要不同线程数|补齐none/numa/cluster×8/16/64/256交互，使用相同offered工作|阶段3`--family deployment`|
|queue是否怕服务异质性|同均值固定/均匀/长尾；再测试有偏work估计|阶段3`--family distribution`、`estimator`|
|低扰动是否成立|固定有用循环工作，none/queue/counters/full与采样周期|阶段3`--family instrumentation`，先校准iterations-per-us|
|真实100ms时间尺度|保持图和QPS，仅逐级提高DAG预算；压力与业务预算两种问题分别报告|本计划生成器`--dag-cpu-us 100000`，或阶段4`capacity`/`production`|
|到达抖动、RPC与长尾|先各自引入，再留一组混合场景作独立验证|阶段4`continuation`/`production`，按实际case参数核对|
|局部性是否抵消均衡收益|固定工作、数据大小、同NUMA/跨NUMA候选；核实实际页位置|阶段4`locality`；当前只读数据不等同生产中间结果|

阶段4`production`是“业务参数压力”，名字不代表真实分布重放。按每算子100ms**线程CPU时间**计算，12 QPS下每request有20/40/60/200/400个算子时，需要约24/48/72/240/480 CPU秒/秒。后两个明显超过本96逻辑CPU子集的名义CPU时间上限，不能作为无损稳态对照；如果真实100ms是含RPC的墙钟，必须重新估算。`stress`里直接增加尾部会增加总需求，也不能称为同均值纯方差实验。

确认阶段用新目录生成`--tier confirm`，默认300秒/轮、预热30秒、7次重复；smoke与初筛calibration仍保持短时设置。独立任务runner从配置读取这些值，predict必须同步改为`--warmup-seconds 30 --steady-end-seconds 300`；DAG runner显式改为`--duration 300 --repeats 7 --seed 1420`（默认pilot seed为420，confirm为1420；若生成时覆盖seed，则以plan为准）。主负载参数必须沿用初测选定值。仅运行预先选定的阶段，先查看`plan.json`的时间预算；一组5策略DAG确认至少需要175分钟投递时间。

确认是否有收益，以完整seed配对为统计单位，使用相同资源和工作量合同。pilot的3轮不足以输出默认区间；confirm的7轮仍不保证能分辨0.5%～1%的差异。若区间跨0，结论是当前证据不足，不能写成等效或“没有影响”。细小收益需要根据pilot整轮差值方差、预先声明的最低有用收益规划独立确认批次。

12 QPS时，pilot的50秒测量窗口约600个request，p99附近只有约6个样本，p99差值区间应为空；270秒约3240个request才超过本工具的20个名义尾部观察门槛。这仅是最低样本提示，不保证高精度。每秒约12个request的p99近似最大值，只用于观察波动。

源码参数优先级也要注意：阶段4case里的同名参数覆盖runner较早的参数，`--extra`最后覆盖所有case。不要仅改runner的`--cpu-us`就以为已经改变生成器固化的预算；生成新计划并核对manifest最终命令。阶段3和阶段4的JSON格式不同，不能混用。

## 7. 回传结果与本轮验收

第一次建议回传拓扑、smoke、calibration及可选metrics；无需先做完整矩阵。**测试机不能上传文件时，直接复制下面的终端摘要即可。** 当前正在执行的calibration继续运行，结束后再更新脚本和采集；本次摘要工具更新不需要重新编译或重跑实验。完成这一批后再次采集拓扑，记录限额与累计throttling变化，差值仍应结合是否共享同一cgroup判断：

```bash
git pull --ff-only
# 新开终端时重新设置变量；如果前面用了其他目录，请保持一致。
TARGET_MASK='0-47,192-239'
TARGET_RUN='results/local/arm-pilot-01'
taskset -c "$TARGET_MASK" python3 scripts/topology.py \
  --output "$TARGET_RUN/topology-after.json"

# 两段可以分别复制发送；只读已有结果，不运行压力。
python3 scripts/terminal_report.py --root "$TARGET_RUN" --section environment
python3 scripts/terminal_report.py --root "$TARGET_RUN" --section runs
```

默认打印smoke三轮与calibration八轮；请保留所有轮次及FLAGS，不只摘出最快一轮。如果终端复制长度有限，将第二条拆成：

```bash
python3 scripts/terminal_report.py --root "$TARGET_RUN" --section runs --phase s3-smoke
python3 scripts/terminal_report.py --root "$TARGET_RUN" --section runs --phase s3-calibration --offset 0 --limit 4
python3 scripts/terminal_report.py --root "$TARGET_RUN" --section runs --phase s3-calibration --offset 4 --limit 4
```

`recorded/planned`用于检查整个阶段是否跑完；manifest只记录已经退出的轮次，运行期间还可能遇到暂时的JSON读取错误。失败轮显示退出码和日志路径，其他完整轮继续分析。脚本退出码1表示计划完成情况无法确认、失败或已显示产物有问题，仍应保留输出；退出码0也不认证稳态或通过配对合同。分页仅检查显示轮次的详细产物。

`LAT`使用manifest声明的窗口，保留窗口内计划到达、窗口外才完成的慢任务。`TREND`比较前后半窗口：完成速率按完成时刻计数，时延按计划到达cohort计数；`successful_unfinished`只包括最终成功的任务，有失败时不是全部积压。`SAMPLING`给出Folly队列采样数、斜率与截断情况。`CPU`为整轮含排空的进程CPU秒，不能除以20秒cohort窗口推断容量；queue插桩未采集单回调CPU时，`callback_cpu_mean_ms=?`正常。calibration出现`short_window`也正常，它提醒30秒初筛不能证明可持续容量。

后续先根据这些文本选择稳定压力点，再继续burst与部署对照。有条件传文件时也可打包整个结果目录；终端摘要不替代原始产物，请在测试机保留它们。

保留`plan/`、每阶段manifest、metadata、summary、原始CSV与日志。分析合同失败时保留原始报错，不能放宽合同直至通过。后续每批结果都应能回答：资源是否相同、工作量是否配对、有没有未完成/拒绝、是否稳定、改善落在等待还是服务执行、区间与样本量能支持多大的结论。

本次本地交付验证：上传拓扑成功生成96逻辑CPU/48核的pilot及confirm计划；cgroup采集8个fixture、目标计划14个fixture通过。根工程12组CTest通过。另将新计划缩为开发机两池、1秒投递和小CPU预算，实际完成36轮运行，验证三组独立任务和两组DAG的运行器、配对合同与图依赖分析链路。缩放结果仅证明工具流程可运行，不是ARM性能证据；目标机配额及正式实验仍以现场采集为准。

终端摘要增量验证：新增7项检查覆盖cohort慢任务、失败/缺文件后继续、部分manifest、分页、采样截断及未知配额，并用已有独立任务运行产物验证真实字段兼容；已注册为`terminal_result_report` CTest。

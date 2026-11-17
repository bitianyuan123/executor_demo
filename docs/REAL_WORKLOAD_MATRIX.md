# 阶段4 v2：每硬件cluster一池的负载与两层均衡实验

**改造目标：所选CPU范围内，每个已核实的硬件cluster对应一个Folly子池；业务只持有RequestExecutor总池。** 当前独立实验范围有6个cluster，因此创建6池。池数随实际选定cluster数变化，不能固定成4，也不能将6写成所有部署的常数。

**当前状态：4017已完成六轮既有CSV的到达核查，见[结论](TARGET_V2_4017_REVIEW.md)。不再重跑旧CPU矩阵，也不追加该批的到达诊断、trace或阈值扫描。异步完成源与驱动已通过开发机及目标机小规模功能验收；等待期间释放worker，恢复callback再推进图。当前仅执行[异步首批八轮指南](TARGET_V2_ASYNC_PILOT_GUIDE.md)，区分恢复结构、固定等待与同合同home/O候选；完成即分析，不自动扩展。目标始终每个已核实硬件cluster一个池。**

研究核心是**单个request内部的大扇出**造成home cluster短时饱和，以及借出计算资源和数据亲和的权衡；请求入口微突发是补充变量，不是替代该核心负载。

目标、实现难点及逐项验证状态集中在[验收覆盖](GOALS_AND_COVERAGE.md)。本指南用于选择下一组对照，不意味着所有负载组合、ARM性能与生产接入均已验证。

目标机已完成[4001初筛](TARGET_V2_4001_REVIEW.md)、[4002配对补证/尺度校准](TARGET_V2_4002_REVIEW.md)、[4003竞争对照](TARGET_V2_4003_REVIEW.md)、[4004入口补证/连续分支校准](TARGET_V2_4004_REVIEW.md)及[4006的24轮后继策略对照](TARGET_V2_4006_REVIEW.md)。[4008](TARGET_V2_4008_REVIEW.md)已完成task异质性，[4009](TARGET_V2_4009_REVIEW.md)又完成联合诊断与burst8。4010有界补证、4011观测对照和[4012信号/线程对照](TARGET_V2_4012_REVIEW.md)均已完成，未知业务量仍为敏感性假设，不以生产采样为前提。本页保留已有能力与后续选项，不是全部待跑清单。历史[4005 trace](TRACE_4005_REVIEW.md)支持关注短时峰值，但不决定六cluster池阈值，亦不替代目标部署策略对照。

## 1. 新增变量及其准确含义

4005之后的复核见[trace到目标实验的分步计划](TRACE_TO_TARGET_PLAN.md)：4007窗口、4008异质性、4009burst、4010补证与[观测扰动18轮](PARALLEL_COVERAGE_PLAN.md)均已完成，其后4012完成有限信号/线程干预。4013六轮机制诊断、4014九轮regular守护、4015短工作S四轮及4016 SD六轮也已完成；4017已完成[既有SD结果的到达核查](TARGET_V2_4017_REVIEW.md)，暂停扩大CPU性能矩阵；新增完成源已通过目标机小型功能验收，当前仅执行[异步首批八轮](TARGET_V2_ASYNC_PILOT_GUIDE.md)。固定延迟及对齐恢复已实现，真实RPC/随机分布及更一般的内部ready释放节奏仍待独立设计，不能将本页所有变量组合都视为当前必跑矩阵。

| 参数 | 作用 | 保持与变化 |
|---|---|---|
| `--arrival regular` | 固定请求间隔 | 原默认保持 |
| `--arrival poisson` | 指数分布请求间隔，固定seed | 与regular同固定工作，具体到达序列不同 |
| `--arrival burst --burst-size B` | 每批B个请求，批次周期`B/QPS`秒 | 平均批次强度为QPS；默认B=4 |
| `--burst-spacing-us U` | 同批第i个请求计划在`批起点+i×U`微秒到达 | 0为同时计划到达；`(B-1)×U`不能超过批周期 |
| `--sync-block-percent P --sync-block-us U` | 由seed/request/tag确定P%的算子，执行固定计算后在callback内同步sleep | 默认0/0；保留线程，**不是异步RPC，也不是100ms CPU的模拟** |
| `--async-percent P --async-delay-us U --async-align-us G --async-capacity N` | 单辅助完成源，算子返回释放worker，固定延迟或网格到期后通过Continuation恢复 | 新affinity-v3-async合同；功能验收已通过，首批八轮筛查待目标机执行；不模拟真实网络，未ready等待不计入子池Q/I/O |
| `--pool-count N` | 令软件池数独立于硬件partition数 | 仅`affinity=none`可与partition数不同；原默认仍一partition一池 |
| `--policy active` | queue +已开始callback计数的评分对照 | callback包括睡眠、抢占，不能称kernel runnable |
| `--policy cpu` | 缓存的worker CPU/运行队列等待时间与queue组合评分 | 后台采样；无效或过期时明确回退到outstanding |
| `--cpu-sampling 0/1` | 单独启停后台采样以测其成本 | cpu策略默认1且不允许0；其余策略默认0 |
| `--cpu-sample-ms 50 --cpu-max-age-ms 150` | 采样间隔与路由可接受的最大样本年龄 | 是试验起点，不是已经验证的生产值 |

burst的计划时间为：

`scheduled_offset_ns(id) = floor(floor(id/B) × B × 1e9 / QPS) + (id mod B) × U × 1000`。

请求实际发起延迟单独记录，后续任务变慢不会修改计划时间。最后一批可不足B个；短运行的“首末到达跨度除请求数”不等于完整批周期定义的长期QPS。正式比较建议requests为B的倍数，并同时检查提交滞后和拒绝。

## 2. 固定工作量与真实尺度

每算子计算迭代数、访存次数依赖seed、request ID、逻辑算子位置。改策略、软件池数、亲和、到达序列或同步sleep不会重新抽取CPU/数据工作。输出新增：

- `planned_total_iterations/accesses`与每request均值，可核对同一固定需求；完整trace会核验实际总量。
- `fixed_work_signature`排除到达方式、QPS和同步sleep，仅用于证明固定计算/访存合同相同；它**不能替代相同到达序列的配对条件**。
- `work_signature`仍包括到达和阻塞合同；同seed、相同arrival hash与资源条件的不同策略才自动配对。
- `planned_sync_block_us`与实际观测sleep墙钟分开；实际时间可能包含额外调度等待。

现有`--widths/--depths`已允许20/80/128/400初始扇出，并支持后续fork与串行链。它们是压力端点，不是生产图大小分布。把第一波从20改到400而保持每算子工作相同，会增加总需求；不能把时延差全归因于并发形状。节点异质性用`--jitter-percent`和`--long-every/--long-factor`，范围为task或request。长尾倍率会提高平均工作，当前没有暗中减小主体工作来凑均值；比较不同异质设置时须检查实际总迭代/访问量并重新校准。

“100ms算子”需要先确定是线程CPU时间还是不含排队的墙钟。16 QPS × 80算子 × 0.1 CPU秒会要求128 CPU秒/秒，已超过独立实验96逻辑CPU的CPU时间上限；这不证明真实业务过载，因为真实算子数分布、100ms口径及executor占比尚不完整。不要用sleep 100ms补足合成算子时长，也不要把96个SMT线程直接当96个物理核吞吐。

## 3. 主线、对照与历史配置

| 实验角色 | 池数与CPU范围 | 用途 |
|---|---|---|
| 主线 | 每个选定硬件cluster一池；当前6池绑定6个cluster | 校准扇出瓶颈、线程过配、L2指标及数据亲和 |
| 亲和控制组 | 保持相同6池、每池线程数、同96CPU并集；none/numa/cluster | 单独识别绑定范围的影响；none/numa允许池间共享CPU |
| 历史重放，可选 | 明确指定4池×128、none、16 QPS | 复查旧采集形态，不参与目标策略排名，不设为必跑步骤 |

目标模式下容量按本cluster CPU数和capacity-scale配置，并受worker数上限约束。线程数128不意味着128CPU容量；8物理核/16逻辑CPU也不意味着SMT吞吐恰好翻倍。none的CPU并集/池数只是共享资源的名义份额，不可替代cluster的局部容量。

当前程序`--affinity cluster`会绑定输入分区，检查CPU可用、不重叠、每分区同NUMA，**没有自动识别或核验物理cluster边界**。必须把已核实的topology中每个硬件cluster的CPU列表作为一个分号分区。开发机默认前两个CPU的人工分区只用于功能测试，不能当真实cluster结果。下例使用已确认的六cluster子集，真实NUMA为0和2。

## 4. 按每cluster一池分步推进

先更新工程。每条matrix缺少`--run`时只输出计划；确认负载后才在同命令中加`--run`并使用新output。已有新总池二进制不需因本次矩阵/文档纠偏而重编C++；首次使用仍按下列构建入口。

```bash
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
BUILD_JOBS=1 ./scripts/build_stage.sh affinity
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
```

### 4.1 cluster home校准：先突出单request内部扇出

固定6池、每池16线程、cluster绑定，低QPS逐级调固定工作，使CPU/request与初始扇出需求到合理量级。`iterations`是工作次数，不代表固定微秒。12请求用于功能和量级判断，不能用于p99结论。

```bash
build-affinity/affinity_demo --cpus "$PARTITIONS" \
  --affinity cluster --threads 16 --policy home --requests 12 --qps 1 \
  --arrival regular --widths 20,4 --depths 1,2 \
  --model none --accesses 0 --iterations 1000000 \
  --max-inflight 32 --memory-mb 512 \
  --output results/local/v2-cluster-calibration-01
python3 experiments/affinity/report.py results/local/v2-cluster-calibration-01
```

用选定工作参数再增加初始宽度至80、400，分别记录总工作增量；固定每算子工作而增加宽度是容量压力试验。若单独研究“等总工作但更尖峰”，先用none模型、无抖动，比较单波`widths/depths=20/20、80/5、400/1`，三者均400算子；链深与关键路径也随之改变，不能称为只改硬件并发。不同图形不自动配对，每种图形内部再比较home/借出。

观测16CPU忙约30ms是校准参照，不是要求每个合成请求都强行达到的值。不能用sleep补齐CPU时间，也不为拟合历史active=80而任意调整参数。多request阶段再扫QPS，例如6/12/16；出现持续积压或拒绝就先退回解释容量。

### 4.2 可选补充：冻结工作量后改变入口到达形态

主线可以从4.1直接进入4.3与4.4：先保持regular到达、固定单request图，验证线程过配、L2借出和数据亲和。下方入口burst对照不是必跑前置步骤，可在核心对照完成后用于检查到达形态稳健性；不能用多request同时到达代替单request内部大扇出。

`cluster-baseline`明确强制home/cluster；要求显式`--affinity cluster`和`--cpus`，池数由分区导出，`--pool-count`仅允许省略或0。线程数/QPS完全来自调用者，不强制128或16。四例比较regular、Poisson、burst4和burst8，后两者是合成压力形态，不是因临时四池而选择的目标池数。

```bash
python3 experiments/affinity/run_matrix.py \
  --suite cluster-baseline --seeds 420 \
  --output results/local/v2-cluster-entry-plan-01 --base-args \
  --cpus "$PARTITIONS" --affinity cluster --threads 16 --qps 12 \
  --requests 96 --max-inflight 128 --memory-mb 512 \
  --widths 20,4 --depths 1,2 --model none --accesses 0 --iterations 1000000
```

将iterations替换成4.1冻结的值；QPS也是显式实验变量。不同到达序列不做同轨迹策略配对。runner末尾的`Report:`仅打印建议命令，不会执行它；实际运行后还需执行`python3 experiments/affinity/report.py <运行目录>/seed-*`。nearest-rank下96请求的p99就是最大值，只能初筛。

### 4.3 先比较线程数和信号，再决定借出策略

保持cluster绑定。`threads`套件按8/16/64/128/256线程/池扫描home，回答等待在哪一层；128是过配变量的一档，不是生产推荐值。固定一个线程档，再跑`signals`比较home/queue/active/outstanding/cpu/work。正式策略比较不应同时修改线程数。

```bash
python3 experiments/affinity/run_matrix.py \
  --suite threads --seeds 420 \
  --output results/local/v2-cluster-threads-plan-01 --base-args \
  --cpus "$PARTITIONS" --affinity cluster --qps 12 \
  --requests 96 --max-inflight 128 --memory-mb 512 \
  --arrival regular --widths 80,4 --depths 1,2 \
  --model none --accesses 0 --iterations 1000000

python3 experiments/affinity/run_matrix.py \
  --suite signals --seeds 420,421,422 \
  --output results/local/v2-cluster-signals-plan-01 --base-args \
  --cpus "$PARTITIONS" --affinity cluster --threads 128 --qps 12 \
  --cross-numa 0 --arrival regular \
  --requests 96 --max-inflight 128 --memory-mb 512 \
  --widths 80,4 --depths 1,2 --model none --accesses 0 --iterations 1000000 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope request --estimate-mode constant
```

第二例有意在过配档检验Q盲区，随后以16线程重复同一组策略。加入可重复的request异质性，避免所有cluster同一时刻负载相同、没有可借资源；它是控制变量，不声称真实分布如此。长尾提高平均工作，先重新核验稳定性，再冻结同一逐request工作比较策略。

### 4.4 数据亲和与局部借出是主线的必要验收

none只是机制基线，不能据此确定生产迁移策略。使用branch-rw或mixed时，重点研究的扇出分支必须有`depth>=2`，使后继确实读取并修改前驱的数据。不能仅把4.3的`--model none`改成branch-rw：其`widths=80,4 / depths=1,2`中，初始80条分支都没有普通后继，不足以充分检验这批迁移分支的parent/home取舍。

可从[亲和工程指南的分支链校准与routing示例](../experiments/affinity/README.md)中的`widths=20,10 / depths=2,2`开始，再扩大初始宽度。改变深度、模型、足迹或访问次数后，重新校准并冻结整份图和访存/计算工作清单；每种策略使用相同清单。depth=1与depth=2的原始数值不能作为同工作量策略配对。完成这一步后：

1. `routing/signals`：home与同NUMA借出，固定`--cross-numa 0`；记录请求尾时延、算子等待、CPU/request、执行墙钟和迁移比例。
2. `follow`：parent/home × fork/all；固定一个评分策略，比较链的数据复用和回迁。现成suite使用outstanding，其余评分用相同负载的直接命令。
3. `granularity`：chunk1/4/8，观察一次扇出集中投递和预占成本；每个算子仍独立入队。
4. 单独开启`--cross-numa 1`并扫描罚项/min-gain/hop；只在同NUMA资源不足时解释远端收益，不能把none下的逻辑池迁移当物理NUMA迁移。
5. 固定6池/每池线程数，用`deployment`对比none/numa/cluster；检查同CPU并集和同工作合同，不能把4池none插入该单变量对照。

数据大小、最大驻留请求和每请求分支数共同决定足迹；参见[亲和工程指南](../experiments/affinity/README.md)。无拒绝、稳定排空、足够请求与多轮交错重复后，再讨论p99改善幅度和策略拐点。

同步阻塞反例继续用`blocking`，但同样传入上面的六分区与`--affinity cluster`，固定线程档。该套件比较0%/50%算子同步sleep10ms与home/active/outstanding/cpu；它检验“callback很多不一定CPU很忙”，不模拟异步RPC。详细追踪、后台采样开销另以overhead或home采样0/1成对验证。

## 5. CPU采样的证据与限制

采样器按固定worker TID集合后台读取Linux线程schedstat，计算同窗口CPU时间增量和runnable等待时间增量除以墙钟时间。它没有在`add()`热路径读取proc文件；路由读取已发布的缓存。两个值是历史窗口的累计需求，不是瞬时idle线程数。50ms窗口会漏掉比它短得多的微突发，因此CPU只能作为慢反馈对照，不能替代当前queue/outstanding。

运行队列计账不可用、样本初次未建立、线程消失或样本陈旧时，CPU策略退回outstanding，并在summary/任务trace记录次数与原因。报告不能把这种运行的改善称为CPU信息收益。可只读检查：

```bash
cat /proc/sys/kernel/sched_schedstats
```

本实验不自动更改内核全局开关。报告列出采样次数、失败组数、总扫描CPU/墙钟和最大单次扫描墙钟。cpu策略的额外采样成本纳入整策略对照；要分离该成本，可为home与候选同时设置`--cpu-sampling 1`复测。trace开关对照仍独立进行。

## 6. 终端结果如何解读

report按计划到达选择request cohort，窗口后才完成的请求仍保留；要求全请求有终态。拒绝/失败运行不会成为性能配对。请求mean/p95/p99、该cohort算子CPU/墙钟和等待，与整轮process CPU/request分开显示。

手工收集终端结果时，保留每run的`pool_count/affinity/threads`、`matrix suite/experiment_role`、工作合同及最后的配对说明。报告从实际summary读取池数；仅在直接父目录的metadata和plan能确认归属时显示矩阵角色，否则为`manual_or_unclassified`。手动校准命令没有矩阵元数据是正常情况，不会因此被排除分析；角色标签也不证明CPU分区确为硬件cluster。不同池数/绑定不能配成策略delta，相同资源与工作下cluster-baseline和signals可以配对。

随后使用同一时间窗口边界打印诊断，先检查home，再查看候选策略：

```bash
python3 experiments/affinity/diagnose.py results/local/v2-cluster-calibration-01 \
  --bin-ms 1000
# 上方96请求/12QPS的regular初筛投递约8秒，用投递期内的[1,6)小窗。
# report也传相同warmup/window；下列目录替换成实际已执行目录。
python3 experiments/affinity/diagnose.py results/local/v2-cluster-signals-run-01/seed-* \
  --warmup-seconds 1 --window-seconds 5 --bin-ms 1000
```

初筛窗口不是稳态证明或正式p99样本设计。正式轮次先增加请求数与运行时长，核对报告中的最后计划到达时间，再预声明测量窗口；例如投递不足8秒时不能用`[5,15)`讨论负载期平均占用，后半段已经是停流后的排空/空窗。若要专门研究排空，可另列该窗口并保留其名称。

条件分位针对Q/I/O与task等待/执行墙钟，按operator记录提交点选样；首波针对计划到达在窗口内的request，分别报告提交记录跨度、最早提交→全部启动，以及最早callback开始→最晚callback完成的跨度（`stage0_callback_span_ms`）；时间趋势计入所有跨窗口请求/callback，区分计划到达、实际投递与完成/积压。三个总体不可混加。它们不直接给出request p99因果关系或30ms CPU饱和证据。完整排空只说明有限工作完成，仍需检查负载期间积压和lag是否增长。阶段3条件关系用于筛选信号；v2在目标线程档/图/工作集下重新测关系、冻结阈值做策略干预，之后用留出的seed/邻近压力复验。

callback occupancy由所有与墙钟测量窗口相交的`start_ns..finish_ns`区间重建，输出每池与全局的时间加权均值/峰值，包含同步睡眠和被抢占时间；它和“在提交时采到的inflight平均数”口径不同，也不等于Linux runnable/on-CPU。历史20/80已知是临时配置的单池值；即使确认同样统计定义，也不能要求目标cluster池复现它。应比较事件机制与负载规模，并重新测目标部署的占用。报告的全池峰值来自合并区间，并非各池峰值相加。

## 7. 历史四池重放仅按需保留

`--suite legacy-four-pool-replay`保留原四池/128线程/none/16QPS的到达形态对照，角色元数据标为`historical_capture_replay`。旧名`real-baseline`保持相同行为但输出弃用说明，避免让历史命令悄悄变成另一种实验。它不属于目标cluster矩阵的必跑项；是否重放由分析旧轨迹的具体问题决定。

即使重放，也必须单独保存CPU全集和负载合同：历史未绑核采集的实际allowed CPUs未确认，不能声称在96CPU子集上已精确复现原运行。已有四池数据保留原标签，不重命名、不拼入六池策略收益，不把历史单池active阈值迁移到cluster池。

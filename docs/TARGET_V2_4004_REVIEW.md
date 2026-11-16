# ARM v2：4004补证完成，进入后继去向对照

依据 `4004.log`，SHA256 `30b8ed482557e15c89bc8ea7aa3f5693a7e1a8ceee3bafb32da9847e2db8b05a`。前半是4003既有12轮的只读flow补证，后半是3轮新的连续分支校准，不能当作15轮新的独立策略实验。原始CSV留在目标机，本文核对回传终端和源码。另收到的[4005历史trace审阅](TRACE_4005_REVIEW.md)是并行的机制证据，不改变六cluster主线。

## 1. 当前决策

1. 入口诊断已补齐：burst计划0.7ms，实际attempt跨度均值1.795～3.985ms，确实发生批内拉宽；它不能再仅解释为整批平移。
2. 扣除入口lag后，burst的O策略attempt→完成均值仍改善2.259～3.315ms；入口差异不是全部收益来源，但也不能把余下差值全部算成L2本身收益。
3. 三个新工作集均12/12完成、peak-inflight=1、171回调/请求。冻结300万迭代和每operator十万次访问，64KiB作为首个数据工作集；256KiB保留为敏感性复验，不继续校准以追逐精确30ms。
4. 下一批为同形状none/branch64，各比较home、all-parent、all-home、fork-parent，三个seed共24轮。先regular12QPS，约8分钟投递时间；burst、256KiB与跨NUMA延后，避免一次改变太多问题。

## 2. 旧4003的入口补证

仍用计划到达 `[2,18)` 的192个成功请求；每轮完整17472回调。均值可以分解，分位数不能相减。

| 到达 | seed | home：计划时延 = lag + attempt后，ms | O：计划时延 = lag + attempt后，ms | attempt后均值改善 ms |
|---|---:|---|---|---:|
| regular | 420 | 44.063 = 0.111 + 43.952 | 25.057 = 0.057 + 25.000 | 18.952 |
| regular | 421 | 45.355 = 0.086 + 45.269 | 25.450 ≈ 0.056 + 25.393 | 19.876 |
| regular | 422 | 41.128 = 0.056 + 41.072 | 23.251 = 0.057 + 23.194 | 17.878 |
| burst8 | 420 | 58.883 = 1.144 + 57.739 | 56.851 = 2.085 + 54.766 | 2.973 |
| burst8 | 421 | 60.136 = 1.107 + 59.029 | 57.408 = 0.638 + 56.770 | 2.259 |
| burst8 | 422 | 55.820 = 0.703 + 55.117 | 53.313 = 1.511 + 51.802 | 3.315 |

burst的attempt后改善约5.15%/3.83%/6.01%，与计划端3.45%/4.54%/4.49%的差别来自lag变化，方向并不总相同。`submitted_ns`仍是driver构造RequestState、准入与root add前的尝试时刻；attempt后还包含这些步骤、全部图执行及调度成本，不能当作独立测得的Folly等待。

每轮24个完整burst，无窗口裁切或尾部不足；计划批内跨度均为0.700ms。

| seed | home实际attempt跨度均值/p99 ms | O实际attempt跨度均值/p99 ms | 首attempt lag均值 home/O ms |
|---:|---|---|---|
| 420 | 2.520 / 6.229 | 3.985 / 10.538 | 0.057 / 0.083 |
| 421 | 2.407 / 5.737 | 1.795 / 5.200 | 0.057 / 0.057 |
| 422 | 2.198 / 10.073 | 2.891 / 5.325 | 0.058 / 0.057 |

首个attempt通常仅晚约57μs，而整批跨度明显增加，证明批内投递拉宽。具体是driver抢占、构造、连续提交或其他成本，需要更细时序才能归因；没有根据把发生器移到未授权的额外CPU，或改变其优先级来使数字更好看。现有burst仍有peak-inflight=8的竞争价值，但实际到达并非理想0.7ms注入。这也是先做regular后继对照、之后再复验burst的理由。

## 3. 原来的迁移主要发生在哪里

regular/O的全部迁移均在首波stage0，三个seed为10368/10366/10368次。burst/O首波迁移7604/8139/8078次，占总迁移97.21%/97.31%/97.53%；普通后继只有26/36/18次。旧图每请求只有4个普通后继，主要回答首次借出，不能充分回答连续数据依赖下的后继去向。

burst的parent策略也观察到83/80/68次 `source!=home && chosen==home`。这是选池结果回到home，不表示命中了 `Reason::ReturnHome`：通用候选搜索也可能选择home。为下一批增加离线 `routing reasons` 列，直接解码CSV已有原因，分别看LocalSpill、ReturnHome、NonFork、HopBudget等，不增加热路径记录。

当前v2的ReturnHome原因必须同时满足source非home、chosen为home；未知原因数值显式显示，矛盾的ReturnHome记录报错。原因列不是每次候选评分或low阈值检查的计数，只表示该回调最终携带的选池原因。请求和任务的数值、旧CSV均不变，不必重打印4003/4004旧结果。

## 4. 连续分支校准：已经可以冻结

六个已核实cluster各一池、128线程/池、名义容量16、home、1QPS、seed420，无抖动/长尾。图80/4宽、2/2深，168个operator、2个join、1个init；普通后继增至84个/请求。每轮2052回调、迁移与跨NUMA均0，没有请求重叠。

| 指标 | none | branch64 | branch256 |
|---|---:|---:|---:|
| 请求均值 ms | 54.507 | 78.985 | 89.364 |
| 进程CPU ms/请求 | 741.517 | 1025.283 | 1086.445 |
| 首波stage0 callback跨度均值 ms | 29.988 | 47.913 | 49.333 |
| init CPU/墙钟均值 ms | 0.103 / 0.105 | 2.700 / 2.713 | 7.894 / 7.920 |
| 普通后继等待均值 ms | 15.304 | 20.304 | 20.936 |
| 普通后继CPU/墙钟均值 ms | 4.390 / 6.999 | 5.984 / 10.316 | 6.279 / 11.126 |

三档每请求固定5.04亿计算迭代；两档branch每请求1680万次访问，每次读写对应分支buffer。64→256KiB使请求均值增加10.379ms，init墙钟增加5.207ms，约占差值一半。这是初始化/first-touch不可忽略的证据，不能把全部差值都解释为cache miss，更不能据home-only实验宣称cluster亲和收益已经测出。

两档branch首请求抽查的64页都在node0，与请求0的home一致；只证明这次有界抽查，不代表其他请求、全部页或后续迁移都已验证。none的page状态empty符合模型。每档12请求不用于p99推断；首波跨度为 `max(stage0.finish)−min(stage0.start)`，包含同时开始的后继对资源的干扰，不是CPU饱和证明。

选择64KiB是先限制初始化占比、保留分支内读写依赖的工程选择，不是认定64KiB接近真实业务或能命中某级cache。256KiB已经校准过，后续可直接作为工作集敏感性对照，不需要再从校准开始。none保留新图形状，避免拿旧4003深1/2冒充同形状参照。

## 5. 下一批矩阵与判读规则

| 维度 | 固定设置 |
|---|---|
| 资源 | 六cluster分区不变；128线程/池、capacity-scale1；max-inflight16、memory512MiB |
| 图与计算 | widths80,4 / depths2,2 / iterations3000000 |
| 工作族 | none、accesses0；branch-rw64KiB、accesses100000；两族shared-kb0 |
| 到达/异质 | regular12QPS、240请求；请求级±25%，hash约1/8×4长任务 |
| 策略 | home、all-parent、all-home、fork-parent；L1 RR、chunk1、max-hops4 |
| 阈值/域 | high1、low.5、min-gain0；仅同NUMA，cross-numa0 |
| 观测 | trace1、cpu-sampling0、page-sample0，各候选一致 |
| 重复/窗口 | seeds420/421/422；计划到达 `[2,18)`，192请求/轮，晚完成保留 |

每轮41040回调；窗口32832回调，其中普通后继16128个。branch64在16请求上限时payload与状态约84.164MiB，满足512MiB预算；这不是总RSS上限。1QPS校准的CPU/请求乘12只作平均需求量级参照，恢复异质性会增加需求，实际仍检查lag、peak、拒绝及积压，不能为拟合原业务50%CPU持续调大每operator耗时。

同一工作族、同seed内部只变策略，runner打乱顺序；seed421反转两个工作族顺序，缓解时间漂移，不宣称消除机器状态干扰。两个工作族分别配对分析，不合并时延或checksum，不把不同访存工作的差值算成纯迁移成本。两族策略差异只能帮助判断结果对数据访问是否敏感，不能单独隔离cache的因果代价。

- all-parent对所有允许的ready提交点评估O，优先实际父池；低负载时仍留父池，忙时才借出。
- all-home满足home≤low及权限/hop条件时回迁；需要看到普通后继 `ReturnHome` 实际出现，才算该分支被覆盖。它不保证home比parent更空闲。
- fork-parent只在addBatch选池，普通后继**及join**绕过均衡。因此all-parent/fork-parent是完整策略比较，不能声称只改变普通后继而join完全不变。
- home是固定请求亲和参照。观察被迁移task的均值不能替代同工作整轮比较，因为迁移选择本身有偏差。

首先验收合同/checksum、无失败/拒绝、同资源和观测，再看ordinary successor、join的原因与迁移，最后比较请求mean/p95/p99、attempt后时延、进程CPU/请求、callback等待与墙钟。若all-home无ReturnHome，或fork/all路径实际上没有差异，保留结果，后续再单独调整一个阈值或压力；不把“策略没有激活”误写成算法等价。

192请求仍是机制初筛，不能将窗口p99直接排名为生产最优。下一阶段在路径差异与容量成立后，冻结候选，增加请求量、独立重复和留出压力做正式尾部复验；burst和256KiB用于检验结论是否翻转，不在本批自动追加全矩阵。

## 6. 可直接执行的下一批命令

只更新Python分析和文档，**无需重编C++或Folly**。新目录已存在时停止；原始数据全部保留。每个seed/工作族跑完打印report和flow；本批不用重复打印长篇Q/I/O分桶，已有diagnose仍可事后按需运行。约8分钟仅指投递时间，另有24次建池、排空和报告。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
FOLLOW_ROOT='results/local/v2-locality-follow-01'
mkdir -p results/local
mkdir "$FOLLOW_ROOT"
for FOLLOW_SEED in 420 421 422
do
  WORKLOAD_ORDER='none branch64'
  if [ "$FOLLOW_SEED" -eq 421 ]; then
    WORKLOAD_ORDER='branch64 none'
  fi
  for WORKLOAD_CASE in $WORKLOAD_ORDER
  do
    FOLLOW_MODEL=none
    FOLLOW_BRANCH_KB=0
    FOLLOW_ACCESSES=0
    if [ "$WORKLOAD_CASE" = branch64 ]; then
      FOLLOW_MODEL=branch-rw
      FOLLOW_BRANCH_KB=64
      FOLLOW_ACCESSES=100000
    fi
    FOLLOW_BLOCK="$FOLLOW_ROOT/$WORKLOAD_CASE/seed-block-$FOLLOW_SEED"
    python3 experiments/affinity/run_matrix.py \
      --suite follow --cases home,all-parent,all-home,fork-parent \
      --seeds "$FOLLOW_SEED" --output "$FOLLOW_BLOCK" --run --base-args \
      --cpus "$PARTITIONS" --affinity cluster --threads 128 --capacity-scale 1 \
      --requests 240 --qps 12 --arrival regular \
      --widths 80,4 --depths 2,2 --iterations 3000000 \
      --model "$FOLLOW_MODEL" --branch-kb "$FOLLOW_BRANCH_KB" --shared-kb 0 \
      --accesses "$FOLLOW_ACCESSES" --jitter-percent 25 --long-every 8 --long-factor 4 \
      --work-scale-scope request --estimate-mode constant \
      --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
      --busy-high 1 --return-low 0.5 --min-gain 0 \
      --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 1 --page-sample 0 \
      --max-inflight 16 --memory-mb 512
    python3 experiments/affinity/report.py "$FOLLOW_BLOCK"/seed-* \
      --warmup-seconds 2 --window-seconds 16
    python3 experiments/affinity/flow_report.py "$FOLLOW_BLOCK"/seed-* \
      --warmup-seconds 2 --window-seconds 16
  done
done
)
```

回传终端输出即可；可以分段收集，但不要只挑收益最好的一轮。若中断，保留已经完成的block，只恢复缺失的block到新目录，避免复用已有plan或覆盖结果。4004的补证与校准无需重做，历史trace补证不阻塞本批。

## 7. 工程与本地验证

新增flow原因列、已知ReturnHome一致性检查与测试，未改变CSV或运行路径。原因映射与当前C++枚举顺序有自动校验，未知数值保持可见。flow的12项测试通过，CTest的affinity_flow/affinity_diagnose/affinity_functional三项通过。按本节命令生成6个block、24条ARM参数计划，仅检查而未执行ARM尺度负载；本地缩放两个人工CPU分区、2线程/池、4/2宽度和2/2深度，两个工作族四策略全部12/12完成，6组home配对通过，flow实际输出NonFork与ReturnHome原因。后者仅验证参数、配对和原因输出，不冒充ARM性能结果。

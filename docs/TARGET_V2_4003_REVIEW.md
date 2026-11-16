# ARM v2：4003请求竞争结果与分支链亲和实验

依据用户回传 `4003.log`，SHA256 `b153e6240d3ebf97b0e7e1491b6c78ebae975c73bcc3b53496ad04628af9b2ae`。目标端执行的是951a76b版本，已完成4002约定的12轮实验及report/diagnose。本文依据终端输出核对结果，未收到原始CSV，未在开发机重跑ARM性能实验。

## 1. 本轮已经回答的问题

在**六个硬件cluster各一个池、128线程/池、同NUMA借出**下，固定这份无大块数据依赖的计算负载：

- regular 12QPS时，outstanding（简称O）相对home的窗口请求均值降低43.13%～43.89%，整轮进程CPU增加0.60%～0.81%。
- burst8、平均仍12QPS时，窗口请求均值仅降低3.45%～4.54%，整轮CPU基本持平。请求同时在途峰值由regular的2变为8，竞争对照已经产生实际重叠。
- O的首波跨度和task等待仍有改善，但请求竞争明显压缩了可借资源的收益。不能把regular约43%的收益直接外推到多请求微突发。

这是当前工作合同下的描述性策略干预证据；不是O优于所有信号的证明，也不是生产p99承诺。下一步可以进入数据亲和与后继去向，不再重跑这12轮或反复调计算量拟合30ms。

## 2. 资源、负载与统计总体

主线仍为当前选定六cluster，CPU分区 `0-15;16-31;32-47;192-207;208-223;224-239`，NUMA为0/0/0/2/2/2。总计96逻辑CPU、每池名义容量16；容量是路由归一化参数，不表示SMT有等比例计算吞吐。每池固定128个worker，共768个。没有使用历史四池配置。

L1按request做RR，L2只比较home与O；O按 `(accepted且未完成的任务数+路由预占数)/容量` 评分，采用parent/all/chunk1，high=1、low=.5、min-gain=0、max-hops=4、禁止跨NUMA，后台CPU采样关闭。借出的是尚未提交的ready回调；没有抢占或搬移已经入队的任务。

每请求为80/4两波、深1/2：88个业务operator、2个join、1个init，共91个回调。每operator基准300万固定迭代，无大块内存访问；请求级±25%抖动及hash决定约1/8的4倍长任务。相同seed的两种到达使用相同逐请求工作；regular与burst的到达合同不同，不能跨到达做同合同策略配对。

每轮240请求、12QPS。regular间隔83.33ms；burst每批8个、批内计划间隔100μs（计划跨度0.7ms），每批约666.67ms。程序报告全部12轮240/240完成、无失败/拒绝，6组同seed同到达的home/O通过配对审计。各seed整轮固定迭代依次为91,140,720,000、88,094,160,000、85,374,960,000。

正式描述窗口预先固定为计划到达 `[2,18)`，每轮192请求（ID 24～215），完整回调17472个；diagnose的operator总体为16896个。burst窗口有24个批次。迟到的投递与完成保留，不按完成时间删尾。CPU、lag p99和peak-inflight来自整轮240请求，不能冒充192请求窗口统计。

## 3. 请求时延与进程CPU

表中时延为窗口成功请求的计划到达→完成；CPU变化为整轮O相对home。改善百分数按 `(home−O)/home`，不是跨seed汇总后的置信区间。

| 到达 | seed | home均值 ms | O均值 ms | 均值改善 | home/O p99 ms | 整轮CPU变化 |
|---|---:|---:|---:|---:|---|---:|
| regular | 420 | 44.063 | 25.057 | 43.13% | 141.800 / 83.109 | +0.604% |
| regular | 421 | 45.355 | 25.450 | 43.89% | 157.724 / 92.676 | +0.783% |
| regular | 422 | 41.128 | 23.251 | 43.47% | 144.667 / 84.288 | +0.814% |
| burst8 | 420 | 58.883 | 56.851 | 3.45% | 167.812 / 146.713 | −0.298% |
| burst8 | 421 | 60.136 | 57.408 | 4.54% | 164.036 / 142.577 | −0.008% |
| burst8 | 422 | 55.820 | 53.313 | 4.49% | 167.747 / 133.761 | −0.084% |

本轮p99全部同向改善，但每轮192请求的p99只由最末约2个样本决定，burst内请求还有相关性。不能据此选择生产最优策略或宣称稳定的尾部收益；三个seed也没有消除机器状态和到达族执行顺序的影响。burst的绝对均值改善仅2.032/2.728/2.507ms，需要下面的入口分解才能判断其中多少来自提交后的执行过程。CPU的微小负差先解释为基本持平，不声称节省计算资源。

## 4. 等待、首波与负载信号

| 到达 | seed | 首波跨度均值 home/O ms | task等待p99 home/O ms | 窗口O路由迁移/请求 |
|---|---:|---|---|---:|
| regular | 420 | 32.721 / 13.352 | 59.785 / 18.306 | 54.00 |
| regular | 421 | 33.728 / 13.546 | 63.107 / 18.749 | 53.99 |
| regular | 422 | 30.603 / 12.354 | 57.628 / 17.245 | 54.00 |
| burst8 | 420 | 37.320 / 32.954 | 60.527 / 46.897 | 40.74 |
| burst8 | 421 | 38.435 / 32.750 | 63.439 / 46.132 | 43.56 |
| burst8 | 422 | 35.795 / 30.310 | 57.698 / 44.945 | 43.14 |

表中首波跨度对应 `stage0_callback_span_ms`，是首波stage0回调从最早开始到最晚完成，即 `max(finish)−min(start)`；它不是从最早提交起算，也不是整个request或16CPU持续onCPU的长度。diagnose另报投递记录跨度和最早投递→全部启动时间。task等待包含Folly队列及被调度前的时间，不能由此拆出内核runq；其起点在L2选池后，也不含完整选池成本。迁移是source池与chosen池不同，不是Linux线程迁核。

regular时O把单请求首波callback并发峰值的逐请求均值由约25～26提高至约51；burst时由约26提高至约44～46。burst全局callback峰值约148～187，**包含被抢占/睡眠的闭包，并不表示有187个CPU同时执行**。该值也不能对齐到历史单池active20/80。O的全局窗口callback占用均值反而更低，符合闭包更快结束的现象，不能据此认为它减少了每operator工作量。

本轮home仍出现明显Q积压；Q=0桶的task等待p99为regular约0.015～0.016ms、burst约0.081～0.090ms，没有充分复现“队列空但大量已领取任务排内核runq”的反例。本轮也没有运行queue策略，故不能宣称已证明O优于Q。阶段3的Q盲点证据仍有效，v2需要针对目标线程档进一步验证。

例如seed420 regular/home的Q/C桶 `0,(0,.5],(.5,1],(1,2],(2,4],(4,8]` 对应task等待中位数约 `0.004,0.033,0.121,5.351,13.524,18.581ms`。这是投递采样、同负载下的条件关系，能说明排队压力与等待相关；它不是 `request p99=f(Q)`，也不能从O改变了选择后的桶分布直接推断因果收益。

4秒趋势格未见持续边界积压增长；burst每格48个到达/投递/完成，格边界积压为0。但格边界可能落在批间空闲段，不能当成整个窗口没有积压或稳态容量已经验收。整轮进程CPU约0.51～0.55CPU秒/请求，按12QPS仅约6.2～6.6CPU时间等价，远未复现原业务整体50%CPU。当前价值是局部突发受限与同NUMA竞争，不是持续全机饱和。

## 5. 必须补查：lag不等于burst被拉宽

整轮请求提交lag p99（home/O）为：regular三个seed `0.060/0.058、0.605/0.059、0.058/0.059ms`；burst为 `5.679/9.987、4.658/7.223、9.523/5.023ms`。

数毫秒lag只能说明计划时刻与实际尝试投递有差距。整批共同后移也会提高lag，却不拉宽批内跨度；所以不能凭这些p99断言“0.7ms burst实际变成了10ms”。需要读每批request的实际attempt时间。producer和辅助线程继承所选CPU并集，与worker共享资源，但现有日志不能区分抢占、request构造、上一请求提交等原因。

新增只读 `flow_report.py`，复用完整输入审计，不增加热路径观测：

- 对成功的计划到达cohort计算 **mean计划到达→完成 = mean投递lag + mean attempt→完成**。p99各自计算，不能相减。失败/拒绝单列，不能从比较中隐去。
- 按request ID/8分批，只对窗口内完整批次输出计划跨度、实际attempt跨度及首attempt lag；迟于窗口的attempt仍保留，边界裁切和尾部不满批次单列。
- 将回调按精确v2 tag分为init、首波stage0、后续波stage0、普通链后继、join，分别报告等待/CPU/墙钟、路由迁移与回home，并核验数量守恒。

request的 `submitted_ns` 是driver在准入检查、RequestState/mmap构造和root add**之前**记录的尝试时间，不能叫root入队时刻。attempt→完成仍包括这些构造和提交成本。role中的回home计数只说明source非home而chosen为home，不直接识别触发的阈值分支。

以下只读已有结果，**无需重编C++或Folly，也不重跑压力**。请保留所有输出；无需重贴旧report/diagnose。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
PAIR_ROOT='results/local/v2-cluster-scale3-pair-01'
python3 experiments/affinity/flow_report.py \
  "$PAIR_ROOT"/regular/seed-* "$PAIR_ROOT"/burst/seed-* \
  --warmup-seconds 2 --window-seconds 16
)
```

## 6. 下一批：只检查三个连续分支负载

为了比较“借出后留在当前池”与“回request home”，把深度改为 `2,2`，让第一波80个分支都有一个读取前序结果的普通后继。图成为168个operator+2个join+1个init=171回调。维持300万迭代，暂时1QPS、无抖动/长尾、home、12请求，每个home池覆盖2请求。

| case | 模型 | 每分支buffer | 每operator随机访问次数 | 每request业务payload |
|---|---|---:|---:|---:|
| branch-0 | none | 0 | 0 | 0 |
| branch-64 | branch-rw | 64KiB | 100000 | 5.25MiB |
| branch-256 | branch-rw | 256KiB | 100000 | 21MiB |

这是建立新工作合同的三点初查，不是在继续调旧负载达到精确30ms。新none控制深度增加带来的计算成本；operator数比4003增加约90.9%，因此不能把与4003的时延差全算作cache成本。两个branch档保持相同访问次数，但容量、索引和数据不同，不作为同工作策略配对。100000次只是可复现起点，64/256KiB并不预先代表某级cache命中/失配。

每分支独占页分隔buffer，两个stage串行访问同一buffer及前序标量；初始化在home触碰所有分支页，随后借出可能产生局部性代价。不同分支没有共享写争用，join只读取标量，后续波使用独立buffer；计算循环与内存循环分开，不是生产指令级交错模型。后续需要用mixed/request-ro及其他工作集验证通用性，不据这个模型直接定最终算法。

最大在途统一设16，memory512MiB：84个256KiB分支加每分支128B状态，16请求约336.164MiB（当前档大小也适配64KiB页）；原max-inflight128会超过512MiB预算并被拒绝。该预算不是总RSS，不含线程栈/trace。`page-sample=1`仅对首请求做有界页位置查询；不保证所有页面固定在home，也不证明cache命中。查询不可用时报告unknown，不替换成推测值。

以下新建结果目录，已有则停止。仍是六cluster各一池，不改线程数、不跨NUMA；预计投递共约33秒，另加启动、排空与报告。若某个case超过1秒/请求并出现重叠，保留结果先解释，不自动提高QPS。先读第5节旧数据再执行本节即可。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
LOCALITY_ROOT='results/local/v2-locality-chain-calibration-01'
mkdir -p results/local
mkdir "$LOCALITY_ROOT"
for BRANCH_KB in 0 64 256
do
  LOCALITY_MODEL=branch-rw
  LOCALITY_ACCESSES=100000
  if [ "$BRANCH_KB" -eq 0 ]; then
    LOCALITY_MODEL=none
    LOCALITY_ACCESSES=0
  fi
  build-affinity/affinity_demo \
    --cpus "$PARTITIONS" --affinity cluster --threads 128 --capacity-scale 1 \
    --requests 12 --qps 1 --arrival regular --seed 420 \
    --widths 80,4 --depths 2,2 --iterations 3000000 \
    --model "$LOCALITY_MODEL" --branch-kb "$BRANCH_KB" --shared-kb 0 \
    --accesses "$LOCALITY_ACCESSES" --jitter-percent 0 --long-every 0 \
    --work-scale-scope request --estimate-mode constant \
    --policy home --home-policy rr --continuation parent \
    --balance-at all --batch-chunk 1 --cross-numa 0 --max-hops 4 \
    --cpu-sampling 0 --trace 1 --page-sample 1 \
    --max-inflight 16 --memory-mb 512 \
    --output "$LOCALITY_ROOT/branch-$BRANCH_KB" \
    >"$LOCALITY_ROOT/branch-$BRANCH_KB.log" 2>&1 || {
      cat "$LOCALITY_ROOT/branch-$BRANCH_KB.log"
      exit 1
    }
done
python3 experiments/affinity/report.py \
  "$LOCALITY_ROOT/branch-0" "$LOCALITY_ROOT/branch-64" "$LOCALITY_ROOT/branch-256"
python3 experiments/affinity/diagnose.py \
  "$LOCALITY_ROOT/branch-0" "$LOCALITY_ROOT/branch-64" "$LOCALITY_ROOT/branch-256" \
  --bin-ms 4000
python3 experiments/affinity/flow_report.py \
  "$LOCALITY_ROOT/branch-0" "$LOCALITY_ROOT/branch-64" "$LOCALITY_ROOT/branch-256"
)
```

回传三份工具的终端输出即可。先检查全部完成、固定工作/访问量、memory/page信息、init成本、普通后继CPU/墙钟、首波跨度与peak，再冻结工作集。每档12请求只检查量级、工作构成与可运行性，不评价p99。depth2的首波stage0跨度还会受已开始stage1的资源竞争影响，不能与旧depth1等同解释。

## 7. 随后的策略比较和验收边界

收到上述结果后选择固定branch工作集，使用已有 `follow` 套件过滤 `home,all-parent,all-home,fork-parent`，同工作、同到达、同采样设置、多seed比较：

1. home给出cluster亲和基线；all-parent允许各ready点借出，后继优先延续父执行池。
2. all-home在home降至low阈值时允许回home；并不是无条件回迁。当前返回路径在busy判断前，不能解释为所有迁移都经过min-gain与跨NUMA惩罚。
3. fork-parent只在fork均衡，普通链后继留在父池，用于隔离持续均衡的额外收益/代价。

`fork-home`在当前实现下也绕过普通后继回迁，仅在后续addBatch可能与fork-parent不同，暂不把它当独立的普通后继回迁对照。先regular12QPS，再保留burst竞争作为复验；是否增加请求量用于尾部推断，需在新工作合同与容量确认后固定。此次不提前跑完整策略矩阵，不同步改变阈值、跨NUMA与工作集。

本阶段后续验收仍包括：其他信号/异质成本/同步阻塞的鲁棒性，none/numa/cluster及线程档的受控部署比较，跨NUMA候选代价，chunk/阈值/滞回，观测成本与留出压力复验。不是全都放进下一批，也不把已有独立任务条件关系当成DAG请求尾时延模型。第五阶段的能力归属、API与生命周期验收仍有意义；当前高层总池未迁入Folly默认实现。

## 8. 本次工程变更与验证

只新增离线flow诊断、对应测试及本文和入口更新，并澄清首波callback跨度的计时起点。没有修改C++负载、调度算法、计数热路径或Folly，旧结果保持原合同。

本地8项flow测试覆盖共同平移与拉宽的区别、窗口边界/不满批次/迟到attempt、失败/无trace、v2角色守恒和非法输入审计；CTest的affinity_functional、affinity_diagnose、affinity_flow三项全部通过。另用两个本地小池、2线程/池、4/2宽度、2/2深度、5000迭代/64次访问，验证none/64/256KiB三个工作集到report/diagnose/flow的连通性，每轮12请求、180回调；额外一个9请求burst4验证完整2批与尾部不满1批的识别。本文两个shell片段通过语法检查。本地检查不提供ARM性能结论。

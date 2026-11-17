# 4011之后：计数信号与线程过配的24轮对照

**当前状态：[4020四轮A1机制覆盖](TARGET_V2_4020_REVIEW.md)已完成，O的业务与恢复等待下降，伴随约3.74%的CPU/请求代价；每格一次，正式性能与生产代表性验收仍未完成。冻结合成模型、计数评分与阈值，不自动追加目标机性能测试或生产trace。下一步按[当地实施手册](PRODUCTION_AGENT_PLAYBOOK.md)或[纯prompt](PRODUCTION_AGENT_PROMPTS_ONLY.md)交接：只读核实、本地设计、已有授权内功能验证与预先定预算的验收；访问不足可止于离线交接。目标始终每个已核实硬件cluster一个池。** 下文保留当时证据/命令，不是新的执行清单。

## 1. 本批目的和边界

[4010/4011结论](TARGET_V2_4010_4011_REVIEW.md)已完成旧trace补证和详细观测开关对照。本批固定已验证的burst8工作合同，回答：

1. 在同线程档内，以Q、Q+I或O作为借出依据，相比home是否改善整request时延与CPU/请求？
2. 从每池16线程到128线程，这些策略的净收益是否变化？是否出现“队列缩短而整request更慢”？
3. 简单Q是否足够，还是补充生命周期计数有可见收益？

这是**评分信号 × 线程数**的有限干预，不是生产负载复现，不建立通用负载→p99公式，也不搜索全部最优阈值。目标仍为每个硬件cluster一个池，本机六池。保持L1 RR、L2 all-parent、同NUMA借出；选择all只是给各信号相同的后继决策机会，不是宣告其优于fork。

| 维度 | 取值 |
|---|---|
| 信号/策略 | home、queue、active、outstanding |
| 每池线程 | 16、128；两档名义容量都为16 |
| seed | 420、421、422 |
| 追踪 | trace0作为性能主比较；保留request统计与必要调度计数 |
| 运行顺序 | 每seed内8格一起打乱，串行执行；总计24轮 |
| 其他 | 完整延续4009/4011负载、数据、到达、CPU范围与NUMA规则 |

Q为原生队列估计，I为已进入未结束callback（包含抢占/阻塞），O为已接纳未结束任务，R为预留。`queue=(Q+R)/C`、`active=(Q+I+R)/C`、`outstanding=(O+R)/C`。active不是仅I，O不是剩余CPU时间。

三种count策略使用相同名义high=1、return-low=0.5、min-gain=0默认值；这是初始评分方案对照，不能据其结果宣称某信号在分别充分调参后仍最优。parent模式下low不是本批主动回home策略。线程减少可能同时改变用户队列和内核等待；不能把时延变化全部叫内核runq改善。

本总池即使使用queue/home仍维护部分生命周期计数；因此本批比较信号选择对路由的影响，不测“原始Folly仅Q”相对“新增I/O维护”的全部成本。后者需独立插桩开关/接入对照。

## 2. 工程更新

无需重新编译C++或Folly。同步工作分支本轮更新，矩阵工具新增：

`--suite signals --cases home,queue,active,outstanding --thread-counts 16,128`

先筛选策略，再展开为`threads-16-home`等8个case，最后按seed打乱。不要另传`--base-args --threads`，工具会拒绝冲突；线程维度只能用于signals。原有矩阵未指定该选项时行为不变。参数和源码/二进制摘要仍写metadata，具体命令写plan，失败即停止且保留产物。

以下全部针对**已有独占96逻辑CPU测试环境**，不作为真实业务直接上线命令。环境间不需要直连，按现有人工同步方式更新脚本即可。若二进制或硬件条件已变化，先记录变化，不直接与旧轮次混配。

## 3. 可直接执行

脚本功能检查不施加目标压力：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
python3 experiments/affinity/tests/functional.py --matrix-only
)
```

正式到达期合计约8分钟，另加启动、排空与报告时间。每轮240请求，正式窗口仍为计划到达`[2,18)`的192请求/24个burst。若发生拒绝/错误，保留终端与失败目录；不得降低到达率后仍当作同一合同的成功轮次。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
SIGNAL_ROOT='results/local/v2-burst-count-threads-01'
mkdir -p results/local
mkdir "$SIGNAL_ROOT"
for SIGNAL_SEED in 420 421 422
do
  SIGNAL_BLOCK="$SIGNAL_ROOT/seed-block-$SIGNAL_SEED"
  python3 experiments/affinity/run_matrix.py \
    --suite signals --cases home,queue,active,outstanding --thread-counts 16,128 \
    --seeds "$SIGNAL_SEED" --output "$SIGNAL_BLOCK" --run --base-args \
    --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 \
    --requests 240 --qps 12 --arrival burst --burst-size 8 --burst-spacing-us 100 \
    --widths 80,4 --depths 2,2 --iterations 3000000 \
    --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 100000 \
    --jitter-percent 25 --long-every 8 --long-factor 4 \
    --work-scale-scope task --estimate-mode constant \
    --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
    --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 0 --page-sample 0 \
    --max-inflight 16 --memory-mb 512
  python3 experiments/affinity/report.py "$SIGNAL_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
  python3 experiments/affinity/flow_report.py "$SIGNAL_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
done
)
```

signals套件不允许基础参数显式覆盖busy-high/return-low/min-gain；本批有意使用count默认值。不要将CPU/work两类加入同一批。所有8格同期执行，已有4011的128/O结果只作历史旁证，不能替代本批同期O/home控制。

只需按seed复制**完整终端**，包括错误、报告和到达诊断；CSV/metadata留在目标机。trace0的任务迁移/等待/评分诊断不可得，新终端表将迁移和跨NUMA列显示为“-”，运行摘要显示migrations=unavailable；旧summary中的默认零仍不能当真实零。

## 4. 收到结果后如何分析

先审计24轮终态、完整窗口、工作/到达/checksum、实际burst跨度和lag。原report在同线程档内以home为基线，每seed应有6组策略/home配对；不同线程档不会被工具当成“资源完全相同”的配对。

随后逐seed列：

- 同线程数下：Q、active、O相对home的mean/p95/p99与整轮CPU/请求。
- 同策略下：128相对16的差。明确这是改变worker预算的干预；保持CPU并集、分区容量、业务固定工作和到达不变，不把它混进同资源策略优势。
- 信号间：active−Q、O−Q，以及差值随线程档的变化。它们描述本合同与默认阈值的净结果，不是对某计数“准确率”的直接测量。

预先保留三种决策分支：

1. 若16线程总体不差、Q与O差异很小，优先研究较少worker与简单策略；这不自动证明含阻塞/RPC的生产也适用。
2. 若128线程只有补充计数才改善，选少量对照轮加trace1，检查Q盲区、实际路由机会、评分/阈值与callback占用；不要只凭小幅均值差宣称O精确。
3. 若所有策略相近或共同恶化，先查到达实现、同NUMA竞争、是否触发借出与尾部波动，不立即加跨NUMA或同时改负载。trace0不能证明“完全没有迁移”，必要时只补有明确问题的诊断。

每格仍192请求、24个相关burst，p99缺乏稳定精度。三seed不是同工作三次重复；有接近1%的差异时先做固定合同交错重复/延长窗口，再谈最优。当前不自动追加一整套阈值、线程和负载乘积。

## 5. 与真实应用调优的分工

本批检查的是受控机制与实现；生产agent按[独立手册](PRODUCTION_AGENT_PLAYBOOK.md)先确认本机Folly路径、绑定/内存、业务段与观测成本，再制定当地可执行实验。它可以在已授权范围内按预先梯度迭代，不能照抄本页CPU编号、128线程或合成工作量。

未知任务粒度、RPC、共享工作集保持显式假设。4010没有补出这些量，因此当前不以其新增桶统计修改负载，也不继续等待它才能开展本批测试。

## 6. 本地验证记录

新增线程维度的计划测试覆盖三seed共24格、展开后统一随机、实际metadata，以及非法/重复线程档、与base线程冲突、错误suite及阈值覆盖的拒绝；失败参数不创建输出目录。相关4项CTest（functional、diagnose、flow、trace_cost）通过，并回归trace0不能显示已测迁移零值。

开发机用两个可用逻辑CPU的人工分区、每池2/4线程、4/2宽与2/2深、少量固定工作跑了八格，每格16/16完成；同资源策略配对和flow到达报告成功，实际解析阈值核实为high1/low0.5/gain0。本文bash命令语法检查通过，24条ARM计划与4011逐项核对，除本批指定的信号/线程/trace与固定all外不改变工作合同。ARM规模仅生成计划，未在开发机执行；不把开发机功能结果解释为目标机性能。

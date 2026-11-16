# ARM v2：4006后继策略对照完成，进入分支异质性验证

依据 `4006.log`，SHA256 `1b39467acb3772c6f4abe34e11e9df347bd6e8b3754beb6f252856a13b9753b4`。核对回传终端、参数和当前源码，原始CSV仍在目标机；没有把终端摘要冒充本地重新审计全部原始记录。

## 1. 当前进展与结论

4004布置的24轮已全部完成，不再重跑。仍为六个已核实cluster各一个Folly池、128线程/池、capacity16、相同96逻辑CPU，真实NUMA为0和2。L1 RR固定request home，L2仅借用同NUMA池，没有回到历史四池配置。

- 6个工作族/seed block，每个4策略，全部240/240完成、失败/拒绝0；窗口按计划到达`[2,18)`选192请求，每轮32832回调，晚完成保留。
- 每个block的work/arrival/checksum签名一致，18组home配对输出齐全，跨NUMA均0。不同seed或none/branch64不是同工作合同。
- 初始扇出借用其他cluster在这批负载上有明显收益；加入连续分支数据访问后仍成立。
- 后继持续均衡路径确实激活，但相比只在fork均衡没有显示稳定的额外均值收益。暂以fork-parent作为迁移较少的参照，保留all-parent作为反例挑战对象，不宣称最优算法已经确定。
- 下一批仅做branch64、task级异质性、home/all-parent/fork-parent、三个seed，共9轮。用同request内分支偏斜检验持续均衡是否有价值；all-home保留实现，暂不继续全矩阵扫描。

## 2. 时延与CPU代价

下表是窗口请求均值，单位ms。按seed独立呈现，不把各轮p99平均成总体p99。

| 工作族 | seed | home | fork-parent | all-parent | all-home |
|---|---:|---:|---:|---:|---:|
| none | 420 | 75.500 | 38.323 | 38.135 | 38.340 |
| none | 421 | 76.046 | 38.419 | 38.672 | 38.319 |
| none | 422 | 70.164 | 35.482 | 35.362 | 35.243 |
| branch64 | 420 | 106.427 | 57.278 | 57.589 | 56.908 |
| branch64 | 421 | 108.108 | 57.792 | 56.681 | 57.153 |
| branch64 | 422 | 99.427 | 52.762 | 53.168 | 52.635 |

三个借出策略相对各自home，none均值降低49.15%～49.77%，branch64降低45.89%～47.57%。整轮240请求的进程CPU代价分别为−0.095%～+0.243%和+0.955%～+1.555%。CPU与时延的窗口不同，不能用窗口operator CPU/request直接与整轮process CPU/request相减解释额外开销；三组seed工作总量也不同。

branch64在相同CPU/数据工作次数下以约1%～1.6%的进程CPU增量换取明显时延下降，是当前合同下可接受代价的候选证据，是否值得由业务SLO决定。它没有单独测cache miss、远端访存或各项调度CPU成本，不能将增量全归于缓存迁移，也不能称所有真实数据亲和场景都已验收。

| 工作族 | home窗口p99范围 ms | 三种借出策略窗口p99范围 ms |
|---|---:|---:|
| none | 242.251～266.902 | 126.370～144.191 |
| branch64 | 332.929～360.287 | 175.816～210.352 |

所有同seed借出相对home的样本p99均下降；每轮仅192请求，名义尾部约2个请求。不能据此区分候选的生产p99、宣称置信区间或将跨seed范围作为不确定性区间。

入口lag均值：none/home为0.120～0.160ms，借出0.056～0.058ms；branch64/home为0.150～0.241ms，借出0.061～0.132ms。扣除lag后均值收益仍约34.6～51.3ms，入口差异不是主要来源。分位数不能做同样减法。attempt→finish仍包含构造、准入、root提交和图执行，不是直接测得的Folly队列等待。

## 3. 后继策略为什么没有拉开

窗口内每轮16128个ordinary successor。这次与旧depth1实验不同，后继路径已经充分存在：

| 工作族 | all-parent后继迁移/16128，seed420/421/422 | fork-parent后继迁移 | all-home后继ReturnHome次数 |
|---|---|---:|---|
| none | 6000 / 5937 / 5871 | 0，全部NonFork | 87 / 125 / 85 |
| branch64 | 6169 / 6240 / 6145 | 0，全部NonFork | 128 / 114 / 141 |

all-parent普通后继迁移约36.4%～38.7%；all-home连同其通用借出约37.5%～39.5%。但all-parent相对fork-parent的均值差，none依次−0.188/+0.253/−0.120ms，branch64为+0.311/−1.111/+0.406ms，方向不一致。all-home相对all-parent也没有跨seed一致优势。all-home相对fork-parent在branch64的差为−0.370/−0.639/−0.127ms，幅度小，仍不足以确定稳定收益。

机制线索是：fork-parent已经把80条链分散到本NUMA多个池（包括home），普通后继仍可由**同一个子池的不同worker**并行领取；保持父池不等于保持原线程或串行整池。当前同request的所有算子共用成本倍率，链形状也规则，因此首波分散后，各分支的后续工作相对均衡。这是对结果的解释假设，下一批task级异质性正用于挑战它。

普通后继submit→start均值，home/借出分别约：none 17.982～18.832ms / 2.013～2.152ms；branch64 22.805～23.768ms / 2.371～2.411ms。只在fork借出已经同时降低了后继等待，额外约6000次后继重选尚未明显进一步改善这些均值。这里的wait含提交后的Folly交接及获得CPU前的等待，不能全叫Folly物理队列驻留时间；wall−CPU也不能自动全算kernel runq。

all-home的ordinary ReturnHome占比仅0.53%～0.87%，它实际触发了，但覆盖强度有限。该策略普通后继约2200～2360次`source!=home && chosen==home`的大部分仍是LocalSpill选择home；all-parent也有约2100～2260次这种结果。因此不能把这些全部叫低阈值回迁，也不能据当前接近的结果否定积极回迁。join中ReturnHome占比更高，但join没有真实大块多父数据融合。之后如需专测回迁，应单独改变low或数据模型并检查原因计数，不在下一批同时调阈值。

home、fork/all还会改变join路由规则；fork/all的性能差是完整策略差，不能称只改变ordinary successor而其他路径完全相同。全部迁移数都是新ready任务的选池变化，不是OS线程迁移或从队列偷取任务。

## 4. 当前能回答与仍不能回答的问题

已经得到目标部署的定量结果：在当前规则到达、低请求重叠、80/4宽和2/2深、request级长尾、同NUMA的合同内，初次借出大幅降低请求时延，64KiB分支数据没有抵消收益；后继进一步迁移很多，但额外收益没有稳定体现。none的peak-inflight为2～3，branch64为3～4，距离max-inflight16仍远，这不是整个目标部署的容量拐点或持续拥塞结论。

尚未验收的是：Q/I/O哪一种更适合判断过载、同request内慢分支、竞争更强时fork是否足够、较大/共享工作集、跨NUMA代价、异步RPC、短算子观测开销和正式尾部稳定性。4006没有负载分桶报告，不能据此给出负载→p99函数；先用现有CSV做第5节的只读诊断。

历史4005的进一步解析为可选辅助，已有[只读窗口工具](TRACE_TO_TARGET_PLAN.md#2-历史trace还值得解析什么)可以打印服务集中与同期压力。不需要重扫旧大trace、恢复四池配置或为本轮1%级候选差异立刻新采调度轨迹。当前最高价值证据来自目标cluster的新反例实验。

## 5. 下一步A：从已有CSV补负载条件诊断，不重跑

先用既有seed列表的第一项420、branch64的三个策略，打印Q/I/O条件分桶及首波/积压信息。这是事后机制诊断样例，不代表全部seed；三seed策略收益仍用完整结果比较。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
DIAG_BLOCK='results/local/v2-locality-follow-01/branch64/seed-block-420'
python3 experiments/affinity/diagnose.py \
  "$DIAG_BLOCK/seed-420-home" \
  "$DIAG_BLOCK/seed-420-fork-parent" \
  "$DIAG_BLOCK/seed-420-all-parent" \
  --warmup-seconds 2 --window-seconds 16 --max-bins 4
)
```

这是所选池在选池**之后**的提交点样本，能给出callback等待/墙钟的条件分布，不是时间加权繁忙比例或无偏预测。Q为0仍可能存在已领取任务、被抢占callback；须看条件分布而非先认定Q无效。现有Q/I/O各自分桶是边缘分布，不能相乘推导Q≈0且I高的联合比例。首波跨度也不能证明CPU连续饱和30ms。若原CSV缺失，保留错误并继续B，不为诊断重跑整批。

## 6. 下一步B：9轮分支异质性实验

问题明确为：**同一request内部存在快慢分支时，仅fork分散是否仍足够，all-parent是否获得持续收益？**

固定branch64、regular12QPS、六cluster、128线程/池、工作量基准与阈值，唯一负载参数变化为`--work-scale-scope task`。仍是±25%扰动、hash约1/8×4；它把长短分配到不同operator，而不是整request一起变长。实际总迭代/访问量和关键路径分布会变化，不声称与4006严格等总工作；新block内三策略必须同合同/到达/checksum，4006仅提供机制参照。

不重编，不改C++或Folly。使用全新目录；约3分钟是9轮的投递时间，不含建池、排空与打印。若失败/拒绝，runner停止并保留证据，不在同一轮降低QPS或增大准入上限掩盖问题。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
SKEW_ROOT='results/local/v2-locality-task-skew-01'
mkdir -p results/local
mkdir "$SKEW_ROOT"
for SKEW_SEED in 420 421 422
do
  SKEW_BLOCK="$SKEW_ROOT/seed-block-$SKEW_SEED"
  python3 experiments/affinity/run_matrix.py \
    --suite follow --cases home,all-parent,fork-parent \
    --seeds "$SKEW_SEED" --output "$SKEW_BLOCK" --run --base-args \
    --cpus "$PARTITIONS" --affinity cluster --threads 128 --capacity-scale 1 \
    --requests 240 --qps 12 --arrival regular \
    --widths 80,4 --depths 2,2 --iterations 3000000 \
    --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 100000 \
    --jitter-percent 25 --long-every 8 --long-factor 4 \
    --work-scale-scope task --estimate-mode constant \
    --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
    --busy-high 1 --return-low 0.5 --min-gain 0 \
    --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 1 --page-sample 0 \
    --max-inflight 16 --memory-mb 512
  python3 experiments/affinity/report.py "$SKEW_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
  python3 experiments/affinity/flow_report.py "$SKEW_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
done
)
```

回传A、B终端即可，可分两份。B仍是机制筛选，不把9轮各192请求合并宣称独立大样本；保留全部seed，不只发最快轮。

判读顺序：先看同合同/成功/lag及peak；再看all-parent普通后继迁移、等待、wall/CPU与fork-parent之差；最后看整request mean/p95/p99及CPU代价。若持续均衡仍无稳定收益，保留fork-parent作为下一档压力的基准，并在新图上复验burst；若它在慢分支下获益，再固定该合同比较Q/active/O及16/128线程。branch256与回迁low、跨NUMA一次只加一个问题。v2非阻塞RPC仍是后续工程项，不能用同步sleep或历史trace代替。

## 7. 工程状态

本轮没有改变运行负载实现、总池策略或Folly。新增历史JSON窗口摘要及自动测试，修订结果报告和当前入口；4004命令保留为已完成实验的合同记录。

本地已通过：窗口分析11项测试（包含实际原分析器JSON结构集成），CTest的sched_trace_analysis/sched_window_analysis/trace_analysis_tools三项；文档B命令生成3个block、9条ARM参数计划且逐项核对，未执行ARM尺度压力。开发机以两个可用CPU分区、2线程/池、4/2宽与2/2深、少量固定计算/访问、task作用域运行三策略，全部12/12完成，2组home配对通过，report/flow/diagnose均成功。命令块通过bash语法检查。上述仅为功能和指南验证，不是新的ARM性能数据。

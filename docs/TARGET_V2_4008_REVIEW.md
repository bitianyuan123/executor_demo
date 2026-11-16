# 4007/4008结果：分支异质性完成，下一步验证突发竞争

依据回传终端，未取得目标机原始CSV。本报告核对命令、合同校验和摘要，不冒充本地重算全部原始记录。

- `4007.log` SHA256：`fc3f16019ddfacd51272ab3ae1ec682f825e64cab73d60e0426b389f506c4c23`。
- `4008.log` SHA256：`9ea9830e78fcbe1fd78714cabd0750c911cb5a14ad7e386e80be70d61f8adb4f`。

## 1. 决策与主线

**更新下一批负载形态，保留当前运行策略和阈值。** task级长短算子已得到验证，暂不需要重写负载发生器。下一批只把regular改成burst8，检验多个请求同时竞争时，仅在fork均衡是否仍足够。持续压力门控保留为候选设计，尚未实现或验收，不能将3ms写成已验证默认值。

部署始终是六个已核实cluster各一池：CPU分区`0-15;16-31;32-47;192-207;208-223;224-239`，真实NUMA为0和2，128线程/池、名义capacity16。L1 RR固定request home，L2使用outstanding，仅允许同NUMA借出。没有转向历史四池未绑CPU配置。

完成状态：4006的24轮后继策略对照、4008的旧CSV诊断和9轮task异质性、4007的已有JSON窗口解析均已回传；不重跑。当前执行入口是本页第6～7节。

## 2. 4008新实验：异质分支下fork仍是合适参照

三seed × home/fork-parent/all-parent，共9轮。每轮240/240完成，失败/拒绝0；正式计划到达窗口`[2,18)`为192请求、32832个callback，其中16128个ordinary successor。同seed的work/arrival/checksum一致，六组home配对齐全，跨NUMA均为0。三seed的工作总量不同，不当作同工作量独立重复。

固定合同：regular12QPS、宽80/4、深2/2、每算子基准300万计算迭代及10万访问、branch-rw 64KiB/分支、task级±25%扰动且hash约1/8为4倍长任务、constant估计、chunk1、high1/low0.5、hop4、CPU采样关闭、trace1、max-inflight16。low控制回home，不是持续压力释放门槛。

下表为请求窗口mean / p99，单位ms；不把各轮p99平均成总体p99。

| seed | home | fork-parent | all-parent |
|---|---:|---:|---:|
| 420 | 121.817 / 148.732 | 76.413 / 107.875 | 76.472 / 113.669 |
| 421 | 123.002 / 156.351 | 76.565 / 108.439 | 76.579 / 111.726 |
| 422 | 123.483 / 154.629 | 77.686 / 106.019 | 77.384 / 120.913 |

两种借出相对home均值改善37.09%～37.75%；整轮process CPU变化−0.172%～−0.032%，应视为基本持平，不能宣布获得可靠CPU节省。入口lag差约0.04～0.10ms，远小于约45～47ms均值收益。CPU是整轮口径，时延是窗口口径，不能直接相减分解成本。

all-parent相对fork-parent的mean差为+0.059/+0.014/−0.302ms，方向不一致；其样本p99三个seed都较高，差+5.794/+3.287/+14.894ms，是需要跟踪的风险信号。每轮仅192请求，名义最末1%约2个请求，不能据此认定生产p99劣化或最优策略已确定。

| seed | all普通后继迁移/16128 | all / fork / home普通后继wait均值 ms |
|---|---:|---:|
| 420 | 5338，33.10% | 2.631 / 2.650 / 23.742 |
| 421 | 5480，33.98% | 2.673 / 2.703 / 23.860 |
| 422 | 5225，32.40% | 2.633 / 2.642 / 23.942 |

fork普通后继全部NonFork、迁移0；每轮额外五千余次后继跨池没有明显再降低等待。此处迁移是新ready任务的选池变化，不是搬队列任务或OS线程迁移。fork-parent仍可在父池的多个worker上执行，绝非固定线程串行。

**为什么继续挑战fork，而不宣布fork足够：** 借出策略peak-inflight全部为2，home仅2～3。首波80分支已散到本NUMA三个池，随机快慢分支可能在低重叠下被摊开；少数已运行慢分支也不能靠重新路由后继来搬走。这些是机制假设，尚未直接重建关键路径。task异质性已经存在，下一缺口是多个请求争抢相同池，而非再次声明“需要加入慢算子”。

本次约37%与4006约46%的收益不是严格等工作的退化比较：request→task作用域改变了实际工作量和关键路径分布。只在各自合同内配对解释。

## 3. 4008前半：Q有信息，但任何单个计数都未验收

前三份diagnose来自**4006、branch64、request作用域、seed420的旧CSV**，不是本次task异质性。每份按operator提交点选32256个样本，不包括init/join；不能与32832个请求cohort callback混为同一分母。

- home的Q=0时wait p99为0.018ms，Q/C∈(2,4]为74.861ms，(4,8]为96.393ms。原生Q在这些样本中明显有描述性信息，不能称它普遍失效。
- Q=0也不保证等待为零：fork/all的wait p99为0.854/2.954ms；对应callback wall p99为48.970/46.129ms。Q不能描述已经领取任务的执行、阻塞或抢占。
- I与wait不稳定单调：all的I/C∈(.5,1]、(1,2]，wait p50分别5.855/1.385ms；home低I也有较长等待。只看“活跃多”不足以概括待领取工作。
- O分桶大体有描述性趋势，但实际仅O用于本轮路由，且观测在选池之后；不能用这种选择后的样本确定O胜过Q或active。

wait为选池、预留和诊断读取之后的提交标记至包装callback开始，包含后续交接、排队和获得执行机会的等待；不是单独Folly物理队列驻留或kernel runq。wall−CPU也不能全部叫runq。

原报告Q/I/O是三组边缘分布，不能推导Q=0且I高的联合比例。新加的`diagnose.py --joint-load`读取同一现有CSV行，输出Q零/非零 × I/C≤1/>1 × O/C≤1/>1，最多八格及样本数、请求/池数、wait/wall分位数。它不加运行时探针、不需重编。多次relaxed读取仍不是原子快照；这些比例是选池后operator样本占比，不是时间繁忙比例、独立样本或request p99预测验收。原CSV缺字段就明确unavailable，不补零。

旧样例的首波提交跨度均值home/fork/all为0.070/0.120/0.117ms，首波callback跨度为67.311/28.969/29.338ms。后者不是CPU连续饱和时长，却明确提示：延迟3ms确认之后，首波工作通常已经完成提交；当前路由器只处理随后新ready任务，不能把已入队首波再分散。

## 4. 4007：支持长短集中段并存，不验证3ms门控

4007只读同一历史10秒JSON，读取其中已有1ms/10ms桶序列，并非两次新采样。coverage仍unknown，四池身份仍有历史映射限制。

1ms结果中，最忙10%桶承载各池45.532%～54.827%的CPU服务。按`onCPU > 2×本池均值`定义的高服务段，p50均为2ms、p95为15～20ms、max为25～55ms，短段和较长集中段共存。1115个2×段、496个4×段不是外部agent的1266个N过载事件；例如pool_b的2×阈值仅8.728个等价CPU，并非统一N>16。

至少一个池超过2×阈值的4280桶中，3550桶只有一个池高，占82.94%；4×时为98.29%。同期onCPU均值/峰值24.646/83.291，observed demand为25.886/92.320。两个峰值未必同一时刻。它支持同时保留“单池集中”和“多池竞争”两种实验，不证明其他池idle，更不能推导六cluster目标中同NUMA可借容量。

外部提出的N、激活率和持续性阈值仍需复核。`7.8/8900≈0.876ms`只有对象、生命周期、边界一致才可解释为驻留时间，不能由线程唤醒推出算子CPU时间；也不能据此断言20任务在16CPU上约1ms全部排空。按最终峰值分组使用了未来信息，不能直接作为在线判据。详细论证、门控状态/成本设计与可选补证见[持续压力审阅](PERSISTENT_PRESSURE_REVIEW.md)和[给测试机agent的审计提示词](TRACE_DURATION_AUDIT_PROMPT.md)。旧trace补证不阻塞目标实验。

## 5. 下一步只改变什么

下一批为**branch64、task作用域、burst8 × home/fork-parent/all-parent × 三seed，共9轮**。平均12QPS、全部工作参数和部署保持不变，每批8请求、计划相隔100µs，批周期约666.7ms、计划批内跨度0.7ms。实际attempt跨度和lag由flow报告核验；不能把计划值当实际同时到达。

RR六home下，连续burst的两个NUMA请求数会出现5/3、4/4、3/5轮转。跨NUMA仍关，另一NUMA有空闲也不保证能借；这是当前同NUMA策略的受控竞争实验。它仍不是持续饱和或目标50%全应用CPU的复刻。旧4003为不同图与数据合同，不替代本次对照。

新旧到达形态的固定计算/访存次数应一致，但arrival/work合同改变；新burst块内做严格策略配对，旧regular作为历史参照。两批跨时段运行，不把它们的裸时延差当作已排除环境漂移的纯arrival因果量。若要精确估计arrival交互或小幅策略差，再交错重复冻结的regular/burst对照。

## 6. A：先打印已有task结果的联合诊断，不重跑

只取既有seed列表第一项420的三个策略；原始CSV仍在目标机。缺文件时保留错误并继续B，不为此重跑。新增标志需要拉取本轮Python脚本；无需编译Folly或C++。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
TASK_DIAG='results/local/v2-locality-task-skew-01/seed-block-420'
python3 experiments/affinity/diagnose.py \
  "$TASK_DIAG/seed-420-home" \
  "$TASK_DIAG/seed-420-fork-parent" \
  "$TASK_DIAG/seed-420-all-parent" \
  --warmup-seconds 2 --window-seconds 16 --max-bins 4 --joint-load
)
```

## 7. B：运行新的9轮burst对照

保持这96个CPU独占，串行运行。使用新目录；已存在时更换末尾编号，不覆盖。约3分钟是投递时间，不含初始化、排空和报告。runner遇失败/拒绝会停止并保留结果；不要在原轮降低QPS或增大准入来掩盖失败。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
BURST_ROOT='results/local/v2-locality-task-burst8-01'
mkdir -p results/local
mkdir "$BURST_ROOT"
for BURST_SEED in 420 421 422
do
  BURST_BLOCK="$BURST_ROOT/seed-block-$BURST_SEED"
  python3 experiments/affinity/run_matrix.py \
    --suite follow --cases home,all-parent,fork-parent \
    --seeds "$BURST_SEED" --output "$BURST_BLOCK" --run --base-args \
    --cpus "$PARTITIONS" --affinity cluster --threads 128 --capacity-scale 1 \
    --requests 240 --qps 12 --arrival burst --burst-size 8 --burst-spacing-us 100 \
    --widths 80,4 --depths 2,2 --iterations 3000000 \
    --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 100000 \
    --jitter-percent 25 --long-every 8 --long-factor 4 \
    --work-scale-scope task --estimate-mode constant \
    --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
    --busy-high 1 --return-low 0.5 --min-gain 0 \
    --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 1 --page-sample 0 \
    --max-inflight 16 --memory-mb 512
  python3 experiments/affinity/report.py "$BURST_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
  python3 experiments/affinity/flow_report.py "$BURST_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
done
)
```

回传A及B完整终端，可按seed分段。保留全部轮次、警告、拒绝和失败，不只发最佳结果。每轮窗口预计192请求/24批，批内请求相关，不能当192个独立尾部样本。

## 8. 回传后怎样判断

1. 先核对同seed工作/到达/checksum、成功/拒绝、峰值inflight及实际burst跨度/lag；入口拉宽可能改变实际压力，但仍须纳入端到端结果。
2. 看home→fork的均值/样本尾部、CPU及首波等待；再看fork→all的ordinary迁移/等待、join和整request变化。不能因为迁移更多就认为更均衡，不能用task wait p99替代request p99。
3. 若多池竞争后借出收益收缩，先检查同NUMA剩余资源与到达形态，不立即降低high、提高迁移强度或把它叫计数失效。若all才稳定受益，则明确其适用压力；若fork仍足够，保留更少迁移的参照进入Q/active/O与16/128线程分块对照。
4. 持续压力门控需单独覆盖短陡峰、中等长峰、退潮/再突发及hold之后仍有新ready任务；实现与测试须先定义连续性、快速退出、紧急旁路和采样空洞，不与本批arrival变化捆绑。v2异步RPC、较大/共享工作集、跨NUMA和正式尾部重复仍是后续独立问题。

## 9. 本轮工程与验证

仅更新离线联合诊断、其测试和文档。负载驱动、RequestExecutor、Folly及策略默认值未改。联合诊断测试覆盖同边缘不同联合分布、原始字段缺失、trace0、空窗口、边界/晚完成、不同池容量及默认输出兼容。

本地验证已完成：联合诊断12项测试通过；CTest的affinity_functional、affinity_diagnose、affinity_flow三项全部通过。直接从本文命令生成三个block、九条ARM计划，逐项验证与4008已完成task合同仅arrival/burst参数不同，两段命令通过bash语法检查。开发机用两个可用CPU人工分区、2线程/池、4/2宽与2/2深、少量计算/访问运行burst8三策略，各16/16完成，2组home配对通过，report/flow/联合diagnose全部成功。ARM尺度未在开发机运行；这些只是功能和指南验证，不是新的目标机性能数据。

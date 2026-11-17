# 负载覆盖与停止规则（更新至4017）

**当前状态：4017已完成六轮既有CSV的到达核查，见[结论](TARGET_V2_4017_REVIEW.md)。不再重跑旧CPU矩阵，也不追加该批的到达诊断、trace或阈值扫描。异步完成源与驱动已通过开发机及目标机小规模功能验收；等待期间释放worker，恢复callback再推进图。[4018异步首批八轮](TARGET_V2_4018_REVIEW.md)已完成；A1下O相对home均值降低19.546%、CPU/请求增加3.721%，保留单次证据限制。当前仅执行[四轮A1机制覆盖](TARGET_V2_ASYNC_MECHANISM_GUIDE.md)，补齐O恢复观测与trace敏感性，完成即分析。目标始终每个已核实硬件cluster一个池。**

现有CPU突发工作族已完成线程、计数信号、后继路由和观测扰动的探索。[4013](TARGET_V2_4013_REVIEW.md)说明Q/O都在借出、过配使callback墙钟膨胀；无需继续重复这些诊断以选出一个信号赢家。**保留原基线，补“候选在哪些负载下仍有效或失效”，不把历史trace拟合成新默认负载。**

目标保持每个已核实硬件cluster一个池，L1 request home、L2新ready算子选池，同NUMA优先。当前六池和CPU编号仅属于已确认的独立测试范围。

## 1. 覆盖清单和优先级

| 问题 | 已有覆盖 | 当前缺口与处理 |
|---|---|---|
| 大扇出、长短算子、依赖与同NUMA借出 | 已有固定工作、分支读写、regular/burst、fork/all、parent/home | 冻结历史结果；当前CPU合同不再全量复测 |
| 16线程简化候选的到达稳健性 | 4014九轮regular已完成，Q/O均值相对同期home下降约38% | 探索性守护通过；保留入口lag异常与尾部限制，不重复这九轮 |
| 短callback、调度/计数相对成本 | 4015的S算子CPU约1.065ms；home trace执行CPU增量1.83%～2.28% | 校准完成；不为贴近0.85ms调参，性能对照保持trace0；单独计数/评分成本仍未隔离 |
| 多request竞争、没有空闲目标、容量边界 | 4016 SD六格peak=3，Q/O均值下降33%～34%、CPU/请求增加6%～7%；一秒边界backlog 0/1 | 4017已量化重叠并结束核查，尚非持续饱和；保留限制，不自动加压 |
| 同步阻塞 | 现有sleep参数已实现，当前CPU批未覆盖 | 独立反例，检验少worker及I/O含阻塞的边界，不称RPC |
| 真实异步等待与集中恢复 | 单辅助完成源、Continuation恢复、固定延迟/网格、取消与排空已实现，新schema独立记录 | **当前编译/小型功能验收**，通过后再开ARM性能合同；尚未模拟真实网络与随机下游分布 |
| 数据亲和、共享与跨NUMA | 64KiB分支真依赖及部分工作集/后继对照 | 共享读、不同工作集、首触页和远端代价仍须专项；无硬件指标不宣称cache收益 |
| 生产尾部及小幅优势 | 历史每格192请求，4016每格1536；多数单seed/单次 | 独立整轮重复、相关请求统计与留出验收；增加同一轮请求数或累加异构历史不能替代重复 |

它们不是全部必须立刻跑完的清单。每批预声明问题、配置和预算；一般最多12次实际运行（包括基线、失败及重试），完成即分析，不自动续跑阈值搜索。遇到功能失败、拒绝、持续增长的积压或发生器严重落后，停止继续加压，保留所有失败证据。

## 2. 已完成的九轮regular守护：保留原合同，不再执行

唯一问题：**16线程下Q/O相对同档home的净收益，是否也存在于平滑到达？** 固定工作、线程、CPU和路由；仅将4012的arrival改为regular，trace回到0。home/Q/O × seed420/421/422，每seed三格打乱，串行运行。未包含128线程，因此本批不重新证明16优于128；与历史burst只作背景比较，不作同期到达效应或差中差估计。

复用现有二进制和脚本，无需重编。预计到达期共约3分钟，另加启动、排空和报告。输出目录必须为新目录。以下只适用于已授权独占CPU的测试环境：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
GUARD_ROOT='results/local/v2-count-regular-guard-01'
mkdir -p results/local
mkdir "$GUARD_ROOT"
python3 experiments/affinity/run_matrix.py \
  --suite signals --cases home,queue,outstanding --thread-counts 16 \
  --seeds 420,421,422 --output "$GUARD_ROOT" --run --base-args \
  --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 \
  --requests 240 --qps 12 --arrival regular --burst-size 8 --burst-spacing-us 100 \
  --widths 80,4 --depths 2,2 --iterations 3000000 \
  --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 100000 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope task --estimate-mode constant \
  --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
  --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 0 --page-sample 0 \
  --max-inflight 16 --memory-mb 512
python3 experiments/affinity/report.py "$GUARD_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/flow_report.py "$GUARD_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
)
```

regular模式不使用burst参数，保留它们便于核对其余设置不变。每轮仍240请求，正式窗口计划到达`[2,18)`仍192请求；不再期待24个burst。原始CSV/metadata留在目标机，仅回贴完整终端。trace0的任务级负载/迁移不可得，不运行diagnose期待补出这些字段。

检查工作/arrival/checksum在**同seed同到达**三格内一致；regular与burst的arrival hash本来就应不同，不把它判成工作失配。保留所有失败/拒绝、发生器lag和完整窗口。

预先判据：Q/O相对home方向稳定则通过这一到达形态的探索性守护；若两者差异仍小，保留可替换实现，停止争夺小幅排名。出现方向反转或明显异常，只选对应现象做一次小批固定合同诊断。三seed工作不同、每格只一次，仍未达到正式p99或等价性验收。

## 3. 后续工作族：S/SD已完成，暂不新增性能矩阵

### 短工作与到达密度分开

冻结图、分支64KiB、长短扰动和亲和策略，先设计以下三个profile：

| profile | iterations / accesses | QPS | 回答什么 |
|---|---:|---:|---|
| L | 3000000 / 100000 | 12 | 已有长工作参照 |
| S | 375000 / 12500 | 12 | 同到达下缩小每算子计算/访问量后的行为 |
| SD | 375000 / 12500 | 96 | 在短工作下提高到达密度，名义迭代/访问需求约恢复L量级 |

1/8是敏感性档位，不是从0.85ms推导的生产参数，也不承诺callback恰为1ms。初触页、init、控制callback、调度成本不随工作一起缩小；SD不能假称与L实测CPU需求完全相等。S相对L同时改变工作需求，不是把同一算子纯粹拆成更多callback。若要隔离同request总工作不变的callback粒度，须新增保留计算状态的分段实现，当前没有该CLI。

4015已完成[四轮S校准](TARGET_V2_SHORT_WORK_GUIDE.md)，四轮peak-inflight均1；4016已完成[SD六轮](TARGET_V2_SHORT_DENSITY_GUIDE.md)，六格peak均3。SD内借出有均值收益及6%～7%的CPU代价，Q/O未分出通用赢家；两档home持续出现较高lag。[4017](TARGET_V2_4017_REVIEW.md)已完成这六轮的有界离线核查，不重跑S/SD或自动追加其他profile。20秒投递时S为240请求、SD为1920请求；每个profile内合同可比，跨profile不能把更少工作造成的时延下降叫策略收益，也不能把非同期S/SD当成完整到达干预对照。

若短工作下确有Q盲区和较差request结果，再比较Q/I/O；若没有，就接受本范围内简单信号够用，不为制造O赢家继续改模型。4015已补home观测开关成本，不能继承长工作下0.34%～0.73%的比例；这仍不是单独I/O计数维护或Q/O评分成本。

### 持续竞争的备选设计：当前不执行

保持L的工作合同、选定到达形态和同一线程档，有限试探12→24→48QPS；这是倍增敏感性梯度，不是在拟合原业务50%CPU。投递期固定20秒时请求数对应240/480/960，诊断窗口仍须在投递期内。先看完整准入/拒绝、scheduled backlog、lag和进程CPU/墙钟；出现持续增长或拒绝停止增加，不通过缩短窗口藏掉积压。

仅在一个可解释压力点比较候选；必要时增加32线程等中间档，不能由16优于128推导16全局最优。max-inflight本身是准入边界，若触发它，应先标明测到的是准入限制，不能擅自提高后继续当同合同结果。

### 阻塞和异步恢复是两个独立合同，工程优先异步功能

同步阻塞可用现有`sync-block-percent/us`，例如零阻塞控制与50%算子计算后额外sleep10ms的机制端点；参数不是生产估计。保持固定计算工作，只增加占worker等待，验证I/O不能等同CPU需求及worker数量边界。先测一个线程档、少量策略，必要时才比较16/128；不同时改变到达和工作粒度。

异步RPC必须释放executor worker，由外部完成源重新提交执行段，详见[设计与功能验收](ASYNC_CONTINUATION_WORKLOAD_DESIGN.md)。该结构已通过单辅助源与Continuation加入驱动，使用affinity-v3-async合同；当前按[异步功能指南](TARGET_V2_ASYNC_FUNCTIONAL_GUIDE.md)验收D0直接推进、A0外部源零配置延迟、A1有界延迟。独立完成与成批完成是不同恢复到达过程，计时、取消、request生命期和排空须先通过功能验证。未知延迟/比例用显式敏感性档，不依赖新的真实trace；也不能把v1已有RPC验证当成v2局部性实现已经验收。本阶段不下发异步性能矩阵；真实网络RPC仍未模拟。

## 4. 什么时候进入生产选型或结束探索

当前可以冻结CPU主导工作族的机制结论，并让真实环境agent按[分阶段手册](PRODUCTION_AGENT_PLAYBOOK.md)或[纯prompt](PRODUCTION_AGENT_PROMPTS_ONLY.md)核实本地事实。独立测试覆盖不会自动证明生产代表性；接收方无需获得这些文件、访问本仓库或重新采历史trace。

只有要声称生产最优、跨NUMA有效或p99可发布时，才需要对应当地业务/资源证据及正式重复。若某工作族中候选差异小于重复波动，结束该族，保留简单候选与回退；若发现具体反例，记录适用范围并针对该机制修改，不重启全矩阵。每一步都能产出结论和可复查的状态，不以“继续试总能找到赢家”为完成标准。

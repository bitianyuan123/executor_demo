# 短工作SD：提高到达密度后的六轮策略对照

4015已完成[短工作S四轮校准](TARGET_V2_SHORT_WORK_GUIDE.md)：regular12QPS下request峰值在途数为1，operator CPU均值约1.065ms，16/128线程的trace0请求均值接近。详细trace的整轮CPU/请求变化约1.83%/2.28%，不能把长工作下的观测成本比例直接用于短工作。

**下一批只执行SD六轮：home/Q/O × 每池16/128线程，seed421，trace0。** 保留S的图、计算/访存、数据布局和亲和设置，将regular到达提高至96QPS、请求数增至1920，仍保留约20秒投递期。不先追加孤立S的策略排名，不自动扩展为全量矩阵。

目标仍是当前选定六个已核实硬件cluster各一个Folly池；历史四池配置不参与本批。无需修改或重新编译C++、Folly及现有工具。

## 1. 为什么选择SD，回答什么

S的peak=1不意味着没有池内排队。4015的home/128线程组，Q非零占96.174%的operator提交样本，Q/C>1占84.084%；16线程对应96.391%和84.462%。Q/C在(2,4]时，提交后等待中位数约4ms。但128线程的Q=0且I>C、O>C仅5条、占0.016%，这些记录不足以支持Q盲区主导尾部。

因此继续扩大孤立S的信号排名不能补足多请求相遇这一覆盖。SD把计划请求间隔从约83.33ms缩短到约10.42ms，在固定短工作下检查：

1. 是否实际出现请求重叠、发起滞后或积压，保持相同准入边界时各策略能否完成全部请求。
2. 在SD内部、同一线程档下，Q/O相对同期home的净时延与CPU/请求变化。
3. 在同策略、同CPU分区下，16/128线程预算的差异。名义池容量两档均为16，不能把线程数当CPU吞吐容量。

SD只恢复与L近似的**名义迭代/访问需求率**，不是已知相同的CPU利用率。请求数从240变为1920，新增request ID会生成新的确定性长短工作；它不是把S的240个请求简单重复八遍，总迭代/访问量也不精确等于S八倍。init、首次触页、控制callback和调度成本的发生率同时提高，实际CPU需求必须重新测量。

这不是生产50%CPU的复现，也不预先保证持续竞争、饱和或无空闲目标。SD没有同期S策略组，因此不把历史S与SD拼成到达密度的策略差中差或相同请求集合的性能配对。

## 2. 六轮固定合同

| 项目 | 取值 |
|---|---|
| 策略/线程 | home、queue、outstanding × 每池16、128线程 |
| seed/顺序 | 固定421；六格在同一seed块内统一打乱、串行执行 |
| 到达 | regular、96QPS、1920请求；窗口仍按计划到达[2,18)秒 |
| 图/工作 | widths=80,4、depths=2,2；iterations=375000、accesses=12500 |
| 数据/异质性 | branch-rw、branch64KiB、shared0；task级±25%、约1/8四倍工作 |
| 路由 | RR home、all-parent、chunk1、hop4、仅同NUMA |
| 评分 | count默认high1/low0.5/min-gain0；不另加阈值覆盖 |
| 观测 | trace0、CPU采样0、page采样0；保留request统计 |
| 限制 | max-inflight16、memory-mb512；无同步sleep、无异步RPC |

Q为Folly队列估计，O为已接纳未结束callback；两者评分均包括预留R并除以名义容量C。它们不是内核runq或精确剩余CPU工作。parent以实际父池为偏好，不保证永不回request home。home绕过L2评分，作为同期控制。

本批预算为六次实际运行，到达期合计约120秒，另加启动、排空和分析时间。失败轮同样占用预算，不自动重试或补满成功格，也不额外安排trace1批。

## 3. 可直接执行

仅适用于已有独占CPU的目标测试环境。输出目录必须新建；已有目录立即停止，不覆盖旧产物。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
DENSITY_ROOT='results/local/v2-short-density-count-01'
mkdir -p results/local
mkdir "$DENSITY_ROOT"

python3 experiments/affinity/run_matrix.py \
  --suite signals --cases home,queue,outstanding --thread-counts 16,128 \
  --seeds 421 --timeout-seconds 120 --output "$DENSITY_ROOT" --run --base-args \
  --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 \
  --requests 1920 --qps 96 --arrival regular \
  --burst-size 8 --burst-spacing-us 100 \
  --widths 80,4 --depths 2,2 --iterations 375000 \
  --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 12500 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope task --estimate-mode constant \
  --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
  --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 0 --page-sample 0 \
  --max-inflight 16 --memory-mb 512

python3 experiments/affinity/report.py "$DENSITY_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/flow_report.py "$DENSITY_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/diagnose.py "$DENSITY_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16 --bin-ms 1000 --max-bins 20
)
```

regular不使用burst-size/spacing，保留二者便于与S审计。不要在base-args再加threads，线程维度由矩阵管理；signals也不允许额外覆盖busy-high、return-low和min-gain。

**trace0下diagnose仍能输出request到达、尝试投递、完成、失败、窗口边界积压和发起lag。** 每轮只打印16个一秒窗口，六轮共96个时间桶，适合终端收集。callback并发、首扇出和Q/I/O分桶会明确不可用，这符合本批设计；不期待离线工具补出未采集的任务信息。此处不启用joint-load，也不调用trace_cost_report。

目标机保留CSV、summary、metadata和plan，只回传完整终端。每轮显式超时上限120秒，包括约20秒投递及后续排空/写结果；超时不是有效完成。工具遇到非零退出码或超时会停止后续case；主程序若存在拒绝/失败，会保存结果后以非零码退出，外层set-e随即停止。若发生此情况，保留终端给出的失败目录和run.log；不要删除失败数据再重跑。严重错误可能没有完整summary，应如实标为未完成。**lag或backlog趋势本身不会触发工具自动中止**，需要根据批后报告复查，再决定是否允许下一批加压。

## 4. 资源与预期合同检查

- 正常每轮1920/1920完成、拒绝/失败0；正式cohort为1536请求，即计划到达ID192～1727。窗口后完成的请求仍保留，不按完成时间删尾。
- 每request仍171个callback，全运行328320个，低于当前代码的2000000个节点×请求上限。正式request-cohort对应262656个callback、258048个operator，但trace0不产生这些任务记录。
- 当前seed421/生成器的计划迭代总量为 **166,355,865,000**、访问总量为 **5,545,195,500**，约为S的7.920倍；arrival hash为 **8354655555075271473**。六格内应一致，checksum也应一致；不要求与S或L的整轮checksum相同。
- 请求数变多不增加max-inflight。4KiB/64KiB页下，每request分支payload加状态为5515776字节，16个在途预算约84.164MiB，低于512MiB；实际以运行时页舍入和summary预算字段为准。
- memory-mb约束payload，不是进程RSS总上限，不包含线程栈、分配器及结果对象。128线程档仍有768个worker，不能将payload估算当整进程内存保证。
- trace0不分配任务记录数组、不写tasks.csv；每轮requests.csv为1920条请求记录，六轮串行。无需为了此次密度实验开启大规模任务trace；以后若需要，另核验输出内存与采集成本。

本批在每个线程档内都有唯一同期home控制；report应产生四组Q/O对home的正式cohort配对。跨线程档保持CPU集合、容量、到达和工作不变，但worker预算不同，应单独陈述，不能混入同资源策略收益。

## 5. 预先分析与停止规则

先检查完整终态、同seed工作/到达/checksum、资源和计划窗口，再看每秒request趋势及peak-inflight。request backlog是已计划到达但未终结的请求，包含发生器滞后，**不是Folly队列深度**。一秒边界无积压不能证明桶内没有竞争，也不能凭最后排空宣布稳态。

在同线程档内比较Q/O相对home的request均值/p95/p99，以及整轮CPU/完成请求。cohort时延和整轮CPU的统计窗口不同，不作直接相减。单seed单次结果用于探索，1536请求并不自动满足独立尾样本、正式p99或小效应置信要求。

1. 六轮完成就停止并分析，不自动追加S、trace1、更多seed或阈值。每格仅单次运行，不能据此估计独立重复的运行波动；差异小而没有重复证据时不选赢家，保留简单候选与可替换实现。
2. **如果SD仍有peak-inflight=1，保留该结果，不自动提高QPS。** 可确认的是这一合同下未观察到request重叠；它仍可能有request内部局部排队，不能把它改名为持续竞争试验成功。
3. 若存在重叠，也须结合积压/lag、完成和成本判断；peak>1不是持续饱和证明，更不代表所有候选池都忙。
4. 发生拒绝、失败、超时或明显持续增长的积压/发起滞后，停止继续加压并解释。触及max-inflight首先说明准入边界被碰到，不直接证明CPU极限；不擅自提高准入上限、缩短窗口或只保留成功请求。
5. trace0无法证明“没有迁移”或Q/I/O怎样改变。只有结果留下具体机制问题时，下一轮才决定是否需要少量trace1或其他观测；不继承4015的home观测成本作为SD所有策略的成本。

主线仍是每cluster一个池、request home与新ready算子借出的权衡。本批不改变数据布局、不增加外部等待，不证明生产代表性；同步阻塞、异步恢复和跨NUMA仍按[覆盖计划](TARGET_V2_COVERAGE_NEXT.md)各自独立推进。

## 6. 本地验证范围

完整bash片段通过语法检查；在临时目录移除run开关后，实际调用矩阵工具生成六格计划，核对先筛选策略再展开线程、seed421统一洗牌、元数据、输出路径及全部合同。独立按现有工作生成算法核算上述总迭代、访问、arrival hash和cohort边界。

开发机未执行目标CPU编号上的native程序或ARM压力；现有二进制没有可代替实际执行的原生dry-run选项，因此使用矩阵plan模式核对实际CLI。运行代码和工具均未修改，这些检查不构成目标机性能结果。

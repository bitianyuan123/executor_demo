# 短工作S：四轮home尺度与观测开关校准

4014的九轮regular守护已完成。按[分步覆盖计划](TARGET_V2_COVERAGE_NEXT.md)，本批只建立短工作S的实测尺度，检查详细观测对它的影响；不继续争夺Q/O的小幅排名，也不提高到达密度。目标仍是每个已核实硬件cluster一个池，当前独立测试范围为六池。

**固定seed421，home × 每池16/128线程 × trace0/1，共四轮。** 复用现有二进制和脚本，无需重新编译C++或Folly。四轮结束后先分析，不自动追加SD、压力梯度、其他评分或阈值扫描。

## 1. 本批合同

| 项目 | 取值及含义 |
|---|---|
| 资源 | 已确认的六个cluster分区，NUMA0/2；两档每池名义容量均16 |
| 线程 | 每池16、128；它们是worker预算，不是16或128个物理核 |
| 到达 | regular 12QPS、240请求，保持4014到达方式和强度 |
| 图 | widths=80,4；depths=2,2；每request168个operator、3个控制callback |
| 工作 | iterations=375000、accesses=12500，分别为原L的1/8 |
| 数据 | branch-rw，每分支64KiB、shared-kb=0；保留home初始化/首次触页 |
| 异质性 | task级±25%，long-every=8、long-factor=4，seed421 |
| 调度 | home-policy=rr、policy=home；保留all-parent/chunk1/hop4/仅同NUMA参数 |
| 观测 | trace0/1；CPU后台采样和page采样均关闭 |
| 外部等待 | 同步阻塞仍为默认0/0，没有异步RPC |
| 准入/内存 | max-inflight=16、memory-mb=512 |
| 分析窗口 | 按计划到达选择[2,18)秒，预期192请求；迟到投递和完成保留 |

home绕过L2评分，all-parent等参数在本批不产生借出。保留这些参数便于审计，不能据本批评价Q/O的评分或选池成本。

1/8是预先选择的敏感性档位，**不是依据旧trace的0.85ms标定，也不要求callback达到1ms**。初触页、init/join、上下文和计数维护不会随计算/访存次数一起缩小，因此实测CPU/request不必降至L的1/8。S改变了每request总需求，不能称为等总工作下纯粹拆细callback。

## 2. 可直接执行的四轮

以下仅用于已有独占CPU的测试环境，不是生产默认配置。正式到达期合计约80秒，另加启动、排空和报告时间。输出目录必须是新目录；不要覆盖旧结果或删除失败轮。

先将四格按seed421预先洗牌，顺序为16/trace0、128/trace1、16/trace1、128/trace0。每格使用overhead套件的单个home case保留计划及元数据，随后串行执行。这个固定顺序不保证消除温度、频率或时间漂移；每格仍只有一次运行。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
SHORT_ROOT='results/local/v2-short-home-calibration-01'
mkdir -p results/local
mkdir "$SHORT_ROOT"

for SHORT_CELL in 16:0 128:1 16:1 128:0
do
  SHORT_THREADS="${SHORT_CELL%:*}"
  SHORT_TRACE="${SHORT_CELL#*:}"
  python3 experiments/affinity/run_matrix.py \
    --suite overhead --cases "home-trace$SHORT_TRACE" --seeds 421 \
    --output "$SHORT_ROOT/threads-$SHORT_THREADS-trace$SHORT_TRACE" \
    --run --base-args \
    --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 \
    --threads "$SHORT_THREADS" \
    --requests 240 --qps 12 --arrival regular \
    --burst-size 8 --burst-spacing-us 100 \
    --widths 80,4 --depths 2,2 --iterations 375000 \
    --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 12500 \
    --jitter-percent 25 --long-every 8 --long-factor 4 \
    --work-scale-scope task --estimate-mode constant \
    --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
    --cross-numa 0 --max-hops 4 --cpu-sampling 0 --page-sample 0 \
    --max-inflight 16 --memory-mb 512
done

python3 experiments/affinity/report.py "$SHORT_ROOT"/threads-*/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/flow_report.py "$SHORT_ROOT"/threads-*/seed-* \
  --warmup-seconds 2 --window-seconds 16

for SHORT_THREADS in 16 128
do
  python3 experiments/affinity/trace_cost_report.py \
    "$SHORT_ROOT"/threads-"$SHORT_THREADS"-trace*/seed-* \
    --warmup-seconds 2 --window-seconds 16
done

python3 experiments/affinity/diagnose.py \
  "$SHORT_ROOT"/threads-*-trace1/seed-* \
  --warmup-seconds 2 --window-seconds 16 --joint-load
)
```

regular不使用burst-size/spacing，保留二者仅方便与4014核对。overhead所选case会明确写入policy=home和相应trace值，不需要另加基础trace参数。

**不能使用`--suite overhead --thread-counts 16,128`：thread-counts仅支持signals。** 这里通过四个单case调用使用现有接口，无需修改矩阵工具。

**trace_cost_report必须分线程档调用。** 当前配对键没有线程数维度，将四轮一次全部传入会报duplicate trace；以上循环每次只传同线程的trace0/1一对。提示“all/fork interaction unavailable”符合本批设计，不需要为补齐该交互新增运行。

目标机保留CSV、metadata和plan，只回传完整终端即可。trace0任务级观测不可得，不把迁移/等待的缺失值解释为零。

## 3. 预期审计与分析

先核对四轮全部240/240成功、无拒绝/失败，正式窗口各192请求。trace1的完整request-cohort应包含32832个callback，其中32256个operator；diagnose按task提交窗口筛选，不能一般地要求它与request-cohort完全相同。

seed421下四轮预期：

- 整轮计划计算迭代数 **21,003,300,000**，访问数 **700,110,000**；它们分别是原L合同的1/8。
- 当前regular生成器的arrival hash为 **17526655543635360459**，与4014相同。四轮的工作、到达及结果checksum必须相符；**S checksum不要求等于L**，因为计算和访问次数已改变。
- 同线程档的trace0/1除了trace和output外，实际CLI及解析后合同一致；trace_cost_report还核验逐request的计划时间、终态和checksum。
- trace1中home迁移/跨NUMA应均为0；两档线程预算不同，不能当成资源完全相同的策略配对。

分析顺序：

1. 用trace1的operator CPU/wall均值和flow角色分解确认短工作实际尺度，同时看init、首波、后继、join。不要用整个request时延当operator执行时间。
2. 用trace0的request均值/p95/p99和整轮CPU/请求描述本S合同下的结果；16/128的差是线程预算干预，不是同资源策略收益。
3. 分别报告两档trace1−trace0的request时延及整轮CPU/请求变化。CPU统计含整轮预热/排空，不是192请求cohort CPU；不要把它与cohort时间直接相减。
4. 需要理解Q/I/O与callback等待时，只看trace1诊断；这些是已选池提交事件上的近似读数，包含抢占/阻塞的callback占用不等于onCPU或内核runq。

## 4. 可以回答的范围与停止规则

这四轮回答短工作S在当前硬件上的实际尺度、保留固定初始化后的CPU/request、home路径详细观测的执行期敏感性，并提供16/128线程的初步描述。它不排名Q/O、不验证持续竞争、RPC、其他数据布局，也不确定生产粒度或最优线程数。

**trace0/1差不是单独计数维护成本。** 两组仍维护总池所需的生命周期/上下文等状态；trace1另包含时间戳、线程CPU时钟、诊断计数读取和记录写入，并可能反过来影响执行次序。差值不是纯探针纳秒或全部I/O计数成本。本实现的进程执行计时从Driver构造之后开始，在CSV导出之前结束，**不包含线程池/trace缓冲分配及运行后导出成本**。不能把执行期差值称为全生命周期观测成本，也不能把home结果直接继承给Q/O路由路径。

一档一次、一个工作seed，不给成本置信区间或正式p99保证；每格192请求的尾部样本仍少。旧L结果仅作尺度背景，不把历史异时运行拼成新的同期配对。

- 四轮完成即停止，先分析；不要为了靠近1ms、0.85ms或某个CPU比例重新调iterations。
- 出现功能失败、拒绝、合同不一致或发生器明显落后，保留原始结果并解释；不删掉慢/失败请求，也不自动提高max-inflight或降低QPS补成成功轮。
- 如果trace差与后续待比较收益同量级，性能比较继续使用trace0，trace1只做有限机制诊断；不把负差或微小差直接称为“无观测成本”。
- 若S尺度和观测都可解释，再从同S合同的少量候选比较、SD到达密度或其他工作族中选一个下一步。它们均不由本脚本自动执行，不同时改变工作、到达、数据和线程等多个维度来寻找赢家。

## 5. 本地验证范围

本文完整bash片段通过语法检查；将测试环境目录替换为开发仓库/临时输出并删除运行开关后，实际调用矩阵工具生成四条计划。核对四格顺序、home/trace/threads、固定seed、工作与资源合同、输出路径和metadata，并核验分线程trace配对选择与计划总迭代/访问数。

开发机只生成计划，没有执行ARM压力，没有新增C++或工具功能；这些检查不构成目标机性能结果。

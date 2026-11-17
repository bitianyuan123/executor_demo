# 离线trace分析期间：独立开展观测扰动对照

**本页18轮已由4011完成，结果见[4010/4011审阅](TARGET_V2_4010_4011_REVIEW.md)，不重跑。其后4012计数信号/线程24轮、4013机制诊断及[4014九轮regular守护](TARGET_V2_4014_REVIEW.md)均已完成，当前测试入口仅为[短工作S四轮](TARGET_V2_SHORT_WORK_GUIDE.md)。下文保留已执行合同。**

## 1. 本次只回答一个问题

**先检查详细任务追踪是否改变策略比较，再决定是否扩大策略矩阵。** [4009](TARGET_V2_4009_REVIEW.md)中all-parent比fork-parent的request均值低约1.01%～1.35%，此前均为`trace=1`。当前尚无同一目标负载下、同期交错的trace开关对照。功能测试和单次读取成本不能代替这项端到端检验。

这项工作与[已有trace的离线补证](WORKLOAD_CALIBRATION_PROMPT.md)互不依赖：分析agent继续读取已复制资料；独立ARM测试环境运行以下18轮。无需生产访问、业务代码、新trace或环境间直连。本轮不修改负载生成器、阈值、调度实现或Folly，不要求重新编译；复用4009的二进制，更新Python脚本即可。

目标始终是**每个已核实硬件cluster一个池**。本机六cluster、96逻辑CPU，128线程/池；不恢复历史四池配置。测试case串行执行，继续独占这组CPU。若分析agent恰好运行在同一物理机，应避免其CPU、内存带宽或I/O负载干扰性能实验；“可以并行”不表示应在同机同时制造压力。

## 2. 18轮合同与测量边界

| 变量 | 本次取值 |
|---|---|
| 策略 | home；fork-parent（仅fork允许借出）；all-parent（后继也重新判断借出） |
| 详细任务追踪 | trace0、trace1 |
| 工作seed | 420、421、422；每seed六个case统一打乱顺序 |
| 固定部署 | cluster分区`0-15;16-31;32-47;192-207;208-223;224-239`，NUMA为0/2，128线程/池，capacity16 |
| 固定负载 | 4009的burst8、12QPS、task异质性、branch64、宽80/4、深2/2；详见命令 |
| 固定调度 | L1 RR，parent后继，同NUMA借出，cross-numa0，CPU后台采样关闭 |

每轮240请求、约20秒到达期；18轮总到达期约6分钟，另加启动、排空及报告时间。正式窗口仍为计划到达`[2,18)`：预计每格192请求、24个完整burst。不要把4009旧trace1替代本次同期trace1，也不要先跑完所有trace0再跑trace1。

`trace=0`关闭详细任务observer、逐任务时间戳/线程CPU时钟及记录写入；**保留调度所需的Q/I/O、预留计数、生命周期管理和request统计**。所以本次测的是详细追踪开关的端到端影响，不是整个总池或全部负载观测的成本。trace0的任务等待、CPU、迁移等不可用，不能将输出中的默认零解释为实际为零。

`process_cpu_s`计时从驱动run开始，到排空、join及采样器停止结束；不含此前的池创建/任务记录预分配，也不含此后的CSV导出。报告中的CPU/完成请求使用**整轮**CPU与整轮完成数，时延则使用上述正式cohort，不能把两者相减作时延分解。开关差包含间接调度与内存影响，不是单个探针的纳秒成本。

## 3. 同步脚本后直接执行

工作分支为`experiment/affinity-load-20261114`。按测试机已有可用方式同步本次更新即可，不要求环境之间联网。运行必需更新为`experiments/affinity/run_matrix.py`、新增的`trace_cost_report.py`；它复用仓库现有`report.py`。建议同步整次提交以保持测试、指南一致。原`build-affinity/affinity_demo`不变。

先做不施加目标压力的脚本验证：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
python3 experiments/affinity/tests/functional.py --matrix-only
python3 experiments/affinity/tests/trace_cost_report_test.py
)
```

随后执行18轮。新目录必须不存在；若中途失败，保留失败目录与终端，不把它覆盖成成功运行。以下只写新的结果目录。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
TRACE_COST_ROOT='results/local/v2-burst-trace-cost-01'
mkdir -p results/local
mkdir "$TRACE_COST_ROOT"
for TRACE_COST_SEED in 420 421 422
do
  TRACE_COST_BLOCK="$TRACE_COST_ROOT/seed-block-$TRACE_COST_SEED"
  python3 experiments/affinity/run_matrix.py \
    --suite follow-overhead --seeds "$TRACE_COST_SEED" \
    --output "$TRACE_COST_BLOCK" --run --base-args \
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
  python3 experiments/affinity/report.py "$TRACE_COST_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
  python3 experiments/affinity/trace_cost_report.py "$TRACE_COST_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
  python3 experiments/affinity/flow_report.py "$TRACE_COST_BLOCK"/seed-* \
    --warmup-seconds 2 --window-seconds 16
done
)
```

回传本批完整终端即可，可按seed分段，无需上传CSV。suite显式覆盖每格的`policy/balance-at/continuation/trace`；命令中trace1是统一基础参数，计划中的trace0格会覆盖成0。程序按全部六格打乱，**不在六个case之间并行**。trace0的flow报告仍能核验request到达，任务级首fork/角色诊断不可用属于预期。

## 4. 回传后怎样判断

1. **先审计合同。** 每轮240完成且失败/拒绝0；每格192个正式请求；同seed工作量、到达计划、逐request checksum及配置匹配。检查实际burst跨度、发生器lag、峰值inflight。不能仅靠总checksum相同或目录名称认定可比。
2. **同trace内比较策略。** 原`report.py`继续分别以trace0-home、trace1-home作基线，保留既有配对保护。两个trace模式分别看home→fork、fork→all的request均值、样本p95/p99和CPU/请求。
3. **同策略比较trace1−trace0。** 新报告逐seed列均值/p95/p99差与运行阶段CPU/完成请求差、百分比；正数表示开启追踪时更高。某个已出现策略缺少trace0或trace1、重复、失败或合同变化时拒绝差值。完整单策略对可独立报告，六格不全时不报告策略交互。
4. **检查策略差是否受开关影响。** 六格完整且合同匹配时打印`(all−fork)trace1 − (all−fork)trace0`。例如all的优势只在trace1出现，就不能忽略观测对排名的影响；两边方向一致也只说明本组未发现明显翻转，不能证明开销不存在。
5. **保留小样本限制。** 每格只有192个相关请求、24个burst，每seed只有一次。三seed对应不同固定工作，不能当同工作三次独立重复；不平均p99，不把全部任务当独立request，也不把这一轮升级成“约1%已显著”或生产p99保证。若差异仍接近波动，下一步应做固定合同交错重复及更多请求，再判断小收益是否值得实现复杂度。

本轮能检查当前计量路径与比较结论的稳健性；不能证明branch64或burst8代表生产，也不能确定生产最优信号、3ms门控或跨NUMA阈值。

## 5. 其他覆盖如何排队

以下是有独立价值的后续工作，不是本轮全部执行清单，也无需等待生产访问：

| 顺序 | 问题与可用工程 | 注意控制 |
|---|---|---|
| 下一小批 | 固定一种后继策略，比较Q/active/O；分别看16与128线程/池 | `active`当前评分是`(Q+I+R)/C`，不是仅I；保持固定工作与容量，先少量场景，不直接跑所有signals组合 |
| 随后反例 | 同步阻塞下，callback占用与CPU需求分离 | 现有blocking可用；不能把同步sleep称为异步RPC，也不能将耗CPU较少误判为更高计算效率 |
| 独立工程 | 非阻塞RPC完成源、回调恢复/失败/取消与上下文生命周期 | 总池Continuation已有功能覆盖；v2驱动尚未生成异步RPC，需明确标为待开发 |
| 证据或假设确定后 | 短任务族、内部ready节奏、不同工作集与跨NUMA | 每次只改变一个主要问题；0.85ms、3ms及64KiB均不能冒充已标定生产参数 |

对短任务和持续压力门控的具体参数，等离线agent厘清可观测量与未知范围再定；对现有测量方法的检查可以现在完成。完整主线与证据边界继续见[覆盖总览](GOALS_AND_COVERAGE.md)及[真实负载校准协议](WORKLOAD_FIDELITY_PROTOCOL.md)。

## 6. 工程验证记录

开发环境已通过4项相关CTest：affinity_functional、affinity_diagnose、affinity_flow、affinity_trace_cost。新增报告有9项测试，覆盖配置不匹配、缺对/重复、窗口外失败、晚完成、总checksum相同但逐request不同、trace0指标不可得和完整六格交互。

另用两个可用逻辑CPU人工分区、2线程/池、4/2宽和2/2深、少量固定工作运行六格，每格16/16完成；原report、新trace-cost及flow报告全部通过。本文命令通过bash语法检查，三个seed的18条ARM计划与4009固定合同逐项比对通过。ARM规模只生成计划，未在开发机执行；以上均为功能与指南验证，不是目标机性能结果。原C++二进制和Folly未修改。

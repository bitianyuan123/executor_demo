# 4012之后的六轮信号与线程机制诊断

[4012](TARGET_V2_4012_REVIEW.md)已完成24轮信号/线程性能初筛。下一批只回答：**在16与128线程下，Q漏掉的占用怎样呈现，Q/O是否实际改变了借出，较少worker为什么值得继续保留？** 不改工作量，不搜新阈值，不重新校准旧trace，不把小样本p99用作策略排名。

## 1. 六轮合同及其必要性

| 维度 | 固定选择 |
|---|---|
| 策略 | home、queue、outstanding |
| 每池线程 | 16、128，名义容量均16 |
| seed | 421，固定中间seed；不按某项最好/最差结果选seed |
| trace | 1；CPU后台采样和page采样仍关闭 |
| 其余 | 完整沿用4012的burst8、工作、RR、all-parent、同NUMA合同 |
| 预算 | 六个case统一打乱，串行执行；六轮后先分析，不自动追加 |

home使任务保持在request原池，减少L2选池造成的样本选择问题；它仍是提交事件采样，不能当连续时间压力。Q/O两组查看实际路由及已选池负载；不同策略产生不同完整执行轨迹，不是同一时刻的反事实观察。

暂不复测active是因为本批检查Q盲区及生命周期补充，并不再次排名三种评分。trace1下I仍会记录，可离线联合分桶；这不能替代active策略的净收益测量，后者已由4012初筛。

正式性能证据保留4012的trace0结果。本批与4012对比只能作观测敏感性的旁证，不能合并样本、视作重复或单独估计trace的因果成本。

## 2. 可直接执行

适用于已确认的独占CPU测试环境。复用4012二进制和脚本，**无需重编C++或Folly**。已有参数与分析工具均支持本批；新指南无需新增运行功能。下列CPU编号不用于真实业务默认配置。

到达期六轮共约两分钟，另加启动、排空及报告时间。新目录已存在时立即停止；不要覆盖旧结果或删掉失败轮。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
DIAG_ROOT='results/local/v2-burst-count-diag-01'
mkdir -p results/local
mkdir "$DIAG_ROOT"
python3 experiments/affinity/run_matrix.py \
  --suite signals --cases home,queue,outstanding --thread-counts 16,128 \
  --seeds 421 --output "$DIAG_ROOT" --run --base-args \
  --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 \
  --requests 240 --qps 12 --arrival burst --burst-size 8 --burst-spacing-us 100 \
  --widths 80,4 --depths 2,2 --iterations 3000000 \
  --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 100000 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope task --estimate-mode constant \
  --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
  --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 1 --page-sample 0 \
  --max-inflight 16 --memory-mb 512
python3 experiments/affinity/report.py "$DIAG_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/flow_report.py "$DIAG_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/diagnose.py "$DIAG_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16 --joint-load
)
```

回传完整终端即可，包括错误和分析告警；CSV/metadata留在目标机。每轮仍240请求，正式窗口`[2,18)`192请求/24个完整burst，正常完整cohort应有32832个callback。`diagnose`的负载分桶按task投递墙钟窗口筛选，其样本数不要求等于按request计划到达选出的cohort算子数。

不要给signals添加busy-high、return-low或min-gain基础参数：本批保持三种count策略的默认high1/low0.5/gain0。不得把cpu/work策略混入六轮；这会同时改变观测成本和评分量纲。

## 3. 收到结果后按什么顺序判断

1. **合同与到达。** 检查六轮终态、资源、工作/arrival/checksum、正式窗口与burst attempt跨度、lag；确认只有trace相对4012变化。失败/未完成保留，不能按完成快的子集宣称收益。
2. **先看两档home。** `diagnose --joint-load`给出Q零/非零 × I/C≤1/>1 × O/C≤1/>1的算子样本和后续等待/墙钟。重点看128档是否有Q=0但I或O高的样本，以及16档是否更多积压留在Folly队列。只陈述观察到的条件关联，不预设队列必定变短。
3. **再看Q/O实际调度。** 用`report`迁移数、`flow`的LocalSpill等路由原因、各角色跨池和后继位置，确认借出确实发生于什么类型的callback。跨NUMA应为0；home不应有跨池；parent不应出现ReturnHome决策。检查首波和普通后继，不能只看全任务平均数。
4. **连接到request进展。** 比较首波投递、启动、callback跨度及整request时延、CPU/请求；callback重叠包括抢占/阻塞，不是onCPU数。等待计时在选池之后开始，不包含完整L2成本。先用这些量排查机制，再决定是否需要内核证据；本批不采内核trace。
5. **结束该批并选一个后续。** 如果借出正常、Q/O净效果仍近且没有新增异常，优先在16线程上保留home/Q/O、按需做regular守护或预声明固定合同交错重复，不继续扩大默认阈值搜索。若出现Q盲区与长等待的明确关联，围绕该现象只补一个工作族/粒度对照。若生命周期或路由约束异常，先修工程并验证，再谈性能。

同一行Q/I/O是分开读取的近似计数，未包含评分中的全部预留R；这些桶不是实际score重放。Q/O组记录的是已经选中的池，没有记录所有候选同一时刻的快照，不能据其计算“原池误判率”或counterfactual收益。home样本也按提交事件加权，不能将某桶比例解释为时间繁忙比例。不同算子成本和池混合，p99/均值不保证随计数单调。

如果16线程组出现大量I>16样本，应先核查计数边界、资源和解析，而不是直接宣布16个worker同时运行更多callback。I≤16时O仍可大于16，其中包括尚未开始的工作；不能把O−I直接当精确物理队列深度。

## 4. 后续保留但本批不执行的边界验证

- **正式尾部/微小收益：** 固定工作seed、明确新运行顺序、同期home和候选，延长窗口并做多次整轮重复；独立保留验证流量/seed。以burst或整轮作为相关性单元，不能把每秒p99平均或把192个请求当独立尾样本。
- **到达守护：** 使用相同CPU工作合同下的regular到达验证简化候选，避免只对单一burst形状有效。既有4008的128线程结果是历史证据，不能替代16线程的同期对照。
- **同步阻塞反例：** 现有`sync-block-percent/us`在固定计算后额外sleep，占住worker；用于检验I/O高却CPU不足时的选择和线程预算边界。它不减少计算量，也不模拟释放worker的异步RPC。不为凑目标CPU利用率直接选阻塞比例；先界定要验证的现象和有限档位。
- **真实异步恢复：** v2驱动尚未生成此工作族；若要验证需独立实现延迟恢复、取消/排空及成批ready的合同。不是把sleep改名即可。本批没有这个前置依赖。

这四类不组成新的自动全量矩阵，也不保证全部在当前阶段执行。主线始终是每个已核实cluster一个池；旧四池配置和历史线程周期不作为新负载或阈值依据。

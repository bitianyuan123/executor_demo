# 先声明对照，再计算整轮配对差值

[`scripts/compare_runs.py`](../scripts/compare_runs.py) 同时支持阶段3、阶段4的 manifest，但每次只分析一种实验单位。它补充原来的相关性/分桶分析，不将独立 task 和 DAG request 合并。

## 使用顺序

1. 在开始正式运行前写下主要对照、允许改变的参数、工作量约束和测量窗口。具体例子为本次的[独立任务合同](../results/review/configs/independent-contrasts.json)和[DAG合同](../results/review/configs/dag-contrasts.json)。保留合同文件版本，不能根据收益结果放宽失败规则。
2. 同一个 seed 运行所有待比较 case；每个 repeat 内随机排序，全部串行执行。阶段3和DAG runner已采用这种分块方式，记录完整命令和二进制哈希。
3. 以计划到达时间选择 cohort。选入的任务/请求即使在窗口后完成，也保留其时延。拒绝、取消和失败仍计数；不能只删掉慢样本。
4. 完成后运行合同检查和配对分析，再查看时间趋势/路径。不要先从表里挑最好的策略，才为它寻找解释。

```bash
python3 scripts/compare_runs.py \
  --manifest results/review/runs/independent/manifest.json \
  --spec results/review/configs/independent-contrasts.json \
  --output results/review/analysis/independent-paired

python3 scripts/compare_runs.py \
  --manifest results/review/runs/dag/manifest.json \
  --spec results/review/configs/dag-contrasts.json \
  --output results/review/analysis/dag-paired
```

原始目录被归档时，先按本轮结果说明解压。manifest目录迁移后优先查找其同级run目录；不要修改工作量字段来通过验证。

## 合同字段

|字段|含义|
|---|---|
|`stage`|`independent` 或 `dag`|
|`window.start_s/end_s`|相对发生器计划起点的测量窗口，左闭右开；DAG schema3保留Poisson首个到达间隔|
|`minimum_pairs`|至少5个完整seed配对才输出探索性bootstrap区间；默认5不是正式功效保证|
|`minimum_tail_observations`|每轮每侧 `0.01×完成样本数` 的最低值；默认20，否则p99只描述、不输出差值区间|
|`bootstrap_draws`|配对均值重采样次数，默认5000；增加次数不会增加真实证据量|
|`comparisons[].baseline/candidate`|manifest中的case名，不能相同，seed必须完整一一配对|
|`allowed_changes`|允许不同的命令选项，不含`--`；其余差异导致合同失败|
|`workload.service`|默认`exact`校验逐任务/节点CPU目标和RPC拆段；`equal_total`只要求整轮总量；`unrestricted`明确放开|
|`workload.arrival`|默认`exact`校验计划时间；`count`只要求数量相同；`unrestricted`允许Poisson等实验中数量不同|
|`workload.graph`|默认`exact`校验节点、依赖、RPC阶段与目标等待；改变图/RPC的实验可显式`unrestricted`|
|`total_relative_tolerance`|`equal_total`容忍的整轮名义预算相对差，默认0.001，可设0；不是实测CPU误差|
|`same_cohort_ids`|是否要求分析窗口内ID集合相同；相同整轮数量并不自动保证同cohort|
|`cohort_total_relative_tolerance`|可选，限制窗口内名义CPU总量的相对差；与整轮预算校验分别执行|

**不允许悄悄扩大可用CPU。** 分析器同时检查进程allowed CPU集合与所有worker实际允许CPU的并集。不同线程分组是处理变量，但更大的总资源不是纯亲和性对照。已记录的二进制哈希不一致也会失败。历史manifest没有哈希时输出告警，不伪造证据。

工作量合同从原始记录计算哈希。DAG请求日志可能按完成顺序输出，先按requestID排序；节点按requestID/nodeID/stage排序。RPC目标延迟也进入签名。CPU预算模式的目标相同只代表名义CPU需求相同，不能证明cache代价或固定有用工作相同。

整轮均值归一化不保证剪掉预热后的子窗口也等量。报告保留每轮 `cohort_units`、`cohort_target_cpu_us`、ID哈希和配对预算差。本次分布对照预声明整轮差为0、cohort允许3%波动，因而仍需结合实际cohort差解释；不能把它写成窗口内严格相同CPU的因果试验。若要求严格相同，可将分布实验cohort设为整轮，并明确包含启动瞬态，或另设计独立的预热和测量工作计划。

## 输出如何解释

主结果为 `candidate − baseline`，负值表示降低。每个seed先得到一轮均值/一轮p99，然后计算差值并对完整配对重采样，给出均值差、相对差、范围和每个seed的原始差。这里的p99差估计对象是“各轮p99差的均值”，**不是合并所有原始样本后重算的p99**，更不是平均每秒p99。

出现配置/工作量失配、缺少完整配对或有拒绝/取消/失败时，不输出成功样本的改善区间；合同失败的结果文件保留原因并返回非零。重复不足时仍输出描述性差值，区间为null。DAG短轮次的p99区间通常为null，这是正确的拒绝推断。

95%区间是探索性percentile bootstrap。5～8对能检查较大效应方向，但小样本区间覆盖率、机器状态相关和多重比较仍有限制；不能将“区间不跨0”自动解释为生产最优。正式确认应预先定义主要比较/最低有用收益，再用独立批次验证；不能不断追加数据直到显著。

每轮另输出：

- 前后半计划/实际投递/完成率，按到达cohort计算的均值；入选样本的投递滞后。
- 窗口起、中、末时仍未完成的请求/任务数，以及采样queue总和的斜率与前后半均值。前者对全完成运行可精确回溯；有失败时本身就是无损时延推断失败，不能把其数值当完整积压。
- planned投递时域结束后的drain、样本截断与状态计数。
- 过短窗口、后半时延增长、投递p99超过1ms、drain明显等启发式告警。

这些告警只帮助定位问题，既不是稳态检验，也不是自动剔除噪声的规则。`paired_estimate`表示配对合同通过且重复数够生成探索区间，**不表示已证明稳态或可用于生产推断**。过载对照可描述积压增长，但不能借由置信区间将其包装成稳态性能收益。

对高相关性的解释仍看[阶段3](03-independent/README.md)和[总实验复核](EXPERIMENT_REVIEW.md)。队列是Folly用户态信号，不是内核runq；这里的分析器没有加入任何内核因果推断。

# 阶段3综合报告：离线收集与统计口径

当前任务是读取已经完成的实验，输出一份综合报告：负载是什么、每种指标在该负载下能解释多少尾部时延、指标与阈值组成的投递策略如何改变尾部时延。**不新增性能实验，不重编译，不修改原始运行结果。**

已有3001/3002提供了主线逐轮时延和正式配对区间；尚缺minimal九轮的离线分析，以及observe完整的指标分桶和逐折预测数据。只有这些数据收集完成后，才形成最终综合报告。当前不把尚未收到的minimal收益或分桶曲线写成已知结果。

## 1. 四份既有证据分别回答什么

|来源|读取范围|用途|
|---|---|---|
|observe-analysis/report.json|main-t16-local、main-t256-local的home观测，3个seed|Q/C、N/C、估计工作量/C、剩余工作量/C的指标质量|
|tune-analysis/report.json|同一主负载，两种线程配置的全部已测阈值和home/spread|阈值曲线、分流比例与时延的权衡；属于调参证据|
|evaluate-alias-analysis/report.json|已经主机别名重算的56轮，7个新seed|冻结Q阈值2、N阈值2、spread相对home的配对效果|
|minimal-analysis/report.json|预先限定的256线程、home/N阈值2/spread、seed32000–32002，共9轮|只维护N时的方向性复核，3个seed，不生成正式bootstrap区间|

minimal只维护选中的N，不能用它比较未采集的Q/remaining。指标质量使用策略尚未介入的home/observe样本；不能把分流后被挑选的低负载池样本用于证明某指标预测更准。

原70轮minimal计划中其他已完成记录全部保留；不因为未执行48轮而追加任务，也不挑选这些额外记录中最好的运行来替代既定九轮。

## 2. 测试机执行步骤

在 `executor_demo-stage3-balance` 工作树中更新脚本。该工作树即使处于detached HEAD，也可以显式快进：

```bash
git fetch origin experiment/stage3-load-balance-20261115
git merge --ff-only origin/experiment/stage3-load-balance-20261115
```

若尚未分析minimal，执行以下命令；如果已经生成本节需要的有效报告，可直接进行收集。分析只读取已有CSV，输出为可再生报告，不启动任何负载。

```bash
python3 experiments/independent/scripts/analyze_balance.py \
  --manifest results/balance-arm/minimal/manifest.json \
  --hostname-alias localhost.localdomain=localhost \
  --hostname-alias-reason '3001/3002: same isolated ARM host; hostname labels only' \
  --output results/balance-arm/minimal-analysis \
  > results/balance-arm/minimal-analysis.stdout.log

python3 experiments/independent/scripts/collect_balance_report.py \
  --root results/balance-arm \
  --output results/balance-arm/report-collection
```

收集器要求上述四份report存在；缺文件时会明确报错，不自动发起实验。输出目录禁止覆盖，重复收集可另取新目录名。来源报告及其SHA、配置、筛选范围和异常记录随结果保存；不把数据质量警告隐藏成“全部通过”。

原minimal报告可能含所选九轮之外的中断或未配齐记录。收集器保留来源警告，须按scope辨别；不能因此判定所选九轮失败，也不能以消除全部来源警告为由补跑原70轮。

测试机无法上传文件时，分三段打印，分别复制终端输出即可：

```bash
cat results/balance-arm/report-collection/summary.txt
cat results/balance-arm/report-collection/quality.txt
cat results/balance-arm/report-collection/buckets.txt
```

可以分三条消息发送。无需打印整份report.json或逐task CSV。完整结构化证据还保留在本地 `collection.json`，不要求上传。

## 3. 最终报告的内容和图表

### 负载与测量位置

说明6个池、每池16/256个worker、每池16逻辑CPU、批次2.4/s、每批100个独立task、约50–150ms线程CPU预算、总平均100ms、同home每2.5秒到达一批，以及统一输入与不同实际投递方式。区分线程CPU时间与墙钟时延、task与request、逻辑CPU与物理核，解释Q/N/C、采集位置和成本。

主时延为 `planned→end`，记作L；排队/调度等待为 `submit→callback start`，记作W；回调墙钟为 `callback start→end`，记作X。补充逐轮submit→end统计、发生器迟到和planned时间分段趋势，用于检查时延是否主要来自发压迟到或持续过载。W含路由开销，X可能含回调执行期间的CPU等待。

### 指标质量：同时看关联、条件尾部和预测

1. **逐task关联。** 分配置、分seed报告指标与W、L的Pearson/Spearman、有效样本数、覆盖率和不同取值数。常量/缺失指标记NA，不记零相关。该相关系数不是“指标与p99的相关系数”。
2. **负载分桶的条件p99。** 横轴为提交前负载指标区间，纵轴为该区间task的L均值、p99；补充W/X分解。每个留出seed单独画线，边界只从其他seed得到，不把不同边界的桶强行平均。每桶展示task数、独立burst数、LOW_TAIL标记。
3. **整seed留出预测。** 用其他seed拟合，预测完整未见seed。比较单task条件均值预测的MAE、条件p99预测的pinball损失、覆盖率，与相同可用样本上的全局基线比较。覆盖率应结合0.99目标与损失看，越高不一定越好。
4. **自身预算控制。** 已知真实合成CPU预算的基线及“指标＋自身预算”仅用于解释任务长短的影响，明确标为oracle，不冒充业务可提前获取的信息。

目标条件量为：

```text
p99(任务完成时延 L | 提交前指标 M 落在 [a,b))
```

它不同于整轮任务p99，也不同于某task有一个“p99值”。MAE是单task实际结果与条件均值预测的误差，不是整轮平均时延预测的误差。

图表至少包含：16/256线程下的各指标条件p99曲线；各seed的尾部预测损失与覆盖率；指标的信息重复关系（当前常量估计下work约等于100ms×N）。保留没有明显改善的结果，不以找到赢家作为报告完成条件。

### 指标＋阈值的实际效果

完整展示已测tune阈值的完成时延和spill比例；将调参、7-seed冻结验证和3-seed minimal复核分开。用每轮实际p99及同seed“候选减home”差值展示策略作用；只有既有合法的7-seed配对区间才作为区间引用。

阈值图只展示实际测试的点，不通过少量网格点拟合出未测的“最优阈值”。

对照home、Q阈值2、N阈值2、static spread；N阈值0.5属于调参证据。解释同数值阈值不等于同分流强度，不能将N策略相对Q策略的全部收益归因于指标更准。static spread是路由基线，不是负载指标。

minimal九轮单独给出逐轮原值、六个配对差与描述性汇总。full/progress和none/minimal的seed与观测配置不同，且CPU预算模式会调整计算循环次数；不将两组的差异直接宣称为观测开销节省。

### 结论、范围和交付件

报告结论分别回答：哪种指标增加了可用信息；这种信息是否改善完成尾部的预测；哪个策略在当前负载下改善实际p99；收益伴随多少跨池分流；哪些结论不能推广到DAG/RPC/真实亲和场景。

计划交付一份仓库内综合报告、可导出的静态图和对应摘要数据。图表引用收到的终端证据与原报告标识，明确数据缺失和尾部样本限制。需要的新信息只通过已完成实验的离线读取补齐，不因此扩大性能矩阵。

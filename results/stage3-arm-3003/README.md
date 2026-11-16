# ARM 3003 终端证据归档

原件为用户提供的3003.log，保留原字节与CRLF；buckets-terminal.txt是用户随后单独提供的终端文本。[source.sha256](source.sha256)分别校验两个来源；[evidence.json](evidence.json)包含原分析器、manifest、report的来源哈希与主机别名授权信息。

**summary、quality及192条buckets已齐。** quality被终端重复打印两次，提取器验证完全一致后去重。唯一源警告位于未选中的minimal queue-h2种子，不影响预定九轮。192桶的24组样本总量各为12000，边界连续且来自训练seed；唯一LOW_TAIL为T256/Q/seed11000末桶n=964。

|文件|内容/记录数|
|---|---|
|counts.csv|4个阶段的收集范围与有效轮数|
|warnings.csv|1条原样保留的源排除警告|
|configurations.csv|7组配置，嵌入JSON不删字段|
|runs.csv|observe 6轮与minimal 9轮，共15条逐轮统计|
|cases.csv|29个策略/配置聚合，各轮p99的平均不等于合并p99|
|comparisons.csv|tune 14组、evaluate 6组、minimal 2组，共22组配对摘要|
|minimal_pairs.csv|6个minimal逐seed配对差|
|quality_groups.csv|2个指标分析组及Work/N冗余说明|
|correlations.csv|48条逐run逐指标逐目标相关性|
|prediction_folds.csv|48条held-seed预测，保留global/oracle/metric/metric+oracle|
|buckets.csv|192条逐seed条件统计，保留区间、任务数、独立批次数、均值和p99|
|quality_derived_summary.csv|由上述字段按fold求平均/最小/最大，非新实验数据|
|figures/|5幅静态图，各含PNG与SVG；条件图按原seed/区间分开|

运行标准库解析，检查记录数量、CSV列数、重复内容一致性；不会读取原始ARM CSV，不会启动任何负载：

```bash
python3 results/stage3-arm-3003/extract_terminal.py
```

绘图依赖matplotlib（生成本归档时使用3.11.2）。有matplotlib的Python环境中执行：

```bash
python3 results/stage3-arm-3003/plot_report.py
```

数值CSV保留终端打印的精度。收集器大多数浮点值格式为`.12g`；原report可能有更多位，未上传部分不能恢复。图中预测指标为三个fold平均及所有fold点，策略正式组为七轮p99平均和min–max，minimal为三个独立配对seed，不混入总体p99或额外置信区间。

完整解读见[ARM_3003_METRIC_POLICY_REPORT.md](../../stages/03-independent/ARM_3003_METRIC_POLICY_REPORT.md)。

# ARM 3007：短峰负载开关对照的原始证据

结论和完整负载说明见[3007分析报告](../../stages/03-independent/ARM_3007_SHORT_PEAK_POLICY_REPORT.md)。`3007.log`保持用户附件原始字节，SHA256：`80ac28db53ed1c032c2b4fd3e401ed3f6e87e3a97c395e7f9396486462ab84a8`。

本轮新增一次source_only，home复用3006。两条`SKIP home`来自证据预检与正常恢复路径，不是两次home执行；每张表打印两次，提取时逐项检查两份一致，只保留一份。home的阶段时延、N/Q分桶和CPU表已与3006逐项核对相同。

请求级补充`request_completion.log`保持后续用户粘贴文本的原始字节，SHA256：`fa25dd4e370471e79fe05abd43de0c9c9539a12f71b018088d582d7606b05ec4`，6250字节。它由同两轮的tasks.csv离线计算，不增加独立试验。主结果：完整请求p99为568.466→567.877ms，基本不变；initial集合完成p99为13.642→4.784ms，约下降64.93%。日志校验有效，600请求/策略，原严格形态失败及事后复核记录未变。

|文件|含义|
|---|---|
|policy_summary.csv、comparisons.csv|两个策略的原打印摘要、原打印总体变化率|
|decomposition.csv|initial、steady、all的S/W/X/L/D均值与p99|
|buckets.csv|仅home的提交前N/C、Q/C条件分桶，不包含source_only条件分桶|
|cpu_procstat.csv|100ms累计CPU执行时间观测|
|home_cpu_scopes.csv、source_only_cpu_scopes.csv|1ms调度事件积分，CPU均值、峰值、高位占比和最长段|
|*_cpu_thresholds.csv|同trace的80/90/95%阈值敏感性|
|*_cpu_aligned_windows.csv|实际首提交对齐的home池窗口平均驻留率，不是原始时间序列|
|initial_spans.csv|实际初始投递跨度|
|observers.json|观察进程与reader线程成本，reader包含在观察进程内|
|review.json|事后短峰复核记录；原严格验收仍失败|
|derived_changes.csv|由打印的3位小数计算阶段差值，带输入精度标识|
|extraction.json|源哈希、打印次数、运行数和证据范围|
|figures/summary.png、summary.svg|由摘要生成的对照图；不伪造原始曲线或误差条|
|request_completion.log|用户回传请求级离线摘要，保留原始字节，不含逐请求原始样本|
|request_completion_summary.csv|8条原打印统计行：两策略的请求两种起点、initial集合与末项计划释放偏移|
|request_completion_comparisons.csv|3条原打印均值/p99变化率，基于测试机未舍入统计量|
|request_completion_evidence.json|补充日志哈希、记录数、与原复核记录的一致性及打印值差值|

复现数据提取与图：

```bash
python3 results/stage3-arm-3007/extract_terminal.py
# 绘图需matplotlib；只读取归档日志，无压力运行
python3 results/stage3-arm-3007/plot_summary.py
```

提取脚本仅用标准库。图在本地已有matplotlib环境生成，测试机不需要安装绘图库或执行上述命令。没有收到测试机原始tasks.csv、逐波CSV或trace，因此本地没有重算逐任务p99、相关系数、逐阶段迁出率或峰值置信区间。每策略一轮，所有差值只描述本次观测。

请求补充也由同一提取脚本归档：检查两策略各600请求、8条统计/3条对比、原复核记录一致，并在输入值与百分比均保留3位小数的舍入区间内检查算术一致。未从子任务分位数推导请求分位数。现有图仍为子任务与CPU诊断，完整请求的主结果见分析报告第4.1节。

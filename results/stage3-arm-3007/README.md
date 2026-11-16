# ARM 3007：短峰负载开关对照的原始证据

结论和完整负载说明见[3007分析报告](../../stages/03-independent/ARM_3007_SHORT_PEAK_POLICY_REPORT.md)。`3007.log`保持用户附件原始字节，SHA256：`80ac28db53ed1c032c2b4fd3e401ed3f6e87e3a97c395e7f9396486462ab84a8`。

本轮新增一次source_only，home复用3006。两条`SKIP home`来自证据预检与正常恢复路径，不是两次home执行；每张表打印两次，提取时逐项检查两份一致，只保留一份。home的阶段时延、N/Q分桶和CPU表已与3006逐项核对相同。

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

复现数据提取与图：

```bash
python3 results/stage3-arm-3007/extract_terminal.py
# 绘图需matplotlib；只读取归档日志，无压力运行
python3 results/stage3-arm-3007/plot_summary.py
```

提取脚本仅用标准库。图在本地已有matplotlib环境生成，测试机不需要安装绘图库或执行上述命令。没有收到测试机原始tasks.csv、逐波CSV或trace，因此本地没有重算逐任务p99、相关系数、逐阶段迁出率或峰值置信区间。每策略一轮，所有差值只描述本次观测。

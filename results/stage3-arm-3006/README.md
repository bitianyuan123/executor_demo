# ARM 3006：短峰home基线的终端证据

解读见[3006分析报告](../../stages/03-independent/ARM_3006_SHORT_PEAK_REPORT.md)。源日志为用户提供的`3006.log`，保持原始字节；SHA256为`6e12456725a9bd52357322da7b821ceea392165452f15b18765ea7e2896bed39`。

日志里实际观察到一次home工作负载，随后续跑命中`SKIP home`，source_only被形态门禁阻止。八种CSV表都打印了四份，提取时检查四份完全相同，只保存一份，不能当成四个重复实验。

|文件|内容|
|---|---|
|policy_summary.csv、decomposition.csv|整体和initial/steady任务时延|
|buckets.csv|按提交前N/C、Q/C与任务阶段分桶|
|cpu_procstat.csv|100ms累计计数CPU观测|
|cpu_scopes.csv|1ms事件重建的各池与并集统计|
|cpu_thresholds.csv|同一trace的80/90/95%阈值敏感性|
|cpu_aligned_windows.csv|按实际首提交对齐的窗口平均驻留率|
|initial_spans.csv|初始80项实际投递跨度|
|extraction.json|源哈希、重复打印数、证据范围|

`python3 results/stage3-arm-3006/extract_terminal.py`可以从随附日志重新生成这些表；它不启动实验。原始逐任务CSV、逐波CSV和trace尚未传回，因此本地只复核终端证据、提取一致性和算术，没有重新计算真实逐任务p99，也没有确认83个碎峰的内部形态。

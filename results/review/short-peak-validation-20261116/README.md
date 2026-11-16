# 短峰工程的本地验证摘要

解读与执行范围见[开发验证记录](../../../stages/03-independent/SHORT_PEAK_VALIDATION.md)。这是x86四CPU开发验证，非ARM验收。

- `functional-plan.json`、`functional-report.*`：无trace功能A/B，3秒，明确标记FUNCTIONAL_ONLY，保留候选p99上升的结果。
- `cpu-home-plan.json`、`cpu-home-summary.json`、`cpu-home-report.txt`：独立6秒home事件采集。近起点峰宽median10ms/p9011ms，但未通过全部暂定形态条件；没有候选运行。
- `cpu-home-waves.csv`：逐请求真实提交对齐的峰宽、背景、截短与覆盖。
- `cpu-home-peak.svg`：一波实际短时曲线；`cpu-home-aligned*.svg`：相位均值及局部放大，不能当逐波峰宽。
- `resume-and-gate.json`：已完成基线复用、门禁失败阻止候选的证据。
- `ctest.log`：最终一轮前12项通过，CPU分析遇到显示层变更期间旧断言；`ctest-cpu-analysis-after-update.log`：同步更新后第13项通过。更早完整13/13回归通过，但未另保存该轮日志。
- `source-hashes.json`：最终计量代码哈希。

上述为自动输出的报告/逐波数据摘存，不是完整任务CSV或事件trace。完整开发原始文件仍保留在`resume-and-gate.json`列出的本地生成目录；这些绝对路径属于开发机，测试环境不应尝试使用。复现应在自己的目录生成新计划。没有将开发时延或CPU数字推广到真实业务。

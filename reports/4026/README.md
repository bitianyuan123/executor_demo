# 4026：六轮阶段/实际池联合核对

[分析与下一步](../../docs/TARGET_V2_4026_REVIEW.md) · [HTML](report.html) · [PDF](report.pdf) · [目标保护候选合同](../../docs/DESTINATION_ADMISSION_EXPERIMENT.md)

本轮读取六份已有3M tail4运行的离线JSON，不增加性能样本。`evidence.json`保留完整原JSON、源行号、匹配的4024运行和衍生指标；`comparison.csv`及`tables.md`为便于复核的简表。逐项校验计数、比例、等待面积守恒、同池交集为同NUMA交集子集，以及4024角色均值的舍入一致性。

4026日志SHA256为`9e3a8d4c3b7d0c3c29727cd3ba77a55aaaead5532d016bb2ad54cfdca1cbcdf9`。报告内目标机三文件hash仅保留其声明，本地没有原CSV进行二次复核。4024请求时延取192请求cohort，进程CPU成本取整轮240请求，未补造实时CPU曲线。

```bash
python3 reports/4026/build_evidence.py --source-log /path/to/4026.log
python3 reports/4026/render_figures.py
python3 reports/final_review/build_report.py --source docs/TARGET_V2_4026_REVIEW.md \
  --output-dir reports/4026 --basename report --figure-layout inline
```

生成器完成474项检查；其边界见`limitations`。`wait_localization`区分相遇callback比例与等待面积覆盖，`request_tradeoff`展示同轮整体时延和CPU成本。两seed分别绘制，不平均p99。HTML内嵌两张图，PDF嵌入中文字体；源码和相关合同链接仍需完整仓库。

阶段重叠分析本身的九项功能测试入口保留为：

```bash
python3 experiments/affinity/tests/request_phase_overlap_test.py
```

这验证离线解析/区间逻辑，不构成新目标性能验收。当前无需目标机重编、重跑或重新分析相同六轮。目标准入和持续门控均为下一版候选，C++策略、默认值和图合同不变。

# 4024统一窗口比较与角色证据

[审阅正文](../../docs/TARGET_V2_4024_REVIEW.md) · [离线HTML](report.html) · [PDF](report.pdf)

本目录将4024新增的24份cohort/flow导出接到4023的36轮执行，不增加实验数，不改原始4023统计。三个工作量档都使用192请求的planned cohort；CPU成本沿用全240请求。

- `evidence.json`：原36轮命令/摘要、统一cohort/角色、24配对，附来源日志与行号。
- `paired_comparisons.csv`、`tables.md`：同图同seed的候选/home变化及完整绝对值。
- `cohort_policy_pairs.png/.svg`：mean/p99与CPU成本的逐seed比较。
- `tail4_role_waits.png/.svg`：3M尾链图首波与后继等待，分别保留两个seed。

只在开发/分析环境重建，不需Folly或执行负载：

```bash
python3 reports/4024/build_evidence.py --source-log /path/to/4024.log
python3 reports/4024/render_figures.py
python3 reports/final_review/build_report.py --source docs/TARGET_V2_4024_REVIEW.md \
  --output-dir reports/4024 --basename report --figure-layout inline
```

输入SHA256固定为`702778049dc293ee6f2d2cc96f0a722a8db260d8a7247b49f9bf579192d781b9`；另外读取版本化的`../4023/evidence.json`并保存其hash。复算检查整轮CPU/在途峰一致、192请求完整性、工作指纹、角色/改投/原因守恒及配对算术。源数据仅为终端导出，不代替目标原CSV的独立复核。

下一步已有CSV的联合分析使用[阶段重叠指南](../../docs/REQUEST_PHASE_OVERLAP_GUIDE.md)。真实业务相似性判断还须结合后续[4025历史trace审阅](../../docs/TARGET_V2_4025_REVIEW.md)，本批不提供新的实时CPU曲线。

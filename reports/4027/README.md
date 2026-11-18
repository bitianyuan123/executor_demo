# 4027：目标准入12轮干预

**后续尾部核对已由[4028](../4028/README.md)完成，当前筛选收束，无需重复本页原下一步。** 本目录保留当时的证据与结果。

[完整分析与只读下一步](../../docs/TARGET_V2_4027_REVIEW.md) · [HTML](report.html) · [PDF](report.pdf)

目标上限关闭/1.0/0.75、两个既有图、seed421/422共12轮新运行。同一构建和工作合同，每轮240请求完成；请求时延及角色指标取计划到达[2,18)秒的192请求，CPU和准入计数取整轮。目标保护明显改变选池和阶段等待，但当前两档都增加请求平均时延，不能选为全面改进或生产默认。

`evidence.json`保留21行终端JSON及源行号、全部逐轮/配对衍生量和边界；`comparison.csv`、`paired_deltas.csv`便于复核。源日志SHA256为`dcbfb3fa9ed56de972cd02d96a0179efa9d0c56ccc2ce182c805eb7b24e19f5f`。701项核对覆盖配置/资源、源码hash、完成/角色/等待守恒及8组差值重算。本地没有目标原CSV，原文件hash仅保留目标工具声明，不能视作本地已重新验证原始文件。

```bash
python3 reports/4027/build_evidence.py --source-log /path/to/4027.log
python3 reports/4027/render_figures.py
python3 reports/final_review/build_report.py --source docs/TARGET_V2_4027_REVIEW.md \
  --output-dir reports/4027 --basename report --figure-layout inline
```

`request_tradeoff`展示同图同seed对off的mean/p99/CPU成本差值；`phase_tradeoff`同时展示tail4首波p99、后继p99与最大等待，避免只看局部p99宣布成功。两seed不合并p99，没有置信区间，也没有补造实时CPU曲线。PNG/SVG、HTML/PDF均可离线查看。

下一步只解析已有12轮的长等待及尾请求，不新增性能样本；命令见完整分析第5节。此次不改C++调度、默认目标上限、负载或Folly。早先编译/功能验证证据仍见[开发验证记录](../../results/review/destination_admission_validation.json)，不与本次目标机性能混合。

新补查工具9项合成测试及对应CTest通过；现有开发小矩阵只读验收通过，13行报告标记development，输入文件未改变。[补查工具验证记录](tail_audit_validation.json)保存源码hash与验证范围。这不是新目标机性能结果。

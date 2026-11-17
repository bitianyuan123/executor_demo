# 自包含阶段报告与可追溯图表

从基础概念开始阅读，无需先看历史日志：

- [离线 HTML](executor_affinity_report.html)：内嵌全部 15 张图，可搜索、有目录，可单文件复制阅读。
- [PDF](executor_affinity_report.pdf)：中文字体内嵌，适合离线传阅；宽图采用横版图页。
- [Markdown 源报告](REPORT.md)：一条请求从到达到80路扇出 → Q/I/O/R读写时序 → 算子逐次选池 → 六个量化场景的绝对指标 → 历史trace指导与候选初值；第7节加入六轮同合同CPU实测。
- [数据来源与配对](data/comparisons.json)、[运行 CSV](data/runs.csv)、[配对 CSV](data/paired_comparisons.csv)、[图表清单](figures/manifest.json)。

HTML/PDF 单独阅读正文和图表不依赖网络。HTML 中附录的仓库文件链接需保留目录结构才能打开，PDF 将这类链接保留为可读标签；进一步核查源码和数据时使用完整仓库。图表同时提供 PNG 与可缩放 SVG。旧批次数字来自已审阅的回传摘要。新增4021保留终端逐窗CPU的5724条记录及源行号，可复算并绘图；它们不代替未取得的原始任务CSV/trace。其他批次缺失CPU利用率保持缺失，不用CPU/请求冒充。

## 重建

不需 Folly、C++ 编译、目标机网络或重新压测。Debian/Ubuntu 图表和文档依赖可安装为：

```bash
sudo apt-get install python3-matplotlib python3-reportlab python3-markdown-it \
  fonts-wqy-zenhei poppler-utils
```

在仓库根目录执行：

```bash
python3 reports/final_review/build_data.py
python3 reports/final_review/build_scenario_data.py
python3 reports/final_review/render_figures.py
python3 reports/final_review/build_report.py
python3 reports/final_review/validate_report.py --require-artifacts
```

`build_data.py` 从版本化的审阅摘要重建 97 条选定运行、108 个配对和 68 条机制记录。独立任务及历史 trace 的整理数据在 `data/independent_evidence.json`、`data/trace_evidence.json`，保留定义和来源；它们不是原始样本。`build_scenario_data.py` 进一步按六个量化场景整理79条运行，并从保存的4011必要原文表格行补齐迁移/角色计数。`render_figures.py` 读取这些历史数据及新增`results/review/4021_summary.json`、`4021_cpu_windows.csv`生成15张图；`cpu_window_metrics.py`复算每窗最忙cluster及同NUMA极差。`build_report.py` 支持 `--source`、`--output-dir`，PDF 可用 `--font` 指定兼容中文 TrueType 字体；绘图默认使用 `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc`。

## 验证范围

`validate_report.py` 检查审阅源哈希、JSON 指针与指标一致性、运行覆盖、配对算术、机制字段、图表产物、Markdown 本地链接、HTML 内嵌图片和 PDF 头部，结果保存在 `validation.json`。另用 PDF 文本提取与页面渲染检查中文和排版。

这些验证保证交付物与已有证据一致，不构成新性能实验、正式置信区间或生产验收。本次重组没有更改调度实现、负载合同或阈值。独立多轮性能、跨 NUMA、真实网络/阻塞及持续饱和仍按报告列出的具体决策触发，不自动追加实验矩阵。

## CPU补采已完成

[4021六轮审阅](../../docs/TARGET_V2_4021_REVIEW.md)补齐一个明确合同的同轮CPU、时延与改投证据：均匀12 QPS，80×2+4×2业务算子，300万迭代/10万访问的task扰动，每池16线程，parent/all、同NUMA、trace1。图14/15按实际窗口与两组独立对照展示结果。此前4011–4020的CPU缺失项未改变。

每轮159个完整六cluster窗口，约99.37%覆盖。六轮全成功且同工作审计通过，**无需为同一目的重跑**。采集命令与边界仍保留于[复现指南](CPU_CAPTURE_GUIDE.md)。没有修改C++、Folly、负载或路由阈值。

回传日志可用标准库提取器重新审阅（日志路径由当地指定，不依赖本机附件目录）：

```bash
python3 scripts/review_cluster_cpu_log.py /path/to/4021.log --output-dir /tmp/review-4021
python3 experiments/affinity/tests/review_cluster_cpu_log_test.py
python3 experiments/affinity/tests/cpu_window_metrics_test.py
```

提取器只接受固定六轮的完整实际执行块，排除计划命令和末尾重复表，保留源SHA256与原行号；拒绝窗口、工作或角色计数不一致。源日志不在仓库也能用已版本化摘要和CSV复算、重建报告。原CPU采集器测试仍为`python3 experiments/affinity/tests/cluster_cpu_capture_test.py`；此次只改分析和文档，无需重新编译目标程序。

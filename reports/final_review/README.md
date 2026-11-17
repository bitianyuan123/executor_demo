# 自包含阶段报告与可追溯图表

从基础概念开始阅读，无需先看历史日志：

- [离线 HTML](executor_affinity_report.html)：内嵌全部 13 张图，可搜索、有目录，可单文件复制阅读。
- [PDF](executor_affinity_report.pdf)：中文字体内嵌，适合离线传阅；宽图采用横版图页。
- [Markdown 源报告](REPORT.md)：一条请求从到达到80路扇出 → Q/I/O/R读写时序 → 算子逐次选池 → 六个量化场景的绝对指标 → 历史trace指导与候选初值。
- [数据来源与配对](data/comparisons.json)、[运行 CSV](data/runs.csv)、[配对 CSV](data/paired_comparisons.csv)、[图表清单](figures/manifest.json)。

HTML/PDF 单独阅读正文和图表不依赖网络。HTML 中附录的仓库文件链接需保留目录结构才能打开，PDF 将这类链接保留为可读标签；进一步核查源码和数据时使用完整仓库。图表同时提供 PNG 与可缩放 SVG。所有数字来自已审阅的回传摘要，不是重新分析不可取得的原始目标 CSV/trace；缺失 CPU 利用率保持缺失，不用 CPU/请求冒充。

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

`build_data.py` 从版本化的审阅摘要重建 97 条选定运行、108 个配对和 68 条机制记录。独立任务及历史 trace 的整理数据在 `data/independent_evidence.json`、`data/trace_evidence.json`，保留定义和来源；它们不是原始样本。`build_scenario_data.py` 进一步按六个量化场景整理79条运行，并从保存的4011必要原文表格行补齐迁移/角色计数。`render_figures.py` 读取这些数据生成 13 张图。`build_report.py` 支持 `--source`、`--output-dir`，PDF 可用 `--font` 指定兼容中文 TrueType 字体；绘图默认使用 `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc`。

## 验证范围

`validate_report.py` 检查审阅源哈希、JSON 指针与指标一致性、运行覆盖、配对算术、机制字段、图表产物、Markdown 本地链接、HTML 内嵌图片和 PDF 头部，结果保存在 `validation.json`。另用 PDF 文本提取与页面渲染检查中文和排版。

这些验证保证交付物与已有证据一致，不构成新性能实验、正式置信区间或生产验收。本次重组没有更改调度实现、负载合同或阈值。独立多轮性能、跨 NUMA、真实网络/阻塞及持续饱和仍按报告列出的具体决策触发，不自动追加实验矩阵。

## 实时CPU证据缺口

既有4011–4020主要运行未保存有效逐cluster CPU序列。新增[可选补采指南](CPU_CAPTURE_GUIDE.md)和独立采集器，只为这一缺口提供同合同有限对照；不以callback占用代替CPU，不要求重跑全部矩阵。采集器测试：`python3 experiments/affinity/tests/cluster_cpu_capture_test.py`。本次C++、Folly和路由阈值没有更改。

# 4025历史服务时间约束与门控候选

[分析与设计](../../docs/TARGET_V2_4025_REVIEW.md) · [离线HTML](report.html) · [PDF](report.pdf)

本轮只读取4025终端报告；没有原始series/trace，也未重扫生产文件。`evidence.json`保留明确列、源行号、1115个高段的分组聚合和共同桶计数；缺标签/歧义分母不补造为可靠统计。原报告输入hash仅按其声明保存。

复算与绘图：

```bash
python3 reports/4025/build_evidence.py --source-log /path/to/4025.log
python3 reports/4025/render_figures.py
python3 reports/final_review/build_report.py --source docs/TARGET_V2_4025_REVIEW.md \
  --output-dir reports/4025 --basename report --figure-layout inline
```

固定输入日志SHA256：`21cf4d7d8a9c5fffec298b4a96946fb67c4dab725cbcdd92a38ea7c99722c6ff`。核对分组计数、时间、面积舍入容差、面积补集、间隔分母和同时高服务桶的重数守恒。它不验证未知trace覆盖，也不推导在线N、请求DAG或生产阈值。

图表`historical_service_shapes.png/.svg`区分事件数与服务量；所有高服务阈值为事后`S>2×本池均值`，不是将来每cluster一池的在线门槛。PressureGate是设计候选、尚未实现，本轮没有新性能实验。

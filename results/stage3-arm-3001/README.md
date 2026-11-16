# ARM 3001 终端证据与复算

`3001.log` 是用户上传附件的逐字节副本，包含 CRLF；`source.sha256` 校验原始文件。没有 ARM 逐任务 CSV、metadata 或完整 manifest。不得将这里的汇总当作原始任务级重新分析。

- `extract_terminal.py`：标准库解析器，去重 stdout 与 Markdown 的重复打印行，检查重复数值相容，保留行号；不生成 CI。
- `runs.csv`：128 条独立运行摘要，ARM 122 轮、dev preset 6 轮。
- `cases.csv`：按环境、阶段、case 汇总打印值；p99 列为每轮 p99 的均值，不是 pooled p99。
- `quality.csv`：288 个唯一指标/目标/模型预测摘要；stdout 的四位小数优先于 Markdown 的三位小数。
- `summary.json`：输入哈希、轮数、case 汇总、预测摘要与适用限制。
- `subsequent_hostname_confirmation.txt`：后续用户打印配置差异的独立转录，说明唯一差异为 hostname；不是原始日志的一部分。
- [审查报告](../../stages/03-independent/ARM_3001_REVIEW.md)：机制结论、冻结边界、停止扩大矩阵的理由。

从仓库根目录运行：

```bash
python3 results/stage3-arm-3001/extract_terminal.py
cd results/stage3-arm-3001
sha256sum -c source.sha256
```

也可输出到临时目录核对：

```bash
python3 results/stage3-arm-3001/extract_terminal.py --output /tmp/stage3-arm-3001-recheck
diff -u results/stage3-arm-3001/cases.csv /tmp/stage3-arm-3001-recheck/cases.csv
diff -u results/stage3-arm-3001/summary.json /tmp/stage3-arm-3001-recheck/summary.json
```

正式配对重算应在 ARM 上读取现存运行目录，显式声明 `localhost.localdomain` 与 `localhost` 的同机别名，并保留其余资源和任务序列校验。这里不能代替该步骤。

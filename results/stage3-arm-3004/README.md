# 3004：request-wave 测试终端证据

这里归档用户提供的 `3004.log` 原始字节及由终端表格提取的数据。没有在本地运行 ARM 负载，也没有收到测试机的原始 task CSV、CPU 采样 CSV、plan 或 manifest；本目录不能替代这些原始文件。

原附件 SHA-256：`5e3d792be96f60c72b656b449ca6918f62cf9a0529a56f33385b20338efb94ec`。`3004.log` 保留原来的 CRLF 换行，`source.sha256` 可用于检查原字节。

## 打印次数与实验次数

附件中有四次 `REQUEST-WAVE DESCRIPTIVE REPORT`：

| 报告 | 打印次数 | 底层运行 | 分析请求窗口 | 每策略分析样本 |
|---|---:|---|---|---:|
| 校准 | 2 | home 一次，持续20秒 | `[5,20)` 秒内到达的请求 | 28,800任务 / 180请求 |
| 正式 | 2 | home、guarded、source_only 各一次，持续60秒 | `[10,60)` 秒内到达的请求 | 96,000任务 / 600请求 |

自动分析打印与随后 `cat report.txt` 是重复打印，**不是两次独立实验**。提取脚本逐表核对两份打印完全相同后去重。校准与正式数据分开保存；不合并为重复种子，不计算重复运行的置信区间。

正式顺序为 `home → guarded → source_only`。三组均为6池、每池128线程、每池16个逻辑CPU，cluster绑定；12请求/秒，每请求初始80任务在20ms窗口释放，再在400ms窗口均匀释放80任务。任务CPU预算均值25ms、jitter=0.5、无长尾，有限样本均值归一化为25ms。它是独立CPU任务模型，不是100ms真实算子的完整复现或DAG模型。

## 文件

- `3004.log`、`source.sha256`：原始终端附件及SHA。
- `extract_terminal.py`：只使用Python标准库的提取与一致性验证脚本。
- `evidence.json`：来源、打印去重、原精度表格、校验结果与证据限制。
- `calibration_policy_summary.csv` / `formal_policy_summary.csv`：整体时延、CPU及路由比例。
- `calibration_comparisons.csv` / `formal_comparisons.csv`：打印的策略比较百分比。
- `calibration_cpu.csv` / `formal_cpu.csv`：CPU union及6池观测。
- `calibration_initial_spans.csv` / `formal_initial_spans.csv`：每请求初始任务的实际提交跨度。
- `calibration_decomposition.csv` / `formal_decomposition.csv`：all/initial/steady阶段的时延分解。
- `calibration_buckets.csv` / `formal_buckets.csv`：home策略提交前N/C、Q/C固定分桶。
- `plot_report.py`、`figures/policy_effects.{png,svg}`、`figures/conditional_p99.{png,svg}`：从已打印聚合值绘制的静态图。

校准终端没有打印comparisons或buckets，其两个CSV只有表头；没有用正式结果填补。数字字符串保留终端精度，通常是三位小数；`NA`仍为`NA`，没有补造有效数字。

## 验证内容

`extract_terminal.py` 已检查：

1. 原附件字节SHA一致；校准和正式各两份打印的输入说明、警告、比较状态及各表相同。
2. 正式三策略均为96,000任务/600请求，initial与steady各48,000任务；校准为28,800/180。
3. 每策略CPU表包含union和6池，summary里的CPU值与union行一致；初始跨度超30ms的数量与比例一致。
4. 正式36桶覆盖3阶段×2指标×6边界，每个阶段/指标的任务数量与阶段表一致；all桶计数等于initial+steady。
5. S、W、X、L、D分桶均值按任务数加权后，与对应home阶段均值的差异均在三位小数舍入容差内（≤0.001ms）。阶段均值分解也通过舍入容差校验。
6. 三组策略比较的均值/p99变化比例，与三位时延数据所允许的舍入区间一致。脚本传播±0.0005的打印误差，不强求直接相除后最后一位完全相同。

**没有合并、相加或平均任何桶的p99**。终端聚合值不足以重新计算总体p99、相关系数、预测误差或置信区间。正式`comparison_valid=true`是测试机分析器打印的结论；本地校验验证的是表格内部一致性，不能独立复核未上传的原始输入轨迹/manifest哈希。

## 统计口径与读图

- S=`end-submit`；W=`start-submit`；X=`end-start`；L=`end-task_planned`；D=`submit-task_planned`，单位ms。每任务和均值满足S=W+X、L=D+S；p99不满足加法。
- 请求窗口包含所选请求的所有后续任务及排空阶段完成，不按完成时间截断。
- CPU利用率使用`[10,60)`正式到达窗口内完整的100ms采样区间；不是请求整个生命周期的CPU利用率。100ms采样峰值不能精确解析不足30ms的尖峰。
- `conditional_p99`采用相同的固定数值边界，但**N/C与Q/C各桶包含的任务不同**。每幅图只展示home策略的原有分桶p99，不能把曲线差异直接解释为预测精度或因果优势。
- Q/C的`[4,+∞)`桶为空，图中不补值、不延伸曲线。steady的`[0.8,1)`桶为N=459、Q=667任务，标注较少的尾部观察数量；其p99只用于描述。
- 策略图的时延和CPU使用独立坐标轴，CPU轴保持0–100%；没有以截断CPU轴夸大约50%的细小差异。

## 复现

在仓库根目录运行，提取无需额外依赖，也不会启动测试负载：

```bash
python3 results/stage3-arm-3004/extract_terminal.py
cd results/stage3-arm-3004
sha256sum -c source.sha256
```

静态绘图需已有matplotlib环境；下面的命令同样从仓库根目录运行：

```bash
python3 results/stage3-arm-3004/plot_report.py
```

绘图只读取本目录CSV。生成PNG和SVG用于报告，不从图形反推或重算ARM原始任务数据。

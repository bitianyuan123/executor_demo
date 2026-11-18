# 4023回传证据与图表

主分析见[4023审阅](../../docs/TARGET_V2_4023_REVIEW.md)，也可阅读内嵌图表的[离线HTML](report.html)或[PDF](report.pdf)。本目录只整理已回传的终端证据，不运行负载，不访问生产，不读取目标原始trace。

- `evidence.json`：3份原始profile摘要及派生量、36轮完整命令和全轮摘要、12轮cohort/角色报告、24个同图同seed配对；保留源行号。
- `request_cpu_windows.csv`：1127行原样数值，另附profile名称与源行号。
- `request_cpu_profiles.png/.svg`：显式request6的1ms服务与生命周期计数，不代表生产或请求分布。
- `shape_policy_pairs.png/.svg`：三档/两图全240请求的逐seed变化；CPU是资源成本，不是利用率。

复算需要原回传日志，只在开发/分析环境执行：

```bash
python3 reports/4023/build_evidence.py --source-log /path/to/4023.log
python3 reports/4023/render_figures.py
python3 reports/final_review/build_report.py --source docs/TARGET_V2_4023_REVIEW.md \
  --output-dir reports/4023 --basename report --figure-layout inline
```

输入固定SHA256为`790427ed9172febde1c9cdcc538783dd789e5347bf298cec0ca7b523206ffd1c`。脚本拒绝不同输入，并核对窗口连续、分区求和、驻留积分、累计服务、持续段、角色守恒和已打印的同图工作指纹。它不替代对目标原CSV/trace的独立复核。重建覆盖本目录派生产物，不修改原日志。

两类负载不可混配：profile保留task扰动和perf；形状矩阵关闭扰动且无perf。只有375k回传192请求cohort，另外两档为初步240请求摘要。原始构建标识未回传，不推断执行版本。该形状套件仍是未校准的结构假设，真实生产trace的定向时间分析没有包含在4023内。

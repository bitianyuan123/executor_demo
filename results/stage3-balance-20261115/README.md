# 阶段3负载指标与均衡：169轮开发验证证据

分析结论与实验条件见[开发结果报告](../../stages/03-independent/BALANCE_RESULTS.md)。这是共享x86开发机的短实验，不能当作隔离ARM测试结果。

- `summary.json`：各组逐case摘要、配对结果和成本统计；p99摘要为各轮p99的等权均值。
- `environment.json`：CPU范围、nice、编译器、原实验二进制SHA及精度修复后功能测试二进制SHA。
- `folly-prefix-manifest.json`：所用独立Folly安装副本的相关文件哈希；没有包含依赖库二进制。
- `ctest.log`：最终7组测试的原始日志。
- `raw-runs.tar.gz`：8组计划、169轮task/burst/pool CSV、metadata、summary、每轮输出、全部分析明细和终端摘要。
- `SHA256SUMS`：上述证据文件校验值。

归档中的每个`manifest.json`只把run/log路径改为相对于该manifest的路径，便于在任意目录解包重算。原始manifest字节另外保存在`manifest.original.json`，并记录其SHA；命令、原环境和冻结记录仍保留原始来源。不要把original与portable同时传入分析器，否则会造成重复轮次。

从仓库根目录重算，例如：

```bash
(cd results/stage3-balance-20261115 && sha256sum -c SHA256SUMS)
mkdir -p results/local/balance-evidence
tar -xzf results/stage3-balance-20261115/raw-runs.tar.gz \
  -C results/local/balance-evidence
python3 experiments/independent/scripts/analyze_balance.py \
  --manifest results/local/balance-evidence/stage3-balance-20261115/evaluate/manifest.json \
  --output results/local/balance-evidence/evaluate-recomputed
python3 experiments/independent/scripts/analyze_balance.py \
  --manifest results/local/balance-evidence/stage3-balance-20261115/observe-poisson/manifest.json \
  --output results/local/balance-evidence/poisson-recomputed
```

其他组名为`observe`、`observe-progress`、`observe-long`、`tune`、`minimal`、`cost`。解包后各`*-analysis/terminal.txt`可以直接打印；`prediction.csv`、`buckets.csv`、`correlations.csv`保存指标质量明细，`paired.csv`保存逐seed策略差，`observer_cost_pairs.csv`保留成本配对。所有坏结果/异常仍可追溯，不只收录最优行。

重跑性能应使用新输出目录；原性能C++源码为`7227e2d`。最终分支修正了非整数容量的输出精度并重新通过功能测试，旧整数容量矩阵未冒称为新二进制重跑。当前工程及目标机分步命令见[运行指南](../../stages/03-independent/BALANCE_GUIDE.md)。

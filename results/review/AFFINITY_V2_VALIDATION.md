# 阶段4 v2本地工程验收

本次在本地x86、GCC 14、已安装的Folly基线上验证新增门面与固定工作量模型。没有运行ARM生产性能实验，以下不能用于选择生产阈值或宣布某策略性能更好。

## 已执行

- 独立配置及Release构建：`cmake -S experiments/affinity -B build-affinity -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=../folly/_install`，单编译任务完成。
- `ctest --test-dir build-affinity --output-on-failure`：3/3通过，包含14项RequestExecutor单测、7项LocalityWorkload单测，以及13个完整小型端到端运行和错误/损坏结果检查。
- RequestExecutor：请求染色、分支上下文、跨线程continuation/Folly RequestContext、关闭与在途后继、取消、异常、部分批量拒绝、连续tags、queue=0而outstanding过载、回home、hop上限、fork-only及容量校验。
- LocalityWorkload：调度顺序和分支并发不改变结果、前驱数据影响后继、四模型计量、构造不预触payload、初始化与stage顺序、预算/溢出、页面查询unknown/partial。单独ASan+UBSan下7项通过；此项不代表完整Folly/门面经过TSan。
- 端到端：home/queue/outstanding/work、parent/home、chunk 1/2、trace 0/1、none/numa、regular/Poisson、request级异质成本估计均保持对应工作合同。拒绝不会让join挂死；非法参数失败；分析器保留窗口外晚完成，并拒绝截断CSV。
- 原默认根工程重新构建，15/15 CTest通过。
- 阶段5两种位置的底层执行器各24项单测通过；生成镜像检查通过。新增高层门面仍只在demo，并未声称已经实现双位置版本。

## 矩阵和终端报告联调

实际执行 `run_matrix.py --suite routing`，2 seeds × 4策略，共8轮；每轮8个request、每请求15个闭包。合计64请求、960闭包，全部完成，无拒绝。每seed四轮的到达hash、工作合同和最终checksum完全一致，报告成功完成6个相对home的配对。

原始小规模日志和终端报告在 [affinity-v2-smoke](affinity-v2-smoke/)。这些轮次是本地功能联调，每轮只有8个request，未控制性能测试所需的环境噪声；**只验收流程，不用于时延/CPU策略排名**。报告中的数值为实际输出，未挑选或修改。

```bash
python3 experiments/affinity/report.py results/review/affinity-v2-smoke/seed-*
python3 scripts/sync_business_executor.py --check --folly-dir ../folly
```

## 留给目标机的实测

先按[新工程指南](../../experiments/affinity/README.md)完成home、低QPS固定工作校准；回传终端报告与页查询结果。之后冻结工作量，按部署/线程数、L2指标、fork/all、parent/home、chunk、足迹、跨NUMA逐步推进。CPU capacity、SMT效率、cache/NUMA真实收益、端到端封装及观测增量成本尚未由本地验收证明。

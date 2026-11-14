# 独立任务负载实验：工程与运行指南

这个项目只使用原版 Folly CPUThreadPoolExecutor。每次 arrival 提交一个或多个独立 CPU 任务；没有 DAG、RPC、跨池均衡或阶段5扩展。先阅读[阶段3设计与分析方案](../../stages/03-independent/README.md)。

## 编译与功能测试

需要 Linux、支持C++20的编译器、CMake、Python3，以及已安装的原版 Folly（本仓库验证基线 `v2026.10.26.00`）。不需要包含 `AffinityExecutor.h` 的源码路径。以下命令从仓库根目录执行，安装目录按实际环境替换：

```bash
cmake -S experiments/independent -B build-independent \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/root/rank_demo/folly/_install
cmake --build build-independent -j1
ctest --test-dir build-independent --output-on-failure
build-independent/independent_load --help
```

独立项目包含三组CTest：9个黑盒测试方法覆盖原版执行器、计数守恒、四档插桩、确定性任务预算、绑定/RR、拒绝、取消、故障清理、采样截断和非法输入；5个分析测试覆盖p99、相关系数、缺失数据和矩阵生成；8个整轮留出预测测试检查训练/测试隔离与缺测处理。它们验证功能，不验证性能优劣。

## 开发机分步运行

先做约16秒的quick对照：两种线程数 × smooth/burst，相同平均任务率，默认使用本机allowed CPU自动划分1–2个分区。

```bash
python3 experiments/independent/scripts/run_matrix.py \
  --binary build-independent/independent_load --preset dev-quick \
  --output results/runs/independent-quick
```

输出目录必须不存在，防止覆盖既有证据。结果包含原始任务/批次CSV、JSON配置、每秒统计、分析报告和运行manifest。每轮串行执行，所有配置次序随机化。quick不能当作稳态容量测量。

先生成较大扫描配置并审查，而不是立即跑全部组合：

```bash
python3 experiments/independent/scripts/run_matrix.py \
  --preset dev-sweep --write-config /tmp/independent-dev.json
python3 experiments/independent/scripts/run_matrix.py \
  --config /tmp/independent-dev.json --binary build-independent/independent_load \
  --only cluster-t1-smooth-low,cluster-t2-smooth-low,cluster-t8-smooth-low \
  --output results/runs/independent-thread-sweep
```

JSON形式是 `{"base":{公共参数},"repeats":3,"seed":420,"cases":[{"name":"名称","options":{覆盖参数}}]}`。参数名与命令行一致、不带`--`。可重复使用 `--override duration=30` 等覆盖所有case；runner保存实际命令和二进制SHA256，失败不会被静默加入成功分析。需要更多压力时，先核对CPU目标与可用资源，再逐级增加arrival-rate。

单次运行示例：

```bash
build-independent/independent_load --output results/runs/independent-one \
  --pools 2 --threads 8 --affinity none \
  --arrival-rate 12 --duration 30 --burst-min 20 --burst-max 20 \
  --cpu-us 3000 --jitter 0.5 --long-prob 0.02 --long-factor 8 \
  --instrumentation full --seed 420
```

这里是12批次/s、每批20个独立任务，即240 tasks/s；不是12个DAG request/s。目标CPU需求约 `240 × .003 × 1.14 = .8208 CPU秒/s`，没有自动包含SMT/NUMA吞吐修正。

## 六池测试机

先保存硬件证据并核实cluster与SMT sibling关系：

```bash
python3 scripts/topology.py --output /tmp/independent-topology.json
```

将真实六组CPU列表写入 `CLUSTER_CPU_LISTS`（分号分组），其真实NUMA节点写入 `CLUSTER_NUMA_IDS`（逗号分隔）。不要直接把示意编号当成目标机拓扑。生成配置：

```bash
python3 experiments/independent/scripts/run_matrix.py --preset target \
  --cpus "$CLUSTER_CPU_LISTS" --numa "$CLUSTER_NUMA_IDS" \
  --write-config /tmp/independent-target.json
```

target强制6池、真实NUMA归属、组间不重叠，而且六组并集必须等于进程allowed CPU集合。若只测试服务器部分CPU，先用`taskset -c`把runner限制到该并集；子进程会继承相同CPU预算。这保证none/numa/cluster比较使用相同总CPU资源。

先选低负载和一档线程数，比较三种亲和：

```bash
python3 experiments/independent/scripts/run_matrix.py \
  --config /tmp/independent-target.json --binary build-independent/independent_load \
  --only none-t16-smooth-low,numa-t16-smooth-low,cluster-t16-smooth-low \
  --output results/runs/independent-target-first
```

候选target矩阵包含120秒运行、5次重复、100ms随机服务预算、8/16/64/256线程。100/400任务的burst是潜在过载探针，请按设计报告先标定能力，再改arrival-rate/准入和drain限制。检查内存及线程资源上限；无须一次运行全部矩阵。

## 分析与插桩对照

原始数据可单独重分析。选择计划到达在20–100秒的任务，保留这些任务在窗口结束之后的完成结果：

```bash
python3 experiments/independent/scripts/analyze.py \
  results/runs/independent-target-first/cluster-t16-smooth-low-r0 \
  --warmup-seconds 20 --steady-end-seconds 100 \
  --output results/local/independent-steady
```

生成Markdown和JSON。报告分开列出task时延、提交至开始等待、回调墙钟、queue分桶、同pool/目标服务时间分层、按计划到达秒的趋势；没有把queue当成内核runq。拒绝/取消/失败保持可见，只有完整成功的burst才有burst时延。

插桩对照使用同一固定迭代工作量：

```bash
python3 experiments/independent/scripts/run_matrix.py \
  --config experiments/independent/configs/instrumentation.json \
  --binary build-independent/independent_load \
  --output results/runs/independent-instrumentation
```

配置使用1池，四档观测、固定迭代、5次重复。`iterations-per-us`只是目标工作量到循环次数的换算，不保证真实微秒；先在测试机单线程低负载下标定，按需要修改参数。若修改工作量，四档必须一起修改。`none`仍有计时和安全计数，不能称零插桩。整体process CPU的测量区间排除线程池初始化和最终CSV序列化，包含发生器/采样/工作线程/排空收尾。

## 文件与字段

|文件|用途|
|---|---|
|metadata.json|完整配置、allowed/effective CPU mask、NUMA标签和限制|
|workers.csv|pool/TID/mask，便于独立内核采样对照|
|tasks.csv|逐任务状态、预生成目标、计划/提交/开始/结束、提交前负载、CPU时间|
|bursts.csv|每批任务数和完成/拒绝/取消/失败；全部成功才有完整批次时延|
|seconds.csv|按完成秒统计task数量/均值/p99，空秒留空时延|
|pools.csv|周期负载快照；采样截断必须结合summary检查|
|summary.json|守恒、总体task/burst指标、过程CPU、采样质量|
|manifest.json|矩阵顺序、种子、实际命令、返回码、二进制SHA256|

`tasks.csv`中未启用的计量字段为`-1`，不是0。没有完成任务时总体时延为JSON `null`。周期采样最多保留`--max-samples`行，默认100万；达到上限后记录丢弃数。任务遥测最多`--max-planned-tasks`条，启动前检查；运行过程中不增长任务数组。每个worker只写自己的任务记录，最终join后统一序列化。

可用`--max-outstanding`验证拒绝、`--drain-seconds`验证取消、`--fail-task`验证异常计数清理；这些故障实验不能混入成功性能统计。进程返回非零时查看日志与质量标志。缺少summary或CSV行数不一致的运行会被分析脚本拒绝。

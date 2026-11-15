# 独立任务负载实验：工程与运行指南

这个项目只使用原版 Folly CPUThreadPoolExecutor。每次 arrival 提交一个或多个独立 CPU 任务；默认保留原来的 home 池投递，没有 DAG、RPC 或阶段5扩展。先阅读[阶段3设计与分析方案](../../stages/03-independent/README.md)。

新增的阶段3负载均衡实验通过显式 `--routing threshold` 启用，在提交时选择其他原生线程池。指标选择、阈值冻结、留出验证及低开销模式见[设计](../../stages/03-independent/BALANCE_DESIGN.md)和[分步运行指南](../../stages/03-independent/BALANCE_GUIDE.md)。旧命令仍使用原行为。

开发机169轮验证与原始数据见[结果报告](../../stages/03-independent/BALANCE_RESULTS.md)。这些短测不代表ARM性能结论。

## 编译与功能测试

需要 Linux、支持C++20的编译器、CMake、Python3，以及已安装的原版 Folly（本仓库验证基线 `v2026.10.26.00`）。不需要包含 `AffinityExecutor.h` 的源码路径。以下命令从仓库根目录执行，安装目录按实际环境替换：

```bash
cmake -S experiments/independent -B build-independent \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/root/rank_demo/folly/_install
cmake --build build-independent -j1
ctest --test-dir build-independent --output-on-failure
build-independent/independent_load --help
```

独立项目包含七组CTest：原有功能、分析和整轮留出预测测试，以及阈值选池、实际线程池计数生命周期、负载均衡统计和矩阵生成测试。覆盖缺测、守恒、拒绝/取消/异常、预算配对、训练隔离和计划恢复；它们验证功能，不验证性能优劣。

## 开发机分步运行

先做约16秒的quick对照：两种线程数 × smooth/burst，相同平均任务率，默认使用本机allowed CPU自动划分1–2个分区。

```bash
python3 experiments/independent/scripts/run_matrix.py \
  --binary build-independent/independent_load --preset dev-quick \
  --output results/runs/independent-quick
```

输出目录必须不存在，防止覆盖既有证据。结果包含原始任务/批次CSV、JSON配置、每秒统计、分析报告和运行manifest。每轮串行执行，同重复号的 case 构成一个时间块，块内配置次序随机化。quick不能当作稳态容量测量。

先生成较大扫描配置并审查，而不是立即跑全部组合：

```bash
python3 experiments/independent/scripts/run_matrix.py \
  --preset dev-sweep --write-config /tmp/independent-dev.json
python3 experiments/independent/scripts/run_matrix.py \
  --config /tmp/independent-dev.json --binary build-independent/independent_load \
  --only cluster-t1-smooth-low,cluster-t2-smooth-low,cluster-t8-smooth-low \
  --output results/runs/independent-thread-sweep
```

JSON形式是 `{"base":{公共参数},"repeats":3,"seed":420,"analysis":{"warmup_seconds":2,"steady_end_seconds":8},"cases":[{"name":"名称","options":{覆盖参数}}]}`。`analysis` 可省略；存在时 runner 自动使用该计划到达窗口。参数名与命令行一致、不带`--`。可重复使用 `--override duration=30` 等覆盖所有case；改变时长时必须同步修改配置中的 analysis 窗口，否则越界会被拒绝；runner保存实际命令和二进制SHA256，失败不会被静默加入成功分析。需要更多压力时，先核对CPU目标与可用资源，再逐级增加arrival-rate。

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

上述旧 target preset 仅用于历史粗筛，不能覆盖全部正式因果对照。它包含120秒运行、5次重复、100ms随机服务预算、8/16/64/256线程。完整正式候选设计应改用下节的分族生成器。100/400任务的burst是潜在过载探针，请按设计报告先标定能力，再改arrival-rate/准入和drain限制。检查内存及线程资源上限；无须一次运行全部矩阵。

## 本轮新增的可控实验设计

完整复核及九个实验族见[设计复核](../../stages/03-independent/DESIGN_REVIEW.md)。建议先生成一族，再审查、选择运行，而不是使用一个混合所有变量的大矩阵：

```bash
python3 stages/03-independent/make_cases.py --family burst \
  --deployment dev --task-rate 1600 --duration 8 --warmup 2 \
  --output /tmp/stage3-burst.json
python3 experiments/independent/scripts/run_matrix.py \
  --config /tmp/stage3-burst.json --binary build-independent/independent_load \
  --output results/runs/independent-burst-review
```

新增控制项：`--mean-normalization exact` 让有限样本总预算精确等于任务数乘 cpu-us；`--burst-placement spread` 在相同 arrival 下将独立任务轮流放到各池；`--estimate-jitter .75` 只给观测估计添加独立随机误差。默认分别为 none、local、0，原始数据的工作负载定义保持不变。bursts.csv 的 pool 仍为该 arrival 的 RR home 标签，spread 下各 task 的实际池以 tasks.csv 的 pool 为准。

目标机示例（1000 tasks/s 仅为命令格式示例，必须替换为你的已测能力）：

```bash
python3 stages/03-independent/make_cases.py --family burst --deployment target \
  --cpus "$CLUSTER_CPU_LISTS" --numa "$CLUSTER_NUMA_IDS" \
  --capacity-task-rate 1000 --load-fractions .2,.5,.8,1.1 \
  --output /tmp/stage3-target-burst.json
```

目标机生成器默认300秒、30秒预热、7个独立重复，输出预计运行小时数及每个case的任务/arrival样本数警告；这些是候选起点，不是统计功效保证。未测容量时使用显式 `--task-rate`，结果仅报告实际任务率与 offered CPU demand。`calibration` 族要求目标机显式给 `--rates`；`qps12` 固定到达率且会随扇出提高总需求，与等压力burst实验分开。

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

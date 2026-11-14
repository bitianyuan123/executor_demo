# 阶段 3 实验设计复核：可控制的差异与可支持的结论

本轮复核确认：原工程已能正确执行独立任务，但旧 target preset 还不足以独立证明所有设计问题。原本把同均值长尾、容量标定和部分对照留给人工编辑配置；不同 burst 还同时改变时间聚集与池内聚集。现补齐控制项及逐问题矩阵。既有 [RESULTS](RESULTS.md) 保留为原版本的短轮次观测，不用新设计替旧证据增加结论强度。

## 1. 修正了什么

|旧限制|本轮修正|仍需核实的边界|
|---|---|---|
|2% 的 8 倍长尾会使平均 CPU 预算增加 14%|`--mean-normalization exact` 精确匹配整轮有限样本总预算；分布族默认开启|默认 none 保留原始独立采样；exact 会产生有限样本相关性，改变绝对长尾时长|
|smooth 与 local burst 同时改变时间和空间分配|新增 `--burst-placement spread`；smooth → spread burst → local burst 三角对照|spread 是独立任务实验控制项，不是阶段 4 的在线负载均衡算法|
|固定 12 arrivals/s 下增加 K，和相同平均压力下增加 K 被放在同一候选表|单列 `burst` 等需求族与 `qps12` 需求增长族|后者可能持续过载，不能当作稳态延迟比较|
|target 预设任务率未关联实测容量|目标配置要求显式 task rate、已测参考容量或 calibration rates|参考容量只属于当时的工作分布、CPU 配额、亲和和线程配置|
|所有重复全局混洗，配对运行可能相隔很久|同重复号为时间块，块内 case 随机化|长时间频率/邻机变化仍需环境证据与重复|
|设计要求预热窗口，但 runner 不会读取矩阵中的窗口|新增 `analysis` 配置，runner 自动传给分析器；时长覆盖冲突直接报错|裁掉启动期不能把持续增长的队列变成稳态|
|工作量 oracle 过于理想；常数倍误差又只是指标缩放|新增 `--estimate-jitter`，使用独立随机流；提供固定估计和独立误差对照|噪声加在合成已知目标上仍是理想灵敏度，不是已验证的生产估计器|

exact 的算法先保留每个任务至少 1 微秒，再按原始预算比例分配剩余整数预算，用累计舍入保证总和为 `N × cpu-us`，不需要排序或第二份任务数组。生成发生在计时前。随机流分别控制到达、宽度、服务预算和估计误差；添加估计误差不改变实际工作。

**“精确等均值”指整轮生成的有限任务集。**如果再截取预热后的 cohort，固定/均匀/长尾三种分布的 cohort 总预算可能略有不同，必须报告该窗口的目标预算均值和实际 CPU，不能把整轮配平误写成窗口也精确配平。延长运行、增加独立重复，或用已知窗口内完全配平的输入才能进一步缩小这项影响。相同预算序列的 local/spread 则能逐任务严格配对。

## 2. 九个独立实验族

生成器为 [`make_cases.py`](make_cases.py)，只输出 JSON、不运行负载。使用原 runner 执行；各族不必全部笛卡尔相乘。

|family|控制与改变|主要统计量和判断|
|---|---|---|
|calibration|同 CPU 分布/线程/亲和，显式逐级 task/s；queue 插桩|完成率、计划到达队列趋势、未完成数、发生器迟到；确定稳定区间，不用整轮 drain 后吞吐冒充稳态能力|
|burst|同 task/s、同 CPU 参数；K=1/20/100/400；K>1 比 local/spread|queue→等待、task/完整 burst mean/p99；分离时间集中和池内集中|
|arrival|同期望 task/s、K=20；regular/Poisson|到达变异及局部重叠的影响；Poisson 实际任务数不同，必须保留并报告|
|distribution|同整轮平均预算；固定、均匀、2%长尾；K=20|队列长度与 work estimate 的预测价值是否随服务变异增大；检查 cohort 均值偏差|
|deployment|完全相同 offered workload；none/numa/cluster × 8/16/64/256 线程（目标默认）|队列等待和回调墙钟是否此消彼长，线程过量的总 CPU/尾延迟影响|
|scale|同名义 CPU 秒/秒，改变 CPU 服务预算并反比改变 task/s|1ms 开发结论能否延伸至100ms业务尺度；粒度越小，投递/观察开销占比越大|
|estimator|同真实目标；oracle、固定均值、带独立误差的oracle/固定估计|观测预测精度与额外计量成本；本阶段无路由策略，不能声称估计器改善业务延迟|
|qps12|固定12批/s，K=20/100/400|贴近背景到达率的需求增长和过载边界；不隔离 burst 的独立效应|
|instrumentation|同固定循环数；none/queue/counters/full与10/100/1000ms采样|过程CPU、等待和时延的整轮配对差；先标定 iterations-per-us，不把 CPU 预算模式当作固定有用工作|

开发机仅人工划分 CPU，目标机才验证六组真实 CPU 与 NUMA。正式部署对照应同时保留：一组相同 offered workload（回答实际迁移配置会怎样），一组分别在各配置实测容量下的相同比例压力（回答相对饱和度相近时怎样）。二者回答不同问题，不能互相替换。`reference_load_fraction` 只表示相对于输入的那一个参考容量；改变亲和、线程或分布后不能继续把它当成本配置的真实 rho。

服务尺度族默认为基准预算的0.1、1、10倍，是调度敏感性检查；目标基准100ms包含10ms/100ms/1s，最后一档明确是比原始700–800ms长尾更强的边界负载。原始业务的100ms平均与700–800ms长尾仍应另以实际分布校准，而不将这个极值当作业务事实。

## 3. 推荐执行顺序与目标机命令

先保存拓扑和 cgroup 配额。最小有价值的顺序是：功能 → 一个配置容量标定 → 等需求 burst → 部署/线程 → 分布与估计 → 插桩。Poisson、服务尺度、qps12 为外推与边界补充；阶段 3 不能替代 DAG、内存或 RPC 实验。

以下命令中的 120/240/480 task/s 只是格式示例，必须按配额、CPU 实测能力和预估需求修改：

```bash
python3 stages/03-independent/make_cases.py --family calibration --deployment target \
  --cpus "$CLUSTER_CPU_LISTS" --numa "$CLUSTER_NUMA_IDS" \
  --rates 120,240,480 --threads 16 --cpu-us 100000 \
  --output /tmp/s3-capacity.json
python3 experiments/independent/scripts/run_matrix.py \
  --config /tmp/s3-capacity.json --binary build-independent/independent_load \
  --output results/runs/s3-capacity
```

再以**实际测出的**同分布参考容量生成压力点，或用 `--task-rate` 明确表示尚未归一化的任务率：

```bash
python3 stages/03-independent/make_cases.py --family burst --deployment target \
  --cpus "$CLUSTER_CPU_LISTS" --numa "$CLUSTER_NUMA_IDS" \
  --task-rate 240 --threads 16 --cpu-us 100000 \
  --output /tmp/s3-equal-demand.json
```

JSON 给出预计 offered 小时数（不含 drain）、每个 case 的分析窗口预期 arrival/task 数和参考压力。结合 `--only` 先选择一个压力点，再扩展；不要未经资源审查直接执行全部矩阵。旧 preset 仍可复现历史实验，新结论应使用这些明确分组或等价的审查后配置。

要严格比较固定宽度的相同任务集，选择 `task-rate × duration` 和 `task-rate × warmup` 能被各 K 整除的配置，并核对 `tasks.csv` 中任务数量和目标预算序列。duration 是半开区间 `[0,duration)`；不满足整除时，最后一个批次可能引入最多 K−1 个任务的边界差。Poisson 比较的是相同期望率，不应事后强行删除过多到达让两组看起来“完全等工作”。

## 4. 样本和可得结论的层次

- **功能样本**：开发 quick、短时 smoke 与错误路径测试。可验证真实 Folly、计数守恒、时间顺序、精确预算、CPU mask、输出字段；不证明性能优劣。
- **探索样本**：开发机8秒、2轮的生成器默认，仅验证实验问题有辨识度、分析没有混淆单位、现象可重复。明确标记 `development-exploratory`。
- **正式候选样本**：目标默认300秒、30秒预热、7轮。必须先审查稳态、发生器迟到、拒绝/取消、环境变化及有效样本；`target-candidate` 不等于已获统计结论。
- **业务小幅收益判断**：例如0.5%平均延迟差异，需要按 pilot 配对差的标准差和预先约定的误差范围设计独立运行数。7轮不是功效保证；环境漂移大于效应时报告无法分辨。

任务之间受到同一 burst、同一池和时间段共同影响。task 数多不能当作同样多的独立实验单位。主推断采用重复运行的配对差；bootstrap 时以完整运行或合理时间块为单位，不能逐 task 随机重采样获得虚假的窄区间。总体 p99 必须从原始 cohort 求得；不平均每秒 p99，也不把“合并全部任务的 p99”当作“典型一轮 p99”。

例如240 tasks/s、K=400时只有0.6 arrivals/s；300秒中去掉30秒仅约162个 burst，虽然有约64,800个 task，burst p99 的极端次序统计仍很稀少。生成器会告警 `few_arrival_warning`。若完整 burst p99 是主指标，需延长观测或明确降级为探索；阈值1000 arrivals、10000 tasks仅是审查提示，不保证任何置信度。

结论必须带有工作负载、CPU集合、线程数、插桩模式、窗口及重复范围。可回答“在这些条件下，相同平均工作量的局部突发增加多少等待，以及队列多大程度预测等待”；不能单凭相关系数声称 Linux runq 是根因、工作量估计必然优于队列，或对生产6个cluster预测固定收益。

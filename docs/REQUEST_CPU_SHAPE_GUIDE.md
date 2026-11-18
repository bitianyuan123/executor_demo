# 请求持续过程中的 CPU 形状：模型边界与下一步诊断

**当前结果证明了给定计算图和工作量下，算子级均衡能减少局部等待；尚未证明这个图的 CPU 服务时间形状与真实业务一致。** 用户指出的差别需要补证。4021 的 100 ms 逐 cluster 统计完成了原来的观测目标，但不足以回答“一个请求开始后，CPU 压力究竟持续 1 ms、10 ms 还是 80 ms”。本指南增加请求关联的细粒度诊断，不重开全部性能矩阵，也不将目标改回历史四池配置。

**4023状态：三份已有采集已恢复解析。** [最新审阅与图表](TARGET_V2_4023_REVIEW.md)确认3M home显式request6的>8CPU服务持续82ms，375k home为11ms，375k O为6ms；这些单请求证据不证明生产相似性。无需重采或重复恢复；下面命令保留作复现。80×1+4×22仍只是未校准假设，后续历史trace补证和复杂依赖/阶段竞争验收仍未完成。

## 1. 为什么现在的模型既有价值，又不能直接等同真实负载

4021 的合同是：均匀 12 QPS，每 83.333 ms 一个请求；六个硬件 cluster 各一个池，每池 16 worker。请求入口 RR 选择 home，因此同一个 home 每 500 ms 接到一个请求。每个请求执行：

```text
init，分配并首次触碰分支数据
  └─ 同时展开 80 条分支
       ├─ 分支 0：stage0 → stage1 ─┐
       ├─ 分支 1：stage0 → stage1  │
       └─ ……分支 79               ├─ join
                                  └─ 展开 4 条两段分支 → join → 请求完成
```

每个 stage0 完成后立即提交自己的 stage1，不等待其他分支的 stage0。首波是 80 项先 ready，随后后继陆续 ready，并非 160 个算子同时 ready。每请求共 168 个业务算子，加 init 和两个 join 为 171 个 callback。

每算子基准 300 万迭代、10 万随机读写访问，task 级 ±25% 扰动，hash 约 1/8 的算子再乘 4。每分支 64 KiB，两段访问相同分支数据；无同步阻塞和异步等待。这个配置把大部分 CPU 工作连续放在第一波：第一波占 160/168 个业务算子；seed 421 的全 240 请求中，第一波占实际迭代及访问数的 **95.265%**。工作计数比例不是精确 CPU 时间比例。

4021 的 home 运行中，算子平均 CPU 时间约 8.14 ms；单请求整轮平均 process CPU 约 **1.37 CPU·秒**。仅首波 stage0，正式 cohort 的角色均值就是 `80×8.25≈660 CPU·ms`。在固定 CPU 成本的解释性模型中：

| 供给条件 | 约 1.37 CPU·秒工作所需的最短供给时间 |
|---|---:|
| 只用一个 cluster 的 16 个逻辑 CPU | 约 85.6 ms |
| 同 NUMA 三个 cluster，共 48 个逻辑 CPU | 约 28.5 ms |
| 假设可同时得到 80 个逻辑 CPU | 约 17.1 ms |

公式是 `CPU 工作面积 / 可用 CPU 数`。依赖、分支尾部不齐、排队会继续增加时延；缓存、SMT 和竞争也会改变 CPU 成本。因此这不是把当前结果精确换算成历史无绑定配置的预测。

仅已测首波 stage0 的 660 CPU·ms，在 16 CPU 上就需要约 41.25 ms 供给，之后还有后继。所以 **3M 档确实是持续数十毫秒的强计算扇出场景**，不能称为已验证的“1 ms 内自愈尖峰”。同时，同 home 每 500 ms 来一个请求，`1.37/0.5≈2.74` 个 CPU 当量，除以 16 仅约 **17%**。长期均值低与单请求期间持续局部高压可以同时存在。

历史 trace 是未绑定 CPU 的池：单池平均 matched on-CPU 约 4.36–7.09 当量，最高 1 ms 桶约 72–79 当量。同样的工作如果能短时得到约 80 CPU，其服务脉冲可以比硬绑 16 CPU 短很多。因此两者不是已证明的逻辑矛盾，**但也没有完成服务形状的一致性验证**。

历史报告中的高服务段 p50 约 2 ms、p95 约 15–20 ms，定义是“超过各池自身平均 CPU 的两倍”的连续 1 ms 桶。它不是请求时长，不是算子时长，也不是统一的 `N>16` 过载段。旧 trace 没有 request、operator、RPC 标记，不能据它还原 80 条两段链。约 0.85–0.868 ms 的线程唤醒周期候选值也不能直接作为算子 CPU 预算。详细边界见[历史证据](../reports/final_review/data/trace_evidence.json)和[4010 解析器审计](../results/review/TRACE_4010_PARSER_AUDIT.md)。

已有结果仍支持同一固定工作下的机制判断：局部等待减少、请求更早完成、总 CPU 工作近似不变，以及 Q/O 对首波和后继的改投强度不同。它不支持“真实业务也改善 38%–39%”，也不能决定真实业务的最优阈值。

## 2. 先用已有结果定位时间尺度：不重新编译，不启动负载

第一步只分析目标机保存的 `summary.json`、`requests.csv`、`tasks.csv`。需要更新 Python 工具，但不需要重新编译 Folly 或 demo：

```bash
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114

python3 experiments/affinity/request_cpu_shape.py \
  --run results/local/cluster-cpu-regular12-3m-t16-seed421-01/01-home/demo \
  --request 6 --bin-ms 1 \
  --output results/local/request-shape-home-r6
```

输出目录必须不存在。工具打印 `REQUEST_CPU_SHAPE` 摘要，保存：

- `summary.json`：所有完成请求的汇总分布、所选请求的 CPU 面积、角色比例和累计服务时间界限。
- `requests_shape.csv`：每个完成请求的面积和时长。
- `request_6_profile.csv`：所选请求逐 1 ms 窗口、逐 pool 及 all 的 **CPU 服务上下界**。

这里的 1 ms 不是新采样。已知一个 callback 的墙钟区间长 W、累计线程 CPU 时间 C，以及它与窗口的交集长度 l，该窗口能容纳的 CPU 服务为：

```text
lower = max(0, C - (W - l))
upper = min(C, l)
```

工具把这些界按请求和池相加，保留不确定性；**不把 CPU 均匀摊到 callback 墙钟区间，也不把上下界叫作真实 CPU 曲线**。每个窗口的极值不一定能同时实现。CPU 比墙钟长等非法记录会拒绝，不通过截断修正。

同一请求的 O 结果可离线对照：

```bash
python3 experiments/affinity/request_cpu_shape.py \
  --run results/local/cluster-cpu-regular12-3m-t16-seed421-01/03-outstanding/demo \
  --request 6 --bin-ms 1 \
  --output results/local/request-shape-outstanding-r6
```

request 6 是显式选择的诊断样本，约在该次运行计划起点后 500 ms 到达，并非正式性能 cohort 的代表样本；不要由单请求重算生产 p99。同 seed、同 request 的固定工作可以核对，但两次独立运行的时间轴不能当成同一现场时刻。

两种工具均以该请求实际 attempt/submitted 时间作为 `t=0`，不是计划到达时间；另列计划到实际 attempt 的延迟。最后一个 callback 的观测结束可能略晚于逻辑请求完成，工具保留并单列这段尾部。

## 3. 有界采集方案：实际调度区间与请求关联（已有记录不重采）

上下界可以判断现有记录是否已经约束了服务形状；要观察实际在哪些细窗口得到 CPU 服务，需要调度区间。新增采集把 callback 的 `worker_tid`、开始/结束时间与 `sched_switch` 区间相交，而不是轮询 CPU 百分比。

demo 的新增字段仅用于诊断：trace=1 时，每个 worker 首次记录通过线程局部缓存取得一次 TID，之后复用；导出任务 TID 和时钟对齐括号。不改变工作、选池和负载均衡策略。需要重编 demo，不需要重编 Folly：

```bash
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114

cmake -S experiments/affinity -B build-affinity \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/home/bty/rank_demo/folly/_install \
  -DBUILD_TESTING=ON
cmake --build build-affinity --target affinity_demo --parallel 8
```

采集合同固定为所选六 cluster、每池 16 worker、regular 12 QPS、24 请求、seed 421、相同 80×2+4×2 图和数据依赖、trace=1、无等待。CPU 集合仍为 `0-47,192-239`，没有恢复四池方案。选择 request 6 避开进程启动和退出边缘；24 请求用于形状诊断，不用于正式 p99 验收。

### 3.1 3M 档、固定 home

先打印完整计划，不运行负载，也不创建目录：

```bash
python3 scripts/run_request_cpu_capture.py \
  --output results/local/request-cpu-3m-home \
  --request-id 6
```

实际执行同一单轮：

```bash
python3 scripts/run_request_cpu_capture.py --run \
  --output results/local/request-cpu-3m-home \
  --request-id 6
```

默认策略是 home，基准 300 万迭代、10 万访问。脚本只执行一轮，任何阶段失败即停止，保留失败文件，不自动重试或继续其他策略。

### 3.2 375k 档、固定 home，到达仍为 12 QPS

已有 4015/4016/4018 测过 37.5 万迭代、1.25 万访问档：业务算子 CPU 均值约 1.05 ms，每请求约 177–181 CPU·ms。它还没有被验证具有怎样的 1 ms 服务形状。这里复用这个工作尺度，只降低算子工作，不改变 DAG，也不把旧线程唤醒周期硬套成算子时间。

```bash
python3 scripts/run_request_cpu_capture.py \
  --output results/local/request-cpu-375k-home \
  --request-id 6 --iterations 375000 --accesses 12500 --qps 12

python3 scripts/run_request_cpu_capture.py --run \
  --output results/local/request-cpu-375k-home \
  --request-id 6 --iterations 375000 --accesses 12500 --qps 12
```

这里保持 12 QPS，**不是既有 96 QPS 实验，也不是等平均负载对照**。它回答同图、同到达和同资源下，工作面积缩小后请求内部的脉冲如何变化。按约 177 CPU·ms 估算，16 CPU 的总供给下限约 11 ms；这只是采集前的尺度参照，实际形状仍以新结果为准。

先执行上述两个 home 诊断。如果确认需要看相同尺度的算子外借形状，再单独执行一轮 O，例如：

```bash
python3 scripts/run_request_cpu_capture.py --run \
  --output results/local/request-cpu-375k-outstanding \
  --request-id 6 --iterations 375000 --accesses 12500 --qps 12 \
  --policy outstanding
```

这不是默认追加的矩阵。不以这几次带调度记录的诊断运行取代此前净性能比较；采集开销尚需独立对照，不能因工具只记录事件就宣称零扰动。

## 4. 哪些是真实服务，哪些是占用或积压

采集使用 `CLOCK_MONOTONIC`，保存每个 `sched_switch`，再离线与 TID 和 callback 边界相交。1/5/10 ms 都是同一事件序列的积分窗口，**不是以这些间隔反复唤醒采集线程**。命令选项参考 Linux 官方 [perf record](https://raw.githubusercontent.com/torvalds/linux/master/tools/perf/Documentation/perf-record.txt) 和 [perf script](https://raw.githubusercontent.com/torvalds/linux/master/tools/perf/Documentation/perf-script.txt) 文档。

| 输出 | 获取位置与含义 | 不能解释成什么 |
|---|---|---|
| `request_scheduled_cpu_equivalent` | 本请求 callback 区间与对应 TID 已闭合的调度驻留区间相交，面积除窗口时长 | 任务 CPU 需求、可运行线程数 |
| `request_cluster_util_percent` | 上一面积再除该行 cluster 的 16 CPU；all 行分母为所选 96 CPU | 整个 cluster 的所有活动 |
| `cluster_nonidle_residence_percent` | 同一时间窗、同一 cluster 所有非 idle 任务的调度驻留面积，包括其他请求和采集等活动 | 只属于本请求或 executor 的 CPU |
| `request_submitted_not_started_mean` | 本请求各 callback 从 submitted 到 start 的重叠面积/窗长 | Folly 原生 Q 或内核 runq 长度 |
| `request_callback_wall_occupancy_mean` | 本请求各 callback 从 start 到 finish 的重叠面积/窗长 | on-CPU；其间可能被抢占或阻塞 |
| `measured_callback_cpu_ms` | callback 内已测线程 CPU 时间之和 | executor 所有框架开销、整个进程 CPU |

“调度驻留”指线程已被切入、尚未被切出的时间。它包括打断非 idle 线程的 IRQ 等时间，而 idle 期间的 IRQ 没有作为非 idle 驻留计入；因此这不是扣除了中断的精确硬件 CPU 利用率。请求列还排除 callback 边界以外的框架工作和异步 helper。驻留面积与 callback CPU 面积的差额不能直接叫作调度等待。

分析器要求纳秒时间戳、CPU 切换链连续、TID 不在不同 CPU 上重叠、请求使用的 cluster 各 CPU 有覆盖其首尾的事件边界；正 CPU 记录却找不到对应驻留也会拒绝。缺事件、边界或时钟无法对齐时，不能用零或插值填充。未使用 cluster 的总活动窗口如果无法完整覆盖，明确输出 null，不解释成空闲。

为避免某个 CPU 长时间 idle 而没有可闭合的首尾事件，采集器在 demo 启动前和完全退出后，各让一个独立辅助进程依次访问所选 CPU 并短暂睡眠，产生边界切换。辅助进程恢复 CPU 并集后启动 demo；这些边界动作不与请求执行重叠，记录在 `boundaries.json`。它们会唤醒 CPU，不能宣称对频率或启动状态没有影响；选择内部请求并保留完整采集合同。分析器仍检查真实切换链，不把哨兵当作填补丢失事件的依据。

工具不修改全局 tracefs 开关、事件过滤或 sysctl。perf 未安装、权限不足、版本不支持选项、事件丢失或 CPU 资源不符时停止并保留错误，不自动调整系统设置。这个步骤只在独立测试环境执行，不要求访问真实生产应用，也不要求重采旧生产 trace。

## 5. 输出、终端回传与离线换窗口

一次成功采集保留：

| 文件 | 用途 |
|---|---|
| `capture.json` | 采集参数、时钟、资源、命令与哈希；采集成功后冻结，避免离线分析所记录的哈希变化 |
| `boundaries.json` | demo 前后逐 CPU 边界动作及时间；这些动作不计入请求的 CPU 窗口 |
| `execution.json` | 后续 profile 执行状态和错误；不回写已冻结的采集元数据 |
| `perf.data`、`sched.txt` | 原始 perf 记录与纳秒级调度事件文本 |
| `demo/` | 本轮工作合同、请求、callback 和 TID 记录 |
| `profile/profile.json` | 所选请求的服务面积、累计服务时间和连续高服务段等摘要 |
| `profile/request_cpu_windows.csv` | 逐窗口、逐 cluster 及 all 的完整序列 |
| `profile.stdout.txt` | 可直接复制的摘要和序列；另有各阶段 stderr 文件 |

只能够从终端回传时，分别打印两轮摘要及序列，无需上传大 trace：

```bash
cat results/local/request-cpu-3m-home/capture.json
cat results/local/request-cpu-3m-home/execution.json
cat results/local/request-cpu-3m-home/boundaries.json
cat results/local/request-cpu-3m-home/profile.stdout.txt

cat results/local/request-cpu-375k-home/capture.json
cat results/local/request-cpu-375k-home/execution.json
cat results/local/request-cpu-375k-home/boundaries.json
cat results/local/request-cpu-375k-home/profile.stdout.txt
```

失败时，打印该目录的 `capture.json`、已存在的 `execution.json` 以及报错指出的 stderr。缺失后续文件表示阶段没有完成，不补写成功标记。

同一次成功采集可重新选 request 12 或 18，或改成 5/10 ms 窗口，**不用重新压测**。例如：

```bash
python3 experiments/affinity/request_sched_profile.py \
  --run results/local/request-cpu-3m-home/demo \
  --trace results/local/request-cpu-3m-home/sched.txt \
  --capture results/local/request-cpu-3m-home/capture.json \
  --request 12 --bin-ms 5 \
  --output results/local/request-cpu-3m-home/profile-r12-5ms
```

request 6、12、18 在六池 RR 下有相同 home，可看同一池不同请求的形状；它们仍是显式选择的少量样本。输出目录始终使用新名称。更换窗口只能改变积分粒度，不能补回未采集的调度事件。

## 6. 用哪些结果判断模型，而不是只看一条 CPU 百分比

先核对工作合同、完整性和总面积，再比较形状：

1. **CPU 面积与阶段比例。** 读原 callback CPU 之和、调度驻留面积、首波 stage0 和首波两段的工作占比；确认第一波是否确实占了绝大多数服务。两种 CPU 计量边界不同，分别列出。
2. **累计服务 t50/t90/t99。** 何时累计交付了该请求 50%/90%/99% 的服务？旧 CSV 工具给界限；新调度工具给所在积分窗口的结束时间，存在一个窗口宽的定位精度。这不是“请求完成时延 p99”。
3. **高服务段长度。** 看超过 8/16/32/48 CPU 当量的连续桶数、总时长和最长段。这里的阈值用于描述形状，不是自动迁移门槛。固定 home 最多只能得到 16 CPU，因此 `>16` 为零完全不能证明没有过载。
4. **服务与积压同时出现的位置。** 一个 cluster 已给出接近 16 CPU 服务，仍有许多本请求 callback 已提交未开始，支持“供给受限且还有ready工作”；单看 CPU 曲线不能推导剩余需求。submitted→start也混合了用户态交接与内核等待，不能分解成两条精确队列。
5. **脉冲随工作尺度如何变化。** 3M 和 375k 仍使用 12 QPS；若前者形成几十毫秒平台、后者只有较短脉冲，应分别讨论这两类场景的均衡收益与亲和成本，而不是把一个策略在3M档的收益外推到全部业务。

4022提出了负载代表性问题，但此前直接指定新图和三档矩阵缺少依据，执行建议已撤回。先补历史池级背景/再次突发约束，再制定图结构、阶段工作和跨请求竞争的验收条件。请求生命周期重叠不等于同资源竞争；需按request阶段、实际池及ready/执行区间联合分析。CPU曲线也不能恢复缺失的多父依赖。后续策略实验须在负载合同得到明确说明后单独开展。

即使新的 demo 采集完整，它也只验证了 **demo 的真实服务形状**。旧生产 trace 缺请求标记，仍不能直接证明逐请求一致。当前可交付的是一组有量化边界的模型家族、观测工具和策略敏感性结果；未知的真实阶段工作比例、异步等待、分支成本与数据复用，需要在当地具备条件时按既有分阶段提示词核实，而不是假定已有答案。

开发验证见[记录](../results/review/request_cpu_shape_validation.json)：demo 编译通过，相关六项 CTest 通过，新增分析与采集共45个单元测试通过；两个人工 CPU 分区上的实际 perf 采集、边界、请求归因及离线换窗口已完成。它们只验证工具功能，没有新增 ARM 目标机性能数据。

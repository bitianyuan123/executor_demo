# Folly线程池：五阶段分析与独立实验

**阶段总报告已按设计和问题重新组织：** [离线 HTML](reports/final_review/executor_affinity_report.html) · [PDF](reports/final_review/executor_affinity_report.pdf) · [可审阅正文](reports/final_review/REPORT.md)。从一条请求的量化到达、80路扇出与后继执行开始，逐点标出Q/I/O/R的读写位置和算子选池过程；用15张图按六种明确负载比较时延、CPU/请求和改投率，再解释旧trace如何指导每cluster一池；不要求先读逐轮日志。数据、来源和重建说明见[报告目录](reports/final_review/README.md)。新增[4021同轮CPU审阅](docs/TARGET_V2_4021_REVIEW.md)已补齐均匀12QPS、首波80算子、每池16线程的六轮CPU窗口与时延/改投对照：平均CPU约17%不变，局部热点和请求等待显著下降。[CPU采集指南](reports/final_review/CPU_CAPTURE_GUIDE.md)保留复现命令，无需为同目的重跑。

**4022修订：观测不能替代负载覆盖。** 旧CSV确认首波占约95.7%的算子CPU。新增[4022分析与执行指南](docs/TARGET_V2_4022_REVIEW.md)及`shape-coverage`，在相同168算子、84分支下比较80×2+4×2与80×1+4×22，并分步覆盖每算子375k、46875、3M迭代档。已兼容目标机perf紧凑格式，可离线恢复已有采集，不需重跑。[请求内CPU形态工具](docs/REQUEST_CPU_SHAPE_GUIDE.md)作为配套观测；均衡机制已验证不代表生产收益或负载覆盖已验收。

按业务理解 → Folly机制 → 独立任务负载 → 复杂DAG → 能力归属逐步验证。**一个仓库管理五个阶段，但简单任务实验、DAG实验和成本探针是独立CMake工程**，无需一次运行所有实验。阶段2～4仅依赖原版Folly；阶段5才需要实验扩展。

| 阶段 | 独立交付 | 要回答的问题 | 验证入口 |
|---|---|---|---|
| 1 | [真实业务分析报告](stages/01-business/README.md) | 已知负载、局部突发、容量与未知项 | 核对业务口径和目标机拓扑 |
| 2 | [Folly分析报告](stages/02-folly/README.md) | 任务队列、唤醒、内核runq、负载获取的成本与覆盖范围 | [读取/维护成本探针](experiments/metrics/README.md) |
| 3 | [独立任务实验设计与指南](stages/03-independent/README.md) | 排除DAG与迁移后，负载怎样影响任务平均/p99 | `experiments/independent` |
| 4 | [复杂DAG设计与指南](stages/04-dag/README.md) | 初始大扇出、依赖、多次fork、RPC和亲和策略 | `experiments/dag`为v1机制/RPC；`experiments/affinity`为v2总池/固定工作局部性主线 |
| 5 | [业务侧与Folly扩展的判断、工程和对照](stages/05-placement/README.md) | 同一能力该放在哪里，集成是否等价 | 业务版与可选库版分别构建 |

先看[本次工程复核与阶段验收](stages/README.md)，每一步都定义了输入、产物、可支持的结论和不能跨越的推断。旧版DAG实验数据仍保留在[历史结果](results/validation.md)，不会将它冒充新阶段3的独立任务证据。

阶段4现新增独立的[两层调度审查与设计](stages/04-dag/TWO_LEVEL_DESIGN.md)和[透明总池/固定工作量局部性实验v2](experiments/affinity/README.md)：业务不传池编号，支持请求染色、自动后继上下文、登记式RPC续接、fork/all均衡、parent/home策略及真正分支读写依赖。旧DAG和已测基线保留；v2需显式运行 `./scripts/build_stage.sh affinity`，不会改变默认构建和历史实验语义。

**业务改造主线：每个已核实的硬件cluster一个Folly子池，由总池完成请求亲和与算子借出。** 当前选定六cluster子集即六池；池数随部署范围变化。16 QPS、4×128线程、无CPU绑定和单池active 20/80是历史trace的临时采集条件，不决定目标配置。上轮实验入口偏移及修正见[方向复核](docs/CLUSTER_DIRECTION_REVIEW.md)。

当前总览见[总目标、实现难点与验收覆盖](docs/GOALS_AND_COVERAGE.md)：集中区分已实现、已验证和待目标机证据，并说明阶段3条件时延如何接到阶段4策略干预。v2性能摘要用`report.py`，负载分桶、首波扇出与积压趋势用`diagnose.py`，实际burst跨度、请求均值分解与后继角色用`flow_report.py`；这些工具不输出生产最优策略或统一的负载→request p99公式。

[截至4022的结论与候选初值](docs/CURRENT_CONCLUSIONS_AND_STARTING_CONFIG.md)集中说明当前机制结论、负载与观测的适用范围、两层算法，以及复现参数与当地起点的区别；下一步并行恢复已有CPU采集和完成新增图形状覆盖，不依赖访问生产环境。

- [大调度轨迹的本地分析工作流](docs/TRACE_WORKFLOW.md)、[流式脚本指标解释](docs/TRACE_ANALYZER.md)、[GLM/DeepSeek分析与审计提示词](docs/TRACE_ANALYSIS_PROMPTS.md)：原始trace留在测试机，只回传终端摘要。
- [负载观测与调度复核](docs/LOAD_OBSERVATION_REVIEW.md)：生产可复用observer适配、后台schedstat、可注册Provider/Scorer及其成本与边界。
- [每cluster一池的实验矩阵](docs/REAL_WORKLOAD_MATRIX.md)：主线验证单request扇出、线程过配、NUMA借出与数据亲和；四池仅作可选历史重放。
- [目标机4001日志审阅](docs/TARGET_V2_4001_REVIEW.md)：v2单request借出初筛、线程数结果与证据边界；当时的补证和校准已由4002完成。
- [目标机4002补证与校准](docs/TARGET_V2_4002_REVIEW.md)：配对审计完成、CPU策略全部回退、首波尺度冻结；当时布置的竞争对照已由4003完成。
- [目标机4003竞争结果](docs/TARGET_V2_4003_REVIEW.md)：regular的O均值收益约43%，burst8缩小至约4%；当时布置的入口补证和分支校准已完成。
- [目标机4004补证与连续分支校准](docs/TARGET_V2_4004_REVIEW.md)：确认burst批内投递拉宽，冻结工作量；当时布置的24轮已由4006完成。
- [4005历史真实trace审阅](docs/TRACE_4005_REVIEW.md)：采用最终精确计数，纠正HLL混用；区分unique、waking与onCPU相关性，不据历史四池决定cluster阈值。
- [目标机4006后继策略结果](docs/TARGET_V2_4006_REVIEW.md)：24轮完成；当时安排的CSV诊断和9轮task级分支异质性已由4008完成。
- [4007/4008结果](docs/TARGET_V2_4008_REVIEW.md)：task异质性下借出均值改善约37%；其后安排的联合诊断和9轮burst8已由4009完成。
- [4009结果](docs/TARGET_V2_4009_REVIEW.md)：burst下借出收益缩至约4%～6%，all相对fork出现约1%的小幅收益，说明负载形态影响判断。
- [4010/4011结论](docs/TARGET_V2_4010_4011_REVIEW.md)：有界旧trace审计及18轮观测对照已完成；关闭详细trace后all均值仍略优，但p99不稳定，不能直接确定生产策略。
- [4012信号/线程结果](docs/TARGET_V2_4012_REVIEW.md)：24轮信号/线程对照完成；三种借出策略从128降至16线程，均值改善2.93%～4.03%，Q/active/O未分出稳定赢家，不能直接指定生产线程数。
- [4013机制结论](docs/TARGET_V2_4013_REVIEW.md)：六轮诊断完成；更多worker使开始前等待缩短、callback墙钟膨胀；Q盲区存在但在本批提交样本中少见，不能选定生产信号。
- [4014 regular结论](docs/TARGET_V2_4014_REVIEW.md)：9轮16线程regular守护已完成；Q/O相对home均值约下降38%，CPU/请求增加不足1%，两信号差不足1.2%，仍不能选赢家或完成p99验收。
- [4015短工作校准](docs/TARGET_V2_4015_REVIEW.md)：短工作S四轮完成；trace0的16/128线程均值几乎相同，详细trace的CPU/请求增加1.83%/2.28%，四轮peak-inflight均1。
- [4016密度结论](docs/TARGET_V2_4016_REVIEW.md)：SD六轮全部1920成功、零失败，正式cohort各1536、peak-inflight均3；同线程Q/O相对home请求均值下降33.36%～34.40%，CPU/请求增加6.01%～7.03%。Q/O排名随线程档变化，尚无统一赢家。
- [4017到达核查](docs/TARGET_V2_4017_REVIEW.md)：home高lag集中于序号相位但原因未知，Q/O响应更短同时重叠更少，不作为因果证明。CPU族结果冻结。
- [4018异步结果](docs/TARGET_V2_4018_REVIEW.md)：八轮完成；A1下O相对home均值降低19.546%、p99降低20.752%，CPU/请求增加3.721%。home外部等待增加而恢复排队变短，不能用请求在途替代ready负载。
- [4020异步机制结论](docs/TARGET_V2_4020_REVIEW.md)：四轮已完成；O的业务与恢复等待下降，trace0请求均值/p99约降低19.87%/21.35%，CPU/请求增加约3.74%。恢复callback直接借出仅0.63%，不能把收益归为大量恢复迁移。单次证据不构成正式尾部验收；[四轮指南](docs/TARGET_V2_ASYNC_MECHANISM_GUIDE.md)已归档，实现与计量合同见[异步设计](docs/ASYNC_CONTINUATION_WORKLOAD_DESIGN.md)。
- [真实应用agent分阶段手册](docs/PRODUCTION_AGENT_PLAYBOOK.md)：只读探索、设计、实现、验收和有限迭代；候选初值与梯度由当地事实约束，不要求跨环境联网。另有[纯prompt交接版](docs/PRODUCTION_AGENT_PROMPTS_ONLY.md)，不需要给接收方传任何文件。
- [持续压力门控候选审阅](docs/PERSISTENT_PRESSURE_REVIEW.md)：复核外部0.85ms/3ms说法，明确观测与状态机缺口；尚未修改运行策略。
- [trace补证与后续目标实验](docs/TRACE_TO_TARGET_PLAN.md)：只读已有JSON分析同期压力与连续高桶；明确task异质性、线程/信号、异步RPC与正式尾部验证的顺序和实现缺口。
- [观测实现验证记录](results/review/TRACE_LOAD_VALIDATION.md)：本地功能和读成本，不冒充ARM真实负载结论。

再次复核后的入口是[实验设计与定量验证协议](stages/EXPERIMENT_REVIEW.md)：区分等需求burst、同均值长尾、容量压力和真实业务参数，明确每项对照能支持的结论。[配对分析指南](stages/COMPARISON_GUIDE.md)提供工作量合同、planned cohort、整轮差值区间和小样本p99门槛；[本轮修订与补测](results/review/README.md)保留实际执行事实。旧结果继续作为历史证据，不替换其数据。

## 获取与依赖

- Demo分支：`experiment/affinity-load-20261114`。
- 原版Folly基线：tag `v2026.10.26.00`，commit `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。
- 可选Folly扩展分支：`experiment/grouped-executor-20261114`，固定提交见[锁定文件](dependencies.lock.json)。
- Linux、C++20、CMake≥3.20、Ninja、Python3；验证环境为Debian13/GCC14。

下列完整依赖命令适用于Debian/Ubuntu。DAG及阶段5的C++测试还需要GoogleTest开发包；RPM系发行版通常使用`gtest-devel`，具体以当前发行版的软件仓库为准。Folly自身关闭测试后编译成功，不代表Demo的测试依赖已经齐备。测试机遇到GTest缺失、拓扑输出目录或`/.build.command`提示时，按[构建排障指南](docs/BUILD_TROUBLESHOOTING.md)继续。

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake ninja-build git python3 \
  libboost-context-dev libboost-filesystem-dev libboost-program-options-dev \
  libboost-regex-dev libboost-system-dev libboost-thread-dev \
  libdouble-conversion-dev libevent-dev libfmt-dev libgflags-dev \
  libgoogle-glog-dev libssl-dev libunwind-dev liblz4-dev libzstd-dev \
  libsnappy-dev zlib1g-dev libiberty-dev libgtest-dev libfast-float-dev

# 前四阶段可以直接使用原版tag
 git clone --branch v2026.10.26.00 git@github.com:bitianyuan123/folly.git
 git clone --branch experiment/affinity-load-20261114 git@github.com:bitianyuan123/executor_demo.git
 cd executor_demo
# 首次完整编译、安装Folly并编译各独立工程；4GB建议-j1
BUILD_JOBS=1 ./scripts/build.sh
```

已有匹配版本的Folly安装时，无需重新编译Folly。`FOLLY_PREFIX`默认为相邻 `../folly/_install`，可显式指定；只构建所需阶段：

```bash
FOLLY_PREFIX=/path/to/folly/install BUILD_JOBS=1 ./scripts/build_stage.sh metrics
FOLLY_PREFIX=/path/to/folly/install BUILD_JOBS=1 ./scripts/build_stage.sh independent
FOLLY_PREFIX=/path/to/folly/install BUILD_JOBS=1 ./scripts/build_stage.sh dag
```

也可以复用已安装Folly，继续完成根工程构建和测试：

```bash
SKIP_FOLLY_BUILD=1 BUILD_JOBS=1 ./scripts/build.sh
```

两个构建脚本默认`BUILD_TESTING=ON`，缺少GTest时不会静默跳过测试。可以用`DEMO_GTEST_SOURCE_DIR=/absolute/path/to/googletest`指定已有GoogleTest源码；不会自动联网下载依赖。显式设置`BUILD_TESTING=OFF`只编译可执行文件并跳过测试，不能将其记录为测试通过。完整用法见[构建排障指南](docs/BUILD_TROUBLESHOOTING.md)。

对应产物位于 `build-metrics/metric_cost_bench`、`build-independent/independent_load`、`build-dag/executor_demo`。DAG可执行文件保留旧名以兼容历史命令，metadata明确记录business后端。各目录也可以直接 `cmake -S experiments/<目录> -B <构建目录> -DCMAKE_PREFIX_PATH=...`，不依赖根CMake。

阶段5再将Folly源码切到实验分支，按其独立指南构建 `build-placement/executor_demo` 与 `build-placement/dag_folly`。源码头文件、生成配置和链接库必须来自兼容基线，不可随意混用系统Folly版本。

## 在测试环境分步执行

当前已完成阶段3、截至4017的cluster机制与到达核查、异步功能验收、4018八轮对照、[4020四轮机制补证](docs/TARGET_V2_4020_REVIEW.md)及[4021六轮CPU对照](docs/TARGET_V2_4021_REVIEW.md)。冻结合成模型、计数评分与阈值，不自动追加性能批次或生产trace；新模式使用affinity-v3-async，旧v2结果继续支持。下一步优先按[分阶段手册](docs/PRODUCTION_AGENT_PLAYBOOK.md)或[纯prompt](docs/PRODUCTION_AGENT_PROMPTS_ONLY.md)交给当地agent只读核实、设计，再在已有授权和预定预算内实施与验收。生产访问不足可止于离线交接；生产代表性和正式多轮性能验收仍未完成。4021本身无需重跑；新增请求形态分析可先读旧CSV，后续事件归因须重编affinity_demo以导出worker TID，调度逻辑不变。

先保存 `python3 scripts/topology.py --output results/local/topology.json`，脚本自动创建输出父目录；核对allowed CPU、真实NUMA/cluster、SMT和quota。测试机6池三种绑定使用同一套真实CPU列表生成，不能猜测连续编号就是同cluster。开发机只做低成本机制验证，不模拟“真实六cluster”。

阶段3先固定服务分布扫描负载和线程数，再比较三种亲和；阶段4先固定DAG与home，再改变跨池策略和fork位置。每次只扩展一个主要问题，串行运行、同seed配对、交错重复，保存所有拒绝/取消/未完成任务和发生器落后。每秒p99只作波动观察，正式比较以完整样本和独立重复为主。

新阶段的实际验证见[分阶段验证记录](stages/VALIDATION.md)。目标ARM环境未在此运行，NUMA局部性收益、真实业务参数与最终策略仍按各阶段的人工复核项验证。

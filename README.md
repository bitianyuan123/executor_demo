# Folly线程池：五阶段分析与独立实验

按业务理解 → Folly机制 → 独立任务负载 → 复杂DAG → 能力归属逐步验证。**一个仓库管理五个阶段，但简单任务实验、DAG实验和成本探针是独立CMake工程**，无需一次运行所有实验。阶段2～4仅依赖原版Folly；阶段5才需要实验扩展。

| 阶段 | 独立交付 | 要回答的问题 | 验证入口 |
|---|---|---|---|
| 1 | [真实业务分析报告](stages/01-business/README.md) | 已知负载、局部突发、容量与未知项 | 核对业务口径和目标机拓扑 |
| 2 | [Folly分析报告](stages/02-folly/README.md) | 任务队列、唤醒、内核runq、负载获取的成本与覆盖范围 | [读取/维护成本探针](experiments/metrics/README.md) |
| 3 | [独立任务实验设计与指南](stages/03-independent/README.md) | 排除DAG与迁移后，负载怎样影响任务平均/p99 | `experiments/independent` |
| 4 | [复杂DAG设计与指南](stages/04-dag/README.md) | 初始大扇出、依赖、多次fork、RPC和亲和策略 | `experiments/dag`，默认业务侧路由 |
| 5 | [业务侧与Folly扩展的判断、工程和对照](stages/05-placement/README.md) | 同一能力该放在哪里，集成是否等价 | 业务版与可选库版分别构建 |

先看[本次工程复核与阶段验收](stages/README.md)，每一步都定义了输入、产物、可支持的结论和不能跨越的推断。旧版DAG实验数据仍保留在[历史结果](results/validation.md)，不会将它冒充新阶段3的独立任务证据。

再次复核后的入口是[实验设计与定量验证协议](stages/EXPERIMENT_REVIEW.md)：区分等需求burst、同均值长尾、容量压力和真实业务参数，明确每项对照能支持的结论。[配对分析指南](stages/COMPARISON_GUIDE.md)提供工作量合同、planned cohort、整轮差值区间和小样本p99门槛；[本轮修订与补测](results/review/README.md)保留实际执行事实。旧结果继续作为历史证据，不替换其数据。

## 获取与依赖

- Demo分支：`experiment/affinity-load-20261114`。
- 原版Folly基线：tag `v2026.10.26.00`，commit `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。
- 可选Folly扩展分支：`experiment/grouped-executor-20261114`，固定提交见[锁定文件](dependencies.lock.json)。
- Linux、C++20、CMake≥3.20、Ninja、Python3；验证环境为Debian13/GCC14。

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

对应产物位于 `build-metrics/metric_cost_bench`、`build-independent/independent_load`、`build-dag/executor_demo`。DAG可执行文件保留旧名以兼容历史命令，metadata明确记录business后端。各目录也可以直接 `cmake -S experiments/<目录> -B <构建目录> -DCMAKE_PREFIX_PATH=...`，不依赖根CMake。

阶段5再将Folly源码切到实验分支，按其独立指南构建 `build-placement/executor_demo` 与 `build-placement/dag_folly`。源码头文件、生成配置和链接库必须来自兼容基线，不可随意混用系统Folly版本。

## 在测试环境分步执行

先保存 `python3 scripts/topology.py --output results/local/topology.json`，核对allowed CPU、真实NUMA/cluster、SMT和quota。测试机6池三种绑定使用同一套真实CPU列表生成，不能猜测连续编号就是同cluster。开发机只做低成本机制验证，不模拟“真实六cluster”。

阶段3先固定服务分布扫描负载和线程数，再比较三种亲和；阶段4先固定DAG与home，再改变跨池策略和fork位置。每次只扩展一个主要问题，串行运行、同seed配对、交错重复，保存所有拒绝/取消/未完成任务和发生器落后。每秒p99只作波动观察，正式比较以完整样本和独立重复为主。

新阶段的实际验证见[分阶段验证记录](stages/VALIDATION.md)。目标ARM环境未在此运行，NUMA局部性收益、真实业务参数与最终策略仍按各阶段的人工复核项验证。

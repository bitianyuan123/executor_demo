# Executor 亲和、负载与图计算时延实验

本工程使用**真实 Folly CPUThreadPoolExecutor**，提供可重复的开放环流量、扇出链与汇合 DAG、异步 RPC 定时回调、多池路由和逐任务观测。本地 x86 结果用于验证机制，不代表 ARM NUMA 的性能收益。

- Demo 分支：`experiment/affinity-load-20261114`
- Folly 分支：`experiment/grouped-executor-20261114`
- Folly 基线：tag `v2026.10.26.00`，commit `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`
- 本次扩展固定提交：`150b684e0679de6962755be2d1411e41d4bd6b4c`，见 [依赖锁定文件](dependencies.lock.json)。
- 扩展：`folly/executors/experimental/AffinityExecutor.h`；组合现有 CPU 池，不修改默认 CPUThreadPoolExecutor 行为。

## 编译与测试

Linux、C++20、CMake >= 3.20、Ninja、Python 3；验证环境为 Debian 13 / GCC 14。4GB 环境建议 `BUILD_JOBS=1`。首次完整 Folly 编译需要一定时间和磁盘空间。

```bash
# Debian/Ubuntu；其他发行版使用等价开发包。
sudo apt-get update
sudo apt-get install -y build-essential cmake ninja-build git python3 \
  libboost-context-dev libboost-filesystem-dev libboost-program-options-dev \
  libboost-regex-dev libboost-system-dev libboost-thread-dev \
  libdouble-conversion-dev libevent-dev libfmt-dev libgflags-dev \
  libgoogle-glog-dev libssl-dev libunwind-dev liblz4-dev libzstd-dev \
  libsnappy-dev zlib1g-dev libiberty-dev libgtest-dev libfast-float-dev

git clone --branch experiment/grouped-executor-20261114 git@github.com:bitianyuan123/folly.git
git clone --branch experiment/affinity-load-20261114 git@github.com:bitianyuan123/executor_demo.git
cd executor_demo
BUILD_JOBS=1 ./scripts/build.sh
./build/executor_demo --help
ctest --test-dir build --output-on-failure
```

目录不相邻时设置 `FOLLY_SOURCE_DIR=/path/to/folly`；安装目录可设置 `FOLLY_PREFIX`。源码头文件与链接的 Folly 必须来自相同基线，不能任意混用系统版本。

## 快速试跑

```bash
python3 scripts/topology.py --output results/my-topology.json

# 六个逻辑线程组共享本机 CPU，没有假造六个物理 cluster。
./build/executor_demo --output results/local/smoke \
  --pools 6 --threads 4 --affinity none --policy home \
  --qps 12 --duration 5 --fanout-min 20 --fanout-max 40 \
  --chain-length 2 --cpu-us 1000 --seed 42
python3 scripts/analyze.py results/local/smoke --output results/local/smoke-analysis

# 两个 CPU 分区的机制对照；串行运行，每轮随机案例顺序。
python3 scripts/run_matrix.py --output results/local/matrix --repeats 3 \
  --only split-t2-home,split-t2-borrow,split-t16-home,split-t16-borrow
```

`--cpu-us` 是每段算子的线程 CPU 时间，不是 sleep 或墙钟忙等。每请求总 CPU 需求约为 `fanout × chain-length × cpu-us`，还需加抖动、长尾和汇合节点。在本机直接使用业务的 100ms 和大扇出，可能很快过载。

## 阅读顺序

| 文档 | 内容 |
|---|---|
| [整体模型](docs/model.md) | 业务工作量、容量、扇出、关键路径与内核调度 |
| [Folly 唤醒源码](docs/folly-wakeup.md) | 入队、信号量、唤醒、取任务及统计接口 |
| [设计](docs/design.md) | 指标、策略、低扰动计量及业务侧 / Folly 层职责 |
| [实验指南](docs/experiments.md) | ARM 部署、负载扫点、RPC、迁移、perf 与统计方法 |
| [生产复核清单](docs/production-review.md) | 当前未知项、风险与业务环境待验证证据 |
| [本地结果](results/validation.md) | 实际编译、测试、实验结果及其限制 |
| [内核追踪诊断](docs/perf-diagnostic.md) | 独立 perf 轮次的真实调度等待及追踪扰动 |

五种路由策略为 `home`、`borrow`、`sticky`、`fanout`、`least-load`。计量包含已提交未开始、已开始未结束以及未完成 CPU 工作估计，并保留 Folly 队列深度作对照。NUMA 标签用于路由，不自动绑定内存。每秒 p99 在 12 QPS 下样本很少，策略决策须结合完整请求时延、拒绝率、多轮对照和内核证据。

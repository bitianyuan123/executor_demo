# Folly busy 独立补丁与用户态 runnable 近似

本交付回答两个问题：怎样便宜地获得执行回调的 worker 数；怎样把它用作 OS running+runnable 的近似。**新增 B 是 worker 占用计数，不是内核线程状态。** 不依赖此前 N 补丁，不改调度、亲和性、负载或线程数量。

针对当前 executor：先审计是否主要做 CPU 运算、RPC 是否真正异步且立即返回。满足时先采 B；如果存在可识别的同步阻塞，后续可评估 `R估计 = B − K已知等待`。K 扩展不在本补丁中。Q 与 N 仍分别表达排队和未完成工作，不能靠 B 替代。

| 文件 | 用途 |
|---|---|
| [DESIGN.md](DESIGN.md) | 指标选择、与 OS/原生统计的差异、流程图和构造伪代码 |
| [native-busy.patch](native-busy.patch) | 独立 Folly 源码补丁及专项测试 |
| [manifest.json](manifest.json) | 精确基线、实现提交、补丁校验值 |
| [PROMPT_ONLY.md](PROMPT_ONLY.md) | 无法交接文件时，直接粘贴给现场 AI；内含版本审计及分步任务 |
| [example.cpp](example.cpp) | 一个执行中回调＋一个排队任务，验证 B/Q/原生统计区别 |
| [VALIDATION.md](VALIDATION.md) | 本地实际验证范围与限制 |

## 应用与编译

基线为 Folly `v2026.10.26.00`，完整 SHA `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。现场版本不同时，先执行提示词中的源码探索，按语义移植，不应强行套行号或掩盖冲突。已有 N 补丁时两个改动可能触及同一位置，必须人工合并并验证两个计数边界，不要直接覆盖。

以下命令假定当前在 Folly 根目录；把交付目录写成自己的绝对路径。

```sh
BUSY_HANDOFF=/absolute/path/to/busy-accounting
git apply --check "$BUSY_HANDOFF/native-busy.patch"
git apply "$BUSY_HANDOFF/native-busy.patch"

cmake -S . -B _build-busy -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DBUILD_BENCHMARKS=OFF \
  -DCMAKE_INSTALL_PREFIX="$PWD/_install-busy"
cmake --build _build-busy -j2
cmake --install _build-busy

cmake -S "$BUSY_HANDOFF" -B "$BUSY_HANDOFF/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DFOLLY_SOURCE_DIR="$PWD" \
  -DCMAKE_PREFIX_PATH="$PWD/_install-busy"
cmake --build "$BUSY_HANDOFF/build" -j2
ctest --test-dir "$BUSY_HANDOFF/build" --output-on-failure
```

Folly 依赖沿用现场已验证的安装方式；独立测试需要 GTest。`BUILD_TESTING=OFF` 可仅编译样例，但不能据此声称专项测试已通过。可选 `-DBUILD_UPSTREAM_CPU_REGRESSION=ON` 另需匹配的 GMock 和 Boost thread；本地已执行该回归。测试使用 Folly 的 TestMain 初始化基础设施。

本补丁改变 `CPUThreadPoolExecutor` 与 `Options` 的布局：Folly 和使用它的应用须一致重编、链接同一份安装。不要把旧安装头文件与新静态库混用。`CPUTask`、父类、IO executor 布局不改。

## 应用读取

```cpp
using Pool = folly::CPUThreadPoolExecutor;
Pool pool(64, Pool::Options().setBusyCounting());
auto observed = pool.getBusyTaskCount();
if (observed) {
  const size_t busy = *observed; // 一次 relaxed 原子读
  // 记录 pool_id、单调时间、busy；需要 Q 时另读 getTaskQueueSize()。
}
```

这些是**本补丁新增 API**。默认关闭；关闭时返回 `folly::none`，不能将其转换为“空闲 0”。配置在构造时固定，不支持运行中切换。B=0 也不是完成屏障；完成仍使用既有 future/join 等同步。

最小现场验收只有：源码/版本核对、编译、一次专项测试和样例；如需评估扰动，用同一固定业务负载做一组 off/on 对比即可。记录吞吐、请求完成 mean/p99、CPU 时间和 B/Q；不自动扩展参数矩阵，不预设 B 比 N 更能降低 p99。OS 近似是否足够好另看 DESIGN 中的校准建议。

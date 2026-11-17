# 业务基础设施维护 N 的最小样例

本目录独立编译，使用已安装的原版 `folly::CPUThreadPoolExecutor`，不依赖原生 N 补丁。`TrackedCpuExecutor` 是统一提交入口，采用组合而非继承；业务算子不需要零散维护计数。

## 语义和 API

- `getOutstandingTaskCount()` 返回 `folly::Optional<size_t>`。开启时，N = 此入口预留但尚未终结的执行回调数；关闭时返回 `none`，不能将其当作空闲的 0 参与选池。
- 计数默认关闭，须构造时显式传入 `true`，与原生方案保持一致；开关在构造时固定。开启后，`add(Func)`、`addWithPriority(Func, priority)`、`add(Func, expiration, expireCallback)`、`add(Func, priority, expiration, expireCallback)` 均经过统一记账。
- 发布给 Folly 之前 N++；排队、worker 等待 CPU、运行、同步阻塞期间均保留；回调正常或异常退出后 N--。
- 提交失败或闭包从未执行即被永久销毁，由移动票据的析构释放；不在 `catch(add)` 中盲目 N--，避免所有权已转移时重复释放。
- 过期任务仍按一个任务计数。Folly 可以先销毁正常回调再执行过期回调，因此两个回调共享一个 lease，直至胜出的回调结束才释放。未提供用户过期回调时，也安装内部完成回调。正常执行不会等待那个未被调用的过期闭包销毁才释放 N。
- 空用户函数明确拒绝，不能因包装而变成普通任务，也不允许用户制造 Folly 内部停止标记。负过期时间明确拒绝，0 表示不启用过期。
- `CounterView` 只保活计数状态，可在包装对象和 pool 销毁后检查最终数值；不会保活 worker 或队列。
- `join()` 排空后 N 应为 0。`stop()` 本身不保证排队对象已经销毁；保留的任务仍计入 N，直到它们执行或销毁。本地零 worker 测试明确验证这一行为。

```cpp
auto cpuPool = std::make_shared<folly::CPUThreadPoolExecutor>(128);
load_accounting::TrackedCpuExecutor tracked(cpuPool, true);

auto n = tracked.getOutstandingTaskCount(); // O(1)，当次任务预留前的观测
if (n) {
  double score = double(*n) / capacity;
  // 在调度层使用 score；这里不写死容量、阈值或跨 NUMA 策略。
}
tracked.add([] { computeCpuFragment(); });
```

`capacity` 必须由调用方定义。N 不知道 CPU 亲和性，不等于 CPU 利用率，也不代表这些任务成本相等。异步 RPC 的等待区间不保留已返回 CPU 回调的 N；响应产生新的 CPU 回调时再次提交计数。同步 RPC 阻塞则仍计入 N。

## 集成边界与成本

一个实际 pool 只建立一个统一计数包装；所有需要统计的入口（包括 continuation 和内部转发）必须经过它。绕过包装直接调用原始 pool 会漏计。样例接受共享 pool 是为了接入已有配置，不意味着允许继续混用多个提交入口。外接 pool 在包装建立之前已经排队或运行的任务无法追溯计数；应在开始接收任务前建立包装，或先排空旧入口再切换，否则初期的 N 仅覆盖新入口任务，不能代表整个 pool。

本类不是 `folly::Executor` 的可替换实现，也没有实现 KeepAlive 协议。已有接口若要求 `Executor*`、`Executor::KeepAlive` 或 Future 自动投递，先在真实版本中审查这些路径：可以统一改造业务提交抽象，或改用原生方案，不应强制转换、伪造 KeepAlive，也不应把原始 pool 传出去后宣称全部计数。

每池一次计数状态分配。普通任务正常路径有计数的两次 relaxed 原子读改写，以及共享计数状态的引用计数操作；移动 Ticket 本身没有独立堆分配。闭包变大可能增加 `folly::Function` 的存储/分配成本。真正启用 expiration 的任务额外有一次 `make_shared<ExpiringLease>` 分配、共享所有权操作和终态原子交换。getter 是一次 relaxed load；disabled 不创建票据。成本需要在真实平台测量，不能将其称为零开销。

计数用于观测，不发布业务数据；读取 N=0 不是结果同步屏障。生命周期仍需 join 等现有同步。并发读取各池后选池不是一次原子事务，多个提交者可同时选中同一池；精确接纳上限需要另行设计 CAS 预留。N 的计数原子不会使阈值调度自动严格。

本样例回调体结束就释放，包含用户过期回调；不等待 Folly 的 `taskProcessed` 观察者、RequestContext 清理或任意用户捕获对象的析构完成。原生库内方案可以覆盖更外围的 `runTask()` 清理，比较时必须说明这一微小边界差异。取消标志不提前减计数；只有实际退出或永久丢弃才释放。

包装对象不能与其成员函数并发销毁；`join/stop` 前需停止提交者。若外部仍持有 pool，已提交任务可在包装对象销毁后继续结束，票据持有的计数 state 保持有效。只有计数状态被延长生命周期，业务捕获对象仍由调用方保证有效。

## 构建和验证

在仓库根目录执行，将前缀替换为真实安装路径：

```bash
cmake -S handoff/load-accounting/business \
  -B build-load-accounting-business -G Ninja \
  -DCMAKE_PREFIX_PATH=/absolute/path/to/folly/_install \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build-load-accounting-business -j1
ctest --test-dir build-load-accounting-business --output-on-failure
./build-load-accounting-business/business_accounting_example
```

示例期望打印 `running=1 queued=1 N=2`、`after drain N=0`、`disabled metric available=0`。测试包含真实 Folly 队列边界的故障注入、正在运行的过期回调、正常执行与未执行销毁，以及 4 个生产者最终排空。它们验证生命周期正确性，不是性能压力实验。

版本差异重点：检查 CPU pool 的四种提交签名、expiration=0 语义、正常和过期分支的独占调用/丢弃行为、`stop` 保留队列的生命周期、`Folly::folly` 与 `GTest::gtest_main` 导出名称。若版本签名不同，按实际源码适配，不机械复制。样例依赖公开接口，不对 `CPUTask` 布局作 ABI 假设；测试中的故障队列直接使用本版本公开的 `CPUTask` / `BlockingQueue`，旧版本可调整测试夹具。

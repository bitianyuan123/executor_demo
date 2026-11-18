# busy 独立补丁验证记录

日期：2026-11-18。Folly 基线 `v2026.10.26.00` / `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`；实现提交 `1b5f0b2b0e6352a36364f17e766dd3a01448a619`，独立分支 `experiment/native-busy-count-20261118`。本次没有合入此前 N 或 grouped executor 的修改。

环境：本地 x86_64 Linux，4个可见CPU，约7.5GiB内存；GCC 14.2.0、CMake 3.31.6、Ninja 1.12.1。Release/C++20，以 `-j2` 完整编译独立 Folly 并安装到专用目录，再用该安装编译样例与测试。4GB环境可将构建并发改为 `-j1`。

| 检查 | 实际结果 |
|---|---|
| Folly完整Release构建与独立安装 | 通过 |
| busy样例 | 通过 |
| 新增busy专项测试 | 15/15通过，无跳过 |
| 原有CPU线程池相关回归 | 15/15通过 |
| 原有IO线程池基础/Join回归 | 2/2通过 |
| CTest入口 | 3/3通过，0失败，总耗时0.45s |
| 补丁独立应用校验 | 干净基线文件上 `git apply --check` 通过，应用后7个变更文件逐字节匹配实现 |

原始记录：[ctest.txt](validation/ctest.txt)、[独立工程配置记录](validation/configure.txt)。原始日志内用户异常/过期异常/线程创建失败是测试故意制造的情况；对应测试均通过。上述测试耗时不是维护 B 的性能测试数据。

样例实际输出：

```text
disabled=unavailable; gated callback: B=1, Q=1, PoolStats.activeThreadCount=1, PoolStats.totalTaskCount=2
after join: B=0; blocked callback was still busy; B is NOT an OS running+runnable measurement
```

其中“阻塞仍B=1”验证的是用户态计数语义，没有读取该 worker 的内核状态，不能解释为已测量 B 与 OS 的误差。

专项测试覆盖默认关闭/启用空闲、排队与拒绝不计、阻塞回调仍计、普通异常、过期回调及异常、无过期回调、TaskObserver计入/QueueObserver排除、所有构造及提交入口、stop时执行中任务的计数、多生产者与动态创建。共享闸门采用支持多个waiter的条件变量，避免将只支持单waiter的Baton用于多个worker。

执行命令（本地路径，现场替换对应目录）：

```sh
cmake -S /root/rank_demo/folly_busy_accounting \
  -B /root/rank_demo/folly_busy_accounting/_build-busy -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DBUILD_BENCHMARKS=OFF \
  -DCMAKE_INSTALL_PREFIX=/root/rank_demo/folly_busy_accounting/_install-busy
cmake --build /root/rank_demo/folly_busy_accounting/_build-busy -j2
cmake --install /root/rank_demo/folly_busy_accounting/_build-busy

cmake -S /root/rank_demo/stage3_balance/handoff/busy-accounting \
  -B /root/rank_demo/stage3_balance/build-busy-accounting -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DFOLLY_SOURCE_DIR=/root/rank_demo/folly_busy_accounting \
  -DCMAKE_PREFIX_PATH=/root/rank_demo/folly_busy_accounting/_install-busy \
  -DGTest_DIR=/root/rank_demo/stage3_balance/build-load-accounting-gtest/install/lib/cmake/GTest \
  -DBUILD_UPSTREAM_CPU_REGRESSION=ON
cmake --build /root/rank_demo/stage3_balance/build-busy-accounting -j2
ctest --test-dir /root/rank_demo/stage3_balance/build-busy-accounting -V
```

这里复用了本地已有的匹配 GTest/GMock 安装，只执行源码中的专项测试及选定回归，没有重新运行整个Folly测试集，也未执行Buck构建。

**尚未验证**：ARM64现场执行、观测开关的实际CPU/时延代价、B与OS running+runnable曲线的误差、任何负载均衡或p99收益。R估计/K只有设计，没有实现或验证。现场下一步只需按README完成版本核对、构建和一次最小验收；没有由本次改造自动触发的性能测试矩阵。

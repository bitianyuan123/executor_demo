# Folly原生N样例

先读[版本移植说明](../FOLLY_PORTING.md)。补丁基线为`v2026.10.26.00`，CPU池新增构造选项`setTaskCounting(true)`及返回`folly::Optional<size_t>`的`getOutstandingTaskCount()`。这是本包新增接口，不是所有Folly版本已经提供的API。

以下变量由现场填真实路径；无须联网。`FOLLY_SRC`应是隔离源码副本，不要直接修改其他agent正在使用的目录。`HANDOFF_DIR`是本包`load-accounting`目录。现场不同版本先按符号移植，不能直接执行补丁应用。

```bash
FOLLY_SRC=/path/to/isolated/folly
HANDOFF_DIR=/path/to/load-accounting
FOLLY_BUILD=/path/to/build-native-folly
FOLLY_PREFIX=/path/to/install-native-folly
SAMPLE_BUILD=/path/to/build-native-sample

# 仅参考基线相同时使用；若已在交付分支上，不再apply一次。
git -C "$FOLLY_SRC" apply --check "$HANDOFF_DIR/folly/native-outstanding.patch"
git -C "$FOLLY_SRC" apply "$HANDOFF_DIR/folly/native-outstanding.patch"

cmake -S "$FOLLY_SRC" -B "$FOLLY_BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DBUILD_BENCHMARKS=OFF \
  -DCMAKE_INSTALL_PREFIX="$FOLLY_PREFIX"
cmake --build "$FOLLY_BUILD" -j1
cmake --install "$FOLLY_BUILD"

cmake -S "$HANDOFF_DIR/folly" -B "$SAMPLE_BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$FOLLY_PREFIX" \
  -DFOLLY_SOURCE_DIR="$FOLLY_SRC" -DBUILD_TESTING=ON
cmake --build "$SAMPLE_BUILD" -j1
ctest --test-dir "$SAMPLE_BUILD" --output-on-failure
"$SAMPLE_BUILD/native_outstanding_example"
```

期望示例打印`N=2`（1项已开始但被gate挡住，1项排队）和`after join: N=0`。这里gate只用于功能验证，不能把其时延当性能数据。

依赖使用现场已有Folly构建工具及GTest；缺少依赖时报告缺项，不要求离线agent联网安装。示例可以`-DBUILD_TESTING=OFF`单独构建，但不能据此称全部正确性测试通过。真实项目还需重编实际应用与相关模块，不能混用新头文件和旧库。

本包的CMake默认只注册定向计数测试，不声称执行整个Folly测试集；真实版本应另运行原有CPU线程池相关测试。完整本地实测范围见[验证记录](../VALIDATION.md)。

相同参考版本下，可在最后一次样例配置命令中增加
`-DBUILD_UPSTREAM_CPU_REGRESSION=ON`，编译现有`ThreadPoolExecutorTest.cpp`并运行
15项选定回归（提交、优先级、过期、KeepAlive、关闭和创建线程失败）。这需要现场已有
GMock及Boost thread。其他版本先检查测试名称与文件，避免过滤器未匹配却误报回归完成。
测试使用本版本`folly/test/common/TestMain.cpp`初始化Folly单例；不能仅换成
`gtest_main`，否则KeepAlive/Timekeeper相关测试会因未初始化而中止。
Folly本体也已注册新增的`executors_cpu_thread_pool_executor_outstanding_test`；
使用已有Folly测试构建体系时可选该目标，不必重跑整个测试集。

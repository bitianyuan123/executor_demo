# 测试环境构建与恢复

本指南对应Demo分支`experiment/affinity-load-20261114`。目标ARM测试机及其发行版尚未在开发环境复现；下面区分日志已确认的问题与待定位的环境提示。

用户随后回传的测试机日志确认：安装GoogleTest 1.14.0后，Demo配置、8个编译/链接步骤和根工程10组CTest全部通过（3.77秒）。先前日志中的工具链为GCC 12.3.1、Python 3.11.6、Boost 1.83.0。这是用户提供的现场构建与功能测试证据；未包含阶段5可选库后端或正式负载对比结果。已经完成这一步的机器无需重复安装GTest，可直接更新脚本、采集拓扑并按各阶段指南继续。

## Folly已安装，Demo报告找不到GTest

`Could NOT find GTest (missing: GTEST_LIBRARY GTEST_INCLUDE_DIR GTEST_MAIN_LIBRARY)`来自阶段4的测试配置。Folly使用`BUILD_TESTS=OFF`编译，而Demo默认`BUILD_TESTING=ON`；两者是独立开关。GoogleTest开发包只用于DAG和可选阶段5的C++测试，不是实验可执行文件本身的运行依赖。

本次日志已显示Folly编译、安装完成，可以直接复用`/home/bty/rank_demo/folly/_install`。在原实验分支工作区更新代码、安装缺失依赖后继续：

```bash
cd /home/bty/rank_demo/executor_demo
git pull --ff-only

# RPM系通常使用此包名；仅当当前发行版仓库提供该包时适用。
# root用户可直接执行；普通用户按本机权限使用sudo。
dnf install gtest-devel

SKIP_FOLLY_BUILD=1 BUILD_JOBS=1 ./scripts/build.sh
python3 scripts/topology.py --output results/local/topology.json
```

Debian/Ubuntu对应`apt-get install libgtest-dev`。如果包管理器没有这个包，不要把“未找到包”当成已解决；可使用下面的已安装前缀或已有源码方式。`SKIP_FOLLY_BUILD=1`只跳过Folly构建与安装，仍然配置、编译Demo并运行其测试。若安装路径不同，加上`FOLLY_PREFIX=/absolute/path/to/folly/install`。

RPM包名参考[Fedora/EPEL的gtest-devel开发包](https://packages.fedoraproject.org/pkgs/gtest/gtest-devel/)；这不代表所有RPM发行版都配置了同样的软件仓库。

### GoogleTest已经安装在自定义位置

直接配置Demo并设置`GTest_ROOT`，指向安装前缀，而不是头文件目录：

```bash
cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/home/bty/rank_demo/folly/_install \
  -DGTest_ROOT=/absolute/path/to/gtest/install \
  -DBUILD_TESTING=ON -DDAG_BUILD_FOLLY_EXTENSION=OFF
cmake --build build --parallel 1
ctest --test-dir build --output-on-failure
```

如果只有源码而没有编译安装的库，使用下一种方式；仅提供`gtest/gtest.h`无法满足链接要求。
安装后的CMake包与库发现机制见[CMake FindGTest文档](https://cmake.org/cmake/help/latest/module/FindGTest.html)。

### GoogleTest只有本地源码

指定已有、可用的GoogleTest源码树根目录，其中应包含`CMakeLists.txt`及`googletest/include/gtest/gtest.h`（不是只有gtest子目录的旧式`/usr/src/gtest`路径）。该选项明确选择源码构建，不会联网获取或更新依赖：

```bash
DEMO_GTEST_SOURCE_DIR=/absolute/path/to/googletest \
  SKIP_FOLLY_BUILD=1 BUILD_JOBS=1 ./scripts/build.sh

# 单独验证阶段4时也可以使用相同选项
DEMO_GTEST_SOURCE_DIR=/absolute/path/to/googletest \
  BUILD_JOBS=1 ./scripts/build_stage.sh dag
```

记录使用的GoogleTest源码版本，便于复现。直接调用CMake时等价参数为`-DDEMO_GTEST_SOURCE_DIR=/absolute/path/to/googletest`。

该路径会保存在相应构建目录的CMake缓存中。之后仅取消环境变量不会改回系统包；切回系统包时，使用相同的`-S`和`-B`重新配置并传入`-DDEMO_GTEST_SOURCE_DIR=`，或者使用新的构建目录。

### 先验证不依赖GTest的阶段，或者只构建可执行文件

阶段2和阶段3可以独立构建并运行各自测试，无需等待DAG的GTest依赖：

```bash
BUILD_JOBS=1 ./scripts/build_stage.sh metrics
BUILD_JOBS=1 ./scripts/build_stage.sh independent
```

如果只需要临时生成DAG可执行文件，可明确关闭测试：

```bash
BUILD_TESTING=OFF BUILD_JOBS=1 ./scripts/build_stage.sh dag
```

这种方式不会运行任何该阶段测试，不能标注为“编译及测试通过”。补齐GTest后重新执行`BUILD_TESTING=ON BUILD_JOBS=1 ./scripts/build_stage.sh dag`，完成实际验证；根工程也支持相同的`BUILD_TESTING`开关。

## 拓扑输出目录不存在

旧版脚本直接写入文件，首次执行`--output results/local/topology.json`会因父目录不存在而报`FileNotFoundError`。修复后会自动创建父目录，重新执行原命令即可。无法立即更新代码时可先执行：

```bash
mkdir -p results/local
python3 scripts/topology.py --output results/local/topology.json
```

输出后仍需按实验指南核对实际allowed CPU、NUMA、cluster、SMT和quota；生成JSON成功仅说明完成了信息采集。

## 编译过程中出现`cat: /.build.command: No such file or directory`

目前在Demo和Folly源码中未找到读取这个文件的代码，尚未确定测试机上的提示来源。用户日志显示Folly最终链接、安装成功；后续阻塞构建的是独立的GTest错误。不要通过创建`/.build.command`来掩盖提示。

如需继续定位，可以收集编译器路径、CMake中的编译器及launcher设置，以及Ninja已经生成的编译命令。下面仅查看已有配置，不重新执行Folly构建：

```bash
type -a c++ gcc g++
readlink -f "$(command -v c++)"
grep -E 'CMAKE_(C|CXX)_COMPILER(:|_LAUNCHER)|RULE_LAUNCH' \
  ../folly/_build/CMakeCache.txt build/CMakeCache.txt
ninja -C ../folly/_build -t commands | head -n 8
```

重点核对测试机实际编译器是否经过外部包装脚本，以及生成命令是否调用了额外工具；如果仍不能确定，再据这些证据定位。当前开发机没有复现该提示，不能据现有日志断言它由Folly或者某个特定环境组件产生。

## 本次修复的本地验证

以下验证在Debian 13 / GCC 14.2开发机执行，与上面的用户回传日志分开记录；未重新编译Folly，也未重新运行性能矩阵：

- `SKIP_FOLLY_BUILD=1 BUILD_JOBS=1 ./scripts/build.sh`：根工程构建成功，默认业务后端10组CTest全部通过。
- 模拟禁止发现系统GTest：测试开启时配置按预期失败，并给出依赖及恢复指引；测试关闭时DAG配置与构建成功，脚本明确报告未运行测试。
- 同一DAG构建目录重新开启测试：独立入口5组CTest全部通过。
- 指定本地GoogleTest 1.15.0源码：实际编译GoogleTest及`business_executor_test`，22个执行器测试全部通过。该路径未使用系统GTest库。
- 拓扑脚本：首次创建多层目录、覆盖已存在文件、纯文件名输出及标准输出均产生可解析JSON。
- 构建脚本语法检查及无效开关拒绝检查通过。

这些检查验证构建恢复与信息采集行为，不代替测试机的正式负载、亲和性和时延对比。

# 实际验证记录与现场最小验收

验证日期：2026-11-17。环境：Linux x86_64、GCC 14.2、C++20、Release。
这是开发机的编译与生命周期验证，不是ARM业务性能实验。

## 结果

|项目|实际结果|范围|
|---|---|---|
|业务侧统一包装器|13项GTest通过，1个CTest入口|原本地Folly安装，只读使用，未应用原生N补丁|
|独立修改的Folly|完整构建、独立前缀安装成功|`v2026.10.26.00`基线加原生N补丁；未覆盖原Folly目录|
|原生N专项|15项GTest通过|禁用、计数、优先级、拒绝/异常、过期有/无回调、移动、停止和状态寿命|
|原有CPU池回归|15项选定GTest通过|原测试源码，包含KeepAlive、Future、RequestContext、关闭、线程创建失败|
|两套使用示例|均成功|运行1项+排队1项时N=2，join后N=0，禁用返回不可用|

原生日志中的两个CTest入口分别包装15项GTest，不能误读为只跑了2项断言。
原始测试输出见[`validation/business-ctest.txt`](validation/business-ctest.txt)、
[`validation/native-ctest.txt`](validation/native-ctest.txt)；示例输出见
[`validation/examples.txt`](validation/examples.txt)。业务异常与过期异常是故意注入的测试输入，
日志中会有对应错误记录，但测试检查了归零及原异常处理语义。

没有运行：整个Folly测试全集、BUCK构建、ASan/TSan、真实ARM应用、计数开关性能对照。
没有新增性能矩阵，也没有修改现有阶段3实验、DAG实验或分组执行器。

## 本地可复现命令

业务实现使用已有原Folly安装；安装中的CPU池代码与参考基线一致。命令在
`/root/rank_demo/stage3_balance`执行：

```bash
cmake -S handoff/load-accounting/business -B build-load-accounting-business \
  -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/root/rank_demo/folly/_install
cmake --build build-load-accounting-business -j1
ctest --test-dir build-load-accounting-business --output-on-failure
./build-load-accounting-business/business_accounting_example
```

原生方案使用隔离的源码、构建和安装位置：

```bash
cmake -S /root/rank_demo/folly_n_accounting \
  -B /root/rank_demo/folly_n_accounting/_build-native -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DBUILD_BENCHMARKS=OFF \
  -DCMAKE_INSTALL_PREFIX=/root/rank_demo/folly_n_accounting/_install-native
cmake --build /root/rank_demo/folly_n_accounting/_build-native -j2
cmake --install /root/rank_demo/folly_n_accounting/_build-native
```

本机GTest安装未导出GMock，原有CPU回归需要GMock。因此从**本机已有源码**建立独立安装，
没有下载依赖或修改系统安装：

```bash
cmake -S /usr/src/googletest -B build-load-accounting-gtest -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_GMOCK=ON -DINSTALL_GTEST=ON \
  -DCMAKE_INSTALL_PREFIX=/root/rank_demo/stage3_balance/build-load-accounting-gtest/install
cmake --build build-load-accounting-gtest -j2
cmake --install build-load-accounting-gtest

cmake -S handoff/load-accounting/folly -B build-load-accounting-native -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/root/rank_demo/folly_n_accounting/_install-native \
  -DFOLLY_SOURCE_DIR=/root/rank_demo/folly_n_accounting \
  -DGTest_DIR=/root/rank_demo/stage3_balance/build-load-accounting-gtest/install/lib/cmake/GTest \
  -DBUILD_UPSTREAM_CPU_REGRESSION=ON
cmake --build build-load-accounting-native -j1
ctest --test-dir build-load-accounting-native --output-on-failure
./build-load-accounting-native/native_outstanding_example
```

参考版本的回归需要`folly/test/common/TestMain.cpp`完成Folly初始化。
初次使用普通`gtest_main`时，KeepAlive测试因Timekeeper单例未初始化中止；
独立构建入口已修正并通过全部15项选定回归。这是测试入口要求，不是N计数失败。
旧版本应使用其自己的测试初始化入口，不机械复制路径。

## 布局与成本

本机类型尺寸检查结果如下，单位字节；不是ARM通用尺寸：

|类型|修改前|修改后|
|---|---:|---:|
|`CPUThreadPoolExecutor::Options`|4|8|
|`CPUThreadPoolExecutor::CPUTask`|128|144|
|`CPUThreadPoolExecutor`|2624|2624|

executor总尺寸因padding未变不代表内部布局兼容。修改了头文件公开布局，必须重编库与消费者。
默认关闭不恢复旧ABI，也不能据此声称完全零扰动。

本包没有给出原子操作或共享指针的纳秒成本；真实平台、争用和任务粒度都会影响成本。
getter是一次relaxed load；维护路径还包含原子读改写、共享所有权及对象尺寸成本。
业务expiration双闭包的共享lease还需要一次逐任务分配。参见各实现README。

## 现场只需要做什么

1. 选一套方案，先确认实际Folly头文件、链接库、提交入口及生命周期。不能传文件时复制
   `PROMPT_ONLY.md`中对应完整prompt；现场仅探索时指定第1阶段。
2. 在隔离改动中构建，跑生命周期测试和已有相关回归。业务包装先于接收任务安装；
   原生计数在构造时启用，重编所有受影响消费者。
3. 验证默认不可用、启用空闲0、排队+执行同时计入、拒绝/异常/过期/丢弃恰好释放、排空归零。
   `stop()`仍保留队列时非零是允许的，不能强行清零。
4. 如已经授权测量开销，只做固定输入的观测off/on各一轮。保持调度、阈值、亲和、线程、
   资源和负载一致；记录请求完成mean/p99、吞吐、失败、CPU和N。不给“开计数”附加均衡收益。
   结果不足以判断时只说明限制，不自动加三次重复或展开参数矩阵。

若现场只能输出终端文本，保留构建/链接来源、测试用例数量、通过/失败、示例N和未覆盖入口即可。
不要为了收集N在每次任务提交时打印日志或读取`/proc`。

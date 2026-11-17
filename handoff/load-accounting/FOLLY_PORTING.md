# Folly原生N：修改说明与版本迁移

## 参考边界

参考基线：`v2026.10.26.00` / `c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。原始本地分组分支HEAD为`4d7cc97d0`，CPUThreadPoolExecutor与ThreadPoolExecutor相关文件对基线没有差异。实现隔离在`experiment/native-outstanding-20261117`，未把旧分组调度补丁混进来。

以下按**类、函数、所有权**定位；行号会随Folly版本改变。现场源码优先。不同版本即使`git apply --check`通过，也不能跳过调用路径审计与重编验证。

## 新增接口

```cpp
using Pool = folly::CPUThreadPoolExecutor;
Pool::Options options;
options.setTaskCounting(true); // 默认false；构造后不切换
Pool pool(16, options);        // 16只是API使用示例，不是线程数调优建议

auto count = pool.getOutstandingTaskCount();
if (count.hasValue()) {
  observe(*count);             // 包含排队和执行；不等于CPU利用率
} else {
  markLoadUnavailable();       // 禁用不能退化为“空闲0”
}
```

本次不把C、request home、0.8/0.5、NUMA候选或负载均衡写进原生CPU池。N是原语；路由策略仍由上层决定。生命周期定义为预留至整个`runTask`处理结束/永久丢弃，包含过期回调和task observer。

## 文件与改造点

|文件/符号|具体修改|正确性原因|
|---|---|---|
|`CPUThreadPoolExecutor.h` / `Options`|增加构造时固定的`taskCounting`，默认false，setter返回Options引用|保留显式启用；未启用不伪报0|
|同文件 / executor成员|增加共享的每池`OutstandingTaskState`，里面一个原子count|计数状态独立于executor对象寿命|
|同文件 / `OutstandingTaskTicket`|不可复制、可移动；创建加一，释放减一；移动源失去所有权，移动赋值先释放旧票据|队列多次move不重复计数或遗漏已有计数|
|同文件 / `CPUTask`|携带票据，默认poison无票据；明确move构造与赋值|生命周期跟任务，而非跟add调用栈|
|同文件 / `addImpl`|普通非空任务在observer与队列发布前获得票据|worker可能早于add返回执行；所有提交重载统一覆盖|
|`CPUThreadPoolExecutor.cpp` / 主构造函数|按Options创建一个每池state|各委托构造路径都使用同一开关|
|同文件 / `getOutstandingTaskCount`|返回Optional，启用时一次relaxed load|不能拿计数作为任务数据的同步屏障|
|同文件 / `threadRun`|普通任务处理块用scope guard，在`runTask`及observer后释放票据|包含正常、异常处理、过期回调；取出/开始不减|
|票据析构|还持有预留时兜底释放|提交失败、关闭丢弃、自定义队列保留再销毁均覆盖|
|CMake/BUCK|Optional移为公开依赖，注册独立计数测试|公开头文件依赖与测试入口随补丁一起交付|

`ThreadPoolExecutor.cpp`无需修改。其`runTask(ThreadPtr, Task&&)`接收引用，本版本不会把派生CPUTask切片；迁移到不同版本必须重新核对。`registerTaskEnqueue()`先通知、后真正入队，单靠enqueued/processed observer无法覆盖队列拒绝和未执行销毁。

## 生命周期伪代码

```text
Options.taskCounting = false
getOutstandingTaskCount():
    disabled -> none
    enabled  -> state.count.load(relaxed)

addImpl(task):
    先保留原有空func/poison约束
    task.ticket = Ticket(state)  // enabled时N++，发布前发生
    原有observer、KeepAlive保护与enqueue流程
    enqueue(move(task))          // 所有权转给队列
    不在add返回后补N++，不因catch而盲目N--

worker取到普通任务:
    scope_exit: task.ticket.release() // 最多一次
    原queue observer
    runTask(task) // 正常func或过期处理，task observers

Ticket move:
    转移state；源变空；不增减N
Ticket move赋值:
    先释放自己原有预留，再接管源
Ticket release或析构:
    若state非空: N--，清空state
```

原生实现每个启用的池分配一个共享state，没有每任务独立ticket堆分配；每任务仍有shared_ptr引用计数成本，不能把整体维护成本简化成只有两次N的原子RMW。默认关闭仍增加CPUTask对象空间及空票据的移动/检查成本。对象大小、编译和测试结果见验证记录，不在未测平台声称“可以忽略”。

## 必须逐项核对的版本差异

1. **真实依赖来源**：应用实际编译的Folly头文件和链接库在哪里，是否vendored、带发行版补丁、静态/共享链接、多个Folly副本。版本字符串之外记录源码commit和关键文件SHA256；同tag有本地补丁时不能视为完全相同。
2. **提交入口**：add、addWithPriority、expiration、自定义重载、子类、Future continuation最终是否进入同一addImpl。定制executor绕过此入口需单独接入，不能假定被自动计数。
3. **任务移动**：队列是否要求默认构造、可复制、特定size/alignment或trivially relocatable；CPUTask是否被切片或跨executor直接搬运。票据只属于一次目标池预留，不能复制。重提同一已持票据任务必须定义转换，不能重复包两层原生预留。
4. **拒绝语义**：本版本bounded queue拒绝和observer调用顺序需保留。不要在外层catch无条件减数；根据任务所有权释放。若其他版本/自定义队列存在接纳后再抛，不能宣称任务未执行。本版本`ensureActiveThreads()`为noexcept，不把版本风险编造成这里的已知异常。
5. **过期和异常**：是否跳过原func、运行expireCallback、何时销毁func；计数应跨过期回调保持。回调已被内部捕获的异常仍要结束计数。不能只把票据放在普通func中，忽略另一条执行路径。
6. **关闭**：区分join排空与stop停止worker；stop可能仍保留队列项，N此时允许非零，不能强清零。实际丢弃/析构时释放。调用getN要求executor仍有效；独立state保活不代表可以访问已销毁executor。
7. **executor寿命**：本版本addImpl明确说明enqueue后executor可能不再存活。保留原KeepAlive流程，新增计数逻辑不得在发布后无保护地解引用this。不要用每任务持有executor KeepAlive随意替代计数state，以免改变关闭语义或形成等待环。
8. **ABI与构建**：Options、executor和CPUTask布局改变，需要一起重编库、应用和使用这些类型的模块/插件。禁用开关不恢复旧ABI。不能混用新头旧库。
9. **开关与观测**：构造时启用；中途注册observer无法补齐已有任务。getter只提供快照，不是原子接纳，也不能用于没有关停提交协议的“看到0就销毁”。

## 最小移植流程

1. 只读记录以上事实和现有工作树变更；使用不影响其他agent的分支/目录。离线环境不要要求下载另一版本源码。
2. 参考版本完全相同时，在隔离副本先`git apply --check`再应用补丁；否则根据符号手工移植，不使用模糊补丁成功作为兼容性证明。
3. 先重编新的Folly，再定向编译本包原生样例与测试；最后重编实际消费者。使用独立安装前缀，不覆盖其他工作的安装目录。
4. 验证禁用/启用、排队+执行、提前执行、正常/异常、拒绝、过期、未执行丢弃、stop/join、move、多生产者；有定制队列再加其契约案例。
5. 若要验证扰动，只做观测off/on，保持业务输入与所有调度参数相同。记录CPU、请求mean/p99、吞吐、失败与排空；没有完整授权或依赖时完成可执行部分并报告实际边界，不编造“生产通过”。

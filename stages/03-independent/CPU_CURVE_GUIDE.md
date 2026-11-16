# 阶段3：自动采集微突发CPU时间曲线

## 1. 本次增加什么

在现有request-wave测试上增加可选`--cpu-trace`。测试运行期间自动流式记录CPU进出idle的调度事件，结束后自动生成**5ms窗口CPU占用曲线**，包含六个cluster及其CPU并集、初始投递窗口和后续阶段。原有100ms `/proc/stat`观测同时保留，CPU数据仍不参与负载均衡。

不改变12 QPS、80+80任务、CPU预算、128线程和三个策略，不增加新的负载矩阵。新的观测需要新的结果目录；3004已完成的100ms汇总不能恢复当时的5ms曲线。若需要这组曲线，只需沿用当前负载再运行三个策略各一次，无需三次重复或重新搜索阈值。

## 2. 为什么不直接把sample-ms设为5

`/proc/stat`导出的累计时间以USER_HZ为单位，常见`getconf CLK_TCK=100`对应10ms计数单位；多个CPU聚合可缓和量化误差，但缩短读取间隔不能恢复未导出的细节。参见[Linux proc文档](https://docs.kernel.org/filesystems/proc.html)。

新模式使用内核调度事件时间，Python读取晚一些不会把事件发生时间改成读取时间：

```text
启用独立trace实例 ─ 启动原负载 ─ 流式读idle进出事件 ─ 排空负载 ─ 停止并检查丢失
                              └─ 原100ms /proc/stat采样继续 ─┘
                                                        ↓
                 按事件时间还原每CPU忙闲区间 → 5ms积分 → 自动出表/曲线
```

每池窗口占用率为：

`100 × Σ该池CPU在窗口中的非idle驻留时间 / (该池逻辑CPU数 × 窗口实际时长)`。

默认5ms窗口用于观察20ms左右的释放过程；可设`--cpu-bin-ms 10`或`2`。窗口越小，曲线细节和抖动越多，不代表时钟精度、零扰动或统计准确性随之提高。文本时间戳通常精确至微秒；记录实际格式，不强称纳秒测量。曲线由时间戳重建，不依赖Python精确每5ms醒来。

## 3. 采集口径和安全边界

- 使用唯一的tracefs instance，仅启用`sched:sched_switch`且过滤`prev_pid==0 || next_pid==0`，只记录CPU进出idle。非idle任务之间切换不需要输出。使用`mono`时钟与应用`CLOCK_MONOTONIC`对齐。依据见[内核独立实例与时钟文档](https://docs.kernel.org/trace/ftrace.html)、[事件过滤文档](https://docs.kernel.org/trace/events.html#event-filtering)。
- 仅配置和清理自己的实例，不修改全局跟踪、内核参数、全局trace options，也不自动挂载tracefs。不需要额外安装BCC、bpftrace或Python绘图库。
- 结果严格称为**非idle任务驻留率/调度口径CPU占用率**。idle任务期间的中断处理未被单独计入，非idle期间的中断包含在驻留时间中，虚拟机steal未校正；因此与`/proc/stat`的execution百分比并非完全同一口径。保留二者并排解释。
- 第一条idle切换的prev状态可用于回推该CPU捕获起点的状态。全程没有事件的CPU标未知，不能当0%；汇总不能缩小CPU分母来掩盖缺失。
- 停止后排空缓冲，检查`overrun`、`commit overrun`、`dropped events`、剩余`entries`和文本LOST。丢失、权限不足、时钟不可对齐或采集异常均显式失败，不自动降级成“5ms的100ms数据”。
- 不支持测试期间CPU热插拔；记录前后online集合并检查，测试期间应保持CPU集合不变。

过滤减少记录量，但调度切换仍执行跟踪过滤，读取和落盘也有成本。输出父观察进程CPU时间、读取线程CPU时间、原始文件大小及质量信息；这些成本不包含全部内核跟踪开销，不能据此宣称零扰动。若要比较策略，三个策略都使用相同采集开关；不要把新开启跟踪的一轮和3004当作唯一变量相同的严格配对。3004的无高精度跟踪结果继续保留为原策略证据，新曲线主要用于解释CPU波形。

## 4. 测试机操作：自动采集和出图

从阶段3仓库根目录执行。当前已有通过3004验证的`build-independent/independent_load`即可；不需要重新构建Folly。本次修改采集/分析脚本及测试注册，不修改C++负载算法。

```bash
git pull --ff-only origin experiment/stage3-load-balance-20261115

python3 experiments/independent/scripts/cpu_trace.py check \
  --cpus 0-47,192-239 --output results/cpu-trace-check
```

该检查约0.2秒，不运行业务demo。需要能创建tracefs实例并启用事件，通常使用测试机已有root权限。检查应显示`status: ok`、`cleanup_complete: true`和零丢失；它只验证捕获能力，完整曲线覆盖率在正式数据分析时再检查。失败时保留错误输出，不要把关闭采集作为高精度验收成功。

随后生成新计划，沿用3004负载，仅增加观测：

```bash
python3 experiments/independent/scripts/request_wave.py plan \
  --deployment arm --out results/request-wave-arm-cpu5 \
  --cpu-trace --cpu-bin-ms 5

python3 experiments/independent/scripts/request_wave.py run \
  --plan results/request-wave-arm-cpu5/plan.json \
  --binary build-independent/independent_load
```

这会自动执行home、source_only、guarded各一次，自动启停采集，生成原有时延报告及高精度CPU曲线。保持原CPU隔离条件；正式发压约三分钟，另有启动、排空、落盘及分析时间。不要手工修改plan，也不要复用3004目录。已有成功结果按指纹校验后跳过；失败结果保留，不静默覆盖。

如果测试机有**确定空闲且允许使用的实验CPU集合之外的CPU**，可在生成计划时加`--observer-cpus CPU列表`，让Python观察进程及读取线程在那组CPU运行。此时父命令不要用`taskset`限制到96个实验CPU。运行器会在启动子进程时把demo明确限制回`0-47,192-239`，不会给worker增加资源；观察CPU也不会被加入利用率分母。未指定则继承父进程亲和，实际mask会记录。不要未经确认就占用其他业务CPU。

## 5. 输出怎么看、如何在终端收集

每个run目录保留`cpu_trace.txt`和`cpu_trace_status.json`，并进入manifest哈希校验。`cpu-curves`目录保存顶层`report.txt`/`summary.json`，每策略生成：

- `cpu_curve.csv`：固定时间窗的pool/union占用、原始容量分母、覆盖与有效性。
- `cpu_full.svg`、`cpu_zoom.svg`：全程曲线和局部放大，包含实际初始投递跨度标记。
- `cpu_aligned.svg`、`aligned_home_curve.csv`：按home请求时间对齐的平均曲线，明确是多请求平均波形，不是某一次请求的瞬时曲线。
- `aligned_windows.csv`：0–30、30–100、100–200、200–300、300–420ms五个相对请求窗口的占用统计，以及样本/覆盖信息。
- `report.txt`：每组CPU的平均占用率、完整5ms窗口峰值、≥90%窗口时长占比及最长连续高占用窗口长度。90%只是诊断定义，不进入调度决策；缺失窗口不补零。

后四个区间是“突发之后的观测窗口”，**不能预先命名为稳态**。还要看曲线是否已下降并形成平台；home组CPU也可能运行其他请求或分流来的任务，不能把它的全部CPU时间归给当前请求。分流后，本request的工作也可能在其他组执行，所以同时看home和全体CPU。

先确认质量，再看初始20–30ms是否饱和、饱和延续多久、后段占用是否趋于平稳。原始时间曲线与对齐平均曲线一起看，平均曲线可能平滑不同请求的峰值。保留窗口后完成的任务；对齐窗口不完整时必须看覆盖说明，不能把缺失补零。

测试机不能上传文件时，只需打印两个短报告，原始trace/CSV/SVG继续保留在测试机：

```bash
cat results/request-wave-arm-cpu5/cpu-curves/report.txt
cat results/request-wave-arm-cpu5/analysis/report.txt
```

重新分析已有高精度捕获不会再次发压：

```bash
python3 experiments/independent/scripts/analyze_cpu_trace.py \
  --plan results/request-wave-arm-cpu5/plan.json \
  --manifest results/request-wave-arm-cpu5/run_manifest.json \
  --output results/request-wave-arm-cpu5/cpu-curves
```

事件在测试期间实时记录到文件，正式SVG在运行结束后自动生成。当前不提供毫秒刷新的浏览器界面。

## 6. 已完成的验证

开发机独立工程构建成功，13组CTest通过；覆盖事件积分、缺失CPU、丢事件、时间与覆盖校验、采集失败清理及原有运行器行为。还在4逻辑CPU开发机实际完成三个策略的自动采集和分析，均无丢事件、CPU覆盖完整、独立trace实例正常清理。另一次实测确认指定观察CPU后，demo仍严格限制在原实验CPU集合内。

这些开发机测试用于验证采集与分析功能；测试期间存在其他工作，不能用其CPU曲线或时延作为ARM策略收益证据。

ARM测试已通过用户提供的3005日志完成验证，见[CPU与时延联合报告](ARM_3005_CPU_CURVE_REPORT.md)：三策略均96/96 CPU覆盖、9999/9999个完整5ms窗口有效，自动采集和分析成功。观察进程使用18–23 CPU秒，不包含全部内核跟踪开销。本轮无需重跑；以上命令保留供未来复现。

# 本地分析 scheduler 文本 trace

`scripts/analyze_sched_trace.py`用Python标准库逐行读取 `cat trace` 保存的文本，或 `trace-cmd report` 的标准输出。它不修改追踪设置、不连接外部服务、不上传原始trace。终端输出适合手工回传；可选JSON包含更完整的审计和窗口统计。

这是内核线程调度分析，不是Folly任务追踪。没有Folly提交/执行标记，就不能直接恢复算子排队、request完成时延或图的关键路径。

## 1. 先提供真实的线程归属

推荐提供与本次采集同时取得的CSV，至少包含以下两列：

```csv
tid,pool
1234,executor-A
1235,executor-A
2001,executor-B
```

线程归属不得根据待验证结论倒推。TID可能被重用，线程名可能截断或修改；旧进程的TID表不适用于新trace。没有映射时，工具输出 `[unmapped]` 的观察统计和线程候选清单，不推断系统一定有四池或六池。

```bash
python3 scripts/analyze_sched_trace.py /path/to/trace.log \
  --tid-map /path/to/tids.csv \
  --cpu-mask 0-47,192-239 --coverage partial \
  --json /path/to/sched-summary.json
```

`--cpu-mask`是十进制CPU列表，用于声明**实际采集覆盖范围**，不是tracefs的十六进制 `tracing_cpumask`。它不事后过滤文件中的记录；若记录CPU不在声明范围内，会保留记录并报告矛盾。文件中没出现某CPU的事件，不等于证明该CPU未采集或确实idle。

`--coverage`默认为 `unknown`。只有确认事件启用、CPU覆盖、过滤器及采集连续性时才声明 `full`；这个参数是人的声明，工具不会因此忽略已发现的丢失和矛盾。

无法取得映射时，可以先仅查看候选：

```bash
python3 scripts/analyze_sched_trace.py /path/to/trace.log --candidate-limit 60
```

也支持明确标记为heuristic的名称规则，按payload中的目标线程名匹配：

```bash
python3 scripts/analyze_sched_trace.py /path/to/trace.log \
  --name-pattern 'candidate-executor=^executor-' \
  --name-pattern 'candidate-io=^io-'
```

CSV优先；多个正则同时匹配的线程仍归为unmapped并记录歧义。首次归属确定后，名称变化不会悄悄将TID重新划到另一池。正式池间定量比较优先用CSV。

## 2. stdin、时间窗和内存上限

```bash
trace-cmd report -N -t -i /path/to/trace.dat | \
  python3 scripts/analyze_sched_trace.py - \
    --tid-map /path/to/tids.csv --cpu-mask 0-47,192-239 \
    --start-seconds 0.2 --duration-seconds 9.5 \
    --json /path/to/sched-summary.json --include-windows
```

时间偏移以首个可解析的支持事件时间为原点，不假定trace内容正好覆盖原计划的10秒。默认窗口大小为1、5、10、30ms，可通过 `--bin-ms 1,5,10,30`调整；窗口是固定分桶，不是滑动窗口，跨桶突发可能被拆开。

存储上限由线程数、池数和窗口数决定，输入事件数不会累积为一张原始事件列表：

| 参数 | 默认值 | 行为 |
|---|---:|---|
| `--max-threads` | 100000 | 线程状态上限；已映射TID预留名额，背景线程不能将其挤掉；跳过事件有审计 |
| `--max-bins` | 20000 | 每种宽度的窗口上限；默认1ms时最多分析20秒，之后仍流式读入但排除 |
| `--max-window-cells` | 400000 | 所有池与窗口宽度的实际单元总上限；达到后停止新增单元并标记窗口统计不完整 |
| `--candidate-limit` | 20 | 仅控制终端候选输出行数 |
| `--include-windows` | 关闭 | JSON可包含逐窗数据；补零展开超过单元上限时省略series并说明 |

CSV映射不超过8192个TID时，映射线程的逐窗unique计数使用精确整数位图。其他线程使用64寄存器HyperLogLog，约13%标准误；每窗/每池均报告两组 `unique_*_method(s)`，不能把HLL近似值用于判断20与22这类细微差别。工具严格分开 `unique_event_tids`（该窗事件涉及的线程）和 `unique_oncpu_tids`（已配对onCPU区间覆盖该窗的线程），不混称“活跃线程”；后者也不是同时运行数。相关性使用独立的onCPU集合，不用waking涉及集合制造结构性相关。

等待分位数采用固定2049格对数直方图，输出上边界近似，普通范围的桶宽约2.2%；均值、计数和最大值不是直方图估计。窗口计数和核当量的p50/p95/p99按有界的完整窗口直接计算；最后不足一个窗口的桶不参加这些分位及相关性。

## 3. 事件语义和重建规则

核对依据为[Linux v6.6 scheduler tracepoint定义](https://github.com/torvalds/linux/blob/v6.6/include/trace/events/sched.h)。`sched_waking`发生在唤醒方上下文；`sched_wakeup`记录实际唤醒为可运行状态，记录位置不保证仍在原唤醒方。事件头的 `[CPU]`是**事件记录CPU**，payload的 `target_cpu`是目标，二者不能混用。

工具处理带key/value payload的五类标准文本事件：

| 事件 | 用途 |
|---|---|
| `sched_waking` | 计数和目标线程活动；不作为成功唤醒或等待起点 |
| `sched_wakeup`、`sched_wakeup_new` | 无歧义时建立成功唤醒到switch-in的等待起点 |
| `sched_switch` | 同CPU连续匹配switch-in/out得到onCPU区间；R/R+离开建立runnable等待 |
| `sched_migrate_task` | 记录迁移并跟踪等待目标；跨出已知覆盖范围或范围不明时删失等待 |

`prev_state=R+`可明确记录抢占；`R`仅说明离开时仍可运行，也可能是yield，不能一概称为抢占。非R状态计为nonrunnable switch-out，其中包含睡眠、阻塞、停止、退出等不同状态，不统一解释为RPC等待。

onCPU区间必须由同CPU上的匹配switch-in和switch-out闭合。成功wake等待必须有后续switch-in；重复wake、目标CPU不一致、明显丢失或矛盾的配对不补值。`sched_waking`次数既不是成功wake次数，也不是不同线程数，更不是Folly子任务或业务request数。

`observed_demand`是窗口内**已配对onCPU时间+已配对runnable等待时间**除以窗口墙钟，是观察到的线程需求核当量。一个线程等待3ms后执行2ms，会贡献相应区间；未闭合和被删失部分不猜测补齐。它不是每CPU完整runq，也不是Folly任务队列深度。

## 4. 丢失、边界和覆盖不足如何降级

依据[ftrace文档](https://docs.kernel.org/trace/ftrace.html)，采集CPU掩码决定事件发生在哪些CPU时可被记录；正在写入时直接读 `trace`也可能得到不一致内容。工具检测不到所有采集错误，因此必须保留采集配置和内核版本。

- 首次看到switch-out而无switch-in，视作左边界未知，不从窗口开始补onCPU。
- 选定窗口后仍继续流式读取后续文本，允许后续switch-out闭合跨右边界区间，再裁剪到窗口；不把已有闭合证据丢掉。到EOF仍未闭合且涉及窗口的运行/等待锚点标为右删失，不向结束补值，也不纳入完整区间分位。
- 选择非零start时，窗口之前的记录仍用于建立状态；闭合区间可以裁剪到窗口，但被裁剪的完整等待不进入时延分位。
- `LOST ... EVENTS`、无法解析的支持事件、时间戳倒序会使已有区间锚点失效。工具不对任意大输入排序；应使用可跨CPU比较的时钟和按时间合并的输入。
- CPU switch链矛盾、运行中又出现迁移/wake等无法可靠解释的状态会保守失效；可能牺牲有效样本，避免将未知区间伪造为连续执行。
- `entries-in-buffer/entries-written`显示历史覆盖时，报告保留文本可能短于计划采集时间；它不等于准确知道窗口内部丢了多少条。
- 掩码不全时，未看到switch-in的wake是删失，不是“无限等待”。确认所记录CPU的switch链完整时，观测onCPU可作为全局总量的一部分；同时有丢失/过滤时，不保证它是严格数学下界。

审计数量会直接打印。不要只摘录均值和p99而丢掉 `audit`、覆盖范围、映射方式与候选清单。声明full也不等于无丢失认证。

## 5. 读什么结果，能回答什么问题

终端按池打印已配对onCPU毫秒/核当量、wake到运行和R离开到运行的均值及近似分位、固定窗口微突发峰值、事件涉及/onCPU两类unique及主要审计。终端直接打印lag=0的waking→unique-onCPU、waking→onCPU、unique-onCPU→onCPU、wakeup→onCPU相关性，分别展示完整窗口和任一变量非零的activity窗口；后者有选择偏差，不能作为无偏改进。undefined会保留n和原因。JSON额外提供不同lag的窗口相关性；正lag表示该池第t窗wake数与第t+lag窗的onCPU不同线程数、核当量、需求或已完成wake等待均值比较。

相关性至少需要20个有定义的窗口，否则输出空值；20是输出门槛，不是统计充分的保证。相邻窗口相关、配对等待只包含成功闭合样本、10秒采样很短，也可能同时受请求扇出驱动。这里不给因果结论、独立样本p值或可信的生产阈值。

本工具可以支持：某池是否出现短时唤醒集中；随后有多少不同线程被涉及；可观察的CPU使用和runnable等待是否同步升高；平均CPU低是否掩盖局部微突发。它不能单独证明数据cache miss、Folly队列堵塞、请求p99变化、哪个二级调度策略最优。这些需要与同一时间轴上的业务/Folly指标及必要的硬件计数器结合。

如果缺映射、CPU掩码不全或成功wake事件未启用，先报告已能证实的事件事实及删失，不要把缺失字段补成“某四个线程池的完整负载”。

## 6. 验证

```bash
python3 tests/sched_trace_test.py
```

测试使用合成时间线，覆盖睡眠与抢占、事件CPU/目标CPU区分、迁移、受限掩码、边界、重复wake、丢失、乱序、带空格名称、映射、stdin以及大事件流存储上限。它验证解析和统计逻辑；仍需用目标内核实际trace确认文本格式和采集设置。

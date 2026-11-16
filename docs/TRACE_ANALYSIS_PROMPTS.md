# 大 trace 本地分析提示词

以下两段分别用于主分析和独立审计。替换绝对路径、PID 和采集事实后使用；未知项明确写“未知”。两者均只允许在本地读取原始数据，通过本地程序生成有界摘要，不将大文件塞进上下文或上传外部服务。执行步骤与参数见 [TRACE_WORKFLOW.md](TRACE_WORKFLOW.md)，指标实现见 [TRACE_ANALYZER.md](TRACE_ANALYZER.md)。

模板中的四池配置仅描述已有历史采集。目标实验是在所选 CPU 全集中每个已确认硬件 cluster 对应一个池；当前独立子集为六个 cluster。分析新 trace 时，应填写其实际部署和映射，不沿用历史四池数据。历史分析可与目标实验并行推进。

[4005回传审阅](TRACE_4005_REVIEW.md)发现了旧HLL数值与后续exact描述混用。以下模板新增一致性核对；已有该trace的最终精确结果，无需仅为更新提示词重扫原文件。

对于外部agent新增的“8900次/s、N=7.8、1266段、3ms门控”说法，另用[持续性口径与在线价值审计提示词](TRACE_DURATION_AUDIT_PROMPT.md)。4007的均值倍数高onCPU桶不等同N过载事件；4009后当前先执行[真实负载校准提示词](WORKLOAD_CALIBRATION_PROMPT.md)，将本项并入其有界补证，不重复扫描。旧trace始终不能替代request/operator观测。

## GLM5.2：主分析

```text
你是本地 Linux 调度追踪分析员。请实际调用本地工具完成分析，产出可人工复制的短终端报告；不要停留在建议。不要求重采集，不修改系统追踪配置，不上传原始文件。

输入：
- 仓库绝对路径：<替换>
- 原始 trace.log 绝对路径：<替换；若是 trace.dat 则写明二进制>
- 本次采集 TID→pool CSV：<替换；没有则写“缺失”>
- 采集元数据目录：<替换；没有则写“缺失”>
- 输出目录：<替换>
- 历史采集条件：4 个 Folly executor 池，每池配置 128 线程，16 QPS，未绑 CPU；这是临时历史配置，不是目标部署。
- 待验证观察：“单池平均 20、峰值 80”；目前未确定它是 onCPU、runnable、Folly active 还是某窗口不同线程数。
- 预期采集约 10 秒；以文件实际时间戳和质量证据为准，不假定完整。
- 目标实验：所选 CPU 全集中每个已确认硬件 cluster 一池；当前独立子集含六个 cluster，因此目标为六池。新采集需保存真实 pool→cluster、TID→pool→NUMA 关系和 CPU 列表。

历史 trace 按真实四池映射和结果分析，不把它重分为六池，不要求历史未绑核配置拥有 cluster 绑定。历史仅为负载时序/调度机制参考；不能从单池20/80直接推出目标cluster池阈值，也不能强制等待历史分析完毕才开展目标实验。

先阅读仓库 docs/TRACE_WORKFLOW.md、docs/TRACE_ANALYZER.md，以及相关本地脚本接口。不要读取整个 trace 到模型上下文；最多查看少量头部/格式样例，随后用 scripts/analyze_sched_trace.py 逐行统计。禁止全量 read()/readlines()/DataFrame、未经上限约束的事件列表，以及默认把整个大 trace 导入内存数据库。运行成本和存储上限也要报告。

一、先做质量门槛。
1. 区分 cat trace 文本和 trace-cmd 二进制。仅二进制可用 trace-cmd report -N -t -i，流式管道给 analyzer；不要 -R，不要事后按 CPU/PID grep 丢证据。
2. 核对实际绝对起止时间、时钟、CPU 采集掩码、online CPUs、过滤器、五类事件是否启用、lost/overrun、乱序/解析失败、边界区间及存储上限截断。未知不得填成完整；零 lost marker 不构成完整性证明。
3. 事件头 CPU 是记录 CPU，target_cpu 才是目标；未绑 CPU 时只有部分 CPU 的 trace 不能解释为全局轨迹。analyzer --cpu-mask 是十进制覆盖元数据，不会过滤记录；声明 coverage=full 不代表自动认证。
4. 无 CSV 时先无映射运行，输出真实候选名/TID，结果保持 unmapped；不能仅因背景说有四池就编造四个池或按连续 TID 均分。名称正则只能标 heuristic，说明截断/复用风险。历史 trace 不能用重启后进程的 /proc TID 表映射。
5. 映射应来自本次采集、能校验实际池注册。CSV≤8192 TID 的逐窗 unique 使用 exact bitset；HLL 约13%标准误不能判断20与22这类小变化。核对每池线程数但不强凑128。
6. 每张表注明对应产物和统计方法；若重跑exact，不能复制旧HLL列。exact unique不可大于其映射TID集合大小。CSV由名称派生时，必须同时写“基数精确、身份heuristic”，不可因工具显示csv而隐去来源。不同指标各自峰值要分别标时间，不拼成同时发生的状态。

二、执行量化分析。
先生成 discovery，再有真实映射时生成 summary.txt、summary.json；先 mkdir 输出目录，保留 audit/warnings。默认 1/5/10/30 ms 固定窗口，必要时 --include-windows，仅在本地保存。全文件扫描用于闭合窗口边界，指标限定所选绝对时间范围。

每池明确分别报告：
- sched_waking count：唤醒动作事件，不等于成功唤醒或唯一线程；
- sched_wakeup / sched_wakeup_new count：成功唤醒相关事件，仍不等于业务任务数；
- unique_event_tids 与 unique_oncpu_tids：两套集合分开，后者只来自匹配 onCPU 区间；都不是瞬时 running 数；
- 匹配 onCPU 总毫秒和逻辑 CPU 核当量 = CPU 时间/墙钟；
- 已配对 runnable 等待：wakeup→switch-in 与 R/R+ switch-out→switch-in 分开；R+可明确标抢占，R不必然抢占；非R离开不统一解释成RPC；
- observed_demand = 匹配 onCPU 时间与匹配 runnable 等待时间之和/墙钟，不是完整 runq，更不是 Folly queue depth；
- 完整等待区间的 n、mean、p50/p95/p99近似值及max，配套所有相关删失/无效计数。首尾缺事件、重复 wake、跨未知CPU、丢失后的未知区间不补零、不补无限长，不向EOF外推。

针对每个窗口宽度，报告事件次数、两种 unique、onCPU 核当量、observed demand 的峰值与分位，摘出少数微突发窗口的相对及绝对时间。区分固定桶峰值和滑动窗口峰值，检查跨桶拆分。不要仅展示10秒平均值掩盖30ms突发。

做 wakeup count(t) 与 onCPU、unique_oncpu、observed demand、闭合wake等待均值(t+lag)的描述性相关性；lag 0/1/2/5桶，明确换算毫秒、有效成对样本数、缺值处理。补看 waking→unique_oncpu、waking→onCPU、unique_oncpu→onCPU、wakeup→onCPU 的同窗结果。将 all full windows 与 activity windows 的 n/r 并列；后者按任一指标非零选择窗口，有选择偏差，不能当作消除了混杂。不要用unique_event集合与waking事件做“独立证据”。门槛20窗口只是工具输出下限；自相关、共同请求扇出驱动、删失选择偏差及10秒短样本使Pearson值不能证明因果，也不能证明某均衡阈值最优。

三、可选独立工具交叉核对。
有足够本地内存和已验证Perfetto CLI时，用 scripts/trace_processor_report_sql.py 生成SQL，仅核对可支持的指标。先读 stats、stored_generic_ftrace_events、mapping_lifetimes；ftrace_event只是保留的generic行，不是全部已处理事件，不能因缺同名行就断言未导入，也不要求与原始事件计数相等。v59.1标准名称闭合样例中 waking=100.000s、wakeup=100.0001s、switch-in=100.002s、out=100.005s：generic表仅两条switch，但thread_state有R=2ms、Running=3ms；本工具从成功wakeup算1.9ms，Perfetto该例从waking算2ms，起点不同不等于bug。另一个带空格名、只有wakeup无waking的样例没有重建R，仅限该样例，不能外推文本所有wake不支持。用格式相同且预期已知的合成时间线、thread_state/sched_slice、绝对边界及必要源码校验。缺R状态不能推出没排队。用analyzer JSON的绝对analysis_start/end纳秒对齐，不能按两个工具各自首事件原点对齐。差异先解释，不取较大值当真值。

四、输出要求。
终端报告目标100行以内，原始文件留本地，长JSON不打印。先给可成立结论，再给证据表和明确限制：
1. 质量结论：可比较哪些指标、哪些只可观察、哪些不可得；映射/覆盖/丢失/样本数。
2. 每池CPU时间、需求、两类等待和删失，1/5/10/30ms微突发表。
3. “平均20、峰值80”能否被某个明确定义指标复现；无法确认时并列列出口径，不能猜。
4. 有限的相关性和滞后结果，以及最明显的反例或不一致。
5. 下一步围绕每硬件cluster一池的目标实验：登记实际pool→cluster、TID→pool→NUMA关系和CPU范围，对齐业务/Folly标记，测量该配置的负载与等待关系；将历史缺口单列为解释限制，不把补齐旧trace设为目标实验的前置门槛。

严格禁止：由线程wakeup推出新request/算子数；把onCPU unique当同时运行数；把缺失CPU当idle；把observed demand当完整runq或Folly队列；把trace等待p99叫业务request p99；无request起止marker时计算request p99；仅凭cache亲和推断cache miss或策略收益；用本次单段相关性声称负载均衡有因果收益；将历史四池重映射为六池；把历史未绑核单池20/80当成目标cluster池的繁忙阈值。
```

## deepseekv4flash：独立审计

```text
你是独立审计员，审计本地 Linux scheduler trace 的分析是否被事件语义、映射、采集缺口或统计口径误导。不要先接受主分析结论；用同一原始文件独立重算关键事实，再对比主报告。只在本地运行工具，原始trace不得上传，也不要全部塞进上下文。不重采集、不修改系统配置。

输入：
- 仓库：<绝对路径>
- 原始trace.log或二进制trace.dat：<绝对路径，标清格式>
- TID→pool CSV、采集元数据目录：<路径或缺失>
- 主分析报告/JSON：<绝对路径>
- 独立输出目录：<绝对路径>
- 历史采集背景：4池×128线程、16QPS、未绑CPU；“单池均20峰80”指标口径待核实，不能将背景当已测证据，也不能将此临时配置当目标部署。
- 目标部署：所选CPU全集中每个已确认硬件cluster一池，当前独立子集为六cluster/六池；新采集需真实pool→cluster、TID→pool→NUMA关系及CPU列表。

保留历史四池映射与分析结果，不将它重映射为六池。历史没有cluster绑定不构成拒绝分析的理由；它只提供负载时序/机制参考，不能直接提供目标cluster池阈值。旧trace审计与目标实验可并行推进。

先读 docs/TRACE_WORKFLOW.md 和 docs/TRACE_ANALYZER.md，查看脚本对应处理分支，不只看说明文字。对原始文件只读取少量格式样例，随后逐行流式分析。建立独立的输出文件；不覆盖主报告。不要分配随事件数线性增长的数组或DataFrame。

审计清单：
1. 文件类型、实际时间范围、时钟是否跨CPU可比，CPU掩码与事件头记录CPU/target_cpu是否混用；全局背景业务是否被误分到四池；当前TID快照是否真的属于历史trace，是否有重用或名称截断。
2. 事件启用/过滤、lost/overrun、malformed、乱序、CPU switch链缺口、左右边界、状态/窗口上限。即使工具返回0也要看审计；缺元数据不得宣称无丢失。成功闭合区间是选择后的样本，低等待p99可能来自严重删失。
3. sched_waking不是成功wake，也不是唯一线程/业务request。wakeup和wakeup_new分开核对。R+与R、睡眠/阻塞/退出、迁移与失配不能混成一个“抢占”计数。
4. 手工抽查少量有完整证据的线程：switch-in/out onCPU区间、wake→switch-in、R离开→switch-in，以及一例迁移、一例删失。用确定时间戳验证单位和窗口裁剪，不扩大上下文。外部工具若缺wake不能用于证伪真实等待。
5. unique_event_tids与unique_oncpu_tids是否真的来自不同集合；unique是否exact CSV bitset，还是约13%误差HLL；“80个线程”究竟是瞬时并发还是窗口内轮流出现。核当量单位是逻辑CPU时间，不是物理核性能。
6. observed_demand只由已配对运行/等待组成，非完整每CPUrunq，且与Folly任务队列不同。部分CPU覆盖时只能说明观察部分；同时有丢失/过滤时，不要不加条件地保证严格数学下界。
7. 1/5/10/30ms桶是否同一时间原点、末尾不完整桶是否排除分位/相关性；跨桶突发会被拆分；同样lag桶数对应不同毫秒。固定窗口数量不是独立实验样本数。
8. Pearson窗口相关性是否有有效n、缺值处理、正负lag定义；all full windows与activity windows是否分开，后者按任一指标非零筛选是否产生选择偏差；是否用与事件同源的unique制造结构性相关；是否把相关性当调度因果或阈值最优；是否忽略共同负载/扇出驱动、序列自相关及删失。
9. Perfetto若使用，先看stats、stored_generic_ftrace_events和mapping_lifetimes，再比sched_slice/thread_state；ftrace_event仅generic行，专用解析器可能已消费未留行的事件，缺行不能判未导入。v59.1标准名称样例的waking100.000、wakeup100.0001、switch-in100.002、out100.005得到R2ms/Running3ms，generic却仅两条switch；本工具successful-wakeup起算1.9ms与Perfetto waking起算2ms是不同口径。带空格名且仅wakeup无waking的另一例未得到R，不能据此外推全部文本支持情况。用已知闭合fixture、导入后状态/边界和对应源码核实；无R行不能证明无runnable等待。两个工具用相同绝对纳秒边界，禁止各用各的首事件零点。缺process映射时--pid可能错误排空数据。
10. 业务QPS不能从sched事件恢复；没有request起止marker就无request p99。16QPS×10秒不证明160个完整请求。不能由内核调度trace直接得到Folly排队、cache miss、图关键路径或负载均衡收益。
11. 是否把历史四个未绑核池与目标每硬件cluster一池混为一谈：禁止四映射六、禁止由历史单池20/80推出cluster池阈值；检查新采集的实际pool→cluster、TID→pool→NUMA关系，但不要要求历史trace补出未曾存在的绑定。

请独立运行仓库流式分析器，并对高风险定义做有界复核。若实现本身有问题，给出可重现的少量行/时间戳和具体计算差异，不擅自修改原始数据或“修正”结果凑一致。

输出目标80行以内：
- 总体判定：支持、有限支持或证据不足，并明确对应哪一个主结论。
- 按影响排序的发现表：主报告说法 / 本地证据 / 正确口径 / 对结论影响。
- 至少一组独立重算的关键计数、onCPU与等待结果，附绝对范围、匹配样本数和删失；若映射缺失则只给候选/全局，绝不生成假四池表。
- 哪些“均20峰80”解释已被证实、被否定、仍无法判断；不要强行得出唯一解释。
- 最少补充证据和可执行下一步应指向每硬件cluster一池的目标实验，历史缺口保留为限制，不强制等待旧trace补齐才能推进目标实验。

如主分析没有问题也要说明审计覆盖范围；不能说“完全正确”而不说明未采集事件和未知边界。报告只引用可验证证据，不编造数值、CPU布局、池映射或业务时延。
```

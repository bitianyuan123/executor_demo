# 阶段3修订：短CPU峰值与持续背景负载

**3006已完成home，本轮先执行离线检查，不再执行下方的计划生成、编译或发压步骤。** [3006报告](ARM_3006_SHORT_PEAK_REPORT.md)记录了约10ms短峰及随后约50%背景，也保留碎峰比例13.833%导致的原严格验收失败。更新脚本后只需：

```bash
git pull --ff-only origin experiment/stage3-load-balance-20261115
python3 experiments/independent/scripts/inspect_short_peak.py \
  --plan results/short-peak-arm/plan.json
```

脚本打印约50行摘要，并写入独立`short-peak-inspection`目录；检查83个碎峰是否主要在20ms内，或存在较晚第二个峰。它不会修改原计划、原分析结果和放行状态，也不会启动负载。保留终端输出供复核。以下为首次执行时的冻结设计与操作记录。

真实业务的新信息是CPU冲高约5–20ms，而3005的home持续高占用约175–185ms。用户已说明5–20ms是粗略观察，尚未确定高占用阈值。**旧结果保留为长峰场景证据，不能据其76%的任务p99收益推荐真实业务参数。** 本轮先检查负载形状，再比较策略，不新增亲和性/线程数/阈值矩阵。

## 1. 为什么必须改CPU工作量

旧初始80项平均25ms纯CPU，总共2000 CPU-ms，即使16个逻辑CPU全部用于这批任务，也至少需要125ms；后续任务继续到来使高占用更久。把投递窗口从20ms改成5ms，无法消除这2000 CPU-ms。也不能把所有项都缩成1ms，那会让总需求由50%降到约2%。

修正方式是：**初始大量短CPU片段形成短峰；后续任务承担剩余工作量，平摊到同home两次请求之间。** 两阶段独立归一化，不能全体再按同一个均值重缩放。A/B使用同一计划、CPU预算和观测；CPU不用于反馈发压或路由。

## 2. 第一版冻结负载

仍是6池、128worker/池、16逻辑CPU/池，授权CPU并集`0-47,192-239`；12request/s，home轮询。不同home错开83.333ms，同home周期500ms。本轮只修正负载，不同时改线程数、亲和性或阈值。

|参数|旧长峰|新短峰候选|
|---|---:|---:|
|初始项数|80|80|
|初始投递窗口|20ms|1ms，最后一项在0.9875ms|
|初始每项平均CPU预算|25ms|约1.020ms|
|初始总CPU预算/请求|2000 CPU-ms|81.6 CPU-ms|
|后续项数|80|80|
|后续投递时间|从20ms起，400ms窗口|从1ms起，500ms窗口，最后一项在494.75ms|
|后续每项平均CPU预算|25ms|约48.980ms|
|每请求总CPU需求|4000 CPU-ms|4000 CPU-ms|
|全局平均名义需求|50%|50%|
|后续背景名义需求/池|约31.25%，且有投递间隙|约48.98%，每500ms延续一轮|

后续项按固定时间释放，无DAG依赖。上一request的后续计算可与下一request的初始任务重叠，背景不是等初始峰结束才启动。预算仍有±50%随机波动；exact保证整轮initial和steady集合各自的均值，不保证每个request完全相同。没有RPC、共享业务数据及700–800ms长尾。

本轮模拟的是CPU需求形状，1ms片段不是对真实100ms算子的复现。初始与后续单项成本相差约48倍，N对二者等权，因此这次必须重新验证N/C策略，不能预设它最优。

## 3. 预算推导与边界

设每池容量C，同home请求周期P秒，目标平均占用ρ，期望峰宽τ秒。每请求总CPU需求`D=C×ρ×P`；若初始CPU工作为B，后续平均需求为`(D−B)/P`。

流体近似为`τ≈B/[C−(D−B)/P]`，解得`B≈C×(1−ρ)×τ/(1−τ/P)`。代入C=16、ρ=0.5、P=0.5s、τ=0.010s，初始约81.633 CPU-ms；取整成80×1020μs，剩余分给80×48980μs，仍为每请求4 CPU秒。取整后的模型峰宽约9.996ms。

推导忽略调度粒度、唤醒/投递成本、瞬时背景和SMT吞吐，**不是实际峰宽承诺**。`--peak-target-ms 10`仅用于生成计划时分配预算，不是运行中实时控制器。当前验收研究平均约50%负载，本轮保持`--target-utilization 0.5`；不要用改变平均压力的方式让峰宽过关。

为避免生成与验收条件必然矛盾的计划，short-peak只接受5–20ms的模型峰宽、45%–55%的名义需求、超过30ms的同home周期，以及不少于逻辑CPU数的worker。参数合法不保证实测通过；本轮不扫描这些参数。

## 4. home先验收，随后只做两策略

|策略|规则|
|---|---|
|home|全部新任务提交原home|
|source_only|提交前home的N/C>0.8时，选其他池中严格更轻的最轻者；无目标回home，无目标绝对cap|

沿用当前对initial和steady每项决策、允许跨NUMA的实现。不加guarded、不测none/NUMA、不扫阈值。第一轮home既是波形检查，也是A/B基线；通过后复用，不另做一次相同home。波形未通过则不启动source_only。

高占用临时按**cluster非idle驻留率≥90%**定义，这是合成实验预设口径，不是已确定的业务阈值。同一份trace另外输出80%、90%、95%敏感性，不额外运行、不事后挑容易通过的阈值。事件重建使用1ms积分窗口，同时保留100ms累计观测核对总量；积分窗口不等于测量准确度，需列覆盖、丢失及观察开销。机制依据见[内核事件文档](https://docs.kernel.org/trace/events.html)和[/proc说明](https://docs.kernel.org/filesystems/proc.html)。

逐request按实际首个initial提交对齐，检查近起点高位段、后30ms开始的背景，并保留逐波数据。不能仅用平均对齐曲线掩盖100–200ms长峰。默认90%口径检查包括：

- 完整CPU采集；完整波形样本至少30且覆盖率至少95%。
- CPU并集平均45%–55%；近起点高位波比例至少90%。
- 近起点高位段median在5–20ms、p90不超过25ms。
- 前30ms不应普遍出现碎裂的额外高位；后段背景应回落，不存在普遍长高位串。

所有细项由冻结计划和分析摘要原样列出，禁止运行中自动调整。本轮只验证合成短峰是否成立；真实阈值尚未知，不能称已经完成业务波形标定。分流后的CPU形态是结果，不要求也通过home形态门槛。

## 5. 测试机操作：先home，再续跑

在`executor_demo-stage3-balance`仓库执行，复用现有Folly：

```bash
git pull --ff-only origin experiment/stage3-load-balance-20261115
cmake -S experiments/independent -B build-independent \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/home/bty/rank_demo/folly/_install
cmake --build build-independent -j2
ctest --test-dir build-independent --output-on-failure

python3 experiments/independent/scripts/request_wave.py plan \
  --profile short-peak --deployment arm \
  --cpu-trace --cpu-bin-ms 1 \
  --out results/short-peak-arm

python3 experiments/independent/scripts/request_wave.py run \
  --plan results/short-peak-arm/plan.json \
  --binary build-independent/independent_load --only home
```

父进程不用taskset，runner在启动工作进程时约束到计划CPU并集。可选`--observer-cpus`只能填已确认空闲且位于实验并集之外的CPU，未知时不要猜。默认60秒请求生成、预热10秒，选取600个完整request、96000任务，迟到释放和完成均保留；这仍是任务统计，不是生产request尾延迟精度保证。

先打印home结果；不能上传文件时，仅贴这些终端摘要即可：

```bash
cat results/short-peak-arm/analysis/report.txt
cat results/short-peak-arm/cpu-curves/report.txt
```

只有形态通过才续跑：

```bash
python3 experiments/independent/scripts/request_wave.py run \
  --plan results/short-peak-arm/plan.json \
  --binary build-independent/independent_load
cat results/short-peak-arm/analysis/report.txt
cat results/short-peak-arm/cpu-curves/report.txt
```

runner复用已完成且身份匹配的home，检查波形后才启动source_only。不要手改plan绕过身份校验；旧3004/3005目录不会被覆盖。波形不符时先看原因，必要时新目录冻结一个修正后的负载，两策略共用，不能跨负载拼成A/B。不单独额外运行calibrate；这次home本身就是校准基线。

CPU图在`cpu-curves/home/`：`cpu_peak.svg`看首个完整请求附近真实40ms曲线，`cpu_aligned_peak.svg`看对齐平均的局部放大，`cpu_aligned.svg`覆盖完整同home周期；逐波峰宽在`request_waveform.csv`。平均曲线只帮助看形状，不能替代逐波持续时间分布。文本摘要会打印统计阈值和覆盖范围，无须上传图。

## 6. 分析重点与停止规则

先判负载形态，再判收益。S为提交入口到计算结束，W为开始前墙钟等待，X为回调墙钟时长，L从本项计划释放开始计时；Q是Folly队列，N是未完成回调，C本轮为16，均不是内核runq或CPU百分比。

initial、steady和all都必须报告S/L/W/X。全体p99可能被约49ms后续任务主导，因此不能只看all，也不能只挑initial的改善而隐瞒后续回归。N/Q桶按阶段解释，特别考虑N将1ms和49ms任务等权的局限。相关性和策略收益仍是两件事。

比较的是同一新负载下source_only相对home的变化，不能把旧155.828ms与新p99相比并归因为策略。批次最后完成时间也不等于DAG请求收益，因为本demo后续按约500ms固定释放。

波形合格而分流无收益或变差也是有效结论，可能说明峰很短、分流成本抵消收益或影响了后续工作；结束本轮，不自动加矩阵。波形不符只修正一个负载假设，不能“调到分流赢”。

## 7. 实现与验证范围

C++新增`--initial-cpu-us`、`--steady-cpu-us`，0继承`--cpu-us`；非零覆盖仅用于request-wave。任一覆盖开启后，exact按两阶段分别归一化，默认估计采用阶段标称值，不泄漏随机真实预算。未开启覆盖的旧生成逻辑保留。

开发机使用`--profile short-peak --deployment dev`缩小资源验证功能。无完整CPU事件证据只能记功能通过，不宣布短峰验收通过；ARM波形与收益须由测试机实际数据确认。本轮本地验证记录见[开发验证记录](SHORT_PEAK_VALIDATION.md)。

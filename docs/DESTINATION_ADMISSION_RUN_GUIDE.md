# 目标借入上限：12次有限对照执行合同

**本合同已由4027完成。** 当前不要求再次执行下方12轮；结果、取舍及现有CSV的只读补查命令见[4027分析](TARGET_V2_4027_REVIEW.md)。以下保留原实验复现方法，不意味着继续扩大矩阵。

4026已补齐旧请求后期与新首波同池相遇的证据，本轮无需再解析同一批历史轨迹，也不重新设计图。现在进行一次有干预变量的对照：**即时O策略相同，仅将投递到其他池的目标上限设为关闭、1.0、0.75。** 实现合同见[目标借入保护设计](DESTINATION_ADMISSION_EXPERIMENT.md)。

本轮回答：减少借入一个已有工作的池，能否降低旧请求后期等待；为此会牺牲多少首波并行、请求完成时延和CPU。它不直接验证源池“连续繁忙1ms才借出”，不认证生产阈值。历史四池不是本轮架构：仍是每个声明硬件cluster一个池，共六池。

## 固定负载、资源与唯一变量

| 项目 | 本轮合同 |
|---|---|
| 到达 | regular 12 QPS；每轮240请求；RR确定请求home |
| 图front80 | 第一波80条深度2的链，整波join后第二波4条深度2的链 |
| 图tail4 | 第一波80条深度1的链，整波join后第二波4条深度22的链 |
| 每请求业务工作 | 两图均168个算子、171个callback、84份64KiB分支数据；每算子3000000迭代、100000次access（branch-rw每次分支读+写）；每请求504000000迭代、读和写各16800000次 |
| 扰动 | 无迭代抖动、无长任务倍增、无同步等待、无异步恢复；不是随机耗时负载 |
| CPU | `0-15;16-31;32-47;192-207;208-223;224-239`；每组16个逻辑CPU、16worker、配置容量C=16；三池/NUMA |
| 路由 | 即时O；源high=1、gain=0、parent、all提交点、chunk=1、hop=4；仅同NUMA |
| 唯一策略变量 | `--target-limit 0 / 1 / 0.75`，0为完全关闭目标规则 |
| 重复和顺序 | seed421、422；每seed包含两图×三策略，确定性打乱次序，共12轮 |
| 记录 | 全部trace=1；无perf，无CPU后台采样；所有组承担相同的已有任务记录成本 |

上限检查为候选目的池近似`(O+R+B)/C <= target_limit`，本轮B=1。没有合格目的池就留preferred，不因该规则拒绝任务。这不是硬容量保证；并发选池和O/R交接可能使实际计数短时超过观察上限。0.75表示约12个计数位置，不能解释为75% CPU利用率。

两图保持总业务工作计数相同，但缓存、路由和实际CPU时间未必相同；只在**同图、同seed**内比较三策略。两图是已有机制场景，尚未确认为真实生产DAG。4026确认其中tail4能出现新旧阶段同池相遇，不等于生产负载拟合完成。

## 1. 更新、编译与功能验收

测试机保留原数据目录。以下只更新指定实验分支并构建本阶段；不需要连接真实业务环境。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114
cmake -S experiments/affinity -B build-affinity \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/home/bty/rank_demo/folly/_install \
  -DBUILD_TESTING=ON
cmake --build build-affinity \
  --target affinity_demo request_executor_test --parallel 8
TEST_CPUS='0-47,192-239'
taskset -c "$TEST_CPUS" ctest --test-dir build-affinity \
  -R '^(request_executor_test|affinity_functional|affinity_async_functional|affinity_flow|affinity_destination_admission_functional|affinity_destination_admission_matrix)$' \
  --output-on-failure
)
```

功能验收不作时延收益断言。关闭规则保持原选池语义，但新增观察字段可能改变对象大小等构建细节；**必须用本次同一个可执行文件重新跑off组**，不能把4024/4026旧结果当作新上限组的配对基线。off/on的CPU差包含新增规则及统计成本，不能直接归因为cache miss减少或增加。

## 2. 先打印计划，再执行固定12轮

默认只生成计划，不启动负载：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
python3 scripts/run_destination_admission_matrix.py \
  --output results/local/destination-admission-plan-01
)
```

计划中应只有front80/tail4和off/cap100/cap075六种cell，每种各两个seed。检查CPU列表与上述合同一致。工具只能核对声明CPU集及实际NUMA归属，不能替操作者重新认证硬件cluster边界。仍需沿用此前核实的隔离CPU资源。

执行必须使用**另一个全新目录**，以防覆盖计划、失败文件或旧结果：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
TEST_CPUS='0-47,192-239'
taskset -c "$TEST_CPUS" python3 scripts/run_destination_admission_matrix.py \
  --output results/local/destination-admission-01 --run
)
```

正常共12轮，每轮计划到达持续约20秒，另有初始化和排空开销。不要并行运行其他负载测试或修改/重新编译本阶段源码与可执行文件。每轮记录实际命令、执行前后源码和binary SHA256、退出状态及日志。缺binary、CPU不在调用方允许范围、失败/超时、请求未完成或配置不符都会停止，保留现场；不会自动跳过失败、重试或覆盖。若目录已存在，选择新的`-02`目录；不要删除旧结果来使命令通过。

## 3. 无需上传文件：打印完整汇总

12轮全部成功后执行：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
python3 scripts/run_destination_admission_matrix.py \
  --report results/local/destination-admission-01 \
  --warmup-seconds 2 --window-seconds 16
)
```

成功输出21行JSON：1行合同、12行逐轮指标、8行同图同seed相对off的差值。复制完整终端输出即可；原始文件继续留在测试机。报告只读，先完成整组校验才输出可比较结果；若拒绝解析，保留并回传具体错误，不把缺失结果视为零。

核对内容包括：12个cell完整、实际options等于计划、同一个构建、固定资源/工作和到达合同、请求checksum和逐算子工作指纹一致、零失败、所有记录守恒。**请求时延统一取计划到达[2,18)秒的192个请求**，保留窗口后完成的回调；整轮CPU/完成请求的分母为240，不混用。

每轮输出包括：

- 请求mean/p95/p99、到达lag与attempt-to-finish；分位数分别统计，不能相减分解。
- whole-run CPU、CPU/完成请求、在途峰、whole-run目标准入stats和决策reason。
- 五类callback的计数、选池改投、回home、等待mean/p50/p95/p99/max、CPU/wall均值。
- 与4026同口径的新旧阶段重叠、后期等待相交callback数和callback-ms、pool/NUMA分别输出。
- 原始三文件及执行凭据SHA256、业务工作及配对指纹。

`target_admission.stats`是整轮**选择/chunk**计数，包含提交尝试；每次选择只有一个主reason，候选拒选计数可以超过选择数。`no_eligible_target`与`decision_reasons.NoEligibleTarget`是同一计数的重复展示，不相加。本轮B=1、全部成功，开启规则时选择数应等于整轮callback数；这不能泛化到B>1或失败重试。off组不维护admission统计，所有这些值为0，表示关闭而非“没有遇到压力”。

`evaluated_selections`表示本次决策曾评估至少一个候选；任务trace的`admission_evaluated`描述**最终选中目标**是否经过检查，二者不是同一个分母。没有合格目标时前者可增加，后者仍为0。任务记录中的选择字段对同chunk成员重复，不能当作独立选择相加。

等待从选池后的submitted到start，不是纯Folly Q，也不是纯内核runq等待。阶段重叠中的executing是callback墙钟区间，含抢占；它不是CPU占用采样。本轮不会新增更细粒度CPU采集；如策略方向值得继续，可另外使用既有CPU采集工具复核，采集组与无perf组分别解释。

## 4. 如何决定是否继续

1. **先验收合同和机制覆盖。** 开启组有多少候选因上限被过滤、多少决策无目标、首波和后继的改投是否变化。若上限从未改变选择，就没有验证到它的作用；不能依据时延相近宣布该机制无效。
2. **同图、同seed看整体代价。** 先看request mean/p95/p99和CPU/请求，再看首波等待与后期等待的交换。两个seed分别列，不把两个p99平均后称全局p99。
3. **看是否只是停止借出。** 若改投几乎为零、后期等待下降而整体完成显著变慢，结果可能接近home隔离，应报告失去并行的代价，不能因后期更快自动判优。
4. **结合阶段相遇解释。** 同池相遇、相交等待是否下降；这是对受控干预的机制辅助证据。重叠仍不是实际CPU服务，不能把95%的交集占比称为95%因果等待损失。
5. **只扩展有明确疑点的方向。** 若两seed方向一致且有可解释取舍，再扩大重复与尾部样本；若小于波动或两seed反向，先查到达lag、背景干扰、拒选覆盖。已有192请求的p99由极少尾部决定，不能作为稳定SLO验收。只有需要检查小算子开销时才追加原46875档front80反例，不立刻扩大全维矩阵。

源池持续确认门控尚需单独实现与验收。本轮关闭/1/.75比较的是目标位置与数量；下一轮若增加源等待D，应在相同目标规则、源压力口径、资源与图上配对D=0和D=1ms，危机旁路另列。不能把目标限额、持续时长、回迁、跨NUMA同时修改后归因于某个阈值。

## 开发机小验证（不是测试机性能结果）

脚本允许显式`--development`，必须指定人工CPU划分。其默认仍是两个原图、12个cell，但每池1worker、4请求、每算子1000迭代/32次访问、50QPS；仅测试入口、记录、配对、离线分析贯通。以下自动取当前允许CPU中的前两个：

```bash
(
set -eu
cd /root/rank_demo/executor_demo
DEV_CPUS=$(python3 -c 'import os; c=sorted(os.sched_getaffinity(0)); assert len(c)>=2; print(str(c[0])+";"+str(c[1]))')
python3 scripts/run_destination_admission_matrix.py \
  --development --cpus "$DEV_CPUS" \
  --output results/review/destination-admission-smoke-01 --run
python3 scripts/run_destination_admission_matrix.py \
  --report results/review/destination-admission-smoke-01 \
  --warmup-seconds 0 --window-seconds all
)
```

开发模式允许显式改`--threads/--requests/--iterations/--accesses/--qps`，所有覆盖均进入metadata和完整命令；非开发模式拒绝这些覆盖。人工CPU组不称硬件cluster，开发汇总保留`development=true`，不得与测试机结果配对或推导最佳上限。

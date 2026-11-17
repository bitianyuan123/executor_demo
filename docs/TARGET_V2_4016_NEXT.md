# 4016之后：只读审查到达与请求重叠，再推进异步恢复

**当前状态：4017已完成六轮既有CSV的到达核查，见[结论](TARGET_V2_4017_REVIEW.md)。不再重跑CPU矩阵、到达诊断或追加trace/阈值。异步完成源与驱动已加入工程，当前按[异步功能指南](TARGET_V2_ASYNC_FUNCTIONAL_GUIDE.md)进行编译及小规模功能验收；等待期间释放worker，恢复callback再推进图。性能对照待功能验收后另立合同。目标始终每个已核实硬件cluster一个池。** 下文保留当时证据/命令，不是新的执行清单。

**本次只分析已有六轮SD的CSV，不再运行压力、增加QPS或补trace。** 4016已观察到request峰值3，Q/O相对同期home的请求均值降低约33%～34%，同时整轮CPU/完成请求增加约6%～7%。先保留这组收益与成本，不据此选Q/O赢家，也不继续扩展CPU工作族的策略矩阵。

本次离线审查完成即停止。下一项工程优先补真正释放worker的异步等待与恢复，先做实现及功能验收；同步sleep仍是另一个反例，不安排在本次中顺带运行。目标始终为每个已核实硬件cluster一个池、L1 request home、L2新ready算子选池，不重放历史四池配置。

## 1. 为什么还需要这一次离线审查

4016的home两档每秒都有较高的发起lag尾部，而Q/O较低。现有flow报告已经说明它不是约6ms均值差的主体：

| 每池线程 | home lag均值 | Q/O lag均值 | home attempt→finish均值 | Q / O attempt→finish均值 |
|---|---:|---:|---:|---:|
| 16 | 0.302ms | 各约0.055ms | 17.622ms | 11.703 / 11.888ms |
| 128 | 0.275ms | 各约0.055ms | 17.802ms | 11.954 / 11.915ms |

均值可以分解，分位数不能相减。上述均值分解只是描述；把lag从时延中扣掉并不能制造一个不受发起过程影响的反事实实验。实际请求发起时间会改变后续重叠与竞争。

现有一秒桶还不能回答高lag是否集中于固定request序号相位、发生后是否有补发，以及request重叠持续了多长时间。现有requests.csv足以补这些描述，无需重新采集内核轨迹或开启callback trace。

六轮实际顺序恰好是home/16、home/128、Q/16、O/16、Q/128、O/128。即使顺序来自预先打乱，本批每格一次、home仍集中在前两轮；时间顺序与策略没有在独立重复中拆开。新脚本不能消除此混淆，也不能把约3ms现象直接归因为内核门控、定时器、某个cluster或均衡算法。

## 2. 输入与运行方式

只更新Python脚本和文档即可，**不需要重编二进制或Folly**。以下命令只读取六个既有结果目录并打印报告；没有run_matrix、native执行或结果覆盖。

更新命令要求当前仍在本实验分支；分支不符或快进更新失败就停止，保留本地修改和全部结果，不使用reset清理。若已同步到包含arrival_audit.py的版本，可以跳过git更新部分，直接执行分析。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114

DENSITY_ROOT='results/local/v2-short-density-count-01'
python3 experiments/affinity/arrival_audit.py \
  "$DENSITY_ROOT/seed-421-threads-16-home" \
  "$DENSITY_ROOT/seed-421-threads-128-home" \
  "$DENSITY_ROOT/seed-421-threads-16-queue" \
  "$DENSITY_ROOT/seed-421-threads-16-outstanding" \
  "$DENSITY_ROOT/seed-421-threads-128-queue" \
  "$DENSITY_ROOT/seed-421-threads-128-outstanding" \
  --warmup-seconds 2 --window-seconds 16 \
  --phase-modulus 6 --lag-threshold-ms 1 --top 3
)
```

原始CSV留在测试机，只回传完整终端输出。脚本复用report.load_run的输入审计，在全部输入通过审计后才打印分析；缺文件、截断、计数或hash不符时保留报错及原始文件，不删除失败请求或重写summary来使审计通过。参数中的相位6是这一次的分组维度，不是工具写死六池的假设。

本次每轮1920条请求、六轮合计11520条；正式计划到达cohort为每轮1536请求。报告每轮只列6个序号相位、阈值两侧的汇总、最多3条高lag上下文和两种重叠统计，终端输出有界。trace0可直接分析，不期待tasks.csv。

## 3. 输出含义与核对顺序

先看整轮及正式cohort的状态守恒。预期每轮整轮1920成功、失败/拒绝0，计划到达`[2,18)`内1536成功；晚于窗口结束才完成的请求仍保留。保留工作签名、到达hash、checksum和资源合同不变的既有核对，不把本脚本当成新增性能运行。

随后看以下四组信息：

1. **lag阈值计数及分层时延。** 严格按lag>1ms与lag≤1ms分层；1ms是预先选定的诊断切分点，不是生产SLO或池繁忙阈值。先看各层样本量，再看该层成功请求的planned及attempt→finish时延；失败数仍独立保留。某层没有请求就是没有样本，不补零时延。
2. **request ID对6取模的相位。** 它回答序号规律是否存在。该字段不是实测request home、执行池、CPU或NUMA；即便当前RR且全部请求成功，也不能将某相位直接命名为某CPU调度问题。
3. **最多3条高lag及相邻实际attempt间隔。** 用来识别一次延迟后是否出现较短的补发间隔，以及是否集中在局部序号。它不能区分driver睡眠唤醒迟、被抢占、前一轮构造/提交拖延等原因；top样本也不代表总体分布。
4. **两种按时间加权的request重叠。** 使用整轮所有请求与`[2,18)`窗口相交的区间，跨边界部分裁剪到窗口内。分别统计scheduled→finish与attempt→finish的均值、峰值及重叠至少2个请求的时间比例。这与只取1536个计划到达请求的时延cohort是两种统计口径。

scheduled→finish重叠包含发生器lag，是等待终态的计划请求量；attempt→finish从driver尝试时间开始，仍包含请求构造、root提交及全部图运算。两者都不是Folly队列深度、callback并发、Linux runnable数量或CPU利用率。它们的峰值也不要求与native在请求对象生命周期内采样的peak-inflight完全相同。

相同窗口下，两种重叠均值之差描述lag区间占用；它不是CPU忙碌的差值。出现较长时间的request重叠也不证明持续饱和或所有候选池都忙，需与现有低backlog、完整完成及CPU成本一起解释。

## 4. 这次分析不做什么

- 不删除lag较高的请求、不按lag分层重新宣布一个“修正后收益”，也不从总体p99减去lag p99。
- 不将按序号或时间分组后的相关性解释为内核、cache、NUMA或负载评分的因果证据；同seed也不保证实际发起时序一致。
- 不把Q/O多出的整轮CPU称为纯负载读取、原子计数或路由开销。trace0各策略仍维护基础计数与生命周期，额外CPU可能来自评分、跨池执行、调度及局部性等共同变化；request CSV不能分解它们。
- 不为了证明某种机制重新开启大规模trace、不复用旧trace的0.85ms/3ms作为新参数，不重跑历史CPU矩阵。

一次成功审查后就结束此项：若看到相位或补发规律，记录为待解释现象；若没有，也不继续切分更多模数、阈值或时间桶寻找模式。输入不完整则记录缺口，不能伪造结论，但该缺口不无限阻塞独立的异步功能开发。

## 5. 随后的工程重点：真正异步恢复，先验收功能

已有[异步等待与恢复设计](ASYNC_CONTINUATION_WORKLOAD_DESIGN.md)，**当前v2负载驱动尚未实现该能力，也没有可执行的异步RPC CLI**。总池Continuation生命周期单测已经存在，仍不能替代外部完成源、图依赖及排空联验。本页不提供不存在的命令。

下一步先完成受限的外部完成源：executor算子登记等待后返回，单个有界辅助完成线程到期后通过Continuation恢复图推进；等待期间不占executor worker，不把待完成RPC数量算成某个子池的CPU负载。总池仍按实际硬件cluster组织，保留request home与实际source继承。

先验收三个小规模合同，不立即下发目标机性能矩阵：

| 合同 | 用途 |
|---|---|
| D0：原有直接推进 | 保留旧行为与固定业务计算基线 |
| A0：相同选点，外部完成源，零配置延迟 | 验证额外恢复callback及机制成本边界；零配置不等于零实际延迟 |
| A1：相同选点和恢复callback，有界非零延迟 | 验证释放worker、等待存活、到期恢复与图依赖 |

用极少请求和分支检查固定迭代/访问/checksum、恢复callback数量、唯一request终态、上下文与home/source继承；验证等待时Q/I/O可降至0但request与drain lease仍存活。取消、注册容量拒绝、恢复失败、Draining期间再次登记等待和完成源停止顺序都必须通过，不能仅验证成功完成一次。

实现及功能通过后，再单独声明最小性能合同、观测开关和资源预算。独立延迟与对齐集中恢复分开研究；同步sleep仍可保留为worker被占用的独立反例，不能称为异步RPC或作为本次离线审核的隐藏下一批。

## 6. 指南验证范围

完整shell已通过语法检查，六目录实际CLI已用临时trace0合成输入验证：每目录1920行、窗口1536行、严格阈值和两种重叠输出均符合参数，输入文件未改动；最后一个输入损坏时返回非零且不输出前五轮的半份分析。该验证未执行native或ARM压力，不构成4016原始CSV的远程复核；真实审查仍需在保留结果的测试机运行上方命令。

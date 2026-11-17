# 异步首批八轮：恢复结构、固定等待与同合同策略对照

**当前状态：本页八轮已由4018完成，见[阶段结论](TARGET_V2_4018_REVIEW.md)。不再执行下方矩阵；命令保留作合同记录。其后[4020四轮A1机制补证](TARGET_V2_4020_REVIEW.md)也已完成。当前冻结合同与策略，转入分步交接，不自动追加性能测试。**

目标机已回传编译、3项CTest（3/3，0.35秒）以及8个成功场景、1个预期容量拒绝场景全部通过。证据是用户粘贴的终端报告，本地未独立读取目标CSV；`Already up to date`没有打印目标git SHA，因此`0c79108`是预期版本，不是已独立核实的版本身份。见[目标功能验收记录](../results/review/async_v3_target_functional_validation.json)。这些结果允许开始有界试验，不提供性能排名。

**本批固定八轮，seed421，每个已核实硬件cluster一个池，每池16线程。** D0/A0/A1各运行home与outstanding（O）的trace0对照，再为home的A0/A1各运行一轮trace1。八轮结束即停止运行，先分析，不追加seed、线程、信号、延迟或对齐梯度，不重跑4016/4017。

## 1. 八轮各回答什么

| 合同 | 外部等待位置与最小延迟 | trace0 | trace1 |
|---|---|---|---|
| D0 | 不启用异步，原图直接推进 | home、O | 无 |
| A0 | 确定性选择50%的operator位置，0µs | home、O | 仅home |
| A1 | 与A0相同选择集合，2000µs | home、O | 仅home |

50%是逻辑位置hash的阈值，不保证每request选中恰好一半。A0仍经过外部完成源与恢复callback，配置0不等于实际等待0；A1的2ms从每次实际issue计起，不是冻结的绝对释放时间表。本批不启用对齐恢复。

- **同合同策略差异：** 在D0、A0、A1内部各比较trace0的O与同期home。O是延续验证的候选，不是已选赢家；16线程是固定资源预算，不代表最优线程数。
- **D0→A0的结构总效应：** 核对相同业务工作后，观察外部完成线程、额外控制callback、依赖推进方式和调度点共同改变的结果。它不是单独的路由、计数器或callback开销测量。
- **A0→A1的等待干预：** 在同策略、相同选择集合与业务工作下改变最小等待。它也会改变ready时刻、请求重叠和竞争，不能解释为请求时延简单增加2ms，不能用两个p99相减还原某一段等待的p99。
- **home的观测敏感性：** A0、A1各自的trace1与trace0配对，查看整轮CPU/请求变化以及源lag、恢复callback等待等机制。home的trace成本与事件分布不能转借给O。

96QPS是短CPU工作下的实验到达密度，不是声称真实业务为96QPS。50%与2ms是受控假设，不是从旧trace反推出的真实RPC比例或延迟。本批仍保留每request开头80分支扇出、后续链与第二波并行、确定性长短工作和branch私有读写；没有把历史四池配置作为目标。

## 2. 固定资源和工作合同

| 项目 | 本批取值 |
|---|---|
| 六个池的CPU分区 | `0-15;16-31;32-47;192-207;208-223;224-239` |
| 全进程允许CPU | `0-47,192-239`，此前已获准独占的96个逻辑CPU |
| 亲和/线程/容量 | cluster亲和；每池16线程；capacity-scale=1，名义容量C=16 |
| 到达/窗口 | regular，96QPS，1920请求；按计划到达选`[2,18)`秒cohort |
| 图/CPU工作 | widths=80,4；depths=2,2；iterations=375000；accesses=12500 |
| 数据/异质性 | branch-rw；branch64KiB，shared0；task级±25%，约1/8四倍工作 |
| request与后继路由 | RR home，parent continuation，balance-at=all，chunk1，hop4 |
| 借出范围/评分 | 仅同NUMA；count默认high1/low0.5/min-gain0，不调阈值 |
| 外部源 | 单辅助线程；align=0；事件容量4096；无同步sleep |
| 其他观测 | CPU采样0、page采样0；trace由八格合同管理 |
| 准入/内存 | max-inflight16；memory-mb512；单轮超时120秒 |

driver和外部完成辅助线程继承上述CPU并集，与worker分享这些CPU；没有另设专用AIO核或实验外CPU。worker仍各自绑定到对应cluster。helper只负责模拟外部完成和恢复提交，不执行真实网络RPC；它的串行派发、源队列和锁也可能形成成本或lag，不能等同于现有业务的AIO运行时。

外部等待期间worker返回，未来恢复尚未ready，不应计入池Q/I/O。总池接纳恢复callback后，它才进入现有ready调度。O按callback计数，轻量`rpc_resume`与原业务operator均算一个；因此新增恢复后的O表现不能只归因于外部等待变长。parent偏好实际父池，不保证永不回request home；all使普通恢复callback也有机会借出。

## 3. 更新并执行八轮

沿用目标机现有路径及已验收二进制。本轮只增加矩阵与分析脚本，C++实现未变，无需重新编译Folly或demo。若更新内容另有C++变更，应先保留版本信息并复查，不能假定旧二进制包含新代码。

下面先打印当前checkout，随后在CPU并集内运行八格。矩阵metadata还记录二进制及源码hash、caller允许CPU、计划和实际参数；git SHA本身不证明二进制与源码一致。输出目录必须新建，存在时直接停止，不覆盖。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114
git rev-parse HEAD

PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
TEST_CPUS='0-47,192-239'
PILOT_ROOT='results/local/v2-async-pilot-01'
mkdir -p results/local
mkdir "$PILOT_ROOT"

taskset -c "$TEST_CPUS" python3 experiments/affinity/run_matrix.py \
  --suite async-pilot --seeds 421 --timeout-seconds 120 \
  --output "$PILOT_ROOT" --run --base-args \
  --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 --threads 16 \
  --requests 1920 --qps 96 --arrival regular \
  --burst-size 8 --burst-spacing-us 100 \
  --widths 80,4 --depths 2,2 --iterations 375000 \
  --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 12500 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope task --estimate-mode constant \
  --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
  --cross-numa 0 --max-hops 4 --cpu-sampling 0 --page-sample 0 \
  --async-align-us 0 --async-capacity 4096 \
  --max-inflight 16 --memory-mb 512

python3 experiments/affinity/async_pilot_report.py "$PILOT_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
)
```

`regular`不使用burst-size/spacing，保留两个参数只为与既有SD工作合同核对。矩阵负责policy、trace、async-percent和async-delay-us；不要在base-args再次指定。八格在同一个seed块内统一随机排序、串行执行，不按D0/A0/A1分别顺序执行。不要加`--cases`或多seed，工具会拒绝；不增加线程维度。

预算为八次实际运行，到达期合计约160秒，另加启动、排空、导出和分析时间。失败也占用预算，不自动补满成功格。单轮120秒上限包括启动、约20秒投递、排空与写结果；超时或非零退出会停止余下case，外层`set -e`随即停止，不继续生成一个表面完整的性能报告。保留失败目录、run.log及终端信息；严重错误可能没有完整summary，不把它当成有效完成。

## 4. 回传与预期审计

回传从版本、八轮运行到`async_pilot_report.py`结束的完整终端输出即可，CSV留在目标机。这个专用报告先核对八格合同、资源、固定业务工作、完成与源守恒，再汇总正式cohort请求时延、整轮CPU/请求、同合同home/O差异与跨模式描述。A0/A1的home trace开关分别配对；不要自行把四个trace目录混成同一合同，也不需要额外调用大批量完整任务报告。

如果八轮已完成，只需重新打印报告，无需再次执行矩阵：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PILOT_ROOT='results/local/v2-async-pilot-01'
python3 experiments/affinity/async_pilot_report.py "$PILOT_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
)
```

当前生成器与seed421的预期值如下，最终以逐轮审计为准；合同不符时保留数据和差异，不改参数把数字凑齐。

- 每轮1920请求成功，失败/拒绝0。正式cohort为计划到达ID192～1727，共1536请求；窗口后完成仍保留，不按完成时间删尾。最后一个计划到达约19.989583秒。
- 每request原有171个callback，其中168个业务operator；整轮原图328320个callback，cohort原图262656个callback。D0不增加恢复。
- A0/A1整轮选择161076个恢复位置，cohort选择128809个。成功完成时实际callback总数489396，cohort391465；不同策略、trace开关和延迟不改变选择集合。trace0只有合同/汇总，不能把未写出的任务时序当成已观测值。
- 所有八格固定业务迭代总量166355865000、访问总量5545195500、arrival hash为8354655555075271473，整轮及逐request业务checksum应一致。恢复控制不重复原计算/访存；D0与A0/A1的完整work contract不同，应比较固定业务工作签名，不强求完整合同签名相同。
- 成功异步轮应有registered=fired=resume_succeeded=161076；pending、canceled、rejected、resume_failed、registration_rejected、callback_failures均为0。helper CPU单独列出，但已包含在进程CPU中，不能再次相加。

trace1的任务记录按2K预留，全轮656640个slot，低于2000000上限；实际成功记录489396条，另有161076条异步事件。请求payload在4KiB/64KiB页舍入下每个约5515776字节，max-inflight16约84.164MiB。**512MiB只限制payload，不是进程RSS上限**，不含任务/事件预留数组、结果CSV对象、线程栈、完成源或分析器内存。只有两格开启详细trace，串行完成；源容量4096约束同时保留的事件，不是整轮计划恢复总数。在本批准入16、每request168个operator且每operator最多登记一个事件的约束下，保守同时事件上界为16×168=2688，低于4096。不要把整轮161076大于4096误判为必然拒绝；若仍出现容量拒绝，先保留故障证据，不自动扩大容量。

报告中的源lag（due→fired）、恢复submitted→start等待分别描述外部源和executor路径，不是内核runq时延。submitted在选池/预留之后，不能覆盖整个恢复提交路径。trace记录执行、观测与调度都会受开关影响，trace1−trace0不是基础计数维护的独立成本；逐callback执行CPU又不包含所有导出、预留分配和驱动成本，需同时保留整轮CPU。

## 5. 分析顺序与停止规则

先检查八格齐全、CPU资源/二进制身份、计划工作、完成和源守恒，再看正式cohort的请求均值/p95/p99、入口lag、整轮请求在途峰值与排空时长，最后看整轮CPU/完成请求、helper成本及两格home详细机制。cohort时延与整轮CPU窗口不同，不能直接相减或拼成一个因果分解。

1. 八轮完成即停止。本批每格只有一个seed的一次运行，不能估计独立重复波动；1536个请求也不自动构成可靠的独立尾样本。差异小或成本/收益冲突时保留候选，不输出通用赢家，不自动追加重复或门控梯度。
2. 失败、拒绝、超时、合同不符必须保留；不只分析成功request，不删除高lag样本来“修正”策略收益，不提高容量或准入上限后悄悄补跑。报告应拒绝不完整或不兼容的八格，不能把缺失值补成0。
3. request时延/在途数增加不等于executor更忙；外部pending单独解释。源峰值接近容量、source lag上升或helper占用可提示模拟完成源自身限制，不能直接外推真实AIO瓶颈。
4. 请求在途峰值大于1不证明持续CPU饱和，峰值较小也不触发自动提高QPS。较大入口lag或额外排空本身不会自动中止程序，须保留打印的时长证据后再决定下一步；本报告不输出重叠时间比例或逐秒backlog，不据此宣称积压趋势已被核验。
5. 仅在本批结果留下明确问题后，才选择下一项有限覆盖：如O路径机制、恢复集中、另一延迟假设或独立重复。本指南不预先启动这些实验，也不把同步sleep当作异步RPC。

与真实业务可共享的是总池封装与Continuation恢复契约；真实RPC完成线程可接入同一接口。当前模拟器不含网络、下游服务、随机尾部、超时重试及传输取消，本批不宣称生产代表性或给出负载到request p99的通用公式。实现边界见[异步设计](ASYNC_CONTINUATION_WORKLOAD_DESIGN.md)，此前CPU族结论及入口lag限制见[4017报告](TARGET_V2_4017_REVIEW.md)。

## 6. 本次工具验证

开发机通过4项相关CTest（含13项新报告用例），八格计划与非法参数/旧目录保护回归通过；两个人工CPU分区、每池1线程、每格4请求的完整八格运行与新摘要联验通过。文档Shell语法、原命令去掉运行开关后的八格计划及本地链接均检查通过。[工具验证记录](../results/review/async_v3_pilot_tool_validation.json)保留合同计数与代码hash；没有执行本页ARM性能矩阵，没有据开发机小样本输出策略结论。

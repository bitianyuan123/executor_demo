# 4022 请求内 CPU 证据与负载覆盖修订

**需要调整负载并补覆盖。细粒度 CPU 是验证负载的观测手段，不能代替改变负载后的策略对照。** 上一轮“先补观测”的理由是避免同时改变多个变量；把后续覆盖只写成可选，容易导致用同一个图不断解释已有收益，不能回答策略是否依赖这个图的特殊性。现在将负载形状与工作量梯度列为明确的下一步，不等待 perf 解析恢复才设计它们。

## 1. 本次已经知道什么

4022 的两份成功输出来自旧4021的 `01-home` 和 `03-outstanding` CSV重分析，没有重新运行这两组240请求。下表使用全240请求，不与原192请求正式cohort混用。

| 指标 | home | O |
|---|---:|---:|
| 请求 planned mean ms | 120.275 | 73.119 |
| 每请求 timed callback CPU ms，均值 | 1367.560 | 1371.818 |
| 首波全部深度 / 全部业务算子 CPU 份额，逐请求均值 | 95.732% | 95.744% |
| 单请求 CPU / 请求过程时长，逐请求均值 | 11.434 CPU当量 | 19.172 CPU当量 |

其中 request 6 是显式诊断样本，结果为：

| 指标 | home | O |
|---|---:|---:|
| attempt至最后callback结束 ms | 127.341 | 74.004 |
| timed callback CPU ms | 1374.030 | 1358.920 |
| 累计交付50% CPU的时间界 ms | 45.568–45.591 | 16.945–16.966 |
| 累计交付90% CPU的时间界 ms | 80.508–80.534 | 34.613–34.646 |
| 累计交付99% CPU的时间界 ms | 113.562–113.587 | 60.393–60.411 |

home该请求仅CPU面积就需要 `1374.030/16≈85.877 ms` 的理想供给。**当前3M档的服务并非只在1–2ms内完成；当前图也不是仅在开头短促扇出后就转成少量计算。** 两段80路宽波占了约95.7%的算子CPU，这是需要覆盖其他工作分布的直接理由，无需先取得精确1ms曲线。

这些界限不是均匀摊出来的CPU曲线，也不能证明整个过程连续满载。O的服务面积接近但交付更早，支持当前模型的均衡机制；单个request 6不是生产代表样本。历史trace缺少request/operator边界，不能据它确定真实DAG、每算子CPU时间或首波CPU份额。

结构化原文及源行号见[4022证据](../results/review/4022_summary.json)。

## 2. 为什么缩小所有算子还不够

把每算子300万迭代改成37.5万迭代，改变了CPU面积和可能的峰值持续时间；若图仍为80×2+4×2，首波工作占比基本保留。它不能检验“首波之后有显著工作，且并行度收缩”这一背景，也不能检验缩短算子和收缩并行度的交互。

新增覆盖采用两个等算子数、等数据分配量的图：

```text
front80：init → 80条深度2的分支 → join → 4条深度2的分支 → join
tail4：  init → 80条深度1的分支 → join → 4条深度22的分支 → join
```

| 控制量 | front80 | tail4 |
|---|---:|---:|
| 业务算子 / 总callback | 168 / 171 | 168 / 171 |
| 分支数据分配 | 84×64KiB=5.25MiB | 相同 |
| 业务fork提交 / 普通后继提交 | 84 / 84 | 相同 |
| 首波业务算子数 | 160 | 80 |
| 首波固定工作计数份额 | 95.24% | 47.62% |
| 首波后的并发与深度 | 4路×2段 | 4路×22段 |

同档每算子迭代与访问数相同，关闭±25%和四倍工作，保证两个图的业务总计数相同。**等工作计数不是等实耗CPU**；依赖深度、关键路径和同分支数据复用都会变化，这正是图形状干预的一部分。不同图的checksum不要求相同；同图、同seed、同工作档下的home/Q/O必须逐请求和逐算子核对一致。

同时保留现有max-hops=4。深链可能更早用尽路径迁移预算，应读取flow报告中的HopBudget原因计数；若它明显限制tail4的路由，需定向增加预算作消融，不能把预算限制误归因于“并发度降低”。

对每个图分别设置三档，形成工作量与图形状的交互覆盖：

| 每算子迭代 / 访问数 | 每请求迭代 / 读数 / 写数 | 用途 |
|---|---|---|
| 375000 / 12500 | 63000000 / 2100000 / 2100000 | 首先执行，比较相同较小工作量在两种图中的策略差异 |
| 46875 / 1563 | 7875000 / 262584 / 262584 | 继续降低工作量，检查调度/观测成本能否抵消借出收益；不预先命名为1ms负载 |
| 3000000 / 100000 | 504000000 / 16800000 / 16800000 | 较大工作量的结构对照，连接旧3M结果但不直接配对 |

1563是整数访问数，不是12500严格除以8。新的均匀3M关闭了旧工作扰动，平均CPU不能沿用旧1.37 CPU秒。极小工作档还可能由初始化、首触和提交开销主导，必须把init单列，不为“拟合尖峰”删去这部分实际开销。

共同条件为选定六cluster各一池、每池16 worker、regular 12QPS、RR home、parent/all、同NUMA借出、branch-rw 64KiB、无外部等待、相同评分参数。初轮固定这些因素，后续才能识别哪一个变量改变收益。

## 3. 已有采集只需离线恢复

4022有四次采集进入离线分析后失败，没有产生新的可用细粒度曲线。首次3M明确报告 `line 1: malformed switch payload`；用户随后确认三种主要配置均为同一错误，并提供perf输出：

```text
[000] 128173.903316950: sched:sched_switch: perf:824058 [120] D ==> migration/0:17 [0]
```

原解析器只支持 `prev_comm=... prev_pid=...` 的具名字段格式，未支持这里的紧凑格式。已兼容两者，保留切换链、丢事件、时钟、TID与CPU归属检查；目标机仍须重新解析并通过这些完整性检查。当前报错发生在离线分析，不能据此认定负载运行失败，也不是已经得到375k形状的结论。

更新Python后执行以下命令，保留原目录，用新目录保存解析结果；不重编，不运行demo，不重采perf：

```bash
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114

for d in request-cpu-3m-home request-cpu-375k-home request-cpu-375k-outstanding; do
  python3 experiments/affinity/request_sched_profile.py \
    --run "results/local/$d/demo" \
    --trace "results/local/$d/sched.txt" \
    --capture "results/local/$d/capture.json" \
    --request 6 --bin-ms 1 \
    --output "results/local/$d/profile-format-v2" || break
done
```

成功输出包含 `payload_format_compact` 计数、摘要和窗口序列，可直接复制终端。原 `execution.json` 仍记录当时的失败，新的profile不改写历史失败记录。若再次报错，打印错误即可；不要为了换解析器重复压测。`request-cpu-3m-home1`是另一次独立采集，按需另行分析，不和首轮混合。

## 4. 新负载覆盖的执行与回传

新增 `run_matrix.py --suite shape-coverage`：每工作档两个图×home/Q/O×两个seed，共12轮，seed内打乱顺序，发生失败即停止。默认参数明确输出到plan和metadata。无perf采集，统一trace1，能够核对工作和改投；这不是零观测开销的净性能结论。

先执行375k档。首次可去掉 `--run` 生成计划，计划输出目录应使用另一新名称；已有matrix工具会保存计划，所以不能把同一非空计划目录直接作为运行目录。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
SHAPE_CPUS='0-47,192-239'
SHAPE_PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'

taskset -c "$SHAPE_CPUS" python3 experiments/affinity/run_matrix.py \
  --suite shape-coverage --seeds 421,422 \
  --output results/local/shape-375k-fixed-01 --run \
  --base-args --cpus "$SHAPE_PARTITIONS" \
  --iterations 375000 --accesses 12500

python3 experiments/affinity/report.py \
  results/local/shape-375k-fixed-01/seed-* \
  --warmup-seconds 2 --window-seconds 16

python3 experiments/affinity/flow_report.py \
  results/local/shape-375k-fixed-01/seed-* \
  --warmup-seconds 2 --window-seconds 16
)
```

已有带TID的affinity_demo可直接使用，这次负载变化不需要新增C++参数。每轮240请求，正式统计按计划到达 `[2,18)` 秒取192请求，晚完成仍计入。报告在相同图/seed内配对，不跨图强行配checksum；每轮p99单列，两个seed只用于方向筛查，不能认证生产尾延迟。

先回传375k的运行摘要、report和flow输出。确认工作守恒、零失败/拒绝、到达lag和采样范围后，继续下面两个档，完成图形状与工作尺度的覆盖；不是等perf恢复才能推进：

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
SHAPE_CPUS='0-47,192-239'
SHAPE_PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
taskset -c "$SHAPE_CPUS" python3 experiments/affinity/run_matrix.py \
  --suite shape-coverage --seeds 421,422 \
  --output results/local/shape-46875-fixed-01 --run \
  --base-args --cpus "$SHAPE_PARTITIONS" \
  --iterations 46875 --accesses 1563

taskset -c "$SHAPE_CPUS" python3 experiments/affinity/run_matrix.py \
  --suite shape-coverage --seeds 421,422 \
  --output results/local/shape-3m-fixed-01 --run \
  --base-args --cpus "$SHAPE_PARTITIONS" \
  --iterations 3000000 --accesses 100000
)
```

后两档分别用上一组report/flow命令替换目录即可。各档12轮、计划约4分钟，不含初始化和排空；不自动追加线程、到达或阈值全矩阵。CPU/请求来自程序累计计量，不能冒充逐毫秒CPU利用率。新矩阵的请求形状可先用 `request_cpu_shape.py` 对每个具体目录离线分析；当前perf封装固定原80×2+4×2图，其旧曲线不能移植到tail4图。

## 5. 如何依据覆盖决定后续策略实验

| 观察 | 可以检验的解释 | 下一项有针对性的实验 |
|---|---|---|
| front80收益明显，tail4明显变小 | 先前收益依赖持续宽并行；四路关键路径限制后半程 | 在同档比较O的fork/all，查看后继改投是否只增加成本 |
| 46875档收益消失或反转 | 路由、记录或亲和成本可能超过少量等待 | 同图trace0/1对照，再扫描有限gain/门槛；不能立即认定该迁或不该迁 |
| Q/O在短工作档差异扩大 | 队列领取窗口和执行占用的作用随尺度变化 | 同图增加T64，与T16比较，仍是每cluster一池 |
| 两个seed排名不同、投递lag或失败明显 | 重复噪声或入场限制尚未排除 | 先核对合同并增加独立重复，不以一次p99定策略 |
| 需要解释请求重叠 | 12QPS均匀到达未覆盖同池多个请求相撞 | 固定图与每请求工作，再加同均值burst/Poisson |

三个工作档的平均CPU预期不同，实际必须计量；本轮隔离每请求工作尺度，不控制为相同平均利用率。以后若比较等平均压力下的不同脉冲，需要另行改变QPS或背景压力，并作为独立对照，不能混进当前图形状结论。

持续压力门控、回home/留parent、跨NUMA均衡仍保留为候选。当前既不把3ms写成生产最优阈值，也不因为原模型收益明显就排除门控。**需要完成的是受控负载干预与策略敏感性验证；细粒度CPU用于解释它们，不能替代它们。**

开发验证见[4022验证记录](../results/review/4022_validation.json)：51项相关Python单元测试通过，既有22次功能运行通过，新增两图六策略格的缩小功能运行及四个同图配对审计通过。它们验证工程与工作守恒，不构成目标机性能或负载代表性验收。

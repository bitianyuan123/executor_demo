# ARM v2 初筛：4001.log 审阅与下一步

依据：用户回传的 `4001.log`，SHA256 为 `b476d1847c67dea336e53b07271aeef7a5f3bf48cb870b4cb5abc4dcfda86f63`。本文核对终端输出与本地 `6324cbd` 实现；没有取得目标机原始 CSV，也没有在开发机重跑 ARM 性能实验。目标二进制的精确版本仍以该机保存的 matrix metadata 为准。

## 1. 当前结论与研究位置

本轮沿着**每硬件 cluster 一个 Folly 子池、总池完成请求亲和和算子借出**的主线：六个分区为 `0-15;16-31;32-47;192-207;208-223;224-239`，NUMA标签为 `0,0,0,2,2,2`。没有转向历史四池配置。

目标机 affinity 构建成功，CTest **8/8** 通过。日志包含一次12请求校准、4轮入口形态、5轮线程数、18轮信号策略，共28次运行；运行摘要的完成数均达到指定请求数。`/.build.command` 提示未阻止此次编译和测试。signals完整 `report.py` 尚未打印，跨策略的CSV工作合同、checksum、配对审计和CPU回退证据仍待补齐。

已有结果支持：**单请求首波集中在一个cluster时，借用同NUMA其他cluster能明显降低平均请求时延，代价是进程CPU用量略有上升。** 所有signals的 `request_peak=1`，无数据数组/访存负载、无跨NUMA借出，目前不能选择生产最终策略。阶段3已有的独立任务条件关系不需重跑，也不能直接用来给这个DAG设置生产阈值。

## 2. 实际负载与调度

| 组别 | 请求/到达 | 图与计算 | 线程/池 | 策略 |
|---|---|---|---:|---|
| calibration | 12，regular 1 QPS | 宽20/4，深1/2，100万迭代 | 16 | home |
| entry | 每轮96，12 QPS；regular/Poisson/burst4/burst8 | 同calibration | 16 | home |
| threads | 每轮96，regular 12 QPS | 宽80/4，深1/2，100万迭代，无异质 | 8/16/64/128/256 | home |
| signals | 每轮96，regular 12 QPS，seed420/421/422 | 同threads；请求级±25%扰动、约1/8请求×4 | 128 | home/queue/active/outstanding/work/cpu |

全部 `model=none, accesses=0`。80/4、1/2的图含88个业务算子、2个join、1个初始化回调，每请求共91个回调。首波80条分支均只有一个算子；第二波4条链各两个算子。它不能充分检验首波迁移分支的后继数据复用，也没有模拟RPC。

L1默认RR固定request home；L2默认parent续接、all就绪点、chunk1，同NUMA优先且本轮禁止跨NUMA。名义容量每池16，128是预建worker数，不是128CPU容量。实现显式设置最小/最大worker数相同，不是只预建16个线程。

queue使用Q；active使用 **Q+I**，不是只有I；outstanding使用已接收未完成O；以上再加提交预占并按容量归一化。I是执行中的callback，包含被抢占和同步阻塞；Q是Folly队列，不是内核runq。work本轮使用常数估计，与O接近，未验证请求成本预测价值。cpu组合后台历史CPU/runq等待采样与Q，无效时回退O。

## 3. 定量结果

### 3.1 线程数初筛：整轮96请求

以下均为seed420、home、相同88个业务算子工作。CPU/请求由进程CPU总秒数除以96换算。

| 线程/池 | 请求均值 ms | 请求p99 ms | 进程CPU ms/请求 |
|---:|---:|---:|---:|
| 8 | 17.903 | 59.155 | 123.883 |
| 16 | 11.201 | 21.571 | 136.817 |
| 64 | 11.573 | 48.211 | 136.039 |
| 128 | 11.375 | 31.536 | 136.394 |
| 256 | 11.593 | 51.888 | 137.085 |

16→256线程没有出现平均时延改善；8线程的均值更高、CPU较低，存在并行度与执行效率取舍的线索。每档只有一轮，不能宣称16是最优线程数。nearest-rank下96样本的p99 **恰好是最大值**，不能据此排名。

入口形态的均值为regular 5.952、Poisson 6.083、burst4 6.177、burst8 6.798 ms；request peak分别1/2/4/8。这是短小图上的补充，不能替代80宽单请求内部扇出研究。

### 3.2 同seed的home → outstanding：整轮96请求

以下来自runner整轮摘要，按已打印的舍入值计算。每seed内到达hash、总迭代数和资源配置一致；正式合同配对仍由第5节的report核验。不同seed工作量不同，不混成同一负载。

| seed | home均值 ms | outstanding均值 ms | 均值变化 | 进程CPU变化 | home/outstanding p99 ms |
|---:|---:|---:|---:|---:|---:|
| 420 | 14.997 | 8.895 | −40.69% | +3.92% | 49.316 / 27.996 |
| 421 | 14.804 | 8.874 | −40.06% | +4.31% | 53.771 / 57.247 |
| 422 | 16.493 | 9.801 | −40.57% | +3.62% | 52.297 / 43.161 |

平均时延改善方向一致，p99并不一致。这支持继续研究借出，不能写成“p99必然改善40%”。CPU列包含整个进程的测量开销，不是纯路由指令成本。

其他策略整轮均值范围：queue 8.761～9.567 ms、active 8.775～9.597 ms、work 8.692～9.647 ms、cpu 8.687～9.839 ms；这是逐轮范围，不是置信区间或合并分位数。它们与O很接近，尚无可信的最优排名。相对各自同seed home，cpu的进程CPU增加8.21%～9.26%，高于其他借出策略约3%～4%；不能未经采样统计核验，将增量全部归因于扫描。

outstanding每轮迁移5184次，即平均54次/请求。这与首波80算子在本NUMA三池分摊相符：init还占home的一个O时，可能形成算子约26/27/27。**阈值16是开始寻找较轻池的门槛，不是每池16任务的硬上限。** 精确逐请求分布需查tasks.csv；总数不能证明每请求都恰好如此。迁移指算子改投池，不是内核线程迁移、已入队任务搬移或远端内存访问。

### 3.3 首波与Q/I/O：诊断窗口[1,6)，60请求

本节不能与前面的整轮96请求统计直接混加。

| seed | home首波跨度均值/中位数 ms | outstanding首波跨度均值/中位数 ms |
|---:|---:|---:|
| 420 | 11.542 / 9.102 | 4.704 / 3.709 |
| 421 | 11.746 / 8.430 | 4.759 / 3.498 |
| 422 | 12.677 / 9.270 | 5.106 / 3.692 |

首波callback峰值的逐请求均值，home约20.7～20.9、outstanding约48.6～48.8，与分散到三个cluster的机制相符。但callback处于开始和结束之间不等于一直在CPU执行，不能据此宣布48CPU同时运行或16CPU持续饱和30ms。

以seed420 home为例，容量C=16：

| 提交时Q/C | 算子数 | 算子开始前等待p50/p99 ms |
|---|---:|---:|
| 0 | 309 | 0.004 / 0.016 |
| (0,.5] | 758 | 0.033 / 0.088 |
| (.5,1] | 532 | 0.121 / 5.409 |
| (1,2] | 1082 | 2.032 / 10.384 |
| (2,4] | 2148 | 5.049 / 20.780 |
| (4,8] | 451 | 6.736 / 28.152 |

大量积压仍在Folly Q中，queue有效是合理的。本批**尚未充分复现“Q很低，但已出队线程在runq上排队”的盲区**，不能推翻阶段3或真实业务观察。提交点I大部分≤8，也不表示整个首波最多8个callback：home扇出投递跨度约0.08ms，快于worker陆续开始回调，提交采样偏向突发早期。

上述分桶是已选目标池的提交时快照，受批内顺序、算子成本和路由选择影响。5280算子来自60请求，不是5280次独立重复。不能变成Q→request p99的统一公式，不能将各算子p99相加得到图p99。

## 4. 影响下一步的证据缺口

1. **没有请求重叠。** signals全部request peak=1；12QPS间隔83.33ms，同一home每500ms收到一个请求。借出时其他cluster很空闲，尚未测试多个request争用借入池。
2. **全局负载偏低。** home约0.173～0.196进程CPU秒/请求，乘12QPS约2.08～2.35 CPU秒/秒，是96逻辑CPU时间上限的约2.17%～2.45%。这是需求量级估计，不是实测利用率或SMT吞吐换算；局部cluster仍可短时饱和。
3. **CPU信号有效覆盖未知。** signals未打印采样/回退统计。不能从校准home未开启采样时的false字段推断内核关闭schedstats，也不能因cpu和O迁移数相同就认定全程等价。
4. **没有数据亲和代价。** none/accesses0不能决定parent/home、跨NUMA或chunk最优值；首波depth1也不足以检验迁移后复用。
5. **尚无正式尾部结论。** 96请求整轮和60请求窗口均是初筛，仍需足量重复、留出seed、观测开销和邻近压力验收。

CPU策略应核对 `cpu-scored-without-fallback`、`fallbacks/evaluated`、原因及sampler扫描CPU/最大墙钟。50ms历史采样比典型9ms首波更慢，即使有效也不是当前微突发的即时信号。768个worker的采集成本需要单独评估。v2尚未将实际Folly queue kind/wakeup interval打印入summary，跨版本或与生产比较前应核对；此次相同构建内部对照不因此失效。

## 5. 下一步只做补报告和两轮校准

### 5.1 只读现有结果，不重跑18轮

runner最后的 `Report: ...` **只显示建议命令，没有执行报告**。下面审计已有CSV，打印同窗口请求指标、合法home差值、CPU有效评分及回退。回传完整终端文本即可，无需上传CSV或重复已有diagnose全表。

```bash
cd /home/bty/rank_demo/executor_demo
python3 experiments/affinity/report.py results/local/v2-cluster-signals-run-01/seed-* \
  --warmup-seconds 1 --window-seconds 5
cat /proc/sys/kernel/sched_schedstats
```

最后的只读开关值是当前状态，不能替代运行时summary；不要为补报告修改内核全局开关。

### 5.2 校准典型首波，暂不扩大策略矩阵

当前home首波中位数约8.4～9.3ms，部分长请求p95已约30～36ms。先去掉异质性，用**300万迭代**作为量级起点，分别校准16/128线程；典型首波20～40ms仅作参照，不保证恰好30ms，也不是onCPU饱和认证。每轮12请求/1QPS。

以下在子shell执行，失败时打印日志并停止；新输出目录存在时不覆盖。已有二进制即可运行，本次文档更新无需重编。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
CAL_ROOT='results/local/v2-cluster-scale3-calibration-01'
mkdir -p results/local
mkdir "$CAL_ROOT"
for NTHREADS in 16 128
do
  build-affinity/affinity_demo --cpus "$PARTITIONS" \
    --affinity cluster --threads "$NTHREADS" --policy home \
    --requests 12 --qps 1 --arrival regular --seed 420 \
    --widths 80,4 --depths 1,2 --model none --accesses 0 \
    --iterations 3000000 --jitter-percent 0 --long-every 0 \
    --max-inflight 32 --memory-mb 512 \
    --output "$CAL_ROOT/t$NTHREADS" > "$CAL_ROOT/t$NTHREADS.log" 2>&1 || {
      cat "$CAL_ROOT/t$NTHREADS.log"
      exit 1
    }
done
python3 experiments/affinity/report.py "$CAL_ROOT/t16" "$CAL_ROOT/t128"
python3 experiments/affinity/diagnose.py "$CAL_ROOT/t16" "$CAL_ROOT/t128" \
  --bin-ms 1000
)
```

关注operator CPU/wall、CPU/请求、首波跨度/峰值、投递lag和完成数。两档线程各自报告，不当成相同资源的策略配对。尺度合理后冻结工作，以128线程、12QPS、原请求异质性先只比较home/O，确认重叠、积压和lag后扩充候选，不直接跳到每算子100ms。

## 6. 随后推进的门槛

顺序为：两轮校准 → 小规模home/O干预并核对重叠 → 连续分支branch-rw/mixed校准 → parent/home、fork/all、chunk、同NUMA/跨NUMA逐项对照 → 足量请求与独立重复的尾部验证。

首波约30ms与整体约50%CPU不是同一个目标。迭代放大3倍，整体需求仍大约仅6～8个逻辑CPU时间等价；要增加持续竞争，应随后独立增加后续波或链工作并重新冻结合同，不能靠一个倍率强行同时拟合两者。增加深度也改变总工作、关键路径与复用，不能与原图直接做“只改调度”的收益比较。

下一批仍限定同六cluster、每cluster一池。128线程是观察过配的一档，不是生产结论，也不以历史四池单池active 20/80为拟合目标。现有结果足够继续阶段4，无需返回阶段3重跑全套。

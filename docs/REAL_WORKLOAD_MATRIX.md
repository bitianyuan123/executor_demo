# 阶段4 v2：真实入口微突发、四池与负载信号对照

本轮新增事实是16 QPS、4个池、每池128线程、worker没有细粒度CPU亲和，request按轮询选择home；观察到**单池**active均值约20、峰值约80。单池范围已明确，但active是Folly callback inflight还是OS runnable、采样与时间窗口定义仍待核对。因此实验把20/80作为诊断参照，没有直接写成阈值，也没有声称现有合成负载已经校准为真实业务。同窗单池均值可以相加，全局峰值不能直接写成4×80；none部署单池可竞争整个CPU并集，不能套用绑定16CPU的容量限制。

## 1. 新增变量及其准确含义

| 参数 | 作用 | 保持与变化 |
|---|---|---|
| `--arrival regular` | 固定请求间隔 | 原默认保持 |
| `--arrival poisson` | 指数分布请求间隔，固定seed | 与regular同固定工作，具体到达序列不同 |
| `--arrival burst --burst-size B` | 每批B个请求，批次周期`B/QPS`秒 | 平均批次强度为QPS；默认B=4 |
| `--burst-spacing-us U` | 同批第i个请求计划在`批起点+i×U`微秒到达 | 0为同时计划到达；`(B-1)×U`不能超过批周期 |
| `--sync-block-percent P --sync-block-us U` | 由seed/request/tag确定P%的算子，执行固定计算后在callback内同步sleep | 默认0/0；保留线程，**不是异步RPC，也不是100ms CPU的模拟** |
| `--pool-count N` | 令软件池数独立于硬件partition数 | 仅`affinity=none`可与partition数不同；原默认仍一partition一池 |
| `--policy active` | queue +已开始callback计数的评分对照 | callback包括睡眠、抢占，不能称kernel runnable |
| `--policy cpu` | 缓存的worker CPU/运行队列等待时间与queue组合评分 | 后台采样；无效或过期时明确回退到outstanding |
| `--cpu-sampling 0/1` | 单独启停后台采样以测其成本 | cpu策略默认1且不允许0；其余策略默认0 |
| `--cpu-sample-ms 50 --cpu-max-age-ms 150` | 采样间隔与路由可接受的最大样本年龄 | 是试验起点，不是已经验证的生产值 |

burst的计划时间为：

`scheduled_offset_ns(id) = floor(floor(id/B) × B × 1e9 / QPS) + (id mod B) × U × 1000`。

请求实际发起延迟单独记录，后续任务变慢不会修改计划时间。最后一批可不足B个；短运行的“首末到达跨度除请求数”不等于完整批周期定义的长期QPS。正式比较建议requests为B的倍数，并同时检查提交滞后和拒绝。

## 2. 固定工作量与真实尺度

每算子计算迭代数、访存次数依赖seed、request ID、逻辑算子位置。改策略、软件池数、亲和、到达序列或同步sleep不会重新抽取CPU/数据工作。输出新增：

- `planned_total_iterations/accesses`与每request均值，可核对同一固定需求；完整trace会核验实际总量。
- `fixed_work_signature`排除到达方式、QPS和同步sleep，仅用于证明固定计算/访存合同相同；它**不能替代相同到达序列的配对条件**。
- `work_signature`仍包括到达和阻塞合同；同seed、相同arrival hash与资源条件的不同策略才自动配对。
- `planned_sync_block_us`与实际观测sleep墙钟分开；实际时间可能包含额外调度等待。

现有`--widths/--depths`已允许20/80/128/400初始扇出，并支持后续fork与串行链。它们是压力端点，不是生产图大小分布。把第一波从20改到400而保持每算子工作相同，会增加总需求；不能把时延差全归因于并发形状。节点异质性用`--jitter-percent`和`--long-every/--long-factor`，范围为task或request。长尾倍率会提高平均工作，当前没有暗中减小主体工作来凑均值；比较不同异质设置时须检查实际总迭代/访问量并重新校准。

“100ms算子”需要先确定是线程CPU时间还是不含排队的墙钟。16 QPS × 80算子 × 0.1 CPU秒会要求128 CPU秒/秒，已超过独立实验96逻辑CPU的CPU时间上限；这不证明真实业务过载，因为真实算子数分布、100ms口径及executor占比尚不完整。不要用sleep 100ms补足合成算子时长，也不要把96个SMT线程直接当96个物理核吞吐。

## 3. 四池none与六cluster是不同实验问题

真实四池基线可沿用已验证的六个硬件partition，指定`--pool-count 4 --affinity none`。四个池都得到完全相同的96 CPU并集；发生器和辅助线程也在该并集。输入partition只用于描述经过验证的CPU全集，不会偷偷限制某池到一个partition。

none显式pool-count下的初始capacity按`CPU并集大小 / 软件池数 × capacity-scale`分摊，并受线程数上限约束。这个值只是评分的名义共享份额，**不是每池拥有独占CPU，也不是测得的有效计算能力**。

六池cluster与四池none之间同时改变软件池数、线程总数、CPU共享方式，不能作为单变量亲和收益。建议分两条路线：

1. 真实部署路线：4池×128线程、none、固定96CPU，只比较到达方式、评分信号、spill/continuation。
2. 硬件分组路线：6池、相同96CPU，固定每池线程数，在none/numa/cluster间比较。复用已有stage3/4资料，但不混入四池基线做自动配对。

## 4. 先做低压力入口校验，再增加到达/算子压力

以下命令适用于用户已隔离的CPU集合。每条matrix默认只写计划；加`--run`才执行。新输出目录不能复用。先编译：

```bash
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
BUILD_JOBS=1 ./scripts/build_stage.sh affinity
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
```

第一步是真实四池设置下的入口形状校验。1000000是迭代工作单位，不代表固定微秒。先一个seed、每例96请求；四例各约6秒计划投递时间，排空另计：

```bash
python3 experiments/affinity/run_matrix.py \
  --binary build-affinity/affinity_demo --suite real-baseline --seeds 420 \
  --output results/local/v2-real-entry-01 --run --base-args \
  --cpus "$PARTITIONS" --requests 96 --max-inflight 128 --memory-mb 512 \
  --widths 20,4 --depths 1,2 --model none --accesses 0 --iterations 1000000

python3 experiments/affinity/report.py results/local/v2-real-entry-01/seed-*/
```

`real-baseline`明确设置4池/128线程/none/16QPS，只改变regular、poisson、4请求微突发、8请求微突发。不要将不同arrival hash的运行称为同轨迹配对。先查看实际CPU/request、CPU与墙钟差、请求重叠、每池callback occupancy、提交滞后、拒绝与工作总量。

如果负载太轻，保持16 QPS，按2倍增加iterations；如果后续要研究数据亲和，改为branch-rw/mixed后重新测CPU成本。每次只改变一个维度。没有业务CPU/请求预算和active口径前，不为追求“刚好80active”任意调整一组参数。

第二步在选定负载、同一burst到达序列下比较六个信号策略。完全相同的request、4个请求一批、RR四池会产生近似对称负载；即便算法正确，也可能没有明显可借用的低负载池。因此这个筛选加入可重复的**合成request异质性**（25%抖动，约1/8请求为4倍工作），各策略保持同一逐request工作，不把这个分布声称为生产事实。先用constant estimate，避免work策略拿到精确先验而获得不公平解释：

```bash
python3 experiments/affinity/run_matrix.py \
  --binary build-affinity/affinity_demo --suite signals --seeds 420,421,422 \
  --output results/local/v2-real-signals-01 --run --base-args \
  --cpus "$PARTITIONS" --pool-count 4 --threads 128 --affinity none \
  --qps 16 --arrival burst --burst-size 4 --burst-spacing-us 100 \
  --requests 96 --max-inflight 128 --memory-mb 512 \
  --widths 20,4 --depths 1,2 --model none --accesses 0 --iterations 1000000 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope request --estimate-mode constant

python3 experiments/affinity/report.py results/local/v2-real-signals-01/seed-*/
```

这仍是筛选窗口，不是p99验收；96请求的p99接近最大值。筛出候选后增加请求数、预热窗口、独立seed，报告每轮p99与配对差异，而不是将所有任务混成一个巨大样本。已有低压力下p99接近的策略不能被宣布等价；先核验决策实际发生、有效CPU样本覆盖以及请求是否重叠。

第三步同步阻塞反例只用于检验信号含义，不代表RPC：

```bash
python3 experiments/affinity/run_matrix.py \
  --binary build-affinity/affinity_demo --suite blocking --seeds 420 \
  --output results/local/v2-blocking-01 --run --base-args \
  --cpus "$PARTITIONS" --pool-count 4 --threads 128 --affinity none \
  --qps 16 --arrival burst --burst-size 4 --burst-spacing-us 100 \
  --requests 32 --max-inflight 128 --memory-mb 512 \
  --widths 20,4 --depths 1,2 --model none --accesses 0 --iterations 10000

python3 experiments/affinity/report.py results/local/v2-blocking-01/seed-*/
```

该suite保持计算/访问量，比较0%与50%算子同步sleep 10ms，以及home/active/outstanding/cpu。不同阻塞合同不自动配对。active和outstanding可能因睡眠而高；CPU使用可能低，但空闲worker数量仍影响能否接新任务。CPU混合指标也不是同步阻塞场景的完美调度器。

## 5. CPU采样的证据与限制

采样器按固定worker TID集合后台读取Linux线程schedstat，计算同窗口CPU时间增量和runnable等待时间增量除以墙钟时间。它没有在`add()`热路径读取proc文件；路由读取已发布的缓存。两个值是历史窗口的累计需求，不是瞬时idle线程数。50ms窗口会漏掉比它短得多的微突发，因此CPU只能作为慢反馈对照，不能替代当前queue/outstanding。

运行队列计账不可用、样本初次未建立、线程消失或样本陈旧时，CPU策略退回outstanding，并在summary/任务trace记录次数与原因。报告不能把这种运行的改善称为CPU信息收益。可只读检查：

```bash
cat /proc/sys/kernel/sched_schedstats
```

本实验不自动更改内核全局开关。报告列出采样次数、失败组数、总扫描CPU/墙钟和最大单次扫描墙钟。cpu策略的额外采样成本纳入整策略对照；要分离该成本，可为home与候选同时设置`--cpu-sampling 1`复测。trace开关对照仍独立进行。

## 6. 终端结果如何解读

report按计划到达选择request cohort，窗口后才完成的请求仍保留；要求全请求有终态。拒绝/失败运行不会成为性能配对。请求mean/p95/p99、该cohort算子CPU/墙钟和等待，与整轮process CPU/request分开显示。

callback occupancy由所有与墙钟测量窗口相交的`start_ns..finish_ns`区间重建，输出每池与全局的时间加权均值/峰值，包含同步睡眠和被抢占时间；它和“在提交时采到的inflight平均数”口径不同，也不等于Linux runnable/on-CPU。真实20/80已知是单池值，只有再确认同样统计定义及时间窗口后才能比较。报告的全池峰值来自合并区间，并非各池峰值相加。

下一阶段再研究branch数据亲和、返回home/跟随parent、仅fork/全部就绪点、迁移预算和chunk。先固定本轮负载与评分，避免同时改请求到达、工作量、数据模型、池数和算法后无法解释收益。

# ARM v2：4002补证、校准收束与请求竞争对照

依据用户回传 `4002.log`，SHA256 `2ccf0d604549cf79bcfe1f48413e9ca19b95106e3fdee6035798dca13df584ca`。前半是[4001](TARGET_V2_4001_REVIEW.md)既有18轮signals的补充报告，后半才是两轮新校准；不能把前半当成新的独立重复。本文核对终端输出与源码，未收到目标机原始CSV，未在开发机重跑ARM性能实验。

## 1. 结论：校准完成，可以继续推进

目标仍是六个已核实硬件cluster各一池，NUMA为0和2，每池名义容量16。三项进展已经明确：

1. 18轮signals的目标端报告完成，15组candidate/home通过工作合同与checksum配对审计，均无失败/拒绝。
2. 三轮cpu策略的全部8640个记录评分点均回退O，CPU有效评分为0；当前结果不是CPU信号收益。
3. 300万迭代的home首波跨度约23ms，达到此前20～40ms量级参照。冻结该工作量，不再为精确拟合30ms追加校准。

下一批只比较home/O，同时设置regular主线和受控burst8竞争对照。CPU采样暂不加入主线；数据亲和、回迁与跨NUMA仍待后续连续分支负载验证。没有转向历史四池配置，也不要求目标cluster复现历史active 20/80。

## 2. 旧signals补证：按[1,6)窗口解释

每轮窗口内60请求、5460回调，整轮仍96请求。报告先检查终态、节点和工作合同，再核对同seed到达/资源/工作签名、请求checksum以及逐task迭代/读写/checksum，最后输出15组配对差值。这补齐4001的合同证据；我们依据目标端报告完成无错误判断，没有在本地重新审计原始CSV。

| seed | home请求均值 ms | O请求均值 ms | 均值变化 |
|---:|---:|---:|---:|
| 420 | 15.021 | 8.847 | −41.10% |
| 421 | 15.586 | 9.004 | −42.23% |
| 422 | 16.710 | 9.610 | −42.49% |

这与4001整轮均值改善约40%的方向一致，但样本总体不同，不能混用。60请求的nearest-rank p99仍是最大值，不能据窗口p99全改善覆盖掉4001整轮某一seed的退化。全部18轮整轮请求峰值仍为1：这是单request扇出借出证据，尚未证明跨request竞争下的收益。

## 3. CPU策略：确认回退，并量化采样成本

三轮均为 `fallbacks/evaluated=8640/8640`、`cpu-scored-without-fallback=0`，原因均为 `missing_cpu=90, missing_runqueue=8550`。运行时采样器记录 `runqueue_accounting_enabled=false`，报告后只读 `/proc/sys/kernel/sched_schedstats` 也输出0。因此应将这三轮解释为 **O回退决策加后台采样开销**，不能比较成有效CPU算法对O的收益。

| seed | 采样CPU秒 | cpu策略相对O多用进程CPU秒 | 相对O增幅 |
|---:|---:|---:|---:|
| 420 | 0.809088 | 0.815939 | +4.54% |
| 421 | 0.805886 | 0.822565 | +4.74% |
| 422 | 0.809619 | 0.865281 | +4.43% |

本表为整轮96请求口径。每轮扫描143次、每次768个worker，各读stat和schedstat；平均每次扫描CPU 5.636～5.662ms、墙钟5.670～5.685ms，最大单次墙钟9.106～10.805ms。额外进程CPU与约0.81秒扫描CPU接近，后者能解释大部分增量，但这不是严格因果分解。

热路径只读已发布缓存，procfs在后台；后台开销仍消耗进程CPU。采样实现是扫描结束后再等50ms，实际循环约55.7ms，不是严格50ms。`failed_groups=6`符合首次没有差分基线时的六组失效，不能解释为143轮全失败。

本轮回退符合设计：没有runq等待信息时，低onCPU并不保证池空闲。后续可单独核验CPU采样启用条件、有效覆盖与成本，双方使用相同采样设置；本阶段无需为继续实验修改全局内核开关，也不将CPU-only作为空闲判据。O是当前干预基线，不是已验证通用于阻塞/异质成本的最优信号。

## 4. 新校准：16与128线程

每档12请求、1QPS、home、none/accesses0、宽80/4、深1/2、300万迭代，无抖动/长尾。两档总迭代均31.68亿、checksum相同，全部完成，无迁移或请求重叠。线程数不同，报告不把它们当成同资源的L2策略配对。

| 指标 | 16线程/池 | 128线程/池 |
|---|---:|---:|
| 请求均值 ms | 31.339 | 31.755 |
| 首波跨度均值/中位数 ms | 22.531 / 21.843 | 23.299 / 22.799 |
| 算子CPU/墙钟均值 ms | 4.286 / 4.323 | 4.426 / 5.236 |
| 进程CPU ms/请求 | 378.022 | 392.526 |
| 首波callback峰值的逐请求均值 | 16.000 | 22.417 |
| 首波callback峰值最大值 | 16 | 25 |

128线程的请求均值约+1.33%、进程CPU约+3.84%，并没有用更多worker换来此负载下的平均时延收益。单轮12请求不能判断最优线程数或精细p99。callback墙钟包括抢占及内部提交，不能把墙钟−CPU直接当成已测内核runq等待；首波跨度也不能证明16CPU持续onCPU。

这些数据已经足够冻结基础计算量。128线程保留为过配档，以便后续研究Q盲点和L2；它不是生产线程数推荐。当前提交快照仍主要是Q积压，不能宣称Q盲点已经充分覆盖。

## 5. 下一批设计：保证竞争对照有信息

只放大计算量并恢复原请求异质性时，regular 12QPS间隔83.33ms，同home池RR复用间隔500ms。普通请求粗略24～40ms，长请求约95～159ms，是基于校准的线性估计，不是运行承诺。可能仅有全局少量重叠；O缩短长请求后甚至仍可能peak=1，不能据此宣称同池竞争已经被测到。

因此用两个独立到达合同，每个内部只改home/O：

| 到达 | 固定工作 | 目的 |
|---|---|---|
| regular，12QPS | 80/4、1/2、300万迭代；请求级±25%、约1/8×4 | 延续主线，观察较长请求与邻池干扰 |
| burst8，批内100μs，平均12QPS | 与regular同seed逐请求工作一致 | 0.7ms内计划投递8请求到6home池，必有两池各接2请求；检验同池与借入池竞争 |

burst是受控压力，不声称真实入口分布如此；实际同时在途仍检查lag和peak。每批间隔约666.67ms，批间可能很空，不能称持续高负载或已复现业务整体50%CPU。按当前校准和该seed工作倍率外推，总体需求仅约6～7个逻辑CPU时间等价。不能为拟合50%直接把每算子提高到100ms。

共2到达×2策略×3seed＝12轮，每轮240请求，约4分钟投递时间，加启动/排空/报告。固定六cluster、128线程/池、同NUMA借出、parent/all/chunk1、容量16；不加入CPU、work、跨NUMA或其他线程档。每seed内策略顺序由runner打乱；两个到达族分别分析，不跨到达合同计算策略配对。

预先固定窗口 `[2,18)`：regular和burst8均为request ID 24～215，共192请求；burst为24个批次。即使请求数相同，也不能把192条当192次独立重复。尾部分位数仅作机制初筛，不能确定生产最优策略。不同seed工作倍率不同，禁止只挑有利seed或为每策略重新校准工作量。

## 6. 执行与终端回传

runner新增可选 `--cases`，只筛选所选suite已有case，保留计划、二进制/源码摘要、资源元数据和逐seed随机顺序。未指定时旧套件行为不变。更新Python和文档即可，**不需要重编C++或Folly**。

以下使用新输出根目录，已有目录会停止，不覆盖历史结果。每个到达族跑完直接打印report和diagnose，不再遗漏runner只提示的 `Report:` 命令。报告失败或运行失败均停止。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
git pull --ff-only
PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
PAIR_ROOT='results/local/v2-cluster-scale3-pair-01'
mkdir -p results/local
mkdir "$PAIR_ROOT"
for ARRIVAL in regular burst
do
  python3 experiments/affinity/run_matrix.py \
    --suite routing --cases home,outstanding --seeds 420,421,422 \
    --output "$PAIR_ROOT/$ARRIVAL" --run --base-args \
    --cpus "$PARTITIONS" --affinity cluster --threads 128 --capacity-scale 1 \
    --requests 240 --qps 12 --arrival "$ARRIVAL" \
    --burst-size 8 --burst-spacing-us 100 \
    --widths 80,4 --depths 1,2 --model none --accesses 0 --iterations 3000000 \
    --jitter-percent 25 --long-every 8 --long-factor 4 \
    --work-scale-scope request --estimate-mode constant \
    --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
    --cross-numa 0 --max-hops 4 --cpu-sampling 0 --trace 1 \
    --max-inflight 128 --memory-mb 512
  python3 experiments/affinity/report.py "$PAIR_ROOT/$ARRIVAL"/seed-* \
    --warmup-seconds 2 --window-seconds 16
  python3 experiments/affinity/diagnose.py "$PAIR_ROOT/$ARRIVAL"/seed-* \
    --warmup-seconds 2 --window-seconds 16 --bin-ms 4000
done
)
```

`--cases`须放在`--base-args`之前。routing保留默认high=1、low=.5、min-gain=0；本轮不调阈值。regular忽略burst参数，不会变成burst。`long-every=8`由hash决定约1/8概率，不是固定每第8请求。

回传以上终端输出，原始CSV留在目标机；无需重复4001/4002。检查顺序为合同/全完成与拒绝 → 提交lag/积压趋势 → 请求峰值/首波 → CPU/请求 → 同seed同到达的均值及分位差。4秒趋势窗用于检查增长，不能显示亚毫秒burst内部；首波诊断补充单请求内部时序，内核onCPU仍需trace。

若regular仍不重叠，它保留单请求对照价值；burst8提供明确的计划竞争压力。若O也有重叠，不能直接当成算法失败，先看等待与总请求时延。如果burst中O退化，应检查多个请求借入同一池、预占/阈值与局部并发，而非只增加迁移强度。出现持续lag、积压或拒绝时保留结果，先解释容量，不继续自动加压。

完成这批后进入首波depth≥2的branch-rw/mixed亲和实验，重新校准并冻结新工作合同，再比较parent/home与fork/all；本轮none/depth1不承担该结论。正式p99、多次独立重复、后续波持续负载和跨NUMA代价继续作为后续验收，避免在无数据依赖图上过早选生产最优算法。

## 7. 本次工程改动与本地验证

只增加Python runner的case筛选及其验证、更新报告和入口文档；没有修改C++负载、调度策略、观测热路径或Folly。验证包括所有默认套件与筛选后的三seed顺序、metadata、10类非法case输入在创建目录前拒绝，以及既有22轮缩放功能运行。

另生成本节12条ARM尺度计划但未执行；用两个本地小运行验证筛选后的`--run`确实只执行home/O，并通过report合同配对与diagnose。6份文档的16个shell片段、58个本地链接及表格差值已检查。这些是工具与指导验证，不是新的ARM性能数据。

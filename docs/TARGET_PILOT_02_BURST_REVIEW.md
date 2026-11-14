# ARM pilot-01：s3-burst 审阅及后续验证

依据：用户提供的 `0002.log`，包含9轮终端摘要、独立任务分桶分析和配对报告。这里只复核文本与分析代码，没有收到目标机原始CSV，未在本地重新计算ARM原始任务数据。沿用二进制SHA256 `f4efe69dbd41f053324f25b5b8080ddf8afe4950c1357bbbf7a6b4677c7c0fc9`。

## 本轮结论

**同样240 task/s和工作预算，K=100集中到单池相比分散到六池，计划到达至完成均值增加约257.737ms，增量主要发生在提交至回调开始之间。** 三个seed方向一致，回调墙钟均值仍接近100ms。这支持本合成模型下“整体平均负载低，局部突发仍可显著增加等待”的解释。

Folly队列在集中场景下包含很强的等待信息；但queue=0不保证立即执行，不能单凭非零队列阈值覆盖所有忙碌状态。下一步先用现有CSV做整轮留出预测，再做cluster下8/16/64/256线程扫描；随后完成16线程部署基线及高线程三亲和对照。调整执行顺序，不改变既有工作量或比较合同。

## 完整性和比较口径

- 9轮退出码均为0，整轮每轮14,400个任务全部完成，总计129,600个；没有拒绝、取消、失败，守恒成立。
- 测量窗口统一为 `[10,60)`，每轮cohort 12,000个任务；同seed的三个case目标预算完全相同。目标机配对工具报告两组合同均为 `descriptive_only`，没有 `invalid_contract`。
- 所有case为cluster亲和、6池×16worker、相同96逻辑CPU并集、full计量、同二进制。smooth与spread改变投递集中程度，spread与local只改变批次内任务的池放置。
- 每轮窗口前后完成率跟随240 task/s，前后半时延没有共同增长趋势；local排空约0.238～0.296秒，未出现持续过载迹象。无采样失败或截断。spread的498个采样时刻来自任务在投递时限前已全部完成并停止采样，不是截断告警。
- 只有3个独立seed配对，按设计不输出置信区间。每轮12,000个任务不能当成12,000次独立实验。
- CPU限额证据来自上一批环境采集；本日志没有新的前后环境快照，不将上批“无带宽限流”扩写为本轮独立重新验证的事实。

## 定量结果

下表均值列为三个运行均值的等权平均，p99列为三轮各自p99的范围，不是合并p99。由终端三位小数计算的数值有舍入误差；总时延配对差优先使用配对报告的原精度数字。

|case|planned→完成均值 ms|submit→开始均值 ms|callback墙钟均值 ms|各轮planned p99范围 ms|
|---|---:|---:|---:|---:|
|smooth|100.177|0.012|100.111|148.564～149.029|
|spread|102.722|2.246|100.178|180.648～183.399|
|local|360.460|260.262|100.100|690.684～691.584|

分解关系针对同一个cohort的均值成立：

`planned→完成 = planned→提交 + 提交→回调开始 + 回调墙钟`。

- smooth→spread：总时延均值增加约2.546ms，其中等待约增加2.234ms，投递滞后约增加0.245ms，回调约增加0.066ms。分散突发并非完全没有排队：100个任务分到6个各16worker的池，部分池收到17个任务。
- spread→local：总时延均值增加约257.737ms，等待约增加258.015ms；回调约减少0.077ms、投递滞后约减少0.200ms，抵消很小一部分等待增量。三个seed总时延差约为257.969、258.159、257.085ms。
- “提交→回调开始”混合入队、通知、worker调度及入口成本。此结果不直接分离纯队列驻留时间与内核runq时间。
- 上式不能用来分解p99；不能把几个分量的p99相加，或将两个p99之差视为某一任务的延迟分量。

每轮整轮user+system约1440 CPU秒，与14,400×100ms预算一致。不同case运行和排空时间稍异，不应将分母差异解释为工作量改变。CPU时间约24秒/秒是本CPU预算模型的时间占用，不等同24个物理核的有效吞吐。

## 队列指标的证据与盲区

local三轮native_queue→等待Pearson均为0.994。按提交前队列分桶：

|队列桶|三轮桶内平均等待范围|
|---|---:|
|0|约0.018ms|
|1～3|约0.027ms|
|4～15|4.385～6.726ms|
|16～63|213.345～222.116ms|
|64及以上|471.194～478.997ms|

这支持队列作为本场景的繁忙信号，但不是通用线性标尺：它同时反映burst中的提交位置、前序工作和固定worker并行宽度。三轮高度相关不是已证明预测泛化，也未测出线上读取成本。

spread里99.09%～99.31%的任务提交前queue=0，但零队列桶平均等待仍有1.866～1.881ms，明显高于smooth约0.012ms。它不是“99%的任务都等了1.87ms”，均值可能由少数慢任务拉高；具体超过1ms/5ms的比例应读取 `analysis.json` 的 `queue_zero`，不能从桶均值推算。

queue=0只描述当时的队列读数，不包含已经执行的任务，也不包含即将提交的当前任务。所有worker正在执行时，即使队列暂空，新任务仍可能等待已有任务结束。通知、唤醒或内核调度也可能参与延迟，现有文本无法逐项归因。

候选补充指标的含义：pending是已提交但未进入回调的任务数，inflight是已进入回调但尚未完成的任务数；它们也不是内核正在运行的线程数。work保留未完成任务的完整估计预算，并非精确剩余CPU。是否值得增加这些指标，要看与queue相同留出样本上的误差降低，以及后续单独测得的读取/更新成本。

## 两个容易误读的摘要字段

1. `callback_mean`是10～60秒cohort的回调墙钟，`callback_cpu_mean_ms=100.001`来自整轮summary。不能直接相减；例如seed422的local回调均值99.921ms小于整轮CPU均值，不是时钟反常。需要wall−CPU时必须从同一批tasks.csv行计算，而且差值仍不等于纯runq等待。
2. spread的 `successful_unfinished(start/mid/end)=0/0/0`是三个边界时刻快照。规则burst可在这些时刻落入批次间隙，不表示平均没有在途任务。周期queue_sum同样不能不加检查地当成精确时间加权平均积压。

本轮system/(user+system)约为smooth 38.6%～38.8%、spread 41.0%～41.3%、local 45.4%～46.6%。仍需后续固定迭代工作量验证，不能将比例差直接归于Folly，也不能声称CPU预算模型测到了相同有用计算量的效率差。

## 下一步一：只分析现有数据

无需重编译、更新脚本或再施压。现有predict对当前单一manifest按case与配置分成三个组，每组3个seed；每折用另外两整轮训练、留下一整轮测试。五个模型在full计量下均有相应特征；预期每组各模型3/3折、累计36,000个留出任务。

```bash
cd /home/bty/rank_demo/executor_demo
TARGET_RUN='results/local/arm-pilot-01'

python3 stages/03-independent/predict.py \
  "$TARGET_RUN/s3-burst/manifest.json" \
  --warmup-seconds 10 --steady-end-seconds 60 \
  --output "$TARGET_RUN/reports/s3-burst-prediction"
cat "$TARGET_RUN/reports/s3-burst-prediction.md"

python3 - "$TARGET_RUN/s3-burst/analysis.json" <<'PY'
import json, sys
from pathlib import Path
for run in json.load(open(sys.argv[1])):
    print(Path(run['path']).name, 'queue_zero:', run['queue_zero'])
PY
```

检查同case、同覆盖任务数下queue+target、pending+target、queue+inflight+target的MAE和p95绝对误差。报告“MAE减少”列以target-only为基线，不是以queue为基线；比较pending是否优于queue要直接比较两行误差。预测的完成时延是submit→end，与本报告planned→end不同。三个独立留出轮只支持描述性验证，不能据此确定生产最优指标或阈值。

当前模型是线性ridge基线。若加入inflight未改善，不能据此证明该指标没有价值，忙闲阈值、剩余服务时间可能需要非线性模型；优先判断local/spread的等待误差，smooth几微秒级误差变化不作为选型依据。

后续工程复查项：predict分组字段还未覆盖burst_placement、mean_normalization、estimate_jitter，也未拒绝重复seed；本次case名称不同、各case参数一致且seed各异，不受影响。当前只输入本次一个manifest，暂不混入其他批次同名case。这些检查需在扩展跨批预测前完善。

## 下一步二：cluster下线程数扫描

优先执行已生成的s3-threads，保持240 task/s、K100 local、工作预算和CPU集合不变，只改变每池8/16/64/256线程。共12轮×60秒，约12分钟加排空和分析。与上面的离线分析串行执行，避免分析进程干扰压测。

```bash
taskset -c 0-47,192-239 python3 experiments/independent/scripts/run_matrix.py \
  --binary build/experiments/independent/independent_load \
  --config "$TARGET_RUN/plan/s3-threads.json" \
  --output "$TARGET_RUN/s3-threads"

python3 scripts/compare_runs.py \
  --manifest "$TARGET_RUN/s3-threads/manifest.json" \
  --spec "$TARGET_RUN/plan/s3-threads-contrasts.json" \
  --output "$TARGET_RUN/reports/s3-threads-paired"

python3 scripts/terminal_report.py \
  --root "$TARGET_RUN" --section runs --phase s3-threads
cat "$TARGET_RUN/s3-threads/analysis.md"
cat "$TARGET_RUN/reports/s3-threads-paired.md"
```

预声明合同以t8为baseline分别比较t16/t64/t256；不要看完数据后只选最有利的一对。线程增多若让queue和submit→start下降，却使callback墙钟上升、总时延不降，说明仅以队列判断繁忙可能遗漏回调期间的资源竞争；没有内核证据时不把全部增长叫runq。

新批次保留t16作为共同参考，但正式线程效应使用本批次内部的完整seed配对，不用旧burst-local替代t16后跨批拼接。

之后仍需16线程的三亲和部署基线，并补256线程（或实际生产线程数）的none/numa/cluster对照。16worker的单池自身最多同时执行16个回调，不能完整检验数百worker在不绑时借用更大CPU范围的能力。当前结果不直接回答绑定亲和造成的生产时延差，也不回答DAG迁移及回迁策略。

已有结果目录必须保留；runner发现同名目录会拒绝覆盖，若已经执行过相应阶段，先打印旧结果。每批完成后回传终端文本并审阅，不一次启动后续全部矩阵。

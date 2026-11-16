# 阶段3：微突发、持续计算与目标池接纳验证指南

**本轮ARM验证已完成，结果见[3004分析报告](ARM_3004_REQUEST_WAVE_REPORT.md)。不需要再次校准或补跑三组。** 下列步骤保留作复现指南；本轮观察到source_only的任务完成p99比home低约76%，增加目标<0.5门槛后的改善为约36%。不据此扩展阈值或亲和性矩阵。

若需要进一步观察20ms微突发期间和后段CPU曲线，新增的[5ms事件采集指南](CPU_CURVE_GUIDE.md)可在相同负载下自动采集、校验和出图。该观测需要新运行，不能从3004的100ms汇总还原，不增加负载矩阵。

本轮替代此前额外的cluster/none四组计划：一次短校准，随后一种冻结负载、三种策略、各一次。不扫描阈值、不重复三次、不运行历史大型矩阵。只更新阶段3独立工程，不涉及DAG和Folly库开发。

## 1. 负载设计与明确假设

已有[ARM报告](ARM_3003_METRIC_POLICY_REPORT.md)保留，但其2.4批/s、每批100项、同home每2.5秒一批的输入不能与本轮直接配对。本轮研究：其他池也在持续工作时，分流是否有效，目标接纳门槛是否有益。

|参数|ARM默认值|解释|
|---|---|---|
|请求到达|regular 12/s|约每83.333ms一个，home按6池RR，同home约500ms一个|
|初始阶段|80项，20ms窗口|均匀释放在`[0,20ms)`；近似30ms内80+任务，均匀分布是实验假设|
|后续阶段|80项，400ms窗口|均匀释放在`[20,420ms)`；数量与持续时间是合成假设，尚非实测DAG|
|任务工作|平均25ms线程CPU时间，均匀±50%|纯CPU忙循环，精确归一化均值；无RPC、依赖或特殊长尾|
|成本估计|每项统一25ms|不使用每项真实随机预算作为路由预知信息|
|线程池|6池，每池128线程，cluster亲和|每池16逻辑CPU，C=16；Folly原生CPUThreadPoolExecutor|
|CPU集合|`0-47,192-239`|分组`0-15;16-31;32-47;192-207;208-223;224-239`|
|NUMA|从对应CPU的sysfs归属读取|不假定编号连续，此机此前为0和2|
|正式窗口|60秒请求到达，预热10秒|选择请求计划到达`[10,60)`的完整任务集合：600请求、96000项|
|随机性|seed 52000|三策略同一输入，仅执行顺序随机一次|

预算为`12×(80+80)×.025=48 CPU秒/秒`，按96逻辑CPU归一化是50%的**名义CPU时间需求**，不等于实测利用率或48个物理核的计算吞吐。

不能直接用100ms：`12×80×.1=96 CPU秒/秒`，仅初始部分已经耗尽这组96逻辑CPU的名义时间预算。因此25ms只是明确、可调整的合成起点，**不能称为真实业务100ms算子的复现**。真实100ms的计时口径、50%的CPU集合和executor占比仍待人工复核。

所有任务先生成计划，提交器按全局就绪时间排序，多请求的初始和后续任务自然交错，不会等一个请求的后续400ms全部投递后才接收下一请求。末尾请求在60秒之后的后续任务仍然提交，慢完成保留至排空。这个时间表没有图依赖，不能把任务p99称为DAG请求p99。

## 2. 必要的三组对照

|策略|源池规则|目标池规则|回答的问题|
|---|---|---|---|
|home|留在home|无|关闭均衡基线|
|source_only|提交前N/C>0.8|选更轻的池|较早分流是否有益|
|guarded|提交前N/C>0.8|目标提交前N/C<0.5，且更轻|接纳限制是否有益|

三组输入、资源、CPU预算、线程和采集模式一致。没有合格目标就留home，不丢弃。只对尚未add的新任务选池，不移动已排队/已运行任务。

C=16时，源N≥13触发；目标N≤7可接收，接收后允许到8，即0.5。这不是CPU利用率80%/50%，也不是池总N的硬上限，目标池自己的请求仍会到来。当前单生产者在add前预留N；多生产者的严格并发预留不在本次范围。

`source_over_threshold`、`no_eligible_target`、`no_improvement`仅统计threshold策略的决策，home为0。报告区分全部任务分母与已触发任务分母。guarded较慢且大量触发后无可接纳目标，可能说明门槛过于保守，不能直接判N无效。0.8/0.5是候选规则，不宣称最优。

## 3. CPU与时延怎样观察

运行器独立于C++进程，每100ms批量读取`/proc/stat`的所选CPU累计时间，记录每组及并集的execution/iowait/steal比例。execution包含user、nice、system、irq、softirq，guest不重复相加；异常/缺失显式标记。CPU数据**不参与发压、路由或接纳决策**。

CPU组与池的对应依赖cluster固定绑定；统计还包括同组其他进程/中断，测试隔离条件必须保持。C++的`epoch_monotonic_ns`用于对齐分析窗口。统计粒度见[内核文档](https://docs.kernel.org/filesystems/proc.html)：100ms会平滑20ms脉冲，不能据此证明或否定瞬时占满。本轮核对任务实际投递与CPU趋势；不引入全程高频调度跟踪。

逐任务把提交前Q/N与该任务自己的时延配对，N尚不含当前任务：S=`end-submit`是入口预测的主要完成时延；W=`start-submit`，X=`end-start`；L=`end-planned`保留发生器滞后D=`submit-planned`。S=W+X、L=D+S成立，但p99不能相加，也不平均每秒p99。

指标表只取home，分别报告initial、steady和全部任务。固定N/C、Q/C桶为`[0,.5),[.5,.8),[.8,1),[1,2),[2,4),[4,+∞)`，列任务/请求数及S均值/p99，低尾部样本只作描述。不再要求三seed模型或相关性必须线性。策略表同时保留S/L、W/X、提交滞后、CPU、分流及无目标比例。

## 4. 测试机复现步骤（已完成，无需重跑）

从阶段3仓库根目录执行，安装前缀按实际路径调整，不重新构建Folly，不进入DAG目录：

```bash
git pull --ff-only origin experiment/stage3-load-balance-20261115
cmake -S experiments/independent -B build-independent \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/home/bty/rank_demo/folly/_install
cmake --build build-independent -j2
ctest --test-dir build-independent --output-on-failure
```

阶段3独立构建不要求DAG的GTest。先生成计划，只打印预算，不发压：

```bash
taskset -c 0-47,192-239 python3 experiments/independent/scripts/request_wave.py plan \
  --deployment arm --out results/request-wave-arm
```

计划校验CPU和NUMA，保存共同参数、指纹和一次执行顺序，不静默覆盖已有计划。随后一次短校准：

```bash
taskset -c 0-47,192-239 python3 experiments/independent/scripts/request_wave.py calibrate \
  --plan results/request-wave-arm/plan.json --binary build-independent/independent_load
```

只有home、20秒请求到达、预热5秒，不进入正式配对。确认没有拒绝/取消/缺任务、投递没有持续落后、CPU范围正确、压力与假设无数量级偏离。校准不会自动调整参数凑到50%。如需修改，用`plan --out 新目录 --cpu-us 新预算`生成新计划，保留旧证据；不能为不同策略分别调整预算。

然后同一计划运行三组，各一次：

```bash
taskset -c 0-47,192-239 python3 experiments/independent/scripts/request_wave.py run \
  --plan results/request-wave-arm/plan.json --binary build-independent/independent_load
python3 experiments/independent/scripts/analyze_request_wave.py \
  --plan results/request-wave-arm/plan.json --output results/request-wave-arm/analysis
cat results/request-wave-arm/analysis/report.txt
```

正式发压约3分钟，另有启动、后续释放和排空时间。相同命令可跳过已完整成功且指纹匹配的结果；失败目录/更换二进制不会被静默覆盖或混合。采集异常要保留manifest和错误信息。

测试环境无法上传文件时，只需贴回`report.txt`。原始CSV和manifest留在测试机；完整分桶在`analysis/buckets.csv`，必要时再打印，不要求一次收集全部原始任务。

## 5. 怎样判读并结束本轮

先检查输入配对与完整性，再比较source_only−home和guarded−source_only：前者看分流收益，后者隔离接纳门槛影响。结合CPU组差异、W/X、投递滞后与无目标比例解释原因。所有时延都是完整到达集合统计，包含窗口后完成的慢项。

一次结果仅作该固定输入下的描述性比较，不给置信区间，不搜索最佳阈值。三组可解释后结束本轮，收益不明显也保留结果，不自动补线程数/突发宽度/none/numa扫描。后续依赖图、关键路径、RPC和数据局部性由阶段4独立研究。

## 6. 开发机快速功能演练

```bash
python3 experiments/independent/scripts/request_wave.py plan \
  --deployment dev --out build-wave-smoke --duration 2 --warmup 0.5
python3 experiments/independent/scripts/request_wave.py run \
  --plan build-wave-smoke/plan.json --binary build-independent/independent_load
python3 experiments/independent/scripts/analyze_request_wave.py \
  --plan build-wave-smoke/plan.json --output build-wave-smoke/analysis
```

dev自动缩小资源和CPU预算，保留功能路径；短样本不能用于ARM性能推断。

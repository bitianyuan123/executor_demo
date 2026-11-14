# 阶段2补充实验：读取负载与维护统计的成本

这个工程只有一个成本探针，直接使用原版 `CPUThreadPoolExecutor`，不包含路由、DAG 或实验性 Folly 扩展。它回答“接口读取多贵、共享计数维护是否会争用”，不能单独回答“该指标能否预测业务时延”；预测能力由[阶段3](../../stages/03-independent/README.md)验证。

## 独立构建和运行

先按[根目录说明](../../README.md)安装固定版本 Folly，然后从仓库根目录执行：

```bash
BUILD_JOBS=1 ./scripts/build_stage.sh metrics
./build-metrics/metric_cost_bench --output results/local/metrics-t4 --threads 4
./build-metrics/metric_cost_bench --output results/local/metrics-t64 --threads 64
# 经典 LifoSem 单独对照，不把其 handoff 语义套到默认队列
./build-metrics/metric_cost_bench --output results/local/metrics-lifo --threads 4 --queue lifo
```

在目标机分别用每池实际线程数测量，例如8/16/64/256。探针本身只创建一个池；不要同时启动其他压测或编译。若想固定到一个cluster，可在核对真实CPU列表后用 `taskset -c <列表>` 启动，reader与worker都继承该范围，必须报告reader也竞争同一组CPU。没有默认猜测硬件拓扑。

## 如何读结果

`reads.csv` 分空闲池和CPU忙、队列有积压的池，比较：循环基线、native queue、1/3个 relaxed atomic load、`getPendingTaskCount`、`getPoolStatsLight`、`getPoolStats`、`getUsedCpuTime`。每批计时再除以次数，保留原始基线，不作不稳定的“相减后真实指令成本”推断。

**表中的p99是批均值的p99，不是单次API调用的p99。** CPU忙时reader被抢占会计入墙钟成本，因此不要把这些数字直接解释为某条原子指令延迟。这个读探针没有高频修改同一快照的生产者：pending/inflight主要在启动和退出时更新；热缓存读取结果不能代表有持续cacheline迁移的线上最坏情况。`getUsedCpuTime`每批最多10次，其余默认100次，samples默认80。

`updates.csv` 单独比较1/2/4个生产者：共同更新一个 relaxed fetch_add、各自更新隔离cacheline、owner私有计数每64次向原子快照发布。输出 wall time / 总更新数，是**聚合吞吐成本**，不是某线程单次更新时延。第三种允许最多约63个事件的发布滞后，且跨N个分片汇总读取为O(N)，不能声称相同准确性下免费提速。测试包括同步起跑和结束join成本；小样本时其占比会增加。

三种更新模式都核验最终计数守恒。具体成本依赖处理器、NUMA、编译器、频率与争用，ARM测试必须重测。只用一次load读现有queue，不会新增每任务计数写入；增加pending/inflight/work则同时增加维护和读取成本，两者应分开评价。

## 从微基准到选择

1. 先用默认queue做低成本基线，先验证任务等待与queue分桶关系。
2. 固定线程数接近有效CPU容量、任务CPU时间分布较窄时，queue通常已足够有用。
3. 线程数远超CPU、任务CPU时间差异大时，再比较pending/inflight/估计工作量是否带来增量解释力。
4. 用阶段3四档计量模式配对重复，比较完整任务时延、吞吐和进程CPU成本；只比较这里的load指令不能证明系统级低扰动。
5. 常态低频采集池CPU时间、必要时短窗口采集内核调度等待。不要把`getPoolStatsLight`放到每次选池的热路径。

本次实际数据、命令与限制见[RESULTS](RESULTS.md)。

# 本轮修订的实际配对结果

全部为4逻辑CPU开发VM、每轮8秒、6个独立seed配对，测量窗口为计划到达1～7秒。不是ARM结果或稳定容量认证。

独立任务第一批1600 task/s；第二批800 task/s是看到第一批波动后增加的独立seed验证，保留第一批所有轮次。DAG为Poisson期望12 QPS、60个算子与3个join、5×4深链的3波图。

dag-threads是发现DAG完成路径主要消耗在回调墙钟后追加的独立seed诊断，比较home策略每池2与16线程；不是原主矩阵的预注册确认结果。

|批次|运行数|整轮offered|测量cohort完成|整轮状态|
|---|---:|---:|---:|---|
|independent|30|384000|288000|{'completed': 384000}|
|independent-lower|18|115200|86400|{'completed': 115200}|
|dag|24|2308|1748|{'completed': 2308}|
|dag-threads|12|1128|828|{'completed': 1128}|

## 预声明对照

均值差是candidate − baseline的整轮配对差；区间是探索性95% percentile bootstrap，抽样单位为seed配对。

|批次 / 对照|均值差ms|95%区间ms|各pair差值范围ms|窗口预算差范围%|p99差值区间|
|---|---:|---|---|---|---|
|independent / time-and-pool-burst|11.326|5.271, 18.751|2.098 ～ 28.167|0.000 ～ 0.000|可作探索描述|
|independent / pool-concentration-at-same-arrivals|-5.206|-12.070, -1.109|-21.869 ～ -0.235|0.000 ～ 0.000|可作探索描述|
|independent / uniform-variance-at-same-total-work|2.591|-2.430, 8.602|-4.722 ～ 16.082|-0.228 ～ 0.316|可作探索描述|
|independent / tail-at-same-total-work|-2.387|-9.428, 3.730|-18.318 ～ 6.546|-1.147 ～ 0.626|可作探索描述|
|independent-lower / time-and-pool-burst|9.531|9.513, 9.549|9.495 ～ 9.568|0.000 ～ 0.000|可作探索描述|
|independent-lower / pool-concentration-at-same-arrivals|-5.009|-5.020, -4.999|-5.032 ～ -4.988|0.000 ～ 0.000|可作探索描述|
|dag / initial-fanout-vs-home|144.783|128.341, 162.320|115.709 ～ 183.161|0.000 ～ 0.000|样本不足，不输出|
|dag / later-forks-added|-143.903|-156.303, -131.295|-164.892 ～ -117.860|0.000 ～ 0.000|样本不足，不输出|
|dag / ordinary-successor-balancing-added|-10.551|-19.934, -0.300|-26.467 ～ 11.628|0.000 ～ 0.000|样本不足，不输出|
|dag-threads / thread-oversubscription|6.781|4.653, 8.847|2.160 ～ 10.657|0.000 ～ 0.000|样本不足，不输出|

## 有效性与解释边界

- independent：告警计数 `{'short_window: no_sustainable_capacity_claim': 30, 'generator_or_submission_lag_p99_above_1ms': 6, 'later_cohort_latency_growth: inspect overload_or_nonstationarity': 7}`。详细每轮值在同名paired JSON/Markdown，告警并未被删除。
- independent-lower：告警计数 `{'short_window: no_sustainable_capacity_claim': 18}`。详细每轮值在同名paired JSON/Markdown，告警并未被删除。
- dag：告警计数 `{'generator_or_submission_lag_p99_above_1ms': 24, 'short_window: no_sustainable_capacity_claim': 24, 'later_cohort_latency_growth: inspect overload_or_nonstationarity': 4}`。详细每轮值在同名paired JSON/Markdown，告警并未被删除。
- dag-threads：告警计数 `{'generator_or_submission_lag_p99_above_1ms': 11, 'later_cohort_latency_growth: inspect overload_or_nonstationarity': 6, 'short_window: no_sustainable_capacity_claim': 12, 'substantial_drain_after_planned_horizon': 2}`。详细每轮值在同名paired JSON/Markdown，告警并未被删除。

- 所有计划内对照均保留；不能把看到最优结果后新选的策略称为预先确认的最优策略。
- 运行数少、窗口短，即使区间不跨0，也不能宣称0.5%～1%收益可重现或生产配置最优。
- 同均值分布对照精确匹配的是整轮预算，窗口预算仍有有限样本波动；表中同时报告其大小。
- p99门槛只检查每轮样本量，不证明任务独立或尾部精度；DAG本轮明确不足，区间为null。
- 未执行ARM NUMA内存放置或缓存归因实验；候选域功能通过测试不代表已测得跨NUMA收益。

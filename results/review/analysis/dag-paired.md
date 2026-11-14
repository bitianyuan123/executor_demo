# 预声明对照与整轮配对分析

差值为 candidate − baseline；负值表示时延降低。区间重采样单位是完整 seed 配对，不是任务或请求。

|对照|有效性|配对数|planned 时延均值差 ms|探索性95%区间|p99区间可报告|
|---|---|---:|---:|---|---|
|initial-fanout-vs-home|paired_estimate|6|144.78266763132328|[128.34123983773057, 162.31982362232077]|否，见样本门槛|
|later-forks-added|paired_estimate|6|-143.90295336269682|[-156.30324402883517, -131.29479104722932]|否，见样本门槛|
|ordinary-successor-balancing-added|paired_estimate|6|-10.550845685877979|[-19.93359334932573, -0.3002431163542288]|否，见样本门槛|

## 每轮有效性诊断

|轮次|cohort完成数|顶端1%名义样本|前/后半均值ms|drain s|诊断|
|---|---:|---:|---|---:|---|
|home seed=20264000|65|0.65|[251.1088, 206.4835]|0.0436|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|initial seed=20264000|65|0.65|[375.0974, 328.4933]|0.0626|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home seed=20264001|81|0.81|[228.0274, 230.0882]|0.1302|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|initial seed=20264001|81|0.81|[415.8194, 408.5018]|0.4740|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home seed=20264002|85|0.85|[323.1911, 402.0791]|0.0658|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|initial seed=20264002|85|0.85|[473.092, 560.2267]|0.1297|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home seed=20264003|73|0.73|[215.1828, 219.8355]|0.0280|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|initial seed=20264003|73|0.73|[360.5333, 368.6971]|0.0252|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home seed=20264004|70|0.70|[215.0311, 416.5637]|0.3312|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|initial seed=20264004|70|0.70|[356.0229, 513.311]|0.3984|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|home seed=20264005|63|0.63|[210.4502, 182.8283]|0.1040|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|initial seed=20264005|63|0.63|[345.1512, 338.8936]|0.1582|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|all-forks seed=20264000|65|0.65|[258.6951, 208.581]|0.0000|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|all-forks seed=20264001|81|0.81|[237.0655, 257.8172]|0.1543|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|all-forks seed=20264002|85|0.85|[324.8297, 382.296]|0.0657|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|all-forks seed=20264003|73|0.73|[237.4977, 215.8114]|0.0000|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|all-forks seed=20264004|70|0.70|[197.1298, 386.6176]|0.2719|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|all-forks seed=20264005|63|0.63|[236.825, 170.0649]|0.0575|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|sticky seed=20264000|65|0.65|[290.1945, 192.2413]|0.0000|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|sticky seed=20264001|81|0.81|[228.105, 239.7122]|0.1194|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|sticky seed=20264002|85|0.85|[312.5628, 372.6053]|0.0687|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|sticky seed=20264003|73|0.73|[202.3807, 198.2351]|0.0000|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|sticky seed=20264004|70|0.70|[188.3567, 390.2998]|0.2889|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|sticky seed=20264005|63|0.63|[196.6101, 165.93]|0.0541|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|

## 解释限制

- Difference is candidate minus baseline; negative latency difference is lower latency.
- Estimand: equally weighted mean of per-run means or per-run p99 differences, not pooled p99.
- CI: exploratory percentile bootstrap over independent seed pairs, not task/request rows.
- At least five pairs enables CI; more repeats and independent batches are needed for small improvements.
- Tail threshold is a sample sufficiency heuristic, not proof of p99 precision or independent observations.
- Short windows, queue trends, lag and drain are diagnostics; this tool does not certify stationarity.
- Multiple contrasts are exploratory without simultaneous inference or a separately confirmed primary contrast.
- Same nominal CPU budget is not identical useful work in CPU-clock mode; use fixed iterations for locality.

完整配对差值、契约违规、工作量哈希、拒绝/删失、到达/完成率、队列趋势见同名 JSON。

# 预声明对照与整轮配对分析

差值为 candidate − baseline；负值表示时延降低。区间重采样单位是完整 seed 配对，不是任务或请求。

|对照|有效性|配对数|planned 时延均值差 ms|探索性95%区间|p99区间可报告|
|---|---|---:|---:|---|---|
|thread-oversubscription|paired_estimate|6|6.780541382795306|[4.652788561199064, 8.846880493056872]|否，见样本门槛|

## 每轮有效性诊断

|轮次|cohort完成数|顶端1%名义样本|前/后半均值ms|drain s|诊断|
|---|---:|---:|---|---:|---|
|home-t2 seed=20265000|69|0.69|[158.0545, 267.6418]|0.1154|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|home-t16 seed=20265000|69|0.69|[164.7271, 277.6314]|0.1264|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|home-t2 seed=20265001|71|0.71|[152.7225, 493.8685]|0.1192|later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|home-t16 seed=20265001|71|0.71|[159.1909, 493.2225]|0.1260|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|home-t2 seed=20265002|58|0.58|[164.6358, 181.6079]|0.1263|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home-t16 seed=20265002|58|0.58|[170.9419, 187.7661]|0.1317|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home-t2 seed=20265003|75|0.75|[337.1244, 195.4989]|0.0949|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home-t16 seed=20265003|75|0.75|[349.4901, 204.5792]|0.0696|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home-t2 seed=20265004|70|0.70|[193.9376, 226.0926]|0.0000|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home-t16 seed=20265004|70|0.70|[203.6992, 230.3191]|0.0000|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|home-t2 seed=20265005|71|0.71|[147.8545, 199.341]|0.6279|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; substantial_drain_after_planned_horizon; short_window: no_sustainable_capacity_claim|
|home-t16 seed=20265005|71|0.71|[150.2638, 208.7105]|0.6378|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; substantial_drain_after_planned_horizon; short_window: no_sustainable_capacity_claim|

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

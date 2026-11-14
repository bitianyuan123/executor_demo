# 预声明对照与整轮配对分析

差值为 candidate − baseline；负值表示时延降低。区间重采样单位是完整 seed 配对，不是任务或请求。

|对照|有效性|配对数|planned 时延均值差 ms|探索性95%区间|p99区间可报告|
|---|---|---:|---:|---|---|
|time-and-pool-burst|paired_estimate|6|11.325971804270834|[5.270719785937499, 18.75061526440972]|是（仍为探索性）|
|pool-concentration-at-same-arrivals|paired_estimate|6|-5.206143315920138|[-12.070133219427083, -1.1092422810763887]|是（仍为探索性）|
|uniform-variance-at-same-total-work|paired_estimate|6|2.5911789300347228|[-2.429643431874999, 8.602034350277778]|是（仍为探索性）|
|tail-at-same-total-work|paired_estimate|6|-2.387443042534723|[-9.428040869861112, 3.7300434491145844]|是（仍为探索性）|

## 每轮有效性诊断

|轮次|cohort完成数|顶端1%名义样本|前/后半均值ms|drain s|诊断|
|---|---:|---:|---|---:|---|
|smooth-uniform seed=20262000|9600|96.00|[7.0287, 2.4939]|0.0008|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20262000|9600|96.00|[15.7444, 13.1532]|0.0068|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20262001|9600|96.00|[2.6447, 3.418]|0.0006|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20262001|9600|96.00|[11.1919, 10.6533]|0.0086|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20262002|9600|96.00|[13.6987, 1.2361]|0.0007|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20262002|9600|96.00|[12.2062, 12.5777]|0.0079|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20262003|9600|96.00|[1.893, 1.4071]|0.0001|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20262003|9600|96.00|[20.8591, 12.8159]|0.0083|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20262004|9600|96.00|[15.2725, 2.6929]|0.0114|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20262004|9600|96.00|[11.5205, 10.6414]|0.0208|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20262005|9600|96.00|[2.4035, 1.3141]|0.0008|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20262005|9600|96.00|[46.8581, 13.1931]|0.0079|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20262000|9600|96.00|[18.0994, 10.3286]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20262001|9600|96.00|[7.72, 13.2855]|0.0000|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20262002|9600|96.00|[7.6596, 11.3442]|0.0000|generator_or_submission_lag_p99_above_1ms; later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20262003|9600|96.00|[5.5828, 20.9885]|0.0000|later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20262004|9600|96.00|[8.9692, 8.6509]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20262005|9600|96.00|[5.7823, 10.5301]|0.0177|later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-local-fixed seed=20262000|9600|96.00|[12.8665, 12.1568]|0.0076|short_window: no_sustainable_capacity_claim|
|burst-local-fixed seed=20262001|9600|96.00|[12.5989, 18.6909]|0.0077|later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-local-fixed seed=20262002|9600|96.00|[20.443, 10.904]|0.0076|short_window: no_sustainable_capacity_claim|
|burst-local-fixed seed=20262003|9600|96.00|[10.7058, 10.6648]|0.0149|short_window: no_sustainable_capacity_claim|
|burst-local-fixed seed=20262004|9600|96.00|[11.2588, 12.1435]|0.0076|short_window: no_sustainable_capacity_claim|
|burst-local-fixed seed=20262005|9600|96.00|[10.6693, 17.2184]|0.0076|later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-local-tail seed=20262000|9600|96.00|[11.5241, 12.1348]|0.0046|short_window: no_sustainable_capacity_claim|
|burst-local-tail seed=20262001|9600|96.00|[13.0833, 21.8542]|0.0113|later_cohort_latency_growth: inspect overload_or_nonstationarity; short_window: no_sustainable_capacity_claim|
|burst-local-tail seed=20262002|9600|96.00|[24.7124, 11.2395]|0.0129|generator_or_submission_lag_p99_above_1ms; short_window: no_sustainable_capacity_claim|
|burst-local-tail seed=20262003|9600|96.00|[11.2014, 11.3946]|0.0059|short_window: no_sustainable_capacity_claim|
|burst-local-tail seed=20262004|9600|96.00|[11.1226, 11.0846]|0.0223|short_window: no_sustainable_capacity_claim|
|burst-local-tail seed=20262005|9600|96.00|[12.6362, 10.778]|0.0206|short_window: no_sustainable_capacity_claim|

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

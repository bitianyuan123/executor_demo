# 预声明对照与整轮配对分析

差值为 candidate − baseline；负值表示时延降低。区间重采样单位是完整 seed 配对，不是任务或请求。

|对照|有效性|配对数|planned 时延均值差 ms|探索性95%区间|p99区间可报告|
|---|---|---:|---:|---|---|
|time-and-pool-burst|paired_estimate|6|9.531408685833334|[9.513463296215278, 9.548892240798612]|是（仍为探索性）|
|pool-concentration-at-same-arrivals|paired_estimate|6|-5.008621950034723|[-5.019824415243057, -4.998673134826389]|是（仍为探索性）|

## 每轮有效性诊断

|轮次|cohort完成数|顶端1%名义样本|前/后半均值ms|drain s|诊断|
|---|---:|---:|---|---:|---|
|smooth-uniform seed=20263000|4800|48.00|[1.0617, 1.0667]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20263000|4800|48.00|[10.5574, 10.6204]|0.0000|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20263001|4800|48.00|[1.0644, 1.0645]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20263001|4800|48.00|[10.541, 10.578]|0.0000|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20263002|4800|48.00|[1.0647, 1.0701]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20263002|4800|48.00|[10.5851, 10.686]|0.0000|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20263003|4800|48.00|[1.0719, 1.0667]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20263003|4800|48.00|[10.5873, 10.6104]|0.0000|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20263004|4800|48.00|[1.063, 1.0643]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20263004|4800|48.00|[10.5487, 10.6378]|0.0000|short_window: no_sustainable_capacity_claim|
|smooth-uniform seed=20263005|4800|48.00|[1.0673, 1.0658]|0.0003|short_window: no_sustainable_capacity_claim|
|burst-local-uniform seed=20263005|4800|48.00|[10.6127, 10.603]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20263000|4800|48.00|[5.5652, 5.6045]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20263001|4800|48.00|[5.5607, 5.5817]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20263002|4800|48.00|[5.578, 5.6296]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20263003|4800|48.00|[5.5859, 5.595]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20263004|4800|48.00|[5.5589, 5.6089]|0.0000|short_window: no_sustainable_capacity_claim|
|burst-spread-uniform seed=20263005|4800|48.00|[5.5999, 5.5962]|0.0000|short_window: no_sustainable_capacity_claim|

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

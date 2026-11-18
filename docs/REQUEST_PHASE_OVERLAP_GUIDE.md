# 已有任务CSV：旧请求后波与新请求首波是否相遇

用于解释4024中3M tail4的变化：借出后首波等待下降，普通后继等待从约0.003ms增到0.12–0.15ms。现有在途峰3或角色平均数不证明同池竞争。本工具只读已完成六轮，不编译、不运行demo、不采集新trace，也不更改负载。

## 测试机命令

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114
python3 experiments/affinity/tests/request_phase_overlap_test.py
python3 experiments/affinity/request_phase_overlap.py \
  results/local/shape-3m-fixed-01/seed-*-tail4-* \
  --warmup-seconds 2 --window-seconds 16 --json
)
```

最后输出每轮一行JSON，共六行。请保留完整终端输出；无需上传原CSV。省略`--json`可看简表，但复核优先使用JSON。输入是各目录已有`summary.json`、`requests.csv`、`tasks.csv`，输出附三文件SHA256。使用Python标准库，无需root、perf或Folly库。

## 输入、过程、输出

```text
输入：完整请求/任务记录 + 图width/depth + 实际chosen池/NUMA + 时间窗口
  → 沿既有tag规则辨认第一波stage0与后续wave算子
  → 以planned arrival严格先后区分新旧请求
  → 同一池、同一NUMA分别扫描半开时间区间
输出：提交时相遇计数、callback区间重叠、请求/资源时长及原文件hash
```

“新请求”取计划到达[2,18)秒且完成的192请求；每个有80个第一波stage0。旧请求必须计划到达更早，取wave≥1的业务算子，排除同请求和计划时刻相同的请求。旧请求允许在统计窗口外到达，窗口后完成的任务不截掉。

- `pending`：选池后的`submitted_ns <= t < start_ns`，不是精确Folly Q。
- `executing`：`start_ns <= t < finish_ns`，包含抢占，是callback墙钟占用，不是on-CPU或runnable。
- `pool`：相同实际chosen子池；cluster亲和及互不重叠CPU集验证通过后，可确定它们使用同一声明CPU范围。
- `numa`：相同NUMA，可在不同cluster；不能据此称同CPU争用。

提交时读的是已经选定目的池，不是选池前压力。区间按每个“新请求、资源”合并，避免80个算子两两相交把时长重复乘80。跨不同请求/资源求和的`request_resource_ms`不是墙钟总时长，更不是CPU·ms。

## 怎么判断

1. 先核对六轮成功解析、cohort192、首波stage0为15360、图`[80,4] / [1,22]`、六个cluster及原始hash。缺任务、失败请求、异步/同步等待、非cluster绑定或时序错误会拒绝解析；不能把拒绝输出当零重叠。
2. 看首波提交时，有旧后波pending或executing的次数，以及多少新请求曾遇到它们。pool为0而NUMA非0表示只观测到同NUMA的分散重叠。
3. 看首波执行与旧后波执行的区间交集比例及分母。只在生命周期重叠而无pool交集时，不能说本场景覆盖了旧后期与新扇出同池竞争。
4. `inverse_later_wait`反向统计cohort旧请求后波算子的等待区间，被更新请求首波stage0执行区间覆盖的数量、并集callback-ms，以及暴露/未暴露的完整等待均值。更新请求可在cohort之外，严格按计划到达先后比较；零等待计入未暴露。这个方向更贴近“0.12–0.15ms后继等待发生在哪”这个问题。覆盖时长按每个旧callback取并集，再跨callback求和，不是墙钟总量。

   此分母含后波的4个stage0及84个普通后继，即每请求88个、每192请求16896个；不应直接当成flow_report中16128个普通后继的同一个分母。
5. 将相遇结果与4024对应角色等待配对解释。即使存在同池重叠，也只是资源与时间关联，不能直接证明等待因果、cache损失或服务不足。长等待本来就更容易与别的任务相遇，暴露组更慢也存在长度偏差。若所有策略都相遇却等待不同，要继续区分选池、排队和运行阶段；不能由一次关联推出生产阈值。

当前工具限定有trace的同步计算`affinity-v2`，不宣称支持任意生产图/异步模型。不新增采样写入；耗时只发生于离线读CSV和排序扫描，内存随当前单轮记录数增长。默认命令顺序处理六轮，原数据不修改。

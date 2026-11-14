# 阶段 4 配置说明

此目录两个 JSON 是开发机 CPU 允许范围包含 `0,1,2,3` 时的**逻辑分区示例**，不是六 cluster/双 NUMA 生产配置。使用前运行 `scripts/topology.py`；若 mask 不匹配，用 `stages/04-dag/make_cases.py` 重新生成。

- `development-baseline.json`：home 固定路由，三种绑定×每池 2/16 线程，共 6 个场景。NUMA 标签都为 0，numa 模式等于共享合并后的 CPU mask，不用于证明跨 NUMA 成本。
- `development-shapes.json`：前重、均匀、后重、深链四种形状，每种 60 算子+3 join；home/borrow/fanout-initial/fanout-all，共 16 个场景。名义算子预算序列由相同 seed 与算子 ID 确定。

运行器在 JSON 以外控制 `--duration --qps --cpu-us --repeats`。先选择可持续负载并检查 `manifest.json` 的最终命令；不要一次并行启动多个矩阵。针对正式测试六池，请传入实机核对后的六个 cluster masks 与对应 NUMA 标签，生成新的配置并随结果归档。

三种绑定对照前，用 `taskset` 将运行器限制到所有显式 masks 的精确并集；示例为 `taskset -c 0-3 python3 scripts/run_matrix.py ...`。none 会继承全部 allowed CPU；如果没有这个约束，给其他线程池预留的 CPU 也可能被 none 使用，使亲和对照的总资源不同。测试机必须替换为自己的并集。

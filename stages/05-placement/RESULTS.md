# 阶段5：业务版与库版接入验证

> 本文记录早期底层分组器两位置的验证，当时17项/10轮的数字保持原样。后续扩展和当前接口边界见[阶段5指南](README.md)及[总覆盖表](../../docs/GOALS_AND_COVERAGE.md)；不能把该历史记录当成高层RequestExecutor已迁入Folly或已完成生产接入的证据。

业务版只包含已提交的 `executor_demo::BusinessExecutor` 和原版Folly公共头文件；库版额外启用 `folly::experimental::AffinityExecutor`。两者使用同一DAG程序和同一放置/计量算法，仅改变打包位置。

实际通过的检查：

- 业务版独立CMake构建，不设置FOLLY_SOURCE_DIR，不包含实验AffinityExecutor头文件。
- 实验库版单独构建，metadata明确为 `folly_experimental`；业务版明确为 `business`，对照脚本会拒绝误传同一个后端。
- 两套各17项执行器测试通过；两种后端分别通过11个DAG黑盒测试方法，包括waves依赖、RPC、initial/all fork规则和取消。
- 同一seed、5种策略×2后端，共10轮、80个request全部完成；逐任务CPU预算签名一致、计数守恒、home不跨池。
- 生成器校验业务header/测试与实验库header/测试的转换完全一致，没有静默漂移。

[完整接入对照记录](../../results/staged/placement-comparison.json)包含实际命令与backend。原始输出在本次压缩归档中 `results/staged/placement/` 下。

这证明的是接入和工作量契约一致，不要求并发竞争下所有迁移轨迹相同，也不能据此宣称库版比业务版更快。当前建议仍是先在业务侧验证并灰度，成熟的通用分组机制再按维护需要放入可选库接口。

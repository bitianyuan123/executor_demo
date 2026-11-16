# 代码参考的边界

`LoadRouting.h`是阶段3 demo 的纯选池辅助代码，原位置`experiments/independent/src/LoadRouting.h`，来源提交`9d6fda4`。随包用于查看精确比较规则，不是可直接嵌入生产的完整调度器；没有随包提供其编译依赖。

它不实现生产统一提交包装、N生命周期、异常/取消/关闭回收、多生产者的原子准入、RequestContext、优先级、DAG依赖和CPU亲和配置。`source_only`/`guarded`是实验配置组合，不应假定一定对应同名C++函数。

现场agent先探索自身提交和执行路径，再用现有基础设施做最小可关闭适配。不得因为本文件存在就替换生产Folly、搬运已入队任务或照抄demo资源编号。

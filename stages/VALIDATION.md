# 本次分阶段交付的验证记录

> 本文件保留前一次分阶段交付的验证事实。最新一轮实验设计复核、修订代码及独立配对补测见[本轮记录](../results/review/README.md)；它新增同均值/池集中对照、候选域、整轮统计门槛，不覆盖或改写下述旧数据。

本记录对应重新分阶段后的工程。旧提交 `8b6884e` 的结果留在 `results/validation.md`，不混作新独立任务实验。新增Folly文档提交为 `dccd2f331534385f604b3b87b224c392dd834aee`，实验header算法与原 `150b684e` 相同；默认原版基线仍为 `v2026.10.26.00 / c8c7adb72c6d73e2fced5ff5cebc96aa3ff65b2c`。

## 1. 五步交付是否独立

|阶段|报告|工程/验证|本机状态|
|---|---|---|---|
|1 真实业务|[报告](01-business/README.md)|已知/未知、容量、压力、三种亲和与取证表|完成；实际业务未知项显式保留|
|2 Folly机制|[报告](02-folly/README.md)|`experiments/metrics`成本探针|独立构建、功能检查和5种探针配置完成|
|3 独立任务|[设计/指南](03-independent/README.md)|`experiments/independent`，原版Folly|64轮、307,200 task全部完成|
|4 复杂DAG|[设计/指南](04-dag/README.md)|`experiments/dag`，业务路由默认后端|48轮、2,016 request全部完成|
|5 能力归属|[设计/指南](05-placement/README.md)|可选库后端与业务版同算法对照|10轮、80 request接入检查完成|

一个仓库用于固定版本和保存证据；各工程可单独配置、构建和测试。阶段2～4不需要Folly实验header，阶段5才启用库后端。任务单位和schema分开：stage3是独立task/burst；stage4是有依赖的request/DAG节点。

## 2. 实际构建和自动化

环境为Debian13、GCC14.2、x86虚拟机，allowed CPU为0–3，单NUMA，可见RAM约7.5GiB；[完整环境](../results/staged/environment.json)。本轮复用已完整编译安装的固定版本Folly，不把已有库写成这次重新完整编译了一遍。所有正式负载轮次串行执行，运行期间未并行编译或运行其他压测。

- metrics、independent、dag、placement四个独立CMake入口分别构建成功；业务DAG未传实验源码路径。
- 根工程聚合构建成功，Release **11组CTest全部通过**，总耗时约3.94秒。
- 包含9个独立任务黑盒测试方法、5个基础统计/配置测试、8个整轮预测测试、两后端各11个DAG黑盒方法、6个图路径分析测试、历史统计检查、业务/库两套各17个执行器测试、后端等价与指标探针功能检查。
- 新代码ASan+UBSan构建成功，业务后端及独立工程的 **8组CTest全部通过**，约2.95秒。原版Folly依赖库未重新插桩，不是完整依赖栈覆盖；没有执行TSan。
- 宿主存在libonion预加载，使用进程级 `ASAN_OPTIONS=verify_asan_link_order=0` 兼容已有加载顺序；未修改宿主配置，未关闭内存/未定义行为检查。
- 修复了审查发现的自定义参数被gflags抢解析、回调异常时计数未清理、采样内存无限增长、目标机亲和比较CPU总资源不一致、随机流混用导致工作量不配对、估计量溢出、图分析只相信CSV而漏边自证等问题，并补了相应检查。

[Release原始测试日志](../results/staged/logs/release-tests.log)、[sanitizer日志](../results/staged/logs/sanitizer-tests.log)、[源码及执行文件SHA256](../results/staged/source-hashes.json)。

复现构建可从单步开始：

```bash
BUILD_JOBS=1 ./scripts/build_stage.sh metrics
BUILD_JOBS=1 ./scripts/build_stage.sh independent
BUILD_JOBS=1 ./scripts/build_stage.sh dag
# 仅阶段5需要实验Folly源码分支
FOLLY_SOURCE_DIR=../folly BUILD_JOBS=1 ./scripts/build_stage.sh placement
```

## 3. 实验结论各自放在哪里

- [阶段2成本结果](../experiments/metrics/RESULTS.md)：native queue是低成本O(1)基线；读取和维护成本分开。批均值p99不叫单次调用p99。
- [阶段3结果](03-independent/RESULTS.md)：同平均任务率的burst增加等待；queue/pending接近，work增益依赖配置；完整观测成本小于波动时报告不足以分辨。
- [阶段4结果](04-dag/RESULTS.md)：图宽度/深度、多次fork和request重叠分别对照；48轮共核验170,016条CPU阶段依赖边，完成路径分解逐请求守恒。
- [阶段5结果](05-placement/RESULTS.md)：业务/库包装位置的接入契约一致，不把打包位置当性能收益。

阶段3 64轮和阶段4 48轮共112轮正式负载实验，全部完成，无拒绝/取消/失败。拒绝、取消、故障和采样截断由独立功能测试覆盖；不能因为性能矩阵无失败就声称所有错误路径已生产验证。

## 4. 原始数据和重新分析

[本次原始数据压缩包](../results/staged/raw-observations.tar.gz)包含四个独立矩阵和后端接入原始CSV/JSON；路径均相对仓库根目录。解压不会覆盖源码。已有同名结果时先核对目录，正式runner默认拒绝覆盖。压缩包、图和主要汇总的校验和见[校验文件](../results/staged/artifact-checksums.sha256)。

```bash
# 在仓库根目录
sha256sum -c results/staged/artifact-checksums.sha256
tar -xzf results/staged/raw-observations.tar.gz

# 纯标准库汇总，无需绘图库
python3 scripts/report_staged.py --no-plot
python3 stages/03-independent/predict.py \
  results/staged/runs/independent-main/manifest.json \
  --output results/staged/analysis/independent-prediction
python3 stages/04-dag/analyze_graph.py \
  results/staged/runs/dag/r* results/staged/runs/dag-overlap/r* \
  --output results/staged/analysis/all-dag-paths

# 可选重绘：在包含matplotlib的Python环境中执行
python3 scripts/report_staged.py
```

`results/staged/configs/`保留实际实验配置，`results/staged/analysis/`保留完整命令、逐轮结果、留出误差与依赖审计。stage3 runner的 `--config` 与stage4 runner的 `--cases-json` 是不同schema，不能交叉使用。

## 5. 测试环境仍需完成什么

真实ARM的六cluster、两NUMA、SMT关系、有效CPU配额和业务CPU时间口径仍需现场核对。本机对numa/none可能使用相同mask，不能声称测出了NUMA差异；内存未做真实NUMA放置，不能据CPU预算模式预测缓存局部性收益。

当前demo具备逐步研究的工程和数据契约，不是通用生产图引擎。任意DAG导入、真实RPC、真实共享可变状态、生产关闭协议、公平性、迁移冷却和成熟估计器仍在各报告的边界中。实际决策按五阶段顺序推进，不用某一步的合成收益替代另一项证据。

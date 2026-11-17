# 真正异步恢复：目标环境先做小规模功能验收

**当前状态：目标机已回传编译、3/3项CTest（0.35秒）、8个成功场景及1个预期容量拒绝场景全部通过，见[目标验收记录](../results/review/async_v3_target_functional_validation.json)。4018异步首批八轮也已完成，当前仅执行[四轮A1机制指南](TARGET_V2_ASYNC_MECHANISM_GUIDE.md)，无需重复本页功能验收。** 本页保留验收方法及边界：只验证新异步路径可以编译、正确完成或拒绝、保持业务工作与上下文、被现有分析器识别，不据此选择线程数、亲和阈值或Q/O赢家。

测试脚本自动取进程当前允许的前两个逻辑CPU，各建一个单线程池。这是人工资源分区，用于低资源工程验证，不代表机器只有两个cluster，也不代表生产推荐两个池。生产研究主线仍为每个已核实硬件cluster一池。执行前确保当前进程至少获准使用两个CPU。

## 1. 编译并执行一次

以下沿用测试机现有仓库与Folly安装路径，只重新编译demo。若本机路径不同，只修改对应路径。无需连接生产环境、读取生产代码、连接下游服务或重新采集trace。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114

cmake -S experiments/affinity -B build-affinity \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/home/bty/rank_demo/folly/_install \
  -DBUILD_TESTING=ON
cmake --build build-affinity \
  --target affinity_demo async_completion_source_test request_executor_test \
  --parallel 2
TEST_CPUS='0-47,192-239'
taskset -c "$TEST_CPUS" ctest --test-dir build-affinity \
  -R '^(async_completion_source_test|request_executor_test|affinity_async_analysis)$' \
  --output-on-failure

taskset -c "$TEST_CPUS" python3 experiments/affinity/tests/async_functional.py \
  build-affinity/affinity_demo
)
```

TEST_CPUS使用此前已获准独占的CPU集合，限制单测及其子线程也在该资源范围内；它不是新环境通用默认值。脚本在其中自动取两个逻辑CPU。回传完整编译结果及测试终端输出即可。脚本每格仅4个请求，总计8个成功场景和1个预期拒绝场景；逐格守恒检查通过后才打印最终成功。预期拒绝的demo进程应返回非零，脚本将它作为一个被验证的故障路径，脚本自身最终应返回0。

脚本成功后清理临时结果；失败时会打印并保留`/tmp/affinity-async-functional-*`目录，其中有实际命令、每格stdout/stderr、CSV和summary。终端末尾应包含具体失败原因与目录；先回传这些内容，不自动循环重跑。需要保留成功结果时可在同一命令末尾添加`--keep-output`，不需要上传整个目录。

## 2. 覆盖内容和验收边界

所有成功场景使用相同4请求、3/2宽度与2/2深度、branch私有数据、固定seed与CPU工作，原图每request有13个callback，其中10个业务operator。async选择对逻辑request/operator的hash确定，与执行位置或实际时间无关。

| 场景 | 变动 | 验收目的 |
|---|---|---|
| D0直接推进 | 不启用异步 | 旧`affinity-v2`文件兼容与业务工作基线 |
| A0零配置延迟 | 全部operator经过外部完成源，再提交恢复callback | 证明恢复路径独立存在；零配置延迟不等于零实际等待 |
| A1独立延迟 | 与A0相同位置，配置2ms延迟 | 验证due及恢复，不要求测得某个性能提升 |
| 对齐恢复 | 全选、1ms最小延迟、相对origin的2ms网格；outstanding、parent、all | 验证计划释放网格及普通恢复调度；不要求操作系统同时执行同格callback |
| 50%位置选择×3 | home、outstanding+home/all、outstanding+parent/fork | 核对相同选中集合、固定工作与上下文；50%是hash阈值，小样本不保证正好选中一半 |
| A1 trace0 | 与A1相同异步合同，关闭逐事件trace | 核对summary守恒，`tasks.csv/async.csv`缺失且不补成0观测 |
| 容量拒绝 | 全选、源容量1、10s延迟用于保持待完成事件 | 核对注册拒绝、request失败终态、取消及排空；失败样本不能进入性能对比 |

所有成功场景必须保持相同计算/访存计划、实际业务callback工作与request checksum。每个选中的operator只额外增加一个`rpc_resume`控制callback；若每request原有K个callback，选中S个位置，成功时为K+S。控制callback不重复原算子的业务计算或访存。

测试核对恢复callback的source与发起operator实际chosen池一致，home保持request原home；home策略不迁移，fork-only下普通恢复不借出。parent/home和all/fork配置均实际运行，但两个CPU的短功能测试不保证触发每一种负载均衡分支，也不能证明回迁或借出的性能优劣。可控的取消、唯一恢复、真实pool等待空闲及Draining细节由对应组件单测覆盖；本脚本不会靠紧张的时序窗口强行制造resume失败。

容量拒绝格的长延迟只为给第二个注册留下明确的容量占用窗口，正常取消应使该格无需等完10秒。脚本断言的是非零退出、已记录拒绝与守恒，而非“必须在某毫秒内完成”；整个子进程有超时保护，卡住时保留证据并失败，不能把超时当作成功排空。容量拒绝的精确竞态由组件单测独立验证。

脚本还调用`report.py`、`flow_report.py`、`diagnose.py`与`arrival_audit.py`，确认新schema、trace0以及已记录失败均可读取。分析器打印的少量请求时延仅用于接口验收，不输出性能排名；成功脚本只打印每格完成数、计划恢复数、callback数量及守恒结果。

## 3. 新参数与可解释范围

| 参数 | 含义 |
|---|---|
| `--async-percent` | 0关闭；1～100按固定hash选择业务operator后的外部等待位置 |
| `--async-delay-us` | 每个选中位置从实际issue起算的固定最小等待；A0为0 |
| `--async-align-us` | 0不对齐；非零时把due向上对齐到相对run origin的释放网格 |
| `--async-capacity` | 完成源可保留的事件上限，容量不足必须可见地拒绝并结束受影响request |

开启异步时输出`affinity-v3-async`；不开启时继续输出旧v2。trace1新增`async.csv`，记录逻辑位置、issue、planned due、source fired和终态；恢复callback仍在`tasks.csv`，使用独立tag并注明原逻辑tag。trace0只保留合同与汇总守恒，没有逐事件时序就不能推断源lag、恢复跨度或池内等待。

外部等待期间worker返回，lease只保证request与排空生命周期，不应被记成CPU池中的ready任务O或执行中callback I。完成源使控制callback重新ready后，才进入总池既有调度路径。同步sleep仍属于占住worker的独立反例，不替代本模型。

这里模拟的是“释放worker、外部完成、重新ready”的机制，不模拟真实网络、服务端、序列化、超时重试或RPC传输取消。固定延迟、选择率和对齐网格均为受控假设，不从历史调度轨迹推断生产RPC参数。功能验收之后的有界性能对照已单独写入[首批八轮合同](TARGET_V2_ASYNC_PILOT_GUIDE.md)；不要把这些功能测试值直接迁移为生产参数，也不要自行追加QPS、线程或门控梯度。

开发机已完成14项CTest、其中13项源组件用例和13项新分析用例；原有直接执行回归及本页8成功+1拒绝场景均通过。独立审阅修正事件时间上界与源峰值守恒后，两项异步分析/功能CTest再次通过。[本地验证记录](../results/review/async_v3_functional_validation.json)保存代码hash与场景守恒。目标机随后按本页回传编译、3项选中CTest及9个功能场景通过；本地未独立读取目标CSV，终端也未打印git SHA，`0c79108`仅是预期实现版本。上述功能结果均不构成六cluster性能或生产RPC验收。

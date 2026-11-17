# A1异步机制四轮：补齐O路径与观测敏感性

4018的八轮异步pilot已经完成；本页只安排后续一次有界机制覆盖，**四轮完成即停止**。继续使用每个已核实硬件cluster一个池、六池各16线程的目标结构，不改变负载、亲和性或调度阈值，也不返回历史四池配置。4018结论见[阶段报告](TARGET_V2_4018_REVIEW.md)。

## 1. 只回答一个尚未闭合的问题

4018中A1的O相对home降低请求mean约19.5%、p99约20.8%，整轮CPU/请求增加约3.7%；这是单次运行的描述性结果。已有详细任务与异步事件仅覆盖home，无法据此解释O的恢复等待、完成源延迟和迁移行为。

本批只回答：**在相同A1外部等待合同下，O的请求时延差异是否伴随业务operator及恢复callback等待下降，完成源、恢复提交与迁移代价又有何变化？**

| 固定A1合同 | trace0 | trace1 |
|---|---|---|
| home | 同期请求时延与整轮CPU对照 | 详细恢复路径与角色对照 |
| O（outstanding） | 同期候选请求时延与整轮CPU | 补齐O机制；检查O自身的观测敏感性 |

两种策略各保留同期trace0/1，是为了避免拿4018的历史trace0直接扣减新trace1；home的观测成本不能代替O。使用现有`overhead`矩阵，只改变policy与trace两个变量，不新加测试框架或热路径观测。

本批不比较Q，不比较all/fork，也不改变parent/home继承。`trace_cost_report.py`中的all/fork交互项因缺少fork格显示不可用，属于本设计的预期结果，**无需补跑**。D0/A0已回答首批结构对照问题，本批不重复。

## 2. 冻结合同与边界

| 项目 | 取值 |
|---|---|
| cluster池CPU分区 | `0-15;16-31;32-47;192-207;208-223;224-239` |
| 全进程CPU并集 | `0-47,192-239`，延续已获准独占的资源 |
| 线程与容量 | 每池16线程，capacity-scale=1，名义C=16 |
| 到达与cohort | regular，96QPS，1920请求；按计划到达选择`[2,18)`秒，1536请求 |
| 图与业务工作 | widths=80,4，depths=2,2，iterations=375000，accesses=12500 |
| 数据与异质性 | branch-rw，branch64KiB，shared0，task级±25%，约1/8四倍工作 |
| A1 | async-percent=50，delay=2000µs，align=0，capacity=4096 |
| 两层路由 | request home RR，parent continuation，balance-at=all，chunk1，hop4 |
| 借出与阈值 | 仅同NUMA；high=1、low=0.5、min-gain=0，不调参 |
| 其他 | CPU/page采样关闭，同步阻塞关闭，max-inflight16，memory-mb512 |
| 预算 | 工作seed421；四轮实际运行；每轮120秒超时；约80秒投递期，另加启动、排空、导出与分析 |

工作seed保持421以冻结业务工作和异步选择集合；每格仅一次新执行，不构成独立重复验收。四格顺序由现有脚本固定为`home-trace0 → outstanding-trace1 → home-trace1 → outstanding-trace0`。不要添加seed、cases、线程梯度或手工交换顺序后合并为同一批。

96QPS、50%异步位置和2ms最小等待是受控实验条件，不是生产标定参数。完成辅助线程与driver继承CPU并集，worker仍绑定各自cluster；没有新增专用AIO核。外部等待释放worker，等待中的事件尚不是executor ready工作，不能算入池Q/I/O。source lag是due→fire，恢复executor等待是选池/预留之后的submitted→start，两者都不是内核runq测量。

## 3. 更新并运行四轮

本次只使用已有C++二进制与分析工具，没有修改Folly或demo热路径；沿用已验收的构建。打印checkout，矩阵另记录二进制/源码hash与计划。git SHA本身不证明现有二进制由当前源码构建；若发现额外C++变更，先保留版本信息，不将旧二进制当成新构建。

输出目录必须新建，已存在则停止，避免覆盖旧结果。下面先完成四轮，再由严格合同审计检查两种策略各自的trace对照，随后输出请求统计与详细角色分段。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
test "$(git branch --show-current)" = 'experiment/affinity-load-20261114'
git pull --ff-only origin experiment/affinity-load-20261114
git rev-parse HEAD
sha256sum build-affinity/affinity_demo

PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'
TEST_CPUS='0-47,192-239'
MECHANISM_ROOT='results/local/v2-async-mechanism-01'
mkdir -p results/local
mkdir "$MECHANISM_ROOT"

taskset -c "$TEST_CPUS" python3 experiments/affinity/run_matrix.py \
  --suite overhead --seeds 421 --timeout-seconds 120 \
  --output "$MECHANISM_ROOT" --run --base-args \
  --cpus "$PARTITIONS" --affinity cluster --capacity-scale 1 --threads 16 \
  --requests 1920 --qps 96 --arrival regular \
  --burst-size 8 --burst-spacing-us 100 \
  --widths 80,4 --depths 2,2 --iterations 375000 \
  --model branch-rw --branch-kb 64 --shared-kb 0 --accesses 12500 \
  --jitter-percent 25 --long-every 8 --long-factor 4 \
  --work-scale-scope task --estimate-mode constant \
  --home-policy rr --continuation parent --balance-at all --batch-chunk 1 \
  --cross-numa 0 --max-hops 4 --cpu-sampling 0 --page-sample 0 \
  --sync-block-percent 0 --sync-block-us 0 \
  --async-percent 50 --async-delay-us 2000 \
  --async-align-us 0 --async-capacity 4096 \
  --max-inflight 16 --memory-mb 512

python3 experiments/affinity/trace_cost_report.py "$MECHANISM_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/report.py "$MECHANISM_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/flow_report.py "$MECHANISM_ROOT"/seed-*-trace1 \
  --warmup-seconds 2 --window-seconds 16
)
```

`regular`不使用burst-size/spacing；保留它们只为与4018的合同一致。policy与trace由四格矩阵控制，不要在base-args重复指定。预期输出目录名为`seed-421-home-trace0`、`seed-421-home-trace1`、`seed-421-outstanding-trace0`、`seed-421-outstanding-trace1`。

失败也占运行预算。非零退出、超时或分析审计失败时，外层`set -e`停止后续步骤；保留失败目录、run.log和终端信息，不覆盖、不提高容量补跑、不只挑成功格输出结论。报告中的失败/拒绝与完整合同不符合必须先处理，不把缺失观测补成0。

## 4. 已运行完成时只打印报告

回传从版本信息、四轮运行到三个报告结束的完整终端输出即可；原始CSV继续留在目标机。若只是复制终端输出遗漏，执行下段，不重跑native实验。

```bash
(
set -eu
cd /home/bty/rank_demo/executor_demo
MECHANISM_ROOT='results/local/v2-async-mechanism-01'
python3 experiments/affinity/trace_cost_report.py "$MECHANISM_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/report.py "$MECHANISM_ROOT"/seed-* \
  --warmup-seconds 2 --window-seconds 16
python3 experiments/affinity/flow_report.py "$MECHANISM_ROOT"/seed-*-trace1 \
  --warmup-seconds 2 --window-seconds 16
)
```

三个报告各自读取原始目录；不是把前一个报告的打印值再作为后一个报告的输入。必须先保留严格审计结果，再解释时延差异。

## 5. 如何解读及何时停止

1. **先看合同与守恒。** 四格应每轮1920成功、失败/拒绝0，cohort1536；业务每request171个原callback，其中168个operator；异步整轮161076次、cohort128809次。整轮固定迭代166355865000、访问5545195500，arrival hash8354655555075271473，checksum12935529526370272479。正常异步registered=fired=resume_succeeded，pending/canceled/rejected及各类失败为0。完整合同与逐request工作核对优先于这些汇总数字。
2. **再看trace0同期策略差异。** 请求mean/p95/p99、入口lag与整轮CPU/请求是主要效果证据。窗口后的完成仍保留；不删除高lag请求。`report.py`也会输出trace1的配对，必须按trace开关区分，不能将两组混成重复样本。
3. **分别评估两种策略的观测敏感性。** 使用`trace_cost_report.py`的home、all-parent各自trace1−trace0结果。all-parent就是本批的O；fork相关交互项不可用属于预期。trace开关引起的差异包含执行路径反馈，并非单个计数器或单次读取成本。一次配对的p99变化也包含运行波动，不能把home的低扰动比例套给O。
4. **最后看两格trace1机制。** `flow_report.py`分别列出business operator角色与rpc_resume的等待、CPU、迁移/回home及选择原因，并列出issue→due、due→fire、fire→submit、submit→start的分布。迁移是总池路由，不是内核CPU迁移；fire→submit包含路由，submitted后的等待不覆盖整个恢复提交。恢复控制callback很轻，但O仍按callback数计数；角色拆分用于识别这种差异，不能据一次相关性立即改成加权算法。
5. **不将不同统计拼成因果分解。** source lag的p99、恢复等待的p99和请求p99属于不同分布，不能相加/相减。helper CPU已包含在整轮进程CPU中，不能重复加总。request在途峰值包含外部等待状态，既不等于kernel runnable数，也不证明持续CPU饱和。

四轮完成后停下分析。本批每格一次，没有正式独立重复或生产p99验收；同一seed的1536请求也不是1536次独立实验。若O在trace1下显示等待下降，只能作为当前合同的机制支持，还需结合O自身trace敏感性；若源延迟或观测反馈明显，先记录这个模拟器的边界。当前不自动扩展对齐释放、延迟/比例/阈值梯度、QPS、线程数或Q策略，不要求新的生产代码或调度轨迹。

## 6. 指南验证范围

本页两个Shell块经过语法检查；从执行块提取矩阵命令、移除`--run`并写入临时目录后，生成四格计划并核对顺序、资源、A1、业务工作和路由参数。开发机另外使用两个人工CPU分区、每池1线程、每格4请求完成缩小四格运行；四格均成功，三个分析器按本页的4/4/2目录选择全部通过。验证记录见[本轮检查](../results/review/4018_followup_validation.json)。开发机未执行目标CPU编号的native压力，这些检查不构成目标机性能结论。C++与分析器能力沿用已验收实现。

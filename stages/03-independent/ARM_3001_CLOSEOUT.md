# ARM 3001：停止扩展矩阵，整理已有证据

适用于已经完成 `results/balance-arm/{observe,tune,evaluate}` 的这台 ARM 测试机。当前步骤是重算已有结果，不是重跑负载。原始终端证据和结果解释见 [3001 复核报告](ARM_3001_REVIEW.md)。

## 1. 本次实验的停止标准

|问题|已有证据|本次处理|
|---|---|---|
|大线程池中增加 N 是否有信息价值？|observe 18 轮完成；N 对等待预测改善，对完成预测改善很小|记录适用范围，不再追加指标直到“胜出”|
|最简单的分流是否改善局部突发？|tune 48 轮、冻结策略 evaluate 56 轮完成，效果幅度明显|统一同机主机名后恢复原有配对，结束主实验|
|动态 N 是否优于静态分散？|该合成负载中 spread 更快；N 阈值 2 分流较少|报告未胜出，保留迁移比例这个代价维度|
|低成本观测是否保留策略收益？|minimal 已启动，日志只覆盖首轮，后续进度未知|可暂缓；如补测，总范围最多 9 轮，复用已完成记录|
|每次计数维护的真实成本、DAG 亲和收益？|本次未形成对应硬件成本或 DAG 证据|留作明确的后续问题，不作为阶段3不断扩展的理由|

主线已有 **122 轮**。重新分析后，若 56 轮仍有效、无缺失，6 个策略比较各恢复 7 个有效同种子配对，且没有实际资源/任务序列变化，即可归档本次简单任务结果并进入 DAG。统计区间必须由机器上的原始 CSV 重算，不用终端四舍五入摘要替代。

如果某项没有胜出，结论就是“当前条件下未显示额外收益”。不自动改阈值、换负载、加种子再重复检验。只有新的明确问题才建立新实验；它不回头改变本次冻结策略的验证身份。`N/C > 2` 已冻结，虽然 tune 中 0.5 更快，也不追溯把 0.5 称为本次已独立验证的最优值。

这里结束的是简单任务的机制与策略比较。生产成本、不同任务长短、缓存亲和、DAG/RPC 和多生产者投递仍需在相应阶段验证；不能将本次收益百分比直接用于线上容量规划。

## 2. 先更新脚本，不重编译

若 minimal 仍在运行，先在原终端按 Ctrl+C，等 runner 退出。它会保留完成记录，中断轮次保留为一次 attempt。不要同时启动另一 runner。

在 `executor_demo-stage3-balance` 工作树内执行：

```bash
git fetch origin experiment/stage3-load-balance-20261115
git merge --ff-only origin/experiment/stage3-load-balance-20261115
```

此次修改限于脚本、测试和阶段3文档，不需要构建 C++。保留原 `build-independent-balance/independent_load`，以便已有 manifest 的二进制 SHA 和路径继续匹配。若本地有修改导致快进失败，保留本地修改并先检查，不使用强制重置。

## 3. 显式统一主机标签，重算 56 轮

用户已确认两个配置组唯一变化都是 `hostname: localhost.localdomain → localhost`；线程数、CPU mask、容量、负载等字段没有变化。这是本次同机连续实验的标签归一化依据。分析器默认仍严格区分主机，不能把任意不同机器都当成同一台。归一化仅作用于分析配置，原始 metadata、manifest、CSV 不变，报告记录映射和理由。

```bash
python3 experiments/independent/scripts/analyze_balance.py \
  --manifest results/balance-arm/evaluate/manifest.json \
  --hostname-alias localhost.localdomain=localhost \
  --hostname-alias-reason '3001: same isolated ARM host; only hostname changed, resource and workload configs identical' \
  --output results/balance-arm/evaluate-alias-analysis \
  > results/balance-arm/evaluate-alias-analysis.stdout.log
```

只需打印以下短摘要供人工收集，不必上传文件，也不必再打印全部预测明细：

```bash
python3 - <<'PY'
import json
r=json.load(open('results/balance-arm/evaluate-alias-analysis/report.json'))
print('runs',len(r['runs']),'invalid',sum(not x['valid'] for x in r['runs']),
      'excluded',len(r['excluded_entries']))
for x in r['comparisons']:
    print(x['case'],'config',x['config_id'],'pairs',x['n_pairs'],
          'invalid',x['n_invalid'],'mean_delta_ms',x['planned_mean_delta_ms'],
          'p99_delta_ms',x['planned_p99_delta_ms'],
          'p99_delta_CI95',x['planned_latency_ms_p99_delta_ci95'],x['status'])
for x in r['pairs']:
    if not x['valid']:
        print('INVALID',x['case'],x['seed'],x['reasons'])
PY
```

预期为 `runs 56 invalid 0 excluded 0`，6 行比较各 `pairs 7 invalid 0`。这是待核验结果，不是已经收到的重算结果。均值差和 p99 差均为“候选减 home”，负值是降低；p99 差是同种子各轮 p99 相减后取均值，不是合并任务的 p99。保留旧 `evaluate-analysis`，以便审计归一化前后的差异。如果还有资源或序列差异，先读报告诊断，不为凑齐 7 对而删除字段或立即补跑。

## 4. 可选 minimal：最多 9 轮，包含已有结果

**这不是进入 DAG 的门槛，也无需现在执行。** 如果需要完成一次最低观测模式的方向性检查，范围固定为：256线程/池、cluster、原主负载、原冻结 N 阈值2、home/N/spread 三种策略、32000–32002 三个种子。总计 9 轮（约9分钟投递时间，另加启动/排空），已经完成的轮次计入9轮并直接复用。3次重复只作方向性复核，不输出正式 bootstrap 区间，不在此步骤声称微小收益。

下面直接使用已经生成的70轮计划和已有目录；无需生成新计划，不改配置 SHA，不改变随机顺序：

```bash
python3 experiments/independent/scripts/balance_matrix.py dry-run \
  --config results/balance-arm/minimal.json \
  --only-case main-t256-local-home \
  --only-case main-t256-local-outstanding-h2 \
  --only-case main-t256-local-spread-policy \
  --only-seeds 32000,32001,32002
```

确认打印的 `selected runs=9/70` 后，按相同筛选恢复：

```bash
taskset -c 0-47,192-239 python3 experiments/independent/scripts/balance_matrix.py run \
  --config results/balance-arm/minimal.json \
  --output results/balance-arm/minimal \
  --binary build-independent-balance/independent_load --resume \
  --only-case main-t256-local-home \
  --only-case main-t256-local-outstanding-h2 \
  --only-case main-t256-local-spread-policy \
  --only-seeds 32000,32001,32002
```

runner要求被选策略同时包含匹配 home；拼错 case/seed 会报错，不能悄悄得到空矩阵。选择只限制本次执行，完整原计划仍保留；9轮完成不冒充70轮全部完成。目录内任何其他已完成结果也继续保留，不因为缩小范围而删除。`--resume`要求此目录已存在，并使用原二进制；不要通过重新编译来“更新”脚本。

复核时单独分析 minimal manifest；它与 progress 的观测配置不同，不合并成更多同配置重复。若目录内已经有其他 seed 或 case 的完成结果，它们也应如实报告。9轮范围之外未执行的配置不是这个小验证的缺陷，也不要求补齐。若该小验证与主结果方向相反，先报告差异并检查模式/计量开销，不自动扩展到70轮。

## 5. 为什么停止继续扫指标和阈值

- 主负载是每批100个独立CPU任务，所有任务估计值均100ms，提交很快。因此 work 在入口近似是 N 的常数缩放，remaining 在这些入口快照中也未显示额外预测信息。更多相同类型的扫参不会创造独立信息。
- N 和 Q 都用数值2作为阈值，不等于施加同样强度的干预。N 分流约67%，Q约51.2%，static spread约83%。N策略更快不能全部归因为“指标更准确”。要专门隔离这一因果问题，应另设匹配分流比例的实验，而不是把它变成本次交付的隐性追加项。
- 静态分散最好的结果有实际含义：当前合成任务没有数据亲和损失，集中在一个cluster没有收益。DAG阶段应该明确加入共享数据、依赖、RPC或其他真实约束，检验保留home的价值；无需让阶段3先证明动态负载均衡必然优于轮询。
- 端到端完成时间受自身任务时长、已经运行但竞争CPU的任务、后续到达共同影响。提交时的单个负载值不足以确定完成p99，不能以“相关性必须线性且很强”作为永远无法结束的验收要求。

本次完整成本矩阵、remaining/work扩展、新阈值、亲和性/到达/估计误差全组合均暂停。之后如要评估生产部署成本，只针对已选N指标设计固定工作量的off/minimal配对，并预先给定轮数与可分辨效应；不把现有大型cost配方当成下一条必须执行的命令。

## 6. 本次脚本修订的本地验收

`python3 tests/test_balance_analysis.py` 的26项测试、`python3 tests/test_balance_matrix.py` 的15项测试均通过。回归用例复现56轮因主机名变化产生3个无效配对，显式别名恢复6组各7对；资源差异、重复home和任务序列差异仍被拒绝。运行工具覆盖原70轮计划只选择9轮、失败attempt保留、完成结果复用、拒绝缺home选择，以及阻止与存活的旧子进程重叠。

这些是本地合成回归验证；ARM正式别名重算仍需在测试机执行第3节命令。此次没有重新运行大型性能矩阵，也没有改变C++实现或二进制。

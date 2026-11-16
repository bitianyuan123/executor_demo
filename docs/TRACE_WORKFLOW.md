# 真实业务 scheduler trace 的本地工作流

目标是用本地工具回答：池内是否有毫秒级唤醒突发、实际获得多少 CPU 时间、可观察的 runnable 等待如何变化。原始大文件留在机器上，只打印短报告人工收集。详细指标定义和限制见 [TRACE_ANALYZER.md](TRACE_ANALYZER.md)，给模型的执行提示见 [TRACE_ANALYSIS_PROMPTS.md](TRACE_ANALYSIS_PROMPTS.md)。

已有历史 trace 的采集条件是 **4 个池 × 128 个线程、16 QPS、未绑 CPU、单池“平均 20、峰值 80”**；四池是当时的临时配置，不是目标部署。“20/80”到底是瞬时 running、running+runnable、某窗口出现过的不同线程，还是 Folly active，需要先确定；不能混用这些口径。历史映射和分析结果按当时实际四池保留，不把四池重映射成六池，也不把目标实验的 CPU 0–47、192–239 自动当成历史采集范围。

后续实验主线是：**在所选 CPU 全集中，每个已确认的硬件 cluster 对应一个池**；当前独立实验子集包含六个 cluster，因此该子集目标为六池。新实验需记录实际 pool→cluster、TID→pool→NUMA 关系及 CPU 列表。历史未绑核 trace 仅用于认识负载时序和调度机制，不要求它补出并不存在的 cluster 绑定，也不能由历史单池“20/80”推出目标 cluster 池的繁忙阈值。目标实验可以独立推进，无须等待旧 trace 分析全部完成。

已有trace的发现、名称派生CSV及精确复算已回传，结论见[4005审阅](TRACE_4005_REVIEW.md)。当前无需重复执行全文件扫描或重采旧业务；优先沿[4004目标实验](TARGET_V2_4004_REVIEW.md)继续。下述完整工作流保留供新trace与质量补证使用。CSV使unique计数精确，不会把名称推断的pool身份自动变成注册证据。

## 1. 已有 `cat trace` 导出的 trace.log：先看质量和线程候选

下面命令在仓库根目录执行。修改 `TRACE_LOG` 为真实路径；输出目录会提前创建。

```bash
cd /home/bty/rank_demo/executor_demo
TRACE_LOG=/absolute/path/to/trace.log
TRACE_OUT="$PWD/results/sched-review"
mkdir -p "$TRACE_OUT"

python3 scripts/analyze_sched_trace.py "$TRACE_LOG" \
  --duration-seconds 10 --bin-ms 1,5,10,30 \
  --candidate-limit 60 --coverage unknown \
  --json "$TRACE_OUT/discovery.json" | tee "$TRACE_OUT/discovery.txt"
```

这是逐行读取，文件大小不直接决定内存。默认状态和窗口都有上限；达到上限会报告降级，不能悄悄当成完整结果。不要用 `read()`、`readlines()`、全量 pandas 或把原始 trace 粘进模型上下文。没有映射时 `[unmapped]` 是背景与业务混合的观察结果，不能替它起四个池名。

先看 `events`、`audit`、实际持续时间、记录 CPU 和候选名称。没有支持事件、格式解析失败、持续时间远短于 10 秒，都先处理采集/格式问题。不存在丢失提示并不能证明采集完整：缺少启用配置、时钟、过滤器、CPU 掩码时，覆盖仍应标为 unknown。

本工具识别 `prev_comm=... prev_pid=... prev_state=R ==> next_comm=...` 等 key/value 格式。它不把数字 `prev_state=0`、插件缩写输出当成标准格式自动猜测。

## 2. 建立属于本次采集的 TID → pool 映射

优先使用业务创建 executor 线程时登记的真实 TID 和池编号。CSV 至少有 `tid,pool` 两列；可以保留 `comm,starttime` 供生命周期核对。

如果进程仍是采集时的同一实例、线程集合稳定，可先查看真实线程名。以下 PID 需要替换。

```bash
TRACE_PID=12345
python3 scripts/executor_tid_map.py --pid "$TRACE_PID" \
  | tee "$TRACE_OUT/live-thread-names.txt"
```

只有确认名称确实编码池编号后，才输入四条真实正则。下面四条规则对应历史四池配置；目标 cluster 实验应按其实际池数登记全部线程，不能照搬四条规则或复用历史 CSV。这里不预设 `executor0` 等名称，也不按 TID 连续范围分池。名称截断后无法区分池时，必须从业务注册信息取得映射。

```bash
read -r -p '真实 pool0 线程名正则: ' POOL0_RE
read -r -p '真实 pool1 线程名正则: ' POOL1_RE
read -r -p '真实 pool2 线程名正则: ' POOL2_RE
read -r -p '真实 pool3 线程名正则: ' POOL3_RE
POOL_ARGS=(
  --pool "pool0=${POOL0_RE:?请提供已核实规则}"
  --pool "pool1=${POOL1_RE:?请提供已核实规则}"
  --pool "pool2=${POOL2_RE:?请提供已核实规则}"
  --pool "pool3=${POOL3_RE:?请提供已核实规则}"
)
python3 scripts/executor_tid_map.py --pid "$TRACE_PID" "${POOL_ARGS[@]}" \
  --output "$TRACE_OUT/tids.csv" | tee "$TRACE_OUT/map-summary.txt"
```

脚本拒绝零匹配和同一线程匹配多池。它输出的是**经名称规则生成的快照**，不是对业务归属的独立证明。逐池数量应与实际线程配置核对；不应为了凑满 128 修改规则。进程已经重启、线程退出重建或 TID 复用时，当前 `/proc` 不能补救旧 trace 的归属，保留无映射结论并为下一次采集保存映射。

正式分析：

```bash
python3 scripts/analyze_sched_trace.py "$TRACE_LOG" \
  --tid-map "$TRACE_OUT/tids.csv" \
  --duration-seconds 10 --bin-ms 1,5,10,30 --coverage unknown \
  --candidate-limit 8 --json "$TRACE_OUT/summary.json" --include-windows \
  | tee "$TRACE_OUT/summary.txt"
```

若保存了真实采集 CPU 列表，可加 `--cpu-mask "$CAPTURE_CPU_LIST"`。这是十进制列表，例如 `0-383`，**只作为覆盖元数据，不过滤已有记录**。它与 tracefs 的十六进制 `tracing_cpumask` 不同。只采了部分 CPU 用 `--coverage partial`；确认完整的 CPU/事件覆盖且审计通过后才声明 full。没有任何 CPU 事件不等于该 CPU idle。

## 3. 若文件是 trace-cmd 的二进制 trace.dat

`trace-cmd report -i` 的输入必须是其二进制记录，不能把 `cat trace` 的文本改名为 `.dat`。`-N` 禁用显示插件，`-t` 保留完整时间精度；不要用 `-R` raw 输出，它可能把状态改成数值并移除本工具需要的分隔格式。[trace-cmd report 手册](https://www.trace-cmd.org/Documentation/trace-cmd/trace-cmd-report.1.html)

```bash
set -o pipefail
trace-cmd report -N -t -i /absolute/path/to/trace.dat \
  2> "$TRACE_OUT/trace-cmd-report.stderr" | \
  python3 scripts/analyze_sched_trace.py - \
    --tid-map "$TRACE_OUT/tids.csv" --coverage unknown \
    --duration-seconds 10 --bin-ms 1,5,10,30 \
    --json "$TRACE_OUT/summary.json" --include-windows \
  | tee "$TRACE_OUT/summary.txt"
cat "$TRACE_OUT/trace-cmd-report.stderr"
```

不要对 report 再加 PID、CPU、线程名或事件过滤；它们可能删掉闭合 switch 区间需要的证据。保留 stderr 的 lost event 提示。不同发行版若仍输出不同格式，应保留少量示例校验解析器，不能在大量 malformed 后继续解释 p99。

## 4. 新采集：独立实例、全 CPU、保存质量证据

这是需要在测试机手工运行的采集方案，仓库工具本身不会启动采集。未绑 CPU 的线程和唤醒它们的线程可能在任何允许的 CPU 上；优先全 CPU 采五类事件，不仅采业务“当前所在”的 CPU。目标 cluster 部署同样需要考虑 cluster 外的唤醒方。事件头 `[CPU]` 是记录发生处，`target_cpu` 才是唤醒目标；只覆盖目标 CPU 仍可能丢失外部 waker 的事件。[内核事件定义](https://github.com/torvalds/linux/blob/v6.6/include/trace/events/sched.h)

采集目标 cluster 实验时，除下方通用元数据，还应从本次实际配置保存每个池对应的硬件 cluster、NUMA、允许 CPU 列表以及本次 TID→pool 映射；当前选定子集应覆盖已确认的六个 cluster。不能仅凭池编号或线程名推断硬件关系。历史四池 trace 不具备 cluster 绑定是其真实采集条件，不是必须先修复才能分析的错误。

以下在有 tracefs 权限的 Bash 中执行，沿用前面的 `TRACE_PID`、`POOL_ARGS`。先确认 PID/正则，选用新的输出目录。`-B` 位于所有 `-e` 前；`-M -1` 在该实例启用全 CPU，`-M` 的其他数值是十六进制，不能直接传 `0-383`。`-k` 保留采后状态以便审计，随后代码显式关闭该实例事件。[trace-cmd record 手册](https://www.trace-cmd.org/Documentation/trace-cmd/trace-cmd-record.1.html)

```bash
TRACE_OUT="$PWD/results/sched-$(date +%Y%m%d-%H%M%S)"
TRACE_INSTANCE="executor_sched_$(date +%s)_$$"
TRACE_ROOT=/sys/kernel/tracing
TRACE_PATH="$TRACE_ROOT/instances/$TRACE_INSTANCE"
mkdir -p "$TRACE_OUT"
test -d "$TRACE_ROOT/instances"
test -d "/proc/$TRACE_PID/task"
trace-cmd list -C
cat /sys/devices/system/cpu/online | tee "$TRACE_OUT/online.before.txt"
uname -a > "$TRACE_OUT/uname.txt"
cat "/proc/$TRACE_PID/stat" > "$TRACE_OUT/process.before.stat"
cat "/proc/$TRACE_PID/status" > "$TRACE_OUT/process.before.status"
cat "/proc/$TRACE_PID/cgroup" > "$TRACE_OUT/process.cgroup"
lscpu -e > "$TRACE_OUT/lscpu.txt"
python3 scripts/executor_tid_map.py --pid "$TRACE_PID" "${POOL_ARGS[@]}" \
  --output "$TRACE_OUT/tids.csv" > "$TRACE_OUT/map.before.txt"
```

确认可用时钟包含 `global` 后运行下块；否则选用经验证可跨 CPU 比较的时钟并记录选择，不用不可靠的跨 CPU 时钟强行计算等待。示例 `-b 1024` 是**每 CPU 1024 KiB**：384 个 CPU 的环形缓冲约 384 MiB，另有记录进程、输出与内核开销。根据目标机器内存和丢失情况调整，而不是把 `buffer_size_kb` 当总大小。[ftrace 缓冲与时钟说明](https://docs.kernel.org/trace/ftrace.html)

```bash
(
  set -euo pipefail
  mkdir "$TRACE_PATH"
  stop_instance() {
    printf '0\n' > "$TRACE_PATH/tracing_on"
    printf '0\n' > "$TRACE_PATH/events/enable"
  }
  trap stop_instance EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  trace-cmd record -B "$TRACE_INSTANCE" -M -1 -C global -b 1024 -k \
    --no-filter \
    -e sched:sched_switch \
    -e sched:sched_waking \
    -e sched:sched_wakeup \
    -e sched:sched_wakeup_new \
    -e sched:sched_migrate_task \
    -o "$TRACE_OUT/trace.dat" sleep 10 \
    > "$TRACE_OUT/record.stdout" 2> "$TRACE_OUT/record.stderr"

  printf '0\n' > "$TRACE_PATH/tracing_on"
  for item in trace_clock tracing_cpumask buffer_size_kb buffer_total_size_kb \
              current_tracer trace_options set_event_pid set_event_notrace_pid; do
    if test -f "$TRACE_PATH/$item"; then
      cat "$TRACE_PATH/$item" > "$TRACE_OUT/$item.txt"
    fi
  done
  for event in sched_switch sched_waking sched_wakeup sched_wakeup_new sched_migrate_task; do
    cat "$TRACE_PATH/events/sched/$event/enable" > "$TRACE_OUT/$event.enable.txt"
    cat "$TRACE_PATH/events/sched/$event/filter" > "$TRACE_OUT/$event.filter.txt"
    cat "$TRACE_PATH/events/sched/$event/format" > "$TRACE_OUT/$event.format.txt"
  done
  for stat in "$TRACE_PATH"/per_cpu/cpu*/stats; do
    test -f "$stat" || continue
    cpu_name=$(basename "$(dirname "$stat")")
    cat "$stat" > "$TRACE_OUT/$cpu_name.stats.txt"
  done
  stop_instance
)

cat /sys/devices/system/cpu/online > "$TRACE_OUT/online.after.txt"
cat "/proc/$TRACE_PID/stat" > "$TRACE_OUT/process.after.stat"
python3 scripts/executor_tid_map.py --pid "$TRACE_PID" "${POOL_ARGS[@]}" \
  --output "$TRACE_OUT/tids.after.csv" > "$TRACE_OUT/map.after.txt"
diff -u "$TRACE_OUT/tids.csv" "$TRACE_OUT/tids.after.csv" || true
cat "$TRACE_OUT/record.stderr"
```

先停止 `tracing_on` 再关事件；仅停止写环形缓冲不等于关闭事件开销。整个代码只写新建实例，不使用全局 `trace-cmd reset`。保留实例用于检查；确认无其他进程使用后可手工 `rmdir "$TRACE_PATH"` 释放其缓冲。实例不是零扰动保证：trace-cmd 有每 CPU 记录进程，`--no-filter` 保留其事件以便观察干扰，采集本身可能改变被测行为。

采后至少核对：record stderr、各 CPU stats 的 overrun/commit overrun/dropped、前后 online CPU、映射及进程 starttime、事件 enable/filter、实际时钟与掩码。随后按第 3 节分析二进制。可将前后均稳定的 online 列表作为 analyzer 的 `--cpu-mask` 元数据；是否 full 仍取决于审计。采集失败或进程退出，应保留失败记录，不拿缺失文件补造成功元数据。

如果仍采用手动 `cat trace`：必须在自己的 instance 停止 `tracing_on` 后再读；先保存 stats/设置，完成后 disable events。正在采集时读取 `trace` 会有一致性风险；ring buffer 覆盖历史后，名义 sleep 10 秒不代表导出的文本有完整 10 秒。[ftrace 读取规则](https://docs.kernel.org/trace/ftrace.html)

## 5. 可选：Perfetto Trace Processor 交叉核对 onCPU

先运行流式工具，再决定是否使用 Perfetto。Trace Processor 导入可消耗与 trace 大小有关的内存，不适合作为 4 GB 环境里无限大文件的默认第一步。全程使用本地 CLI，不要求上传到网页。

开发环境已用 **v59.1** launcher 验证下面调用方式。目标机需要预先放置已验证版本的 launcher/二进制；路径不是安装保证，其他版本以 `query --help` 为准。[官方 CLI 文档](https://perfetto.dev/docs/reference/trace-processor-cli)

如果手头只有二进制，先转为标准文本并保留 stderr；已有 `cat trace` 文件则直接设 `TRACE_LOG`，不用再次转换。

```bash
trace-cmd report -N -t -i "$TRACE_OUT/trace.dat" \
  > "$TRACE_OUT/trace.log" 2> "$TRACE_OUT/report.stderr"
TRACE_LOG="$TRACE_OUT/trace.log"
```

生成 SQL 时使用流式分析器的**绝对纳秒边界**，避免两个工具各自以“首个成功导入事件”为零点造成错位：

```bash
python3 - "$TRACE_OUT" <<'PY'
import json, pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
report = json.loads((out / 'summary.json').read_text())
subprocess.run([
    sys.executable, 'scripts/trace_processor_report_sql.py',
    '--tid-map', str(out / 'tids.csv'),
    '--start-ns', str(report['analysis_start_timestamp_ns']),
    '--end-ns', str(report['analysis_end_timestamp_ns']),
    '--output', str(out / 'report.sql'),
], check=True)
PY

TP_LAUNCHER=/tmp/executor-trace-processor-launcher
python3 "$TP_LAUNCHER" query --format csv \
  -f "$TRACE_OUT/report.sql" "$TRACE_LOG" \
  > "$TRACE_OUT/perfetto.csv" 2> "$TRACE_OUT/perfetto.stderr"
cat "$TRACE_OUT/perfetto.stderr"
cat "$TRACE_OUT/perfetto.csv"
```

必须先看 `quality_errors`、`mapping_lifetimes` 和 `stored_generic_ftrace_events`，再对比 `observed_oncpu`。其中 `ftrace_event` 只反映保留的 generic 事件行，**不是所有已处理事件的清单**；专门解析器可能消费事件建立状态，而不保留同名 generic 行。`mapping_lifetimes` 的 0 表示没找到，超过 1 可能是 TID 多生命周期，均需核对。文本导入可能缺少 process 归属，因此不要直接加 `--pid` 把所有匹配线程排空；只有确认 importer 的 PID 关系正确后才用该过滤参数。

本地 v59.1 的标准名称合成文本包含 waking=100.000 s、wakeup=100.0001 s、switch-in=100.002 s、switch-out=100.005 s。`ftrace_event` 只有两条 switch，但 `trace_bounds` 从 waking 开始，`thread_state` 确有 R=2 ms、Running=3 ms。这证明**没有 waking/wakeup generic 行，不能推出事件未被导入或处理**。本工具从成功 wakeup 起算等待为 1.9 ms，Perfetto 该例从 waking 起算为 2 ms；这是测量起点不同，不能直接认定任一工具有 bug。

另一个带空格线程名、只有 wakeup 而无 waking 的样例未重建 R 等待，只能报告这个样例的结果，不能外推成“文本不支持 wake”。实际验证应结合格式相同、预期已知的闭合合成时间线、`thread_state`/`sched_slice`、绝对时间边界及必要的对应版本源码。generic 计数只作旁证，不要求等于所有原始事件数；缺 R 行也不能证明无等待。原生采集与文本 importer 是不同路径，不保证接受相同格式和事件组合。[Perfetto CPU scheduling](https://perfetto.dev/docs/data-sources/cpu-scheduling)

两个工具 onCPU 不一致时，先查绝对边界、缺首事件、开放区间、丢失后的保守删失、TID 重用和导入格式。不可将较大的值自动当真值，或调参让结果“对齐”。

## 6. 手工回传哪些内容，如何推进

首轮只收集 `summary.txt`（或 discovery.txt）、采集时钟/CPU 范围、lost/overrun 摘要、映射来源与逐池线程数。保留 `summary.json` 在本地；需要解释个别突发时再打印对应的少量窗口，不打印全部原始 trace。

| 指标 | 正确口径 | 不能代替 |
|---|---|---|
| waking/wakeup count | 窗口内事件数，两者分开 | 不同线程数、算子数、request 数 |
| unique_event / unique_oncpu | 事件涉及 / 闭合 onCPU 区间覆盖窗口的不同 TID | 瞬时并行度；两者不能互换 |
| onCPU 核当量 | 已配对线程 CPU 时间 / 墙钟时间，单位是逻辑 CPU | 物理核性能、指令工作量、Folly active |
| observed demand | 已配对 onCPU + runnable 等待时间 / 墙钟时间 | 完整每 CPU runq、Folly 队列深度 |
| wake-to-run p99 | 成功唤醒到运行的闭合样本近似分位 | Folly 入队等待、算子或 request p99 |

固定 1/5/10/30 ms 桶观察微突发，跨桶突发可能被拆开。每个 p99 都带样本数与删失；10 秒的连续窗口彼此相关，不能当作独立重复实验。lag 为若干“桶”，例如 30 ms 的 lag 5 是 150 ms，不能与 1 ms 的 lag 5 直接比较。

相关性分清 all full windows 与 activity windows：前者纳入完整窗口中的零活动期，后者只选比较双方至少一个非零的窗口，会引入选择偏差。两组 n 与 r 应并列解读。waking→unique_oncpu、waking→onCPU、unique_oncpu→onCPU 和 wakeup→onCPU 可帮助区分唤醒事件、轮流运行的线程数量和实际 CPU 时间，但共享时间窗、共同扇出来源仍会造成相关性。

历史证据可帮助观察“突发后 CPU 等待是否增大”，为目标实验设计提供时序和机制参考。负载均衡收益和繁忙阈值应在每硬件 cluster 一池的目标配置上，用同资源、同业务负载、重复且顺序交错的策略对比来确定，并对齐 Folly 排队/active、request 开始/结束和必要硬件指标。不能将历史四个未绑核池的单池“20/80”换算为六个 cluster 池的阈值；池容量、竞争范围与亲和条件已经改变。旧 trace 分析与目标 cluster 实验可并行推进，不将补齐历史证据作为后续实验的前置门槛。没有 request marker 就不计算 request p99；历史 16 QPS × 10 秒至多只是期望请求规模的粗略参考，不是成功完成的样本数证明。

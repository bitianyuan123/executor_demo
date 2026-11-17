#!/usr/bin/env bash
# Optional, bounded CPU-window supplement. Default prints a plan only.
set -euo pipefail

CAPTURE_REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CAPTURE_ROOT='results/local/cluster-cpu-regular12-3m-t16-seed421-01'
CAPTURE_BINARY='build-affinity/affinity_demo'
CAPTURE_PYTHON='python3'
CAPTURE_EXECUTE=0
CAPTURE_ACTIVE_PID=''
CAPTURE_CPUS='0-47,192-239'
CAPTURE_PARTITIONS='0-15;16-31;32-47;192-207;208-223;224-239'

usage() {
  cat <<'EOF'
Usage: scripts/run_cluster_cpu_comparison.sh [--run] [--output NEWDIR]
       [--binary PATH] [--python EXECUTABLE] [--help]

Default: print the six commands; do not launch workloads or create result files.
--run: execute exactly six serial rounds, stop on first failure, never retry.
Order: home, queue, outstanding, outstanding, queue, home.
Fixed: 6 verified clusters / 96 CPUs; 16 workers per pool; regular 12 QPS;
       seed 421; 240 requests; 80/4 branches x depth 2/2;
       3,000,000 iterations + 100,000 accesses per base operator;
       branch-rw 64 KiB; task jitter +/-25%, about 1/8 at 4x work;
       parent/all; same NUMA; chunk 1; high 1; gain 0; hop 4;
       trace 1; no async/sync blocking; CPU capture 100ms; timeout 120s/round.
Statistics: planned-request cohort [2,18)s; complete CPU read windows inside it.
Relative output/binary paths are resolved from the repository root.
This is optional new sampling, NOT previously collected CPU curves or a 1ms
burst experiment. Two repeats per policy do not validate production p99.
EOF
}

while (($#)); do
  case "$1" in
    --run) CAPTURE_EXECUTE=1; shift ;;
    --output|--binary|--python)
      if (($# < 2)); then printf 'Missing value for %s\n' "$1" >&2; exit 2; fi
      case "$1" in
        --output) CAPTURE_ROOT=$2 ;;
        --binary) CAPTURE_BINARY=$2 ;;
        --python) CAPTURE_PYTHON=$2 ;;
      esac
      shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
cd -- "$CAPTURE_REPO"
if [[ "$CAPTURE_ROOT" != /* ]]; then CAPTURE_ROOT="$CAPTURE_REPO/$CAPTURE_ROOT"; fi
if [[ "$CAPTURE_BINARY" != /* ]]; then CAPTURE_BINARY="$CAPTURE_REPO/$CAPTURE_BINARY"; fi

CAPTURE_POLICIES=(home queue outstanding outstanding queue home)
CAPTURE_COMMON=(
  --cpus "$CAPTURE_PARTITIONS" --affinity cluster --threads 16 --capacity-scale 1
  --requests 240 --qps 12 --arrival regular --burst-size 8 --burst-spacing-us 100
  --widths 80,4 --depths 2,2
  --iterations 3000000 --accesses 100000 --model branch-rw
  --branch-kb 64 --shared-kb 0 --jitter-percent 25 --long-every 8 --long-factor 4
  --work-scale-scope task --estimate-mode constant --home-policy rr
  --continuation parent --balance-at all --batch-chunk 1
  --busy-high 1 --return-low 0.5 --min-gain 0 --cross-numa 0 --cross-numa-penalty 0
  --max-hops 4 --max-outstanding 0
  --cpu-sampling 0 --trace 1 --page-sample 0 --max-inflight 16 --memory-mb 512 --seed 421
  --sync-block-percent 0 --sync-block-us 0
  --async-percent 0 --async-delay-us 0 --async-align-us 0 --async-capacity 4096
)

print_argv() {
  printf '%q ' "$@"
  printf '\n'
}

round_command() {
  local capture_round=$1 capture_policy=$2
  CAPTURE_COMMAND=(
    taskset -c "$CAPTURE_CPUS" "$CAPTURE_PYTHON" experiments/affinity/cluster_cpu_capture.py
    --partitions "$CAPTURE_PARTITIONS" --interval-ms 100 --timeout-seconds 120
    --output "$capture_round/capture" --summary-path "$capture_round/demo/summary.json"
    --warmup-seconds 2 --window-seconds 16
    -- taskset -c "$CAPTURE_CPUS" "$CAPTURE_BINARY"
    --output "$capture_round/demo" --policy "$capture_policy" "${CAPTURE_COMMON[@]}"
  )
}

printf 'OPTIONAL CPU SUPPLEMENT: %s\n' "$([[ $CAPTURE_EXECUTE == 1 ]] && printf EXECUTE || printf PLAN_ONLY)"
printf 'Output: %s\n' "$CAPTURE_ROOT"
printf 'CPU set: %s\nPartitions: %s\n' "$CAPTURE_CPUS" "$CAPTURE_PARTITIONS"
printf 'Six fixed rounds; trace=1 throughout; timeout=120s each; failure stops the batch.\n'
printf 'CPU windows=100ms averages, not 1ms peaks. Request cohort=[2,18)s.\n'
for capture_index in "${!CAPTURE_POLICIES[@]}"; do
  capture_policy=${CAPTURE_POLICIES[$capture_index]}
  printf -v capture_label '%02d-%s' "$((capture_index + 1))" "$capture_policy"
  round_command "$CAPTURE_ROOT/$capture_label" "$capture_policy"
  printf 'ROUND %s\n' "$capture_label"
  print_argv "${CAPTURE_COMMAND[@]}"
done
if ((CAPTURE_EXECUTE == 0)); then
  printf 'PLAN ONLY: nothing launched or written. Review the guide, then add --run with a new output directory.\n'
  exit 0
fi

[[ ! -e "$CAPTURE_ROOT" ]] || { printf 'Refusing existing output: %s\n' "$CAPTURE_ROOT" >&2; exit 2; }
[[ -x "$CAPTURE_BINARY" ]] || { printf 'Binary is not executable: %s\n' "$CAPTURE_BINARY" >&2; exit 2; }
command -v "$CAPTURE_PYTHON" >/dev/null
command -v taskset >/dev/null
# taskset can intersect a request with a cgroup CPU restriction: require the
# effective mask to equal the approved 96-CPU set before launching any workload.
taskset -c "$CAPTURE_CPUS" "$CAPTURE_PYTHON" - <<'PY'
import os
expected = set(range(48)) | set(range(192,240))
actual = os.sched_getaffinity(0)
if actual != expected:
    raise SystemExit('CPU preflight failed: effective affinity differs from approved 0-47,192-239; no workload launched')
print('CPU preflight passed: effective mask has all 96 approved logical CPUs')
PY
mkdir -p -- "$(dirname -- "$CAPTURE_ROOT")"
mkdir -- "$CAPTURE_ROOT"
{
  printf 'optional_cluster_cpu_supplement_v1\n'
  printf 'started_utc='; date -u +%FT%TZ
  printf 'repository_commit='; git rev-parse HEAD
  printf 'binary_sha256='; sha256sum -- "$CAPTURE_BINARY"
  printf 'policies=home,queue,outstanding,outstanding,queue,home\n'
  printf 'requests=240 qps=12 arrival=regular iterations=3000000 accesses=100000 threads=16 seed=421 trace=1\n'
  printf 'CPU_capture_interval_ms=100 formal_scope_seconds=[2,18) timeout_per_round_seconds=120 max_rounds=6\n'
} > "$CAPTURE_ROOT/batch.txt"
cat -- "$CAPTURE_ROOT/batch.txt"

stop_active() {
  local capture_signal=$1 capture_exit=$2
  trap - INT TERM
  if [[ -n "$CAPTURE_ACTIVE_PID" ]] && kill -0 "$CAPTURE_ACTIVE_PID" 2>/dev/null; then
    kill -s "$capture_signal" "$CAPTURE_ACTIVE_PID" 2>/dev/null || true
    wait "$CAPTURE_ACTIVE_PID" || true
  fi
  printf 'Batch interrupted; partial results retained at %s\n' "$CAPTURE_ROOT" >&2
  exit "$capture_exit"
}
trap 'stop_active INT 130' INT
trap 'stop_active TERM 143' TERM
CAPTURE_RUNS=()
for capture_index in "${!CAPTURE_POLICIES[@]}"; do
  capture_policy=${CAPTURE_POLICIES[$capture_index]}
  printf -v capture_label '%02d-%s' "$((capture_index + 1))" "$capture_policy"
  capture_round="$CAPTURE_ROOT/$capture_label"
  mkdir -- "$capture_round"
  CAPTURE_RUNS+=("$capture_round/demo")
  round_command "$capture_round" "$capture_policy"
  print_argv "${CAPTURE_COMMAND[@]}" > "$capture_round/command.txt"
  printf '\nSTART ROUND %s\n' "$capture_label"
  "${CAPTURE_COMMAND[@]}" > "$capture_round/cpu-terminal.txt" 2>&1 &
  CAPTURE_ACTIVE_PID=$!
  if wait "$CAPTURE_ACTIVE_PID"; then
    capture_status=0
  else
    capture_status=$?
  fi
  CAPTURE_ACTIVE_PID=''
  cat -- "$capture_round/cpu-terminal.txt"
  if ((capture_status != 0)); then
    printf 'STOP: round %s exit=%s; no later rounds launched. See %s/capture/run.log\n' "$capture_label" "$capture_status" "$capture_round" >&2
    exit "$capture_status"
  fi
  "$CAPTURE_PYTHON" experiments/affinity/report.py "$capture_round/demo" \
    --warmup-seconds 2 --window-seconds 16 > "$capture_round/request-report.txt"
  cat -- "$capture_round/request-report.txt"
  "$CAPTURE_PYTHON" experiments/affinity/flow_report.py "$capture_round/demo" \
    --warmup-seconds 2 --window-seconds 16 > "$capture_round/flow-report.txt"
  cat -- "$capture_round/flow-report.txt"
  "$CAPTURE_PYTHON" - "$capture_round/demo" "$CAPTURE_ROOT/01-home/demo" > "$capture_round/role-rates.txt" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path('experiments/affinity').resolve()))
import report
import flow_report
run = report.load_run(Path(sys.argv[1]), 2, 16)
if not run['valid_for_comparison'] or len(run['completed_cohort']) != 192:
    raise SystemExit('invalid comparison or cohort !=192; stop batch')
baseline = report.load_run(Path(sys.argv[2]), 2, 16)
if run['pairing_key'] != baseline['pairing_key']:
    raise SystemExit('comparison contract differs from first home; stop batch')
request_work = lambda item: [(r['request'], r['scheduled_ns'] - item['summary']['origin_ns'], r['checksum']) for r in item['requests']]
if request_work(run) != request_work(baseline):
    raise SystemExit('request arrival/checksum contract differs from first home; stop batch')
task_work = lambda item: sorted((t['request'], t['tag'], t['iterations'], t['read_words'], t['write_words'], t['checksum'], t['sync_block_target_us']) for t in item['tasks'])
if task_work(run) != task_work(baseline):
    raise SystemExit('actual operator work differs from first home; stop batch')
print('SAME_WORK_AUDIT: resources, arrival, request checksums and operator work match first home')
ids = {r['request'] for r in run['completed_cohort']}
roles = flow_report.task_roles(run['summary'], [t for t in run['tasks'] if t['request'] in ids])
if roles is None:
    raise SystemExit('trace must be enabled for role routing counts')
print('ROLE_ROUTING: same planned-request cohort; source!=chosen is pool placement, not OS migration')
print('role,n,changed_pool,changed_pool_percent,wait_mean_ms')
for role, row in roles.items():
    rate = 100 * row['migrations'] / row['n'] if row['n'] else None
    print('%s,%d,%d,%s,%s' % (role, row['n'], row['migrations'],
          'unavailable' if rate is None else '%.6f' % rate,
          'unavailable' if row['wait_ms'] is None else '%.6f' % row['wait_ms']))
PY
  cat -- "$capture_round/role-rates.txt"
  printf 'END ROUND %s\n' "$capture_label"
done

# Each three-round block has exactly one home; the existing analyzer can audit
# work/checksums and form unique pairs. Combining all six would give two homes.
"$CAPTURE_PYTHON" experiments/affinity/report.py "${CAPTURE_RUNS[0]}" "${CAPTURE_RUNS[1]}" "${CAPTURE_RUNS[2]}" \
  --warmup-seconds 2 --window-seconds 16 > "$CAPTURE_ROOT/block-1-comparison.txt"
cat -- "$CAPTURE_ROOT/block-1-comparison.txt"
"$CAPTURE_PYTHON" experiments/affinity/report.py "${CAPTURE_RUNS[5]}" "${CAPTURE_RUNS[4]}" "${CAPTURE_RUNS[3]}" \
  --warmup-seconds 2 --window-seconds 16 > "$CAPTURE_ROOT/block-2-comparison.txt"
cat -- "$CAPTURE_ROOT/block-2-comparison.txt"
printf 'SIX ROUNDS COMPLETE: %s\n' "$CAPTURE_ROOT"
printf 'Keep both blocks separate: do not average their p99 or call two runs a production tail guarantee.\n'

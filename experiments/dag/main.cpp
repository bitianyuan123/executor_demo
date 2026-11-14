// Linux/Folly synthetic DAG benchmark. All timestamps are relative CLOCK_MONOTONIC.
#ifdef EXECUTOR_DEMO_USE_FOLLY_EXTENSION
#include <folly/executors/experimental/AffinityExecutor.h>
#else
#include <executor_demo/BusinessExecutor.h>
#endif
#include <folly/init/Init.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cerrno>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <numeric>
#include <queue>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <sched.h>
#include <sys/utsname.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifdef EXECUTOR_DEMO_USE_FOLLY_EXTENSION
using Executor = folly::experimental::AffinityExecutor;
constexpr const char* kBackend = "folly_experimental";
#else
using Executor = executor_demo::BusinessExecutor;
constexpr const char* kBackend = "business";
#endif
using Clock = std::chrono::steady_clock;

uint64_t clockNs(clockid_t id) {
  timespec ts{};
  if (clock_gettime(id, &ts) != 0) throw std::runtime_error("clock_gettime failed");
  return uint64_t(ts.tv_sec) * 1000000000ULL + uint64_t(ts.tv_nsec);
}
int workerTid() {
  static thread_local const int tid = int(syscall(SYS_gettid));
  return tid;
}
uint64_t mix(uint64_t x) {
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}
double unit(uint64_t x) { return double(mix(x) >> 11) * 0x1.0p-53; }
std::vector<std::string> split(const std::string& s, char c) {
  std::vector<std::string> out;
  std::stringstream stream(s);
  std::string part;
  while (std::getline(stream, part, c)) out.push_back(part);
  return out;
}
std::string json(const std::string& s) {
  std::string out = "\"";
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') { out += '\\'; out += char(c); }
    else if (c == '\n') out += "\\n";
    else if (c < 32) out += '?';
    else out += char(c);
  }
  return out + '"';
}
std::string joinInts(const std::vector<int>& xs) {
  std::ostringstream o;
  for (size_t i = 0; i < xs.size(); ++i) { if (i) o << ','; o << xs[i]; }
  return o.str();
}
uint64_t parseU64(const std::string& s) {
  if (s.empty() || s[0] == '-') throw std::invalid_argument("expected nonnegative integer: " + s);
  size_t n = 0;
  auto v = std::stoull(s, &n);
  if (n != s.size()) throw std::invalid_argument("invalid integer: " + s);
  return v;
}
double parseDouble(const std::string& s) {
  size_t n = 0;
  auto v = std::stod(s, &n);
  if (n != s.size() || !std::isfinite(v)) throw std::invalid_argument("invalid finite number: " + s);
  return v;
}
std::vector<int> parseCpus(const std::string& text) {
  std::set<int> cpus;
  for (const auto& item : split(text, ',')) {
    auto range = split(item, '-');
    if (range.empty() || range.size() > 2) throw std::invalid_argument("invalid CPU list " + text);
    auto lo = parseU64(range[0]);
    auto hi = range.size() == 2 ? parseU64(range[1]) : lo;
    if (lo > hi || hi >= CPU_SETSIZE) throw std::invalid_argument("CPU outside supported CPU_SETSIZE");
    for (uint64_t c = lo; c <= hi; ++c) cpus.insert(int(c));
  }
  if (cpus.empty()) throw std::invalid_argument("empty CPU group");
  return {cpus.begin(), cpus.end()};
}
std::vector<int> allowedCpus() {
  cpu_set_t set;
  CPU_ZERO(&set);
  if (sched_getaffinity(0, sizeof(set), &set)) throw std::runtime_error("sched_getaffinity failed");
  std::vector<int> out;
  for (int i = 0; i < CPU_SETSIZE; ++i) if (CPU_ISSET(i, &set)) out.push_back(i);
  return out;
}

struct Options {
  std::string output = "results", cpus, numa, capacities;
  std::string affinity = "none", policy = "home", metric = "work", queue = "default", workMode = "cpu";
  std::string graph = "chains", waveWidths, waveDepths, balanceForks = "all", arrival = "regular";
  std::string candidateScope = "all";
  std::vector<size_t> widths, depths;
  size_t pools = 6, threads = 2, fanoutMin = 20, fanoutMax = 40, chainLength = 2;
  size_t maxInflight = 128, memoryKb = 0;
  uint64_t requests = 0, cpuUs = 1000, joinUs = 100, rpcDelayUs = 5000, seed = 42, wakeUs = 0;
  uint64_t sampleMs = 100, estimateUs = 0, iterationsPerUs = 64;
  uint64_t longMinUs = 0, longMaxUs = 0;
  bool taskLog = true;
  double qps = 12, duration = 10, jitter = 0.5, longProb = 0.02, longFactor = 8;
  double rpcProb = 0, rpcJitter = 0, drainSeconds = 30, crossNumaPenaltyUs = 0, minGainUs = 0, relativeGain = 0;
};
const char* help = R"HELP(Real Folly CPUThreadPoolExecutor affinity/DAG experiment (Linux).
Usage: executor_demo [--option VALUE ...]
  --output DIR                 Output directory (default results, must not contain run CSVs)
  --pools N --threads N         Logical groups, fixed threads per group (6, 2)
  --cpus '0-7;8-15;...'         One explicit CPU list per group; no inferred physical clusters
  --numa '0,0,0,1,1,1'         One NUMA label per group; required with affinity=numa
  --capacities '8,8,...'        Optional positive relative CPU capacity per group
  --affinity none|cluster|numa  none inherits allowed cpuset; numa unions same-label CPU lists
  --policy home|borrow|sticky|fanout|least-load  (home)
  --metric work|queue|pending  Work estimate, native semaphore guess, or wrapper pending (work)
  --candidate-scope all|preferred-numa  Restrict candidates to preferred group's NUMA (all)
                               Restricted scope needs explicit CPU/NUMA and cluster/numa affinity.
  --queue default|lifo|throttled --wake-us N (default, 0; nonzero only throttled, <=1e6)
  --qps N --duration SECONDS --requests N    Regular open-loop arrivals (12,10,0)
                               requests=0 uses duration; positive count also capped by duration
  --arrival regular|poisson    Seeded arrival schedule independent of work/routing (regular)
  --graph chains|waves         chains keeps original independent chains plus one join (chains)
  --fanout-min N --fanout-max N --chain-length N  chains width range and chain depth (20,40,2)
  --wave-widths '20,4,8'       Required in waves: branch count in each fork/join wave
  --wave-depths '1,2,1'        Optional depth per wave; defaults to --chain-length for all waves
  --balance-forks initial|all  fanout policy: balance initial submissions or every fork (all)
                               Other policies still evaluate every submission. Each wave has one join.
                               Total CPU nodes = sum(width * depth) + number of waves.
  --cpu-us N --jitter F         Nominal per-operator CPU microseconds and uniform +/- fraction (1000,.5)
  --long-prob F --long-factor F Heavy-tail probability and multiplier (.02,8)
  --long-min-us N --long-max-us N  Optional absolute uniform tail; both required, overrides factor
  --join-us N                  CPU microseconds of every wave join, including final join (100)
  --rpc-prob F --rpc-delay-us N Split operators around timer-driven RPC suspension (0,5000)
  --rpc-jitter F               Uniform +/- fraction of RPC delay, independently seeded (0)
  --work-mode cpu|iterations  CPU-time target or fixed work amount across placements (cpu)
  --iterations-per-us N        Fixed-mode loop iterations per nominal work microsecond (64)
  --memory-kb N                Shared read-only per-request dataset, main-thread first touch (0)
  --task-log 0|1               Emit detailed task rows (1); timing overhead remains when disabled
  --estimate-us N              Fixed estimate instead of known synthetic work (0=known work)
  --seed N --max-inflight N    Reproducible work and admission/telemetry bound (42,128)
  --drain-seconds N            Cancel remaining work after drain deadline, mark censored (30)
  --sample-ms N                Group snapshot period (100)
  --cross-numa-penalty-us N --min-gain-us N --relative-gain F  Routing penalties/hysteresis (0,0,0)
  --help
All *_ns CSV timestamps are relative CLOCK_MONOTONIC. enqueue_us starts before routing.
CPU work uses CLOCK_THREAD_CPUTIME_ID, not executor sleeps. RPC timers do not block workers.
Six logical groups on a small host do not represent six physical clusters. Capacity defaults
are min(threads, logical CPU counts), not a claim of SMT throughput; overlapping cpus share capacity.
Outputs: metadata.json, workers.csv, tasks.csv, requests.csv, seconds.csv, pools.csv, summary.json.
seconds.csv is finalized on shutdown, using exact completion-time cohorts. No latency for
rejected requests is silently treated as success. SIGKILL/process death cannot flush results.
)HELP";
Options parse(int argc, char** argv) {
  Options o;
  for (int i = 1; i < argc; ++i) {
    std::string key = argv[i];
    if (key == "--help") { std::cout << help; std::exit(0); }
    if (i + 1 == argc) throw std::invalid_argument("missing value for " + key);
    std::string val = argv[++i];
    if (key == "--output") o.output = val;
    else if (key == "--cpus") o.cpus = val;
    else if (key == "--numa") o.numa = val;
    else if (key == "--capacities") o.capacities = val;
    else if (key == "--affinity") o.affinity = val;
    else if (key == "--policy") o.policy = val;
    else if (key == "--metric") o.metric = val;
    else if (key == "--queue") o.queue = val;
    else if (key == "--work-mode") o.workMode = val;
    else if (key == "--graph") o.graph = val;
    else if (key == "--wave-widths") o.waveWidths = val;
    else if (key == "--wave-depths") o.waveDepths = val;
    else if (key == "--balance-forks") o.balanceForks = val;
    else if (key == "--arrival") o.arrival = val;
    else if (key == "--candidate-scope") o.candidateScope = val;
    else if (key == "--pools") o.pools = parseU64(val);
    else if (key == "--threads") o.threads = parseU64(val);
    else if (key == "--fanout-min") o.fanoutMin = parseU64(val);
    else if (key == "--fanout-max") o.fanoutMax = parseU64(val);
    else if (key == "--chain-length") o.chainLength = parseU64(val);
    else if (key == "--max-inflight") o.maxInflight = parseU64(val);
    else if (key == "--memory-kb") o.memoryKb = parseU64(val);
    else if (key == "--requests") o.requests = parseU64(val);
    else if (key == "--cpu-us") o.cpuUs = parseU64(val);
    else if (key == "--join-us") o.joinUs = parseU64(val);
    else if (key == "--rpc-delay-us") o.rpcDelayUs = parseU64(val);
    else if (key == "--long-min-us") o.longMinUs = parseU64(val);
    else if (key == "--long-max-us") o.longMaxUs = parseU64(val);
    else if (key == "--seed") o.seed = parseU64(val);
    else if (key == "--wake-us") o.wakeUs = parseU64(val);
    else if (key == "--sample-ms") o.sampleMs = parseU64(val);
    else if (key == "--estimate-us") o.estimateUs = parseU64(val);
    else if (key == "--iterations-per-us") o.iterationsPerUs = parseU64(val);
    else if (key == "--task-log") { if (val != "0" && val != "1") throw std::invalid_argument("task-log must be 0 or 1"); o.taskLog = val == "1"; }
    else if (key == "--qps") o.qps = parseDouble(val);
    else if (key == "--duration") o.duration = parseDouble(val);
    else if (key == "--jitter") o.jitter = parseDouble(val);
    else if (key == "--long-prob") o.longProb = parseDouble(val);
    else if (key == "--long-factor") o.longFactor = parseDouble(val);
    else if (key == "--rpc-prob") o.rpcProb = parseDouble(val);
    else if (key == "--rpc-jitter") o.rpcJitter = parseDouble(val);
    else if (key == "--drain-seconds") o.drainSeconds = parseDouble(val);
    else if (key == "--cross-numa-penalty-us") o.crossNumaPenaltyUs = parseDouble(val);
    else if (key == "--min-gain-us") o.minGainUs = parseDouble(val);
    else if (key == "--relative-gain") o.relativeGain = parseDouble(val);
    else throw std::invalid_argument("unknown option " + key);
  }
  if (!o.pools || o.pools > 128 || !o.threads || o.threads > 4096 || !o.fanoutMin ||
      o.fanoutMin > o.fanoutMax || o.fanoutMax > 10000 || !o.chainLength || o.chainLength > 1000 ||
      o.fanoutMax * o.chainLength > 100000 || !o.maxInflight || o.maxInflight > 100000 ||
      !o.cpuUs || !o.joinUs || o.cpuUs > 10000000 || o.joinUs > 10000000 || o.memoryKb > 1048576 ||
      !(o.qps > 0 && o.qps <= 1000000) || !(o.duration > 0 && o.duration <= 86400) ||
      o.requests > 1000000 || std::ceil(o.qps * o.duration) > 1000000 ||
      o.jitter < 0 || o.jitter > 1 || o.longProb < 0 || o.longProb > 1 || o.longFactor < 1 ||
      double(o.cpuUs) * (1 + o.jitter) * (o.longMaxUs ? 1 : o.longFactor) > 10000000 ||
      bool(o.longMinUs) != bool(o.longMaxUs) || o.longMinUs > o.longMaxUs || o.longMaxUs > 10000000 ||
      o.rpcProb < 0 || o.rpcProb > 1 || o.rpcDelayUs > 3600000000ULL ||
      o.rpcJitter < 0 || o.rpcJitter > 1 || double(o.rpcDelayUs) * (1 + o.rpcJitter) > 3600000000ULL ||
      o.wakeUs > 1000000 || (o.wakeUs != 0 && o.queue != "throttled") ||
      o.drainSeconds < 0 || o.drainSeconds > 86400 || !o.sampleMs || o.sampleMs > 60000 ||
      (o.workMode != "cpu" && o.workMode != "iterations") || !o.iterationsPerUs || o.iterationsPerUs > 1000000 ||
      o.estimateUs > 10000000 || o.crossNumaPenaltyUs < 0 || o.minGainUs < 0 || o.relativeGain < 0 || o.relativeGain >= 1)
    throw std::invalid_argument("configuration outside supported bounds; see --help");
  if (o.graph != "chains" && o.graph != "waves") throw std::invalid_argument("graph must be chains or waves");
  if (o.arrival != "regular" && o.arrival != "poisson") throw std::invalid_argument("arrival must be regular or poisson");
  if (o.candidateScope != "all" && o.candidateScope != "preferred-numa")
    throw std::invalid_argument("candidate-scope must be all or preferred-numa");
  if (o.balanceForks != "initial" && o.balanceForks != "all") throw std::invalid_argument("balance-forks must be initial or all");
  if (o.graph == "chains" && (!o.waveWidths.empty() || !o.waveDepths.empty()))
    throw std::invalid_argument("wave-widths/depths require graph=waves");
  if (o.graph == "waves") {
    if (o.waveWidths.empty()) throw std::invalid_argument("graph=waves requires explicit wave-widths");
    for (const auto& width : split(o.waveWidths, ',')) o.widths.push_back(parseU64(width));
    if (o.widths.empty() || o.widths.size() > 1000) throw std::invalid_argument("invalid wave count");
    if (o.waveDepths.empty()) o.depths.assign(o.widths.size(), o.chainLength);
    else for (const auto& depth : split(o.waveDepths, ',')) o.depths.push_back(parseU64(depth));
    if (o.widths.size() != o.depths.size()) throw std::invalid_argument("wave-widths and wave-depths counts differ");
    uint64_t total = o.widths.size();
    for (size_t wave = 0; wave < o.widths.size(); ++wave) {
      if (!o.widths[wave] || o.widths[wave] > 10000 || !o.depths[wave] || o.depths[wave] > 1000)
        throw std::invalid_argument("wave width/depth outside supported bounds");
      total += o.widths[wave] * o.depths[wave];
    }
    if (total > 100000) throw std::invalid_argument("total CPU nodes must be <= 100000");
  }
  return o;
}

struct Topology {
  std::vector<int> allowed;
  std::vector<std::vector<int>> configured, effective;
  std::vector<int> numa;
  bool overlaps = false;
  Executor::Config config;
};
Topology topology(const Options& o) {
  Topology t;
  t.allowed = allowedCpus();
  t.configured.resize(o.pools);
  t.effective.resize(o.pools);
  t.numa.resize(o.pools, -1);
  if (!o.cpus.empty()) {
    auto parts = split(o.cpus, ';');
    if (parts.size() != o.pools) throw std::invalid_argument("--cpus count must equal --pools");
    for (size_t i = 0; i < parts.size(); ++i) {
      t.configured[i] = parseCpus(parts[i]);
      for (int cpu : t.configured[i])
        if (!std::binary_search(t.allowed.begin(), t.allowed.end(), cpu))
          throw std::invalid_argument("CPU " + std::to_string(cpu) + " outside process allowed cpuset");
    }
  }
  if (!o.numa.empty()) {
    auto ns = split(o.numa, ',');
    if (ns.size() != o.pools) throw std::invalid_argument("--numa count must equal --pools");
    for (size_t i = 0; i < ns.size(); ++i) {
      auto n = parseU64(ns[i]);
      if (n > 65535) throw std::invalid_argument("invalid NUMA label");
      t.numa[i] = int(n);
    }
  }
  if (o.affinity != "none" && o.affinity != "cluster" && o.affinity != "numa")
    throw std::invalid_argument("invalid affinity");
  if (o.affinity != "none" && o.cpus.empty())
    throw std::invalid_argument("binding needs explicit --cpus; physical clusters are never inferred");
  if (o.affinity == "numa" && o.numa.empty()) throw std::invalid_argument("numa binding needs --numa");
  if (o.candidateScope == "preferred-numa") {
    if (o.affinity == "none" || o.cpus.empty() || o.numa.empty())
      throw std::invalid_argument("preferred-numa needs explicit --cpus/--numa and cluster or numa affinity");
    // Group labels alone cannot establish where a worker may actually execute.
    // Validate physical NUMA membership before a restricted candidate policy.
    for (size_t group = 0; group < o.pools; ++group) for (int cpu : t.configured[group]) {
      const auto cpuPath = std::filesystem::path("/sys/devices/system/cpu") / ("cpu" + std::to_string(cpu));
      int actual = -1;
      for (const auto& entry : std::filesystem::directory_iterator(cpuPath)) {
        const auto name = entry.path().filename().string();
        if (name.size() > 4 && name.rfind("node", 0) == 0 &&
            name.find_first_not_of("0123456789", 4) == std::string::npos) {
          if (actual != -1) throw std::invalid_argument("ambiguous sysfs NUMA membership");
          actual = int(parseU64(name.substr(4)));
        }
      }
      if (actual != t.numa[group])
        throw std::invalid_argument("NUMA label does not match sysfs for CPU " + std::to_string(cpu));
    }
    t.config.candidateScope = Executor::CandidateScope::PreferredNuma;
  }
  std::vector<double> caps;
  if (!o.capacities.empty()) {
    auto cs = split(o.capacities, ',');
    if (cs.size() != o.pools) throw std::invalid_argument("--capacities count must equal --pools");
    for (const auto& c : cs) { caps.push_back(parseDouble(c)); if (caps.back() <= 0) throw std::invalid_argument("capacity must be positive"); }
  }
  for (size_t i = 0; i < o.pools; ++i) {
    auto effective = o.affinity == "none" ? t.allowed : t.configured[i];
    if (o.affinity == "numa") {
      std::set<int> merged;
      for (size_t j = 0; j < o.pools; ++j)
        if (t.numa[j] == t.numa[i]) merged.insert(t.configured[j].begin(), t.configured[j].end());
      effective.assign(merged.begin(), merged.end());
    }
    t.effective[i] = effective;
    Executor::GroupConfig group;
    // An empty list means inherit affinity, rather than resetting the calling process mask.
    group.cpus = o.affinity == "none" ? std::vector<int>{} : effective;
    group.numaNode = t.numa[i];
    group.threads = o.threads;
    group.capacity = caps.empty() ? double(std::min(o.threads, effective.size())) : caps[i];
    group.name = "dag-g" + std::to_string(i);
    t.config.groups.push_back(std::move(group));
  }
  for (size_t i = 0; i < o.pools; ++i) for (size_t j = 0; j < i; ++j)
    for (int cpu : t.effective[i]) if (std::binary_search(t.effective[j].begin(), t.effective[j].end(), cpu)) t.overlaps = true;
  if (o.policy == "home") t.config.policy = Executor::Policy::Home;
  else if (o.policy == "borrow") t.config.policy = Executor::Policy::Borrow;
  else if (o.policy == "sticky") t.config.policy = Executor::Policy::Sticky;
  else if (o.policy == "fanout") t.config.policy = Executor::Policy::Fanout;
  else if (o.policy == "least-load") t.config.policy = Executor::Policy::LeastLoad;
  else throw std::invalid_argument("invalid policy");
  if (o.metric == "work") t.config.metric = Executor::LoadMetric::Work;
  else if (o.metric == "queue") t.config.metric = Executor::LoadMetric::Queue;
  else if (o.metric == "pending") t.config.metric = Executor::LoadMetric::Pending;
  else throw std::invalid_argument("invalid metric");
  if (o.queue == "default") t.config.queueKind = Executor::QueueKind::Default;
  else if (o.queue == "lifo") t.config.queueKind = Executor::QueueKind::Lifo;
  else if (o.queue == "throttled") t.config.queueKind = Executor::QueueKind::Throttled;
  else throw std::invalid_argument("invalid queue");
  t.config.wakeUpInterval = std::chrono::microseconds(o.wakeUs);
  t.config.crossNumaPenaltyUs = o.crossNumaPenaltyUs;
  t.config.minGainUs = o.minGainUs;
  t.config.relativeGain = o.relativeGain;
  return t;
}

struct TaskRecord {
  bool used = false;
  int workerTid = 0;
  uint64_t submit = 0, start = 0, finish = 0, cpuNs = 0, estimate = 0, work = 0;
  size_t parent = 0, depth = 0, wave = 0;
  bool isFanout = false, balanceEligible = false, isJoin = false;
  Executor::Decision decision{};
  std::string status;
};
struct Node {
  uint64_t workUs = 0, rpcDelayUs = 0;
  bool rpc = false;
  TaskRecord records[2];
};
struct Request {
  uint64_t id = 0, scheduled = 0, offered = 0, finish = 0, plannedCpuUs = 0;
  size_t home = 0, fanout = 0;
  std::atomic<uint64_t> firstStart{0};
  std::atomic<size_t> remaining{0};
  std::atomic<bool> canceled{false}, failed{false};
  std::vector<Node> nodes;
  std::vector<TaskRecord> joins;
  std::vector<size_t> widths, depths, offsets;
  size_t operatorCount = 0;
  std::vector<uint64_t> data;
  std::string status;
};
struct Second {
  uint64_t offered = 0, accepted = 0, rejected = 0, censored = 0, failed = 0;
  std::vector<double> latencies;
};

// A single timer loop models AIO completions; no executor worker sleeps on RPCs.
class Timers {
 public:
  Timers() : thread_([this] { loop(); }) {}
  ~Timers() { stop(); }
  void add(std::chrono::microseconds delay, std::function<void()> fn) {
    std::unique_lock<std::mutex> lock(mutex_);
    if (stopped_) { lock.unlock(); fn(); return; }
    events_.push(Event{Clock::now() + delay, seq_++, std::move(fn)});
    cv_.notify_one();
  }
  void flush() {
    std::vector<std::function<void()>> callbacks;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopped_ = true;
      while (!events_.empty()) { callbacks.push_back(std::move(const_cast<Event&>(events_.top()).fn)); events_.pop(); }
    }
    cv_.notify_all();
    for (auto& fn : callbacks) fn();
  }
  void stop() { flush(); if (thread_.joinable()) thread_.join(); }
 private:
  struct Event { Clock::time_point due; uint64_t seq; std::function<void()> fn; };
  struct Later { bool operator()(const Event& a, const Event& b) const { return a.due == b.due ? a.seq > b.seq : a.due > b.due; } };
  void loop() {
    std::unique_lock<std::mutex> lock(mutex_);
    for (;;) {
      if (stopped_ && events_.empty()) break;
      if (events_.empty()) { cv_.wait(lock); continue; }
      auto due = events_.top().due;
      if (Clock::now() < due) { cv_.wait_until(lock, due); continue; }
      auto fn = std::move(const_cast<Event&>(events_.top()).fn);
      events_.pop();
      lock.unlock(); fn(); lock.lock();
    }
  }
  std::mutex mutex_;
  std::condition_variable cv_;
  std::priority_queue<Event, std::vector<Event>, Later> events_;
  uint64_t seq_ = 0;
  bool stopped_ = false;
  std::thread thread_;
};

class Experiment {
 public:
  Experiment(Options options, Topology topo) : options_(std::move(options)), topo_(std::move(topo)) {
    std::filesystem::create_directories(options_.output);
    for (const char* f : {"metadata.json", "tasks.csv", "requests.csv", "pools.csv", "summary.json"})
      if (std::filesystem::exists(path(f))) throw std::runtime_error("refusing to overwrite existing run: " + path(f));
    tasks_.open(path("tasks.csv")); requests_.open(path("requests.csv")); pools_.open(path("pools.csv"));
    if (!tasks_ || !requests_ || !pools_) throw std::runtime_error("cannot create output files");
    tasks_ << "request_id,node_id,stage,home_group,parent_group,chosen_group,submit_ns,start_ns,finish_ns,enqueue_us,cpu_us,wall_us,pending_before,inflight_before,outstanding_before,outstanding_cpu_us_before,queue_before,work_estimate_us,fanout,depth,is_fanout,migrated,cross_numa,preferred_load_us,chosen_load_us,move_reason,work_target_us,worker_tid,status,wave,kind,balance_eligible,predecessors,rpc_delay_target_us\n";
    requests_ << "request_id,scheduled_ns,offered_ns,first_start_ns,finish_ns,latency_us,accepted_to_finish_us,admission_lag_us,status,fanout,planned_cpu_us,operator_nodes,cpu_nodes,waves\n";
    pools_ << "time_ns,group,pending,inflight,outstanding,estimated_outstanding_cpu_us,queue_depth,submitted,completed,rejected,migrated_in,failed,capacity\n";
    executor_ = std::make_unique<Executor>(topo_.config);
    origin_ = clockNs(CLOCK_MONOTONIC);
    std::ofstream workers(path("workers.csv"));
    workers << "group,tid\n";
    for (const auto& s : executor_->snapshots()) for (auto tid : s.workerTids) workers << s.group << ',' << tid << '\n';
    writeMetadata("running");
  }
  ~Experiment() {
    cancel_.store(true, std::memory_order_relaxed);
    timers_.stop();
    if (executor_) executor_->join();
    samplerStop_.store(true, std::memory_order_relaxed);
    sampleCv_.notify_all();
    if (sampler_.joinable()) sampler_.join();
    { std::lock_guard<std::mutex> lock(logMutex_); logStop_ = true; }
    logCv_.notify_all();
    if (logger_.joinable()) logger_.join();
  }
  int run() {
    // Generate exogenous arrivals before starting the clock. A Poisson horizon
    // must not be capped at ceil(rate * duration), which would clip busy seeds.
    std::vector<uint64_t> arrivals;
    double nextSeconds = 0;
    const uint64_t limit = options_.requests ? options_.requests : 1000000;
    for (uint64_t id = 0; ; ++id) {
      if (options_.arrival == "poisson")
        nextSeconds += -std::log1p(-unit(options_.seed ^ mix(id) ^ 0xa771a1ULL)) / options_.qps;
      else nextSeconds = double(id) / options_.qps;
      if (nextSeconds >= options_.duration || (options_.requests && id >= limit)) break;
      if (id >= limit) throw std::invalid_argument("arrival realization exceeds one million requests; shorten duration");
      arrivals.push_back(uint64_t(nextSeconds * 1e9));
    }
    logger_ = std::thread([this] { loggerLoop(); });
    sampler_ = std::thread([this] { samplerLoop(); });
    arrivalOrigin_ = now();
    for (uint64_t id = 0; id < arrivals.size(); ++id) {
      uint64_t offset = arrivals[id];
      // Use the exact same clock and absolute origin for deadlines and CSVs.
      // Sampling steady_clock and CLOCK_MONOTONIC separately can invert timestamps
      // if the producer is preempted between the two samples.
      const uint64_t deadlineNs = origin_ + arrivalOrigin_ + offset;
      const timespec deadline{time_t(deadlineNs / 1000000000ULL), long(deadlineNs % 1000000000ULL)};
      int sleepError;
      do { sleepError = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, nullptr); }
      while (sleepError == EINTR);
      if (sleepError) throw std::runtime_error("clock_nanosleep failed");
      auto offeredAt = now();
      // A slot includes finished requests not yet serialized, bounding detailed telemetry too.
      const bool admitted = active_.load(std::memory_order_relaxed) < options_.maxInflight;
      auto r = makeRequest(id, arrivalOrigin_ + offset, admitted);
      r->offered = offeredAt;
      ++offered_;
      if (!admitted) {
        r->status = "rejected"; ++rejected_;
        // Only the arrival producer may wait for rejected-row logging. Scheduled
        // arrivals stay fixed, so any resulting admission lag remains observable.
        { std::unique_lock<std::mutex> lock(logMutex_);
          logSpaceCv_.wait(lock, [this] { return logQueue_.size() < options_.maxInflight; });
          logQueue_.push_back(std::move(r)); }
        logCv_.notify_one(); continue;
      }
      active_.fetch_add(1, std::memory_order_relaxed);
      ++accepted_;
      startWave(r, 0, r->home);
    }
    admissionEnd_ = now();
    {
      std::unique_lock<std::mutex> lock(doneMutex_);
      if (!doneCv_.wait_for(lock, std::chrono::duration<double>(options_.drainSeconds),
                           [this] { return active_.load(std::memory_order_acquire) == 0; })) {
        censorAt_.store(now(), std::memory_order_relaxed);
        timedOut_ = true;
        cancel_.store(true, std::memory_order_release);
        lock.unlock();
        timers_.flush();
        lock.lock();
        // CPU kernels check cancellation every bounded batch; queued work becomes cheap no-op.
        doneCv_.wait(lock, [this] { return active_.load(std::memory_order_acquire) == 0; });
      }
    }
    timers_.stop();
    executor_->join();
    samplerStop_.store(true, std::memory_order_relaxed); sampleCv_.notify_all(); sampler_.join();
    sample();
    { std::lock_guard<std::mutex> lock(logMutex_); logStop_ = true; }
    logCv_.notify_all(); logger_.join();
    finish_ = now();
    writeSeconds(); writeSummary(); writeMetadata("completed");
    tasks_.flush(); requests_.flush(); pools_.flush();
    if (!tasks_ || !requests_ || !pools_ || ioFailed_) throw std::runtime_error("output write failed; results are incomplete");
    std::cout << "offered=" << offered_ << " accepted=" << accepted_ << " completed=" << completed_
              << " rejected=" << rejected_ << " censored=" << censored_ << " failed=" << failed_
              << " tasks=" << tasksLogged_ << " output=" << options_.output << '\n';
    return failed_ ? 2 : 0;
  }
 private:
  std::string path(const char* f) const { return (std::filesystem::path(options_.output) / f).string(); }
  uint64_t now() const { return clockNs(CLOCK_MONOTONIC) - origin_; }
  std::shared_ptr<Request> makeRequest(uint64_t id, uint64_t scheduled, bool admitted) {
    auto r = std::make_shared<Request>();
    r->id = id; r->scheduled = scheduled; r->home = id % options_.pools;
    auto key = mix(options_.seed ^ mix(id));
    r->fanout = options_.fanoutMin + key % (options_.fanoutMax - options_.fanoutMin + 1);
    r->widths = options_.graph == "waves" ? options_.widths : std::vector<size_t>{r->fanout};
    r->depths = options_.graph == "waves" ? options_.depths : std::vector<size_t>{options_.chainLength};
    r->fanout = r->widths[0];
    for (size_t wave = 0; wave < r->widths.size(); ++wave) {
      r->offsets.push_back(r->operatorCount);
      r->operatorCount += r->widths[wave] * r->depths[wave];
    }
    if (admitted) { r->nodes.resize(r->operatorCount); r->joins.resize(r->widths.size()); }
    for (size_t i = 0; i < r->operatorCount; ++i) {
      auto k = mix(key ^ mix(i + 1));
      double work = double(options_.cpuUs) * (1 + options_.jitter * (2 * unit(k) - 1));
      if (unit(k ^ 0xdeadbeefULL) < options_.longProb) {
        work = options_.longMaxUs ? options_.longMinUs +
            unit(k ^ 0x7a11ULL) * (options_.longMaxUs - options_.longMinUs) : work * options_.longFactor;
      }
      const auto workUs = std::max<uint64_t>(2, uint64_t(std::llround(work)));
      if (admitted) {
        r->nodes[i].workUs = workUs;
        r->nodes[i].rpc = unit(k ^ 0xa10a10ULL) < options_.rpcProb;
        r->nodes[i].rpcDelayUs = uint64_t(std::llround(options_.rpcDelayUs *
            (1 + options_.rpcJitter * (2 * unit(k ^ 0xd31a9ULL) - 1))));
      }
      r->plannedCpuUs += workUs;
    }
    r->plannedCpuUs += options_.joinUs * r->widths.size();
    // Immutable request-local memory lets successors reuse data without adding a data race.
    if (admitted) r->data.resize(options_.memoryKb * 1024 / sizeof(uint64_t));
    for (size_t i = 0; i < r->data.size(); ++i) r->data[i] = mix(key + i);
    return r;
  }
  bool cpuWork(uint64_t us, const std::vector<uint64_t>& data) {
    uint64_t start = clockNs(CLOCK_THREAD_CPUTIME_ID), state = mix(us + 1);
    static thread_local volatile uint64_t sink = 0;
    const bool fixed = options_.workMode == "iterations";
    uint64_t remaining = fixed ? us * options_.iterationsPerUs : 0;
    for (;;) {
      const uint64_t batch = fixed ? std::min<uint64_t>(remaining, 256) : 256;
      for (uint64_t i = 0; i < batch; ++i) {
        state ^= state << 13; state ^= state >> 7; state ^= state << 17;
        if (!data.empty()) state += data[state % data.size()];
      }
      if (cancel_.load(std::memory_order_relaxed)) { sink = state; return false; }
      if (fixed) { remaining -= batch; if (remaining == 0) break; }
      else if (clockNs(CLOCK_THREAD_CPUTIME_ID) - start >= us * 1000) break;
    }
    sink = state;
    (void)sink;
    return true;
  }
  void setFirstStart(const std::shared_ptr<Request>& r, uint64_t start) {
    auto previous = r->firstStart.load(std::memory_order_relaxed);
    while ((previous == 0 || start < previous) &&
           !r->firstStart.compare_exchange_weak(previous, start, std::memory_order_relaxed)) {}
  }
  // The last branch releases a join; that join alone releases the next wave.
  // remaining's acquire/release chain publishes all predecessor records before logging.
  // A branch owns exactly one decrement across its CPU stages and optional RPC.
  // A canceled continuation performs that decrement without scheduling descendants.
  // A wave cannot reset remaining until the previous wave's last decrement AND
  // its independently submitted join have completed; no older branch can decrement it.
  void startWave(const std::shared_ptr<Request>& r, size_t wave, size_t parent) noexcept {
    r->remaining.store(r->widths[wave], std::memory_order_relaxed);
    for (size_t branch = 0; branch < r->widths[wave]; ++branch)
      submitNode(r, wave, branch, 0, 0, parent);
  }
  void submitNode(const std::shared_ptr<Request>& r, size_t wave, size_t branch, size_t depth, int stage, size_t parent) noexcept {
    if (cancel_.load(std::memory_order_acquire)) { r->canceled.store(true); branchDone(r, wave, parent); return; }
    const size_t nodeId = r->offsets[wave] + branch * r->depths[wave] + depth;
    auto& node = r->nodes[nodeId];
    auto& rec = node.records[stage];
    rec.used = true; rec.submit = now(); rec.parent = parent; rec.depth = depth; rec.wave = wave;
    rec.isFanout = depth == 0 && stage == 0;
    rec.balanceEligible = rec.isFanout && (wave == 0 || options_.balanceForks == "all");
    rec.work = node.rpc ? (stage == 0 ? node.workUs / 2 : node.workUs - node.workUs / 2) : node.workUs;
    rec.estimate = options_.estimateUs ? options_.estimateUs : rec.work;
    Executor::Hint hint;
    hint.home = r->home; hint.parent = parent; hint.hasParent = !(wave == 0 && rec.isFanout);
    hint.isFanout = rec.balanceEligible; hint.estimatedCpuUs = rec.estimate;
    try {
      bool admitted = executor_->add(hint, [this, r, wave, branch, depth, stage, nodeId](const Executor::Decision& decision) {
        auto& n = r->nodes[nodeId]; auto& record = n.records[stage];
        record.decision = decision; record.workerTid = workerTid(); record.start = now(); setFirstStart(r, record.start);
        const auto cpuStart = clockNs(CLOCK_THREAD_CPUTIME_ID);
        bool ok = false;
        try { ok = cpuWork(record.work, r->data); record.status = ok ? "completed" : "canceled"; }
        catch (...) { record.status = "failed"; r->failed.store(true); }
        record.cpuNs = clockNs(CLOCK_THREAD_CPUTIME_ID) - cpuStart; record.finish = now();
        if (!ok) { if (record.status == "canceled") r->canceled.store(true); branchDone(r, wave, decision.chosen); return; }
        if (n.rpc && stage == 0) {
          try {
            timers_.add(std::chrono::microseconds(n.rpcDelayUs), [this, r, wave, branch, depth, chosen = decision.chosen] {
              submitNode(r, wave, branch, depth, 1, chosen);
            });
          } catch (...) { r->failed.store(true); branchDone(r, wave, decision.chosen); }
        } else if (depth + 1 < r->depths[wave]) {
          submitNode(r, wave, branch, depth + 1, 0, decision.chosen);
        } else { branchDone(r, wave, decision.chosen); }
      });
      if (!admitted) { rec.status = "submission_rejected"; rec.finish = now(); r->failed.store(true); branchDone(r, wave, parent); }
    } catch (...) { rec.status = "failed"; rec.finish = now(); r->failed.store(true); branchDone(r, wave, parent); }
  }
  void branchDone(const std::shared_ptr<Request>& r, size_t wave, size_t parent) noexcept {
    if (r->remaining.fetch_sub(1, std::memory_order_acq_rel) != 1) return;
    if (cancel_.load(std::memory_order_acquire)) r->canceled.store(true);
    if (r->canceled.load() || r->failed.load()) { finishRequest(r); return; }
    auto& rec = r->joins[wave];
    rec.used = true; rec.isJoin = true; rec.wave = wave; rec.submit = now(); rec.parent = parent;
    rec.depth = r->depths[wave]; rec.work = options_.joinUs; rec.estimate = options_.estimateUs ? options_.estimateUs : rec.work;
    Executor::Hint hint;
    hint.home = r->home; hint.parent = parent; hint.hasParent = true; hint.isFanout = false; hint.estimatedCpuUs = rec.estimate;
    try {
      bool admitted = executor_->add(hint, [this, r, wave](const Executor::Decision& decision) {
        auto& record = r->joins[wave]; record.decision = decision; record.workerTid = workerTid(); record.start = now(); setFirstStart(r, record.start);
        auto cpuStart = clockNs(CLOCK_THREAD_CPUTIME_ID);
        bool ok = false;
        try {
          ok = cpuWork(record.work, r->data); record.status = ok ? "completed" : "canceled";
          if (!ok) r->canceled.store(true);
        } catch (...) { record.status = "failed"; r->failed.store(true); }
        record.cpuNs = clockNs(CLOCK_THREAD_CPUTIME_ID) - cpuStart; record.finish = now();
        if (ok && wave + 1 < r->widths.size()) startWave(r, wave + 1, decision.chosen);
        else finishRequest(r);
      });
      if (!admitted) { rec.status = "submission_rejected"; rec.finish = now(); r->failed.store(true); finishRequest(r); }
    } catch (...) { rec.status = "failed"; rec.finish = now(); r->failed.store(true); finishRequest(r); }
  }
  void finishRequest(const std::shared_ptr<Request>& r) noexcept {
    r->finish = now();
    const auto cutoff = censorAt_.load(std::memory_order_acquire);
    if (cutoff && r->finish >= cutoff) r->canceled.store(true);
    if (r->failed.load()) r->status = "failed";
    else if (r->canceled.load()) { r->status = "censored"; if (censorAt_.load()) r->finish = censorAt_.load(); }
    else r->status = "completed";
    enqueueLog(r);
  }
  void enqueueLog(std::shared_ptr<Request> r) noexcept {
    try { { std::lock_guard<std::mutex> lock(logMutex_); logQueue_.push_back(std::move(r)); } logCv_.notify_one(); }
    catch (...) { std::cerr << "fatal: telemetry queue allocation failed\n"; std::terminate(); }
  }
  void writeTask(const Request& r, size_t id, int stage, const TaskRecord& t) {
    if (!t.used || !options_.taskLog) return;
    const auto& d = t.decision;
    std::string reason = d.chosen == d.preferred ? "preferred" : "load_balance";
    if (options_.policy == "home") reason = "home";
    else if (options_.policy == "borrow" && t.parent != r.home && d.chosen == r.home) reason = "return_home";
    else if ((options_.policy == "sticky" || options_.policy == "fanout") && !t.balanceEligible && d.chosen == t.parent) reason = "follow_parent";
    tasks_ << r.id << ',' << id << ',' << stage << ',' << r.home << ',' << t.parent << ',' << d.chosen << ','
           << t.submit << ',' << t.start << ',' << t.finish << ',';
    if (t.start) tasks_ << double(t.start - t.submit) / 1000;
    tasks_ << ',' << double(t.cpuNs) / 1000 << ',';
    if (t.start) tasks_ << double(t.finish - t.start) / 1000;
    tasks_ << ',' << d.chosenPending << ',' << d.chosenInflight << ',' << d.chosenOutstanding << ','
           << d.chosenOutstandingCpuUs << ',' << d.chosenQueueDepth << ',' << t.estimate << ',' << r.fanout << ','
           << t.depth << ',' << t.isFanout << ',' << d.migrated << ',' << d.crossNuma << ','
           << d.preferredLoadUs << ',' << d.chosenLoadUs << ',' << reason << ',' << t.work << ',' << t.workerTid << ',' << t.status
           << ',' << t.wave << ',' << (t.isJoin ? "join" : "operator") << ',' << t.balanceEligible << ',';
    // IDs describe graph dependencies; RPC stage 1 also depends on stage 0 of the same ID.
    if (stage == 1) tasks_ << id;
    else if (t.isJoin) {
      for (size_t b = 0; b < r.widths[t.wave]; ++b) {
        if (b) tasks_ << ';';
        tasks_ << r.offsets[t.wave] + (b + 1) * r.depths[t.wave] - 1;
      }
    } else if (t.depth > 0) tasks_ << id - 1;
    else if (t.wave > 0) tasks_ << r.operatorCount + t.wave - 1;
    tasks_ << ',' << (stage == 1 ? r.nodes[id].rpcDelayUs : 0) << '\n';
    ++tasksLogged_;
  }
  void loggerLoop() {
    for (;;) {
      std::shared_ptr<Request> r;
      {
        std::unique_lock<std::mutex> lock(logMutex_);
        logCv_.wait(lock, [this] { return logStop_ || !logQueue_.empty(); });
        if (logQueue_.empty() && logStop_) break;
        r = std::move(logQueue_.front()); logQueue_.pop_front();
      }
      logSpaceCv_.notify_one();
      try {
        auto& arrival = seconds_[r->scheduled / 1000000000ULL];
        ++arrival.offered;
        if (r->status == "rejected") ++arrival.rejected;
        else ++arrival.accepted;
        if (r->status == "completed") {
          ++completed_;
          seconds_[r->finish / 1000000000ULL].latencies.push_back(double(r->finish - r->scheduled) / 1000);
        } else if (r->status == "censored") { ++censored_; ++seconds_[r->finish / 1000000000ULL].censored; }
        else if (r->status == "failed") { ++failed_; ++seconds_[r->finish / 1000000000ULL].failed; }
        for (size_t i = 0; i < r->nodes.size(); ++i) for (int s = 0; s < 2; ++s) writeTask(*r, i, s, r->nodes[i].records[s]);
        for (size_t wave = 0; wave < r->joins.size(); ++wave) writeTask(*r, r->operatorCount + wave, 0, r->joins[wave]);
        requests_ << r->id << ',' << r->scheduled << ',' << r->offered << ',' << r->firstStart.load() << ',' << r->finish << ',';
        if (r->status != "rejected") requests_ << double(r->finish - r->scheduled) / 1000;
        requests_ << ',';
        if (r->status != "rejected") requests_ << double(r->finish - r->offered) / 1000;
        requests_ << ',' << double(r->offered - r->scheduled) / 1000 << ',' << r->status << ',' << r->fanout << ',' << r->plannedCpuUs
                  << ',' << r->operatorCount << ',' << r->operatorCount + r->widths.size() << ',' << r->widths.size() << '\n';
      } catch (...) { ioFailed_ = true; }
      if (r->status != "rejected") {
        // Synchronize with condition-variable predicate to avoid a missed final notification.
        { std::lock_guard<std::mutex> lock(doneMutex_); active_.fetch_sub(1, std::memory_order_release); }
        doneCv_.notify_all();
      }
    }
  }
  void sample() {
    auto ts = now();
    for (const auto& s : executor_->snapshots())
      pools_ << ts << ',' << s.group << ',' << s.pending << ',' << s.inflight << ',' << s.outstanding << ','
             << s.estimatedOutstandingCpuUs << ',' << s.queueDepth << ',' << s.submitted << ',' << s.completed << ','
             << s.rejected << ',' << s.migratedIn << ',' << s.failed << ',' << s.capacity << '\n';
  }
  void samplerLoop() {
    std::unique_lock<std::mutex> lock(sampleMutex_);
    while (!samplerStop_.load(std::memory_order_relaxed)) {
      sample();
      sampleCv_.wait_for(lock, std::chrono::milliseconds(options_.sampleMs), [this] { return samplerStop_.load(); });
    }
  }
  void writeSeconds() {
    std::ofstream out(path("seconds.csv"));
    out << "second,completed,mean_us,p99_us,offered,accepted,rejected,censored,failed\n";
    uint64_t last = seconds_.empty() ? 0 : seconds_.rbegin()->first;
    for (uint64_t second = 0; second <= last; ++second) {
      auto& s = seconds_[second]; std::sort(s.latencies.begin(), s.latencies.end());
      out << second << ',' << s.latencies.size() << ',';
      if (!s.latencies.empty()) out << std::accumulate(s.latencies.begin(), s.latencies.end(), 0.0) / s.latencies.size();
      out << ',';
      if (!s.latencies.empty()) out << s.latencies[size_t(std::ceil(.99 * s.latencies.size())) - 1];
      out << ',' << s.offered << ',' << s.accepted << ',' << s.rejected << ',' << s.censored << ',' << s.failed << '\n';
    }
    if (!out) ioFailed_ = true;
  }
  void writeSummary() {
    std::ofstream out(path("summary.json"));
    out << "{\n  \"offered\": " << offered_ << ", \"accepted\": " << accepted_ << ", \"rejected\": " << rejected_
        << ", \"completed\": " << completed_ << ", \"censored\": " << censored_ << ", \"failed\": " << failed_
        << ",\n  \"task_rows\": " << tasksLogged_ << ", \"drain_timed_out\": " << (timedOut_ ? "true" : "false")
        << ", \"admission_end_ns\": " << admissionEnd_ << ", \"end_ns\": " << finish_ << ",\n"
        << "  \"conservation_ok\": " << ((offered_ == accepted_ + rejected_ && accepted_ == completed_ + censored_ + failed_) ? "true" : "false")
        << ", \"io_failed\": " << (ioFailed_ ? "true" : "false") << "\n}\n";
    if (!out) ioFailed_ = true;
  }
  void writeMetadata(const std::string& phase) {
    auto temporary = path("metadata.json.tmp");
    std::ofstream out(temporary);
    utsname host{}; uname(&host);
    out << std::setprecision(16) << "{\n  \"schema_version\": 3, \"phase\": " << json(phase)
        << ", \"backend\": " << json(kBackend)
        << ", \"pid\": " << getpid() << ",\n  \"clock\": \"CLOCK_MONOTONIC\", \"origin_monotonic_ns\": " << origin_
        << ", \"arrival_origin_ns\": " << arrivalOrigin_
        << ", \"cpu_clock\": \"CLOCK_THREAD_CPUTIME_ID\",\n  \"architecture\": " << json(host.machine)
        << ", \"kernel\": " << json(host.release) << ", \"hostname\": " << json(host.nodename)
        << ",\n  \"allowed_cpus\": [" << joinInts(topo_.allowed) << "], \"effective_cpuset_overlap\": " << (topo_.overlaps ? "true" : "false")
        << ", \"numa_labels_verified\": " << (options_.candidateScope == "preferred-numa" ? "true" : "false")
        << ",\n  \"config\": {\"pools\": " << options_.pools << ", \"threads_per_pool\": " << options_.threads
        << ", \"affinity\": " << json(options_.affinity) << ", \"policy\": " << json(options_.policy) << ", \"metric\": " << json(options_.metric)
        << ", \"queue\": " << json(options_.queue) << ", \"wake_us\": " << options_.wakeUs << ", \"seed\": " << options_.seed
        << ", \"qps\": " << options_.qps << ", \"duration\": " << options_.duration << ", \"requests\": " << options_.requests
        << ", \"arrival\": " << json(options_.arrival)
        << ", \"candidate_scope\": " << json(options_.candidateScope)
        << ", \"fanout_min\": " << options_.fanoutMin << ", \"fanout_max\": " << options_.fanoutMax << ", \"chain_length\": " << options_.chainLength
        << ", \"graph\": " << json(options_.graph) << ", \"wave_widths\": " << json(options_.waveWidths)
        << ", \"wave_depths\": " << json(options_.waveDepths) << ", \"balance_forks\": " << json(options_.balanceForks)
        << ", \"cpu_us\": " << options_.cpuUs << ", \"jitter\": " << options_.jitter << ", \"long_prob\": " << options_.longProb
        << ", \"long_factor\": " << options_.longFactor << ", \"long_min_us\": " << options_.longMinUs
        << ", \"long_max_us\": " << options_.longMaxUs << ", \"join_us\": " << options_.joinUs << ", \"rpc_prob\": " << options_.rpcProb
        << ", \"rpc_jitter\": " << options_.rpcJitter
        << ", \"rpc_delay_us\": " << options_.rpcDelayUs << ", \"max_inflight\": " << options_.maxInflight
        << ", \"drain_seconds\": " << options_.drainSeconds << ", \"sample_ms\": " << options_.sampleMs << ", \"memory_kb\": " << options_.memoryKb
        << ", \"cross_numa_penalty_us\": " << options_.crossNumaPenaltyUs << ", \"min_gain_us\": " << options_.minGainUs
        << ", \"work_mode\": " << json(options_.workMode) << ", \"iterations_per_us\": " << options_.iterationsPerUs
        << ", \"task_log\": " << (options_.taskLog ? "true" : "false") << ", \"estimate_us\": " << options_.estimateUs
        << ", \"relative_gain\": " << options_.relativeGain << "},\n  \"groups\": [\n";
    for (size_t i = 0; i < options_.pools; ++i) {
      if (i) out << ",\n";
      out << "    {\"group\": " << i << ", \"numa_label\": " << topo_.numa[i]
          << ", \"configured_cpus\": [" << joinInts(topo_.configured[i]) << "], \"effective_cpus\": [" << joinInts(topo_.effective[i])
          << "], \"capacity\": " << topo_.config.groups[i].capacity << '}';
    }
    out << "\n  ],\n  \"limitations\": [\"Logical groups do not imply physical clusters; explicit topology must be verified.\","
        << "\"Capacity defaults to min(threads, logical CPU width), ignores SMT scaling and cgroup CPU quota unless overridden.\","
        << "\"Queue depth is Folly semaphore valueGuess, not physical occupancy; pending is accepted but callback not started.\","
        << "\"Inflight includes descheduled/runnable callbacks; full CPU estimates remain outstanding until completion.\","
        << "\"Snapshots use independently sampled relaxed atomics; do not assert simultaneous counter identities mid-run.\","
        << "\"Detailed instrumentation and synthetic CPU clocks perturb short tasks; compare against a production control.\","
        << "\"CPU-time mode fixes CPU use and cannot expose locality service-time cost; fixed iterations requires external calibration.\","
        << "\"Read-only request memory is initialized on main thread; no NUMA memory binding or real RPC/cache model.\","
        << "\"Waves use a complete barrier after independent branch chains; joins inherit the last predecessor group.\","
        << "\"is_fanout labels wave starts (including width one); balance_eligible controls fanout policy only.\","
        << "\"Admission slots include completed requests waiting for CSV serialization; rejects bound memory.\","
        << "\"seconds.csv uses final completion cohorts; rejected/censored requests are not successful latency observations.\","
        << "\"Default queue selection is pinned Folly build/runtime configuration; queue=lifo or throttled is explicit.\"]\n}\n";
    out.close();
    if (!out) throw std::runtime_error("metadata write failed");
    std::filesystem::rename(temporary, path("metadata.json"));
  }
  Options options_;
  uint64_t arrivalOrigin_ = 0;
  Topology topo_;
  std::unique_ptr<Executor> executor_;
  Timers timers_;
  uint64_t origin_ = 0, admissionEnd_ = 0, finish_ = 0;
  std::atomic<uint64_t> censorAt_{0};
  std::atomic<bool> cancel_{false}, samplerStop_{false};
  std::atomic<size_t> active_{0};
  uint64_t offered_ = 0, accepted_ = 0, rejected_ = 0, completed_ = 0, censored_ = 0, failed_ = 0, tasksLogged_ = 0;
  bool timedOut_ = false, ioFailed_ = false, logStop_ = false;
  std::mutex doneMutex_, logMutex_, sampleMutex_;
  std::condition_variable doneCv_, logCv_, logSpaceCv_, sampleCv_;
  std::deque<std::shared_ptr<Request>> logQueue_;
  std::thread logger_, sampler_;
  std::map<uint64_t, Second> seconds_;
  std::ofstream tasks_, requests_, pools_;
};

int main(int argc, char** argv) {
  try {
    auto options = parse(argc, argv);
    // Parse demo arguments ourselves; still initialize Folly's runtime without gflags consuming them.
    folly::Init follyInit(&argc, &argv, folly::InitOptions{}.useGFlags(false));
    auto topo = topology(options);
    Experiment experiment(std::move(options), std::move(topo));
    return experiment.run();
  } catch (const std::exception& e) { std::cerr << "executor_demo: " << e.what() << '\n'; return 1; }
}

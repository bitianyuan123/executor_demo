#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <executor_demo/AsyncCompletionSource.h>
#include <executor_demo/LinuxSchedstatLoadProvider.h>
#include <executor_demo/LocalityWorkload.h>
#include <executor_demo/RequestExecutor.h>
#include <filesystem>
#include <folly/json.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <numeric>
#include <random>
#include <sched.h>
#include <set>
#include <sstream>
#include <thread>
#include <time.h>
#include <sys/syscall.h>
#include <unistd.h>

using Pool = executor_demo::RequestExecutor;
using Work = executor_demo::LocalityWorkload;
using CpuSampler = executor_demo::LinuxSchedstatLoadProvider;
using CompletionSource = executor_demo::AsyncCompletionSource;
using Clock = std::chrono::steady_clock;
static uint64_t ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             Clock::now().time_since_epoch())
      .count();
}
static uint64_t processCpuNs() {
  timespec t{};
  if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t))
    throw std::runtime_error("process CPU clock failed");
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}
static uint64_t cpuNs() {
  timespec t{};
  if (clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t))
    throw std::runtime_error("thread CPU clock failed");
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}
static uint64_t mix(uint64_t x) {
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}
static std::vector<std::string> split(const std::string &s, char separator) {
  std::vector<std::string> v;
  std::istringstream in(s);
  std::string x;
  while (std::getline(in, x, separator))
    v.push_back(x);
  return v;
}
static uint64_t integer(const std::string &s) {
  size_t end = 0;
  if (s.empty() || s[0] == '-')
    throw std::invalid_argument("invalid integer: " + s);
  auto n = std::stoull(s, &end);
  if (end != s.size())
    throw std::invalid_argument("invalid integer: " + s);
  return n;
}
static std::vector<size_t> numbers(const std::string &s) {
  std::vector<size_t> v;
  for (auto &x : split(s, ','))
    v.push_back(integer(x));
  return v;
}
static std::vector<int> cpuList(const std::string &s) {
  std::set<int> result;
  for (auto &token : split(s, ',')) {
    auto range = split(token, '-');
    if (range.empty() || range.size() > 2)
      throw std::invalid_argument("invalid CPU range");
    auto lo = integer(range[0]),
         hi = range.size() == 2 ? integer(range[1]) : lo;
    if (hi < lo || hi >= CPU_SETSIZE)
      throw std::invalid_argument("CPU range exceeds supported cpu_set_t");
    for (auto i = lo; i <= hi; ++i)
      result.insert(int(i));
  }
  return {result.begin(), result.end()};
}
static int nodeOf(int cpu) {
  auto path = std::filesystem::path("/sys/devices/system/cpu/cpu" +
                                    std::to_string(cpu));
  for (auto &p : std::filesystem::directory_iterator(path)) {
    auto n = p.path().filename().string();
    if (n.rfind("node", 0) == 0 && n.size() > 4 &&
        std::all_of(n.begin() + 4, n.end(),
                    [](char c) { return c >= '0' && c <= '9'; }))
      return std::stoi(n.substr(4));
  }
  return -1;
}
struct Config {
  std::map<std::string, std::string> args;
  size_t requests = 24, maxInflight = 16, threads = 2, memoryMiB = 256;
  size_t poolCount = 0, burstSize = 4, syncBlockPercent = 0;
  uint64_t burstSpacingUs = 0, syncBlockUs = 0;
  size_t asyncPercent = 0, asyncCapacity = 4096;
  uint64_t asyncDelayUs = 0, asyncAlignUs = 0;
  uint64_t cpuSampleMs = 50, cpuMaxAgeMs = 150;
  uint64_t seed = 1, iterations = 10000, accesses = 1000, estimateUs = 1000;
  double qps = 12;
  size_t jitter = 0, longEvery = 0, longFactor = 8;
  std::vector<size_t> widths{8, 4}, depths{2, 2};
  std::string model = "branch-rw", policy = "home", continuation = "parent",
              arrival = "regular", affinity = "cluster",
              output = "results/affinity-smoke", balanceAt = "all",
              scaleScope = "task", estimateMode = "constant";
  bool trace = true, pageSample = false, cpuSampling = false;
  Work::Config work;
  Pool::Config pool;
  std::vector<std::vector<int>> partitions;
  size_t nodes = 0, branches = 0, taskSlots = 0;
};
static Config parse(int argc, char **argv) {
  Config c;
  std::set<std::string> keys = {"cpus",
                                "pool-count",
                                "affinity",
                                "threads",
                                "capacity-scale",
                                "requests",
                                "max-inflight",
                                "qps",
                                "arrival",
                                "burst-size",
                                "burst-spacing-us",
                                "sync-block-percent",
                                "sync-block-us",
                                "async-percent",
                                "async-delay-us",
                                "async-align-us",
                                "async-capacity",
                                "cpu-sampling",
                                "cpu-sample-ms",
                                "cpu-max-age-ms",
                                "seed",
                                "widths",
                                "depths",
                                "iterations",
                                "accesses",
                                "jitter-percent",
                                "long-every",
                                "long-factor",
                                "estimate-us",
                                "work-scale-scope",
                                "estimate-mode",
                                "model",
                                "shared-kb",
                                "branch-kb",
                                "memory-mb",
                                "policy",
                                "continuation",
                                "balance-at",
                                "home-policy",
                                "busy-high",
                                "return-low",
                                "min-gain",
                                "cross-numa",
                                "cross-numa-penalty",
                                "max-hops",
                                "batch-chunk",
                                "max-outstanding",
                                "trace",
                                "page-sample",
                                "output"};
  for (int i = 1; i < argc; ++i) {
    std::string key = argv[i];
    if (key == "--help") {
      std::cout << "See experiments/affinity/README.md. Options (all require a "
                   "value):\n";
      for (auto &k : keys)
        std::cout << "--" << k << "\n";
      std::exit(0);
    }
    if (key.rfind("--", 0) != 0 || !keys.count(key.substr(2)) || i + 1 >= argc)
      throw std::invalid_argument("unknown or incomplete argument: " + key);
    key = key.substr(2);
    if (!c.args.emplace(key, argv[++i]).second)
      throw std::invalid_argument("duplicate argument: " + key);
  }
  auto s = [&](const char *k, std::string d) {
    auto it = c.args.find(k);
    return it == c.args.end() ? d : it->second;
  };
  auto u = [&](const char *k, uint64_t d) {
    return integer(s(k, std::to_string(d)));
  };
  auto d = [&](const char *k, double v) {
    auto x = s(k, std::to_string(v));
    size_t end;
    double r = std::stod(x, &end);
    if (end != x.size() || !std::isfinite(r))
      throw std::invalid_argument("invalid real: " + x);
    return r;
  };
  auto b = [&](const char *k, bool v) {
    auto n = u(k, v);
    if (n > 1)
      throw std::invalid_argument("boolean requires 0 or 1");
    return bool(n);
  };
  c.requests = u("requests", c.requests);
  c.poolCount = u("pool-count", 0);
  c.burstSize = u("burst-size", c.burstSize);
  c.burstSpacingUs = u("burst-spacing-us", 0);
  c.syncBlockPercent = u("sync-block-percent", 0);
  c.syncBlockUs = u("sync-block-us", 0);
  c.asyncPercent = u("async-percent", 0);
  c.asyncDelayUs = u("async-delay-us", 0);
  c.asyncAlignUs = u("async-align-us", 0);
  c.asyncCapacity = u("async-capacity", 4096);
  if (c.asyncPercent > 100 || c.asyncDelayUs > 10000000 ||
      c.asyncAlignUs > 10000000 || !c.asyncCapacity ||
      c.asyncCapacity > 2000000 ||
      (!c.asyncPercent && (c.asyncDelayUs || c.asyncAlignUs)))
    throw std::invalid_argument(
        "invalid asynchronous completion configuration");
  c.maxInflight = u("max-inflight", c.maxInflight);
  c.threads = u("threads", c.threads);
  c.memoryMiB = u("memory-mb", c.memoryMiB);
  c.seed = u("seed", c.seed);
  c.iterations = u("iterations", c.iterations);
  c.accesses = u("accesses", c.accesses);
  c.estimateUs = u("estimate-us", c.estimateUs);
  c.qps = d("qps", c.qps);
  c.jitter = u("jitter-percent", 0);
  c.longEvery = u("long-every", 0);
  c.longFactor = u("long-factor", 8);
  c.scaleScope = s("work-scale-scope", "task");
  c.estimateMode = s("estimate-mode", "constant");
  if ((c.scaleScope != "task" && c.scaleScope != "request") ||
      (c.estimateMode != "constant" && c.estimateMode != "request-scale") ||
      (c.estimateMode == "request-scale" && c.scaleScope != "request"))
    throw std::invalid_argument(
        "request-scale estimates require work-scale-scope=request");
  c.widths = numbers(s("widths", "8,4"));
  c.depths = numbers(s("depths", "2,2"));
  c.model = s("model", c.model);
  c.policy = s("policy", c.policy);
  c.cpuSampling = b("cpu-sampling", c.policy == "cpu");
  c.cpuSampleMs = u("cpu-sample-ms", 50);
  c.cpuMaxAgeMs = u("cpu-max-age-ms", 150);
  if (!c.cpuSampleMs || c.cpuSampleMs > 10000 || !c.cpuMaxAgeMs ||
      c.cpuMaxAgeMs > 60000 || (c.policy == "cpu" && !c.cpuSampling))
    throw std::invalid_argument(
        "invalid CPU sampling configuration; cpu policy requires sampling");
  c.pool.cpuMaxAgeNs = c.cpuMaxAgeMs * 1000000;
  c.continuation = s("continuation", c.continuation);
  c.arrival = s("arrival", c.arrival);
  c.affinity = s("affinity", c.affinity);
  c.output = s("output", c.output);
  c.trace = b("trace", true);
  c.pageSample = b("page-sample", false);
  if (!c.requests || c.requests > 100000 || !c.maxInflight ||
      c.maxInflight > 10000 || !c.threads || c.threads > 4096 ||
      c.memoryMiB < 1 || c.memoryMiB > 1048576 || c.qps < 0.001 ||
      c.qps > 1000000 || c.jitter > 90 || !c.longFactor ||
      c.longFactor > 1000 || !c.estimateUs || c.estimateUs > 1000000000 ||
      c.widths.empty() || c.widths.size() != c.depths.size() ||
      c.widths.size() > 16)
    throw std::invalid_argument("configuration outside experiment bounds");
  if (c.poolCount > 64 || !c.burstSize || c.burstSize > 100000 ||
      c.burstSpacingUs > 1000000000 || c.syncBlockPercent > 100 ||
      c.syncBlockUs > 10000000 ||
      ((c.syncBlockPercent != 0) != (c.syncBlockUs != 0)))
    throw std::invalid_argument(
        "invalid pool/burst/synchronous-blocking configuration");
  c.nodes = 1;
  for (size_t i = 0; i < c.widths.size(); ++i) {
    if (!c.widths[i] || c.widths[i] > 4000 || !c.depths[i] ||
        c.depths[i] > 1000)
      throw std::invalid_argument("invalid graph dimensions");
    c.branches += c.widths[i];
    c.nodes += c.widths[i] * c.depths[i] + 1;
  }
  c.taskSlots = c.nodes * (c.asyncPercent ? 2 : 1);
  if (c.taskSlots * c.requests > 2000000 ||
      c.iterations > 1000000000000ULL / c.longFactor ||
      c.accesses > 1000000000000ULL / c.longFactor)
    throw std::invalid_argument("work/trace size exceeds experiment bound");
  if (c.arrival != "regular" && c.arrival != "poisson" && c.arrival != "burst")
    throw std::invalid_argument("arrival must be regular, poisson, or burst");
  if (c.arrival == "burst" &&
      static_cast<long double>(c.burstSize - 1) * c.burstSpacingUs >
          static_cast<long double>(c.burstSize) * 1000000 / c.qps)
    throw std::invalid_argument("burst spacing exceeds its batch period");
  if (c.model == "none")
    c.work.model = Work::Model::None;
  else if (c.model == "request-ro")
    c.work.model = Work::Model::RequestReadOnly;
  else if (c.model == "branch-rw")
    c.work.model = Work::Model::BranchReadWrite;
  else if (c.model == "mixed")
    c.work.model = Work::Model::Mixed;
  else
    throw std::invalid_argument("unknown model");
  const bool shared = c.model == "request-ro" || c.model == "mixed",
             branch = c.model == "branch-rw" || c.model == "mixed";
  auto sharedKB = u("shared-kb", shared ? 1024 : 0),
       branchKB = u("branch-kb", branch ? 64 : 0);
  if (sharedKB > c.memoryMiB * 1024 || branchKB > c.memoryMiB * 1024)
    throw std::invalid_argument("working set exceeds memory cap");
  c.work.sharedBytes = sharedKB * 1024;
  c.work.branchBytes = branchKB * 1024;
  c.work.branches = c.branches;
  c.work.maxBytes = c.memoryMiB * 1024 * 1024 / c.maxInflight;
  c.work.seed = c.seed;
  if (c.model == "none" && c.accesses)
    throw std::invalid_argument("none requires --accesses 0");
  if (std::filesystem::exists(c.output + "/summary.json") ||
      std::filesystem::exists(c.output + "/requests.csv"))
    throw std::invalid_argument(
        "output already contains results; choose a new output directory");
  Work check(c.work); // validates page rounding and max-inflight payload
                      // budget; never touches payload
  cpu_set_t allowed;
  CPU_ZERO(&allowed);
  if (sched_getaffinity(0, sizeof(allowed), &allowed))
    throw std::runtime_error("sched_getaffinity failed");
  auto cpuArg = s("cpus", "");
  if (cpuArg.empty()) {
    for (int cpu = 0; cpu < CPU_SETSIZE && c.partitions.size() < 2; ++cpu)
      if (CPU_ISSET(cpu, &allowed))
        c.partitions.push_back({cpu});
  } else
    for (auto &group : split(cpuArg, ';'))
      c.partitions.push_back(cpuList(group));
  if (c.partitions.empty() || c.partitions.size() > 64)
    throw std::invalid_argument("invalid group count");
  const bool explicitPoolCount = c.poolCount != 0;
  if (!c.poolCount)
    c.poolCount = c.partitions.size();
  if (c.affinity != "none" && c.poolCount != c.partitions.size())
    throw std::invalid_argument(
        "different pool-count and partition count requires affinity=none");
  std::set<int> seen;
  std::vector<int> nodes;
  for (auto &cpus : c.partitions) {
    if (cpus.empty())
      throw std::invalid_argument("empty CPU group");
    int node = nodeOf(cpus[0]);
    for (int cpu : cpus) {
      if (!CPU_ISSET(cpu, &allowed) || !seen.insert(cpu).second ||
          nodeOf(cpu) != node)
        throw std::invalid_argument("partitions must be disjoint, allowed, and "
                                    "each within one NUMA node");
    }
    nodes.push_back(node);
  }
  // Keep the producer and subsequently created helper threads in the same
  // resource universe as the workers, including the 'none' deployment.
  cpu_set_t experimentMask;
  CPU_ZERO(&experimentMask);
  for (int cpu : seen)
    CPU_SET(cpu, &experimentMask);
  if (sched_setaffinity(0, sizeof(experimentMask), &experimentMask))
    throw std::runtime_error("cannot bind producer to experiment CPU union");
  double scale = d("capacity-scale", 1);
  if (scale <= 0 || scale > 1)
    throw std::invalid_argument("capacity-scale must be in (0,1]");
  for (size_t i = 0; i < c.poolCount; ++i) {
    executor_demo::BusinessExecutor::GroupConfig g;
    g.threads = c.threads;
    g.numaNode = c.affinity == "none" ? -1 : nodes[i];
    g.capacity = std::min<double>(
        c.threads, c.affinity == "none" && explicitPoolCount
                       ? double(seen.size()) * scale / c.poolCount
                       : c.partitions[i].size() * scale);
    if (c.affinity == "cluster")
      g.cpus = c.partitions[i];
    else if (c.affinity == "numa") {
      for (size_t j = 0; j < c.partitions.size(); ++j)
        if (nodes[i] == nodes[j])
          g.cpus.insert(g.cpus.end(), c.partitions[j].begin(),
                        c.partitions[j].end());
    } else if (c.affinity == "none") {
      g.cpus.assign(seen.begin(), seen.end());
      g.numaNode = -1;
    } else
      throw std::invalid_argument("invalid affinity");
    c.pool.groups.push_back(std::move(g));
  }
  if (c.policy == "home")
    c.pool.policy = Pool::Policy::Home;
  else if (c.policy == "queue")
    c.pool.policy = Pool::Policy::QueueSpill;
  else if (c.policy == "outstanding")
    c.pool.policy = Pool::Policy::OutstandingSpill;
  else if (c.policy == "work")
    c.pool.policy = Pool::Policy::WorkSpill;
  else if (c.policy == "active")
    c.pool.policy = Pool::Policy::ActiveSpill;
  else if (c.policy == "cpu")
    c.pool.policy = Pool::Policy::CpuSpill;
  else
    throw std::invalid_argument("invalid policy");
  if (c.continuation == "parent")
    c.pool.continuationPolicy = Pool::ContinuationPolicy::FollowParent;
  else if (c.continuation == "home")
    c.pool.continuationPolicy = Pool::ContinuationPolicy::ReturnHome;
  else
    throw std::invalid_argument("invalid continuation policy");
  auto home = s("home-policy", "rr");
  if (home == "hash")
    c.pool.homePolicy = Pool::HomePolicy::ColorHash;
  else if (home != "rr")
    throw std::invalid_argument("invalid home policy");
  c.balanceAt = s("balance-at", "all");
  if (c.balanceAt != "all" && c.balanceAt != "fork")
    throw std::invalid_argument("balance-at must be all or fork");
  c.pool.balanceOnlyAtFork = c.balanceAt == "fork";
  c.pool.busyHigh =
      d("busy-high", c.policy == "work" ? double(c.estimateUs) : 1);
  c.pool.returnLow = d("return-low", c.pool.busyHigh / 2);
  c.pool.minGain = d("min-gain", 0);
  c.pool.crossNumaPenalty = d("cross-numa-penalty", 0);
  c.pool.allowCrossNuma = b("cross-numa", false);
  c.pool.maxHops = u("max-hops", 4);
  c.pool.batchChunk = u("batch-chunk", 1);
  c.pool.maxOutstanding = u("max-outstanding", 0);
  return c;
}
struct TaskRow {
  Pool::Observation observation;
  uint64_t cpu = 0, iterations = 0, reads = 0, writes = 0, checksum = 0;
  uint64_t syncBlockUs = 0, syncBlockWallNs = 0;
  int workerTid = 0; // Diagnostic join key; never used for routing.
  bool observed = false;
};
struct RequestRow {
  uint64_t scheduled = 0, submitted = 0, finished = 0, checksum = 0;
  size_t concurrent = 0;
  std::string status = "not_submitted", error;
};
struct AsyncRow {
  uint64_t issue = 0, due = 0, fired = 0, terminal = 0;
  const char *status = nullptr;
};
class Experiment {
  struct RequestState {
    RequestState(Work::Config config, size_t id, std::atomic<size_t> &active)
        : work(config), id(id), active(active) {
      active.fetch_add(1);
    }
    ~RequestState() { active.fetch_sub(1, std::memory_order_release); }
    Work work;
    size_t id;
    std::atomic<size_t> remaining{0};
    std::atomic<bool> terminal{false};
    std::atomic<size_t> &active;
  };

public:
  explicit Experiment(Config config)
      : c_(std::move(config)), requests_(c_.requests),
        tasks_(c_.trace ? c_.requests * c_.taskSlots : 0),
        asyncRows_(c_.trace && c_.asyncPercent ? c_.requests * c_.nodes : 0) {
    if (c_.trace)
      c_.pool.observer = [this](const Pool::Observation &observation) {
        if (observation.color >= c_.requests || observation.tag >= c_.taskSlots)
          throw std::logic_error("trace index");
        auto &row = tasks_[observation.color * c_.taskSlots + observation.tag];
        // Observer runs on the callback's worker. Cache once per worker, rather
        // than adding a syscall to every operator or to the trace=0 path.
        static thread_local const int tid = static_cast<int>(::syscall(SYS_gettid));
        row.workerTid = tid;
        row.observation = observation;
        row.observed = true;
      };
    if (c_.cpuSampling) {
      CpuSampler::Config samplerConfig;
      samplerConfig.interval = std::chrono::milliseconds(c_.cpuSampleMs);
      sampler_ =
          std::make_shared<CpuSampler>(samplerConfig, c_.pool.groups.size());
      c_.pool.loadProvider = sampler_;
    }
    pool_ = std::make_unique<Pool>(c_.pool);
    if (sampler_) {
      std::vector<std::vector<pid_t>> threadGroups;
      for (const auto &snapshot : pool_->snapshots())
        threadGroups.emplace_back(snapshot.workerTids.begin(),
                                  snapshot.workerTids.end());
      sampler_->setThreadGroups(threadGroups);
    }
  }
  ~Experiment() {
    // Failure cleanup must cancel leases before joining the executor. Both
    // objects and all callback state stay alive until callbacks have drained.
    if (completion_)
      completion_->abort();
    if (pool_)
      pool_->join();
    if (sampler_)
      sampler_->stop();
  }
  void run() {
    processStart_ = processCpuNs();
    if (c_.asyncPercent)
      completion_ =
          std::make_unique<CompletionSource>(c_.requests, c_.asyncCapacity);
    if (sampler_)
      sampler_->start();
    std::mt19937_64 rng(c_.seed);
    std::exponential_distribution<double> gap(c_.qps);
    origin_ = ns() + 10000000;
    uint64_t offset = 0;
    for (size_t id = 0; id < c_.requests; ++id) {
      if (c_.arrival == "burst")
        offset = uint64_t(static_cast<long double>(id / c_.burstSize) *
                          c_.burstSize * 1000000000 / c_.qps) +
                 (id % c_.burstSize) * c_.burstSpacingUs * 1000;
      else if (id)
        offset = c_.arrival == "regular" ? uint64_t(double(id) * 1e9 / c_.qps)
                                         : offset + uint64_t(gap(rng) * 1e9);
      auto &row = requests_[id];
      row.scheduled = origin_ + offset;
      arrivalHash_ = mix(arrivalHash_ ^ offset);
      std::this_thread::sleep_until(
          Clock::time_point(std::chrono::nanoseconds(row.scheduled)));
      row.submitted = ns();
      if (active_.load(std::memory_order_acquire) >= c_.maxInflight) {
        row.status = "admission_rejected";
        row.finished = ns();
        continue;
      }
      auto wc = c_.work;
      wc.requestId = id;
      auto state = std::make_shared<RequestState>(wc, id, active_);
      row.concurrent = active_.load();
      auto observed = peak_.load();
      while (observed < row.concurrent &&
             !peak_.compare_exchange_weak(observed, row.concurrent)) {
      }
      auto request = pool_->beginRequest(id);
      Pool::TaskOptions options;
      options.tag = 0;
      options.estimatedCpuUs = c_.estimateUs;
      options.pinHome = true;
      try {
        if (!pool_->add(
                request,
                [this, state] {
                  guarded(state, 0, [&] {
                    state->work.initialize();
                    if (c_.pageSample && state->id == 0)
                      pages_ = state->work.samplePageLocations();
                    fork(state, 0);
                  });
                },
                options))
          finish(state, "submit_rejected");
      } catch (const std::exception &error) {
        finish(state, "failed", error.what());
      }
    }
    pool_->join();
    // The source must continue accepting waits registered by callbacks while
    // the executor drains. Its last dispatch may still own request references.
    if (completion_)
      completion_->stopAfterDrain();
    if (sampler_)
      sampler_->stop();
    end_ = ns();
    processCpu_ = processCpuNs() - processStart_;
    if (active_ != 0 || pool_->observerFailures() != 0)
      throw std::runtime_error("request accounting or observer failure");
    if (completion_) {
      const auto stats = completion_->stats();
      if (stats.pending || stats.callbackFailures ||
          stats.registered != stats.fired + stats.canceled ||
          stats.fired != resumeSucceeded_.load() + resumeFailed_.load() ||
          stats.rejected != registrationRejected_.load())
        throw std::runtime_error("asynchronous completion accounting failure");
    }
    write();
  }

private:
  size_t waveBase(size_t wave) const {
    size_t n = 1;
    for (size_t i = 0; i < wave; ++i)
      n += c_.widths[i] * c_.depths[i] + 1;
    return n;
  }
  size_t branchBase(size_t wave) const {
    return std::accumulate(c_.widths.begin(), c_.widths.begin() + wave,
                           size_t(0));
  }
  size_t taskId(size_t wave, size_t branch, size_t stage) const {
    return waveBase(wave) + stage * c_.widths[wave] + branch;
  }
  uint64_t scaledWork(uint64_t base, size_t id, size_t tag) const {
    if (c_.scaleScope == "request")
      tag = 1;
    auto random = mix(c_.seed ^ mix(id) ^ mix(tag));
    uint64_t percent =
        100 - c_.jitter + (c_.jitter ? random % (2 * c_.jitter + 1) : 0);
    return base * percent / 100 *
           ((c_.longEvery && random % c_.longEvery == 0) ? c_.longFactor : 1);
  }
  uint64_t estimate(size_t request) const {
    return c_.estimateMode == "request-scale"
               ? std::max<uint64_t>(1, scaledWork(c_.estimateUs, request, 1))
               : c_.estimateUs;
  }
  uint64_t syncBlockUs(size_t request, size_t tag) const {
    return mix(c_.seed ^ mix(request) ^ mix(tag) ^ 0xb10c51eeULL) % 100 <
                   c_.syncBlockPercent
               ? c_.syncBlockUs
               : 0;
  }
  bool asyncSelected(size_t request, size_t tag) const {
    return mix(c_.seed ^ mix(request) ^ mix(tag) ^ 0xa510c0deULL) % 100 <
           c_.asyncPercent;
  }
  template <class F>
  void guarded(const std::shared_ptr<RequestState> &state, size_t tag,
               F &&callback) {
    if (state->terminal.load(std::memory_order_acquire))
      return;
    uint64_t begin = c_.trace ? cpuNs() : 0;
    try {
      callback();
    } catch (const std::exception &error) {
      finish(state, "failed", error.what());
    } catch (...) {
      finish(state, "failed", "unknown callback exception");
    }
    if (c_.trace)
      tasks_[state->id * c_.taskSlots + tag].cpu = cpuNs() - begin;
  }
  void fork(const std::shared_ptr<RequestState> &state, size_t wave) {
    state->remaining.store(c_.widths[wave], std::memory_order_release);
    std::vector<folly::Func> callbacks;
    for (size_t b = 0; b < c_.widths[wave]; ++b)
      callbacks.emplace_back(
          [this, state, wave, b] { stage(state, wave, b, 0); });
    Pool::TaskOptions options;
    options.tag = waveBase(wave);
    options.sequentialTags = true;
    options.estimatedCpuUs = estimate(state->id);
    if (pool_->addBatch(std::move(callbacks), options) != c_.widths[wave])
      finish(state, "partial_batch_rejected");
  }
  void stage(const std::shared_ptr<RequestState> &state, size_t wave,
             size_t branch, size_t depth) {
    const auto tag = taskId(wave, branch, depth);
    guarded(state, tag, [&] {
      auto result = state->work.run(branchBase(wave) + branch, depth,
                                    scaledWork(c_.iterations, state->id, tag),
                                    scaledWork(c_.accesses, state->id, tag));
      if (c_.trace) {
        auto &row = tasks_[state->id * c_.taskSlots + tag];
        row.iterations = result.computeIterations;
        row.reads = result.readWords;
        row.writes = result.writtenWords;
        row.checksum = result.checksum;
      }
      // Controlled synchronous blocking counterexample. This deliberately
      // retains the executor callback/thread; it does not model asynchronous
      // RPC.
      const auto blockingUs = syncBlockUs(state->id, tag);
      if (blockingUs) {
        const auto blockStart = c_.trace ? ns() : 0;
        std::this_thread::sleep_for(std::chrono::microseconds(blockingUs));
        if (c_.trace) {
          auto &row = tasks_[state->id * c_.taskSlots + tag];
          row.syncBlockUs = blockingUs;
          row.syncBlockWallNs = ns() - blockStart;
        }
      }
      if (c_.asyncPercent && asyncSelected(state->id, tag))
        awaitCompletion(state, wave, branch, depth, tag);
      else
        advance(state, wave, branch, depth);
    });
  }
  void awaitCompletion(const std::shared_ptr<RequestState> &state, size_t wave,
                       size_t branch, size_t depth, size_t tag) {
    const auto issue = ns();
    auto due = issue + c_.asyncDelayUs * 1000;
    if (c_.asyncAlignUs) {
      const auto grid = c_.asyncAlignUs * 1000;
      due = origin_ + ((due - origin_ + grid - 1) / grid) * grid;
    }
    // Both callbacks are owned by one event and only the source thread invokes
    // one of them. Rejected registrations leave ownership with this caller.
    auto token =
        std::make_shared<Pool::Continuation>(pool_->captureContinuation());
    AsyncRow *row =
        c_.trace ? &asyncRows_[state->id * c_.nodes + tag] : nullptr;
    if (row) {
      row->issue = issue;
      row->due = due;
    }
    auto fire = [this, state, wave, branch, depth, tag, token, row] {
      if (row)
        row->fired = ns();
      bool resumed = false;
      try {
        Pool::TaskOptions options;
        options.tag = c_.nodes + tag;
        options.estimatedCpuUs = 1;
        resumed = token->resume(
            [this, state, wave, branch, depth, tag] {
              guarded(state, c_.nodes + tag,
                      [&] { advance(state, wave, branch, depth); });
            },
            options);
      } catch (...) {
        // resume consumes its token on submission failure. Cancellation also
        // covers allocation failure while constructing its callback argument.
        token->cancel();
      }
      if (resumed)
        resumeSucceeded_.fetch_add(1, std::memory_order_relaxed);
      else {
        resumeFailed_.fetch_add(1, std::memory_order_relaxed);
        finish(state, "async_resume_failed");
      }
      if (row) {
        row->terminal = ns();
        row->status = resumed ? "resumed" : "resume_failed";
      }
    };
    auto cancel = [this, state, token, row] {
      if (row)
        row->fired = ns();
      token->cancel();
      if (row) {
        row->terminal = ns();
        row->status = "canceled";
      }
      finish(state, "async_canceled");
    };
    bool accepted = false;
    try {
      accepted = completion_->schedule(
          state->id, Clock::time_point(std::chrono::nanoseconds(due)),
          std::move(fire), std::move(cancel));
    } catch (...) {
      token->cancel();
      // Allocation failure is a driver failure, not a capacity rejection.
      // No valid event row was registered; guarded() records request failure.
      if (row)
        *row = {};
      throw;
    }
    if (!accepted) {
      token->cancel();
      registrationRejected_.fetch_add(1, std::memory_order_relaxed);
      if (row) {
        row->terminal = ns();
        row->status = "register_rejected";
      }
      finish(state, "async_register_rejected");
    }
  }
  void advance(const std::shared_ptr<RequestState> &state, size_t wave,
               size_t branch, size_t depth) {
    if (depth + 1 < c_.depths[wave]) {
      Pool::TaskOptions options;
      options.tag = taskId(wave, branch, depth + 1);
      options.estimatedCpuUs = estimate(state->id);
      if (!pool_->add([this, state, wave, branch,
                       depth] { stage(state, wave, branch, depth + 1); },
                      options))
        finish(state, "child_rejected");
    } else if (state->remaining.fetch_sub(1, std::memory_order_acq_rel) == 1) {
      Pool::TaskOptions options;
      options.tag = waveBase(wave) + c_.widths[wave] * c_.depths[wave];
      options.estimatedCpuUs = 1;
      if (!pool_->add(
              [this, state, wave, tag = options.tag] {
                guarded(state, tag, [&] {
                  // Join reads every completed branch result. Later waves use
                  // separate branch buffers; the barrier is a real control
                  // dependency.
                  auto checksum = state->work.finalChecksum();
                  if (c_.trace)
                    tasks_[state->id * c_.taskSlots + tag].checksum = checksum;
                  if (wave + 1 < c_.widths.size())
                    fork(state, wave + 1);
                  else {
                    requests_[state->id].checksum = checksum;
                    finish(state, "completed");
                  }
                });
              },
              options))
        finish(state, "join_rejected");
    }
  }
  void finish(const std::shared_ptr<RequestState> &state, std::string status,
              std::string error = "") {
    if (state->terminal.exchange(true, std::memory_order_acq_rel))
      return;
    auto &row = requests_[state->id];
    row.finished = ns();
    row.status = std::move(status);
    row.error = std::move(error);
    if (completion_ && row.status != "completed")
      completion_->cancelRequest(state->id);
  }
  static double quantile(std::vector<double> v, double p) {
    if (v.empty())
      return 0;
    std::sort(v.begin(), v.end());
    return v[std::min(v.size() - 1, size_t(std::ceil(v.size() * p)) - 1)];
  }
  void write() {
    std::filesystem::create_directories(c_.output);
    folly::dynamic config = folly::dynamic::object;
    for (auto &[key, value] : c_.args)
      config[key] = value;
    folly::dynamic summary = folly::dynamic::object(
        "schema", c_.asyncPercent ? "affinity-v3-async" : "affinity-v2")(
        "options", config)("seed", c_.seed)("policy", c_.policy)(
        "continuation", c_.continuation)("affinity", c_.affinity)(
        "model", c_.model)("requests", c_.requests)("nodes_per_request",
                                                    c_.nodes)("qps", c_.qps)(
        "iterations",
        c_.iterations)("accesses", c_.accesses)("threads_per_pool", c_.threads)(
        "trace", c_.trace)("arrival_hash", std::to_string(arrivalHash_))(
        "peak_inflight", peak_.load())("duration_with_drain_s",
                                       double(end_ - origin_) / 1e9)(
        "page_query_enabled", c_.pageSample);
    summary["process_cpu_s"] = double(processCpu_) / 1e9;
    folly::dynamic asyncContract = folly::dynamic::object;
    if (completion_) {
      asyncContract = folly::dynamic::object("percent", c_.asyncPercent)(
          "delay_us", c_.asyncDelayUs)("align_us", c_.asyncAlignUs)(
          "capacity", c_.asyncCapacity)("selection", "splitmix64-v1");
      summary["async_contract"] = asyncContract;
      summary["task_slots_per_request"] = c_.taskSlots;
      const auto stats = completion_->stats();
      summary["async_source"] = folly::dynamic::object(
          "registered", stats.registered)("fired", stats.fired)(
          "canceled", stats.canceled)("rejected", stats.rejected)(
          "pending", stats.pending)("peak_pending", stats.peakPending)(
          "callback_failures", stats.callbackFailures)(
          "helper_cpu_s", double(stats.helperCpuNs) / 1e9)(
          "helper_cpu_valid", stats.helperCpuValid)("resume_succeeded",
                                                    resumeSucceeded_.load())(
          "resume_failed", resumeFailed_.load())("registration_rejected",
                                                 registrationRejected_.load());
    }
    summary["origin_ns"] = origin_;
    // Perf request profiles require the same clock as task wall timestamps.
    // Retain a bracket check instead of silently assuming every C++ platform's
    // steady_clock is CLOCK_MONOTONIC. This runs only during result export.
    uint64_t bestSpan = UINT64_MAX, beforeMono = 0, mono = 0, afterMono = 0;
    for (int i = 0; i < 8; ++i) {
      const auto before = ns();
      timespec t{};
      if (clock_gettime(CLOCK_MONOTONIC, &t))
        throw std::runtime_error("monotonic clock check failed");
      const auto after = ns();
      if (after - before < bestSpan) {
        bestSpan = after - before;
        beforeMono = before;
        afterMono = after;
        mono = uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
      }
    }
    summary["diagnostic_clock"] = folly::dynamic::object(
        "task_clock", "std::chrono::steady_clock")(
        "reference_clock", "CLOCK_MONOTONIC")(
        "steady_before_ns", beforeMono)("monotonic_ns", mono)(
        "steady_after_ns", afterMono)("bracket_ns", bestSpan)(
        "compatible", beforeMono <= mono && mono <= afterMono);
    summary["balance_at"] = c_.balanceAt;
    summary["max_inflight"] = c_.maxInflight;
    summary["memory_mb"] = c_.memoryMiB;
    summary["pool_count"] = c_.poolCount;
    summary["arrival"] = c_.arrival;
    summary["burst_size"] = c_.burstSize;
    summary["burst_spacing_us"] = c_.burstSpacingUs;
    summary["sync_block_percent"] = c_.syncBlockPercent;
    summary["sync_block_us"] = c_.syncBlockUs;
    summary["cpu_sampling"] = c_.cpuSampling;
    summary["cpu_sample_ms"] = c_.cpuSampleMs;
    summary["cpu_max_age_ms"] = c_.cpuMaxAgeMs;
    summary["load_scorer"] = pool_->loadScorerName();
    summary["load_score_units"] = pool_->loadScoreUnits();
    const auto samplerStats =
        sampler_ ? sampler_->stats() : CpuSampler::Stats{};
    summary["cpu_sampler"] = folly::dynamic::object(
        "scans", samplerStats.scans)("failed_groups",
                                     samplerStats.failedGroups)(
        "scan_cpu_s", double(samplerStats.totalScanCpuNs) / 1e9)(
        "scan_wall_s", double(samplerStats.totalScanWallNs) / 1e9)(
        "max_scan_wall_ms", double(samplerStats.maxScanWallNs) / 1e6)(
        "runqueue_accounting_enabled", samplerStats.runqueueAccountingEnabled);
    summary["widths"] = folly::dynamic::array;
    summary["depths"] = folly::dynamic::array;
    for (auto x : c_.widths)
      summary["widths"].push_back(x);
    for (auto x : c_.depths)
      summary["depths"].push_back(x);
    summary["groups"] = folly::dynamic::array;
    for (auto &g : c_.pool.groups) {
      folly::dynamic item = folly::dynamic::object("numa", g.numaNode)(
          "capacity", g.capacity)("cpus", folly::dynamic::array);
      for (int cpu : g.cpus)
        item["cpus"].push_back(cpu);
      summary["groups"].push_back(item);
    }
    folly::dynamic contract =
        folly::dynamic::object("seed", c_.seed)("requests", c_.requests)(
            "qps", c_.qps)("arrival", c_.arrival)("widths", summary["widths"])(
            "depths", summary["depths"])("model", c_.model)(
            "iterations", c_.iterations)("accesses", c_.accesses)(
            "jitter", c_.jitter)("long_every", c_.longEvery)(
            "long_factor", c_.longFactor)("shared_bytes", c_.work.sharedBytes)(
            "branch_bytes", c_.work.branchBytes);
    contract["work_scale_scope"] = c_.scaleScope;
    contract["sync_block_percent"] = c_.syncBlockPercent;
    contract["sync_block_us"] = c_.syncBlockUs;
    if (completion_)
      contract["async_contract"] = asyncContract;
    if (c_.arrival == "burst") {
      contract["burst_size"] = c_.burstSize;
      contract["burst_spacing_us"] = c_.burstSpacingUs;
    }
    summary["work_signature"] = folly::toJson(contract);
    summary["work_contract"] = contract;
    // This narrower contract establishes fixed computational work equality
    // across arrival/blocking counterexamples; it is not an arrival-pair key.
    auto fixedWorkContract = contract;
    for (auto key : {"arrival", "qps", "burst_size", "burst_spacing_us",
                     "sync_block_percent", "sync_block_us", "async_contract"})
      fixedWorkContract.erase(key);
    summary["fixed_work_signature"] = folly::toJson(fixedWorkContract);
    uint64_t plannedIterations = 0, plannedAccesses = 0, plannedBlockUs = 0;
    size_t plannedBlockedOperators = 0;
    size_t plannedResumes = 0;
    folly::dynamic plannedResumeCounts = folly::dynamic::array;
    for (size_t id = 0; id < c_.requests; ++id) {
      size_t requestResumes = 0;
      for (size_t wave = 0; wave < c_.widths.size(); ++wave)
        for (size_t depth = 0; depth < c_.depths[wave]; ++depth)
          for (size_t branch = 0; branch < c_.widths[wave]; ++branch) {
            auto tag = taskId(wave, branch, depth);
            plannedIterations += scaledWork(c_.iterations, id, tag);
            plannedAccesses += scaledWork(c_.accesses, id, tag);
            auto block = syncBlockUs(id, tag);
            plannedBlockUs += block;
            plannedBlockedOperators += block != 0;
            requestResumes += c_.asyncPercent && asyncSelected(id, tag);
          }
      plannedResumes += requestResumes;
      plannedResumeCounts.push_back(requestResumes);
    }
    if (completion_) {
      summary["planned_resumes"] = plannedResumes;
      summary["planned_resume_counts"] = std::move(plannedResumeCounts);
    }
    summary["planned_total_iterations"] = plannedIterations;
    summary["planned_total_accesses"] = plannedAccesses;
    summary["planned_iterations_per_request"] =
        double(plannedIterations) / c_.requests;
    summary["planned_accesses_per_request"] =
        double(plannedAccesses) / c_.requests;
    summary["planned_blocked_operators"] = plannedBlockedOperators;
    summary["planned_sync_block_us"] = plannedBlockUs;
    summary["routing"] = folly::dynamic::object("busy_high", c_.pool.busyHigh)(
        "return_low", c_.pool.returnLow)("min_gain", c_.pool.minGain)(
        "cross_numa_penalty", c_.pool.crossNumaPenalty)("cross_numa",
                                                        c_.pool.allowCrossNuma)(
        "max_hops", c_.pool.maxHops)("batch_chunk", c_.pool.batchChunk)(
        "max_outstanding", c_.pool.maxOutstanding)("estimate_us",
                                                   c_.estimateUs)(
        "home_policy",
        c_.pool.homePolicy == Pool::HomePolicy::RoundRobin ? "rr" : "hash");
    summary["routing"]["estimate_mode"] = c_.estimateMode;
    Work allocation(c_.work);
    summary["payload_bytes_per_request"] = allocation.allocatedBytes();
    summary["payload_budget_at_max_inflight"] =
        allocation.budgetBytes() * c_.maxInflight;
    summary["pages"] = folly::dynamic::object(
        "status", c_.pageSample ? pages_.status : "disabled")(
        "errno", pages_.errnoValue)("sampled", pages_.sampledPages)(
        "known", pages_.knownPages)("nodes", folly::dynamic::object);
    for (auto &[node, count] : pages_.nodeCounts)
      summary["pages"]["nodes"][std::to_string(node)] = count;
    std::vector<double> latencies, waits, services;
    size_t completed = 0, failed = 0, observed = 0, migrations = 0, cross = 0;
    uint64_t checksum = 0, totalCpu = 0;
    uint64_t observedBlockUs = 0, observedBlockWallNs = 0;
    size_t scoreFallbacks = 0;
    size_t scoreEvaluations = 0, cpuScoredWithoutFallback = 0;
    std::map<std::string, size_t> scoreFallbackReasons;
    std::ofstream req(c_.output + "/requests.csv");
    req << "request,scheduled_ns,submitted_ns,finished_ns,status,concurrent,"
           "checksum\n";
    for (size_t id = 0; id < requests_.size(); ++id) {
      auto &row = requests_[id];
      req << id << ',' << row.scheduled << ',' << row.submitted << ','
          << row.finished << ',' << row.status << ',' << row.concurrent << ','
          << row.checksum << '\n';
      if (row.status == "completed") {
        ++completed;
        latencies.push_back(double(row.finished - row.scheduled) / 1e6);
        checksum = mix(checksum ^ row.checksum);
      } else {
        ++failed;
        if (!row.error.empty())
          std::cerr << "request " << id << ": " << row.error << '\n';
      }
    }
    if (c_.trace) {
      std::ofstream trace(c_.output + "/tasks.csv");
      trace << "request,tag,kind,home,source,chosen,hops,reason,migrated,cross_"
               "numa,queue_before,outstanding_before,preferred_score,chosen_"
               "score,submitted_ns,start_ns,finish_ns,cpu_ns,iterations,read_"
               "words,write_words,checksum,sync_block_target_us,sync_block_"
               "wall_ns,"
               "callback_inflight_before,sampled_cpu_cores,sampled_runqueue_"
               "wait_cores,"
               "sampled_runnable,sampled_at_ns,sample_window_ns,cpu_valid,"
               "runqueue_valid,"
               "runnable_valid,score_evaluated,score_fallback,score_fallback_"
               "reason,worker_tid";
      if (completion_)
        trace << ",logical_tag";
      trace << '\n';
      for (size_t i = 0; i < tasks_.size(); ++i) {
        auto &row = tasks_[i];
        if (!row.observed)
          continue;
        auto &o = row.observation;
        ++observed;
        migrations += o.migrated;
        cross += o.crossNuma;
        totalCpu += row.cpu;
        observedBlockUs += row.syncBlockUs;
        observedBlockWallNs += row.syncBlockWallNs;
        scoreEvaluations += o.scoreEvaluated;
        cpuScoredWithoutFallback +=
            c_.policy == "cpu" && o.scoreEvaluated && !o.scoreFallback;
        if (o.scoreFallback) {
          ++scoreFallbacks;
          ++scoreFallbackReasons[executor_demo::loadFallbackName(
              o.scoreFallbackReason)];
        }
        waits.push_back(double(o.startNs - o.submittedNs) / 1e6);
        services.push_back(double(o.finishNs - o.startNs) / 1e6);
        bool isJoin = false;
        for (size_t w = 0; w < c_.widths.size(); ++w)
          isJoin |= o.tag == waveBase(w) + c_.widths[w] * c_.depths[w];
        const char *kind = o.tag >= c_.nodes ? "rpc_resume"
                           : o.tag == 0      ? "init"
                           : isJoin          ? "join"
                                             : "operator";
        trace << o.color << ',' << o.tag << ',' << kind << ',' << o.home << ','
              << o.source << ',' << o.chosen << ',' << o.hops << ','
              << int(o.reason) << ',' << o.migrated << ',' << o.crossNuma << ','
              << o.queueBefore << ',' << o.outstandingBefore << ','
              << o.preferredScore << ',' << o.chosenScore << ','
              << o.submittedNs << ',' << o.startNs << ',' << o.finishNs << ','
              << row.cpu << ',' << row.iterations << ',' << row.reads << ','
              << row.writes << ',' << row.checksum << ',' << row.syncBlockUs
              << ',' << row.syncBlockWallNs << ',' << o.callbackInflightBefore
              << ',' << o.sampledCpuCores << ',' << o.sampledRunqueueWaitCores
              << ',' << o.sampledRunnable << ',' << o.sampledAtNs << ','
              << o.sampleWindowNs << ',' << o.cpuValid << ',' << o.runqueueValid
              << ',' << o.runnableValid << ',' << o.scoreEvaluated << ','
              << o.scoreFallback << ','
              << executor_demo::loadFallbackName(o.scoreFallbackReason) << ','
              << row.workerTid;
        if (completion_)
          trace << ',' << (o.tag >= c_.nodes ? o.tag - c_.nodes : o.tag);
        trace << '\n';
      }
      trace.flush();
      if (!trace)
        throw std::runtime_error("task trace write failed");
      if (completion_) {
        std::ofstream events(c_.output + "/async.csv");
        events << "request,logical_tag,issue_ns,due_ns,fired_ns,terminal_ns,"
                  "status\n";
        for (size_t i = 0; i < asyncRows_.size(); ++i) {
          const auto &row = asyncRows_[i];
          if (row.status)
            events << i / c_.nodes << ',' << i % c_.nodes << ',' << row.issue
                   << ',' << row.due << ',' << row.fired << ',' << row.terminal
                   << ',' << row.status << '\n';
        }
        events.flush();
        if (!events)
          throw std::runtime_error("async trace write failed");
      }
    }
    summary["completed"] = completed;
    summary["failed_or_rejected"] = failed;
    summary["observed_tasks"] = observed;
    summary["checksum"] = std::to_string(checksum);
    summary["migrations"] = migrations;
    summary["cross_numa"] = cross;
    summary["request_mean_ms"] =
        latencies.empty()
            ? 0
            : std::accumulate(latencies.begin(), latencies.end(), 0.0) /
                  latencies.size();
    summary["request_p50_ms"] = quantile(latencies, .5);
    summary["request_p95_ms"] = quantile(latencies, .95);
    summary["request_p99_ms"] = quantile(latencies, .99);
    summary["task_wait_p99_ms"] = quantile(waits, .99);
    summary["task_wall_p99_ms"] = quantile(services, .99);
    summary["observed_task_cpu_s"] = double(totalCpu) / 1e9;
    summary["observed_sync_block_target_us"] = observedBlockUs;
    summary["observed_sync_block_wall_s"] = double(observedBlockWallNs) / 1e9;
    summary["score_fallbacks"] = scoreFallbacks;
    summary["score_evaluations"] = scoreEvaluations;
    summary["cpu_scored_without_fallback"] = cpuScoredWithoutFallback;
    summary["score_fallback_reasons"] = folly::dynamic::object;
    for (const auto &[reason, count] : scoreFallbackReasons)
      summary["score_fallback_reasons"][reason] = count;
    std::ofstream out(c_.output + "/summary.json");
    out << folly::toPrettyJson(summary) << '\n';
    req.flush();
    out.flush();
    if (!out || !req)
      throw std::runtime_error("result write failed");
    std::cout << folly::toJson(summary) << '\n';
    if (failed)
      throw std::runtime_error("run contains rejected/failed requests; do not "
                               "use for performance claims");
  }
  Config c_;
  std::vector<RequestRow> requests_;
  std::vector<TaskRow> tasks_;
  std::vector<AsyncRow> asyncRows_;
  std::atomic<uint64_t> resumeSucceeded_{0}, resumeFailed_{0},
      registrationRejected_{0};
  std::atomic<size_t> active_{0}, peak_{0};
  Work::PageSample pages_;
  uint64_t origin_ = 0, end_ = 0, arrivalHash_ = 0, processStart_ = 0,
           processCpu_ = 0;
  std::shared_ptr<CpuSampler> sampler_;
  // Destroy/drain the executor before the state used by outstanding callbacks.
  std::unique_ptr<Pool> pool_;
  std::unique_ptr<CompletionSource> completion_;
};
int main(int argc, char **argv) {
  try {
    Experiment(parse(argc, argv)).run();
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "affinity_demo: " << error.what() << '\n';
    return 2;
  }
}

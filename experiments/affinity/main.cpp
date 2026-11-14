#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
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

using Pool = executor_demo::RequestExecutor;
using Work = executor_demo::LocalityWorkload;
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
  uint64_t seed = 1, iterations = 10000, accesses = 1000, estimateUs = 1000;
  double qps = 12;
  size_t jitter = 0, longEvery = 0, longFactor = 8;
  std::vector<size_t> widths{8, 4}, depths{2, 2};
  std::string model = "branch-rw", policy = "home", continuation = "parent",
              arrival = "regular", affinity = "cluster",
              output = "results/affinity-smoke", balanceAt = "all",
              scaleScope = "task", estimateMode = "constant";
  bool trace = true, pageSample = false;
  Work::Config work;
  Pool::Config pool;
  std::vector<std::vector<int>> partitions;
  size_t nodes = 0, branches = 0;
};
static Config parse(int argc, char **argv) {
  Config c;
  std::set<std::string> keys = {"cpus",
                                "affinity",
                                "threads",
                                "capacity-scale",
                                "requests",
                                "max-inflight",
                                "qps",
                                "arrival",
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
  c.nodes = 1;
  for (size_t i = 0; i < c.widths.size(); ++i) {
    if (!c.widths[i] || c.widths[i] > 4000 || !c.depths[i] ||
        c.depths[i] > 1000)
      throw std::invalid_argument("invalid graph dimensions");
    c.branches += c.widths[i];
    c.nodes += c.widths[i] * c.depths[i] + 1;
  }
  if (c.nodes * c.requests > 2000000 ||
      c.iterations > 1000000000000ULL / c.longFactor ||
      c.accesses > 1000000000000ULL / c.longFactor)
    throw std::invalid_argument("work/trace size exceeds experiment bound");
  if (c.arrival != "regular" && c.arrival != "poisson")
    throw std::invalid_argument("arrival must be regular or poisson");
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
  for (size_t i = 0; i < c.partitions.size(); ++i) {
    executor_demo::BusinessExecutor::GroupConfig g;
    g.threads = c.threads;
    g.numaNode = nodes[i];
    g.capacity = std::min<double>(c.threads, c.partitions[i].size() * scale);
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
  bool observed = false;
};
struct RequestRow {
  uint64_t scheduled = 0, submitted = 0, finished = 0, checksum = 0;
  size_t concurrent = 0;
  std::string status = "not_submitted", error;
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
        tasks_(c_.trace ? c_.requests * c_.nodes : 0) {
    if (c_.trace)
      c_.pool.observer = [this](const Pool::Observation &observation) {
        if (observation.color >= c_.requests || observation.tag >= c_.nodes)
          throw std::logic_error("trace index");
        auto &row = tasks_[observation.color * c_.nodes + observation.tag];
        row.observation = observation;
        row.observed = true;
      };
    pool_ = std::make_unique<Pool>(c_.pool);
  }
  void run() {
    processStart_ = processCpuNs();
    std::mt19937_64 rng(c_.seed);
    std::exponential_distribution<double> gap(c_.qps);
    origin_ = ns() + 10000000;
    uint64_t offset = 0;
    for (size_t id = 0; id < c_.requests; ++id) {
      if (id)
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
    end_ = ns();
    processCpu_ = processCpuNs() - processStart_;
    if (active_ != 0 || pool_->observerFailures() != 0)
      throw std::runtime_error("request accounting or observer failure");
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
      tasks_[state->id * c_.nodes + tag].cpu = cpuNs() - begin;
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
        auto &row = tasks_[state->id * c_.nodes + tag];
        row.iterations = result.computeIterations;
        row.reads = result.readWords;
        row.writes = result.writtenWords;
        row.checksum = result.checksum;
      }
      if (depth + 1 < c_.depths[wave]) {
        Pool::TaskOptions options;
        options.tag = taskId(wave, branch, depth + 1);
        options.estimatedCpuUs = estimate(state->id);
        if (!pool_->add([this, state, wave, branch,
                         depth] { stage(state, wave, branch, depth + 1); },
                        options))
          finish(state, "child_rejected");
      } else if (state->remaining.fetch_sub(1, std::memory_order_acq_rel) ==
                 1) {
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
                      tasks_[state->id * c_.nodes + tag].checksum = checksum;
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
    });
  }
  void finish(const std::shared_ptr<RequestState> &state, std::string status,
              std::string error = "") {
    if (state->terminal.exchange(true, std::memory_order_acq_rel))
      return;
    auto &row = requests_[state->id];
    row.finished = ns();
    row.status = std::move(status);
    row.error = std::move(error);
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
    folly::dynamic summary = folly::dynamic::object("schema", "affinity-v2")(
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
    summary["origin_ns"] = origin_;
    summary["balance_at"] = c_.balanceAt;
    summary["max_inflight"] = c_.maxInflight;
    summary["memory_mb"] = c_.memoryMiB;
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
    summary["work_signature"] = folly::toJson(contract);
    summary["work_contract"] = contract;
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
               "words,write_words,checksum\n";
      for (size_t i = 0; i < tasks_.size(); ++i) {
        auto &row = tasks_[i];
        if (!row.observed)
          continue;
        auto &o = row.observation;
        ++observed;
        migrations += o.migrated;
        cross += o.crossNuma;
        totalCpu += row.cpu;
        waits.push_back(double(o.startNs - o.submittedNs) / 1e6);
        services.push_back(double(o.finishNs - o.startNs) / 1e6);
        bool isJoin = false;
        for (size_t w = 0; w < c_.widths.size(); ++w)
          isJoin |= o.tag == waveBase(w) + c_.widths[w] * c_.depths[w];
        const char *kind = o.tag == 0 ? "init" : isJoin ? "join" : "operator";
        trace << o.color << ',' << o.tag << ',' << kind << ',' << o.home << ','
              << o.source << ',' << o.chosen << ',' << o.hops << ','
              << int(o.reason) << ',' << o.migrated << ',' << o.crossNuma << ','
              << o.queueBefore << ',' << o.outstandingBefore << ','
              << o.preferredScore << ',' << o.chosenScore << ','
              << o.submittedNs << ',' << o.startNs << ',' << o.finishNs << ','
              << row.cpu << ',' << row.iterations << ',' << row.reads << ','
              << row.writes << ',' << row.checksum << '\n';
      }
      trace.flush();
      if (!trace)
        throw std::runtime_error("task trace write failed");
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
  std::atomic<size_t> active_{0}, peak_{0};
  Work::PageSample pages_;
  uint64_t origin_ = 0, end_ = 0, arrivalHash_ = 0, processStart_ = 0,
           processCpu_ = 0;
  // Destroy/drain the executor before the state used by outstanding callbacks.
  std::unique_ptr<Pool> pool_;
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

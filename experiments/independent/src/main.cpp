// Stage 3: original Folly executor, independent jobs and business-side admission routing.
#include "LoadRouting.h"
#include <folly/executors/CPUThreadPoolExecutor.h>
#include <folly/executors/thread_factory/ThreadFactory.h>
#include <folly/init/Init.h>
#include <folly/ScopeGuard.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <limits>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <numeric>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <cerrno>
#include <cstring>
#include <sched.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

using Pool = folly::CPUThreadPoolExecutor;
constexpr auto relaxed = std::memory_order_relaxed;
uint64_t ns(clockid_t clock = CLOCK_MONOTONIC) {
  timespec t{};
  if (clock_gettime(clock, &t)) throw std::runtime_error("clock_gettime failed");
  return uint64_t(t.tv_sec) * 1000000000ULL + t.tv_nsec;
}
void until(uint64_t n) {
  timespec t{time_t(n / 1000000000ULL), long(n % 1000000000ULL)};
  int rc;
  do { rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &t, nullptr); } while (rc == EINTR);
  if (rc) throw std::runtime_error("clock_nanosleep failed");
}
uint64_t integer(const std::string& s) {
  if (s.empty() || s[0] == '-') throw std::invalid_argument("expected unsigned integer");
  size_t n; auto v = std::stoull(s, &n);
  if (n != s.size()) throw std::invalid_argument("invalid integer");
  return v;
}
double number(const std::string& s) {
  size_t n; auto v = std::stod(s, &n);
  if (n != s.size() || !std::isfinite(v)) throw std::invalid_argument("expected finite number");
  return v;
}
std::vector<std::string> split(const std::string& s, char c) {
  std::stringstream stream(s); std::string x; std::vector<std::string> out;
  while (std::getline(stream, x, c)) out.push_back(x);
  return out;
}
std::vector<int> cpuList(const std::string& s) {
  std::set<int> out;
  for (const auto& x : split(s, ',')) {
    auto r = split(x, '-');
    if (r.empty() || r.size() > 2) throw std::invalid_argument("invalid CPU range");
    auto a = integer(r[0]), b = r.size() == 2 ? integer(r[1]) : a;
    if (b < a || b >= CPU_SETSIZE) throw std::invalid_argument("CPU outside CPU_SETSIZE");
    for (auto i = a; i <= b; ++i) out.insert(int(i));
  }
  if (out.empty()) throw std::invalid_argument("empty CPU mask");
  return {out.begin(), out.end()};
}
std::vector<int> allowed() {
  cpu_set_t set; CPU_ZERO(&set);
  if (sched_getaffinity(0, sizeof(set), &set)) throw std::runtime_error("sched_getaffinity failed");
  std::vector<int> v;
  for (int c = 0; c < CPU_SETSIZE; ++c) if (CPU_ISSET(c, &set)) v.push_back(c);
  return v;
}
std::string js(const std::string& s) {
  std::string o = "\"";
  for (auto c : s) { if (c == '"' || c == '\\') o += '\\'; if (c == '\n') o += "\\n"; else o += c; }
  return o + '"';
}
template<class T> std::string array(const std::vector<T>& xs) {
  std::ostringstream o; o << std::setprecision(std::numeric_limits<double>::max_digits10) << '[';
  for (size_t i = 0; i < xs.size(); ++i) { if (i) o << ','; o << xs[i]; }
  o << ']'; return o.str();
}
struct Opt {
  std::string output = "independent-run", cpus, numa, affinity = "none", deployment = "dev";
  std::string instrumentation = "full", arrival = "regular", workMode = "cpu";
  std::string meanNormalization = "none", burstPlacement = "local";
  std::string workload = "flat";
  std::string routing = "home", loadMetric = "outstanding", capacities;
  std::string loadObservation = "off", requestedLoadObservation = "off";
  double loadThreshold = 1, minImprovement = 0, acceptThreshold = -1;
  double burstWindowMs = 20, steadyWindowMs = 400;
  size_t steadyTasks = 80;
  uint64_t progressUs = 5000, failSubmitTask = std::numeric_limits<uint64_t>::max();
  size_t pools = 2, threads = 2, burstMin = 1, burstMax = 1, maxOutstanding = 10000, maxPlanned = 1000000;
  uint64_t cpuUs = 1000, estimateUs = 0, seed = 42, arrivals = 0, sampleMs = 100, iterationsPerUs = 64;
  size_t maxSamples=1000000; uint64_t failTask=std::numeric_limits<uint64_t>::max();
  double rate = 100, duration = 5, jitter = .5, tailProb = .02, tailFactor = 8, drain = 30, estimateJitter = 0;
};
const char* help = R"(Independent original Folly CPUThreadPoolExecutor experiment.
All options take one value. No AffinityExecutor, graph or RPC; routing happens before add().
--output DIR                       New result directory
--deployment dev|target            target requires 6 verified explicit CPU groups/NUMA labels
--pools N --threads N               Pools and fixed threads per pool (2,2)
--cpus '0-7;8-15;...' --numa '0,0,...'  Explicit cluster CPU groups and real NUMA node IDs
--affinity none|numa|cluster        none inherits current cpuset, numa unions same-node groups
--arrival-rate N --duration S       Open-loop arrivals/second and offered duration (100,5)
--arrival regular|poisson --arrivals N  N=0 means duration determines count
--burst-min N --burst-max N         Independent jobs per arrival, same round-robin pool (1,1)
--workload flat|request-wave       flat simultaneous burst; wave initial + steady releases (flat)
--burst-window-ms F                wave initial jobs at uniform offsets in [0,F) ms (20)
--steady-tasks N --steady-window-ms F  wave follow-up jobs in [initial window,initial+F) (80,400)
                                  duration limits request arrivals, not their follow-up releases
--burst-placement local|spread     One RR pool per arrival or RR pool per task (local)
--cpu-us N --jitter F               Uniform CPU budget around N, fraction F (1000,.5)
--long-prob F --long-factor F       Mixture tail probability and multiplier (.02,8)
--mean-normalization none|exact    exact rescales finite workload to mean cpu-us (none)
--work-mode cpu|iterations --iterations-per-us N  CPU target or fixed loop work (cpu,64)
--estimate-us N                    Work estimate for full/extra observation, 0=synthetic target
--estimate-jitter F                Independent multiplicative U(1-F,1+F) estimate error (0)
--instrumentation none|queue|counters|full  Increasing observation levels (full)
--routing home|threshold|spread    Keep home, threshold spill, or static task RR (home)
--load-metric queue|outstanding|work|remaining  Threshold routing metric (outstanding)
--load-threshold F --min-improvement F  Strict home threshold and required improvement (1,0)
--accept-threshold F               Candidate pre-add score must be strictly below F; -1 disables
--capacities 'N,N,...'             Per-pool accounting divisors; default min(threads,mask CPUs)
--load-observation off|minimal|basic|progress  Extra load snapshots/counters (off)
                                  minimal observes only load-metric; threshold off becomes basic
                                  remaining enables progress unless explicit minimal
--progress-us N                   CPU-us interval for remaining-work publication (5000)
--fail-submit-task N              Test-only: fail add path after reservation for task N
--sample-ms N --seed N             Periodic snapshots and deterministic workload (100,42)
--max-samples N                    Bound periodic sample rows; report truncation (1000000)
--fail-task N                      Test-only: inject callback failure for task ID N
--max-outstanding N                Nonblocking task admission limit across pools (10000)
--max-planned-tasks N              Bound preallocated telemetry memory (1000000)
--drain-seconds S                  Cancel after max(duration,last task release) + S (30)
--help
Times use CLOCK_MONOTONIC; work targets use CLOCK_THREAD_CPUTIME_ID, never worker sleeps.
All modes retain admission/cancellation atomics and basic latency timestamps: none is not zero overhead.
)";
Opt parse(int argc, char** argv) {
  Opt o;
  for (int i = 1; i < argc; ++i) {
    std::string k = argv[i]; if (k == "--help") { std::cout << help; std::exit(0); }
    if (++i == argc) throw std::invalid_argument("missing value for " + k);
    std::string v = argv[i];
    if (k == "--output") o.output = v;
    else if (k == "--deployment") o.deployment = v;
    else if (k == "--cpus") o.cpus = v;
    else if (k == "--numa") o.numa = v;
    else if (k == "--affinity") o.affinity = v;
    else if (k == "--instrumentation") o.instrumentation = v;
    else if (k == "--arrival") o.arrival = v;
    else if (k == "--work-mode") o.workMode = v;
    else if (k == "--mean-normalization") o.meanNormalization = v;
    else if (k == "--burst-placement") o.burstPlacement = v;
    else if (k == "--workload") o.workload = v;
    else if (k == "--burst-window-ms") o.burstWindowMs = number(v);
    else if (k == "--steady-window-ms") o.steadyWindowMs = number(v);
    else if (k == "--steady-tasks") o.steadyTasks = integer(v);
    else if (k == "--routing") o.routing = v;
    else if (k == "--load-metric") o.loadMetric = v;
    else if (k == "--load-observation") o.requestedLoadObservation = v;
    else if (k == "--capacities") o.capacities = v;
    else if (k == "--load-threshold") o.loadThreshold = number(v);
    else if (k == "--accept-threshold") o.acceptThreshold = number(v);
    else if (k == "--min-improvement") o.minImprovement = number(v);
    else if (k == "--progress-us") o.progressUs = integer(v);
    else if (k == "--fail-submit-task") o.failSubmitTask = integer(v);
    else if (k == "--pools") o.pools = integer(v);
    else if (k == "--threads") o.threads = integer(v);
    else if (k == "--burst-min") o.burstMin = integer(v);
    else if (k == "--burst-max") o.burstMax = integer(v);
    else if (k == "--max-outstanding") o.maxOutstanding = integer(v);
    else if (k == "--max-planned-tasks") o.maxPlanned = integer(v);
    else if (k == "--cpu-us") o.cpuUs = integer(v);
    else if (k == "--estimate-us") o.estimateUs = integer(v);
    else if (k == "--seed") o.seed = integer(v);
    else if (k == "--arrivals") o.arrivals = integer(v);
    else if (k == "--sample-ms") o.sampleMs = integer(v);
    else if (k == "--max-samples") o.maxSamples = integer(v);
    else if (k == "--fail-task") o.failTask = integer(v);
    else if (k == "--iterations-per-us") o.iterationsPerUs = integer(v);
    else if (k == "--arrival-rate") o.rate = number(v);
    else if (k == "--duration") o.duration = number(v);
    else if (k == "--jitter") o.jitter = number(v);
    else if (k == "--long-prob") o.tailProb = number(v);
    else if (k == "--long-factor") o.tailFactor = number(v);
    else if (k == "--drain-seconds") o.drain = number(v);
    else if (k == "--estimate-jitter") o.estimateJitter = number(v);
    else throw std::invalid_argument("unknown option " + k);
  }
  auto choice = [](const std::string& x, std::initializer_list<const char*> vals) {
    for (auto v : vals) if (x == v) return true;
    return false;
  };
  if (!choice(o.deployment,{"dev","target"}) || !choice(o.affinity,{"none","numa","cluster"}) ||
      !choice(o.instrumentation,{"none","queue","counters","full"}) || !choice(o.arrival,{"regular","poisson"}) ||
      !choice(o.workMode,{"cpu","iterations"}) || !choice(o.meanNormalization,{"none","exact"}) ||
      !choice(o.burstPlacement,{"local","spread"}) ||
      !choice(o.workload,{"flat","request-wave"}) ||
      !choice(o.routing,{"home","threshold","spread"}) ||
      !choice(o.loadMetric,{"queue","outstanding","work","remaining"}) ||
      !choice(o.requestedLoadObservation,{"off","minimal","basic","progress"})) throw std::invalid_argument("invalid enum option");
  if (!o.pools || o.pools > 1024 || !o.threads || o.threads > 10000 || !o.burstMin || o.burstMax < o.burstMin ||
      !o.maxOutstanding || !o.maxPlanned || o.maxPlanned > 10000000 || !o.cpuUs || o.cpuUs > 3600000000ULL ||
      !o.iterationsPerUs || o.iterationsPerUs > 1000000 || o.rate <= 0 || o.rate > 1e7 || o.duration <= 0 ||
      o.duration > 86400 || o.drain <= 0 || o.drain > 86400 || o.jitter < 0 || o.jitter >= 1 ||
      o.tailProb < 0 || o.tailProb > 1 || o.tailFactor < 1 || o.tailFactor > 1000 || !o.sampleMs ||
      o.sampleMs > 60000 || o.maxSamples > 10000000 || o.estimateUs > 3600000000000ULL)
    throw std::invalid_argument("option outside supported bounds");
  if (o.loadThreshold < 0 || o.minImprovement < 0 || !o.progressUs || o.progressUs > 3600000000ULL ||
      (o.acceptThreshold < 0 && o.acceptThreshold != -1))
    throw std::invalid_argument("invalid load threshold, improvement or progress interval");
  if (o.burstWindowMs < 0 || o.burstWindowMs > 86400000 ||
      o.steadyWindowMs <= 0 || o.steadyWindowMs > 86400000 ||
      (o.workload == "request-wave" && o.steadyTasks > o.maxPlanned))
    throw std::invalid_argument("invalid request-wave window or steady task count");
  if (o.workload == "request-wave" && o.burstPlacement != "local")
    throw std::invalid_argument("request-wave requires burst-placement=local for fixed request home; use routing=spread for static spreading");
  o.loadObservation = o.requestedLoadObservation;
  if (o.routing == "threshold" && o.loadObservation == "off") o.loadObservation = "basic";
  if (o.loadMetric == "remaining" && o.loadObservation != "minimal") o.loadObservation = "progress";
  if(o.estimateJitter<0 || o.estimateJitter>=1) throw std::invalid_argument("estimate-jitter must be in [0,1)");
  if (o.deployment == "target" && (o.pools != 6 || o.cpus.empty() || o.numa.empty()))
    throw std::invalid_argument("target requires pools=6 and explicit cpus/numa");
  if (o.affinity != "none" && o.cpus.empty()) throw std::invalid_argument("affinity requires explicit CPU groups");
  if (o.affinity == "numa" && o.numa.empty()) throw std::invalid_argument("numa affinity requires real node labels");
  return o;
}
struct Layout { std::vector<int> inherited, nodes, extraAllowed; bool sameCpuUnion=true; std::vector<std::vector<int>> groups, masks; };
Layout layout(const Opt& o) {
  Layout l; l.inherited = allowed();
  if (!o.cpus.empty()) for (auto s : split(o.cpus, ';')) l.groups.push_back(cpuList(s));
  else l.groups.resize(o.pools, l.inherited);
  if (l.groups.size() != o.pools) throw std::invalid_argument("CPU group count differs from pools");
  std::set<int> assigned;
  for (const auto& g : l.groups) for (int c : g) {
    if (!std::binary_search(l.inherited.begin(), l.inherited.end(), c)) throw std::invalid_argument("CPU not allowed");
    if (!o.cpus.empty() && !assigned.insert(c).second) throw std::invalid_argument("explicit cluster groups must be disjoint");
  }
  std::set<int> represented;
  for(const auto& g:l.groups)represented.insert(g.begin(),g.end());
  for(int c:l.inherited)if(!represented.count(c))l.extraAllowed.push_back(c);
  l.sameCpuUnion=l.extraAllowed.empty();
  if(o.deployment=="target"&&!l.sameCpuUnion)
    throw std::invalid_argument("target CPU groups must cover the inherited allowed mask; launch with taskset restricted to the union of the six groups");
  if (!o.numa.empty()) {
    for (auto s : split(o.numa, ',')) {
      auto node=integer(s);
      if(node>uint64_t(std::numeric_limits<int>::max())) throw std::invalid_argument("NUMA node ID overflow");
      l.nodes.push_back(int(node));
    }
    if (l.nodes.size() != o.pools) throw std::invalid_argument("NUMA label count differs from pools");
    for (size_t i = 0; i < o.pools; ++i) for (int c : l.groups[i]) {
      auto p = "/sys/devices/system/node/node" + std::to_string(l.nodes[i]) + "/cpu" + std::to_string(c);
      if (!std::filesystem::exists(p)) throw std::invalid_argument("CPU/NUMA label mismatch: " + p);
    }
  } else l.nodes.resize(o.pools, -1);
  l.masks = l.groups;
  if (o.affinity == "none") l.masks.assign(o.pools, l.inherited);
  if (o.affinity == "numa") for (size_t i = 0; i < o.pools; ++i) {
    std::set<int> u;
    for (size_t j = 0; j < o.pools; ++j) if (l.nodes[i] == l.nodes[j]) u.insert(l.groups[j].begin(), l.groups[j].end());
    l.masks[i] = {u.begin(), u.end()};
  }
  return l;
}
class Factory : public folly::ThreadFactory {
 public:
  Factory(std::vector<int> mask, std::string name): mask_(std::move(mask)), name_(std::move(name)) {}
  std::thread newThread(folly::Func&& f) override {
    return std::thread([this, fn=std::move(f)]() mutable {
      cpu_set_t set; CPU_ZERO(&set); for (int c : mask_) CPU_SET(c,&set);
      int e = pthread_setaffinity_np(pthread_self(),sizeof(set),&set);
      if (e) error.store(e, relaxed);
      { std::lock_guard lock(mu); tids.push_back(int(syscall(SYS_gettid))); }
      fn();
    });
  }
  const std::string& getNamePrefix() const override { return name_; }
  std::atomic<int> error{0}; std::mutex mu; std::vector<int> tids;
 private:
  std::vector<int> mask_; std::string name_;
};
struct alignas(64) Counters {
  std::atomic<uint64_t> pending{0}, inflight{0}, work{0}, outstanding{0}, remaining{0};
};
struct LoadSnapshot {
  int64_t queue=-1, outstanding=-1, work=-1, remaining=-1, inflight=-1;
};
// One record per task. Main owns pre-submit fields, exactly one callback owns the rest.
// No reader inspects callback fields until all workers have been joined.
struct Task {
  size_t id=0, burst=0, pool=0, homePool=0, burstIndex=0, burstRemaining=0;
  LoadSnapshot homeLoad, selectedLoad;
  double homeScore=-1, selectedScore=-1, capacity=0;
  int64_t routeNs=-1, routeCandidates=-1;
  bool spilled=false;
  bool sourceOverThreshold=false, noEligibleTarget=false, noImprovement=false;
  size_t eligibleCandidates=0;
  bool steady=false;
  uint64_t planned=0, requestPlanned=0, submit=0, start=0, end=0, target=0, estimate=0, cpu=0;
  int64_t queue=-1, pending=-1, inflight=-1, work=-1;
  int tid=-1;
  enum State { Planned, Rejected, Completed, Canceled, Deadline, Failed } state=Planned;
};
struct Burst { size_t first, count, pool; uint64_t planned, actual=0; };
struct Sample { uint64_t t; size_t pool; int64_t queue, pending, inflight, work, outstanding, remaining; double capacity; };
std::vector<Task> makeTasks(const Opt& o, std::vector<Burst>& bursts) {
  std::mt19937_64 workload(o.seed), arrival(o.seed ^ 0xabcde1234ULL), widths(o.seed ^ 0x491bda22ULL);
  std::uniform_real_distribution<double> unit(0,1);
  std::uniform_int_distribution<size_t> width(o.burstMin,o.burstMax);
  std::exponential_distribution<double> interval(o.rate);
  std::vector<Task> tasks;
  double t = o.arrival == "regular" ? 0 : interval(arrival);
  for (size_t b=0; t < o.duration && (!o.arrivals || b < o.arrivals); ++b) {
    size_t initial = width(widths);
    if (initial > o.maxPlanned || (o.workload == "request-wave" && o.steadyTasks > o.maxPlanned-initial))
      throw std::invalid_argument("planned task count exceeds max-planned-tasks");
    size_t k = initial + (o.workload == "request-wave" ? o.steadyTasks : 0);
    if (k > o.maxPlanned || tasks.size() > o.maxPlanned-k) throw std::invalid_argument("planned task count exceeds max-planned-tasks");
    auto when = uint64_t(t * 1e9); bursts.push_back({tasks.size(),k,b%o.pools,when});
    for (size_t j=0; j<k; ++j) {
      auto target = uint64_t(std::max(1., double(o.cpuUs) * (1 + o.jitter*(2*unit(workload)-1)) * (unit(workload)<o.tailProb ? o.tailFactor : 1)));
      Task r; r.id=tasks.size(); r.burst=b;
      r.pool=o.burstPlacement=="local" ? b%o.pools : r.id%o.pools;
      r.homePool=r.pool; r.burstIndex=j; r.burstRemaining=k-j-1;
      r.planned=when; r.requestPlanned=when;
      if (o.workload == "request-wave") {
        r.steady=j>=initial;
        // Deterministic offsets preserve an identical open-loop trace across policies.
        const double offsetMs=r.steady ? o.burstWindowMs+
          o.steadyWindowMs*double(j-initial)/double(o.steadyTasks) :
          o.burstWindowMs*double(j)/double(initial);
        r.planned+=uint64_t(offsetMs*1e6);
      }
      r.target=target; tasks.push_back(r);
    }
    t = o.arrival == "regular" ? double(b+1)/o.rate : t+interval(arrival);
  }
  // An opt-in finite-sample control: preserve a positive budget per task and
  // distribute the remainder proportionally, using cumulative rounding so the
  // integer total is exactly N * cpuUs without a sort or another O(N) buffer.
  // Raw/default workloads remain unchanged for compatibility with old evidence.
  if(o.meanNormalization=="exact" && !tasks.empty()) {
    long double weights=0;
    for(const auto& r:tasks)weights+=r.target-1;
    uint64_t extra=uint64_t(tasks.size())*(o.cpuUs-1), assigned=0;
    long double cumulative=0;
    for(size_t i=0;i<tasks.size();++i) {
      auto& r=tasks[i]; cumulative+=r.target-1;
      uint64_t next=i+1==tasks.size() ? extra :
        (weights ? uint64_t(std::round(cumulative/weights*extra)) : uint64_t(i+1)*(o.cpuUs-1));
      r.target=1+next-assigned; assigned=next;
    }
  }
  uint64_t totalEstimate=0;
  std::mt19937_64 estimates(o.seed ^ 0x71d3babeULL);
  for(auto& r:tasks) {
    if(r.target>std::numeric_limits<uint64_t>::max()/std::max<uint64_t>(1000,o.iterationsPerUs))
      throw std::invalid_argument("normalized task budget exceeds supported clock/work range");
    r.estimate=o.estimateUs ? o.estimateUs : r.target;
    if(o.estimateJitter)r.estimate=uint64_t(std::max(1.,double(r.estimate)*(1+o.estimateJitter*(2*unit(estimates)-1))));
    if(r.estimate>uint64_t(std::numeric_limits<int64_t>::max())-totalEstimate)
      throw std::invalid_argument("sum of work estimates exceeds supported range");
    totalEstimate+=r.estimate;
  }
  return tasks;
}
void work(Task& t, const Opt& o, const std::atomic<bool>& cancel, bool cpuRecord,
          std::atomic<uint64_t>* remainingCounter, uint64_t& unpublished) {
  const auto startCpu=ns(CLOCK_THREAD_CPUTIME_ID);
  uint64_t x=t.id+1, done=0, finishCpu=startCpu, nextProgress=o.progressUs;
  const uint64_t maxIterations=t.target*o.iterationsPerUs;
  while (!cancel.load(relaxed)) {
    for (int i=0;i<256;++i) { x ^= x<<13; x ^= x>>7; x ^= x<<17; }
    asm volatile("" : "+r"(x));
    done += 256;
    if (o.workMode == "cpu" || remainingCounter) {
      finishCpu=ns(CLOCK_THREAD_CPUTIME_ID);
      if (remainingCounter) {
        const uint64_t consumed=(finishCpu-startCpu)/1000;
        if (consumed >= nextProgress) {
          const uint64_t nextRemaining=t.estimate-std::min(t.estimate,consumed);
          // Each callback subtracts only its own unpublished estimate. Under-
          // estimates reach zero while work may still be running.
          const uint64_t decrement=unpublished-nextRemaining;
          if(decrement)remainingCounter->fetch_sub(decrement,relaxed);
          unpublished=nextRemaining;
          nextProgress=consumed+o.progressUs;
        }
      }
    }
    if (o.workMode == "cpu") {
      if (finishCpu-startCpu >= t.target*1000) break;
    } else if (done >= maxIterations) break;
  }
  if (cpuRecord) t.cpu=ns(CLOCK_THREAD_CPUTIME_ID)-startCpu;
  t.state=cancel.load(relaxed) ? Task::Canceled : Task::Completed;
}
double mean(const std::vector<double>& v) { double sum=0; for(auto x:v) sum+=x; return v.empty()?0:sum/v.size(); }
double percentile(std::vector<double> v, double p=.99) {
  if(v.empty()) return 0;
  std::sort(v.begin(),v.end()); return v[size_t(std::ceil(p*v.size()))-1];
}
std::string stat(const std::vector<double>& v, bool p99=false) {
  if(v.empty()) return "null";
  std::ostringstream s; s<<std::setprecision(12)<<(p99?percentile(v):mean(v)); return s.str();
}
const char* stateName(Task::State s) {
  switch(s) { case Task::Rejected:return "rejected"; case Task::Completed:return "completed";
    case Task::Canceled:return "canceled"; case Task::Deadline:return "deadline_rejected"; case Task::Failed:return "failed"; default:return "invalid"; }
}
int run(const Opt& o) {
  auto l=layout(o);
  std::vector<double> capacities;
  if (!o.capacities.empty()) {
    if (o.capacities.back()==',') throw std::invalid_argument("empty capacity");
    for (const auto& value:split(o.capacities,',')) {
      double capacity=number(value);
      if (!(capacity > 0) || capacity > 1e9 || !std::isfinite(double(std::numeric_limits<int64_t>::max())/capacity))
        throw std::invalid_argument("capacity must be positive, <= 1e9, and permit finite load scores");
      capacities.push_back(capacity);
    }
    if (capacities.size()!=o.pools) throw std::invalid_argument("capacity count differs from pools");
  } else for (const auto& mask:l.masks) capacities.push_back(double(std::min(o.threads,mask.size())));
  std::vector<Burst> bursts; auto tasks=makeTasks(o,bursts);
  if(tasks.empty()) throw std::invalid_argument("no planned tasks: increase duration/rate");
  // Storage remains request-contiguous for burst accounting; submission follows
  // the global ready-time order, so overlapping waves cannot block later requests.
  std::vector<size_t> releaseOrder(tasks.size());
  std::iota(releaseOrder.begin(),releaseOrder.end(),0);
  if(o.workload=="request-wave") std::stable_sort(releaseOrder.begin(),releaseOrder.end(),
    [&](size_t a,size_t b){return tasks[a].planned<tasks[b].planned;});
  const uint64_t lastRelease=tasks[releaseOrder.back()].planned;
  uint64_t totalTargetUs=0, initialTargetUs=0, steadyTargetUs=0;
  size_t initialTasks=0, steadyTasks=0;
  for(const auto& task:tasks) {
    if(task.target>std::numeric_limits<uint64_t>::max()-totalTargetUs)
      throw std::invalid_argument("total CPU target overflow");
    totalTargetUs+=task.target;
    if(task.steady){steadyTargetUs+=task.target;++steadyTasks;}
    else {initialTargetUs+=task.target;++initialTasks;}
  }
  if(std::filesystem::exists(o.output)) throw std::invalid_argument("output directory already exists");
  std::filesystem::create_directories(o.output);
  const auto path=[&](const char* f){return std::filesystem::path(o.output)/f;};
  const bool observe=o.loadObservation!="off", minimal=o.loadObservation=="minimal";
  const bool progress=o.loadObservation=="progress"||(minimal&&o.loadMetric=="remaining");
  const bool queue=o.instrumentation!="none", counts=o.instrumentation=="counters"||o.instrumentation=="full", full=o.instrumentation=="full";
  const bool observeNative=observe&&(!minimal||o.loadMetric=="queue");
  const bool maintainOutstanding=observe&&(!minimal||o.loadMetric=="outstanding");
  const bool observeWork=observe&&(!minimal||o.loadMetric=="work");
  const bool maintainWork=full||observeWork, observeQueue=queue||observeNative;
  std::vector<std::shared_ptr<Factory>> factories;
  std::vector<std::unique_ptr<Pool>> pools;
  std::vector<std::unique_ptr<Counters>> counters;
  for(size_t i=0;i<o.pools;++i) {
    factories.push_back(std::make_shared<Factory>(l.masks[i],"independent"+std::to_string(i)));
    counters.push_back(std::make_unique<Counters>());
    pools.push_back(std::make_unique<Pool>(std::make_pair(o.threads,o.threads),factories.back()));
    if(factories.back()->error.load(relaxed)) throw std::runtime_error("worker affinity setup failed");
  }
  std::ofstream workers(path("workers.csv")); workers<<"pool,tid,allowed_cpus\n";
  for(size_t i=0;i<o.pools;++i) {
    std::lock_guard lock(factories[i]->mu);
    for(int tid:factories[i]->tids) workers<<i<<','<<tid<<",\""<<array(l.masks[i])<<"\"\n";
  }
  workers.close();
  utsname host{}; uname(&host);
  const uint64_t epoch=ns()+20000000ULL,
    deadline=epoch+std::max(uint64_t(o.duration*1e9),lastRelease)+uint64_t(o.drain*1e9);
  std::ofstream metadata(path("metadata.json")); metadata<<std::setprecision(std::numeric_limits<double>::max_digits10);
  metadata<<"{\n\"schema\":1,\"stage\":3,\"executor\":\"original folly::CPUThreadPoolExecutor\",\"queue\":\"Folly default\",\n"
    <<"\"deployment\":"<<js(o.deployment)<<",\"hostname\":"<<js(host.nodename)<<",\"kernel\":"<<js(host.release)<<",\"machine\":"<<js(host.machine)
    <<",\"pid\":"<<getpid()<<",\"epoch_monotonic_ns\":"<<epoch
    <<",\"affinity\":"<<js(o.affinity)<<",\"instrumentation\":"<<js(o.instrumentation)
    <<",\"routing\":"<<js(o.routing)<<",\"load_metric\":"<<js(o.loadMetric)<<",\"load_threshold\":"<<o.loadThreshold
    <<",\"accept_threshold\":"<<o.acceptThreshold
    <<",\"capacities\":"<<array(capacities)<<",\"capacity_source\":"<<js(o.capacities.empty()?"min(threads,effective_mask_logical_cpus)":"explicit")
    <<",\"min_improvement\":"<<o.minImprovement<<",\"progress_us\":"<<o.progressUs
    <<",\"requested_load_observation\":"<<js(o.requestedLoadObservation)<<",\"effective_load_observation\":"<<js(o.loadObservation)
    <<",\"maintain_outstanding\":"<<(maintainOutstanding?"true":"false")
    <<",\"maintain_work\":"<<(maintainWork?"true":"false")<<",\"publish_remaining\":"<<(progress?"true":"false")
    <<",\"load_score_unit\":"<<js(o.loadMetric=="work"||o.loadMetric=="remaining"?"estimated_cpu_ms_per_capacity":"tasks_per_capacity")
    <<",\"fail_submit_task\":"<<(o.failSubmitTask==std::numeric_limits<uint64_t>::max()?"null":std::to_string(o.failSubmitTask))
    <<",\"same_cpu_union\":"<<(l.sameCpuUnion?"true":"false")<<",\"extra_allowed_cpus\":"<<array(l.extraAllowed)
    <<",\"pools\":"<<o.pools<<",\"threads_per_pool\":"<<o.threads<<",\"allowed_cpus\":"<<array(l.inherited)<<",\"numa_labels\":"<<array(l.nodes)<<",\"effective_masks\":[";
  for(size_t i=0;i<l.masks.size();++i) { if(i)metadata<<','; metadata<<array(l.masks[i]); }
  metadata<<"],\"arrival\":"<<js(o.arrival)<<",\"arrival_rate\":"<<o.rate<<",\"duration_seconds\":"<<o.duration<<",\"arrival_limit\":"<<o.arrivals
    <<",\"burst_min\":"<<o.burstMin<<",\"burst_max\":"<<o.burstMax<<",\"burst_placement\":"<<js(o.burstPlacement)
    <<",\"workload\":"<<js(o.workload)<<",\"burst_window_ms\":"<<o.burstWindowMs
    <<",\"steady_tasks\":"<<o.steadyTasks<<",\"steady_window_ms\":"<<o.steadyWindowMs
    <<",\"last_task_release_ns\":"<<lastRelease<<",\"planned_cpu_us\":"<<totalTargetUs
    <<",\"planned_initial_tasks\":"<<initialTasks<<",\"planned_steady_tasks\":"<<steadyTasks
    <<",\"planned_initial_cpu_us\":"<<initialTargetUs<<",\"planned_steady_cpu_us\":"<<steadyTargetUs
    <<",\"mean_normalization\":"<<js(o.meanNormalization)<<",\"cpu_us\":"<<o.cpuUs<<",\"jitter\":"<<o.jitter
    <<",\"long_prob\":"<<o.tailProb<<",\"long_factor\":"<<o.tailFactor<<",\"estimate_us\":"<<o.estimateUs
    <<",\"estimate_jitter\":"<<o.estimateJitter<<",\"seed\":"<<o.seed
    <<",\"work_mode\":"<<js(o.workMode)<<",\"iterations_per_us\":"<<o.iterationsPerUs<<",\"max_outstanding\":"<<o.maxOutstanding
    <<",\"max_planned_tasks\":"<<o.maxPlanned<<",\"sample_ms\":"<<o.sampleMs<<",\"max_samples\":"<<o.maxSamples<<",\"fail_task\":"<<(o.failTask==std::numeric_limits<uint64_t>::max()?"null":std::to_string(o.failTask))<<",\"drain_seconds\":"<<o.drain
    <<",\"planned_arrivals\":"<<bursts.size()<<",\"planned_tasks\":"<<tasks.size()<<",\"clock\":\"CLOCK_MONOTONIC relative ns\","
    <<"\"limits\":[\"Native queue is approximate Folly queue telemetry, not kernel runq\",\"No numa memory binding or physical cluster discovery\","
    <<"\"All instrumentation modes retain safety atomics, CPU work clock and basic task timestamps\",\"work estimate retains full cost until completion, not remaining CPU\",\"Capacity is a logical accounting divisor, not physical throughput; overlapping masks share resources\",\"Load snapshots use separate relaxed atomic reads, not one coherent multi-pool transaction\",\"Remaining is a periodically published estimate; underestimation may yield zero before completion\",\"Routing observes only currently submitted tasks, not future burst arrivals; no queued-task migration\",\"Single producer reserves before the next decision; acceptance is a pre-add score gate, not a lifetime load cap\",\"request-wave follow-ups are independent scheduled tasks, not dependency-driven DAG readiness\",\"Progress observation adds thread CPU clock reads to iterations mode\",\"spilled counts successful add calls whose selected pool differs from home\"]}\n";
  metadata.close();
  std::atomic<uint64_t> outstanding{0}; std::atomic<bool> cancel{false}, stopSample{false};
  std::vector<Sample> samples;
  uint64_t samplesDropped=0; bool samplingFailed=false;
  std::mutex sampleMutex; std::condition_variable sampleWake;
  rusage usageBegin{},usageEnd{}; getrusage(RUSAGE_SELF,&usageBegin);
  std::thread sampler;
  if(queue||observe) sampler=std::thread([&] {
    try {
    uint64_t next=epoch;
    while(!stopSample.load(relaxed)) {
      {
        std::unique_lock lock(sampleMutex);
        auto now=ns();
        if(now<next) sampleWake.wait_for(lock,std::chrono::nanoseconds(next-now),[&]{return stopSample.load(relaxed);});
      }
      if(stopSample.load(relaxed)) break;
      uint64_t t=ns()-epoch;
      for(size_t i=0;i<o.pools;++i) {
        auto& c=*counters[i];
        if(samples.size()<o.maxSamples) {
          samples.push_back({t,i,observeQueue?int64_t(pools[i]->getTaskQueueSize()):-1,counts?int64_t(c.pending.load(relaxed)):-1,
            counts?int64_t(c.inflight.load(relaxed)):-1,maintainWork?int64_t(c.work.load(relaxed)):-1,
            maintainOutstanding?int64_t(c.outstanding.load(relaxed)):-1,progress?int64_t(c.remaining.load(relaxed)):-1,capacities[i]});
        } else ++samplesDropped;
      }
      next+=o.sampleMs*1000000ULL;
    }
    } catch(...) { samplingFailed=true; }
  });
  std::vector<LoadSnapshot> snapshots(o.pools);
  const auto readLoad=[&](size_t pool) {
    auto& c=*counters[pool];
    snapshots[pool]={observeNative?int64_t(pools[pool]->getTaskQueueSize()):-1,
      maintainOutstanding?int64_t(c.outstanding.load(relaxed)):-1,
      observeWork?int64_t(c.work.load(relaxed)):-1,progress?int64_t(c.remaining.load(relaxed)):-1,
      counts&&!minimal?int64_t(c.inflight.load(relaxed)):-1};
    const auto& s=snapshots[pool];
    double value=o.loadMetric=="queue" ? s.queue : o.loadMetric=="outstanding" ? s.outstanding :
      o.loadMetric=="work" ? s.work : s.remaining;
    return independent::normalizedScore(value,capacities[pool],o.loadMetric=="work"||o.loadMetric=="remaining");
  };
  for(size_t taskIndex:releaseOrder) {
      auto& t=tasks[taskIndex];
      auto& b=bursts[t.burst];
      // For flat workloads keep the old one-sleep-per-burst hot path.
      if(o.workload=="request-wave" || t.burstIndex==0)until(epoch+t.planned);
      t.submit=ns()-epoch;
      if(t.burstIndex==0)b.actual=t.submit;
      // Routing observes the state before this task's reservation. There is one
      // producer; future burst tasks are not inspected by the admission policy.
      if(observe) {
        const auto routeBegin=ns();
        if(o.routing=="threshold") {
          const auto decision=independent::chooseThreshold(t.homePool,o.pools,o.loadThreshold,
            o.minImprovement,t.id%o.pools,readLoad,o.acceptThreshold);
          t.pool=decision.pool; t.homeScore=decision.homeScore;
          t.selectedScore=decision.selectedScore; t.routeCandidates=decision.candidates;
          t.sourceOverThreshold=decision.sourceOverThreshold;
          t.noEligibleTarget=decision.noEligibleTarget; t.noImprovement=decision.noImprovement;
          t.eligibleCandidates=decision.eligibleCandidates;
        } else {
          t.homeScore=readLoad(t.homePool); t.routeCandidates=1;
          if(o.routing=="spread")t.pool=t.id%o.pools;
          if(t.pool==t.homePool)t.selectedScore=t.homeScore;
          else {t.selectedScore=readLoad(t.pool);++t.routeCandidates;}
        }
        t.routeNs=ns()-routeBegin;
        t.homeLoad=snapshots[t.homePool]; t.selectedLoad=snapshots[t.pool];
      } else if(o.routing=="spread")t.pool=t.id%o.pools;
      t.capacity=capacities[t.pool];
      auto& c=*counters[t.pool]; auto& pool=*pools[t.pool];
      // Historical columns continue to describe the actual selected pool.
      if(observeQueue)t.queue=observeNative?t.selectedLoad.queue:int64_t(pool.getTaskQueueSize());
      if(counts){t.pending=c.pending.load(relaxed);t.inflight=observe&&!minimal?t.selectedLoad.inflight:int64_t(c.inflight.load(relaxed));}
      if(maintainWork)t.work=observeWork?t.selectedLoad.work:int64_t(c.work.load(relaxed));
      if(ns()>=deadline) {t.state=Task::Deadline;continue;}
      if(outstanding.fetch_add(1,relaxed)>=o.maxOutstanding) {outstanding.fetch_sub(1,relaxed);t.state=Task::Rejected;continue;}
      // Reserve on the target before add(): a worker may begin immediately.
      if(maintainOutstanding)c.outstanding.fetch_add(1,relaxed);
      if(counts)c.pending.fetch_add(1,relaxed);
      if(maintainWork)c.work.fetch_add(t.estimate,relaxed);
      if(progress)c.remaining.fetch_add(t.estimate,relaxed);
      try {
        if(t.id==o.failSubmitTask)throw std::runtime_error("injected submit failure");
        pool.add([&, p=&t, cp=&c] {
          bool entered=false;
          uint64_t unpublished=p->estimate;
          auto cleanup=folly::makeGuard([&] {
            if(progress)cp->remaining.fetch_sub(unpublished,relaxed);
            if(maintainWork)cp->work.fetch_sub(p->estimate,relaxed);
            if(counts) {
              if(entered)cp->inflight.fetch_sub(1,relaxed);
              else cp->pending.fetch_sub(1,relaxed);
            }
            if(maintainOutstanding)cp->outstanding.fetch_sub(1,relaxed);
            outstanding.fetch_sub(1,std::memory_order_release);
          });
          try {
            p->start=ns()-epoch;
            if(counts){cp->inflight.fetch_add(1,relaxed);cp->pending.fetch_sub(1,relaxed);entered=true;}
            if(full)p->tid=int(syscall(SYS_gettid));
            if(p->id==o.failTask)throw std::runtime_error("injected callback failure");
            work(*p,o,cancel,full,progress?&cp->remaining:nullptr,unpublished);
            p->end=ns()-epoch;
          } catch(...) {
            p->state=Task::Failed;
            if(!p->start)p->start=p->submit;
            try {p->end=ns()-epoch;}catch(...){p->end=p->start;}
          }
        });
        t.spilled=t.pool!=t.homePool;
      } catch(...) {
        if(progress)c.remaining.fetch_sub(t.estimate,relaxed);
        if(maintainWork)c.work.fetch_sub(t.estimate,relaxed);
        if(counts)c.pending.fetch_sub(1,relaxed);
        if(maintainOutstanding)c.outstanding.fetch_sub(1,relaxed);
        outstanding.fetch_sub(1,relaxed);t.state=Task::Rejected;
      }
  }
  while(outstanding.load(std::memory_order_acquire)) {
    if(ns()>=deadline)cancel.store(true,relaxed);
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  const uint64_t finish=ns()-epoch;
  {std::lock_guard lock(sampleMutex);stopSample.store(true,relaxed);} sampleWake.notify_all(); if(sampler.joinable())sampler.join();
  for(auto& p:pools)p->join();
  getrusage(RUSAGE_SELF,&usageEnd);
  auto tvSeconds=[](const timeval& tv){return double(tv.tv_sec)+double(tv.tv_usec)/1e6;};
  uint64_t completed=0,rejected=0,canceled=0,failed=0,spilled=0;
  uint64_t sourceOverThreshold=0,noEligibleTarget=0,noImprovement=0;
  std::vector<double> latency,wait,plannedLatency,cpuTime,burstLatency,arrivalLag,routeCosts;
  std::map<uint64_t,std::vector<double>> seconds;
  std::ofstream csv(path("tasks.csv")); csv<<std::setprecision(std::numeric_limits<double>::max_digits10)<<"task,burst,pool,status,target_cpu_us,estimate_us,planned_ns,submit_ns,start_ns,end_ns,native_queue,pending,inflight,work_estimate_us,thread_cpu_ns,worker_tid,home_pool,home_native_queue,home_outstanding,home_work_estimate_us,home_remaining_work_us,selected_native_queue,selected_outstanding,selected_work_estimate_us,selected_remaining_work_us,home_score,selected_score,route_ns,route_candidates,spilled,burst_index,burst_remaining,capacity,home_inflight,selected_inflight,request_planned_ns,phase,source_over_threshold,no_eligible_target,no_improvement,eligible_candidates\n";
  for(const auto& t:tasks) {
    csv<<t.id<<','<<t.burst<<','<<t.pool<<','<<stateName(t.state)<<','<<t.target<<','<<t.estimate<<','<<t.planned<<','<<t.submit<<','<<t.start<<','<<t.end<<','<<t.queue<<','<<t.pending<<','<<t.inflight<<','<<t.work<<','<<(full?int64_t(t.cpu):-1)<<','<<t.tid
      <<','<<t.homePool<<','<<t.homeLoad.queue<<','<<t.homeLoad.outstanding<<','<<t.homeLoad.work<<','<<t.homeLoad.remaining
      <<','<<t.selectedLoad.queue<<','<<t.selectedLoad.outstanding<<','<<t.selectedLoad.work<<','<<t.selectedLoad.remaining
      <<','<<t.homeScore<<','<<t.selectedScore<<','<<t.routeNs<<','<<t.routeCandidates<<','<<int(t.spilled)
      <<','<<t.burstIndex<<','<<t.burstRemaining<<','<<t.capacity<<','<<t.homeLoad.inflight<<','<<t.selectedLoad.inflight
      <<','<<t.requestPlanned<<','<<(t.steady?"steady":"initial")<<','<<int(t.sourceOverThreshold)
      <<','<<int(t.noEligibleTarget)<<','<<int(t.noImprovement)<<','<<t.eligibleCandidates<<'\n';
    if(t.spilled)++spilled;
    if(t.sourceOverThreshold)++sourceOverThreshold;
    if(t.noEligibleTarget)++noEligibleTarget;
    if(t.noImprovement)++noImprovement;
    if(t.routeNs>=0)routeCosts.push_back(double(t.routeNs));
    if(t.state==Task::Completed) {
      ++completed;double elapsed=double(t.end-t.submit)/1e6; latency.push_back(elapsed);wait.push_back(double(t.start-t.submit)/1e6);
      plannedLatency.push_back(double(t.end-t.planned)/1e6);seconds[t.end/1000000000ULL].push_back(elapsed);
      if(full)cpuTime.push_back(double(t.cpu)/1e6);
    } else if(t.state==Task::Canceled)++canceled;else if(t.state==Task::Failed)++failed;else ++rejected;
  }
  csv.close();
  std::ofstream bc(path("bursts.csv"));bc<<"burst,pool,planned_ns,actual_ns,tasks,completed,rejected,canceled,failed,end_ns,complete_latency_ms\n";
  for(size_t i=0;i<bursts.size();++i) {
    const auto& b=bursts[i];size_t ok=0,rej=0,can=0,fail=0;uint64_t end=0;
    for(size_t j=0;j<b.count;++j){const auto& t=tasks[b.first+j];if(t.state==Task::Completed)++ok;else if(t.state==Task::Canceled)++can;else if(t.state==Task::Failed)++fail;else ++rej;end=std::max(end,t.end);}
    bc<<i<<','<<b.pool<<','<<b.planned<<','<<b.actual<<','<<b.count<<','<<ok<<','<<rej<<','<<can<<','<<fail<<','<<end<<',';
    if(ok==b.count){double v=double(end-b.actual)/1e6;bc<<v;burstLatency.push_back(v);}bc<<'\n';arrivalLag.push_back(double(b.actual-b.planned)/1e6);
  }
  std::ofstream sc(path("seconds.csv"));sc<<"completion_second,completed_tasks,task_mean_ms,task_p99_ms\n";
  for(uint64_t s=0;s<=finish/1000000000ULL;++s){auto& v=seconds[s];sc<<s<<','<<v.size()<<','<<(v.empty()?"":stat(v))<<','<<(v.empty()?"":stat(v,true))<<'\n';}
  std::ofstream pc(path("pools.csv"));pc<<std::setprecision(std::numeric_limits<double>::max_digits10)<<"time_ns,pool,native_queue,pending,inflight,work_estimate_us,outstanding,remaining_work_us,capacity\n";
  for(const auto& s:samples)pc<<s.t<<','<<s.pool<<','<<s.queue<<','<<s.pending<<','<<s.inflight<<','<<s.work<<','<<s.outstanding<<','<<s.remaining<<','<<s.capacity<<'\n';
  bc.close();sc.close();pc.close();
  if(!csv||!bc||!sc||!pc||!metadata||!workers)throw std::runtime_error("output write failed");
  bool balanced=outstanding.load()==0;
  for(const auto& c:counters)if(c->pending.load()||c->inflight.load()||c->work.load()||c->outstanding.load()||c->remaining.load())balanced=false;
  std::ofstream summary(path("summary.json"));summary<<std::setprecision(12)<<"{\"planned_tasks\":"<<tasks.size()<<",\"completed_tasks\":"<<completed
    <<",\"rejected_tasks\":"<<rejected<<",\"canceled_tasks\":"<<canceled<<",\"failed_tasks\":"<<failed<<",\"complete_bursts\":"<<burstLatency.size()
    <<",\"spilled_tasks\":"<<spilled<<",\"route_ns_mean\":"<<stat(routeCosts)<<",\"route_ns_p99\":"<<stat(routeCosts,true)
    <<",\"source_over_threshold_tasks\":"<<sourceOverThreshold<<",\"no_eligible_target_tasks\":"<<noEligibleTarget
    <<",\"no_improvement_tasks\":"<<noImprovement
    <<",\"final_outstanding\":"<<outstanding.load()<<",\"counters_balanced\":"<<(balanced?"true":"false")
    <<",\"conservation_ok\":"<<(completed+rejected+canceled+failed==tasks.size()&&balanced?"true":"false")<<",\"observed_seconds\":"<<double(finish)/1e9
    <<",\"task_mean_ms\":"<<stat(latency)<<",\"task_p99_ms\":"<<stat(latency,true)<<",\"submit_to_start_mean_ms\":"<<stat(wait)
    <<",\"planned_to_end_mean_ms\":"<<stat(plannedLatency)<<",\"burst_mean_ms\":"<<stat(burstLatency)<<",\"burst_p99_ms\":"<<stat(burstLatency,true)
    <<",\"arrival_lag_mean_ms\":"<<stat(arrivalLag)<<",\"arrival_lag_p99_ms\":"<<stat(arrivalLag,true)<<",\"work_cpu_mean_ms\":"<<stat(cpuTime)
    <<",\"sample_rows\":"<<samples.size()<<",\"sample_rows_dropped\":"<<samplesDropped<<",\"sampling_truncated\":"<<(samplesDropped?"true":"false")<<",\"sampling_failed\":"<<(samplingFailed?"true":"false")
    <<",\"process_user_cpu_seconds\":"<<tvSeconds(usageEnd.ru_utime)-tvSeconds(usageBegin.ru_utime)
    <<",\"process_system_cpu_seconds\":"<<tvSeconds(usageEnd.ru_stime)-tvSeconds(usageBegin.ru_stime)
    <<",\"final_pool_counters\":[";
  for(size_t i=0;i<counters.size();++i) {
    if(i)summary<<',';
    const auto& c=*counters[i];
    summary<<"{\"pool\":"<<i<<",\"pending\":"<<c.pending.load()<<",\"inflight\":"<<c.inflight.load()
      <<",\"work_estimate_us\":"<<c.work.load()<<",\"outstanding\":"<<c.outstanding.load()
      <<",\"remaining_work_us\":"<<c.remaining.load()<<'}';
  }
  summary<<"]}\n";
  summary.close();if(!summary)throw std::runtime_error("summary write failed");
  std::cout<<"tasks="<<tasks.size()<<" completed="<<completed<<" rejected="<<rejected<<" canceled="<<canceled<<" failed="<<failed<<" mean_ms="<<stat(latency)<<" p99_ms="<<stat(latency,true)<<'\n';
  return balanced&&!failed&&!samplingFailed?0:2;
}
int main(int argc,char** argv) {
  try {
    auto options=parse(argc,argv);
    int follyArgc=1; char* follyArgs[]={argv[0],nullptr}; char** follyArgv=follyArgs;
    folly::Init init(&follyArgc,&follyArgv);
    return run(options);
  }
  catch(const std::exception& e){std::cerr<<"independent_load: "<<e.what()<<'\n';return 1;}
}

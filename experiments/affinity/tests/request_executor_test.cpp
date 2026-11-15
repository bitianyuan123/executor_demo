#include <executor_demo/RequestExecutor.h>
#include <folly/portability/GTest.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <future>
#include <map>
#include <mutex>
#include <thread>

namespace executor_demo {
namespace {
using Pool = RequestExecutor;
using namespace std::chrono_literals;

class Gate {
 public:
  void wait() { std::unique_lock lock(mutex_); cv_.wait(lock, [&] { return open_; }); }
  void open() { std::lock_guard lock(mutex_); open_ = true; cv_.notify_all(); }
 private:
  std::mutex mutex_;
  std::condition_variable cv_;
  bool open_{false};
};

struct Records {
  void add(const Pool::Observation& row) { std::lock_guard lock(mutex); rows.push_back(row); }
  std::vector<Pool::Observation> take() { std::lock_guard lock(mutex); return rows; }
  std::mutex mutex;
  std::vector<Pool::Observation> rows;
};

Pool::Config config(Records* records = nullptr, size_t groupCount = 2) {
  Pool::Config result;
  result.groups.resize(groupCount);
  for (auto& group : result.groups) { group.threads = 1; group.capacity = 1; group.numaNode = 0; }
  if (records) result.observer = [records](const auto& row) { records->add(row); };
  return result;
}

bool waitUntil(const std::function<bool()>& condition) {
  const auto end = std::chrono::steady_clock::now() + 3s;
  while (std::chrono::steady_clock::now() < end) {
    if (condition()) return true;
    std::this_thread::yield();
  }
  return condition();
}

TEST(RequestExecutor, RoundRobinHomeAndConcurrentChildrenHaveIsolatedContexts) {
  Records records;
  Pool pool(config(&records));
  auto first = pool.beginRequest(100), second = pool.beginRequest(101);
  std::atomic<unsigned> children{0};
  for (auto request : {first, second}) {
    ASSERT_TRUE(pool.add(request, [&] {
      for (size_t i = 0; i < 12; ++i)
        EXPECT_TRUE(pool.add([&] { ++children; }));
    }));
  }
  pool.join();
  EXPECT_EQ(children, 24);
  const auto rows = records.take();
  ASSERT_EQ(rows.size(), 26);
  for (const auto& row : rows) {
    EXPECT_EQ(row.home, row.color == 100 ? 0 : 1);
    EXPECT_EQ(row.chosen, row.home);
    EXPECT_LE(row.submittedNs, row.startNs);
    EXPECT_LE(row.startNs, row.finishNs);
  }
  EXPECT_FALSE(pool.beginRequest(102));
  EXPECT_FALSE(pool.add(first, [] {}));
  pool.join();
}

TEST(RequestExecutor, ColorHashIsStableWithoutAdvancingRoundRobinState) {
  Records records;
  auto cfg = config(&records);
  cfg.homePolicy = Pool::HomePolicy::ColorHash;
  Pool pool(std::move(cfg));
  for (size_t i = 0; i < 12; ++i) {
    auto request = pool.beginRequest(i % 3);
    ASSERT_TRUE(pool.add(request, [] {}));
  }
  pool.join();
  std::map<uint64_t, size_t> home;
  for (const auto& row : records.take()) {
    const auto [it, inserted] = home.emplace(row.color, row.home);
    EXPECT_EQ(it->second, row.home);
    (void)inserted;
  }
  EXPECT_EQ(home.size(), 3);
}

TEST(RequestExecutor, ThreeAndSixGroupsReachEveryHomeAndKeepChildrenThere) {
  for (size_t groupCount : {size_t(3), size_t(6)}) {
    SCOPED_TRACE(groupCount);
    Records records;
    // Logical groups deliberately share the caller's CPU mask: this checks the
    // facade contract on small hosts, not hardware cluster/cache behavior.
    Pool pool(config(&records, groupCount));
    std::atomic<size_t> children{0};
    for (size_t request = 0; request < 2 * groupCount; ++request) {
      ASSERT_TRUE(pool.add(pool.beginRequest(1000 + request), [&] {
        EXPECT_TRUE(pool.add([&] { ++children; }, {1}));
      }));
    }
    pool.join();
    EXPECT_EQ(children, 2 * groupCount);
    const auto rows = records.take();
    ASSERT_EQ(rows.size(), 4 * groupCount);
    std::vector<size_t> observed(groupCount);
    for (const auto& row : rows) {
      const auto expected = (row.color - 1000) % groupCount;
      EXPECT_EQ(row.home, expected);
      EXPECT_EQ(row.chosen, expected);
      ASSERT_LT(row.chosen, groupCount);
      ++observed[row.chosen];
    }
    const auto snapshots = pool.snapshots();
    ASSERT_EQ(snapshots.size(), groupCount);
    for (size_t group = 0; group < groupCount; ++group) {
      EXPECT_EQ(observed[group], 4);
      EXPECT_EQ(snapshots[group].submitted, 4);
      EXPECT_EQ(snapshots[group].completed, 4);
      EXPECT_EQ(snapshots[group].outstanding, 0);
    }
  }
}

TEST(RequestExecutor, RegisteredRpcRestoresContextAndCompletesDuringDrain) {
  Pool pool(config());
  auto request = pool.beginRequest(19);
  std::promise<Pool::Continuation> saved;
  auto future = saved.get_future();
  std::shared_ptr<folly::RequestContext> expected;
  {
    folly::RequestContextScopeGuard scope;
    expected = folly::RequestContext::saveContext();
    ASSERT_TRUE(pool.add(request, [&] { saved.set_value(pool.captureContinuation()); }));
  }
  ASSERT_EQ(future.wait_for(3s), std::future_status::ready);
  auto continuation = future.get();
  std::atomic<bool> joined{false}, restored{false}, childRan{false};
  std::thread joining([&] { pool.join(); joined = true; });
  EXPECT_TRUE(waitUntil([&] { return !pool.acceptingRequests(); }));
  EXPECT_FALSE(joined.load());
  EXPECT_FALSE(pool.add(request, [] {}));
  std::thread rpc([&, token = std::move(continuation)]() mutable {
    EXPECT_TRUE(token.resume([&] {
      restored = folly::RequestContext::saveContext() == expected;
      EXPECT_TRUE(pool.add([&] { childRan = true; }));
    }));
    EXPECT_FALSE(token.resume([] {}));
  });
  rpc.join();
  joining.join();
  EXPECT_TRUE(joined.load());
  EXPECT_TRUE(restored.load());
  EXPECT_TRUE(childRan.load());
}

TEST(RequestExecutor, CancelAndThrowingCallbacksReleaseDrainLeases) {
  Records records;
  Pool pool(config(&records));
  auto request = pool.beginRequest(7);
  std::promise<Pool::Continuation> saved;
  auto future = saved.get_future();
  ASSERT_TRUE(pool.add(request, [&] {
    auto dropped = pool.captureContinuation();
    saved.set_value(pool.captureContinuation());
    throw std::runtime_error("expected callback failure");
  }));
  ASSERT_EQ(future.wait_for(3s), std::future_status::ready);
  auto token = future.get();
  std::thread joining([&] { pool.join(); });
  EXPECT_TRUE(waitUntil([&] { return !pool.acceptingRequests(); }));
  token.cancel();
  token.cancel();
  EXPECT_FALSE(token.resume([] {}));
  joining.join();
  const auto rows = records.take();
  ASSERT_EQ(rows.size(), 1);
  EXPECT_TRUE(rows[0].failed);
  uint64_t submitted = 0, completed = 0, failed = 0;
  for (const auto& snapshot : pool.snapshots()) {
    EXPECT_EQ(snapshot.outstanding, 0);
    EXPECT_EQ(snapshot.pending, 0);
    EXPECT_EQ(snapshot.inflight, 0);
    submitted += snapshot.submitted; completed += snapshot.completed; failed += snapshot.failed;
  }
  EXPECT_EQ(submitted, 1);
  EXPECT_EQ(completed, 1);
  EXPECT_EQ(failed, 1);
}

TEST(RequestExecutor, OutstandingSeesBusyRunningTaskThatQueueCannotSee) {
  for (auto policy : {Pool::Policy::QueueSpill, Pool::Policy::OutstandingSpill}) {
    Records records;
    Gate gate;
    auto cfg = config(&records);
    cfg.policy = policy; cfg.busyHigh = 0; cfg.returnLow = 0;
    Pool pool(std::move(cfg));
    auto release = folly::makeGuard([&] { gate.open(); });
    auto request = pool.beginRequest(0);
    std::promise<void> started, done;
    auto ready = started.get_future(), finished = done.get_future();
    ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
    ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
    // The initial submit has returned, so its reservation is no longer visible.
    ASSERT_TRUE(pool.add(request, [&] { done.set_value(); }, {9, 1000, false, false}));
    if (policy == Pool::Policy::OutstandingSpill)
      EXPECT_EQ(finished.wait_for(3s), std::future_status::ready);
    else
      EXPECT_EQ(finished.wait_for(20ms), std::future_status::timeout);
    gate.open(); pool.join();
    const auto rows = records.take();
    auto row = std::find_if(rows.begin(), rows.end(), [](const auto& value) { return value.tag == 9; });
    ASSERT_NE(row, rows.end());
    EXPECT_EQ(row->chosen, policy == Pool::Policy::OutstandingSpill ? 1 : 0);
  }
}

TEST(RequestExecutor, BatchChunksAreIndependentCallbacksWithUniqueTags) {
  Records records;
  Gate gate;
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::OutstandingSpill; cfg.batchChunk = 4;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(40);
  std::atomic<size_t> ran{0};
  std::vector<folly::Func> tasks;
  for (size_t i = 0; i < 8; ++i) tasks.push_back([&] { gate.wait(); ++ran; });
  EXPECT_EQ(pool.addBatch(request, std::move(tasks), {20, 1000, false, true}), 8);
  auto snapshots = pool.snapshots();
  EXPECT_EQ(snapshots[0].outstanding, 4);
  EXPECT_EQ(snapshots[1].outstanding, 4);
  gate.open(); pool.join();
  EXPECT_EQ(ran, 8);
  auto rows = records.take();
  std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) { return a.tag < b.tag; });
  ASSERT_EQ(rows.size(), 8);
  for (size_t i = 0; i < 8; ++i) {
    EXPECT_EQ(rows[i].tag, 20 + i);
    EXPECT_EQ(rows[i].chosen, i < 4 ? 0 : 1);
  }
}

TEST(RequestExecutor, BatchReportsExactAcceptedPrefixOnCapacityRejection) {
  Gate gate;
  auto cfg = config(); cfg.maxOutstanding = 1; cfg.batchChunk = 8;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(0);
  std::atomic<size_t> ran{0};
  std::vector<folly::Func> tasks;
  for (size_t i = 0; i < 3; ++i) tasks.push_back([&] { gate.wait(); ++ran; });
  EXPECT_EQ(pool.addBatch(request, std::move(tasks)), 1);
  gate.open(); pool.join();
  EXPECT_EQ(ran, 1);
  EXPECT_EQ(pool.snapshots()[0].outstanding, 0);
}

TEST(RequestExecutor, ReturnHomeLowWatermarkAndHopBudgetAreIndependent) {
  for (size_t maxHops : {size_t(1), size_t(4)}) {
    Records records;
    Gate gate;
    auto cfg = config(&records);
    cfg.policy = Pool::Policy::OutstandingSpill;
    cfg.continuationPolicy = Pool::ContinuationPolicy::ReturnHome;
    cfg.busyHigh = 0; cfg.returnLow = 0; cfg.maxHops = maxHops;
    Pool pool(std::move(cfg));
    auto release = folly::makeGuard([&] { gate.open(); });
    auto request = pool.beginRequest(0);
    std::promise<void> started;
    auto ready = started.get_future();
    ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
    ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
    ASSERT_TRUE(pool.add(request, [&] {
      gate.open();
      EXPECT_TRUE(waitUntil([&] { return pool.snapshots()[0].outstanding == 0; }));
      EXPECT_TRUE(pool.add([] {}, {9, 1000, false, false}));
    }));
    pool.join();
    const auto rows = records.take();
    const auto row = std::find_if(rows.begin(), rows.end(), [](const auto& value) { return value.tag == 9; });
    ASSERT_NE(row, rows.end());
    EXPECT_EQ(row->chosen, maxHops == 1 ? 1 : 0);
    EXPECT_EQ(row->reason, maxHops == 1 ? Pool::Reason::HopBudget : Pool::Reason::ReturnHome);
  }
}

TEST(RequestExecutor, UnknownAndForeignContextsFailClearlyAndObserverFailureDoesNotLeak) {
  auto cfg = config(); cfg.observer = [](const auto&) { throw std::runtime_error("observer failed"); };
  Pool pool(std::move(cfg)), other(config());
  auto request = pool.beginRequest(1);
  EXPECT_THROW(pool.add([] {}), std::logic_error);
  EXPECT_THROW(pool.captureContinuation(), std::logic_error);
  EXPECT_THROW(other.add(request, [] {}), std::invalid_argument);
  EXPECT_THROW(pool.add(Pool::Request{}, [] {}), std::invalid_argument);
  ASSERT_TRUE(pool.add(request, [] {}));
  pool.join();
  EXPECT_EQ(pool.observerFailures(), 1);
  EXPECT_EQ(pool.snapshots()[0].completed, 1);
}

TEST(RequestExecutor, LocalDomainIsPreferredAndRemoteBorrowCanBeDisabled) {
  for (bool allow : {false, true}) {
    Records records;
    Gate gate;
    auto cfg = config(&records);
    cfg.groups[1].numaNode = 1;
    cfg.policy = Pool::Policy::WorkSpill; cfg.busyHigh = 0; cfg.returnLow = 0;
    cfg.allowCrossNuma = allow;
    Pool pool(std::move(cfg));
    auto release = folly::makeGuard([&] { gate.open(); });
    auto request = pool.beginRequest(0);
    std::promise<void> started;
    auto ready = started.get_future();
    ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
    ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
    ASSERT_TRUE(pool.add(request, [] {}, {9, 1000, false, false}));
    gate.open(); pool.join();
    const auto rows = records.take();
    const auto row = std::find_if(rows.begin(), rows.end(), [](const auto& value) { return value.tag == 9; });
    ASSERT_NE(row, rows.end());
    EXPECT_EQ(row->chosen, allow ? 1 : 0);
    EXPECT_EQ(row->crossNuma, allow);
  }
}

TEST(RequestExecutor, ZeroHopBudgetPreventsInitialBorrow) {
  Records records;
  Gate gate;
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::OutstandingSpill;
  cfg.busyHigh = 0; cfg.returnLow = 0; cfg.maxHops = 0;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(0);
  std::promise<void> started;
  auto ready = started.get_future();
  ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
  ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
  ASSERT_TRUE(pool.add(request, [] {}, {9, 1000, false, false}));
  gate.open(); pool.join();
  for (const auto& row : records.take()) {
    EXPECT_EQ(row.chosen, 0);
    EXPECT_EQ(row.hops, 0);
    EXPECT_EQ(row.reason, Pool::Reason::HopBudget);
  }
}

TEST(RequestExecutor, ConcurrentClosingLosesNoAcceptedRootOrInternalChild) {
  for (size_t attempt = 0; attempt < 12; ++attempt) {
    auto cfg = config();
    cfg.policy = Pool::Policy::OutstandingSpill;
    cfg.groups[0].threads = cfg.groups[1].threads = 2;
    Pool pool(std::move(cfg));
    auto request = pool.beginRequest(attempt);
    std::atomic<size_t> accepted{0}, roots{0}, children{0};
    auto root = [&] {
      ++roots;
      EXPECT_TRUE(pool.add([&] { ++children; }));
    };
    ASSERT_TRUE(pool.add(request, root));
    ++accepted;
    std::vector<std::thread> producers;
    for (size_t p = 0; p < 4; ++p)
      producers.emplace_back([&] {
        for (size_t i = 0; i < 100; ++i)
          if (pool.add(request, root)) ++accepted;
      });
    pool.join();
    for (auto& thread : producers) thread.join();
    EXPECT_EQ(roots.load(), accepted.load());
    EXPECT_EQ(children.load(), accepted.load());
    uint64_t completed = 0;
    for (const auto& snapshot : pool.snapshots()) {
      EXPECT_EQ(snapshot.outstanding, 0);
      completed += snapshot.completed;
    }
    EXPECT_EQ(completed, accepted.load() * 2);
  }
}

TEST(RequestExecutor, InvalidCapacityIsRejectedBeforeAnyRouting) {
  for (double capacity : {0.0, -1.0, std::numeric_limits<double>::quiet_NaN(),
                          std::numeric_limits<double>::infinity()}) {
    auto cfg = config();
    cfg.groups[0].capacity = capacity;
    EXPECT_THROW(Pool pool(std::move(cfg)), std::invalid_argument);
  }
  auto cfg = config();
  cfg.groups[0].capacity = 0.25;
  Pool pool(std::move(cfg));
  ASSERT_TRUE(pool.add(pool.beginRequest(1), [] {}));
  pool.join();
}

TEST(RequestExecutor, ForkOnlyFollowsBorrowedParentUntilNextFork) {
  Records records;
  Gate gate;
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::OutstandingSpill;
  cfg.continuationPolicy = Pool::ContinuationPolicy::ReturnHome;
  cfg.balanceOnlyAtFork = true;
  cfg.busyHigh = 0; cfg.returnLow = 0;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(0);
  std::promise<void> started;
  auto ready = started.get_future();
  ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
  ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
  std::vector<folly::Func> fork;
  fork.emplace_back([&] {
    gate.open();
    EXPECT_TRUE(waitUntil([&] { return pool.snapshots()[0].outstanding == 0; }));
    EXPECT_TRUE(pool.add([&] {
      std::vector<folly::Func> nextFork;
      nextFork.emplace_back([] {});
      EXPECT_EQ(pool.addBatch(std::move(nextFork), {3, 1000, false, false}), 1);
    }, {2, 1000, false, false}));
  });
  EXPECT_EQ(pool.addBatch(request, std::move(fork), {1, 1000, false, false}), 1);
  pool.join();
  auto rows = records.take();
  std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) { return a.tag < b.tag; });
  ASSERT_EQ(rows.size(), 4);
  EXPECT_EQ(rows[0].chosen, 0);
  EXPECT_EQ(rows[1].chosen, 1);
  EXPECT_EQ(rows[1].reason, Pool::Reason::LocalSpill);
  EXPECT_EQ(rows[2].chosen, 1);
  EXPECT_EQ(rows[2].reason, Pool::Reason::NonFork);
  EXPECT_EQ(rows[3].chosen, 0);
  EXPECT_EQ(rows[3].reason, Pool::Reason::ReturnHome);
}

class PublishedProvider final : public LoadProvider {
 public:
  explicit PublishedProvider(size_t groups = 2) : samples(groups) {}
  std::vector<LoadSample> samples;
  LoadSample read(size_t group) const noexcept override {
    return group < samples.size() ? samples[group] : LoadSample{};
  }
  const char* name() const noexcept override { return "test-published"; }
};

TEST(RequestExecutor, ThreeAndSixGroupsRouteToLastGroupUsingNormalizedCapacity) {
  for (size_t groupCount : {size_t(3), size_t(6)}) {
    SCOPED_TRACE(groupCount);
    Records records;
    auto cfg = config(&records, groupCount);
    auto provider = std::make_shared<PublishedProvider>(groupCount);
    cfg.policy = Pool::Policy::QueueSpill;
    cfg.loadProvider = provider;
    for (size_t group = 0; group < groupCount; ++group) {
      auto& sample = provider->samples[group];
      sample.queueValid = true;
      sample.queueDepth = group == 0 ? 8 : group == groupCount - 1 ? 4 : 3;
      sample.capacity = 999; // Configured capacity must remain authoritative.
      cfg.groups[group].capacity = group == 0 || group == groupCount - 1 ? 1 : 0.5;
    }
    // The last group has MORE queued tasks than intermediate candidates, but
    // less normalized pressure: 4/1 < 3/0.5 < home 8/1. Snapshots verify the
    // actual callback was submitted there, not merely a reported decision.
    Pool pool(std::move(cfg));
    ASSERT_TRUE(pool.add(pool.beginRequest(7), [] {}));
    pool.join();
    const auto rows = records.take();
    ASSERT_EQ(rows.size(), 1);
    EXPECT_EQ(rows[0].home, 0);
    EXPECT_EQ(rows[0].chosen, groupCount - 1);
    EXPECT_EQ(rows[0].reason, Pool::Reason::LocalSpill);
    EXPECT_DOUBLE_EQ(rows[0].preferredScore, 8);
    EXPECT_DOUBLE_EQ(rows[0].chosenScore, 4);
    const auto snapshots = pool.snapshots();
    ASSERT_EQ(snapshots.size(), groupCount);
    for (size_t group = 0; group < groupCount; ++group)
      EXPECT_EQ(snapshots[group].completed, group == groupCount - 1 ? 1 : 0);
  }
}

TEST(RequestExecutor, ThreeAndSixGroupsPreferLocalDomainBeforeRemoteFallback) {
  struct Scenario {
    uint64_t localQueue;
    bool allowRemote;
    Pool::Reason reason;
  };
  for (size_t groupCount : {size_t(3), size_t(6)}) {
    for (const auto scenario : {
             Scenario{2, true, Pool::Reason::LocalSpill},
             Scenario{8, true, Pool::Reason::CrossNumaSpill},
             Scenario{8, false, Pool::Reason::InsufficientGain}}) {
      SCOPED_TRACE(::testing::Message() << "groups=" << groupCount
                   << " local_queue=" << scenario.localQueue
                   << " allow_remote=" << scenario.allowRemote);
      Records records;
      auto cfg = config(&records, groupCount);
      auto provider = std::make_shared<PublishedProvider>(groupCount);
      cfg.policy = Pool::Policy::QueueSpill;
      cfg.loadProvider = provider;
      cfg.allowCrossNuma = scenario.allowRemote;
      for (size_t group = 0; group < groupCount; ++group) {
        // Noncontiguous labels and a local candidate at the final index guard
        // against assumptions about group order or clusters per NUMA node.
        cfg.groups[group].numaNode = group == 0 || group == groupCount - 1 ? 10 : 27;
        provider->samples[group].queueValid = true;
        provider->samples[group].queueDepth =
            group == 0 ? 8 : group == groupCount - 1 ? scenario.localQueue : 0;
      }
      const auto expected = scenario.localQueue < 8 ? groupCount - 1
                            : scenario.allowRemote ? size_t(1) : size_t(0);
      Pool pool(std::move(cfg));
      ASSERT_TRUE(pool.add(pool.beginRequest(17), [] {}));
      pool.join();
      const auto rows = records.take();
      ASSERT_EQ(rows.size(), 1);
      EXPECT_EQ(rows[0].home, 0);
      EXPECT_EQ(rows[0].chosen, expected);
      EXPECT_EQ(rows[0].reason, scenario.reason);
      EXPECT_EQ(rows[0].crossNuma, expected == 1);
      EXPECT_DOUBLE_EQ(rows[0].preferredScore, 8);
      EXPECT_DOUBLE_EQ(rows[0].chosenScore, expected == 0 ? 8 : expected == 1 ? 0 : 2);
      const auto snapshots = pool.snapshots();
      ASSERT_EQ(snapshots.size(), groupCount);
      for (size_t group = 0; group < groupCount; ++group)
        EXPECT_EQ(snapshots[group].completed, group == expected ? 1 : 0);
    }
  }
}

class SquaredQueueScorer final : public LoadScorer {
 public:
  LoadInputs needs() const noexcept override { return LoadInputs::QueueOnly; }
  const char* name() const noexcept override { return "test-squared-queue"; }
  const char* units() const noexcept override { return "squared_tasks_per_capacity"; }
  LoadScore score(const LoadSample& sample, const ScoreContext& context) const noexcept override {
    const auto tasks = double(sample.queueDepth) + double(context.reservedTasks);
    return {tasks * tasks / sample.capacity, false, LoadFallback::None};
  }
};

TEST(RequestExecutor, RegisteredScorerAndPublishedProviderKeepRoutingSeparate) {
  Records records;
  auto provider = std::make_shared<PublishedProvider>();
  for (size_t i = 0; i < provider->samples.size(); ++i) {
    provider->samples[i].queueValid = true;
    provider->samples[i].queueDepth = i == 0 ? 10 : 1;
    provider->samples[i].capacity = 999; // Config remains authoritative.
  }
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::QueueSpill;
  cfg.loadScorer = std::make_shared<SquaredQueueScorer>();
  cfg.loadProvider = provider;
  Pool pool(std::move(cfg));
  EXPECT_STREQ(pool.loadScorerName(), "test-squared-queue");
  EXPECT_DOUBLE_EQ(pool.readLoadSample(0).capacity, 1);
  ASSERT_TRUE(pool.add(pool.beginRequest(7), [] {}));
  pool.join();
  const auto rows = records.take();
  ASSERT_EQ(rows.size(), 1);
  EXPECT_EQ(rows[0].chosen, 1);
  EXPECT_DOUBLE_EQ(rows[0].preferredScore, 100);
  EXPECT_DOUBLE_EQ(rows[0].chosenScore, 1);
  EXPECT_TRUE(rows[0].scoreEvaluated);
  EXPECT_FALSE(rows[0].scoreFallback);
}

TEST(RequestExecutor, ActivePolicyObservesBlockedCallbacksWithoutCallingThemCpuRunning) {
  Records records;
  Gate gate;
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::ActiveSpill;
  cfg.busyHigh = cfg.returnLow = 0;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(0);
  std::promise<void> started, finished;
  auto ready = started.get_future(), done = finished.get_future();
  ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
  ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
  const auto load = pool.readLoadSample(0);
  EXPECT_EQ(load.queueDepth, 0);
  EXPECT_EQ(load.callbackInflight, 1);
  EXPECT_FALSE(load.cpuValid); // Inflight by itself provides no CPU measurement.
  ASSERT_TRUE(pool.add(request, [&] { finished.set_value(); }, {9, 1000, false, false}));
  EXPECT_EQ(done.wait_for(3s), std::future_status::ready);
  gate.open(); pool.join();
  for (const auto& row : records.take()) if (row.tag == 9) {
    EXPECT_EQ(row.chosen, 1);
    EXPECT_DOUBLE_EQ(row.preferredScore, 1);
    EXPECT_FALSE(row.cpuValid);
  }
}

TEST(RequestExecutor, StaleCpuProviderFallsBackToLiveNativeOutstanding) {
  Records records;
  Gate gate;
  auto provider = std::make_shared<PublishedProvider>();
  for (auto& sample : provider->samples) {
    sample.cpuValid = sample.runqueueValid = true;
    sample.sampledAtNs = 1; sample.sampleWindowNs = 1;
    sample.outstandingValid = true;
    sample.outstanding = 0; // Must not replace live O in CPU fallback.
  }
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::CpuSpill;
  cfg.loadProvider = provider;
  cfg.busyHigh = cfg.returnLow = 0;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(0);
  std::promise<void> started, finished;
  auto ready = started.get_future(), done = finished.get_future();
  ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
  ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
  ASSERT_TRUE(pool.add(request, [&] { finished.set_value(); }, {9, 1000, false, false}));
  EXPECT_EQ(done.wait_for(3s), std::future_status::ready);
  gate.open(); pool.join();
  for (const auto& row : records.take()) if (row.tag == 9) {
    EXPECT_EQ(row.chosen, 1);
    EXPECT_TRUE(row.scoreFallback);
    EXPECT_EQ(row.scoreFallbackReason, LoadFallback::StaleCpu);
  }
}

TEST(RequestExecutor, CpuProviderWithFreshRunqueueDemandRoutesEvenWhenNativeQueueIsEmpty) {
  Records records;
  auto provider = std::make_shared<PublishedProvider>();
  const auto timestamp = uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
  for (auto& sample : provider->samples) {
    sample.cpuValid = sample.runqueueValid = true;
    sample.sampledAtNs = timestamp; sample.sampleWindowNs = 1000000;
  }
  provider->samples[0].sampledCpuCores = 1;
  provider->samples[0].sampledRunqueueWaitCores = 4;
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::CpuSpill;
  cfg.loadProvider = provider;
  cfg.cpuMaxAgeNs = 10000000000ULL;
  Pool pool(std::move(cfg));
  ASSERT_TRUE(pool.add(pool.beginRequest(0), [] {}));
  pool.join();
  const auto rows = records.take();
  ASSERT_EQ(rows.size(), 1);
  EXPECT_EQ(rows[0].chosen, 1);
  EXPECT_DOUBLE_EQ(rows[0].preferredScore, 5);
  EXPECT_FALSE(rows[0].scoreFallback);
}

TEST(RequestExecutor, CpuStrategyUsesConfiguredWorkerLimitForBlockedPool) {
  Records records;
  Gate gate;
  auto provider = std::make_shared<PublishedProvider>();
  const auto timestamp = uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
  for (auto& sample : provider->samples) {
    sample.cpuValid = sample.runqueueValid = true;
    sample.sampledAtNs = timestamp; sample.sampleWindowNs = 1000000;
  }
  auto cfg = config(&records);
  cfg.policy = Pool::Policy::CpuSpill;
  cfg.loadProvider = provider;
  cfg.cpuMaxAgeNs = 10000000000ULL;
  cfg.busyHigh = cfg.returnLow = 0;
  Pool pool(std::move(cfg));
  auto release = folly::makeGuard([&] { gate.open(); });
  auto request = pool.beginRequest(0);
  std::promise<void> started, finished;
  auto ready = started.get_future(), done = finished.get_future();
  ASSERT_TRUE(pool.add(request, [&] { started.set_value(); gate.wait(); }));
  ASSERT_EQ(ready.wait_for(3s), std::future_status::ready);
  EXPECT_EQ(pool.readLoadSample(0).workerCount, 1);
  ASSERT_TRUE(pool.add(request, [&] { finished.set_value(); }, {9, 1000, false, false}));
  EXPECT_EQ(done.wait_for(3s), std::future_status::ready);
  gate.open(); pool.join();
  for (const auto& row : records.take()) if (row.tag == 9) {
    EXPECT_EQ(row.chosen, 1);
    EXPECT_TRUE(row.scoreFallback);
    EXPECT_EQ(row.scoreFallbackReason, LoadFallback::WorkerSaturated);
  }
}

} // namespace
} // namespace executor_demo

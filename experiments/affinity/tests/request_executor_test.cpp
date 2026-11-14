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

Pool::Config config(Records* records = nullptr) {
  Pool::Config result;
  result.groups.resize(2);
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

} // namespace
} // namespace executor_demo

#include "TrackedCpuExecutor.h"

#include <atomic>
#include <chrono>
#include <stdexcept>
#include <thread>
#include <vector>

#include <gtest/gtest.h>
#include <folly/executors/task_queue/UnboundedBlockingQueue.h>
#include <folly/synchronization/Baton.h>

using load_accounting::TrackedCpuExecutor;
using Pool = folly::CPUThreadPoolExecutor;
using namespace std::chrono_literals;

namespace {
// Each gated test installs a later scope guard that posts all releases AND
// joins before any Gate or callback-captured object can be destroyed. Posting
// from Gate's destructor alone would allow workers to touch a destroyed Baton.
struct Gate {
  folly::Baton<> entered;
  folly::Baton<> release;
  void run() {
    entered.post();
    release.wait();
  }
  bool wait() { return entered.try_wait_for(2s); }
};

// Fault injection at the real Folly queue boundary, not a mock executor.
class RejectQueue : public folly::UnboundedBlockingQueue<Pool::CPUTask> {
 public:
  folly::BlockingQueueAddResult add(Pool::CPUTask&& task) override {
    if (task.func_) {
      throw folly::QueueFullException("test rejection before ownership transfer");
    }
    return folly::UnboundedBlockingQueue<Pool::CPUTask>::add(std::move(task));
  }
};

class ConsumeThenThrowQueue
    : public folly::UnboundedBlockingQueue<Pool::CPUTask> {
 public:
  folly::BlockingQueueAddResult add(Pool::CPUTask&& task) override {
    if (task.func_) {
      Pool::CPUTask owned(std::move(task));
      throw folly::QueueFullException("test rejection after move");
    }
    return folly::UnboundedBlockingQueue<Pool::CPUTask>::add(std::move(task));
  }
};
} // namespace

TEST(BusinessAccounting, CountsQueuedAndRunningUntilCallbackReturns) {
  TrackedCpuExecutor executor(1, true);
  Gate blocker;
  auto cleanup = folly::makeGuard([&] {
    blocker.release.post();
    executor.join();
  });
  executor.add([&] { blocker.run(); });
  ASSERT_TRUE(blocker.wait());
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 1);
  executor.add([] {});
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 2);
  blocker.release.post();
  executor.join();
  cleanup.dismiss();
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, RunningExceptionReleasesReservation) {
  TrackedCpuExecutor executor(1, true);
  executor.add([] { throw std::runtime_error("expected callback failure"); });
  executor.join();
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, QueueRefusalReleasesWithoutManualCatchDecrement) {
  auto pool = std::make_shared<Pool>(1, std::make_unique<RejectQueue>());
  TrackedCpuExecutor executor(pool, true);
  EXPECT_THROW(executor.add([] {}), folly::QueueFullException);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
  EXPECT_THROW(executor.add([] {}, 1ms, [] {}), folly::QueueFullException);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
  executor.join();
}

TEST(BusinessAccounting, MovedThenThrownTaskDoesNotDoubleRelease) {
  auto pool = std::make_shared<Pool>(1, std::make_unique<ConsumeThenThrowQueue>());
  TrackedCpuExecutor executor(pool, true);
  EXPECT_THROW(executor.add([] {}), folly::QueueFullException);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
  EXPECT_THROW(executor.add([] {}, 1ms, [] {}), folly::QueueFullException);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
  executor.join();
}

TEST(BusinessAccounting, ExpirationKeepsCountDuringExpiryCallback) {
  TrackedCpuExecutor executor(1, true);
  Gate blocker;
  Gate expiry;
  std::atomic<bool> ranNormal{false};
  auto cleanup = folly::makeGuard([&] {
    blocker.release.post();
    expiry.release.post();
    executor.join();
  });
  executor.add([&] { blocker.run(); });
  ASSERT_TRUE(blocker.wait());
  executor.add([&] { ranNormal = true; }, 1ms, [&] { expiry.run(); });
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 2);
  std::this_thread::sleep_for(5ms);
  blocker.release.post();
  ASSERT_TRUE(expiry.wait());
  // Folly has destroyed the normal callback by this point. N must remain 1.
  EXPECT_FALSE(ranNormal.load());
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 1);
  expiry.release.post();
  executor.join();
  cleanup.dismiss();
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, ExpirationWithoutUserCallbackAndExceptionBothRelease) {
  TrackedCpuExecutor executor(1, true);
  Gate blocker;
  std::atomic<unsigned> normal{0};
  std::atomic<unsigned> expired{0};
  auto cleanup = folly::makeGuard([&] {
    blocker.release.post();
    executor.join();
  });
  executor.add([&] { blocker.run(); });
  ASSERT_TRUE(blocker.wait());
  executor.add([&] { ++normal; }, 1ms);
  executor.add([&] { ++normal; }, 1ms, [&] {
    ++expired;
    throw std::runtime_error("expected expiry failure");
  });
  std::this_thread::sleep_for(5ms);
  blocker.release.post();
  executor.join();
  cleanup.dismiss();
  EXPECT_EQ(normal.load(), 0);
  EXPECT_EQ(expired.load(), 1);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, NonExpiredCallbackReleasesEvenWithUnusedExpiryOwner) {
  TrackedCpuExecutor executor(1, true);
  Gate normal;
  std::atomic<bool> expired{false};
  auto cleanup = folly::makeGuard([&] {
    normal.release.post();
    executor.join();
  });
  executor.add([&] { normal.run(); }, 1h, [&] { expired = true; });
  ASSERT_TRUE(normal.wait());
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 1);
  normal.release.post();
  executor.join();
  cleanup.dismiss();
  EXPECT_FALSE(expired.load());
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, BothPriorityEntrypointsAreCounted) {
  auto pool = std::make_shared<Pool>(1, int8_t{3});
  TrackedCpuExecutor executor(pool, true);
  Gate blocker;
  std::atomic<unsigned> completed{0};
  auto cleanup = folly::makeGuard([&] {
    blocker.release.post();
    executor.join();
  });
  executor.add([&] { blocker.run(); });
  ASSERT_TRUE(blocker.wait());
  executor.addWithPriority([&] { ++completed; }, 1);
  executor.add([&] { ++completed; }, -1, 1h, [] {});
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 3);
  blocker.release.post();
  executor.join();
  cleanup.dismiss();
  EXPECT_EQ(completed.load(), 2);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, UnexecutedQueueDestructionReleasesBothKindsOfTicket) {
  // Outlive the pool even if an unexpected exception bypasses explicit reset.
  std::atomic<bool> ran{false};
  auto pool = std::make_shared<Pool>(0);
  auto wrapper = std::make_unique<TrackedCpuExecutor>(pool, true);
  auto counter = wrapper->counterView();
  wrapper->add([&] { ran = true; });
  wrapper->add([&] { ran = true; }, 1ms, [&] { ran = true; });
  EXPECT_EQ(*counter.getOutstandingTaskCount(), 2);
  wrapper->stop();
  // stop is not a promise that queued objects have already been destroyed.
  EXPECT_EQ(*counter.getOutstandingTaskCount(), 2);
  wrapper.reset();
  EXPECT_EQ(*counter.getOutstandingTaskCount(), 2);
  pool.reset();
  EXPECT_FALSE(ran.load());
  EXPECT_EQ(*counter.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, CounterOutlivesFacadeWhileSharedPoolFinishesTask) {
  auto pool = std::make_shared<Pool>(1);
  auto wrapper = std::make_unique<TrackedCpuExecutor>(pool, true);
  auto counter = wrapper->counterView();
  Gate blocker;
  auto cleanup = folly::makeGuard([&] {
    blocker.release.post();
    pool->join();
  });
  wrapper->add([&] { blocker.run(); });
  ASSERT_TRUE(blocker.wait());
  wrapper.reset();
  EXPECT_EQ(*counter.getOutstandingTaskCount(), 1);
  blocker.release.post();
  pool->join();
  cleanup.dismiss();
  EXPECT_EQ(*counter.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, ConcurrentProducersDrainToZero) {
  TrackedCpuExecutor executor(4, true);
  std::atomic<unsigned> completed{0};
  std::vector<std::thread> producers;
  auto cleanup = folly::makeGuard([&] {
    // Also handles failure while constructing one of the producer threads.
    for (auto& producer : producers) {
      if (producer.joinable()) {
        producer.join();
      }
    }
    executor.join();
  });
  for (unsigned i = 0; i != 4; ++i) {
    producers.emplace_back([&] {
      for (unsigned j = 0; j != 100; ++j) {
        executor.add([&] { ++completed; });
      }
    });
  }
  for (auto& producer : producers) {
    producer.join();
  }
  executor.join();
  cleanup.dismiss();
  EXPECT_EQ(completed.load(), 400);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
}

TEST(BusinessAccounting, DisabledMeansUnavailableWhileCallbacksStillRun) {
  TrackedCpuExecutor executor(1); // Verify default-off, not only explicit false.
  std::atomic<unsigned> completed{0};
  auto cleanup = folly::makeGuard([&] { executor.join(); });
  EXPECT_FALSE(executor.getOutstandingTaskCount().hasValue());
  EXPECT_FALSE(executor.counterView().getOutstandingTaskCount().hasValue());
  executor.add([&] { ++completed; });
  executor.add([&] { ++completed; }, 1h, [] {});
  executor.join();
  cleanup.dismiss();
  EXPECT_EQ(completed.load(), 2);
  EXPECT_FALSE(executor.getOutstandingTaskCount().hasValue());
}

TEST(BusinessAccounting, EmptyUserFunctionsCannotBecomeHiddenPoisonOrWork) {
  TrackedCpuExecutor executor(1, true);
  EXPECT_THROW(executor.add(nullptr), std::invalid_argument);
  EXPECT_THROW(executor.addWithPriority(nullptr, 0), std::invalid_argument);
  EXPECT_THROW(executor.add(nullptr, 1ms, [] {}), std::invalid_argument);
  EXPECT_THROW(executor.add([] {}, -1ms), std::invalid_argument);
  EXPECT_EQ(*executor.getOutstandingTaskCount(), 0);
  executor.join();
}

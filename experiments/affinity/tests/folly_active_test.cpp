#include <atomic>
#include <chrono>
#include <condition_variable>
#include <executor_demo/FollyActiveLoadProvider.h>
#include <folly/ScopeGuard.h>
#include <future>
#include <gtest/gtest.h>
#include <mutex>
#include <thread>

namespace {
using namespace std::chrono_literals;

class Gate {
 public:
  void wait() {
    std::unique_lock lock(mutex_);
    changed_.wait(lock, [&] { return open_; });
  }
  void open() {
    std::lock_guard lock(mutex_);
    open_ = true;
    changed_.notify_all();
  }
 private:
  std::mutex mutex_;
  std::condition_variable changed_;
  bool open_{false};
};
} // namespace

TEST(FollyActiveProvider, ExistingPoolTracksBlockedCallbacksAndQueuedWork) {
  folly::CPUThreadPoolExecutor pool(std::make_pair(size_t(1), size_t(1)));
  executor_demo::FollyActiveLoadProvider provider({&pool}, {1.0});
  std::promise<void> started, release;
  auto permit = release.get_future().share();
  pool.add([&] {
    started.set_value();
    permit.wait();
  });
  started.get_future().wait();
  pool.add([] {});
  auto sample = provider.read(0);
  EXPECT_TRUE(sample.callbackInflightValid);
  EXPECT_EQ(sample.callbackInflight, 1);
  EXPECT_EQ(sample.queueDepth, 1);
  EXPECT_FALSE(sample.cpuValid);
  EXPECT_FALSE(sample.outstandingValid);
  executor_demo::ActiveLoadScorer score;
  EXPECT_EQ(score.score(sample, {}).value, 2);
  // A sleeping callback still counts active. This is deliberately not a
  // claim of runnable demand or current CPU consumption.
  EXPECT_THROW((executor_demo::FollyActiveLoadProvider({&pool}, {1.0})),
               std::invalid_argument);
  release.set_value();
  pool.join();
  EXPECT_EQ(provider.read(0).callbackInflight, 0);
}
TEST(FollyActiveProvider, CallbackExceptionRestoresInflightCount) {
  folly::CPUThreadPoolExecutor pool(std::make_pair(size_t(1), size_t(1)));
  {
    executor_demo::FollyActiveLoadProvider provider({&pool}, {1.0});
    EXPECT_FALSE(provider.read(8).queueValid);
    pool.add([] { throw std::runtime_error("expected test exception"); });
    pool.join();
    EXPECT_EQ(provider.read(0).callbackInflight, 0);
  }
  // Folly still owns an observer; its shared gauge is valid until destruction.
}

TEST(FollyActiveProvider, RunningAndNewCallbacksCompleteAfterProviderDestruction) {
  Gate release;
  folly::CPUThreadPoolExecutor pool(std::make_pair(size_t(1), size_t(1)));
  std::promise<void> started, completedAfterDestruction;
  std::atomic<size_t> completed{0};
  auto cleanup = folly::makeGuard([&] { release.open(); pool.join(); });
  {
    executor_demo::FollyActiveLoadProvider provider({&pool}, {1.0});
    pool.add([&] {
      started.set_value();
      release.wait();
      ++completed;
    });
    ASSERT_EQ(started.get_future().wait_for(3s), std::future_status::ready);
    EXPECT_EQ(provider.read(0).callbackInflight, 1);
  } // Destroy the provider while taskProcessed() is still pending.

  // New dequeue/processed notifications also reach the observer retained by
  // Folly after its provider has gone away; no dangling provider is read here.
  pool.add([&] {
    ++completed;
    throw std::runtime_error("expected exception after provider destruction");
  });
  pool.add([&] {
    ++completed;
    completedAfterDestruction.set_value();
  });
  release.open();
  EXPECT_EQ(completedAfterDestruction.get_future().wait_for(3s),
            std::future_status::ready);
  pool.join();
  cleanup.dismiss();
  EXPECT_EQ(completed, 3);
}

TEST(FollyActiveProvider, ExpiredTaskAndThrowingExpiryCallbackRestoreInflightCount) {
  for (bool throwOnExpiry : {false, true}) {
    SCOPED_TRACE(throwOnExpiry);
    Gate release;
    folly::CPUThreadPoolExecutor pool(std::make_pair(size_t(1), size_t(1)));
    executor_demo::FollyActiveLoadProvider provider({&pool}, {1.0});
    std::promise<void> started;
    std::atomic<size_t> bodyCalls{0}, expiryCalls{0};
    auto cleanup = folly::makeGuard([&] { release.open(); pool.join(); });
    pool.add([&] {
      started.set_value();
      release.wait();
    });
    ASSERT_EQ(started.get_future().wait_for(3s), std::future_status::ready);
    pool.add([&] { ++bodyCalls; }, 1ms, [&] {
      ++expiryCalls;
      // Dequeued fires before Folly chooses the expiration branch, even when
      // the original callback never starts; processed must release this count.
      EXPECT_EQ(provider.read(0).callbackInflight, 1);
      if (throwOnExpiry)
        throw std::runtime_error("expected expiry callback exception");
    });
    EXPECT_EQ(provider.read(0).queueDepth, 1);
    EXPECT_EQ(provider.read(0).callbackInflight, 1);
    // The sole worker cannot take the task until we release it. Starting this
    // short wait after add() guarantees its 1 ms expiration has elapsed.
    std::this_thread::sleep_until(std::chrono::steady_clock::now() + 2ms);
    release.open();
    pool.join();
    cleanup.dismiss();
    EXPECT_EQ(bodyCalls, 0);
    EXPECT_EQ(expiryCalls, 1);
    EXPECT_EQ(provider.read(0).callbackInflight, 0);
    EXPECT_EQ(provider.read(0).queueDepth, 0);
  }
}

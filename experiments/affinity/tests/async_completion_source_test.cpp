#include <executor_demo/AsyncCompletionSource.h>
#include <executor_demo/RequestExecutor.h>
#include <folly/portability/GTest.h>

#include <atomic>
#include <future>
#include <functional>
#include <memory>
#include <vector>

namespace executor_demo {
namespace {
using Source = AsyncCompletionSource;
using namespace std::chrono_literals;

class Gate {
 public:
  void wait() {
    std::unique_lock lock(mutex_);
    cv_.wait(lock, [&] { return open_; });
  }
  void open() {
    std::lock_guard lock(mutex_);
    open_ = true;
    cv_.notify_all();
  }
 private:
  std::mutex mutex_;
  std::condition_variable cv_;
  bool open_{false};
};

bool waitUntil(const std::function<bool()>& condition) {
  const auto end = Source::Clock::now() + 3s;
  while (Source::Clock::now() < end) {
    if (condition()) return true;
    std::this_thread::yield();
  }
  return condition();
}

void expectConserved(const Source::Stats& stats) {
  EXPECT_EQ(stats.registered, stats.fired + stats.canceled + stats.pending);
  EXPECT_EQ(stats.pending, 0);
  EXPECT_TRUE(stats.helperCpuValid);
}

TEST(AsyncCompletionSource, DueOrderAndEqualDeadlineRegistrationOrder) {
  Source source(2, 8);
  Gate release;
  std::promise<void> entered;
  std::vector<int> order;
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] {
    entered.set_value(); release.wait();
  }, [] {}));
  EXPECT_EQ(entered.get_future().wait_for(3s), std::future_status::ready);
  const auto due = Source::Clock::now() - 1s;
  for (const auto [offset, value] : std::vector<std::pair<int, int>>{{2, 2}, {1, 1}, {2, 3}}) {
    EXPECT_TRUE(source.schedule(1, due + std::chrono::milliseconds(offset),
                               [&, value] { order.push_back(value); }, [] {}));
  }
  release.open();
  source.stopAfterDrain();
  EXPECT_EQ(order, (std::vector<int>{1, 2, 3}));
  const auto stats = source.stats();
  EXPECT_EQ(stats.registered, 4);
  EXPECT_EQ(stats.fired, 4);
  EXPECT_EQ(stats.peakPending, 4);
  expectConserved(stats);
}

TEST(AsyncCompletionSource, CapacityIncludesExecutingCallbackAndRejectedCapturesRelease) {
  Source source(2, 2);
  Gate release;
  std::promise<void> entered;
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] {
    entered.set_value(); release.wait();
  }, [] {}));
  EXPECT_EQ(entered.get_future().wait_for(3s), std::future_status::ready);
  EXPECT_TRUE(source.schedule(1, Source::Clock::now() + 1h, [] {}, [] {}));
  auto marker = std::make_shared<int>(7);
  std::weak_ptr<int> weak = marker;
  EXPECT_FALSE(source.schedule(1, Source::Clock::now(), [owned = std::move(marker)] {}, [] {}));
  EXPECT_TRUE(weak.expired());
  EXPECT_EQ(source.stats().pending, 2);
  source.cancelRequest(1);
  release.open();
  source.stopAfterDrain();
  const auto stats = source.stats();
  EXPECT_EQ(stats.rejected, 1);
  EXPECT_EQ(stats.fired, 1);
  EXPECT_EQ(stats.canceled, 1);
  expectConserved(stats);
}

TEST(AsyncCompletionSource, CancellationWinsBeforeDispatchAndIsSticky) {
  Source source(3, 8);
  Gate release;
  std::promise<void> entered;
  std::atomic<unsigned> fired{0}, canceled{0};
  const auto caller = std::this_thread::get_id();
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] {
    entered.set_value(); release.wait();
  }, [] {}));
  EXPECT_EQ(entered.get_future().wait_for(3s), std::future_status::ready);
  for (int i = 0; i < 3; ++i) {
    EXPECT_TRUE(source.schedule(1, Source::Clock::now(), [&] { ++fired; }, [&] {
      EXPECT_NE(std::this_thread::get_id(), caller);
      ++canceled;
      source.cancelRequest(1); // Reentrant calls do not run under source mutex.
      EXPECT_EQ(source.stats().registered, 4);
    }));
  }
  source.cancelRequest(1);
  source.cancelRequest(1);
  source.cancelRequest(2); // Empty requests still become permanently canceled.
  EXPECT_FALSE(source.schedule(1, Source::Clock::now(), [] {}, [] {}));
  EXPECT_FALSE(source.schedule(2, Source::Clock::now(), [] {}, [] {}));
  release.open();
  source.stopAfterDrain();
  EXPECT_EQ(fired, 0);
  EXPECT_EQ(canceled, 3);
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, DispatchWinsBeforeCancellationWithoutDoubleCallback) {
  Source source(1, 2);
  Gate release;
  std::promise<void> entered;
  std::atomic<unsigned> fired{0}, canceled{0};
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] {
    ++fired; entered.set_value(); release.wait();
  }, [&] { ++canceled; }));
  EXPECT_EQ(entered.get_future().wait_for(3s), std::future_status::ready);
  source.cancelRequest(0);
  EXPECT_FALSE(source.schedule(0, Source::Clock::now(), [] {}, [] {}));
  release.open();
  source.stopAfterDrain();
  EXPECT_EQ(fired, 1);
  EXPECT_EQ(canceled, 0);
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, EmptyOpenSourceAcceptsLaterRegistration) {
  Source source(1, 2);
  std::atomic<unsigned> fired{0};
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] { ++fired; }, [] {}));
  EXPECT_TRUE(waitUntil([&] { return source.stats().pending == 0; }));
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] { ++fired; }, [] {}));
  source.stopAfterDrain();
  EXPECT_EQ(fired, 2);
  EXPECT_FALSE(source.schedule(0, Source::Clock::now(), [] {}, [] {}));
  source.stopAfterDrain();
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, EarlierRegistrationWakesTimerAndNeverFiresBeforeDue) {
  Source source(2, 3);
  std::promise<Source::Clock::time_point> fired;
  auto result = fired.get_future();
  EXPECT_TRUE(source.schedule(0, Source::Clock::now() + 1h,
                             [] { ADD_FAILURE() << "distant event fired"; }, [] {}));
  const auto due = Source::Clock::now() + 5ms;
  EXPECT_TRUE(source.schedule(1, due, [&] { fired.set_value(Source::Clock::now()); }, [] {}));
  const auto status = result.wait_for(3s);
  EXPECT_EQ(status, std::future_status::ready);
  if (status == std::future_status::ready) EXPECT_GE(result.get(), due);
  source.cancelRequest(0);
  // Abort prevents a test hang if an earlier event incorrectly stayed pending.
  if (status != std::future_status::ready) source.abort();
  else source.stopAfterDrain();
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, AbortCancelsDistantEventsAndCanEscalateGracefulDrain) {
  Source source(2, 2);
  std::atomic<unsigned> fired{0}, canceled{0};
  EXPECT_TRUE(source.schedule(0, Source::Clock::now() + 1h,
                             [&] { ++fired; }, [&] { ++canceled; }));
  auto draining = std::async(std::launch::async, [&] { source.stopAfterDrain(); });
  EXPECT_EQ(draining.wait_for(5ms), std::future_status::timeout);
  source.abort();
  EXPECT_EQ(draining.wait_for(3s), std::future_status::ready);
  draining.get();
  source.abort();
  EXPECT_FALSE(source.schedule(1, Source::Clock::now(), [] {}, [] {}));
  EXPECT_EQ(fired, 0);
  EXPECT_EQ(canceled, 1);
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, AbortWaitsForAlreadyDispatchedFire) {
  Source source(2, 3);
  Gate release;
  std::promise<void> entered;
  std::atomic<unsigned> fired{0}, canceled{0};
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&] {
    ++fired; entered.set_value(); release.wait();
  }, [&] { ++canceled; }));
  EXPECT_EQ(entered.get_future().wait_for(3s), std::future_status::ready);
  EXPECT_TRUE(source.schedule(1, Source::Clock::now() + 1h, [&] { ++fired; }, [&] { ++canceled; }));
  auto aborting = std::async(std::launch::async, [&] { source.abort(); });
  EXPECT_EQ(aborting.wait_for(5ms), std::future_status::timeout);
  release.open();
  EXPECT_EQ(aborting.wait_for(3s), std::future_status::ready);
  aborting.get();
  EXPECT_EQ(fired, 1);
  EXPECT_EQ(canceled, 1);
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, CallbackFailuresContainedAndOwnThreadJoinRejected) {
  Source source(3, 8);
  EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [] {
    throw std::runtime_error("expected fire failure");
  }, [] {}));
  EXPECT_TRUE(source.schedule(1, Source::Clock::now() + 1h, [] {}, [] {
    throw std::runtime_error("expected cancel failure");
  }));
  source.cancelRequest(1);
  EXPECT_TRUE(source.schedule(2, Source::Clock::now(), [&] {
    EXPECT_THROW(source.stopAfterDrain(), std::logic_error);
    EXPECT_THROW(source.abort(), std::logic_error);
    EXPECT_TRUE(source.schedule(2, Source::Clock::now(), [] {}, [] {}));
  }, [] {}));
  EXPECT_TRUE(waitUntil([&] { return source.stats().pending == 0; }));
  source.stopAfterDrain();
  const auto stats = source.stats();
  EXPECT_EQ(stats.callbackFailures, 2);
  EXPECT_EQ(stats.fired, 3);
  EXPECT_EQ(stats.canceled, 1);
  expectConserved(stats);
}

TEST(AsyncCompletionSource, RejectsInvalidConfigurationAndRequestWithoutCallingCallbacks) {
  EXPECT_THROW(Source(0, 1), std::invalid_argument);
  EXPECT_THROW(Source(1, 0), std::invalid_argument);
  Source source(1, 1);
  std::atomic<unsigned> called{0};
  EXPECT_FALSE(source.schedule(1, Source::Clock::now(), [&] { ++called; }, [&] { ++called; }));
  EXPECT_THROW(source.cancelRequest(1), std::out_of_range);
  EXPECT_THROW(source.schedule(0, Source::Clock::now(), folly::Func{}, [] {}), std::invalid_argument);
  source.stopAfterDrain();
  EXPECT_EQ(called, 0);
  EXPECT_EQ(source.stats().rejected, 1);
  expectConserved(source.stats());
}

RequestExecutor::Config poolConfig() {
  RequestExecutor::Config config;
  config.groups.resize(1);
  config.groups[0].threads = 1;
  config.groups[0].capacity = 1;
  config.groups[0].numaNode = 0;
  return config;
}

TEST(AsyncCompletionSource, WaitingContinuationLeavesPoolIdleButKeepsDrainAndCanWaitAgain) {
  RequestExecutor pool(poolConfig());
  Source source(2, 8);
  Gate releaseSource, releaseSecond;
  std::promise<void> sourceEntered, registered, secondEntered;
  EXPECT_TRUE(source.schedule(1, Source::Clock::now(), [&] {
    sourceEntered.set_value(); releaseSource.wait();
  }, [] {}));
  EXPECT_EQ(sourceEntered.get_future().wait_for(3s), std::future_status::ready);
  auto marker = std::make_shared<int>(9);
  std::weak_ptr<int> weak = marker;
  std::shared_ptr<folly::RequestContext> expectedContext;
  std::atomic<bool> resumed{false}, restored{false}, joined{false};
  {
    folly::RequestContextScopeGuard context;
    expectedContext = folly::RequestContext::saveContext();
    EXPECT_TRUE(pool.add(pool.beginRequest(0), [&, owned = std::move(marker)] {
      auto token = std::make_shared<RequestExecutor::Continuation>(pool.captureContinuation());
      EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&, token, owned] {
        EXPECT_TRUE(token->resume([&, owned] {
          restored = folly::RequestContext::saveContext() == expectedContext;
          // The timer queue is empty while this resumed executor callback is
          // alive; a later registration must still be accepted during drain.
          secondEntered.set_value();
          releaseSecond.wait();
          auto next = std::make_shared<RequestExecutor::Continuation>(pool.captureContinuation());
          EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&, next, owned] {
            EXPECT_TRUE(next->resume([&, owned] { resumed = true; }));
          }, [next] { next->cancel(); }));
        }));
      }, [token] { token->cancel(); }));
      registered.set_value();
    }));
  }
  EXPECT_EQ(registered.get_future().wait_for(3s), std::future_status::ready);
  EXPECT_TRUE(waitUntil([&] {
    const auto load = pool.readLoadSample(0);
    return load.queueDepth == 0 && load.outstanding == 0 && load.callbackInflight == 0;
  }));
  EXPECT_FALSE(weak.expired());
  std::thread joining([&] { pool.join(); joined = true; });
  EXPECT_TRUE(waitUntil([&] { return !pool.acceptingRequests(); }));
  EXPECT_FALSE(joined.load());
  releaseSource.open();
  EXPECT_EQ(secondEntered.get_future().wait_for(3s), std::future_status::ready);
  EXPECT_TRUE(waitUntil([&] { return source.stats().pending == 0; }));
  EXPECT_FALSE(joined.load());
  releaseSecond.open();
  joining.join();
  source.stopAfterDrain();
  EXPECT_TRUE(restored.load());
  EXPECT_TRUE(resumed.load());
  EXPECT_TRUE(weak.expired());
  EXPECT_EQ(source.stats().fired, 3);
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, CancelingWaitReleasesContinuationAndRequestCaptures) {
  RequestExecutor pool(poolConfig());
  Source source(1, 2);
  auto marker = std::make_shared<int>(9);
  std::weak_ptr<int> weak = marker;
  std::promise<void> registered;
  std::atomic<bool> fired{false}, joined{false};
  EXPECT_TRUE(pool.add(pool.beginRequest(0), [&, owned = std::move(marker)] {
    auto token = std::make_shared<RequestExecutor::Continuation>(pool.captureContinuation());
    EXPECT_TRUE(source.schedule(0, Source::Clock::now() + 1h, [&, token, owned] {
      fired = true;
      token->resume([] {});
    }, [token, owned] { token->cancel(); }));
    registered.set_value();
  }));
  EXPECT_EQ(registered.get_future().wait_for(3s), std::future_status::ready);
  std::thread joining([&] { pool.join(); joined = true; });
  EXPECT_TRUE(waitUntil([&] { return !pool.acceptingRequests(); }));
  EXPECT_FALSE(joined.load());
  EXPECT_FALSE(weak.expired());
  source.cancelRequest(0);
  joining.join();
  source.stopAfterDrain();
  EXPECT_FALSE(fired.load());
  EXPECT_TRUE(weak.expired());
  const auto load = pool.readLoadSample(0);
  EXPECT_EQ(load.queueDepth, 0);
  EXPECT_EQ(load.outstanding, 0);
  EXPECT_EQ(load.callbackInflight, 0);
  EXPECT_EQ(source.stats().canceled, 1);
  expectConserved(source.stats());
}

TEST(AsyncCompletionSource, RejectedResumeReleasesLeaseWhileAnotherCallbackOccupiesCapacity) {
  auto config = poolConfig();
  config.maxOutstanding = 1;
  RequestExecutor pool(config);
  Source source(2, 4);
  Gate releaseSource, releaseWorker;
  std::promise<void> sourceEntered, registered, workerEntered;
  std::promise<bool> resumed;
  auto resumedResult = resumed.get_future();
  EXPECT_TRUE(source.schedule(1, Source::Clock::now(), [&] {
    sourceEntered.set_value(); releaseSource.wait();
  }, [] {}));
  EXPECT_EQ(sourceEntered.get_future().wait_for(3s), std::future_status::ready);
  auto marker = std::make_shared<int>(4);
  std::weak_ptr<int> weak = marker;
  EXPECT_TRUE(pool.add(pool.beginRequest(0), [&, owned = std::move(marker)] {
    auto token = std::make_shared<RequestExecutor::Continuation>(pool.captureContinuation());
    EXPECT_TRUE(source.schedule(0, Source::Clock::now(), [&, token, owned] {
      resumed.set_value(token->resume([] { ADD_FAILURE() << "rejected resume ran"; }));
      EXPECT_FALSE(token->resume([] {}));
    }, [token, owned] { token->cancel(); }));
    registered.set_value();
  }));
  EXPECT_EQ(registered.get_future().wait_for(3s), std::future_status::ready);
  EXPECT_TRUE(waitUntil([&] { return pool.readLoadSample(0).outstanding == 0; }));
  EXPECT_TRUE(pool.add(pool.beginRequest(1), [&] {
    workerEntered.set_value(); releaseWorker.wait();
  }));
  EXPECT_EQ(workerEntered.get_future().wait_for(3s), std::future_status::ready);
  EXPECT_EQ(pool.readLoadSample(0).outstanding, 1);
  std::atomic<bool> joined{false};
  std::thread joining([&] { pool.join(); joined = true; });
  EXPECT_TRUE(waitUntil([&] { return !pool.acceptingRequests(); }));
  releaseSource.open();
  const auto status = resumedResult.wait_for(3s);
  EXPECT_EQ(status, std::future_status::ready);
  if (status == std::future_status::ready) EXPECT_FALSE(resumedResult.get());
  EXPECT_TRUE(waitUntil([&] { return source.stats().pending == 0; }));
  EXPECT_TRUE(weak.expired());
  EXPECT_FALSE(joined.load());
  releaseWorker.open();
  joining.join();
  source.stopAfterDrain();
  const auto load = pool.readLoadSample(0);
  EXPECT_EQ(load.queueDepth, 0);
  EXPECT_EQ(load.outstanding, 0);
  EXPECT_EQ(load.callbackInflight, 0);
  EXPECT_EQ(source.stats().fired, 2);
  expectConserved(source.stats());
}

} // namespace
} // namespace executor_demo

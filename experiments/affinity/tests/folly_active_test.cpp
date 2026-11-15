#include <condition_variable>
#include <executor_demo/FollyActiveLoadProvider.h>
#include <future>
#include <gtest/gtest.h>

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
TEST(FollyActiveProvider, ObserverStateSurvivesProviderAndCallbackException) {
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

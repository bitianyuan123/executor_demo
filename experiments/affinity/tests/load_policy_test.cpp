#include <executor_demo/LoadPolicy.h>
#include <gtest/gtest.h>

#include <cmath>
#include <limits>

namespace executor_demo {
namespace {

LoadSample counters() {
  LoadSample sample;
  sample.queueDepth = 4;
  sample.outstanding = 12;
  sample.callbackInflight = 6;
  sample.estimatedOutstandingCpuUs = 12000;
  sample.capacity = 4;
  sample.queueValid = sample.outstandingValid = sample.callbackInflightValid = sample.workValid = true;
  return sample;
}
LoadSample cpuSample() {
  auto sample = counters();
  sample.sampledCpuCores = 4;
  sample.sampledRunqueueWaitCores = 8;
  sample.cpuValid = sample.runqueueValid = true;
  sample.sampledAtNs = 1000;
  sample.sampleWindowNs = 100;
  return sample;
}

TEST(LoadPolicy, ExistingMetricsKeepUnitsAndReservations) {
  const auto sample = counters();
  const ScoreContext context{4, 4000, 0};
  EXPECT_DOUBLE_EQ(QueueLoadScorer().score(sample, context).value, 2);
  EXPECT_DOUBLE_EQ(OutstandingLoadScorer().score(sample, context).value, 4);
  EXPECT_DOUBLE_EQ(WorkLoadScorer().score(sample, context).value, 4000);
  EXPECT_EQ(QueueLoadScorer().needs(), LoadInputs::QueueOnly);
  EXPECT_EQ(OutstandingLoadScorer().needs(), LoadInputs::Counters);
}

TEST(LoadPolicy, ActiveDoesNotAddOutstandingAgainAndExposesHandoffGap) {
  auto sample = counters();
  const auto active = ActiveLoadScorer().score(sample, ScoreContext{2, 0, 0});
  EXPECT_DOUBLE_EQ(active.value, 3); // (4 queued + 6 callback inflight + 2 reserved) / 4
  sample.queueDepth = sample.callbackInflight = 0;
  sample.outstanding = 4;
  EXPECT_DOUBLE_EQ(ActiveLoadScorer().score(sample, {}).value, 0);
  EXPECT_DOUBLE_EQ(OutstandingLoadScorer().score(sample, {}).value, 1);
  // An O=4,Q=0,I=0 handoff snapshot is not an idle-pool proof.
}

TEST(LoadPolicy, BlockedCallbackCounterexampleDoesNotBecomeCpuRunnableFact) {
  auto sample = cpuSample();
  sample.queueDepth = 0;
  sample.outstanding = sample.callbackInflight = 16;
  sample.sampledCpuCores = sample.sampledRunqueueWaitCores = 0;
  sample.capacity = 8;
  EXPECT_DOUBLE_EQ(ActiveLoadScorer().score(sample, {}).value, 2);
  const auto cpu = CpuHybridLoadScorer(100).score(sample, {0, 0, 1050});
  EXPECT_FALSE(cpu.fallback);
  EXPECT_DOUBLE_EQ(cpu.value, 0);
  // This could describe callbacks waiting synchronously. CPU demand is low;
  // it does not imply worker threads are available to execute another callback.
}

TEST(LoadPolicy, RunqueueDemandCanExposePressureWithEmptyFollyQueue) {
  auto sample = cpuSample();
  sample.queueDepth = 0;
  sample.sampledCpuCores = 4;
  sample.sampledRunqueueWaitCores = 12;
  const auto score = CpuHybridLoadScorer(100).score(sample, {0, 0, 1050});
  EXPECT_FALSE(score.fallback);
  EXPECT_DOUBLE_EQ(score.value, 4); // Average runnable demand 16, capacity 4.
}

TEST(LoadPolicy, FreshLowCpuCannotHideExhaustedWorkers) {
  auto sample = cpuSample();
  sample.queueDepth = 0;
  sample.sampledCpuCores = sample.sampledRunqueueWaitCores = 0;
  sample.capacity = 16;
  sample.workerCount = 128;
  sample.callbackInflight = sample.outstanding = 128;
  auto score = CpuHybridLoadScorer(100).score(sample, {0, 0, 1050});
  EXPECT_TRUE(score.fallback);
  EXPECT_EQ(score.reason, LoadFallback::WorkerSaturated);
  EXPECT_DOUBLE_EQ(score.value, 8);
  sample.callbackInflight = sample.outstanding = 80;
  score = CpuHybridLoadScorer(100).score(sample, {0, 0, 1050});
  EXPECT_FALSE(score.fallback);
  EXPECT_DOUBLE_EQ(score.value, 0); // Forty-eight workers remain available.
  sample.callbackInflight = sample.outstanding = 128;
  sample.workerCount = 0;
  score = CpuHybridLoadScorer(100).score(sample, {0, 0, 1050});
  EXPECT_FALSE(score.fallback); // Unknown worker count is not an invented limit.
}

TEST(LoadPolicy, CpuUsesMaximumAcrossDifferentObservationWindows) {
  auto sample = cpuSample();
  sample.queueDepth = 20;
  auto score = CpuHybridLoadScorer(100).score(sample, {4, 0, 1050});
  EXPECT_DOUBLE_EQ(score.value, 6); // max(queue+reservations=24, sampled demand=12)/4
  sample.runnableValid = true;
  sample.sampledRunnable = 32;
  score = CpuHybridLoadScorer(100).score(sample, {4, 0, 1050});
  EXPECT_DOUBLE_EQ(score.value, 8); // Do not add runnable again to runtime+wait.
}

TEST(LoadPolicy, StaleMissingOrUnavailableCpuFallsBackToOutstanding) {
  auto sample = cpuSample();
  const ScoreContext context{4, 0, 1101};
  auto score = CpuHybridLoadScorer(100).score(sample, context);
  EXPECT_DOUBLE_EQ(score.value, 4);
  EXPECT_TRUE(score.fallback);
  EXPECT_EQ(score.reason, LoadFallback::StaleCpu);
  sample.sampledAtNs = 1100;
  sample.cpuValid = false;
  score = CpuHybridLoadScorer(100).score(sample, context);
  EXPECT_EQ(score.reason, LoadFallback::MissingCpu);
  EXPECT_DOUBLE_EQ(score.value, 4);
  sample.cpuValid = true; sample.runqueueValid = false;
  sample.sampledRunqueueWaitCores = 0; // Disabled schedstats is not measured zero wait.
  score = CpuHybridLoadScorer(100).score(sample, context);
  EXPECT_EQ(score.reason, LoadFallback::MissingRunqueue);
  EXPECT_DOUBLE_EQ(score.value, 4);
}

TEST(LoadPolicy, InvalidTimesAndNonfiniteCpuCannotAppearIdle) {
  auto sample = cpuSample();
  sample.sampledAtNs = 0;
  EXPECT_EQ(CpuHybridLoadScorer(100).score(sample, {0, 0, 1050}).reason, LoadFallback::StaleCpu);
  sample.sampledAtNs = 1051;
  EXPECT_EQ(CpuHybridLoadScorer(100).score(sample, {0, 0, 1050}).reason, LoadFallback::StaleCpu);
  sample.sampledAtNs = 1000; sample.sampleWindowNs = 0;
  EXPECT_EQ(CpuHybridLoadScorer(100).score(sample, {0, 0, 1050}).reason, LoadFallback::StaleCpu);
  sample.sampleWindowNs = 100;
  for (double value : {-1.0, std::numeric_limits<double>::quiet_NaN(),
                       std::numeric_limits<double>::infinity()}) {
    sample.sampledCpuCores = value;
    auto score = CpuHybridLoadScorer(100).score(sample, {0, 0, 1050});
    EXPECT_EQ(score.reason, LoadFallback::InvalidCpu);
    EXPECT_DOUBLE_EQ(score.value, 3);
  }
}

TEST(LoadPolicy, MissingCountersAndWorkUnitsAreHandledExplicitly) {
  auto sample = counters();
  sample.callbackInflightValid = false;
  auto score = ActiveLoadScorer().score(sample, {});
  EXPECT_EQ(score.reason, LoadFallback::MissingCounter);
  EXPECT_DOUBLE_EQ(score.value, 3);
  sample.workValid = false;
  score = WorkLoadScorer().score(sample, {});
  EXPECT_TRUE(std::isinf(score.value)); // Counts cannot substitute for microseconds.
  sample.outstandingValid = false;
  score = OutstandingLoadScorer().score(sample, {});
  EXPECT_TRUE(std::isinf(score.value));
}

TEST(LoadPolicy, CapacityAndLargeCountsDoNotSilentlyWrapOrDivideByZero) {
  auto sample = counters();
  for (double capacity : {0.0, -1.0, std::numeric_limits<double>::infinity()}) {
    sample.capacity = capacity;
    auto score = ActiveLoadScorer().score(sample, {});
    EXPECT_EQ(score.reason, LoadFallback::InvalidCapacity);
    EXPECT_TRUE(std::isinf(score.value));
  }
  sample.capacity = 1;
  sample.queueDepth = sample.callbackInflight = std::numeric_limits<uint64_t>::max();
  EXPECT_GT(ActiveLoadScorer().score(sample, {}).value,
            double(std::numeric_limits<uint64_t>::max()));
  EXPECT_THROW(CpuHybridLoadScorer scorer(0), std::invalid_argument);
}

} // namespace
} // namespace executor_demo

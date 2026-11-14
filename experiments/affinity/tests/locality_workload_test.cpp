#include <executor_demo/LocalityWorkload.h>

#include <gtest/gtest.h>

#include <algorithm>
#include <cerrno>
#include <limits>
#include <thread>
#include <vector>

namespace executor_demo {
namespace {

using Workload = LocalityWorkload;

Workload::Config config(Workload::Model model) {
  Workload::Config result;
  result.model = model;
  result.branches = 4;
  result.seed = 123;
  result.requestId = 42;
  result.maxBytes = 1024 * 1024;
  if (model == Workload::Model::RequestReadOnly || model == Workload::Model::Mixed)
    result.sharedBytes = 4096;
  if (model == Workload::Model::BranchReadWrite || model == Workload::Model::Mixed)
    result.branchBytes = 8192;
  return result;
}

TEST(LocalityWorkload, InitializationAndLogicalStagesAreExplicit) {
  Workload work(config(Workload::Model::Mixed));
  EXPECT_FALSE(work.initialized());
  EXPECT_THROW(work.run(0, 0, 100, 100), std::logic_error);
  work.initialize();
  EXPECT_TRUE(work.initialized());
  EXPECT_THROW(work.initialize(), std::logic_error);
  const auto result = work.run(0, 0, 100, 100);
  EXPECT_EQ(result.computeIterations, 100);
  EXPECT_EQ(result.readWords, 200);
  EXPECT_EQ(result.writtenWords, 100);
  EXPECT_EQ(result.checksum, work.branchChecksum(0));
  EXPECT_THROW(work.run(0, 0, 100, 100), std::invalid_argument);
  EXPECT_THROW(work.run(0, 2, 100, 100), std::invalid_argument);
  EXPECT_THROW(work.run(4, 0, 100, 100), std::out_of_range);
  work.run(0, 1, 100, 100);
  EXPECT_EQ(work.completedStages(0), 2);
}

TEST(LocalityWorkload, BranchInterleavingAndConcurrentExecutionPreserveActualResults) {
  for (auto model : {Workload::Model::None, Workload::Model::RequestReadOnly,
                     Workload::Model::BranchReadWrite, Workload::Model::Mixed}) {
    Workload serial(config(model)), interleaved(config(model)), parallel(config(model));
    serial.initialize(); interleaved.initialize(); parallel.initialize();
    const uint64_t accesses = model == Workload::Model::None ? 0 : 3000;
    for (size_t branch = 0; branch < 4; ++branch)
      for (size_t stage = 0; stage < 4; ++stage)
        serial.run(branch, stage, 300 + stage, accesses + (accesses ? stage : 0));
    for (size_t stage = 0; stage < 4; ++stage)
      for (size_t branch = 4; branch-- > 0;)
        interleaved.run(branch, stage, 300 + stage, accesses + (accesses ? stage : 0));
    std::vector<std::thread> workers;
    for (size_t branch = 0; branch < 4; ++branch)
      workers.emplace_back([&, branch] {
        for (size_t stage = 0; stage < 4; ++stage)
          parallel.run(branch, stage, 300 + stage, accesses + (accesses ? stage : 0));
      });
    for (auto& worker : workers) worker.join();
    EXPECT_EQ(serial.finalChecksum(), interleaved.finalChecksum());
    EXPECT_EQ(serial.finalChecksum(), parallel.finalChecksum());
    for (size_t branch = 0; branch < 4; ++branch) {
      EXPECT_EQ(serial.branchChecksum(branch), parallel.branchChecksum(branch));
      EXPECT_EQ(parallel.completedStages(branch), 4);
    }
  }
}

TEST(LocalityWorkload, OtherBranchWritesDoNotChangeAChainAndPredecessorWorkDoes) {
  Workload first(config(Workload::Model::BranchReadWrite));
  Workload second(config(Workload::Model::BranchReadWrite));
  Workload changed(config(Workload::Model::BranchReadWrite));
  first.initialize(); second.initialize(); changed.initialize();
  first.run(0, 0, 500, 1000);
  second.run(1, 0, 50000, 100000);
  second.run(0, 0, 500, 1000);
  changed.run(0, 0, 500, 1001);
  EXPECT_EQ(first.run(0, 1, 500, 1000).checksum, second.run(0, 1, 500, 1000).checksum);
  EXPECT_NE(first.branchChecksum(0), changed.run(0, 1, 500, 1000).checksum);
}

TEST(LocalityWorkload, ModelAccessCountsAreExplicitAndNoMemoryModelRejectsAccesses) {
  for (auto model : {Workload::Model::None, Workload::Model::RequestReadOnly,
                     Workload::Model::BranchReadWrite, Workload::Model::Mixed}) {
    Workload work(config(model));
    work.initialize();
    if (model == Workload::Model::None) {
      EXPECT_THROW(work.run(0, 0, 10, 1), std::invalid_argument);
      EXPECT_EQ(work.run(0, 0, 10, 0).readWords, 0);
      EXPECT_EQ(work.allocatedBytes(), 0);
    } else {
      auto result = work.run(0, 0, 10, 25);
      EXPECT_EQ(result.readWords, model == Workload::Model::Mixed ? 50 : 25);
      EXPECT_EQ(result.writtenWords, model == Workload::Model::RequestReadOnly ? 0 : 25);
    }
  }
}

TEST(LocalityWorkload, BoundsRejectBeforeMappingOrExecuting) {
  auto cfg = config(Workload::Model::Mixed);
  cfg.maxBytes = 4096;
  EXPECT_THROW(Workload{cfg}, std::invalid_argument);
  cfg = config(Workload::Model::BranchReadWrite);
  cfg.branches = std::numeric_limits<size_t>::max();
  EXPECT_THROW(Workload{cfg}, std::overflow_error);
  cfg = config(Workload::Model::RequestReadOnly);
  cfg.sharedBytes = std::numeric_limits<size_t>::max() - 7;
  EXPECT_THROW(Workload{cfg}, std::overflow_error);
  cfg = config(Workload::Model::Mixed);
  cfg.branchBytes = 3;
  EXPECT_THROW(Workload{cfg}, std::invalid_argument);
  cfg = config(Workload::Model::None);
  cfg.sharedBytes = 8;
  EXPECT_THROW(Workload{cfg}, std::invalid_argument);
  Workload work(config(Workload::Model::Mixed));
  work.initialize();
  EXPECT_THROW(work.run(0, 0, 1, std::numeric_limits<uint64_t>::max()), std::overflow_error);
  EXPECT_EQ(work.completedStages(0), 0);
}

long deniedQuery(unsigned long, void**, int*) { errno = EPERM; return -1; }
long partialQuery(unsigned long count, void**, int* status) {
  for (unsigned long i = 0; i < count; ++i) status[i] = i % 2 ? -ENOENT : 2;
  return 0;
}

long residencyQuery(unsigned long count, void** pages, int* status) {
  const size_t pageBytes = static_cast<size_t>(::sysconf(_SC_PAGESIZE));
  for (unsigned long i = 0; i < count; ++i) {
    unsigned char resident = 0;
    if (::mincore(pages[i], pageBytes, &resident) != 0) return -1;
    // This test adapter reports residency, not a physical NUMA node assignment.
    status[i] = (resident & 1) ? 0 : -ENOENT;
  }
  return 0;
}

TEST(LocalityWorkload, AnonymousPayloadIsFirstTouchedOnlyByInitialize) {
  Workload work(config(Workload::Model::Mixed));
  const auto before = work.samplePageLocations(1000, residencyQuery);
  ASSERT_EQ(before.errnoValue, 0);
  EXPECT_EQ(before.knownPages, 0);
  work.initialize();
  const auto after = work.samplePageLocations(1000, residencyQuery);
  ASSERT_EQ(after.errnoValue, 0);
  EXPECT_EQ(after.knownPages, after.sampledPages);
  EXPECT_EQ(after.sampledPages, work.allocatedBytes() / work.pageBytes());
}

TEST(LocalityWorkload, PageQueriesPreserveUnknownAndPartialEvidence) {
  Workload work(config(Workload::Model::Mixed));
  auto denied = work.samplePageLocations(4, deniedQuery);
  EXPECT_EQ(denied.status, "unknown");
  EXPECT_EQ(denied.errnoValue, EPERM);
  EXPECT_EQ(denied.knownPages, 0);
  EXPECT_TRUE(denied.nodeCounts.empty());
  EXPECT_EQ(denied.perPageStatus, std::vector<int>(4, -EPERM));
  work.initialize();
  auto partial = work.samplePageLocations(4, partialQuery);
  EXPECT_EQ(partial.status, "partial");
  EXPECT_EQ(partial.sampledPages, 4);
  EXPECT_EQ(partial.knownPages, 2);
  EXPECT_EQ(partial.nodeCounts.at(2), 2);
  auto actual = work.samplePageLocations(8);
  EXPECT_EQ(actual.sampledPages, 8);
  EXPECT_EQ(actual.perPageStatus.size(), 8);
  EXPECT_LE(actual.knownPages, 8);
  if (actual.errnoValue) {
    EXPECT_EQ(actual.status, "unknown");
    EXPECT_TRUE(actual.nodeCounts.empty());
  }
  EXPECT_THROW(work.samplePageLocations(0), std::invalid_argument);
  Workload noMemory(config(Workload::Model::None));
  EXPECT_EQ(noMemory.samplePageLocations().status, "empty");
}

} // namespace
} // namespace executor_demo

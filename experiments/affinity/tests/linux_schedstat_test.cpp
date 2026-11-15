#include <executor_demo/LinuxSchedstatLoadProvider.h>
#include <filesystem>
#include <fstream>
#include <gtest/gtest.h>
#include <sstream>

using executor_demo::LinuxSchedstatLoadProvider;
class SchedstatTest : public ::testing::Test {
protected:
  std::filesystem::path root;
  void SetUp() override {
    char path[] = "/tmp/executor-schedstat-XXXXXX";
    auto *directory = ::mkdtemp(path);
    ASSERT_NE(directory, nullptr);
    root = directory;
    std::filesystem::create_directories(root / "sys/kernel");
    enabled(1);
    write(11, 100, 20, 7);
    write(12, 200, 30, 8);
  }
  void TearDown() override { std::filesystem::remove_all(root); }
  void enabled(int value) {
    std::ofstream(root / "sys/kernel/sched_schedstats") << value;
  }
  void write(int tid, uint64_t cpu, uint64_t waiting, uint64_t birth) {
    auto path = root / "123/task" / std::to_string(tid);
    std::filesystem::create_directories(path);
    std::ofstream stat(path / "stat");
    stat << tid << " (worker ) with space) S";
    for (int field = 4; field < 22; ++field)
      stat << " 0";
    stat << ' ' << birth << " 0\n";
    std::ofstream(path / "schedstat") << cpu << ' ' << waiting << " 2\n";
  }
  LinuxSchedstatLoadProvider::Config config() const {
    LinuxSchedstatLoadProvider::Config c;
    c.procRoot = root.string();
    c.pid = 123;
    c.interval = std::chrono::milliseconds(2);
    return c;
  }
};
TEST_F(SchedstatTest, BaselineDeltaDisabledAndMissingWorker) {
  LinuxSchedstatLoadProvider provider(config(), 1);
  provider.setThreadGroups({{11, 12}});
  EXPECT_FALSE(provider.read(0).cpuValid);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  write(11, 1000100, 200020, 7);
  write(12, 2000200, 300030, 8);
  provider.sampleOnce();
  auto sample = provider.read(0);
  ASSERT_TRUE(sample.cpuValid);
  ASSERT_TRUE(sample.runqueueValid);
  ASSERT_GT(sample.sampleWindowNs, 0);
  EXPECT_NEAR(sample.sampledCpuCores * sample.sampleWindowNs, 3000000, 0.1);
  EXPECT_NEAR(sample.sampledRunqueueWaitCores * sample.sampleWindowNs, 500000,
              0.1);
  enabled(0);
  provider.sampleOnce();
  sample = provider.read(0);
  EXPECT_TRUE(sample.cpuValid);
  EXPECT_FALSE(sample.runqueueValid);
  std::filesystem::remove(root / "123/task/12/schedstat");
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid); // no partial group presented as idle
  EXPECT_GT(provider.stats().totalScanWallNs, 0);
}
TEST_F(SchedstatTest, TidReusePermanentlyInvalidatesTheRegistration) {
  LinuxSchedstatLoadProvider provider(config(), 1);
  provider.setThreadGroups({{11}});
  provider.sampleOnce();
  write(11, 1000, 200, 99);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  write(11, 1100, 300, 99);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  // A later apparent return to the original identity cannot revive a known
  // replaced worker. A new provider is the explicit registration boundary.
  write(11, 1200, 400, 7);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  write(11, 1300, 500, 7);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  LinuxSchedstatLoadProvider replacement(config(), 1);
  replacement.setThreadGroups({{11}});
  replacement.sampleOnce();
  write(11, 1400, 600, 7);
  replacement.sampleOnce();
  EXPECT_TRUE(replacement.read(0).cpuValid);
}
TEST_F(SchedstatTest, SameWorkerCounterResetCanEstablishANewDeltaBaseline) {
  LinuxSchedstatLoadProvider provider(config(), 1);
  provider.setThreadGroups({{11}});
  provider.sampleOnce();
  write(11, 1, 1, 7);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  write(11, 101, 101, 7);
  provider.sampleOnce();
  EXPECT_TRUE(provider.read(0).cpuValid);
}
TEST_F(SchedstatTest, ZeroStartTimeIsAValidLockedIdentity) {
  write(11, 100, 20, 0);
  LinuxSchedstatLoadProvider provider(config(), 1);
  provider.setThreadGroups({{11}});
  provider.sampleOnce();
  write(11, 200, 40, 0);
  provider.sampleOnce();
  EXPECT_TRUE(provider.read(0).cpuValid);
  write(11, 300, 60, 1);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
  write(11, 400, 80, 1);
  provider.sampleOnce();
  EXPECT_FALSE(provider.read(0).cpuValid);
}
TEST_F(SchedstatTest, RegistrationAndBackgroundLifecycle) {
  LinuxSchedstatLoadProvider provider(config(), 1);
  EXPECT_THROW(provider.start(), std::logic_error);
  EXPECT_THROW(provider.setThreadGroups({{11, 11}}), std::invalid_argument);
  provider.setThreadGroups({{11}});
  provider.start();
  EXPECT_THROW(provider.sampleOnce(), std::logic_error);
  EXPECT_THROW(provider.setThreadGroups({{12}}), std::logic_error);
  std::this_thread::sleep_for(std::chrono::milliseconds(8));
  provider.stop();
  provider.stop();
  EXPECT_GT(provider.stats().scans, 0);
  EXPECT_FALSE(provider.read(99).cpuValid);
}

#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <executor_demo/LoadPolicy.h>
#include <fstream>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace executor_demo {

// An optional diagnostic / slow-feedback provider, usable with existing CPU
// pools given their Linux TIDs. No procfs access occurs in read(). Registration
// is immutable while sampling: resize/replacement needs a new provider and a
// fresh baseline. Register a worker lifetime, not a historical recycled TID.
// The first successful stat/schedstat read locks each worker's start time. A
// later birth mismatch permanently invalidates that worker registration. TID
// reuse before that first valid baseline cannot be detected from a TID alone;
// callers must keep the registered workers alive while establishing it.
// Lifecycle methods (registration/start/sampleOnce/stop/destruction) belong to
// one management thread. read()/stats() may run concurrently while this object
// is alive; lifecycle calls are not a concurrent control API.
class LinuxSchedstatLoadProvider final : public LoadProvider {
public:
  struct Config {
    std::chrono::milliseconds interval{50};
    std::string procRoot{"/proc"};
    pid_t pid{::getpid()};
  };
  struct Stats {
    uint64_t scans{0}, failedGroups{0}, totalScanWallNs{0}, totalScanCpuNs{0},
        maxScanWallNs{0};
    bool runqueueAccountingEnabled{false};
  };

  LinuxSchedstatLoadProvider(Config config, size_t groups)
      : config_(std::move(config)) {
    if (!groups || groups > 4096 || config_.interval.count() < 1 ||
        config_.pid <= 0)
      throw std::invalid_argument("invalid schedstat provider configuration");
    groups_.resize(groups);
    for (size_t i = 0; i < groups; ++i)
      slots_.push_back(std::make_unique<Slot>());
  }
  ~LinuxSchedstatLoadProvider() override { stop(); }
  LinuxSchedstatLoadProvider(const LinuxSchedstatLoadProvider &) = delete;
  LinuxSchedstatLoadProvider &
  operator=(const LinuxSchedstatLoadProvider &) = delete;

  void setThreadGroups(const std::vector<std::vector<pid_t>> &tids) {
    if (started_ || scans_.load() || tids.size() != groups_.size())
      throw std::logic_error("register TIDs once, before sampling");
    std::set<pid_t> unique;
    for (const auto &group : tids) {
      if (group.empty())
        throw std::invalid_argument("empty schedstat thread group");
      for (auto tid : group)
        if (tid <= 0 || !unique.insert(tid).second)
          throw std::invalid_argument("invalid/duplicate worker TID");
    }
    for (size_t i = 0; i < tids.size(); ++i) {
      groups_[i].clear();
      for (auto tid : tids[i])
        groups_[i].push_back(Worker{tid, {}});
    }
    registered_ = true;
  }
  void start() {
    if (started_ || !registered_)
      throw std::logic_error(
          "schedstat provider already started or unregistered");
    started_ = true;
    stopping_.store(false);
    worker_ = std::thread([this] {
      while (!stopping_.load(std::memory_order_relaxed)) {
        collect();
        std::unique_lock lock(waitMutex_);
        // Delay is after a scan: an expensive scan cannot create a catch-up
        // storm. Published sampleWindowNs includes the measured actual gap.
        changed_.wait_for(lock, config_.interval,
                          [&] { return stopping_.load(); });
      }
    });
  }
  void stop() noexcept {
    stopping_.store(true);
    changed_.notify_all();
    if (worker_.joinable())
      worker_.join();
  }
  // Explicit offline/probe entry. Never call from an executor add() path.
  void sampleOnce() {
    if (started_ || !registered_)
      throw std::logic_error(
          "manual scan requires registered, unstarted provider");
    collect();
  }
  const char *name() const noexcept override {
    return "linux_schedstat_cached";
  }
  LoadSample read(size_t group) const noexcept override {
    LoadSample sample;
    if (group >= slots_.size())
      return sample;
    const auto &slot = *slots_[group];
    const auto version = slot.version.load(std::memory_order_acquire);
    if (version & 1)
      return sample;
    const auto runtime = slot.runtimeNs.load(std::memory_order_relaxed);
    const auto waiting = slot.waitNs.load(std::memory_order_relaxed);
    const auto interval = slot.windowNs.load(std::memory_order_relaxed);
    sample.sampledAtNs = slot.atNs.load(std::memory_order_relaxed);
    sample.sampleWindowNs = interval;
    const auto flags = slot.flags.load(std::memory_order_relaxed);
    // Bounded optimistic read: fall back on concurrent publication, never spin
    // on a preempted publisher. Atomic payload avoids C++ seqlock data races.
    std::atomic_thread_fence(std::memory_order_acquire);
    if (version != slot.version.load(std::memory_order_acquire) || !interval)
      return LoadSample{};
    sample.sampledCpuCores = double(runtime) / double(interval);
    sample.sampledRunqueueWaitCores = double(waiting) / double(interval);
    sample.cpuValid = flags & 1;
    sample.runqueueValid = flags & 2;
    // No instantaneous runnable gauge: schedstat provides accumulated time.
    return sample;
  }
  Stats stats() const noexcept {
    return {scans_.load(), failed_.load(),  wall_.load(),
            cpu_.load(),   maxWall_.load(), runqueueEnabled_.load()};
  }

private:
  struct Counters {
    uint64_t runtime{0}, wait{0}, startTime{0};
    bool valid{false};
  };
  struct Worker {
    pid_t tid;
    Counters previous;
    uint64_t expectedStartTime{0};
    bool identityLocked{false}, identityLost{false};
  };
  struct alignas(128) Slot {
    std::atomic<uint64_t> version{0}, runtimeNs{0}, waitNs{0}, windowNs{0},
        atNs{0}, flags{0};
  };
  static uint64_t now() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  }
  static uint64_t threadCpu() noexcept {
    timespec value{};
    if (::clock_gettime(CLOCK_THREAD_CPUTIME_ID, &value))
      return 0;
    return uint64_t(value.tv_sec) * 1000000000 + value.tv_nsec;
  }
  Counters counters(pid_t tid) const {
    const auto base = config_.procRoot + "/" + std::to_string(config_.pid) +
                      "/task/" + std::to_string(tid);
    Counters result;
    std::ifstream stat(base + "/stat");
    std::string line;
    if (!std::getline(stat, line))
      return result;
    auto end = line.rfind(") ");
    if (end == std::string::npos)
      return result;
    std::istringstream fields(line.substr(end + 2));
    std::string field;
    for (size_t i = 0; i <= 19; ++i) {
      if (!(fields >> field))
        return result;
      if (i == 19) {
        if (field.empty() || field[0] == '-')
          return result;
        size_t used = 0;
        result.startTime = std::stoull(field, &used);
        if (used != field.size())
          return Counters{};
      }
    }
    std::ifstream sched(base + "/schedstat");
    uint64_t slices = 0;
    if (!(sched >> result.runtime >> result.wait >> slices))
      return Counters{};
    result.valid = true;
    return result;
  }
  bool accountingEnabled() const {
    std::ifstream enabled(config_.procRoot + "/sys/kernel/sched_schedstats");
    int value = 0;
    return bool(enabled >> value) && value == 1;
  }
  void publish(size_t group, uint64_t runtime, uint64_t waiting,
               uint64_t window, uint64_t at, unsigned flags) noexcept {
    auto &slot = *slots_[group];
    slot.version.fetch_add(1, std::memory_order_acq_rel);
    slot.runtimeNs.store(runtime, std::memory_order_relaxed);
    slot.waitNs.store(waiting, std::memory_order_relaxed);
    slot.windowNs.store(window, std::memory_order_relaxed);
    slot.atNs.store(at, std::memory_order_relaxed);
    slot.flags.store(flags, std::memory_order_relaxed);
    slot.version.fetch_add(1, std::memory_order_release);
  }
  void collect() noexcept {
    const auto begin = now(), cpuBegin = threadCpu();
    bool enabled = false;
    try {
      enabled = accountingEnabled();
    } catch (...) {
    }
    runqueueEnabled_.store(enabled);
    for (size_t group = 0; group < groups_.size(); ++group) {
      bool valid = previousAt_ != 0;
      uint64_t runtime = 0, waiting = 0;
      for (auto &worker : groups_[group]) {
        Counters current;
        try {
          current = counters(worker.tid);
        } catch (...) {
        }
        if (current.valid) {
          if (!worker.identityLocked) {
            worker.expectedStartTime = current.startTime;
            worker.identityLocked = true;
          } else if (current.startTime != worker.expectedStartTime) {
            // Never silently attach a replacement thread to the old pool.
            // Even a later sample reporting the original birth does not clear
            // this condition; recovery requires an explicitly new provider.
            worker.identityLost = true;
          }
        }
        const auto &previous = worker.previous;
        if (worker.identityLost || !current.valid || !previous.valid ||
            current.startTime != previous.startTime ||
            current.runtime < previous.runtime ||
            current.wait < previous.wait) {
          valid = false;
        } else {
          const auto dr = current.runtime - previous.runtime,
                     dw = current.wait - previous.wait;
          if (UINT64_MAX - runtime < dr || UINT64_MAX - waiting < dw)
            valid = false;
          else {
            runtime += dr;
            waiting += dw;
          }
        }
        worker.previous = current;
      }
      // A missing worker invalidates the entire group, never reads as idle.
      if (!valid)
        failed_.fetch_add(1, std::memory_order_relaxed);
      publish(group, runtime, waiting, previousAt_ ? begin - previousAt_ : 0,
              begin,
              valid ? (1U | (enabled && previousEnabled_ ? 2U : 0U)) : 0U);
    }
    previousAt_ = begin;
    previousEnabled_ = enabled;
    const auto elapsed = now() - begin, cpuElapsed = threadCpu() - cpuBegin;
    wall_.fetch_add(elapsed, std::memory_order_relaxed);
    cpu_.fetch_add(cpuElapsed, std::memory_order_relaxed);
    maxWall_.store(std::max(maxWall_.load(), elapsed));
    scans_.fetch_add(1, std::memory_order_relaxed);
  }
  Config config_;
  std::vector<std::vector<Worker>> groups_;
  std::vector<std::unique_ptr<Slot>> slots_;
  bool registered_{false}, started_{false}, previousEnabled_{false};
  uint64_t previousAt_{0};
  std::atomic<bool> stopping_{false}, runqueueEnabled_{false};
  std::atomic<uint64_t> scans_{0}, failed_{0}, wall_{0}, cpu_{0}, maxWall_{0};
  std::mutex waitMutex_;
  std::condition_variable changed_;
  std::thread worker_;
};
} // namespace executor_demo

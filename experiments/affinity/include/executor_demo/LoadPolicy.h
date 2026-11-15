#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace executor_demo {

// A provider returns published values only: read() must not perform procfs I/O,
// allocate, take a worker-list lock, or wait for a sampling thread. Basic pool
// counters are current relaxed observations, not one transactional snapshot.
// OS-derived observations describe a historical window in steady-clock time.
struct LoadSample {
  uint64_t queueDepth{0}, outstanding{0}, callbackInflight{0};
  uint64_t estimatedOutstandingCpuUs{0};
  uint64_t workerCount{0}; // Configured workers; 0 means unknown, not zero capacity.
  double capacity{1}; // Configured effective CPU-equivalent capacity, not workers.
  bool queueValid{false}, outstandingValid{false};
  bool callbackInflightValid{false}, workValid{false};
  double sampledCpuCores{0}; // Sum of thread CPU seconds / wall seconds.
  double sampledRunqueueWaitCores{0}; // Runnable-wait thread seconds / wall seconds.
  uint64_t sampledRunnable{0}; // Optional instantaneous R-state count, includes on-CPU.
  bool cpuValid{false}, runqueueValid{false}, runnableValid{false};
  uint64_t sampledAtNs{0}, sampleWindowNs{0};
};

class LoadProvider {
 public:
  virtual ~LoadProvider() = default;
  virtual LoadSample read(size_t group) const noexcept = 0;
  virtual const char* name() const noexcept { return "external"; }
};

enum class LoadInputs { QueueOnly, Counters, SampledCpu };
enum class LoadFallback {
  None, MissingCounter, MissingCpu, MissingRunqueue, StaleCpu, InvalidCpu,
  InvalidCapacity, InvalidScore, WorkerSaturated
};
struct ScoreContext {
  uint64_t reservedTasks{0}, reservedCpuUs{0}, nowNs{0};
};
struct LoadScore {
  double value{std::numeric_limits<double>::infinity()};
  bool fallback{false};
  LoadFallback reason{LoadFallback::None};
};

class LoadScorer {
 public:
  virtual ~LoadScorer() = default;
  virtual LoadScore score(const LoadSample&, const ScoreContext&) const noexcept = 0;
  virtual LoadInputs needs() const noexcept { return LoadInputs::Counters; }
  virtual const char* name() const noexcept = 0;
  virtual const char* units() const noexcept = 0;
};

namespace load_policy_detail {
inline bool validCapacity(const LoadSample& sample) noexcept {
  return std::isfinite(sample.capacity) && sample.capacity > 0;
}
inline LoadScore invalid(LoadFallback reason) noexcept {
  return {std::numeric_limits<double>::infinity(), true, reason};
}
inline LoadScore outstandingFallback(const LoadSample& sample,
                                    const ScoreContext& context,
                                    LoadFallback reason) noexcept {
  if (!validCapacity(sample)) return invalid(LoadFallback::InvalidCapacity);
  if (!sample.outstandingValid) return invalid(LoadFallback::MissingCounter);
  return {(double(sample.outstanding) + double(context.reservedTasks)) / sample.capacity,
          reason != LoadFallback::None, reason};
}
} // namespace load_policy_detail

class QueueLoadScorer final : public LoadScorer {
 public:
  LoadInputs needs() const noexcept override { return LoadInputs::QueueOnly; }
  const char* name() const noexcept override { return "queue"; }
  const char* units() const noexcept override { return "queued_tasks_per_capacity"; }
  LoadScore score(const LoadSample& sample, const ScoreContext& context) const noexcept override {
    using namespace load_policy_detail;
    if (!validCapacity(sample)) return invalid(LoadFallback::InvalidCapacity);
    if (!sample.queueValid) return outstandingFallback(sample, context, LoadFallback::MissingCounter);
    return {(double(sample.queueDepth) + double(context.reservedTasks)) / sample.capacity,
            false, LoadFallback::None};
  }
};

class OutstandingLoadScorer final : public LoadScorer {
 public:
  const char* name() const noexcept override { return "outstanding"; }
  const char* units() const noexcept override { return "outstanding_tasks_per_capacity"; }
  LoadScore score(const LoadSample& sample, const ScoreContext& context) const noexcept override {
    return load_policy_detail::outstandingFallback(sample, context, LoadFallback::None);
  }
};

class WorkLoadScorer final : public LoadScorer {
 public:
  const char* name() const noexcept override { return "work"; }
  const char* units() const noexcept override { return "estimated_cpu_us_per_capacity"; }
  LoadScore score(const LoadSample& sample, const ScoreContext& context) const noexcept override {
    using namespace load_policy_detail;
    if (!validCapacity(sample)) return invalid(LoadFallback::InvalidCapacity);
    // Missing work cannot fall back to a task-count score: its thresholds have
    // different units. A valid default provider always supplies this counter.
    if (!sample.workValid) return invalid(LoadFallback::MissingCounter);
    return {(double(sample.estimatedOutstandingCpuUs) + double(context.reservedCpuUs)) / sample.capacity,
            false, LoadFallback::None};
  }
};

class ActiveLoadScorer final : public LoadScorer {
 public:
  const char* name() const noexcept override { return "active"; }
  const char* units() const noexcept override { return "queue_plus_callback_inflight_per_capacity"; }
  LoadScore score(const LoadSample& sample, const ScoreContext& context) const noexcept override {
    using namespace load_policy_detail;
    if (!validCapacity(sample)) return invalid(LoadFallback::InvalidCapacity);
    if (!sample.queueValid || !sample.callbackInflightValid)
      return outstandingFallback(sample, context, LoadFallback::MissingCounter);
    // Callback inflight includes preempted and synchronously blocked callbacks;
    // it is NOT kernel runnable/on-CPU. Queue + inflight may miss callbacks in
    // the take-to-callback handoff window; outstanding is the fuller baseline.
    // Do not add outstanding again: it already includes these callbacks.
    return {(double(sample.queueDepth) + double(sample.callbackInflight) +
             double(context.reservedTasks)) / sample.capacity, false, LoadFallback::None};
  }
};

class CpuHybridLoadScorer final : public LoadScorer {
 public:
  explicit CpuHybridLoadScorer(uint64_t maxAgeNs = 100000000) : maxAgeNs_(maxAgeNs) {
    if (!maxAgeNs_) throw std::invalid_argument("CpuHybridLoadScorer: max age must be positive");
  }
  LoadInputs needs() const noexcept override { return LoadInputs::SampledCpu; }
  const char* name() const noexcept override { return "cpu-hybrid"; }
  const char* units() const noexcept override { return "max_queue_or_sampled_runnable_demand_per_capacity"; }
  LoadScore score(const LoadSample& sample, const ScoreContext& context) const noexcept override {
    using namespace load_policy_detail;
    const auto fallback = [&](LoadFallback reason) {
      return outstandingFallback(sample, context, reason);
    };
    if (!validCapacity(sample)) return invalid(LoadFallback::InvalidCapacity);
    if (!sample.cpuValid) return fallback(LoadFallback::MissingCpu);
    if (!sample.runqueueValid) return fallback(LoadFallback::MissingRunqueue);
    if (!sample.sampledAtNs || !sample.sampleWindowNs || sample.sampledAtNs > context.nowNs ||
        context.nowNs - sample.sampledAtNs > maxAgeNs_)
      return fallback(LoadFallback::StaleCpu);
    if (!std::isfinite(sample.sampledCpuCores) || sample.sampledCpuCores < 0 ||
        !std::isfinite(sample.sampledRunqueueWaitCores) || sample.sampledRunqueueWaitCores < 0)
      return fallback(LoadFallback::InvalidCpu);
    if (!sample.queueValid) return fallback(LoadFallback::MissingCounter);
    // Low CPU demand does not imply a worker can accept a callback. Inflight
    // includes synchronous waits; when every worker is occupied, retain the
    // live outstanding-pressure baseline. The caller's high watermark still
    // determines whether that pressure is enough to borrow elsewhere.
    if (sample.callbackInflightValid && sample.workerCount > 0 &&
        sample.callbackInflight >= sample.workerCount)
      return fallback(LoadFallback::WorkerSaturated);
    // CPU time and runnable waiting time are disjoint per-thread states over
    // one window. Their sum estimates average runnable thread demand. A real
    // collector must flag unavailable/disabled schedstats as invalid, not 0.
    // Max combines the historical signal and current queue; they are not
    // summed because tasks can contribute to both at different times.
    const auto queued = double(sample.queueDepth) + double(context.reservedTasks);
    auto demand = sample.sampledCpuCores + sample.sampledRunqueueWaitCores;
    if (!std::isfinite(demand)) return fallback(LoadFallback::InvalidCpu);
    if (sample.runnableValid) demand = std::max(demand, double(sample.sampledRunnable));
    return {std::max(queued, demand) / sample.capacity, false, LoadFallback::None};
  }
 private:
  const uint64_t maxAgeNs_;
};

inline const char* loadFallbackName(LoadFallback reason) noexcept {
  switch (reason) {
    case LoadFallback::None: return "none";
    case LoadFallback::MissingCounter: return "missing_counter";
    case LoadFallback::MissingCpu: return "missing_cpu";
    case LoadFallback::MissingRunqueue: return "missing_runqueue";
    case LoadFallback::StaleCpu: return "stale_cpu";
    case LoadFallback::InvalidCpu: return "invalid_cpu";
    case LoadFallback::InvalidCapacity: return "invalid_capacity";
    case LoadFallback::InvalidScore: return "invalid_score";
    case LoadFallback::WorkerSaturated: return "worker_saturated";
  }
  return "unknown";
}

} // namespace executor_demo

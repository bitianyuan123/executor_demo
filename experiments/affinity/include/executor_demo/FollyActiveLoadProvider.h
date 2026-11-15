#pragma once
#include <atomic>
#include <cmath>
#include <executor_demo/LoadPolicy.h>
#include <folly/executors/CPUThreadPoolExecutor.h>
#include <memory>
#include <stdexcept>
#include <vector>

namespace executor_demo {

// Production adapter for existing stock Folly CPU pools, independent of the DAG
// demo. Attach during initial setup with no concurrent admission, before any
// task starts. The setup stats check is diagnostic, not a drain barrier: a
// callback in the dequeue handoff window can escape a relaxed occupancy check.
// Attach only once per pool lifetime. Folly has no observer removal API here;
// repeated attachment permanently adds observer callbacks and counter updates.
// Construction/destruction belong to one management thread; read() can run
// concurrently with worker callbacks while both provider and pools are alive.
// Providers hold non-owning pool pointers; pools must outlive every read().
// Folly owns observers until pool destruction, including their shared gauge.
class FollyActiveLoadProvider final : public LoadProvider {
  struct alignas(128) Gauge {
    std::atomic<uint64_t> inflight{0};
  };
  class Observer final : public folly::ThreadPoolExecutor::TaskObserver {
  public:
    explicit Observer(std::shared_ptr<Gauge> gauge)
        : gauge_(std::move(gauge)) {}
    void taskDequeued(
        const folly::ThreadPoolExecutor::DequeuedTaskInfo &) noexcept override {
      gauge_->inflight.fetch_add(1, std::memory_order_relaxed);
    }
    void taskProcessed(const folly::ThreadPoolExecutor::ProcessedTaskInfo
                           &) noexcept override {
      gauge_->inflight.fetch_sub(1, std::memory_order_relaxed);
    }

  private:
    std::shared_ptr<Gauge> gauge_;
  };

public:
  FollyActiveLoadProvider(
      const std::vector<folly::CPUThreadPoolExecutor *> &pools,
      const std::vector<double> &capacities)
      : pools_(pools), capacities_(capacities) {
    if (pools.empty() || pools.size() != capacities.size())
      throw std::invalid_argument("invalid Folly active provider groups");
    for (size_t i = 0; i < pools.size(); ++i) {
      if (!pools[i] || !std::isfinite(capacities[i]) || capacities[i] <= 0)
        throw std::invalid_argument(
            "invalid Folly active provider pool/capacity");
      auto state = pools[i]->getPoolStatsLight(); // setup only, not read path
      if (state.activeThreadCount || state.pendingTaskCount)
        throw std::invalid_argument(
            "attach observer before enabling task admission");
    }
    for (auto *pool : pools) {
      auto gauge = std::make_shared<Gauge>();
      pool->addTaskObserver(std::make_unique<Observer>(gauge));
      gauges_.push_back(std::move(gauge));
    }
  }
  const char *name() const noexcept override {
    return "folly_task_observer_active";
  }
  LoadSample read(size_t group) const noexcept override {
    LoadSample sample;
    if (group >= pools_.size())
      return sample;
    sample.capacity = capacities_[group];
    // numThreads() is an atomic configured-maximum read in this Folly version;
    // it requires no worker-list lock and follows subsequent pool resizing.
    sample.workerCount = pools_[group]->numThreads();
    sample.queueDepth = pools_[group]->getTaskQueueSize();
    sample.callbackInflight =
        gauges_[group]->inflight.load(std::memory_order_relaxed);
    sample.queueValid = sample.callbackInflightValid = true;
    // Intentionally no outstanding/work estimate: enqueue callbacks alone are
    // insufficient to repair failed submission, cancellation and shutdown.
    return sample;
  }

private:
  std::vector<folly::CPUThreadPoolExecutor *> pools_;
  std::vector<double> capacities_;
  std::vector<std::shared_ptr<Gauge>> gauges_;
};
} // namespace executor_demo

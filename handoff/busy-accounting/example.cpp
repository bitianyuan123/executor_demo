#include <folly/executors/CPUThreadPoolExecutor.h>

#include <chrono>
#include <future>
#include <iostream>
#include <stdexcept>

int main() {
  using Pool = folly::CPUThreadPoolExecutor;
  using namespace std::chrono_literals;

  Pool disabled(1);
  if (disabled.getBusyTaskCount().has_value()) {
    throw std::runtime_error("disabled counter must be unavailable");
  }
  disabled.join();

  // Fixed one-worker pool. Shared-future gating demonstrates that a callback
  // can be busy without consuming CPU: B is worker occupancy, not OS state.
  std::promise<void> entered;
  std::promise<void> release;
  auto started = entered.get_future();
  auto released = release.get_future().share();
  Pool pool(
      std::make_pair(size_t{1}, size_t{1}),
      std::make_shared<folly::NamedThreadFactory>("BusyExample"),
      Pool::Options().setBusyCounting());
  pool.add([&] {
    entered.set_value();
    released.wait_for(10s);
  });
  const bool enteredOnTime = started.wait_for(5s) == std::future_status::ready;
  if (!enteredOnTime) {
    release.set_value();
    pool.join();
    throw std::runtime_error("worker did not enter callback");
  }
  pool.add([] {});
  const auto busy = pool.getBusyTaskCount();
  const auto queued = pool.getTaskQueueSize();
  const auto stats = pool.getPoolStats();
  // The gate makes this particular observation stable. In a running system
  // these fields are separate snapshots and need not satisfy an identity.
  const bool ok = busy && *busy == 1 && queued == 1 &&
      stats.activeThreadCount == 1 && stats.totalTaskCount == 2;
  std::cout << "disabled=unavailable; gated callback: B="
            << busy.value_or(0) << ", Q=" << queued
            << ", PoolStats.activeThreadCount=" << stats.activeThreadCount
            << ", PoolStats.totalTaskCount=" << stats.totalTaskCount << '\n';
  release.set_value();
  pool.join();
  if (!ok || pool.getBusyTaskCount().value_or(1) != 0) {
    throw std::runtime_error("busy accounting mismatch");
  }
  std::cout << "after join: B=0; blocked callback was still busy; "
               "B is NOT an OS running+runnable measurement\n";
}

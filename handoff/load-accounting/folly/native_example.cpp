#include <folly/executors/CPUThreadPoolExecutor.h>
#include <condition_variable>
#include <iostream>
#include <mutex>

int main() {
  using Pool = folly::CPUThreadPoolExecutor;
  Pool disabled(1);
  if (disabled.getOutstandingTaskCount().hasValue()) return 1;
  disabled.join();

  Pool::Options options;
  options.setTaskCounting(true);
  Pool pool(std::make_pair(size_t{1}, size_t{1}),
            std::make_shared<folly::NamedThreadFactory>("n-example"), options);
  std::mutex mutex;
  std::condition_variable changed;
  bool started = false, release = false;
  pool.add([&] {
    std::unique_lock lock(mutex);
    started = true;
    changed.notify_all();
    changed.wait(lock, [&] { return release; });
  });
  {
    std::unique_lock lock(mutex);
    changed.wait(lock, [&] { return started; });
  }
  pool.add([] {});
  auto outstanding = pool.getOutstandingTaskCount();
  std::cout << "disabled=unavailable; one held callback plus one queued task: N="
            << *outstanding << '\n';
  {
    std::lock_guard lock(mutex);
    release = true;
  }
  changed.notify_all();
  pool.join();
  std::cout << "after join: N=" << *pool.getOutstandingTaskCount() << '\n';
  return *outstanding == 2 && *pool.getOutstandingTaskCount() == 0 ? 0 : 1;
}

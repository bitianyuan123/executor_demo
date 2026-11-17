#include "TrackedCpuExecutor.h"

#include <iostream>

#include <folly/synchronization/Baton.h>

int main() {
  load_accounting::TrackedCpuExecutor executor(1, true);
  folly::Baton<> entered;
  folly::Baton<> release;
  executor.add([&] {
    entered.post();
    release.wait();
  });
  entered.wait();
  executor.add([] {});
  std::cout << "running=1 queued=1 N="
            << *executor.getOutstandingTaskCount() << '\n';
  release.post();
  executor.join();
  std::cout << "after drain N=" << *executor.getOutstandingTaskCount() << '\n';
  load_accounting::TrackedCpuExecutor disabled(1); // Counting defaults to off.
  std::cout << "disabled metric available="
            << disabled.getOutstandingTaskCount().hasValue() << '\n';
  disabled.join();
}

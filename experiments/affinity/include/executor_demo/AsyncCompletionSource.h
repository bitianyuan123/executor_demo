#pragma once

#include <folly/Executor.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <ctime>
#include <limits>
#include <iterator>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

namespace executor_demo {

// A bounded, single-thread external-completion model. It does not perform RPCs.
// Request IDs belong to one run, are in [0, maxRequests), and cannot be reused
// after cancellation. Only the helper invokes accepted events' callbacks, and
// never while holding its mutex. An event already dispatched to onFire wins a
// racing cancellation; otherwise cancellation wins. Callback exceptions are
// counted and contained. Application/request failure handling belongs to the
// callback, not this source.
//
// The owner outlives all callers. Normal shutdown is executor.join(), followed
// by stopAfterDrain(): an empty source can still receive future registrations.
// abort() cancels queued events and waits for an already executing callback.
// Lifecycle methods and destruction must occur outside the helper thread.
class AsyncCompletionSource {
 public:
  using Clock = std::chrono::steady_clock;
  struct Stats {
    uint64_t registered{0}, fired{0}, canceled{0}, rejected{0};
    // pending includes the one callback currently executing on the helper.
    // registered == fired + canceled + pending, including during callbacks.
    uint64_t pending{0}, peakPending{0}, callbackFailures{0};
    // Final helper thread CPU, available after shutdown, including callbacks.
    uint64_t helperCpuNs{0};
    bool helperCpuValid{false};
  };

  AsyncCompletionSource(size_t maxRequests, size_t capacity)
      : capacity_(capacity), canceledRequests_(maxRequests, false),
        requestEvents_(maxRequests) {
    if (!maxRequests || !capacity) {
      throw std::invalid_argument("async source needs positive request and event capacities");
    }
    cancellations_.reserve(maxRequests);
    helper_ = std::thread([this] { run(); });
    helperId_ = helper_.get_id();
  }
  AsyncCompletionSource(const AsyncCompletionSource&) = delete;
  AsyncCompletionSource& operator=(const AsyncCompletionSource&) = delete;
  ~AsyncCompletionSource() { abort(); }

  // False means ownership was not accepted: neither callback is invoked, and
  // their captures are destroyed on this calling thread. This includes full,
  // canceled, closed and out-of-range requests. Allocation failure may throw;
  // no event is registered on that path. Both callbacks must be nonempty.
  bool schedule(uint64_t requestId, Clock::time_point due,
                folly::Func onFire, folly::Func onCancel) {
    if (!onFire || !onCancel) {
      throw std::invalid_argument("async source requires fire and cancel callbacks");
    }
    auto event = std::make_unique<Event>(requestId, std::move(onFire), std::move(onCancel));
    {
      std::lock_guard lock(mutex_);
      if (mode_ != Mode::Open || requestId >= requestEvents_.size() ||
          canceledRequests_[requestId] || stats_.pending >= capacity_) {
        ++stats_.rejected;
        return false;
      }
      if (sequence_ == std::numeric_limits<uint64_t>::max()) {
        throw std::overflow_error("async source sequence exhausted");
      }
      const Key key{due, sequence_};
      auto& index = requestEvents_[requestId];
      index.push_back(key);
      event->requestPosition = std::prev(index.end());
      try {
        events_.emplace(key, std::move(event));
      } catch (...) {
        index.pop_back();
        throw;
      }
      ++sequence_;
      ++stats_.registered;
      ++stats_.pending;
      stats_.peakPending = std::max(stats_.peakPending, stats_.pending);
    }
    cv_.notify_one();
    return true;
  }

  // Serializes with registration and dispatch. Cancellation remains sticky
  // even when the request currently has no pending events. It never invokes
  // callbacks on the caller and repeated cancellation is harmless.
  void cancelRequest(uint64_t requestId) {
    {
      std::lock_guard lock(mutex_);
      if (requestId >= requestEvents_.size()) {
        throw std::out_of_range("async source cancellation request ID out of range");
      }
      if (canceledRequests_[requestId]) return;
      canceledRequests_[requestId] = true;
      cancellations_.push_back(requestId); // Storage reserved at construction.
    }
    cv_.notify_one();
  }

  // Rejects new events, honors deadlines for accepted events, then joins.
  // Call only after executor.join() in the normal continuation lifecycle.
  void stopAfterDrain() {
    requireExternalLifecycleCaller();
    {
      std::lock_guard lock(mutex_);
      if (mode_ == Mode::Open) mode_ = Mode::Drain;
    }
    cv_.notify_one();
    joinHelper();
  }

  // Escalates a concurrent graceful drain, rejects registrations, and invokes
  // onCancel for queued events regardless of their deadlines. Idempotent.
  void abort() {
    requireExternalLifecycleCaller();
    {
      std::lock_guard lock(mutex_);
      if (mode_ != Mode::Stopped) mode_ = Mode::Abort;
    }
    cv_.notify_one();
    joinHelper();
  }

  Stats stats() const {
    std::lock_guard lock(mutex_);
    return stats_;
  }

 private:
  using Key = std::pair<Clock::time_point, uint64_t>;
  struct Event {
    Event(uint64_t id, folly::Func fire, folly::Func cancel)
        : requestId(id), onFire(std::move(fire)), onCancel(std::move(cancel)) {}
    uint64_t requestId;
    folly::Func onFire, onCancel;
    std::list<Key>::iterator requestPosition;
  };
  using Events = std::map<Key, std::unique_ptr<Event>>;
  enum class Mode { Open, Drain, Abort, Stopped };

  void requireExternalLifecycleCaller() const {
    if (std::this_thread::get_id() == helperId_) {
      throw std::logic_error("async source stop/abort cannot join its own helper thread");
    }
  }
  void joinHelper() {
    // State transitions occur before taking this lock, so abort can interrupt
    // a stopAfterDrain caller waiting on a distant deadline.
    std::lock_guard lock(joinMutex_);
    if (helper_.joinable()) helper_.join();
  }
  std::unique_ptr<Event> extract(Events::iterator position) {
    auto result = std::move(position->second);
    requestEvents_[result->requestId].erase(result->requestPosition);
    events_.erase(position);
    return result;
  }
  static bool threadCpu(uint64_t& value) noexcept {
    timespec sample{};
    if (clock_gettime(CLOCK_THREAD_CPUTIME_ID, &sample) != 0) return false;
    value = static_cast<uint64_t>(sample.tv_sec) * 1000000000ULL + sample.tv_nsec;
    return true;
  }
  void run() {
    uint64_t cpuStart = 0;
    const bool cpuStartValid = threadCpu(cpuStart);
    for (;;) {
      std::unique_ptr<Event> event;
      bool canceled = false;
      {
        std::unique_lock lock(mutex_);
        for (;;) {
          // No future event can be registered for these sticky canceled IDs.
          while (!cancellations_.empty() &&
                 requestEvents_[cancellations_.back()].empty()) {
            cancellations_.pop_back();
          }
          if (!cancellations_.empty()) {
            const auto id = cancellations_.back();
            event = extract(events_.find(requestEvents_[id].front()));
            canceled = true;
            break;
          }
          if (mode_ == Mode::Abort && !events_.empty()) {
            event = extract(events_.begin());
            canceled = true;
            break;
          }
          if (events_.empty()) {
            if (mode_ != Mode::Open) {
              uint64_t cpuEnd = 0;
              stats_.helperCpuValid = cpuStartValid && threadCpu(cpuEnd) && cpuEnd >= cpuStart;
              if (stats_.helperCpuValid) stats_.helperCpuNs = cpuEnd - cpuStart;
              mode_ = Mode::Stopped;
              return;
            }
            cv_.wait(lock);
            continue;
          }
          const auto first = events_.begin();
          if (first->first.first <= Clock::now()) {
            event = extract(first);
            break;
          }
          cv_.wait_until(lock, first->first.first);
        }
      }
      bool failed = false;
      try {
        if (canceled) event->onCancel();
        else event->onFire();
      } catch (...) {
        failed = true;
      }
      // Release captures/continuation leases outside the source mutex too.
      event.reset();
      {
        std::lock_guard lock(mutex_);
        --stats_.pending;
        if (canceled) ++stats_.canceled;
        else ++stats_.fired;
        if (failed) ++stats_.callbackFailures;
      }
    }
  }

  const size_t capacity_;
  mutable std::mutex mutex_;
  std::mutex joinMutex_;
  std::condition_variable cv_;
  Mode mode_{Mode::Open};
  Stats stats_;
  uint64_t sequence_{0};
  std::vector<bool> canceledRequests_;
  std::vector<std::list<Key>> requestEvents_;
  std::vector<uint64_t> cancellations_;
  Events events_;
  std::thread helper_;
  std::thread::id helperId_;
};

} // namespace executor_demo

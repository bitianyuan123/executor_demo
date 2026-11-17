#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <utility>

#include <folly/Optional.h>
#include <folly/ScopeGuard.h>
#include <folly/executors/CPUThreadPoolExecutor.h>

namespace load_accounting {

// Independent of the wrapper/pool lifetime; tickets and CounterView retain it.
struct CounterState {
  std::atomic<size_t> outstanding{0};
};

class CounterView {
 public:
  folly::Optional<size_t> getOutstandingTaskCount() const noexcept {
    if (!state_) {
      return folly::none; // Disabled is unknown, never an idle routing signal.
    }
    return state_->outstanding.load(std::memory_order_relaxed);
  }

 private:
  friend class TrackedCpuExecutor;
  explicit CounterView(std::shared_ptr<CounterState> state)
      : state_(std::move(state)) {}
  std::shared_ptr<CounterState> state_;
};

// This is composition, not a replacement implementation of folly::Executor's
// KeepAlive protocol. All counted submissions must enter through this object.
class TrackedCpuExecutor {
 public:
  using Func = folly::Func;
  using Pool = folly::CPUThreadPoolExecutor;

  explicit TrackedCpuExecutor(size_t threads, bool enabled = false)
      : TrackedCpuExecutor(std::make_shared<Pool>(threads), enabled) {}

  explicit TrackedCpuExecutor(std::shared_ptr<Pool> pool, bool enabled = false)
      : state_(enabled ? std::make_shared<CounterState>() : nullptr),
        pool_(std::move(pool)) {
    if (!pool_) {
      throw std::invalid_argument("TrackedCpuExecutor requires a pool");
    }
  }

  // Copying a facade must not accidentally create a second counter for a pool.
  TrackedCpuExecutor(const TrackedCpuExecutor&) = delete;
  TrackedCpuExecutor& operator=(const TrackedCpuExecutor&) = delete;

  CounterView counterView() const { return CounterView(state_); }
  folly::Optional<size_t> getOutstandingTaskCount() const noexcept {
    if (!state_) {
      return folly::none;
    }
    return state_->outstanding.load(std::memory_order_relaxed);
  }

  void add(Func func) { pool_->add(wrapNormal(std::move(func))); }

  void addWithPriority(Func func, int8_t priority) {
    pool_->addWithPriority(wrapNormal(std::move(func)), priority);
  }

  void add(
      Func func,
      std::chrono::milliseconds expiration,
      Func expireCallback = nullptr) {
    auto pair = wrapExpiring(
        std::move(func), expiration, std::move(expireCallback));
    pool_->add(std::move(pair.first), expiration, std::move(pair.second));
  }

  void add(
      Func func,
      int8_t priority,
      std::chrono::milliseconds expiration,
      Func expireCallback = nullptr) {
    auto pair = wrapExpiring(
        std::move(func), expiration, std::move(expireCallback));
    pool_->add(
        std::move(pair.first), priority, expiration, std::move(pair.second));
  }

  // Lifecycle calls require that concurrent producers have already stopped.
  void join() { pool_->join(); }
  void stop() { pool_->stop(); }

 private:
  class Ticket {
   public:
    explicit Ticket(std::shared_ptr<CounterState> state)
        : state_(std::move(state)) {
      state_->outstanding.fetch_add(1, std::memory_order_relaxed);
    }
    Ticket(Ticket&& other) noexcept : state_(std::move(other.state_)) {}
    Ticket(const Ticket&) = delete;
    Ticket& operator=(const Ticket&) = delete;
    Ticket& operator=(Ticket&&) = delete;
    ~Ticket() { finish(); }

    void finish() noexcept {
      if (state_) {
        state_->outstanding.fetch_sub(1, std::memory_order_relaxed);
        state_.reset();
      }
    }

   private:
    std::shared_ptr<CounterState> state_;
  };

  // A real expiring task has TWO callback owners. The losing callback can be
  // destroyed before the winning callback runs; it must not release N early.
  struct ExpiringLease {
    explicit ExpiringLease(std::shared_ptr<CounterState> state)
        : ticket(std::move(state)) {}
    void finish() noexcept {
      if (!finished.exchange(true, std::memory_order_relaxed)) {
        ticket.finish();
      }
    }
    Ticket ticket; // Final-owner destruction handles unexecuted/drop paths.
    std::atomic<bool> finished{false};
  };

  static void requireTask(const Func& func) {
    if (!func) {
      throw std::invalid_argument("empty user task is not a poison marker");
    }
  }

  Func wrapNormal(Func func) const {
    requireTask(func);
    if (!state_) {
      return func;
    }
    // Reservation precedes publication to Folly. Exception unwinding or closure
    // destruction releases it without assuming add() did/did not take ownership.
    return [ticket = Ticket(state_), func = std::move(func)]() mutable {
      auto release = folly::makeGuard([&] { ticket.finish(); });
      func();
    };
  }

  std::pair<Func, Func> wrapExpiring(
      Func func,
      std::chrono::milliseconds expiration,
      Func expireCallback) const {
    requireTask(func);
    if (expiration.count() < 0) {
      throw std::invalid_argument("negative expiration is unsupported");
    }
    if (!state_) {
      return {std::move(func), std::move(expireCallback)};
    }
    if (expiration.count() == 0) {
      return {wrapNormal(std::move(func)), std::move(expireCallback)};
    }
    auto lease = std::make_shared<ExpiringLease>(state_);
    Func normal = [lease, func = std::move(func)]() mutable {
      auto release = folly::makeGuard([&] { lease->finish(); });
      func();
    };
    // Even with no user expiry callback, an internal completion callback must
    // keep the reservation alive until Folly actually handles the expiration.
    Func expired = [lease, func = std::move(expireCallback)]() mutable {
      auto release = folly::makeGuard([&] { lease->finish(); });
      if (func) {
        func();
      }
    };
    return {std::move(normal), std::move(expired)};
  }

  std::shared_ptr<CounterState> state_;
  std::shared_ptr<Pool> pool_;
};

} // namespace load_accounting

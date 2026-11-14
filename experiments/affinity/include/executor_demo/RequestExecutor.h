#pragma once

#include <executor_demo/BusinessExecutor.h>
#include <folly/io/async/Request.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>
#include <vector>

namespace executor_demo {

// Opt-in request-aware facade. Placement only affects newly ready callbacks;
// queued/running callbacks are never stolen or preempted. Public callbacks do
// not see group identifiers. Request handles are non-leasing: pending callbacks
// and explicitly captured continuations, rather than arbitrary handle copies,
// keep draining alive. The owner must outlive callers of its member functions.
class RequestExecutor {
  struct Identity {};
  struct RequestState {
    std::shared_ptr<Identity> identity;
    uint64_t color;
    size_t home;
  };
  struct Context {
    std::shared_ptr<RequestState> request;
    size_t source{0}, hops{0};
    bool hasParent{false};
  };

 public:
  enum class HomePolicy { RoundRobin, ColorHash };
  enum class Policy { Home, QueueSpill, OutstandingSpill, WorkSpill };
  enum class ContinuationPolicy { FollowParent, ReturnHome };
  enum class Reason {
    Home, BelowBusyThreshold, ReturnHome, LocalSpill, CrossNumaSpill,
    InsufficientGain, HopBudget, PinnedHome, NonFork
  };
  struct TaskOptions {
    uint64_t tag{0};
    uint64_t estimatedCpuUs{1000};
    bool pinHome{false};
    bool sequentialTags{false}; // addBatch emits tag + global batch index.
    bool isFork{false}; // addBatch sets this automatically for every member.
  };
  struct Observation {
    uint64_t tag{0}, color{0};
    size_t home{0}, source{0}, chosen{0}, hops{0};
    double preferredScore{0}, chosenScore{0};
    uint64_t submittedNs{0}, startNs{0}, finishNs{0};
    uint64_t queueBefore{0}, outstandingBefore{0};
    bool migrated{false}, crossNuma{false}, failed{false};
    Reason reason{Reason::Home};
  };
  using Observer = std::function<void(const Observation&)>;
  struct Config {
    std::vector<BusinessExecutor::GroupConfig> groups;
    HomePolicy homePolicy{HomePolicy::RoundRobin};
    Policy policy{Policy::Home};
    ContinuationPolicy continuationPolicy{ContinuationPolicy::FollowParent};
    // Scores: queue/outstanding count per capacity; work is estimated CPU us
    // per capacity. These are policy-specific units, not universal time bounds.
    double busyHigh{1}, returnLow{0.5}, minGain{0}, crossNumaPenalty{0};
    bool allowCrossNuma{false};
    bool balanceOnlyAtFork{false}; // Non-fork continuations follow their source.
    size_t maxHops{4}, batchChunk{1}, maxOutstanding{0};
    BusinessExecutor::QueueKind queueKind{BusinessExecutor::QueueKind::Default};
    Observer observer;
  };

  class Request {
   public:
    Request() = default;
    explicit operator bool() const noexcept { return bool(state_); }
   private:
    friend class RequestExecutor;
    explicit Request(std::shared_ptr<RequestState> state) : state_(std::move(state)) {}
    std::shared_ptr<RequestState> state_;
  };

  class Continuation {
   public:
    Continuation() = default;
    Continuation(const Continuation&) = delete;
    Continuation& operator=(const Continuation&) = delete;
    Continuation(Continuation&& other) noexcept
        : owner_(std::exchange(other.owner_, nullptr)),
          context_(std::move(other.context_)), requestContext_(std::move(other.requestContext_)) {}
    Continuation& operator=(Continuation&& other) noexcept {
      if (this != &other) {
        cancel();
        owner_ = std::exchange(other.owner_, nullptr);
        context_ = std::move(other.context_);
        requestContext_ = std::move(other.requestContext_);
      }
      return *this;
    }
    ~Continuation() { cancel(); }
    explicit operator bool() const noexcept { return owner_ != nullptr; }
    // One-shot, move-only token. The token itself must not be accessed
    // concurrently; move it to the RPC completion/cancellation owner.
    bool resume(folly::Func task) { return resume(std::move(task), TaskOptions{}); }
    bool resume(folly::Func task, TaskOptions options) {
      auto* owner = std::exchange(owner_, nullptr);
      if (!owner) return false;
      auto release = folly::makeGuard([&] { owner->releaseWork(); });
      folly::RequestContextScopeGuard restore(std::move(requestContext_));
      return owner->submit(context_, std::move(task), options, true);
    }
    void cancel() noexcept {
      if (auto* owner = std::exchange(owner_, nullptr)) owner->releaseWork();
      context_ = {};
      requestContext_.reset();
    }
   private:
    friend class RequestExecutor;
    Continuation(RequestExecutor* owner, Context context,
                 std::shared_ptr<folly::RequestContext> requestContext)
        : owner_(owner), context_(std::move(context)), requestContext_(std::move(requestContext)) {}
    RequestExecutor* owner_{nullptr};
    Context context_;
    std::shared_ptr<folly::RequestContext> requestContext_;
  };

  explicit RequestExecutor(Config config)
      : config_(validate(std::move(config))), pool_(poolConfig(config_)),
        identity_(std::make_shared<Identity>()) {
    reservations_.reserve(config_.groups.size());
    for (size_t i = 0; i < config_.groups.size(); ++i)
      reservations_.push_back(std::make_unique<ReservationCount>());
  }
  RequestExecutor(const RequestExecutor&) = delete;
  RequestExecutor& operator=(const RequestExecutor&) = delete;
  ~RequestExecutor() { join(); }

  Request beginRequest(uint64_t color) {
    if (state_.load() != State::Open) return {};
    const auto home = config_.homePolicy == HomePolicy::RoundRobin
        ? sequence_.fetch_add(1, std::memory_order_relaxed) % config_.groups.size()
        : hash(color) % config_.groups.size();
    return Request(std::make_shared<RequestState>(RequestState{identity_, color, home}));
  }

  bool add(const Request& request, folly::Func task) {
    return add(request, std::move(task), TaskOptions{});
  }
  bool add(const Request& request, folly::Func task, TaskOptions options) {
    return submit(rootContext(request), std::move(task), options, false);
  }
  bool add(folly::Func task) { return add(std::move(task), TaskOptions{}); }
  bool add(folly::Func task, TaskOptions options) {
    return submit(currentContext(), std::move(task), options, true);
  }
  size_t addBatch(const Request& request, std::vector<folly::Func> tasks) {
    return addBatch(request, std::move(tasks), TaskOptions{});
  }
  size_t addBatch(const Request& request, std::vector<folly::Func> tasks, TaskOptions options) {
    return batch(rootContext(request), std::move(tasks), options, false);
  }
  size_t addBatch(std::vector<folly::Func> tasks) {
    return addBatch(std::move(tasks), TaskOptions{});
  }
  size_t addBatch(std::vector<folly::Func> tasks, TaskOptions options) {
    return batch(currentContext(), std::move(tasks), options, true);
  }

  Continuation captureContinuation() {
    auto context = currentContext();
    auto requestContext = folly::RequestContext::saveContext();
    // A running callback is already leased, so join cannot pass its drain
    // predicate between this increment and returning the new lease.
    activeWork_.fetch_add(1);
    return Continuation(this, std::move(context), std::move(requestContext));
  }

  // Diagnostic operation only; hot-path routing never calls snapshots().
  std::vector<BusinessExecutor::Snapshot> snapshots() const { return pool_.snapshots(); }
  uint64_t observerFailures() const noexcept { return observerFailures_.load(std::memory_order_relaxed); }
  bool acceptingRequests() const noexcept { return state_.load() == State::Open; }

  // Stop new requests/external root submissions, drain accepted callbacks and
  // registered RPC continuations, then join the underlying pools. A captured
  // token must eventually be resumed, canceled, or destroyed by its owner.
  void join() {
    if (current_ && current_->owner == this)
      throw std::logic_error("RequestExecutor: join from own callback");
    std::lock_guard serial(joinMutex_);
    if (state_.load() == State::Joined) return;
    state_.store(State::Draining);
    {
      std::unique_lock lock(drainMutex_);
      drained_.wait(lock, [&] { return operations_.load() == 0 && activeWork_.load() == 0; });
    }
    state_.store(State::Joined);
    pool_.join();
  }

 private:
  enum class State { Open, Draining, Joined };
  struct InstalledContext { RequestExecutor* owner; Context context; };
  inline static thread_local InstalledContext* current_{nullptr};
  struct alignas(64) ReservationCount { std::atomic<uint64_t> tasks{0}, work{0}; };
  struct Selection {
    size_t chosen{0};
    double preferredScore{0}, chosenScore{0};
    Reason reason{Reason::Home};
  };

  static Config validate(Config config) {
    if (config.groups.empty() || !config.batchChunk ||
        !std::isfinite(config.busyHigh) || config.busyHigh < 0 ||
        !std::isfinite(config.returnLow) || config.returnLow < 0 || config.returnLow > config.busyHigh ||
        !std::isfinite(config.minGain) || config.minGain < 0 ||
        !std::isfinite(config.crossNumaPenalty) || config.crossNumaPenalty < 0)
      throw std::invalid_argument("RequestExecutor: invalid routing configuration");
    for (const auto& group : config.groups) {
      if (!group.threads || !std::isfinite(group.capacity) || group.capacity <= 0)
        throw std::invalid_argument("RequestExecutor: group threads/capacity must be positive");
    }
    // Unknown NUMA labels are a single unknown domain, not evidence of locality.
    // Cross-NUMA experiments must supply actual labels in their group config.
    return config;
  }
  static BusinessExecutor::Config poolConfig(const Config& config) {
    BusinessExecutor::Config result;
    result.groups = config.groups;
    result.policy = BusinessExecutor::Policy::Home;
    result.maxOutstanding = config.maxOutstanding;
    result.queueKind = config.queueKind;
    return result;
  }
  static uint64_t hash(uint64_t value) {
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
  }
  static uint64_t now() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
  }
  Context rootContext(const Request& request) const {
    if (!request.state_ || request.state_->identity != identity_)
      throw std::invalid_argument("RequestExecutor: missing or foreign request");
    return Context{request.state_, request.state_->home, 0, false};
  }
  Context currentContext() const {
    if (!current_ || current_->owner != this)
      throw std::logic_error("RequestExecutor: child submission outside its callback");
    return current_->context;
  }
  static void validateTask(const folly::Func& task, const TaskOptions& options) {
    if (!task || !options.estimatedCpuUs)
      throw std::invalid_argument("RequestExecutor: empty callback or zero work estimate");
  }
  bool maySubmit(bool drainCapability) const {
    const auto state = state_.load();
    return state == State::Open || (state == State::Draining && drainCapability);
  }
  bool crossNuma(size_t a, size_t b) const {
    const auto x = config_.groups[a].numaNode, y = config_.groups[b].numaNode;
    return x >= 0 && y >= 0 && x != y;
  }
  double score(size_t group) const {
    const auto& reserved = *reservations_[group];
    double value;
    if (config_.policy == Policy::QueueSpill) {
      value = double(pool_.readQueueDepth(group)) + reserved.tasks.load(std::memory_order_relaxed);
    } else {
      const auto load = pool_.readLoad(group);
      value = config_.policy == Policy::WorkSpill
          ? double(load.estimatedOutstandingCpuUs) + reserved.work.load(std::memory_order_relaxed)
          : double(load.outstanding) + reserved.tasks.load(std::memory_order_relaxed);
    }
    return value / config_.groups[group].capacity;
  }
  Selection choose(const Context& context, const TaskOptions& options) const {
    const auto home = context.request->home;
    if (options.pinHome || config_.policy == Policy::Home)
      return {home, 0, 0, options.pinHome ? Reason::PinnedHome : Reason::Home};
    size_t preferred = context.hasParent ? context.source : home;
    // Fork-only mode suppresses both borrowing and ReturnHome on ordinary
    // continuations. ReturnHome is reconsidered at the next explicit fork.
    if (config_.balanceOnlyAtFork && !options.isFork)
      return {preferred, 0, 0, Reason::NonFork};
    auto preferredScore = score(preferred);
    if (context.hops >= config_.maxHops)
      return {preferred, preferredScore, preferredScore, Reason::HopBudget};
    Reason reason = Reason::BelowBusyThreshold;
    // ReturnHome is a deliberate affinity policy: below the low watermark it
    // returns even if the current source is idle. This is not a measured cache
    // benefit. Migration hop and NUMA restrictions still apply.
    if (config_.continuationPolicy == ContinuationPolicy::ReturnHome &&
        preferred != home && (!crossNuma(preferred, home) || config_.allowCrossNuma)) {
      const auto homeScore = score(home);
      if (homeScore <= config_.returnLow) {
        preferred = home;
        preferredScore = homeScore;
        reason = Reason::ReturnHome;
      }
    }
    Selection result{preferred, preferredScore, preferredScore, reason};
    if (preferredScore <= config_.busyHigh) return result;
    result.reason = Reason::InsufficientGain;
    // Search the local NUMA domain first; a qualifying local gain takes
    // precedence over a potentially larger remote gain. Six groups need no
    // elaborate tree or shared decision mutex.
    for (int pass = 0; pass < (config_.allowCrossNuma ? 2 : 1); ++pass) {
      for (size_t candidate = 0; candidate < config_.groups.size(); ++candidate) {
        if (candidate == preferred || crossNuma(preferred, candidate) != bool(pass)) continue;
        auto candidateScore = score(candidate) + (pass ? config_.crossNumaPenalty : 0);
        if (candidateScore < result.chosenScore &&
            preferredScore - candidateScore > config_.minGain) {
          result.chosen = candidate;
          result.chosenScore = candidateScore;
          result.reason = pass ? Reason::CrossNumaSpill : Reason::LocalSpill;
        }
      }
      if (result.chosen != preferred) break;
    }
    return result;
  }

  // Reservations cover only not-yet-enqueued members of one selected chunk.
  // They make later choices see them; approximate concurrent readers can still
  // choose the same candidate before either producer publishes a reservation.
  void reserve(size_t group, size_t count, uint64_t estimate) {
    if (count > std::numeric_limits<uint64_t>::max() / estimate)
      throw std::overflow_error("RequestExecutor: batch estimate overflow");
    reservations_[group]->tasks.fetch_add(count, std::memory_order_relaxed);
    reservations_[group]->work.fetch_add(count * estimate, std::memory_order_relaxed);
  }
  void unreserve(size_t group, size_t count, uint64_t estimate) noexcept {
    reservations_[group]->work.fetch_sub(count * estimate, std::memory_order_relaxed);
    reservations_[group]->tasks.fetch_sub(count, std::memory_order_relaxed);
  }
  bool submit(Context context, folly::Func task, TaskOptions options, bool drainCapability) {
    validateTask(task, options);
    operations_.fetch_add(1);
    auto leave = folly::makeGuard([&] { operations_.fetch_sub(1); notifyDrain(); });
    if (!maySubmit(drainCapability)) return false;
    const auto selected = choose(context, options);
    reserve(selected.chosen, 1, options.estimatedCpuUs);
    auto reserved = folly::makeGuard([&] { unreserve(selected.chosen, 1, options.estimatedCpuUs); });
    return enqueue(std::move(context), std::move(task), options, selected);
  }
  size_t batch(Context context, std::vector<folly::Func> tasks,
               TaskOptions options, bool drainCapability) {
    options.isFork = true;
    for (const auto& task : tasks) validateTask(task, options);
    if (options.sequentialTags && !tasks.empty() &&
        tasks.size() - 1 > std::numeric_limits<uint64_t>::max() - options.tag)
      throw std::overflow_error("RequestExecutor: batch tag overflow");
    operations_.fetch_add(1);
    auto leave = folly::makeGuard([&] { operations_.fetch_sub(1); notifyDrain(); });
    if (!maySubmit(drainCapability)) return 0;
    size_t accepted = 0;
    while (accepted < tasks.size()) {
      const auto chunk = std::min(config_.batchChunk, tasks.size() - accepted);
      const auto selected = choose(context, options);
      reserve(selected.chosen, chunk, options.estimatedCpuUs);
      size_t remaining = chunk;
      auto reserved = folly::makeGuard([&] { unreserve(selected.chosen, remaining, options.estimatedCpuUs); });
      for (size_t i = 0; i < chunk; ++i) {
        bool ok;
        auto taskOptions = options;
        if (options.sequentialTags) taskOptions.tag += accepted;
        try { ok = enqueue(context, std::move(tasks[accepted]), taskOptions, selected); }
        catch (...) {
          // A prefix can already be executing. Report its exact length;
          // callers must cancel/complete the unscheduled suffix explicitly.
          return accepted;
        }
        unreserve(selected.chosen, 1, options.estimatedCpuUs);
        --remaining;
        if (!ok) return accepted;
        ++accepted;
      }
    }
    return accepted;
  }
  bool enqueue(Context context, folly::Func task, const TaskOptions& options,
               const Selection& selected) {
    Observation observation;
    if (config_.observer) {
      const auto before = pool_.readLoad(selected.chosen);
      observation.tag = options.tag;
      observation.color = context.request->color;
      observation.home = context.request->home;
      observation.source = context.source;
      observation.chosen = selected.chosen;
      observation.migrated = context.source != selected.chosen;
      observation.crossNuma = crossNuma(context.source, selected.chosen);
      observation.hops = context.hops + observation.migrated;
      observation.preferredScore = selected.preferredScore;
      observation.chosenScore = selected.chosenScore;
      observation.reason = selected.reason;
      observation.queueBefore = before.queueDepth;
      observation.outstandingBefore = before.outstanding;
      observation.submittedNs = now();
    }
    context.hops += context.source != selected.chosen;
    context.source = selected.chosen;
    context.hasParent = true;
    BusinessExecutor::Hint hint;
    hint.home = selected.chosen;
    hint.estimatedCpuUs = options.estimatedCpuUs;
    activeWork_.fetch_add(1);
    auto rollback = folly::makeGuard([&] { releaseWork(); });
    const bool accepted = pool_.add(hint,
        [this, context = std::move(context), task = std::move(task), observation](const auto&) mutable {
          InstalledContext installed{this, std::move(context)};
          auto* previous = current_;
          current_ = &installed;
          if (config_.observer) observation.startNs = now();
          auto finish = folly::makeGuard([&] {
            if (config_.observer) {
              observation.finishNs = now();
              try { config_.observer(observation); }
              catch (...) { observerFailures_.fetch_add(1, std::memory_order_relaxed); }
            }
            current_ = previous;
            releaseWork();
          });
          try { task(); }
          catch (...) { observation.failed = true; throw; }
        });
    if (accepted) rollback.dismiss();
    return accepted;
  }
  void releaseWork() noexcept { activeWork_.fetch_sub(1); notifyDrain(); }
  void notifyDrain() noexcept {
    if (state_.load() != State::Open) {
      std::lock_guard lock(drainMutex_);
      drained_.notify_all();
    }
  }

  const Config config_;
  BusinessExecutor pool_;
  std::shared_ptr<Identity> identity_;
  std::vector<std::unique_ptr<ReservationCount>> reservations_;
  std::atomic<uint64_t> sequence_{0}, operations_{0}, activeWork_{0}, observerFailures_{0};
  std::atomic<State> state_{State::Open};
  std::mutex joinMutex_, drainMutex_;
  std::condition_variable drained_;
};

} // namespace executor_demo

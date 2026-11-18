#pragma once

#include <executor_demo/LoadPolicy.h>

namespace executor_demo {

enum class AdmissionFailure { None, InvalidSample, OverLimit };
struct AdmissionDecision {
  bool allowed{false};
  AdmissionFailure failure{AdmissionFailure::InvalidSample};
  double before{0}, after{0};
};

// Placement permission is separate from the source/target ranking score. A
// policy must use published samples only: no I/O, allocation, locks or waiting.
// It constrains a newly selected destination, not acceptance into preferred.
class AdmissionPolicy {
 public:
  virtual ~AdmissionPolicy() = default;
  virtual AdmissionDecision evaluate(const LoadSample&, const ScoreContext&,
                                     size_t incomingTasks) const noexcept = 0;
  virtual LoadInputs needs() const noexcept { return LoadInputs::Counters; }
  virtual const char* name() const noexcept = 0;
  virtual const char* units() const noexcept = 0;
};

class OutstandingAdmissionPolicy final : public AdmissionPolicy {
 public:
  explicit OutstandingAdmissionPolicy(double limit) : limit_(limit) {
    if (!std::isfinite(limit) || limit <= 0)
      throw std::invalid_argument("OutstandingAdmissionPolicy: limit must be finite and positive");
  }
  const char* name() const noexcept override { return "outstanding-after"; }
  const char* units() const noexcept override { return "outstanding_reserved_incoming_tasks_per_capacity"; }
  double limit() const noexcept { return limit_; }
  AdmissionDecision evaluate(const LoadSample& sample, const ScoreContext& context,
                             size_t incomingTasks) const noexcept override {
    if (!load_policy_detail::validCapacity(sample) || !sample.outstandingValid || !incomingTasks)
      return {};
    // Convert terms before addition: uint64 counters may not be summed safely.
    // This is an approximate observation, not an atomic admission transaction.
    const auto count = static_cast<long double>(sample.outstanding) + context.reservedTasks;
    const auto before = count / sample.capacity;
    const auto after = (count + incomingTasks) / sample.capacity;
    if (!std::isfinite(double(before)) || !std::isfinite(double(after))) return {};
    const bool allowed = after <= static_cast<long double>(limit_);
    return {allowed, allowed ? AdmissionFailure::None : AdmissionFailure::OverLimit,
            double(before), double(after)};
  }
 private:
  const double limit_;
};

} // namespace executor_demo

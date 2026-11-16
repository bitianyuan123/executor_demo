#pragma once

#include <cstddef>
#include <stdexcept>

namespace independent {
// Capacity is an accounting divisor, not an estimate of physical CPU throughput.
inline double normalizedScore(double value, double capacity, bool isWork) {
  if (!(value >= 0) || !(capacity > 0)) {
    throw std::invalid_argument("invalid load or capacity");
  }
  return value / capacity / (isWork ? 1000.0 : 1.0);
}

struct Decision {
  std::size_t pool;
  double homeScore;
  double selectedScore;
  std::size_t candidates;
  bool sourceOverThreshold = false;
  bool noEligibleTarget = false;
  bool noImprovement = false;
  std::size_t eligibleCandidates = 0;
};

// readScore samples each pool once. Equal candidate scores are resolved by the
// cyclic order beginning at tieStart, so repeated bursts do not favor pool 0.
// No atomic multi-pool snapshot or cross-producer reservation is implied.
template<class ReadScore>
Decision chooseThreshold(std::size_t home, std::size_t pools, double threshold,
                         double minImprovement, std::size_t tieStart,
                         ReadScore&& readScore, double acceptThreshold = -1) {
  if (!pools || home >= pools || threshold < 0 || minImprovement < 0 ||
      (acceptThreshold < 0 && acceptThreshold != -1)) {
    throw std::invalid_argument("invalid threshold routing configuration");
  }
  const double homeScore = readScore(home);
  Decision decision{home, homeScore, homeScore, 1};
  if (!(homeScore > threshold)) return decision;
  decision.sourceOverThreshold = true;
  double best = homeScore - minImprovement;
  for (std::size_t offset = 0; offset < pools; ++offset) {
    const std::size_t pool = (tieStart % pools + offset) % pools;
    if (pool == home) continue;
    const double score = readScore(pool);
    ++decision.candidates;
    // The acceptance test is a pre-reservation snapshot, not a hard cap.
    // Only this demo's single producer reserves before the next routing decision.
    if (acceptThreshold >= 0 && !(score < acceptThreshold)) continue;
    ++decision.eligibleCandidates;
    if (score < best) {
      best = score;
      decision.pool = pool;
      decision.selectedScore = score;
    }
  }
  decision.noEligibleTarget = acceptThreshold >= 0 && decision.eligibleCandidates == 0;
  decision.noImprovement = decision.pool == home && !decision.noEligibleTarget;
  return decision;
}
} // namespace independent

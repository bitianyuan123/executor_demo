#include "../src/LoadRouting.h"

#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
void require(bool ok, const char* message) {
  if (!ok) throw std::runtime_error(message);
}
independent::Decision decide(const std::vector<double>& scores, double threshold=.8,
                             double acceptance=.5, double improvement=0) {
  std::vector<unsigned> reads(scores.size());
  auto decision=independent::chooseThreshold(0,scores.size(),threshold,improvement,0,
    [&](std::size_t pool) {
      require(++reads[pool]==1,"pool was sampled more than once");
      return scores[pool];
    },acceptance);
  std::size_t count=0;
  for(auto n:reads)count+=n;
  require(count==decision.candidates,"sample count does not match candidates");
  return decision;
}
}

int main() {
  try {
    const auto atSource=decide({.8,0});
    require(atSource.pool==0 && !atSource.sourceOverThreshold && atSource.candidates==1,
            "source gate must be strictly greater than threshold");
    require(!atSource.noEligibleTarget && !atSource.noImprovement,
            "untriggered routing must not count fallback reasons");
    const auto atTarget=decide({.8125,.5,.6});
    require(atTarget.sourceOverThreshold && atTarget.pool==0 && atTarget.noEligibleTarget,
            "target gate must be strictly less than threshold");
    require(!atTarget.noImprovement && atTarget.eligibleCandidates==0,
            "ineligible target fallback reason mismatch");
    const auto accepted=decide({13./16,7./16,.5});
    require(accepted.pool==1 && accepted.eligibleCandidates==1,
            "N=13 should spill to N=7 at C=16");
    require(!accepted.noEligibleTarget && !accepted.noImprovement,
            "successful decision must not count fallback reasons");
    const auto insufficient=decide({.9,.4},.8,.5,.5);
    require(insufficient.pool==0 && insufficient.noImprovement && !insufficient.noEligibleTarget,
            "eligible target with insufficient improvement is distinct from no eligible target");
    const auto zeroAcceptance=decide({1,0},.8,0);
    require(zeroAcceptance.pool==0 && zeroAcceptance.noEligibleTarget,
            "zero acceptance forbids even zero-load targets with strict comparison");
    const auto disabled=decide({2,1},.8,-1);
    require(disabled.pool==1 && disabled.eligibleCandidates==1,
            "disabled target gate must preserve prior lighter-pool routing");
    const auto busy=decide({2,3},.8,-1);
    require(busy.pool==0 && busy.noImprovement && !busy.noEligibleTarget,
            "disabled acceptance must not report target rejection");
    const auto singleton=decide({1});
    require(singleton.noEligibleTarget && singleton.pool==0,
            "no target pools must fall back without losing the task");
    bool rejected=false;
    try { decide({1,0},.8,-2); } catch(const std::invalid_argument&) { rejected=true; }
    require(rejected,"only -1 disables the target gate");
    std::cout<<"acceptance boundary and fallback checks passed\n";
    return 0;
  } catch(const std::exception& error) {
    std::cerr<<error.what()<<'\n';
    return 1;
  }
}

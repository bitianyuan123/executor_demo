#include "LoadRouting.h"
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
independent::Decision decide(const std::vector<double>& loads, std::size_t home,
                             double threshold, double improvement=0,
                             std::size_t tieStart=0) {
  std::vector<unsigned> reads(loads.size());
  auto decision=independent::chooseThreshold(home,loads.size(),threshold,improvement,tieStart,
    [&](std::size_t pool) { require(++reads[pool]==1,"pool sampled twice"); return loads[pool]; });
  unsigned count=0;
  for(auto n:reads)count+=n;
  require(count==decision.candidates,"candidate count mismatch");
  return decision;
}
}
int main() {
  try {
    require(independent::normalizedScore(32,16,false)==2,"count capacity normalization");
    require(independent::normalizedScore(1600000,16,true)==100,"work units are milliseconds");
    auto atThreshold=decide({1,0,0},0,1);
    require(atThreshold.pool==0 && atThreshold.candidates==1,"strict threshold violated");
    auto empty=decide({0,0,0},0,0);
    require(empty.pool==0 && empty.candidates==1,"zero threshold must retain empty home");
    auto trigger=decide({3,2,1},0,1);
    require(trigger.pool==2 && trigger.candidates==3 && trigger.selectedScore==1,"lowest load not selected");
    auto allBusy=decide({3,3,4},0,1);
    require(allBusy.pool==0 && allBusy.selectedScore==3,"no improvement must stay home");
    auto exactImprovement=decide({3,2},0,1,1);
    require(exactImprovement.pool==0,"min improvement must be strict");
    auto sufficientImprovement=decide({3,1.9},0,1,1);
    require(sufficientImprovement.pool==1,"sufficient improvement missed");
    require(decide({3,0,0},0,1,0,1).pool==1,"first cyclic tie choice");
    require(decide({3,0,0},0,1,0,2).pool==2,"rotated cyclic tie choice");
    require(decide({3,0,0},0,1,0,5).pool==2,"cyclic start wraps");
    auto singleton=decide({7},0,1);
    require(singleton.pool==0 && singleton.candidates==1,"singleton fallback");
    auto differentHome=decide({0,2,8},2,1,0,2);
    require(differentHome.pool==0 && differentHome.homeScore==8,"nonzero home mismatch");
    // The same raw task count need not mean the same score on unequal masks.
    std::vector<double> raw{12,12,10}, capacities{2,8,2};
    auto heterogeneous=independent::chooseThreshold(0,3,1,0,0,[&](std::size_t p) {
      return independent::normalizedScore(raw[p],capacities[p],false);
    });
    require(heterogeneous.pool==1,"capacity-aware minimum");
    std::cout<<"routing core: deterministic threshold, fallback, rotation and units passed\n";
    return 0;
  } catch(const std::exception& error) {
    std::cerr<<error.what()<<'\n';return 1;
  }
}

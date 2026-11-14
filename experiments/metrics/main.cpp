// Snapshot read cost and accounting write cost, deliberately not a DAG demo.
#include <folly/executors/CPUThreadPoolExecutor.h>
#include <folly/init/Init.h>
#include <folly/ScopeGuard.h>
#include <algorithm>
#include <atomic>
#include <barrier>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
using Pool = folly::CPUThreadPoolExecutor;
static uint64_t observed = 0;
struct alignas(128) Slot { std::atomic<uint64_t> value{0}; };

template<class F>
void measure(std::ostream& out, const std::string& state, size_t threads,
             const std::string& name, size_t samples, size_t batch, F&& operation) {
  std::vector<double> ns;
  for (size_t warm = 0; warm < 20; ++warm) observed ^= operation();
  for (size_t s = 0; s < samples; ++s) {
    uint64_t result = 0;
    const auto start = Clock::now();
    for (size_t b = 0; b < batch; ++b) result += operation();
    const auto stop = Clock::now();
    observed ^= result;
    ns.push_back(std::chrono::duration<double, std::nano>(stop-start).count()/batch);
  }
  std::sort(ns.begin(), ns.end());
  auto quantile = [&](double p) { return ns[size_t(std::ceil(p*ns.size()))-1]; };
  out << state << ',' << threads << ',' << name << ',' << samples << ',' << batch << ','
      << std::accumulate(ns.begin(), ns.end(), 0.0)/ns.size() << ','
      << quantile(.5) << ',' << quantile(.99) << ',' << ns.front() << ',' << ns.back() << '\n';
}

void snapshots(std::ostream& out, size_t threads, size_t samples, size_t batch,
               const std::string& queue) {
  auto q = queue == "lifo" ? Pool::makeLifoSemQueue() : Pool::makeDefaultQueue();
  Pool pool(std::make_pair(threads, threads), std::move(q));
  alignas(128) std::atomic<uint64_t> pending{0}, inflight{0}, work{0};
  std::atomic<bool> stop{false};
  auto cleanup=folly::makeGuard([&] {stop.store(true);pool.join();});
  auto readAll = [&](const std::string& state) {
    measure(out,state,threads,"loop_baseline",samples,batch,[] {
      uint64_t value = 1;
      asm volatile("" : "+r"(value) : : "memory");
      return value;
    });
    measure(out,state,threads,"native_queue",samples,batch,[&] {return pool.getTaskQueueSize();});
    measure(out,state,threads,"relaxed_one_load",samples,batch,[&] {return pending.load(std::memory_order_relaxed);});
    measure(out,state,threads,"relaxed_three_loads",samples,batch,[&] {
      return pending.load(std::memory_order_relaxed)+inflight.load(std::memory_order_relaxed)+work.load(std::memory_order_relaxed);
    });
    measure(out,state,threads,"pending_api",samples,batch,[&] {return pool.getPendingTaskCount();});
    measure(out,state,threads,"pool_stats_light",samples,batch,[&] {return pool.getPoolStatsLight().activeThreadCount;});
    measure(out,state,threads,"pool_stats",samples,batch,[&] {return pool.getPoolStats().activeThreadCount;});
    measure(out,state,threads,"used_cpu_time",samples,std::min<size_t>(batch,10),[&] {return pool.getUsedCpuTime().count();});
  };
  readAll("idle");
  // Block consumers with CPU work, not a mutex or sleeping worker. This is a
  // separate diagnostic: reader wall cost can include preemption on a busy host.
  const size_t count=threads*8;
  pending.store(count); work.store(count*1000);
  for (size_t i=0;i<count;++i) pool.add([&] {
    pending.fetch_sub(1,std::memory_order_relaxed);
    inflight.fetch_add(1,std::memory_order_relaxed);
    uint64_t x=1;
    while (!stop.load(std::memory_order_relaxed)) {
      for (size_t j=0;j<256;++j) x=x*6364136223846793005ULL+1;
      asm volatile("" : "+r"(x) : : "memory");
    }
    work.fetch_sub(1000,std::memory_order_relaxed);
    inflight.fetch_sub(1,std::memory_order_relaxed);
  });
  const auto deadline=Clock::now()+std::chrono::seconds(5);
  while (inflight.load()!=threads && Clock::now()<deadline) std::this_thread::yield();
  if (inflight.load()!=threads) {
    throw std::runtime_error("workers did not start");
  }
  readAll("cpu_busy_with_backlog");
}

void writes(std::ostream& out, size_t producers, size_t operations, size_t repeat) {
  for (const std::string mode : {"shared_fetch_add", "padded_fetch_add", "padded_store_every_64"}) {
    std::atomic<uint64_t> shared{0};
    auto slots=std::make_unique<Slot[]>(producers);
    std::barrier ready(static_cast<std::ptrdiff_t>(producers+1));
    std::vector<std::thread> workers;
    for(size_t p=0;p<producers;++p) workers.emplace_back([&,p] {
      ready.arrive_and_wait();
      uint64_t owner=0;
      if(mode=="shared_fetch_add") {
        for(size_t i=0;i<operations;++i) shared.fetch_add(1,std::memory_order_relaxed);
      } else if(mode=="padded_fetch_add") {
        for(size_t i=0;i<operations;++i) slots[p].value.fetch_add(1,std::memory_order_relaxed);
      } else {
        for(size_t i=0;i<operations;++i) {
          ++owner;
          // Prevent the compiler replacing the synthetic task-count loop by
          // its final value. This mode explicitly permits a stale snapshot.
          asm volatile("" : "+r"(owner) : : "memory");
          if ((i&63)==63) slots[p].value.store(owner,std::memory_order_relaxed);
        }
      }
      if(mode=="padded_store_every_64") slots[p].value.store(owner,std::memory_order_relaxed);
    });
    const auto start=Clock::now();ready.arrive_and_wait();
    for(auto& t:workers)t.join();
    const auto elapsed=std::chrono::duration<double,std::nano>(Clock::now()-start).count();
    uint64_t total=shared.load();
    for(size_t p=0;p<producers;++p)total+=slots[p].value.load();
    if(total!=producers*operations)throw std::runtime_error("counter conservation failure");
    out<<repeat<<','<<mode<<','<<producers<<','<<operations<<','<<elapsed<<','
       <<elapsed/(producers*operations)<<','<<total<<'\n';
  }
}

int main(int argc,char** argv) {
  try {
    std::string output,queue="default";
    size_t threads=4,samples=80,batch=100,operations=100000,repeats=3;
    for(int i=1;i<argc;++i) {
      std::string k=argv[i];
      if(k=="--help") {std::cout<<"metric_cost_bench --output NEW_DIR [--threads 4 --samples 80 --batch 100 --operations 100000 --repeats 3 --queue default|lifo]\n";return 0;}
      if(i+1==argc)throw std::invalid_argument("missing option value");
      std::string v=argv[++i];
      if(k=="--output")output=v;
      else if(k=="--queue")queue=v;
      else {
        size_t consumed=0;
        if(v.empty()||v[0]=='-')throw std::invalid_argument("positive integer required");
        auto n=std::stoull(v,&consumed);
        if(consumed!=v.size()||!n||n>100000000)throw std::invalid_argument("invalid integer");
        if(k=="--threads")threads=n;else if(k=="--samples")samples=n;
        else if(k=="--batch")batch=n;else if(k=="--operations")operations=n;
        else if(k=="--repeats")repeats=n;else throw std::invalid_argument("unknown option: "+k);
      }
    }
    if(output.empty()||threads>1024||(queue!="default"&&queue!="lifo"))throw std::invalid_argument("invalid output/threads/queue");
    int follyArgc=1;
    char** follyArgv=argv;
    folly::Init init(&follyArgc,&follyArgv,false);
    if(std::filesystem::exists(output))throw std::invalid_argument("output directory already exists");
    std::filesystem::create_directories(output);
    std::ofstream reads(output+"/reads.csv"),updates(output+"/updates.csv");
    reads.exceptions(std::ios::failbit|std::ios::badbit); updates.exceptions(std::ios::failbit|std::ios::badbit);
    reads<<"state,threads,metric,samples,batch,mean_ns_per_call,p50_batch_ns_per_call,p99_batch_ns_per_call,min_batch_ns_per_call,max_batch_ns_per_call\n";
    updates<<"repeat,mode,producers,operations_per_producer,elapsed_ns,aggregate_ns_per_update,final_count\n";
    snapshots(reads,threads,samples,batch,queue);
    for(size_t r=0;r<repeats;++r)for(size_t p:{1U,2U,4U})writes(updates,p,operations,r);
    reads.close();updates.close();
    std::ofstream meta(output+"/metadata.json.tmp");
    meta.exceptions(std::ios::failbit|std::ios::badbit);
    meta<<"{\"completed\":true,\"queue\":\""<<queue<<"\",\"threads\":"<<threads
        <<",\"read_p99_is_batch_mean_not_individual_call\":true,\"sink\":"<<observed<<"}\n";
    meta.close();
    std::filesystem::rename(output+"/metadata.json.tmp",output+"/metadata.json");
    std::cout<<output<<'\n';
  } catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}
}

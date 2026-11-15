#include <algorithm>
#include <chrono>
#include <executor_demo/FollyActiveLoadProvider.h>
#include <executor_demo/LinuxSchedstatLoadProvider.h>
#include <future>
#include <iomanip>
#include <iostream>
#include <latch>
#include <numeric>
#include <sys/syscall.h>

using Clock = std::chrono::steady_clock;
static double sink = 0;
template <class F>
void measure(const char *label, size_t repetitions, size_t batch, F operation) {
  std::vector<double> samples;
  for (size_t round = 0; round < repetitions; ++round) {
    double value = 0;
    auto begin = Clock::now();
    for (size_t i = 0; i < batch; ++i) {
      value += operation();
      asm volatile("" : "+g"(value) : : "memory");
    }
    samples.push_back(
        std::chrono::duration<double, std::nano>(Clock::now() - begin).count() /
        batch);
    sink += value;
  }
  std::sort(samples.begin(), samples.end());
  std::cout << label << ',' << batch << ','
            << std::accumulate(samples.begin(), samples.end(), 0.0) /
                   samples.size()
            << ',' << samples[samples.size() / 2] << ','
            << samples[size_t(std::ceil(samples.size() * .99)) - 1] << '\n';
}
int main(int argc, char **argv) {
  try {
    size_t threads = 4, iterations = 2000, scans = 5;
    for (int i = 1; i < argc; ++i) {
      std::string key = argv[i];
      if (i + 1 >= argc)
        throw std::invalid_argument(
            "expected --threads/--iterations/--scans value");
      std::string value = argv[++i];
      size_t end = 0;
      auto n = std::stoull(value, &end);
      if (value.empty() || value[0] == '-' || end != value.size() || !n)
        throw std::invalid_argument("positive integer required");
      if (key == "--threads")
        threads = n;
      else if (key == "--iterations")
        iterations = n;
      else if (key == "--scans")
        scans = n;
      else
        throw std::invalid_argument("unknown option");
    }
    if (threads > 1024 || iterations > 1000000 || scans > 1000)
      throw std::invalid_argument("probe bounds exceeded");
    folly::CPUThreadPoolExecutor pool(std::make_pair(threads, threads));
    executor_demo::FollyActiveLoadProvider active({&pool}, {double(threads)});
    std::vector<pid_t> tids(threads);
    std::latch ready(threads), finished(threads);
    std::promise<void> release;
    auto permit = release.get_future().share();
    for (size_t i = 0; i < threads; ++i)
      pool.add([&, i] {
        tids[i] = pid_t(::syscall(SYS_gettid));
        ready.count_down();
        permit.wait();
        finished.count_down();
      });
    ready.wait();
    release.set_value();
    finished.wait();
    executor_demo::LinuxSchedstatLoadProvider sampler({}, 1);
    sampler.setThreadGroups({tids});
    sampler.sampleOnce();
    executor_demo::ActiveLoadScorer activeScorer;
    const executor_demo::LoadScorer *virtualScorer = &activeScorer;
    std::cout << std::fixed << std::setprecision(3)
              << "metric,batch,mean_ns,p50_batch_mean_ns,p99_batch_mean_ns\n";
    measure("loop_baseline", 40, iterations, [] {
      double v = 1;
      asm volatile("" : "+g"(v) : : "memory");
      return v;
    });
    measure("native_queue", 40, iterations,
            [&] { return double(pool.getTaskQueueSize()); });
    measure("folly_stats_light_scan", 40, iterations,
            [&] { return double(pool.getPoolStatsLight().activeThreadCount); });
    measure("registered_active_provider_read", 40, iterations,
            [&] { return double(active.read(0).callbackInflight); });
    measure("registered_active_read_and_score", 40, iterations,
            [&] { return virtualScorer->score(active.read(0), {}).value; });
    measure("cached_schedstat_read", 40, iterations,
            [&] { return sampler.read(0).sampledCpuCores; });
    measure("proc_scan_all_registered_threads", scans, 1, [&] {
      sampler.sampleOnce();
      return double(sampler.stats().scans);
    });
    auto stats = sampler.stats();
    std::cout << "threads=" << threads << " scan_count=" << stats.scans
              << " scan_cpu_ns=" << stats.totalScanCpuNs
              << " scan_wall_ns=" << stats.totalScanWallNs
              << " max_scan_wall_ns=" << stats.maxScanWallNs
              << " schedstats_enabled=" << stats.runqueueAccountingEnabled
              << " sink=" << sink << '\n';
    std::cout << "Idle warmed-cache read microbenchmark; batch p99 is not "
                 "individual-call p99. Observer writes, contention and "
                 "end-to-end routing overhead require separate paired runs.\n";
    pool.join();
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
}

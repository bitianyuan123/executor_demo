#pragma once

#include <algorithm>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace executor_demo {

// A fixed-work data-dependency kernel. The caller owns DAG synchronization:
// initialize() precedes every run(), a branch runs sequentially, and its scalar
// may be read by a join only after that branch completes. Distinct branches may
// run concurrently. There are no atomics in the data-access loop.
class LocalityWorkload {
 public:
  enum class Model { None, RequestReadOnly, BranchReadWrite, Mixed };

  struct Config {
    Model model{Model::None};
    size_t branches{1};
    size_t sharedBytes{0};
    size_t branchBytes{0};
    // Includes page-rounded payload and the branch-state array, not allocator
    // bookkeeping or the object itself. The driver also bounds request count.
    size_t maxBytes{256ULL * 1024 * 1024};
    uint64_t seed{1};
    uint64_t requestId{0};
  };

  struct Result {
    uint64_t checksum{0};
    uint64_t computeIterations{0};
    uint64_t readWords{0};
    uint64_t writtenWords{0};
  };

  struct PageSample {
    std::string status{"unknown"}; // complete, partial, unknown, or empty
    int errnoValue{0};
    size_t sampledPages{0};
    size_t knownPages{0};
    std::map<int, size_t> nodeCounts;
    // Nonnegative values are actual NUMA node IDs; negatives are kernel errors.
    std::vector<int> perPageStatus;
  };

  // Injectable only for deterministic error-path tests/platform adapters.
  // A query must have move_pages(query) semantics: it must not move pages.
  using PageQuery = long (*)(unsigned long, void**, int*);

  explicit LocalityWorkload(Config config) : config_(config) {
    const long pageSize = ::sysconf(_SC_PAGESIZE);
    if (pageSize <= 0) throw std::runtime_error("cannot determine page size");
    pageBytes_ = static_cast<size_t>(pageSize);
    if (!config_.branches || !config_.maxBytes)
      throw std::invalid_argument("positive branches and maxBytes required");
    if (config_.sharedBytes % sizeof(uint64_t) ||
        config_.branchBytes % sizeof(uint64_t))
      throw std::invalid_argument("data sizes must be multiples of uint64_t");
    const bool shared = hasShared();
    const bool branch = hasBranch();
    if ((shared != (config_.sharedBytes != 0)) ||
        (branch != (config_.branchBytes != 0)))
      throw std::invalid_argument("data sizes must match the selected model");
    if (config_.model != Model::None && !shared && !branch)
      throw std::invalid_argument("unknown locality model");

    sharedStride_ = roundPages(config_.sharedBytes);
    branchStride_ = roundPages(config_.branchBytes);
    mappedBytes_ = checkedAdd(sharedStride_, checkedMultiply(branchStride_, config_.branches));
    budgetBytes_ = checkedAdd(mappedBytes_, checkedMultiply(sizeof(BranchState), config_.branches));
    if (budgetBytes_ > config_.maxBytes)
      throw std::invalid_argument("locality workload exceeds maxBytes");
    branches_.resize(config_.branches);
    if (mappedBytes_) {
      void* result = ::mmap(nullptr, mappedBytes_, PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
      if (result == MAP_FAILED) throw std::runtime_error("locality mmap failed: errno=" + std::to_string(errno));
      mapping_ = static_cast<unsigned char*>(result);
    }
    // No payload address is read or written here. The anonymous mapping is
    // first faulted by initialize(), which the driver schedules on request home.
  }

  ~LocalityWorkload() {
    if (mapping_) ::munmap(mapping_, mappedBytes_);
  }
  LocalityWorkload(const LocalityWorkload&) = delete;
  LocalityWorkload& operator=(const LocalityWorkload&) = delete;
  LocalityWorkload(LocalityWorkload&&) = delete;
  LocalityWorkload& operator=(LocalityWorkload&&) = delete;

  const Config& config() const noexcept { return config_; }
  size_t allocatedBytes() const noexcept { return mappedBytes_; }
  size_t budgetBytes() const noexcept { return budgetBytes_; }
  size_t pageBytes() const noexcept { return pageBytes_; }
  bool initialized() const noexcept { return initialized_; }

  void initialize() {
    if (initialized_) throw std::logic_error("locality workload initialized twice");
    // Touch padding too, so every mapped page is really faulted here. No mbind
    // is applied: first-touch is observed, never represented as guaranteed bind.
    for (size_t word = 0; word < sharedStride_ / sizeof(uint64_t); ++word)
      sharedData()[word] = mix(baseSeed() ^ mix(word));
    for (size_t branch = 0; branch < config_.branches; ++branch) {
      auto& state = branches_[branch];
      state.checksum = mix(baseSeed() ^ mix(branch + 1));
      state.nextStage = 0;
      for (size_t word = 0; word < branchStride_ / sizeof(uint64_t); ++word)
        branchData(branch)[word] = mix(state.checksum ^ mix(word));
    }
    initialized_ = true;
  }

  Result run(size_t branch, uint64_t stage, uint64_t computeIterations,
             uint64_t memoryAccesses) {
    requireInitialized();
    auto& current = checkedBranch(branch);
    if (stage != current.nextStage || stage == std::numeric_limits<uint64_t>::max())
      throw std::invalid_argument("branch stages must start at zero and be contiguous");
    if (config_.model == Model::None && memoryAccesses)
      throw std::invalid_argument("None model requires zero memory accesses");
    const uint64_t readPlanes = uint64_t(hasShared()) + uint64_t(hasBranch());
    if (readPlanes && memoryAccesses > std::numeric_limits<uint64_t>::max() / readPlanes)
      throw std::overflow_error("memory access counters overflow");

    uint64_t value = mix(current.checksum ^ mix(stage) ^ baseSeed());
    for (uint64_t i = 0; i < computeIterations; ++i) {
      value ^= value << 13;
      value ^= value >> 7;
      value ^= value << 17;
    }
    const size_t sharedWords = config_.sharedBytes / sizeof(uint64_t);
    const size_t branchWords = config_.branchBytes / sizeof(uint64_t);
    for (uint64_t i = 0; i < memoryAccesses; ++i) {
      value = mix(value ^ i);
      if (sharedWords) value ^= sharedData()[value % sharedWords];
      if (branchWords) {
        auto& word = branchData(branch)[value % branchWords];
        // Each successor consumes both its predecessor's scalar and mutated
        // buffer. Other branches never read or write this buffer.
        value = mix(value ^ word);
        word = value;
      }
    }
    current.checksum = mix(value ^ computeIterations ^ mix(memoryAccesses));
    ++current.nextStage;
    return Result{current.checksum, computeIterations,
                  memoryAccesses * readPlanes, hasBranch() ? memoryAccesses : 0};
  }

  uint64_t branchChecksum(size_t branch) const {
    requireInitialized();
    return checkedBranch(branch).checksum;
  }
  uint64_t completedStages(size_t branch) const {
    requireInitialized();
    return checkedBranch(branch).nextStage;
  }
  uint64_t finalChecksum() const {
    requireInitialized();
    uint64_t result = mix(baseSeed());
    // This is the actual join: consume each branch's final scalar after its
    // dependency has completed, in logical branch order, never arrival order.
    for (size_t branch = 0; branch < branches_.size(); ++branch)
      result = mix(result ^ mix(branch + 1) ^ branches_[branch].checksum ^
                   mix(branches_[branch].nextStage));
    return result;
  }

  PageSample samplePageLocations(size_t maxPages = 64, PageQuery query = systemPageQuery) const {
    if (!maxPages || !query) throw std::invalid_argument("positive maxPages and page query required");
    PageSample result;
    const size_t totalPages = mappedBytes_ / pageBytes_;
    if (!totalPages) { result.status = "empty"; return result; }
    result.sampledPages = std::min(totalPages, maxPages);
    std::vector<void*> pages;
    pages.reserve(result.sampledPages);
    for (size_t i = 0; i < result.sampledPages; ++i) {
      // Sample at a fixed page stride without a potentially overflowing i*N.
      // This is a sample of the mapping, not a page-placement census.
      const size_t index = i * (totalPages / result.sampledPages);
      pages.push_back(mapping_ + index * pageBytes_);
    }
    result.perPageStatus.assign(result.sampledPages, -ENOENT);
    errno = 0;
    const long queryResult = query(static_cast<unsigned long>(pages.size()),
                                   pages.data(), result.perPageStatus.data());
    if (queryResult < 0) {
      result.errnoValue = errno ? errno : EIO;
      result.perPageStatus.assign(result.sampledPages, -result.errnoValue);
      return result;
    }
    for (int node : result.perPageStatus) {
      if (node < 0) continue;
      ++result.knownPages;
      ++result.nodeCounts[node];
    }
    if (result.knownPages == result.sampledPages) result.status = "complete";
    else if (result.knownPages) result.status = "partial";
    return result;
  }

 private:
  // Conservative separation of scalar writes; payload branches are page-aligned.
  struct alignas(128) BranchState {
    uint64_t checksum{0};
    uint64_t nextStage{0};
  };
  static uint64_t mix(uint64_t value) noexcept {
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
  }
  uint64_t baseSeed() const noexcept { return mix(config_.seed ^ mix(config_.requestId)); }
  bool hasShared() const noexcept {
    return config_.model == Model::RequestReadOnly || config_.model == Model::Mixed;
  }
  bool hasBranch() const noexcept {
    return config_.model == Model::BranchReadWrite || config_.model == Model::Mixed;
  }
  static size_t checkedAdd(size_t a, size_t b) {
    if (a > std::numeric_limits<size_t>::max() - b)
      throw std::overflow_error("locality allocation size overflow");
    return a + b;
  }
  static size_t checkedMultiply(size_t a, size_t b) {
    if (b && a > std::numeric_limits<size_t>::max() / b)
      throw std::overflow_error("locality allocation size overflow");
    return a * b;
  }
  size_t roundPages(size_t bytes) const {
    return bytes ? checkedMultiply(checkedAdd(bytes, pageBytes_ - 1) / pageBytes_, pageBytes_) : 0;
  }
  uint64_t* sharedData() const noexcept { return reinterpret_cast<uint64_t*>(mapping_); }
  uint64_t* branchData(size_t branch) const noexcept {
    return reinterpret_cast<uint64_t*>(mapping_ + sharedStride_ + branch * branchStride_);
  }
  BranchState& checkedBranch(size_t branch) {
    if (branch >= branches_.size()) throw std::out_of_range("branch index");
    return branches_[branch];
  }
  const BranchState& checkedBranch(size_t branch) const {
    if (branch >= branches_.size()) throw std::out_of_range("branch index");
    return branches_[branch];
  }
  void requireInitialized() const {
    if (!initialized_) throw std::logic_error("locality workload has not been initialized");
  }
  static long systemPageQuery(unsigned long count, void** pages, int* status) {
#if defined(__linux__) && defined(SYS_move_pages)
    return ::syscall(SYS_move_pages, 0, count, pages, nullptr, status, 0);
#else
    (void)count; (void)pages; (void)status;
    errno = ENOSYS;
    return -1;
#endif
  }

  Config config_;
  size_t pageBytes_{0}, sharedStride_{0}, branchStride_{0};
  size_t mappedBytes_{0}, budgetBytes_{0};
  unsigned char* mapping_{nullptr};
  std::vector<BranchState> branches_;
  bool initialized_{false};
};

} // namespace executor_demo

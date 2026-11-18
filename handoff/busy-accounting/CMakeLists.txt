cmake_minimum_required(VERSION 3.20)
project(folly_busy_handoff LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(folly CONFIG REQUIRED)
find_package(Threads REQUIRED)
include(CTest)

add_executable(native_busy_example example.cpp)
target_link_libraries(native_busy_example PRIVATE Folly::folly Threads::Threads)

if(BUILD_TESTING)
  add_test(NAME native_busy_example COMMAND native_busy_example)
  set_tests_properties(native_busy_example PROPERTIES TIMEOUT 30)
  find_package(GTest REQUIRED)
  set(FOLLY_SOURCE_DIR "" CACHE PATH "Source tree with the native busy patch applied")
  if(NOT EXISTS "${FOLLY_SOURCE_DIR}/folly/executors/test/CPUThreadPoolExecutorBusyTest.cpp")
    message(FATAL_ERROR "Set FOLLY_SOURCE_DIR to the patched Folly source tree; its focused test source is required")
  endif()
  add_executable(native_busy_test
    "${FOLLY_SOURCE_DIR}/folly/executors/test/CPUThreadPoolExecutorBusyTest.cpp"
    "${FOLLY_SOURCE_DIR}/folly/test/common/TestMain.cpp")
  target_link_libraries(native_busy_test PRIVATE Folly::folly GTest::gtest Threads::Threads)
  add_test(NAME native_busy COMMAND native_busy_test)
  set_tests_properties(native_busy PROPERTIES TIMEOUT 60)

  option(BUILD_UPSTREAM_CPU_REGRESSION "Build selected existing Folly CPU pool regressions" OFF)
  if(BUILD_UPSTREAM_CPU_REGRESSION)
    find_package(Boost REQUIRED COMPONENTS thread)
    if(NOT TARGET GTest::gmock)
      message(FATAL_ERROR "Upstream regression needs GMock. Set GTest_DIR to a matching local GTest/GMock installation, or leave BUILD_UPSTREAM_CPU_REGRESSION=OFF; focused tests remain available.")
    endif()
    add_executable(upstream_thread_pool_test
      "${FOLLY_SOURCE_DIR}/folly/executors/test/ThreadPoolExecutorTest.cpp"
      "${FOLLY_SOURCE_DIR}/folly/test/common/TestMain.cpp")
    target_link_libraries(upstream_thread_pool_test PRIVATE
      Folly::folly GTest::gtest GTest::gmock Boost::thread Threads::Threads)
    set(cpu_regression_filter
      "ThreadPoolExecutorTypedTest/1.Basic:ThreadPoolExecutorTypedTest/1.Join:ThreadPoolExecutorTypedTest/0.Basic:ThreadPoolExecutorTypedTest/0.Join:ThreadPoolExecutorTypedTest/0.Stop:ThreadPoolExecutorTypedTest/0.Destroy:ThreadPoolExecutorTypedTest/0.TaskObserver:ThreadPoolExecutorTypedTest/0.KeepAlive:ThreadPoolExecutorTypedTest/0.FuturePool:ThreadPoolExecutorTest.CPUExpiration:ThreadPoolExecutorTest.AddWithPriority:ThreadPoolExecutorTest.BlockingQueue:ThreadPoolExecutorTest.RequestContext:ThreadPoolExecutorTest.AddImplToleratesThreadCreationFailure:ThreadPoolExecutorTest.AddImplNoThreadsAndCreationFails:ThreadPoolExecutorTest.SetNumThreadsNoOpOnFailure_CPU:ThreadPoolExecutorTest.SetNumThreadsNoOpOnPartialFailure_CPU")
    add_test(NAME upstream_cpu_regression COMMAND upstream_thread_pool_test
      "--gtest_filter=${cpu_regression_filter}")
    set_tests_properties(upstream_cpu_regression PROPERTIES TIMEOUT 90)
  endif()
endif()

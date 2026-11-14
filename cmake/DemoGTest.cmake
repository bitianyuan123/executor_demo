# Tests stay required by default. Accept an explicit offline source tree as an
# alternative to a system package; never download dependencies during configure.
set(DEMO_GTEST_SOURCE_DIR "" CACHE PATH "Optional GoogleTest source tree for DAG tests")
if(DEMO_GTEST_SOURCE_DIR)
  if(NOT EXISTS "${DEMO_GTEST_SOURCE_DIR}/CMakeLists.txt" OR
     NOT EXISTS "${DEMO_GTEST_SOURCE_DIR}/googletest/include/gtest/gtest.h")
    message(FATAL_ERROR "DEMO_GTEST_SOURCE_DIR must be the GoogleTest source root (containing googletest/include/gtest/gtest.h)")
  endif()
  set(BUILD_GMOCK OFF CACHE BOOL "Build GoogleMock" FORCE)
  set(INSTALL_GTEST OFF CACHE BOOL "Install GoogleTest" FORCE)
  add_subdirectory("${DEMO_GTEST_SOURCE_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/googletest" EXCLUDE_FROM_ALL)
else()
  find_package(GTest QUIET)
endif()
if(NOT TARGET GTest::gtest_main)
  message(FATAL_ERROR
    "DAG tests require GoogleTest development headers and libraries. "
    "Install libgtest-dev (Debian/Ubuntu) or gtest-devel (where provided by your RPM distribution), "
    "set GTest_ROOT to an existing installation, or set DEMO_GTEST_SOURCE_DIR to a local source tree. "
    "Folly built with BUILD_TESTS=OFF does not supply this dependency. "
    "For an explicit build without validation use -DBUILD_TESTING=OFF. "
    "Stages metrics and independent do not require GTest. See docs/BUILD_TROUBLESHOOTING.md.")
endif()

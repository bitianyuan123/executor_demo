#!/usr/bin/env bash
# Sourced by the build entry points; keep test defaults and reporting consistent.
build_testing=${BUILD_TESTING:-ON}
build_testing=${build_testing^^}
case "$build_testing" in
  ON|OFF) ;;
  *) echo 'BUILD_TESTING must be ON or OFF.' >&2; exit 2 ;;
esac
demo_test_options=("-DBUILD_TESTING=$build_testing")
if [[ -n "${DEMO_GTEST_SOURCE_DIR:-}" ]]; then
  demo_test_options+=("-DDEMO_GTEST_SOURCE_DIR=$DEMO_GTEST_SOURCE_DIR")
fi

run_demo_tests() {
  if [[ "$build_testing" == ON ]]; then
    ctest --test-dir "$1" --output-on-failure --no-tests=error
  else
    echo 'BUILD_TESTING=OFF: tests were not built or run; this build is not test-validated.' >&2
  fi
}

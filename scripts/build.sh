#!/usr/bin/env bash
set -euo pipefail
demo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
folly_source=${FOLLY_SOURCE_DIR:-"$demo_root/../folly"}
folly_prefix=${FOLLY_PREFIX:-"$folly_source/_install"}
build_jobs=${BUILD_JOBS:-2}
source "$demo_root/scripts/build_common.sh"
skip_folly_build=${SKIP_FOLLY_BUILD:-0}
case "$skip_folly_build" in
  0|1) ;;
  *) echo 'SKIP_FOLLY_BUILD must be 0 or 1.' >&2; exit 2 ;;
esac
if [[ "$skip_folly_build" == 0 ]]; then
  if [[ ! -f "$folly_source/folly/executors/CPUThreadPoolExecutor.h" ]]; then
    echo 'Missing Folly source tree; stock pinned tag is sufficient for stages 2-4.' >&2
    exit 1
  fi
  cmake -S "$folly_source" -B "$folly_source/_build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF \
    -DCMAKE_INSTALL_PREFIX="$folly_prefix"
  cmake --build "$folly_source/_build" --parallel "$build_jobs"
  cmake --install "$folly_source/_build"
else
  echo "Reusing installed Folly from $folly_prefix"
fi
cmake -S "$demo_root" -B "$demo_root/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$folly_prefix" \
  -DFOLLY_SOURCE_DIR="$folly_source" -DDAG_BUILD_FOLLY_EXTENSION=OFF \
  "${demo_test_options[@]}"
cmake --build "$demo_root/build" --parallel "$build_jobs"
run_demo_tests "$demo_root/build"

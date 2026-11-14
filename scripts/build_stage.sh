#!/usr/bin/env bash
set -euo pipefail
demo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
stage=${1:-independent}
folly_prefix=${FOLLY_PREFIX:-"$demo_root/../folly/_install"}
build_jobs=${BUILD_JOBS:-1}
source "$demo_root/scripts/build_common.sh"
extra=()
case "$stage" in
  independent|metrics|dag) source_dir="$demo_root/experiments/$stage" ;;
  placement)
    source_dir="$demo_root/experiments/dag"
    extra=(-DDAG_BUILD_FOLLY_EXTENSION=ON "-DFOLLY_SOURCE_DIR=${FOLLY_SOURCE_DIR:-$demo_root/../folly}")
    ;;
  *) echo 'Usage: build_stage.sh independent|metrics|dag|placement' >&2; exit 2 ;;
esac
cmake -S "$source_dir" -B "$demo_root/build-$stage" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$folly_prefix" \
  "${extra[@]}" "${demo_test_options[@]}"
cmake --build "$demo_root/build-$stage" --parallel "$build_jobs"
run_demo_tests "$demo_root/build-$stage"

#!/usr/bin/env bash
# Test exact upstream scheduler code with POSIX-only platform shims and controlled pthread timing.
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_root=${1:?usage: validate_px4_lockstep_backport.sh PX4_SOURCE OUTPUT_DIR}
out_root=${2:?usage: validate_px4_lockstep_backport.sh PX4_SOURCE OUTPUT_DIR}
base_commit=83c4f4e580558816f0e398918f66358988004696
upstream_commit=27a21488b21f6334ec6eb9a89ddfe44016814500
scheduler=platforms/posix/src/px4/common/lockstep_scheduler
assets="$repo_root/simulation/px4/lockstep-validation"
mkdir -p "$out_root"
out_root=$(cd "$out_root" && pwd)
source_root=$(cd "$source_root" && pwd)
test "$(git -C "$source_root" rev-parse HEAD)" = "$base_commit"
# Both builds run precisely the same regression and the unmodified upstream test source.
for kind in baseline upstream fixed; do
    mkdir -p "$out_root/$kind/lockstep_scheduler"
    for spec in 'src/lockstep_scheduler.cpp lockstep_scheduler.cpp' 'include/lockstep_scheduler/lockstep_scheduler.h lockstep_scheduler/lockstep_scheduler.h'; do
        read -r from to <<< "$spec"
        if [[ $kind == baseline ]]; then
            git -C "$source_root" show "$base_commit:$scheduler/$from" > "$out_root/$kind/$to"
        elif [[ $kind == upstream ]]; then
            git -C "$source_root" show "$upstream_commit:$scheduler/$from" > "$out_root/$kind/$to"
        else
            cp "$source_root/$scheduler/$from" "$out_root/$kind/$to"
        fi
    done
    common=(-std=c++17 -O2 -Wall -Wextra -pthread -include "$assets/shims/px4_platform_common/log.h"
        -I"$out_root/$kind" -I"$assets/shims" -I"$source_root/$scheduler/include" -I"$source_root/$scheduler/include/lockstep_scheduler")
    "${CXX:-g++}" "${common[@]}" "$out_root/$kind/lockstep_scheduler.cpp" "$source_root/$scheduler/src/lockstep_components.cpp" \
        "$assets/lifecycle_regression.cpp" -Wl,--wrap=pthread_cond_wait -Wl,--wrap=pthread_cond_timedwait -Wl,--wrap=pthread_mutex_lock \
        -o "$out_root/$kind/lifecycle_regression"
    # Ubuntu libgtest-dev supplies pkg-config metadata and static libraries.
    read -r -a gtest_flags <<< "$(pkg-config --cflags --libs gtest_main)"
    "${CXX:-g++}" "${common[@]}" "$out_root/$kind/lockstep_scheduler.cpp" "$source_root/$scheduler/src/lockstep_components.cpp" \
        "$source_root/$scheduler/test/src/lockstep_scheduler_test.cpp" "${gtest_flags[@]}" -o "$out_root/$kind/upstream_test"
done
run_expected() {
    local expected=$1 label=$2
    shift 2
    local rc=0
    timeout 120 "$@" || rc=$?
    printf '%s exit=%s expected=%s\n' "$label" "$rc" "$expected"
    [[ $rc == "$expected" ]]
}
for kind in baseline upstream fixed; do
    expected=0
    [[ $kind == baseline ]] && expected=1
    run_expected "$expected" "$kind lifecycle" "$out_root/$kind/lifecycle_regression" lifecycle
    run_expected "$expected" "$kind fanout" "$out_root/$kind/lifecycle_regression" fanout
    external_expected=0
    [[ $kind == upstream ]] && external_expected=1
    run_expected "$external_expected" "$kind external notification" "$out_root/$kind/lifecycle_regression" external
    # Original upstream polling is intentionally rejected by the external notification regression.
    # Its random All test has also hung in this environment; it is not a candidate acceptance gate.
    if [[ $kind != upstream ]]; then
        repeats=1
        [[ $kind == fixed ]] && repeats=10
        run_expected 0 "$kind original upstream test" "$out_root/$kind/upstream_test" --gtest_color=no --gtest_repeat="$repeats"
    fi
    for repeat in {1..10}; do
        run_expected "$expected" "$kind lifecycle repeat=$repeat" "$out_root/$kind/lifecycle_regression" lifecycle
    done
done
printf 'PX4 lockstep three-way RED/GREEN validation PASS\n'

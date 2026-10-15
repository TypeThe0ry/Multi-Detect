#!/usr/bin/env bash
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base_temp="$repo_root/.pytest-basetemp/onboard"
mkdir -p "$base_temp"
# The gate is CPU-only so it never competes with a training or eval job for the GPU.
export CUDA_VISIBLE_DEVICES=-1
mapfile -t onboard_files < <(PYTHONPATH="$repo_root/tests:$repo_root" python -c 'from onboard_gate_files import validate; print("\n".join(map(str, validate())))' | tr -d '\r')
if [ "${PIPESTATUS[0]}" -ne 0 ] || [ "${#onboard_files[@]}" -eq 0 ]; then echo 'onboard gate: missing or empty file list' >&2; exit 2; fi

started="$(date +%s)"
set +e
(cd "$repo_root" && python -m pytest -m onboard --basetemp "$base_temp" "${onboard_files[@]}" "$@")
status=$?
set -e
elapsed=$(( $(date +%s) - started ))
if [ "$status" -eq 0 ]; then
    result=PASS
else
    result=FAIL
fi
printf 'onboard gate: %s (%ss)\n' "$result" "$elapsed"
exit "$status"

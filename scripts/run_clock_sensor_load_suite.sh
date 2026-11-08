#!/usr/bin/env bash
set -euo pipefail

# Bounded, serial SITL comparison for clock/frame robustness. All Python used
# by this lane is the VLM environment because the onboard teacher may import
# torch/transformers.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON_BIN=/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python
OUT_ROOT=${CLOCK_SENSOR_LOAD_OUT:-${ROOT}/.tmp/clock-sensor-load-20261108}
SCENARIO=${ROOT}/configs/scenarios/generated-count-site/gen_cnt2_w0000.scenario.json
ONBOARD_CONFIG=/data/projects/Multi-Detect-env/Multi-Detect/artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/count-cones/onboard.json
SLOT_POOL=66-95
SITL_CPUS=66-75
LOAD_CPUS=0-11
LOAD_SECONDS=${LOAD_SECONDS:-600}
DRY_RUN=0

if [[ ${1:-} == "--dry-run" ]]; then
    DRY_RUN=1
    shift
fi
if (($#)); then
    printf 'usage: %s [--dry-run]\n' "$0" >&2
    exit 2
fi
if [[ ! -x ${PYTHON_BIN} ]]; then
    printf 'missing VLM Python: %s\n' "${PYTHON_BIN}" >&2
    exit 2
fi
if [[ ! -f ${SCENARIO} || ! -f ${ONBOARD_CONFIG} ]]; then
    printf 'missing scenario or onboard config\n' >&2
    exit 2
fi
if [[ -e ${OUT_ROOT} && -n "$(find "${OUT_ROOT}" -mindepth 1 -print -quit)" ]]; then
    printf 'refusing non-empty output directory: %s\n' "${OUT_ROOT}" >&2
    exit 2
fi
mkdir -p "${OUT_ROOT}"

export PYTHONPATH=${ROOT}/src:${ROOT}
export PYTHONUTF8=1
export TMPDIR=${ROOT}/.tmp
export MULTIDETECT_SITL_SLOTS=3
export MULTIDETECT_SITL_SLOT=0
export MULTIDETECT_SIM_CPUSET=auto
export MULTIDETECT_SIM_CPU_POOL=${SLOT_POOL}
export MULTIDETECT_SIM_CPUS_PER_SLOT=10
export MULTIDETECT_SIM_GPU_DEVICES=0,1
export MULTIDETECT_SIM_GPU_AVOID=2,3
export MULTIDETECT_PX4_LOGGER_MODE=off

write_record() {
    local path=$1 condition=$2 status=$3 exit_code=$4 start_utc=$5 end_utc=$6 stop_reason=$7 collector_pid=$8 load_pid=$9 monitor_pid=${10}
    CONDITION=${condition} STATUS=${status} EXIT_CODE=${exit_code} START_UTC=${start_utc} END_UTC=${end_utc} \
    STOP_REASON=${stop_reason} COLLECTOR_PID=${collector_pid} LOAD_PID=${load_pid} MONITOR_PID=${monitor_pid} \
    SLOT=0 SITL_SLOTS=3 SITL_CPUSET=${SITL_CPUS} LOAD_CPUSET=${LOAD_CPUS} SCENARIO=${SCENARIO} ONBOARD_CONFIG=${ONBOARD_CONFIG} \
    "${PYTHON_BIN}" - "${path}" <<'PY'
import json, os, sys

def value(name):
    raw = os.environ.get(name)
    try:
        return int(raw) if name.endswith("_PID") or name == "EXIT_CODE" or name in {"SLOT", "SITL_SLOTS"} else raw
    except (TypeError, ValueError):
        return raw

path = sys.argv[1]
row = {
    "schema_version": 1,
    "condition": os.environ["CONDITION"],
    "status": os.environ["STATUS"],
    "collector_exit_code": value("EXIT_CODE"),
    "start_utc": os.environ["START_UTC"],
    "end_utc": os.environ["END_UTC"],
    "stop_reason": os.environ["STOP_REASON"],
    "collector_pid": value("COLLECTOR_PID"),
    "load_pid": value("LOAD_PID"),
    "monitor_pid": value("MONITOR_PID"),
    "slot": value("SLOT"),
    "sitl_slots": value("SITL_SLOTS"),
    "sitl_cpuset": os.environ["SITL_CPUSET"],
    "load_cpuset": os.environ["LOAD_CPUSET"],
    "scenario": os.environ["SCENARIO"],
    "onboard_config": os.environ["ONBOARD_CONFIG"],
}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(row, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
PY
}

write_input_manifest() {
    local path=$1 condition=$2
    CONDITION=${condition} SITL_CPUS=${SITL_CPUS} LOAD_CPUS=${LOAD_CPUS} \
    "${PYTHON_BIN}" - "${path}" "${SCENARIO}" "${ONBOARD_CONFIG}" <<'PY'
import hashlib, json, os, sys

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return {"path": path, "sha256": h.hexdigest(), "size": os.path.getsize(path)}

row = {
    "condition": os.environ["CONDITION"],
    "scenario": digest(sys.argv[2]),
    "onboard_config": digest(sys.argv[3]),
    "slot": 0,
    "sitl_slots": 3,
    "sitl_cpuset": os.environ["SITL_CPUS"],
    "load_cpuset": os.environ["LOAD_CPUS"],
    "same_semantic_inputs": True,
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(row, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
PY
}

run_condition() {
    local condition=$1 load=$2
    local condition_dir=${OUT_ROOT}/${condition}
    mkdir -p "${condition_dir}"
    local start_utc end_utc collector_pid=0 load_pid=0 monitor_pid=0 exit_code=0 status stop_reason
    start_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    write_input_manifest "${condition_dir}/input.json" "${condition}"

    MONITOR_CONDITION=${condition} MONITOR_DIR=${condition_dir} MONITOR_CPUSET=${SITL_CPUS} \
    "${PYTHON_BIN}" - <<'PY' >"${condition_dir}/host-pressure-monitor.log" 2>&1 &
import json, os, pathlib, time

root = pathlib.Path(os.environ["MONITOR_DIR"])
path = root / "host_pressure.jsonl"
while True:
    row = {
        "condition": os.environ["MONITOR_CONDITION"],
        "wall_time": time.time(),
        "wall_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "monotonic_ns": time.monotonic_ns(),
        "cpuset_cpus": os.environ["MONITOR_CPUSET"],
    }
    try:
        row["loadavg"] = pathlib.Path("/proc/loadavg").read_text(encoding="ascii").strip()
    except OSError:
        row["loadavg"] = None
    for name in ("cpu", "memory", "io"):
        try:
            row[f"{name}_pressure"] = pathlib.Path(f"/proc/pressure/{name}").read_text(encoding="ascii").strip()
        except OSError:
            row[f"{name}_pressure"] = None
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    time.sleep(5)
PY
    monitor_pid=$!

    if ((load)); then
        stress-ng --cpu 12 --taskset "${LOAD_CPUS}" --timeout "${LOAD_SECONDS}s" --metrics-brief >"${condition_dir}/load.log" 2>&1 &
        load_pid=$!
    fi

    set +e
    "${PYTHON_BIN}" "${ROOT}/scripts/collect_sim_teacher_episodes.py" \
        --scenario "${SCENARIO}" \
        --teacher onboard \
        --episodes 1 \
        --seed 0 \
        --steps-per-episode 6 \
        --out "${condition_dir}/episodes" \
        --rgb-size 1280x960 \
        --max-episode-s 300 \
        --timeout-s 300 \
        --heartbeat-timeout-s 240 \
        --control-port 14560 \
        --telemetry-port 14561 \
        --container-prefix "md-clock-sensor-load-${condition}" \
        --worker-index 0 \
        --operator-port 15480 \
        --segmentation \
        --onboard-config "${ONBOARD_CONFIG}" \
        >"${condition_dir}/collector.stdout.jsonl" \
        2>"${condition_dir}/collector.stderr.log" &
    collector_pid=$!
    wait "${collector_pid}"
    exit_code=$?
    set -e
    if ((exit_code == 0)); then status=complete; stop_reason=collector_exit_0; else status=failed; stop_reason=collector_exit_${exit_code}; fi
    end_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    kill "${monitor_pid}" 2>/dev/null || true
    if ((load_pid)); then kill "${load_pid}" 2>/dev/null || true; fi
    wait "${monitor_pid}" 2>/dev/null || true
    if ((load_pid)); then wait "${load_pid}" 2>/dev/null || true; fi
    write_record "${condition_dir}/condition.json" "${condition}" "${status}" "${exit_code}" "${start_utc}" "${end_utc}" "${stop_reason}" "${collector_pid}" "${load_pid}" "${monitor_pid}"
}

if ((DRY_RUN)); then
    for condition in baseline loaded; do
        condition_dir=${OUT_ROOT}/${condition}
        mkdir -p "${condition_dir}"
        write_input_manifest "${condition_dir}/input.json" "${condition}"
        "${PYTHON_BIN}" "${ROOT}/scripts/collect_sim_teacher_episodes.py" \
            --scenario "${SCENARIO}" --teacher onboard --episodes 1 --seed 0 \
            --steps-per-episode 6 --out "${condition_dir}/episodes" --rgb-size 1280x960 \
            --max-episode-s 300 --timeout-s 300 --heartbeat-timeout-s 240 \
            --control-port 14560 --telemetry-port 14561 \
            --container-prefix "md-clock-sensor-load-${condition}" --worker-index 0 \
            --operator-port 15480 --segmentation --onboard-config "${ONBOARD_CONFIG}" \
            --dry-run >"${condition_dir}/dry-run.json"
    done
else
    run_condition baseline 0
    run_condition loaded 1
fi

printf 'suite_output=%s\n' "${OUT_ROOT}"

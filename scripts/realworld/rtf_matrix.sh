#!/usr/bin/env bash
# 顺序测量宽区域 RTF；只负责编排，默认不改变既有世界或采集行为。
# manifest 为 JSON 数组，每项包含 name、scenario、resolved_root、extent_m；可选 airframe。
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
SITL_PYTHON=${SITL_PYTHON:-/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python}
TOOLS_PYTHON=${TOOLS_PYTHON:-/data/datasets/multidetect-sim-assets/tools/venv/bin/python}
SLOT=${RTF_MATRIX_SLOT:-7}
GPU=${RTF_MATRIX_GPU:-1}
GAP_S=${RTF_MATRIX_GAP_S:-30}
WAIT_S=${RTF_MATRIX_WAIT_S:-3600}
CPU_MAX=${RTF_MATRIX_CPU_MAX:-0.75}
GPU_MAX_MIB=${RTF_MATRIX_GPU_MAX_MIB:-2000}
WINDOW_S=${RTF_MATRIX_WINDOW_S:-120}
WARMUP_S=${RTF_MATRIX_WARMUP_S:-45}
INTERVAL_S=${RTF_MATRIX_INTERVAL_S:-2}
OUT=${RTF_MATRIX_OUT:-$ROOT/.tmp/rtf-matrix-$(date -u +%Y%m%dT%H%M%SZ)}
SINGLE_ROUND=${RTF_MATRIX_SINGLE_ROUND:-0}
RUN_TAG=${RTF_MATRIX_RUN_TAG:-}
GPU_UUID=
MANIFEST=

usage() {
  cat <<USAGE
用法：$0 --manifest FILE [--out DIR]

FILE 是 JSON 数组；每项至少包含 name、scenario、resolved_root、extent_m，
可选 airframe（rc_cessna_mono_cam 默认，或 delta_tailsitter_gimbal_cam）。每项会在 RTF_MATRIX_SLOT 指定的 slot 顺序执行，
仅允许 slot 5、6、7（默认 7），可使用 GPU 0–3（默认 1）；
长航程由 .venv-vlm 运行，stats 采样由同一项目环境运行。
RTF_MATRIX_SINGLE_ROUND=1 时要求恰好一项，直接写入 --out，不重试或清理 episode。
单轮可选 image（容器镜像引用）、gimbal_bridge_stabilize（JSON 布尔值，true 仅支持 Delta）。

可无人值守运行：
  systemd-run --user --unit=multidetect-rtf-matrix --collect \\
    --working-directory="$ROOT" $0 --manifest FILE --out DIR
USAGE
}

while (($#)); do
  case "$1" in
    --manifest) MANIFEST=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$MANIFEST" ]] || { usage >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "manifest 不存在：$MANIFEST" >&2; exit 2; }
[[ -x "$SITL_PYTHON" ]] || { echo "缺少 SITL Python：$SITL_PYTHON" >&2; exit 2; }
[[ "$SLOT" =~ ^[567]$ ]] || { echo "只允许 slot 5、6、7" >&2; exit 2; }
[[ "$GPU" =~ ^[0-3]$ ]] || { echo "矩阵只允许 GPU0–3（RTF_MATRIX_GPU）" >&2; exit 2; }
[[ -z "$RUN_TAG" || "$RUN_TAG" =~ ^[a-z0-9]{1,32}$ ]] || { echo "无效的 RTF_MATRIX_RUN_TAG" >&2; exit 2; }
mkdir -p "$OUT"

export PYTHONPATH="$ROOT/src:$ROOT"
export PYTHONUTF8=1
export TMPDIR=${TMPDIR:-$ROOT/.tmp}
export MULTIDETECT_SITL_SLOTS=${MULTIDETECT_SITL_SLOTS:-8}
export MULTIDETECT_SIM_GPU=1
export MULTIDETECT_SIM_GPU_DEVICES=$GPU
export MULTIDETECT_SIM_ALLOW_GPUS=$GPU
export MULTIDETECT_SIM_GPU_AVOID=
export MULTIDETECT_SIM_CGROUP_PARENT=${MULTIDETECT_SIM_CGROUP_PARENT:-}
export MULTIDETECT_SIM_NETWORK=${MULTIDETECT_SIM_NETWORK:-bridge}
export MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=${MULTIDETECT_SIM_FIRST_FRAME_WAIT_S:-240}
(( MULTIDETECT_SITL_SLOTS >= 8 )) || { echo "MULTIDETECT_SITL_SLOTS 必须至少为 8" >&2; exit 2; }

json_rows() {
  "$TOOLS_PYTHON" - "$MANIFEST" "$SINGLE_ROUND" <<'PY'
import json, math, sys
from pathlib import Path
from scripts.realworld.collection_runtime import runtime_options
from scripts.realworld import x1_scenario_class as policy
rows=json.load(open(sys.argv[1], encoding='utf-8'))
if not isinstance(rows, list) or not rows:
    raise SystemExit('manifest 必须是非空 JSON 数组')
for i,row in enumerate(rows):
    if not isinstance(row, dict): raise SystemExit(f'第 {i} 项不是对象')
    for key in ('name','scenario','resolved_root','extent_m'):
        if key not in row: raise SystemExit(f'第 {i} 项缺少 {key}')
    airframe=row.get('airframe', 'rc_cessna_mono_cam')
    if airframe not in ('rc_cessna_mono_cam', 'delta_tailsitter_gimbal_cam'):
        raise SystemExit(f'第 {i} 项 airframe 必须是 rc_cessna_mono_cam 或 delta_tailsitter_gimbal_cam')
    extent=float(row['extent_m'])
    scenario_class=row.get('scenario_class', policy.WIDE_AREA)
    world=Path(row['scenario']).name.removesuffix('.scenario.json')
    try:
        threshold=policy.distance_gate(world, scenario_class)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    short=scenario_class == policy.MOUNTAIN_SHORT
    if short and (extent != 6000 or airframe != 'delta_tailsitter_gimbal_cam'):
        raise SystemExit(f'第 {i} 项 mountain_short 只适用于 6 km X1 场景')
    minimum=float(row.get('minimum_max_distance_m', threshold if extent >= 6000 else 1000))
    if (not math.isfinite(minimum) or (short and minimum != threshold)
            or (not short and extent >= 6000 and minimum < threshold)):
        raise SystemExit(f'第 {i} 项 minimum_max_distance_m 不符合 {scenario_class} 门槛')
    row.update(scenario_class=scenario_class, minimum_max_distance_m=minimum)
    if sys.argv[2] == '1':
        try:
            runtime_options(row, airframe, f'manifest[{i}]')
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    minimum_rtf=float(row.get('minimum_rtf', 0.9))
    if not math.isfinite(minimum_rtf) or minimum_rtf < 0.9: raise SystemExit(f'第 {i} 项 minimum_rtf 必须是 >= 0.9 的有限数')
    print(json.dumps(row, ensure_ascii=False, separators=(',', ':')))
PY
}

training_or_busy_gpu() {
  local rows pid cmd lower
  rows=$(nvidia-smi -i "$GPU" --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null || true)
  while IFS=, read -r pid _ _; do
    pid=${pid//[[:space:]]/}
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$pid" == "$$" ]] && continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    lower=${cmd,,}
    # 对 GPU1 上任何非本脚本进程保守等待；训练关键词用于报告原因。
    if [[ "$lower" == *torchrun* || "$lower" == *deepspeed* || "$lower" == *accelerate* || "$lower" == *train* || "$lower" == *finetun* ]]; then
      echo "训练作业 pid=$pid cmd=${cmd:0:160}" >&2
    else
      echo "GPU$GPU 已被其他进程占用 pid=$pid cmd=${cmd:0:160}" >&2
    fi
    return 0
  done <<< "$rows"
  return 1
}

preflight() {
  local avail_g
  avail_g=$(df --output=avail -BG "$OUT" | tail -1 | tr -dc '0-9')
  (( avail_g >= ${RTF_MATRIX_MIN_FREE_GB:-40} )) || { echo "磁盘剩余 ${avail_g}G 低于 ${RTF_MATRIX_MIN_FREE_GB:-40}G，拒绝开跑" >&2; return 1; }
  local start=$SECONDS load used total
  while :; do
    load=$(awk '{print $1}' /proc/loadavg)
    used=$(nvidia-smi -i "$GPU" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d '[:space:]' || echo 999999)
    local gpu_ok=0
    if [[ "${RTF_MATRIX_SHARE_GPU:-0}" == 1 ]]; then
      # 与已确认的训练共享同一张卡：只要求剩余显存足够（总量 20 GiB）。
      total=$(nvidia-smi -i "$GPU" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | tr -d '[:space:]' || echo 0)
      awk -v u="$used" -v t="$total" -v need="${RTF_MATRIX_SHARE_MIN_FREE_MIB:-9000}" 'BEGIN{exit !(t-u >= need)}' && gpu_ok=1
    elif ! training_or_busy_gpu && awk -v u="$used" -v max="$GPU_MAX_MIB" 'BEGIN{exit !(u <= max)}'; then
      gpu_ok=1
    fi
    if (( gpu_ok )) && awk -v l="$load" -v max="$CPU_MAX" 'BEGIN{exit !(l <= max)}'; then
      echo "GPU$GPU/CPU 空闲：load=$load gpu_used_mib=$used" >&2
      return 0
    fi
    echo "开始前检查未通过：load=$load gpu_used_mib=$used；继续等待，不终止任何外部进程" >&2
    (( SECONDS - start < WAIT_S )) || { echo "等待 GPU$GPU/CPU 空闲超时" >&2; return 1; }
    sleep 30
  done
}

run_one() {
  local row=$1 attempt=$2 name scenario root extent airframe overlay model_root world_root map_root terrain_root server_config run_dir flight_pid stats_rc flight_rc prefix
  local runtime_argv
  local -a flight_args=() runtime_args=()
  name=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$row")
  scenario=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["scenario"])' "$row")
  root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["resolved_root"])' "$row")
  overlay=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("overlay_root", ""))' "$row")
  model_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_model_root", ""))' "$row")
  world_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_world_root", ""))' "$row")
  map_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_map_root", ""))' "$row")
  terrain_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_terrain_root", ""))' "$row")
  server_config=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("server_config", ""))' "$row")
  extent=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["extent_m"])' "$row")
  airframe=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("airframe","rc_cessna_mono_cam"))' "$row")
  if [[ "$airframe" != rc_cessna_mono_cam ]]; then flight_args=(--airframe "$airframe"); fi
  if [[ "$SINGLE_ROUND" == 1 ]]; then
    runtime_argv=$("$TOOLS_PYTHON" - "$row" <<'PY'
import json, sys
row = json.loads(sys.argv[1])
if 'image' in row:
    print('--image', row['image'], sep='\n')
if row.get('gimbal_bridge_stabilize') is True:
    print('--gimbal-bridge-stabilize')
PY
    ) || return 2
    if [[ -n "$runtime_argv" ]]; then
      mapfile -t runtime_args <<< "$runtime_argv"
      flight_args+=("${runtime_args[@]}")
    fi
  fi
  run_dir=${3:-"$OUT/$name/attempt-$attempt"}
  prefix="md-rw-long-slot-$SLOT${RUN_TAG:+-$RUN_TAG}"
  mkdir -p "$run_dir"
  echo "[$name] extent=${extent}m scenario=$scenario" | tee "$run_dir/status.log"
  export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$(realpath "$root")
  if [[ -n "$overlay" ]]; then export MULTIDETECT_RTF_OVERLAY_ROOT=$(realpath "$overlay"); else unset MULTIDETECT_RTF_OVERLAY_ROOT; fi
  if [[ -n "$model_root" ]]; then export MULTIDETECT_RTF_MODEL_ROOT=$(realpath "$model_root"); else unset MULTIDETECT_RTF_MODEL_ROOT; fi
  if [[ -n "$world_root" ]]; then export MULTIDETECT_RTF_WORLD_ROOT=$(realpath "$world_root"); else unset MULTIDETECT_RTF_WORLD_ROOT; fi
  if [[ -n "$map_root" ]]; then export MULTIDETECT_RTF_MAP_ROOT=$(realpath "$map_root"); else unset MULTIDETECT_RTF_MAP_ROOT; fi
  if [[ -n "$terrain_root" ]]; then export MULTIDETECT_RTF_TERRAIN_ROOT=$(realpath "$terrain_root"); else unset MULTIDETECT_RTF_TERRAIN_ROOT; fi
  if [[ -n "$server_config" ]]; then export MULTIDETECT_RTF_SERVER_CONFIG=$(realpath "$server_config"); else unset MULTIDETECT_RTF_SERVER_CONFIG; fi
  export MULTIDETECT_LONG_RANGE_CONTAINER_PREFIX="$prefix"
  local guard_rc=0 result_rc=0
  if [[ "$SINGLE_ROUND" == 1 ]]; then
    local flight_json stats_json
    flight_json=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
      "$SITL_PYTHON" scripts/realworld/long_range_flight.py "$SLOT" "$scenario" "$run_dir/flight" "${flight_args[@]}")
    stats_json=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
      "$TOOLS_PYTHON" scripts/realworld/sample_stats.py --container-prefix "$prefix-" --gpu "$GPU" \
      --warmup-s "$WARMUP_S" --window-s "$WINDOW_S" --interval-s "$INTERVAL_S" --out "$run_dir/stats.json")
    set +e
    "$TOOLS_PYTHON" scripts/realworld/collection_guard.py run --out "$run_dir" --gpu "$GPU" --prefix "$prefix-" \
      --flight-command "$flight_json" --stats-command "$stats_json"
    guard_rc=$?
    set -e
    stats_rc=$("$TOOLS_PYTHON" -c 'import json,sys; x=json.load(open(sys.argv[1])); print(x.get("stats_returncode") if x.get("stats_returncode") is not None else 1)' "$run_dir/guard.json")
    flight_rc=$("$TOOLS_PYTHON" -c 'import json,sys; x=json.load(open(sys.argv[1])); print(x.get("flight_returncode") if x.get("flight_returncode") is not None else 1)' "$run_dir/guard.json")
  else
    "$SITL_PYTHON" scripts/realworld/long_range_flight.py \
      "$SLOT" "$scenario" "$run_dir/flight" "${flight_args[@]}" >"$run_dir/flight.stdout.log" 2>&1 &
    flight_pid=$!
    trap 'if kill -0 "$flight_pid" 2>/dev/null; then kill "$flight_pid" 2>/dev/null || true; fi' RETURN
    set +e
    "$TOOLS_PYTHON" scripts/realworld/sample_stats.py --container-prefix "$prefix-" --gpu "$GPU" \
      --warmup-s "$WARMUP_S" --window-s "$WINDOW_S" --interval-s "$INTERVAL_S" --out "$run_dir/stats.json"
    stats_rc=$?
    wait "$flight_pid"
    flight_rc=$?
    set -e
    trap - RETURN
  fi
  "$TOOLS_PYTHON" - "$run_dir" "$name" "$extent" "$airframe" "$stats_rc" "$flight_rc" "$attempt" "$SLOT" "$row" "$GPU_UUID" "$SINGLE_ROUND" "$GPU" <<'PY' || result_rc=$?
import json,math,sys
from pathlib import Path
from scripts.realworld.collection_runtime import read_episode_metadata, runtime_evidence
out=Path(sys.argv[1]); stats_path=out/'stats.json'; flight_path=out/'flight/long-range-result.json'
flight_result_present = flight_path.is_file()
flight = json.loads(flight_path.read_text(encoding='utf-8')) if flight_result_present else {}
flight_rc = int(sys.argv[6])
stats=json.loads(stats_path.read_text(encoding='utf-8')) if stats_path.exists() else {}
manifest=json.loads(sys.argv[9]); slot=int(sys.argv[8]); minimum=manifest['minimum_max_distance_m']; minimum_rtf=float(manifest.get("minimum_rtf", 0.9))
scenario_class=manifest['scenario_class']
container_name=stats.get("container_name")
route=flight.get("route_completed") is True
distance=flight.get("max_distance_from_spawn_m")
slot_name_ok=isinstance(container_name,str) and f"-slot-{slot}-" in container_name
stats_ok=stats.get("measurement_complete") is True and stats.get("errors")==[]
gpu=sys.argv[12]
stats_gpu_ok=str(stats.get("gpu"))==gpu
rtf=stats.get("rtf_window_median")
rtf_ok=type(rtf) in (int,float) and math.isfinite(rtf) and rtf >= minimum_rtf
distance_ok=type(distance) in (int,float) and math.isfinite(distance) and distance >= minimum
policy_ok=(flight_result_present and flight.get('scenario_class','wide_area')==scenario_class
           and (scenario_class!='mountain_short' or flight.get('minimum_max_distance_m')==minimum))
valid=(int(sys.argv[5])==0 and flight_rc==0 and route and distance_ok and policy_ok and stats_ok and stats_gpu_ok and rtf_ok and slot_name_ok)
issues=[]
if not flight_result_present:
    issues.append(f"missing flight/long-range-result.json (flight return code {flight_rc})")
else:
    if not route: issues.append("route_completed is not true")
    if not distance_ok: issues.append(f"max_distance_from_spawn_m is below {minimum:g} m or missing")
    if not policy_ok: issues.append('flight distance policy does not match matrix entry')
if not stats_ok: issues.append("stats.json is incomplete or has errors")
if stats and not stats_gpu_ok: issues.append("stats GPU does not match matrix GPU")
if not rtf_ok: issues.append(f"rtf_window_median is below {minimum_rtf:g} or missing")
if not slot_name_ok: issues.append(f"container name does not contain -slot-{slot}-")
# Matrix identity must not disappear with missing stats or be overwritten by them.
row={**stats, 'name':sys.argv[2], 'variant':manifest.get("variant",sys.argv[2].split("_")[0]),
     'attempt':int(sys.argv[7]), 'extent_m':float(sys.argv[3]), 'scenario_class':scenario_class,
     'minimum_max_distance_m':minimum, 'minimum_rtf':minimum_rtf,
     'airframe':sys.argv[4], 'stats_returncode':int(sys.argv[5]), 'flight_returncode':flight_rc,
     'route_completed':route, 'max_distance_from_spawn_m':distance, 'flight_status':flight.get("status"),
     'container_name_slot_verified':slot_name_ok, 'valid_round':valid, 'round_errors':issues,
     'measurement_complete':stats_ok and stats_gpu_ok, 'gpu':gpu, 'gpu_uuid':sys.argv[10] or None}
if sys.argv[11] == '1':
    episode = read_episode_metadata(list((out / 'flight/episodes').glob('*/episode.json')))
    row.update(runtime_evidence(manifest, episode))
(out/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(row,ensure_ascii=False))
raise SystemExit(0 if valid else 1)
PY
  (( guard_rc == 76 )) && return 76
  return "$result_rc"
}

rows=$(json_rows) || exit 2
if [[ "$SINGLE_ROUND" == 1 ]]; then
  [[ -n "$rows" && "$rows" != *$'\n'* ]] || { echo "单轮模式要求一项 manifest" >&2; exit 2; }
  attempt=${RTF_MATRIX_ATTEMPT:-1}
  [[ "$attempt" =~ ^[1-9][0-9]*$ ]] || { echo "无效的 RTF_MATRIX_ATTEMPT" >&2; exit 2; }
  GPU_UUID=${RTF_MATRIX_X1_GPU_UUID:-$(nvidia-smi -i "$GPU" --query-gpu=uuid --format=csv,noheader)}
  [[ "$GPU_UUID" =~ ^GPU-[a-fA-F0-9-]{36}$ ]] || { echo "无法确认 GPU$GPU UUID" >&2; exit 1; }
  lock_root=${RTF_MATRIX_GPU_LOCK_ROOT:-/data/scratch/multidetect-rtf-gpu-locks}
  mkdir -p "$lock_root"
  exec {gpu_lock}>"$lock_root/$GPU_UUID.lock"
  if [[ -n ${RTF_MATRIX_X1_GPU_UUID:-} ]]; then
    if ! flock -n "$gpu_lock"; then
      echo "GPU$GPU 已有租约，下轮重新选卡" >&2
      printf '{"voided_reason":"x1_gpu_unavailable","detail":"gpu_lease_busy"}\n' > "$OUT/guard.json"
      exit 75
    fi
  else
    flock -w "$WAIT_S" "$gpu_lock" || { echo "等待 GPU$GPU 租约超时" >&2; exit 1; }
  fi
  export RTF_MATRIX_START_LOAD_MAX=${RTF_MATRIX_START_LOAD_MAX:-60}
  export RTF_MATRIX_START_WAIT_S=${RTF_MATRIX_START_WAIT_S:-$WAIT_S}
  "$TOOLS_PYTHON" scripts/realworld/collection_guard.py gate --out "$OUT" --gpu "$GPU" || exit $?
  run_one "$rows" "$attempt" "$OUT"
  exit 0
fi

: > "$OUT/matrix.jsonl"
MAX_ATTEMPTS=${RTF_MATRIX_MAX_ATTEMPTS:-3}
[[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || { echo "RTF_MATRIX_MAX_ATTEMPTS 必须为正整数" >&2; exit 2; }
while IFS= read -r row; do
  name=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$row")
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    preflight
    set +e
    run_one "$row" "$attempt" | tee -a "$OUT/matrix.log"
    rc=${PIPESTATUS[0]}
    set -e
    result_path="$OUT/$name/attempt-$attempt/result.json"
    [[ -f "$result_path" ]] && "$TOOLS_PYTHON" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1],encoding="utf-8")),ensure_ascii=False))' "$result_path" >> "$OUT/matrix.jsonl"
    if (( rc != 0 )) && [[ "${RTF_MATRIX_KEEP_FAILED_EPISODES:-0}" != 1 ]]; then
      rm -rf "$OUT/$name/attempt-$attempt/flight/episodes"
    fi
    if (( rc == 0 )); then break; fi
    echo "[$name] 第 $attempt/$MAX_ATTEMPTS 轮不完整；保留记录并重跑" >&2
    (( attempt < MAX_ATTEMPTS )) && sleep "$GAP_S"
  done
  sleep "$GAP_S"
done <<< "$rows"

"$TOOLS_PYTHON" - "$OUT/matrix.jsonl" "$OUT/summary.json" "$MANIFEST" <<'PY'
import json,sys
from statistics import median
from pathlib import Path
rows=[json.loads(x) for x in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines() if x.strip()]
manifest=json.load(open(sys.argv[3],encoding='utf-8')) if len(sys.argv)>3 else []
requested={row["name"] for row in manifest}
valid_by_name={}
for row in rows:
    if row.get("valid_round") is True:
        valid_by_name.setdefault(row["name"],row)
valid=set(valid_by_name)
selected=[valid_by_name[row["name"]] for row in manifest if row["name"] in valid_by_name]
def classify(row):
    return row.get('scenario_class','wide_area')
variants=list(dict.fromkeys((classify(row), row.get("variant","unknown")) for row in manifest))
variant_summary=[]
for scenario_class, variant in variants:
    class_rows=[row for row in manifest if classify(row)==scenario_class]
    items=[row for row in class_rows if row.get("variant","unknown")==variant]
    names={row["name"] for row in items}
    measured=[valid_by_name[name] for name in (row["name"] for row in items) if name in valid_by_name]
    rtfs=[float(row["rtf_window_median"]) for row in measured if row.get("rtf_window_median") is not None]
    loads=[float(row["host_load1_median"]) for row in measured if row.get("host_load1_median") is not None]
    distances=[float(row["max_distance_from_spawn_m"]) for row in measured
               if row.get("max_distance_from_spawn_m") is not None]
    ratios=[]
    if variant == "V0":
        relative=1.0 if rtfs else None
    else:
        for index,item in enumerate(class_rows):
            if item.get("variant") != variant or item["name"] not in valid_by_name:
                continue
            before=next((row for row in reversed(class_rows[:index]) if row.get("variant")=="V0"),None)
            after=next((row for row in class_rows[index+1:] if row.get("variant")=="V0"),None)
            adjacent=[valid_by_name[x["name"]].get("rtf_window_median") for x in (before,after)
                      if x is not None and x["name"] in valid_by_name]
            adjacent=[float(value) for value in adjacent if value is not None]
            candidate=valid_by_name[item["name"]].get("rtf_window_median")
            if adjacent and candidate is not None:
                ratios.append(float(candidate)/median(adjacent))
        relative=median(ratios) if ratios else None
    variant_summary.append({"variant":variant,"scenario_class":scenario_class,
                            "expected_rounds":len(items),"valid_rounds":len(measured),
                            "complete":names <= valid,"rtf_median":median(rtfs) if rtfs else None,
                            "relative_v0_median":relative,"relative_v0_round_count":len(ratios),
                            "host_load1_median":median(loads) if loads else None,
                            "max_distance_from_spawn_m_median":median(distances) if distances else None})
summary={'schema_version':1,'rows':rows,'valid_rows':selected,
         'variant_summary':variant_summary,
         'scenario_class_counts':{
             scenario_class:{'expected_rounds':sum(classify(r)==scenario_class for r in manifest),
                             'valid_rounds':sum(classify(r)==scenario_class for r in selected)}
             for scenario_class in ('wide_area','mountain_short')},
         'stable_window_rtf_median_min':min((r.get('rtf_window_median') for r in selected if r.get('rtf_window_median') is not None),default=None),
         'all_routes_completed':requested <= {r["name"] for r in selected if r.get("route_completed") is True},
         'all_measurements_complete':requested <= {r["name"] for r in selected if r.get("measurement_complete") is True},
         'all_requested_rounds_valid':requested <= valid}
Path(sys.argv[2]).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False))
PY

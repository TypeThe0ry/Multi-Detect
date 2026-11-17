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
MANIFEST=

usage() {
  cat <<USAGE
用法：$0 --manifest FILE [--out DIR]

FILE 是 JSON 数组；每项至少包含 name、scenario、resolved_root、extent_m，
可选 airframe（默认 rc_cessna_mono_cam）。每项会在 RTF_MATRIX_SLOT 指定的 slot 顺序执行，
仅允许 slot 5、6、7（默认 7），固定使用 GPU 1；
固定翼长航程由 .venv-vlm 运行，stats 采样由同一项目环境运行。

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
[[ "$GPU" =~ ^[13]$ ]] || { echo "矩阵只允许 GPU1 或 GPU3（RTF_MATRIX_GPU）" >&2; exit 2; }
mkdir -p "$OUT"

export PYTHONPATH="$ROOT/src:$ROOT"
export PYTHONUTF8=1
export TMPDIR=${TMPDIR:-$ROOT/.tmp}
export MULTIDETECT_SITL_SLOTS=${MULTIDETECT_SITL_SLOTS:-8}
export MULTIDETECT_SIM_GPU=1
export MULTIDETECT_SIM_GPU_DEVICES=$GPU
export MULTIDETECT_SIM_ALLOW_GPUS=1,3
export MULTIDETECT_SIM_GPU_AVOID=0
export MULTIDETECT_SIM_CGROUP_PARENT=${MULTIDETECT_SIM_CGROUP_PARENT:-}
export MULTIDETECT_SIM_NETWORK=${MULTIDETECT_SIM_NETWORK:-bridge}
export MULTIDETECT_SIM_FIRST_FRAME_WAIT_S=${MULTIDETECT_SIM_FIRST_FRAME_WAIT_S:-240}
(( MULTIDETECT_SITL_SLOTS >= 8 )) || { echo "MULTIDETECT_SITL_SLOTS 必须至少为 8" >&2; exit 2; }

json_rows() {
  "$TOOLS_PYTHON" - "$MANIFEST" <<'PY'
import json, math, sys
rows=json.load(open(sys.argv[1], encoding='utf-8'))
if not isinstance(rows, list) or not rows:
    raise SystemExit('manifest 必须是非空 JSON 数组')
for i,row in enumerate(rows):
    if not isinstance(row, dict): raise SystemExit(f'第 {i} 项不是对象')
    for key in ('name','scenario','resolved_root','extent_m'):
        if key not in row: raise SystemExit(f'第 {i} 项缺少 {key}')
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
  local start=$SECONDS load used
  while :; do
    load=$(awk '{print $1}' /proc/loadavg)
    used=$(nvidia-smi -i "$GPU" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | tr -d '[:space:]' || echo 999999)
    if ! training_or_busy_gpu && awk -v l="$load" -v max="$CPU_MAX" 'BEGIN{exit !(l <= max)}' \
       && awk -v u="$used" -v max="$GPU_MAX_MIB" 'BEGIN{exit !(u <= max)}'; then
      echo "GPU$GPU/CPU 空闲：load=$load gpu_used_mib=$used" >&2
      return 0
    fi
    echo "开始前检查未通过：load=$load gpu_used_mib=$used；继续等待，不终止任何外部进程" >&2
    (( SECONDS - start < WAIT_S )) || { echo "等待 GPU$GPU/CPU 空闲超时" >&2; return 1; }
    sleep 30
  done
}

run_one() {
  local row=$1 attempt=$2 name scenario root extent airframe overlay model_root world_root map_root terrain_root run_dir flight_pid stats_rc flight_rc
  name=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$row")
  scenario=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["scenario"])' "$row")
  root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["resolved_root"])' "$row")
  overlay=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("overlay_root", ""))' "$row")
  model_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_model_root", ""))' "$row")
  world_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_world_root", ""))' "$row")
  map_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_map_root", ""))' "$row")
  terrain_root=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("variant_terrain_root", ""))' "$row")
  extent=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1])["extent_m"])' "$row")
  airframe=$("$TOOLS_PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get("airframe","rc_cessna_mono_cam"))' "$row")
  run_dir="$OUT/$name/attempt-$attempt"
  mkdir -p "$run_dir"
  echo "[$name] extent=${extent}m scenario=$scenario" | tee "$run_dir/status.log"
  export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=$(realpath "$root")
  if [[ -n "$overlay" ]]; then export MULTIDETECT_RTF_OVERLAY_ROOT=$(realpath "$overlay"); else unset MULTIDETECT_RTF_OVERLAY_ROOT; fi
  if [[ -n "$model_root" ]]; then export MULTIDETECT_RTF_MODEL_ROOT=$(realpath "$model_root"); else unset MULTIDETECT_RTF_MODEL_ROOT; fi
  if [[ -n "$world_root" ]]; then export MULTIDETECT_RTF_WORLD_ROOT=$(realpath "$world_root"); else unset MULTIDETECT_RTF_WORLD_ROOT; fi
  if [[ -n "$map_root" ]]; then export MULTIDETECT_RTF_MAP_ROOT=$(realpath "$map_root"); else unset MULTIDETECT_RTF_MAP_ROOT; fi
  if [[ -n "$terrain_root" ]]; then export MULTIDETECT_RTF_TERRAIN_ROOT=$(realpath "$terrain_root"); else unset MULTIDETECT_RTF_TERRAIN_ROOT; fi
  "$SITL_PYTHON" scripts/realworld/long_range_flight.py "$SLOT" "$scenario" "$run_dir/flight" >"$run_dir/flight.stdout.log" 2>&1 &
  flight_pid=$!
  trap 'if kill -0 "$flight_pid" 2>/dev/null; then kill "$flight_pid" 2>/dev/null || true; fi' RETURN
  set +e
  "$TOOLS_PYTHON" scripts/realworld/sample_stats.py --container-prefix "md-rw-long-slot-$SLOT-" --gpu "$GPU" \
    --warmup-s "$WARMUP_S" --window-s "$WINDOW_S" --interval-s "$INTERVAL_S" --out "$run_dir/stats.json"
  stats_rc=$?
  wait "$flight_pid"
  flight_rc=$?
  set -e
  trap - RETURN
  "$TOOLS_PYTHON" - "$run_dir" "$name" "$extent" "$airframe" "$stats_rc" "$flight_rc" "$attempt" "$SLOT" "$row" <<'PY'
import json,sys
from pathlib import Path
out=Path(sys.argv[1]); stats_path=out/'stats.json'; flight_path=out/'flight/long-range-result.json'
flight=json.loads(flight_path.read_text(encoding='utf-8')) if flight_path.exists() else {}
stats=json.loads(stats_path.read_text(encoding='utf-8')) if stats_path.exists() else {}
manifest=json.loads(sys.argv[9]); slot=int(sys.argv[8]); minimum=float(manifest.get("minimum_max_distance_m", 4000 if float(sys.argv[3]) >= 6000 else 1000)); minimum_rtf=float(manifest.get("minimum_rtf", 0.9))
container_name=stats.get("container_name")
route=flight.get("route_completed") is True
distance=flight.get("max_distance_from_spawn_m")
slot_name_ok=isinstance(container_name,str) and f"-slot-{slot}-" in container_name
stats_ok=stats.get("measurement_complete") is True and stats.get("errors")==[]
rtf=stats.get("rtf_window_median")
rtf_ok=isinstance(rtf,(int,float)) and rtf >= minimum_rtf
distance_ok=isinstance(distance,(int,float)) and distance >= minimum
valid=(int(sys.argv[5])==0 and int(sys.argv[6])==0 and route and distance_ok and stats_ok and rtf_ok and slot_name_ok)
issues=[]
if not route: issues.append("route_completed is not true")
if not distance_ok: issues.append(f"max_distance_from_spawn_m is below {minimum:g} m or missing")
if not stats_ok: issues.append("stats.json is incomplete or has errors")
if not rtf_ok: issues.append(f"rtf_window_median is below {minimum_rtf:g} or missing")
if not slot_name_ok: issues.append(f"container name does not contain -slot-{slot}-")
row={'name':sys.argv[2], 'variant':manifest.get("variant",sys.argv[2].split("_")[0]),
     'attempt':int(sys.argv[7]), 'extent_m':float(sys.argv[3]), 'minimum_max_distance_m':minimum, 'minimum_rtf':minimum_rtf,
     'airframe':sys.argv[4], 'stats_returncode':int(sys.argv[5]), 'flight_returncode':int(sys.argv[6]),
     'route_completed':route, 'max_distance_from_spawn_m':distance, 'flight_status':flight.get("status"),
     'container_name_slot_verified':slot_name_ok, 'valid_round':valid, 'round_errors':issues,
     'measurement_complete':False}
row.update(stats)
(out/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(row,ensure_ascii=False))
raise SystemExit(0 if valid else 1)
PY
  local result_rc=$?
  return "$result_rc"
}

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
    if (( rc == 0 )); then break; fi
    echo "[$name] 第 $attempt/$MAX_ATTEMPTS 轮不完整；保留记录并重跑" >&2
    (( attempt < MAX_ATTEMPTS )) && sleep "$GAP_S"
  done
  sleep "$GAP_S"
done < <(json_rows)

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
variants=list(dict.fromkeys(row.get("variant","unknown") for row in manifest))
variant_summary=[]
for variant in variants:
    items=[row for row in manifest if row.get("variant","unknown")==variant]
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
        for index,item in enumerate(manifest):
            if item.get("variant") != variant or item["name"] not in valid_by_name:
                continue
            before=next((manifest[j] for j in range(index-1,-1,-1) if manifest[j].get("variant")=="V0"),None)
            after=next((manifest[j] for j in range(index+1,len(manifest)) if manifest[j].get("variant")=="V0"),None)
            adjacent=[valid_by_name[x["name"]].get("rtf_window_median") for x in (before,after)
                      if x is not None and x["name"] in valid_by_name]
            adjacent=[float(value) for value in adjacent if value is not None]
            candidate=valid_by_name[item["name"]].get("rtf_window_median")
            if adjacent and candidate is not None:
                ratios.append(float(candidate)/median(adjacent))
        relative=median(ratios) if ratios else None
    variant_summary.append({"variant":variant,"expected_rounds":len(items),"valid_rounds":len(measured),
                            "complete":names <= valid,"rtf_median":median(rtfs) if rtfs else None,
                            "relative_v0_median":relative,"relative_v0_round_count":len(ratios),
                            "host_load1_median":median(loads) if loads else None,
                            "max_distance_from_spawn_m_median":median(distances) if distances else None})
summary={'schema_version':1,'rows':rows,'valid_rows':selected,
         'variant_summary':variant_summary,
         'stable_window_rtf_median_min':min((r.get('rtf_window_median') for r in selected if r.get('rtf_window_median') is not None),default=None),
         'all_routes_completed':requested <= {r["name"] for r in selected if r.get("route_completed") is True},
         'all_measurements_complete':requested <= {r["name"] for r in selected if r.get("measurement_complete") is True},
         'all_requested_rounds_valid':requested <= valid}
Path(sys.argv[2]).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False))
PY

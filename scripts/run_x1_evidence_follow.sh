#!/usr/bin/env bash
# Run from the patched repository root. Only --plan-only is CPU-only.
set -euo pipefail
mode=${1:-}
variant=${2:---event}
case "$mode:$variant" in
    --plan-only:--event|--plan-only:--cadence|--sitl:--event|--sitl:--cadence) ;;
    *) printf 'Usage: %s --plan-only|--sitl [--event|--cadence]\n' "$0" >&2; exit 2 ;;
esac
MD=/data/projects/Multi-Detect-env/Multi-Detect
CPU="$MD/.venv/bin/python"
VLM="$MD/.venv-vlm/bin/python"
REF=/data/scratch/cx-wc-sampling
ARCHIVE=/data/scratch/x1-scn-run
ASSET=/data/datasets/multidetect-sim-assets/wide-collect/assets/rw_farm_iowa_w6km
WORLD=rw_farm_iowa_w6km__clear_noon
mkdir -p "$PWD/.tmp"
RUN=$(mktemp -d "$PWD/.tmp/evidence-follow.XXXXXXXX")
mkdir "$RUN/world" "$RUN/tmp"
cp "$REF/.tmp/x1-iowa-world/$WORLD.sdf" "$RUN/world/"
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR="$RUN/tmp"
export MULTIDETECT_SITL_SLOTS=8 MULTIDETECT_RTF_WORLD_ROOT="$RUN/world"
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT="$ASSET/restored/models"
unset MULTIDETECT_RTF_MODEL_ROOT
common=(--world rw_farm_iowa_w6km --scenario "configs/scenarios/realworld/delta_tailsitter_gimbal_cam/$WORLD.scenario.json"
        --sentence 'Fly over the yard at 85 m AGL, then return home and land.'
        --mission-package "$ARCHIVE/iowa-context.json" --recorded-output "$ARCHIVE/recorded-plan.json"
        --contingency-scenario configs/simulation/x1-evidence-follow-faults-v1.json --inject-fault target_found
        --generate-live-evidence --slot 7)
"$CPU" scripts/run_x1_autonomy_demo.py "${common[@]}" --plan-only --output "$RUN/plain"
"$CPU" scripts/realworld/x1_site_evidence.py --world-sdf "$RUN/world/$WORLD.sdf" \
    --dem "simulation/terrain/$WORLD.dem.json" --variant-source "$ASSET/x1-variant-source" \
    --models-root "$ASSET/restored/models" --inventory "$RUN/iowa-obstacle-inventory.json" \
    --bind "$RUN/plain/result.json" "$RUN/evidence-follow.site-bounds.json"
bound=(--live-evidence-bounds "$RUN/evidence-follow.site-bounds.json"
       --assumed-sim-bounds configs/simulation/x1-assumed-sim-bounds-iowa.json
       --recorded-retask configs/simulation/x1-evidence-follow-decision-v1.json --perception-tools)
if [[ $variant == --cadence ]]; then
    bound+=(--decision-cadence-s 1)
fi
"$CPU" scripts/run_x1_autonomy_demo.py "${common[@]}" "${bound[@]}" --plan-only --output "$RUN/preflight"
if [[ $mode == --sitl ]]; then
    "$VLM" scripts/run_x1_autonomy_demo.py "${common[@]}" "${bound[@]}" --output "$RUN/live"
fi
printf '%s\n' "$RUN"

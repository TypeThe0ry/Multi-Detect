#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONPATH=src:. PYTHONUTF8=1
export MULTIDETECT_PX4_PROFILE=main MULTIDETECT_PX4_LOGGER_MODE=default
export MULTIDETECT_SITL_SLOTS=${MULTIDETECT_SITL_SLOTS:-8}
exec /data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python -m scripts.measure_x1_fw_rtl "$@"

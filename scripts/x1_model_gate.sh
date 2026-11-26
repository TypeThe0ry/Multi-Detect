#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONUTF8=1
exec /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m scripts.x1_model_gate "$@"

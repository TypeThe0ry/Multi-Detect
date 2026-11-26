#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec /data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python \
    scripts/run_x1_closed_loop_suite.py "$@"

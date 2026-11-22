#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/python3 -B "$(dirname -- "${BASH_SOURCE[0]}")/x1_world_pipeline.py" "$@"

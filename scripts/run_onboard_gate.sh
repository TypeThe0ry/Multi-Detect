#!/usr/bin/env bash
# Thin wrapper; the gate itself is scripts/run_onboard_gate.py.
exec python "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_onboard_gate.py" "$@"

#!/usr/bin/env pwsh
# Thin wrapper; the gate itself is scripts/run_onboard_gate.py.
& python (Join-Path $PSScriptRoot 'run_onboard_gate.py') @args
exit $LASTEXITCODE

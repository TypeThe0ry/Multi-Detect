#!/usr/bin/env pwsh
# Every argument is passed through to pytest (e.g. -q -x -k name).
$PytestArgs = @($args)

$ErrorActionPreference = 'Continue'
$exitCode = 1
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$baseTemp = Join-Path $repoRoot '.pytest-basetemp\onboard'
New-Item -ItemType Directory -Force -Path $baseTemp | Out-Null
# The gate is CPU-only so it never competes with a training or eval job for the GPU.
$env:CUDA_VISIBLE_DEVICES = '-1'
$testsDir = Join-Path $repoRoot 'tests'
$onboardFiles = @(python -c "import sys; sys.path.insert(0, sys.argv[1]); from onboard_gate_files import validate; print(chr(10).join(map(str, validate())))" $testsDir)
if ($LASTEXITCODE -ne 0 -or $onboardFiles.Count -eq 0) { Write-Error 'onboard gate: missing or empty file list'; exit 2 }

$started = Get-Date
Push-Location $repoRoot
try {
    & python -m pytest -m onboard --basetemp $baseTemp @onboardFiles @PytestArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
$elapsed = ((Get-Date) - $started).TotalSeconds
$status = if ($exitCode -eq 0) { 'PASS' } else { 'FAIL' }
Write-Host ("onboard gate: {0} ({1:N1}s)" -f $status, $elapsed)
exit $exitCode

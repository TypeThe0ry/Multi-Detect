#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = 'Continue'
$exitCode = 1
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$baseTemp = Join-Path $repoRoot '.pytest-basetemp\onboard'
New-Item -ItemType Directory -Force -Path $baseTemp | Out-Null
$onboardFiles = @(python -c "import sys; sys.path.insert(0, 'tests'); from onboard_gate_files import validate; print('`n'.join(map(str, validate())))")
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

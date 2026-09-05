param(
    [string]$ScenarioManifest = "configs\scenarios\river_rescue_vtol_001.scenario.json",
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PythonCandidates = @(
    (Join-Path $Root "ground-control\MultiDetectGCS\.venv\Scripts\python.exe"),
    ((Get-Command python -ErrorAction SilentlyContinue).Source)
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
if (@($PythonCandidates).Count -eq 0) { throw "No Python interpreter found" }
$Python = @($PythonCandidates)[0]
$Manifest = if ([IO.Path]::IsPathRooted($ScenarioManifest)) { $ScenarioManifest } else { Join-Path $Root $ScenarioManifest }
if (-not (Test-Path -LiteralPath $Manifest)) { throw "Scenario manifest does not exist: $Manifest" }
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $id = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $EvidencePath = Join-Path $Root "artifacts\evaluation\simulator-3d-control-$id.json"
} elseif (-not [IO.Path]::IsPathRooted($EvidencePath)) { $EvidencePath = Join-Path $Root $EvidencePath }
[IO.Directory]::CreateDirectory((Split-Path -Parent $EvidencePath)) | Out-Null
& $Python (Join-Path $Root "scripts\run_simulator_3d_control_acceptance.py") --scenario-manifest $Manifest --out $EvidencePath
if ($LASTEXITCODE -ne 0) { throw "3-D SIM control acceptance failed ($LASTEXITCODE): $EvidencePath" }
Write-Host "3-D SIM control evidence: $EvidencePath"

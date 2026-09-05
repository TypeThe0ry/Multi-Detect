param(
    [string]$ScenarioManifest = "configs\scenarios\river_rescue_vtol_001.scenario.json",
    [string]$EvidencePath = "",
    [string]$ContainerName = "multidetect-vtol-sitl-scenario-acceptance",
    [ValidateRange(1024, 65535)]
    [int]$MavlinkPort = 18570,
    [ValidateRange(0.0001, 1.0)]
    [double]$FaultTimeScale = 0.01,
    [ValidateRange(3, 120)]
    [double]$TransitionTimeoutSeconds = 15.0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PythonCandidates = @(
    (Join-Path $Root "ground-control\MultiDetectGCS\.venv\Scripts\python.exe"),
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
if ($PythonCandidates.Count -eq 0) {
    throw "No Python interpreter found. Build the QGC venv first."
}
$Python = @($PythonCandidates)[0]
$ManifestPath = if ([IO.Path]::IsPathRooted($ScenarioManifest)) {
    $ScenarioManifest
}
else {
    Join-Path $Root $ScenarioManifest
}
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Scenario manifest does not exist: $ManifestPath"
}
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$ArtifactDirectory = Join-Path $Root "artifacts\evaluation"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $ArtifactDirectory "px4-vtol-sitl-scenario-acceptance-$RunId.json"
}
elseif (-not [IO.Path]::IsPathRooted($EvidencePath)) {
    $EvidencePath = Join-Path $Root $EvidencePath
}

[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null
if (Test-Path -LiteralPath $EvidencePath) {
    throw "Evidence path already exists: $EvidencePath"
}

$Arguments = @(
    (Join-Path $Root "scripts\vtol_sitl_scenario_acceptance.py"),
    "--scenario-manifest", $ManifestPath,
    "--evidence", $EvidencePath,
    "--container-name", $ContainerName,
    "--mavlink-port", "$MavlinkPort",
    "--fault-time-scale", "$FaultTimeScale",
    "--transition-timeout-seconds", "$TransitionTimeoutSeconds"
)
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "PX4 Gazebo VTOL scenario acceptance failed with exit code $LASTEXITCODE. Evidence: $EvidencePath"
}
Write-Host "VTOL SIM evidence: $EvidencePath"

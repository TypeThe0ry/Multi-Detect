[CmdletBinding()]
param(
    # Every input has an explicit default so the current pre.8 binding cannot
    # silently drift to whichever file happens to be newest in artifacts/.
    [string]$RepositoryRoot = '',
    [string]$Python = '',
    [string]$VersionManifest = 'configs\product-version.json',
    [string]$ParityJson = 'configs\release-parity.json',
    [string]$ReplayEvidence = 'artifacts\evaluation\release-mode-replay-20260901-pre.8.json',
    [string]$SimEvidence = 'artifacts\evaluation\release-mode-sim-20260902-pre.8-combined-qgc-v4.json',
    [string]$HilEvidence = 'artifacts\evaluation\release-mode-hil-20260902-target-state-evidence-v1.json',
    [string]$RealEvidence = 'artifacts\evaluation\release-mode-real-20260901-pre.8-field-rerun.json',
    # v34 is a regenerated copy of the historical Gazebo SIM replay-set.  The
    # copy preserves every source frame/event while rebinding recording_hash
    # to the current schema (including optional telemetry fields); the
    # historical artifacts remain immutable provenance and are not used by a
    # no-argument run.
    [string]$ReplaySetManifest = 'artifacts\evaluation\terminal-continuation-v34\replay-set-sim-gazebo-2026\replay-set.manifest.json',
    [string]$ReplaySetAccuracyEvidence = 'artifacts\evaluation\terminal-continuation-v34\replay-set-sim-gazebo-2026\accuracy-report-20260904-pre8.json',
    [string]$ReplaySetCalibrationVerification = 'artifacts\evaluation\terminal-continuation-v34\replay-set-sim-gazebo-2026\threshold-calibration-20260904-pre8-verification.json',
    # Keep the no-argument wrapper bound to the latest same-day preflight.  A
    # stale field report is still preserved as historical evidence, but must
    # not silently become the canonical current input.
    [string]$RealFieldLinkPreflight = 'artifacts\evaluation\terminal-continuation-v33\field-link-preflight-live-20260904T-current.json',
    [ValidateRange(0, 86400)]
    [double]$RealFieldLinkMaxAgeSeconds = 900.0,
    # Optional six-strata field bundle.  Keep it empty by default so historic
    # pre.8 rebinding remains byte-compatible; when supplied, the manifest
    # writer rechecks the serialized report and binds its SHA-256.
    [string]$FieldEvidenceBundle = '',
    [string]$RealTargetAccuracyReport = '',
    [string]$RealTargetStateEvidenceReport = '',
    # Optional authenticated alert-link bench.  When supplied, the same
    # wrapper run binds the REAL non-loopback report into the E7 manifest;
    # leave it empty to preserve the prerelease software baseline.
    [string]$RealAlertLinkBench = '',
    # Optional hash-bound model-review envelope.  Keep it empty by default so
    # existing prerelease rebinding remains compatible; final release checks
    # must pass the reviewed deployment candidate explicitly.
    [string]$ModelReviewBundle = '',
    [string]$SimulatorSafetyEvidence = 'artifacts\evaluation\simulator-safety-acceptance-latest.json',
    [string]$ActuatorSafetyEvidence = 'artifacts\evaluation\actuator-safety-software-acceptance-latest.json',
    # Use the current portable-path matrix by default.  The legacy
    # acceptance-latest file remains available as historical provenance, but
    # it embeds checkout-specific absolute paths and must not be the canonical
    # migration/reproducibility binding.
    [string]$SimulatorScenarioMatrixEvidence = 'artifacts\evaluation\simulator-scenario-matrix-20260903-portable-paths-pre8.json',
    [string]$SimulatorCombinedEvidence = 'artifacts\evaluation\combined-sim-qgc-20260902-hwnd-fallback-v4.json',
    [string]$SimulatorCombinedReleaseModeEvidence = 'artifacts\evaluation\release-mode-sim-20260902-pre.8-combined-qgc-v4.json',
    # Bind the v62 join rebuilt from the six immutable external runtime/AI
    # source records against the current release-parity file. The prior v52
    # summary now fails source recomputation because it records an older parity
    # file hash, so it remains historical evidence only.
    [string]$SimulatorExternalEvidence = 'artifacts\evaluation\terminal-continuation-v63\simulator-external-runtime-ai-join-v63-current-parity-pre8.json',
    [string]$SimulatorExternalEvidenceRevalidation = 'artifacts\evaluation\terminal-continuation-v63\simulator-external-runtime-ai-revalidation-v63-current-parity-pre8.json',
    [string]$Px4AirframeTransactionEvidence = 'artifacts\evaluation\px4-airframe-mission-transaction-acceptance-latest.json',
    # External Docker/Gazebo response from fixed-wing, multirotor and VTOL,
    # measured exclusively from PX4 GLOBAL_POSITION_INT and bound to source.
    [string]$Px4ReportedPositionResponseMatrixEvidence = 'artifacts\evaluation\terminal-continuation-v63\px4-reported-position-response-matrix-v63-pre8.json',
    # Two positive runs and three explicit fail-closed faults for each airframe.
    [string]$Px4ReportedPositionResponseCampaignEvidence = 'artifacts\evaluation\terminal-continuation-v63\px4-reported-position-response-campaign-v63-pre8.json',
    # Optional HIL/REAL Mission feedback matrix. Each array item is
    # MODE|VEHICLE|MISSION_JSON|SIGNED_RUNTIME_PROFILE_JSON|REPORT_JSON.
    # Windows paths cannot contain '|',
    # which keeps this compact form unambiguous. Supply either zero or all six
    # HIL/REAL x fixed-wing/multirotor/VTOL combinations.
    [string[]]$Px4MissionFeedbackBinding = @(),
    # The v56 model SDFs bind the camera include pose to base_link and keep
    # source/wire camera rows top-to-bottom. Bind the regenerated evidence so
    # the release gate validates the current model/stack bytes instead of
    # accepting the historical pre-camera-frame v54 assets.
    [string[]]$Simulator3dControlEvidence = @(
        'artifacts\evaluation\terminal-continuation-v56\simulator-3d-control-v56-camera-frame-vtol-pre8.json',
        'artifacts\evaluation\terminal-continuation-v56\simulator-3d-control-v56-camera-frame-fixed-wing-pre8.json',
        'artifacts\evaluation\terminal-continuation-v56\simulator-3d-control-v56-camera-frame-multirotor-pre8.json'
    ),
    [string[]]$SimulatorSceneStreamSetEvidence = @(
        'artifacts\evaluation\terminal-continuation-v30\scene-stream-fixed-wing-20260904.json',
        'artifacts\evaluation\terminal-continuation-v30\scene-stream-multirotor-20260904.json',
        'artifacts\evaluation\terminal-continuation-v30\scene-stream-vtol-20260904.json'
    ),
    [string]$OutputDirectory = 'artifacts\evaluation',
    [string]$Stamp = '',
    [string]$EvaluatedAtUtc = '',
    [switch]$Strict
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptRoot = [string]$PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    throw 'Unable to resolve the release-gate wrapper directory'
}

$resolvedRepositoryRoot = if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    (Resolve-Path -LiteralPath (Join-Path $scriptRoot '..')).Path
}
else {
    (Resolve-Path -LiteralPath $RepositoryRoot).Path
}

function Resolve-RepositoryPath {
    param([Parameter(Mandatory)] [string]$Requested)

    if ([System.IO.Path]::IsPathRooted($Requested)) {
        return [System.IO.Path]::GetFullPath($Requested)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot $Requested))
}

function Resolve-Python {
    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        if (Test-Path -LiteralPath $Python -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($Python)
        }
        $explicit = Get-Command $Python -ErrorAction SilentlyContinue
        if ($null -ne $explicit -and -not [string]::IsNullOrWhiteSpace([string]$explicit.Source)) {
            return [string]$explicit.Source
        }
        throw "Python interpreter was not found: $Python"
    }

    $candidates = @(
        (Join-Path $resolvedRepositoryRoot 'ground-control\MultiDetectGCS\.venv\Scripts\python.exe'),
        (Join-Path $resolvedRepositoryRoot '.venv\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    foreach ($commandName in @('python.exe', 'python', 'py.exe', 'py')) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
            return [string]$command.Source
        }
    }
    throw 'No Python interpreter found. Install Python or the repository QGC venv.'
}

function Invoke-Python {
    param(
        [Parameter(Mandatory)] [string]$Interpreter,
        [Parameter(Mandatory)] [string[]]$Arguments
    )

    # StrictMode raises when a native process did not populate the automatic
    # LASTEXITCODE variable (this happens on some PowerShell hosts when the
    # interpreter is a venv executable and the invocation returns no native
    # status record).  Keep process-start failures visible, but make a missing
    # status deterministic instead of aborting before the manifest is written.
    $lines = @(& $Interpreter @Arguments 2>&1)
    $exitCode = 0
    $lastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -ne $lastExitCodeVariable) {
        $exitCode = [int]$lastExitCodeVariable.Value
    }
    return [pscustomobject]@{
        output = ($lines -join [Environment]::NewLine)
        exit_code = $exitCode
    }
}

function Assert-InputFile {
    param([Parameter(Mandatory)] [string]$Requested)

    $path = Resolve-RepositoryPath -Requested $Requested
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release-gate input is missing: $path"
    }
    return $path
}

function Assert-NewOutput {
    param([Parameter(Mandatory)] [string]$Path)

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if (Test-Path -LiteralPath $Path) {
        throw "Refusing to overwrite existing release-gate output: $Path"
    }
}

$python = Resolve-Python
$versionManifestPath = Assert-InputFile -Requested $VersionManifest
$version = Get-Content -LiteralPath $versionManifestPath -Raw | ConvertFrom-Json
$releaseLabel = [string]$version.release_label
if ([string]::IsNullOrWhiteSpace($releaseLabel)) {
    throw "Version manifest has no release_label: $versionManifestPath"
}

if ([string]::IsNullOrWhiteSpace($Stamp)) {
    $now = [DateTime]::UtcNow
    $Stamp = $now.ToString('yyyyMMddTHHmmssZ')
    if ([string]::IsNullOrWhiteSpace($EvaluatedAtUtc)) {
        $EvaluatedAtUtc = $now.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
}
if ($Stamp -notmatch '^[0-9]{8}T[0-9]{6}Z$') {
    throw "Stamp must use UTC format yyyyMMddTHHmmssZ: $Stamp"
}
if ([string]::IsNullOrWhiteSpace($EvaluatedAtUtc)) {
    $EvaluatedAtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
}

# Keep the established pre.8 filename convention while deriving the version
# suffix from the canonical manifest.  A later prerelease can be run with the
# same wrapper without editing its source.
$fileSuffix = if ($releaseLabel -match '^0\.3\.0-(.+)$') { $Matches[1] } else { $releaseLabel }
if ($fileSuffix -notmatch '^[A-Za-z0-9._-]+$') {
    throw "release_label contains filename-unsafe characters: $releaseLabel"
}
$outputRoot = Resolve-RepositoryPath -Requested $OutputDirectory
$baseName = "release-gate-$fileSuffix-current-$Stamp-v6-bound"
$manifestOut = Join-Path $outputRoot ($baseName + '.json')
$reportOut = Join-Path $outputRoot ($baseName + '-report.json')
$strictOut = Join-Path $outputRoot ($baseName + '-strict.json')
Assert-NewOutput -Path $manifestOut
Assert-NewOutput -Path $reportOut
if ($Strict) {
    Assert-NewOutput -Path $strictOut
}

$inputMap = [ordered]@{
    '--parity-json' = (Assert-InputFile -Requested $ParityJson)
    '--replay-evidence' = (Assert-InputFile -Requested $ReplayEvidence)
    '--sim-evidence' = (Assert-InputFile -Requested $SimEvidence)
    '--hil-evidence' = (Assert-InputFile -Requested $HilEvidence)
    '--real-evidence' = (Assert-InputFile -Requested $RealEvidence)
    '--replay-set-manifest' = (Assert-InputFile -Requested $ReplaySetManifest)
    '--replay-set-accuracy-evidence' = (Assert-InputFile -Requested $ReplaySetAccuracyEvidence)
    '--replay-set-calibration-verification' = (Assert-InputFile -Requested $ReplaySetCalibrationVerification)
    '--real-field-link-preflight' = (Assert-InputFile -Requested $RealFieldLinkPreflight)
    '--simulator-safety-evidence' = (Assert-InputFile -Requested $SimulatorSafetyEvidence)
    '--actuator-safety-evidence' = (Assert-InputFile -Requested $ActuatorSafetyEvidence)
    '--simulator-scenario-matrix-evidence' = (Assert-InputFile -Requested $SimulatorScenarioMatrixEvidence)
    '--simulator-combined-evidence' = (Assert-InputFile -Requested $SimulatorCombinedEvidence)
    '--simulator-combined-release-mode-evidence' = (Assert-InputFile -Requested $SimulatorCombinedReleaseModeEvidence)
    '--simulator-external-evidence' = (Assert-InputFile -Requested $SimulatorExternalEvidence)
    '--simulator-external-evidence-revalidation' = (Assert-InputFile -Requested $SimulatorExternalEvidenceRevalidation)
    '--px4-airframe-transaction-evidence' = (Assert-InputFile -Requested $Px4AirframeTransactionEvidence)
    '--px4-reported-position-response-matrix-evidence' = (Assert-InputFile -Requested $Px4ReportedPositionResponseMatrixEvidence)
    '--px4-reported-position-response-campaign-evidence' = (Assert-InputFile -Requested $Px4ReportedPositionResponseCampaignEvidence)
}
$optionalInputMap = [ordered]@{
    '--field-evidence-bundle' = $FieldEvidenceBundle
    '--real-target-accuracy-report' = $RealTargetAccuracyReport
    '--real-target-state-evidence-report' = $RealTargetStateEvidenceReport
    '--real-alert-link-bench' = $RealAlertLinkBench
    '--model-review-bundle' = $ModelReviewBundle
}
$controlArguments = [System.Collections.Generic.List[string]]::new()
$controlArguments.Add((Resolve-RepositoryPath -Requested 'scripts\create_release_gate_manifest.py'))
$controlArguments.Add('--out')
$controlArguments.Add($manifestOut)
foreach ($entry in $inputMap.GetEnumerator()) {
    $controlArguments.Add([string]$entry.Key)
    $controlArguments.Add([string]$entry.Value)
}
foreach ($entry in $optionalInputMap.GetEnumerator()) {
    if (-not [string]::IsNullOrWhiteSpace([string]$entry.Value)) {
        $controlArguments.Add([string]$entry.Key)
        $controlArguments.Add((Assert-InputFile -Requested ([string]$entry.Value)))
    }
}
foreach ($path in $Simulator3dControlEvidence) {
    $controlArguments.Add('--simulator-3d-control-evidence')
    $controlArguments.Add((Assert-InputFile -Requested $path))
}
foreach ($path in $SimulatorSceneStreamSetEvidence) {
    $controlArguments.Add('--simulator-scene-stream-set-evidence')
    $controlArguments.Add((Assert-InputFile -Requested $path))
}
foreach ($binding in $Px4MissionFeedbackBinding) {
    $parts = @($binding -split '\|', 5)
    if ($parts.Count -ne 5) {
        throw "PX4 Mission feedback binding must be MODE|VEHICLE|MISSION_JSON|SIGNED_RUNTIME_PROFILE_JSON|REPORT_JSON: $binding"
    }
    $mode = [string]$parts[0]
    $vehicle = [string]$parts[1]
    if ([string]::IsNullOrWhiteSpace($mode) -or [string]::IsNullOrWhiteSpace($vehicle)) {
        throw "PX4 Mission feedback binding has an empty mode or vehicle: $binding"
    }
    $controlArguments.Add('--px4-mission-feedback-evidence')
    $controlArguments.Add($mode)
    $controlArguments.Add($vehicle)
    $controlArguments.Add((Assert-InputFile -Requested ([string]$parts[2])))
    $controlArguments.Add((Assert-InputFile -Requested ([string]$parts[3])))
    $controlArguments.Add((Assert-InputFile -Requested ([string]$parts[4])))
}
$controlArguments.Add('--real-field-link-max-age-s')
$controlArguments.Add($RealFieldLinkMaxAgeSeconds.ToString([Globalization.CultureInfo]::InvariantCulture))
$controlArguments.Add('--allow-incomplete')
$controlArguments.Add('--no-overwrite')

Write-Output "Release-gate version: $releaseLabel"
Write-Output "Manifest: $manifestOut"
Write-Output "Report: $reportOut"
$manifestResult = Invoke-Python -Interpreter $python -Arguments $controlArguments.ToArray()
if ($manifestResult.exit_code -ne 0) {
    throw "Manifest generation failed with exit code $($manifestResult.exit_code): $($manifestResult.output)"
}
if (-not (Test-Path -LiteralPath $manifestOut -PathType Leaf)) {
    throw "Manifest generation did not create its output: $manifestOut"
}

$checkScript = Resolve-RepositoryPath -Requested 'scripts\check_release_gate.py'
$reportArguments = [System.Collections.Generic.List[string]]::new()
$reportArguments.Add($checkScript)
$reportArguments.Add($manifestOut)
$reportArguments.Add('--version-manifest')
$reportArguments.Add($versionManifestPath)
$reportArguments.Add('--out')
$reportArguments.Add($reportOut)
$reportArguments.Add('--evaluated-at-utc')
$reportArguments.Add($EvaluatedAtUtc)
$reportArguments.Add('--report-only')
$reportResult = Invoke-Python -Interpreter $python -Arguments $reportArguments.ToArray()
if ($reportResult.exit_code -ne 0) {
    throw "Report evaluation failed with exit code $($reportResult.exit_code): $($reportResult.output)"
}
if (-not (Test-Path -LiteralPath $reportOut -PathType Leaf)) {
    throw "Release-gate evaluation did not create its report: $reportOut"
}

$report = Get-Content -LiteralPath $reportOut -Raw | ConvertFrom-Json
Write-Output (
    'Report status: gate_passed={0}; release_allowed={1}; evaluated_at_utc={2}' -f
    [bool]$report.gate_passed, [bool]$report.release_allowed, $report.evaluated_at_utc
)
if ($report.release_blocked_reasons) {
    Write-Output ('Blocked reasons: {0}' -f (@($report.release_blocked_reasons) -join '; '))
}

if ($Strict) {
    $strictArguments = [System.Collections.Generic.List[string]]::new()
    $strictArguments.Add($checkScript)
    $strictArguments.Add($manifestOut)
    $strictArguments.Add('--version-manifest')
    $strictArguments.Add($versionManifestPath)
    $strictArguments.Add('--out')
    $strictArguments.Add($strictOut)
    $strictArguments.Add('--evaluated-at-utc')
    $strictArguments.Add($EvaluatedAtUtc)
    $strictResult = Invoke-Python -Interpreter $python -Arguments $strictArguments.ToArray()
    if ($strictResult.exit_code -notin @(0, 2)) {
        throw "Strict release-gate evaluation failed unexpectedly with exit code $($strictResult.exit_code): $($strictResult.output)"
    }
    if (-not (Test-Path -LiteralPath $strictOut -PathType Leaf)) {
        throw "Strict release-gate evaluation did not create its report: $strictOut"
    }
    Write-Output "Strict report: $strictOut (exit=$($strictResult.exit_code))"
    exit $strictResult.exit_code
}

# The default is a diagnostic/report-only run: pending field, visual, HIL and
# REAL gates remain visible in JSON without making routine evidence rebinding
# fail.  -Strict is the explicit final-release check and returns its gate code.
exit 0

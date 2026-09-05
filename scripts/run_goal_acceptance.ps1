[CmdletBinding()]
param(
    [string]$CoreRoot = '',
    [string]$QgcRoot = '',
    [string]$QgcBuildRoot = '',
    [string]$EvidencePath = '',
    [string]$FixedWingAcceptanceOutput = '',
    [string]$ActuatorSafetyOutput = '',
    [string]$Mode2PayloadAcceptanceOutput = '',
    [string]$Mode3ApproachAcceptanceOutput = '',
    [string]$SimulatorSafetyOutput = '',
    [string]$Px4AirframeTransactionOutput = '',
    [string]$SimulatorScenarioMatrixOutput = '',
    [string]$Simulator3dVtolOutput = '',
    [string]$Simulator3dFixedWingOutput = '',
    [string]$Simulator3dMultirotorOutput = '',
    [string]$TestTempRoot = '',
    [string]$FieldLinkPreflight = '',
    [ValidateRange(1, 86400)]
    [double]$FieldLinkMaxAgeSeconds = 900.0,
    [string]$ReplaySetManifest = '',
    [string]$ReplaySetAccuracyReport = '',
    [string]$ReplaySetReadinessOutput = '',
    [string]$CombinedSimEvidence = '',
    [string]$CombinedSimReleaseModeEvidence = '',
    [string]$SimulatorExternalEvidence = '',
    [string]$SimulatorExternalRevalidationOutput = '',
    [switch]$ComputerUseOperatorAuthorized,
    [switch]$RequireFieldLinkPreflight,
    [switch]$RequireReplaySet,
    [switch]$RequireCombinedSimEvidence,
    [switch]$RequireSimulatorExternalEvidence,
    [switch]$RequireSimulator3dControlEvidence,
    # Keep software/static acceptance runnable after a workstation migration
    # without claiming that production signing credentials are provisioned.
    [switch]$AllowMissingOperatorCredentials
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Windows PowerShell does not materialize the automatic LASTEXITCODE variable
# in every host when a native command produces no output (and nested
# powershell.exe -File invocations make that difference observable).  Reading
# it directly under StrictMode can therefore abort the acceptance run before
# the actual command result is recorded.  Keep the normal native exit code
# semantics, while treating an absent variable as the successful default for
# a command that never set one.
function Get-NativeExitCode {
    $lastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -eq $lastExitCodeVariable) {
        return 0
    }
    try {
        return [int]$lastExitCodeVariable.Value
    }
    catch {
        # An existing but malformed exit-code variable is a failed probe; do
        # not silently turn it into a passing gate.
        return 1
    }
}

# Parameter defaults are evaluated before PowerShell reliably populates
# ``$PSScriptRoot`` in every host (notably a nested ``powershell.exe -File``
# invocation).  Resolve the script location after the parameter block so the
# documented no-argument entry point works after a workstation migration.
$scriptRoot = [string]$PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    throw "Unable to resolve the acceptance script directory"
}
$defaultCoreRoot = Split-Path -Parent $scriptRoot
if ([string]::IsNullOrWhiteSpace($CoreRoot)) {
    $CoreRoot = $defaultCoreRoot
}
if ([string]::IsNullOrWhiteSpace($QgcRoot)) {
    $QgcRoot = Join-Path $defaultCoreRoot "ground-control\MultiDetectGCS"
}
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $defaultCoreRoot "artifacts/evaluation/multidetect-goal-acceptance-latest.json"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]]$ArgumentList,
        [Parameter(Mandatory)] [string]$WorkingDirectory,
        [switch]$DiscardOutput
    )

    Push-Location $WorkingDirectory
    try {
        if ($DiscardOutput) {
            & $FilePath @ArgumentList | Out-Null
        }
        else {
            & $FilePath @ArgumentList
        }
        $commandExitCode = Get-NativeExitCode
        if ($commandExitCode -ne 0) {
            throw "Command failed with exit code ${commandExitCode}: $FilePath $($ArgumentList -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Test-IcmpReachable {
    param([Parameter(Mandatory)] [string]$ComputerName)

    # ``Test-Connection -TimeoutSeconds`` exists in PowerShell 6+, but not in
    # the Windows PowerShell 5.1 host still used by some migrated workstations.
    # The .NET Ping API gives both hosts the same one-second bounded probe.
    $ping = New-Object System.Net.NetworkInformation.Ping
    try {
        $reply = $ping.Send($ComputerName, 1000)
        return $reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success
    }
    catch {
        return $false
    }
    finally {
        $ping.Dispose()
    }
}

function Resolve-OutputArtifactPath {
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$Requested,
        [Parameter(Mandatory)] [string]$DefaultPath
    )

    $candidate = if ([string]::IsNullOrWhiteSpace($Requested)) {
        $DefaultPath
    }
    elseif ([System.IO.Path]::IsPathRooted($Requested)) {
        $Requested
    }
    else {
        Join-Path $CoreRoot $Requested
    }
    $resolved = [System.IO.Path]::GetFullPath($candidate)
    $parent = Split-Path -Parent $resolved
    if (-not [string]::IsNullOrWhiteSpace($parent) -and
            -not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    return $resolved
}

function Get-FieldLinkPreflightBinding {
    param(
        # Windows PowerShell rejects an empty string for a Mandatory string
        # parameter before the function body can treat it as "not supplied".
        # The no-field-link software gate intentionally passes the default
        # empty value, so opt into that value explicitly.
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$ReportPath,
        [Parameter(Mandatory)] [bool]$Required
    )

    # A goal-acceptance snapshot may be produced before the aircraft is
    # connected, so the binding is optional by default.  When a report is
    # supplied, however, consume it through the standalone verifier and keep
    # its path/hash/readiness in the evidence.  A caller can make that input a
    # hard prerequisite with -RequireFieldLinkPreflight.
    $binding = [ordered]@{
        provided = -not [string]::IsNullOrWhiteSpace($ReportPath)
        required = $Required
        path = $null
        report_sha256 = $null
        report_bytes = 0
        max_age_s = $FieldLinkMaxAgeSeconds
        report_timestamp_epoch_s = $null
        report_age_s = $null
        freshness = $null
        verified = $false
        hardware_link_ready = $false
        route_checks_requested = $false
        route_checks_passed = $null
        route_source_cidr = $null
        gate_passed = $false
        verification_exit_code = $null
        reasons = @()
    }
    if (-not [bool]$binding.provided) {
        if ($Required) {
            $binding.reasons = @("field-link preflight path is required")
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    try {
        $resolvedReport = (Resolve-Path -LiteralPath $ReportPath -ErrorAction Stop).Path
    }
    catch {
        $binding.reasons = @("field-link preflight path cannot be resolved: $ReportPath")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    if (-not (Test-Path -LiteralPath $resolvedReport -PathType Leaf)) {
        $binding.reasons = @("field-link preflight report is not a file: $resolvedReport")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $binding.path = [System.IO.Path]::GetFullPath($resolvedReport)
    $binding.report_bytes = [int64](Get-Item -LiteralPath $binding.path).Length
    $binding.report_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.path).Hash.ToLowerInvariant()

    $maxAgeArgument = ('{0:R}' -f $FieldLinkMaxAgeSeconds)
    $verificationOutput = (& $python $fieldLinkVerifier $binding.path '--max-age-s' $maxAgeArgument 2>&1 | Out-String)
    $binding.verification_exit_code = Get-NativeExitCode
    try {
        $verification = $verificationOutput | ConvertFrom-Json
    }
    catch {
        $binding.reasons = @("field-link preflight verifier output is not valid JSON: $($_.Exception.Message)")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    $binding.verified = $true
    if ($verification.PSObject.Properties.Name -contains 'max_age_s' -and
            $null -ne $verification.max_age_s) {
        $binding.max_age_s = [double]$verification.max_age_s
    }
    if ($verification.PSObject.Properties.Name -contains 'report_timestamp_epoch_s') {
        $binding.report_timestamp_epoch_s = $verification.report_timestamp_epoch_s
    }
    if ($verification.PSObject.Properties.Name -contains 'report_age_s') {
        $binding.report_age_s = $verification.report_age_s
    }
    if ($verification.PSObject.Properties.Name -contains 'checks' -and
            $null -ne $verification.checks -and
            $verification.checks.PSObject.Properties.Name -contains 'freshness') {
        $binding.freshness = $verification.checks.freshness
    }
    $binding.hardware_link_ready = [bool]$verification.hardware_link_ready
    if ($null -ne $verification.summary) {
        $binding.route_checks_requested = [bool]$verification.summary.route_checks_requested
        if ($null -eq $verification.summary.route_checks_passed) {
            $binding.route_checks_passed = $null
        }
        else {
            $binding.route_checks_passed = [bool]$verification.summary.route_checks_passed
        }
        if ($null -ne $verification.summary.route_source_cidr) {
            $binding.route_source_cidr = [string]$verification.summary.route_source_cidr
        }
    }
    $binding.gate_passed = ($binding.verification_exit_code -eq 0) -and [bool]$verification.gate_passed
    if ($null -ne $verification.reasons) {
        $binding.reasons = @($verification.reasons | ForEach-Object { [string]$_ })
    }
    if ($Required -and -not [bool]$binding.gate_passed) {
        $reasonText = if (@($binding.reasons).Count -gt 0) {
            $binding.reasons -join '; '
        }
        else {
            "verifier exit code $($binding.verification_exit_code)"
        }
        throw "Field-link preflight is not ready: $reasonText"
    }
    return $binding
}

function Get-ReplaySetReadinessBinding {
    param(
        # Replay evidence is optional for software-only acceptance snapshots,
        # but when a path is supplied it is always re-verified through the
        # standalone readiness command.  ``AllowEmptyString`` keeps the
        # default no-argument entry point valid on Windows PowerShell 5.1.
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$ManifestPath,
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$AccuracyReportPath,
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$ReadinessOutputPath,
        [Parameter(Mandatory)] [bool]$Required
    )

    # The goal snapshot must make the REPLAY gate explicit.  A missing
    # manifest is therefore represented as an unprovided/pending binding,
    # rather than silently omitting the gate from the evidence.
    $binding = [ordered]@{
        provided = -not [string]::IsNullOrWhiteSpace($ManifestPath)
        required = $Required
        manifest_path = $null
        manifest_sha256 = $null
        canonical_sha256 = $null
        dataset_id = $null
        dataset_version = $null
        accuracy_report_path = $null
        accuracy_report_sha256 = $null
        accuracy_report_bytes = 0
        readiness_path = $null
        readiness_sha256 = $null
        readiness_bytes = 0
        manifest_gate_passed = $false
        accuracy_gate_passed = $false
        gate_passed = $false
        verification_exit_code = $null
        verified = $false
        blocking_reasons = @()
        next_actions = @()
        reasons = @()
    }

    if (-not [bool]$binding.provided) {
        $binding.reasons = @("replay-set manifest path was not supplied")
        $binding.blocking_reasons = @(
            "provide a complete six-stratum deployment replay-set manifest and accuracy report"
        )
        if ($Required) {
            throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
        }
        return $binding
    }

    $manifestCandidate = if ([System.IO.Path]::IsPathRooted($ManifestPath)) {
        $ManifestPath
    }
    else {
        Join-Path $CoreRoot $ManifestPath
    }
    try {
        $resolvedManifest = (Resolve-Path -LiteralPath $manifestCandidate -ErrorAction Stop).Path
    }
    catch {
        $binding.reasons = @("replay-set manifest path cannot be resolved: $ManifestPath")
        if ($Required) {
            throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
        }
        return $binding
    }
    if (-not (Test-Path -LiteralPath $resolvedManifest -PathType Leaf)) {
        $binding.reasons = @("replay-set manifest is not a file: $resolvedManifest")
        if ($Required) {
            throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
        }
        return $binding
    }
    $binding.manifest_path = [System.IO.Path]::GetFullPath($resolvedManifest)
    $binding.manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.manifest_path).Hash.ToLowerInvariant()

    if (-not [string]::IsNullOrWhiteSpace($AccuracyReportPath)) {
        $accuracyCandidate = if ([System.IO.Path]::IsPathRooted($AccuracyReportPath)) {
            $AccuracyReportPath
        }
        else {
            Join-Path $CoreRoot $AccuracyReportPath
        }
        try {
            $resolvedAccuracy = (Resolve-Path -LiteralPath $accuracyCandidate -ErrorAction Stop).Path
        }
        catch {
            $binding.reasons = @("replay-set accuracy report path cannot be resolved: $AccuracyReportPath")
            if ($Required) {
                throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
            }
            return $binding
        }
        if (-not (Test-Path -LiteralPath $resolvedAccuracy -PathType Leaf)) {
            $binding.reasons = @("replay-set accuracy report is not a file: $resolvedAccuracy")
            if ($Required) {
                throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
            }
            return $binding
        }
        $binding.accuracy_report_path = [System.IO.Path]::GetFullPath($resolvedAccuracy)
        $binding.accuracy_report_bytes = [int64](Get-Item -LiteralPath $binding.accuracy_report_path).Length
        $binding.accuracy_report_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.accuracy_report_path).Hash.ToLowerInvariant()
    }

    $binding.readiness_path = Resolve-OutputArtifactPath `
        -Requested $ReadinessOutputPath `
        -DefaultPath (Join-Path $CoreRoot "artifacts/evaluation/replay-set-readiness-goal-acceptance-latest.json")
    $inputPaths = @($binding.manifest_path)
    if (-not [string]::IsNullOrWhiteSpace([string]$binding.accuracy_report_path)) {
        $inputPaths += [string]$binding.accuracy_report_path
    }
    if (@($inputPaths | Where-Object { [System.IO.Path]::GetFullPath($_) -eq [string]$binding.readiness_path }).Count -gt 0) {
        $binding.reasons = @("replay-set readiness output must be different from its input manifest/report")
        if ($Required) {
            throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
        }
        return $binding
    }

    $readinessArguments = @(
        $replaySetReadinessScript,
        $binding.manifest_path,
        "--out",
        $binding.readiness_path
    )
    if (-not [string]::IsNullOrWhiteSpace([string]$binding.accuracy_report_path)) {
        $readinessArguments += @("--accuracy-report", $binding.accuracy_report_path)
    }
    $verificationOutput = (& $python @readinessArguments 2>&1 | Out-String)
    $binding.verification_exit_code = Get-NativeExitCode
    $readiness = $null
    try {
        $readiness = $verificationOutput | ConvertFrom-Json
    }
    catch {
        $binding.reasons = @("replay-set readiness output is not valid JSON: $($_.Exception.Message)")
        if ($Required) {
            throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
        }
        return $binding
    }
    if (-not (Test-Path -LiteralPath $binding.readiness_path -PathType Leaf)) {
        $binding.reasons = @("replay-set readiness command did not produce output: $($binding.readiness_path)")
        if ($Required) {
            throw "Replay-set readiness is not ready: $($binding.reasons -join '; ')"
        }
        return $binding
    }

    $binding.readiness_bytes = [int64](Get-Item -LiteralPath $binding.readiness_path).Length
    $binding.readiness_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.readiness_path).Hash.ToLowerInvariant()
    $binding.verified = $true
    if ($readiness.PSObject.Properties.Name -contains 'manifest_sha256') {
        $reportedManifestSha = [string]$readiness.manifest_sha256
        if ($reportedManifestSha -ine [string]$binding.manifest_sha256) {
            $binding.reasons += "replay-set readiness manifest hash does not match the input manifest"
        }
    }
    else {
        $binding.reasons += "replay-set readiness report is missing manifest_sha256"
    }
    if ($readiness.PSObject.Properties.Name -contains 'canonical_sha256') {
        $binding.canonical_sha256 = [string]$readiness.canonical_sha256
    }
    if ($readiness.PSObject.Properties.Name -contains 'dataset_id') {
        $binding.dataset_id = [string]$readiness.dataset_id
    }
    if ($readiness.PSObject.Properties.Name -contains 'dataset_version') {
        $binding.dataset_version = [string]$readiness.dataset_version
    }
    if ($readiness.PSObject.Properties.Name -contains 'manifest_gate_passed') {
        $binding.manifest_gate_passed = [bool]$readiness.manifest_gate_passed
    }
    if ($readiness.PSObject.Properties.Name -contains 'accuracy_gate_passed') {
        $binding.accuracy_gate_passed = [bool]$readiness.accuracy_gate_passed
    }
    if ($readiness.PSObject.Properties.Name -contains 'blocking_reasons' -and
            $null -ne $readiness.blocking_reasons) {
        $binding.blocking_reasons = @($readiness.blocking_reasons | ForEach-Object { [string]$_ })
    }
    if ($readiness.PSObject.Properties.Name -contains 'next_actions' -and
            $null -ne $readiness.next_actions) {
        $binding.next_actions = @($readiness.next_actions | ForEach-Object { [string]$_ })
    }
    $binding.gate_passed = ($binding.verification_exit_code -in @(0, 2)) -and
        ($readiness.PSObject.Properties.Name -contains 'gate_passed') -and
        [bool]$readiness.gate_passed -and
        @($binding.reasons).Count -eq 0
    if ($binding.verification_exit_code -notin @(0, 2)) {
        $binding.reasons += "replay-set readiness verifier returned unexpected exit code $($binding.verification_exit_code)"
    }
    $binding.reasons = @($binding.reasons | Select-Object -Unique)
    if ($Required -and -not [bool]$binding.gate_passed) {
        $reasonText = if (@($binding.reasons).Count -gt 0) {
            $binding.reasons -join '; '
        }
        elseif (@($binding.blocking_reasons).Count -gt 0) {
            $binding.blocking_reasons -join '; '
        }
        else {
            "readiness report gate_passed=false"
        }
        throw "Replay-set readiness is not ready: $reasonText"
    }
    return $binding
}

function Get-CombinedSimBinding {
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$EvidencePath,
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$ReleaseModeEvidencePath,
        [Parameter(Mandatory)] [bool]$Required
    )

    # Combined SIM evidence is an optional software provenance input for the
    # goal snapshot.  When supplied, both the source JSON and its release-mode
    # envelope are rehashed by the standalone verifier; a caller can promote
    # the binding to a hard prerequisite with -RequireCombinedSimEvidence.
    $binding = [ordered]@{
        provided = (-not [string]::IsNullOrWhiteSpace($EvidencePath)) -or
            (-not [string]::IsNullOrWhiteSpace($ReleaseModeEvidencePath))
        required = $Required
        evidence_path = $null
        evidence_sha256 = $null
        evidence_bytes = 0
        release_mode_path = $null
        release_mode_sha256 = $null
        release_mode_bytes = 0
        verified = $false
        gate_passed = $false
        verification_exit_code = $null
        verification = $null
        reasons = @()
    }
    if (-not [bool]$binding.provided) {
        if ($Required) {
            $binding.reasons = @("combined SIM evidence and release-mode envelope are required")
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    if ([string]::IsNullOrWhiteSpace($EvidencePath) -or
            [string]::IsNullOrWhiteSpace($ReleaseModeEvidencePath)) {
        $binding.reasons = @("combined SIM evidence requires both source and release-mode paths")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    try {
        $resolvedEvidence = (Resolve-Path -LiteralPath $EvidencePath -ErrorAction Stop).Path
        $resolvedReleaseMode = (Resolve-Path -LiteralPath $ReleaseModeEvidencePath -ErrorAction Stop).Path
    }
    catch {
        $binding.reasons = @("combined SIM evidence path cannot be resolved: $EvidencePath / $ReleaseModeEvidencePath")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    foreach ($candidate in @($resolvedEvidence, $resolvedReleaseMode)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $binding.reasons = @("combined SIM evidence path is not a file: $candidate")
            if ($Required) {
                throw ($binding.reasons -join '; ')
            }
            return $binding
        }
    }
    if ([System.IO.Path]::GetFullPath($resolvedEvidence) -eq
            [System.IO.Path]::GetFullPath($resolvedReleaseMode)) {
        $binding.reasons = @("combined SIM source and release-mode evidence must be different files")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $binding.evidence_path = [System.IO.Path]::GetFullPath($resolvedEvidence)
    $binding.evidence_bytes = [int64](Get-Item -LiteralPath $binding.evidence_path).Length
    $binding.evidence_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.evidence_path).Hash.ToLowerInvariant()
    $binding.release_mode_path = [System.IO.Path]::GetFullPath($resolvedReleaseMode)
    $binding.release_mode_bytes = [int64](Get-Item -LiteralPath $binding.release_mode_path).Length
    $binding.release_mode_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.release_mode_path).Hash.ToLowerInvariant()

    $verificationOutput = (& $python $combinedVerifier $binding.evidence_path `
        "--expected-release-label" ([string]$productVersion.release_label) `
        "--release-mode-evidence" $binding.release_mode_path `
        "--require-release-mode" 2>&1 | Out-String)
    $binding.verification_exit_code = Get-NativeExitCode
    try {
        $binding.verification = $verificationOutput | ConvertFrom-Json
    }
    catch {
        $binding.reasons = @("combined SIM verifier output is not valid JSON: $($_.Exception.Message)")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    $binding.verified = $true
    $binding.gate_passed = ($binding.verification_exit_code -eq 0) -and
        [bool]$binding.verification.gate_passed
    if ($null -ne $binding.verification.reasons) {
        $binding.reasons = @($binding.verification.reasons | ForEach-Object { [string]$_ })
    }
    if ($Required -and -not [bool]$binding.gate_passed) {
        $reasonText = if (@($binding.reasons).Count -gt 0) {
            $binding.reasons -join '; '
        }
        else {
            "verifier exit code $($binding.verification_exit_code)"
        }
        throw "Combined SIM evidence is not valid: $reasonText"
    }
    return $binding
}

function Get-SimulatorExternalEvidenceBinding {
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$JoinPath,
        [Parameter(Mandatory)] [AllowEmptyString()] [string]$RevalidationPath,
        [Parameter(Mandatory)] [bool]$Required
    )

    # External SIM evidence is an optional software provenance input for the
    # goal snapshot.  When supplied, the stored three-airframe join is
    # revalidated against all six source reports.  This keeps the acceptance
    # artifact tied to reproducible VTOL/fixed-wing/multirotor runtime and
    # live-AI evidence instead of trusting a hand-edited summary.
    $binding = [ordered]@{
        provided = -not [string]::IsNullOrWhiteSpace($JoinPath)
        required = $Required
        path = $null
        report_sha256 = $null
        report_bytes = 0
        revalidation_path = $null
        revalidation_sha256 = $null
        revalidation_bytes = 0
        verified = $false
        source_recomputed = $false
        release_label = $null
        expected_release_label = [string]$productVersion.release_label
        gate_passed = $false
        verification_exit_code = $null
        reasons = @()
    }

    if (-not [bool]$binding.provided) {
        if ($Required) {
            $binding.reasons = @("simulator external evidence path is required")
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $joinCandidate = if ([System.IO.Path]::IsPathRooted($JoinPath)) {
        $JoinPath
    }
    else {
        Join-Path $CoreRoot $JoinPath
    }
    try {
        $resolvedJoin = (Resolve-Path -LiteralPath $joinCandidate -ErrorAction Stop).Path
    }
    catch {
        $binding.reasons = @("simulator external evidence path cannot be resolved: $JoinPath")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    if (-not (Test-Path -LiteralPath $resolvedJoin -PathType Leaf)) {
        $binding.reasons = @("simulator external evidence is not a file: $resolvedJoin")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    $binding.path = [System.IO.Path]::GetFullPath($resolvedJoin)
    $binding.report_bytes = [int64](Get-Item -LiteralPath $binding.path).Length
    $binding.report_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.path).Hash.ToLowerInvariant()

    if ([string]::IsNullOrWhiteSpace($RevalidationPath)) {
        $binding.reasons = @("simulator external evidence revalidation output path is required")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    $revalidationCandidate = if ([System.IO.Path]::IsPathRooted($RevalidationPath)) {
        $RevalidationPath
    }
    else {
        Join-Path $CoreRoot $RevalidationPath
    }
    $binding.revalidation_path = [System.IO.Path]::GetFullPath($revalidationCandidate)
    if ($binding.path -eq $binding.revalidation_path) {
        $binding.reasons = @("simulator external evidence and revalidation output must be different files")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $verificationOutput = (& $python $simulatorExternalVerifier `
        "--join" $binding.path `
        "--expected-release-label" ([string]$productVersion.release_label) `
        "--out" $binding.revalidation_path 2>&1 | Out-String)
    $binding.verification_exit_code = Get-NativeExitCode
    $verification = $null
    try {
        $verification = $verificationOutput | ConvertFrom-Json
    }
    catch {
        $binding.reasons = @("simulator external evidence verifier output is not valid JSON: $($_.Exception.Message)")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $binding.verified = $true
    if ($verification.PSObject.Properties.Name -contains 'source_recomputed') {
        $binding.source_recomputed = [bool]$verification.source_recomputed
    }
    if ($verification.PSObject.Properties.Name -contains 'release_label') {
        $binding.release_label = [string]$verification.release_label
    }
    if ($verification.PSObject.Properties.Name -contains 'expected_release_label' -and
            -not [string]::IsNullOrWhiteSpace([string]$verification.expected_release_label)) {
        $binding.expected_release_label = [string]$verification.expected_release_label
    }
    if ($verification.PSObject.Properties.Name -contains 'reasons' -and $null -ne $verification.reasons) {
        $binding.reasons = @($verification.reasons | ForEach-Object { [string]$_ })
    }
    if (-not $binding.source_recomputed) {
        if (@($binding.reasons) -notcontains "simulator external evidence source recomputation did not run") {
            $binding.reasons += "simulator external evidence source recomputation did not run"
        }
    }
    if ($binding.release_label -ne [string]$productVersion.release_label) {
        $binding.reasons += "simulator external evidence release label does not match product manifest"
    }
    if ($binding.expected_release_label -ne [string]$productVersion.release_label) {
        $binding.reasons += "simulator external evidence verifier expected-release label does not match product manifest"
    }

    if ($verification.PSObject.Properties.Name -contains 'report_sha256' -and
            [string]$verification.report_sha256 -ine [string]$binding.report_sha256) {
        $binding.reasons += "simulator external evidence report hash does not match the join input"
    }
    if ($verification.PSObject.Properties.Name -contains 'report_bytes' -and
            [int64]$verification.report_bytes -ne [int64]$binding.report_bytes) {
        $binding.reasons += "simulator external evidence report byte count does not match the join input"
    }
    if ($verification.PSObject.Properties.Name -contains 'report_path' -and
            -not [string]::IsNullOrWhiteSpace([string]$verification.report_path)) {
        try {
            $reportedPath = [System.IO.Path]::GetFullPath([string]$verification.report_path)
            if ($reportedPath -ne [string]$binding.path) {
                $binding.reasons += "simulator external evidence verifier bound a different join path"
            }
        }
        catch {
            $binding.reasons += "simulator external evidence verifier report path is invalid"
        }
    }

    if (Test-Path -LiteralPath $binding.revalidation_path -PathType Leaf) {
        $binding.revalidation_bytes = [int64](Get-Item -LiteralPath $binding.revalidation_path).Length
        $binding.revalidation_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binding.revalidation_path).Hash.ToLowerInvariant()
    }
    else {
        $binding.reasons += "simulator external evidence verifier did not produce revalidation output"
    }
    $binding.gate_passed = ($binding.verification_exit_code -eq 0) -and
        ($verification.PSObject.Properties.Name -contains 'gate_passed') -and
        [bool]$verification.gate_passed -and
        [bool]$binding.source_recomputed -and
        @($binding.reasons).Count -eq 0
    if ($Required -and -not [bool]$binding.gate_passed) {
        $reasonText = if (@($binding.reasons).Count -gt 0) {
            $binding.reasons -join '; '
        }
        else {
            "verifier exit code $($binding.verification_exit_code)"
        }
        throw "Simulator external evidence is not valid: $reasonText"
    }
    return $binding
}

function Get-Simulator3dControlBinding {
    param(
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]]$EvidencePaths,
        [Parameter(Mandatory)] [string]$ManifestDirectory,
        [Parameter(Mandatory)] [bool]$Required
    )

    # The deterministic 3-D control gate is an optional provenance input for
    # the goal snapshot.  If supplied, all three airframes are re-opened by
    # the independent verifier and their source hashes are recorded.  The
    # external PX4/Gazebo and visual/hardware fields remain not_claimed.
    $binding = [ordered]@{
        provided = @($EvidencePaths).Count -gt 0
        required = $Required
        paths = @()
        evidence = @()
        verified = $false
        gate_passed = $false
        verification_exit_code = $null
        verification = $null
        reasons = @()
    }
    if (-not [bool]$binding.provided) {
        if ($Required) {
            $binding.reasons = @("three-airframe 3-D control evidence is required")
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    if (@($EvidencePaths).Count -ne 3) {
        $binding.reasons = @("three-airframe 3-D control evidence requires exactly three files")
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $resolvedPaths = @()
    foreach ($requestedPath in @($EvidencePaths)) {
        if ([string]::IsNullOrWhiteSpace([string]$requestedPath)) {
            $binding.reasons += "three-airframe 3-D control evidence path is empty"
            continue
        }
        $candidate = if ([System.IO.Path]::IsPathRooted([string]$requestedPath)) {
            [string]$requestedPath
        }
        else {
            Join-Path $CoreRoot ([string]$requestedPath)
        }
        try {
            $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
        }
        catch {
            $binding.reasons += "three-airframe 3-D control evidence path cannot be resolved: $requestedPath"
            continue
        }
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            $binding.reasons += "three-airframe 3-D control evidence is not a file: $resolved"
            continue
        }
        $resolvedAbsolute = [System.IO.Path]::GetFullPath($resolved)
        if (@($resolvedPaths) -contains $resolvedAbsolute) {
            $binding.reasons += "three-airframe 3-D control evidence paths must be unique"
            continue
        }
        $resolvedPaths += $resolvedAbsolute
    }
    $binding.paths = @($resolvedPaths)
    if (@($binding.reasons).Count -gt 0) {
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }

    $verifyArgs = @(
        "--bind",
        "--expected-release-label", [string]$productVersion.release_label,
        "--manifest-directory", [System.IO.Path]::GetFullPath($ManifestDirectory)
    ) + @($resolvedPaths)
    $verificationOutput = (& $python $sim3dControlVerifier @verifyArgs 2>&1 | Out-String)
    $binding.verification_exit_code = Get-NativeExitCode
    try {
        $binding.verification = $verificationOutput | ConvertFrom-Json
    }
    catch {
        $binding.reasons += "three-airframe 3-D control verifier output is not valid JSON: $($_.Exception.Message)"
        if ($Required) {
            throw ($binding.reasons -join '; ')
        }
        return $binding
    }
    $binding.verified = $true
    if ($binding.verification.PSObject.Properties.Name -contains 'reasons' -and
            $null -ne $binding.verification.reasons) {
        $binding.reasons += @($binding.verification.reasons | ForEach-Object { [string]$_ })
    }
    if ([string]$binding.verification.event -ne "simulator_3d_control_binding_verified") {
        $binding.reasons += "three-airframe 3-D control verifier returned an unexpected event"
    }
    $binding.gate_passed = ($binding.verification_exit_code -eq 0) -and
        [bool]$binding.verification.gate_passed -and
        @($binding.reasons).Count -eq 0
    $binding.evidence = @(
        $resolvedPaths | ForEach-Object {
            [ordered]@{
                path = $_
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash.ToLowerInvariant()
                bytes = [int64](Get-Item -LiteralPath $_).Length
            }
        }
    )
    if ($Required -and -not [bool]$binding.gate_passed) {
        $reasonText = if (@($binding.reasons).Count -gt 0) {
            $binding.reasons -join '; '
        }
        else {
            "verifier exit code $($binding.verification_exit_code)"
        }
        throw "Three-airframe 3-D control evidence is not valid: $reasonText"
    }
    return $binding
}

function Get-GitProvenance {
    param([Parameter(Mandatory)] [string]$RepositoryRoot)

    # Keep the acceptance artifact tied to the source snapshot that produced
    # it.  A dirty worktree is allowed for prerelease software gates, but the
    # fact must be explicit and the status set must have a stable digest.  Do
    # not serialize raw paths here: they can contain operator/user names and
    # are not needed to reproduce the identity check.
    $gitAvailable = $false
    $headSha = $null
    $statusSha = $null
    $statusCount = 0
    $statusCommandSucceeded = $false
    try {
        $headOutput = @(& git.exe -C $RepositoryRoot rev-parse --verify HEAD 2>$null)
    $headExitCode = Get-NativeExitCode
        if ($headExitCode -eq 0 -and $headOutput.Count -gt 0) {
            $candidate = [string]$headOutput[0]
            if ($candidate -match '^[0-9a-fA-F]{40}$') {
                $headSha = $candidate.ToLowerInvariant()
                $gitAvailable = $true
            }
        }

        $statusOutput = @(& git.exe -C $RepositoryRoot status --porcelain=v1 --untracked-files=all 2>$null)
        $statusExitCode = Get-NativeExitCode
        if ($statusExitCode -eq 0) {
            $statusCommandSucceeded = $true
            $statusCount = $statusOutput.Count
            $statusText = if ($statusCount -gt 0) {
                ($statusOutput -join "`n") + "`n"
            } else {
                ""
            }
            $utf8 = New-Object System.Text.UTF8Encoding($false)
            $statusBytes = $utf8.GetBytes($statusText)
            $sha256 = [System.Security.Cryptography.SHA256]::Create()
            try {
                $statusSha = ([System.BitConverter]::ToString($sha256.ComputeHash($statusBytes))).Replace("-", "").ToLowerInvariant()
            }
            finally {
                $sha256.Dispose()
            }
        }
    }
    catch {
        # Provenance is descriptive.  A missing Git executable must not turn a
        # software/static gate into an unrelated runtime failure.
    }

    $worktreeClean = $gitAvailable -and $statusCommandSucceeded -and $statusCount -eq 0
    [ordered]@{
        git_available = $gitAvailable
        head_sha = $headSha
        dirty_entry_count = $statusCount
        status_sha256 = $statusSha
        worktree_clean = $worktreeClean
        snapshot_kind = if ($worktreeClean) { "clean_commit" } elseif ($gitAvailable -and $statusCommandSucceeded) { "dirty_worktree_snapshot" } else { "unknown" }
    }
}

$CoreRoot = (Resolve-Path -LiteralPath $CoreRoot).Path
$QgcRoot = (Resolve-Path -LiteralPath $QgcRoot).Path
$versionManifestPath = Join-Path $CoreRoot 'configs\product-version.json'
if (-not (Test-Path -LiteralPath $versionManifestPath -PathType Leaf)) {
    throw "Product version manifest is missing: $versionManifestPath"
}
try {
    $productVersion = Get-Content -LiteralPath $versionManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw "Product version manifest is not valid JSON: $versionManifestPath ($($_.Exception.Message))"
}
foreach ($field in @('release_label', 'numeric', 'channel')) {
    if (-not ($productVersion.PSObject.Properties.Name -contains $field) -or
            [string]::IsNullOrWhiteSpace([string]$productVersion.$field)) {
        throw "Product version manifest is missing a usable '$field': $versionManifestPath"
    }
}
if ([string]$productVersion.channel -notin @('prerelease', 'release')) {
    throw "Unsupported product version channel: $($productVersion.channel)"
}
if ([string]$productVersion.channel -eq 'prerelease' -and
        [string]$productVersion.release_label -notmatch '^\d+\.\d+\.\d+-pre\.[1-9]\d*$') {
    throw "Invalid prerelease release_label: $($productVersion.release_label)"
}
if ([string]$productVersion.channel -eq 'release' -and
        ([string]$productVersion.release_label -ne [string]$productVersion.numeric)) {
    throw "Release channel requires release_label to equal numeric: $($productVersion.release_label) vs $($productVersion.numeric)"
}
if ([string]::IsNullOrWhiteSpace($QgcBuildRoot)) {
    $QgcBuildRoot = Join-Path $CoreRoot ("build\windows-v{0}-release" -f $productVersion.release_label)
}
$QgcBuildRoot = (Resolve-Path -LiteralPath $QgcBuildRoot).Path
$python = Join-Path $QgcRoot ".venv/Scripts/python.exe"
$pytestTempRoot = Resolve-OutputArtifactPath `
    -Requested $TestTempRoot `
    -DefaultPath (Join-Path $CoreRoot ".tmp-pytest-goal")
if (Test-Path -LiteralPath $pytestTempRoot -PathType Leaf) {
    throw "Pytest temporary root is a file, expected a directory: $pytestTempRoot"
}
New-Item -ItemType Directory -Path $pytestTempRoot -Force | Out-Null
# Keep pytest/tmp_path and any subprocesses launched by the acceptance suite
# off a full system drive.  These are process-scoped environment variables;
# the parent PowerShell session is unchanged when this script exits.
$env:TEMP = $pytestTempRoot
$env:TMP = $pytestTempRoot
$portableRoot = Join-Path $QgcBuildRoot ("portable-v{0}" -f $productVersion.release_label)
$stagingBin = Join-Path $portableRoot "bin"
$qgcExe = Join-Path $stagingBin "MultiDetectGCS.exe"
$selfTest = Join-Path $QgcBuildRoot "Release/MultiDetectOperatorProtocolSelfTest.exe"
$model = Join-Path $CoreRoot "artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.onnx"
$modelDescriptor = Join-Path $CoreRoot "configs/models/ultralytics_yolo26n_coco80_trt86_raw.json"
$fieldLinkVerifier = Join-Path $CoreRoot "scripts/verify_field_link_preflight.py"
$replaySetReadinessScript = Join-Path $CoreRoot "scripts/check_replay_set_readiness.py"
$combinedVerifier = Join-Path $CoreRoot "scripts/verify_combined_sim_evidence.py"
$simulatorExternalVerifier = Join-Path $CoreRoot "scripts/verify_simulator_external_evidence.py"
$qgcSettings = Join-Path $env:APPDATA "QGroundControl/MultiDetectGCS Daily.ini"
$operatorController = Join-Path $QgcRoot "custom/src/MultiDetectOperatorController.cc"
$fixedWingAimControl = Join-Path $CoreRoot "src/multidetect/fixed_wing_aim_control.py"
$fixedWingAcceptanceScript = Join-Path $CoreRoot "scripts/run_fixed_wing_software_acceptance.py"
$fixedWingAcceptanceEvidence = Join-Path $CoreRoot "artifacts/evaluation/fixed-wing-software-acceptance-latest.json"
$actuatorSafetyScript = Join-Path $CoreRoot "scripts/run_actuator_safety_acceptance.py"
$actuatorSafetyVerifier = Join-Path $CoreRoot "scripts/verify_actuator_safety_evidence.py"
$actuatorSafetyEvidence = Join-Path $CoreRoot "artifacts/evaluation/actuator-safety-software-acceptance-latest.json"
$mode2PayloadAcceptanceScript = Join-Path $CoreRoot "scripts/run_mode2_payload_hil_acceptance.py"
$mode2PayloadAcceptanceEvidence = Join-Path $CoreRoot "artifacts/evaluation/mode2-payload-hil-acceptance-latest.json"
$mode3ApproachAcceptanceScript = Join-Path $CoreRoot "scripts/run_mode3_approach_hil_acceptance.py"
$mode3ApproachAcceptanceEvidence = Join-Path $CoreRoot "artifacts/evaluation/mode3-approach-hil-acceptance-latest.json"
$simSafetyScript = Join-Path $CoreRoot "scripts/run_simulator_safety_acceptance.py"
$simSafetyVerifier = Join-Path $CoreRoot "scripts/verify_simulator_safety_evidence.py"
$simSafetyEvidence = Join-Path $CoreRoot "artifacts/evaluation/simulator-safety-acceptance-latest.json"
$simScenarioMatrixScript = Join-Path $CoreRoot "scripts/run_simulator_scenario_matrix.py"
$simScenarioMatrixVerifier = Join-Path $CoreRoot "scripts/verify_simulator_scenario_matrix.py"
$simScenarioMatrixEvidence = Join-Path $CoreRoot "artifacts/evaluation/simulator-scenario-matrix-acceptance-latest.json"
$sim3dControlScript = Join-Path $CoreRoot "scripts/run_simulator_3d_control_acceptance.py"
$sim3dControlVerifier = Join-Path $CoreRoot "scripts/verify_simulator_3d_control_evidence.py"
$sim3dVtolManifest = Join-Path $CoreRoot "configs/scenarios/river_rescue_vtol_001.scenario.json"
$sim3dFixedWingManifest = Join-Path $CoreRoot "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json"
$sim3dMultirotorManifest = Join-Path $CoreRoot "configs/scenarios/urban_inspection_multirotor_001.scenario.json"
$sim3dVtolEvidence = Join-Path $CoreRoot "artifacts/evaluation/simulator-3d-control-acceptance-latest.json"
$sim3dFixedWingEvidence = Join-Path $CoreRoot "artifacts/evaluation/simulator-3d-control-acceptance-fixed-wing-latest.json"
$sim3dMultirotorEvidence = Join-Path $CoreRoot "artifacts/evaluation/simulator-3d-control-acceptance-multirotor-latest.json"
$px4AirframeTransactionScript = Join-Path $CoreRoot "scripts/run_px4_airframe_transaction_acceptance.py"
$px4AirframeTransactionVerifier = Join-Path $CoreRoot "scripts/verify_px4_airframe_transaction_acceptance.py"
$px4AirframeTransactionEvidence = Join-Path $CoreRoot "artifacts/evaluation/px4-airframe-mission-transaction-acceptance-latest.json"
$fixedWingAcceptanceEvidence = Resolve-OutputArtifactPath `
    -Requested $FixedWingAcceptanceOutput `
    -DefaultPath $fixedWingAcceptanceEvidence
$actuatorSafetyEvidence = Resolve-OutputArtifactPath `
    -Requested $ActuatorSafetyOutput `
    -DefaultPath $actuatorSafetyEvidence
$mode2PayloadAcceptanceEvidence = Resolve-OutputArtifactPath `
    -Requested $Mode2PayloadAcceptanceOutput `
    -DefaultPath $mode2PayloadAcceptanceEvidence
$mode3ApproachAcceptanceEvidence = Resolve-OutputArtifactPath `
    -Requested $Mode3ApproachAcceptanceOutput `
    -DefaultPath $mode3ApproachAcceptanceEvidence
$simSafetyEvidence = Resolve-OutputArtifactPath `
    -Requested $SimulatorSafetyOutput `
    -DefaultPath $simSafetyEvidence
$simScenarioMatrixEvidence = Resolve-OutputArtifactPath `
    -Requested $SimulatorScenarioMatrixOutput `
    -DefaultPath $simScenarioMatrixEvidence
$sim3dVtolEvidence = Resolve-OutputArtifactPath `
    -Requested $Simulator3dVtolOutput `
    -DefaultPath $sim3dVtolEvidence
$sim3dFixedWingEvidence = Resolve-OutputArtifactPath `
    -Requested $Simulator3dFixedWingOutput `
    -DefaultPath $sim3dFixedWingEvidence
$sim3dMultirotorEvidence = Resolve-OutputArtifactPath `
    -Requested $Simulator3dMultirotorOutput `
    -DefaultPath $sim3dMultirotorEvidence
$px4AirframeTransactionEvidence = Resolve-OutputArtifactPath `
    -Requested $Px4AirframeTransactionOutput `
    -DefaultPath $px4AirframeTransactionEvidence
$simulatorExternalRevalidationEvidence = Resolve-OutputArtifactPath `
    -Requested $SimulatorExternalRevalidationOutput `
    -DefaultPath (Join-Path $CoreRoot "artifacts/evaluation/simulator-external-runtime-ai-revalidation-acceptance-latest.json")
# Keep the printable board in the tracked configuration tree.  The artifacts/
# tree is intentionally ignored, so using it here made a fresh clone fail the
# acceptance preflight before a field operator could even print the board.
$calibrationBoard = Join-Path $CoreRoot "configs/calibration/charuco-7x5-40mm-20mm-4200px.png"
$calibrationCollector = Join-Path $CoreRoot "scripts/capture_charuco_calibration.py"

foreach ($required in @($python, $qgcExe, $selfTest, $model, $modelDescriptor, $fieldLinkVerifier, $replaySetReadinessScript, $combinedVerifier, $simulatorExternalVerifier, $sim3dControlScript, $sim3dControlVerifier, $sim3dVtolManifest, $sim3dFixedWingManifest, $sim3dMultirotorManifest, $operatorController, $fixedWingAimControl, $fixedWingAcceptanceScript, $actuatorSafetyScript, $actuatorSafetyVerifier, $mode2PayloadAcceptanceScript, $mode3ApproachAcceptanceScript, $simSafetyScript, $simSafetyVerifier, $simScenarioMatrixScript, $simScenarioMatrixVerifier, $px4AirframeTransactionScript, $px4AirframeTransactionVerifier, $calibrationBoard, $calibrationCollector)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required acceptance artifact is missing: $required"
    }
}

$fieldLinkBinding = Get-FieldLinkPreflightBinding `
    -ReportPath $FieldLinkPreflight `
    -Required ([bool]$RequireFieldLinkPreflight)
$replaySetBinding = Get-ReplaySetReadinessBinding `
    -ManifestPath $ReplaySetManifest `
    -AccuracyReportPath $ReplaySetAccuracyReport `
    -ReadinessOutputPath $ReplaySetReadinessOutput `
    -Required ([bool]$RequireReplaySet)
$combinedSimBinding = Get-CombinedSimBinding `
    -EvidencePath $CombinedSimEvidence `
    -ReleaseModeEvidencePath $CombinedSimReleaseModeEvidence `
    -Required ([bool]$RequireCombinedSimEvidence)
$simulatorExternalBinding = Get-SimulatorExternalEvidenceBinding `
    -JoinPath $SimulatorExternalEvidence `
    -RevalidationPath $simulatorExternalRevalidationEvidence `
    -Required ([bool]$RequireSimulatorExternalEvidence)

Invoke-Checked $python @("-m", "ruff", "check", "src", "tests", "scripts") $CoreRoot
Invoke-Checked $python @("-m", "pytest", "-q", "--basetemp", $pytestTempRoot) $CoreRoot
Invoke-Checked $python @("-m", "pytest", "-q", "--basetemp", $pytestTempRoot, "tests/test_qgc_native_flyview_style.py") $CoreRoot
Invoke-Checked $python @("-m", "pytest", "-q", "--basetemp", $pytestTempRoot, "tools/tests/test_multidetect_custom_app.py") $QgcRoot
Push-Location $QgcRoot
try {
    $qgcCustomCollection = & $python -m pytest --collect-only -q --basetemp $pytestTempRoot tools/tests/test_multidetect_custom_app.py
    $qgcCustomCollectionExitCode = Get-NativeExitCode
    if ($qgcCustomCollectionExitCode -ne 0) {
        throw "QGC custom test collection failed with exit code $qgcCustomCollectionExitCode"
    }
}
finally {
    Pop-Location
}
$qgcCustomTests = @($qgcCustomCollection | Where-Object { $_ -match "::" }).Count
if ($qgcCustomTests -le 0) {
    throw "QGC custom test collection returned no tests"
}
$fixedWingAcceptanceOutput = & $python $fixedWingAcceptanceScript "--out" $fixedWingAcceptanceEvidence
$fixedWingAcceptanceExitCode = Get-NativeExitCode
if ($fixedWingAcceptanceExitCode -ne 0) {
    throw "Fixed-wing software acceptance failed with exit code $fixedWingAcceptanceExitCode"
}
if (-not (Test-Path -LiteralPath $fixedWingAcceptanceEvidence -PathType Leaf)) {
    throw "Fixed-wing software acceptance did not produce evidence: $fixedWingAcceptanceEvidence"
}
$fixedWingAcceptance = Get-Content -Raw -LiteralPath $fixedWingAcceptanceEvidence | ConvertFrom-Json
if ([string]$fixedWingAcceptance.event -ne "fixed_wing_software_acceptance_passed") {
    throw "Fixed-wing software acceptance returned an unexpected event: $($fixedWingAcceptance.event)"
}
if ([string]$fixedWingAcceptance.release_label -ne [string]$productVersion.release_label) {
    throw "Fixed-wing software acceptance release label does not match product manifest"
}
if ([string]$fixedWingAcceptance.runtime_mode -ne "HIL" -or
        [bool]$fixedWingAcceptance.software_only -ne $true) {
    throw "Fixed-wing software acceptance must remain software-only HIL evidence"
}
$actuatorSafetyOutputText = (& $python $actuatorSafetyScript "--out" $actuatorSafetyEvidence 2>&1 | Out-String)
$actuatorSafetyRunExitCode = Get-NativeExitCode
if ($actuatorSafetyRunExitCode -ne 0) {
    throw "Actuator safety software acceptance failed with exit code ${actuatorSafetyRunExitCode}: $actuatorSafetyOutputText"
}
if (-not (Test-Path -LiteralPath $actuatorSafetyEvidence -PathType Leaf)) {
    throw "Actuator safety software acceptance did not produce evidence: $actuatorSafetyEvidence"
}
$actuatorSafetyVerificationOutput = (& $python $actuatorSafetyVerifier $actuatorSafetyEvidence "--expected-release-label" $productVersion.release_label 2>&1 | Out-String)
$actuatorSafetyVerificationExitCode = Get-NativeExitCode
try {
    $actuatorSafetyVerification = $actuatorSafetyVerificationOutput | ConvertFrom-Json
}
catch {
    throw "Actuator safety verifier output is not valid JSON: $($_.Exception.Message)"
}
if ($actuatorSafetyVerificationExitCode -ne 0 -or [bool]$actuatorSafetyVerification.gate_passed -ne $true) {
    throw "Actuator safety evidence verifier did not pass: $actuatorSafetyVerificationOutput"
}
$actuatorSafetyEvidenceObject = Get-Content -Raw -LiteralPath $actuatorSafetyEvidence | ConvertFrom-Json
if ([string]$actuatorSafetyEvidenceObject.event -ne "actuator_safety_software_acceptance_finished" -or
        [string]$actuatorSafetyEvidenceObject.runtime_mode -ne "HIL" -or
        [bool]$actuatorSafetyEvidenceObject.software_only -ne $true -or
        [bool]$actuatorSafetyEvidenceObject.physical_outputs_present -ne $false) {
    throw "Actuator safety evidence must remain software-only HIL evidence"
}
$mode2PayloadAcceptanceOutputText = (& $python $mode2PayloadAcceptanceScript `
    "--project-root" $CoreRoot `
    "--out" $mode2PayloadAcceptanceEvidence 2>&1 | Out-String)
$mode2PayloadAcceptanceRunExitCode = Get-NativeExitCode
if ($mode2PayloadAcceptanceRunExitCode -ne 0) {
    throw "Mode-2 payload HIL acceptance failed with exit code ${mode2PayloadAcceptanceRunExitCode}: $mode2PayloadAcceptanceOutputText"
}
if (-not (Test-Path -LiteralPath $mode2PayloadAcceptanceEvidence -PathType Leaf)) {
    throw "Mode-2 payload HIL acceptance did not produce evidence: $mode2PayloadAcceptanceEvidence"
}
$mode2PayloadAcceptance = Get-Content -Raw -LiteralPath $mode2PayloadAcceptanceEvidence | ConvertFrom-Json
if ([string]$mode2PayloadAcceptance.event -ne "mode2_payload_hil_acceptance_passed" -or
        [bool]$mode2PayloadAcceptance.flight_control_enabled -ne $false -or
        [bool]$mode2PayloadAcceptance.physical_release_enabled -ne $false -or
        [bool]$mode2PayloadAcceptance.real_payload_interface_present -ne $false) {
    throw "Mode-2 payload HIL contract did not remain advisory/software-only"
}
$mode3ApproachAcceptanceOutputText = (& $python $mode3ApproachAcceptanceScript `
    "--out" $mode3ApproachAcceptanceEvidence 2>&1 | Out-String)
$mode3ApproachAcceptanceRunExitCode = Get-NativeExitCode
if ($mode3ApproachAcceptanceRunExitCode -ne 0) {
    throw "Mode-3 approach HIL acceptance failed with exit code ${mode3ApproachAcceptanceRunExitCode}: $mode3ApproachAcceptanceOutputText"
}
if (-not (Test-Path -LiteralPath $mode3ApproachAcceptanceEvidence -PathType Leaf)) {
    throw "Mode-3 approach HIL acceptance did not produce evidence: $mode3ApproachAcceptanceEvidence"
}
$mode3ApproachAcceptance = Get-Content -Raw -LiteralPath $mode3ApproachAcceptanceEvidence | ConvertFrom-Json
if ([string]$mode3ApproachAcceptance.event -ne "mode3_approach_hil_acceptance_passed" -or
        [bool]$mode3ApproachAcceptance.advisory_only -ne $true -or
        [bool]$mode3ApproachAcceptance.flight_control_enabled -ne $false -or
        [bool]$mode3ApproachAcceptance.physical_release_enabled -ne $false -or
        [bool]$mode3ApproachAcceptance.real_actuator_interface_present -ne $false) {
    throw "Mode-3 approach HIL contract did not remain advisory/software-only"
}
$simSafetyOutput = (& $python $simSafetyScript "--out" $simSafetyEvidence 2>&1 | Out-String)
$simSafetyRunExitCode = Get-NativeExitCode
if ($simSafetyRunExitCode -ne 0) {
    throw "SIM safety acceptance failed with exit code ${simSafetyRunExitCode}: $simSafetyOutput"
}
if (-not (Test-Path -LiteralPath $simSafetyEvidence -PathType Leaf)) {
    throw "SIM safety acceptance did not produce evidence: $simSafetyEvidence"
}
$simSafetyVerificationOutput = (& $python $simSafetyVerifier $simSafetyEvidence 2>&1 | Out-String)
$simSafetyVerificationExitCode = Get-NativeExitCode
try {
    $simSafetyVerification = $simSafetyVerificationOutput | ConvertFrom-Json
}
catch {
    throw "SIM safety verifier output is not valid JSON: $($_.Exception.Message)"
}
if ($simSafetyVerificationExitCode -ne 0 -or [bool]$simSafetyVerification.gate_passed -ne $true) {
    throw "SIM safety evidence verifier did not pass: $simSafetyVerificationOutput"
}
# The acceptance run must exercise every declared deterministic fault window,
# not merely the first nominal seconds of each scenario.  The matrix runner
# auto-extends the shared frame budget through the latest event and keeps the
# result fail-closed if any event is never observed.
$simScenarioMatrixOutputText = (& $python $simScenarioMatrixScript "--cover-faults" "--fault-tail" "1.0" "--out" $simScenarioMatrixEvidence 2>&1 | Out-String)
$simScenarioMatrixRunExitCode = Get-NativeExitCode
if ($simScenarioMatrixRunExitCode -ne 0) {
    throw "SIM scenario matrix acceptance failed with exit code ${simScenarioMatrixRunExitCode}: $simScenarioMatrixOutputText"
}
if (-not (Test-Path -LiteralPath $simScenarioMatrixEvidence -PathType Leaf)) {
    throw "SIM scenario matrix acceptance did not produce evidence: $simScenarioMatrixEvidence"
}
$simScenarioMatrixVerificationOutput = (& $python $simScenarioMatrixVerifier $simScenarioMatrixEvidence "--expected-release-label" $productVersion.release_label 2>&1 | Out-String)
$simScenarioMatrixVerificationExitCode = Get-NativeExitCode
try {
    $simScenarioMatrixVerification = $simScenarioMatrixVerificationOutput | ConvertFrom-Json
}
catch {
    throw "SIM scenario matrix verifier output is not valid JSON: $($_.Exception.Message)"
}
if ($simScenarioMatrixVerificationExitCode -ne 0 -or [bool]$simScenarioMatrixVerification.gate_passed -ne $true) {
    throw "SIM scenario matrix evidence verifier did not pass: $simScenarioMatrixVerificationOutput"
}
$simScenarioMatrixEvidenceObject = Get-Content -Raw -LiteralPath $simScenarioMatrixEvidence | ConvertFrom-Json
if ([bool]$simScenarioMatrixEvidenceObject.fault_coverage_required -ne $true -or
        [bool]$simScenarioMatrixEvidenceObject.fault_coverage_auto_extended -ne $true) {
    throw "SIM scenario matrix acceptance did not require auto-extended fault coverage"
}
$px4AirframeTransactionRunOutput = (& $python $px4AirframeTransactionScript "--out" $px4AirframeTransactionEvidence 2>&1 | Out-String)
$px4AirframeTransactionRunExitCode = Get-NativeExitCode
if ($px4AirframeTransactionRunExitCode -ne 0) {
    throw "PX4 three-airframe transaction acceptance failed with exit code ${px4AirframeTransactionRunExitCode}: $px4AirframeTransactionRunOutput"
}
if (-not (Test-Path -LiteralPath $px4AirframeTransactionEvidence -PathType Leaf)) {
    throw "PX4 three-airframe transaction acceptance did not produce evidence: $px4AirframeTransactionEvidence"
}
$px4AirframeTransactionVerificationOutput = (& $python $px4AirframeTransactionVerifier $px4AirframeTransactionEvidence 2>&1 | Out-String)
$px4AirframeTransactionVerificationExitCode = Get-NativeExitCode
try {
    $px4AirframeTransactionVerification = $px4AirframeTransactionVerificationOutput | ConvertFrom-Json
}
catch {
    throw "PX4 three-airframe transaction verifier output is not valid JSON: $($_.Exception.Message)"
}
if ($px4AirframeTransactionVerificationExitCode -ne 0 -or [bool]$px4AirframeTransactionVerification.gate_passed -ne $true) {
    throw "PX4 three-airframe transaction evidence verifier did not pass: $px4AirframeTransactionVerificationOutput"
}
$sim3dRuns = @(
    [ordered]@{ name = "vtol"; manifest = $sim3dVtolManifest; output = $sim3dVtolEvidence },
    [ordered]@{ name = "fixed-wing"; manifest = $sim3dFixedWingManifest; output = $sim3dFixedWingEvidence },
    [ordered]@{ name = "multirotor"; manifest = $sim3dMultirotorManifest; output = $sim3dMultirotorEvidence }
)
foreach ($sim3dRun in $sim3dRuns) {
    $sim3dRunOutput = (& $python $sim3dControlScript `
        "--scenario-manifest" $sim3dRun.manifest `
        "--out" $sim3dRun.output 2>&1 | Out-String)
    $sim3dRunExitCode = Get-NativeExitCode
    if ($sim3dRunExitCode -ne 0) {
        throw "SIM 3-D $($sim3dRun.name) control acceptance failed with exit code ${sim3dRunExitCode}: $sim3dRunOutput"
    }
    if (-not (Test-Path -LiteralPath $sim3dRun.output -PathType Leaf)) {
        throw "SIM 3-D $($sim3dRun.name) control acceptance did not produce evidence: $($sim3dRun.output)"
    }
    $sim3dRunEvidence = Get-Content -Raw -LiteralPath $sim3dRun.output | ConvertFrom-Json
    if ([string]$sim3dRunEvidence.product -ne "Multi-Detect" -or
            [string]$sim3dRunEvidence.release_label -ne [string]$productVersion.release_label -or
            [string]$sim3dRunEvidence.runtime_mode -ne "sim" -or
            [bool]$sim3dRunEvidence.gate_passed -ne $true -or
            [bool]$sim3dRunEvidence.deterministic_gate_passed -ne $true -or
            [bool]$sim3dRunEvidence.external_runtime_gate_passed -ne $false) {
        throw "SIM 3-D $($sim3dRun.name) control evidence failed its deterministic or claim boundary"
    }
}
$sim3dBinding = Get-Simulator3dControlBinding `
    -EvidencePaths @($sim3dVtolEvidence, $sim3dFixedWingEvidence, $sim3dMultirotorEvidence) `
    -ManifestDirectory ([System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($EvidencePath))) `
    -Required ([bool]$RequireSimulator3dControlEvidence)
$collection = & $python -m pytest --collect-only -q --basetemp $pytestTempRoot
$coreCollectionExitCode = Get-NativeExitCode
if ($coreCollectionExitCode -ne 0) {
    throw "Core test collection failed with exit code $coreCollectionExitCode"
}
$coreTestsCollected = 0
foreach ($line in $collection) {
    if ($line -match ":\s*(\d+)\s*$") {
        $coreTestsCollected += [int]$Matches[1]
    }
}
if ($coreTestsCollected -le 0) {
    throw "Core test collection returned no tests"
}
# The repository intentionally keeps Windows-native CRLF files.  Tell Git to
# ignore the CR byte when checking for real trailing whitespace so this gate
# does not reject every unchanged CRLF line in the worktree.
Invoke-Checked "git.exe" @("-c", "core.whitespace=cr-at-eol", "diff", "--check") $CoreRoot
Invoke-Checked "git.exe" @("-c", "core.whitespace=cr-at-eol", "diff", "--check") $QgcRoot

$qtMatch = Select-String -LiteralPath (Join-Path $QgcBuildRoot "CMakeCache.txt") -Pattern "^Qt6_DIR:PATH=(.+)/lib/cmake/Qt6$" | Select-Object -First 1
if ($null -eq $qtMatch) {
    throw "Qt6_DIR is missing from the QGC CMake cache"
}
$cachePath = Join-Path $QgcBuildRoot 'CMakeCache.txt'
$cacheVersionMatch = Select-String -LiteralPath $cachePath -Pattern '^QGC_APP_VERSION_OVERRIDE:STRING=(.+)$' | Select-Object -First 1
if ($null -ne $cacheVersionMatch -and $cacheVersionMatch.Matches[0].Groups[1].Value -cne [string]$productVersion.numeric) {
    throw "QGC CMake cache numeric version does not match product manifest: $($cacheVersionMatch.Matches[0].Groups[1].Value) vs $($productVersion.numeric)"
}
$cacheReleaseLabelMatch = Select-String -LiteralPath $cachePath -Pattern '^QGC_APP_RELEASE_LABEL:STRING=(.+)$' | Select-Object -First 1
if ($null -ne $cacheReleaseLabelMatch -and $cacheReleaseLabelMatch.Matches[0].Groups[1].Value -cne [string]$productVersion.release_label) {
    throw "QGC CMake cache release label does not match product manifest: $($cacheReleaseLabelMatch.Matches[0].Groups[1].Value) vs $($productVersion.release_label)"
}
$qtBin = Join-Path $qtMatch.Matches[0].Groups[1].Value "bin"
$qmlCacheGen = Join-Path $qtBin "qmlcachegen.exe"
if (-not (Test-Path -LiteralPath $qmlCacheGen -PathType Leaf)) {
    throw "Qt QML cache compiler is missing: $qmlCacheGen"
}
foreach ($qml in @(
    "custom/src/FlyViewVideo.qml",
    "custom/src/FlyViewCustomLayer.qml",
    "custom/src/MultiDetectConfigure.qml",
    "custom/src/SelectViewDropdown.qml",
    "custom/res/Custom/MultiDetect/MultiDetectState.qml",
    "custom/res/Custom/MultiDetect/MultiDetectVideoOverlay.qml"
)) {
    # qmlformat tries to resolve every imported QGC type and returns failure
    # when run outside the generated QML module graph. qmlcachegen --bare is
    # the same syntax parser used by the build and keeps this gate independent
    # of the host's QML import search path.
    Invoke-Checked $qmlCacheGen @("--bare", (Join-Path $QgcRoot $qml)) $QgcRoot -DiscardOutput
}

$oldPath = $env:PATH
try {
    $env:PATH = "$stagingBin;$oldPath"
    Invoke-Checked $selfTest @() $QgcRoot
}
finally {
    $env:PATH = $oldPath
}

$descriptor = Get-Content -Raw -LiteralPath $modelDescriptor | ConvertFrom-Json
$modelHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $model).Hash.ToLowerInvariant()
if ($modelHash -ne [string]$descriptor.artifact_sha256) {
    throw "Raw COCO model hash does not match its deployment descriptor"
}

$settingsText = if (Test-Path -LiteralPath $qgcSettings) {
    Get-Content -Raw -LiteralPath $qgcSettings
} else {
    ""
}
$qgcDefaults = [ordered]@{
    gr01_host = $settingsText -match "(?m)^Link0\\host=192\.168\.144\.11\r?$"
    gr01_port = $settingsText -match "(?m)^Link0\\port=5760\r?$"
    gr01_auto = $settingsText -match "(?m)^Link0\\auto=true\r?$"
    camera_rtsp = $settingsText -match '(?m)^rtspUrl="rtsp://192\.168\.144\.108:554/stream=0"\r?$'
    camera_source = $settingsText -match "(?m)^videoSource=RTSP Video Stream\r?$"
}
if ($qgcDefaults.Values -contains $false) {
    throw "QGC zero-configuration GR01/RTSP settings are incomplete"
}

$operatorControllerText = Get-Content -Raw -LiteralPath $operatorController
$operatorMetadataDefaults = [ordered]@{
    jetson_host = $operatorControllerText -match 'environmentValue\("MULTIDETECT_OPERATOR_UDP_HOST",\s*QStringLiteral\("192\.168\.144\.20"\)\)'
    jetson_port = $operatorControllerText -match 'environmentInteger\("MULTIDETECT_OPERATOR_UDP_PORT",\s*14580\)'
    qgc_local_port = $operatorControllerText -match 'environmentInteger\("MULTIDETECT_OPERATOR_UDP_LOCAL_PORT",\s*14581\)'
    strict_signing = $operatorControllerText -match 'MAVLinkSigning::UnsignedAcceptancePolicy::Strict'
}
if ($operatorMetadataDefaults.Values -contains $false) {
    throw "QGC zero-configuration signed Jetson metadata defaults are incomplete"
}

$operatorKey = [Environment]::GetEnvironmentVariable("MULTIDETECT_OPERATOR_KEY", "User")
$operatorMavlinkKey = [Environment]::GetEnvironmentVariable("MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX", "User")
$operatorMavlinkKeyTrimmed = if ($null -eq $operatorMavlinkKey) { "" } else { $operatorMavlinkKey.Trim() }
$operatorCredentialReadiness = [ordered]@{
    operator_key_present = -not [string]::IsNullOrWhiteSpace($operatorKey)
    mavlink_key_hex_present = -not [string]::IsNullOrWhiteSpace($operatorMavlinkKey)
    mavlink_key_hex_length_valid = $operatorMavlinkKeyTrimmed.Length -eq 64
    mavlink_key_hex_format_valid = $operatorMavlinkKeyTrimmed -match '^[0-9A-Fa-f]{64}$'
}
$operatorCredentialsReady = -not ($operatorCredentialReadiness.Values -contains $false)
if (-not $operatorCredentialsReady -and -not $AllowMissingOperatorCredentials) {
    throw "Windows user-scoped operator credentials are incomplete"
}

$physicalEthernetProfiles = @(
    Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceDescription -notmatch "Wireless" -and $_.Name -notmatch "WLAN|Wi-Fi" } |
        ForEach-Object {
            $adapter = $_
            $ipv4 = @(
                Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                    Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.254\.' } |
                    Select-Object -ExpandProperty IPAddress
            )
            [pscustomobject]@{
                adapter = $adapter
                ipv4 = $ipv4
                is_up = [string]$adapter.Status -eq "Up"
                aircraft_subnet = [bool]($ipv4 | Where-Object { $_ -match '^192\.168\.144\.' })
            }
        }
)
# Prefer the aircraft subnet, then an active physical adapter, and only then a
# disconnected/unknown adapter.  The old first-item selection picked the
# disconnected VeryKuai TAP device on migrated workstations even when the
# actual Ethernet NIC was up.
$selectedEthernetProfile = $physicalEthernetProfiles |
    Sort-Object `
        @{ Expression = { if ($_.aircraft_subnet) { 0 } elseif ($_.is_up) { 1 } else { 2 } } }, `
        @{ Expression = { $_.adapter.Name } } |
    Select-Object -First 1
$physicalEthernet = if ($null -ne $selectedEthernetProfile) { $selectedEthernetProfile.adapter } else { $null }
$physicalEthernetIpv4 = if ($null -ne $selectedEthernetProfile) { @($selectedEthernetProfile.ipv4) } else { @() }
$aircraftSubnetMatch = if ($null -ne $selectedEthernetProfile) { [bool]$selectedEthernetProfile.aircraft_subnet } else { $false }
$jetsonReachable = Test-IcmpReachable -ComputerName "192.168.144.20"
$cameraReachable = Test-IcmpReachable -ComputerName "192.168.144.108"
$networkReady = if ([bool]$fieldLinkBinding.provided) {
    [bool]$fieldLinkBinding.gate_passed -and [bool]$fieldLinkBinding.hardware_link_ready
}
else {
    [bool]($jetsonReachable -and $cameraReachable)
}
$gitProvenance = Get-GitProvenance -RepositoryRoot $CoreRoot
$remainingLiveGates = @(
    if (-not $operatorCredentialsReady) {
        "Provision Windows user-scoped MULTIDETECT_OPERATOR_KEY and MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX with scripts/provision_multidetect_operator_keys.ps1 before signed live control"
    }
    if (-not [bool]$replaySetBinding.gate_passed) {
        "Provide a complete six-stratum deployment replay-set with reviewed truth/predictions and an accuracy report"
    }
    "Present the generated ChArUco board in 20 or more diverse camera views and generate the qualified calibration file"
    "Verify servo direction, limits, cancel and lost-lock return with propulsion disconnected"
    "Exercise DET to TRK to LCK to TGT with real detections"
    "Run the Jetson soak and record latency, temperature, RSS and reconnect evidence"
    if ([bool]$simulatorExternalBinding.gate_passed) {
        # The hash-bound Docker/PX4/Gazebo runtime and live-AI machine gate is
        # already closed in this snapshot.  Keep the remaining item precise:
        # human Fly View confirmation is still a separate visual gate.
        "Complete manual QGC Fly View visual acceptance for the three external PX4/Gazebo scenarios"
    }
    else {
        "Run external PX4/Gazebo 3-D runtime for VTOL, fixed-wing and multirotor and complete manual QGC Fly View acceptance"
    }
)

$evidence = [ordered]@{
    schema_version = 1
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    product_version = [ordered]@{
        manifest = $versionManifestPath
        release_label = [string]$productVersion.release_label
        numeric = [string]$productVersion.numeric
        channel = [string]$productVersion.channel
        qgc_build_root = $QgcBuildRoot
        portable_root = $portableRoot
        pytest_temp_root = $pytestTempRoot
    }
    git_provenance = $gitProvenance
    software_status = if ($operatorCredentialsReady) { "pass" } else { "pass_with_environment_gap" }
    hardware_status = if ($networkReady) { "online_control_dynamic_pending" } else { "offline_pending" }
    core_tests_collected = $coreTestsCollected
    qgc_custom_tests = $qgcCustomTests
    qgc_protocol_self_test = "pass"
    qml_parse = "pass"
    qgc_native_style_contract = [ordered]@{
        static_tests = "pass"
        surface = "native_qgc_flyview"
        second_window = $false
        second_video_surface = $false
        private_theme = $false
        computer_use_operator_authorized = [bool]$ComputerUseOperatorAuthorized
        computer_use_tool_exposed = $false
        operator_visual_acceptance = "not_claimed"
    }
    git_diff_check = "pass"
    fixed_wing_software_acceptance = [ordered]@{
        path = $fixedWingAcceptanceEvidence
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $fixedWingAcceptanceEvidence).Hash
        event = [string]$fixedWingAcceptance.event
        runtime_mode = [string]$fixedWingAcceptance.runtime_mode
        software_only = [bool]$fixedWingAcceptance.software_only
        state_path = @($fixedWingAcceptance.state_path)
        ui_contract = $fixedWingAcceptance.ui_contract
        physical_actuators = $fixedWingAcceptance.physical_actuators
    }
    actuator_safety_acceptance = [ordered]@{
        path = $actuatorSafetyEvidence
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $actuatorSafetyEvidence).Hash.ToLowerInvariant()
        event = [string]$actuatorSafetyEvidenceObject.event
        runtime_mode = [string]$actuatorSafetyEvidenceObject.runtime_mode
        software_only = [bool]$actuatorSafetyEvidenceObject.software_only
        physical_outputs_present = [bool]$actuatorSafetyEvidenceObject.physical_outputs_present
        propulsion_disconnected = [string]$actuatorSafetyEvidenceObject.propulsion_disconnected
        gate_passed = [bool]$actuatorSafetyVerification.gate_passed
        verifier_exit_code = $actuatorSafetyVerificationExitCode
        checks = $actuatorSafetyVerification.checks
    }
    hil_contract_acceptance = [ordered]@{
        runtime_mode = "HIL"
        software_only = $true
        all_contracts_passed = $true
        mode2_payload = [ordered]@{
            path = $mode2PayloadAcceptanceEvidence
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $mode2PayloadAcceptanceEvidence).Hash.ToLowerInvariant()
            event = [string]$mode2PayloadAcceptance.event
            advisory_only = $true
            flight_control_enabled = [bool]$mode2PayloadAcceptance.flight_control_enabled
            physical_release_enabled = [bool]$mode2PayloadAcceptance.physical_release_enabled
            real_payload_interface_present = [bool]$mode2PayloadAcceptance.real_payload_interface_present
        }
        mode3_approach = [ordered]@{
            path = $mode3ApproachAcceptanceEvidence
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $mode3ApproachAcceptanceEvidence).Hash.ToLowerInvariant()
            event = [string]$mode3ApproachAcceptance.event
            advisory_only = [bool]$mode3ApproachAcceptance.advisory_only
            flight_control_enabled = [bool]$mode3ApproachAcceptance.flight_control_enabled
            physical_release_enabled = [bool]$mode3ApproachAcceptance.physical_release_enabled
            real_actuator_interface_present = [bool]$mode3ApproachAcceptance.real_actuator_interface_present
        }
    }
    simulator_safety_acceptance = [ordered]@{
        path = $simSafetyEvidence
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $simSafetyEvidence).Hash.ToLowerInvariant()
        event = "simulator_safety_acceptance_finished"
        runtime_mode = "sim"
        gate_passed = [bool]$simSafetyVerification.gate_passed
        verifier_exit_code = $simSafetyVerificationExitCode
        checks = $simSafetyVerification.checks
        hardware_acceptance = "not_claimed"
        operator_visual_acceptance = "not_claimed"
    }
    simulator_scenario_matrix_acceptance = [ordered]@{
        path = $simScenarioMatrixEvidence
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $simScenarioMatrixEvidence).Hash.ToLowerInvariant()
        event = "simulator_scenario_matrix_finished"
        verifier_event = [string]$simScenarioMatrixVerification.event
        schema_version = [int]$simScenarioMatrixVerification.schema_version
        release_label = [string]$productVersion.release_label
        gate_passed = [bool]$simScenarioMatrixVerification.gate_passed
        verifier_exit_code = $simScenarioMatrixVerificationExitCode
        scenario_count = [int]$simScenarioMatrixVerification.scenario_count
        modes = @($simScenarioMatrixVerification.modes)
        claims = $simScenarioMatrixVerification.claims
        checks = $simScenarioMatrixVerification.checks
        requested_frames = [int]$simScenarioMatrixEvidenceObject.requested_frames
        requested_frames_before_fault_coverage = [int]$simScenarioMatrixEvidenceObject.requested_frames_before_fault_coverage
        fault_coverage_required = [bool]$simScenarioMatrixEvidenceObject.fault_coverage_required
        fault_coverage_auto_extended = [bool]$simScenarioMatrixEvidenceObject.fault_coverage_auto_extended
        fault_coverage_tail_s = [double]$simScenarioMatrixEvidenceObject.fault_coverage_tail_s
        fault_coverage = @($simScenarioMatrixEvidenceObject.scenarios | ForEach-Object { $_.fault_coverage })
        truth_loaded = $false
        truth_free = $true
        external_3d_runtime = "not_claimed"
        qgc_visual_acceptance = "not_claimed"
        hardware_acceptance = "not_claimed"
    }
    px4_airframe_transaction_acceptance = [ordered]@{
        path = $px4AirframeTransactionEvidence
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $px4AirframeTransactionEvidence).Hash.ToLowerInvariant()
        verifier_exit_code = $px4AirframeTransactionVerificationExitCode
        gate_passed = [bool]$px4AirframeTransactionVerification.gate_passed
        checks = $px4AirframeTransactionVerification.checks
        vehicle_classes = @($px4AirframeTransactionVerification.entries | ForEach-Object { [string]$_.vehicle_class })
        runtime_mode = "SIM"
        physical_output_enabled = $false
    }
    simulator_combined_acceptance = $combinedSimBinding
    simulator_external_evidence = $simulatorExternalBinding
    simulator_3d_control_acceptance = $sim3dBinding
    fixed_camera = $true
    gimbal_input_path = $false
    fixed_wing_aim_control = [ordered]@{
        implemented = $true
        mavlink_set_attitude_target = $true
        px4_offboard_prestream = $true
        lost_lock_return_mode = "entry_mode_or_AUTO"
        physical_release = $false
        source_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $fixedWingAimControl).Hash.ToLowerInvariant()
    }
    mode3_operator_status = [ordered]@{
        qgc_signed_selection_and_confirmation = $true
        jetson_single_pixhawk_writer = $true
        production_phase_names = @("CENTERING", "AIMING", "ABORT")
        flight_control_authority_flag = $true
    }
    camera_calibration_capture = [ordered]@{
        board_path = $calibrationBoard
        board_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $calibrationBoard).Hash.ToLowerInvariant()
        collector_path = $calibrationCollector
        collector_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $calibrationCollector).Hash.ToLowerInvariant()
        current_scene_board_detected = $false
    }
    qgc_defaults = $qgcDefaults
    operator_metadata_defaults = $operatorMetadataDefaults
    field_link_preflight = $fieldLinkBinding
    replay_set_readiness = $replaySetBinding
    operator_credential_readiness = [ordered]@{
        operator_key_present = [bool]$operatorCredentialReadiness.operator_key_present
        mavlink_key_hex_present = [bool]$operatorCredentialReadiness.mavlink_key_hex_present
        mavlink_key_hex_length_valid = [bool]$operatorCredentialReadiness.mavlink_key_hex_length_valid
        mavlink_key_hex_format_valid = [bool]$operatorCredentialReadiness.mavlink_key_hex_format_valid
        ready = $operatorCredentialsReady
        gate_overridden = [bool]$AllowMissingOperatorCredentials
    }
    qgc_exe = [ordered]@{
        path = $qgcExe
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $qgcExe).Hash
    }
    common_detector = [ordered]@{
        path = $model
        sha256 = $modelHash
        raw_output = "1x84x8400"
        embedded_topk = $false
        embedded_nms = $false
    }
    network = [ordered]@{
        ethernet_name = if ($null -ne $physicalEthernet) { $physicalEthernet.Name } else { $null }
        ethernet_status = if ($null -ne $physicalEthernet) { [string]$physicalEthernet.Status } else { "NotFound" }
        ethernet_ipv4 = @($physicalEthernetIpv4)
        aircraft_subnet_match = $aircraftSubnetMatch
        physical_adapters = @(
            $physicalEthernetProfiles | ForEach-Object {
                [ordered]@{
                    name = [string]$_.adapter.Name
                    status = [string]$_.adapter.Status
                    ipv4 = @($_.ipv4)
                    aircraft_subnet = [bool]$_.aircraft_subnet
                }
            }
        )
        jetson_reachable = $jetsonReachable
        camera_reachable = $cameraReachable
        field_link_ready = [bool]$fieldLinkBinding.hardware_link_ready
        field_link_gate_passed = [bool]$fieldLinkBinding.gate_passed
    }
    remaining_live_gates = $remainingLiveGates
}

$evidenceDirectory = Split-Path -Parent $EvidencePath
New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
$evidenceJson = $evidence | ConvertTo-Json -Depth 8
# Windows PowerShell 5.1 interprets ``-Encoding utf8`` as UTF-8 *with* BOM,
# while PowerShell 7 writes without one.  E7 evidence is a cross-tool JSON
# contract, so use the .NET writer explicitly and keep the bytes stable.
$evidencePathAbsolute = [System.IO.Path]::GetFullPath($EvidencePath)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $evidencePathAbsolute,
    $evidenceJson + [Environment]::NewLine,
    $utf8NoBom
)
$evidenceJson

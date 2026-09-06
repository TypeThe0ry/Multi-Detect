[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$HostAddress,
    [ValidateRange(1, 65535)]
    [int]$Port = 14580,
    [Parameter(Mandatory = $true)]
    [string]$HardwareId,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSourceCidr,
    [string]$OperatorHmacKeyEnvironment = 'MULTIDETECT_OPERATOR_HMAC_KEY',
    [string]$MavlinkSigningKeyEnvironment = 'MULTIDETECT_MAVLINK_SIGNING_KEY_HEX',
    [ValidateRange(1, 10000)]
    [int]$MinimumRoundTrips = 100,
    [ValidateRange(0.0, 1.0)]
    [double]$MaximumPacketLossRate = 0.01,
    [ValidateRange(0.001, 600000.0)]
    [double]$MaximumAckLatencyP95Milliseconds = 500.0,
    [ValidateRange(1, 100)]
    [int]$MaximumAttempts = 3,
    [ValidateRange(0, 86400)]
    [double]$MaximumEvidenceAgeSeconds = 900.0,
    [string]$RepositoryRoot = '',
    [string]$Python = '',
    [string]$OutputDirectory = 'artifacts\evaluation',
    [string]$Stamp = '',
    [switch]$PlanOnly,
    [string]$PlanOutput = '',
    [string[]]$ReleaseGateExtraArguments = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptRoot = [string]$PSScriptRoot
$defaultRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptRoot '..'))
$root = if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $defaultRoot
} else {
    [System.IO.Path]::GetFullPath($RepositoryRoot)
}
if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    throw "Repository root does not exist: $root"
}

function Resolve-RepositoryPath {
    param([Parameter(Mandatory = $true)][string]$Requested)
    $candidate = if ([System.IO.Path]::IsPathRooted($Requested)) {
        [System.IO.Path]::GetFullPath($Requested)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $root $Requested))
    }
    $rootPrefix = $root.TrimEnd('\') + '\'
    if ($candidate -ne $root -and -not $candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes repository root: $candidate"
    }
    return $candidate
}

function Resolve-Python {
    if (-not [string]::IsNullOrWhiteSpace($Python)) {
        $candidate = [System.IO.Path]::GetFullPath($Python)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Python interpreter does not exist: $candidate"
        }
        return $candidate
    }
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    $command = Get-Command python -ErrorAction Stop
    return [string]$command.Source
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    & $Executable @Arguments
    $code = if ($null -eq (Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue)) {
        0
    } else {
        $LASTEXITCODE
    }
    if ($code -ne 0) {
        throw "$Label failed with exit code $code"
    }
}

try {
    $remoteAddress = [System.Net.IPAddress]::Parse($HostAddress)
}
catch {
    throw "HostAddress must be a literal IPv4 address: $HostAddress"
}
if ($remoteAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    [System.Net.IPAddress]::IsLoopback($remoteAddress)) {
    throw "HostAddress must be a non-loopback IPv4 address: $HostAddress"
}
$cidrParts = $ExpectedSourceCidr.Split('/', 2)
$cidrPrefix = 0
try {
    $cidrAddress = [System.Net.IPAddress]::Parse($cidrParts[0])
}
catch {
    throw "ExpectedSourceCidr must be an IPv4 CIDR: $ExpectedSourceCidr"
}
if ($cidrParts.Count -ne 2 -or
    $cidrAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    -not [int]::TryParse($cidrParts[1], [ref]$cidrPrefix) -or
    $cidrPrefix -lt 0 -or $cidrPrefix -gt 32) {
    throw "ExpectedSourceCidr must be an IPv4 CIDR: $ExpectedSourceCidr"
}

foreach ($secret in @(
        @{ key = 'operator_hmac'; name = $OperatorHmacKeyEnvironment },
        @{ key = 'mavlink_signing'; name = $MavlinkSigningKeyEnvironment }
    )) {
    $environmentName = [string]$secret.name
    if ($environmentName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "Invalid environment variable name: $environmentName"
    }
    $environmentValue = Get-Item -LiteralPath ("Env:" + $environmentName) -ErrorAction SilentlyContinue
    $present = $null -ne $environmentValue -and
        -not [string]::IsNullOrWhiteSpace([string]$environmentValue.Value)
    if (-not $PlanOnly -and -not $present) {
        throw "Required secret environment variable is not set: $environmentName"
    }
}

if ([string]::IsNullOrWhiteSpace($HardwareId)) {
    throw 'HardwareId must not be empty.'
}
if ([string]::IsNullOrWhiteSpace($Stamp)) {
    $Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ', [Globalization.CultureInfo]::InvariantCulture)
}
if ($Stamp -notmatch '^[0-9]{8}T[0-9]{6}Z$') {
    throw 'Stamp must use yyyyMMddTHHmmssZ.'
}

$controlledArguments = @(
    '-RepositoryRoot', '-Python', '-RealGr01OperatorLinkBench',
    '-RealGr01OperatorLinkMaxAgeSeconds', '-OutputDirectory', '-Stamp', '-EvaluatedAtUtc'
)
foreach ($argument in $ReleaseGateExtraArguments) {
    $argumentName = ([string]$argument -split '=', 2)[0]
    if ($controlledArguments -contains $argumentName) {
        throw "ReleaseGateExtraArguments cannot override controlled parameter: $argument"
    }
}

$pythonPath = Resolve-Python
$multiDetectCli = Resolve-RepositoryPath -Requested 'scripts\run_multidetect_cli.py'
Invoke-Checked -Executable $pythonPath -Arguments @($multiDetectCli, '--help') -Label 'Python package preflight'
$outputRoot = Resolve-RepositoryPath -Requested $OutputDirectory
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

function Get-Gr01PlanArguments {
    param([Parameter(Mandatory = $true)][string]$Destination)
    $planTool = Resolve-RepositoryPath -Requested 'scripts\verify_gr01_release_session_plan.py'
    return @(
        $planTool, 'create',
        '--host-address', $remoteAddress.ToString(),
        '--port', [string]$Port,
        '--hardware-id', $HardwareId,
        '--expected-source-cidr', $ExpectedSourceCidr,
        '--operator-hmac-environment', $OperatorHmacKeyEnvironment,
        '--mavlink-signing-environment', $MavlinkSigningKeyEnvironment,
        '--minimum-round-trips', [string]$MinimumRoundTrips,
        '--maximum-packet-loss-rate', $MaximumPacketLossRate.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ),
        '--maximum-ack-latency-p95-ms', $MaximumAckLatencyP95Milliseconds.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ),
        '--maximum-attempts', [string]$MaximumAttempts,
        '--maximum-evidence-age-s', $MaximumEvidenceAgeSeconds.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ),
        '--repository-root', $root,
        '--python', $pythonPath,
        '--output-root', $outputRoot,
        '--out', $Destination
    )
}

if ($PlanOnly) {
    $planPath = if ([string]::IsNullOrWhiteSpace($PlanOutput)) {
        Join-Path $outputRoot ("gr01-release-session-plan-" + $Stamp + '.json')
    } else {
        Resolve-RepositoryPath -Requested $PlanOutput
    }
    $planTool = Resolve-RepositoryPath -Requested 'scripts\verify_gr01_release_session_plan.py'
    $planArguments = Get-Gr01PlanArguments -Destination $planPath
    Invoke-Checked -Executable $pythonPath -Arguments $planArguments -Label 'GR01 release-session plan creation'
    Invoke-Checked -Executable $pythonPath -Arguments @(
        $planTool, 'verify', $planPath, '--check-environment'
    ) -Label 'GR01 release-session plan verification'
    Write-Output "GR01 release session plan written: $planPath"
    return
}
$sessionRoot = Join-Path $outputRoot ("gr01-release-session-" + $Stamp)
if (Test-Path -LiteralPath $sessionRoot) {
    throw "Session output already exists: $sessionRoot"
}
New-Item -ItemType Directory -Path $sessionRoot | Out-Null
$planPath = Join-Path $sessionRoot 'gr01-release-session-plan.json'
$planTool = Resolve-RepositoryPath -Requested 'scripts\verify_gr01_release_session_plan.py'
Invoke-Checked -Executable $pythonPath -Arguments (Get-Gr01PlanArguments -Destination $planPath) `
    -Label 'GR01 release-session execution preflight creation'
Invoke-Checked -Executable $pythonPath -Arguments @(
    $planTool, 'verify', $planPath, '--check-environment', '--require-ready'
) -Label 'GR01 release-session execution preflight verification'

$benchPath = Join-Path $sessionRoot 'gr01-link-bench.json'
$benchArguments = @(
    $multiDetectCli, 'gr01-link-bench',
    '--host', $HostAddress,
    '--port', [string]$Port,
    '--operator-hmac-key-env', $OperatorHmacKeyEnvironment,
    '--mavlink-signing-key-hex-env', $MavlinkSigningKeyEnvironment,
    '--minimum-round-trips', [string]$MinimumRoundTrips,
    '--maximum-attempts', [string]$MaximumAttempts,
    '--maximum-packet-loss-rate', $MaximumPacketLossRate.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--maximum-ack-latency-p95-ms', $MaximumAckLatencyP95Milliseconds.ToString([Globalization.CultureInfo]::InvariantCulture),
    '--hardware-mode',
    '--hardware-id', $HardwareId,
    '--expected-source-cidr', $ExpectedSourceCidr,
    '--out', $benchPath
)
Invoke-Checked -Executable $pythonPath -Arguments $benchArguments -Label 'GR01 hardware bench'
Invoke-Checked -Executable $pythonPath -Arguments @(
    (Resolve-RepositoryPath -Requested 'scripts\verify_gr01_link_bench.py'),
    $benchPath,
    '--require-passed'
) -Label 'GR01 independent bench verification'

$gateDirectory = Join-Path $sessionRoot 'release-gate'
$evaluatedAtUtc = [DateTime]::UtcNow.ToString(
    'yyyy-MM-ddTHH:mm:ss.ffffffZ',
    [Globalization.CultureInfo]::InvariantCulture
)
$releaseWrapper = Resolve-RepositoryPath -Requested 'scripts\run_current_release_gate_pre8.ps1'
$releaseArguments = @(
    '-RepositoryRoot', $root,
    '-Python', $pythonPath,
    '-RealGr01OperatorLinkBench', $benchPath,
    '-RealGr01OperatorLinkMaxAgeSeconds', $MaximumEvidenceAgeSeconds.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    ),
    '-OutputDirectory', $gateDirectory,
    '-Stamp', $Stamp,
    '-EvaluatedAtUtc', $evaluatedAtUtc
) + $ReleaseGateExtraArguments
& $releaseWrapper @releaseArguments
if ($LASTEXITCODE -ne 0) {
    throw "Release-gate wrapper failed with exit code $LASTEXITCODE"
}

$manifestFiles = @(Get-ChildItem -LiteralPath $gateDirectory -File -Filter '*.json' |
    Where-Object { $_.Name -notlike '*-report.json' -and $_.Name -notlike '*-strict.json' })
$reportFiles = @(Get-ChildItem -LiteralPath $gateDirectory -File -Filter '*-report.json')
if ($manifestFiles.Count -ne 1 -or $reportFiles.Count -ne 1) {
    throw 'Release-gate wrapper did not create exactly one manifest and one report.'
}

$recordPath = Join-Path $sessionRoot 'gr01-release-session.json'
$sessionId = "gr01-release-session-$Stamp"
$recordTool = Resolve-RepositoryPath -Requested 'scripts\verify_gr01_release_session.py'
Invoke-Checked -Executable $pythonPath -Arguments @(
    $recordTool,
    'create',
    '--plan', $planPath,
    '--bench', $benchPath,
    '--manifest', $manifestFiles[0].FullName,
    '--report', $reportFiles[0].FullName,
    '--version-manifest', (Resolve-RepositoryPath -Requested 'configs\product-version.json'),
    '--session-id', $sessionId,
    '--out', $recordPath
) -Label 'GR01 release-session record creation'
Invoke-Checked -Executable $pythonPath -Arguments @(
    $recordTool,
    'verify',
    $recordPath,
    '--version-manifest', (Resolve-RepositoryPath -Requested 'configs\product-version.json')
) -Label 'GR01 release-session independent verification'

Write-Output "GR01 release session passed: $recordPath"
Write-Output 'This record verifies the GR01 E7 binding only; inspect release_allowed separately.'

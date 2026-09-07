[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaseDirectory,
    [Parameter(Mandatory = $true)]
    [string]$Mission,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeObservation,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeProfile,
    [Parameter(Mandatory = $true)]
    [ValidateSet('hil', 'real')]
    [string]$RuntimeMode,
    [Parameter(Mandatory = $true)]
    [string]$Endpoint,
    [ValidateRange(1, 4000000)]
    [int]$Baud = 57600,
    [ValidateRange(1, 255)]
    [int]$ExpectedSystemId = 1,
    [ValidateRange(1, 255)]
    [int]$ExpectedComponentId = 1,
    [ValidateRange(0.1, 3600)]
    [double]$ProviderStaleAfterSeconds = 1.5,
    [ValidateRange(0.05, 3600)]
    [double]$DurationSeconds = 30.0,
    [ValidateRange(0.001, 10)]
    [double]$PollIntervalSeconds = 0.05,
    [switch]$NoRequestAutopilotVersion,
    [switch]$RequestRangingStreams,
    [string]$RuntimeProfileHmacKeyEnv = 'MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY',
    [Parameter(Mandatory = $true)]
    [string]$RuntimeProfileKeyId,
    [string]$RuntimeSigningIdentityHash = '',
    [string]$RuntimeSigningKeyHexEnv = '',
    [string]$MissionFeedbackOut = 'feedback\px4-mission-feedback.json',
    [string]$EndpointVehicleIdentityOut = 'identity\px4-endpoint-vehicle.json',
    [string]$JoinOut = 'join\live-runtime-observation-mission-binding.json',
    [string]$PythonPath = '',
    [switch]$Overwrite
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve the repository from this file instead of the caller's current
# directory.  The capture module itself enforces that every input/output
# sidecar remains beneath BaseDirectory; this wrapper only supplies a stable
# Python/module environment for Windows field operators.
$scriptRoot = [string]$PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    throw 'Unable to resolve the PX4 runtime capture script directory'
}
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptRoot '..'))

function Assert-NonEmpty {
    param(
        [Parameter(Mandatory = $true)] [string]$Value,
        [Parameter(Mandatory = $true)] [string]$Name
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name must not be empty"
    }
}

Assert-NonEmpty -Value $BaseDirectory -Name 'BaseDirectory'
Assert-NonEmpty -Value $Mission -Name 'Mission'
Assert-NonEmpty -Value $RuntimeObservation -Name 'RuntimeObservation'
Assert-NonEmpty -Value $RuntimeProfile -Name 'RuntimeProfile'
Assert-NonEmpty -Value $RuntimeMode -Name 'RuntimeMode'
Assert-NonEmpty -Value $Endpoint -Name 'Endpoint'
Assert-NonEmpty -Value $RuntimeProfileHmacKeyEnv -Name 'RuntimeProfileHmacKeyEnv'
Assert-NonEmpty -Value $RuntimeProfileKeyId -Name 'RuntimeProfileKeyId'
$RuntimeMode = $RuntimeMode.ToLowerInvariant()

foreach ($number in @(
        $ProviderStaleAfterSeconds,
        $DurationSeconds,
        $PollIntervalSeconds
    )) {
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) {
        throw 'ProviderStaleAfterSeconds, DurationSeconds and PollIntervalSeconds must be finite numbers'
    }
}

if (-not [string]::IsNullOrWhiteSpace($RuntimeSigningIdentityHash) -and
    -not [string]::IsNullOrWhiteSpace($RuntimeSigningKeyHexEnv)) {
    throw 'RuntimeSigningIdentityHash and RuntimeSigningKeyHexEnv are mutually exclusive'
}
if ($RuntimeMode -eq 'real' -and
    [string]::IsNullOrWhiteSpace($RuntimeSigningIdentityHash) -and
    [string]::IsNullOrWhiteSpace($RuntimeSigningKeyHexEnv)) {
    throw 'REAL capture requires RuntimeSigningIdentityHash or RuntimeSigningKeyHexEnv'
}
if ($RuntimeMode -eq 'hil' -and
    (-not [string]::IsNullOrWhiteSpace($RuntimeSigningIdentityHash) -or
        -not [string]::IsNullOrWhiteSpace($RuntimeSigningKeyHexEnv))) {
    throw 'HIL capture cannot carry a REAL signing identity'
}

function Resolve-Python {
    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        $explicit = [System.IO.Path]::GetFullPath($PythonPath)
        if (-not (Test-Path -LiteralPath $explicit -PathType Leaf)) {
            throw "PythonPath does not point to a file: $explicit"
        }
        return $explicit
    }

    $candidates = @(
        (Join-Path $repositoryRoot 'ground-control\MultiDetectGCS\.venv\Scripts\python.exe'),
        (Join-Path $repositoryRoot '.venv\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    foreach ($commandName in @('python.exe', 'python', 'py.exe', 'py')) {
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
            return [string]$command.Source
        }
    }
    throw 'No Python interpreter found. Install Python or create the repository .venv.'
}

function Get-NativeExitCode {
    $variable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -eq $variable) {
        return 0
    }
    return [int]$variable.Value
}

function Format-InvariantDouble {
    param([double]$Value)
    return $Value.ToString('R', [System.Globalization.CultureInfo]::InvariantCulture)
}

$python = Resolve-Python
$arguments = [System.Collections.Generic.List[string]]::new()
$arguments.Add('-m')
$arguments.Add('multidetect.px4_runtime_capture')
$arguments.Add('--base-directory')
$arguments.Add($BaseDirectory)
$arguments.Add('--mission')
$arguments.Add($Mission)
$arguments.Add('--runtime-observation')
$arguments.Add($RuntimeObservation)
$arguments.Add('--runtime-profile')
$arguments.Add($RuntimeProfile)
$arguments.Add('--runtime-mode')
$arguments.Add($RuntimeMode)
$arguments.Add('--endpoint')
$arguments.Add($Endpoint)
$arguments.Add('--baud')
$arguments.Add([string]$Baud)
$arguments.Add('--expected-system-id')
$arguments.Add([string]$ExpectedSystemId)
$arguments.Add('--expected-component-id')
$arguments.Add([string]$ExpectedComponentId)
$arguments.Add('--provider-stale-after-seconds')
$arguments.Add((Format-InvariantDouble -Value $ProviderStaleAfterSeconds))
$arguments.Add('--duration-seconds')
$arguments.Add((Format-InvariantDouble -Value $DurationSeconds))
$arguments.Add('--poll-interval-seconds')
$arguments.Add((Format-InvariantDouble -Value $PollIntervalSeconds))
$arguments.Add('--runtime-profile-hmac-key-env')
$arguments.Add($RuntimeProfileHmacKeyEnv)
$arguments.Add('--runtime-profile-key-id')
$arguments.Add($RuntimeProfileKeyId)
$arguments.Add('--mission-feedback-out')
$arguments.Add($MissionFeedbackOut)
$arguments.Add('--endpoint-vehicle-identity-out')
$arguments.Add($EndpointVehicleIdentityOut)
$arguments.Add('--join-out')
$arguments.Add($JoinOut)

if (-not $NoRequestAutopilotVersion) {
    $arguments.Add('--request-autopilot-version')
}
else {
    $arguments.Add('--no-request-autopilot-version')
}
if ($RequestRangingStreams) {
    $arguments.Add('--request-ranging-streams')
}
if (-not [string]::IsNullOrWhiteSpace($RuntimeSigningIdentityHash)) {
    $arguments.Add('--runtime-signing-identity-hash')
    $arguments.Add($RuntimeSigningIdentityHash)
}
if (-not [string]::IsNullOrWhiteSpace($RuntimeSigningKeyHexEnv)) {
    $arguments.Add('--runtime-signing-key-hex-env')
    $arguments.Add($RuntimeSigningKeyHexEnv)
}
if ($Overwrite) {
    $arguments.Add('--overwrite')
}

# The HMAC/signing material is intentionally never placed in this argument
# list.  The Python CLI resolves the named environment variables in its own
# process, so PowerShell transcripts and error output contain names only.
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    & $python @($arguments.ToArray())
    $exitCode = Get-NativeExitCode
}
finally {
    if ($null -eq $previousPythonPath) {
        Remove-Item -LiteralPath 'Env:PYTHONPATH' -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $previousPythonPath
    }
}

if ($exitCode -ne 0) {
    exit $exitCode
}

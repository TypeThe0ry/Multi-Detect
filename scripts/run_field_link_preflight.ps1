[CmdletBinding()]
param(
    [string]$InterfaceAlias = 'Ethernet',
    [switch]$AutoSelectInterface,
    [string]$ExpectedSourceCidr = '192.168.144.0/24',
    # Keep endpoint selection explicit and portable across workstations.  The
    # defaults match the GR01 field harness, while callers can override each
    # host/port after moving the aircraft to another subnet.
    [string]$JetsonHost = '192.168.144.20',
    [string]$Gr01Host = '192.168.144.11',
    [string]$CameraHost = '192.168.144.108',
    [string]$InterfaceDiagnostic = '',
    [switch]$GenerateInterfaceDiagnostic,
    [string]$AddressState = '',
    [ValidateRange(0, 300)]
    [double]$AddressStateMaxAgeSeconds = 300.0,
    [string]$ExpectedFieldAddress = '192.168.144.250',
    [ValidateRange(1, 32)]
    [int]$ExpectedFieldPrefixLength = 24,
    [ValidateRange(1, 65535)]
    [int]$JetsonPort = 22,
    [ValidateRange(1, 65535)]
    [int]$Gr01Port = 5760,
    [ValidateRange(1, 65535)]
    [int]$RtspPort = 554,
    [ValidateRange(1, 65535)]
    [int]$MetadataPort = 14580,
    [ValidateRange(1, 65535)]
    [int]$QgcPort = 14581,
    [string]$Output = '',
    [ValidateRange(1, 100)]
    [int]$Samples = 3,
    [ValidateRange(0, 3600)]
    [double]$IntervalSeconds = 0.5,
    [ValidateRange(0.1, 60)]
    [double]$TimeoutSeconds = 2.0,
    [string]$DiscoverCidr = '',
    [ValidateRange(1, 65534)]
    [int]$DiscoverMaxHosts = 256,
    [ValidateRange(1, 256)]
    [int]$DiscoverWorkers = 32,
    [ValidateRange(0, 60)]
    [double]$DiscoverTimeoutSeconds = 0.0,
    [switch]$ProtocolChecks,
    [switch]$StopOnFailure,
    [switch]$RequireRoute,
    [Alias('RequireAll')]
    [switch]$RequireReady
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve paths from the script itself so the command works after a checkout
# migration and does not depend on the caller's current directory.
$scriptRoot = [string]$PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    throw 'Unable to resolve the field-link helper directory'
}
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $scriptRoot '..')).Path

foreach ($number in @($IntervalSeconds, $TimeoutSeconds)) {
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) {
        throw 'IntervalSeconds and TimeoutSeconds must be finite numbers'
    }
}
if ([double]::IsNaN($AddressStateMaxAgeSeconds) -or
    [double]::IsInfinity($AddressStateMaxAgeSeconds)) {
    throw 'AddressStateMaxAgeSeconds must be a finite number'
}

if ([double]::IsNaN($DiscoverTimeoutSeconds) -or [double]::IsInfinity($DiscoverTimeoutSeconds)) {
    throw 'DiscoverTimeoutSeconds must be a finite number'
}
if ([string]::IsNullOrWhiteSpace($DiscoverCidr) -and $DiscoverTimeoutSeconds -gt 0.0) {
    throw 'DiscoverTimeoutSeconds requires DiscoverCidr'
}
foreach ($endpoint in @(
        @{ Name = 'JetsonHost'; Value = $JetsonHost },
        @{ Name = 'Gr01Host'; Value = $Gr01Host },
        @{ Name = 'CameraHost'; Value = $CameraHost }
    )) {
    if ([string]::IsNullOrWhiteSpace([string]$endpoint.Value)) {
        throw ('{0} must not be empty' -f $endpoint.Name)
    }
}

function Resolve-RepositoryPath {
    param([Parameter(Mandatory)] [string]$Requested)

    if ([System.IO.Path]::IsPathRooted($Requested)) {
        return [System.IO.Path]::GetFullPath($Requested)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $Requested))
}

function Resolve-Python {
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
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
            return [string]$command.Source
        }
    }
    throw 'No Python interpreter found. Install Python or the repository QGC venv.'
}

function Invoke-JsonCommand {
    param(
        [Parameter(Mandatory)] [string]$Python,
        [Parameter(Mandatory)] [string[]]$Arguments
    )

    $lines = @(& $Python @Arguments 2>&1)
    # PowerShell does not materialize the automatic LASTEXITCODE variable on
    # every host when a native child returns through an empty pipeline.  Read
    # it defensively so StrictMode cannot turn a valid diagnostic into a
    # wrapper exception.  A missing value is equivalent to the native
    # command's successful zero exit status; non-zero values remain intact.
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

$prepareScript = Join-Path $repositoryRoot 'scripts\prepare_field_link.ps1'
$interfaceDiagnosticScript = Join-Path $repositoryRoot 'scripts\inspect_field_link_interfaces.ps1'
$preflightScript = Join-Path $repositoryRoot 'scripts\run_field_link_preflight.py'
$verifyScript = Join-Path $repositoryRoot 'scripts\verify_field_link_preflight.py'
foreach ($requiredFile in @($prepareScript, $interfaceDiagnosticScript, $preflightScript, $verifyScript)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required field-link helper is missing: $requiredFile"
    }
}

$resolvedOutput = if ([string]::IsNullOrWhiteSpace($Output)) {
    Resolve-RepositoryPath -Requested (Join-Path 'artifacts\evaluation' (
        'field-link-preflight-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    ))
}
else {
    Resolve-RepositoryPath -Requested $Output
}
$outputParent = Split-Path -Parent $resolvedOutput
if (-not [string]::IsNullOrWhiteSpace($outputParent)) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}

$python = Resolve-Python

# The interface probe is deliberately delegated to the existing VerifyOnly
# path.  It never adds/removes an address and gives the operator a visible
# snapshot before any endpoint probe runs.
$stateLines = @(
    if ($AutoSelectInterface) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $prepareScript `
            -InterfaceAlias $InterfaceAlias -AutoSelectCidr $ExpectedSourceCidr `
            -AutoSelectInterface -VerifyOnly 2>&1
    }
    else {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $prepareScript `
            -InterfaceAlias $InterfaceAlias -VerifyOnly 2>&1
    }
)
$stateExitCode = 0
$stateLastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
if ($null -ne $stateLastExitCodeVariable) {
    $stateExitCode = [int]$stateLastExitCodeVariable.Value
}
$stateText = $stateLines -join [Environment]::NewLine
if ($stateExitCode -ne 0) {
    throw "Interface verification failed with exit code ${stateExitCode}: $stateText"
}
try {
    $state = $stateText | ConvertFrom-Json
}
catch {
    throw "Interface verification did not return JSON: $($_.Exception.Message)"
}
$addressSummary = @($state.addresses | ForEach-Object {
        '{0}/{1}' -f $_.ip_address, $_.prefix_length
    }) -join ', '
Write-Output (
    'Interface {0}: status={1}, link_speed={2}, addresses={3}' -f
    $state.interface_alias, $state.interface_status, $state.link_speed, $addressSummary
)

$resolvedInterfaceDiagnostic = $null
if ($GenerateInterfaceDiagnostic) {
    # Generate the read-only adapter snapshot in the same invocation so a
    # migrated workstation cannot accidentally bind a stale diagnostic file.
    # The inspect helper writes only the requested evidence artifact; it never
    # changes addresses, routes, DNS or flight-control state.
    $diagnosticRequested = if ([string]::IsNullOrWhiteSpace($InterfaceDiagnostic)) {
        Join-Path 'artifacts\evaluation' (
            'field-link-interface-diagnostic-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
        )
    }
    else {
        $InterfaceDiagnostic
    }
    $resolvedInterfaceDiagnostic = Resolve-RepositoryPath -Requested $diagnosticRequested
    if ($resolvedInterfaceDiagnostic -eq $resolvedOutput) {
        throw 'Interface diagnostic and field-link preflight output must refer to different files'
    }
    $diagnosticParent = Split-Path -Parent $resolvedInterfaceDiagnostic
    if (-not [string]::IsNullOrWhiteSpace($diagnosticParent)) {
        New-Item -ItemType Directory -Path $diagnosticParent -Force | Out-Null
    }
    $diagnosticLines = @(
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $interfaceDiagnosticScript `
            -ExpectedSourceCidr $ExpectedSourceCidr -Output $resolvedInterfaceDiagnostic 2>&1
    )
    $diagnosticExitCode = 0
    $diagnosticLastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -ne $diagnosticLastExitCodeVariable) {
        $diagnosticExitCode = [int]$diagnosticLastExitCodeVariable.Value
    }
    $diagnosticText = $diagnosticLines -join [Environment]::NewLine
    if (-not [string]::IsNullOrWhiteSpace($diagnosticText)) {
        Write-Output $diagnosticText
    }
    if ($diagnosticExitCode -ne 0) {
        throw "Interface diagnostic failed with exit code ${diagnosticExitCode}: $diagnosticText"
    }
    if (-not (Test-Path -LiteralPath $resolvedInterfaceDiagnostic -PathType Leaf)) {
        throw "Interface diagnostic did not create its report: $resolvedInterfaceDiagnostic"
    }
    Write-Output ('Generated read-only interface diagnostic: {0}' -f $resolvedInterfaceDiagnostic)
}
elseif (-not [string]::IsNullOrWhiteSpace($InterfaceDiagnostic)) {
    $resolvedInterfaceDiagnostic = Resolve-RepositoryPath -Requested $InterfaceDiagnostic
    if (-not (Test-Path -LiteralPath $resolvedInterfaceDiagnostic -PathType Leaf)) {
        throw "Interface diagnostic does not exist: $resolvedInterfaceDiagnostic"
    }
    Write-Output ('Interface diagnostic: {0}' -f $resolvedInterfaceDiagnostic)
}

$resolvedAddressState = $null
if (-not [string]::IsNullOrWhiteSpace($AddressState)) {
    $resolvedAddressState = Resolve-RepositoryPath -Requested $AddressState
    if (-not (Test-Path -LiteralPath $resolvedAddressState -PathType Leaf)) {
        throw "Field-link address state does not exist: $resolvedAddressState"
    }
    if ($resolvedAddressState -eq $resolvedOutput) {
        throw 'Address state and field-link preflight output must refer to different files'
    }
    if ($null -ne $resolvedInterfaceDiagnostic -and
        $resolvedAddressState -eq $resolvedInterfaceDiagnostic) {
        throw 'Address state and interface diagnostic must refer to different files'
    }
    Write-Output ('Field-link address state: {0}' -f $resolvedAddressState)
}

$preflightArguments = [System.Collections.Generic.List[string]]::new()
$preflightArguments.Add($preflightScript)
$preflightArguments.Add('--timeout-s')
$preflightArguments.Add(([string]$TimeoutSeconds))
$preflightArguments.Add('--samples')
$preflightArguments.Add(([string]$Samples))
$preflightArguments.Add('--interval-s')
$preflightArguments.Add(([string]$IntervalSeconds))
$preflightArguments.Add('--out')
$preflightArguments.Add($resolvedOutput)
$preflightArguments.Add('--jetson-host')
$preflightArguments.Add($JetsonHost)
$preflightArguments.Add('--gr01-host')
$preflightArguments.Add($Gr01Host)
$preflightArguments.Add('--camera-host')
$preflightArguments.Add($CameraHost)
$preflightArguments.Add('--jetson-port')
$preflightArguments.Add(([string]$JetsonPort))
$preflightArguments.Add('--gr01-port')
$preflightArguments.Add(([string]$Gr01Port))
$preflightArguments.Add('--rtsp-port')
$preflightArguments.Add(([string]$RtspPort))
$preflightArguments.Add('--metadata-port')
$preflightArguments.Add(([string]$MetadataPort))
$preflightArguments.Add('--qgc-port')
$preflightArguments.Add(([string]$QgcPort))
if ($ProtocolChecks) {
    $preflightArguments.Add('--protocol-checks')
}
if ($StopOnFailure) {
    # Forward the bounded-watch fail-fast option without changing the
    # wrapper's read-only behavior.  The Python verifier still records the
    # collected sample count and keeps a partial watch fail-closed.
    $preflightArguments.Add('--stop-on-failure')
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedSourceCidr)) {
    $preflightArguments.Add('--expected-source-cidr')
    $preflightArguments.Add($ExpectedSourceCidr)
}
if ($null -ne $resolvedInterfaceDiagnostic) {
    # Bind the exact read-only migration snapshot to the preflight report.
    # The Python verifier reopens and hashes this file; the wrapper never
    # mutates adapter addresses, routes or DNS settings.
    $preflightArguments.Add('--interface-diagnostic')
    $preflightArguments.Add($resolvedInterfaceDiagnostic)
}
if ($null -ne $resolvedInterfaceDiagnostic -or $null -ne $resolvedAddressState) {
    $preflightArguments.Add('--interface-alias')
    $preflightArguments.Add([string]$state.interface_alias)
}
if ($null -ne $resolvedAddressState) {
    # A supplied receipt must be a fresh, successful apply record.  Python
    # reopens and verifies it before probing, then hash-binds it into the
    # saved report; plan/verify/what-if records are rejected.
    $preflightArguments.Add('--address-state')
    $preflightArguments.Add($resolvedAddressState)
    $preflightArguments.Add('--address-state-max-age-s')
    $preflightArguments.Add(([string]$AddressStateMaxAgeSeconds))
    $preflightArguments.Add('--expected-field-address')
    $preflightArguments.Add($ExpectedFieldAddress)
    $preflightArguments.Add('--expected-field-prefix-length')
    $preflightArguments.Add(([string]$ExpectedFieldPrefixLength))
}
if ($RequireRoute) {
    $preflightArguments.Add('--require-route')
}
if ($RequireReady) {
    $preflightArguments.Add('--require-all')
}
if (-not [string]::IsNullOrWhiteSpace($DiscoverCidr)) {
    # Discovery is opt-in and diagnostic only; candidates never replace the
    # explicitly configured Jetson/GR01/camera endpoints or readiness result.
    $preflightArguments.Add('--discover-cidr')
    $preflightArguments.Add($DiscoverCidr)
    $preflightArguments.Add('--discover-max-hosts')
    $preflightArguments.Add(([string]$DiscoverMaxHosts))
    $preflightArguments.Add('--discover-workers')
    $preflightArguments.Add(([string]$DiscoverWorkers))
    if ($DiscoverTimeoutSeconds -gt 0.0) {
        $preflightArguments.Add('--discover-timeout-s')
        $preflightArguments.Add(([string]$DiscoverTimeoutSeconds))
    }
}

$preflightResult = Invoke-JsonCommand -Python $python -Arguments $preflightArguments.ToArray()
if (-not [string]::IsNullOrWhiteSpace($preflightResult.output)) {
    Write-Output $preflightResult.output
}
if (-not (Test-Path -LiteralPath $resolvedOutput -PathType Leaf)) {
    throw "Field-link preflight did not create its report: $resolvedOutput"
}

$verifyArguments = [System.Collections.Generic.List[string]]::new()
$verifyArguments.Add($verifyScript)
$verifyArguments.Add($resolvedOutput)
if (-not $RequireReady) {
    $verifyArguments.Add('--allow-unready')
}
$verifyResult = Invoke-JsonCommand -Python $python -Arguments $verifyArguments.ToArray()
if (-not [string]::IsNullOrWhiteSpace($verifyResult.output)) {
    Write-Output $verifyResult.output
}

Write-Output ('Report: {0}' -f $resolvedOutput)
Write-Output ('Preflight exit={0}; verifier exit={1}; require_ready={2}' -f
    $preflightResult.exit_code, $verifyResult.exit_code, [bool]$RequireReady)

# A normal diagnostic run exits zero even while the aircraft is unplugged;
# the JSON and verifier output retain hardware_link_ready=false.  CI/acceptance
# callers opt into a hard gate with -RequireReady.
if ($RequireReady) {
    if ($preflightResult.exit_code -ne 0) {
        exit $preflightResult.exit_code
    }
    exit $verifyResult.exit_code
}
if ($preflightResult.exit_code -gt 1) {
    exit $preflightResult.exit_code
}
exit $verifyResult.exit_code

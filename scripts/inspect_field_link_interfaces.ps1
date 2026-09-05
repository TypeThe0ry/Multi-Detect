[CmdletBinding()]
param(
    [string]$ExpectedSourceCidr = '192.168.144.0/24',
    [string]$Output = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This helper is deliberately read-only.  It is useful immediately after a
# workstation migration, before prepare_field_link.ps1 is allowed to add a
# secondary address.  It enumerates physical adapters (including disconnected
# ones), their IPv4 addresses, and a directly connected route for the target
# aircraft subnet.  It never calls New/Remove/Set-Net*.
$scriptRoot = [string]$PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
    throw 'Unable to resolve the interface diagnostic script directory'
}
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $scriptRoot '..')).Path

function Resolve-RepositoryPath {
    param([Parameter(Mandatory)] [string]$Requested)

    if ([System.IO.Path]::IsPathRooted($Requested)) {
        return [System.IO.Path]::GetFullPath($Requested)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $Requested))
}

function Convert-IPv4BytesToString {
    param([Parameter(Mandatory)] [byte[]]$Bytes)

    if ($Bytes.Count -ne 4) {
        throw 'IPv4 addresses must contain exactly four bytes'
    }
    return '{0}.{1}.{2}.{3}' -f $Bytes[0], $Bytes[1], $Bytes[2], $Bytes[3]
}

function Parse-IPv4Cidr {
    param([Parameter(Mandatory)] [string]$Cidr)

    $parts = $Cidr.Trim().Split('/', 2)
    if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[0])) {
        throw "Invalid IPv4 CIDR: $Cidr"
    }

    $prefix = 0
    if (-not [int]::TryParse($parts[1], [ref]$prefix) -or $prefix -lt 0 -or $prefix -gt 32) {
        throw "Invalid IPv4 CIDR prefix: $Cidr"
    }

    try {
        $addressBytes = [System.Net.IPAddress]::Parse($parts[0]).GetAddressBytes()
    }
    catch {
        throw "Invalid IPv4 CIDR address: $Cidr"
    }
    if ($addressBytes.Count -ne 4) {
        throw "IPv4 CIDR is required: $Cidr"
    }

    $networkBytes = [byte[]]::new(4)
    $fullBytes = [int][math]::Floor($prefix / 8)
    for ($index = 0; $index -lt 4; $index++) {
        if ($index -lt $fullBytes) {
            $networkBytes[$index] = [byte]$addressBytes[$index]
        }
        elseif ($index -eq $fullBytes -and ($prefix % 8) -gt 0) {
            $remainingBits = $prefix % 8
            $mask = 255 - ([int][math]::Pow(2, 8 - $remainingBits) - 1)
            $networkBytes[$index] = [byte]($addressBytes[$index] -band $mask)
        }
        else {
            $networkBytes[$index] = [byte]0
        }
    }

    return [pscustomobject]@{
        input_cidr = $Cidr.Trim()
        prefix_length = $prefix
        network_bytes = $networkBytes
        canonical_cidr = ('{0}/{1}' -f (Convert-IPv4BytesToString -Bytes $networkBytes), $prefix)
    }
}

function Test-IPv4InCidr {
    param(
        [Parameter(Mandatory)] [string]$Address,
        [Parameter(Mandatory)] [pscustomobject]$CidrInfo
    )

    try {
        $addressBytes = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
    }
    catch {
        return $false
    }
    if ($addressBytes.Count -ne 4) {
        return $false
    }

    $prefix = [int]$CidrInfo.prefix_length
    $fullBytes = [int][math]::Floor($prefix / 8)
    for ($index = 0; $index -lt $fullBytes; $index++) {
        if ($addressBytes[$index] -ne $CidrInfo.network_bytes[$index]) {
            return $false
        }
    }
    $remainingBits = $prefix % 8
    if ($remainingBits -gt 0) {
        $mask = 255 - ([int][math]::Pow(2, 8 - $remainingBits) - 1)
        if (($addressBytes[$fullBytes] -band $mask) -ne ($CidrInfo.network_bytes[$fullBytes] -band $mask)) {
            return $false
        }
    }
    return $true
}

function Convert-AddressRecord {
    param([Parameter(Mandatory)] $Address)

    return [ordered]@{
        ip_address = [string]$Address.IPAddress
        prefix_length = [int]$Address.PrefixLength
        address_state = [string]$Address.AddressState
        skip_as_source = [bool]$Address.SkipAsSource
    }
}

function Convert-RouteRecord {
    param([Parameter(Mandatory)] $Route)

    return [ordered]@{
        destination_prefix = [string]$Route.DestinationPrefix
        next_hop = [string]$Route.NextHop
        route_metric = [int]$Route.RouteMetric
        policy_store = [string]$Route.PolicyStore
        interface_index = [int]$Route.InterfaceIndex
    }
}

$cidrInfo = Parse-IPv4Cidr -Cidr $ExpectedSourceCidr

$physicalQueryFallback = $false
try {
    $adapters = @(Get-NetAdapter -Physical -ErrorAction Stop)
}
catch {
    # Older Windows builds may not expose -Physical.  Keep the fallback
    # explicit in the report so a reviewer can distinguish it from a strict
    # physical-adapter inventory.
    $physicalQueryFallback = $true
    $adapters = @(Get-NetAdapter -ErrorAction Stop)
}

$adapterReports = [System.Collections.Generic.List[object]]::new()
$routeReports = [System.Collections.Generic.List[object]]::new()
$candidateAliases = [System.Collections.Generic.List[string]]::new()

foreach ($adapter in $adapters) {
    $addresses = @(
        Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    )
    $targetAddresses = @($addresses | Where-Object {
            Test-IPv4InCidr -Address ([string]$_.IPAddress) -CidrInfo $cidrInfo
        })
    $routes = @(
        Get-NetRoute -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { [string]$_.DestinationPrefix -eq $cidrInfo.canonical_cidr }
    )

    foreach ($route in $routes) {
        $routeReports.Add([ordered]@{
                interface_alias = [string]$adapter.Name
                interface_status = [string]$adapter.Status
                route = Convert-RouteRecord -Route $route
            })
    }

    $status = [string]$adapter.Status
    $isUp = $status -eq 'Up'
    $hasTargetAddress = $targetAddresses.Count -gt 0
    $hasTargetRoute = $routes.Count -gt 0
    $readyCandidate = $isUp -and $hasTargetAddress
    $hardwareInterface = $null
    $hardwareProperty = $adapter.PSObject.Properties['HardwareInterface']
    if ($null -ne $hardwareProperty -and $null -ne $hardwareProperty.Value) {
        $hardwareInterface = [bool]$hardwareProperty.Value
    }
    if ($readyCandidate) {
        $candidateAliases.Add([string]$adapter.Name)
    }

    $adapterReports.Add([ordered]@{
            interface_alias = [string]$adapter.Name
            interface_index = [int]$adapter.ifIndex
            interface_status = $status
            link_speed = [string]$adapter.LinkSpeed
            mac_address = [string]$adapter.MacAddress
            hardware_interface = $hardwareInterface
            addresses = @($addresses | ForEach-Object { Convert-AddressRecord -Address $_ })
            target_cidr_addresses = @($targetAddresses | ForEach-Object { Convert-AddressRecord -Address $_ })
            target_cidr_match = [bool]$hasTargetAddress
            target_route_present = [bool]$hasTargetRoute
            ready_candidate = [bool]$readyCandidate
        })
}

$selectionStatus = if ($candidateAliases.Count -eq 1) {
    'ready'
}
elseif ($candidateAliases.Count -gt 1) {
    'ambiguous'
}
else {
    'missing'
}

$upAliases = @($adapterReports | Where-Object { $_.interface_status -eq 'Up' } |
    ForEach-Object { [string]$_.interface_alias })
$nextStep = if ($selectionStatus -eq 'ready') {
    'Use the single ready interface explicitly with run_field_link_preflight.ps1; no address change is required.'
}
elseif ($selectionStatus -eq 'ambiguous') {
    'Several active physical interfaces already match the aircraft subnet; pass -InterfaceAlias explicitly and do not auto-select.'
}
elseif ($upAliases.Count -gt 0) {
    'No active interface currently has an address in the aircraft subnet. Verify the cable and choose one alias, then run prepare_field_link.ps1 -VerifyOnly or its explicit -Elevate path.'
}
else {
    'No physical interface is Up. Connect/enable the aircraft Ethernet adapter, then rerun this read-only diagnostic.'
}

$resolvedOutput = if ([string]::IsNullOrWhiteSpace($Output)) {
    Resolve-RepositoryPath -Requested (Join-Path 'artifacts\evaluation' (
            'field-link-interface-diagnostic-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
        ))
}
else {
    Resolve-RepositoryPath -Requested $Output
}
$outputParent = Split-Path -Parent $resolvedOutput
if (-not [string]::IsNullOrWhiteSpace($outputParent)) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}

$report = [ordered]@{
    schema_version = 1
    read_only = $true
    generated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    computer_name = [string]$env:COMPUTERNAME
    expected_source_cidr = $cidrInfo.input_cidr
    normalized_source_cidr = $cidrInfo.canonical_cidr
    physical_query_fallback = [bool]$physicalQueryFallback
    adapters = @($adapterReports)
    target_routes = @($routeReports)
    selection = [ordered]@{
        status = $selectionStatus
        candidate_count = [int]$candidateAliases.Count
        candidate_aliases = @($candidateAliases)
    }
    next_step = $nextStep
    report_path = $resolvedOutput
}

$json = $report | ConvertTo-Json -Depth 12
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($resolvedOutput, $json, $utf8NoBom)
Write-Output $json

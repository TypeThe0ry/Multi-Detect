[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InterfaceAlias = 'Ethernet',
    [string]$IpAddress = '192.168.144.250',
    [ValidateRange(1, 32)]
    [int]$PrefixLength = 24,
    [string]$AutoSelectCidr = '',
    [switch]$AutoSelectInterface,
    [switch]$VerifyOnly,
    [switch]$Remove,
    [switch]$Elevate
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-TargetAddress {
    return Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -eq $IpAddress -and
            [int]$_.PrefixLength -eq $PrefixLength
        }
}

function Show-FieldLinkState {
    $adapter = Get-NetAdapter -Name $InterfaceAlias -ErrorAction Stop
    $addresses = @(Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction Stop)
    $routes = @(Get-NetRoute -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -DestinationPrefix '192.168.144.0/24' -ErrorAction SilentlyContinue)

    [pscustomobject]@{
        interface_alias = $adapter.Name
        interface_index = $adapter.ifIndex
        interface_status = [string]$adapter.Status
        link_speed = [string]$adapter.LinkSpeed
        addresses = @($addresses | ForEach-Object {
            [ordered]@{
                ip_address = [string]$_.IPAddress
                prefix_length = [int]$_.PrefixLength
                address_state = [string]$_.AddressState
                skip_as_source = [bool]$_.SkipAsSource
            }
        })
        aircraft_route = @($routes | ForEach-Object {
            [ordered]@{
                destination_prefix = [string]$_.DestinationPrefix
                next_hop = [string]$_.NextHop
                route_metric = [int]$_.RouteMetric
                policy_store = [string]$_.PolicyStore
            }
        })
    }
}

function Test-IPv4InCidr {
    param(
        [Parameter(Mandatory)] [string]$Address,
        [Parameter(Mandatory)] [string]$Cidr
    )

    $parts = $Cidr.Split('/', 2)
    if ($parts.Count -ne 2) {
        throw "Invalid IPv4 CIDR: $Cidr"
    }
    $prefix = 0
    if (-not [int]::TryParse($parts[1], [ref]$prefix) -or $prefix -lt 0 -or $prefix -gt 32) {
        throw "Invalid IPv4 CIDR prefix: $Cidr"
    }
    try {
        $addressBytes = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
        $networkBytes = [System.Net.IPAddress]::Parse($parts[0]).GetAddressBytes()
    }
    catch {
        throw "Invalid IPv4 CIDR or address: $Cidr / $Address"
    }
    if ($addressBytes.Count -ne 4 -or $networkBytes.Count -ne 4) {
        throw "IPv4 CIDR and address are required: $Cidr / $Address"
    }

    $fullBytes = [int][math]::Floor($prefix / 8)
    for ($index = 0; $index -lt $fullBytes; $index++) {
        if ($addressBytes[$index] -ne $networkBytes[$index]) {
            return $false
        }
    }
    $remainingBits = $prefix % 8
    if ($remainingBits -gt 0) {
        $mask = 255 - ([int][math]::Pow(2, 8 - $remainingBits) - 1)
        if (($addressBytes[$fullBytes] -band $mask) -ne ($networkBytes[$fullBytes] -band $mask)) {
            return $false
        }
    }
    return $true
}

function Resolve-AutoInterfaceAlias {
    $cidr = if ([string]::IsNullOrWhiteSpace($AutoSelectCidr)) {
        '{0}/{1}' -f $IpAddress, $PrefixLength
    }
    else {
        $AutoSelectCidr.Trim()
    }
    $physicalAdapters = @()
    try {
        $physicalAdapters = @(Get-NetAdapter -Physical -ErrorAction Stop |
            Where-Object { $_.Status -eq 'Up' })
    }
    catch {
        # Older Windows builds may not expose -Physical.  The address/status
        # filters below remain read-only and are still safer than guessing.
        $physicalAdapters = @(Get-NetAdapter -ErrorAction Stop |
            Where-Object { $_.Status -eq 'Up' })
    }

    $candidates = @()
    foreach ($adapter in $physicalAdapters) {
        $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue)
        foreach ($address in $addresses) {
            if (Test-IPv4InCidr -Address ([string]$address.IPAddress) -Cidr $cidr) {
                $candidates += [pscustomobject]@{
                    alias = [string]$adapter.Name
                    address = [string]$address.IPAddress
                    prefix_length = [int]$address.PrefixLength
                }
            }
        }
    }

    $uniqueCandidates = @($candidates | Group-Object alias | ForEach-Object { $_.Group[0] })
    if ($uniqueCandidates.Count -eq 1) {
        return [string]$uniqueCandidates[0].alias
    }
    if ($uniqueCandidates.Count -eq 0) {
        $active = @($physicalAdapters | ForEach-Object {
                $addresses = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue)
                $formatted = @($addresses | ForEach-Object { '{0}/{1}' -f $_.IPAddress, $_.PrefixLength }) -join ', '
                '{0} [{1}]' -f $_.Name, $formatted
            }) -join '; '
        throw "No active physical interface has an IPv4 address in $cidr. Active interfaces: $active"
    }
    $names = ($uniqueCandidates | ForEach-Object { '{0} ({1})' -f $_.alias, $_.address }) -join ', '
    throw "Auto-select found multiple active physical interfaces in ${cidr}: $names"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-StartProcessArgument {
    param([Parameter(Mandatory)][string]$Value)

    # Start-Process joins ArgumentList elements into one command line. Quote
    # values containing whitespace and escape embedded quotes/backslashes so
    # interface aliases and repository paths survive the UAC relaunch.
    if ($Value -notmatch '[\s"]') {
        return $Value
    }

    $escaped = $Value -replace '(\\*)"', '$1$1\\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Invoke-ElevatedSelf {
    $hostPath = (Get-Process -Id $PID -ErrorAction Stop).Path
    if ([string]::IsNullOrWhiteSpace($hostPath)) {
        $hostPath = Join-Path $PSHOME 'powershell.exe'
    }

    $childArguments = [System.Collections.Generic.List[string]]::new()
    $childArguments.Add('-NoProfile')
    $childArguments.Add('-ExecutionPolicy')
    $childArguments.Add('Bypass')
    $childArguments.Add('-File')
    $childArguments.Add($PSCommandPath)
    $childArguments.Add('-InterfaceAlias')
    $childArguments.Add($InterfaceAlias)
    $childArguments.Add('-IpAddress')
    $childArguments.Add($IpAddress)
    $childArguments.Add('-PrefixLength')
    $childArguments.Add([string]$PrefixLength)
    if (-not [string]::IsNullOrWhiteSpace($AutoSelectCidr)) {
        $childArguments.Add('-AutoSelectCidr')
        $childArguments.Add($AutoSelectCidr)
    }
    if ($Remove) {
        $childArguments.Add('-Remove')
    }

    $argumentLine = ($childArguments | ForEach-Object {
            ConvertTo-StartProcessArgument -Value ([string]$_)
        }) -join ' '

    if ($PSCmdlet.ShouldProcess('Administrator PowerShell', 'Relaunch field-link helper')) {
        $child = Start-Process -FilePath $hostPath -Verb RunAs -ArgumentList $argumentLine -Wait -PassThru
        exit ([int]$child.ExitCode)
    }
}

# This changes only a secondary host address. It deliberately does not add a
# gateway, DNS server, route to another interface, or any flight-control
# traffic. Run with an elevated PowerShell when applying or removing.
if ($AutoSelectInterface) {
    # Resolve before the existence check.  The selected alias is then passed
    # to the elevated child, so UAC does not repeat a potentially ambiguous
    # selection after the operator has already seen the read-only result.
    $InterfaceAlias = Resolve-AutoInterfaceAlias
}

if (-not (Get-NetAdapter -Name $InterfaceAlias -ErrorAction SilentlyContinue)) {
    throw "Network interface '$InterfaceAlias' was not found"
}

if ($VerifyOnly) {
    Show-FieldLinkState | ConvertTo-Json -Depth 6
    exit 0
}

if ($Elevate -and -not (Test-IsAdministrator)) {
    Invoke-ElevatedSelf
    exit 0
}

if (-not (Test-IsAdministrator)) {
    throw 'Applying or removing the field-link address requires an elevated PowerShell window'
}

$existing = @(Get-TargetAddress)

if ($Remove) {
    if ($existing.Count -eq 0) {
        Write-Output "No matching secondary address is present: $IpAddress/$PrefixLength"
    }
    else {
        foreach ($address in $existing) {
            if ($PSCmdlet.ShouldProcess("$InterfaceAlias $IpAddress/$PrefixLength", 'Remove secondary field-link address')) {
                Remove-NetIPAddress -InterfaceIndex $address.InterfaceIndex -IPAddress $address.IPAddress -Confirm:$false -ErrorAction Stop
            }
        }
    }
}
else {
    $sameAddressElsewhere = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -eq $IpAddress -and
            $_.InterfaceAlias -ne $InterfaceAlias
        })
    if ($sameAddressElsewhere.Count -gt 0) {
        $owners = ($sameAddressElsewhere | ForEach-Object { $_.InterfaceAlias }) -join ', '
        throw "Address $IpAddress is already assigned to another interface: $owners"
    }

    if ($existing.Count -eq 0) {
        if ($PSCmdlet.ShouldProcess("$InterfaceAlias $IpAddress/$PrefixLength", 'Add secondary field-link address')) {
            New-NetIPAddress -InterfaceAlias $InterfaceAlias -IPAddress $IpAddress -PrefixLength $PrefixLength -SkipAsSource $true -ErrorAction Stop | Out-Null
        }
    }
    else {
        Write-Output "Secondary address already present: $IpAddress/$PrefixLength"
    }
}

Show-FieldLinkState | ConvertTo-Json -Depth 6

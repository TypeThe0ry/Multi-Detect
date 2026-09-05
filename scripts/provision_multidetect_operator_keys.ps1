#!/usr/bin/env pwsh

<#
.SYNOPSIS
    Provision the Windows user-scoped credentials used by signed live control.

.DESCRIPTION
    The Multi-Detect live-control path reads two credentials from the Windows
    user environment:

      MULTIDETECT_OPERATOR_KEY              UTF-8 HMAC material (>= 32 bytes)
      MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX  exactly 32 bytes as 64 hex chars

    Existing valid values are reused by default.  Use -Force to rotate both
    values.  The script writes only the User environment scope, verifies the
    values by reading that scope back, and never prints the secret material.
    Restart terminals and QGC after running it so a new process inherits the
    user-scoped values.
#>

[CmdletBinding()]
param(
    [ValidateRange(32, 4096)]
    [int]$OperatorKeyBytes = 32,

    # MAVLink2 signing keys are fixed at 32 bytes.  Keeping this parameter
    # explicit makes the generated artifact self-describing while rejecting
    # accidental non-standard key sizes.
    [ValidateRange(32, 32)]
    [int]$MavlinkKeyBytes = 32,

    [switch]$Force,
    [switch]$PassThru
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$operatorKeyName = "MULTIDETECT_OPERATOR_KEY"
$mavlinkKeyName = "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX"

function Get-Utf8Bytes {
    param([AllowEmptyString()][string]$Value)

    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $encoded = $utf8.GetBytes($Value)
        # The unary comma keeps byte[] as one pipeline object instead of
        # expanding it into one output item per byte.
        return ,$encoded
    }
    finally {
        $utf8 = $null
    }
}

function Test-OperatorKey {
    param([AllowNull()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    $bytes = $null
    try {
        $bytes = Get-Utf8Bytes -Value $Value
        return $bytes.Length -ge 32
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $bytes) {
            [Array]::Clear($bytes, 0, $bytes.Length)
        }
    }
}

function Test-MavlinkKey {
    param([AllowNull()][string]$Value)

    if ($null -eq $Value) {
        return $false
    }
    $trimmed = $Value.Trim()
    return $trimmed.Length -eq 64 -and $trimmed -match '^[0-9A-Fa-f]{64}$'
}

function New-RandomBase64Key {
    param([Parameter(Mandatory)][int]$ByteCount)

    $bytes = New-Object byte[] $ByteCount
    $randomNumberGenerator = $null
    try {
        $randomNumberGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
        $randomNumberGenerator.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes)
    }
    finally {
        if ($null -ne $randomNumberGenerator) {
            $randomNumberGenerator.Dispose()
        }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function New-RandomHexKey {
    param([Parameter(Mandatory)][int]$ByteCount)

    $bytes = New-Object byte[] $ByteCount
    $randomNumberGenerator = $null
    try {
        $randomNumberGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
        $randomNumberGenerator.GetBytes($bytes)
        return ([BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $randomNumberGenerator) {
            $randomNumberGenerator.Dispose()
        }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    $sha256 = $null
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        return ([BitConverter]::ToString($sha256.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $sha256) {
            $sha256.Dispose()
        }
    }
}

function Get-MavlinkBytes {
    param([Parameter(Mandatory)][string]$Hex)

    $trimmed = $Hex.Trim()
    $bytes = New-Object byte[] ($trimmed.Length / 2)
    for ($index = 0; $index -lt $bytes.Length; $index++) {
        $bytes[$index] = [Convert]::ToByte($trimmed.Substring($index * 2, 2), 16)
    }
    # Keep the byte[] intact for the SHA-256 API and cleanup caller.
    return ,$bytes
}

$existingOperatorKey = [Environment]::GetEnvironmentVariable($operatorKeyName, "User")
$existingMavlinkKey = [Environment]::GetEnvironmentVariable($mavlinkKeyName, "User")
$reuseOperatorKey = (-not $Force) -and (Test-OperatorKey -Value $existingOperatorKey)
$reuseMavlinkKey = (-not $Force) -and (Test-MavlinkKey -Value $existingMavlinkKey)

$operatorAction = if ($reuseOperatorKey) { "reused" } else { "created" }
$mavlinkAction = if ($reuseMavlinkKey) { "reused" } else { "created" }

$operatorKey = if ($reuseOperatorKey) {
    [string]$existingOperatorKey
}
else {
    New-RandomBase64Key -ByteCount $OperatorKeyBytes
}

$mavlinkKey = if ($reuseMavlinkKey) {
    ([string]$existingMavlinkKey).Trim().ToLowerInvariant()
}
else {
    New-RandomHexKey -ByteCount $MavlinkKeyBytes
}

$verifiedOperatorBytes = $null
$verifiedMavlinkBytes = $null
try {
    if (-not $reuseOperatorKey) {
        [Environment]::SetEnvironmentVariable($operatorKeyName, $operatorKey, "User")
    }
    if (-not $reuseMavlinkKey) {
        [Environment]::SetEnvironmentVariable($mavlinkKeyName, $mavlinkKey, "User")
    }

    $verifiedOperatorKey = [Environment]::GetEnvironmentVariable($operatorKeyName, "User")
    $verifiedMavlinkKey = [Environment]::GetEnvironmentVariable($mavlinkKeyName, "User")
    if (-not (Test-OperatorKey -Value $verifiedOperatorKey)) {
        throw "$operatorKeyName was not persisted as a valid user-scoped value"
    }
    if (-not (Test-MavlinkKey -Value $verifiedMavlinkKey)) {
        throw "$mavlinkKeyName was not persisted as a valid user-scoped value"
    }

    $verifiedOperatorBytes = Get-Utf8Bytes -Value ([string]$verifiedOperatorKey)
    $verifiedMavlinkBytes = Get-MavlinkBytes -Hex ([string]$verifiedMavlinkKey)
    $status = [ordered]@{
        schema_version = 1
        scope = "User"
        operator_key = [ordered]@{
            name = $operatorKeyName
            action = $operatorAction
            utf8_bytes = $verifiedOperatorBytes.Length
            sha256 = Get-Sha256Hex -Bytes $verifiedOperatorBytes
        }
        mavlink_signing_key = [ordered]@{
            name = $mavlinkKeyName
            action = $mavlinkAction
            bytes = $verifiedMavlinkBytes.Length
            sha256 = Get-Sha256Hex -Bytes $verifiedMavlinkBytes
        }
        force_rotation = [bool]$Force
        secrets_printed = $false
        restart_required = $true
    }

    if ($PassThru) {
        $status | ConvertTo-Json -Depth 5
    }
    else {
        Write-Output ("Multi-Detect user credentials ready (operator: {0}; MAVLink: {1})." -f `
            $operatorAction, $mavlinkAction)
        Write-Output "Secrets were not printed. Restart the terminal and QGC before signed live control."
    }
}
finally {
    if ($null -ne $verifiedOperatorBytes) {
        [Array]::Clear($verifiedOperatorBytes, 0, $verifiedOperatorBytes.Length)
    }
    if ($null -ne $verifiedMavlinkBytes) {
        [Array]::Clear($verifiedMavlinkBytes, 0, $verifiedMavlinkBytes.Length)
    }
    $operatorKey = $null
    $mavlinkKey = $null
    $existingOperatorKey = $null
    $existingMavlinkKey = $null
    $verifiedOperatorKey = $null
    $verifiedMavlinkKey = $null
}

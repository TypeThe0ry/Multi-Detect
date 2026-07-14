[CmdletBinding()]
param(
    [string]$QgcPath = "",
    [string]$Gr01Host = "192.168.144.11",
    [ValidateRange(1, 65535)]
    [int]$Gr01Port = 5760,
    [ValidateRange(1, 65535)]
    [int]$QgcUdpPort = 14550,
    [ValidateRange(1, 65535)]
    [int]$BridgeUdpPort = 14560,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $repoRoot "artifacts\evaluation"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bridgeOutput = Join-Path $logDirectory "qgc-gr01-readonly-$timestamp.json"
$bridgeError = Join-Path $logDirectory "qgc-gr01-readonly-$timestamp.stderr.log"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Multi-Detect Python environment was not found: $pythonPath"
}
if ([string]::IsNullOrWhiteSpace($QgcPath)) {
    $githubRoot = Split-Path -Parent $repoRoot
    $qgcCandidates = @(
        (Join-Path $githubRoot "QGroundControl-MultiDetect\build-multidetect-release\staging\bin\MultiDetectGCS.exe")
        (Join-Path $env:USERPROFILE "Apps\QGroundControl-Daily-2026-07-10\bin\QGroundControl.exe")
        (Join-Path $env:ProgramFiles "QGroundControl\bin\QGroundControl.exe")
    )
    $QgcPath = $qgcCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($QgcPath)) {
        throw "No deployed QGroundControl executable was found. Checked: $($qgcCandidates -join ', ')"
    }
}
elseif (-not (Test-Path -LiteralPath $QgcPath -PathType Leaf)) {
    throw "QGroundControl executable was not found: $QgcPath"
}
$QgcPath = (Resolve-Path -LiteralPath $QgcPath).Path
if ($QgcUdpPort -eq $BridgeUdpPort) {
    throw "QGC and bridge UDP ports must be different."
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

$probe = [System.Net.Sockets.TcpClient]::new()
try {
    $connectTask = $probe.ConnectAsync($Gr01Host, $Gr01Port)
    if (-not $connectTask.Wait([TimeSpan]::FromSeconds(5))) {
        throw "GR01 TCP probe timed out at ${Gr01Host}:$Gr01Port"
    }
    if (-not $probe.Connected) {
        throw "GR01 TCP probe failed at ${Gr01Host}:$Gr01Port"
    }
}
finally {
    $probe.Dispose()
}

Write-Host "GR01 TCP is reachable at ${Gr01Host}:$Gr01Port" -ForegroundColor Green
Write-Host "Mode: read-only QGC diagnostics (parameter writes, flight commands, missions, actuators and payload commands are blocked)." -ForegroundColor Yellow
Write-Host "QGC executable: $QgcPath" -ForegroundColor Cyan

if ($ValidateOnly) {
    Write-Host "Launcher validation passed; QGC and bridge were not started." -ForegroundColor Green
    exit 0
}

$bridgeArguments = @(
    "-m", "multidetect.qgc_readonly_bridge",
    "--gr01-host", $Gr01Host,
    "--gr01-port", $Gr01Port,
    "--qgc-host", "127.0.0.1",
    "--qgc-port", $QgcUdpPort,
    "--local-udp-port", $BridgeUdpPort
)

$bridgeProcess = $null
$qgcProcess = $null
try {
    $bridgeProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList $bridgeArguments `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $bridgeOutput `
        -RedirectStandardError $bridgeError `
        -PassThru

    Start-Sleep -Milliseconds 750
    if ($bridgeProcess.HasExited) {
        $errorText = if (Test-Path -LiteralPath $bridgeError) {
            Get-Content -Raw -LiteralPath $bridgeError
        }
        else {
            "No bridge error log was produced."
        }
        throw "The GR01 read-only bridge exited during startup: $errorText"
    }

    $qgcProcessName = [IO.Path]::GetFileNameWithoutExtension($QgcPath)
    $qgcProcess = Get-Process -Name $qgcProcessName -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $QgcPath } |
        Select-Object -First 1
    if ($null -eq $qgcProcess) {
        $qgcProcess = Start-Process -FilePath $QgcPath -WorkingDirectory (Split-Path -Parent $QgcPath) -PassThru
        Write-Host "QGroundControl started. It will auto-detect V6X over local UDP." -ForegroundColor Green
    }
    else {
        Write-Host "Using the already-running QGroundControl process. V6X will appear over local UDP." -ForegroundColor Green
    }
    Write-Host "Close QGroundControl to stop the read-only bridge." -ForegroundColor Cyan
    Wait-Process -Id $qgcProcess.Id
}
finally {
    if ($null -ne $bridgeProcess -and -not $bridgeProcess.HasExited) {
        Stop-Process -Id $bridgeProcess.Id -Force
        Wait-Process -Id $bridgeProcess.Id -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $bridgeError) {
        $errorInfo = Get-Item -LiteralPath $bridgeError
        if ($errorInfo.Length -eq 0) {
            Remove-Item -LiteralPath $bridgeError -Force
        }
    }
    if (Test-Path -LiteralPath $bridgeOutput) {
        $outputInfo = Get-Item -LiteralPath $bridgeOutput
        if ($outputInfo.Length -eq 0) {
            Remove-Item -LiteralPath $bridgeOutput -Force
        }
    }
}

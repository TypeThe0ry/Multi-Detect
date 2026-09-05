[CmdletBinding()]
param(
    # The default is a read-only inventory.  State changes require -Apply.
    [switch]$Apply,

    # Moving the reparse point is attempted first.  This switch explicitly
    # permits the last-resort fsutil/remove path when Windows refuses to move
    # an orphaned AF_UNIX socket with WinError 1920.
    [switch]$RemoveStaleEntry,

    # Last-resort reversible fallback: move the exact Docker run directory to
    # a sibling backup and recreate an empty run directory.  Nothing is
    # deleted; all existing runtime entries remain in the backup.
    [switch]$RotateRunDirectory,

    # Restart Docker Desktop only after the selected entry has been handled.
    [switch]$RestartDocker
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Docker Desktop stale-socket recovery is only supported on Windows.'
}
if (-not $Apply -and ($RemoveStaleEntry -or $RotateRunDirectory -or $RestartDocker)) {
    throw '-RemoveStaleEntry, -RotateRunDirectory and -RestartDocker require -Apply.'
}

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw 'The current account has no LocalApplicationData directory.'
}
$runRoot = [IO.Path]::GetFullPath((Join-Path $localAppData 'Docker\run'))
if (-not (Test-Path -LiteralPath $runRoot -PathType Container)) {
    throw "Docker run directory is missing: $runRoot"
}

$entryName = 'dockerInference'
$entryPath = [IO.Path]::GetFullPath((Join-Path $runRoot $entryName))
$records = @()
try {
    $item = Get-Item -LiteralPath $entryPath -Force -ErrorAction Stop
    if (-not [string]::Equals(
            $item.DirectoryName,
            $runRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Unexpected parent directory for recovery target: $($item.FullName)"
    }
    $isReparse = (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    $records += [ordered]@{
        name = $entryName
        path = $item.FullName
        present = $true
        reparse_point = $isReparse
        attributes = [string]$item.Attributes
        bytes = $item.Length
        error = $null
    }
}
catch {
    # Get-Item can still surface a WinError 1920 for a stale AF_UNIX entry.  The
    # path is retained in the report, but no recovery action is guessed from a
    # failed probe.
    $records += [ordered]@{
        name = $entryName
        path = $entryPath
        present = $null
        reparse_point = $null
        attributes = $null
        bytes = $null
        error = $_.Exception.Message
    }
}

Write-Output 'Docker Desktop stale-entry inventory:'
$records | ConvertTo-Json -Depth 6

$present = @($records | Where-Object { $_.present -eq $true })
if (-not $Apply) {
    Write-Output 'Read-only mode: no process, reparse point, or Docker data was changed.'
    exit 0
}

$probeFailures = @($records | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_.error) })
if ($probeFailures.Count -gt 0) {
    throw (
        "Unable to inspect the exact Docker entry before applying recovery: " +
        (($probeFailures | ForEach-Object { $_.error }) -join '; ')
    )
}

$dockerProcesses = @(
    Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue
)
if ($dockerProcesses.Count -gt 0) {
    $pids = ($dockerProcesses | Select-Object -ExpandProperty Id) -join ','
    throw "Docker Desktop is still running (PID $pids). Exit it completely, then rerun with -Apply."
}

if ($present.Count -eq 0) {
    Write-Output 'No readable dockerInference entry remains; nothing to recover.'
}
else {
    $targetRecord = $present[0]
    if ($targetRecord.reparse_point -ne $true) {
        throw "Refusing to touch a non-reparse entry: $($targetRecord.path)"
    }
    if ($RotateRunDirectory) {
        # Some Windows AF_UNIX reparse entries return WinError 1920 even to
        # fsutil/DeleteFile.  Rotating the already validated Docker runtime
        # directory keeps every entry recoverable and gives Docker a clean
        # directory in which to recreate its sockets.
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
        $dockerRoot = Split-Path -Parent $runRoot
        $backupRoot = Join-Path $dockerRoot "run-recovery-backup-$stamp"
        if (Test-Path -LiteralPath $backupRoot) {
            throw "Refusing to overwrite an existing recovery directory: $backupRoot"
        }
        $entryNames = @(
            Get-ChildItem -LiteralPath $runRoot -Force -ErrorAction Stop |
                Select-Object -ExpandProperty Name
        )
        Move-Item -LiteralPath $runRoot -Destination $backupRoot -Force
        New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
        Write-Output "Rotated exact Docker run directory to reversible backup: $backupRoot"
        Write-Output ("Preserved entries: " + (($entryNames | Sort-Object) -join ', '))
    }
    else {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
        $backupRoot = Join-Path $runRoot "recovery-backup-$stamp"
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $backupPath = Join-Path $backupRoot $entryName
        try {
            Move-Item -LiteralPath $targetRecord.path -Destination $backupPath -Force
            Write-Output "Moved stale entry to reversible backup: $backupPath"
        }
        catch {
            if (-not $RemoveStaleEntry) {
                throw (
                    "Windows refused to move $($targetRecord.path): $($_.Exception.Message). " +
                    'After confirming Docker Desktop is stopped, rerun with ' +
                    '-Apply -RemoveStaleEntry to clear only this exact reparse entry, or ' +
                    '-Apply -RotateRunDirectory for a reversible runtime-directory rotation.'
                )
            }
            & fsutil.exe reparsepoint delete $targetRecord.path
            if ($LASTEXITCODE -ne 0) {
                throw "fsutil could not clear the stale reparse point (exit code $LASTEXITCODE): $($targetRecord.path)"
            }
            Remove-Item -LiteralPath $targetRecord.path -Force
            Write-Output "Removed only the confirmed stale entry: $($targetRecord.path)"
        }
    }
}

if ($RestartDocker) {
    $dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $dockerDesktop -PathType Leaf)) {
        throw "Docker Desktop executable was not found: $dockerDesktop"
    }
    Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
    Write-Output 'Docker Desktop restart requested.'
}

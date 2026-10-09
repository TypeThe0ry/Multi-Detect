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

    # Keep the historical dockerInference default, while allowing a confirmed
    # Docker Desktop AF_UNIX entry such as sailor-ingest.sock to be selected.
    [ValidateSet('dockerInference', 'sailor-ingest.sock', 'sailor-ingest.sock.stale')]
    [string]$EntryName = 'dockerInference',

    # Use the exact socket path reported by backend.error.json when it lives
    # outside Docker\run (for example docker-secrets-engine\engine.sock).
    [string]$EntryPath,

    # Reversible fallback for a validated exact socket parent directory.
    [switch]$RotateTargetDirectory,

    # Restart Docker Desktop only after the selected entry has been handled.
    [switch]$RestartDocker
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:OS -ne 'Windows_NT') {
    throw 'Docker Desktop stale-socket recovery is only supported on Windows.'
}
if (-not $Apply -and ($RemoveStaleEntry -or $RotateRunDirectory -or $RotateTargetDirectory -or $RestartDocker)) {
    throw '-RemoveStaleEntry, -RotateRunDirectory, -RotateTargetDirectory and -RestartDocker require -Apply.'
}
if ($RotateRunDirectory -and $RotateTargetDirectory) {
    throw 'Choose only one of -RotateRunDirectory or -RotateTargetDirectory.'
}
if ($RotateTargetDirectory -and [string]::IsNullOrWhiteSpace($EntryPath)) {
    throw '-RotateTargetDirectory requires -EntryPath.'
}

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw 'The current account has no LocalApplicationData directory.'
}
$localRoot = [IO.Path]::GetFullPath($localAppData).TrimEnd('\')
$runRoot = [IO.Path]::GetFullPath((Join-Path $localAppData 'Docker\run'))
$targetRoot = $runRoot
if ([string]::IsNullOrWhiteSpace($EntryPath)) {
    if (-not (Test-Path -LiteralPath $runRoot -PathType Container)) {
        throw "Docker run directory is missing: $runRoot"
    }
    $selectedEntryName = $EntryName
    $entryPath = [IO.Path]::GetFullPath((Join-Path $runRoot $selectedEntryName))
}
else {
    $entryPath = [IO.Path]::GetFullPath($EntryPath)
    $localPrefix = $localRoot + '\'
    if (-not $entryPath.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing a recovery target outside LocalApplicationData: $entryPath"
    }
    $targetRoot = Split-Path -Parent $entryPath
    $selectedEntryName = Split-Path -Leaf $entryPath
    if ([string]::IsNullOrWhiteSpace($selectedEntryName) -or $targetRoot -eq $localRoot) {
        throw "Refusing an invalid exact recovery target: $entryPath"
    }
    if (-not (Test-Path -LiteralPath $targetRoot -PathType Container)) {
        throw "Socket parent directory is missing: $targetRoot"
    }
}
$records = @()
try {
    $item = Get-Item -LiteralPath $entryPath -Force -ErrorAction Stop
    if (-not [string]::Equals(
            $item.DirectoryName,
            $targetRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Unexpected parent directory for recovery target: $($item.FullName)"
    }
    $isReparse = (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    $records += [ordered]@{
        name = $selectedEntryName
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
        name = $selectedEntryName
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
    Write-Output "No readable $selectedEntryName entry remains; nothing to recover."
}
else {
    $targetRecord = $present[0]
    if ($targetRecord.reparse_point -ne $true) {
        throw "Refusing to touch a non-reparse entry: $($targetRecord.path)"
    }
    if ($RotateRunDirectory -or $RotateTargetDirectory) {
        # Some Windows AF_UNIX reparse entries return WinError 1920 even to
        # fsutil/DeleteFile.  Rotating the already validated Docker runtime
        # directory keeps every entry recoverable and gives Docker a clean
        # directory in which to recreate its sockets.
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
        $rotationParent = Split-Path -Parent $targetRoot
        $rotationName = Split-Path -Leaf $targetRoot
        $backupRoot = Join-Path $rotationParent "$rotationName-recovery-backup-$stamp"
        if (Test-Path -LiteralPath $backupRoot) {
            throw "Refusing to overwrite an existing recovery directory: $backupRoot"
        }
        $entryNames = @(
            Get-ChildItem -LiteralPath $targetRoot -Force -ErrorAction Stop |
                Select-Object -ExpandProperty Name
        )
        Move-Item -LiteralPath $targetRoot -Destination $backupRoot -Force
        New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
        Write-Output "Rotated exact Docker socket directory to reversible backup: $backupRoot"
        Write-Output ("Preserved entries: " + (($entryNames | Sort-Object) -join ', '))
    }
    else {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
        $backupRoot = Join-Path $targetRoot "recovery-backup-$stamp"
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        $backupPath = Join-Path $backupRoot $selectedEntryName
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

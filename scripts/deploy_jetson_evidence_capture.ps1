[CmdletBinding()]
param(
    [string]$HostAlias = "192.168.144.20",
    [string]$RemoteToolsRoot = "/home/jetson/Multi-Detect-evidence-tools",
    [string]$RuntimePython = "/home/jetson/Multi-Detect/.venv/bin/python",
    [string]$DeploymentId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"),
    [string]$OutputDirectory = "build/jetson-evidence-deploy",
    [switch]$PackageOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tar = Get-Command tar.exe -ErrorAction Stop

if ($DeploymentId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$') {
    throw "DeploymentId must contain only letters, digits, '.', '_' or '-'."
}
if ($RemoteToolsRoot -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteToolsRoot must be an absolute POSIX path without spaces."
}
if ($RuntimePython -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RuntimePython must be an absolute POSIX path without spaces."
}
if (-not $PackageOnly -and $HostAlias -notmatch '^[A-Za-z0-9._:-]+$') {
    throw "HostAlias contains unsupported characters."
}

$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $workspace $OutputDirectory))
}
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$archiveName = "multidetect-evidence-tools-$DeploymentId.tar"
$archivePath = Join-Path $outputRoot $archiveName
$receiptPath = Join-Path $outputRoot "multidetect-evidence-tools-$DeploymentId.package.json"
$installReceiptPath = Join-Path $outputRoot "multidetect-evidence-tools-$DeploymentId.install.json"
$remoteOutputPath = Join-Path $outputRoot "multidetect-evidence-tools-$DeploymentId.remote-output.txt"
if ((Test-Path -LiteralPath $archivePath) -or (Test-Path -LiteralPath $receiptPath)) {
    throw "Deployment package output already exists for DeploymentId '$DeploymentId'."
}
if ((Test-Path -LiteralPath $installReceiptPath) -or
    (Test-Path -LiteralPath $remoteOutputPath)) {
    throw "Deployment installation output already exists for DeploymentId '$DeploymentId'."
}

$temporaryRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) "multidetect-evidence-deploy-$DeploymentId-$([guid]::NewGuid().ToString('N'))"
$payloadRoot = Join-Path $temporaryRoot "payload"

try {
    New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null
    $relativeFiles = [System.Collections.Generic.List[string]]::new()
    $relativeFiles.Add("configs/product-version.json")
    $relativeFiles.Add("scripts/record_jetson_rtsp_evidence.sh")
    Get-ChildItem -LiteralPath (Join-Path $workspace "src/multidetect") -Recurse -File |
        Where-Object {
            $_.Extension -eq ".py" -and
            $_.FullName -notmatch '[\\/]__pycache__[\\/]'
        } |
        ForEach-Object {
            $relative = [System.IO.Path]::GetRelativePath($workspace, $_.FullName)
            $relativeFiles.Add($relative.Replace('\', '/'))
        }

    $manifest = [System.Collections.Generic.List[string]]::new()
    foreach ($relativePath in ($relativeFiles | Sort-Object -Unique)) {
        $sourcePath = Join-Path $workspace $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Deployment input is missing: $relativePath"
        }
        $destinationPath = Join-Path $payloadRoot $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) -Force |
            Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath
        $hash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifest.Add("$hash  $relativePath")
    }
    $manifestPath = Join-Path $payloadRoot "source-sha256.txt"
    [System.IO.File]::WriteAllText(
        $manifestPath,
        (($manifest -join "`n") + "`n"),
        [System.Text.Encoding]::ASCII
    )

    & $tar.Source -cf $archivePath -C $payloadRoot .
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the Jetson evidence deployment archive failed: $LASTEXITCODE"
    }
    $archiveItem = Get-Item -LiteralPath $archivePath
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $archiveEntries = @(& $tar.Source -tf $archivePath)
    if ($LASTEXITCODE -ne 0 -or $archiveEntries.Count -eq 0) {
        throw "The generated evidence deployment archive cannot be listed."
    }
    foreach ($entry in $archiveEntries) {
        $normalizedEntry = $entry.Replace('\', '/')
        if ($normalizedEntry.StartsWith('./')) {
            $normalizedEntry = $normalizedEntry.Substring(2)
        }
        if ([string]::IsNullOrWhiteSpace($normalizedEntry)) {
            continue
        }
        if ($normalizedEntry.StartsWith('/') -or
            $normalizedEntry.Split('/') -contains '..') {
            throw "Unsafe path in generated deployment archive: $entry"
        }
    }
    $verificationRoot = Join-Path $temporaryRoot "verification"
    New-Item -ItemType Directory -Path $verificationRoot | Out-Null
    & $tar.Source -xf $archivePath -C $verificationRoot
    if ($LASTEXITCODE -ne 0) {
        throw "The generated evidence deployment archive cannot be extracted."
    }
    $verifiedManifestPath = Join-Path $verificationRoot "source-sha256.txt"
    $verifiedManifestHash = (
        Get-FileHash -LiteralPath $verifiedManifestPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $expectedManifestHash = (
        Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($verifiedManifestHash -ne $expectedManifestHash) {
        throw "The packaged source manifest SHA-256 differs from its source."
    }
    $verifiedFileCount = 0
    foreach ($line in Get-Content -LiteralPath $verifiedManifestPath) {
        if ($line -notmatch '^([0-9a-f]{64})  ([A-Za-z0-9._/-]+)$') {
            throw "Invalid source manifest line in generated deployment package."
        }
        $expectedHash = $Matches[1]
        $relativePath = $Matches[2]
        if ($relativePath.StartsWith('/') -or $relativePath.Split('/') -contains '..') {
            throw "Unsafe source manifest path in generated deployment package."
        }
        $candidate = [System.IO.Path]::GetFullPath((Join-Path $verificationRoot $relativePath))
        $verificationPrefix = [System.IO.Path]::GetFullPath($verificationRoot) + `
            [System.IO.Path]::DirectorySeparatorChar
        if (-not $candidate.StartsWith(
                $verificationPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Packaged deployment file is missing or outside its root: $relativePath"
        }
        $actualHash = (
            Get-FileHash -LiteralPath $candidate -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "Packaged deployment file SHA-256 mismatch: $relativePath"
        }
        $verifiedFileCount += 1
    }
    $actualPackageFiles = @(
        Get-ChildItem -LiteralPath $verificationRoot -Recurse -File
    )
    if ($verifiedFileCount -ne $manifest.Count -or
        $actualPackageFiles.Count -ne ($manifest.Count + 1)) {
        throw "Generated deployment package file inventory is not closed."
    }
    $sourceHead = (& git -C $workspace rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceHead -notmatch '^[0-9a-f]{40}$') {
        throw "Unable to resolve the deployment source Git commit."
    }
    $sourceDirty = [bool](& git -C $workspace status --porcelain=v1)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the deployment source worktree."
    }
    $receipt = [ordered]@{
        schema_version = 1
        event = "jetson_evidence_capture_package_created"
        deployment_id = $DeploymentId
        created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        source_git_head = $sourceHead
        source_dirty = $sourceDirty
        archive_name = $archiveName
        archive_bytes = $archiveItem.Length
        archive_sha256 = $archiveHash
        file_count = $manifest.Count
        source_manifest_sha256 = (
            Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        package_self_verified = $true
        verified_file_count = $verifiedFileCount
        remote_tools_root = $RemoteToolsRoot
        runtime_python = $RuntimePython
        package_only = [bool]$PackageOnly
        service_restart_requested = $false
        sudo_used = $false
        release_ready = $false
    }
    [System.IO.File]::WriteAllText(
        $receiptPath,
        (($receipt | ConvertTo-Json -Depth 5) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )

    if ($PackageOnly) {
        Write-Output "JETSON_EVIDENCE_PACKAGE=$archivePath"
        Write-Output "JETSON_EVIDENCE_PACKAGE_RECEIPT=$receiptPath"
        return
    }

    $ssh = Get-Command ssh.exe -ErrorAction Stop
    $scp = Get-Command scp.exe -ErrorAction Stop
    & $scp.Source -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=1 `
        $archivePath "${HostAlias}:/tmp/$archiveName"
    if ($LASTEXITCODE -ne 0) {
        throw "Copying the Jetson evidence deployment archive failed: $LASTEXITCODE"
    }

    $remoteScript = @"
set -euo pipefail
tools_root='$RemoteToolsRoot'
deployment_id='$DeploymentId'
runtime_python='$RuntimePython'
archive='/tmp/$archiveName'
target="`$tools_root/`$deployment_id"
stage="`$tools_root/.`$deployment_id.partial"

cleanup() {
    rm -rf -- "`$stage"
}
trap cleanup EXIT
test -x "`$runtime_python"
test -f "`$archive"
test ! -e "`$target"
test ! -e "`$stage"
mkdir -p "`$stage"
tar -xf "`$archive" -C "`$stage"
rm -f -- "`$archive"
cd "`$stage"
sha256sum --check source-sha256.txt
"`$runtime_python" -m compileall -q src
bash -n scripts/record_jetson_rtsp_evidence.sh
PYTHONPATH="`$stage/src" "`$runtime_python" -c 'import multidetect.rtsp_evidence_recording, multidetect.rtsp_evidence_replay, multidetect.evidence_bundle_transfer'
chmod 755 scripts/record_jetson_rtsp_evidence.sh
mv -- "`$stage" "`$target"
trap - EXIT
printf 'JETSON_EVIDENCE_TOOLS_READY=%s\n' "`$target"
printf 'JETSON_EVIDENCE_RUN=MULTIDETECT_ROOT=%s MULTIDETECT_PYTHON=%s bash %s/scripts/record_jetson_rtsp_evidence.sh\n' "`$target" "`$runtime_python" "`$target"
"@
    $remoteOutput = @(
        & $ssh.Source -o BatchMode=yes -o ConnectTimeout=20 -o ConnectionAttempts=1 `
            $HostAlias $remoteScript 2>&1 |
            ForEach-Object { $_.ToString() }
    )
    $remoteExitCode = $LASTEXITCODE
    $remoteOutputText = if ($remoteOutput.Count -eq 0) {
        ""
    } else {
        ($remoteOutput -join "`n") + "`n"
    }
    [System.IO.File]::WriteAllText(
        $remoteOutputPath,
        $remoteOutputText,
        [System.Text.UTF8Encoding]::new($false)
    )
    foreach ($line in $remoteOutput) {
        Write-Output $line
    }
    if ($remoteExitCode -ne 0) {
        $failedOutputPath = $remoteOutputPath.Replace(
            ".remote-output.txt",
            ".failed-remote-output.txt"
        )
        Move-Item -LiteralPath $remoteOutputPath -Destination $failedOutputPath
        throw "Installing isolated Jetson evidence tools failed: $remoteExitCode"
    }
    $expectedTarget = "$RemoteToolsRoot/$DeploymentId"
    $readyLines = @($remoteOutput | Where-Object { $_ -like 'JETSON_EVIDENCE_TOOLS_READY=*' })
    $runLines = @($remoteOutput | Where-Object { $_ -like 'JETSON_EVIDENCE_RUN=*' })
    if ($readyLines.Count -ne 1 -or
        $readyLines[0] -ne "JETSON_EVIDENCE_TOOLS_READY=$expectedTarget" -or
        $runLines.Count -ne 1) {
        throw "Jetson evidence deployment returned an invalid completion marker."
    }
    $remoteOutputItem = Get-Item -LiteralPath $remoteOutputPath
    $packageReceiptItem = Get-Item -LiteralPath $receiptPath
    $installReceipt = [ordered]@{
        schema_version = 1
        event = "jetson_evidence_tools_installed"
        installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        deployment_id = $DeploymentId
        host_alias = $HostAlias
        remote_target = $expectedTarget
        runtime_python = $RuntimePython
        source_git_head = $sourceHead
        package_receipt = [ordered]@{
            file_name = $packageReceiptItem.Name
            file_bytes = $packageReceiptItem.Length
            file_sha256 = (
                Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
        }
        archive_sha256 = $archiveHash
        remote_output = [ordered]@{
            file_name = $remoteOutputItem.Name
            file_bytes = $remoteOutputItem.Length
            file_sha256 = (
                Get-FileHash -LiteralPath $remoteOutputPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            ready_marker = $readyLines[0]
            run_marker_present = $true
        }
        remote_checks = [ordered]@{
            package_manifest_verified = $true
            python_compileall_passed = $true
            bash_syntax_passed = $true
            module_imports_passed = $true
            recorder_executable = $true
            atomic_install_completed = $true
        }
        service_restart_requested = $false
        sudo_used = $false
        remote_install_verified = $true
        hardware_acceptance = $false
        release_ready = $false
    }
    [System.IO.File]::WriteAllText(
        $installReceiptPath,
        (($installReceipt | ConvertTo-Json -Depth 8) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "JETSON_EVIDENCE_PACKAGE_RECEIPT=$receiptPath"
    Write-Output "JETSON_EVIDENCE_INSTALL_RECEIPT=$installReceiptPath"
} finally {
    $tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $resolvedTemporary = [System.IO.Path]::GetFullPath($temporaryRoot)
    if ($resolvedTemporary.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedTemporary)) {
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
    }
}

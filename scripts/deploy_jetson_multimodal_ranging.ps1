[CmdletBinding()]
param(
    [string]$HostAlias = "multidetect-jetson",
    [string]$RemoteRoot = "/home/jetson/Multi-Detect",
    [string]$ServiceName = "multidetect-live.service",
    [string]$DeploymentId = (Get-Date -Format "yyyyMMddTHHmmssZ"),
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ssh = Get-Command ssh.exe -ErrorAction Stop
$scp = Get-Command scp.exe -ErrorAction Stop
$tar = Get-Command tar.exe -ErrorAction Stop

# Keep the deployment deliberately narrow: this transfers only the ranging/depth
# runtime and its durable handoff note. QGC source and build artifacts stay local.
$files = @(
    "README.md",
    "scripts/run_jetson_fire_patrol.sh",
    "src/multidetect/adaptive_ranging.py",
    "src/multidetect/rgb_slam_range.py",
    "src/multidetect/cli.py",
    "src/multidetect/depth_grid_udp.py",
    "src/multidetect/live.py",
    "src/multidetect/metric_depth.py",
    "src/multidetect/multimodal_ranging.py",
    "src/multidetect/operator_link.py",
    "src/multidetect/operator_protocol.py",
    "src/multidetect/operator_status.py",
    "src/multidetect/visual_inertial_range.py"
)

if ($DeploymentId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$') {
    throw "DeploymentId must contain only letters, digits, '.', '_' or '-'."
}
if ($RemoteRoot -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteRoot must be an absolute POSIX path without spaces."
}
if ($ServiceName -notmatch '^[A-Za-z0-9@._-]+\.service$') {
    throw "ServiceName must be a systemd service unit name."
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) "multidetect-ranging-$DeploymentId"
$payloadRoot = Join-Path $temporaryRoot "payload"
$archiveName = "multidetect-ranging-$DeploymentId.tar"
$archivePath = Join-Path $temporaryRoot $archiveName

try {
    New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null
    $manifest = [System.Collections.Generic.List[string]]::new()

    foreach ($relativePath in $files) {
        $sourcePath = Join-Path $workspace $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Deployment input is missing: $relativePath"
        }
        $destinationPath = Join-Path $payloadRoot $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) -Force | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
        $hash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifest.Add("$hash  $relativePath")
    }
    Set-Content -LiteralPath (Join-Path $payloadRoot "source-sha256.txt") -Value $manifest -Encoding ascii

    & $tar.Source -cf $archivePath -C $payloadRoot .
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the Jetson deployment archive failed with exit code $LASTEXITCODE."
    }

    & $scp.Source -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=1 `
        $archivePath "${HostAlias}:/tmp/$archiveName"
    if ($LASTEXITCODE -ne 0) {
        throw "Copying the Jetson deployment archive failed with exit code $LASTEXITCODE."
    }

    $restart = if ($NoRestart) { "0" } else { "1" }
    $remoteScript = @"
set -euo pipefail
root='$RemoteRoot'
service='$ServiceName'
deployment_id='$DeploymentId'
archive='/tmp/$archiveName'
restart='$restart'
deployment_root="`$root/artifacts/deployment/`$deployment_id"
stage="`$deployment_root/stage"
backup="`$deployment_root/backup"
manifest="`$stage/source-sha256.txt"
committed=0

rollback() {
    if [[ "`$committed" != "0" || ! -f "`$manifest" ]]; then
        return
    fi
    while read -r expected relative; do
        target="`$root/`$relative"
        saved="`$backup/`$relative"
        if [[ -e "`$saved" ]]; then
            mkdir -p "`$(dirname "`$target")"
            cp -a "`$saved" "`$target"
        else
            rm -f "`$target"
        fi
    done < "`$manifest"
}
trap rollback ERR

test -f "`$archive"
rm -rf "`$stage"
mkdir -p "`$stage" "`$backup"
tar -xf "`$archive" -C "`$stage"
rm -f "`$archive"

cd "`$stage"
sha256sum --check source-sha256.txt
"`$root/.venv/bin/python" -m compileall -q src scripts
bash -n scripts/run_jetson_fire_patrol.sh

while read -r expected relative; do
    source="`$stage/`$relative"
    target="`$root/`$relative"
    saved="`$backup/`$relative"
    mkdir -p "`$(dirname "`$saved")" "`$(dirname "`$target")"
    if [[ -e "`$target" ]]; then
        cp -a "`$target" "`$saved"
    fi
    cp -a "`$source" "`$target"
done < "`$manifest"

"`$root/.venv/bin/python" -m compileall -q "`$root/src" "`$root/scripts"
PYTHONPATH="`$root/src" "`$root/.venv/bin/python" - <<'PY'
from multidetect.adaptive_ranging import AdaptiveRangingPolicy
from multidetect.rgb_slam_range import RgbSlamRangeEstimator
from multidetect.operator_protocol import decode_operator_packet

assert AdaptiveRangingPolicy().config.maximum_distance_m == 800.0
assert RgbSlamRangeEstimator().config.maximum_distance_m == 800.0
assert callable(decode_operator_packet)
PY

if [[ "`$restart" == "1" ]]; then
    sudo -n systemctl restart "`$service"
    systemctl is-active --quiet "`$service"
    systemctl show "`$service" -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager
fi

committed=1
printf 'MULTIDETECT_RANGING_DEPLOY_OK=%s\n' "`$deployment_root"
"@

    & $ssh.Source -o BatchMode=yes -o ConnectTimeout=20 -o ConnectionAttempts=1 `
        $HostAlias $remoteScript
    if ($LASTEXITCODE -ne 0) {
        throw "Jetson deployment verification failed with exit code $LASTEXITCODE."
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

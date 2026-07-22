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
    "scripts/export_outdoor_depth_calibration_candidates.py",
    "scripts/fit_outdoor_depth_calibration.py",
    "scripts/validate_outdoor_depth_calibration.py",
    "scripts/run_jetson_fire_patrol.sh",
    "src/multidetect/adaptive_ranging.py",
    "src/multidetect/depth_calibration.py",
    "src/multidetect/domain.py",
    "src/multidetect/rgb_slam_range.py",
    "src/multidetect/cli.py",
    "src/multidetect/depth_grid_udp.py",
    "src/multidetect/live.py",
    "src/multidetect/metric_depth.py",
    "src/multidetect/multimodal_ranging.py",
    "src/multidetect/operator_link.py",
    "src/multidetect/operator_bridge.py",
    "src/multidetect/operator_mavlink.py",
    "src/multidetect/operator_protocol.py",
    "src/multidetect/operator_status.py",
    "src/multidetect/operator_udp.py",
    "src/multidetect/pixhawk.py",
    "src/multidetect/target_geolocation.py",
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
    $manifestPath = Join-Path $payloadRoot "source-sha256.txt"
    [System.IO.File]::WriteAllText(
        $manifestPath,
        (($manifest -join "`n") + "`n"),
        [System.Text.Encoding]::ASCII
    )

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
    # Do not preserve Windows archive timestamps: a same-size Python edit can
    # otherwise validate against stale timestamp-based bytecode on the Jetson.
    cp -f "`$source" "`$target"
done < "`$manifest"

# Windows archive creation does not preserve the executable bit. The systemd unit
# invokes this launcher directly, so restore the required mode after every sync.
chmod 755 "`$root/scripts/run_jetson_fire_patrol.sh"
test -x "`$root/scripts/run_jetson_fire_patrol.sh"

# File mtimes can be close across Windows tar creation and the Jetson clock.
# Clear generated bytecode so the restarted service never retains a same-size stale module.
find "`$root/src/multidetect" -type d -name __pycache__ -prune -exec rm -rf {} +
"`$root/.venv/bin/python" -m compileall -q "`$root/src" "`$root/scripts"
grep -Fq 'metadata_peer_timeout_s: float = 5.0' "`$root/src/multidetect/operator_udp.py"
grep -Fq 'def encode_target_geolocation_status' "`$root/src/multidetect/operator_mavlink.py"
grep -Fq 'def _target_geolocation_status_due' "`$root/src/multidetect/operator_bridge.py"
grep -Fq 'TARGET_GEOLOCATION_STATUS = 22' "`$root/src/multidetect/operator_protocol.py"
grep -Fq 'maximum_local_position_age_s: float = 0.60' "`$root/src/multidetect/adaptive_ranging.py"
grep -Fq 'minimum_gps_fix_type: int = 3' "`$root/src/multidetect/adaptive_ranging.py"
grep -Fq 'gps_horizontal_accuracy_m' "`$root/src/multidetect/pixhawk.py"
grep -Fq 'CALIBRATION_DOCUMENT_SCHEMA_VERSION = 1' "`$root/src/multidetect/depth_calibration.py"
grep -Fq 'geometry-accepted target depth events' "`$root/scripts/export_outdoor_depth_calibration_candidates.py"
grep -Fq 'automatic_calibration_update=false' "`$root/scripts/validate_outdoor_depth_calibration.py"
grep -Fq -- '--metric-depth-calibration-document' "`$root/scripts/run_jetson_fire_patrol.sh"
grep -Fq 'minimum_range_m: float = 0.4' "`$root/src/multidetect/rgb_slam_range.py"
grep -Fq 'maximum_range_m: float = 800.0' "`$root/src/multidetect/rgb_slam_range.py"

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

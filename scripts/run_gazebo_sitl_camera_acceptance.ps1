param(
    [ValidateRange(3, 600)]
    [int]$Frames = 30,
    [ValidateRange(10, 300)]
    [int]$TimeoutSeconds = 45,
    [switch]$PersistFrames,
    [string]$ContainerName = "multidetect-gazebo-sitl-camera-acceptance",
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ProductVersionPath = Join-Path $Root "configs\product-version.json"
if (-not (Test-Path -LiteralPath $ProductVersionPath -PathType Leaf)) {
    throw "Product version manifest is unavailable: $ProductVersionPath"
}
$ProductVersion = Get-Content -LiteralPath $ProductVersionPath -Raw | ConvertFrom-Json
$ReleaseLabel = [string]$ProductVersion.release_label
if ([string]::IsNullOrWhiteSpace($ReleaseLabel) -or [string]$ProductVersion.channel -ne "prerelease") {
    throw "Gazebo acceptance requires a validated prerelease product version."
}
$ImageReference = "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
$ImageDescription = "PX4 SITL with Gazebo Harmonic simulation"
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$ArtifactDirectory = Join-Path $Root "artifacts\evaluation"
$RunDirectory = Join-Path $ArtifactDirectory "gazebo-sitl-camera-$RunId"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $ArtifactDirectory "gazebo-sitl-camera-acceptance-$RunId.json"
}
elseif (-not [IO.Path]::IsPathRooted($EvidencePath)) {
    $EvidencePath = Join-Path $Root $EvidencePath
}
$DockerCandidates = @(
    (Get-Command docker -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$DockerCandidates = @($DockerCandidates)

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-CapturedCommand {
    param([string]$FilePath, [string[]]$ArgumentList)
    $output = @(& $FilePath @ArgumentList 2>&1)
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $exitCode
        Lines = @($output | ForEach-Object { "$($_)" })
    }
}

function Get-JsonFromLines {
    param([string[]]$Lines)
    for ($index = $Lines.Count - 1; $index -ge 0; $index--) {
        $line = $Lines[$index]
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith("{")) { continue }
        try { return ($trimmed | ConvertFrom-Json) } catch { }
    }
    return $null
}

function Get-Sha256OrNull {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

Assert-True ($DockerCandidates.Count -gt 0) "Docker Desktop is unavailable; install/start Docker and retry."
$Docker = @($DockerCandidates)[0]
Assert-True (-not (Test-Path -LiteralPath $EvidencePath)) "Evidence path already exists: $EvidencePath"
Assert-True (-not (Test-Path -LiteralPath $RunDirectory)) "Artifact directory already exists: $RunDirectory"
[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null
[IO.Directory]::CreateDirectory($RunDirectory) | Out-Null

$dockerInfo = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("info", "--format", "{{.ServerVersion}}")
Assert-True ($dockerInfo.ExitCode -eq 0) "Docker Engine is not ready: $($dockerInfo.Lines -join ' ')"
$dockerServerVersion = ($dockerInfo.Lines | Select-Object -Last 1).Trim()

$imageInspect = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("image", "inspect", $ImageReference, "--format", "{{.Id}}")
if ($imageInspect.ExitCode -ne 0) {
    $pull = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("pull", $ImageReference)
    Assert-True ($pull.ExitCode -eq 0) "Unable to pull the pinned Gazebo SITL image: $($pull.Lines -join ' ')"
    $imageInspect = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("image", "inspect", $ImageReference, "--format", "{{.Id}}")
}
Assert-True ($imageInspect.ExitCode -eq 0) "Pinned Gazebo SITL image is unavailable after pull."
$imageId = ($imageInspect.Lines | Select-Object -Last 1).Trim()
$existing = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("container", "inspect", $ContainerName)
Assert-True ($existing.ExitCode -ne 0) "Container name already exists; refusing to replace it: $ContainerName"

$containerStarted = $false
$containerId = $null
$worldReady = $false
$capture = $null
$captureCommand = $null
$failure = $null
$stopCommand = $null
$capturePath = Join-Path $RunDirectory "capture.json"
$volumeRoot = "{0}:/workspace:ro" -f $Root
$volumeArtifacts = "{0}:/artifacts" -f $RunDirectory

try {
    $run = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @(
        "run", "--rm", "-d", "--name", $ContainerName,
        "--label", "multidetect.purpose=gazebo-sitl-camera-acceptance",
        "-w", "/opt/px4-gazebo",
        "-e", "HEADLESS=1",
        "-e", "PX4_SIM_MODEL=gz_x500_mono_cam",
        "-e", "PX4_GZ_WORLD=default",
        "-e", "GZ_VERBOSE=0",
        "-v", $volumeRoot,
        "-v", $volumeArtifacts,
        $ImageReference,
        "-s", "etc/init.d-posix/rcS"
    )
    Assert-True ($run.ExitCode -eq 0) "Gazebo SITL container failed to start: $($run.Lines -join ' ')"
    $containerId = ($run.Lines | Select-Object -Last 1).Trim()
    Assert-True (-not [string]::IsNullOrWhiteSpace($containerId)) "Docker returned an empty container ID."
    $containerStarted = $true

    for ($attempt = 1; $attempt -le 45; $attempt++) {
        $state = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("inspect", $ContainerName, "--format", "{{.State.Status}} {{.State.ExitCode}}")
        Assert-True ($state.ExitCode -eq 0) "Gazebo SITL container disappeared before world readiness."
        $logs = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("logs", "--tail", "80", $ContainerName)
        if ($logs.Lines -match "Gazebo world is ready" -and $logs.Lines -match "Spawning Gazebo model") {
            $worldReady = $true
            break
        }
        if (($state.Lines -join " ") -match "exited") {
            throw "Gazebo SITL exited before world readiness: $($logs.Lines -join ' ')"
        }
        Start-Sleep -Seconds 1
    }
    Assert-True $worldReady "Gazebo world did not become ready within 45 seconds."

    $captureArguments = @(
        "exec", $ContainerName, "python3", "/workspace/scripts/gazebo_camera_capture.py",
        "--frames", "$Frames", "--timeout-seconds", "$TimeoutSeconds", "--output", "/artifacts"
    )
    if ($PersistFrames) { $captureArguments += "--persist-frames" }
    $captureCommand = Invoke-CapturedCommand -FilePath $Docker -ArgumentList $captureArguments
    $capture = Get-JsonFromLines -Lines $captureCommand.Lines
    Assert-True ($captureCommand.ExitCode -eq 0) "Gazebo camera capture failed: $($captureCommand.Lines -join ' ')"
    Assert-True ($null -ne $capture) "Gazebo camera capture did not emit a JSON result."
    Assert-True (Test-Path -LiteralPath $capturePath) "Gazebo camera capture.json is missing."
    Assert-True ([bool]$capture.gate_passed) "Gazebo camera capture gate did not pass."
}
catch {
    $failure = $_.Exception.Message
}
finally {
    if ($containerStarted) {
        $stopCommand = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("stop", "--time", "2", $ContainerName)
    }
}

$previewFiles = @(Get-ChildItem -LiteralPath $RunDirectory -Filter "frame-*.ppm" -File -ErrorAction SilentlyContinue | Sort-Object Name)
$persistedFiles = @(Get-ChildItem -LiteralPath $RunDirectory -Filter "sensor-frame-*.ppm" -File -ErrorAction SilentlyContinue | Sort-Object Name)
$result = [ordered]@{
    schema = "multidetect.gazebo-sitl-camera-acceptance"
    schema_version = 1
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    gate_passed = ($null -eq $failure -and $worldReady -and $null -ne $capture -and [bool]$capture.gate_passed)
    failure = $failure
    docker = [ordered]@{ executable = $Docker; server_version = $dockerServerVersion; image = $ImageReference; image_id = $imageId; image_description = $ImageDescription }
    container = [ordered]@{ name = $ContainerName; id = $containerId; started = $containerStarted; world_ready = $worldReady; stopped = ($null -ne $stopCommand -and $stopCommand.ExitCode -eq 0); scope = "owned disposable Gazebo/PX4 SITL container only" }
    scenario = [ordered]@{ world = "default"; px4_sim_model = "gz_x500_mono_cam"; model_instance = "x500_mono_cam_0"; camera_topic = "/world/default/model/x500_mono_cam_0/link/camera_link/sensor/camera/image"; source = "gazebo://default/x500_mono_cam_0/camera" }
    capture = $capture
    capture_command_exit_code = if ($null -ne $captureCommand) { $captureCommand.ExitCode } else { $null }
    artifact_directory = $RunDirectory
    capture_sha256 = Get-Sha256OrNull -Path $capturePath
    preview_files = @($previewFiles | ForEach-Object { [ordered]@{ path = $_.FullName; bytes = $_.Length; sha256 = (Get-Sha256OrNull -Path $_.FullName) } })
    persisted_frame_files = @($persistedFiles | ForEach-Object { [ordered]@{ path = $_.FullName; bytes = $_.Length; sha256 = (Get-Sha256OrNull -Path $_.FullName) } })
    persist_frames = [bool]$PersistFrames
    real_camera_contacted = $false
    real_pixhawk_contacted = $false
    hardware_control_enabled = $false
    transport_sync_complete = $false
    operator_visual_acceptance = "not_claimed"
    counted_as_qgc_e4_gate = $false
    release_status = "v$ReleaseLabel; final v$($ProductVersion.numeric) remains blocked"
}
$result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
Write-Output ($result | ConvertTo-Json -Depth 12 -Compress)
if (-not $result.gate_passed) { throw ($failure ?? "Gazebo SITL camera acceptance failed.") }

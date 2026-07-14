param(
    [ValidatePattern("^\d+$")]
    [string]$CameraSource = "0",
    [ValidateSet("auto", "dshow", "msmf", "ffmpeg", "gstreamer")]
    [string]$CameraBackend = "dshow",
    [ValidateRange(600, 1200)]
    [int]$LiveFrames = 900,
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Executable = Join-Path $Root ".venv\Scripts\multi-detect.exe"
$MissionUploader = Join-Path $Root "scripts\px4_sitl_mission_uploader.py"
$MissionConfig = Join-Path $Root "configs\missions\fire_patrol.demo.json"
$SyntheticModel = Join-Path $Root "artifacts\synthetic-hil\synthetic-fire-nx6-hil.onnx"
$SyntheticManifest = Join-Path $Root "artifacts\synthetic-hil\synthetic-fire-nx6-hil.manifest.json"
$ImageReference = "px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216"
$ImageReleaseContext = "v1.18.0-beta1"
$ContainerName = "multidetect-px4-auto-mission-acceptance"
$PurposeLabel = "px4-sitl-auto-mission-acceptance"
$SitlPort = 14652
$ProtectedGroundStationPort = 14550
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$ArtifactDirectory = Join-Path $Root "artifacts\evaluation"
$AuditPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-auto-mission-$RunId.audit.jsonl"
$PredictionPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-auto-mission-$RunId.predictions.jsonl"
$LiveStdoutPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-auto-mission-$RunId.stdout.jsonl"
$LiveStderrPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-auto-mission-$RunId.stderr.log"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-auto-mission-acceptance-$RunId.json"
}
elseif (-not [IO.Path]::IsPathRooted($EvidencePath)) {
    $EvidencePath = Join-Path $Root $EvidencePath
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-CapturedCommand {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    $output = @(& $FilePath @ArgumentList 2>&1)
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $exitCode
        Lines = @($output | ForEach-Object { "$_" })
    }
}

function Get-JsonEvent {
    param(
        [string[]]$Lines,
        [string]$Event
    )
    $matches = @()
    foreach ($line in $Lines) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith("{")) {
            continue
        }
        try {
            $record = $trimmed | ConvertFrom-Json
        }
        catch {
            continue
        }
        if ($record.event -eq $Event) {
            $matches += $record
        }
    }
    Assert-True ($matches.Count -gt 0) "Command output did not contain JSON event '$Event'."
    return $matches[-1]
}

function Get-UdpPortOwners {
    param([int]$Port)
    if (-not (Get-Command Get-NetUDPEndpoint -ErrorAction SilentlyContinue)) {
        return @()
    }
    return @(
        Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Sort-Object
    )
}

function Get-HaversineDistanceM {
    param(
        [double]$Latitude1,
        [double]$Longitude1,
        [double]$Latitude2,
        [double]$Longitude2
    )
    $earthRadiusM = 6371008.8
    $toRadians = [Math]::PI / 180.0
    $latitudeDelta = ($Latitude2 - $Latitude1) * $toRadians
    $longitudeDelta = ($Longitude2 - $Longitude1) * $toRadians
    $a = [Math]::Sin($latitudeDelta / 2.0) * [Math]::Sin($latitudeDelta / 2.0) +
        [Math]::Cos($Latitude1 * $toRadians) * [Math]::Cos($Latitude2 * $toRadians) *
        [Math]::Sin($longitudeDelta / 2.0) * [Math]::Sin($longitudeDelta / 2.0)
    return 2.0 * $earthRadiusM * [Math]::Atan2([Math]::Sqrt($a), [Math]::Sqrt(1.0 - $a))
}

function Wait-OwnedContainerRemoved {
    param([string]$Name)
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        $inspect = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "container", "inspect", $Name
        )
        if ($inspect.ExitCode -ne 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Owned disposable SITL container did not auto-remove."
}

foreach ($requiredPath in @(
    $Python,
    $Executable,
    $MissionUploader,
    $MissionConfig,
    $SyntheticModel,
    $SyntheticManifest
)) {
    Assert-True (Test-Path -LiteralPath $requiredPath) "Required AUTO mission HIL path is missing: $requiredPath"
}
Assert-True ($SitlPort -ne $ProtectedGroundStationPort) "SITL must never use protected ground-station UDP 14550."
Assert-True (-not (Test-Path -LiteralPath $EvidencePath)) "Evidence path already exists: $EvidencePath"
[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null

$sitlPortOwners = @(Get-UdpPortOwners -Port $SitlPort)
Assert-True ($sitlPortOwners.Count -eq 0) "Isolated SITL UDP $SitlPort is already in use."
$groundStationOwnersBefore = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)

$dockerInfo = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
    "info", "--format", "{{.ServerVersion}}"
)
Assert-True ($dockerInfo.ExitCode -eq 0) "Docker engine is unavailable. Start Docker Desktop and retry."
$dockerServerVersion = ($dockerInfo.Lines | Select-Object -Last 1).Trim()

$imageInspect = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
    "image", "inspect", $ImageReference, "--format", "{{.Id}}"
)
if ($imageInspect.ExitCode -ne 0) {
    $pull = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @("pull", $ImageReference)
    Assert-True ($pull.ExitCode -eq 0) "Unable to pull the pinned PX4 SITL image."
    $imageInspect = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "image", "inspect", $ImageReference, "--format", "{{.Id}}"
    )
}
Assert-True ($imageInspect.ExitCode -eq 0) "Pinned PX4 SITL image is unavailable after pull."
$imageId = ($imageInspect.Lines | Select-Object -Last 1).Trim()

$existingContainer = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
    "container", "inspect", $ContainerName
)
Assert-True ($existingContainer.ExitCode -ne 0) "Container name already exists; refusing to replace it: $ContainerName"

$containerStarted = $false
$containerStopped = $false
$simulatedVehicleArmed = $false
$containerId = $null
$liveProcess = $null
$px4VersionLines = @()
$parameterOverrides = [ordered]@{}

try {
    $containerCommand = "sed -i 's|mavlink start -x -u `$udp_gcs_port_local|mavlink start -x -u `$udp_gcs_port_local -o $SitlPort|' /opt/px4/etc/init.d-posix/px4-rc.mavlink && exec /opt/px4/bin/px4-entrypoint.sh"
    $run = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "run",
        "--rm",
        "-d",
        "--name", $ContainerName,
        "--label", "multidetect.purpose=$PurposeLabel",
        "--entrypoint", "/bin/sh",
        "-e", "PX4_SIM_MODEL=sihsim_airplane",
        $ImageReference,
        "-c", $containerCommand
    )
    Assert-True ($run.ExitCode -eq 0) "PX4 AUTO mission SITL container failed to start: $($run.Lines -join ' ')"
    $containerId = ($run.Lines | Select-Object -Last 1).Trim()
    Assert-True (-not [string]::IsNullOrWhiteSpace($containerId)) "Docker returned an empty container ID."
    $containerStarted = $true

    $probePassed = $false
    $probeOutput = @()
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        $probe = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
            "pixhawk-check",
            "--endpoint", "udpin:0.0.0.0:$SitlPort",
            "--samples", "12",
            "--interval-seconds", "0.1",
            "--expected-system-id", "1",
            "--expected-autopilot", "px4",
            "--expected-vehicle-type", "fixed_wing",
            "--require-operational-state",
            "--require-fresh-link",
            "--require-fresh-position"
        )
        $probeOutput = $probe.Lines
        if ($probe.ExitCode -eq 0) {
            $probePassed = $true
            break
        }
        Start-Sleep -Milliseconds 300
    }
    Assert-True $probePassed "PX4 AUTO mission SITL did not become ready: $($probeOutput -join ' ')"

    $version = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-ver", "all"
    )
    Assert-True ($version.ExitCode -eq 0) "Unable to read the PX4 SITL build identity."
    $px4VersionLines = $version.Lines

    foreach ($parameter in @(
        @("MIS_TKO_LAND_REQ", "1"),
        @("RWTO_TKOFF", "0"),
        @("MIS_TAKEOFF_ALT", "3"),
        @("SIM_BAT_DRAIN", "300")
    )) {
        $setResult = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "exec", $ContainerName, "/opt/px4/bin/px4-param", "set", $parameter[0], $parameter[1]
        )
        Assert-True ($setResult.ExitCode -eq 0) "Unable to set SITL-only parameter $($parameter[0])."
        $parameterOverrides[$parameter[0]] = [ordered]@{
            value = $parameter[1]
            output = $setResult.Lines
            scope = "owned disposable Docker PX4 process only"
        }
    }

    $upload = Invoke-CapturedCommand -FilePath $Python -ArgumentList @(
        $MissionUploader,
        "--container-name", $ContainerName,
        "--container-id", $containerId,
        "--acknowledge-owned-disposable-sitl"
    )
    Assert-True ($upload.ExitCode -eq 0) "SITL mission upload failed: $($upload.Lines -join ' ')"
    $uploadEvidence = Get-JsonEvent -Lines $upload.Lines -Event "px4_sitl_mission_upload_finished"
    Assert-True ($uploadEvidence.protocol.acknowledged -eq $true) "PX4 did not acknowledge the SITL mission."
    Assert-True ($uploadEvidence.protocol.request_sequences.Count -eq 3) "PX4 did not request all three mission items."
    Assert-True ($uploadEvidence.real_v6x_contacted -eq $false) "Mission uploader did not preserve its SITL-only boundary."

    $liveArguments = @(
        "-u",
        "-m",
        "multidetect.cli",
        "live-camera",
        $MissionConfig,
        "--source", $CameraSource,
        "--backend", $CameraBackend,
        "--width", "640",
        "--height", "480",
        "--onnx-model", $SyntheticModel,
        "--model-manifest", $SyntheticManifest,
        "--class-names", "fire,smoke",
        "--output-coordinates", "normalized_xyxy",
        "--allow-synthetic-hil-model",
        "--provider", "CPUExecutionProvider",
        "--pixhawk-endpoint", "udpin:0.0.0.0:$SitlPort",
        "--pixhawk-system-id", "1",
        "--pixhawk-expected-autopilot", "px4",
        "--pixhawk-expected-vehicle-type", "fixed_wing",
        "--require-pixhawk-operational-state",
        "--observe-pixhawk-lifecycle",
        "--task-area-mission-sequence", "1",
        "--allowed-auto-mode", "MISSION",
        "--max-frames", "$LiveFrames",
        "--no-display",
        "--audit-out", $AuditPath,
        "--prediction-log-out", $PredictionPath
    )
    $liveProcess = Start-Process `
        -FilePath $Python `
        -ArgumentList $liveArguments `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $LiveStdoutPath `
        -RedirectStandardError $LiveStderrPath

    $liveStarted = $false
    for ($attempt = 1; $attempt -le 75; $attempt++) {
        if ($liveProcess.HasExited) {
            break
        }
        if (Test-Path -LiteralPath $LiveStdoutPath) {
            $startedRecord = Select-String -LiteralPath $LiveStdoutPath -SimpleMatch '"event":"live_camera_started"'
            if ($startedRecord) {
                $liveStarted = $true
                break
            }
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $liveStarted) {
        $earlyOutput = if (Test-Path -LiteralPath $LiveStdoutPath) {
            Get-Content -LiteralPath $LiveStdoutPath -Raw
        }
        else {
            ""
        }
        $earlyError = if (Test-Path -LiteralPath $LiveStderrPath) {
            Get-Content -LiteralPath $LiveStderrPath -Raw
        }
        else {
            ""
        }
        $earlyStatus = if ($liveProcess.HasExited) {
            "exited with code $($liveProcess.ExitCode)"
        }
        else {
            "still running"
        }
        throw "Live camera did not reach its started state ($earlyStatus). stdout=$earlyOutput stderr=$earlyError"
    }

    $arm = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-commander", "arm", "-f"
    )
    Assert-True ($arm.ExitCode -eq 0) "Unable to arm the owned disposable PX4 SITL instance."
    $simulatedVehicleArmed = $true
    $missionMode = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-commander", "mode", "auto:mission"
    )
    Assert-True ($missionMode.ExitCode -eq 0) "Unable to request Mission mode inside owned PX4 SITL."

    $completed = $liveProcess.WaitForExit(120000)
    Assert-True $completed "Live AUTO mission patrol exceeded its bounded runtime."
    $liveProcess.Refresh()
    $liveExitCode = $liveProcess.ExitCode
    $liveLines = @(Get-Content -LiteralPath $LiveStdoutPath)
    $liveErrorText = if (Test-Path -LiteralPath $LiveStderrPath) {
        Get-Content -LiteralPath $LiveStderrPath -Raw
    }
    else {
        ""
    }
    Assert-True ($liveExitCode -eq 0) "Live AUTO mission patrol failed: $liveErrorText $($liveLines -join ' ')"
    $liveStartedEvidence = Get-JsonEvent -Lines $liveLines -Event "live_camera_started"
    $liveFinishedEvidence = Get-JsonEvent -Lines $liveLines -Event "live_camera_finished"
    $fireAlertEvidence = Get-JsonEvent -Lines $liveLines -Event "fire_alert"
    Assert-True ($liveFinishedEvidence.phase -eq "searching") "AUTO mission patrol did not finish in searching."
    Assert-True ($liveFinishedEvidence.alerts_delivered -eq 1) "AUTO mission patrol did not deliver exactly one alert."
    Assert-True ($liveFinishedEvidence.authorizations -eq 0) "Patrol-only AUTO mission created an authorization."
    Assert-True ($liveFinishedEvidence.simulated_payload_cycles -eq 0) "Patrol-only AUTO mission ran a payload cycle."
    Assert-True ($liveFinishedEvidence.pixhawk.messages_transmitted -eq 0) "Multi-Detect transmitted MAVLink during AUTO mission observation."

    $auditRows = @(Get-Content -LiteralPath $AuditPath | ForEach-Object { $_ | ConvertFrom-Json })
    $predictionRows = @(
        Get-Content -LiteralPath $PredictionPath | ForEach-Object { $_ | ConvertFrom-Json }
    )
    $transitionEvents = @($auditRows | Where-Object { $_.event_type -eq "mission.transition" })
    $transitionNames = @($transitionEvents | ForEach-Object { $_.details.event })
    $navigationWaitingEvents = @(
        $auditRows | Where-Object {
            $_.event_type -eq "mission.pixhawk_lifecycle_waiting" -and
            $_.details.phase -eq "navigating"
        }
    )
    $sequenceWaitingEvents = @(
        $navigationWaitingEvents | Where-Object {
            ($_.details.reasons -join " ") -match "current=0, required=1"
        }
    )
    $confirmedAlertEvents = @(
        $auditRows | Where-Object { $_.event_type -eq "alert.fire_confirmed" }
    )
    $authorizationEvents = @(
        $auditRows | Where-Object { $_.event_type -like "authorization.*" }
    )
    $payloadActionEvents = @(
        $auditRows | Where-Object {
            $_.event_type -like "payload.*" -and
            $_.event_type -ne "payload.inventory_evaluated"
        }
    )
    $flameCandidateFrames = @(
        $predictionRows | Where-Object {
            @($_.detections | Where-Object { $_.label -eq "flame" }).Count -gt 0
        }
    ).Count
    Assert-True ($predictionRows.Count -eq $LiveFrames) "AUTO mission prediction log is incomplete."
    Assert-True ($flameCandidateFrames -gt 0) "AUTO mission HIL produced no flame candidates."
    Assert-True ($sequenceWaitingEvents.Count -gt 0) "Observer did not wait for mission sequence 1."
    foreach ($requiredTransition in @("launch", "arrive_task_area", "target_confirmed", "alert_reported")) {
        Assert-True ($transitionNames -contains $requiredTransition) "Missing AUTO mission transition: $requiredTransition"
    }
    Assert-True ($confirmedAlertEvents.Count -eq 1) "AUTO mission audit did not contain one confirmed alert."
    Assert-True ($authorizationEvents.Count -eq 0) "AUTO mission patrol recorded an authorization event."
    Assert-True ($payloadActionEvents.Count -eq 0) "AUTO mission patrol recorded a payload action event."

    $finalCheck = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
        "pixhawk-check",
        "--endpoint", "udpin:0.0.0.0:$SitlPort",
        "--samples", "40",
        "--interval-seconds", "0.1",
        "--expected-system-id", "1",
        "--expected-autopilot", "px4",
        "--expected-vehicle-type", "fixed_wing",
        "--require-operational-state",
        "--require-fresh-link",
        "--require-fresh-position"
    )
    Assert-True ($finalCheck.ExitCode -eq 0) "Final AUTO mission telemetry qualification failed."
    $finalTelemetry = Get-JsonEvent -Lines $finalCheck.Lines -Event "pixhawk_read_only_check_finished"
    Assert-True ($finalTelemetry.latest.armed -eq $true) "SITL was no longer armed during final mission evidence."
    Assert-True ($finalTelemetry.latest.flight_mode -eq "MISSION") "SITL was not in Mission mode during final evidence."
    Assert-True ($finalTelemetry.latest.mission_sequence -ge 1) "PX4 mission sequence never reached the configured task-area gate."
    Assert-True ($finalTelemetry.messages_transmitted -eq 0) "Final telemetry check transmitted a MAVLink message."

    $homeLatitude = [double]$uploadEvidence.home.latitude_e7 / 10000000.0
    $homeLongitude = [double]$uploadEvidence.home.longitude_e7 / 10000000.0
    $movementDistanceM = Get-HaversineDistanceM `
        -Latitude1 $homeLatitude `
        -Longitude1 $homeLongitude `
        -Latitude2 ([double]$finalTelemetry.latest.latitude_deg) `
        -Longitude2 ([double]$finalTelemetry.latest.longitude_deg)
    Assert-True ($movementDistanceM -gt 20.0) "SITL position did not move far enough to prove mission execution."

    $disarm = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-commander", "disarm", "-f"
    )
    Assert-True ($disarm.ExitCode -eq 0) "Unable to disarm the owned disposable PX4 SITL instance."
    $simulatedVehicleArmed = $false
    $stop = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "stop", "--timeout", "1", $ContainerName
    )
    Assert-True ($stop.ExitCode -eq 0) "Unable to stop the owned PX4 AUTO mission SITL container."
    $containerStopped = $true
    Wait-OwnedContainerRemoved -Name $ContainerName

    $groundStationOwnersAfter = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
    $groundStationPortUnchanged = (($groundStationOwnersBefore -join ",") -eq ($groundStationOwnersAfter -join ","))
    Assert-True $groundStationPortUnchanged "Ground-station UDP 14550 ownership changed during AUTO mission HIL."

    $evidence = [ordered]@{
        schema_version = 1
        event = "px4_fixed_wing_sitl_auto_mission_acceptance"
        run_id = $RunId
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        scope = [ordered]@{
            software_only = $true
            real_v6x_contacted = $false
            real_payload_control_enabled = $false
            synthetic_model_accuracy_claim = $false
            auto_mission_lifecycle_only = $true
            aerodynamic_validation = $false
        }
        host = [ordered]@{
            docker_server_version = $dockerServerVersion
            protected_ground_station_port = $ProtectedGroundStationPort
            protected_port_owners_before = $groundStationOwnersBefore
            protected_port_owners_after = $groundStationOwnersAfter
            protected_port_ownership_unchanged = $groundStationPortUnchanged
            isolated_sitl_port = $SitlPort
        }
        px4_sitl = [ordered]@{
            image_reference = $ImageReference
            image_release_context = $ImageReleaseContext
            image_id = $imageId
            container_id = $containerId
            container_name = $ContainerName
            simulation_model = "sihsim_airplane"
            version_output = $px4VersionLines
            parameter_overrides = $parameterOverrides
            in_container_commands = @(
                "px4-param set (four lifecycle-only overrides)",
                "px4-commander arm -f",
                "px4-commander mode auto:mission",
                "px4-commander disarm -f"
            )
        }
        mission_upload = $uploadEvidence
        live_patrol = [ordered]@{
            requested_frames = $LiveFrames
            prediction_frames = $predictionRows.Count
            flame_candidate_frames = $flameCandidateFrames
            mission_sequence_wait_events = $sequenceWaitingEvents.Count
            transitions = @($transitionEvents | ForEach-Object { $_.details })
            confirmed_alert_events = $confirmedAlertEvents.Count
            authorization_events = $authorizationEvents.Count
            payload_action_events = $payloadActionEvents.Count
            started = $liveStartedEvidence
            fire_alert = $fireAlertEvidence
            finished = $liveFinishedEvidence
        }
        final_telemetry = $finalTelemetry
        movement_distance_from_upload_home_m = $movementDistanceM
        assertions = [ordered]@{
            pinned_image_digest = $true
            isolated_owned_container = $true
            px4_fixed_wing_identity = $true
            mission_upload_acknowledged = $true
            mission_mode_observed = $true
            mission_sequence_reached_task_area = $true
            position_changed = $true
            patrol_alert_count = 1
            authorization_count = 0
            payload_action_count = 0
            multidetect_mavlink_messages_transmitted = 0
            protected_ground_station_port_unchanged = $groundStationPortUnchanged
            all_passed = $true
        }
        artifacts = [ordered]@{
            audit_jsonl = $AuditPath
            predictions_jsonl = $PredictionPath
            live_stdout_jsonl = $LiveStdoutPath
            live_stderr_log = $LiveStderrPath
        }
        limitations = @(
            "The mission uses 3-5 m HIL-only altitudes and modified parameters solely to exercise lifecycle gates.",
            "The mission intentionally omits a fixed-wing landing pattern and sets MIS_TKO_LAND_REQ=1 only inside the disposable container.",
            "PX4 fixed-wing SIH is experimental and this run is not aerodynamic, launch, landing, wind, terrain or field-safety evidence.",
            "The constant-output ONNX artifact proves interface ordering only and makes no fire-detection accuracy claim.",
            "Mission upload, parameter overrides, arming and mode changes are confined to the owned disposable Docker container; Multi-Detect remains receive-only.",
            "No real V6X, actuator, radio or payload controller is contacted."
        )
        official_references = @(
            "https://mavlink.io/en/services/mission.html",
            "https://docs.px4.io/main/en/flight_modes_fw/mission",
            "https://docs.px4.io/main/en/sim_sih/"
        )
    }

    $evidenceDirectory = Split-Path -Parent $EvidencePath
    [IO.Directory]::CreateDirectory($evidenceDirectory) | Out-Null
    $temporaryEvidencePath = "$EvidencePath.tmp-$RunId"
    [IO.File]::WriteAllText(
        $temporaryEvidencePath,
        ($evidence | ConvertTo-Json -Depth 40) + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryEvidencePath -Destination $EvidencePath

    [ordered]@{
        event = "px4_fixed_wing_sitl_auto_mission_acceptance_finished"
        gate_passed = $true
        evidence_path = $EvidencePath
        phase = $liveFinishedEvidence.phase
        alerts = $liveFinishedEvidence.alerts_delivered
        authorizations = $liveFinishedEvidence.authorizations
        payload_actions = $payloadActionEvents.Count
        mission_sequence = $finalTelemetry.latest.mission_sequence
        movement_distance_m = $movementDistanceM
        multidetect_mavlink_messages_transmitted = 0
        protected_ground_station_port_unchanged = $groundStationPortUnchanged
        real_v6x_contacted = $false
        hardware_control_enabled = $false
    } | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $liveProcess -and -not $liveProcess.HasExited) {
        Stop-Process -Id $liveProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($containerStarted -and -not $containerStopped) {
        $identity = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "container", "inspect", $ContainerName, "--format", "{{.Id}}"
        )
        if (
            $identity.ExitCode -eq 0 -and
            -not [string]::IsNullOrWhiteSpace($containerId) -and
            ($identity.Lines | Select-Object -Last 1).Trim().StartsWith($containerId)
        ) {
            if ($simulatedVehicleArmed) {
                Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
                    "exec", $ContainerName, "/opt/px4/bin/px4-commander", "disarm", "-f"
                ) | Out-Null
            }
            Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
                "stop", "--timeout", "1", $ContainerName
            ) | Out-Null
        }
    }
}

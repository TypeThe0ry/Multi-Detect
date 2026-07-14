param(
    [string]$CameraSource = "0",
    [ValidateSet("auto", "dshow", "msmf", "ffmpeg", "gstreamer")]
    [string]$CameraBackend = "dshow",
    [ValidateRange(30, 600)]
    [int]$LiveFrames = 90,
    [switch]$IncludeInContainerArmedPatrolHil,
    [ValidateRange(120, 600)]
    [int]$PositivePatrolFrames = 180,
    [ValidateRange(1, 65535)]
    [int]$HostPort = 14650,
    [string]$ContainerName = "multidetect-px4-sitl-acceptance",
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $Root ".venv\Scripts\multi-detect.exe"
$MissionConfig = Join-Path $Root "configs\missions\fire_suppression_fixed_wing.demo.json"
$PatrolMissionConfig = Join-Path $Root "configs\missions\fire_patrol.demo.json"
$SyntheticModel = Join-Path $Root "artifacts\synthetic-hil\synthetic-fire-nx6-hil.onnx"
$SyntheticManifest = Join-Path $Root "artifacts\synthetic-hil\synthetic-fire-nx6-hil.manifest.json"
$ImageReference = "px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216"
$ImageReleaseContext = "v1.18.0-beta1"
$ProtectedGroundStationPort = 14550
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$ArtifactDirectory = Join-Path $Root "artifacts\evaluation"
$AuditPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-fire-gate-$RunId.audit.jsonl"
$PredictionPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-fire-gate-$RunId.predictions.jsonl"
$PositiveAuditPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-armed-patrol-$RunId.audit.jsonl"
$PositivePredictionPath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-armed-patrol-$RunId.predictions.jsonl"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-readonly-acceptance-$RunId.json"
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

foreach ($requiredPath in @($Executable, $MissionConfig, $PatrolMissionConfig, $SyntheticModel, $SyntheticManifest)) {
    Assert-True (Test-Path -LiteralPath $requiredPath) "Required acceptance path is missing: $requiredPath"
}
Assert-True ($HostPort -ne $ProtectedGroundStationPort) "Port 14550 is reserved for the real ground-station session."
Assert-True (-not (Test-Path -LiteralPath $EvidencePath)) "Evidence path already exists: $EvidencePath"
[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null

$selectedPortOwners = @(Get-UdpPortOwners -Port $HostPort)
Assert-True ($selectedPortOwners.Count -eq 0) "Selected SITL receive port $HostPort is already in use."
$groundStationOwnersBefore = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)

$dockerInfo = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @("info", "--format", "{{.ServerVersion}}")
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
$containerId = $null
$px4VersionLines = @()
$strictCheck = $null
$liveStarted = $null
$liveFinished = $null
$staleCheck = $null
$simulatedVehicleArmed = $false
$positiveQualification = $null
$positiveLiveStarted = $null
$positiveLiveFinished = $null
$positiveTransitionEvents = @()
$positiveAlertEvents = @()
$positiveAuthorizationEvents = @()
$positivePayloadActionEvents = @()
$positivePredictionRows = @()
$positiveFlameCandidateFrames = 0

try {
    $containerCommand = "sed -i 's|mavlink start -x -u `$udp_gcs_port_local|mavlink start -x -u `$udp_gcs_port_local -o $HostPort|' /opt/px4/etc/init.d-posix/px4-rc.mavlink && exec /opt/px4/bin/px4-entrypoint.sh"
    $run = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "run",
        "--rm",
        "-d",
        "--name", $ContainerName,
        "--label", "multidetect.purpose=px4-sitl-readonly-acceptance",
        "--entrypoint", "/bin/sh",
        "-e", "PX4_SIM_MODEL=sihsim_airplane",
        $ImageReference,
        "-c", $containerCommand
    )
    Assert-True ($run.ExitCode -eq 0) "PX4 SITL container failed to start: $($run.Lines -join ' ')"
    $containerId = ($run.Lines | Select-Object -Last 1).Trim()
    Assert-True (-not [string]::IsNullOrWhiteSpace($containerId)) "Docker returned an empty container ID."
    $containerStarted = $true

    $probePassed = $false
    $probeOutput = @()
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        $probe = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
            "pixhawk-check",
            "--endpoint", "udpin:0.0.0.0:$HostPort",
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
    Assert-True $probePassed "PX4 SITL did not become ready: $($probeOutput -join ' ')"

    $version = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-ver", "all"
    )
    Assert-True ($version.ExitCode -eq 0) "Unable to read the PX4 SITL build identity."
    $px4VersionLines = $version.Lines

    $strict = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
        "pixhawk-check",
        "--endpoint", "udpin:0.0.0.0:$HostPort",
        "--samples", "60",
        "--interval-seconds", "0.1",
        "--expected-system-id", "1",
        "--expected-autopilot", "px4",
        "--expected-vehicle-type", "fixed_wing",
        "--require-operational-state",
        "--require-fresh-link",
        "--require-fresh-position"
    )
    Assert-True ($strict.ExitCode -eq 0) "Strict PX4 receive qualification failed: $($strict.Lines -join ' ')"
    $strictCheck = Get-JsonEvent -Lines $strict.Lines -Event "pixhawk_read_only_check_finished"
    Assert-True ($strictCheck.gate_passed -eq $true) "Strict PX4 receive gate did not pass."
    Assert-True ($strictCheck.messages_transmitted -eq 0) "Read-only PX4 check transmitted a message."
    Assert-True ($strictCheck.heartbeat_identity.autopilot_name -eq "MAV_AUTOPILOT_PX4") "Unexpected autopilot identity."
    Assert-True ($strictCheck.heartbeat_identity.vehicle_type_name -eq "MAV_TYPE_FIXED_WING") "Unexpected vehicle type."
    Assert-True ($strictCheck.heartbeat_identity.system_status_name -eq "MAV_STATE_STANDBY") "SITL is not in the expected safe standby state."

    $live = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
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
        "--pixhawk-endpoint", "udpin:0.0.0.0:$HostPort",
        "--pixhawk-system-id", "1",
        "--pixhawk-expected-autopilot", "px4",
        "--pixhawk-expected-vehicle-type", "fixed_wing",
        "--require-pixhawk-operational-state",
        "--observe-pixhawk-lifecycle",
        "--task-area-mission-sequence", "0",
        "--allowed-auto-mode", "AUTO",
        "--max-frames", "$LiveFrames",
        "--no-display",
        "--audit-out", $AuditPath,
        "--prediction-log-out", $PredictionPath
    )
    Assert-True ($live.ExitCode -eq 0) "Live fire-gate check failed: $($live.Lines -join ' ')"
    $liveStarted = Get-JsonEvent -Lines $live.Lines -Event "live_camera_started"
    $liveFinished = Get-JsonEvent -Lines $live.Lines -Event "live_camera_finished"
    Assert-True ($liveStarted.fire_model_synthetic_hil_only -eq $true) "Adversarial test did not use the declared synthetic HIL model."
    Assert-True ($liveStarted.physical_release_supported -eq $false) "Physical release unexpectedly became available."
    Assert-True ($liveFinished.phase -eq "standby") "Fire candidates bypassed the observed flight lifecycle gate."
    Assert-True ($liveFinished.authorizations -eq 0) "An authorization was created while the vehicle was not flight-ready."
    Assert-True ($liveFinished.alerts_delivered -eq 0) "A payload-capable standby mission emitted an operational alert unexpectedly."
    Assert-True ($liveFinished.simulated_payload_cycles -eq 0) "A simulated payload cycle ran without authorization."
    Assert-True ($liveFinished.pixhawk.messages_transmitted -eq 0) "Live lifecycle observation transmitted a Pixhawk message."

    $auditRows = @(Get-Content -LiteralPath $AuditPath | ForEach-Object { $_ | ConvertFrom-Json })
    $predictionRows = @(Get-Content -LiteralPath $PredictionPath | ForEach-Object { $_ | ConvertFrom-Json })
    $lifecycleWaiting = @($auditRows | Where-Object { $_.event_type -eq "mission.pixhawk_lifecycle_waiting" })
    $missionTransitions = @($auditRows | Where-Object { $_.event_type -eq "mission.transition" })
    $authorizationEvents = @($auditRows | Where-Object { $_.event_type -like "authorization.*" })
    $payloadEvents = @($auditRows | Where-Object { $_.event_type -like "payload.*" })
    $fireCandidateFrames = @(
        $predictionRows | Where-Object {
            @($_.detections | Where-Object { $_.label -eq "flame" }).Count -gt 0
        }
    ).Count
    Assert-True ($predictionRows.Count -eq $LiveFrames) "Prediction log frame count does not match the requested live frame count."
    Assert-True ($fireCandidateFrames -gt 0) "Synthetic adversarial test did not produce any flame candidates."
    Assert-True ($lifecycleWaiting.Count -gt 0) "No lifecycle waiting evidence was recorded."
    Assert-True ($missionTransitions.Count -eq 0) "Mission transitioned out of standby during the adversarial fire test."
    Assert-True ($authorizationEvents.Count -eq 0) "Authorization events were recorded while the vehicle was not flight-ready."
    Assert-True ($payloadEvents.Count -eq 0) "Payload events were recorded while the vehicle was not flight-ready."

    if ($IncludeInContainerArmedPatrolHil) {
        $arm = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "exec", $ContainerName, "/opt/px4/bin/px4-commander", "arm", "-f"
        )
        Assert-True ($arm.ExitCode -eq 0) "Unable to arm the owned disposable PX4 SITL instance."
        $simulatedVehicleArmed = $true

        $loiter = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "exec", $ContainerName, "/opt/px4/bin/px4-commander", "mode", "auto:loiter"
        )
        Assert-True ($loiter.ExitCode -eq 0) "Unable to select auto:loiter inside the owned PX4 SITL instance."

        $active = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
            "pixhawk-check",
            "--endpoint", "udpin:0.0.0.0:$HostPort",
            "--samples", "30",
            "--interval-seconds", "0.1",
            "--expected-system-id", "1",
            "--expected-autopilot", "px4",
            "--expected-vehicle-type", "fixed_wing",
            "--require-operational-state",
            "--require-fresh-link",
            "--require-fresh-position"
        )
        Assert-True ($active.ExitCode -eq 0) "Armed SITL qualification failed: $($active.Lines -join ' ')"
        $positiveQualification = Get-JsonEvent -Lines $active.Lines -Event "pixhawk_read_only_check_finished"
        Assert-True ($positiveQualification.heartbeat_identity.system_status_name -eq "MAV_STATE_ACTIVE") "Owned SITL did not report MAV_STATE_ACTIVE."
        Assert-True ($positiveQualification.latest.armed -eq $true) "Owned SITL did not report armed=true."
        Assert-True ($positiveQualification.latest.flight_mode -eq "LOITER") "Owned SITL did not report LOITER."
        Assert-True ($positiveQualification.messages_transmitted -eq 0) "Armed SITL qualification transmitted a Pixhawk message."

        $positiveLive = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
            "live-camera",
            $PatrolMissionConfig,
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
            "--pixhawk-endpoint", "udpin:0.0.0.0:$HostPort",
            "--pixhawk-system-id", "1",
            "--pixhawk-expected-autopilot", "px4",
            "--pixhawk-expected-vehicle-type", "fixed_wing",
            "--require-pixhawk-operational-state",
            "--observe-pixhawk-lifecycle",
            "--task-area-mission-sequence", "0",
            "--allowed-auto-mode", "LOITER",
            "--max-frames", "$PositivePatrolFrames",
            "--no-display",
            "--audit-out", $PositiveAuditPath,
            "--prediction-log-out", $PositivePredictionPath
        )
        Assert-True ($positiveLive.ExitCode -eq 0) "Positive patrol lifecycle HIL failed: $($positiveLive.Lines -join ' ')"
        $positiveLiveStarted = Get-JsonEvent -Lines $positiveLive.Lines -Event "live_camera_started"
        $positiveLiveFinished = Get-JsonEvent -Lines $positiveLive.Lines -Event "live_camera_finished"
        Assert-True ($positiveLiveFinished.phase -eq "searching") "Armed allowed-mode patrol did not finish in searching."
        Assert-True ($positiveLiveFinished.alerts_delivered -eq 1) "Armed allowed-mode patrol did not deliver exactly one deduplicated alert."
        Assert-True ($positiveLiveFinished.authorizations -eq 0) "Patrol-only HIL created an authorization."
        Assert-True ($positiveLiveFinished.simulated_payload_cycles -eq 0) "Patrol-only HIL ran a simulated payload cycle."
        Assert-True ($positiveLiveFinished.physical_release_supported -eq $false) "Patrol-only HIL exposed physical release support."
        Assert-True ($positiveLiveFinished.pixhawk.messages_transmitted -eq 0) "Positive patrol lifecycle observation transmitted a Pixhawk message."

        $positiveAuditRows = @(
            Get-Content -LiteralPath $PositiveAuditPath | ForEach-Object { $_ | ConvertFrom-Json }
        )
        $positivePredictionRows = @(
            Get-Content -LiteralPath $PositivePredictionPath | ForEach-Object { $_ | ConvertFrom-Json }
        )
        $positiveTransitionEvents = @(
            $positiveAuditRows | Where-Object { $_.event_type -eq "mission.transition" }
        )
        $positiveTransitionNames = @($positiveTransitionEvents | ForEach-Object { $_.details.event })
        $positiveAlertEvents = @(
            $positiveAuditRows | Where-Object { $_.event_type -eq "alert.fire_confirmed" }
        )
        $positiveAuthorizationEvents = @(
            $positiveAuditRows | Where-Object { $_.event_type -like "authorization.*" }
        )
        $positivePayloadActionEvents = @(
            $positiveAuditRows | Where-Object {
                $_.event_type -like "payload.*" -and
                $_.event_type -ne "payload.inventory_evaluated"
            }
        )
        $positiveFlameCandidateFrames = @(
            $positivePredictionRows | Where-Object {
                @($_.detections | Where-Object { $_.label -eq "flame" }).Count -gt 0
            }
        ).Count
        Assert-True ($positivePredictionRows.Count -eq $PositivePatrolFrames) "Positive patrol prediction count is incomplete."
        Assert-True ($positiveFlameCandidateFrames -gt 0) "Positive patrol HIL produced no flame candidates."
        Assert-True ($positiveTransitionNames -contains "launch") "Positive patrol HIL did not observe launch readiness."
        Assert-True ($positiveTransitionNames -contains "arrive_task_area") "Positive patrol HIL did not enter searching."
        Assert-True ($positiveTransitionNames -contains "alert_reported") "Positive patrol HIL did not return to searching after alerting."
        Assert-True ($positiveAlertEvents.Count -eq 1) "Positive patrol HIL did not record exactly one confirmed fire alert."
        Assert-True ($positiveAuthorizationEvents.Count -eq 0) "Positive patrol HIL recorded an authorization event."
        Assert-True ($positivePayloadActionEvents.Count -eq 0) "Positive patrol HIL recorded a payload action event."

        $disarm = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "exec", $ContainerName, "/opt/px4/bin/px4-commander", "disarm", "-f"
        )
        Assert-True ($disarm.ExitCode -eq 0) "Unable to disarm the owned disposable PX4 SITL instance."
        $simulatedVehicleArmed = $false
    }

    $stop = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @("stop", "--timeout", "1", $ContainerName)
    Assert-True ($stop.ExitCode -eq 0) "Unable to stop the owned PX4 SITL container."
    $containerStopped = $true

    $stale = Invoke-CapturedCommand -FilePath $Executable -ArgumentList @(
        "pixhawk-check",
        "--endpoint", "udpin:0.0.0.0:$HostPort",
        "--samples", "20",
        "--interval-seconds", "0.1",
        "--expected-system-id", "1",
        "--expected-autopilot", "px4",
        "--expected-vehicle-type", "fixed_wing",
        "--require-operational-state",
        "--require-fresh-link",
        "--require-fresh-position"
    )
    Assert-True ($stale.ExitCode -ne 0) "Fresh-link gate unexpectedly passed after PX4 SITL stopped."
    $staleCheck = Get-JsonEvent -Lines $stale.Lines -Event "pixhawk_read_only_check_finished"
    Assert-True ($staleCheck.gate_passed -eq $false) "Stopped-SITL result did not fail closed."
    Assert-True ($staleCheck.messages_transmitted -eq 0) "Stopped-SITL check transmitted a message."

    $groundStationOwnersAfter = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
    $groundStationPortUnchanged = (($groundStationOwnersBefore -join ",") -eq ($groundStationOwnersAfter -join ","))
    Assert-True $groundStationPortUnchanged "Ground-station UDP 14550 ownership changed during acceptance."

    $positivePatrolEvidence = $null
    if ($IncludeInContainerArmedPatrolHil) {
        $positivePatrolEvidence = [ordered]@{
            explicit_opt_in = $true
            control_scope = "owned disposable Docker PX4 process only"
            application_flight_commands_enabled = $false
            auto_mission_validated = $false
            allowed_mode_override = "LOITER"
            qualification = $positiveQualification
            requested_frames = $PositivePatrolFrames
            prediction_frames = $positivePredictionRows.Count
            flame_candidate_frames = $positiveFlameCandidateFrames
            transition_events = @($positiveTransitionEvents | ForEach-Object { $_.details })
            confirmed_fire_alert_events = $positiveAlertEvents.Count
            authorization_events = $positiveAuthorizationEvents.Count
            payload_action_events = $positivePayloadActionEvents.Count
            live_started = $positiveLiveStarted
            live_finished = $positiveLiveFinished
        }
    }
    $simulatorCommandBoundary = if ($IncludeInContainerArmedPatrolHil) {
        "Arm, auto:loiter and disarm were invoked only through docker exec inside the newly created disposable PX4 SITL container; Multi-Detect still transmitted zero MAVLink messages."
    }
    else {
        "No arming, mode change, mission upload, parameter write, actuator command or payload command was sent."
    }

    $evidence = [ordered]@{
        schema_version = 1
        event = "px4_fixed_wing_sitl_readonly_acceptance"
        run_id = $RunId
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        scope = [ordered]@{
            software_only = $true
            real_v6x_contacted = $false
            real_payload_control_enabled = $false
            synthetic_model_accuracy_claim = $false
            in_container_armed_patrol_hil_included = [bool]$IncludeInContainerArmedPatrolHil
        }
        host = [ordered]@{
            docker_server_version = $dockerServerVersion
            protected_ground_station_port = $ProtectedGroundStationPort
            protected_port_owners_before = $groundStationOwnersBefore
            protected_port_owners_after = $groundStationOwnersAfter
            protected_port_ownership_unchanged = $groundStationPortUnchanged
            isolated_sitl_receive_port = $HostPort
        }
        px4_sitl = [ordered]@{
            image_reference = $ImageReference
            image_release_context = $ImageReleaseContext
            image_id = $imageId
            container_id = $containerId
            simulation_model = "sihsim_airplane"
            endpoint = "udpin:0.0.0.0:$HostPort"
            version_output = $px4VersionLines
        }
        strict_receive_qualification = $strictCheck
        adversarial_fire_gate = [ordered]@{
            model = "synthetic constant flame candidate HIL"
            model_is_operational_detector = $false
            requested_frames = $LiveFrames
            prediction_frames = $predictionRows.Count
            flame_candidate_frames = $fireCandidateFrames
            lifecycle_waiting_events = $lifecycleWaiting.Count
            mission_transition_events = $missionTransitions.Count
            authorization_events = $authorizationEvents.Count
            payload_events = $payloadEvents.Count
            live_started = $liveStarted
            live_finished = $liveFinished
        }
        stopped_sitl_fail_closed = $staleCheck
        in_container_armed_patrol_hil = $positivePatrolEvidence
        assertions = [ordered]@{
            pinned_image_digest = $true
            px4_fixed_wing_identity = $true
            strict_fresh_link_and_position_gate_passed = $true
            adversarial_flame_candidates_observed = $true
            unarmed_loiter_remained_standby = $true
            no_authorization_created = $true
            no_payload_event_created = $true
            pixhawk_messages_transmitted = 0
            stopped_sitl_failed_freshness_gate = $true
            optional_armed_patrol_reached_searching = (
                -not $IncludeInContainerArmedPatrolHil -or $positiveLiveFinished.phase -eq "searching"
            )
            optional_armed_patrol_alert_count = if ($IncludeInContainerArmedPatrolHil) { 1 } else { 0 }
            optional_armed_patrol_application_messages_transmitted = 0
            protected_ground_station_port_unchanged = $groundStationPortUnchanged
            all_passed = $true
        }
        artifacts = [ordered]@{
            audit_jsonl = $AuditPath
            predictions_jsonl = $PredictionPath
            armed_patrol_audit_jsonl = if ($IncludeInContainerArmedPatrolHil) { $PositiveAuditPath } else { $null }
            armed_patrol_predictions_jsonl = if ($IncludeInContainerArmedPatrolHil) { $PositivePredictionPath } else { $null }
        }
        limitations = @(
            "The pinned image is a v1.18.0-beta1 software artifact and is not proof of the firmware installed on the real V6X.",
            "PX4 fixed-wing SIH is a software dynamics test, not an aerodynamic, launch, payload, wind, terrain or field-safety validation.",
            "The constant-output ONNX model tests interface and fail-closed ordering only; it makes no fire-detection accuracy claim.",
            $simulatorCommandBoundary,
            "The optional armed LOITER patrol is a gate-plumbing HIL and is not an AUTO mission or route-execution acceptance."
        )
        official_references = @(
            "https://docs.px4.io/main/en/simulation/",
            "https://docs.px4.io/main/en/simulation/px4_sitl_prebuilt_packages",
            "https://docs.px4.io/main/en/sim_sih/"
        )
    }

    $evidenceDirectory = Split-Path -Parent $EvidencePath
    [IO.Directory]::CreateDirectory($evidenceDirectory) | Out-Null
    $temporaryEvidencePath = "$EvidencePath.tmp-$RunId"
    $evidenceJson = $evidence | ConvertTo-Json -Depth 30
    [IO.File]::WriteAllText(
        $temporaryEvidencePath,
        $evidenceJson + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryEvidencePath -Destination $EvidencePath

    [ordered]@{
        event = "px4_fixed_wing_sitl_readonly_acceptance_finished"
        gate_passed = $true
        evidence_path = $EvidencePath
        audit_path = $AuditPath
        prediction_path = $PredictionPath
        flame_candidate_frames = $fireCandidateFrames
        phase = $liveFinished.phase
        authorizations = $liveFinished.authorizations
        payload_events = $payloadEvents.Count
        in_container_armed_patrol_hil_included = [bool]$IncludeInContainerArmedPatrolHil
        armed_patrol_phase = if ($IncludeInContainerArmedPatrolHil) { $positiveLiveFinished.phase } else { $null }
        armed_patrol_alerts = if ($IncludeInContainerArmedPatrolHil) { $positiveLiveFinished.alerts_delivered } else { 0 }
        pixhawk_messages_transmitted = 0
        stopped_sitl_failed_freshness_gate = $true
        protected_ground_station_port_unchanged = $groundStationPortUnchanged
        hardware_control_enabled = $false
    } | ConvertTo-Json -Compress
}
finally {
    if ($containerStarted -and -not $containerStopped) {
        $ownedContainer = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
            "container", "inspect", $ContainerName
        )
        if ($ownedContainer.ExitCode -eq 0) {
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

param(
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Executable = Join-Path $Root ".venv\Scripts\multi-detect.exe"
$MissionUploader = Join-Path $Root "scripts\px4_sitl_mission_uploader.py"
$HeartbeatSender = Join-Path $Root "scripts\px4_sitl_gcs_heartbeat.py"
$ImageReference = "px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216"
$ImageReleaseContext = "v1.18.0-beta1"
$ContainerName = "multidetect-px4-datalink-loss-acceptance"
$PurposeLabel = "px4-sitl-datalink-loss-acceptance"
$SitlPort = 14652
$GcsInputPort = 18570
$GcsInputBinding = "127.0.0.1:$($GcsInputPort):$($GcsInputPort)/udp"
$ProtectedGroundStationPort = 14550
$InitialHeartbeatDurationSeconds = 24
$ReconnectHeartbeatDurationSeconds = 12
$HeartbeatRateHz = 2
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$ArtifactDirectory = Join-Path $Root "artifacts\evaluation"
$InitialHeartbeatStdoutPath = Join-Path $ArtifactDirectory "px4-datalink-initial-heartbeat-$RunId.stdout.jsonl"
$InitialHeartbeatStderrPath = Join-Path $ArtifactDirectory "px4-datalink-initial-heartbeat-$RunId.stderr.log"
$ReconnectHeartbeatStdoutPath = Join-Path $ArtifactDirectory "px4-datalink-reconnect-heartbeat-$RunId.stdout.jsonl"
$ReconnectHeartbeatStderrPath = Join-Path $ArtifactDirectory "px4-datalink-reconnect-heartbeat-$RunId.stderr.log"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $ArtifactDirectory "px4-fixed-wing-sitl-datalink-loss-acceptance-$RunId.json"
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

function Get-Px4Parameter {
    param(
        [string]$Name,
        [string]$OwnedContainerName
    )
    $show = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $OwnedContainerName, "/opt/px4/bin/px4-param", "show", $Name
    )
    Assert-True ($show.ExitCode -eq 0) "Unable to read SITL-only parameter $Name."
    $pattern = "\b$([regex]::Escape($Name))\b.*:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
    $line = @($show.Lines | Where-Object { $_ -match $pattern } | Select-Object -Last 1)
    Assert-True ($line.Count -eq 1) "Unable to parse SITL-only parameter $Name."
    $matched = $line[0] -match $pattern
    Assert-True $matched "Unable to parse numeric value for SITL-only parameter $Name."
    return [pscustomobject]@{
        name = $Name
        value = [double]::Parse(
            $Matches[1],
            [Globalization.CultureInfo]::InvariantCulture
        )
        output = $show.Lines
        scope = "owned disposable Docker PX4 process only"
    }
}

function Get-Px4TopicSnapshot {
    param(
        [string]$Topic,
        [string]$OwnedContainerName
    )
    $snapshot = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $OwnedContainerName, "/opt/px4/bin/px4-listener", $Topic, "-n", "1"
    )
    Assert-True ($snapshot.ExitCode -eq 0) "Unable to read PX4 SITL topic $Topic."
    return [pscustomobject]@{
        topic = $Topic
        lines = $snapshot.Lines
        text = ($snapshot.Lines -join [Environment]::NewLine)
    }
}

function Assert-TopicBoolean {
    param(
        [object]$Snapshot,
        [string]$Field,
        [bool]$Expected
    )
    $expectedText = if ($Expected) { "True" } else { "False" }
    $pattern = "(?m)^\s*$([regex]::Escape($Field)):\s*$expectedText\s*$"
    Assert-True (
        [regex]::IsMatch([string]$Snapshot.text, $pattern)
    ) "PX4 topic $($Snapshot.topic) did not report $Field=$expectedText."
}

function Get-ReadOnlyTelemetry {
    param(
        [int]$Samples,
        [bool]$RequireOperationalState
    )
    $arguments = @(
        "pixhawk-check",
        "--endpoint", "udpin:0.0.0.0:$SitlPort",
        "--samples", "$Samples",
        "--interval-seconds", "0.1",
        "--expected-system-id", "1",
        "--expected-autopilot", "px4",
        "--expected-vehicle-type", "fixed_wing",
        "--require-fresh-link",
        "--require-fresh-position"
    )
    if ($RequireOperationalState) {
        $arguments += "--require-operational-state"
    }
    $captured = Invoke-CapturedCommand -FilePath $Executable -ArgumentList $arguments
    Assert-True ($captured.ExitCode -eq 0) "PX4 read-only telemetry qualification failed: $($captured.Lines -join ' ')"
    $telemetry = Get-JsonEvent -Lines $captured.Lines -Event "pixhawk_read_only_check_finished"
    Assert-True ($telemetry.gate_passed -eq $true) "PX4 read-only telemetry gate did not pass."
    Assert-True ($telemetry.read_only -eq $true) "PX4 telemetry provider was not receive-only."
    Assert-True ($telemetry.messages_transmitted -eq 0) "Multi-Detect transmitted MAVLink during a read-only check."
    return $telemetry
}

function Start-SitlHeartbeat {
    param(
        [int]$DurationSeconds,
        [string]$StdoutPath,
        [string]$StderrPath
    )
    $arguments = @(
        "-u",
        $HeartbeatSender,
        "--endpoint", "udpout:127.0.0.1:$GcsInputPort",
        "--duration-seconds", "$DurationSeconds",
        "--rate-hz", "$HeartbeatRateHz",
        "--acknowledge-owned-disposable-sitl"
    )
    $startParameters = @{
        FilePath = $Python
        ArgumentList = $arguments
        PassThru = $true
        WindowStyle = "Hidden"
        RedirectStandardOutput = $StdoutPath
        RedirectStandardError = $StderrPath
    }
    return Start-Process @startParameters
}

function Wait-SitlHeartbeat {
    param(
        [object]$Process,
        [string]$StdoutPath,
        [string]$StderrPath,
        [int]$TimeoutMilliseconds
    )
    $completed = $Process.WaitForExit($TimeoutMilliseconds)
    Assert-True $completed "Bounded SITL heartbeat sender exceeded its runtime."
    $Process.Refresh()
    $stdoutLines = if (Test-Path -LiteralPath $StdoutPath) {
        @(Get-Content -LiteralPath $StdoutPath)
    }
    else {
        @()
    }
    $stderrText = if (Test-Path -LiteralPath $StderrPath) {
        Get-Content -LiteralPath $StderrPath -Raw
    }
    else {
        ""
    }
    Assert-True ($Process.ExitCode -eq 0) "SITL heartbeat sender failed: $stderrText $($stdoutLines -join ' ')"
    $evidence = Get-JsonEvent -Lines $stdoutLines -Event "px4_sitl_gcs_heartbeat_finished"
    Assert-True ($evidence.software_only -eq $true) "Heartbeat sender did not preserve its software-only boundary."
    Assert-True ($evidence.real_v6x_contacted -eq $false) "Heartbeat sender reported contact with real hardware."
    return $evidence
}

foreach ($requiredPath in @(
    $Python,
    $Executable,
    $MissionUploader,
    $HeartbeatSender
)) {
    Assert-True (Test-Path -LiteralPath $requiredPath) "Required datalink-loss HIL path is missing: $requiredPath"
}
Assert-True ($SitlPort -ne $ProtectedGroundStationPort) "SITL must never use protected ground-station UDP 14550."
Assert-True ($GcsInputPort -ne $ProtectedGroundStationPort) "GCS heartbeat input must never use protected ground-station UDP 14550."
Assert-True ($SitlPort -ne $GcsInputPort) "SITL telemetry and GCS heartbeat input ports must be distinct."
Assert-True (-not (Test-Path -LiteralPath $EvidencePath)) "Evidence path already exists: $EvidencePath"
[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null

$sitlPortOwnersBefore = @(Get-UdpPortOwners -Port $SitlPort)
$gcsInputPortOwnersBefore = @(Get-UdpPortOwners -Port $GcsInputPort)
$groundStationOwnersBefore = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
Assert-True ($sitlPortOwnersBefore.Count -eq 0) "Isolated SITL UDP $SitlPort is already in use."
Assert-True ($gcsInputPortOwnersBefore.Count -eq 0) "Loopback GCS UDP $GcsInputPort is already in use."

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
$initialHeartbeatProcess = $null
$reconnectHeartbeatProcess = $null
$px4VersionLines = @()
$initialParameters = [ordered]@{}
$parameterOverrides = [ordered]@{}

try {
    $containerCommand = @'
sed -i 's|mavlink start -x -u $udp_gcs_port_local|mavlink start -x -u $udp_gcs_port_local -o __SITL_PORT__|' /opt/px4/etc/init.d-posix/px4-rc.mavlink && exec /opt/px4/bin/px4-entrypoint.sh
'@
    $containerCommand = $containerCommand.Replace("__SITL_PORT__", "$SitlPort")
    $run = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "run",
        "--rm",
        "-d",
        "--name", $ContainerName,
        "--label", "multidetect.purpose=$PurposeLabel",
        "--network", "bridge",
        "--security-opt", "no-new-privileges:true",
        "--cap-drop", "ALL",
        "-p", $GcsInputBinding,
        "--entrypoint", "/bin/sh",
        "-e", "PX4_SIM_MODEL=sihsim_airplane",
        $ImageReference,
        "-c", $containerCommand
    )
    Assert-True ($run.ExitCode -eq 0) "PX4 datalink-loss SITL container failed to start: $($run.Lines -join ' ')"
    $containerId = ($run.Lines | Select-Object -Last 1).Trim()
    Assert-True (-not [string]::IsNullOrWhiteSpace($containerId)) "Docker returned an empty container ID."
    $containerStarted = $true

    $ready = $false
    $readyOutput = @()
    for ($attempt = 1; $attempt -le 15; $attempt++) {
        try {
            $readyTelemetry = Get-ReadOnlyTelemetry -Samples 12 -RequireOperationalState $true
            $ready = $true
            break
        }
        catch {
            $readyOutput = @("$($_.Exception.Message)")
            Start-Sleep -Milliseconds 300
        }
    }
    Assert-True $ready "PX4 datalink-loss SITL did not become ready: $($readyOutput -join ' ')"
    Assert-True ($readyTelemetry.latest.armed -eq $false) "Fresh SITL was unexpectedly armed."

    $version = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-ver", "all"
    )
    Assert-True ($version.ExitCode -eq 0) "Unable to read the PX4 SITL build identity."
    $px4VersionLines = $version.Lines

    foreach ($name in @("COM_DL_LOSS_T", "NAV_DLL_ACT")) {
        $parameter = Get-Px4Parameter -Name $name -OwnedContainerName $ContainerName
        $initialParameters[$name] = [ordered]@{
            value = $parameter.value
            output = $parameter.output
            scope = $parameter.scope
        }
    }
    Assert-True ($initialParameters.COM_DL_LOSS_T.value -eq 10) "Pinned PX4 image no longer defaults COM_DL_LOSS_T to 10 seconds."
    Assert-True ($initialParameters.NAV_DLL_ACT.value -eq 0) "Pinned PX4 image no longer defaults NAV_DLL_ACT to Disabled."

    $initialHeartbeatProcess = Start-SitlHeartbeat -DurationSeconds $InitialHeartbeatDurationSeconds -StdoutPath $InitialHeartbeatStdoutPath -StderrPath $InitialHeartbeatStderrPath
    Start-Sleep -Milliseconds 1500
    Assert-True (-not $initialHeartbeatProcess.HasExited) "Initial SITL heartbeat sender exited before connection qualification."
    $configurationLinkFlags = Get-Px4TopicSnapshot -Topic "failsafe_flags" -OwnedContainerName $ContainerName
    $configurationLinkStatus = Get-Px4TopicSnapshot -Topic "vehicle_status" -OwnedContainerName $ContainerName
    Assert-TopicBoolean -Snapshot $configurationLinkFlags -Field "gcs_connection_lost" -Expected $false
    Assert-TopicBoolean -Snapshot $configurationLinkStatus -Field "gcs_connection_lost" -Expected $false

    foreach ($parameter in @(
        @("COM_DL_LOSS_T", "5"),
        @("NAV_DLL_ACT", "1"),
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
            requested_value = $parameter[1]
            verified_value = $null
            set_output = $setResult.Lines
            show_output = @()
            scope = "owned disposable Docker PX4 process only"
        }
    }

    foreach ($expected in @(
        @("COM_DL_LOSS_T", 5),
        @("NAV_DLL_ACT", 1)
    )) {
        $verified = Get-Px4Parameter -Name $expected[0] -OwnedContainerName $ContainerName
        Assert-True ($verified.value -eq $expected[1]) "SITL-only parameter $($expected[0]) did not retain its requested value."
        $parameterOverrides[$expected[0]].verified_value = $verified.value
        $parameterOverrides[$expected[0]].show_output = $verified.output
    }

    $upload = Invoke-CapturedCommand -FilePath $Python -ArgumentList @(
        $MissionUploader,
        "--container-name", $ContainerName,
        "--container-id", $containerId,
        "--ownership-profile", "datalink_loss",
        "--acknowledge-owned-disposable-sitl"
    )
    Assert-True ($upload.ExitCode -eq 0) "SITL mission upload failed: $($upload.Lines -join ' ')"
    $uploadEvidence = Get-JsonEvent -Lines $upload.Lines -Event "px4_sitl_mission_upload_finished"
    Assert-True ($uploadEvidence.protocol.acknowledged -eq $true) "PX4 did not acknowledge the SITL mission."
    Assert-True ($uploadEvidence.container.ownership_profile -eq "datalink_loss") "Mission uploader did not enforce the datalink-loss ownership profile."
    Assert-True ($uploadEvidence.container.checks.exact_host_port_boundary -eq $true) "Mission uploader did not prove the exact loopback port boundary."
    Assert-True ($uploadEvidence.real_v6x_contacted -eq $false) "Mission uploader did not preserve its SITL-only boundary."

    $connectedFlags = Get-Px4TopicSnapshot -Topic "failsafe_flags" -OwnedContainerName $ContainerName
    $connectedStatus = Get-Px4TopicSnapshot -Topic "vehicle_status" -OwnedContainerName $ContainerName
    Assert-TopicBoolean -Snapshot $connectedFlags -Field "gcs_connection_lost" -Expected $false
    Assert-TopicBoolean -Snapshot $connectedStatus -Field "gcs_connection_lost" -Expected $false

    $arm = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-commander", "arm", "-f"
    )
    Assert-True ($arm.ExitCode -eq 0) "Unable to arm the owned disposable PX4 SITL instance."
    $simulatedVehicleArmed = $true
    $missionMode = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-commander", "mode", "auto:mission"
    )
    Assert-True ($missionMode.ExitCode -eq 0) "Unable to request Mission mode inside owned PX4 SITL."
    Start-Sleep -Seconds 2

    $preLossTelemetry = Get-ReadOnlyTelemetry -Samples 30 -RequireOperationalState $true
    Assert-True ($preLossTelemetry.latest.armed -eq $true) "SITL was not armed before the datalink-loss test."
    Assert-True ($preLossTelemetry.latest.flight_mode -eq "MISSION") "SITL did not enter Mission mode before the datalink-loss test."

    $initialHeartbeatEvidence = Wait-SitlHeartbeat -Process $initialHeartbeatProcess -StdoutPath $InitialHeartbeatStdoutPath -StderrPath $InitialHeartbeatStderrPath -TimeoutMilliseconds 30000
    $initialHeartbeatProcess = $null
    Assert-True ($initialHeartbeatEvidence.messages_transmitted -gt 0) "Initial GCS heartbeat sender transmitted no heartbeats."

    $lossWatchStarted = Get-Date
    $lossDetected = $false
    $lostFlags = $null
    for ($attempt = 1; $attempt -le 24; $attempt++) {
        Start-Sleep -Milliseconds 500
        $candidateFlags = Get-Px4TopicSnapshot -Topic "failsafe_flags" -OwnedContainerName $ContainerName
        if ([regex]::IsMatch([string]$candidateFlags.text, "(?m)^\s*gcs_connection_lost:\s*True\s*$")) {
            $lostFlags = $candidateFlags
            $lossDetected = $true
            break
        }
    }
    $lossDetectedAt = Get-Date
    $lossElapsedSeconds = ($lossDetectedAt - $lossWatchStarted).TotalSeconds
    Assert-True $lossDetected "PX4 did not report GCS connection loss after the bounded heartbeat stream stopped."
    Assert-TopicBoolean -Snapshot $lostFlags -Field "gcs_connection_lost" -Expected $true

    $lostStatus = $null
    $failsafeActive = $false
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $candidateStatus = Get-Px4TopicSnapshot -Topic "vehicle_status" -OwnedContainerName $ContainerName
        $gcsLost = [regex]::IsMatch([string]$candidateStatus.text, "(?m)^\s*gcs_connection_lost:\s*True\s*$")
        $failsafe = [regex]::IsMatch([string]$candidateStatus.text, "(?m)^\s*failsafe:\s*True\s*$")
        if ($gcsLost -and $failsafe) {
            $lostStatus = $candidateStatus
            $failsafeActive = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Assert-True $failsafeActive "PX4 did not enter its datalink-loss failsafe state."
    Assert-TopicBoolean -Snapshot $lostStatus -Field "gcs_connection_lost" -Expected $true
    Assert-TopicBoolean -Snapshot $lostStatus -Field "failsafe" -Expected $true

    $postLossTelemetry = Get-ReadOnlyTelemetry -Samples 30 -RequireOperationalState $false
    Assert-True ($postLossTelemetry.latest.armed -eq $true) "SITL disarmed instead of executing the configured Hold action."
    Assert-True ($postLossTelemetry.latest.flight_mode -eq "LOITER") "NAV_DLL_ACT=1 did not change fixed-wing Mission mode to Hold/LOITER."

    $reconnectHeartbeatProcess = Start-SitlHeartbeat -DurationSeconds $ReconnectHeartbeatDurationSeconds -StdoutPath $ReconnectHeartbeatStdoutPath -StderrPath $ReconnectHeartbeatStderrPath
    Start-Sleep -Milliseconds 1500
    Assert-True (-not $reconnectHeartbeatProcess.HasExited) "Reconnect SITL heartbeat sender exited before qualification."

    $reconnectedFlags = $null
    $reconnected = $false
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $candidateFlags = Get-Px4TopicSnapshot -Topic "failsafe_flags" -OwnedContainerName $ContainerName
        if ([regex]::IsMatch([string]$candidateFlags.text, "(?m)^\s*gcs_connection_lost:\s*False\s*$")) {
            $reconnectedFlags = $candidateFlags
            $reconnected = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Assert-True $reconnected "PX4 did not clear its GCS connection-loss flag after heartbeat recovery."
    $reconnectedStatus = Get-Px4TopicSnapshot -Topic "vehicle_status" -OwnedContainerName $ContainerName
    Assert-TopicBoolean -Snapshot $reconnectedFlags -Field "gcs_connection_lost" -Expected $false
    Assert-TopicBoolean -Snapshot $reconnectedStatus -Field "gcs_connection_lost" -Expected $false

    $postReconnectTelemetry = Get-ReadOnlyTelemetry -Samples 30 -RequireOperationalState $false
    Assert-True ($postReconnectTelemetry.latest.armed -eq $true) "SITL was no longer armed during reconnect evidence."
    Assert-True ($postReconnectTelemetry.latest.flight_mode -eq "LOITER") "PX4 left Hold without an explicit operator mode change after GCS reconnection."
    $reconnectFailsafeActive = [regex]::IsMatch(
        [string]$reconnectedStatus.text,
        "(?m)^\s*failsafe:\s*True\s*$"
    )

    $disarm = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "exec", $ContainerName, "/opt/px4/bin/px4-commander", "disarm", "-f"
    )
    Assert-True ($disarm.ExitCode -eq 0) "Unable to disarm the owned disposable PX4 SITL instance."
    $simulatedVehicleArmed = $false

    $reconnectHeartbeatEvidence = Wait-SitlHeartbeat -Process $reconnectHeartbeatProcess -StdoutPath $ReconnectHeartbeatStdoutPath -StderrPath $ReconnectHeartbeatStderrPath -TimeoutMilliseconds 20000
    $reconnectHeartbeatProcess = $null
    Assert-True ($reconnectHeartbeatEvidence.messages_transmitted -gt 0) "Reconnect GCS heartbeat sender transmitted no heartbeats."

    $stop = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "stop", "--timeout", "1", $ContainerName
    )
    Assert-True ($stop.ExitCode -eq 0) "Unable to stop the owned PX4 datalink-loss SITL container."
    $containerStopped = $true
    Wait-OwnedContainerRemoved -Name $ContainerName

    $sitlPortOwnersAfter = @(Get-UdpPortOwners -Port $SitlPort)
    $gcsInputPortOwnersAfter = @(Get-UdpPortOwners -Port $GcsInputPort)
    $groundStationOwnersAfter = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
    $groundStationPortUnchanged = (($groundStationOwnersBefore -join ",") -eq ($groundStationOwnersAfter -join ","))
    Assert-True ($sitlPortOwnersAfter.Count -eq 0) "Isolated SITL telemetry port remained occupied after cleanup."
    Assert-True ($gcsInputPortOwnersAfter.Count -eq 0) "Loopback GCS input port remained occupied after cleanup."
    Assert-True $groundStationPortUnchanged "Ground-station UDP 14550 ownership changed during datalink-loss HIL."

    $evidence = [ordered]@{
        schema_version = 1
        event = "px4_fixed_wing_sitl_datalink_loss_acceptance"
        run_id = $RunId
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        scope = [ordered]@{
            software_only = $true
            real_v6x_contacted = $false
            real_payload_control_enabled = $false
            physical_release_possible = $false
            parameter_writes_confined_to_owned_container = $true
            aerodynamic_validation = $false
        }
        host = [ordered]@{
            docker_server_version = $dockerServerVersion
            protected_ground_station_port = $ProtectedGroundStationPort
            protected_port_owners_before = $groundStationOwnersBefore
            protected_port_owners_after = $groundStationOwnersAfter
            protected_port_ownership_unchanged = $groundStationPortUnchanged
            isolated_sitl_port = $SitlPort
            isolated_sitl_port_owners_before = $sitlPortOwnersBefore
            isolated_sitl_port_owners_after = $sitlPortOwnersAfter
            loopback_gcs_input_port = $GcsInputPort
            loopback_gcs_binding = $GcsInputBinding
            loopback_gcs_port_owners_before = $gcsInputPortOwnersBefore
            loopback_gcs_port_owners_after = $gcsInputPortOwnersAfter
        }
        px4_sitl = [ordered]@{
            image_reference = $ImageReference
            image_release_context = $ImageReleaseContext
            image_id = $imageId
            container_id = $containerId
            container_name = $ContainerName
            purpose_label = $PurposeLabel
            simulation_model = "sihsim_airplane"
            version_output = $px4VersionLines
            initial_parameters = $initialParameters
            parameter_overrides = $parameterOverrides
            in_container_commands = @(
                "px4-param set (six SITL-only overrides)",
                "px4-commander arm -f",
                "px4-commander mode auto:mission",
                "px4-commander disarm -f"
            )
        }
        mission_upload = $uploadEvidence
        connected_before_loss = [ordered]@{
            configuration_failsafe_flags = $configurationLinkFlags.lines
            configuration_vehicle_status = $configurationLinkStatus.lines
            failsafe_flags = $connectedFlags.lines
            vehicle_status = $connectedStatus.lines
            telemetry = $preLossTelemetry
            heartbeat = $initialHeartbeatEvidence
        }
        detected_loss = [ordered]@{
            detection_elapsed_after_sender_exit_seconds = $lossElapsedSeconds
            failsafe_flags = $lostFlags.lines
            vehicle_status = $lostStatus.lines
            telemetry = $postLossTelemetry
            configured_timeout_seconds = 5
            configured_action = "Hold"
            fixed_wing_mode_observed = "LOITER"
        }
        reconnected = [ordered]@{
            failsafe_flags = $reconnectedFlags.lines
            vehicle_status = $reconnectedStatus.lines
            telemetry = $postReconnectTelemetry
            heartbeat = $reconnectHeartbeatEvidence
            observed_flight_mode = $postReconnectTelemetry.latest.flight_mode
            failsafe_active_when_sampled = $reconnectFailsafeActive
        }
        assertions = [ordered]@{
            pinned_image_digest = $true
            isolated_owned_container = $true
            exact_loopback_gcs_port_mapping = $true
            initial_COM_DL_LOSS_T = 10
            initial_NAV_DLL_ACT = 0
            sitl_COM_DL_LOSS_T = 5
            sitl_NAV_DLL_ACT = 1
            connected_flag_cleared_before_arm = $true
            mission_mode_observed_before_loss = $true
            gcs_loss_flag_observed = $true
            failsafe_state_observed = $true
            hold_loiter_mode_observed = $true
            gcs_reconnect_flag_cleared = $true
            hold_retained_after_reconnect = $true
            multidetect_mavlink_messages_transmitted = 0
            physical_payload_actions = 0
            protected_ground_station_port_unchanged = $groundStationPortUnchanged
            all_passed = $true
        }
        artifacts = [ordered]@{
            initial_heartbeat_stdout_jsonl = $InitialHeartbeatStdoutPath
            initial_heartbeat_stderr_log = $InitialHeartbeatStderrPath
            reconnect_heartbeat_stdout_jsonl = $ReconnectHeartbeatStdoutPath
            reconnect_heartbeat_stderr_log = $ReconnectHeartbeatStderrPath
        }
        limitations = @(
            "COM_DL_LOSS_T=5 and NAV_DLL_ACT=1 are written only inside the owned disposable PX4 SITL container; the real V6X is not contacted.",
            "PX4 fixed-wing Hold is represented by LOITER in MAVLink flight-mode telemetry.",
            "After GCS heartbeat recovery, PX4 clears the connection-loss flag but retains LOITER/Hold until an explicit operator mode change; the acceptance does not auto-resume Mission.",
            "The mission uses 3-5 m lifecycle-only HIL altitudes and is not aerodynamic, launch, landing, wind, terrain or field-safety evidence.",
            "Mission upload and bounded GCS heartbeats are simulator stimuli; all Multi-Detect telemetry checks remain receive-only with zero transmitted messages.",
            "No real actuator, radio, camera, Jetson service, V6X or payload controller is contacted."
        )
        official_references = @(
            "https://docs.px4.io/main/en/advanced_config/parameter_reference.html#COM_DL_LOSS_T",
            "https://docs.px4.io/main/en/advanced_config/parameter_reference.html#NAV_DLL_ACT",
            "https://docs.px4.io/main/en/flight_modes_fw/hold",
            "https://docs.px4.io/main/en/sim_sih/"
        )
    }

    $evidenceDirectory = Split-Path -Parent $EvidencePath
    [IO.Directory]::CreateDirectory($evidenceDirectory) | Out-Null
    $temporaryEvidencePath = "$EvidencePath.tmp-$RunId"
    [IO.File]::WriteAllText(
        $temporaryEvidencePath,
        ($evidence | ConvertTo-Json -Depth 50) + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryEvidencePath -Destination $EvidencePath

    [ordered]@{
        event = "px4_fixed_wing_sitl_datalink_loss_acceptance_finished"
        gate_passed = $true
        evidence_path = $EvidencePath
        before_loss_mode = $preLossTelemetry.latest.flight_mode
        after_loss_mode = $postLossTelemetry.latest.flight_mode
        after_reconnect_mode = $postReconnectTelemetry.latest.flight_mode
        gcs_loss_detected = $true
        hold_action_observed = $true
        reconnect_observed = $true
        multidetect_mavlink_messages_transmitted = 0
        protected_ground_station_port_unchanged = $groundStationPortUnchanged
        real_v6x_contacted = $false
        hardware_control_enabled = $false
    } | ConvertTo-Json -Compress
}
finally {
    foreach ($heartbeatProcess in @($initialHeartbeatProcess, $reconnectHeartbeatProcess)) {
        if ($null -ne $heartbeatProcess -and -not $heartbeatProcess.HasExited) {
            Stop-Process -Id $heartbeatProcess.Id -Force -ErrorAction SilentlyContinue
        }
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

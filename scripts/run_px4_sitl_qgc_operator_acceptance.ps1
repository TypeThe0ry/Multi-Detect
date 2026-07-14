param(
    [string]$QgcRoot = "",
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$RouterModule = "multidetect.px4_sitl_qgc_router"
$ImageReference = "px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216"
$ImageReleaseContext = "v1.18.0-beta1"
$ContainerName = "multidetect-px4-qgc-operator-acceptance"
$PurposeLabel = "px4-sitl-qgc-operator-acceptance"
$QgcPort = 14669
$RouterPort = 14667
$SitlTelemetryPort = 14668
$SitlInputPort = 18570
$SitlInputBinding = "127.0.0.1:$($SitlInputPort):$($SitlInputPort)/udp"
$ProtectedGroundStationPort = 14550
$RouterDurationSeconds = 45
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$ArtifactDirectory = Join-Path $Root "artifacts\evaluation"
if ([string]::IsNullOrWhiteSpace($QgcRoot)) {
    $QgcRoot = Join-Path (Split-Path -Parent $Root) "QGroundControl-MultiDetect"
}
elseif (-not [IO.Path]::IsPathRooted($QgcRoot)) {
    $QgcRoot = Join-Path $Root $QgcRoot
}
$QgcRoot = [IO.Path]::GetFullPath($QgcRoot)
$QgcExecutable = Join-Path $QgcRoot "build-multidetect-release\Release\MultiDetectGCS.exe"
$QgcDriver = Join-Path $QgcRoot "custom\tests\operator_closed_loop_hil.py"
$QgcCustomOptions = Join-Path $QgcRoot "custom\cmake\CustomOverrides.cmake"
$QgcControllerSource = Join-Path $QgcRoot "custom\src\MultiDetectOperatorController.cc"
$QgcCmakeCache = Join-Path $QgcRoot "build-multidetect-release\CMakeCache.txt"
$RouterStdoutPath = Join-Path $ArtifactDirectory "px4-qgc-router-$RunId.stdout.jsonl"
$RouterStderrPath = Join-Path $ArtifactDirectory "px4-qgc-router-$RunId.stderr.log"
$DriverStdoutPath = Join-Path $ArtifactDirectory "px4-qgc-jetson-metadata-$RunId.stdout.jsonl"
$DriverStderrPath = Join-Path $ArtifactDirectory "px4-qgc-jetson-metadata-$RunId.stderr.log"
$QgcStdoutPath = Join-Path $ArtifactDirectory "px4-qgc-app-$RunId.stdout.log"
$QgcStderrPath = Join-Path $ArtifactDirectory "px4-qgc-app-$RunId.stderr.log"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $ArtifactDirectory "px4-sitl-qgc-operator-acceptance-$RunId.json"
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
    # Windows PowerShell can promote native stderr records to terminating errors
    # when the script-wide preference is Stop. Some probes intentionally expect
    # a non-zero exit (for example, asserting that a disposable container does
    # not already exist), so capture stderr and decide from the native exit code.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $FilePath @ArgumentList 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Lines = @($output | ForEach-Object { "$_" })
    }
}

function Get-JsonEventFromFile {
    param(
        [string]$Path,
        [string]$Event
    )
    Assert-True (Test-Path -LiteralPath $Path) "Expected JSON output is missing: $Path"
    $matches = @()
    foreach ($line in @(Get-Content -LiteralPath $Path)) {
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
        if ([string]::IsNullOrEmpty($Event) -or $record.event -eq $Event) {
            $matches += $record
        }
    }
    Assert-True ($matches.Count -gt 0) "JSON output did not contain event '$Event': $Path"
    return $matches[-1]
}

function Get-TextIfPresent {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    try {
        return Get-Content -LiteralPath $Path -Raw
    }
    catch {
        return ""
    }
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

function Get-ProcessUdpEndpoints {
    param([int]$ProcessId)
    if (-not (Get-Command Get-NetUDPEndpoint -ErrorAction SilentlyContinue)) {
        return @()
    }
    return @(
        Get-NetUDPEndpoint -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $ProcessId } |
            Sort-Object LocalAddress, LocalPort |
            ForEach-Object {
                [ordered]@{
                    local_address = $_.LocalAddress
                    local_port = $_.LocalPort
                    owning_process = $_.OwningProcess
                }
            }
    )
}

function Get-CrashDumpSignatures {
    $crashDirectory = Join-Path $env:LOCALAPPDATA "CrashDumps"
    if (-not (Test-Path -LiteralPath $crashDirectory)) {
        return @()
    }
    return @(
        Get-ChildItem -LiteralPath $crashDirectory -Filter "MultiDetectGCS*.dmp" -File -ErrorAction SilentlyContinue |
            Sort-Object FullName |
            ForEach-Object {
                "{0}|{1}|{2}" -f $_.FullName, $_.Length, $_.LastWriteTimeUtc.Ticks
            }
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

function Wait-ProcessCompleted {
    param(
        [object]$Process,
        [int]$TimeoutMilliseconds,
        [string]$Name
    )
    $completed = $Process.WaitForExit($TimeoutMilliseconds)
    Assert-True $completed "$Name exceeded its bounded runtime."
    $Process.WaitForExit()
    $Process.Refresh()
    Assert-True ($Process.ExitCode -eq 0) "$Name failed with exit code $($Process.ExitCode)."
    return $Process.ExitCode
}

function Get-OwnedContainerEvidence {
    param(
        [string]$Name,
        [string]$ExpectedId
    )
    $inspect = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "container", "inspect", $Name
    )
    Assert-True ($inspect.ExitCode -eq 0) "Owned PX4 SITL container is unavailable."
    $inspectJson = $inspect.Lines -join [Environment]::NewLine
    $records = @($inspectJson | ConvertFrom-Json)
    Assert-True ($records.Count -eq 1) "Docker inspect returned an unexpected container set."
    $record = $records[0]
    $portBindings = $record.HostConfig.PortBindings
    $binding = @($portBindings."$SitlInputPort/udp" | Where-Object { $null -ne $_ })
    $devices = @($record.HostConfig.Devices | Where-Object { $null -ne $_ })
    $mounts = @($record.Mounts | Where-Object { $null -ne $_ })
    $checks = [ordered]@{
        identity = ([string]$record.Id).StartsWith($ExpectedId)
        running = $record.State.Running -eq $true
        pinned_image = $record.Config.Image -eq $ImageReference
        purpose_label = $record.Config.Labels."multidetect.purpose" -eq $PurposeLabel
        fixed_wing_sih = @($record.Config.Env) -contains "PX4_SIM_MODEL=sihsim_airplane"
        bridge_network = $record.HostConfig.NetworkMode -eq "bridge"
        not_privileged = $record.HostConfig.Privileged -eq $false
        no_devices = $devices.Count -eq 0
        no_mounts = $mounts.Count -eq 0
        exact_loopback_port = (
            $binding.Count -eq 1 -and
            $binding[0].HostIp -eq "127.0.0.1" -and
            $binding[0].HostPort -eq "$SitlInputPort"
        )
        isolated_telemetry_destination = (
            (@($record.Config.Cmd) -join " ") -match "-o $SitlTelemetryPort"
        )
        protected_port_not_targeted = (
            (@($record.Config.Cmd) -join " ") -notmatch "-o $ProtectedGroundStationPort"
        )
    }
    $failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
    Assert-True ($failed.Count -eq 0) "Owned PX4 SITL container checks failed: $($failed -join ', ')"
    return [ordered]@{
        container_id = $record.Id
        container_name = $Name
        checks = $checks
    }
}

function Get-CmakeCacheValue {
    param(
        [string]$Path,
        [string]$Name
    )
    $match = Select-String -LiteralPath $Path -Pattern "^$([regex]::Escape($Name)):[^=]+=(.+)$" |
        Select-Object -First 1
    Assert-True ($null -ne $match) "CMake cache value is missing: $Name"
    return $match.Matches[0].Groups[1].Value
}

foreach ($requiredPath in @(
    $Python,
    $QgcExecutable,
    $QgcDriver,
    $QgcCustomOptions,
    $QgcControllerSource,
    $QgcCmakeCache
)) {
    Assert-True (Test-Path -LiteralPath $requiredPath) "Required PX4/QGC HIL path is missing: $requiredPath"
}
Assert-True (-not (Test-Path -LiteralPath $EvidencePath)) "Evidence path already exists: $EvidencePath"
[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null

$qt6Directory = Get-CmakeCacheValue -Path $QgcCmakeCache -Name "Qt6_DIR"
$QtRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $qt6Directory))
$GstreamerRoot = Get-CmakeCacheValue -Path $QgcCmakeCache -Name "GStreamer_ROOT_DIR"
$QtBin = Join-Path $QtRoot "bin"
$QtPlugins = Join-Path $QtRoot "plugins"
$QtQml = Join-Path $QtRoot "qml"
$GstreamerBin = Join-Path $GstreamerRoot "bin"
$GstreamerPlugins = Join-Path $GstreamerRoot "lib\gstreamer-1.0"
foreach ($runtimePath in @(
    (Join-Path $QtBin "Qt6Core.dll"),
    $QtPlugins,
    $QtQml,
    (Join-Path $GstreamerBin "gstreamer-1.0-0.dll"),
    $GstreamerPlugins
)) {
    Assert-True (Test-Path -LiteralPath $runtimePath) "Required QGC runtime path is missing: $runtimePath"
}

$customOptionsText = Get-Content -LiteralPath $QgcCustomOptions -Raw
$controllerSourceText = Get-Content -LiteralPath $QgcControllerSource -Raw
Assert-True ($customOptionsText -match "MULTIDETECT_FLIGHT_CONTROL_WRITES=0") "QGC flight-control compile gate is not disabled."
Assert-True ($customOptionsText -match "MULTIDETECT_PHYSICAL_RELEASE=0") "QGC physical-release compile gate is not disabled."
Assert-True ($controllerSourceText -match "static_assert\(MULTIDETECT_FLIGHT_CONTROL_WRITES == 0") "QGC flight-control static assertion is missing."
Assert-True ($controllerSourceText -match "static_assert\(MULTIDETECT_PHYSICAL_RELEASE == 0") "QGC physical-release static assertion is missing."
Assert-True ($controllerSourceText -match "software HIL requires the --isolated-hil") "QGC isolated-HIL runtime guard is missing."

$allTestPorts = @($QgcPort, $RouterPort, $SitlTelemetryPort, $SitlInputPort)
Assert-True (($allTestPorts | Select-Object -Unique).Count -eq $allTestPorts.Count) "PX4/QGC HIL ports must be distinct."
Assert-True ($allTestPorts -notcontains $ProtectedGroundStationPort) "PX4/QGC HIL must not use protected UDP 14550."
foreach ($port in $allTestPorts) {
    Assert-True (@(Get-UdpPortOwners -Port $port).Count -eq 0) "PX4/QGC HIL UDP $port is already in use."
}
$protectedOwnersBefore = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
$crashDumpsBefore = @(Get-CrashDumpSignatures)

$dockerInfo = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
    "info", "--format", "{{.ServerVersion}}"
)
Assert-True ($dockerInfo.ExitCode -eq 0) "Docker engine is unavailable. Start Docker Desktop and retry."
$dockerServerVersion = ($dockerInfo.Lines | Select-Object -Last 1).Trim()
$imageInspect = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
    "image", "inspect", $ImageReference, "--format", "{{.Id}}"
)
Assert-True ($imageInspect.ExitCode -eq 0) "Pinned PX4 SITL image is unavailable."
$imageId = ($imageInspect.Lines | Select-Object -Last 1).Trim()
$existingContainer = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
    "container", "inspect", $ContainerName
)
Assert-True ($existingContainer.ExitCode -ne 0) "Container name already exists; refusing to replace it: $ContainerName"

$environmentNames = @(
    "MULTIDETECT_OPERATOR_KEY",
    "MULTIDETECT_OPERATOR_ID",
    "MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL",
    "MULTIDETECT_OPERATOR_HIL_UDP_PORT",
    "MULTIDETECT_OPERATOR_HIL_AUTO_EXIT",
    "MULTIDETECT_OPERATOR_HIL_AUTO_EXERCISE",
    "MULTIDETECT_OPERATOR_HIL_REQUIRE_INITIAL_CONNECT",
    "MULTIDETECT_OPERATOR_GCS_SYSTEM_ID",
    "MULTIDETECT_OPERATOR_JETSON_SYSTEM_ID",
    "MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID",
    "PYTHONPATH",
    "PATH",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "GST_PLUGIN_PATH"
)
$savedEnvironment = @{}
foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$temporaryKeyBytes = New-Object byte[] 32
$randomNumberGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $randomNumberGenerator.GetBytes($temporaryKeyBytes)
}
finally {
    $randomNumberGenerator.Dispose()
}
$temporaryKey = [Convert]::ToBase64String($temporaryKeyBytes)
$env:MULTIDETECT_OPERATOR_KEY = $temporaryKey
$env:MULTIDETECT_OPERATOR_ID = "software-hil-operator"
$env:MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL = "1"
$env:MULTIDETECT_OPERATOR_HIL_UDP_PORT = "$QgcPort"
$env:MULTIDETECT_OPERATOR_HIL_AUTO_EXIT = "1"
$env:MULTIDETECT_OPERATOR_HIL_AUTO_EXERCISE = "1"
$env:MULTIDETECT_OPERATOR_HIL_REQUIRE_INITIAL_CONNECT = "1"
$env:MULTIDETECT_OPERATOR_GCS_SYSTEM_ID = "255"
$env:MULTIDETECT_OPERATOR_JETSON_SYSTEM_ID = "1"
$env:MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID = "191"
$env:PYTHONPATH = Join-Path $Root "src"
$env:PATH = "$QtBin;$GstreamerBin;$env:PATH"
$env:QT_PLUGIN_PATH = $QtPlugins
$env:QML2_IMPORT_PATH = $QtQml
$env:GST_PLUGIN_PATH = $GstreamerPlugins

$routerProcess = $null
$driverProcess = $null
$qgcProcess = $null
$containerStarted = $false
$containerStopped = $false
$containerId = $null

try {
    $routerArguments = @(
        "-u",
        "-m", $RouterModule,
        "--qgc-port", "$QgcPort",
        "--router-port", "$RouterPort",
        "--sitl-telemetry-port", "$SitlTelemetryPort",
        "--sitl-input-port", "$SitlInputPort",
        "--duration-seconds", "$RouterDurationSeconds",
        "--acknowledge-owned-disposable-sitl"
    )
    $routerStart = @{
        FilePath = $Python
        ArgumentList = $routerArguments
        PassThru = $true
        WindowStyle = "Hidden"
        RedirectStandardOutput = $RouterStdoutPath
        RedirectStandardError = $RouterStderrPath
    }
    $routerProcess = Start-Process @routerStart
    # Pin the native process handle before it can exit. Windows PowerShell 5.1
    # otherwise may return a null ExitCode for short-lived redirected processes.
    $null = $routerProcess.Handle
    Start-Sleep -Milliseconds 500
    Assert-True (-not $routerProcess.HasExited) "PX4/QGC read-only router exited during startup."

    $driverArguments = @(
        "-u",
        $QgcDriver,
        "--port", "$QgcPort",
        "--timeout", "40",
        "--metadata-only"
    )
    $driverStart = @{
        FilePath = $Python
        ArgumentList = $driverArguments
        WorkingDirectory = $QgcRoot
        PassThru = $true
        WindowStyle = "Hidden"
        RedirectStandardOutput = $DriverStdoutPath
        RedirectStandardError = $DriverStderrPath
    }
    $driverProcess = Start-Process @driverStart
    $null = $driverProcess.Handle

    $qgcArguments = @(
        "--system-id", "255",
        "--isolated-hil",
        "--allow-multiple",
        "--log-output",
        "--logging", "MultiDetect.Operator.debug=true;MultiDetect.Operator.info=true"
    )
    $qgcStart = @{
        FilePath = $QgcExecutable
        ArgumentList = $qgcArguments
        WorkingDirectory = $QgcRoot
        PassThru = $true
        WindowStyle = "Hidden"
        RedirectStandardOutput = $QgcStdoutPath
        RedirectStandardError = $QgcStderrPath
    }
    $qgcProcess = Start-Process @qgcStart
    $null = $qgcProcess.Handle

    $hilListening = $false
    for ($attempt = 1; $attempt -le 150; $attempt++) {
        if ($qgcProcess.HasExited -or $driverProcess.HasExited -or $routerProcess.HasExited) {
            break
        }
        $qgcLog = (Get-TextIfPresent -Path $QgcStdoutPath) + (Get-TextIfPresent -Path $QgcStderrPath)
        if ($qgcLog -match "ephemeral HIL UDP link listening on port $QgcPort") {
            $hilListening = $true
            break
        }
        Start-Sleep -Milliseconds 100
    }
    $qgcEarlyLog = (Get-TextIfPresent -Path $QgcStdoutPath) + (Get-TextIfPresent -Path $QgcStderrPath)
    Assert-True $hilListening "QGC did not open the isolated HIL link. log=$qgcEarlyLog"
    Assert-True (-not $driverProcess.HasExited) "Jetson metadata driver exited before PX4 SITL startup."
    Assert-True (-not $routerProcess.HasExited) "PX4/QGC router exited before PX4 SITL startup."

    $qgcUdpEndpointsDuring = @(Get-ProcessUdpEndpoints -ProcessId $qgcProcess.Id)
    $qgcUdpPortsDuring = @($qgcUdpEndpointsDuring | ForEach-Object { $_.local_port })
    Assert-True ($qgcUdpPortsDuring -contains $QgcPort) "QGC process did not own the isolated HIL UDP port."
    Assert-True ($qgcUdpPortsDuring -notcontains $ProtectedGroundStationPort) "QGC isolated HIL unexpectedly opened protected UDP 14550."
    $protectedOwnersDuring = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
    Assert-True (
        ($protectedOwnersBefore -join ",") -eq ($protectedOwnersDuring -join ",")
    ) "Protected UDP 14550 ownership changed before PX4 SITL startup."

    $containerCommand = @'
sed -i 's|mavlink start -x -u $udp_gcs_port_local|mavlink start -x -u $udp_gcs_port_local -o __SITL_TELEMETRY_PORT__|' /opt/px4/etc/init.d-posix/px4-rc.mavlink && exec /opt/px4/bin/px4-entrypoint.sh
'@
    $containerCommand = $containerCommand.Replace("__SITL_TELEMETRY_PORT__", "$SitlTelemetryPort")
    $run = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "run",
        "--rm",
        "-d",
        "--name", $ContainerName,
        "--label", "multidetect.purpose=$PurposeLabel",
        "--network", "bridge",
        "--security-opt", "no-new-privileges:true",
        "--cap-drop", "ALL",
        "-p", $SitlInputBinding,
        "--entrypoint", "/bin/sh",
        "-e", "PX4_SIM_MODEL=sihsim_airplane",
        $ImageReference,
        "-c", $containerCommand
    )
    Assert-True ($run.ExitCode -eq 0) "PX4/QGC SITL container failed to start: $($run.Lines -join ' ')"
    $containerId = ($run.Lines | Select-Object -Last 1).Trim()
    Assert-True (-not [string]::IsNullOrWhiteSpace($containerId)) "Docker returned an empty container ID."
    $containerStarted = $true
    $containerEvidence = Get-OwnedContainerEvidence -Name $ContainerName -ExpectedId $containerId

    Wait-ProcessCompleted -Process $driverProcess -TimeoutMilliseconds 45000 -Name "Jetson metadata-only HIL driver" | Out-Null
    Wait-ProcessCompleted -Process $qgcProcess -TimeoutMilliseconds 20000 -Name "Multi-Detect QGC" | Out-Null

    $stop = Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
        "stop", "--timeout", "1", $ContainerName
    )
    Assert-True ($stop.ExitCode -eq 0) "Unable to stop the owned PX4/QGC SITL container."
    $containerStopped = $true
    Wait-OwnedContainerRemoved -Name $ContainerName

    Wait-ProcessCompleted -Process $routerProcess -TimeoutMilliseconds 50000 -Name "PX4/QGC read-only router" | Out-Null

    $driverEvidence = Get-JsonEventFromFile -Path $DriverStdoutPath -Event ""
    $routerEvidence = Get-JsonEventFromFile -Path $RouterStdoutPath -Event "px4_sitl_qgc_readonly_router_finished"
    Assert-True ($driverEvidence.authorization -eq "APPROVE") "QGC did not complete the bound authorization decision."
    Assert-True ($driverEvidence.authorization_bound -eq $true) "QGC authorization was not challenge-bound."
    Assert-True ($driverEvidence.selection -eq "SELECT") "QGC did not send the expected target selection."
    Assert-True ($driverEvidence.metadata_only -eq $true) "Jetson HIL driver was not metadata-only."
    Assert-True ($driverEvidence.external_autopilot_required -eq $true) "Jetson HIL driver did not require an external autopilot."
    Assert-True ($driverEvidence.autopilot_heartbeats_sent -eq 0) "Jetson HIL driver spoofed an autopilot heartbeat."
    Assert-True ($driverEvidence.jetson_component_heartbeats_sent -gt 0) "Jetson component 191 sent no presence heartbeat."
    Assert-True ($driverEvidence.real_v6x_contacted -eq $false) "Jetson HIL driver reported real V6X contact."
    Assert-True ($routerEvidence.px4_frames_forwarded -gt 0) "Router received no PX4 SITL telemetry."
    Assert-True ($routerEvidence.px4_autopilot_heartbeats_forwarded -gt 0) "Router received no PX4 autopilot heartbeat."
    Assert-True ($routerEvidence.px4_unexpected_system_frames_blocked -eq 0) "Router observed an unexpected PX4 source system."
    Assert-True ($routerEvidence.qgc_operator_tunnel_frames_local_only -ge 2) "Router did not observe both QGC operator commands."
    Assert-True ($routerEvidence.qgc_forbidden_frames_blocked -eq 0) "QGC attempted a forbidden PX4-bound message."
    Assert-True ($routerEvidence.file_mutating_ftp_opcodes_forwarded -eq 0) "Router forwarded a file-mutating MAVLink FTP opcode."
    Assert-True ($routerEvidence.system_time_frames_forwarded -eq 0) "Router forwarded QGC SYSTEM_TIME into PX4 SITL."
    Assert-True ($routerEvidence.diagnostic_prearm_check_only -eq $true) "Router diagnostic command policy is not constrained to prearm checks."
    Assert-True ($routerEvidence.operator_tunnel_forwarded_to_px4 -eq $false) "Operator TUNNEL was forwarded to PX4."
    Assert-True ($routerEvidence.real_v6x_contacted -eq $false) "Router reported real V6X contact."
    Assert-True ($routerEvidence.px4_discarded_bytes -eq 0) "Router discarded malformed PX4 bytes."
    Assert-True ($routerEvidence.qgc_discarded_bytes -eq 0) "Router discarded malformed QGC bytes."

    $qgcLog = (Get-TextIfPresent -Path $QgcStdoutPath) + (Get-TextIfPresent -Path $QgcStderrPath)
    Assert-True ($qgcLog -match "ephemeral HIL UDP link listening on port $QgcPort") "QGC log lacks isolated HIL startup."
    Assert-True ($qgcLog -match "HIL target-selection metadata sent") "QGC log lacks target-selection transmission."
    Assert-True ($qgcLog -match "HIL PX4 initial connection ready") "QGC did not wait for PX4 initial connection readiness."
    Assert-True ($qgcLog -match "HIL authenticated message set complete") "QGC log lacks authenticated-loop completion."
    Assert-True ($qgcLog -notmatch "rejected metadata:") "QGC rejected operator metadata during acceptance."
    $authenticatedSetMatch = [regex]::Match(
        $qgcLog,
        "HIL authenticated message set complete; authenticated_packets=(\d+)"
    )
    Assert-True $authenticatedSetMatch.Success "QGC completion log lacks its authenticated packet count."
    $authenticatedMetadataPackets = [int]$authenticatedSetMatch.Groups[1].Value
    Assert-True ($authenticatedMetadataPackets -ge 6) "QGC authenticated fewer than six required metadata packets."

    $protectedOwnersAfter = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
    $protectedPortUnchanged = (
        ($protectedOwnersBefore -join ",") -eq ($protectedOwnersAfter -join ",")
    )
    Assert-True $protectedPortUnchanged "Protected UDP 14550 ownership changed during PX4/QGC HIL."
    foreach ($port in $allTestPorts) {
        Assert-True (@(Get-UdpPortOwners -Port $port).Count -eq 0) "PX4/QGC HIL UDP $port remained occupied after cleanup."
    }
    $crashDumpsAfter = @(Get-CrashDumpSignatures)
    $crashDumpsUnchanged = (
        ($crashDumpsBefore -join [Environment]::NewLine) -eq
        ($crashDumpsAfter -join [Environment]::NewLine)
    )
    Assert-True $crashDumpsUnchanged "MultiDetectGCS created or changed a Windows crash dump."

    $qgcHash = (Get-FileHash -LiteralPath $QgcExecutable -Algorithm SHA256).Hash
    $evidence = [ordered]@{
        schema_version = 1
        event = "px4_sitl_qgc_operator_acceptance"
        run_id = $RunId
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        scope = [ordered]@{
            software_only = $true
            real_v6x_contacted = $false
            real_jetson_contacted = $false
            physical_payload_control_enabled = $false
            flight_control_writes_enabled = $false
            parameter_writes_enabled = $false
            mission_writes_enabled = $false
        }
        host = [ordered]@{
            docker_server_version = $dockerServerVersion
            protected_ground_station_port = $ProtectedGroundStationPort
            protected_port_owners_before = $protectedOwnersBefore
            protected_port_owners_during = $protectedOwnersDuring
            protected_port_owners_after = $protectedOwnersAfter
            protected_port_ownership_unchanged = $protectedPortUnchanged
            test_ports = $allTestPorts
            test_ports_released = $true
            crash_dumps_before = $crashDumpsBefore
            crash_dumps_after = $crashDumpsAfter
            crash_dumps_unchanged = $crashDumpsUnchanged
        }
        qgc = [ordered]@{
            executable = $QgcExecutable
            executable_sha256 = $qgcHash
            exit_code = $qgcProcess.ExitCode
            isolated_hil_cli = $true
            gcs_system_id = 255
            udp_endpoints_during = $qgcUdpEndpointsDuring
            authenticated_metadata_packets_in_log = $authenticatedMetadataPackets
            runtime = [ordered]@{
                qt_root = $QtRoot
                gstreamer_root = $GstreamerRoot
            }
            compile_gates = [ordered]@{
                MULTIDETECT_FLIGHT_CONTROL_WRITES = 0
                MULTIDETECT_PHYSICAL_RELEASE = 0
                static_assertions_present = $true
            }
        }
        px4_sitl = [ordered]@{
            image_reference = $ImageReference
            image_release_context = $ImageReleaseContext
            image_id = $imageId
            container = $containerEvidence
            parameter_overrides = 0
            arm_commands = 0
            mode_commands = 0
            mission_uploads = 0
        }
        jetson_metadata_hil = $driverEvidence
        readonly_router = $routerEvidence
        assertions = [ordered]@{
            real_px4_sitl_autopilot_heartbeat_observed = $true
            jetson_autopilot_heartbeat_spoof_count = 0
            target_selection_completed = $true
            tracking_mission_safety_challenge_received = $true
            challenge_bound_authorization_completed = $true
            qgc_forbidden_px4_messages = 0
            file_mutating_ftp_opcodes_forwarded = 0
            system_time_frames_forwarded = 0
            diagnostic_prearm_check_only = $true
            operator_tunnel_forwarded_to_px4 = 0
            physical_payload_actions = 0
            protected_ground_station_port_unchanged = $true
            all_processes_exit_zero = $true
            all_test_ports_released = $true
            no_new_crash_dump = $true
            all_passed = $true
        }
        artifacts = [ordered]@{
            qgc_stdout_log = $QgcStdoutPath
            qgc_stderr_log = $QgcStderrPath
            jetson_metadata_stdout_jsonl = $DriverStdoutPath
            jetson_metadata_stderr_log = $DriverStderrPath
            router_stdout_jsonl = $RouterStdoutPath
            router_stderr_log = $RouterStderrPath
        }
        limitations = @(
            "PX4 is the pinned fixed-wing SIH image, not a build matched to the real V6X firmware.",
            "Jetson component 191 and operator metadata are simulated on localhost; no real Jetson, GR01, G20 or V6X is contacted.",
            "QGC core read-only discovery traffic may reach PX4 SITL through an allowlist; parameter writes, flight commands, mission writes, actuators and payload commands are blocked.",
            "Unsigned outer MAVLink is allowed only by the isolated localhost HIL boundary; production requires MAVLink 2 signing.",
            "Authorization reaches only DEPLOYMENT_READY / RELEASE INHIBITED; no physical release path exists."
        )
    }

    $temporaryEvidencePath = "$EvidencePath.tmp-$RunId"
    [IO.File]::WriteAllText(
        $temporaryEvidencePath,
        ($evidence | ConvertTo-Json -Depth 50) + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryEvidencePath -Destination $EvidencePath

    [ordered]@{
        event = "px4_sitl_qgc_operator_acceptance_finished"
        gate_passed = $true
        evidence_path = $EvidencePath
        px4_autopilot_heartbeats = $routerEvidence.px4_autopilot_heartbeats_forwarded
        authenticated_metadata_packets = $authenticatedMetadataPackets
        operator_tunnels_local_only = $routerEvidence.qgc_operator_tunnel_frames_local_only
        qgc_forbidden_px4_messages = 0
        autopilot_heartbeat_spoof_count = 0
        physical_payload_actions = 0
        protected_ground_station_port_unchanged = $true
        real_v6x_contacted = $false
        hardware_control_enabled = $false
    } | ConvertTo-Json -Compress
}
finally {
    foreach ($process in @($qgcProcess, $driverProcess, $routerProcess)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
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
            Invoke-CapturedCommand -FilePath "docker" -ArgumentList @(
                "stop", "--timeout", "1", $ContainerName
            ) | Out-Null
        }
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    $temporaryKey = $null
    if ($null -ne $temporaryKeyBytes) {
        [Array]::Clear($temporaryKeyBytes, 0, $temporaryKeyBytes.Length)
        $temporaryKeyBytes = $null
    }
}

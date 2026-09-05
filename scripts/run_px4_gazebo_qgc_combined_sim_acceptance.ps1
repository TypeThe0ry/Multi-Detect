param(
    [ValidateRange(120, 1800)]
    # Keep the finite sensor producer alive past the Mission-ready barrier.
    # A 600-frame run can legitimately finish at the same instant the
    # evaluator writes mission-active-ready.json, leaving no post-ready frames
    # for the native QGC sequence-advance proof.  900 frames preserves the
    # bounded run while leaving a deterministic capture margin on contended
    # Windows hosts.
    [int]$Frames = 900,
    [ValidateRange(30, 600)]
    [int]$RouterDurationSeconds = 420,
    [ValidateRange(2, 30)]
    [double]$FixedWingHoldSeconds = 12.0,
    # Mirrors vtol_sitl_mission_acceptance.py's declared 10..600 second
    # observation bound. The wrapper derives both ready and process waits from
    # this value instead of clipping the helper with unrelated constants.
    [ValidateRange(10, 600)]
    [int]$MissionTimeoutSeconds = 240,
    [ValidateRange(60, 300)]
    [int]$MissionSetupAllowanceSeconds = 120,
    [ValidateRange(1, 60)]
    [int]$MissionCaptureSequenceAdvance = 5,
    [string]$QgcPortableBin = "",
    [string]$EvidencePath = "",
    [string]$ContainerName = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$ProductVersionManifest = Join-Path $Root "configs\product-version.json"
if (-not (Test-Path -LiteralPath $ProductVersionManifest -PathType Leaf)) {
    throw "Product version manifest is missing: $ProductVersionManifest"
}
$ProductVersion = Get-Content -LiteralPath $ProductVersionManifest -Raw | ConvertFrom-Json
$ReleaseLabel = [string]$ProductVersion.release_label
$NumericVersion = [string]$ProductVersion.numeric
if ([string]::IsNullOrWhiteSpace($ReleaseLabel) -or [string]::IsNullOrWhiteSpace($NumericVersion)) {
    throw "Product version manifest is missing release_label or numeric."
}
$ReleaseStatus = "v$ReleaseLabel; final v$NumericVersion remains blocked"
$PurposeLabel = "combined-px4-gazebo-qgc-sim-acceptance"
$ImageReference = "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
$ImageDigest = "bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
$RunId = "{0}-{1}" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"), $PID
$RunDirectory = Join-Path $Root "artifacts\evaluation\combined-sim-qgc-$RunId"
$ArtifactDirectory = Split-Path -Parent $RunDirectory
$CombinedStageTracePath = Join-Path $RunDirectory "combined-stage-progress.jsonl"
# The Gazebo camera publishes nominally at 10 Hz, but a full 1280x960 BGR
# frame must also cross Docker stdout, inference, RTSP acceptance and the QGC
# post-adoption ACK barrier.  On a contended Windows host the observed
# end-to-end rate can be below 3 Hz.  Scale the sensor deadline with the frame
# budget instead of using a brittle fixed 100-second cutoff.
$SensorStreamTimeoutSeconds = [int][Math]::Max(
    180,
    [Math]::Ceiling($Frames / 2.0) + 30
)
$IntegratedWaitTimeoutMilliseconds = ($SensorStreamTimeoutSeconds + 45) * 1000
# Keep the VTOL evaluator deadlines tied to the requested fixed-wing dwell.
# The recommended 12-second dwell retains the historical 55/70-second budgets,
# while larger accepted values cannot be clipped by those former constants.
$FixedWingReadyTimeoutSeconds = [int][Math]::Max(
    55,
    [Math]::Ceiling($FixedWingHoldSeconds + 43.0)
)
$VtolProcessWaitTimeoutMilliseconds = [int][Math]::Max(
    70000,
    [Math]::Ceiling(($FixedWingHoldSeconds + 58.0) * 1000.0)
)
# The Mission helper performs bounded heartbeat/GPS/navigation/transaction
# setup before entering its own mission observation deadline. Cover both
# phases, including the helper's selectable 600-second maximum, and leave a
# small flush margin for its durable result JSON and redirected streams.
$MissionReadyTimeoutSeconds = $MissionSetupAllowanceSeconds + $MissionTimeoutSeconds
$MissionProcessWaitTimeoutMilliseconds = (
    $MissionSetupAllowanceSeconds + $MissionTimeoutSeconds + 30
) * 1000
if ([string]::IsNullOrWhiteSpace($ContainerName)) {
    $ContainerName = "multidetect-combined-sim-$RunId".ToLowerInvariant()
}
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $RunDirectory "combined-evidence.json"
}
elseif (-not [IO.Path]::IsPathRooted($EvidencePath)) {
    $EvidencePath = Join-Path $Root $EvidencePath
}
if ([string]::IsNullOrWhiteSpace($QgcPortableBin)) {
    $QgcPortableBin = Join-Path $Root ("build\windows-v{0}-release\portable-v{0}\bin" -f $ReleaseLabel)
}
elseif (-not [IO.Path]::IsPathRooted($QgcPortableBin)) {
    $QgcPortableBin = Join-Path $Root $QgcPortableBin
}
$QgcPortableBin = [IO.Path]::GetFullPath($QgcPortableBin)
$EvidencePath = [IO.Path]::GetFullPath($EvidencePath)
$EvidenceParentDirectory = Split-Path -Parent $EvidencePath
$EvidenceSerializationProgressPath = "$EvidencePath.serialization-progress.jsonl"

# All host ports are isolated, loopback-only and deliberately distinct.  They
# are allocated at run time because Docker Desktop and other developer tools
# may occupy any historical fixed port between invocations.  The container
# input port remains fixed inside the disposable PX4 network namespace.
$CombinedSimPortRangeMin = 30000
$CombinedSimPortRangeMax = 60000
$CombinedSimHostPortCount = 9
$PortAllocationMode = "dynamic_loopback"
$OperatorServerPort = $null
$OperatorQgcLocalPort = $null
$Px4TelemetryPort = $null
$Px4InputHostPort = $null
$QgcPx4Port = $null
$QgcRouterPort = $null
$EvaluatorPort = $null
$EvaluatorRouterPort = $null
$RtspPort = $null
$Px4ContainerInputPort = 18570
$ProtectedGroundStationPort = 14550
$EvaluatorSystemId = 250
$EvaluatorComponentId = 191

$PythonCandidates = @(
    (Join-Path $Root "ground-control\MultiDetectGCS\.venv\Scripts\python.exe"),
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$DockerCandidates = @(
    (Get-Command docker -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$QgcExecutable = Join-Path $QgcPortableBin "MultiDetectGCS.exe"
$RtspHelper = Join-Path $QgcPortableBin "MultiDetectReplayRtspPublisher.exe"
$ScenarioManifest = Join-Path $Root "configs\scenarios\river_rescue_vtol_001.scenario.json"
$MissionConfig = Join-Path $Root "configs\missions\fire_patrol.demo.json"
$StreamScript = Join-Path $Root "scripts\gazebo_sensor_stream.py"
$DesktopCaptureValidator = Join-Path $Root "scripts\validate_qgc_desktop_capture.py"
$NativeVideoGrabValidator = Join-Path $Root "scripts\validate_qgc_native_video_grab.py"
$ToolbarLayoutValidator = Join-Path $Root "scripts\validate_qgc_toolbar_layout.py"
$AttachedVtolScript = Join-Path $Root "scripts\vtol_sitl_attached_acceptance.py"
$MissionAcceptanceScript = Join-Path $Root "scripts\vtol_sitl_mission_acceptance.py"
$ModelRoot = Join-Path $Root "simulation\gazebo\models"
$ModelConfig = Join-Path $ModelRoot "standard_vtol_mono_cam\model.config"
$ModelSdf = Join-Path $ModelRoot "standard_vtol_mono_cam\model.sdf"
$WorldAsset = Join-Path $Root "simulation\gazebo\worlds\river_rescue_vtol_001.sdf"
$TerrainDem = Join-Path $Root "simulation\terrain\river_rescue_vtol_001.dem.json"
$FixtureRoot = Join-Path $Root ("build\qgc-replay-fixture-v{0}" -f $ReleaseLabel)
$OnnxModel = Join-Path $FixtureRoot "model\synthetic-fire-nx6-hil.onnx"
$ModelManifest = Join-Path $FixtureRoot "model\synthetic-fire-nx6-hil.manifest.json"

$RouterStdout = Join-Path $RunDirectory "router.stdout.jsonl"
$RouterStderr = Join-Path $RunDirectory "router.stderr.log"
$QgcStdout = Join-Path $RunDirectory "qgc.stdout.log"
$QgcStderr = Join-Path $RunDirectory "qgc.stderr.log"
$QgcNativeVideoDiagnosticsPath = Join-Path $RunDirectory "qgc-native-video-diagnostics.json"
$QgcVideoBackendSettingsEvidencePath = Join-Path $RunDirectory "qgc-video-backend-settings.json"
$IntegratedStdout = Join-Path $RunDirectory "integrated-sim-qgc.stdout.jsonl"
$IntegratedStderr = Join-Path $RunDirectory "integrated-sim-qgc.stderr.log"
$IntegratedResult = Join-Path $RunDirectory "integrated-sim-qgc.result.json"
$IntegratedAudit = Join-Path $RunDirectory "integrated-sim-qgc.audit.jsonl"
$VtolStdout = Join-Path $RunDirectory "vtol-attached.stdout.jsonl"
$VtolStderr = Join-Path $RunDirectory "vtol-attached.stderr.log"
$VtolResult = Join-Path $RunDirectory "vtol-attached.result.json"
$FixedWingReady = Join-Path $RunDirectory "fixed-wing-ready.json"
$MissionStdout = Join-Path $RunDirectory "vtol-mission.stdout.jsonl"
$MissionStderr = Join-Path $RunDirectory "vtol-mission.stderr.log"
$MissionResult = Join-Path $RunDirectory "vtol-mission.result.json"
$MissionJournal = Join-Path $RunDirectory "vtol-mission.transaction.jsonl"
$MissionActiveReady = Join-Path $RunDirectory "vtol-mission-active-ready.json"
$RuntimePreflightEvidencePath = Join-Path $RunDirectory "runtime-preflight.json"
$ContainerLog = Join-Path $RunDirectory "container.log"
$ContainerLogStderr = Join-Path $RunDirectory "container.log.stderr"
$ContainerInspectPath = Join-Path $RunDirectory "container.inspect.sanitized.json"
$InitialScreenshot = Join-Path $RunDirectory "qgc-before-pip-swap.png"
$PipSwappedScreenshot = Join-Path $RunDirectory "qgc-after-pip-swap.png"
$FixedWingScreenshot = Join-Path $RunDirectory "qgc-fly-view-fixed-wing.png"
$MissionActiveScreenshot = Join-Path $RunDirectory "qgc-fly-view-auto-mission.png"
$LiveVideoEvidencePath = Join-Path $RunDirectory "qgc-live-video-surface-proof.json"
$NativeVideoGrabPrefix = Join-Path $RunDirectory "qgc-native-video-item"
$NativeVideoPipSwappedCapture = "$NativeVideoGrabPrefix-pip-swapped.png"
$NativeVideoFixedWingCapture = "$NativeVideoGrabPrefix-fixed-wing.png"
$NativeVideoGrabEvidencePath = Join-Path $RunDirectory "qgc-native-video-grab-proof.json"
$ToolbarClosedScreenshot = Join-Path $RunDirectory "qgc-native-toolbar-closed.png"
$ToolbarOpenScreenshot = Join-Path $RunDirectory "qgc-native-toolbar-drawer-open.png"
$ToolbarObservationPath = Join-Path $RunDirectory "qgc-native-toolbar-indicator-observation.json"
$ToolbarEvidencePath = Join-Path $RunDirectory "qgc-native-toolbar-indicator-evidence.json"
$QgcIsolatedSettings = Join-Path ([IO.Path]::GetTempPath()) "MultiDetectGCS\isolated-hil-settings\QGroundControl\MultiDetectGCS Isolated HIL.ini"
$QgcNativeVideoLoggingCategories = @(
    "MultiDetect.Operator",
    "API.QGCApplication",
    "Video.VideoManager",
    "Video.GStreamer.GStreamer",
    "Video.GStreamer.GstVideoReceiver",
    "Video.GStreamer.QGCQVideoSinkController",
    "Video.GStreamer.QgcQVideoSink",
    "Video.GStreamer.DecoderRanks",
    "Video.GStreamer.FrameMap",
    "Video.GStreamer.HwBuffers.Factory",
    "Video.GStreamer.HwBuffers.Config",
    "Video.GStreamer.HwBuffers.Facade",
    "Video.GStreamer.HwBuffers.GstD3D11Bridge",
    "Video.GStreamer.HwBuffers.GstD3D11Buf",
    "Video.GStreamer.HwBuffers.GstD3D12Bridge",
    "Video.GStreamer.HwBuffers.GstD3D12Buf"
)

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Write-Utf8NoBomText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowNull()][string]$Text
    )

    # Windows PowerShell 5.1's `Set-Content -Encoding utf8` emits a UTF-8 BOM.
    # That is accepted by some readers but is not valid at the start of a JSON
    # text for strict parsers.  Keep the evidence envelope interoperable by
    # writing it explicitly with a BOM-free UTF-8 encoding.
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, [string]$Text, $encoding)
}

function Write-CombinedStageTrace {
    param([string]$Message)
    # Best-effort stage telemetry for long-running Windows acceptance runs. It
    # is deliberately sidecar-only and never feeds a gate or a control path.
    try {
        Add-Content -LiteralPath $CombinedStageTracePath `
            -Value ("{0}`t{1}" -f (Get-Date).ToUniversalTime().ToString('o'), $Message) `
            -Encoding utf8 -ErrorAction SilentlyContinue
    }
    catch { }
}

function ConvertTo-WindowsProcessArgument {
    param([AllowNull()][string]$Value)

    if ($null -eq $Value) { return '""' }
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }

    # ProcessStartInfo.Arguments is the only portable launch property on
    # Windows PowerShell/.NET Framework.  Quote arguments according to the
    # CommandLineToArgvW rules so paths, whitespace and the multi-line `sh -c`
    # payload survive the fallback without Start-Process flattening surprises.
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            # A literal quote must always be preceded by an odd run of
            # backslashes, including the zero-backslash case.  The previous
            # branch only emitted the escape run when a backslash was already
            # pending, so a nested quote in the multi-line `sh -c` payload
            # closed the outer ProcessStartInfo argument under Windows
            # PowerShell 5.1/.NET Framework.  Docker then received fragments
            # of the shell script as argv[0]/argv[1] (for example `ahostsv4`)
            # and `/bin/sh` failed with an ``expecting ")"`` syntax error.
            [void]$builder.Append((('\' * ($backslashes * 2 + 1)) -join ''))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append((('\' * $backslashes) -join ''))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append((('\' * ($backslashes * 2)) -join ''))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-CapturedCommand {
    param([string]$FilePath, [string[]]$ArgumentList, [int]$TimeoutSeconds = 0)
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($TimeoutSeconds -gt 0) {
            $process = $null
            try {
                # Start-Process flattens -ArgumentList into one command line on
                # Windows. That corrupts the multi-line `sh -c` payload used by
                # docker run. Newer .NET exposes ArgumentList and preserves
                # every argv element; Windows PowerShell/.NET Framework falls
                # back to a CommandLineToArgvW-compatible quoted Arguments
                # string below.
                $startInfo = [Diagnostics.ProcessStartInfo]::new()
                $startInfo.FileName = $FilePath
                $startInfo.UseShellExecute = $false
                $startInfo.CreateNoWindow = $true
                $startInfo.RedirectStandardOutput = $true
                $startInfo.RedirectStandardError = $true
                $argumentListProperty = $startInfo.GetType().GetProperty("ArgumentList")
                if ($null -ne $argumentListProperty) {
                    $nativeArgumentList = $argumentListProperty.GetValue($startInfo, $null)
                    foreach ($argument in $ArgumentList) {
                        [void]$nativeArgumentList.Add([string]$argument)
                    }
                }
                else {
                    $startInfo.Arguments = @(
                        $ArgumentList | ForEach-Object {
                            ConvertTo-WindowsProcessArgument -Value ([string]$_)
                        }
                    ) -join ' '
                }
                $process = [Diagnostics.Process]::new()
                $process.StartInfo = $startInfo
                Assert-True ($process.Start()) "Unable to start command: $FilePath"
                $stdoutTask = $process.StandardOutput.ReadToEndAsync()
                $stderrTask = $process.StandardError.ReadToEndAsync()
                if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
                    $process.Kill()
                    $process.WaitForExit()
                    throw "Command timed out after $TimeoutSeconds seconds: $FilePath"
                }
                $stdoutText = $stdoutTask.GetAwaiter().GetResult()
                $stderrText = $stderrTask.GetAwaiter().GetResult()
                $lines = @(
                    @($stdoutText -split "`r?`n" | Where-Object { $_ -ne "" })
                    @($stderrText -split "`r?`n" | Where-Object { $_ -ne "" })
                )
                $exitCode = $process.ExitCode
            }
            finally {
                if ($null -ne $process) { $process.Dispose() }
            }
        }
        else {
            $lines = @(& $FilePath @ArgumentList 2>&1)
            $exitCode = $LASTEXITCODE
        }
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Lines = @($lines | ForEach-Object { "$_" }) }
}

function Save-CommandOutputToFile {
    <#
    Capture a diagnostic command without materialising its stdout in a
    PowerShell object. Docker/Gazebo can emit very large ANSI progress streams
    (sometimes a single 20+ MB line); Invoke-CapturedCommand's string/array
    path makes that output grow quadratically while joining and serialising.
    Direct redirection keeps the acceptance runner bounded and leaves the
    original command exit code available to the evidence writer.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][string]$OutputPath,
        [Parameter(Mandatory)][string]$ErrorPath,
        [ValidateRange(1, 600)][int]$TimeoutSeconds = 30
    )

    foreach ($path in @($OutputPath, $ErrorPath)) {
        $parent = Split-Path -Parent $path
        if (-not [string]::IsNullOrWhiteSpace($parent)) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }

    $process = $null
    try {
        # The log command has only simple argv values (subcommand, --tail,
        # bounded count and container name), so Start-Process' standard
        # ArgumentList handling is sufficient here. The complex multiline
        # docker `sh -c` payload continues to use Invoke-CapturedCommand's
        # CommandLineToArgvW fallback above.
        $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $OutputPath -RedirectStandardError $ErrorPath
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch { }
            try { $process.WaitForExit() } catch { }
            return 124
        }
        return [int]$process.ExitCode
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

function Get-Sha256OrNull {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    Write-CombinedStageTrace -Message ("hash-start={0}" -f $Path)
    # Start-Process with redirected stdout/stderr can release its file handle
    # a few milliseconds after WaitForExit() returns.  Hashing immediately
    # would then turn an otherwise useful failure/success record into a wrapper
    # exception ("file is being used by another process").  Retry the read
    # briefly, but keep the evidence writer fail-closed by returning null if a
    # producer still owns the file after the bounded grace period.
    $deadline = (Get-Date).AddSeconds(8)
    do {
        $stream = $null
        $hasher = $null
        try {
            # Use the .NET stream API instead of the PowerShell Utility
            # Get-FileHash function.  The acceptance launcher also runs under
            # Windows PowerShell 5.1 hosts where command auto-loading can be
            # unavailable after the parent environment changes PATH; the
            # repository-owned implementation remains deterministic and shares
            # read access with redirected child diagnostics.
            $stream = [IO.File]::Open(
                $Path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::ReadWrite
            )
            $hasher = [Security.Cryptography.SHA256]::Create()
            $hash = ([BitConverter]::ToString($hasher.ComputeHash($stream)) -replace "-", "")
            Write-CombinedStageTrace -Message ("hash-done={0}" -f $Path)
            return $hash
        }
        catch {
            Write-CombinedStageTrace -Message ("hash-error={0};type={1}" -f $Path, $_.Exception.GetType().FullName)
            if ((Get-Date) -ge $deadline) {
                Write-CombinedStageTrace -Message ("hash-timeout={0}" -f $Path)
                return $null
            }
            Start-Sleep -Milliseconds 100
        }
        finally {
            if ($null -ne $hasher) { $hasher.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
        }
    } while ($true)
}

function Wait-FileReadable {
    param([string]$Path, [int]$TimeoutMilliseconds = 8000)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $true }
    $deadline = (Get-Date).AddMilliseconds($TimeoutMilliseconds)
    while ((Get-Date) -lt $deadline) {
        $stream = $null
        try {
            # FileShare.ReadWrite succeeds for ordinary readers but fails while
            # a redirected child still owns the file with an exclusive handle.
            $stream = [IO.File]::Open(
                $Path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::ReadWrite
            )
            return $true
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    return $false
}

function Get-StringSha256 {
    param([string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace "-", "") }
    finally { $sha.Dispose(); [Array]::Clear($bytes, 0, $bytes.Length) }
}

function Get-CompositeSha256 {
    param([string[]]$Paths)
    $rows = foreach ($path in $Paths) {
        $full = [IO.Path]::GetFullPath($path)
        "{0}`t{1}" -f $full, (Get-Sha256OrNull -Path $full)
    }
    return Get-StringSha256 -Value ($rows -join "`n")
}

function Get-TextIfPresent {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    try { return Get-Content -LiteralPath $Path -Raw }
    catch { return "" }
}

function Get-FileDiagnosticRecord {
    param([string]$Path, [int]$MaximumTailCharacters = 32768)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{
            path = $Path
            exists = $false
            bytes = $null
            sha256 = $null
            tail = ""
            tail_truncated = $false
        }
    }
    $item = Get-Item -LiteralPath $Path
    $text = Get-TextIfPresent -Path $Path
    $truncated = $text.Length -gt $MaximumTailCharacters
    $tail = if ($truncated) {
        $text.Substring($text.Length - $MaximumTailCharacters)
    }
    else {
        $text
    }
    return [ordered]@{
        path = $Path
        exists = $true
        bytes = $item.Length
        sha256 = Get-Sha256OrNull -Path $Path
        tail = $tail
        tail_truncated = $truncated
    }
}

function ConvertTo-JsonSafeEvidenceValue {
    <#
    Windows PowerShell 5.1's ConvertTo-Json can allocate without a useful
    bound when a deep evidence graph mixes OrderedDictionary, PSCustomObject
    and adapted .NET members.  The combined-SIM result contains only JSON
    data, so project it to dictionaries/arrays/scalars first.  This keeps the
    complete child evidence in its dedicated files while making the aggregate
    writer deterministic and resistant to accidental reference cycles.
    #>
    param(
        [AllowNull()][object]$Value,
        [int]$Depth = 30,
        [Collections.Generic.HashSet[int]]$Visited,
        [int]$MaximumCollectionItems = 4096,
        [hashtable]$ProjectionState
    )

    if ($null -eq $Visited) {
        $Visited = [Collections.Generic.HashSet[int]]::new()
    }
    if ($null -eq $ProjectionState) {
        $ProjectionState = @{
            node_count = 0
            maximum_nodes = 20000
            truncated = $false
        }
    }
    if ($null -eq $Value) { return $null }

    if ([int]$ProjectionState.node_count -ge [int]$ProjectionState.maximum_nodes) {
        $ProjectionState.truncated = $true
        return "<projection-truncated:$($ProjectionState.maximum_nodes)>"
    }
    $ProjectionState.node_count = [int]$ProjectionState.node_count + 1

    $type = $Value.GetType()
    if ($Value -is [string]) {
        # Get-Content/Get-Content -Raw can attach FileSystem provider ETS
        # members (PSPath, PSProvider, ReadCount, ...).  Returning that adapted
        # value verbatim lets a later ConvertTo-Json walk the provider graph;
        # copy the scalar so the aggregate contains a plain System.String.
        return [string]::Copy([string]$Value)
    }
    if (
        $Value -is [bool] -or $Value -is [char] -or
        $Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64] -or $Value -is [single] -or
        $Value -is [double] -or $Value -is [decimal]
    ) {
        return $Value
    }
    if ($Value -is [datetime]) {
        return $Value.ToUniversalTime().ToString('o')
    }
    if ($Value -is [datetimeoffset]) {
        return $Value.ToUniversalTime().ToString('o')
    }
    if ($Depth -le 0) {
        return [string]$Value
    }

    $trackReference = (-not $type.IsValueType -and -not ($Value -is [string]))
    $referenceId = 0
    if ($trackReference) {
        $referenceId = [Runtime.CompilerServices.RuntimeHelpers]::GetHashCode($Value)
        if (-not $Visited.Add($referenceId)) {
            return "<cycle>"
        }
    }

    try {
        if ($Value -is [Collections.IDictionary]) {
            $result = [ordered]@{}
            $entryCount = 0
            foreach ($key in $Value.Keys) {
                if ([int]$ProjectionState.node_count -ge [int]$ProjectionState.maximum_nodes) {
                    $ProjectionState.truncated = $true
                    $result["<projection-truncated:$($ProjectionState.maximum_nodes)>"] = $true
                    break
                }
                if ($entryCount -ge $MaximumCollectionItems) {
                    $result["<dictionary-truncated:$MaximumCollectionItems>"] = $true
                    break
                }
                $result[[string]$key] = ConvertTo-JsonSafeEvidenceValue `
                    -Value $Value[$key] -Depth ($Depth - 1) -Visited $Visited `
                    -MaximumCollectionItems $MaximumCollectionItems -ProjectionState $ProjectionState
                $entryCount++
            }
            return $result
        }

        if ($Value -is [Collections.IEnumerable]) {
            $items = [Collections.Generic.List[object]]::new()
            $itemCount = 0
            foreach ($item in $Value) {
                if ([int]$ProjectionState.node_count -ge [int]$ProjectionState.maximum_nodes) {
                    $ProjectionState.truncated = $true
                    [void]$items.Add("<projection-truncated:$($ProjectionState.maximum_nodes)>")
                    break
                }
                if ($itemCount -ge $MaximumCollectionItems) {
                    [void]$items.Add("<collection-truncated:$MaximumCollectionItems>")
                    break
                }
                [void]$items.Add((ConvertTo-JsonSafeEvidenceValue `
                    -Value $item -Depth ($Depth - 1) -Visited $Visited `
                    -MaximumCollectionItems $MaximumCollectionItems -ProjectionState $ProjectionState))
                $itemCount++
            }
            return ,$items.ToArray()
        }

        # Only ConvertFrom-Json PSCustomObject property bags are expected here.
        # PowerShell adapts arbitrary .NET objects through PSObject.Properties;
        # walking those properties can expose process handles, modules, thread
        # collections or other cyclic runtime graphs and defeat the JSON bound.
        # Preserve the evidence shape with a typed marker instead of traversing
        # an object that is outside the JSON-safe allow-list.
        $propertyBagType = $type.FullName -in @(
            'System.Management.Automation.PSCustomObject',
            'System.Management.Automation.PSObject'
        )
        if (-not $propertyBagType) {
            return "<unsupported-type:$($type.FullName)>"
        }

        $object = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            if ([int]$ProjectionState.node_count -ge [int]$ProjectionState.maximum_nodes) {
                $ProjectionState.truncated = $true
                $object["<projection-truncated:$($ProjectionState.maximum_nodes)>"] = $true
                break
            }
            if ($property.MemberType -notin @('NoteProperty', 'Property', 'AliasProperty', 'ScriptProperty')) {
                continue
            }
            $object[$property.Name] = ConvertTo-JsonSafeEvidenceValue `
                -Value $property.Value -Depth ($Depth - 1) -Visited $Visited `
                -MaximumCollectionItems $MaximumCollectionItems -ProjectionState $ProjectionState
        }
        if ($object.Count -gt 0) {
            return $object
        }
        return [string]$Value
    }
    finally {
        if ($trackReference) {
            [void]$Visited.Remove($referenceId)
        }
    }
}

function Write-EvidenceSerializationProgress {
    param([string]$Message)
    # This sidecar is deliberately plain text and best-effort.  It survives a
    # host PowerShell 5.1 ConvertTo-Json stall, making the exact top-level field
    # observable without contaminating the normative aggregate JSON.
    try {
        Add-Content -LiteralPath $EvidenceSerializationProgressPath `
            -Value ("{0}`t{1}" -f (Get-Date).ToUniversalTime().ToString('o'), $Message) `
            -Encoding utf8 -ErrorAction SilentlyContinue
    }
    catch { }
}

function Get-JsonEventFromFile {
    param([string]$Path, [string]$Event)
    Assert-True (Test-Path -LiteralPath $Path) "Expected JSONL is missing: $Path"
    $matches = @()
    foreach ($line in @(Get-Content -LiteralPath $Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith("{")) { continue }
        try { $record = $trimmed | ConvertFrom-Json }
        catch { continue }
        if ([string]::IsNullOrWhiteSpace($Event) -or $record.event -eq $Event) { $matches += $record }
    }
    Assert-True ($matches.Count -gt 0) "JSONL did not contain event '$Event': $Path"
    return $matches[-1]
}

function Get-UdpPortOwners {
    param([int]$Port)
    try {
        return @(Get-NetUDPEndpoint -LocalPort $Port -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique | Sort-Object)
    }
    catch { return @() }
}

function Get-TcpPortOwners {
    param([int]$Port)
    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique | Sort-Object)
    }
    catch { return @() }
}

function Test-CombinedSimLoopbackPortFree {
    param([Parameter(Mandatory)][int]$Port)

    # The endpoint queries are only a fast diagnostic.  Bind both protocols as
    # the authoritative check so a Docker Desktop helper or another process
    # cannot win a race between discovery and the later child launches.
    if (@(Get-UdpPortOwners -Port $Port).Count -gt 0) { return $false }
    if (@(Get-TcpPortOwners -Port $Port).Count -gt 0) { return $false }

    $udp = $null
    $tcp = $null
    try {
        $udp = [System.Net.Sockets.UdpClient]::new()
        $udp.Client.Bind([System.Net.IPEndPoint]::new([System.Net.IPAddress]::Loopback, $Port))
        $tcp = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $Port
        )
        $tcp.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $tcp) {
            try { $tcp.Stop() } catch { }
        }
        if ($null -ne $udp) {
            try { $udp.Dispose() } catch { }
        }
    }
}

function Get-FreeCombinedSimPorts {
    param(
        [Parameter(Mandatory)][int]$Count,
        [Parameter(Mandatory)][int]$Minimum,
        [Parameter(Mandatory)][int]$Maximum,
        [int[]]$Excluded = @()
    )

    Assert-True ($Count -gt 0) "Combined SIM dynamic port count must be positive."
    Assert-True ($Minimum -ge 1024 -and $Maximum -le 65535 -and $Minimum -le $Maximum) `
        "Combined SIM dynamic port range is invalid."

    $selected = New-Object System.Collections.Generic.List[int]
    $attempts = 0
    while ($selected.Count -lt $Count -and $attempts -lt 10000) {
        $attempts++
        $candidate = Get-Random -Minimum $Minimum -Maximum ($Maximum + 1)
        if ($Excluded -contains $candidate -or $selected.Contains($candidate)) { continue }
        if (Test-CombinedSimLoopbackPortFree -Port $candidate) {
            [void]$selected.Add($candidate)
        }
    }

    Assert-True ($selected.Count -eq $Count) `
        "Unable to allocate $Count free loopback ports in [$Minimum,$Maximum] after $attempts attempts."
    return $selected.ToArray()
}

function Get-CrashDumpSignatures {
    $paths = @(
        (Join-Path $env:LOCALAPPDATA "CrashDumps"),
        (Join-Path $env:TEMP "MultiDetectGCS")
    )
    $rows = foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        Get-ChildItem -LiteralPath $path -Filter "*MultiDetectGCS*.dmp" -File -ErrorAction SilentlyContinue |
            ForEach-Object { "{0}|{1}|{2}" -f $_.FullName, $_.Length, $_.LastWriteTimeUtc.Ticks }
    }
    return @($rows | Sort-Object)
}

function Wait-ProcessCompleted {
    param([Diagnostics.Process]$Process, [int]$TimeoutMilliseconds, [string]$Name)
    Assert-True ($null -ne $Process) "$Name process was not started."
    if (-not $Process.WaitForExit($TimeoutMilliseconds)) {
        throw "$Name did not exit within $TimeoutMilliseconds ms."
    }
    $Process.Refresh()
    return $Process.ExitCode
}

function Wait-TextPattern {
    param([string[]]$Paths, [string]$Pattern, [int]$TimeoutSeconds, [Diagnostics.Process[]]$Processes = @())
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        # Read first. A child can flush its final observable and exit between
        # polling ticks; checking HasExited first would discard valid tail
        # evidence and turn a clean completion into a wrapper race failure.
        $text = ($Paths | ForEach-Object { Get-TextIfPresent -Path $_ }) -join "`n"
        if ($text -match $Pattern) { return $text }
        foreach ($process in $Processes) {
            if ($null -ne $process -and $process.HasExited) {
                $text = ($Paths | ForEach-Object { Get-TextIfPresent -Path $_ }) -join "`n"
                if ($text -match $Pattern) { return $text }
                throw "Process $($process.Id) exited while waiting for '$Pattern'."
            }
        }
        Start-Sleep -Milliseconds 150
    }
    throw "Timed out waiting for '$Pattern' in $($Paths -join ', ')."
}

function Get-TextPatternCount {
    param([string[]]$Paths, [string]$Pattern)
    $text = ($Paths | ForEach-Object { Get-TextIfPresent -Path $_ }) -join "`n"
    return [regex]::Matches($text, $Pattern).Count
}

function Wait-TextPatternCountGreaterThan {
    param(
        [string[]]$Paths,
        [string]$Pattern,
        [int]$BaselineCount,
        [int]$TimeoutSeconds,
        [Diagnostics.Process[]]$Processes = @()
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $count = Get-TextPatternCount -Paths $Paths -Pattern $Pattern
        if ($count -gt $BaselineCount) { return $count }
        foreach ($process in $Processes) {
            if ($null -ne $process -and $process.HasExited) {
                $count = Get-TextPatternCount -Paths $Paths -Pattern $Pattern
                if ($count -gt $BaselineCount) { return $count }
                throw "Process $($process.Id) exited while waiting for '$Pattern' count to exceed $BaselineCount."
            }
        }
        Start-Sleep -Milliseconds 150
    }
    throw "Timed out waiting for '$Pattern' count to exceed $BaselineCount in $($Paths -join ', ')."
}

function Get-LatestQgcRuntimeStatusSequence {
    param([string[]]$Paths)
    $text = ($Paths | ForEach-Object { Get-TextIfPresent -Path $_ }) -join "`n"
    $matches = [regex]::Matches($text, 'runtime status adopted and acknowledged sequence=(\d+)')
    if ($matches.Count -eq 0) { return 0 }
    return [int]$matches[$matches.Count - 1].Groups[1].Value
}

function Wait-QgcRuntimeStatusSequence {
    param(
        [string[]]$Paths,
        [int]$MinimumSequence,
        [int]$TimeoutSeconds,
        [Diagnostics.Process[]]$Processes = @()
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $sequence = Get-LatestQgcRuntimeStatusSequence -Paths $Paths
        if ($sequence -ge $MinimumSequence) { return $sequence }
        foreach ($process in $Processes) {
            if ($null -ne $process -and $process.HasExited) {
                $sequence = Get-LatestQgcRuntimeStatusSequence -Paths $Paths
                if ($sequence -ge $MinimumSequence) { return $sequence }
                throw "Process $($process.Id) exited before QGC type-24 sequence reached $MinimumSequence."
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for QGC post-adoption type-24 sequence $MinimumSequence."
}

function Wait-FileCreated {
    param([string]$Path, [int]$TimeoutSeconds, [Diagnostics.Process[]]$Processes = @())
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) { return }
        foreach ($process in $Processes) {
            if ($null -ne $process -and $process.HasExited) {
                $exitCode = $null
                try { $exitCode = $process.ExitCode } catch { }
                $diagnostic = "Process $($process.Id) exited before creating $Path (exit_code=$exitCode)."
                if ($null -ne $vtolProcess -and $process.Id -eq $vtolProcess.Id) {
                    $diagnostic += " stdout=$VtolStdout stderr=$VtolStderr result=$VtolResult."
                }
                throw $diagnostic
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for file: $Path"
}

function Get-NormalizedPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\').ToLowerInvariant()
}

function Get-OwnedContainerEvidence {
    param([string]$Docker, [string]$Name, [string]$ExpectedId)
    $inspect = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("inspect", $Name)
    Assert-True ($inspect.ExitCode -eq 0) "Owned combined SIM container disappeared."
    $records = ($inspect.Lines -join "`n") | ConvertFrom-Json
    Assert-True ($records.Count -eq 1) "Docker inspect returned an unexpected container set."
    $record = $records[0]
    $mounts = @($record.Mounts)
    $expectedMounts = @{
        "/opt/multidetect-gazebo-models" = Get-NormalizedPath -Path $ModelRoot
        "/opt/px4-gazebo/share/gz/worlds/default.sdf" = Get-NormalizedPath -Path $WorldAsset
        "/workspace/scripts" = Get-NormalizedPath -Path (Join-Path $Root "scripts")
    }
    $mountChecks = @()
    foreach ($mount in $mounts) {
        $expected = $expectedMounts["$($mount.Destination)"]
        $mountChecks += [ordered]@{
            source = "$($mount.Source)"
            destination = "$($mount.Destination)"
            read_only = ($mount.RW -eq $false)
            source_matches = ($null -ne $expected -and (Get-NormalizedPath -Path "$($mount.Source)") -eq $expected)
        }
    }
    $portBinding = $record.HostConfig.PortBindings."$Px4ContainerInputPort/udp"
    $envRows = @($record.Config.Env)
    $checks = [ordered]@{
        exact_container_id = ($record.Id -eq $ExpectedId)
        exact_container_name = ($record.Name -eq "/$Name")
        exact_pinned_image = ($record.Config.Image -eq $ImageReference)
        owned_purpose_label = ($record.Config.Labels."multidetect.purpose" -eq $PurposeLabel)
        running = ($record.State.Running -eq $true)
        not_privileged = ($record.HostConfig.Privileged -eq $false)
        bridge_network = ($record.HostConfig.NetworkMode -in @("bridge", "default"))
        no_devices = (@($record.HostConfig.Devices).Count -eq 0)
        all_capabilities_dropped = (@($record.HostConfig.CapDrop) -contains "ALL")
        no_new_privileges = (@($record.HostConfig.SecurityOpt) -contains "no-new-privileges:true")
        exactly_three_mounts = ($mounts.Count -eq 3)
        mounts_match_readonly_whitelist = ($mountChecks.Count -eq 3 -and @($mountChecks | Where-Object { -not $_.read_only -or -not $_.source_matches }).Count -eq 0)
        exact_loopback_input_binding = (
            @($record.HostConfig.PortBindings.PSObject.Properties).Count -eq 1 -and
            @($portBinding).Count -eq 1 -and
            $portBinding[0].HostIp -eq "127.0.0.1" -and
            $portBinding[0].HostPort -eq "$Px4InputHostPort"
        )
        exact_px4_gz_models = ($envRows -contains "PX4_GZ_MODELS=/opt/multidetect-gazebo-models")
        exact_px4_model = ($envRows -contains "PX4_SIM_MODEL=gz_standard_vtol_mono_cam")
        exact_px4_autostart = ($envRows -contains "PX4_SYS_AUTOSTART=4004")
        exact_world_alias = ($envRows -contains "PX4_GZ_WORLD=default")
    }
    $failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
    Assert-True ($failed.Count -eq 0) "Owned container checks failed: $($failed -join ', ')"
    $sanitized = [ordered]@{
        id = $record.Id
        name = $record.Name
        image = $record.Config.Image
        image_id = $record.Image
        purpose_label = $record.Config.Labels."multidetect.purpose"
        state = [ordered]@{ running = $record.State.Running; status = $record.State.Status }
        network_mode = $record.HostConfig.NetworkMode
        privileged = $record.HostConfig.Privileged
        cap_drop = @($record.HostConfig.CapDrop)
        security_opt = @($record.HostConfig.SecurityOpt)
        port_binding = [ordered]@{ container = $Px4ContainerInputPort; host = $Px4InputHostPort; host_ip = "127.0.0.1" }
        mounts = $mountChecks
        checks = $checks
    }
    $sanitized | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ContainerInspectPath -Encoding utf8
    return $sanitized
}

function Stop-OwnedContainer {
    param([string]$Docker, [string]$Name, [string]$ExpectedId)
    if ([string]::IsNullOrWhiteSpace($ExpectedId)) { return $false }
    # Docker Desktop can transiently return 502 while its Linux engine is
    # restarting.  Keep cleanup bounded so the acceptance wrapper still writes
    # a diagnostic evidence record instead of hanging indefinitely in a
    # best-effort teardown call.
    $inspect = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("inspect", "--format", "{{.Id}}", $Name) -TimeoutSeconds 20
    if ($inspect.ExitCode -ne 0) { return $true }
    $actual = ($inspect.Lines | Select-Object -Last 1).Trim()
    Assert-True ($actual -eq $ExpectedId) "Refusing to stop container because its ID changed."
    $stop = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("stop", "--time", "2", $Name) -TimeoutSeconds 20
    Assert-True ($stop.ExitCode -eq 0) "Unable to stop owned combined SIM container."
    $remove = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("rm", $Name) -TimeoutSeconds 20
    Assert-True ($remove.ExitCode -eq 0) "Unable to remove stopped owned combined SIM container."
    return $true
}

function Get-GazeboTopicPublisherState {
    param(
        [string]$Docker,
        [string]$ContainerName,
        [string]$Topic
    )
    $info = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @(
        "exec", $ContainerName, "gz", "topic", "-i", "-t", $Topic
    ) -TimeoutSeconds 10
    $text = $info.Lines -join "`n"
    $publisherPresent = $info.ExitCode -eq 0 -and
        $text -match "(?im)^\s*Publishers(?:\s*\[[^\]]+\])?\s*:"
    [pscustomobject]@{
        topic = $Topic
        exit_code = $info.ExitCode
        publisher_present = [bool]$publisherPresent
        lines = @($info.Lines)
    }
}

if (-not ([System.Management.Automation.PSTypeName]'MultiDetectWindow.Native').Type) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace MultiDetectWindow {
    public static class Native {
        [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] public struct WINDOWPLACEMENT {
            public int length, flags, showCmd;
            public POINT ptMinPosition, ptMaxPosition;
            public RECT rcNormalPosition;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] public struct MONITORINFOEX {
            public int cbSize;
            public RECT rcMonitor, rcWork;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szDevice;
        }
        public sealed class OccluderInfo {
            public long Handle;
            public uint ProcessId;
            public RECT Rect;
            public string WindowTitle;
            public string ClassName;
        }
        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
        [StructLayout(LayoutKind.Sequential)] private struct MOUSEINPUT {
            public int dx, dy;
            public uint mouseData, dwFlags, time;
            public UIntPtr dwExtraInfo;
        }
        [StructLayout(LayoutKind.Explicit)] private struct INPUTUNION {
            [FieldOffset(0)] public MOUSEINPUT mouse;
        }
        [StructLayout(LayoutKind.Sequential)] private struct INPUT {
            public uint type;
            public INPUTUNION data;
        }
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
        [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr hWnd, out RECT rect);
        [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr hWnd, ref POINT point);
        [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT point);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll", SetLastError = true)] public static extern bool AttachThreadInput(uint attachThreadId, uint attachToThreadId, bool attach);
        [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hWnd);
        [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
        [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int virtualKey);
        [DllImport("user32.dll")] private static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extraInfo);
        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);
        [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int command);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int count);
        [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT point);
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT placement);
        [DllImport("user32.dll")] public static extern bool SetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT placement);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] public static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int index);
        [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr hWnd, uint flags);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder text, int count);
        [DllImport("user32.dll")] public static extern IntPtr MonitorFromRect(ref RECT rect, uint flags);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFOEX info);
        [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")] public static extern IntPtr GetThreadDpiAwarenessContext();
        [DllImport("user32.dll")] public static extern bool AreDpiAwarenessContextsEqual(IntPtr first, IntPtr second);
        [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr hWnd);
        [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hWnd, IntPtr hdc);
        [DllImport("gdi32.dll", SetLastError = true)] private static extern bool BitBlt(
            IntPtr destination, int destinationX, int destinationY, int width, int height,
            IntPtr source, int sourceX, int sourceY, uint rasterOperation);
        [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, INPUT[] inputs, int size);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
        [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hWnd, int attribute, out int value, int size);
        [DllImport("dwmapi.dll")] public static extern int DwmFlush();

        private static void PulseAltKey() {
            const int VK_MENU = 0x12;
            const uint KEYEVENTF_KEYUP = 0x0002;
            if ((GetAsyncKeyState(VK_MENU) & 0x8000) != 0) {
                return;
            }
            keybd_event((byte)VK_MENU, 0, 0, UIntPtr.Zero);
            try {
                System.Threading.Thread.Sleep(1);
            }
            finally {
                keybd_event((byte)VK_MENU, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
            }
        }

        public static bool ForceForegroundWindow(IntPtr target, bool pulseAlt) {
            if (!IsWindow(target)) {
                return false;
            }
            if (GetForegroundWindow() == target) {
                return true;
            }

            uint targetProcessId = 0;
            uint targetThreadId = GetWindowThreadProcessId(target, out targetProcessId);
            if (targetThreadId == 0) {
                return false;
            }

            IntPtr foreground = GetForegroundWindow();
            uint foregroundProcessId = 0;
            uint foregroundThreadId = foreground == IntPtr.Zero
                ? 0
                : GetWindowThreadProcessId(foreground, out foregroundProcessId);
            uint currentThreadId = GetCurrentThreadId();
            bool currentForegroundAttached = false;
            bool currentTargetAttached = false;
            bool foregroundTargetAttached = false;
            bool foregroundActivated = false;
            bool detachSucceeded = true;
            try {
                if (pulseAlt) {
                    PulseAltKey();
                }
                if (foregroundThreadId != 0 && foregroundThreadId != currentThreadId) {
                    currentForegroundAttached = AttachThreadInput(currentThreadId, foregroundThreadId, true);
                }
                if (targetThreadId != currentThreadId) {
                    currentTargetAttached = AttachThreadInput(currentThreadId, targetThreadId, true);
                }
                if (foregroundThreadId != 0 && foregroundThreadId != targetThreadId) {
                    foregroundTargetAttached = AttachThreadInput(foregroundThreadId, targetThreadId, true);
                }

                ShowWindowAsync(target, 5); // SW_SHOW, preserving the caller-selected placement.
                BringWindowToTop(target);
                SetForegroundWindow(target);
                SetActiveWindow(target);
                SetFocus(target);
                 foregroundActivated = GetForegroundWindow() == target;
                 // A PowerShell process launched from a non-interactive PTY is
                 // not eligible for Windows' foreground-lock exemption.  A
                 // real pointer click on the already-raised QGC client is the
                 // deterministic fallback and remains bound to this HWND.
                 if (!foregroundActivated) {
                     foregroundActivated = ActivateBySyntheticClick(target);
                 }
            }
            finally {
                if (foregroundTargetAttached) {
                    detachSucceeded = AttachThreadInput(foregroundThreadId, targetThreadId, false) && detachSucceeded;
                }
                if (currentTargetAttached) {
                    detachSucceeded = AttachThreadInput(currentThreadId, targetThreadId, false) && detachSucceeded;
                }
                if (currentForegroundAttached) {
                    detachSucceeded = AttachThreadInput(currentThreadId, foregroundThreadId, false) && detachSucceeded;
                }
            }
             return foregroundActivated && detachSucceeded && GetForegroundWindow() == target;
         }

         public static bool ActivateBySyntheticClick(IntPtr target) {
             if (!IsWindow(target) || !IsWindowVisible(target)) {
                 return false;
             }
             RECT rect;
             if (!GetWindowRect(target, out rect)) {
                 return false;
             }
             int width = rect.Right - rect.Left;
             int height = rect.Bottom - rect.Top;
             if (width < 4 || height < 4) {
                 return false;
             }
             POINT previous;
             if (!GetCursorPos(out previous)) {
                 return false;
             }
             int x = rect.Left + width / 2;
             int y = rect.Top + height / 2;
             bool activated = false;
             try {
                 if (!SetCursorPos(x, y)) {
                     return false;
                 }
                 INPUT[] inputs = new INPUT[2];
                 inputs[0].type = 0;
                 inputs[0].data.mouse.dwFlags = 0x0002; // MOUSEEVENTF_LEFTDOWN
                 inputs[1].type = 0;
                 inputs[1].data.mouse.dwFlags = 0x0004; // MOUSEEVENTF_LEFTUP
                 if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT))) != inputs.Length) {
                     return false;
                 }
                 System.Threading.Thread.Sleep(120);
                 activated = GetForegroundWindow() == target;
             }
             finally {
                 SetCursorPos(previous.X, previous.Y);
             }
             return activated;
         }

        public static bool CaptureScreenRectangle(
            IntPtr destination, int sourceX, int sourceY, int width, int height) {
            const uint SRCCOPY = 0x00CC0020;
            const uint CAPTUREBLT = 0x40000000;
            IntPtr source = GetDC(IntPtr.Zero);
            if (source == IntPtr.Zero) {
                return false;
            }
            try {
                return BitBlt(destination, 0, 0, width, height, source, sourceX, sourceY, SRCCOPY | CAPTUREBLT);
            }
            finally {
                ReleaseDC(IntPtr.Zero, source);
            }
        }

        private static bool Intersects(RECT first, RECT second) {
            return first.Left < second.Right && first.Right > second.Left
                && first.Top < second.Bottom && first.Bottom > second.Top;
        }

        public static OccluderInfo[] GetExternalOccluders(IntPtr target, RECT captureRect, uint targetProcessId) {
            List<OccluderInfo> occluders = new List<OccluderInfo>();
            bool targetReached = false;
            EnumWindowsProc callback = delegate(IntPtr candidate, IntPtr unused) {
                if (candidate == target) {
                    targetReached = true;
                    return false;
                }
                if (!IsWindowVisible(candidate) || IsIconic(candidate)) {
                    return true;
                }
                int cloaked = 0;
                if (DwmGetWindowAttribute(candidate, 14, out cloaked, sizeof(int)) == 0 && cloaked != 0) {
                    return true;
                }
                uint processId = 0;
                GetWindowThreadProcessId(candidate, out processId);
                if (processId == targetProcessId) {
                    return true;
                }
                RECT rect;
                if (GetWindowRect(candidate, out rect) && Intersects(rect, captureRect)) {
                    var title = new System.Text.StringBuilder(512);
                    GetWindowText(candidate, title, title.Capacity);
                    var className = new System.Text.StringBuilder(256);
                    GetClassName(candidate, className, className.Capacity);
                    occluders.Add(new OccluderInfo {
                        Handle = candidate.ToInt64(), ProcessId = processId, Rect = rect,
                        WindowTitle = title.ToString(), ClassName = className.ToString()
                    });
                }
                return true;
            };
            if (!EnumWindows(callback, IntPtr.Zero)) {
                int error = Marshal.GetLastWin32Error();
                if (!targetReached && error != 0) {
                    throw new InvalidOperationException("EnumWindows failed before reaching exact QGC HWND.");
                }
            }
            GC.KeepAlive(callback);
            if (!targetReached) {
                throw new InvalidOperationException("Exact QGC HWND is not a visible top-level desktop window.");
            }
            return occluders.ToArray();
        }

        public static bool SendLeftClick() {
            INPUT[] inputs = new INPUT[2];
            inputs[0].type = 0;
            inputs[0].data.mouse.dwFlags = 0x0002;
            inputs[1].type = 0;
            inputs[1].data.mouse.dwFlags = 0x0004;
            return SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT))) == inputs.Length;
        }

        public static bool SendEscapeKey() {
            const byte VK_ESCAPE = 0x1B;
            const uint KEYEVENTF_KEYUP = 0x0002;
            keybd_event(VK_ESCAPE, 0, 0, UIntPtr.Zero);
            keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
            return (GetAsyncKeyState(VK_ESCAPE) & 0x8000) == 0;
        }

        public static long[] GetVisibleTopLevelWindowsForProcess(uint expectedProcessId) {
            List<long> handles = new List<long>();
            EnumWindowsProc callback = delegate(IntPtr candidate, IntPtr unused) {
                if (!IsWindowVisible(candidate) || IsIconic(candidate)) {
                    return true;
                }
                int cloaked = 0;
                if (DwmGetWindowAttribute(candidate, 14, out cloaked, sizeof(int)) == 0 && cloaked != 0) {
                    return true;
                }
                uint processId = 0;
                GetWindowThreadProcessId(candidate, out processId);
                if (processId == expectedProcessId) {
                    handles.Add(candidate.ToInt64());
                }
                return true;
            };
            if (!EnumWindows(callback, IntPtr.Zero)) {
                int error = Marshal.GetLastWin32Error();
                if (error != 0) {
                    throw new InvalidOperationException("EnumWindows failed while collecting QGC top-level surfaces.");
                }
            }
            GC.KeepAlive(callback);
            handles.Sort();
            return handles.ToArray();
        }
    }
}
'@
}

# Qt Quick exposes Accessible.name/role through Microsoft's UI Automation
# provider.  Loading both contracts here keeps element discovery bound to the
# exact native QGC HWND instead of falling back to brittle screen coordinates.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-QgcWindow {
    param([Diagnostics.Process]$Process, [int]$TimeoutSeconds = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Assert-True (-not $Process.HasExited) "QGC exited before a main window was available."
        $Process.Refresh()
        $handle = $Process.MainWindowHandle
        [uint32]$owner = 0
        $handleValid = $false
        if ($handle -ne [IntPtr]::Zero -and [MultiDetectWindow.Native]::IsWindow($handle)) {
            $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($handle, [ref]$owner)
            if ($owner -eq $Process.Id) {
                $handleValid = $true
            }
        }

        if (-not $handleValid) {
            # Qt can briefly return MainWindowHandle=0 while the existing top-level
            # surface is being reparented or recreated.  Recover only from the
            # exact PID's visible, non-cloaked top-level set; if there is more than
            # one candidate, keep the gate fail-closed instead of guessing.
            $candidateHandles = @(
                [MultiDetectWindow.Native]::GetVisibleTopLevelWindowsForProcess([uint32]$Process.Id)
            )
            if ($candidateHandles.Count -eq 1) {
                $handle = [IntPtr]([int64]$candidateHandles[0])
                $owner = 0
                $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($handle, [ref]$owner)
                $handleValid = $owner -eq [uint32]$Process.Id
                if ($handleValid) {
                    Write-CombinedStageTrace -Message (
                        "qgc-main-window-fallback-hwnd=0x{0:X16}" -f $handle.ToInt64()
                    )
                }
            }
        }

        if ($handleValid) {
            [MultiDetectWindow.Native+RECT]$rect = New-Object MultiDetectWindow.Native+RECT
            Assert-True ([MultiDetectWindow.Native]::GetWindowRect($handle, [ref]$rect)) "GetWindowRect failed for QGC."
            return [pscustomobject]@{ Handle = $handle; Rect = $rect; ProcessId = $owner }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "QGC main HWND was not available within $TimeoutSeconds seconds."
}

function Set-ExactForegroundWindow {
    param(
        [IntPtr]$Handle,
        [uint32]$ExpectedProcessId,
        [int]$TimeoutMilliseconds = 4000
    )
    if ($Handle -eq [IntPtr]::Zero -or -not [MultiDetectWindow.Native]::IsWindow($Handle)) {
        return $false
    }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    $attempt = 0
    do {
        [uint32]$targetOwner = 0
        $targetThread = [MultiDetectWindow.Native]::GetWindowThreadProcessId($Handle, [ref]$targetOwner)
        if ($targetThread -eq 0 -or $targetOwner -ne $ExpectedProcessId) {
            return $false
        }
        # First try uses only thread-input attachment. A single, balanced ALT
        # pulse is used on the second try to satisfy Windows foreground-lock
        # policy without leaving a modifier pressed or accepting another HWND.
        $null = [MultiDetectWindow.Native]::ForceForegroundWindow($Handle, ($attempt -eq 1))
        if ([MultiDetectWindow.Native]::DwmFlush() -lt 0) {
            return $false
        }
        Start-Sleep -Milliseconds 75
        $foreground = [MultiDetectWindow.Native]::GetForegroundWindow()
        [uint32]$foregroundOwner = 0
        if ($foreground -ne [IntPtr]::Zero) {
            $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($foreground, [ref]$foregroundOwner)
        }
        if ($foreground -eq $Handle -and $foregroundOwner -eq $ExpectedProcessId) {
            return $true
        }
        $attempt++
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Get-StableForegroundWindowState {
    param([int]$TimeoutMilliseconds = 2000)
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $firstHandle = [MultiDetectWindow.Native]::GetForegroundWindow()
        [uint32]$firstOwner = 0
        if ($firstHandle -ne [IntPtr]::Zero -and [MultiDetectWindow.Native]::IsWindow($firstHandle)) {
            $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($firstHandle, [ref]$firstOwner)
        }
        if ($firstHandle -ne [IntPtr]::Zero -and $firstOwner -ne 0) {
            Start-Sleep -Milliseconds 50
            $secondHandle = [MultiDetectWindow.Native]::GetForegroundWindow()
            [uint32]$secondOwner = 0
            if ($secondHandle -ne [IntPtr]::Zero -and [MultiDetectWindow.Native]::IsWindow($secondHandle)) {
                $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($secondHandle, [ref]$secondOwner)
            }
            if ($secondHandle -eq $firstHandle -and $secondOwner -eq $firstOwner) {
                return [pscustomobject]@{ Handle = $firstHandle; ProcessId = $firstOwner }
            }
        }
        Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "No stable foreground HWND/PID pair was available before QGC desktop automation."
}

function Set-QgcWindowCaptureForeground {
    param([pscustomobject]$Window)
    $previous = Get-StableForegroundWindowState
    $originalTopmost = (([MultiDetectWindow.Native]::GetWindowLongPtr($Window.Handle, -20).ToInt64() -band 0x8) -ne 0)
    $flags = [uint32](0x0001 -bor 0x0002 -bor 0x0040) # NOSIZE | NOMOVE | SHOWWINDOW
    $showCommand = if ([MultiDetectWindow.Native]::IsIconic($Window.Handle)) { 9 } else { 5 } # SW_RESTORE or SW_SHOW; never resize/maximize
    $null = [MultiDetectWindow.Native]::ShowWindowAsync($Window.Handle, $showCommand)
    Assert-True ([MultiDetectWindow.Native]::SetWindowPos($Window.Handle, [IntPtr](-1), 0, 0, 0, 0, $flags)) "Unable to raise the exact QGC HWND for capture."
    Assert-True (Set-ExactForegroundWindow -Handle $Window.Handle -ExpectedProcessId ([uint32]$Window.ProcessId)) "Exact QGC HWND did not become foreground for native PiP input."
    return [pscustomobject]@{
        Handle = $previous.Handle
        ProcessId = $previous.ProcessId
        QgcOriginalTopmost = $originalTopmost
    }
}

function Restore-QgcWindowCaptureForeground {
    param([pscustomobject]$Window, [pscustomobject]$Previous)
    $flags = [uint32](0x0001 -bor 0x0002 -bor 0x0010) # NOSIZE | NOMOVE | NOACTIVATE
    $restoreOrder = if ($Previous.QgcOriginalTopmost) { [IntPtr](-1) } else { [IntPtr](-2) }
    Assert-True ([MultiDetectWindow.Native]::SetWindowPos($Window.Handle, $restoreOrder, 0, 0, 0, 0, $flags)) "QGC topmost state was not restored after native PiP input."
    if ($Previous.Handle -ne $Window.Handle) {
        Assert-True (Set-ExactForegroundWindow -Handle $Previous.Handle -ExpectedProcessId ([uint32]$Previous.ProcessId)) "Previous foreground HWND/PID was not restored after native PiP input."
    }
    else {
        Assert-True (Set-ExactForegroundWindow -Handle $Window.Handle -ExpectedProcessId ([uint32]$Window.ProcessId)) "QGC foreground HWND was not preserved after native PiP input."
    }
}

function Save-QgcWindowCapture {
    param([Diagnostics.Process]$Process, [string]$Path)
    Add-Type -AssemblyName System.Drawing
    $perMonitorV2 = [IntPtr](-4)
    $previousDpiContext = [MultiDetectWindow.Native]::SetThreadDpiAwarenessContext($perMonitorV2)
    Assert-True ($previousDpiContext -ne [IntPtr]::Zero) "Unable to enter per-monitor-v2 DPI context for QGC capture."
    $window = $null
    $bitmap = $null
    $graphics = $null
    $captureError = $null
    $previousForegroundState = $null
    $previousForeground = [IntPtr]::Zero
    [uint32]$previousForegroundOwner = 0
    $foregroundDuring = [IntPtr]::Zero
    $foregroundRestored = $false
    $cursorPreserved = $false
    $cursorMovedOutside = $false
    $cursorRestored = $false
    $placementPreserved = $false
    $placementRestored = $false
    $topmostRestored = $false
    $dpiContextRestored = $false
    $originalTopmost = $false
    $minimum = 255
    $maximum = 0
    $unique = [Collections.Generic.HashSet[int]]::new()
    try {
        $window = Get-QgcWindow -Process $Process
        [MultiDetectWindow.Native+WINDOWPLACEMENT]$originalPlacement = New-Object MultiDetectWindow.Native+WINDOWPLACEMENT
        $originalPlacement.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MultiDetectWindow.Native+WINDOWPLACEMENT])
        Assert-True ([MultiDetectWindow.Native]::GetWindowPlacement($window.Handle, [ref]$originalPlacement)) "Unable to preserve exact QGC WINDOWPLACEMENT."
        $placementPreserved = $true
        $originalTopmost = (([MultiDetectWindow.Native]::GetWindowLongPtr($window.Handle, -20).ToInt64() -band 0x8) -ne 0)
        $previousForegroundState = Get-StableForegroundWindowState
        $previousForeground = $previousForegroundState.Handle
        $previousForegroundOwner = [uint32]$previousForegroundState.ProcessId
        [MultiDetectWindow.Native+POINT]$previousCursor = New-Object MultiDetectWindow.Native+POINT
        Assert-True ([MultiDetectWindow.Native]::GetCursorPos([ref]$previousCursor)) "Unable to preserve pointer position for exact-client capture."
        $cursorPreserved = $true

        $showCommand = if ($originalPlacement.showCmd -eq 2) { 9 } else { $originalPlacement.showCmd }
        if ($showCommand -notin @(1, 3, 9)) { $showCommand = 1 }
        $null = [MultiDetectWindow.Native]::ShowWindowAsync($window.Handle, $showCommand)
        $raiseFlags = [uint32](0x0001 -bor 0x0002 -bor 0x0040) # NOSIZE | NOMOVE | SHOWWINDOW
        Assert-True ([MultiDetectWindow.Native]::SetWindowPos($window.Handle, [IntPtr](-1), 0, 0, 0, 0, $raiseFlags)) "Unable to raise exact QGC HWND for desktop capture."
        Assert-True (Set-ExactForegroundWindow -Handle $window.Handle -ExpectedProcessId ([uint32]$Process.Id)) "Exact QGC HWND did not become the foreground window for desktop capture."
        $foregroundDuring = [MultiDetectWindow.Native]::GetForegroundWindow()
        [uint32]$foregroundOwner = 0
        $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($foregroundDuring, [ref]$foregroundOwner)
        Assert-True ($foregroundDuring -eq $window.Handle -and $foregroundOwner -eq $Process.Id) "Exact QGC HWND did not become the foreground window for desktop capture."

        [MultiDetectWindow.Native+RECT]$rect = New-Object MultiDetectWindow.Native+RECT
        Assert-True ([MultiDetectWindow.Native]::GetWindowRect($window.Handle, [ref]$rect)) "GetWindowRect failed during capture."
        $windowWidth = $rect.Right - $rect.Left
        $windowHeight = $rect.Bottom - $rect.Top
        [MultiDetectWindow.Native+RECT]$clientRect = New-Object MultiDetectWindow.Native+RECT
        Assert-True ([MultiDetectWindow.Native]::GetClientRect($window.Handle, [ref]$clientRect)) "GetClientRect failed during exact-client capture."
        [MultiDetectWindow.Native+POINT]$clientTopLeft = New-Object MultiDetectWindow.Native+POINT
        $clientTopLeft.X = $clientRect.Left; $clientTopLeft.Y = $clientRect.Top
        [MultiDetectWindow.Native+POINT]$clientBottomRight = New-Object MultiDetectWindow.Native+POINT
        $clientBottomRight.X = $clientRect.Right; $clientBottomRight.Y = $clientRect.Bottom
        Assert-True ([MultiDetectWindow.Native]::ClientToScreen($window.Handle, [ref]$clientTopLeft)) "Unable to map exact QGC client top-left to physical screen coordinates."
        Assert-True ([MultiDetectWindow.Native]::ClientToScreen($window.Handle, [ref]$clientBottomRight)) "Unable to map exact QGC client bottom-right to physical screen coordinates."
        $width = $clientBottomRight.X - $clientTopLeft.X
        $height = $clientBottomRight.Y - $clientTopLeft.Y
        Assert-True ($width -ge 640 -and $height -ge 480) "QGC capture surface is unexpectedly small."

        [MultiDetectWindow.Native+RECT]$clientScreenRect = New-Object MultiDetectWindow.Native+RECT
        $clientScreenRect.Left = $clientTopLeft.X; $clientScreenRect.Top = $clientTopLeft.Y
        $clientScreenRect.Right = $clientBottomRight.X; $clientScreenRect.Bottom = $clientBottomRight.Y
        $monitor = [MultiDetectWindow.Native]::MonitorFromRect([ref]$clientScreenRect, 0)
        Assert-True ($monitor -ne [IntPtr]::Zero) "Exact QGC client rectangle is not visible on a desktop monitor."
        [MultiDetectWindow.Native+MONITORINFOEX]$monitorInfo = New-Object MultiDetectWindow.Native+MONITORINFOEX
        $monitorInfo.cbSize = [Runtime.InteropServices.Marshal]::SizeOf([type][MultiDetectWindow.Native+MONITORINFOEX])
        Assert-True ([MultiDetectWindow.Native]::GetMonitorInfo($monitor, [ref]$monitorInfo)) "Unable to resolve QGC capture monitor bounds."
        Assert-True (
            $clientScreenRect.Left -ge $monitorInfo.rcMonitor.Left -and
            $clientScreenRect.Top -ge $monitorInfo.rcMonitor.Top -and
            $clientScreenRect.Right -le $monitorInfo.rcMonitor.Right -and
            $clientScreenRect.Bottom -le $monitorInfo.rcMonitor.Bottom
        ) "Exact QGC client rectangle spans monitors or leaves the selected monitor."

        $cursorCandidates = @(
            [pscustomobject]@{ X = $clientScreenRect.Left; Y = $clientScreenRect.Top - 1 },
            [pscustomobject]@{ X = $clientScreenRect.Left; Y = $clientScreenRect.Bottom },
            [pscustomobject]@{ X = $clientScreenRect.Left - 1; Y = $clientScreenRect.Top },
            [pscustomobject]@{ X = $clientScreenRect.Right; Y = $clientScreenRect.Top }
        )
        $cursorOutside = $cursorCandidates | Where-Object {
            $_.X -ge $monitorInfo.rcMonitor.Left -and $_.X -lt $monitorInfo.rcMonitor.Right -and
            $_.Y -ge $monitorInfo.rcMonitor.Top -and $_.Y -lt $monitorInfo.rcMonitor.Bottom
        } | Select-Object -First 1
        Assert-True ($null -ne $cursorOutside) "No monitor-owned pointer coordinate exists outside exact QGC client capture."
        Assert-True ([MultiDetectWindow.Native]::SetCursorPos($cursorOutside.X, $cursorOutside.Y)) "Unable to move pointer outside exact QGC client capture."
        $cursorMovedOutside = $true

        # Desktop composition can briefly expose an Explorer/XAML or other window
        # between the foreground check and WindowFromPoint.  Re-assert the exact
        # QGC HWND and retry the complete occlusion/ownership proof a bounded
        # number of times.  Persistent intersections still fail closed; this is
        # only a stabilization window for a transient foreground race.
        $occluders = @()
        $occluderMessage = "An external top-level window intersects the exact QGC client capture rectangle."
        $ownershipGridPoints = 0
        $ownershipCaptureReady = $false
        $lastOwnershipFailure = $null
        $captureOwnershipAttempts = 5
        for ($ownershipAttempt = 1; $ownershipAttempt -le $captureOwnershipAttempts; $ownershipAttempt++) {
            if ($ownershipAttempt -gt 1) {
                $reRaiseFlags = [uint32](0x0001 -bor 0x0002 -bor 0x0040) # NOSIZE | NOMOVE | SHOWWINDOW
                $reRaised = [MultiDetectWindow.Native]::SetWindowPos($window.Handle, [IntPtr](-1), 0, 0, 0, 0, $reRaiseFlags)
                $reForeground = $reRaised -and (Set-ExactForegroundWindow -Handle $window.Handle -ExpectedProcessId ([uint32]$Process.Id))
                if (-not $reForeground) {
                    $lastOwnershipFailure = "QGC foreground/raise was lost during exact-client capture stabilization attempt $ownershipAttempt/$captureOwnershipAttempts."
                    Start-Sleep -Milliseconds 150
                    continue
                }
                Assert-True ([MultiDetectWindow.Native]::DwmFlush() -ge 0) "DwmFlush failed while stabilizing exact-client QGC desktop capture."
                Start-Sleep -Milliseconds 150
            }

            $foregroundDuring = [MultiDetectWindow.Native]::GetForegroundWindow()
            [uint32]$foregroundOwner = 0
            if ($foregroundDuring -ne [IntPtr]::Zero) {
                $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($foregroundDuring, [ref]$foregroundOwner)
            }
            if ($foregroundDuring -ne $window.Handle -or $foregroundOwner -ne $Process.Id) {
                $lastOwnershipFailure = (
                    "Exact QGC HWND was not foreground during exact-client capture stabilization: " +
                    "foreground_hwnd=$(('0x{0:X}' -f $foregroundDuring.ToInt64())) foreground_pid=$foregroundOwner " +
                    "qgc_hwnd=$(('0x{0:X}' -f $window.Handle.ToInt64())) qgc_pid=$($Process.Id)."
                )
                Start-Sleep -Milliseconds 150
                continue
            }

            $occluders = @([MultiDetectWindow.Native]::GetExternalOccluders($window.Handle, $clientScreenRect, [uint32]$Process.Id))
            $occluderMessage = "An external top-level window intersects the exact QGC client capture rectangle."
            if ($occluders.Count -ne 0) {
                $occluderDetails = @($occluders | ForEach-Object {
                    $rectText = "($($_.Rect.Left),$($_.Rect.Top))-($($_.Rect.Right),$($_.Rect.Bottom))"
                    $titleText = if ([string]::IsNullOrWhiteSpace($_.WindowTitle)) { "<untitled>" } else { $_.WindowTitle }
                    $classText = if ([string]::IsNullOrWhiteSpace($_.ClassName)) { "<unknown-class>" } else { $_.ClassName }
                    "hwnd=$(('0x{0:X}' -f $_.Handle)) pid=$($_.ProcessId) rect=$rectText class=$classText title=$titleText"
                }) -join "; "
                $occluderMessage = (
                    "An external top-level window intersects the exact QGC client capture rectangle: " +
                    "count=$($occluders.Count); $occluderDetails"
                )
                $lastOwnershipFailure = $occluderMessage
                Start-Sleep -Milliseconds 150
                continue
            }

            $attemptGridPoints = 0
            $attemptOwnershipFailure = $null
            for ($gridY = 0; $gridY -lt 5 -and $null -eq $attemptOwnershipFailure; $gridY++) {
                for ($gridX = 0; $gridX -lt 5; $gridX++) {
                    [MultiDetectWindow.Native+POINT]$samplePoint = New-Object MultiDetectWindow.Native+POINT
                    $samplePoint.X = $clientScreenRect.Left + [int][Math]::Floor((($gridX + 0.5) * $width) / 5.0)
                    $samplePoint.Y = $clientScreenRect.Top + [int][Math]::Floor((($gridY + 0.5) * $height) / 5.0)
                    $hitWindow = [MultiDetectWindow.Native]::WindowFromPoint($samplePoint)
                    [uint32]$hitOwner = 0
                    $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($hitWindow, [ref]$hitOwner)
                    if ($hitWindow -eq [IntPtr]::Zero -or $hitOwner -ne $Process.Id) {
                        $hitRoot = [MultiDetectWindow.Native]::GetAncestor($hitWindow, 2) # GA_ROOT
                        [uint32]$hitRootOwner = 0
                        if ($hitRoot -ne [IntPtr]::Zero) {
                            $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($hitRoot, [ref]$hitRootOwner)
                        }
                        $hitClass = [Text.StringBuilder]::new(256)
                        if ($hitWindow -ne [IntPtr]::Zero) {
                            $null = [MultiDetectWindow.Native]::GetClassName($hitWindow, $hitClass, $hitClass.Capacity)
                        }
                        $attemptOwnershipFailure = (
                            "Exact-client ownership grid found a pixel not owned by QGC: " +
                            "grid=($gridX,$gridY) screen=($($samplePoint.X),$($samplePoint.Y)) " +
                            "hit_hwnd=$(('0x{0:X}' -f $hitWindow.ToInt64())) hit_pid=$hitOwner " +
                            "hit_class=$($hitClass.ToString()) root_hwnd=$(('0x{0:X}' -f $hitRoot.ToInt64())) " +
                            "root_pid=$hitRootOwner qgc_hwnd=$(('0x{0:X}' -f $window.Handle.ToInt64())) qgc_pid=$($Process.Id)."
                        )
                        break
                    }
                    $attemptGridPoints++
                }
            }
            if ($null -eq $attemptOwnershipFailure) {
                $ownershipGridPoints = $attemptGridPoints
                $ownershipCaptureReady = $true
                break
            }
            $lastOwnershipFailure = $attemptOwnershipFailure
            Start-Sleep -Milliseconds 150
        }
        Assert-True ($occluders.Count -eq 0) $occluderMessage
        if (-not $ownershipCaptureReady) {
            if ($null -ne $lastOwnershipFailure) { throw $lastOwnershipFailure }
            throw "Exact-client QGC ownership proof did not stabilize within $captureOwnershipAttempts attempts."
        }

        Assert-True ([MultiDetectWindow.Native]::DwmFlush() -ge 0) "DwmFlush failed before exact-client desktop capture."
        Start-Sleep -Milliseconds 100
        $bitmap = New-Object Drawing.Bitmap $width, $height, ([Drawing.Imaging.PixelFormat]::Format24bppRgb)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        $destinationDc = $graphics.GetHdc()
        try {
            $captureStartedAtUtc = (Get-Date).ToUniversalTime()
            Assert-True ([MultiDetectWindow.Native]::CaptureScreenRectangle(
                $destinationDc, $clientScreenRect.Left, $clientScreenRect.Top, $width, $height
            )) "Native BitBlt failed during exact-client QGC desktop capture."
            $captureCompletedAtUtc = (Get-Date).ToUniversalTime()
        }
        finally {
            $graphics.ReleaseHdc($destinationDc)
        }
        $bitmap.Save($Path, [Drawing.Imaging.ImageFormat]::Png)
        for ($y = 0; $y -lt $height; $y += [Math]::Max(1, [int]($height / 40))) {
            for ($x = 0; $x -lt $width; $x += [Math]::Max(1, [int]($width / 60))) {
                $color = $bitmap.GetPixel($x, $y)
                $luminance = [int](0.2126 * $color.R + 0.7152 * $color.G + 0.0722 * $color.B)
                $minimum = [Math]::Min($minimum, $luminance)
                $maximum = [Math]::Max($maximum, $luminance)
                $null = $unique.Add($color.ToArgb())
            }
        }
    }
    catch { $captureError = $_ }
    finally {
        if ($null -ne $graphics) { $graphics.Dispose() }
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($cursorPreserved) {
            $null = [MultiDetectWindow.Native]::SetCursorPos($previousCursor.X, $previousCursor.Y)
            [MultiDetectWindow.Native+POINT]$cursorAfter = New-Object MultiDetectWindow.Native+POINT
            $cursorRestored = [MultiDetectWindow.Native]::GetCursorPos([ref]$cursorAfter) -and $cursorAfter.X -eq $previousCursor.X -and $cursorAfter.Y -eq $previousCursor.Y
        }
        if ($placementPreserved -and $null -ne $window) {
            $placementSet = [MultiDetectWindow.Native]::SetWindowPlacement($window.Handle, [ref]$originalPlacement)
            $restoreFlags = [uint32](0x0001 -bor 0x0002 -bor 0x0010) # NOSIZE | NOMOVE | NOACTIVATE
            $restoreOrder = if ($originalTopmost) { [IntPtr](-1) } else { [IntPtr](-2) }
            $topmostSet = [MultiDetectWindow.Native]::SetWindowPos($window.Handle, $restoreOrder, 0, 0, 0, 0, $restoreFlags)
            [MultiDetectWindow.Native+WINDOWPLACEMENT]$placementAfter = New-Object MultiDetectWindow.Native+WINDOWPLACEMENT
            $placementAfter.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MultiDetectWindow.Native+WINDOWPLACEMENT])
            $placementRead = [MultiDetectWindow.Native]::GetWindowPlacement($window.Handle, [ref]$placementAfter)
            $placementRestored = $placementSet -and $placementRead -and $placementAfter.showCmd -eq $originalPlacement.showCmd -and
                $placementAfter.rcNormalPosition.Left -eq $originalPlacement.rcNormalPosition.Left -and
                $placementAfter.rcNormalPosition.Top -eq $originalPlacement.rcNormalPosition.Top -and
                $placementAfter.rcNormalPosition.Right -eq $originalPlacement.rcNormalPosition.Right -and
                $placementAfter.rcNormalPosition.Bottom -eq $originalPlacement.rcNormalPosition.Bottom
            $topmostAfter = (([MultiDetectWindow.Native]::GetWindowLongPtr($window.Handle, -20).ToInt64() -band 0x8) -ne 0)
            $topmostRestored = $topmostSet -and $topmostAfter -eq $originalTopmost
        }
        if ($null -ne $previousForegroundState) {
            $foregroundRestoreAttempted = Set-ExactForegroundWindow -Handle $previousForeground -ExpectedProcessId $previousForegroundOwner
            $restoredForeground = [MultiDetectWindow.Native]::GetForegroundWindow()
            [uint32]$restoredForegroundOwner = 0
            if ($restoredForeground -ne [IntPtr]::Zero) {
                $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($restoredForeground, [ref]$restoredForegroundOwner)
            }
            $foregroundRestored = $foregroundRestoreAttempted -and $restoredForeground -eq $previousForeground -and $restoredForegroundOwner -eq $previousForegroundOwner
        }
        $null = [MultiDetectWindow.Native]::SetThreadDpiAwarenessContext($previousDpiContext)
        $dpiContextRestored = [MultiDetectWindow.Native]::AreDpiAwarenessContextsEqual(
            [MultiDetectWindow.Native]::GetThreadDpiAwarenessContext(), $previousDpiContext
        )
    }
    if ($null -ne $captureError) { throw $captureError }
    Assert-True $cursorRestored "Pointer position was not restored after exact-client desktop capture."
    Assert-True $placementRestored "QGC WINDOWPLACEMENT was not restored after exact-client desktop capture."
    Assert-True $topmostRestored "QGC topmost state was not restored after exact-client desktop capture."
    Assert-True $foregroundRestored "Foreground HWND was not restored after exact-client desktop capture."
    Assert-True $dpiContextRestored "Capture thread DPI-awareness context was not restored."
    Assert-True (($maximum - $minimum) -ge 20 -and $unique.Count -ge 20) "QGC window capture is blank or too low variance."
    $title = [Text.StringBuilder]::new(512)
    $null = [MultiDetectWindow.Native]::GetWindowText($window.Handle, $title, $title.Capacity)
    return [ordered]@{
        path = $Path
        sha256 = Get-Sha256OrNull -Path $Path
        bytes = (Get-Item -LiteralPath $Path).Length
        qgc_pid = $Process.Id
        qgc_hwnd = ("0x{0:X}" -f $window.Handle.ToInt64())
        qgc_window_title = $title.ToString()
        exact_window_rect = [ordered]@{ left = $rect.Left; top = $rect.Top; width = $windowWidth; height = $windowHeight }
        exact_client_rect_screen_physical = [ordered]@{
            left = $clientScreenRect.Left; top = $clientScreenRect.Top; width = $width; height = $height
        }
        monitor = [ordered]@{
            device = $monitorInfo.szDevice
            bounds = [ordered]@{ left = $monitorInfo.rcMonitor.Left; top = $monitorInfo.rcMonitor.Top; right = $monitorInfo.rcMonitor.Right; bottom = $monitorInfo.rcMonitor.Bottom }
            work_area = [ordered]@{ left = $monitorInfo.rcWork.Left; top = $monitorInfo.rcWork.Top; right = $monitorInfo.rcWork.Right; bottom = $monitorInfo.rcWork.Bottom }
        }
        qgc_dpi = [MultiDetectWindow.Native]::GetDpiForWindow($window.Handle)
        dpi_awareness = "per_monitor_v2"
        foreground_hwnd_before = ("0x{0:X}" -f $previousForeground.ToInt64())
        foreground_pid_before = $previousForegroundOwner
        foreground_hwnd_during = ("0x{0:X}" -f $foregroundDuring.ToInt64())
        foreground_exact_qgc = ($foregroundDuring -eq $window.Handle)
        occlusion_check = [ordered]@{
            external_top_level_intersections = $occluders.Count
            ownership_grid_points = $ownershipGridPoints
            ownership_grid_all_qgc_pid = ($ownershipGridPoints -eq 25)
        }
        restoration = [ordered]@{
            cursor_restored = $cursorRestored
            window_placement_restored = $placementRestored
            topmost_state_restored = $topmostRestored
            foreground_restored = $foregroundRestored
            dpi_context_restored = $dpiContextRestored
        }
        cursor_moved_outside_client = $cursorMovedOutside
        captured_started_at_utc = $captureStartedAtUtc.ToString('o')
        captured_at_utc = $captureCompletedAtUtc.ToString('o')
        luminance_range = ($maximum - $minimum)
        sampled_unique_colors = $unique.Count
        capture_method = "powershell_exact_pid_hwnd_native_bitblt_srccopy_captureblt_exact_client"
        capture_scope = "exact_qgc_client_only_no_full_desktop"
        full_desktop_capture_used = $false
    }
}

function Convert-AutomationBoundingRect {
    param([System.Windows.Automation.AutomationElement]$Element)
    $rect = $Element.Current.BoundingRectangle
    Assert-True (-not $rect.IsEmpty -and $rect.Width -gt 0 -and $rect.Height -gt 0) "UI Automation element has no positive bounding rectangle."
    return [ordered]@{
        left = [double]$rect.Left
        top = [double]$rect.Top
        right = [double]$rect.Right
        bottom = [double]$rect.Bottom
        width = [double]$rect.Width
        height = [double]$rect.Height
    }
}

function Find-QgcAutomationElementByName {
    param(
        [IntPtr]$MainHandle,
        [string]$AccessibleName,
        [int]$TimeoutMilliseconds = 5000,
        [switch]$RequireVisible
    )
    $rootElement = [System.Windows.Automation.AutomationElement]::FromHandle($MainHandle)
    Assert-True ($null -ne $rootElement) "UI Automation could not bind the exact QGC HWND."
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $AccessibleName
    )
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $element = $rootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition
        )
        if ($null -ne $element) {
            try {
                if (-not $RequireVisible -or -not $element.Current.IsOffscreen) {
                    return $element
                }
            }
            catch [System.Windows.Automation.ElementNotAvailableException] {
                $element = $null
            }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Invoke-QgcAutomationButton {
    <#
    Use the native QGC accessibility provider only after an exact physical
    pointer click has failed to produce the expected lifecycle record. The
    root is always the already-validated QGC main HWND, so this is a semantic
    retry of the same toolbar control rather than a coordinate fallback.
    #>
    param(
        [IntPtr]$MainHandle,
        [uint32]$ExpectedProcessId,
        [string]$AccessibleName,
        [string]$AutomationId,
        [int]$TimeoutMilliseconds = 3000
    )

    $result = [ordered]@{
        attempted = $true
        invoked = $false
        accessible_name = $AccessibleName
        automation_id = $AutomationId
        root_hwnd = ("0x{0:X16}" -f $MainHandle.ToInt64())
        root_owner_pid = $ExpectedProcessId
        root_hwnd_matches_qgc = $false
        root_pid_matches_qgc = $false
        control_type = $null
        error = $null
        method = "exact_qgc_hwnd_uia_invoke_pattern"
    }

    try {
        [uint32]$rootOwner = 0
        $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($MainHandle, [ref]$rootOwner)
        $result.root_hwnd_matches_qgc = [MultiDetectWindow.Native]::IsWindow($MainHandle)
        $result.root_pid_matches_qgc = ($rootOwner -eq $ExpectedProcessId)
        if (-not ($result.root_hwnd_matches_qgc -and $result.root_pid_matches_qgc)) {
            $result.error = "QGC main HWND/PID provenance changed before accessibility retry."
            return [pscustomobject]$result
        }

        $element = Find-QgcAutomationElementByName -MainHandle $MainHandle `
            -AccessibleName $AccessibleName -TimeoutMilliseconds $TimeoutMilliseconds -RequireVisible
        if ($null -eq $element) {
            $result.error = "QGC accessibility provider did not expose the exact toolbar button."
            return [pscustomobject]$result
        }

        $current = $element.Current
        $result.control_type = $current.ControlType.ProgrammaticName
        if ($AutomationId -and $current.AutomationId -and $current.AutomationId -ne $AutomationId) {
            $result.error = "QGC accessibility automation id did not match the geometry-exported control."
            return [pscustomobject]$result
        }
        $pattern = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        if ($null -eq $pattern) {
            $result.error = "QGC toolbar button did not expose InvokePattern."
            return [pscustomobject]$result
        }
        $pattern.Invoke()
        $result.invoked = $true
    }
    catch {
        $result.error = $_.Exception.Message
    }
    return [pscustomobject]$result
}

function Wait-QgcHilUiGeometryRecord {
    param(
        [string[]]$Paths,
        [Diagnostics.Process]$Process,
        [string]$Role,
        [long]$MinimumSequence = -1,
        [int]$TimeoutMilliseconds = 5000
    )
    $marker = "MULTIDETECT_HIL_UI_GEOMETRY "
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        Assert-True (-not $Process.HasExited) "QGC exited while waiting for native QML geometry role '$Role'."
        $latest = $null
        foreach ($path in $Paths) {
            $text = Get-TextIfPresent -Path $path
            if ([string]::IsNullOrWhiteSpace($text)) { continue }
            foreach ($line in [regex]::Split($text, "\r?\n")) {
                $markerIndex = $line.IndexOf($marker, [StringComparison]::Ordinal)
                if ($markerIndex -lt 0) { continue }
                $candidate = $line.Substring($markerIndex + $marker.Length).Trim()
                $jsonEnd = $candidate.LastIndexOf('}')
                if ($jsonEnd -lt 0) { continue }
                try {
                    $record = $candidate.Substring(0, $jsonEnd + 1) | ConvertFrom-Json
                }
                catch { continue }
                if ($record.schema -ne "multidetect.qgc-hil-ui-geometry" -or
                    [int]$record.schema_version -ne 1 -or $record.role -ne $Role) {
                    continue
                }
                $sequence = [long]$record.sequence
                if ($sequence -lt 1) { continue }
                if ($sequence -le $MinimumSequence) { continue }
                if ($null -eq $latest -or $sequence -gt [long]$latest.sequence) {
                    $latest = $record
                }
            }
        }
        if ($null -ne $latest) { return $latest }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Wait-QgcHilUiGeometryRecordStable {
    param(
        [string[]]$Paths,
        [Diagnostics.Process]$Process,
        [string]$Role,
        [long]$MinimumSequence = -1,
        [int]$StableMilliseconds = 350,
        [int]$TimeoutMilliseconds = 8000
    )
    # Geometry can be emitted more than once while QGC creates native toolbar
    # indicators.  A sequence number alone is not enough: an ancestor
    # Flickable may move the Loader without changing the Loader's own x.  Wait
    # for the scene rectangle to remain identical across fresh records and
    # fail closed if the layout never settles.
    Assert-True ($StableMilliseconds -gt 0 -and $TimeoutMilliseconds -ge $StableMilliseconds) `
        "Native QGC geometry stability bounds are invalid."
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    $latest = $null
    $latestSignature = $null
    $stableSince = $null
    do {
        Assert-True (-not $Process.HasExited) "QGC exited while waiting for stable native QML geometry role '$Role'."
        $candidate = Wait-QgcHilUiGeometryRecord -Paths $Paths -Process $Process `
            -Role $Role -MinimumSequence $MinimumSequence -TimeoutMilliseconds 120
        if ($null -ne $candidate) {
            $signature = @(
                $candidate.scene_rect.left,
                $candidate.scene_rect.top,
                $candidate.scene_rect.right,
                $candidate.scene_rect.bottom,
                $candidate.window_scene_size.width,
                $candidate.window_scene_size.height,
                $candidate.visible,
                $candidate.enabled
            ) -join "|"
            $now = [DateTime]::UtcNow
            if ($signature -ne $latestSignature) {
                $latest = $candidate
                $latestSignature = $signature
                $stableSince = $now
            }
            elseif ($null -ne $stableSince -and
                    ($now - $stableSince).TotalMilliseconds -ge $StableMilliseconds) {
                return $latest
            }
            $MinimumSequence = [long]$candidate.sequence
        }
        Start-Sleep -Milliseconds 75
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Convert-QgcHilUiGeometryToPhysicalScreenRect {
    param(
        [pscustomobject]$Record,
        [int]$ClientLeft,
        [int]$ClientTop,
        [int]$ClientWidth,
        [int]$ClientHeight
    )
    Assert-True ($null -ne $Record.scene_rect -and $null -ne $Record.window_scene_size) `
        "QGC QML geometry record is missing scene_rect or window_scene_size."
    $windowWidth = [double]$Record.window_scene_size.width
    $windowHeight = [double]$Record.window_scene_size.height
    $left = [double]$Record.scene_rect.left
    $top = [double]$Record.scene_rect.top
    $right = [double]$Record.scene_rect.right
    $bottom = [double]$Record.scene_rect.bottom
    foreach ($value in @($windowWidth, $windowHeight, $left, $top, $right, $bottom)) {
        Assert-True (-not [double]::IsNaN($value) -and -not [double]::IsInfinity($value)) `
            "QGC QML geometry contains a non-finite coordinate."
    }
    Assert-True ($windowWidth -gt 0 -and $windowHeight -gt 0 -and
                 $right -gt $left -and $bottom -gt $top) `
        "QGC QML geometry has no positive scene or item area."
    Assert-True ($left -ge -0.5 -and $top -ge -0.5 -and
                 $right -le ($windowWidth + 0.5) -and $bottom -le ($windowHeight + 0.5)) `
        "QGC QML geometry is outside the exact main-window content scene."
    $scaleX = $ClientWidth / $windowWidth
    $scaleY = $ClientHeight / $windowHeight
    Assert-True ($scaleX -gt 0 -and $scaleY -gt 0 -and
                 -not [double]::IsNaN($scaleX) -and -not [double]::IsInfinity($scaleX) -and
                 -not [double]::IsNaN($scaleY) -and -not [double]::IsInfinity($scaleY)) `
        "Unable to derive a finite logical-scene to physical-client scale."
    $reportedDpr = [double]$Record.device_pixel_ratio
    Assert-True ($reportedDpr -gt 0 -and [Math]::Abs($scaleX - $reportedDpr) -le 0.10 -and
                 [Math]::Abs($scaleY - $reportedDpr) -le 0.10) `
        "QGC QML scene/client scale does not match the current screen device-pixel ratio."
    $physicalRect = [ordered]@{
        left = $ClientLeft + [int][Math]::Round($left * $scaleX)
        top = $ClientTop + [int][Math]::Round($top * $scaleY)
        right = $ClientLeft + [int][Math]::Round($right * $scaleX)
        bottom = $ClientTop + [int][Math]::Round($bottom * $scaleY)
    }
    $physicalRect.width = $physicalRect.right - $physicalRect.left
    $physicalRect.height = $physicalRect.bottom - $physicalRect.top
    Assert-True ($physicalRect.width -gt 0 -and $physicalRect.height -gt 0) `
        "Mapped QGC QML geometry has no positive physical-pixel area."
    return [pscustomobject]@{
        bounding_rect = $physicalRect
        scene_to_physical_scale_x = $scaleX
        scene_to_physical_scale_y = $scaleY
        reported_device_pixel_ratio = $reportedDpr
    }
}

function Get-QgcVisibleTopLevelHwndSet {
    param([Diagnostics.Process]$Process, [IntPtr]$MainHandle)
    $handles = @([MultiDetectWindow.Native]::GetVisibleTopLevelWindowsForProcess([uint32]$Process.Id) | ForEach-Object {
        "0x{0:X16}" -f [long]$_
    } | Sort-Object -Unique)
    $expected = "0x{0:X16}" -f $MainHandle.ToInt64()
    Assert-True ($handles -contains $expected) "Exact QGC main HWND is absent from its visible top-level surface set."
    return $handles
}

function Get-QgcPointProvenanceSample {
    param(
        [string]$Name,
        [int]$ScreenX,
        [int]$ScreenY,
        [IntPtr]$ExpectedRootHwnd,
        [uint32]$ExpectedProcessId
    )
    [MultiDetectWindow.Native+POINT]$point = New-Object MultiDetectWindow.Native+POINT
    $point.X = $ScreenX
    $point.Y = $ScreenY
    $hitHwnd = [MultiDetectWindow.Native]::WindowFromPoint($point)
    $rootHwnd = if ($hitHwnd -ne [IntPtr]::Zero) {
        [MultiDetectWindow.Native]::GetAncestor($hitHwnd, 2) # GA_ROOT
    }
    else { [IntPtr]::Zero }
    [uint32]$rootOwnerPid = 0
    if ($rootHwnd -ne [IntPtr]::Zero) {
        $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($rootHwnd, [ref]$rootOwnerPid)
    }
    return [pscustomobject][ordered]@{
        name = $Name
        screen_x = $ScreenX
        screen_y = $ScreenY
        window_from_point_hwnd = ("0x{0:X16}" -f $hitHwnd.ToInt64())
        root_hwnd = ("0x{0:X16}" -f $rootHwnd.ToInt64())
        root_owner_pid = $rootOwnerPid
        qgc_hwnd_matches = ($rootHwnd -eq $ExpectedRootHwnd)
        qgc_pid_matches = ($rootOwnerPid -eq $ExpectedProcessId)
        provenance_method = "WindowFromPoint->GetAncestor(GA_ROOT)->GetWindowThreadProcessId"
    }
}

function Invoke-QgcNativeToolbarIndicatorSmoke {
    param(
        [Diagnostics.Process]$Process,
        [string]$ClosedCapture,
        [string]$OpenCapture,
        [string]$ObservationPath,
        [string]$ResultPath,
        [string]$Python,
        [string]$Validator,
        [string[]]$QgcLogPaths
    )
    $window = Get-QgcWindow -Process $Process
    $mainHwndText = "0x{0:X16}" -f $window.Handle.ToInt64()
    [MultiDetectWindow.Native+RECT]$clientRect = New-Object MultiDetectWindow.Native+RECT
    Assert-True ([MultiDetectWindow.Native]::GetClientRect($window.Handle, [ref]$clientRect)) "GetClientRect failed before native toolbar smoke."
    [MultiDetectWindow.Native+POINT]$clientTopLeft = New-Object MultiDetectWindow.Native+POINT
    $clientTopLeft.X = $clientRect.Left; $clientTopLeft.Y = $clientRect.Top
    [MultiDetectWindow.Native+POINT]$clientBottomRight = New-Object MultiDetectWindow.Native+POINT
    $clientBottomRight.X = $clientRect.Right; $clientBottomRight.Y = $clientRect.Bottom
    Assert-True ([MultiDetectWindow.Native]::ClientToScreen($window.Handle, [ref]$clientTopLeft)) "Unable to map QGC client top-left for native toolbar smoke."
    Assert-True ([MultiDetectWindow.Native]::ClientToScreen($window.Handle, [ref]$clientBottomRight)) "Unable to map QGC client bottom-right for native toolbar smoke."

    $indicatorEvidence = $null
    $drawerEvidence = $null
    $clickEvidence = $null
    $visibleClosed = @()
    $visibleOpen = @()
    $visibleAfterClose = @()
    $drawerClosedAfterEscape = $false
    $geometryError = $null
    $previousForeground = $null
    $automationEvidence = $null
    # Keep the exact QGC HWND in the foreground/topmost state for the whole
    # native toolbar transaction.  Save-QgcWindowCapture deliberately restores
    # the foreground window after each image; entering this critical section
    # first makes that restoration land back on QGC instead of briefly exposing
    # Docker Desktop (or another top-level window) over the next capture.
    $previousForeground = Set-QgcWindowCaptureForeground -Window $window
    $closedCaptureEvidence = Save-QgcWindowCapture -Process $Process -Path $ClosedCapture
    # Save-QgcWindowCapture runs under PER_MONITOR_AWARE_V2 and therefore owns
    # the authoritative physical-pixel client rectangle.  QML reports logical
    # scene coordinates; map them into this exact PID/HWND capture rectangle
    # instead of relying on UIA DPI virtualization or guessed toolbar offsets.
    $captureClient = $closedCaptureEvidence.exact_client_rect_screen_physical
    $clientTopLeft.X = [int]$captureClient.left
    $clientTopLeft.Y = [int]$captureClient.top
    $clientBottomRight.X = [int]$captureClient.left + [int]$captureClient.width
    $clientBottomRight.Y = [int]$captureClient.top + [int]$captureClient.height
    $perMonitorV2 = [IntPtr](-4)
    $previousInteractionDpiContext = [MultiDetectWindow.Native]::SetThreadDpiAwarenessContext($perMonitorV2)
    Assert-True ($previousInteractionDpiContext -ne [IntPtr]::Zero) `
        "Unable to enter per-monitor-v2 DPI context for exact QGC toolbar input."
    $interactionDpiContextRestored = $false
    [MultiDetectWindow.Native+POINT]$previousCursor = New-Object MultiDetectWindow.Native+POINT
    $cursorPreserved = [MultiDetectWindow.Native]::GetCursorPos([ref]$previousCursor)
    try {
        $visibleClosed = @(Get-QgcVisibleTopLevelHwndSet -Process $Process -MainHandle $window.Handle)
        Assert-True ($visibleClosed.Count -eq 1) "QGC exposes more than its one native top-level window before toolbar click."
        $indicator = Wait-QgcHilUiGeometryRecordStable -Paths $QgcLogPaths -Process $Process `
            -Role "indicator_ready" -StableMilliseconds 350 -TimeoutMilliseconds 8000
        Assert-True ($null -ne $indicator) `
            "QGC did not emit the acceptance-only native toolbar QML geometry record."
        Assert-True ($indicator.state -eq "visible" -and $indicator.visible -eq $true -and
                     $indicator.item_object_name -eq "toolbar_multiDetectIndicator" -and
                     $indicator.accessible_name -eq "MultiDetect AI Status" -and
                     $indicator.accessible_role -eq "Button") `
            "QGC native toolbar geometry record has stale identity, visibility, or accessibility semantics."
        # Foreground promotion itself can allow QGC to finish creating or
        # hiding native vehicle indicators. Refresh the exact QML rectangle
        # after promotion, immediately before pointer input, so the click is
        # bound to the same settled scene geometry used for provenance.
        $freshIndicator = Wait-QgcHilUiGeometryRecordStable -Paths $QgcLogPaths -Process $Process `
            -Role "indicator_ready" -MinimumSequence ([long]$indicator.sequence) `
            -StableMilliseconds 350 -TimeoutMilliseconds 4000
        Assert-True ($null -ne $freshIndicator) `
            "QGC did not emit a fresh stable native toolbar geometry record before the exact click."
        $indicator = $freshIndicator
        $indicatorMapping = Convert-QgcHilUiGeometryToPhysicalScreenRect -Record $indicator `
            -ClientLeft $clientTopLeft.X -ClientTop $clientTopLeft.Y `
            -ClientWidth ($clientBottomRight.X - $clientTopLeft.X) `
            -ClientHeight ($clientBottomRight.Y - $clientTopLeft.Y)
        $indicatorRect = $indicatorMapping.bounding_rect
        $clickX = [int][Math]::Round(($indicatorRect.left + $indicatorRect.right) / 2.0)
        $clickY = [int][Math]::Round(($indicatorRect.top + $indicatorRect.bottom) / 2.0)
        $indicatorProvenanceSamples = @(
            Get-QgcPointProvenanceSample -Name "center" -ScreenX $clickX -ScreenY $clickY `
                -ExpectedRootHwnd $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
        )
        $indicatorPidMatches = @($indicatorProvenanceSamples | Where-Object { -not $_.qgc_pid_matches }).Count -eq 0
        $indicatorHwndMatches = @($indicatorProvenanceSamples | Where-Object { -not $_.qgc_hwnd_matches }).Count -eq 0
        Assert-True ($indicatorPidMatches -and $indicatorHwndMatches) `
            "The native toolbar centre is not rooted in the exact QGC main HWND/PID."
        $indicatorEvidence = [ordered]@{
            accessible_name = [string]$indicator.accessible_name
            automation_id = [string]$indicator.accessible_id
            control_type = "Qml.Accessible.Button"
            bounding_rect = $indicatorRect
            geometry_source = "qgc_qml_scene_geometry_export"
            geometry_schema = [string]$indicator.schema
            role = [string]$indicator.role
            geometry_role = [string]$indicator.role
            geometry_sequence = [long]$indicator.sequence
            qgc_pid_matches = $indicatorPidMatches
            qgc_hwnd_matches = $indicatorHwndMatches
            provenance_samples = $indicatorProvenanceSamples
            geometry_bound_to_exact_qgc_pid = $true
            geometry_mapped_through_exact_main_hwnd = $true
            qml_scene_rect = $indicator.scene_rect
            qml_window_scene_size = $indicator.window_scene_size
            scene_to_physical_scale_x = $indicatorMapping.scene_to_physical_scale_x
            scene_to_physical_scale_y = $indicatorMapping.scene_to_physical_scale_y
            reported_device_pixel_ratio = $indicatorMapping.reported_device_pixel_ratio
            refreshed_before_click = $true
        }

        [MultiDetectWindow.Native+POINT]$clickPoint = New-Object MultiDetectWindow.Native+POINT
        $clickPoint.X = $clickX; $clickPoint.Y = $clickY
        Assert-True ([MultiDetectWindow.Native]::SetCursorPos($clickX, $clickY)) `
            "Unable to move the pointer to the QGC-reported native toolbar centre."
        Start-Sleep -Milliseconds 120
        [MultiDetectWindow.Native+POINT]$actualCursor = New-Object MultiDetectWindow.Native+POINT
        $cursorReadSucceeded = [MultiDetectWindow.Native]::GetCursorPos([ref]$actualCursor)
        Assert-True ($cursorReadSucceeded -and
                     $actualCursor.X -eq $clickX -and $actualCursor.Y -eq $clickY) `
            ("The pointer did not land on the exact physical QGC toolbar centre " +
             "(requested=$clickX,$clickY actual=$($actualCursor.X),$($actualCursor.Y) " +
             "dpi_context=0x{0:X})." -f ([MultiDetectWindow.Native]::GetThreadDpiAwarenessContext().ToInt64()))
        $hitWindow = [MultiDetectWindow.Native]::WindowFromPoint($clickPoint)
        $hitRootWindow = [MultiDetectWindow.Native]::GetAncestor($hitWindow, 2) # GA_ROOT
        [uint32]$hitRootOwner = 0
        if ($hitRootWindow -ne [IntPtr]::Zero) {
            $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($hitRootWindow, [ref]$hitRootOwner)
        }
        Assert-True ($hitWindow -ne [IntPtr]::Zero -and $hitRootWindow -eq $window.Handle -and
                     $hitRootOwner -eq $Process.Id) `
            "WindowFromPoint at the QGC-reported toolbar centre is not rooted in the exact QGC main HWND/PID."

        $clickSentAtUtc = (Get-Date).ToUniversalTime()
        Assert-True ([MultiDetectWindow.Native]::SendLeftClick()) "SendInput did not deliver the toolbar Button click."
        $clickEvidence = [ordered]@{
            screen_x = $clickX
            screen_y = $clickY
            window_from_point_hwnd = ("0x{0:X16}" -f $hitWindow.ToInt64())
            root_hwnd = ("0x{0:X16}" -f $hitRootWindow.ToInt64())
            root_owner_pid = $hitRootOwner
            owner_pid = $hitRootOwner
            owner_matches_qgc = ($hitRootOwner -eq $Process.Id)
            root_hwnd_matches_qgc = ($hitRootWindow -eq $window.Handle)
            cursor_position_verified = ($actualCursor.X -eq $clickX -and $actualCursor.Y -eq $clickY)
            dpi_awareness = "per_monitor_v2"
            sent_at_utc = $clickSentAtUtc.ToString('o')
            method = "qml_scene_geometry_export_plus_exact_hwnd_client_to_screen_plus_sendinput"
            indicator_geometry_sequence = [long]$indicator.sequence
        }

        $drawer = Wait-QgcHilUiGeometryRecord -Paths $QgcLogPaths -Process $Process `
            -Role "drawer_open" -MinimumSequence ([long]$indicator.sequence) -TimeoutMilliseconds 8000
        if ($null -eq $drawer) {
            # The exact physical click remains the primary assertion. On
            # Windows, Qt Quick can occasionally miss a SendInput transition
            # while the toolbar Loader is settling even though the pointer is
            # proven to be over the correct QGC HWND. Retry the same control
            # through its native InvokePattern, still rooted in this exact
            # HWND/PID and the geometry-exported accessible identity.
            Assert-True (Set-ExactForegroundWindow -Handle $window.Handle `
                -ExpectedProcessId ([uint32]$Process.Id)) `
                "Exact QGC HWND lost foreground before the native accessibility retry."
            $automationEvidence = Invoke-QgcAutomationButton `
                -MainHandle $window.Handle -ExpectedProcessId ([uint32]$Process.Id) `
                -AccessibleName "MultiDetect AI Status" `
                -AutomationId "toolbar_multiDetectIndicator" -TimeoutMilliseconds 3000
            $drawer = Wait-QgcHilUiGeometryRecord -Paths $QgcLogPaths -Process $Process `
                -Role "drawer_open" -MinimumSequence ([long]$indicator.sequence) -TimeoutMilliseconds 8000
        }
        Assert-True ($null -ne $drawer) `
            "QGC did not emit native ToolIndicatorPage drawer geometry after the exact toolbar click."
        Assert-True ($drawer.state -eq "visible" -and $drawer.visible -eq $true -and
                     $drawer.accessible_name -eq "MultiDetect AI Status Drawer" -and
                     $drawer.accessible_role -eq "Pane") `
            "QGC native drawer geometry record has stale identity, visibility, or accessibility semantics."
        $drawerMapping = Convert-QgcHilUiGeometryToPhysicalScreenRect -Record $drawer `
            -ClientLeft $clientTopLeft.X -ClientTop $clientTopLeft.Y `
            -ClientWidth ($clientBottomRight.X - $clientTopLeft.X) `
            -ClientHeight ($clientBottomRight.Y - $clientTopLeft.Y)
        $drawerRect = $drawerMapping.bounding_rect
        $drawerInsetX = [int][Math]::Max(2, [Math]::Min(
            [Math]::Floor(($drawerRect.width - 1) / 4.0),
            [Math]::Round($drawerRect.width * 0.08)
        ))
        $drawerInsetY = [int][Math]::Max(2, [Math]::Min(
            [Math]::Floor(($drawerRect.height - 1) / 4.0),
            [Math]::Round($drawerRect.height * 0.08)
        ))
        $drawerCenterX = [int][Math]::Round(($drawerRect.left + $drawerRect.right) / 2.0)
        $drawerCenterY = [int][Math]::Round(($drawerRect.top + $drawerRect.bottom) / 2.0)
        $drawerProvenanceSamples = @(
            Get-QgcPointProvenanceSample -Name "center" -ScreenX $drawerCenterX -ScreenY $drawerCenterY `
                -ExpectedRootHwnd $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
            Get-QgcPointProvenanceSample -Name "top_left_inset" `
                -ScreenX ([int]$drawerRect.left + $drawerInsetX) `
                -ScreenY ([int]$drawerRect.top + $drawerInsetY) `
                -ExpectedRootHwnd $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
            Get-QgcPointProvenanceSample -Name "top_right_inset" `
                -ScreenX ([int]$drawerRect.right - $drawerInsetX - 1) `
                -ScreenY ([int]$drawerRect.top + $drawerInsetY) `
                -ExpectedRootHwnd $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
            Get-QgcPointProvenanceSample -Name "bottom_left_inset" `
                -ScreenX ([int]$drawerRect.left + $drawerInsetX) `
                -ScreenY ([int]$drawerRect.bottom - $drawerInsetY - 1) `
                -ExpectedRootHwnd $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
            Get-QgcPointProvenanceSample -Name "bottom_right_inset" `
                -ScreenX ([int]$drawerRect.right - $drawerInsetX - 1) `
                -ScreenY ([int]$drawerRect.bottom - $drawerInsetY - 1) `
                -ExpectedRootHwnd $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
        )
        $drawerPidMatches = @($drawerProvenanceSamples | Where-Object { -not $_.qgc_pid_matches }).Count -eq 0
        $drawerHwndMatches = @($drawerProvenanceSamples | Where-Object { -not $_.qgc_hwnd_matches }).Count -eq 0
        Assert-True ($drawerProvenanceSamples.Count -eq 5 -and $drawerPidMatches -and $drawerHwndMatches) `
            "Native ToolIndicatorPage centre/inset samples are not rooted in the exact QGC main HWND/PID."
        $drawerEvidence = [ordered]@{
            accessible_name = [string]$drawer.accessible_name
            automation_id = [string]$drawer.accessible_id
            control_type = "Qml.Accessible.Pane"
            bounding_rect = $drawerRect
            geometry_source = "qgc_qml_scene_geometry_export"
            geometry_schema = [string]$drawer.schema
            role = [string]$drawer.role
            geometry_role = [string]$drawer.role
            geometry_sequence = [long]$drawer.sequence
            qgc_pid_matches = $drawerPidMatches
            qgc_hwnd_matches = $drawerHwndMatches
            provenance_samples = $drawerProvenanceSamples
            geometry_bound_to_exact_qgc_pid = $true
            geometry_mapped_through_exact_main_hwnd = $true
            qml_scene_rect = $drawer.scene_rect
            qml_window_scene_size = $drawer.window_scene_size
            scene_to_physical_scale_x = $drawerMapping.scene_to_physical_scale_x
            scene_to_physical_scale_y = $drawerMapping.scene_to_physical_scale_y
            reported_device_pixel_ratio = $drawerMapping.reported_device_pixel_ratio
        }
        $visibleOpen = @(Get-QgcVisibleTopLevelHwndSet -Process $Process -MainHandle $window.Handle)
        Assert-True (($visibleOpen -join ',') -eq ($visibleClosed -join ',')) "Opening the MultiDetect drawer created a second visible QGC top-level window."
        $openCaptureEvidence = Save-QgcWindowCapture -Process $Process -Path $OpenCapture

        Assert-True (Set-ExactForegroundWindow -Handle $window.Handle -ExpectedProcessId ([uint32]$Process.Id)) "Exact QGC HWND lost foreground before Escape drawer close."
        Assert-True ([MultiDetectWindow.Native]::SendEscapeKey()) "Native Escape key down/up did not complete."
        $drawerClosed = Wait-QgcHilUiGeometryRecord -Paths $QgcLogPaths -Process $Process `
            -Role "drawer_closed" -MinimumSequence ([long]$drawer.sequence) -TimeoutMilliseconds 8000
        $drawerClosedAfterEscape = $null -ne $drawerClosed -and $drawerClosed.state -eq "closed"
        Assert-True $drawerClosedAfterEscape `
            "MultiDetect native ToolIndicatorPage drawer did not emit its closed lifecycle after Escape."
        $visibleAfterClose = @(Get-QgcVisibleTopLevelHwndSet -Process $Process -MainHandle $window.Handle)
        Assert-True (($visibleAfterClose -join ',') -eq ($visibleClosed -join ',')) "Visible QGC top-level HWND set changed after closing the drawer."
    }
    catch {
        $geometryError = $_.Exception.Message
    }
    finally {
        if ($null -ne $drawerEvidence -and -not $drawerClosedAfterEscape) {
            $null = Set-ExactForegroundWindow -Handle $window.Handle -ExpectedProcessId ([uint32]$Process.Id)
            $null = [MultiDetectWindow.Native]::SendEscapeKey()
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-Path -LiteralPath $OpenCapture -PathType Leaf) -and (Test-Path -LiteralPath $ClosedCapture -PathType Leaf)) {
            # Preserve deterministic validator input when the exact QGC QML
            # geometry or native input path fails. Identical pixels plus the
            # geometry_error keep the machine gate fail-closed.
            Copy-Item -LiteralPath $ClosedCapture -Destination $OpenCapture
        }
        if ($cursorPreserved) {
            $null = [MultiDetectWindow.Native]::SetCursorPos($previousCursor.X, $previousCursor.Y)
        }
        if ($null -ne $previousForeground) {
            Restore-QgcWindowCaptureForeground -Window $window -Previous $previousForeground
        }
        $null = [MultiDetectWindow.Native]::SetThreadDpiAwarenessContext($previousInteractionDpiContext)
        $interactionDpiContextRestored = [MultiDetectWindow.Native]::AreDpiAwarenessContextsEqual(
            [MultiDetectWindow.Native]::GetThreadDpiAwarenessContext(), $previousInteractionDpiContext
        )
    }
    Assert-True $interactionDpiContextRestored "Toolbar input thread DPI-awareness context was not restored."

    $observation = [ordered]@{
        schema = "multidetect.qgc-native-toolbar-observation"
        schema_version = 1
        generated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        qgc_pid = $Process.Id
        qgc_main_hwnd = $mainHwndText
        client_screen_rect = [ordered]@{
            left = $clientTopLeft.X; top = $clientTopLeft.Y
            right = $clientBottomRight.X; bottom = $clientBottomRight.Y
            width = ($clientBottomRight.X - $clientTopLeft.X)
            height = ($clientBottomRight.Y - $clientTopLeft.Y)
        }
        indicator = $indicatorEvidence
        drawer = $drawerEvidence
        click = $clickEvidence
        accessibility_retry = $automationEvidence
        visible_top_level_hwnds_closed = @($visibleClosed)
        visible_top_level_hwnds_open = @($visibleOpen)
        visible_top_level_hwnds_after_close = @($visibleAfterClose)
        drawer_closed_after_escape = $drawerClosedAfterEscape
        closed_capture = $closedCaptureEvidence
        open_capture = $openCaptureEvidence
        geometry_error = $geometryError
        automation_error = $null
        ui_automation_status = "not_required_qgc_qml_geometry_export"
        complete_qml_rendering_semantics_proved = $false
        operator_visual_acceptance = "not_claimed"
    }
    $observation | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $ObservationPath -Encoding utf8
    $validation = Invoke-CapturedCommand -FilePath $Python -ArgumentList @(
        "-u", $Validator,
        "--observation", $ObservationPath,
        "--closed-capture", $ClosedCapture,
        "--open-capture", $OpenCapture,
        "--result-out", $ResultPath
    ) -TimeoutSeconds 30
    Assert-True (Test-Path -LiteralPath $ResultPath -PathType Leaf) "Native toolbar validator did not write fail-closed evidence."
    $result = Get-Content -LiteralPath $ResultPath -Raw | ConvertFrom-Json
    Assert-True ($validation.ExitCode -eq 0 -and $result.gate_passed -eq $true) "Native toolbar machine-visual gate failed: $($validation.Lines -join ' ')"
    Assert-True ($result.machine_visual_toolbar_smoke -eq "passed") "Native toolbar evidence did not report the bounded machine smoke pass."
    Assert-True ($result.complete_qml_rendering_semantics_proved -eq $false) "Native toolbar evidence improperly claimed complete QML semantics."
    Assert-True ($result.operator_visual_acceptance -eq "not_claimed") "Native toolbar evidence improperly claimed operator visual acceptance."
    return $result
}

function Invoke-QgcNativePipSwap {
    param([Diagnostics.Process]$Process)
    $window = Get-QgcWindow -Process $Process
    [MultiDetectWindow.Native+RECT]$clientRect = New-Object MultiDetectWindow.Native+RECT
    Assert-True ([MultiDetectWindow.Native]::GetClientRect($window.Handle, [ref]$clientRect)) "GetClientRect failed before PiP swap."
    $width = $clientRect.Right - $clientRect.Left
    $height = $clientRect.Bottom - $clientRect.Top
    Assert-True ($width -ge 640 -and $height -ge 480) "QGC client area is unexpectedly small before PiP swap."
    # Derived from QGC PipView.qml: width=parent.width*0.2, height=width*9/16,
    # anchored left/bottom by FlyView.qml with the native tools margin. Click
    # the centre of that QGC-owned MouseArea, away from its resize/window/hide
    # controls. Use real foreground pointer input: Qt Quick does not reliably
    # promote posted WM_LBUTTON messages into a QQuickPointerEvent sequence.
    $pipWidth = $width * 0.20
    $pipHeight = $pipWidth * 9.0 / 16.0
    $margin = [Math]::Max(8.0, $width * 0.004)
    [MultiDetectWindow.Native+POINT]$clientPoint = New-Object MultiDetectWindow.Native+POINT
    $clientPoint.X = [int]($margin + ($pipWidth / 2.0))
    $clientPoint.Y = [int]($height - $margin - ($pipHeight / 2.0))
    [MultiDetectWindow.Native+POINT]$screenPoint = New-Object MultiDetectWindow.Native+POINT
    $screenPoint.X = $clientPoint.X
    $screenPoint.Y = $clientPoint.Y
    Assert-True ([MultiDetectWindow.Native]::ClientToScreen($window.Handle, [ref]$screenPoint)) "Unable to map the native QGC PiP point to screen coordinates."
    [MultiDetectWindow.Native+POINT]$previousCursor = New-Object MultiDetectWindow.Native+POINT
    Assert-True ([MultiDetectWindow.Native]::GetCursorPos([ref]$previousCursor)) "Unable to preserve the current pointer position."
    $previousForeground = Set-QgcWindowCaptureForeground -Window $window
    try {
        Assert-True ([MultiDetectWindow.Native]::SetCursorPos($screenPoint.X, $screenPoint.Y)) "Unable to move the pointer to the native QGC PiP."
        Start-Sleep -Milliseconds 120
        $hitWindow = [MultiDetectWindow.Native]::WindowFromPoint($screenPoint)
        [uint32]$hitOwner = 0
        $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($hitWindow, [ref]$hitOwner)
        Assert-True ($hitOwner -eq $Process.Id) "Native PiP pointer coordinate is not owned by the exact QGC process."
        $foregroundBeforeClick = [MultiDetectWindow.Native]::GetForegroundWindow()
        [uint32]$foregroundOwnerBeforeClick = 0
        $null = [MultiDetectWindow.Native]::GetWindowThreadProcessId($foregroundBeforeClick, [ref]$foregroundOwnerBeforeClick)
        Assert-True ($foregroundBeforeClick -eq $window.Handle -and $foregroundOwnerBeforeClick -eq $Process.Id) "Exact QGC HWND/PID lost foreground before native PiP click."
        [MultiDetectWindow.Native+POINT]$cursorBeforeClick = New-Object MultiDetectWindow.Native+POINT
        Assert-True ([MultiDetectWindow.Native]::GetCursorPos([ref]$cursorBeforeClick)) "Unable to verify the native PiP pointer position."
        Assert-True ($cursorBeforeClick.X -eq $screenPoint.X -and $cursorBeforeClick.Y -eq $screenPoint.Y) "Native pointer did not reach the exact QGC PiP coordinate."
        $clickSentAtUtc = (Get-Date).ToUniversalTime()
        Assert-True ([MultiDetectWindow.Native]::SendLeftClick()) "SendInput did not deliver both native left-click events."
        Start-Sleep -Milliseconds 800
    }
    finally {
        $null = [MultiDetectWindow.Native]::SetCursorPos($previousCursor.X, $previousCursor.Y)
        Restore-QgcWindowCaptureForeground -Window $window -Previous $previousForeground
    }
    return [ordered]@{
        screen_x = $screenPoint.X
        screen_y = $screenPoint.Y
        client_x = $clientPoint.X
        client_y = $clientPoint.Y
        client_width = $width
        client_height = $height
        hit_hwnd = ("0x{0:X}" -f $hitWindow.ToInt64())
        hit_owner_pid = $hitOwner
        foreground_hwnd_before_click = ("0x{0:X}" -f $foregroundBeforeClick.ToInt64())
        foreground_owner_pid_before_click = $foregroundOwnerBeforeClick
        cursor_verified_before_click = ($cursorBeforeClick.X -eq $screenPoint.X -and $cursorBeforeClick.Y -eq $screenPoint.Y)
        click_sent_at_utc = $clickSentAtUtc.ToString('o')
        method = "exact_pid_hwnd_foreground_pointer_click"
        derivation = "QGC FlyView anchors left/bottom plus native PipView 20 percent by 9/16 geometry"
    }
}

function Get-QgcPipSettingsSnapshot {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            path = $Path; path_exists = $false; qgc_qml_section_exists = $false
            is_pip_visible_explicit = $null; main_fly_window_is_map_explicit = $null
            last_write_time_utc = $null; sha256 = $null
        }
    }
    $text = Get-Content -LiteralPath $Path -Raw
    $section = [regex]::Match($text, '(?ms)^\[QGCQml\]\s*(?<body>.*?)(?=^\[|\z)')
    $pipSetting = if ($section.Success) { [regex]::Match($section.Groups['body'].Value, '(?mi)^IsPIPVisible\s*=\s*(?<value>true|false|1|0)\s*$') } else { $null }
    $mapSetting = if ($section.Success) { [regex]::Match($section.Groups['body'].Value, '(?mi)^MainFlyWindowIsMap\s*=\s*(?<value>true|false|1|0)\s*$') } else { $null }
    $item = Get-Item -LiteralPath $Path
    return [pscustomobject]@{
        path = $Path
        path_exists = $true
        qgc_qml_section_exists = $section.Success
        is_pip_visible_explicit = if ($null -ne $pipSetting -and $pipSetting.Success) { @('true', '1') -contains $pipSetting.Groups['value'].Value.ToLowerInvariant() } else { $null }
        main_fly_window_is_map_explicit = if ($null -ne $mapSetting -and $mapSetting.Success) { @('true', '1') -contains $mapSetting.Groups['value'].Value.ToLowerInvariant() } else { $null }
        last_write_time_utc = $item.LastWriteTimeUtc.ToString('o')
        sha256 = Get-Sha256OrNull -Path $Path
    }
}

function Get-QgcVideoSettingsSnapshot {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            path = $Path
            path_exists = $false
            video_section_exists = $false
            force_cpu_video_path_explicit = $false
            force_cpu_video_path_raw = $null
            force_cpu_video_path_enabled = $false
            force_video_decoder_explicit = $false
            force_video_decoder_raw = $null
            last_write_time_utc = $null
            sha256 = $null
        }
    }
    $text = Get-Content -LiteralPath $Path -Raw
    $section = [regex]::Match($text, '(?ms)^\[Video\]\s*\r?\n(?<body>.*?)(?=^\[|\z)')
    $forceCpu = if ($section.Success) {
        [regex]::Match($section.Groups['body'].Value, '(?mi)^forceCpuVideoPath\s*=\s*(?<value>[^\r\n]*)\s*$')
    }
    else { $null }
    $forceDecoder = if ($section.Success) {
        [regex]::Match($section.Groups['body'].Value, '(?mi)^forceVideoDecoder\s*=\s*(?<value>[^\r\n]*)\s*$')
    }
    else { $null }
    $forceCpuRaw = if ($null -ne $forceCpu -and $forceCpu.Success) { $forceCpu.Groups['value'].Value.Trim() } else { $null }
    $forceDecoderRaw = if ($null -ne $forceDecoder -and $forceDecoder.Success) { $forceDecoder.Groups['value'].Value.Trim() } else { $null }
    $item = Get-Item -LiteralPath $Path
    return [pscustomobject]@{
        path = $Path
        path_exists = $true
        video_section_exists = $section.Success
        force_cpu_video_path_explicit = ($null -ne $forceCpu -and $forceCpu.Success)
        force_cpu_video_path_raw = $forceCpuRaw
        force_cpu_video_path_enabled = ($null -ne $forceCpuRaw -and @('true', '1', 'yes', 'on') -contains $forceCpuRaw.ToLowerInvariant())
        force_video_decoder_explicit = ($null -ne $forceDecoder -and $forceDecoder.Success)
        force_video_decoder_raw = $forceDecoderRaw
        last_write_time_utc = $item.LastWriteTimeUtc.ToString('o')
        sha256 = Get-Sha256OrNull -Path $Path
    }
}

function Wait-QgcCpuVideoSettings {
    param(
        [string]$Path,
        [int]$TimeoutSeconds = 15,
        [Diagnostics.Process[]]$Processes = @()
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { $snapshot = Get-QgcVideoSettingsSnapshot -Path $Path }
        catch { $snapshot = $null }
        if (
            $null -ne $snapshot -and
            $snapshot.force_cpu_video_path_explicit -eq $true -and
            $snapshot.force_cpu_video_path_enabled -eq $true -and
            $snapshot.force_video_decoder_explicit -eq $false
        ) {
            return $snapshot
        }
        foreach ($process in $Processes) {
            if ($null -ne $process -and $process.HasExited) {
                throw "Process $($process.Id) exited before QGC persisted the isolated-HIL CPU-video A/B setting."
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for Video/forceCpuVideoPath=true with no forceVideoDecoder override in $Path."
}

function Wait-QgcMainFlyWindowIsMapSetting {
    param(
        [string]$Path,
        [bool]$Expected,
        [int]$TimeoutSeconds = 10,
        [Diagnostics.Process[]]$Processes = @()
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { $snapshot = Get-QgcPipSettingsSnapshot -Path $Path }
        catch { $snapshot = $null }
        if ($null -ne $snapshot -and $null -ne $snapshot.main_fly_window_is_map_explicit -and $snapshot.main_fly_window_is_map_explicit -eq $Expected) { return $snapshot }
        foreach ($process in $Processes) {
            if ($null -ne $process -and $process.HasExited) {
                throw "Process $($process.Id) exited before QGC persisted QGCQml/MainFlyWindowIsMap=$($Expected.ToString().ToLowerInvariant())."
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for native QGC QGCQml/MainFlyWindowIsMap=$($Expected.ToString().ToLowerInvariant()) in $Path."
}

Assert-True ($PythonCandidates.Count -gt 0) "QGC Python venv is unavailable."
Assert-True ($DockerCandidates.Count -gt 0) "Docker Desktop CLI is unavailable."
$Python = @($PythonCandidates)[0]
$Docker = @($DockerCandidates)[0]
$DockerDirectory = Split-Path -Parent $Docker
$DockerChildArgument = if ($Docker -match '\s') { [char]34 + $Docker + [char]34 } else { $Docker }
foreach ($required in @(
    $QgcExecutable, $RtspHelper, $ScenarioManifest, $MissionConfig, $StreamScript,
    $DesktopCaptureValidator, $NativeVideoGrabValidator, $ToolbarLayoutValidator,
    $AttachedVtolScript, $MissionAcceptanceScript,
    $ModelConfig, $ModelSdf, $WorldAsset, $TerrainDem,
    $OnnxModel, $ModelManifest
)) {
    Assert-True (Test-Path -LiteralPath $required -PathType Leaf) "Required combined-SIM artifact is missing: $required"
}
$ModelManifestDocument = Get-Content -LiteralPath $ModelManifest -Raw | ConvertFrom-Json
$ModelInputShape = @($ModelManifestDocument.input.shape)
Assert-True ($ModelInputShape.Count -eq 4) "Model manifest input.shape must contain exactly four NCHW dimensions."
Assert-True ([int]$ModelInputShape[0] -eq 1) "Model manifest input.shape batch dimension must be 1."
Assert-True ([int]$ModelInputShape[1] -gt 0) "Model manifest input.shape channel dimension must be positive."
$PerceptionInputHeight = [int]$ModelInputShape[2]
$PerceptionInputWidth = [int]$ModelInputShape[3]
Assert-True ($PerceptionInputWidth -gt 0 -and $PerceptionInputHeight -gt 0) "Model manifest input.shape width/height must be positive."
Assert-True (-not (Test-Path -LiteralPath $RunDirectory)) "Run directory already exists: $RunDirectory"
Assert-True (-not (Test-Path -LiteralPath $EvidencePath)) "Evidence path already exists: $EvidencePath"
[IO.Directory]::CreateDirectory($ArtifactDirectory) | Out-Null
[IO.Directory]::CreateDirectory($RunDirectory) | Out-Null
try { Remove-Item -LiteralPath $CombinedStageTracePath -Force -ErrorAction SilentlyContinue } catch { }
Write-CombinedStageTrace -Message 'run-directory-ready'
if (-not [string]::IsNullOrWhiteSpace($EvidenceParentDirectory)) {
    [IO.Directory]::CreateDirectory($EvidenceParentDirectory) | Out-Null
}

$allocatedCombinedSimPorts = @(Get-FreeCombinedSimPorts `
    -Count $CombinedSimHostPortCount `
    -Minimum $CombinedSimPortRangeMin `
    -Maximum $CombinedSimPortRangeMax `
    -Excluded @($ProtectedGroundStationPort, $Px4ContainerInputPort))
Assert-True ($allocatedCombinedSimPorts.Count -eq $CombinedSimHostPortCount) `
    "Combined SIM allocated an unexpected number of host ports."
$OperatorServerPort = [int]$allocatedCombinedSimPorts[0]
$OperatorQgcLocalPort = [int]$allocatedCombinedSimPorts[1]
$Px4TelemetryPort = [int]$allocatedCombinedSimPorts[2]
$Px4InputHostPort = [int]$allocatedCombinedSimPorts[3]
$QgcPx4Port = [int]$allocatedCombinedSimPorts[4]
$QgcRouterPort = [int]$allocatedCombinedSimPorts[5]
$EvaluatorPort = [int]$allocatedCombinedSimPorts[6]
$EvaluatorRouterPort = [int]$allocatedCombinedSimPorts[7]
$RtspPort = [int]$allocatedCombinedSimPorts[8]

$allUdpPorts = @(
    $OperatorServerPort, $OperatorQgcLocalPort, $Px4TelemetryPort, $Px4InputHostPort,
    $QgcPx4Port, $QgcRouterPort, $EvaluatorPort, $EvaluatorRouterPort
)
Assert-True (($allUdpPorts | Select-Object -Unique).Count -eq $allUdpPorts.Count) "Combined SIM UDP ports are not distinct."
Assert-True ($allUdpPorts -notcontains $ProtectedGroundStationPort) "Combined SIM attempted to use protected UDP 14550."
foreach ($port in $allUdpPorts) {
    Assert-True (@(Get-UdpPortOwners -Port $port).Count -eq 0) "Combined SIM UDP port $port is already occupied."
}
Assert-True (@(Get-TcpPortOwners -Port $RtspPort).Count -eq 0) "Combined SIM RTSP port $RtspPort is already occupied."
$protectedOwnersBefore = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
$crashDumpsBefore = @(Get-CrashDumpSignatures)

$runtimePreflightPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
try {
    # This probe is repository-owned and read-only.  Keep the environment
    # override local so the outer acceptance cleanup contract remains intact.
    $env:PYTHONPATH = Join-Path $Root "src"
    $runtimePreflight = Invoke-CapturedCommand -FilePath $Python -ArgumentList @(
        "-m", "multidetect.simulator", "runtime-preflight", "--out", $RuntimePreflightEvidencePath
    ) -TimeoutSeconds 20
}
finally {
    if ($null -eq $runtimePreflightPythonPath) {
        Remove-Item -LiteralPath "Env:PYTHONPATH" -ErrorAction SilentlyContinue
    }
    else {
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $runtimePreflightPythonPath, "Process")
    }
}
Assert-True ($runtimePreflight.ExitCode -eq 0) `
    "External SIM runtime-preflight command failed: $($runtimePreflight.Lines -join ' ')"
Assert-True (Test-Path -LiteralPath $RuntimePreflightEvidencePath -PathType Leaf) `
    "Runtime-preflight did not write its evidence artifact: $RuntimePreflightEvidencePath"
$runtimePreflightDocument = Get-Content -LiteralPath $RuntimePreflightEvidencePath -Raw | ConvertFrom-Json
$runtimePreflightReadiness = $runtimePreflightDocument.readiness
Assert-True ($null -ne $runtimePreflightReadiness -and $runtimePreflightReadiness.ready_to_attempt -eq $true) `
    "External SIM runtime is not ready: $($runtimePreflightReadiness.reason). See $RuntimePreflightEvidencePath"

$dockerInfo = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("info", "--format", "{{.ServerVersion}}") -TimeoutSeconds 30
Assert-True ($dockerInfo.ExitCode -eq 0) "Docker Engine is not ready: $($dockerInfo.Lines -join ' ')"
$dockerServerVersion = ($dockerInfo.Lines | Select-Object -Last 1).Trim()
$imageInspect = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("image", "inspect", $ImageReference, "--format", "{{.Id}}") -TimeoutSeconds 30
if ($imageInspect.ExitCode -ne 0) {
    $pull = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("pull", $ImageReference) -TimeoutSeconds 600
    Assert-True ($pull.ExitCode -eq 0) "Unable to pull pinned PX4/Gazebo image."
    $imageInspect = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("image", "inspect", $ImageReference, "--format", "{{.Id}}") -TimeoutSeconds 30
}
Assert-True ($imageInspect.ExitCode -eq 0) "Pinned PX4/Gazebo image is unavailable."
$imageId = ($imageInspect.Lines | Select-Object -Last 1).Trim()
$existing = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("container", "inspect", $ContainerName)
Assert-True ($existing.ExitCode -ne 0) "Container already exists; refusing to replace it: $ContainerName"

$streamScriptHash = Get-Sha256OrNull -Path $StreamScript
$worldHash = Get-Sha256OrNull -Path $WorldAsset
$terrainDemHash = Get-Sha256OrNull -Path $TerrainDem
$modelAssetsHash = Get-CompositeSha256 -Paths @($ModelConfig, $ModelSdf)
$airframeProfileIdentity = @(
    "image=$ImageReference",
    "autostart=4004",
    "model=gz_standard_vtol_mono_cam",
    "px4_gz_models=/opt/multidetect-gazebo-models",
    "world=default",
    "world_sha256=$worldHash"
) -join "`n"
$airframeProfileHash = Get-StringSha256 -Value $airframeProfileIdentity
$gitHead = (& git -C $Root rev-parse HEAD).Trim()
$gitStatus = (& git -C $Root status --porcelain=v1 -uall) -join "`n"
$gitStatusHash = Get-StringSha256 -Value $gitStatus

$environmentNames = @(
    "MULTIDETECT_EXPECTED_RUNTIME_MODE", "MULTIDETECT_OPERATOR_KEY",
    "MULTIDETECT_OPERATOR_ID", "MULTIDETECT_OPERATOR_UDP_HOST",
    "MULTIDETECT_OPERATOR_UDP_PORT", "MULTIDETECT_OPERATOR_UDP_LOCAL_PORT",
    "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX", "MULTIDETECT_OPERATOR_STREAM_ID",
    "MULTIDETECT_OPERATOR_STREAM_WIDTH", "MULTIDETECT_OPERATOR_STREAM_HEIGHT",
    "MULTIDETECT_OPERATOR_STREAM_ROTATION", "MULTIDETECT_OPERATOR_GCS_SYSTEM_ID",
    "MULTIDETECT_OPERATOR_JETSON_SYSTEM_ID", "MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID",
    "MULTIDETECT_VIDEO_RTSP_URL", "MULTIDETECT_DISABLE_VEHICLE_LINK",
    "MULTIDETECT_HIL_UI_AUTO_EXERCISE", "MULTIDETECT_HIL_UI_AUTO_EXIT",
    "MULTIDETECT_HIL_UI_GEOMETRY_EXPORT", "MULTIDETECT_HIL_VIDEO_GRAB_EXPORT",
    "MULTIDETECT_HIL_VIDEO_GRAB_PREFIX",
    "QGC_LOG_LEVEL",
    "PYTHONPATH", "PATH"
)
$savedEnvironment = @{}
$environmentSnapshotComplete = $false
$hmacBytes = $null
$signingBytes = $null
$temporaryHmac = $null
$temporarySigningHex = $null

# This outer guard begins before the first secret allocation or process-level
# environment mutation and remains active through every preflight, execution,
# evidence, and terminal gate path below.
try {
foreach ($name in $environmentNames) { $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
$environmentSnapshotComplete = $true
$hmacBytes = New-Object byte[] 32
$signingBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($hmacBytes); $rng.GetBytes($signingBytes) }
finally { $rng.Dispose() }
$temporaryHmac = [Convert]::ToBase64String($hmacBytes)
$temporarySigningHex = ([BitConverter]::ToString($signingBytes) -replace "-", "").ToLowerInvariant()

$env:MULTIDETECT_EXPECTED_RUNTIME_MODE = "SIM"
$env:MULTIDETECT_OPERATOR_KEY = $temporaryHmac
$env:MULTIDETECT_OPERATOR_ID = "sim-qgc-operator"
$env:MULTIDETECT_OPERATOR_UDP_HOST = "127.0.0.1"
$env:MULTIDETECT_OPERATOR_UDP_PORT = "$OperatorServerPort"
$env:MULTIDETECT_OPERATOR_UDP_LOCAL_PORT = "$OperatorQgcLocalPort"
$env:MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX = $temporarySigningHex
$env:MULTIDETECT_OPERATOR_STREAM_ID = "camera-main"
$env:MULTIDETECT_OPERATOR_STREAM_WIDTH = "1280"
$env:MULTIDETECT_OPERATOR_STREAM_HEIGHT = "960"
$env:MULTIDETECT_OPERATOR_STREAM_ROTATION = "0"
$env:MULTIDETECT_OPERATOR_GCS_SYSTEM_ID = "255"
$env:MULTIDETECT_OPERATOR_JETSON_SYSTEM_ID = "1"
$env:MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID = "191"
$env:MULTIDETECT_VIDEO_RTSP_URL = "rtsp://127.0.0.1:$RtspPort/multidetect-sim"
# PowerShell's overload binder converts `$null` to an empty string for
# Environment.SetEnvironmentVariable.  Qt then observes the variable as set,
# and the fail-closed flag parser deliberately treats that empty value as true.
# Remove the entries through the Env: provider so the QGC child really inherits
# them as absent.
Remove-Item -LiteralPath "Env:MULTIDETECT_DISABLE_VEHICLE_LINK" -ErrorAction SilentlyContinue
# These acceptance-only switches must remain absent, not merely set to "0".
# CustomPlugin parses false spellings, but QGCApplication/main deliberately use
# qEnvironmentVariableIsSet for their special auto-exit teardown path; "0" would
# therefore still opt this native Fly View process into that path.
Remove-Item -LiteralPath "Env:MULTIDETECT_HIL_UI_AUTO_EXERCISE" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "Env:MULTIDETECT_HIL_UI_AUTO_EXIT" -ErrorAction SilentlyContinue
$env:MULTIDETECT_HIL_UI_GEOMETRY_EXPORT = "1"
$env:MULTIDETECT_HIL_VIDEO_GRAB_EXPORT = "1"
$env:MULTIDETECT_HIL_VIDEO_GRAB_PREFIX = $NativeVideoGrabPrefix
$env:QGC_LOG_LEVEL = "info"
$hilUiGeometryExportEnabledBeforeLaunch =
    (Test-Path -LiteralPath "Env:MULTIDETECT_HIL_UI_GEOMETRY_EXPORT") -and
    $env:MULTIDETECT_HIL_UI_GEOMETRY_EXPORT -eq "1"
Assert-True $hilUiGeometryExportEnabledBeforeLaunch `
    "Combined SIM could not enable its read-only QGC toolbar geometry export."
$hilNativeVideoGrabExportEnabledBeforeLaunch =
    (Test-Path -LiteralPath "Env:MULTIDETECT_HIL_VIDEO_GRAB_EXPORT") -and
    $env:MULTIDETECT_HIL_VIDEO_GRAB_EXPORT -eq "1" -and
    [IO.Path]::IsPathRooted($env:MULTIDETECT_HIL_VIDEO_GRAB_PREFIX) -and
    [IO.Path]::GetFullPath($env:MULTIDETECT_HIL_VIDEO_GRAB_PREFIX) -eq
        [IO.Path]::GetFullPath($NativeVideoGrabPrefix)
Assert-True $hilNativeVideoGrabExportEnabledBeforeLaunch `
    "Combined SIM could not enable its fail-closed native QGC VideoOutput grab export."
$qgcLogLevelBeforeLaunch = $env:QGC_LOG_LEVEL
Assert-True ($qgcLogLevelBeforeLaunch -eq "info") `
    "Combined SIM could not force deterministic QGC info-level geometry logging."
$acceptanceOnlyEnvironmentAbsentBeforeLaunch =
    -not (Test-Path -LiteralPath "Env:MULTIDETECT_DISABLE_VEHICLE_LINK") -and
    -not (Test-Path -LiteralPath "Env:MULTIDETECT_HIL_UI_AUTO_EXERCISE") -and
    -not (Test-Path -LiteralPath "Env:MULTIDETECT_HIL_UI_AUTO_EXIT")
Assert-True $acceptanceOnlyEnvironmentAbsentBeforeLaunch `
    "Combined SIM must launch QGC without replay link isolation or automatic HIL UI exercise variables."
$env:PYTHONPATH = Join-Path $Root "src"
$env:PATH = "$QgcPortableBin;$DockerDirectory;$env:PATH"

$containerStarted = $false
$containerStopped = $false
$containerId = $null
$containerEvidence = $null
$routerProcess = $null
$qgcProcess = $null
$integratedProcess = $null
$vtolProcess = $null
$missionProcess = $null
$routerEvidence = $null
$integratedStartEvidence = $null
$integratedEvidence = $null
$vtolEvidence = $null
$missionEvidence = $null
$initialCapture = $null
$pipSwappedCapture = $null
$fixedWingCapture = $null
$missionActiveCapture = $null
$missionActiveReadyEvidence = $null
$liveVideoSurfaceEvidence = $null
$nativeVideoGrabEvidence = $null
$toolbarEvidence = $null
$qgcVideoSettingsBeforeLaunch = $null
$qgcVideoSettingsAfterLaunch = $null
$qgcVideoSettingsAfterRun = $null
$qgcVideoBackendSettingsEvidence = $null
$qgcStartTimeUtc = $null
$pipSwap = $null
$qgcUdpPortsDuring = @()
$failure = $null
$qgcCloseMethod = $null
$postAdoptionAckCountBeforeFixedWing = $null
$postAdoptionAckCountAtFixedWingReady = $null
$mainFlyWindowIsMapBeforePipSwap = $null
$mainFlyWindowIsMapAfterPipSwap = $null
$expectedRuntimeRunHash64Hex = $null
$pipSwappedSequenceAfterCapture = $null
$fixedWingSequenceAfterCapture = $null
$missionReadyQgcSequenceBaseline = $null
$missionCaptureQgcSequenceTarget = $null
$missionActiveSequenceAfterCapture = $null
$missionExitCode = $null
$missionStageFailure = $null
$missionResultParseFailure = $null
$missionDiagnostics = $null
$missionJournalEvidence = $null
$requiredSensorTopics = @(
    "/world/default/model/standard_vtol_mono_cam_0/link/camera_link/sensor/camera/image",
    "/world/default/model/standard_vtol_mono_cam_0/link/camera_link/sensor/camera/camera_info",
    "/world/default/model/standard_vtol_mono_cam_0/link/base_link/sensor/navsat_sensor/navsat",
    "/model/standard_vtol_mono_cam_0/odometry_with_covariance"
)
$requiredOdometryTopic = "/model/standard_vtol_mono_cam_0/odometry_with_covariance"
$requiredSensorTopicsReady = $false
$missingRequiredSensorTopics = @($requiredSensorTopics)
$lastSensorTopicList = @()
$requiredSensorPublisherStates = @()
$requiredSensorPublisherStableChecks = 0
$odometryTopicListed = $false
$odometryPublisherStatus = "not_checked"
$odometryPublisherPresent = $null
$odometryTopicInfoExitCode = $null
$odometryTopicInfoLines = @()

try {
    $startup = @'
set -eu
DOCKER_HOST_IP="$(getent ahostsv4 host.docker.internal | awk '/STREAM/ {print $1; exit}')"
test -n "$DOCKER_HOST_IP"
RC=/opt/px4-gazebo/etc/init.d-posix/px4-rc.mavlink
sed -i "s|mavlink start -x -u \$udp_gcs_port_local.*|mavlink start -x -t $DOCKER_HOST_IP -u \$udp_gcs_port_local -o __PX4_TELEMETRY_PORT__ -r 4000000 -f \$mavlink_network_interface_arg|" "$RC"
exec /opt/px4-gazebo/bin/px4 -s etc/init.d-posix/rcS
'@
    # PowerShell here-strings inherit the checkout's line endings.  The
    # payload is passed as one exact argv element to Linux `/bin/sh -c`, where
    # a CR from a Windows CRLF checkout turns `set -eu` into an invalid option.
    # Normalize only this in-container shell payload; keep argv boundaries
    # exact so quoting, `$1`, and the sed expression remain untouched.
    $startup = $startup.Replace("`r`n", "`n").Replace("`r", "`n")
    $startup = $startup.Replace("__PX4_TELEMETRY_PORT__", "$Px4TelemetryPort")
    $run = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @(
        "run", "-d", "--name", $ContainerName,
        "--label", "multidetect.purpose=$PurposeLabel",
        "--network", "bridge", "--add-host", "host.docker.internal:host-gateway",
        "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
        "--publish", "127.0.0.1:$Px4InputHostPort`:$Px4ContainerInputPort/udp",
        "--mount", "type=bind,source=$ModelRoot,target=/opt/multidetect-gazebo-models,readonly",
        "--mount", "type=bind,source=$WorldAsset,target=/opt/px4-gazebo/share/gz/worlds/default.sdf,readonly",
        "--mount", "type=bind,source=$(Join-Path $Root 'scripts'),target=/workspace/scripts,readonly",
        "--workdir", "/opt/px4-gazebo", "--entrypoint", "/bin/sh",
        "--env", "HEADLESS=1", "--env", "PX4_SYS_AUTOSTART=4004",
        "--env", "PX4_SIM_MODEL=gz_standard_vtol_mono_cam",
        "--env", "PX4_GZ_MODELS=/opt/multidetect-gazebo-models",
        "--env", "PX4_GZ_WORLD=default", "--env", "GZ_VERBOSE=0",
        "--env", "GZ_SIM_RESOURCE_PATH=/opt/multidetect-gazebo-models:/opt/px4-gazebo/share/gz/models:/opt/px4-gazebo/share/gz/worlds",
        $ImageReference, "-c", $startup
    ) -TimeoutSeconds 30
    Assert-True ($run.ExitCode -eq 0) "Combined PX4/Gazebo container failed to start: $($run.Lines -join ' ')"
    $containerId = ($run.Lines | Select-Object -Last 1).Trim()
    Assert-True (-not [string]::IsNullOrWhiteSpace($containerId)) "Docker returned an empty container ID."
    $containerStarted = $true
    $containerEvidence = Get-OwnedContainerEvidence -Docker $Docker -Name $ContainerName -ExpectedId $containerId

    for ($attempt = 1; $attempt -le 90; $attempt++) {
        $state = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("inspect", "--format", "{{.State.Running}}", $ContainerName)
        Assert-True ($state.ExitCode -eq 0 -and ($state.Lines | Select-Object -Last 1).Trim() -eq "true") "Combined container exited before Gazebo topics were ready."
        $topics = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("exec", $ContainerName, "gz", "topic", "-l") -TimeoutSeconds 10
        if ($topics.ExitCode -eq 0) {
            $lastSensorTopicList = @($topics.Lines)
            $missingRequiredSensorTopics = @($requiredSensorTopics | Where-Object { $lastSensorTopicList -notcontains $_ })
            if ($missingRequiredSensorTopics.Count -eq 0) {
                # A topic name can survive a Gazebo/PX4 plugin restart while
                # its publisher is still absent.  Require two consecutive
                # publisher probes before starting the binary sensor helper;
                # this closes the startup race that otherwise presents an
                # immediate EOF to docker exec on the first frame.
                $requiredSensorPublisherStates = @($requiredSensorTopics | ForEach-Object {
                    Get-GazeboTopicPublisherState -Docker $Docker -ContainerName $ContainerName -Topic $_
                })
                if (@($requiredSensorPublisherStates | Where-Object { -not $_.publisher_present }).Count -eq 0) {
                    $requiredSensorPublisherStableChecks++
                }
                else {
                    $requiredSensorPublisherStableChecks = 0
                }
                if ($requiredSensorPublisherStableChecks -ge 2) {
                    $requiredSensorTopicsReady = $true
                    break
                }
            }
        }
        Start-Sleep -Milliseconds 500
    }
    Assert-True $requiredSensorTopicsReady "Gazebo camera image/CameraInfo/NavSat/full-odometry topics were not ready within 90 seconds."

    # `gz topic -l` lists a topic created only by a subscriber as well as one
    # backed by a publisher.  Full 3D odometry is a wire-v2 readiness source;
    # classify it separately as well so a subscriber-created topic name cannot
    # become a false pose/velocity-fusion claim.
    $odometryTopicListed = $lastSensorTopicList -contains $requiredOdometryTopic
    $odometryTopicInfo = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @(
        "exec", $ContainerName, "gz", "topic", "-i", "-t", $requiredOdometryTopic
    ) -TimeoutSeconds 10
    $odometryTopicInfoExitCode = $odometryTopicInfo.ExitCode
    $odometryTopicInfoLines = @($odometryTopicInfo.Lines)
    Assert-True ($odometryTopicInfoExitCode -eq 0) "Unable to classify required Gazebo odometry publisher state."
    $odometryTopicInfoText = $odometryTopicInfoLines -join "`n"
    if ($odometryTopicInfoText -match "(?im)^\s*No publishers on topic\b") {
        $odometryPublisherStatus = "absent"
        $odometryPublisherPresent = $false
    }
    elseif ($odometryTopicInfoText -match "(?im)^\s*Publishers(?:\s*\[[^\]]+\])?\s*:") {
        $odometryPublisherStatus = "present"
        $odometryPublisherPresent = $true
    }
    else {
        throw "Required Gazebo odometry topic info did not identify publisher state."
    }
    Assert-True ($odometryPublisherPresent -eq $true) "Required Gazebo full-odometry publisher is absent."

    $routerArguments = @(
        "-u", "-m", "multidetect.px4_gazebo_qgc_router",
        "--qgc-port", "$QgcPx4Port", "--qgc-router-port", "$QgcRouterPort",
        "--evaluator-port", "$EvaluatorPort", "--evaluator-router-port", "$EvaluatorRouterPort",
        "--px4-telemetry-port", "$Px4TelemetryPort", "--px4-input-port", "$Px4InputHostPort",
        "--evaluator-system-id", "$EvaluatorSystemId", "--evaluator-component-id", "$EvaluatorComponentId",
        "--duration-seconds", "$RouterDurationSeconds", "--acknowledge-owned-disposable-sitl"
    )
    $routerProcess = Start-Process -FilePath $Python -ArgumentList $routerArguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $RouterStdout -RedirectStandardError $RouterStderr
    $null = $routerProcess.Handle
    Start-Sleep -Milliseconds 500
    Assert-True (-not $routerProcess.HasExited) "Combined PX4/QGC/evaluator router exited during startup."

    $integratedArguments = @(
        "-u", "-m", "multidetect.cli", "integrated-sim-qgc",
        $MissionConfig, $ScenarioManifest,
        # Start-Process flattens arguments into a Windows command line. Keep
        # the absolute Docker Desktop path as one quoted argv value; argparse
        # receives the unquoted path and the backend binds the exact binary.
        "--docker-executable", $DockerChildArgument, "--container-name", $ContainerName,
        "--container-image-sha256", $ImageDigest,
        "--stream-script-sha256", $streamScriptHash,
        "--model-assets-sha256", $modelAssetsHash,
        "--airframe-profile-sha256", $airframeProfileHash,
        "--terrain-dem", $TerrainDem,
        "--stream-script", "/workspace/scripts/gazebo_sensor_stream.py",
        "--model-instance", "standard_vtol_mono_cam_0", "--gazebo-world", "default",
        "--frame-count", "$Frames", "--stream-timeout-seconds", "$SensorStreamTimeoutSeconds",
        "--frame-width", "1280", "--frame-height", "960",
        "--rtsp-helper", $RtspHelper, "--rtsp-bind-host", "127.0.0.1",
        "--rtsp-port", "$RtspPort", "--rtsp-mount", "/multidetect-sim",
        "--helper-ready-timeout-seconds", "15", "--qgc-client-timeout-seconds", "60",
        "--frame-ack-timeout-seconds", "3",
        "--metadata-acceptance-ack-timeout-seconds", "3",
        "--helper-exit-timeout-seconds", "10",
        "--maximum-ack-to-metadata-skew-ms", "100",
        "--qgc-heartbeat-ready-timeout-seconds", "30",
        "--operator-udp-bind-host", "127.0.0.1", "--operator-udp-port", "$OperatorServerPort",
        "--operator-hmac-key-env", "MULTIDETECT_OPERATOR_KEY",
        "--mavlink-signing-key-hex-env", "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX",
        "--operator-stream-id", "camera-main", "--operator-id", "sim-qgc-operator",
        "--audit-out", $IntegratedAudit, "--result-out", $IntegratedResult,
        "--perception-args", "--onnx-model", $OnnxModel, "--model-manifest", $ModelManifest,
        "--class-names", "fire,smoke", "--input-width", "$PerceptionInputWidth", "--input-height", "$PerceptionInputHeight",
        "--output-coordinates", "normalized_xyxy", "--allow-synthetic-hil-model",
        "--warmup-iterations", "0", "--candidate-stability-frames", "1"
    )
    $qgcArguments = @(
        "--system-id", "255", "--isolated-hil", "--isolated-hil-force-cpu-video",
        "--isolated-hil-px4-udp-port", "$QgcPx4Port",
        "--isolated-hil-px4-target-port", "$QgcRouterPort",
        "--allow-multiple", "--log-output",
        "--logging", ($QgcNativeVideoLoggingCategories -join ",")
    )
    # Bind both the signed operator UDP server and strict RTSP helper before
    # QGC starts. Starting QGC while its configured UDP peer is still absent
    # causes Windows to deliver ICMP port-unreachable to the connected QGC
    # UDPLink; the link can then keep transmitting heartbeats while no longer
    # accepting the returned signed metadata. The sensor source is already
    # live at this point, so QGC connects once and receives frames immediately
    # on its original FlyView/VideoManager surface.
    $integratedProcess = Start-Process -FilePath $Python -ArgumentList $integratedArguments -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput $IntegratedStdout -RedirectStandardError $IntegratedStderr
    $null = $integratedProcess.Handle
    $null = Wait-TextPattern -Paths @($IntegratedStdout) -Pattern '"event":\s*"integrated_sim_qgc_started"' -TimeoutSeconds 45 -Processes @($integratedProcess, $routerProcess)
    $integratedStartEvidence = Get-JsonEventFromFile -Path $IntegratedStdout -Event "integrated_sim_qgc_started"
    Assert-True ($integratedStartEvidence.runtime_mode -eq "sim") "Integrated start event is not RuntimeMode.SIM."
    Assert-True ($integratedStartEvidence.ground_truth_isolated -eq $true) "Integrated start event did not preserve Ground Truth isolation."
    Assert-True ($integratedStartEvidence.hardware_outputs_present -eq $false -and $integratedStartEvidence.real_pixhawk_contacted -eq $false) "Integrated start event contains a hardware-output claim."
    Assert-True ([string]$integratedStartEvidence.runtime_descriptor_hash -match '^[0-9a-fA-F]{64}$') "Integrated start event has no valid runtime descriptor hash."
    Assert-True ([string]$integratedStartEvidence.run_identity_hash -match '^[0-9a-fA-F]{64}$') "Integrated start event has no valid runtime provenance hash."

    # This snapshot may contain a previous isolated-HIL run. It is retained as
    # context only: QGC itself clears this namespace on every isolated-HIL boot,
    # then applies the command-line A/B before VideoSettings is constructed.
    $qgcVideoSettingsBeforeLaunch = Get-QgcVideoSettingsSnapshot -Path $QgcIsolatedSettings
    $qgcProcess = Start-Process -FilePath $QgcExecutable -ArgumentList $qgcArguments -WorkingDirectory $QgcPortableBin -PassThru -WindowStyle Normal -RedirectStandardOutput $QgcStdout -RedirectStandardError $QgcStderr
    $null = $qgcProcess.Handle
    $qgcStartTimeUtc = $qgcProcess.StartTime.ToUniversalTime()
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern 'isolated HIL CPU-video A/B enabled: Video/forceCpuVideoPath=true; forceVideoDecoder=default' -TimeoutSeconds 45 -Processes @($qgcProcess, $integratedProcess, $routerProcess)
    $qgcVideoSettingsAfterLaunch = Wait-QgcCpuVideoSettings -Path $QgcIsolatedSettings -TimeoutSeconds 15 -Processes @($qgcProcess, $integratedProcess, $routerProcess)
    Assert-True ([DateTime]::Parse($qgcVideoSettingsAfterLaunch.last_write_time_utc).ToUniversalTime() -ge $qgcStartTimeUtc) "The isolated-HIL CPU-video setting predates this QGC process."
    Assert-True ([string]$qgcVideoSettingsAfterLaunch.sha256 -match '^[0-9A-Fa-f]{64}$') "The isolated-HIL settings snapshot has no SHA-256 identity."
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern "direct signed operator metadata ready" -TimeoutSeconds 45 -Processes @($qgcProcess, $integratedProcess, $routerProcess)
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern "isolated HIL PX4 UDP link listening on port $QgcPx4Port" -TimeoutSeconds 15 -Processes @($qgcProcess, $integratedProcess, $routerProcess)
    $qgcUdpPortsDuring = @(Get-NetUDPEndpoint -OwningProcess $qgcProcess.Id -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object)
    Assert-True ($qgcUdpPortsDuring -contains $OperatorQgcLocalPort) "QGC does not own the signed operator UDP listener."
    Assert-True ($qgcUdpPortsDuring -contains $QgcPx4Port) "QGC does not own the isolated PX4 UDP listener."
    Assert-True ($qgcUdpPortsDuring -notcontains $ProtectedGroundStationPort) "QGC opened protected UDP 14550."
    Assert-True ((@(Get-UdpPortOwners -Port $ProtectedGroundStationPort) -join ',') -eq ($protectedOwnersBefore -join ',')) "Protected UDP 14550 ownership changed during QGC startup."

    # Keep the native QGC vehicle and video lifecycle as the observable gate.
    # There is no simulator-only window or alternate visual surface here.
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern "new vehicle Vehicle" -TimeoutSeconds 45 -Processes @($qgcProcess, $routerProcess)

    # The production perception factory can take several seconds before the
    # bridge starts its signed-heartbeat lease. Keep this wait beyond the
    # bridge's own 30-second peer-ready timeout so a real child error is
    # preserved instead of the wrapper killing it first.
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern "runtime status adopted and acknowledged sequence=" -TimeoutSeconds 45 -Processes @($integratedProcess, $qgcProcess, $routerProcess)
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern 'Video "videoContent" streaming changed, active: yes' -TimeoutSeconds 20 -Processes @($integratedProcess, $qgcProcess, $routerProcess)
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern 'Video "videoContent" resized\. New resolution: 1280 x 960' -TimeoutSeconds 20 -Processes @($integratedProcess, $qgcProcess, $routerProcess)
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern 'Video "videoContent" decoding changed, active: yes' -TimeoutSeconds 20 -Processes @($integratedProcess, $qgcProcess, $routerProcess)

    $initialCapture = Save-QgcWindowCapture -Process $qgcProcess -Path $InitialScreenshot
    # Exercise the custom status through QGC's native toolbar/drawer contract.
    # The QGC toolbar Item exports its actual logical scene rectangle only for
    # this isolated-HIL run. Map that rectangle through the exact QGC HWND's
    # physical client area, then use owner-checked SendInput. This remains
    # fail-closed without guessed coordinates or a second visual surface.
    $toolbarEvidence = Invoke-QgcNativeToolbarIndicatorSmoke `
        -Process $qgcProcess `
        -ClosedCapture $ToolbarClosedScreenshot `
        -OpenCapture $ToolbarOpenScreenshot `
        -ObservationPath $ToolbarObservationPath `
        -ResultPath $ToolbarEvidencePath `
        -Python $Python `
        -Validator $ToolbarLayoutValidator `
        -QgcLogPaths @($QgcStdout, $QgcStderr)
    $pipSettingsBeforePipSwap = Get-QgcPipSettingsSnapshot -Path $QgcIsolatedSettings
    Assert-True ($pipSettingsBeforePipSwap.path_exists -and $pipSettingsBeforePipSwap.qgc_qml_section_exists) "Native QGC isolated QGCQml settings are unavailable before PiP swap."
    Assert-True ($pipSettingsBeforePipSwap.is_pip_visible_explicit -eq $true) "Native QGC PiP is not explicitly visible before swap."
    Assert-True ([DateTime]::Parse($pipSettingsBeforePipSwap.last_write_time_utc).ToUniversalTime() -ge $qgcProcess.StartTime.ToUniversalTime()) "Native QGC isolated settings are stale before PiP swap."
    # PipView.qml defaults MainFlyWindowIsMap to true when the key is absent.
    $mainFlyWindowIsMapBeforePipSwap = if ($null -eq $pipSettingsBeforePipSwap.main_fly_window_is_map_explicit) { $true } else { $pipSettingsBeforePipSwap.main_fly_window_is_map_explicit }
    Assert-True ($mainFlyWindowIsMapBeforePipSwap -eq $true) "Native QGC did not start with the map as the full Fly View item."
    $pipSwap = Invoke-QgcNativePipSwap -Process $qgcProcess
    $pipSettingsAfterPipSwap = Wait-QgcMainFlyWindowIsMapSetting -Path $QgcIsolatedSettings -Expected $false -TimeoutSeconds 10 -Processes @($integratedProcess, $qgcProcess, $routerProcess)
    $mainFlyWindowIsMapAfterPipSwap = $pipSettingsAfterPipSwap.main_fly_window_is_map_explicit
    Assert-True ($pipSettingsAfterPipSwap.sha256 -ne $pipSettingsBeforePipSwap.sha256) "Native QGC PiP state changed without an isolated-settings hash transition."
    Assert-True ([DateTime]::Parse($pipSettingsAfterPipSwap.last_write_time_utc).ToUniversalTime() -ge [DateTime]::Parse($pipSwap.click_sent_at_utc).ToUniversalTime()) "Native QGC PiP state timestamp predates the pointer click."
    $pipSwap.map_full_before = $mainFlyWindowIsMapBeforePipSwap
    $pipSwap.map_full_after = $mainFlyWindowIsMapAfterPipSwap
    $pipSwap.state_evidence = "QSettings QGCQml/MainFlyWindowIsMap true(default)->false"
    $pipSwap.settings_before = $pipSettingsBeforePipSwap
    $pipSwap.settings_after = $pipSettingsAfterPipSwap
    Wait-FileCreated -Path $NativeVideoPipSwappedCapture -TimeoutSeconds 15 -Processes @($integratedProcess, $qgcProcess, $routerProcess)
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern 'MULTIDETECT_HIL_VIDEO_GRAB .*"role":"pip-swapped"' -TimeoutSeconds 5 -Processes @($integratedProcess, $qgcProcess)
    $pipSwappedCapture = Save-QgcWindowCapture -Process $qgcProcess -Path $PipSwappedScreenshot
    $pipSwappedSequenceAfterCapture = Get-LatestQgcRuntimeStatusSequence -Paths @($QgcStdout, $QgcStderr)
    Assert-True ($pipSwappedSequenceAfterCapture -gt 0) "QGC emitted no type-24 sequence before the first live-video capture."
    $pipSwappedCapture["post_adoption_type24_sequence_after_capture"] = $pipSwappedSequenceAfterCapture
    $postAdoptionAckCountBeforeFixedWing = Get-TextPatternCount -Paths @($QgcStdout, $QgcStderr) -Pattern "runtime status adopted and acknowledged sequence="

    $vtolArguments = @(
        "-u", $AttachedVtolScript, "--scenario-manifest", $ScenarioManifest,
        "--evidence", $VtolResult, "--container-name", $ContainerName,
        "--evaluator-port", "$EvaluatorPort", "--expected-container-label", $PurposeLabel,
        "--transition-timeout-seconds", "15", "--fixed-wing-hold-seconds", "$FixedWingHoldSeconds",
        "--multicopter-hold-seconds", "1", "--fixed-wing-ready-file", $FixedWingReady,
        "--source-system", "$EvaluatorSystemId", "--source-component", "$EvaluatorComponentId"
    )
    $vtolProcess = Start-Process -FilePath $Python -ArgumentList $vtolArguments -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput $VtolStdout -RedirectStandardError $VtolStderr
    $null = $vtolProcess.Handle
    # The integrated sensor/AI producer is a bounded finite-frame process.  It
    # is expected to exit cleanly once --frame-count has been acknowledged, and
    # that can happen while the attached PX4 VTOL evaluator is still driving its
    # ARM -> FW transition.  Once the first PiP capture has passed, an ordinary
    # integrated exit is therefore not a stage failure; only the process that
    # owns the awaited artifact (VTOL), QGC, or the long-lived router may abort
    # this barrier.  Keeping the completed producer in these process lists
    # created a false failure with exit_code=0 and discarded the real evaluator
    # evidence before its fixed-wing-ready file could be written.
    Wait-FileCreated -Path $FixedWingReady -TimeoutSeconds $FixedWingReadyTimeoutSeconds -Processes @($vtolProcess, $qgcProcess, $routerProcess)
    $postAdoptionAckCountAtFixedWingReady = Wait-TextPatternCountGreaterThan -Paths @($QgcStdout, $QgcStderr) -Pattern "runtime status adopted and acknowledged sequence=" -BaselineCount $postAdoptionAckCountBeforeFixedWing -TimeoutSeconds 5 -Processes @($qgcProcess)
    $null = Wait-QgcRuntimeStatusSequence -Paths @($QgcStdout, $QgcStderr) -MinimumSequence ($pipSwappedSequenceAfterCapture + 5) -TimeoutSeconds 10 -Processes @($qgcProcess)
    # The VTOL evaluator owns fixed-wing-ready.json, but it does not create
    # QGC's native fixed-wing VideoOutput capture or its corresponding log
    # event.  Its clean exit is therefore terminal success for that producer,
    # not a failure of the still-running QGC capture stage.  Keep only the
    # processes that can actually produce (or invalidate) this artifact in
    # the wait barrier so an exit_code=0 from VTOL cannot discard evidence.
    Wait-FileCreated -Path $NativeVideoFixedWingCapture -TimeoutSeconds 15 -Processes @($qgcProcess, $routerProcess)
    $null = Wait-TextPattern -Paths @($QgcStdout, $QgcStderr) -Pattern 'MULTIDETECT_HIL_VIDEO_GRAB .*"role":"fixed-wing"' -TimeoutSeconds 5 -Processes @($qgcProcess)
    $fixedWingCapture = Save-QgcWindowCapture -Process $qgcProcess -Path $FixedWingScreenshot
    $fixedWingSequenceAfterCapture = Get-LatestQgcRuntimeStatusSequence -Paths @($QgcStdout, $QgcStderr)
    Assert-True ($fixedWingSequenceAfterCapture -ge ($pipSwappedSequenceAfterCapture + 5)) "QGC type-24 sequence did not advance five frames across live-video captures."
    $fixedWingCapture["post_adoption_type24_sequence_after_capture"] = $fixedWingSequenceAfterCapture

    # Validate the pixels of QGC's one existing native VideoOutput directly.
    # The acceptance hook is non-visual, fail-closed behind --isolated-hil plus
    # explicit environment opt-in, and does not create a second UI surface.
    $nativeVideoValidation = Invoke-CapturedCommand -FilePath $Python -ArgumentList @(
        "-u", $NativeVideoGrabValidator,
        "--capture-a", $NativeVideoPipSwappedCapture,
        "--capture-b", $NativeVideoFixedWingCapture,
        "--qgc-log", $QgcStdout,
        "--qgc-log", $QgcStderr,
        "--qgc-started-at-utc", "$($qgcStartTimeUtc.ToString('o'))",
        "--sequence-a", "$pipSwappedSequenceAfterCapture",
        "--sequence-b", "$fixedWingSequenceAfterCapture",
        "--minimum-sequence-advance", "5",
        "--expected-decoded-width", "1280",
        "--expected-decoded-height", "960",
        "--result-out", $NativeVideoGrabEvidencePath
    ) -TimeoutSeconds 30
    Assert-True ($nativeVideoValidation.ExitCode -eq 0) "Native QGC VideoOutput grab validator failed: $($nativeVideoValidation.Lines -join ' ')"
    $nativeVideoGrabEvidence = Get-Content -LiteralPath $NativeVideoGrabEvidencePath -Raw | ConvertFrom-Json
    Assert-True ($nativeVideoGrabEvidence.gate_passed -eq $true) "Native QGC VideoOutput pixel gate did not pass."
    Assert-True ($nativeVideoGrabEvidence.native_qquickitem_video_pixels_proved -eq $true) "QGC's native videoContent item did not produce non-black, non-uniform pixels."
    Assert-True ($nativeVideoGrabEvidence.transport_bound_native_qquickitem_video_surface_proved -eq $true) "Native VideoOutput pixels were not bound to type-24 sequence advancement."
    Assert-True ($nativeVideoGrabEvidence.native_qgc_ui_contract.second_video_output_created -eq $false -and $nativeVideoGrabEvidence.native_qgc_ui_contract.new_window_created -eq $false) "Native VideoOutput proof improperly created an independent UI surface."
    Assert-True ($nativeVideoGrabEvidence.complete_qml_rendering_semantics_proved -eq $false) "Native VideoOutput proof improperly claimed complete QML rendering semantics."
    Assert-True ($nativeVideoGrabEvidence.operator_visual_acceptance -eq "not_claimed") "Native VideoOutput proof improperly claimed operator visual acceptance."

    # Keep the exact-client BitBlt capture as a separate compositor diagnostic.
    # A black GDI capture cannot override a successful native QQuickItem grab;
    # it means the GPU/compositor surface remains unproved through BitBlt.
    $liveVideoValidation = Invoke-CapturedCommand -FilePath $Python -ArgumentList @(
        "-u", $DesktopCaptureValidator,
        "--capture-a", $PipSwappedScreenshot, "--capture-b", $FixedWingScreenshot,
        "--sequence-a", "$pipSwappedSequenceAfterCapture",
        "--sequence-b", "$fixedWingSequenceAfterCapture",
        "--capture-a-at-utc", "$($pipSwappedCapture.captured_at_utc)",
        "--capture-b-at-utc", "$($fixedWingCapture.captured_at_utc)",
        "--minimum-sequence-advance", "5",
        "--rtsp-streaming-proved", "--rtsp-decoding-proved",
        "--rtsp-width", "1280", "--rtsp-height", "960",
        "--result-out", $LiveVideoEvidencePath
    ) -TimeoutSeconds 30
    if (Test-Path -LiteralPath $LiveVideoEvidencePath -PathType Leaf) {
        $liveVideoSurfaceEvidence = Get-Content -LiteralPath $LiveVideoEvidencePath -Raw | ConvertFrom-Json
        Assert-True ($liveVideoSurfaceEvidence.exact_source_frame_identity_proved -eq $false) "Desktop capture improperly claimed exact source-frame identity."
        Assert-True ($liveVideoSurfaceEvidence.qml_rendering_proved -eq $false) "Desktop capture improperly claimed complete QML rendering semantics."
        Assert-True ($liveVideoSurfaceEvidence.operator_visual_acceptance -eq "not_claimed") "Desktop capture improperly claimed operator visual acceptance."
    }

    $vtolExit = Wait-ProcessCompleted -Process $vtolProcess -TimeoutMilliseconds $VtolProcessWaitTimeoutMilliseconds -Name "attached VTOL evaluator"
    Assert-True ($vtolExit -eq 0) "Attached VTOL evaluator failed; stderr=$(Get-TextIfPresent -Path $VtolStderr)"

    # The previous attached transition helper has now closed the exact evaluator
    # return port.  Reuse the same owned container, router, PX4 vehicle and native
    # QGC Fly View for the complete hash-bound PX4 Mission transaction.  This is
    # a test-only evaluator adapter; the production perception runtime retains no
    # flight-control capability or MAVLink writer.
    $missionArguments = @(
        "-u", $MissionAcceptanceScript,
        "--scenario-manifest", $ScenarioManifest,
        "--evidence", $MissionResult,
        "--container-name", $ContainerName,
        "--expected-container-id", $containerId,
        "--expected-container-label", $PurposeLabel,
        "--evaluator-port", "$EvaluatorPort",
        "--airframe-profile-sha256", $airframeProfileHash,
        "--runtime-descriptor-sha256", ([string]$integratedStartEvidence.runtime_descriptor_hash),
        "--runtime-provenance-sha256", ([string]$integratedStartEvidence.run_identity_hash),
        "--mission-journal", $MissionJournal,
        "--mission-active-ready-file", $MissionActiveReady,
        "--mission-timeout-seconds", "$MissionTimeoutSeconds",
        "--source-system", "$EvaluatorSystemId",
        "--source-component", "$EvaluatorComponentId",
        "--acknowledge-owned-disposable-sitl"
    )
    $missionProcess = Start-Process -FilePath $Python -ArgumentList $missionArguments -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput $MissionStdout -RedirectStandardError $MissionStderr
    $null = $missionProcess.Handle
    try {
        # The finite integrated producer may already have completed all sensor
        # frames by the time the Mission helper reaches START_PERMITTED.  Its
        # clean exit is expected and must not abort the Mission-ready barrier.
        Wait-FileCreated -Path $MissionActiveReady -TimeoutSeconds $MissionReadyTimeoutSeconds -Processes @($missionProcess, $qgcProcess, $routerProcess)
        $missionActiveReadyEvidence = Get-Content -LiteralPath $MissionActiveReady -Raw | ConvertFrom-Json
        Assert-True ($missionActiveReadyEvidence.operator_visual_acceptance -eq "not_claimed") "Mission-ready fixture improperly claimed operator visual acceptance."
        Assert-True ($missionActiveReadyEvidence.transaction_state -eq "start_permitted") "Mission-ready barrier was written before the transaction reached START_PERMITTED."
        Assert-True ($missionActiveReadyEvidence.auto_mission_confirmed -eq $true -and $missionActiveReadyEvidence.auto_mission_active -eq $true) "Mission-ready barrier did not confirm the current exact PX4 AUTO.MISSION mode."
        Assert-True ($missionActiveReadyEvidence.armed_after_permit -eq $true) "Mission-ready barrier did not prove post-permit ARM."
        Assert-True ($missionActiveReadyEvidence.in_air_observed -eq $true -and $missionActiveReadyEvidence.in_air_at_ready -eq $true) "Mission-ready barrier was not written during a current PX4 in-air state."
        Assert-True ($missionActiveReadyEvidence.mission_progress.observed -eq $true) "Mission-ready barrier contains no observed Mission progress."
        Assert-True ([int]$missionActiveReadyEvidence.mission_activity.sequence -ge 0) "Mission-ready barrier contains no bounded Mission activity sequence."

        # Establish a fresh QGC/native type-24 state baseline only after the
        # Mission helper's in-air ready barrier. Never reuse the older
        # fixed-wing capture threshold: the screenshot must follow N newly
        # adopted/acknowledged states from the actual Mission-ready instant.
        $missionReadyQgcSequenceBaseline = Get-LatestQgcRuntimeStatusSequence -Paths @($QgcStdout, $QgcStderr)
        Assert-True ($missionReadyQgcSequenceBaseline -gt 0) "QGC emitted no type-24 sequence at the Mission-ready barrier."
        $missionCaptureQgcSequenceTarget = $missionReadyQgcSequenceBaseline + $MissionCaptureSequenceAdvance
        $null = Wait-QgcRuntimeStatusSequence -Paths @($QgcStdout, $QgcStderr) -MinimumSequence $missionCaptureQgcSequenceTarget -TimeoutSeconds 30 -Processes @($qgcProcess)
        $missionActiveCapture = Save-QgcWindowCapture -Process $qgcProcess -Path $MissionActiveScreenshot
        $missionActiveSequenceAfterCapture = Get-LatestQgcRuntimeStatusSequence -Paths @($QgcStdout, $QgcStderr)
        Assert-True ($missionActiveSequenceAfterCapture -ge $missionCaptureQgcSequenceTarget) "QGC type-24 sequence did not advance by the required Mission-ready frame count before AUTO.MISSION capture."
        $missionActiveCapture["post_adoption_type24_sequence_at_mission_ready"] = $missionReadyQgcSequenceBaseline
        $missionActiveCapture["required_post_ready_sequence_advance"] = $MissionCaptureSequenceAdvance
        $missionActiveCapture["required_post_ready_sequence_target"] = $missionCaptureQgcSequenceTarget
        $missionActiveCapture["post_adoption_type24_sequence_after_capture"] = $missionActiveSequenceAfterCapture

        $missionExitCode = Wait-ProcessCompleted -Process $missionProcess -TimeoutMilliseconds $MissionProcessWaitTimeoutMilliseconds -Name "VTOL Mission evaluator"
    }
    catch {
        $missionStageFailure = $_.Exception.Message
    }
    finally {
        # The helper writes its durable result even on fail-closed exits. Always
        # consume it and both redirected streams before propagating any ready,
        # sequence, timeout, parse, or non-zero-exit failure to the outer gate.
        if ($null -ne $missionProcess -and $missionProcess.HasExited) {
            try {
                $missionProcess.WaitForExit()
                $missionProcess.Refresh()
                $missionExitCode = $missionProcess.ExitCode
            }
            catch { }
        }
        # Give PowerShell's redirected stdout/stderr handles a bounded grace
        # period to close before diagnostics hash/read the files.  This is
        # intentionally best-effort: the helper process result still drives
        # the gate, while diagnostics must never replace the real stage error.
        foreach ($diagnosticPath in @($MissionResult, $MissionJournal, $MissionStdout, $MissionStderr)) {
            $null = Wait-FileReadable -Path $diagnosticPath
        }
        if (Test-Path -LiteralPath $MissionResult -PathType Leaf) {
            try {
                $missionEvidence = Get-Content -LiteralPath $MissionResult -Raw | ConvertFrom-Json
                $missionJournalEvidence = $missionEvidence.mission_journal
            }
            catch {
                $missionResultParseFailure = $_.Exception.Message
            }
        }
        $missionDiagnostics = [ordered]@{
            schema = "multidetect.combined-sim-mission-diagnostics"
            schema_version = 1
            captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
            helper_timeout_seconds = $MissionTimeoutSeconds
            setup_allowance_seconds = $MissionSetupAllowanceSeconds
            ready_wait_timeout_seconds = $MissionReadyTimeoutSeconds
            process_wait_timeout_milliseconds = $MissionProcessWaitTimeoutMilliseconds
            required_post_ready_sequence_advance = $MissionCaptureSequenceAdvance
            qgc_type24_sequence_at_ready = $missionReadyQgcSequenceBaseline
            qgc_type24_sequence_target = $missionCaptureQgcSequenceTarget
            qgc_type24_sequence_after_capture = $missionActiveSequenceAfterCapture
            ready_file_present = (Test-Path -LiteralPath $MissionActiveReady -PathType Leaf)
            result_file_present = (Test-Path -LiteralPath $MissionResult -PathType Leaf)
            result_loaded = ($null -ne $missionEvidence)
            result_gate_passed = if ($null -ne $missionEvidence) { $missionEvidence.gate_passed } else { $null }
            result_failure = if ($null -ne $missionEvidence) { $missionEvidence.failure } else { $null }
            result_parse_failure = $missionResultParseFailure
            helper_exit_code = $missionExitCode
            stage_failure = $missionStageFailure
            journal_path = $MissionJournal
            journal = Get-FileDiagnosticRecord -Path $MissionJournal
            journal_loaded = ($null -ne $missionJournalEvidence)
            journal_state = if ($null -ne $missionJournalEvidence) { $missionJournalEvidence.state } else { $null }
            journal_recovery_required = if ($null -ne $missionJournalEvidence) { $missionJournalEvidence.recovery_required } else { $null }
            journal_record_count = if ($null -ne $missionJournalEvidence) { $missionJournalEvidence.record_count } else { $null }
            result = Get-FileDiagnosticRecord -Path $MissionResult
            stdout = Get-FileDiagnosticRecord -Path $MissionStdout
            stderr = Get-FileDiagnosticRecord -Path $MissionStderr
        }
    }
    if ($null -ne $missionResultParseFailure -and $null -eq $missionStageFailure) {
        $missionStageFailure = "VTOL Mission result JSON could not be parsed: $missionResultParseFailure"
    }
    if ($null -eq $missionStageFailure -and $missionExitCode -ne 0) {
        $missionStageFailure = "VTOL Mission evaluator failed with exit code $missionExitCode; stderr=$(Get-TextIfPresent -Path $MissionStderr)"
    }
    if ($null -eq $missionStageFailure -and $null -eq $missionEvidence) {
        $missionStageFailure = "VTOL Mission evaluator produced no readable result evidence."
    }
    if ($null -ne $missionDiagnostics) {
        $missionDiagnostics.stage_failure = $missionStageFailure
    }
    if ($null -ne $missionStageFailure) { throw $missionStageFailure }
    Assert-True ($missionEvidence.gate_passed -eq $true) "Complete VTOL Mission gate did not pass."
    Assert-True ($missionEvidence.schema_version -eq 1) "Complete VTOL Mission evidence schema is not version one."
    Assert-True ($missionEvidence.transaction.final_state -eq "start_permitted") "Mission transaction did not finish at START_PERMITTED."
    Assert-True ($null -eq $missionEvidence.transaction.denial_reason) "Mission transaction contains a denial reason after a claimed pass."
    $missionTransactionStates = @($missionEvidence.transaction.state_events | ForEach-Object { [string]$_.state })
    Assert-True (($missionTransactionStates -join ',') -eq "authorized,uploading,uploaded,readback_verified,committed,start_permitted") "Mission transaction state ordering is incomplete or out of order."

    $missionItemCount = [int]$missionEvidence.protocol.mission_item_count
    Assert-True ($missionItemCount -ge 7 -and $missionItemCount -le 64) "Complete VTOL Mission item count is outside the bounded full-lifecycle range."
    Assert-True ($missionEvidence.protocol.upload_acknowledged -eq $true) "PX4 did not acknowledge the Mission upload."
    Assert-True ($missionEvidence.protocol.readback_hash_match -eq $true) "PX4 Mission readback hash does not match the uploaded wire plan."
    Assert-True ($missionEvidence.protocol.int_only -eq $true) "Mission transaction used a legacy non-INT request/item."
    Assert-True (
        $missionEvidence.protocol.uploaded_protocol_sha256 -eq $missionEvidence.protocol.readback_protocol_sha256 -and
        $missionEvidence.protocol.uploaded_protocol_sha256 -eq $missionEvidence.protocol.committed_protocol_sha256
    ) "Uploaded, read-back and committed Mission protocol hashes are not identical."
    Assert-True (@($missionEvidence.protocol.request_message_types | Where-Object { $_ -ne "MISSION_REQUEST_INT" }).Count -eq 0) "PX4 requested a legacy Mission item during upload."
    Assert-True (@($missionEvidence.protocol.readback_message_types | Where-Object { $_ -ne "MISSION_ITEM_INT" }).Count -eq 0) "PX4 returned a legacy Mission item during readback."
    $expectedMissionSequences = @(0..($missionItemCount - 1))
    $uploadMissionSequences = @($missionEvidence.protocol.request_sequences | ForEach-Object { [int]$_ } | Sort-Object -Unique)
    $readbackMissionSequences = @($missionEvidence.protocol.readback_sequences | ForEach-Object { [int]$_ } | Sort-Object -Unique)
    Assert-True (@(Compare-Object -ReferenceObject $expectedMissionSequences -DifferenceObject $uploadMissionSequences).Count -eq 0) "PX4 did not request every uploaded Mission sequence."
    Assert-True (@(Compare-Object -ReferenceObject $expectedMissionSequences -DifferenceObject $readbackMissionSequences).Count -eq 0) "PX4 Mission readback did not return every sequence."
    $missionJournalEvidence = $missionEvidence.mission_journal
    Assert-True ($null -ne $missionJournalEvidence) "Mission evidence is missing the durable transaction journal block."
    Assert-True (
        [IO.Path]::GetFullPath([string]$missionJournalEvidence.path) -eq
        [IO.Path]::GetFullPath($MissionJournal)
    ) "Mission journal path is not bound to the combined SIM run directory."
    Assert-True ($missionJournalEvidence.recovery_required -eq $false) "Mission journal requires recovery after a claimed pass."
    Assert-True ($missionJournalEvidence.state -eq "start_permitted") "Mission journal did not finish at START_PERMITTED."
    Assert-True ([string]$missionJournalEvidence.identity_hash -match '^[0-9a-fA-F]{64}$') "Mission journal identity hash is missing or malformed."
    Assert-True ([int]$missionJournalEvidence.record_count -ge ($missionItemCount + 6)) "Mission journal does not contain the header, lifecycle transitions and every requested item."
    $journalRequestedSequences = @(
        $missionJournalEvidence.requested_sequences |
        ForEach-Object { [int]$_ } | Sort-Object -Unique
    )
    Assert-True (
        @(Compare-Object -ReferenceObject $expectedMissionSequences -DifferenceObject $journalRequestedSequences).Count -eq 0
    ) "Mission journal does not contain every requested Mission item sequence."

    Assert-True ($missionEvidence.flight.auto_mission_confirmed -eq $true -and $missionEvidence.flight.armed_after_permit -eq $true) "Mission did not prove exact AUTO.MISSION followed by post-permit ARM."
    Assert-True ($missionEvidence.flight.in_air_observed -eq $true -and $missionEvidence.flight.fixed_wing_observed -eq $true -and $missionEvidence.flight.multicopter_observed -eq $true) "Mission did not prove the VTOL takeoff/FW/MC lifecycle."
    Assert-True ($missionEvidence.flight.landed_observed -eq $true -and $missionEvidence.flight.terminal_disarmed -eq $true) "Mission did not prove VTOL_LAND and terminal disarm."
    Assert-True ([double]$missionEvidence.flight.maximum_distance_from_home_m -ge 35.0) "Mission did not prove enough horizontal movement."
    Assert-True ([double]$missionEvidence.flight.maximum_relative_altitude_m -ge 10.0) "Mission did not prove enough relative altitude."
    Assert-True (@($missionEvidence.flight.mission_current_sequences | ForEach-Object { [int]$_ }) -contains ($missionItemCount - 1)) "PX4 never reported the terminal Mission item as current."
    Assert-True (@($missionEvidence.flight.mission_item_reached_sequences | ForEach-Object { [int]$_ }) -contains ($missionItemCount - 1)) "PX4 never reported the terminal VTOL_LAND item reached."
    $missionControlEvents = @($missionEvidence.mavlink.control_command_events)
    $missionCommandLongEvents = @($missionControlEvents | Where-Object { $null -ne $_.command_id })
    Assert-True (@($missionControlEvents | Where-Object { $_.message -eq "SET_MODE" -and $_.mode -eq "PX4_AUTO.MISSION" -and $_.confirmed -eq $true -and $_.after_permit -eq $true }).Count -eq 1) "Mission control evidence does not contain exactly one post-permit PX4 AUTO.MISSION SET_MODE."
    Assert-True (@($missionCommandLongEvents | Where-Object { $_.command_id -eq 400 -and $_.arm -eq $true -and $_.after_permit -eq $true }).Count -eq 1) "Mission control evidence does not contain exactly one post-permit ARM."
    Assert-True ($missionEvidence.safety.operator_visual_acceptance -eq "not_claimed" -and $missionEvidence.safety.qgc_fly_view_visual_acceptance -eq "not_claimed") "Mission helper improperly claimed human/QGC visual acceptance."
    Assert-True ($missionEvidence.safety.product_runtime_flight_control_enabled -eq $false -and $missionEvidence.safety.hardware_control_enabled -eq $false) "Mission evidence improperly enables a production/hardware control path."
    Assert-True ($missionEvidence.safety.real_pixhawk_contacted -eq $false -and $missionEvidence.safety.real_jetson_contacted -eq $false -and $missionEvidence.safety.real_camera_contacted -eq $false) "Mission evidence improperly claims real hardware contact."

    $integratedExit = Wait-ProcessCompleted -Process $integratedProcess -TimeoutMilliseconds $IntegratedWaitTimeoutMilliseconds -Name "integrated SIM QGC"
    Assert-True ($integratedExit -eq 0) "Integrated SIM QGC failed; stderr=$(Get-TextIfPresent -Path $IntegratedStderr)"
    $integratedEvidence = Get-Content -LiteralPath $IntegratedResult -Raw | ConvertFrom-Json
    $vtolEvidence = Get-Content -LiteralPath $VtolResult -Raw | ConvertFrom-Json
    $expectedRuntimeRunHash64Hex = ([string]$integratedEvidence.run_identity_hash).Substring(0, 16).ToLowerInvariant()
    Assert-True ($integratedEvidence.schema_version -eq 2) "Integrated QGC evidence is not canonical schema version 2."
    Assert-True ($integratedEvidence.event -eq "integrated_sim_qgc_finished") "Integrated result event is wrong."
    Assert-True ($integratedEvidence.runtime_mode -eq "sim") "Integrated runtime mode is not SIM."
    Assert-True ($integratedEvidence.qgc_transport_acceptance_gate -eq "passed_rtsp_frame_ack_and_runtime_status_post_adoption_ack") "Integrated result did not pass the canonical QGC transport acceptance gate."
    Assert-True ($integratedEvidence.operator_visual_acceptance -eq "not_claimed") "Integrated result improperly claimed human operator visual acceptance."
    Assert-True ($integratedEvidence.synchronization_protocol.runtime_status_message_type -eq 23 -and $integratedEvidence.synchronization_protocol.runtime_status_acceptance_ack_message_type -eq 24) "Integrated result does not identify the canonical type-23/type-24 protocol."
    Assert-True ($integratedEvidence.synchronization_protocol.type24_scope -eq "native_controller_post_adoption_only") "RuntimeStatusAck scope is not restricted to native controller adoption."
    Assert-True ($integratedEvidence.synchronization_protocol.runtime_mode_policy_acceptance_proved -eq $false -and $integratedEvidence.synchronization_protocol.qml_rendering_proved -eq $false) "Transport evidence improperly claims policy or QML rendering acceptance."
    Assert-True ($integratedEvidence.synchronization.complete -eq $true) "QGC synchronization barrier is incomplete."
    Assert-True ($integratedEvidence.synchronization.video_frame_count -eq $Frames) "QGC video ACK count is incomplete."
    Assert-True ($integratedEvidence.synchronization.metadata_send_count -eq $Frames) "Runtime-status type-23 send count is incomplete."
    Assert-True ($integratedEvidence.synchronization.metadata_acceptance_ack_count -eq $Frames) "QGC post-adoption type-24 ACK count is incomplete."
    Assert-True ($integratedEvidence.synchronization.metadata_barrier_count -eq $Frames) "Canonical QGC acceptance barrier count is incomplete."
    Assert-True ($integratedEvidence.synchronization.metadata_barrier_count -eq $integratedEvidence.synchronization.metadata_acceptance_ack_count) "Metadata barrier count is not bound to canonical QGC type-24 acceptance ACKs."
    Assert-True ($integratedEvidence.synchronization.metadata_barrier_count_semantics -eq "legacy_alias_of_canonical_post_adoption_type24_ack_count") "Metadata barrier alias semantics are missing or stale."
    Assert-True ($integratedEvidence.synchronization.unique_sent_runtime_sequence_count -eq $Frames) "Runtime-status type-23 send sequences are incomplete or duplicated."
    Assert-True ($integratedEvidence.synchronization.first_sent_runtime_sequence -eq 1) "First runtime-status type-23 send sequence is not one."
    Assert-True ($integratedEvidence.synchronization.last_sent_runtime_sequence -eq $Frames) "Last runtime-status type-23 send sequence does not match the frame count."
    Assert-True ($integratedEvidence.synchronization.sent_runtime_sequences_contiguous -eq $true) "Runtime-status type-23 send sequences are not contiguous."
    Assert-True ($integratedEvidence.synchronization.unique_acknowledged_runtime_sequence_count -eq $Frames) "QGC runtime-status ACK sequences are incomplete or duplicated."
    Assert-True ($integratedEvidence.synchronization.first_acknowledged_runtime_sequence -eq 1) "First QGC runtime-status ACK sequence is not one."
    Assert-True ($integratedEvidence.synchronization.last_acknowledged_runtime_sequence -eq $Frames) "Last QGC runtime-status ACK sequence does not match the frame count."
    Assert-True ($integratedEvidence.synchronization.acknowledged_runtime_sequences_contiguous -eq $true) "QGC runtime-status ACK sequences are not contiguous."
    Assert-True ($integratedEvidence.synchronization.acknowledged_runtime_run_hash64_hex -eq $expectedRuntimeRunHash64Hex) "QGC runtime-status ACK run hash is not bound to the integrated SIM identity."
    Assert-True ($integratedEvidence.synchronization.qgc_peer_host -eq "127.0.0.1" -and $integratedEvidence.synchronization.qgc_peer_port -eq $OperatorQgcLocalPort) "Ready heartbeat peer is not the configured loopback QGC endpoint."
    Assert-True ($integratedEvidence.synchronization.acceptance_ack_peer_host -eq "127.0.0.1" -and $integratedEvidence.synchronization.acceptance_ack_peer_port -eq $OperatorQgcLocalPort) "Runtime-status acceptance ACK peer is not the ready QGC endpoint."
    Assert-True ($integratedEvidence.synchronization.acceptance_ack_peer_host -eq $integratedEvidence.synchronization.qgc_peer_host -and $integratedEvidence.synchronization.acceptance_ack_peer_port -eq $integratedEvidence.synchronization.qgc_peer_port) "Runtime-status ACK peer differs from the ready heartbeat peer."
    Assert-True ($integratedEvidence.synchronization.maximum_metadata_acceptance_ack_latency_ms -le $integratedEvidence.synchronization.allowed_metadata_acceptance_ack_latency_ms) "QGC runtime-status acceptance ACK latency exceeded the configured limit."
    Assert-True ($integratedEvidence.synchronization.maximum_frame_to_acceptance_latency_ms -le ($integratedEvidence.synchronization.allowed_ack_to_metadata_skew_ms + $integratedEvidence.synchronization.allowed_metadata_acceptance_ack_latency_ms)) "Frame-to-QGC-acceptance latency exceeded the composed barrier budget."
    Assert-True ($integratedEvidence.sensor_transport.complete -eq $true) "Gazebo sensor transport is incomplete."
    Assert-True ($integratedEvidence.sensor_transport.odometry_required -eq $true) "Integrated SIM did not require Gazebo wire-v2 full odometry."
    Assert-True ($integratedEvidence.sensor_transport.full_odometry_pose_velocity_fusion_claimed -eq $true) "Integrated SIM did not validate full odometry pose/velocity fusion."
    Assert-True ($integratedEvidence.sensor_transport.odometry_timestamp_frame_count -eq $Frames) "Integrated SIM did not bind full odometry to every accepted frame."
    Assert-True ($integratedEvidence.terrain_dem_sha256 -eq $terrainDemHash) "Integrated SIM AGL is not bound to the scenario terrain DEM."
    Assert-True ($integratedEvidence.terrain_agl_reference -eq "GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL") "Integrated SIM AGL reference is not the DEM/MSL contract."
    Assert-True ($integratedEvidence.odometry_local_up_used_as_agl -eq $false) "Integrated SIM improperly treated Gazebo local-up as AGL."
    Assert-True ($integratedEvidence.rtsp_publisher.helper_exit_code -eq 0) "RTSP helper did not exit cleanly."
    Assert-True ($integratedEvidence.rtsp_publisher.eos_sent -eq $true -and $integratedEvidence.rtsp_publisher.summary_received -eq $true) "RTSP EOS/summary is incomplete."
    Assert-True ($integratedEvidence.result.processed_frames -eq $Frames) "Shared production core did not process all SIM frames."
    Assert-True ($vtolEvidence.gate_passed -eq $true) "Attached VTOL gate did not pass."
    Assert-True (@($vtolEvidence.mavlink.command_events).Count -eq 4) "Attached VTOL command count is not four."
    Assert-True ($vtolEvidence.position_quality.false_geolocations -eq 0) "VTOL position stream contains false geolocations."

    if (-not $qgcProcess.HasExited) {
        if ($qgcProcess.CloseMainWindow()) {
            $qgcCloseMethod = "CloseMainWindow"
            if (-not $qgcProcess.WaitForExit(15000)) {
                $qgcProcess.Kill(); $qgcProcess.WaitForExit(); $qgcCloseMethod = "exact_pid_kill_after_close_timeout"
            }
        }
        else { $qgcProcess.Kill(); $qgcProcess.WaitForExit(); $qgcCloseMethod = "exact_pid_kill_no_close" }
    }
    else { $qgcCloseMethod = "already_exited" }

    $routerExit = Wait-ProcessCompleted -Process $routerProcess -TimeoutMilliseconds (($RouterDurationSeconds + 10) * 1000) -Name "combined SIM router"
    Assert-True ($routerExit -eq 0) "Combined SIM router failed: $(Get-TextIfPresent -Path $RouterStderr)"
    $routerEvidence = Get-JsonEventFromFile -Path $RouterStdout -Event "px4_gazebo_qgc_router_finished"
    Assert-True ($routerEvidence.schema_version -eq 2) "Combined router evidence schema is not version two."
    Assert-True ($routerEvidence.px4_frames_forwarded_to_qgc -gt 0) "Router forwarded no PX4 telemetry to QGC."
    Assert-True ($routerEvidence.px4_frames_forwarded_to_evaluator -gt 0) "Router forwarded no PX4 telemetry to evaluator."
    $expectedEvaluatorCommandLongCount = 4 + $missionCommandLongEvents.Count
    Assert-True ($routerEvidence.evaluator_control_commands_forwarded -eq $expectedEvaluatorCommandLongCount) "Router evaluator COMMAND_LONG count does not match the attached and Mission helpers."
    Assert-True ($routerEvidence.evaluator_mission_frames_forwarded -ge (2 * $missionItemCount + 3)) "Router did not forward the complete Mission upload/readback/start transaction."
    Assert-True ($routerEvidence.evaluator_mission_items_forwarded -ge $missionItemCount) "Router did not forward every Mission item."
    Assert-True ($routerEvidence.evaluator_auto_mission_mode_changes_forwarded -eq 1) "Router did not forward exactly one restricted PX4 AUTO.MISSION mode change."
    $routerMissionSequences = @($routerEvidence.evaluator_forwarded_mission_item_sequences.PSObject.Properties.Name | ForEach-Object { [int]$_ } | Sort-Object -Unique)
    Assert-True (@(Compare-Object -ReferenceObject $expectedMissionSequences -DifferenceObject $routerMissionSequences).Count -eq 0) "Router evidence does not contain every Mission item sequence."
    $expectedMissionCommandIds = @($missionEvidence.protocol.wire_items | ForEach-Object { [int]$_.command } | Sort-Object -Unique)
    $routerMissionCommandIds = @($routerEvidence.evaluator_forwarded_mission_command_ids.PSObject.Properties.Name | ForEach-Object { [int]$_ } | Sort-Object -Unique)
    Assert-True (@(Compare-Object -ReferenceObject $expectedMissionCommandIds -DifferenceObject $routerMissionCommandIds).Count -eq 0) "Router evidence does not contain every compiled Mission command ID."
    Assert-True ($routerEvidence.evaluator_bounded_mission_transaction_allowed -eq $true) "Router did not identify the bounded evaluator Mission transaction."
    Assert-True ($routerEvidence.evaluator_mission_clear_all_allowed -eq $false -and $routerEvidence.evaluator_set_mode_restricted_to_px4_auto_mission -eq $true) "Router Mission clear/mode restrictions are stale."
    Assert-True ($routerEvidence.qgc_mission_writes_allowed -eq $false -and $routerEvidence.qgc_flight_commands_allowed -eq $false) "Router allowed a QGC Mission write or flight command."
    Assert-True ($routerEvidence.evaluator_forbidden_frames_blocked -eq 0) "Evaluator emitted a forbidden MAVLink frame."
    Assert-True ($routerEvidence.qgc_forbidden_frames_blocked -eq 0) "QGC emitted a forbidden PX4-bound MAVLink frame."
    Assert-True ($routerEvidence.px4_discarded_bytes -eq 0 -and $routerEvidence.qgc_discarded_bytes -eq 0 -and $routerEvidence.evaluator_discarded_bytes -eq 0) "Router discarded MAVLink bytes."
}
catch {
    $failure = $_.Exception.Message
}
finally {
    foreach ($process in @($integratedProcess, $vtolProcess, $missionProcess, $qgcProcess, $routerProcess)) {
        if ($null -ne $process -and -not $process.HasExited) {
            try { $process.Kill(); $process.WaitForExit() } catch { }
        }
    }
    if ($containerStarted) {
        # Keep Docker's noisy PX4/Gazebo terminal stream out of the PowerShell
        # object pipeline. The bounded tail is diagnostic only; all normative
        # router/mission evidence is captured in their dedicated JSONL files.
        $containerLogExitCode = Save-CommandOutputToFile -FilePath $Docker `
            -ArgumentList @("logs", "--tail", "256", $ContainerName) `
            -OutputPath $ContainerLog -ErrorPath $ContainerLogStderr -TimeoutSeconds 30
        if ($containerLogExitCode -ne 0 -and $null -eq $failure) {
            $failure = "Docker log capture failed with exit code $containerLogExitCode."
        }
        try { $containerStopped = Stop-OwnedContainer -Docker $Docker -Name $ContainerName -ExpectedId $containerId }
        catch { if ($null -eq $failure) { $failure = $_.Exception.Message } }
    }
}

$protectedOwnersAfter = @(Get-UdpPortOwners -Port $ProtectedGroundStationPort)
$protectedPortUnchanged = (($protectedOwnersBefore -join ',') -eq ($protectedOwnersAfter -join ','))
$portsReleased = $true
foreach ($port in $allUdpPorts) {
    if (@(Get-UdpPortOwners -Port $port).Count -ne 0) { $portsReleased = $false }
}
if (@(Get-TcpPortOwners -Port $RtspPort).Count -ne 0) { $portsReleased = $false }
$crashDumpsAfter = @(Get-CrashDumpSignatures)
$crashDumpsUnchanged = (($crashDumpsBefore -join "`n") -eq ($crashDumpsAfter -join "`n"))
Write-CombinedStageTrace -Message 'cleanup-and-resource-checks-complete'
if (-not $protectedPortUnchanged -and $null -eq $failure) { $failure = "Protected UDP 14550 ownership changed." }
if (-not $portsReleased -and $null -eq $failure) { $failure = "One or more combined-SIM ports remain occupied." }
if (-not $crashDumpsUnchanged -and $null -eq $failure) { $failure = "QGC created or changed a crash dump." }

$qgcLog = (Get-TextIfPresent -Path $QgcStdout) + "`n" + (Get-TextIfPresent -Path $QgcStderr)
Write-CombinedStageTrace -Message ("qgc-log-read;chars={0}" -f $qgcLog.Length)
$qgcCpuVideoAbLogLines = @(
    [regex]::Matches(
        $qgcLog,
        '(?m)^.*isolated HIL CPU-video A/B enabled: Video/forceCpuVideoPath=true; forceVideoDecoder=default.*$'
    ) | ForEach-Object { $_.Value.Trim() }
)
$qgcVideoSettingsAfterRunError = $null
try {
    $qgcVideoSettingsAfterRun = Get-QgcVideoSettingsSnapshot -Path $QgcIsolatedSettings
}
catch {
    $qgcVideoSettingsAfterRun = $null
    $qgcVideoSettingsAfterRunError = $_.Exception.Message
}
$qgcVideoSettingsAfterLaunchValid = (
    $null -ne $qgcVideoSettingsAfterLaunch -and
    $qgcVideoSettingsAfterLaunch.path_exists -eq $true -and
    $qgcVideoSettingsAfterLaunch.video_section_exists -eq $true -and
    $qgcVideoSettingsAfterLaunch.force_cpu_video_path_explicit -eq $true -and
    $qgcVideoSettingsAfterLaunch.force_cpu_video_path_enabled -eq $true -and
    $qgcVideoSettingsAfterLaunch.force_video_decoder_explicit -eq $false
)
$qgcVideoSettingsAfterRunValid = (
    $null -ne $qgcVideoSettingsAfterRun -and
    $qgcVideoSettingsAfterRun.path_exists -eq $true -and
    $qgcVideoSettingsAfterRun.video_section_exists -eq $true -and
    $qgcVideoSettingsAfterRun.force_cpu_video_path_explicit -eq $true -and
    $qgcVideoSettingsAfterRun.force_cpu_video_path_enabled -eq $true -and
    $qgcVideoSettingsAfterRun.force_video_decoder_explicit -eq $false
)
$qgcVideoSettingsTimestampBound = (
    $null -ne $qgcStartTimeUtc -and
    $qgcVideoSettingsAfterLaunchValid -and
    [DateTime]::Parse($qgcVideoSettingsAfterLaunch.last_write_time_utc).ToUniversalTime() -ge $qgcStartTimeUtc -and
    $qgcVideoSettingsAfterRunValid -and
    [DateTime]::Parse($qgcVideoSettingsAfterRun.last_write_time_utc).ToUniversalTime() -ge $qgcStartTimeUtc
)
$qgcCpuVideoAbProved = (
    $qgcCpuVideoAbLogLines.Count -ge 1 -and
    $qgcVideoSettingsAfterLaunchValid -and
    $qgcVideoSettingsAfterRunValid -and
    $qgcVideoSettingsTimestampBound
)
$qgcVideoBackendSettingsEvidence = [ordered]@{
    schema = "multidetect.qgc-video-backend-settings"
    schema_version = 1
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    mode = "isolated_hil_force_cpu_only"
    command_line_flag = "--isolated-hil-force-cpu-video"
    qgc_started_at_utc = if ($null -ne $qgcStartTimeUtc) { $qgcStartTimeUtc.ToString("o") } else { $null }
    explicit_native_log_count = $qgcCpuVideoAbLogLines.Count
    explicit_native_log = if ($qgcCpuVideoAbLogLines.Count -gt 0) { $qgcCpuVideoAbLogLines[-1] } else { $null }
    prelaunch_snapshot = $qgcVideoSettingsBeforeLaunch
    prelaunch_snapshot_is_runtime_authority = $false
    runtime_snapshot = $qgcVideoSettingsAfterLaunch
    post_run_snapshot = $qgcVideoSettingsAfterRun
    post_run_snapshot_error = $qgcVideoSettingsAfterRunError
    runtime_snapshot_timestamp_bound_to_qgc_process = $qgcVideoSettingsTimestampBound
    force_cpu_video_path_proved = $qgcCpuVideoAbProved
    force_video_decoder_override_present = (
        ($null -ne $qgcVideoSettingsAfterLaunch -and $qgcVideoSettingsAfterLaunch.force_video_decoder_explicit -eq $true) -or
        ($null -ne $qgcVideoSettingsAfterRun -and $qgcVideoSettingsAfterRun.force_video_decoder_explicit -eq $true)
    )
    single_variable_ab = "Video/forceCpuVideoPath=true; Video/forceVideoDecoder remains absent/default"
    native_ui_modified = $false
    operator_visual_acceptance = "not_claimed"
    evidence_semantics = "QGC-owned isolated settings and native startup log prove the CPU-memory backend A/B only; the prelaunch file is context, and this artifact does not prove visible pixels."
}
$qgcVideoBackendSettingsEvidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $QgcVideoBackendSettingsEvidencePath -Encoding utf8
if ($null -eq $failure -and -not $qgcCpuVideoAbProved) {
    $failure = "QGC did not preserve the timestamp-bound isolated-HIL forceCpuVideoPath=true setting without a forceVideoDecoder override."
}
$qgcRhiLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*Resolved RHI backend:.*zero-copy path:.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcDecoderSelectionLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*Decodebin3 selected codec:rank -.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcSinkCapsLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*set_caps: format=.*pixfmt=.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcSinkNegotiationLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*Negotiation update: format=.*size=.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcSinkFirstFrameLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*first frame delivered via\s+\S+\s+path\s+\d+x\d+.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcSinkFrameFlowLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*frame flow:.*delivered=.*input=.*dropped=.*cpuFrames=.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcSinkMappingFailureLogLines = @(
    [regex]::Matches($qgcLog, '(?m)^.*show_frame: mapping failed, consecutive=.*$') |
        ForEach-Object { $_.Value.Trim() }
)
$qgcRhiMatch = [regex]::Match(
    $qgcLog,
    'Resolved RHI backend:\s*(?<backend>.+?)\s+\S+\s+zero-copy path:\s*(?<family>.+?)\s+-\s+Video\.GStreamer\.GStreamer\s+-'
)
$qgcSinkFirstFrameMatch = [regex]::Match(
    $qgcLog,
    'first frame delivered via\s+(?<delivery_path>\S+)\s+path\s+(?<width>\d+)x(?<height>\d+)'
)
$qgcSinkCapsMatch = [regex]::Match(
    $qgcLog,
    'set_caps:\s*format=\s*(?<format>\S+)\s+(?<width>\d+)\s*x\s*(?<height>\d+)\s+pixfmt=\s*(?<pixfmt>\d+)'
)
$qgcNativeVideoDiagnostics = [ordered]@{
    schema = "multidetect.qgc-native-video-log-diagnostics"
    schema_version = 1
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    native_chain = "GStreamer qgcqvideosink -> QVideoSink -> FlightDisplayViewVideoOutput.qml VideoOutput"
    source_logs = [ordered]@{
        stdout = $QgcStdout
        stderr = $QgcStderr
    }
    logging_categories = @($QgcNativeVideoLoggingCategories)
    backend_ab = $qgcVideoBackendSettingsEvidence
    rhi = [ordered]@{
        log_count = $qgcRhiLogLines.Count
        resolved_backend = if ($qgcRhiMatch.Success) { $qgcRhiMatch.Groups['backend'].Value.Trim() } else { $null }
        zero_copy_family = if ($qgcRhiMatch.Success) { $qgcRhiMatch.Groups['family'].Value.Trim() } else { $null }
        last_log = if ($qgcRhiLogLines.Count -gt 0) { $qgcRhiLogLines[-1] } else { $null }
    }
    decoder = [ordered]@{
        selection_log_count = $qgcDecoderSelectionLogLines.Count
        last_selection_log = if ($qgcDecoderSelectionLogLines.Count -gt 0) { $qgcDecoderSelectionLogLines[-1] } else { $null }
    }
    native_sink = [ordered]@{
        caps_log_count = $qgcSinkCapsLogLines.Count
        negotiated_format = if ($qgcSinkCapsMatch.Success) { $qgcSinkCapsMatch.Groups['format'].Value } else { $null }
        negotiated_width = if ($qgcSinkCapsMatch.Success) { [int]$qgcSinkCapsMatch.Groups['width'].Value } else { $null }
        negotiated_height = if ($qgcSinkCapsMatch.Success) { [int]$qgcSinkCapsMatch.Groups['height'].Value } else { $null }
        qt_pixel_format = if ($qgcSinkCapsMatch.Success) { [int]$qgcSinkCapsMatch.Groups['pixfmt'].Value } else { $null }
        last_caps_log = if ($qgcSinkCapsLogLines.Count -gt 0) { $qgcSinkCapsLogLines[-1] } else { $null }
        negotiation_log_count = $qgcSinkNegotiationLogLines.Count
        last_negotiation_log = if ($qgcSinkNegotiationLogLines.Count -gt 0) { $qgcSinkNegotiationLogLines[-1] } else { $null }
        first_frame_log_count = $qgcSinkFirstFrameLogLines.Count
        first_frame_delivery_path = if ($qgcSinkFirstFrameMatch.Success) { $qgcSinkFirstFrameMatch.Groups['delivery_path'].Value } else { $null }
        first_frame_width = if ($qgcSinkFirstFrameMatch.Success) { [int]$qgcSinkFirstFrameMatch.Groups['width'].Value } else { $null }
        first_frame_height = if ($qgcSinkFirstFrameMatch.Success) { [int]$qgcSinkFirstFrameMatch.Groups['height'].Value } else { $null }
        first_frame_log = if ($qgcSinkFirstFrameLogLines.Count -gt 0) { $qgcSinkFirstFrameLogLines[0] } else { $null }
        frame_flow_log_count = $qgcSinkFrameFlowLogLines.Count
        last_frame_flow_log = if ($qgcSinkFrameFlowLogLines.Count -gt 0) { $qgcSinkFrameFlowLogLines[-1] } else { $null }
        mapping_failure_log_count = $qgcSinkMappingFailureLogLines.Count
        last_mapping_failure_log = if ($qgcSinkMappingFailureLogLines.Count -gt 0) { $qgcSinkMappingFailureLogLines[-1] } else { $null }
    }
    claims = [ordered]@{
        native_sink_first_frame_telemetry_present = ($qgcSinkFirstFrameLogLines.Count -gt 0)
        qt_quick_pixels_presented_proved = (
            $null -ne $nativeVideoGrabEvidence -and
            $nativeVideoGrabEvidence.native_qquickitem_video_pixels_proved -eq $true
        )
        native_qquickitem_video_surface_transport_bound = (
            $null -ne $nativeVideoGrabEvidence -and
            $nativeVideoGrabEvidence.transport_bound_native_qquickitem_video_surface_proved -eq $true
        )
        desktop_capture_gpu_surface_proved = (
            $null -ne $liveVideoSurfaceEvidence -and
            $liveVideoSurfaceEvidence.gate_passed -eq $true
        )
        qml_rendering_proved = $false
        complete_qml_rendering_semantics_proved = $false
        operator_visual_acceptance = "not_claimed"
    }
    evidence_semantics = "GStreamer/QVideoSink logs are transport diagnostics; a separate acceptance-only grab of QGC's existing videoContent item proves native QQuickItem pixels when present, while desktop compositor capture and operator visual acceptance remain separate."
}
$qgcNativeVideoDiagnostics | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $QgcNativeVideoDiagnosticsPath -Encoding utf8
Write-CombinedStageTrace -Message 'native-video-diagnostics-written'
$authenticatedRuntimeStatusMatches = [regex]::Matches($qgcLog, "authenticated metadata type=23 sequence=(\d+)")
$postAdoptionRuntimeStatusMatches = [regex]::Matches($qgcLog, 'runtime status adopted and acknowledged sequence=(\d+) run_hash64="([0-9a-fA-F]{16})"')
$hilUiAutoExerciseLogMatches = [regex]::Matches($qgcLog, "MultiDetect HIL UI auto exercise")
$hilUiAutoExerciseDisabled = $hilUiAutoExerciseLogMatches.Count -eq 0
$authenticatedRuntimeStatusCount = $authenticatedRuntimeStatusMatches.Count
$runtimeStatusCount = $postAdoptionRuntimeStatusMatches.Count
$authenticatedRuntimeStatusSequences = @($authenticatedRuntimeStatusMatches | ForEach-Object { [int]$_.Groups[1].Value })
$postAdoptionRuntimeStatusSequences = @($postAdoptionRuntimeStatusMatches | ForEach-Object { [int]$_.Groups[1].Value })
$postAdoptionRuntimeStatusRunHashes = @($postAdoptionRuntimeStatusMatches | ForEach-Object { $_.Groups[2].Value.ToLowerInvariant() })
$qgcRuntimeSequencesContiguous = ($authenticatedRuntimeStatusCount -eq $Frames -and $runtimeStatusCount -eq $Frames)
for ($index = 0; $qgcRuntimeSequencesContiguous -and $index -lt $Frames; $index++) {
    $expectedSequence = $index + 1
    if ($authenticatedRuntimeStatusSequences[$index] -ne $expectedSequence -or $postAdoptionRuntimeStatusSequences[$index] -ne $expectedSequence) {
        $qgcRuntimeSequencesContiguous = $false
    }
}
$qgcRuntimeRunHashesBound = ($null -ne $integratedEvidence -and $postAdoptionRuntimeStatusRunHashes.Count -eq $Frames -and @($postAdoptionRuntimeStatusRunHashes | Select-Object -Unique).Count -eq 1 -and $postAdoptionRuntimeStatusRunHashes[0] -eq $expectedRuntimeRunHash64Hex)
if ($null -eq $failure -and $authenticatedRuntimeStatusCount -ne $Frames) {
    $failure = "QGC authenticated $authenticatedRuntimeStatusCount runtime-status type-23 packets; expected $Frames."
}
if ($null -eq $failure -and $runtimeStatusCount -ne $Frames) {
    $failure = "QGC emitted $runtimeStatusCount post-adoption runtime-status ACK log entries; expected $Frames."
}
if ($null -eq $failure -and -not $qgcRuntimeSequencesContiguous) {
    $failure = "QGC type-23 adoption and type-24 acknowledgement sequences are not exactly contiguous from 1 through $Frames."
}
if ($null -eq $failure -and -not $qgcRuntimeRunHashesBound) {
    $failure = "QGC post-adoption acknowledgement log run hashes do not match the integrated SIM identity."
}
if ($null -eq $failure -and -not $hilUiAutoExerciseDisabled) {
    $failure = "Combined SIM unexpectedly enabled the acceptance-only automatic HIL UI driver."
}
Write-CombinedStageTrace -Message 'pre-evidence-object'
$evidence = [ordered]@{
    schema = "multidetect.px4-gazebo-qgc-combined-sim-acceptance"
    schema_version = 2
    runtime_mode = "sim"
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    run_id = $RunId
    gate_passed = (
        $null -eq $failure -and $null -ne $routerEvidence -and
        $null -ne $integratedEvidence -and $null -ne $vtolEvidence -and
        $null -ne $missionEvidence -and $null -ne $fixedWingCapture -and
        $null -ne $missionActiveCapture -and $null -ne $nativeVideoGrabEvidence -and
        $nativeVideoGrabEvidence.transport_bound_native_qquickitem_video_surface_proved -eq $true -and
        $null -ne $toolbarEvidence -and $toolbarEvidence.gate_passed -eq $true
    )
    failure = $failure
    release_status = $ReleaseStatus
    source = [ordered]@{ git_head = $gitHead; dirty = (-not [string]::IsNullOrWhiteSpace($gitStatus)); git_status_sha256 = $gitStatusHash }
    hashes = [ordered]@{
        qgc_exe = [ordered]@{ path = $QgcExecutable; bytes = (Get-Item $QgcExecutable).Length; sha256 = Get-Sha256OrNull $QgcExecutable }
        rtsp_helper = [ordered]@{ path = $RtspHelper; bytes = (Get-Item $RtspHelper).Length; sha256 = Get-Sha256OrNull $RtspHelper }
        product_version_manifest = Get-Sha256OrNull $ProductVersionManifest
        scenario_manifest = Get-Sha256OrNull $ScenarioManifest
        terrain_dem = [ordered]@{ path = $TerrainDem; sha256 = $terrainDemHash; vertical_datum = "MSL"; local_up_used_as_agl = $false }
        mission_config = Get-Sha256OrNull $MissionConfig
        stream_script = $streamScriptHash
        desktop_capture_validator = Get-Sha256OrNull $DesktopCaptureValidator
        native_video_grab_validator = Get-Sha256OrNull $NativeVideoGrabValidator
        toolbar_layout_validator = Get-Sha256OrNull $ToolbarLayoutValidator
        vtol_mission_acceptance = Get-Sha256OrNull $MissionAcceptanceScript
        mission_journal = [ordered]@{
            path = $MissionJournal
            bytes = if (Test-Path -LiteralPath $MissionJournal -PathType Leaf) { (Get-Item -LiteralPath $MissionJournal).Length } else { $null }
            sha256 = Get-Sha256OrNull -Path $MissionJournal
        }
        world_asset = [ordered]@{ path = $WorldAsset; sha256 = $worldHash; container_alias = "/opt/px4-gazebo/share/gz/worlds/default.sdf"; world_namespace = "default" }
        model_assets_composite_sha256 = $modelAssetsHash
        airframe_profile_sha256 = $airframeProfileHash
        synthetic_hil_model = Get-Sha256OrNull $OnnxModel
        synthetic_hil_model_manifest = Get-Sha256OrNull $ModelManifest
        qgc_native_video_diagnostics = [ordered]@{ path = $QgcNativeVideoDiagnosticsPath; sha256 = Get-Sha256OrNull $QgcNativeVideoDiagnosticsPath }
        qgc_video_backend_settings = [ordered]@{ path = $QgcVideoBackendSettingsEvidencePath; sha256 = Get-Sha256OrNull $QgcVideoBackendSettingsEvidencePath }
        qgc_native_toolbar_indicator_evidence = [ordered]@{ path = $ToolbarEvidencePath; sha256 = Get-Sha256OrNull $ToolbarEvidencePath }
    }
    runtime_preflight = [ordered]@{
        path = $RuntimePreflightEvidencePath
        sha256 = Get-Sha256OrNull $RuntimePreflightEvidencePath
        readiness = if ($null -ne $runtimePreflightDocument) { $runtimePreflightDocument.readiness } else { $null }
        docker_diagnostics = if ($null -ne $runtimePreflightDocument) { $runtimePreflightDocument.docker_diagnostics } else { $null }
    }
    docker = [ordered]@{
        executable = $Docker; server_version = $dockerServerVersion; image = $ImageReference; image_id = $imageId
        container = $containerEvidence; container_started = $containerStarted; container_stopped_and_removed = $containerStopped
        container_log = $ContainerLog; container_log_sha256 = Get-Sha256OrNull $ContainerLog
        exactly_one_px4_gazebo_container = ($containerStarted -and $containerStopped)
    }
    gazebo_sensor_topics = [ordered]@{
        stream_timeout_seconds = $SensorStreamTimeoutSeconds
        readiness_semantics = "camera image, CameraInfo, NavSat, and full 3D odometry are required and publisher-stable"
        required = @($requiredSensorTopics)
        required_ready = $requiredSensorTopicsReady
        missing_required = @($missingRequiredSensorTopics)
        publisher_readiness = [ordered]@{
            stable_checks_required = 2
            stable_checks_observed = $requiredSensorPublisherStableChecks
            all_required_publishers_present = (
                @($requiredSensorPublisherStates | Where-Object { -not $_.publisher_present }).Count -eq 0 -and
                $requiredSensorPublisherStates.Count -eq $requiredSensorTopics.Count
            )
            required_states = @($requiredSensorPublisherStates)
        }
        odometry = [ordered]@{
            topic = $requiredOdometryTopic
            required_for_readiness = $true
            listed_by_gz_topic = $odometryTopicListed
            publisher_status = $odometryPublisherStatus
            publisher_present = $odometryPublisherPresent
            observed_timestamp_frame_count = if ($null -ne $integratedEvidence) { $integratedEvidence.sensor_transport.odometry_timestamp_frame_count } else { $null }
            integrated_transport_odometry_required = if ($null -ne $integratedEvidence) { $integratedEvidence.sensor_transport.odometry_required } else { $null }
            full_pose_velocity_fusion_claimed = if ($null -ne $integratedEvidence) { $integratedEvidence.sensor_transport.full_odometry_pose_velocity_fusion_claimed } else { $false }
            topic_info_exit_code = $odometryTopicInfoExitCode
            topic_info_lines = @($odometryTopicInfoLines)
        }
    }
    ports = [ordered]@{
        allocation_mode = $PortAllocationMode
        dynamic_range = [ordered]@{ minimum = $CombinedSimPortRangeMin; maximum = $CombinedSimPortRangeMax }
        allocated_host_ports = @($allocatedCombinedSimPorts)
        rtsp_tcp = $RtspPort; operator_server_udp = $OperatorServerPort; operator_qgc_udp = $OperatorQgcLocalPort
        px4_telemetry_udp = $Px4TelemetryPort; px4_input_host_udp = $Px4InputHostPort
        qgc_px4_udp = $QgcPx4Port; qgc_router_udp = $QgcRouterPort
        evaluator_udp = $EvaluatorPort; evaluator_router_udp = $EvaluatorRouterPort
        protected_udp_14550_before = $protectedOwnersBefore; protected_udp_14550_after = $protectedOwnersAfter
        protected_udp_14550_unchanged = $protectedPortUnchanged; all_test_ports_released = $portsReleased
    }
    router = $routerEvidence
    integrated_sim_qgc_started = $integratedStartEvidence
    integrated_sim_qgc = $integratedEvidence
    synchronization_semantics = [ordered]@{
        per_frame_order = "RTSP frame_accepted -> signed type-23 send -> QGC native state adoption -> signed type-24 acceptance ACK"
        metadata_send_count = "local signed type-23 sends"
        metadata_acceptance_ack_count = "authenticated QGC post-adoption type-24 acknowledgements"
        metadata_barrier_count = "legacy alias of canonical valid type-24 acknowledgements; never sender-side sends"
        type24_scope = "native controller post-adoption only"
        runtime_mode_policy_acceptance_proved = $false
        qml_rendering_proved = $false
        complete_qml_rendering_semantics_proved = $false
        native_qquickitem_video_pixels_proved = if ($null -ne $nativeVideoGrabEvidence) { $nativeVideoGrabEvidence.native_qquickitem_video_pixels_proved } else { $false }
        transport_bound_visible_video_surface_proved = if ($null -ne $nativeVideoGrabEvidence) { $nativeVideoGrabEvidence.transport_bound_native_qquickitem_video_surface_proved } else { $false }
        desktop_capture_gpu_surface_proved = if ($null -ne $liveVideoSurfaceEvidence) { $liveVideoSurfaceEvidence.gate_passed } else { $false }
        exact_source_frame_identity_proved = $false
        operator_visual_acceptance = "not_claimed"
    }
    vtol_attached = $vtolEvidence
    vtol_mission = $missionEvidence
    vtol_mission_journal = $missionJournalEvidence
    vtol_mission_diagnostics = $missionDiagnostics
    qgc = [ordered]@{
        executable = $QgcExecutable; process_udp_ports_during = $qgcUdpPortsDuring; close_method = $qgcCloseMethod
        expected_runtime_mode = "SIM"; post_adoption_runtime_status_ack_logs = $runtimeStatusCount
        authenticated_runtime_status_type23_packets = $authenticatedRuntimeStatusCount
        post_adoption_runtime_status_type24_acks_sent = $runtimeStatusCount
        authenticated_runtime_status_type23_sequences = $authenticatedRuntimeStatusSequences
        post_adoption_runtime_status_type24_sequences = $postAdoptionRuntimeStatusSequences
        post_adoption_runtime_status_run_hash64_hex = @($postAdoptionRuntimeStatusRunHashes | Select-Object -Unique)
        runtime_status_sequences_contiguous_from_one = $qgcRuntimeSequencesContiguous
        runtime_status_run_hash_bound_to_integrated_identity = $qgcRuntimeRunHashesBound
        acceptance_only_environment_absent_before_launch = $acceptanceOnlyEnvironmentAbsentBeforeLaunch
        hil_ui_geometry_export_enabled_before_launch = $hilUiGeometryExportEnabledBeforeLaunch
        hil_native_video_grab_export_enabled_before_launch = $hilNativeVideoGrabExportEnabledBeforeLaunch
        qgc_log_level_before_launch = $qgcLogLevelBeforeLaunch
        hil_ui_auto_exercise_log_count = $hilUiAutoExerciseLogMatches.Count
        hil_ui_auto_exercise_disabled = $hilUiAutoExerciseDisabled
        post_adoption_ack_count_before_fixed_wing = $postAdoptionAckCountBeforeFixedWing
        post_adoption_ack_count_at_fixed_wing_ready = $postAdoptionAckCountAtFixedWingReady
        main_fly_window_is_map_before_pip_swap = $mainFlyWindowIsMapBeforePipSwap
        main_fly_window_is_map_after_pip_swap = $mainFlyWindowIsMapAfterPipSwap
        pip_swap_video_restart_expected = $false
        native_surface = "QGroundControl FlyView/FlyViewVideo/PipView/FlyViewCustomLayer"
        independent_simulator_window_created = $false
        video_backend_ab = $qgcVideoBackendSettingsEvidence
        native_video_log_diagnostics = $qgcNativeVideoDiagnostics
        pip_swap = $pipSwap
        initial_capture = $initialCapture
        pip_swapped_capture = $pipSwappedCapture
        fixed_wing_capture = $fixedWingCapture
        auto_mission_capture = $missionActiveCapture
        auto_mission_ready = $missionActiveReadyEvidence
        auto_mission_type24_sequence_at_ready = $missionReadyQgcSequenceBaseline
        auto_mission_required_post_ready_sequence_advance = $MissionCaptureSequenceAdvance
        auto_mission_type24_sequence_target = $missionCaptureQgcSequenceTarget
        native_video_grab_proof = $nativeVideoGrabEvidence
        desktop_live_video_surface_diagnostic = $liveVideoSurfaceEvidence
        native_toolbar_indicator = $toolbarEvidence
        machine_visual_toolbar_smoke = if ($null -ne $toolbarEvidence -and $toolbarEvidence.gate_passed) { "passed" } else { "failed" }
        complete_qml_rendering_semantics_proved = $false
        machine_visual_smoke = if ($null -ne $nativeVideoGrabEvidence -and $nativeVideoGrabEvidence.gate_passed) { "passed_native_qquickitem_videoContent_pixels_and_transport_binding" } else { "failed" }
        native_qquickitem_video_pixels_proved = if ($null -ne $nativeVideoGrabEvidence) { $nativeVideoGrabEvidence.native_qquickitem_video_pixels_proved } else { $false }
        transport_bound_visible_video_surface_proved = if ($null -ne $nativeVideoGrabEvidence) { $nativeVideoGrabEvidence.transport_bound_native_qquickitem_video_surface_proved } else { $false }
        desktop_capture_gpu_surface_proved = if ($null -ne $liveVideoSurfaceEvidence) { $liveVideoSurfaceEvidence.gate_passed } else { $false }
        temporal_source_motion_proved = if ($null -ne $liveVideoSurfaceEvidence) { $liveVideoSurfaceEvidence.temporal_source_motion_proved } else { $false }
        exact_source_frame_identity_proved = $false
        qml_rendering_proved = $false
        operator_visual_acceptance = "not_claimed"
    }
    safety = [ordered]@{
        software_only = $true; synthetic_hil_model = $true; production_detector_accuracy_claimed = $false
        product_runtime_flight_control_enabled = $false; product_runtime_actuator_output_enabled = $false
        evaluator_commands_test_only = $true; payload_control_enabled = $false
        real_pixhawk_contacted = $false; real_jetson_contacted = $false; real_camera_contacted = $false
        full_odometry_pose_velocity_fusion_claimed = if ($null -ne $integratedEvidence) { $integratedEvidence.sensor_transport.full_odometry_pose_velocity_fusion_claimed } else { $false }; operator_visual_acceptance = "not_claimed"
        crash_dumps_unchanged = $crashDumpsUnchanged
    }
    artifacts = [ordered]@{
        run_directory = $RunDirectory; router_stdout = $RouterStdout; router_stderr = $RouterStderr
        qgc_stdout = $QgcStdout; qgc_stderr = $QgcStderr; integrated_stdout = $IntegratedStdout
        integrated_stderr = $IntegratedStderr; integrated_result = $IntegratedResult; integrated_audit = $IntegratedAudit
        vtol_stdout = $VtolStdout; vtol_stderr = $VtolStderr; vtol_result = $VtolResult
        mission_stdout = $MissionStdout; mission_stderr = $MissionStderr; mission_result = $MissionResult
        mission_journal = $MissionJournal
        mission_active_ready = $MissionActiveReady; mission_active_screenshot = $MissionActiveScreenshot
        runtime_preflight = $RuntimePreflightEvidencePath
        container_inspect = $ContainerInspectPath; container_log = $ContainerLog; container_log_stderr = $ContainerLogStderr
        qgc_live_video_surface_proof = $LiveVideoEvidencePath
        qgc_native_video_grab_proof = $NativeVideoGrabEvidencePath
        qgc_native_video_pip_swapped_capture = $NativeVideoPipSwappedCapture
        qgc_native_video_fixed_wing_capture = $NativeVideoFixedWingCapture
        qgc_native_video_diagnostics = $QgcNativeVideoDiagnosticsPath
        qgc_video_backend_settings = $QgcVideoBackendSettingsEvidencePath
        qgc_native_toolbar_observation = $ToolbarObservationPath
        qgc_native_toolbar_indicator_evidence = $ToolbarEvidencePath
        qgc_native_toolbar_closed_screenshot = $ToolbarClosedScreenshot
        qgc_native_toolbar_drawer_open_screenshot = $ToolbarOpenScreenshot
        evidence_serialization_progress = $EvidenceSerializationProgressPath
    }
}
Write-CombinedStageTrace -Message ("evidence-object-built;top-level-fields={0}" -f $evidence.Count)
# Normalize the heterogeneous PowerShell object graph before serializing.  The
# projection is intentionally bounded and JSON-only; all large/raw diagnostics
# remain available at the paths listed under `artifacts`.
$ProjectionMaximumNodesPerField = 20000
$jsonSafeEvidence = [ordered]@{}
$projectionFields = [ordered]@{}
$jsonFieldParts = [Collections.Generic.List[string]]::new()
try { Remove-Item -LiteralPath $EvidenceSerializationProgressPath -Force -ErrorAction SilentlyContinue } catch { }
Write-EvidenceSerializationProgress -Message 'serialization-start'
foreach ($property in $evidence.GetEnumerator()) {
    Write-EvidenceSerializationProgress -Message ("field-start={0}" -f [string]$property.Key)
    $projectionState = @{
        node_count = 0
        maximum_nodes = $ProjectionMaximumNodesPerField
        truncated = $false
    }
    $propertyKey = [string]$property.Key
    $projectedValue = ConvertTo-JsonSafeEvidenceValue -Value $property.Value -Depth 30 -ProjectionState $projectionState
    Write-EvidenceSerializationProgress -Message ("field-projected={0};nodes={1};truncated={2}" -f $propertyKey, $projectionState.node_count, $projectionState.truncated)
    $jsonSafeEvidence[$propertyKey] = $projectedValue
    $projectionFields[[string]$property.Key] = [ordered]@{
        node_count = [int]$projectionState.node_count
        maximum_nodes = [int]$projectionState.maximum_nodes
        truncated = [bool]$projectionState.truncated
    }
    # Serialize each already-normalized field independently. Windows
    # PowerShell 5.1 can still spend unbounded time walking a large root
    # object passed to ConvertTo-Json, even when every child is JSON-safe.
    # Per-field conversion keeps the work proportional to the field budget
    # and lets the final document be assembled without another graph walk.
    $jsonKey = ConvertTo-Json -InputObject $propertyKey -Compress
    # ConvertTo-Json emits no pipeline value for a top-level `$null` in
    # Windows PowerShell 5.1.  Keep the independently assembled document valid
    # JSON instead of producing an empty value such as `"failure":`.
    $jsonValue = if ($null -eq $projectedValue) {
        'null'
    }
    else {
        ConvertTo-Json -InputObject $projectedValue -Depth 30 -Compress
    }
    [void]$jsonFieldParts.Add("$jsonKey`:$jsonValue")
    Write-EvidenceSerializationProgress -Message ("field-serialized={0};bytes={1}" -f $propertyKey, [Text.Encoding]::UTF8.GetByteCount([string]$jsonValue))
}
$jsonSafeEvidence['serialization'] = [ordered]@{
    schema = 'multidetect.bounded-json-safe-evidence'
    schema_version = 1
    mode = 'per_top_level_field'
    maximum_collection_items = 4096
    maximum_nodes_per_field = $ProjectionMaximumNodesPerField
    fields = $projectionFields
}
$jsonSerialization = ConvertTo-Json -InputObject $jsonSafeEvidence['serialization'] -Depth 30 -Compress
[void]$jsonFieldParts.Add('"serialization":' + $jsonSerialization)
$evidenceJson = '{' + ($jsonFieldParts -join ',') + '}'
Write-Utf8NoBomText -Path $EvidencePath -Text $evidenceJson
Write-EvidenceSerializationProgress -Message ("serialization-complete;bytes={0}" -f [Text.Encoding]::UTF8.GetByteCount($evidenceJson))
Write-Output $evidenceJson
if (-not $evidence.gate_passed) {
    $terminalFailure = if ([string]::IsNullOrWhiteSpace([string]$failure)) {
        "Combined PX4/Gazebo/QGC SIM acceptance failed."
    }
    else { [string]$failure }
    throw $terminalFailure
}
Write-Host "Combined SIM evidence: $EvidencePath"
Write-Host "Native QGC fixed-wing screenshot: $FixedWingScreenshot"
}
finally {
    # Restore every captured process-level value even if one restoration fails,
    # then clear both mutable secret buffers on every exit path.
    $sensitiveCleanupFailure = $null
    if ($environmentSnapshotComplete) {
        foreach ($name in $environmentNames) {
            try {
                if ($null -eq $savedEnvironment[$name]) {
                    if (Test-Path -LiteralPath "Env:$name") {
                        Remove-Item -LiteralPath "Env:$name" -ErrorAction Stop
                    }
                }
                else {
                    [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
                }
            }
            catch {
                if ($null -eq $sensitiveCleanupFailure) { $sensitiveCleanupFailure = $_.Exception }
            }
        }
    }
    try {
        if ($null -ne $hmacBytes) { [Array]::Clear($hmacBytes, 0, $hmacBytes.Length) }
    }
    finally {
        if ($null -ne $signingBytes) { [Array]::Clear($signingBytes, 0, $signingBytes.Length) }
        $temporaryHmac = $null
        $temporarySigningHex = $null
    }
    if ($null -ne $sensitiveCleanupFailure) { throw $sensitiveCleanupFailure }
}

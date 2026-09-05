from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_px4_gazebo_qgc_combined_sim_acceptance.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _read_script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _integer_assignment(text: str, variable: str) -> int:
    match = re.search(
        rf"^\${re.escape(variable)}\s*=\s*(\d+)\s*(?:#.*)?$",
        text,
        flags=re.MULTILINE,
    )
    assert match is not None, f"missing integer assignment for ${variable}"
    return int(match.group(1))


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell parser is unavailable")
def test_combined_acceptance_script_parses_as_powershell() -> None:
    escaped_path = str(SCRIPT).replace("'", "''")
    command = (
        "$tokens = $null; $parseErrors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped_path}', "
        "[ref]$tokens, [ref]$parseErrors) | Out-Null; "
        "if ($parseErrors.Count -ne 0) { "
        "$parseErrors | ForEach-Object { [Console]::Error.WriteLine($_.ToString()) }; exit 1 }; "
        "exit 0"
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell compiler is unavailable")
def test_embedded_exact_window_native_csharp_compiles(tmp_path: Path) -> None:
    text = _read_script()
    match = re.search(
        r"Add-Type -TypeDefinition @'\r?\n(.*?)\r?\n'@",
        text,
        flags=re.DOTALL,
    )
    assert match is not None
    source_path = tmp_path / "MultiDetectWindow.Native.cs"
    source_path.write_text(match.group(1), encoding="utf-8")
    escaped_path = str(source_path).replace("'", "''")
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Add-Type -Path '{escaped_path}' -ErrorAction Stop; exit 0",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_combined_gate_owns_one_hardened_container_and_three_readonly_mounts() -> None:
    text = _read_script()

    assert text.count('"run", "-d", "--name", $ContainerName') == 1
    assert '"--network", "bridge"' in text
    assert '"--security-opt", "no-new-privileges:true"' in text
    assert '"--cap-drop", "ALL"' in text
    assert '"--device"' not in text

    mounts = re.findall(r'^\s*"--mount",\s*"([^"]+)",?$', text, flags=re.MULTILINE)
    assert len(mounts) == 3
    assert all(mount.endswith(",readonly") for mount in mounts)
    assert any(
        "source=$ModelRoot,target=/opt/multidetect-gazebo-models,readonly" in mount
        for mount in mounts
    )
    assert any(
        "source=$WorldAsset,target=/opt/px4-gazebo/share/gz/worlds/default.sdf,readonly"
        in mount
        for mount in mounts
    )
    assert any("target=/workspace/scripts,readonly" in mount for mount in mounts)

    assert "exactly_three_mounts = ($mounts.Count -eq 3)" in text
    assert "mounts_match_readonly_whitelist" in text
    assert "exactly_one_px4_gazebo_container = ($containerStarted -and $containerStopped)" in text


def test_river_world_model_and_required_sensor_topics_are_bound_to_default_namespace() -> None:
    text = _read_script()

    assert r"simulation\gazebo\worlds\river_rescue_vtol_001.sdf" in text
    assert "/opt/px4-gazebo/share/gz/worlds/default.sdf" in text
    assert '"--env", "PX4_GZ_WORLD=default"' in text
    assert '"--env", "PX4_GZ_MODELS=/opt/multidetect-gazebo-models"' in text
    assert '"--env", "PX4_SIM_MODEL=gz_standard_vtol_mono_cam"' in text
    assert '"--env", "PX4_SYS_AUTOSTART=4004"' in text
    assert '"world_sha256=$worldHash"' in text
    assert (
        'container_alias = "/opt/px4-gazebo/share/gz/worlds/default.sdf"; '
        'world_namespace = "default"'
    ) in text

    assert "/world/default/model/standard_vtol_mono_cam_0/" in text
    assert "/sensor/camera/image" in text
    assert "/sensor/camera/camera_info" in text
    assert "/sensor/navsat_sensor/navsat" in text
    assert "/model/standard_vtol_mono_cam_0/odometry_with_covariance" in text

    required_topics_match = re.search(
        r"\$requiredSensorTopics\s*=\s*@\((.*?)\n\)",
        text,
        flags=re.DOTALL,
    )
    assert required_topics_match is not None
    required_topics = required_topics_match.group(1)
    assert "/sensor/camera/image" in required_topics
    assert "/sensor/camera/camera_info" in required_topics
    assert "/sensor/navsat_sensor/navsat" in required_topics
    assert "/odometry_with_covariance" in required_topics

    assert (
        '$requiredOdometryTopic = '
        '"/model/standard_vtol_mono_cam_0/odometry_with_covariance"'
    ) in text
    assert '"gz", "topic", "-i", "-t", $requiredOdometryTopic' in text
    assert 'required_for_readiness = $true' in text
    assert 'publisher_status = $odometryPublisherStatus' in text
    assert 'publisher_present = $odometryPublisherPresent' in text
    assert "$integratedEvidence.sensor_transport.odometry_required -eq $true" in text
    assert (
        "$integratedEvidence.sensor_transport."
        "full_odometry_pose_velocity_fusion_claimed -eq $true"
    ) in text
    assert "Required Gazebo full-odometry publisher is absent" in text
    assert "$integratedEvidence.sensor_transport.odometry_timestamp_frame_count -eq $Frames" in text
    assert "$integratedEvidence.terrain_dem_sha256 -eq $terrainDemHash" in text
    assert "$integratedEvidence.odometry_local_up_used_as_agl -eq $false" in text
    assert "observed_timestamp_frame_count" in text
    assert 'No publishers on topic' in text
    assert 'Gazebo camera image/CameraInfo/NavSat/full-odometry topics were not ready' in text


def test_combined_sensor_readiness_requires_stable_gazebo_publishers_before_stream_start() -> None:
    text = _read_script()

    assert "function Get-GazeboTopicPublisherState" in text
    readiness_start = text.index("$requiredSensorTopicsReady = $false")
    readiness_end = text.index("$odometryTopicListed = $false", readiness_start)
    readiness_block = text[readiness_start:readiness_end]
    assert "$requiredSensorPublisherStates = @()" in readiness_block
    assert "$requiredSensorPublisherStableChecks = 0" in readiness_block

    loop_start = text.index("for ($attempt = 1; $attempt -le 90; $attempt++)")
    loop_end = text.index("Assert-True $requiredSensorTopicsReady", loop_start)
    loop = text[loop_start:loop_end]
    assert "Get-GazeboTopicPublisherState" in loop
    assert "$requiredSensorPublisherStableChecks++" in loop
    assert "$requiredSensorPublisherStableChecks = 0" in loop
    assert "$requiredSensorPublisherStableChecks -ge 2" in loop
    assert "Start-Sleep -Milliseconds 500" in loop

    evidence_start = text.index("gazebo_sensor_topics = [ordered]@{")
    evidence_end = text.index("odometry = [ordered]@{", evidence_start)
    evidence = text[evidence_start:evidence_end]
    assert "publisher_readiness = [ordered]@{" in evidence
    assert "stable_checks_required = 2" in evidence
    assert "all_required_publishers_present" in evidence


def test_every_combined_gate_port_is_distinct_loopback_only_and_excludes_14550() -> None:
    text = _read_script()

    # Host ports must be selected at run time.  Docker Desktop reserves ports
    # independently of the repository, so fixed historical assignments made
    # otherwise healthy combined-SIM runs fail before the container starts.
    assert _integer_assignment(text, "CombinedSimPortRangeMin") == 30000
    assert _integer_assignment(text, "CombinedSimPortRangeMax") == 60000
    assert _integer_assignment(text, "CombinedSimHostPortCount") == 9
    assert '"dynamic_loopback"' in text
    assert "function Test-CombinedSimLoopbackPortFree" in text
    assert "function Get-FreeCombinedSimPorts" in text
    assert "-Count $CombinedSimHostPortCount" in text
    assert "-Excluded @($ProtectedGroundStationPort, $Px4ContainerInputPort)" in text
    assert "$allocatedCombinedSimPorts.Count -eq $CombinedSimHostPortCount" in text
    assert "$OperatorServerPort = [int]$allocatedCombinedSimPorts[0]" in text
    assert "$RtspPort = [int]$allocatedCombinedSimPorts[8]" in text

    # The only fixed values are the container-internal input and protected
    # production endpoint; neither is exposed as a dynamic host port.
    assert _integer_assignment(text, "Px4ContainerInputPort") == 18570
    assert _integer_assignment(text, "ProtectedGroundStationPort") == 14550

    assert "Select-Object -Unique).Count -eq $allUdpPorts.Count" in text
    assert "$allUdpPorts -notcontains $ProtectedGroundStationPort" in text
    assert '"127.0.0.1:$Px4InputHostPort`:$Px4ContainerInputPort/udp"' in text
    assert 'MULTIDETECT_OPERATOR_UDP_HOST = "127.0.0.1"' in text
    assert 'MULTIDETECT_VIDEO_RTSP_URL = "rtsp://127.0.0.1:$RtspPort/' in text
    assert '"--rtsp-bind-host", "127.0.0.1"' in text
    assert '"--operator-udp-bind-host", "127.0.0.1"' in text
    assert "protected_udp_14550_unchanged = $protectedPortUnchanged" in text


def test_combined_sim_cleanup_does_not_wait_forever_for_docker_desktop() -> None:
    text = _read_script()
    cleanup_start = text.index("function Stop-OwnedContainer")
    cleanup_end = text.index("if (-not ([System.Management.Automation.PSTypeName]", cleanup_start)
    cleanup = text[cleanup_start:cleanup_end]
    assert '"inspect", "--format", "{{.Id}}", $Name)' in cleanup
    assert "-TimeoutSeconds 20" in cleanup


def test_portable_qgc_runs_visible_on_the_native_fly_view_surface() -> None:
    text = _read_script()

    assert '$ProductVersionManifest = Join-Path $Root "configs\\product-version.json"' in text
    assert '$ReleaseParityPath = Join-Path $Root "configs\\release-parity.json"' in text
    assert '$QgcExecutable, $RtspHelper, $ReleaseParityPath, $ScenarioManifest' in text
    assert "release_parity = [ordered]@{" in text
    assert "bytes = (Get-Item $ReleaseParityPath).Length" in text
    assert "sha256 = Get-Sha256OrNull $ReleaseParityPath" in text
    assert (
        "parity = (Get-Content -LiteralPath $ReleaseParityPath -Raw | "
        "ConvertFrom-Json).parity"
    ) in text
    assert '"build\\windows-v{0}-release\\portable-v{0}\\bin" -f $ReleaseLabel' in text
    assert '"build\\qgc-replay-fixture-v{0}" -f $ReleaseLabel' in text
    assert 'release_status = $ReleaseStatus' in text
    assert "0.3.0-pre.3" not in text
    assert '$QgcExecutable = Join-Path $QgcPortableBin "MultiDetectGCS.exe"' in text
    assert (
        '$RtspHelper = Join-Path $QgcPortableBin "MultiDetectReplayRtspPublisher.exe"'
        in text
    )
    assert "-FilePath $QgcExecutable" in text
    assert "-WorkingDirectory $QgcPortableBin" in text
    assert "-WindowStyle Normal" in text
    assert '"--isolated-hil"' in text
    assert '"--isolated-hil-px4-udp-port"' in text
    assert '"--isolated-hil-px4-target-port"' in text
    assert (
        'Remove-Item -LiteralPath "Env:MULTIDETECT_DISABLE_VEHICLE_LINK" '
        '-ErrorAction SilentlyContinue'
    ) in text
    assert (
        'Remove-Item -LiteralPath "Env:MULTIDETECT_HIL_UI_AUTO_EXERCISE" '
        '-ErrorAction SilentlyContinue'
    ) in text
    assert (
        'Remove-Item -LiteralPath "Env:MULTIDETECT_HIL_UI_AUTO_EXIT" '
        '-ErrorAction SilentlyContinue'
    ) in text
    assert 'SetEnvironmentVariable("MULTIDETECT_DISABLE_VEHICLE_LINK", $null' not in text
    assert 'SetEnvironmentVariable("MULTIDETECT_HIL_UI_AUTO_EXERCISE", $null' not in text
    assert 'SetEnvironmentVariable("MULTIDETECT_HIL_UI_AUTO_EXIT", $null' not in text
    assert 'if ($null -eq $savedEnvironment[$name])' in text
    assert 'if (Test-Path -LiteralPath "Env:$name")' in text
    assert 'Remove-Item -LiteralPath "Env:$name" -ErrorAction Stop' in text
    assert "$acceptanceOnlyEnvironmentAbsentBeforeLaunch" in text
    assert "MultiDetect HIL UI auto exercise" in text
    assert "hil_ui_auto_exercise_log_count = $hilUiAutoExerciseLogMatches.Count" in text
    assert "hil_ui_auto_exercise_disabled = $hilUiAutoExerciseDisabled" in text
    assert 'qEnvironmentVariableIsSet for their special auto-exit teardown path' in text
    assert '$env:MULTIDETECT_HIL_UI_AUTO_EXERCISE = "0"' not in text
    assert '$env:MULTIDETECT_HIL_UI_AUTO_EXIT = "0"' not in text
    assert '"MULTIDETECT_HIL_UI_GEOMETRY_EXPORT"' in text
    assert '$env:MULTIDETECT_HIL_UI_GEOMETRY_EXPORT = "1"' in text
    assert "$hilUiGeometryExportEnabledBeforeLaunch" in text
    assert '"QGC_LOG_LEVEL"' in text
    assert '$env:QGC_LOG_LEVEL = "info"' in text
    assert "qgc_log_level_before_launch = $qgcLogLevelBeforeLaunch" in text
    assert "Save-QgcWindowCapture -Process $qgcProcess" in text
    assert "Invoke-QgcNativePipSwap -Process $qgcProcess" in text
    assert (
        'native_surface = "QGroundControl FlyView/FlyViewVideo/PipView/FlyViewCustomLayer"'
        in text
    )
    assert "independent_simulator_window_created = $false" in text
    assert "ApplicationWindow" not in text
    assert "QtQuick.Window" not in text


def test_native_multidetect_toolbar_uses_qgc_scene_geometry_and_exact_hwnd_input() -> None:
    text = _read_script()

    assert "MULTIDETECT_HIL_UI_GEOMETRY " in text
    assert "Wait-QgcHilUiGeometryRecord" in text
    assert "Convert-QgcHilUiGeometryToPhysicalScreenRect" in text
    assert 'Role "indicator_ready"' in text
    assert 'Role "drawer_open"' in text
    assert 'Role "drawer_closed"' in text
    assert 'geometry_source = "qgc_qml_scene_geometry_export"' in text
    assert 'geometry_schema = [string]$indicator.schema' in text
    assert "Get-QgcPointProvenanceSample" in text
    assert "[MultiDetectWindow.Native]::GetAncestor($hitHwnd, 2)" in text
    assert "GetWindowThreadProcessId($rootHwnd" in text
    assert "qgc_pid_matches = $indicatorPidMatches" in text
    assert "qgc_hwnd_matches = $indicatorHwndMatches" in text
    assert "qgc_pid_matches = $drawerPidMatches" in text
    assert "qgc_hwnd_matches = $drawerHwndMatches" in text
    assert "provenance_samples = $indicatorProvenanceSamples" in text
    assert "provenance_samples = $drawerProvenanceSamples" in text
    assert "qgc_pid_matches = $true" not in text
    assert "qgc_hwnd_matches = $true" not in text
    for sample_name in (
        "center",
        "top_left_inset",
        "top_right_inset",
        "bottom_left_inset",
        "bottom_right_inset",
    ):
        assert f'-Name "{sample_name}"' in text
    assert "$drawerProvenanceSamples.Count -eq 5" in text
    assert "scene_to_physical_scale_x" in text
    assert "reported_device_pixel_ratio" in text
    assert "exact_client_rect_screen_physical" in text
    assert "WindowFromPoint($clickPoint)" in text
    assert "owner_matches_qgc = ($hitRootOwner -eq $Process.Id)" in text
    assert "root_hwnd_matches_qgc = ($hitRootWindow -eq $window.Handle)" in text
    assert 'dpi_awareness = "per_monitor_v2"' in text
    assert "cursor_position_verified" in text
    assert (
        text.index("$previousForeground = Set-QgcWindowCaptureForeground -Window $window")
        < text.index("$hitWindow = [MultiDetectWindow.Native]::WindowFromPoint($clickPoint)")
    )
    assert (
        text.index("[MultiDetectWindow.Native]::SetCursorPos($clickX, $clickY)")
        < text.index("$hitWindow = [MultiDetectWindow.Native]::WindowFromPoint($clickPoint)")
    )
    assert (
        'method = "qml_scene_geometry_export_plus_exact_hwnd_client_to_screen_plus_sendinput"'
        in text
    )
    assert "GetVisibleTopLevelWindowsForProcess" in text
    assert "SendEscapeKey()" in text
    assert "drawerClosedAfterEscape" in text
    assert "not_required_qgc_qml_geometry_export" in text
    assert "qgc-native-toolbar-indicator-observation.json" in text
    assert "qgc-native-toolbar-indicator-evidence.json" in text
    assert "qgc-native-toolbar-drawer-open.png" in text
    assert '"--observation", $ObservationPath' in text
    assert '"--closed-capture", $ClosedCapture' in text
    assert '"--open-capture", $OpenCapture' in text
    assert 'machine_visual_toolbar_smoke -eq "passed"' in text
    assert "complete_qml_rendering_semantics_proved = $false" in text
    assert 'operator_visual_acceptance = "not_claimed"' in text
    assert "guessed coordinates" in text
    assert "ApplicationWindow" not in text
    assert "QtQuick.Window" not in text
    assert "VideoOutput {" not in text


def test_native_qml_geometry_parser_rejects_non_positive_sequences() -> None:
    text = _read_script()

    sequence_parse = text.index("$sequence = [long]$record.sequence")
    sequence_floor = text.index("if ($sequence -lt 1) { continue }", sequence_parse)
    minimum_sequence = text.index(
        "if ($sequence -le $MinimumSequence) { continue }", sequence_floor
    )
    assert sequence_parse < sequence_floor < minimum_sequence


def test_integrated_operator_and_rtsp_bind_before_qgc_starts() -> None:
    text = _read_script()

    integrated_start = text.index(
        "$integratedProcess = Start-Process -FilePath $Python -ArgumentList $integratedArguments"
    )
    qgc_start = text.index(
        "$qgcProcess = Start-Process -FilePath $QgcExecutable -ArgumentList $qgcArguments"
    )

    assert integrated_start < qgc_start
    assert "ICMP port-unreachable" in text
    assert 'Pattern "runtime status adopted and acknowledged sequence="' in text
    assert 'Pattern \'Video "videoContent" streaming changed, active: yes\'' in text
    assert 'Pattern \'Video "videoContent" decoding changed, active: yes\'' in text
    assert "New resolution: 1280 x 960" in text
    assert "Wait-QgcMainFlyWindowIsMapSetting" in text
    assert "QGCQml/MainFlyWindowIsMap true(default)->false" in text
    assert "pip_swap_video_restart_expected = $false" in text


def test_combined_acceptance_runs_runtime_preflight_before_docker_info() -> None:
    text = _read_script()

    assert (
        '$RuntimePreflightEvidencePath = Join-Path $RunDirectory "runtime-preflight.json"'
        in text
    )
    preflight_index = text.index(
        '"-m", "multidetect.simulator", "runtime-preflight", "--out", $RuntimePreflightEvidencePath'
    )
    docker_info_index = text.index(
        '$dockerInfo = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("info"'
    )
    assert preflight_index < docker_info_index
    assert '$runtimePreflightReadiness.ready_to_attempt -eq $true' in text
    assert '"External SIM runtime is not ready:' in text
    assert 'runtime_preflight = [ordered]@{' in text
    assert 'runtime_preflight = $RuntimePreflightEvidencePath' in text
    # The parsed document must survive the later process-variable setup so the
    # final evidence retains readiness and bounded Docker diagnostics.
    assert "$runtimePreflightDocument = $null" not in text
    assert text.index(
        "$runtimePreflightDocument = Get-Content -LiteralPath $RuntimePreflightEvidencePath"
    ) < text.index("$dockerInfo = Invoke-CapturedCommand")


def test_timeout_command_wrapper_supports_windows_powershell_argument_fallback() -> None:
    text = _read_script()

    quoting_function = text.index("function ConvertTo-WindowsProcessArgument")
    invoke_function = text.index("function Invoke-CapturedCommand")
    fallback = text.index("$startInfo.Arguments =", invoke_function)
    process_start = text.index("Assert-True ($process.Start())", invoke_function)

    assert quoting_function < invoke_function < fallback < process_start
    assert "CommandLineToArgvW" in text
    assert 'GetProperty("ArgumentList")' in text
    assert "ConvertTo-WindowsProcessArgument -Value ([string]$_)" in text
    assert "multi-line `sh -c` payload" in text

    # Windows PowerShell 5.1/.NET Framework has no ProcessStartInfo.ArgumentList.
    # Keep the fallback's nested-quote rule pinned: even a quote with zero
    # pending backslashes must receive an odd escape run, otherwise the shell
    # script is split into bogus Docker argv elements (for example ``ahostsv4``).
    assert "A literal quote must always be preceded by an odd run" in text
    assert "('\\' * ($backslashes * 2 + 1))" in text
    assert "if ($backslashes -gt 0) {" in text


def test_container_log_capture_is_streamed_to_bounded_files() -> None:
    text = _read_script()

    helper = text.index("function Save-CommandOutputToFile")
    finally_capture = text.index(
        "$containerLogExitCode = Save-CommandOutputToFile"
    )
    assert helper < finally_capture
    assert "RedirectStandardOutput $OutputPath" in text[helper:]
    assert "RedirectStandardError $ErrorPath" in text[helper:]
    assert '"logs", "--tail", "256", $ContainerName' in text[finally_capture:]
    assert "$ContainerLogStderr = Join-Path $RunDirectory \"container.log.stderr\"" in text
    assert "container_log_stderr = $ContainerLogStderr" in text
    # Regression guard: the old implementation materialised the complete
    # Docker stream in an object array and could consume multiple gigabytes
    # when PX4 emitted ANSI prompts without newlines.
    assert '$logs = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @("logs"' not in text


def test_native_qgc_video_diagnostics_reuse_logging_without_claiming_visible_pixels() -> None:
    text = _read_script()

    for category in (
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
        "Video.GStreamer.HwBuffers.GstD3D12Buf",
    ):
        assert f'"{category}"' in text

    assert '"--logging", ($QgcNativeVideoLoggingCategories -join ",")' in text
    categories_start = text.index("$QgcNativeVideoLoggingCategories = @(")
    categories_end = text.index(")", categories_start)
    assert '"full"' not in text[categories_start:categories_end]
    assert "Resolved RHI backend:" in text
    assert "zero-copy path:" in text
    assert "Decodebin3 selected codec:rank -" in text
    assert "set_caps: format=" in text
    assert "Negotiation update: format=" in text
    assert "first frame delivered via" in text
    assert "frame flow:" in text
    assert "show_frame: mapping failed" in text
    assert (
        'native_chain = "GStreamer qgcqvideosink -> QVideoSink -> '
        'FlightDisplayViewVideoOutput.qml VideoOutput"'
    ) in text
    assert 'schema = "multidetect.qgc-native-video-log-diagnostics"' in text
    assert '"qgc-native-video-diagnostics.json"' in text
    assert "native_sink_first_frame_telemetry_present" in text
    assert "qt_quick_pixels_presented_proved = (" in text
    assert "$nativeVideoGrabEvidence.native_qquickitem_video_pixels_proved -eq $true" in text
    assert "native_qquickitem_video_surface_transport_bound" in text
    assert "desktop_capture_gpu_surface_proved = (" in text
    assert "$liveVideoSurfaceEvidence.gate_passed -eq $true" in text
    assert "qml_rendering_proved = $false" in text
    assert "complete_qml_rendering_semantics_proved = $false" in text
    assert 'operator_visual_acceptance = "not_claimed"' in text
    assert "separate acceptance-only grab of QGC's existing videoContent item" in text

    # The A/B changes one native backend setting after QGC clears its isolated
    # profile. No decoder override or parallel QML surface is introduced.
    assert '"--isolated-hil-force-cpu-video"' in text
    assert "function Get-QgcVideoSettingsSnapshot" in text
    assert "function Wait-QgcCpuVideoSettings" in text
    assert "force_cpu_video_path_enabled -eq $true" in text
    assert "force_video_decoder_explicit -eq $false" in text
    assert (
        "isolated HIL CPU-video A/B enabled: Video/forceCpuVideoPath=true; "
        "forceVideoDecoder=default"
    ) in text
    assert 'schema = "multidetect.qgc-video-backend-settings"' in text
    assert '"qgc-video-backend-settings.json"' in text
    assert "prelaunch_snapshot_is_runtime_authority = $false" in text
    assert "runtime_snapshot_timestamp_bound_to_qgc_process" in text
    assert "force_cpu_video_path_proved = $qgcCpuVideoAbProved" in text
    assert "force_video_decoder_override_present" in text
    assert 'native_ui_modified = $false' in text
    assert "this artifact does not prove visible pixels" in text

    evidence_start = text.index("$evidence = [ordered]@{")
    assert 'runtime_mode = "sim"' in text[evidence_start : evidence_start + 300]
    gate_start = text.index("gate_passed = (", evidence_start)
    gate_end = text.index("failure = $failure", gate_start)
    assert "$qgcNativeVideoDiagnostics" not in text[gate_start:gate_end]
    assert "$qgcVideoBackendSettingsEvidence" not in text[gate_start:gate_end]

    # The evidence route observes the existing QGC surface. It must not create
    # an acceptance-only window or another VideoOutput.
    assert "ApplicationWindow" not in text
    assert "QtQuick.Window" not in text
    assert "VideoOutput {" not in text


def test_sensor_stream_deadline_scales_with_the_frame_budget() -> None:
    text = _read_script()

    assert "$SensorStreamTimeoutSeconds = [int][Math]::Max(" in text
    assert "[Math]::Ceiling($Frames / 2.0) + 30" in text


def test_perception_dimensions_are_derived_from_model_manifest() -> None:
    text = _read_script()

    required_block = text[text.index("foreach ($required in @("):text.index(
        "Assert-True (-not (Test-Path -LiteralPath $RunDirectory))"
    )]
    assert (
        "$ModelManifestDocument = Get-Content -LiteralPath $ModelManifest -Raw | "
        "ConvertFrom-Json"
    ) in required_block
    assert "$ModelInputShape = @($ModelManifestDocument.input.shape)" in required_block
    assert "Assert-True ($ModelInputShape.Count -eq 4)" in required_block
    assert "$PerceptionInputHeight = [int]$ModelInputShape[2]" in required_block
    assert "$PerceptionInputWidth = [int]$ModelInputShape[3]" in required_block
    assert (
        '"--input-width", "$PerceptionInputWidth", '
        '"--input-height", "$PerceptionInputHeight"'
    ) in text
    assert '"--input-width", "64", "--input-height", "64"' not in text

    assert '"--stream-timeout-seconds", "$SensorStreamTimeoutSeconds"' in text
    assert "TimeoutMilliseconds $IntegratedWaitTimeoutMilliseconds" in text
    assert "stream_timeout_seconds = $SensorStreamTimeoutSeconds" in text
    assert '"--stream-timeout-seconds", "100"' not in text


def test_custom_evidence_path_parent_is_created_before_terminal_write() -> None:
    text = _read_script()

    assert "$EvidencePath = [IO.Path]::GetFullPath($EvidencePath)" in text
    assert "$EvidenceParentDirectory = Split-Path -Parent $EvidencePath" in text
    create_start = text.index("[IO.Directory]::CreateDirectory($ArtifactDirectory)")
    create_end = text.index("$allUdpPorts = @(", create_start)
    create_block = text[create_start:create_end]
    assert "[IO.Directory]::CreateDirectory($EvidenceParentDirectory)" in create_block


def test_text_barrier_reads_tail_before_treating_child_exit_as_failure() -> None:
    text = _read_script()
    start = text.index("function Wait-TextPattern")
    end = text.index("function Get-TextPatternCount", start)
    helper = text[start:end]

    first_read = helper.index("$text = ($Paths | ForEach-Object")
    first_match = helper.index("if ($text -match $Pattern)")
    process_check = helper.index("foreach ($process in $Processes)")

    assert first_read < first_match < process_check
    assert helper.count("$text = ($Paths | ForEach-Object") == 2
    assert "checking HasExited first would discard valid tail" in helper


def test_fixed_wing_phase_requires_a_new_post_adoption_ack_log() -> None:
    text = _read_script()

    assert "function Wait-TextPatternCountGreaterThan" in text
    assert "$postAdoptionAckCountBeforeFixedWing = Get-TextPatternCount" in text
    assert "$postAdoptionAckCountAtFixedWingReady = Wait-TextPatternCountGreaterThan" in text
    assert "-BaselineCount $postAdoptionAckCountBeforeFixedWing" in text
    assert "post_adoption_ack_count_before_fixed_wing" in text
    assert "post_adoption_ack_count_at_fixed_wing_ready" in text


def test_finite_integrated_producer_exit_does_not_abort_later_vtol_or_mission_barriers() -> None:
    text = _read_script()

    fixed_wing_start = text.index("Wait-FileCreated -Path $FixedWingReady")
    fixed_wing_end = text.index("$fixedWingCapture = Save-QgcWindowCapture", fixed_wing_start)
    fixed_wing_block = text[fixed_wing_start:fixed_wing_end]
    assert "$integratedProcess" not in fixed_wing_block
    fixed_wing_capture_start = text.index(
        "Wait-FileCreated -Path $NativeVideoFixedWingCapture", fixed_wing_start
    )
    fixed_wing_capture_end = text.index(
        "$fixedWingCapture = Save-QgcWindowCapture", fixed_wing_capture_start
    )
    fixed_wing_capture_block = text[fixed_wing_capture_start:fixed_wing_capture_end]
    assert "$vtolProcess" not in fixed_wing_capture_block
    assert "-Processes @($qgcProcess, $routerProcess)" in fixed_wing_capture_block
    assert (
        "-Pattern 'MULTIDETECT_HIL_VIDEO_GRAB .*\"role\":\"fixed-wing\"'"
        " -TimeoutSeconds 5 -Processes @($qgcProcess)"
    ) in fixed_wing_capture_block
    producer_comment = text.index("# The integrated sensor/AI producer")
    assert producer_comment < fixed_wing_start
    assert "exit_code=0" in text[producer_comment:fixed_wing_start]

    mission_start = text.index("Wait-FileCreated -Path $MissionActiveReady")
    mission_end = text.index("$missionActiveReadyEvidence =", mission_start)
    mission_block = text[mission_start:mission_end]
    assert "$integratedProcess" not in mission_block

    mission_sequence_start = text.index("$missionCaptureQgcSequenceTarget", mission_start)
    mission_sequence_end = text.index("$missionActiveCapture =", mission_sequence_start)
    assert "$integratedProcess" not in text[mission_sequence_start:mission_sequence_end]


def test_multi_endpoint_router_attached_vtol_and_integrated_sim_share_one_container() -> None:
    text = _read_script()

    assert '"multidetect.px4_gazebo_qgc_router"' in text
    for argument in (
        "--qgc-port",
        "--qgc-router-port",
        "--evaluator-port",
        "--evaluator-router-port",
        "--px4-telemetry-port",
        "--px4-input-port",
        "--evaluator-system-id",
        "--evaluator-component-id",
    ):
        assert f'"{argument}"' in text
    assert (
        r'$AttachedVtolScript = Join-Path $Root "scripts\vtol_sitl_attached_acceptance.py"'
        in text
    )
    assert '"--container-name", $ContainerName' in text
    assert '"--fixed-wing-ready-file", $FixedWingReady' in text
    assert "$expectedEvaluatorCommandLongCount = 4 + $missionCommandLongEvents.Count" in text
    assert (
        "$routerEvidence.evaluator_control_commands_forwarded -eq "
        "$expectedEvaluatorCommandLongCount"
    ) in text
    assert "@($vtolEvidence.mavlink.command_events).Count -eq 4" in text

    assert '"multidetect.cli", "integrated-sim-qgc"' in text
    assert '"--stream-script", "/workspace/scripts/gazebo_sensor_stream.py"' in text
    assert '"--model-instance", "standard_vtol_mono_cam_0"' in text
    assert '"--gazebo-world", "default"' in text
    assert '"--terrain-dem", $TerrainDem' in text
    assert '"--rtsp-helper", $RtspHelper' in text
    assert '"--metadata-acceptance-ack-timeout-seconds", "3"' in text
    assert '"--operator-hmac-key-env", "MULTIDETECT_OPERATOR_KEY"' in text
    assert (
        '"--mavlink-signing-key-hex-env", "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX"'
        in text
    )
    assert "$integratedEvidence.synchronization.video_frame_count -eq $Frames" in text
    assert "$integratedEvidence.synchronization.metadata_send_count -eq $Frames" in text
    assert "$integratedEvidence.synchronization.metadata_acceptance_ack_count -eq $Frames" in text
    assert "$integratedEvidence.synchronization.metadata_barrier_count -eq $Frames" in text
    assert "unique_sent_runtime_sequence_count -eq $Frames" in text
    assert "first_sent_runtime_sequence -eq 1" in text
    assert "last_sent_runtime_sequence -eq $Frames" in text
    assert "sent_runtime_sequences_contiguous -eq $true" in text
    assert "unique_acknowledged_runtime_sequence_count -eq $Frames" in text
    assert "first_acknowledged_runtime_sequence -eq 1" in text
    assert "last_acknowledged_runtime_sequence -eq $Frames" in text
    assert "acknowledged_runtime_sequences_contiguous -eq $true" in text
    assert "acknowledged_runtime_run_hash64_hex -eq $expectedRuntimeRunHash64Hex" in text
    assert (
        "metadata_barrier_count -eq "
        "$integratedEvidence.synchronization.metadata_acceptance_ack_count"
    ) in text
    assert "acceptance_ack_peer_port -eq $OperatorQgcLocalPort" in text
    assert "acceptance_ack_peer_host -eq $integratedEvidence.synchronization.qgc_peer_host" in text
    assert "runtime status adopted and acknowledged sequence=" in text
    assert "$authenticatedRuntimeStatusCount -ne $Frames" in text
    assert "$runtimeStatusCount -ne $Frames" in text
    assert "authenticated_runtime_status_type23_packets" in text
    assert "post_adoption_runtime_status_type24_acks_sent" in text
    assert "authenticated_runtime_status_type23_sequences" in text
    assert "post_adoption_runtime_status_type24_sequences" in text
    assert "runtime_status_sequences_contiguous_from_one" in text
    assert "runtime_status_run_hash_bound_to_integrated_identity" in text
    assert "schema_version = 2" in text
    assert "legacy alias of canonical valid type-24 acknowledgements" in text
    assert 'type24_scope = "native controller post-adoption only"' in text
    assert "runtime_mode_policy_acceptance_proved = $false" in text
    assert "qml_rendering_proved = $false" in text
    assert 'operator_visual_acceptance = "not_claimed"' in text
    assert "$integratedEvidence.rtsp_publisher.helper_exit_code -eq 0" in text
    assert "$integratedEvidence.rtsp_publisher.eos_sent -eq $true" in text

    perception_index = text.index('"--perception-args"')
    result_index = text.index('"--result-out", $IntegratedResult')
    assert perception_index > result_index


def test_temporary_operator_keys_are_process_only_zeroed_and_never_serialized() -> None:
    text = _read_script()

    assert "$hmacBytes = New-Object byte[] 32" in text
    assert "$signingBytes = New-Object byte[] 32" in text
    assert "[Security.Cryptography.RandomNumberGenerator]::Create()" in text
    assert "$rng.GetBytes($hmacBytes)" in text
    assert "$rng.GetBytes($signingBytes)" in text
    assert "$rng.Dispose()" in text
    assert "$env:MULTIDETECT_OPERATOR_KEY = $temporaryHmac" in text
    assert "$env:MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX = $temporarySigningHex" in text
    assert (
        '[Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")'
        in text
    )
    assert "[Array]::Clear($hmacBytes, 0, $hmacBytes.Length)" in text
    assert "[Array]::Clear($signingBytes, 0, $signingBytes.Length)" in text
    assert "$temporaryHmac = $null" in text
    assert "$temporarySigningHex = $null" in text

    evidence_start = text.index("$evidence = [ordered]@{")
    evidence_end = text.index("$ProjectionMaximumNodesPerField = 20000", evidence_start)
    evidence = text[evidence_start:evidence_end]
    for secret_reference in (
        "$temporaryHmac",
        "$temporarySigningHex",
        "$hmacBytes",
        "$signingBytes",
        "$env:MULTIDETECT_OPERATOR_KEY",
        "$env:MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX",
    ):
        assert secret_reference not in evidence

    visual_claims = re.findall(
        r'operator_visual_acceptance\s*=\s*"([^"]+)"',
        evidence,
    )
    assert visual_claims
    assert set(visual_claims) == {"not_claimed"}


def test_aggregate_evidence_uses_bounded_json_safe_projection() -> None:
    text = _read_script()

    assert "function Write-Utf8NoBomText" in text
    assert "New-Object System.Text.UTF8Encoding($false)" in text
    assert "[IO.File]::WriteAllText($Path, [string]$Text, $encoding)" in text
    assert "function ConvertTo-JsonSafeEvidenceValue" in text
    assert "RuntimeHelpers]::GetHashCode($Value)" in text
    assert "return \"<cycle>\"" in text
    assert "MaximumCollectionItems = 4096" in text
    assert "<dictionary-truncated:$MaximumCollectionItems>" in text
    assert "<unsupported-type:$($type.FullName)>" in text
    assert "maximum_nodes = 20000" in text
    assert "<projection-truncated:$($ProjectionState.maximum_nodes)>" in text
    assert "return [string]::Copy([string]$Value)" in text
    assert "PSPath, PSProvider, ReadCount" in text
    assert "-ProjectionState $ProjectionState" in text
    assert "$ProjectionMaximumNodesPerField = 20000" in text
    assert "$evidence.GetEnumerator()" in text
    assert "mode = 'per_top_level_field'" in text
    assert "maximum_nodes_per_field = $ProjectionMaximumNodesPerField" in text
    assert "$jsonFieldParts = [Collections.Generic.List[string]]::new()" in text
    assert "$jsonValue = if ($null -eq $projectedValue)" in text
    assert "'null'" in text
    assert "ConvertTo-Json -InputObject $projectedValue -Depth 30 -Compress" in text
    assert "$evidenceJson = '{' + ($jsonFieldParts -join ',') + '}'" in text
    assert "Write-Utf8NoBomText -Path $EvidencePath -Text $evidenceJson" in text
    assert "Write-Output $evidenceJson" in text



def test_sensitive_environment_and_secret_buffers_have_one_outer_cleanup_guard() -> None:
    text = _read_script()

    guard_comment = text.index(
        "# This outer guard begins before the first secret allocation"
    )
    outer_try = text.index("try {", guard_comment)
    snapshot = text.index(
        "foreach ($name in $environmentNames) { $savedEnvironment[$name] = "
        '[Environment]::GetEnvironmentVariable($name, "Process") }',
        outer_try,
    )
    snapshot_complete = text.index("$environmentSnapshotComplete = $true", snapshot)
    hmac_allocation = text.index("$hmacBytes = New-Object byte[] 32", snapshot_complete)
    signing_allocation = text.index(
        "$signingBytes = New-Object byte[] 32", hmac_allocation
    )
    first_environment_mutation = text.index(
        '$env:MULTIDETECT_EXPECTED_RUNTIME_MODE = "SIM"', signing_allocation
    )
    post_mutation_preflight = text.index(
        "Assert-True $hilNativeVideoGrabExportEnabledBeforeLaunch",
        first_environment_mutation,
    )
    execution_try = text.index("try {\n    $startup = @'", post_mutation_preflight)
    terminal_gate = text.index("if (-not $evidence.gate_passed)", execution_try)
    outer_finally = text.index(
        "finally {\n    # Restore every captured process-level value", terminal_gate
    )

    assert outer_try < snapshot < snapshot_complete < hmac_allocation
    assert hmac_allocation < signing_allocation < first_environment_mutation
    assert first_environment_mutation < post_mutation_preflight < execution_try
    assert execution_try < terminal_gate < outer_finally

    cleanup = text[outer_finally:]
    assert "if ($environmentSnapshotComplete)" in cleanup
    assert (
        '[Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")'
        in cleanup
    )
    assert 'Remove-Item -LiteralPath "Env:$name"' in cleanup
    assert (
        "if ($null -ne $hmacBytes) { "
        "[Array]::Clear($hmacBytes, 0, $hmacBytes.Length) }"
        in cleanup
    )
    assert (
        "if ($null -ne $signingBytes) { "
        "[Array]::Clear($signingBytes, 0, $signingBytes.Length) }"
        in cleanup
    )
    assert cleanup.index("[Array]::Clear($hmacBytes") < cleanup.index(
        "[Array]::Clear($signingBytes"
    )
    assert "$temporaryHmac = $null" in cleanup
    assert "$temporarySigningHex = $null" in cleanup

    process_cleanup = text[
        text.index("finally {\n    foreach ($process in @(", execution_try) : outer_finally
    ]
    assert "$savedEnvironment" not in process_cleanup
    assert "[Array]::Clear($hmacBytes" not in process_cleanup
    assert "[Array]::Clear($signingBytes" not in process_cleanup


def test_recommended_long_run_parameters_flow_to_unclipped_wait_budgets() -> None:
    text = _read_script()
    param_end = text.index("\n)\n", text.index("param("))
    param_block = text[:param_end]

    assert "[int]$Frames = 900" in param_block
    assert "[int]$RouterDurationSeconds = 420" in param_block
    assert "[double]$FixedWingHoldSeconds = 12.0" in param_block
    assert "[int]$MissionTimeoutSeconds = 240" in param_block
    assert "[int]$MissionSetupAllowanceSeconds = 120" in param_block
    assert "[int]$MissionCaptureSequenceAdvance = 5" in param_block
    assert "[ValidateRange(120, 1800)]" in param_block
    assert "[ValidateRange(30, 600)]" in param_block
    assert "[ValidateRange(2, 30)]" in param_block
    assert "[ValidateRange(10, 600)]" in param_block
    assert "[ValidateRange(60, 300)]" in param_block
    assert "[ValidateRange(1, 60)]" in param_block

    assert '"--frame-count", "$Frames"' in text
    assert '"--duration-seconds", "$RouterDurationSeconds"' in text
    assert '"--fixed-wing-hold-seconds", "$FixedWingHoldSeconds"' in text
    assert "[Math]::Ceiling($Frames / 2.0) + 30" in text
    assert "($SensorStreamTimeoutSeconds + 45) * 1000" in text
    assert "(($RouterDurationSeconds + 10) * 1000)" in text
    assert "[Math]::Ceiling($FixedWingHoldSeconds + 43.0)" in text
    assert "[Math]::Ceiling(($FixedWingHoldSeconds + 58.0) * 1000.0)" in text
    assert (
        "$MissionReadyTimeoutSeconds = "
        "$MissionSetupAllowanceSeconds + $MissionTimeoutSeconds"
    ) in text
    assert "$MissionSetupAllowanceSeconds + $MissionTimeoutSeconds + 30" in text
    assert '"--mission-timeout-seconds", "$MissionTimeoutSeconds"' in text
    assert (
        "Wait-FileCreated -Path $FixedWingReady "
        "-TimeoutSeconds $FixedWingReadyTimeoutSeconds"
        in text
    )
    assert (
        "Wait-ProcessCompleted -Process $vtolProcess "
        "-TimeoutMilliseconds $VtolProcessWaitTimeoutMilliseconds"
        in text
    )
    assert "-Path $FixedWingReady -TimeoutSeconds 55" not in text
    assert "-Process $vtolProcess -TimeoutMilliseconds 70000" not in text
    assert (
        "Wait-FileCreated -Path $MissionActiveReady "
        "-TimeoutSeconds $MissionReadyTimeoutSeconds"
    ) in text
    assert (
        "Wait-ProcessCompleted -Process $missionProcess "
        "-TimeoutMilliseconds $MissionProcessWaitTimeoutMilliseconds"
    ) in text
    assert "-Path $MissionActiveReady -TimeoutSeconds 150" not in text
    assert "-Process $missionProcess -TimeoutMilliseconds 270000" not in text

def test_linux_container_startup_payload_is_normalized_to_lf() -> None:
    text = _read_script()

    here_string = text.index("$startup = @'")
    lf_normalization = text.index(
        '$startup = $startup.Replace("`r`n", "`n").Replace("`r", "`n")',
        here_string,
    )
    telemetry_substitution = text.index(
        '$startup = $startup.Replace("__PX4_TELEMETRY_PORT__", "$Px4TelemetryPort")',
        lf_normalization,
    )
    docker_run = text.index(
        '$run = Invoke-CapturedCommand -FilePath $Docker -ArgumentList @(',
        telemetry_substitution,
    )

    assert here_string < lf_normalization < telemetry_substitution < docker_run


def test_exact_qgc_pid_hwnd_capture_reads_only_the_owned_client_rectangle() -> None:
    text = _read_script()

    assert "$handle = $Process.MainWindowHandle" in text
    assert "$candidateHandles = @(" in text
    assert (
        "[MultiDetectWindow.Native]::GetVisibleTopLevelWindowsForProcess([uint32]$Process.Id)"
        in text
    )
    assert "$candidateHandles.Count -eq 1" in text
    assert "qgc-main-window-fallback-hwnd=0x{0:X16}" in text
    assert "GetWindowThreadProcessId(IntPtr hWnd, out uint processId)" in text
    assert "GetWindowThreadProcessId($handle, [ref]$owner)" in text
    assert "if ($owner -eq $Process.Id)" in text
    assert "GetWindowRect($window.Handle, [ref]$rect)" in text
    assert "PrintWindow" not in text
    assert "CaptureScreenRectangle(" in text
    assert "$destinationDc, $clientScreenRect.Left, $clientScreenRect.Top" in text
    assert "const uint SRCCOPY = 0x00CC0020" in text
    assert "const uint CAPTUREBLT = 0x40000000" in text
    assert "SRCCOPY | CAPTUREBLT" in text
    assert "BitBlt(destination, 0, 0, width, height" in text
    assert 'qgc_pid = $Process.Id' in text
    assert 'qgc_hwnd = ("0x{0:X}" -f $window.Handle.ToInt64())' in text
    assert "qgc_window_title = $title.ToString()" in text
    assert (
        'capture_method = "powershell_exact_pid_hwnd_native_bitblt_srccopy_captureblt_exact_client"'
        in text
    )
    assert 'capture_scope = "exact_qgc_client_only_no_full_desktop"' in text
    assert "full_desktop_capture_used = $false" in text
    assert "GetClientRect($window.Handle, [ref]$clientRect)" in text
    assert "ClientToScreen($window.Handle, [ref]$clientTopLeft)" in text
    assert "ClientToScreen($window.Handle, [ref]$clientBottomRight)" in text
    assert "MonitorFromRect([ref]$clientScreenRect, 0)" in text
    assert "SetThreadDpiAwarenessContext($perMonitorV2)" in text
    assert "GetDpiForWindow($window.Handle)" in text
    assert "GetExternalOccluders($window.Handle, $clientScreenRect" in text
    assert "$occluders.Count -eq 0" in text
    assert "$ownershipGridPoints -eq 25" in text
    assert "$foregroundDuring -eq $window.Handle" in text
    assert "AttachThreadInput(uint attachThreadId" in text
    assert "GetCurrentThreadId()" in text
    assert "BringWindowToTop(IntPtr hWnd)" in text
    assert "ForceForegroundWindow(IntPtr target, bool pulseAlt)" in text
    assert "PulseAltKey()" in text
    assert "$attempt -eq 1" in text
    assert "$foregroundRestored = $false" in text
    assert "$previousForegroundOwner = [uint32]$previousForegroundState.ProcessId" in text
    assert "Get-StableForegroundWindowState" in text
    assert "may legally retain a different foreground HWND" not in text
    assert (
        "Set-ExactForegroundWindow -Handle $window.Handle -ExpectedProcessId "
        "([uint32]$Process.Id)"
        in text
    )
    assert (
        "Set-ExactForegroundWindow -Handle $previousForeground "
        "-ExpectedProcessId $previousForegroundOwner"
        in text
    )
    assert "cursor_restored = $cursorRestored" in text
    assert "cursor_restoration_required = $false" in text
    assert "cursor_position_mutated_by_capture = $false" in text
    assert "SetCursorPos($cursorOutside.X, $cursorOutside.Y)" not in text
    assert "window_placement_restored = $placementRestored" in text
    assert "topmost_state_restored = $topmostRestored" in text
    assert "foreground_restored = $foregroundRestored" in text
    assert "dpi_context_restored = $dpiContextRestored" in text

    # Native PiP interaction remains exact-PID pointer input, not a second UI.
    assert "ClientToScreen($window.Handle, [ref]$screenPoint)" in text
    assert "SetCursorPos($screenPoint.X, $screenPoint.Y)" in text
    assert "WindowFromPoint($screenPoint)" in text
    assert "$hitOwner -eq $Process.Id" in text
    assert "SW_RESTORE or SW_SHOW; never resize/maximize" in text
    assert "ShowWindowAsync($Window.Handle, 3)" not in text
    assert "$foregroundBeforeClick -eq $window.Handle" in text
    assert "$foregroundOwnerBeforeClick -eq $Process.Id" in text
    assert "$cursorBeforeClick.X -eq $screenPoint.X" in text
    assert "QgcOriginalTopmost" in text
    assert "SendLeftClick()" in text
    assert "SendInput((uint)inputs.Length" in text
    assert "mouse_event(" not in text
    assert "PostMessage($window.Handle, 0x0201" not in text
    assert "PostMessage($window.Handle, 0x0202" not in text
    assert "settings_sha256" not in text  # evidence uses snapshot.sha256, never an invented value
    assert "$pipSettingsAfterPipSwap.sha256 -ne $pipSettingsBeforePipSwap.sha256" in text
    assert "$pipSettingsBeforePipSwap.is_pip_visible_explicit -eq $true" in text
    assert "PrimaryScreen" not in text
    assert "GetDesktopWindow" not in text


def test_native_videooutput_grab_binds_qquickitem_pixels_to_type24_advancement() -> None:
    text = _read_script()

    assert '"scripts\\validate_qgc_native_video_grab.py"' in text
    assert '"MULTIDETECT_HIL_VIDEO_GRAB_EXPORT"' in text
    assert '"MULTIDETECT_HIL_VIDEO_GRAB_PREFIX"' in text
    assert '$env:MULTIDETECT_HIL_VIDEO_GRAB_EXPORT = "1"' in text
    assert "$env:MULTIDETECT_HIL_VIDEO_GRAB_PREFIX = $NativeVideoGrabPrefix" in text
    assert "[IO.Path]::IsPathRooted($env:MULTIDETECT_HIL_VIDEO_GRAB_PREFIX)" in text
    assert "Wait-FileCreated -Path $NativeVideoPipSwappedCapture" in text
    assert "Wait-FileCreated -Path $NativeVideoFixedWingCapture" in text
    assert 'MULTIDETECT_HIL_VIDEO_GRAB .*"role":"pip-swapped"' in text
    assert 'MULTIDETECT_HIL_VIDEO_GRAB .*"role":"fixed-wing"' in text
    assert '"--capture-a", $NativeVideoPipSwappedCapture' in text
    assert '"--capture-b", $NativeVideoFixedWingCapture' in text
    assert '"--qgc-log", $QgcStdout' in text
    assert '"--qgc-log", $QgcStderr' in text
    assert '"--qgc-started-at-utc", "$($qgcStartTimeUtc.ToString(\'o\'))"' in text
    assert (
        "$nativeVideoGrabEvidence."
        "transport_bound_native_qquickitem_video_surface_proved -eq $true"
        in text
    )
    assert (
        "$nativeVideoGrabEvidence.native_qgc_ui_contract."
        "second_video_output_created -eq $false"
        in text
    )
    assert 'operator_visual_acceptance -eq "not_claimed"' in text

    # Exact-client BitBlt remains an independent compositor diagnostic.  It
    # cannot make a black GPU capture look like a successful native item grab.
    assert '"scripts\\validate_qgc_desktop_capture.py"' in text
    assert '"--capture-a", $PipSwappedScreenshot' in text
    assert '"--capture-b", $FixedWingScreenshot' in text
    assert '"--minimum-sequence-advance", "5"' in text
    assert '"--rtsp-streaming-proved", "--rtsp-decoding-proved"' in text
    assert "Get-LatestQgcRuntimeStatusSequence" in text
    assert "Wait-QgcRuntimeStatusSequence" in text
    assert "($pipSwappedSequenceAfterCapture + 5)" in text
    assert "if (Test-Path -LiteralPath $LiveVideoEvidencePath -PathType Leaf)" in text
    assert (
        "$liveVideoSurfaceEvidence."
        "transport_bound_visible_video_surface_proved -eq $true"
        not in text
    )
    assert "transport_bound_visible_video_surface_proved" in text
    assert "temporal_source_motion_proved" in text
    assert "exact_source_frame_identity_proved = $false" in text
    assert "qml_rendering_proved = $false" in text
    assert "complete_qml_rendering_semantics_proved = $false" in text
    assert 'operator_visual_acceptance = "not_claimed"' in text


def test_complete_vtol_mission_reuses_same_router_container_and_native_qgc_surface() -> None:
    text = _read_script()

    assert (
        r'$MissionAcceptanceScript = Join-Path $Root '
        r'"scripts\vtol_sitl_mission_acceptance.py"'
    ) in text
    assert '"--expected-container-id", $containerId' in text
    assert '"--expected-container-label", $PurposeLabel' in text
    assert '"--evaluator-port", "$EvaluatorPort"' in text
    assert '"--airframe-profile-sha256", $airframeProfileHash' in text
    assert (
        '"--runtime-descriptor-sha256", '
        '([string]$integratedStartEvidence.runtime_descriptor_hash)'
    ) in text
    assert (
        '"--runtime-provenance-sha256", '
        '([string]$integratedStartEvidence.run_identity_hash)'
    ) in text
    assert '"--mission-active-ready-file", $MissionActiveReady' in text
    assert '$MissionJournal = Join-Path $RunDirectory "vtol-mission.transaction.jsonl"' in text
    assert '"--mission-journal", $MissionJournal' in text
    assert '"--acknowledge-owned-disposable-sitl"' in text
    assert (
        'Get-JsonEventFromFile -Path $IntegratedStdout '
        '-Event "integrated_sim_qgc_started"'
    ) in text
    assert 'Wait-FileCreated -Path $MissionActiveReady' in text
    assert 'Save-QgcWindowCapture -Process $qgcProcess -Path $MissionActiveScreenshot' in text
    assert '$missionActiveReadyEvidence.auto_mission_active -eq $true' in text
    assert '$missionActiveReadyEvidence.in_air_observed -eq $true' in text
    assert '$missionActiveReadyEvidence.in_air_at_ready -eq $true' in text
    assert '$missionActiveReadyEvidence.mission_progress.observed -eq $true' in text
    assert (
        "$missionReadyQgcSequenceBaseline = "
        "Get-LatestQgcRuntimeStatusSequence"
    ) in text
    assert (
        "$missionCaptureQgcSequenceTarget = "
        "$missionReadyQgcSequenceBaseline + $MissionCaptureSequenceAdvance"
    ) in text
    assert (
        "$missionActiveSequenceAfterCapture -ge "
        "$missionCaptureQgcSequenceTarget"
    ) in text
    mission_ready_index = text.index(
        "$missionActiveReadyEvidence = Get-Content -LiteralPath $MissionActiveReady"
    )
    mission_baseline_index = text.index(
        "$missionReadyQgcSequenceBaseline = Get-LatestQgcRuntimeStatusSequence"
    )
    mission_capture_index = text.index(
        "Save-QgcWindowCapture -Process $qgcProcess -Path $MissionActiveScreenshot"
    )
    assert mission_ready_index < mission_baseline_index < mission_capture_index
    mission_phase = text[mission_ready_index:mission_capture_index]
    assert "$fixedWingSequenceAfterCapture + 5" not in mission_phase
    assert '$missionEvidence.gate_passed -eq $true' in text
    assert '$missionEvidence.transaction.final_state -eq "start_permitted"' in text
    assert '"authorized,uploading,uploaded,readback_verified,committed,start_permitted"' in text
    assert '$missionEvidence.protocol.upload_acknowledged -eq $true' in text
    assert '$missionEvidence.protocol.readback_hash_match -eq $true' in text
    assert '$missionEvidence.protocol.int_only -eq $true' in text
    assert 'uploaded_protocol_sha256 -eq $missionEvidence.protocol.readback_protocol_sha256' in text
    assert 'Compare-Object -ReferenceObject $expectedMissionSequences' in text
    assert '$missionEvidence.mission_journal' in text
    assert '$missionJournalEvidence.recovery_required -eq $false' in text
    assert '$missionJournalEvidence.state -eq "start_permitted"' in text
    assert '$missionJournalEvidence.record_count -ge ($missionItemCount + 6)' in text
    assert '$missionEvidence.flight.fixed_wing_observed -eq $true' in text
    assert '$missionEvidence.flight.multicopter_observed -eq $true' in text
    assert '$missionEvidence.flight.landed_observed -eq $true' in text
    assert '$missionEvidence.flight.terminal_disarmed -eq $true' in text
    assert 'maximum_distance_from_home_m -ge 35.0' in text
    assert 'maximum_relative_altitude_m -ge 10.0' in text
    assert '$routerEvidence.evaluator_mission_frames_forwarded -ge' in text
    assert '$routerEvidence.evaluator_mission_items_forwarded -ge $missionItemCount' in text
    assert '$routerEvidence.evaluator_auto_mission_mode_changes_forwarded -eq 1' in text
    assert '$routerEvidence.evaluator_mission_clear_all_allowed -eq $false' in text
    assert '$routerEvidence.qgc_mission_writes_allowed -eq $false' in text
    assert 'vtol_mission = $missionEvidence' in text
    assert 'vtol_mission_journal = $missionJournalEvidence' in text
    assert 'mission_journal = [ordered]@{' in text
    assert 'auto_mission_capture = $missionActiveCapture' in text
    assert 'operator_visual_acceptance -eq "not_claimed"' in text

    # The Mission helper is headless and attaches only after the first evaluator
    # process has exited; it never launches a second UI or a second container.
    assert text.index('$vtolExit = Wait-ProcessCompleted') < text.index(
        '$missionProcess = Start-Process'
    )
    assert text.count('"run", "-d", "--name", $ContainerName') == 1
    assert text.count('$qgcProcess = Start-Process -FilePath $QgcExecutable') == 1
    assert 'independent_simulator_window_created = $false' in text


def test_mission_failure_artifacts_are_ingested_before_stage_failure_propagates() -> None:
    text = _read_script()
    mission_start = text.index("$missionProcess = Start-Process")
    mission_stage_catch = text.index("$missionStageFailure = $_.Exception.Message", mission_start)
    result_ingest = text.index(
        "$missionEvidence = Get-Content -LiteralPath $MissionResult -Raw | ConvertFrom-Json",
        mission_stage_catch,
    )
    diagnostics = text.index("$missionDiagnostics = [ordered]@{", result_ingest)
    stage_throw = text.index(
        "if ($null -ne $missionStageFailure) { throw $missionStageFailure }",
        diagnostics,
    )

    assert mission_start < mission_stage_catch < result_ingest < diagnostics < stage_throw
    diagnostics_block = text[diagnostics:stage_throw]
    assert "result = Get-FileDiagnosticRecord -Path $MissionResult" in diagnostics_block
    assert "journal = Get-FileDiagnosticRecord -Path $MissionJournal" in diagnostics_block
    assert "stdout = Get-FileDiagnosticRecord -Path $MissionStdout" in diagnostics_block
    assert "stderr = Get-FileDiagnosticRecord -Path $MissionStderr" in diagnostics_block
    assert "result_loaded = ($null -ne $missionEvidence)" in diagnostics_block
    assert "helper_exit_code = $missionExitCode" in diagnostics_block
    assert "vtol_mission_diagnostics = $missionDiagnostics" in text

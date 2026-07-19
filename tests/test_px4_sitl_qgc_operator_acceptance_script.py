from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_px4_sitl_qgc_operator_acceptance.ps1"


def test_qgc_operator_acceptance_is_pinned_owned_and_loopback_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        "px4io/px4-sitl@sha256:"
        "bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216" in text
    )
    assert '$ContainerName = "multidetect-px4-qgc-operator-acceptance"' in text
    assert '$PurposeLabel = "px4-sitl-qgc-operator-acceptance"' in text
    assert "$QgcPort = 14669" in text
    assert "$RouterPort = 14667" in text
    assert "$SitlTelemetryPort = 14668" in text
    assert "$SitlInputPort = 18570" in text
    assert '$SitlInputBinding = "127.0.0.1:' in text
    assert "$ProtectedGroundStationPort = 14550" in text
    assert '"--network", "bridge"' in text
    assert '"--security-opt", "no-new-privileges:true"' in text
    assert '"--cap-drop", "ALL"' in text
    assert '"--rm"' in text
    assert '"PX4_SIM_MODEL=sihsim_airplane"' in text
    assert '"--device"' not in text
    assert '"--mount"' not in text


def test_qgc_runs_only_in_isolated_hil_with_metadata_only_jetson() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert '"--ISOLATED-HIL"' in upper
    assert '"--METADATA-ONLY"' in upper
    assert 'MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL = "1"' in upper
    assert 'MULTIDETECT_OPERATOR_HIL_AUTO_EXIT = "1"' in upper
    assert 'MULTIDETECT_OPERATOR_HIL_AUTO_EXERCISE = "1"' in upper
    assert 'MULTIDETECT_OPERATOR_HIL_REQUIRE_INITIAL_CONNECT = "1"' in upper
    assert 'MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID = "191"' in upper
    assert "AUTOPILOT_HEARTBEATS_SENT -EQ 0" in upper
    assert "PX4_AUTOPILOT_HEARTBEATS_FORWARDED -GT 0" in upper
    assert "TARGET_POOL_PAGE_COUNT -EQ 2" in upper
    assert "TARGET_POOL_TRACK_COUNT -EQ 3" in upper
    assert "TRACKING_METADATA_PACKETS_SENT -EQ 30" in upper
    assert "TRACKING_METADATA_RATE_HZ -GE 15" in upper
    assert "TRACKINGMETADATAPACKETS -GE 30" in upper
    assert "TRACKINGMETADATARATEHZ -GE 15.0" in upper
    assert "TARGET-POOL SNAPSHOT COMPLETE REVISION=3 TRACKS=3 PAGES=2" in upper
    assert "AUTHENTICATEDMETADATAPACKETS -GE 37" in upper
    assert "PAGED_TARGET_POOL_ATOMICALLY_ASSEMBLED = $TRUE" in upper
    assert "SOFTWARE HIL REQUIRES THE --ISOLATED-HIL" in upper
    assert "GET-PROCESSUDPENDPOINTS" in upper
    assert 'GET-CMAKECACHEVALUE -PATH $QGCCMAKECACHE -NAME "QT6_DIR"' in upper
    assert 'GET-CMAKECACHEVALUE -PATH $QGCCMAKECACHE -NAME "GSTREAMER_ROOT_DIR"' in upper
    assert "$ENV:QT_PLUGIN_PATH = $QTPLUGINS" in upper
    assert "$ENV:GST_PLUGIN_PATH = $GSTREAMERPLUGINS" in upper


def test_router_blocks_every_qgc_write_and_keeps_tunnel_off_px4() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert '"MULTIDETECT.PX4_SITL_QGC_ROUTER"' in upper
    assert "--ACKNOWLEDGE-OWNED-DISPOSABLE-SITL" in upper
    assert "QGC_FORBIDDEN_FRAMES_BLOCKED -EQ 0" in upper
    assert "FILE_MUTATING_FTP_OPCODES_FORWARDED -EQ 0" in upper
    assert "SYSTEM_TIME_FRAMES_FORWARDED -EQ 0" in upper
    assert "DIAGNOSTIC_PREARM_CHECK_ONLY -EQ $TRUE" in upper
    assert "OPERATOR_TUNNEL_FORWARDED_TO_PX4 -EQ $FALSE" in upper
    assert "PARAMETER_WRITES_ENABLED = $FALSE" in upper
    assert "MISSION_WRITES_ENABLED = $FALSE" in upper
    assert "FLIGHT_CONTROL_WRITES_ENABLED = $FALSE" in upper
    assert "PHYSICAL_PAYLOAD_CONTROL_ENABLED = $FALSE" in upper
    assert "PARAMETER_OVERRIDES = 0" in upper
    assert "ARM_COMMANDS = 0" in upper
    assert "MODE_COMMANDS = 0" in upper
    assert "MISSION_UPLOADS = 0" in upper
    assert "PX4-PARAM" not in upper
    assert "PX4-COMMANDER" not in upper


def test_acceptance_cleans_processes_container_ports_and_secret() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert "GET-CRASHDUMPSIGNATURES" in upper
    assert "PROTECTEDPORTUNCHANGED" in upper
    assert "STOP-PROCESS -ID $PROCESS.ID" in upper
    assert '"STOP", "--TIMEOUT", "1", $CONTAINERNAME' in upper
    assert "WAIT-OWNEDCONTAINERREMOVED" in upper
    assert 'SETENVIRONMENTVARIABLE($NAME, $SAVEDENVIRONMENT[$NAME], "PROCESS")' in upper
    assert "$TEMPORARYKEY = $NULL" in upper
    assert "DOCKER RM" not in upper
    assert "REMOVE-ITEM" not in upper


def test_native_probe_stderr_is_captured_without_bypassing_exit_code_checks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert "$PREVIOUSERRORACTIONPREFERENCE = $ERRORACTIONPREFERENCE" in upper
    assert '$ERRORACTIONPREFERENCE = "CONTINUE"' in upper
    assert "$EXITCODE = $LASTEXITCODE" in upper
    assert "$ERRORACTIONPREFERENCE = $PREVIOUSERRORACTIONPREFERENCE" in upper


def test_hil_key_generation_supports_windows_powershell_and_zeroes_key_bytes() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert "RANDOMNUMBERGENERATOR]::CREATE()" in upper
    assert ".GETBYTES($TEMPORARYKEYBYTES)" in upper
    assert ".DISPOSE()" in upper
    assert "RANDOMNUMBERGENERATOR]::GETBYTES(32)" not in upper
    assert "[ARRAY]::CLEAR($TEMPORARYKEYBYTES, 0, $TEMPORARYKEYBYTES.LENGTH)" in upper


def test_empty_docker_arrays_do_not_turn_null_into_a_false_mount_or_device() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert "$DEVICES = @($RECORD.HOSTCONFIG.DEVICES | WHERE-OBJECT { $NULL -NE $_ })" in upper
    assert "$MOUNTS = @($RECORD.MOUNTS | WHERE-OBJECT { $NULL -NE $_ })" in upper
    assert "NO_DEVICES = $DEVICES.COUNT -EQ 0" in upper
    assert "NO_MOUNTS = $MOUNTS.COUNT -EQ 0" in upper


def test_short_lived_redirected_process_handles_are_pinned_for_exit_codes() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "$null = $routerProcess.Handle" in text
    assert "$null = $driverProcess.Handle" in text
    assert "$null = $qgcProcess.Handle" in text

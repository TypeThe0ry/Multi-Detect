from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_px4_sitl_datalink_loss_acceptance.ps1"


def test_datalink_loss_acceptance_is_digest_pinned_and_loopback_isolated() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        "px4io/px4-sitl@sha256:"
        "bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216" in text
    )
    assert '$ContainerName = "multidetect-px4-datalink-loss-acceptance"' in text
    assert '$PurposeLabel = "px4-sitl-datalink-loss-acceptance"' in text
    assert "$SitlPort = 14652" in text
    assert "$GcsInputPort = 18570" in text
    assert '$GcsInputBinding = "127.0.0.1:$($GcsInputPort):$($GcsInputPort)/udp"' in text
    assert "$ProtectedGroundStationPort = 14550" in text
    assert '"--network", "bridge"' in text
    assert '"--security-opt", "no-new-privileges:true"' in text
    assert '"--cap-drop", "ALL"' in text
    assert '"-p", $GcsInputBinding' in text
    assert '"--rm"' in text
    assert '"PX4_SIM_MODEL=sihsim_airplane"' in text
    assert '"--device"' not in text
    assert '"--mount"' not in text


def test_datalink_parameters_are_legal_and_confined_to_owned_sitl() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert '@("COM_DL_LOSS_T", "5")' in text
    assert '@("NAV_DLL_ACT", "1")' in text
    assert '@("COM_DL_LOSS_T", "1")' not in text
    assert '"EXEC", $CONTAINERNAME, "/OPT/PX4/BIN/PX4-PARAM", "SET"' in upper
    assert '"--OWNERSHIP-PROFILE", "DATALINK_LOSS"' in upper
    assert "--ACKNOWLEDGE-OWNED-DISPOSABLE-SITL" in upper
    assert "EXACT_HOST_PORT_BOUNDARY" in upper
    assert "PARAMETER_WRITES_CONFINED_TO_OWNED_CONTAINER = $TRUE" in upper
    heartbeat_start = text.index("$initialHeartbeatProcess = Start-SitlHeartbeat")
    nav_datalink_override = text.index('@("NAV_DLL_ACT", "1")')
    assert heartbeat_start < nav_datalink_override


def test_datalink_loss_acceptance_proves_mission_hold_and_reconnect() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert '$preLossTelemetry.latest.flight_mode -eq "MISSION"' in text
    assert '$postLossTelemetry.latest.flight_mode -eq "LOITER"' in text
    assert '$postReconnectTelemetry.latest.flight_mode -eq "LOITER"' in text
    assert '-Field "gcs_connection_lost" -Expected $false' in text
    assert '-Field "gcs_connection_lost" -Expected $true' in text
    assert '-Field "failsafe" -Expected $true' in text
    assert '"px4_sitl_gcs_heartbeat_finished"' in text
    assert "gcs_reconnect_flag_cleared = $true" in text
    assert "hold_loiter_mode_observed = $true" in text
    assert "hold_retained_after_reconnect = $true" in text


def test_datalink_loss_acceptance_preserves_hardware_and_cleanup_boundaries() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert "MESSAGES_TRANSMITTED -EQ 0" in upper
    assert "MULTIDETECT_MAVLINK_MESSAGES_TRANSMITTED = 0" in upper
    assert "REAL_V6X_CONTACTED = $FALSE" in upper
    assert "PHYSICAL_RELEASE_POSSIBLE = $FALSE" in upper
    assert "PHYSICAL_PAYLOAD_ACTIONS = 0" in upper
    assert "PROTECTED_GROUND_STATION_PORT_UNCHANGED" in upper
    assert '"EXEC", $CONTAINERNAME, "/OPT/PX4/BIN/PX4-COMMANDER", "DISARM", "-F"' in upper
    assert '"STOP", "--TIMEOUT", "1", $CONTAINERNAME' in upper
    assert "STOP-PROCESS -ID $HEARTBEATPROCESS.ID" in upper
    assert "DOCKER RM" not in upper
    assert "REMOVE-ITEM" not in upper

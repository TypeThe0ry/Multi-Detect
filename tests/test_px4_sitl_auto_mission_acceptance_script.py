from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_px4_sitl_auto_mission_acceptance.ps1"


def test_auto_mission_acceptance_is_digest_pinned_and_port_isolated() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        "px4io/px4-sitl@sha256:"
        "bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216" in text
    )
    assert '$ContainerName = "multidetect-px4-auto-mission-acceptance"' in text
    assert '$PurposeLabel = "px4-sitl-auto-mission-acceptance"' in text
    assert "$SitlPort = 14652" in text
    assert "$ProtectedGroundStationPort = 14550" in text
    assert "$SitlPort -ne $ProtectedGroundStationPort" in text
    assert '"--rm"' in text
    assert '"PX4_SIM_MODEL=sihsim_airplane"' in text


def test_auto_mission_commands_are_confined_to_new_container() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert '"EXEC", $CONTAINERNAME, "/OPT/PX4/BIN/PX4-PARAM", "SET"' in upper
    assert '"EXEC", $CONTAINERNAME, "/OPT/PX4/BIN/PX4-COMMANDER", "ARM", "-F"' in upper
    assert '"EXEC", $CONTAINERNAME, "/OPT/PX4/BIN/PX4-COMMANDER", "MODE", "AUTO:MISSION"' in upper
    assert '"EXEC", $CONTAINERNAME, "/OPT/PX4/BIN/PX4-COMMANDER", "DISARM", "-F"' in upper
    assert "--ACKNOWLEDGE-OWNED-DISPOSABLE-SITL" in upper
    assert "DOCKER RM" not in upper
    assert "REMOVE-ITEM" not in upper
    assert "UDPOUT:" not in upper


def test_multidetect_stays_receive_only_and_patrol_has_no_payload_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert '"-U"' in upper
    assert '"-M"' in upper
    assert '"MULTIDETECT.CLI"' in upper
    assert "-FILEPATH $PYTHON" in upper
    assert '"--OBSERVE-PIXHAWK-LIFECYCLE"' in upper
    assert '"--TASK-AREA-MISSION-SEQUENCE", "1"' in upper
    assert '"--ALLOWED-AUTO-MODE", "MISSION"' in upper
    assert "MESSAGES_TRANSMITTED -EQ 0" in upper
    assert "AUTHORIZATIONS -EQ 0" in upper
    assert "SIMULATED_PAYLOAD_CYCLES -EQ 0" in upper
    assert "--SIMULATE-PAYLOAD-CYCLE" not in upper
    assert "--INERT-PAYLOAD-HIL" not in upper
    assert "REAL_V6X_CONTACTED = $FALSE" in upper


def test_auto_mission_acceptance_proves_sequence_wait_and_safe_cleanup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert '"current=0, required=1"' in text
    assert "mission_sequence -ge 1" in text
    assert "movementDistanceM -gt 20.0" in text
    assert "protected_port_ownership_unchanged" in text
    assert "Stop-Process -Id $liveProcess.Id" in text
    assert '"stop", "--timeout", "1", $ContainerName' in text
    assert "The mission uses 3-5 m HIL-only altitudes" in text
    assert "this run is not aerodynamic" in text

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_px4_sitl_readonly_acceptance.ps1"


def test_px4_sitl_acceptance_is_digest_pinned_and_port_isolated() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        "px4io/px4-sitl@sha256:"
        "bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216" in text
    )
    assert "[int]$HostPort = 14650" in text
    assert "$ProtectedGroundStationPort = 14550" in text
    assert "$HostPort -ne $ProtectedGroundStationPort" in text
    assert '"PX4_SIM_MODEL=sihsim_airplane"' in text
    assert '"--rm"' in text
    assert '"--entrypoint", "/bin/sh"' in text


def test_px4_sitl_acceptance_keeps_pixhawk_and_payload_paths_read_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert '"PIXHAWK-CHECK"' in upper
    assert '"--OBSERVE-PIXHAWK-LIFECYCLE"' in upper
    assert '"--REQUIRE-PIXHAWK-OPERATIONAL-STATE"' in upper
    assert '"--ALLOW-SYNTHETIC-HIL-MODEL"' in upper
    assert "MESSAGES_TRANSMITTED -EQ 0" in upper
    assert "PHYSICAL_RELEASE_SUPPORTED -EQ $FALSE" in upper
    assert "SIMULATED_PAYLOAD_CYCLES -EQ 0" in upper
    assert "PARAM_SET" not in upper
    assert "COMMAND_LONG" not in upper
    assert "MISSION_ITEM" not in upper
    assert "--SIMULATE-PAYLOAD-CYCLE" not in upper
    assert "DOCKER RM" not in upper
    assert "REMOVE-ITEM" not in upper


def test_px4_sitl_acceptance_has_negative_freshness_and_owned_cleanup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "Fresh-link gate unexpectedly passed after PX4 SITL stopped." in text
    assert "Stopped-SITL result did not fail closed." in text
    assert "finally {" in text
    assert '"stop", "--timeout", "1", $ContainerName' in text
    assert "Container name already exists; refusing to replace it" in text
    assert "protected_ground_station_port_unchanged" in text


def test_optional_armed_patrol_is_explicit_and_confined_to_owned_container() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$IncludeInContainerArmedPatrolHil" in text
    assert '"exec", $ContainerName, "/opt/px4/bin/px4-commander", "arm", "-f"' in text
    assert '"exec", $ContainerName, "/opt/px4/bin/px4-commander", "mode", "auto:loiter"' in text
    assert '"exec", $ContainerName, "/opt/px4/bin/px4-commander", "disarm", "-f"' in text
    assert "owned disposable Docker PX4 process only" in text
    assert "auto_mission_validated = $false" in text
    assert "application_flight_commands_enabled = $false" in text
    assert '"--allowed-auto-mode", "LOITER"' in text
    assert "Positive patrol lifecycle observation transmitted a Pixhawk message." in text

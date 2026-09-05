from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.actuator_safety import (
    ActuatorSafetyConfig,
    ActuatorSafetyError,
    ActuatorSafetySession,
    ServoChannelSpec,
    run_actuator_safety_software_acceptance,
    verify_actuator_safety_evidence,
)


def test_software_acceptance_covers_direction_limits_and_recovery() -> None:
    report = run_actuator_safety_software_acceptance()

    assert report["event"] == "actuator_safety_software_acceptance_finished"
    assert report["runtime_mode"] == "HIL"
    assert report["release_label"].startswith("0.3.0-pre.")
    assert report["gate_passed"] is True
    assert report["software_only"] is True
    assert report["physical_outputs_present"] is False
    assert all(report["checks"].values())
    assert report["active_command"]["pwm_us"] == {
        "roll_left": 1800,
        "roll_right": 1200,
        "pitch": 1350,
    }
    assert report["cancel"]["pwm_us"] == {
        "roll_left": 1500,
        "roll_right": 1500,
        "pitch": 1500,
    }
    assert report["lost_lock"]["restored_entry_mode"] == "POSCTL"
    assert report["watchdog"]["neutral_frame_count"] == 1
    assert report["watchdog"]["second_watchdog_returned_none"] is True


def test_software_acceptance_is_deterministic() -> None:
    first = run_actuator_safety_software_acceptance()
    second = run_actuator_safety_software_acceptance()
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )


def test_verifier_rejects_tampered_pwm_or_claims() -> None:
    tampered = copy.deepcopy(run_actuator_safety_software_acceptance())
    tampered["active_command"]["pwm_us"]["roll_left"] = 1500
    tampered["claims"]["hardware_acceptance"] = "pass"

    result = verify_actuator_safety_evidence(tampered)

    assert result["gate_passed"] is False
    assert any("active_command" in reason for reason in result["reasons"])
    assert any("claims.hardware_acceptance" in reason for reason in result["reasons"])


def test_session_rejects_out_of_range_and_wrong_target_commands() -> None:
    config = ActuatorSafetyConfig((ServoChannelSpec("roll", 1, 1100, 1500, 1900),))
    session = ActuatorSafetySession(config)
    session.bind(target_id="target-1", entry_mode="AUTO")

    with pytest.raises(ActuatorSafetyError, match=r"within \[-1, 1\]"):
        session.command(target_id="target-1", commands={"roll": 1.1})
    with pytest.raises(ActuatorSafetyError, match="target binding"):
        session.command(target_id="target-2", commands={"roll": 0.0})


def test_failsafe_is_latched_and_neutral_is_one_shot() -> None:
    config = ActuatorSafetyConfig((ServoChannelSpec("roll", 1, 1100, 1500, 1900),))
    session = ActuatorSafetySession(config)
    session.bind(target_id="target-1", entry_mode="AUTO")
    session.command(target_id="target-1", commands={"roll": 0.5})

    assert session.watchdog() is not None
    assert session.watchdog() is None
    with pytest.raises(ActuatorSafetyError, match="failsafe state"):
        session.command(target_id="target-1", commands={"roll": 0.0})


def test_acceptance_script_writes_and_verifies_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "actuator-safety.json"
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_actuator_safety_acceptance.py"
    verifier = (
        Path(__file__).resolve().parents[1] / "scripts" / "verify_actuator_safety_evidence.py"
    )
    run = subprocess.run(
        [sys.executable, str(script), "--out", str(evidence)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert "generated_at_utc" not in document
    checked = subprocess.run(
        [sys.executable, str(verifier), str(evidence)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["gate_passed"] is True


def test_actuator_schema_is_tracked_and_strict() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "schemas"
        / "actuator-safety-evidence.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["physical_outputs_present"]["const"] is False

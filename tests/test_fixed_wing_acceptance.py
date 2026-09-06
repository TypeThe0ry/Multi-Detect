from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from multidetect.fixed_wing_acceptance import run_fixed_wing_software_acceptance


def test_fixed_wing_software_acceptance_closes_target_and_control_gates() -> None:
    report = run_fixed_wing_software_acceptance()

    assert report["event"] == "fixed_wing_software_acceptance_passed"
    assert report["gate_passed"] is True
    assert report["state_path"] == ["DET", "TRK", "LCK", "TGT"]
    assert report["runtime_mode"] == "HIL"
    assert report["software_only"] is True

    target_pool = report["target_pool"]
    assert target_pool["frame_states"] == ["detected", "detected", "tracking"]
    assert target_pool["tracking_actionable"] is True
    assert target_pool["primary_track_id"] == target_pool["tracking_track_id"]

    confirmation = report["target_confirmation"]
    assert confirmation["challenge_issued"] is True
    assert confirmation["challenge_bound_to_selection"] is True
    assert confirmation["confirmation_accepted"] is True
    assert confirmation["flight_control_enabled"] is False

    attitude = report["attitude_control"]
    assert attitude["aim_states"] == ["prestream", "prestream", "active"]
    assert attitude["offboard_requested_once"] is True
    assert attitude["mode_requests"] == ["OFFBOARD", "AUTO"]
    assert abs(attitude["active_roll_deg"]) <= attitude["roll_limit_deg"]
    assert abs(attitude["active_pitch_deg"]) <= attitude["pitch_limit_deg"]
    assert attitude["quaternion_norm"] == 1.0

    assert report["pilot_cancel"]["reason"] == "pilot_input_cancelled"
    assert report["pilot_cancel"]["latched_until_new_binding"] is True
    assert report["target_loss_recovery"]["restored_entry_mode"] == "POSCTL"

    actuator = report["actuator_safety"]
    assert actuator["gate_passed"] is True
    assert actuator["software_only"] is True
    assert actuator["physical_outputs_present"] is False
    assert actuator["checks"]["direction_mapping"] is True
    assert actuator["checks"]["absolute_limits"] is True
    assert actuator["checks"]["lost_lock_restores_entry_mode"] is True

    ui_contract = report["ui_contract"]
    assert ui_contract["surface"] == "native_qgc_flyview"
    assert ui_contract["second_window"] is False
    assert ui_contract["second_video_surface"] is False
    assert ui_contract["custom_theme"] is False
    assert ui_contract["operator_visual_acceptance"] == "not_claimed"

    physical = report["physical_actuators"]
    assert physical["pixhawk_link_opened"] is False
    assert physical["mavlink_messages_sent"] == 0
    assert physical["physical_output_enabled"] is False


def test_fixed_wing_acceptance_cli_writes_json_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "fixed-wing.json"
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_fixed_wing_software_acceptance.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--out", str(evidence)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["event"] == "fixed_wing_software_acceptance_passed"
    assert report["generated_at_utc"].endswith("Z")

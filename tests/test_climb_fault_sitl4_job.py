from __future__ import annotations

import json
from pathlib import Path

from scripts.prepare_climb_fault_sitl4_suite import build_suite


def test_climb_fault_sitl4_expects_fault_escalation_without_weakening_other_checks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = root / "configs/onboard/suite-main-r21idr3-r24plan.json"
    output = tmp_path / "suite.json"

    build_suite(source, output)
    suite = json.loads(output.read_text(encoding="utf-8"))

    assert [mission["id"] for mission in suite["missions"]] == ["operator-abort"]
    mission = suite["missions"][0]
    assert mission["sim_ground_events"] == [{"t_s": 0, "event": "abort"}]
    assert mission["sim_climb_timeout_injection"] is True
    assert mission["sim_pre_abort_descent_altitude_m"] == 1.5
    supervisor = mission["expect"]["supervisor"]
    assert supervisor["final_decision"] == "land_now"
    assert supervisor["reason"] == "climb_failed_suspected_loss_of_control"
    assert supervisor["return_home_started"] is True
    assert supervisor["onboard_log_exists"] is True
    assert supervisor["logged_decisions"] == ["return_now"]
    assert supervisor["logged_reasons"] == ["explicit operator abort"]
    assert supervisor["abort_hover_before_return"] is True
    assert supervisor["original_mission_completed"] is False

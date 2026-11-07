from __future__ import annotations

import json
from pathlib import Path

from scripts.check_onboard_climb_px4_evidence import (
    MISSING,
    ONBOARD,
    PX4,
    check_climb_fault_evidence,
)
from scripts import eval_onboard_loop_campaign as campaign
from scripts import run_mission_suite as campaign_suite


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts/benchmarks/onboard-climb-fault-sitl3-20261107T172724Z-1332460"


def _classes(report):
    return {point["id"]: point["class"] for point in report["points"]}


def test_real_sitl3_artifact_preserves_px4_onboard_boundary():
    report = check_climb_fault_evidence(ARTIFACT)
    classes = _classes(report)

    assert classes == {
        "px4_boot_time_advancing": PX4,
        "low_altitude_airborne_before_abort": PX4,
        "two_climb_timeouts_no_altitude_gain": ONBOARD,
        "no_third_climb": ONBOARD,
        "failure_event_alert": ONBOARD,
        "land_now_handoff": ONBOARD,
        "land_mode_switch_ack": PX4,
        "land_mode": PX4,
        "fresh_landed_state_1": PX4,
        "disarm": PX4,
    }
    assert report["counts"] == {PX4: 6, ONBOARD: 4, MISSING: 0}
    assert report["read_only"] is True


def test_missing_px4_land_evidence_is_not_inferred_from_onboard_logs(tmp_path):
    root = tmp_path / "episode"
    (root / "mission_log").mkdir(parents=True)
    (root / "mavlink.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"message_type": "LOCAL_POSITION_NED", "time_boot_ms": 1, "z": -1.0},
                {"message_type": "LOCAL_POSITION_NED", "time_boot_ms": 2, "z": -1.1},
                {"message_type": "EXTENDED_SYS_STATE", "landed_state": 2},
            ]
        ),
        encoding="utf-8",
    )
    (root / "mission_log/mission.jsonl").write_text(
        json.dumps({"kind": "escalation", "action": "land_now"}) + "\n", encoding="utf-8"
    )

    classes = _classes(check_climb_fault_evidence(root))
    assert classes["px4_boot_time_advancing"] == PX4
    assert classes["land_now_handoff"] == ONBOARD
    assert classes["land_mode_switch_ack"] == MISSING
    assert classes["land_mode"] == MISSING
    assert classes["fresh_landed_state_1"] == MISSING
    assert classes["disarm"] == MISSING


def test_checker_does_not_write_artifact(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    report = check_climb_fault_evidence(root)
    after = sorted(path.relative_to(root) for path in root.rglob("*"))
    assert before == after == []
    assert all(point["class"] == MISSING for point in report["points"])


def test_campaign_attaches_checker_only_when_climb_injection_is_enabled(tmp_path):
    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "steps.jsonl").write_text("{}\n", encoding="utf-8")

    assert campaign._climb_fault_evidence(episode, enabled=False) is None
    report = campaign._climb_fault_evidence(episode, enabled=True)

    assert report is not None
    assert report["checker"] == "onboard-climb-px4-evidence-checker"
    assert report["read_only"] is True
    assert report["counts"] == {PX4: 0, ONBOARD: 0, MISSING: 10}
    assert not any(path.name == "evidence.json" for path in episode.rglob("*"))


def test_real_sitl3_evidence_can_render_in_offline_suite_report():
    evidence = check_climb_fault_evidence(ARTIFACT)
    mission = {
        "id": "operator-abort",
        "world": "gen_cnt2_w0003",
        "expect": {"skills": ["monitor", "return_home"]},
        "episodes": 1,
    }
    episode = {
        "world": "gen_cnt2_w0003",
        "status": "complete",
        "mission_plan_skills": ["monitor", "return_home"],
        "climb_fault_evidence": evidence,
    }

    table = campaign_suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})
    markdown = campaign_suite.render_markdown(table)
    assert table["missions"][0]["climb_fault_evidence"]["counts"] == {
        PX4: 6,
        ONBOARD: 4,
        MISSING: 0,
    }
    assert "land_now_handoff" in markdown
    assert "land_mode_switch_ack" in markdown

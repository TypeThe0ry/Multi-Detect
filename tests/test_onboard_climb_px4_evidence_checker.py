from __future__ import annotations

import json
from pathlib import Path

from scripts.check_onboard_climb_px4_evidence import (
    MISSING,
    ONBOARD,
    PX4,
    check_climb_fault_evidence,
)


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

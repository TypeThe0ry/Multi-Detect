from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import sign_document
from scripts.onboard_main import _admit_package
from scripts.run_onboard_fault_campaign import (
    FaultEvent,
    expected_outcome,
    schedule_faults,
    verdict,
)


def test_fault_schedule_is_sorted_and_rejects_overlap():
    events = schedule_faults([
        FaultEvent("pose_stale", 2, 0.5),
        FaultEvent("camera_drop", 0, 0.5),
    ])
    assert [event.fault for event in events] == ["camera_drop", "pose_stale"]
    with pytest.raises(ValueError, match="overlapping"):
        schedule_faults([FaultEvent("camera_drop", 0, 2), FaultEvent("pose_stale", 1, 1)])


@pytest.mark.parametrize(
    ("fault", "expected"),
    [("camera_freeze", "conservative"), ("battery_sag", "return_now"),
     ("offboard_loss", "refuse"), ("gcs_heartbeat_recovery", "continue")],
)
def test_expected_outcome_from_policy(fault, expected):
    assert expected_outcome(fault, {"action": "continue_then_return"}) == expected


def test_verdict_checks_reason_and_deadline():
    assert verdict("conservative", "hold", "stale pose", elapsed_s=0.2)["passed"]
    assert not verdict("return_now", "hold", "stale battery", elapsed_s=0.2)["passed"]
    assert not verdict("continue", "continue", "ok", elapsed_s=4, deadline_s=3)["passed"]


def test_package_entry_accepts_signature_and_rejects_bad_signature(tmp_path):
    from test_mission_supervisor import document

    key = b"bench-hmac-key"
    package_path = tmp_path / "mission.json"
    key_path = tmp_path / "operator.key"
    key_path.write_bytes(key)
    package_path.write_text(
        json.dumps(sign_document(document(), key, algorithm="hmac-sha256")), encoding="utf-8"
    )
    args = SimpleNamespace(
        mission_package=package_path, operator_key=key_path, task=None, mode="real"
    )
    package, options = _admit_package(args)
    assert package.mission_id == "sortie-1" and options["task_text"] == package.plan.goal
    key_path.write_bytes(b"wrong-key")
    with pytest.raises(ValueError, match="admission refused"):
        _admit_package(args)


def test_package_loader_refuses_missing_reference_and_invalid_policy():
    from test_mission_supervisor import document

    raw = document()
    raw["plan"]["steps"][0]["params"]["target_ref"] = "poi:missing"
    with pytest.raises(ValueError, match="unknown onboard reference"):
        load_mission_package(raw)
    raw = document()
    raw["link_loss_policy"] = {"action": "not-a-policy"}
    with pytest.raises(ValueError, match="unknown link-loss policy"):
        load_mission_package(raw)

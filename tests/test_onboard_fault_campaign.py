from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import sign_document
from scripts.onboard_main import _admit_package
from scripts.run_onboard_fault_campaign import (
    CampaignSession,
    FaultEvent,
    _build_session,
    expected_outcome,
    run_campaign,
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


def _mutant_factory(mutator):
    """Build a real session, then install one deliberately defective controller."""

    def factory(package, event, work_dir) -> CampaignSession:
        session = _build_session(package, event, work_dir)
        mutator(session)
        return session

    return factory


def _always_hold(session: CampaignSession) -> None:
    session.controller.decide = lambda _pose: ("hold", "mutant always-hold runtime")


def _always_return(session: CampaignSession) -> None:
    session.controller.decide = lambda _pose: ("return_now", "mutant always-return runtime")


def _ignores_policy(session: CampaignSession) -> None:
    # A policy-ignoring runtime continues while a continue-then-return package
    # is past its configured silence timeout.
    session.controller.decide = lambda _pose: (
        "continue", "mutant ignored package policy"
    )


def _relaxes_latched_policy(session: CampaignSession) -> None:
    """Release a terminal supervisor action after it has been observed."""

    original = session.controller.decide

    def decide(pose):
        action, reason = original(pose)
        if action in {"return_now", "land_now"}:
            session.controller.latched = None
            return "continue", "mutant released latched policy"
        return action, reason

    session.controller.decide = decide


def _no_op_runtime(session: CampaignSession) -> None:
    # Stop the genuine runtime before replacing it so even startup setpoints
    # cannot be mistaken for execution evidence from the mutant.
    session.runtime.close()

    class NoOpRuntime:
        hazard = None

        def run(self, _duration_s):
            return None

        def report(self):
            return {"setpoints": {"sent_count": 0}, "autonomy_steps": 0}

        def close(self):
            return None

    session.runtime = NoOpRuntime()


@pytest.mark.parametrize(
    "mutator", [_no_op_runtime, _always_hold, _always_return, _ignores_policy,
                _relaxes_latched_policy],
    ids=["no-op", "always-hold", "always-return", "ignores-package-policy",
         "releases-latched-policy"],
)
def test_campaign_rejects_defective_runtime(mutator, tmp_path):
    from test_mission_supervisor import document

    key = b"campaign-mutation-key"
    package_path = tmp_path / "mission.json"
    key_path = tmp_path / "operator.key"
    key_path.write_bytes(key)
    package_path.write_text(
        json.dumps(sign_document(document("continue_then_return", timeout=0.4), key,
                                  algorithm="hmac-sha256")),
        encoding="utf-8",
    )
    report = run_campaign(
        work_dir=tmp_path / mutator.__name__,
        package=package_path,
        operator_key=key_path,
        runtime_factory=_mutant_factory(mutator),
        duration_s=0.75,
    )
    assert report["passed"] is False
    assert any(not row["passed"] for row in report["scenarios"])

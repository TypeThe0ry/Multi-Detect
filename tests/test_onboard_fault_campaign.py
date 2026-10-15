from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import sign_document
from scripts.onboard_main import _admit_package
from scripts.run_onboard_fault_campaign import (
    FAULTS,
    CampaignSession,
    FaultEvent,
    PositionSetpoint,
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

    key = b"bench-hmac-key!!"
    package_path = tmp_path / "mission.json"
    key_path = tmp_path / "operator.key"
    key_path.write_text("hmac:" + key.hex(), encoding="ascii")
    package_path.write_text(
        json.dumps(sign_document(document(), key, algorithm="hmac-sha256")), encoding="utf-8"
    )
    args = SimpleNamespace(
        mission_package=package_path, operator_key=key_path, task=None, mode="real"
    )
    package, options = _admit_package(args)
    assert package.mission_id == "sortie-1" and options["task_text"] == package.plan.goal
    key_path.write_text("hmac:" + (b"wrong-key-material!!").hex(), encoding="ascii")
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
    if session.controller.event.fault == "gcs_heartbeat_loss":
        session.controller.decide = lambda _pose: (
            "continue", "mutant released latched policy"
        )
        return

    original = session.controller.decide

    def decide(pose):
        action, reason = original(pose)
        if action in {"return_now", "land_now"}:
            session.controller.latched = None
            return "continue", "mutant released latched policy"
        return action, reason

    session.controller.decide = decide


def _prefault_rtl_then_continue(session: CampaignSession) -> None:
    """Inject a pre-fault RTL, then incorrectly continue after GCS loss."""
    if session.controller.event.fault == "gcs_heartbeat_loss":
        session.vehicle.rtl()
        session.controller.decide = lambda _pose: (
            "continue", "mutant continued after pre-fault rtl"
        )


def _out_of_area_after_fence(session: CampaignSession) -> None:
    """Emit an unsafe post-fault setpoint only in the fence scenario."""
    if session.controller.event.fault != "fence_approach":
        return
    original = session.vehicle.send_setpoint
    area = session.vehicle.area

    def send(setpoint):
        if session.controller.fault_active():
            setpoint = PositionSetpoint(
                area["north_max_m"] + 1.0, setpoint.east_m, setpoint.down_m,
                setpoint.yaw_deg
            )
        original(setpoint)

    session.vehicle.send_setpoint = send


def _released_latch(session: CampaignSession) -> None:
    """Release a GCS-loss terminal latch after it is set."""
    if session.controller.event.fault != "gcs_heartbeat_loss":
        return
    original = session.controller.decide

    def decide(pose):
        action, reason = original(pose)
        if action == "return_now":
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
    ("mutator", "expected_failed"), [
        (_no_op_runtime, set(FAULTS)),
        (_always_hold, {"gcs_heartbeat_loss", "battery_sag", "battery_stale",
                        "gcs_heartbeat_recovery"}),
        (_always_return, {"camera_freeze", "camera_drop", "pose_stale",
                          "slow_model_timeout", "slow_model_crash", "detector_exception",
                          "gcs_heartbeat_loss", "gcs_heartbeat_recovery", "offboard_loss",
                          "battery_sag", "battery_stale", "home_change"}),
        (_ignores_policy, {"gcs_heartbeat_loss", "gcs_heartbeat_recovery",
                           "battery_sag", "battery_stale"}),
        (_relaxes_latched_policy, {"gcs_heartbeat_loss"}),
        (_prefault_rtl_then_continue, {"gcs_heartbeat_loss"}),
        (_out_of_area_after_fence, {"fence_approach"}),
        (_released_latch, {"gcs_heartbeat_loss"}),
    ],
    ids=["no-op", "always-hold", "always-return", "ignores-package-policy",
         "releases-latched-policy", "prefault-rtl-then-continue",
         "fence-out-of-area", "released-latch"],
)
def test_campaign_rejects_defective_runtime(mutator, expected_failed, tmp_path):
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
            duration_s=5.5,
    )
    assert report["passed"] is False
    failed = {row["scenario"] for row in report["scenarios"] if not row["passed"]}
    assert expected_failed <= failed

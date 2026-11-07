from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import sign_document
from scripts.onboard_main import _admit_package
from scripts.onboard_policy import validate_sim_ground_events
from scripts.collect_sim_teacher_episodes import SimClimbTimeoutInjector
from scripts.collect_sim_teacher_episodes import sim_pre_abort_descent_altitude
from multidetect.sim_teacher.executor import ExecutionResult
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts.run_onboard_fault_campaign import (
    FAULTS,
    CampaignSession,
    FaultEvent,
    PositionSetpoint,
    _build_session,
    expected_outcome,
    run_campaign,
    run_climb_failure_fixture,
    schedule_faults,
    verdict,
)
from scripts.run_mission_suite import merge_config


def test_fault_schedule_is_sorted_and_rejects_overlap():
    events = schedule_faults(
        [
            FaultEvent("pose_stale", 2, 0.5),
            FaultEvent("camera_drop", 0, 0.5),
        ]
    )
    assert [event.fault for event in events] == ["camera_drop", "pose_stale"]
    with pytest.raises(ValueError, match="overlapping"):
        schedule_faults([FaultEvent("camera_drop", 0, 2), FaultEvent("pose_stale", 1, 1)])


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("camera_freeze", "conservative"),
        ("battery_sag", "return_now"),
        ("offboard_loss", "refuse"),
        ("gcs_heartbeat_recovery", "continue"),
    ],
)
def test_expected_outcome_from_policy(fault, expected):
    assert expected_outcome(fault, {"action": "continue_then_return"}) == expected


def test_verdict_checks_reason_and_deadline():
    assert verdict("conservative", "hold", "stale pose", elapsed_s=0.2)["passed"]
    assert not verdict("return_now", "hold", "stale battery", elapsed_s=0.2)["passed"]
    assert not verdict("continue", "continue", "ok", elapsed_s=4, deadline_s=3)["passed"]


def test_two_climb_timeouts_escalate_through_policy_with_durable_failure(tmp_path):
    report = run_climb_failure_fixture(tmp_path / "fixture")
    assert report["passed"], report["checks"]
    assert all(report["checks"].values())
    summary = report["final_summary"]
    reason = "climb_failed_suspected_loss_of_control"
    assert summary["failure_class"] == summary["terminal_reason"] == reason
    assert summary["terminal_action"] == "land_now"
    assert summary["recovery_history"][-1]["state"] == "failed"
    assert reason in summary["report"]["alerts"]
    assert summary["recovery_failure"]["land_execution"] == "unverified"
    assert report["px4_ack"] is report["px4_mode"] is report["px4_landed_state"] is None
    records = [json.loads(line) for line in (tmp_path / "fixture/mission.jsonl").read_text(encoding="utf-8").splitlines()]
    finals = [row["summary"] for row in records if row["kind"] == "final_summary"]
    assert [row["terminal_action"] for row in finals] == ["return_now", "land_now"]
    events = [row for row in records if row["kind"] == reason]
    assert len(events) == 1 and events[0]["consecutive_timeouts"] == 2


def test_climb_fixture_rejects_missing_policy_outcome_feedback(tmp_path, monkeypatch):
    from scripts.onboard_policy import OnboardPolicy

    monkeypatch.setattr(OnboardPolicy, "report_primitive_outcome", lambda *args, **kwargs: False)
    report = run_climb_failure_fixture(tmp_path / "mutant")
    assert not report["passed"]
    assert not report["checks"]["no_third_climb"]
    assert not report["checks"]["land_escalation"]
    assert not report["checks"]["final_failure"]


def test_sim_climb_injector_is_sim_only_and_matches_first_two_pure_climbs():
    with pytest.raises(ValueError, match="SIM onboard collector"):
        SimClimbTimeoutInjector(enabled=True, runtime_mode="real", teacher="onboard")
    with pytest.raises(ValueError, match="SIM onboard collector"):
        SimClimbTimeoutInjector(enabled=True, runtime_mode="sim", teacher="expert_search")
    injector = SimClimbTimeoutInjector(enabled=True, runtime_mode="sim", teacher="onboard")
    climb = ActionPrimitive.translate(0, 0, 15)
    before = {
        "pose": {"local_ned_m": [0.0, 0.0, -0.8]},
        "flight_state": {"landed_state": 2},
        "landed": False,
    }
    assert injector.matches(climb, active_skill="return_home", before=before)
    assert not injector.matches(
        climb,
        active_skill="return_home",
        before={**before, "pose": {"local_ned_m": [0.0, 0.0, -3.0]}},
    )
    assert not injector.matches(
        climb,
        active_skill="return_home",
        before={**before, "landed": True},
    )
    assert not injector.matches(
        climb,
        active_skill="return_home",
        before={"pose": before["pose"], "flight_state": {"landed_state": None}},
    )
    assert not injector.matches(climb, active_skill="monitor", before=before)
    assert not injector.matches(ActionPrimitive.translate(1, 0, 15), active_skill="return_home", before=before)
    assert not injector.matches(ActionPrimitive.translate(0, 0, -2), active_skill="return_home", before=before)

    class Executor:
        def __init__(self):
            self.actions = []
            self.criterion = SimpleNamespace(timeout_s=20.0)

        def execute(self, action):
            self.actions.append(action)
            return ExecutionResult("success", 20.0, ack={"held_position": True})

    executor = Executor()
    for _ in range(2):
        assert injector.matches(climb, active_skill="return_home", before=before)
        result = injector.execute(executor, climb)
        assert result.status == "timeout"
        assert result.ack["sim_fault_injection"] == "return_home_climb_timeout"
    assert all(action.kind == "hold" and action.seconds == 20.0 for action in executor.actions)
    assert not injector.matches(climb, active_skill="return_home", before=before)
    assert injector.injected == 2


def test_sim_climb_injector_defaults_off():
    injector = SimClimbTimeoutInjector(enabled=False, runtime_mode="sim", teacher="onboard")
    before = {"pose": {"local_ned_m": [0.0, 0.0, -0.8]}, "flight_state": {"landed_state": 2}, "landed": False}
    assert not injector.matches(ActionPrimitive.translate(0, 0, 10), active_skill="return_home", before=before)


def test_suite_mission_selects_sim_climb_injection_only_for_that_mission():
    defaults = {"mission": "from_text"}
    enabled = merge_config(defaults, {"id": "fault-case", "sim_climb_timeout_injection": True})
    disabled = merge_config(defaults, {"id": "normal-case"})
    assert enabled["sim_climb_timeout_injection"] is True
    assert "sim_climb_timeout_injection" not in disabled


def test_sim_climb_fault_scenario_requires_a_valid_low_altitude_pre_abort_descent():
    scenario = merge_config(
        {"mission": "from_text"},
        {
            "id": "operator-abort-low-climb-fault",
            "sim_climb_timeout_injection": True,
            "sim_pre_abort_descent_altitude_m": 1.5,
            "sim_ground_events": [{"t_s": 0, "event": "abort"}],
            "mission_package": {},
        },
    )
    assert scenario["sim_ground_events"] == [{"t_s": 0, "event": "abort"}]
    assert validate_sim_ground_events(scenario) == scenario["sim_ground_events"]
    assert sim_pre_abort_descent_altitude(scenario, teacher="onboard") == 1.5
    assert sim_pre_abort_descent_altitude(
        {"sim_climb_timeout_injection": True}, teacher="onboard"
    ) is None
    with pytest.raises(ValueError, match="requires SIM onboard climb injection"):
        sim_pre_abort_descent_altitude(
            {"sim_pre_abort_descent_altitude_m": 1.5}, teacher="onboard"
        )
    with pytest.raises(ValueError, match="requires SIM onboard climb injection"):
        sim_pre_abort_descent_altitude(scenario, teacher="expert_search")
    with pytest.raises(ValueError, match=r"\[0.5, 2.5\)"):
        sim_pre_abort_descent_altitude(
            {**scenario, "sim_pre_abort_descent_altitude_m": 2.5}, teacher="onboard"
        )


def test_package_entry_accepts_signature_and_rejects_bad_signature(tmp_path):
    from test_mission_supervisor import document

    key = b"bench-hmac-key!!"
    package_path = tmp_path / "mission.json"
    key_path = tmp_path / "operator.key"
    key_path.write_text("hmac:" + key.hex(), encoding="ascii")
    if os.name != "nt":
        key_path.chmod(0o600)
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
    session.controller.decide = lambda _pose: ("continue", "mutant ignored package policy")


def _relaxes_latched_policy(session: CampaignSession) -> None:
    """Release a terminal supervisor action after it has been observed."""
    if session.controller.event.fault == "gcs_heartbeat_loss":
        session.controller.decide = lambda _pose: ("continue", "mutant released latched policy")
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
            "continue",
            "mutant continued after pre-fault rtl",
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
                area["north_max_m"] + 1.0, setpoint.east_m, setpoint.down_m, setpoint.yaw_deg
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
    ("mutator", "expected_failed"),
    [
        (_no_op_runtime, set(FAULTS)),
        (
            _always_hold,
            {"gcs_heartbeat_loss", "battery_sag", "battery_stale", "gcs_heartbeat_recovery"},
        ),
        (
            _always_return,
            {
                "camera_freeze",
                "camera_drop",
                "pose_stale",
                "slow_model_timeout",
                "slow_model_crash",
                "detector_exception",
                "gcs_heartbeat_loss",
                "gcs_heartbeat_recovery",
                "offboard_loss",
                "battery_sag",
                "battery_stale",
                "home_change",
            },
        ),
        (
            _ignores_policy,
            {"gcs_heartbeat_loss", "gcs_heartbeat_recovery", "battery_sag", "battery_stale"},
        ),
        (_relaxes_latched_policy, {"gcs_heartbeat_loss"}),
        (_prefault_rtl_then_continue, {"gcs_heartbeat_loss"}),
        (_out_of_area_after_fence, {"fence_approach"}),
        (_released_latch, {"gcs_heartbeat_loss"}),
    ],
    ids=[
        "no-op",
        "always-hold",
        "always-return",
        "ignores-package-policy",
        "releases-latched-policy",
        "prefault-rtl-then-continue",
        "fence-out-of-area",
        "released-latch",
    ],
)
def test_campaign_rejects_defective_runtime(mutator, expected_failed, tmp_path):
    from test_mission_supervisor import document

    key = b"campaign-mutation-key"
    package_path = tmp_path / "mission.json"
    key_path = tmp_path / "operator.key"
    key_path.write_bytes(key)
    package_path.write_text(
        json.dumps(
            sign_document(
                document("continue_then_return", timeout=0.4), key, algorithm="hmac-sha256"
            )
        ),
        encoding="utf-8",
    )
    report = run_campaign(
        work_dir=tmp_path / mutator.__name__,
        package=package_path,
        operator_key=key_path,
        runtime_factory=_mutant_factory(mutator),
        timing_scale=0.25,
        evaluation_padding_s=0.05,
    )
    assert report["passed"] is False
    failed = {row["scenario"] for row in report["scenarios"] if not row["passed"]}
    assert expected_failed <= failed

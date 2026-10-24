"""CPU-only preflight and policy tests with a controlled host clock."""

from __future__ import annotations

import copy
import json

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_package import POLICIES, RISK_DEFAULTS, load_mission_package
from multidetect.autonomy.mission_security import sign_document
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.autonomy.onboard_mission_log import OnboardMissionLog
from multidetect.sim_teacher.primitives import ActionPrimitive


def document(policy="continue", timeout=30):
    return {
        "mission_id": "sortie-1",
        "risk_level": "low",
        "link_loss_policy": {"action": policy, "timeout_s": timeout},
        "home": [0, 0],
        "places": {"poi:gate": [40, 0]},
        "zones": {},
        "routes": {},
        "oversight_steps": [],
        "heartbeat_timeout_s": 5,
        "operating_area": {
            "north_min_m": -100,
            "north_max_m": 100,
            "east_min_m": -100,
            "east_max_m": 100,
            "min_altitude_m": 1,
            "max_altitude_m": 60,
        },
        "budget": {
            "time_s": 86400,
            "reserve_time_s": 60,
            "energy_wh": 300,
            "reserve_energy_wh": 30,
        },
        "plan": {
            "plan_version": 1,
            "goal": "visit gate then home",
            "report": ["positions"],
            "steps": [
                {
                    "id": "gate",
                    "skill": "goto",
                    "params": {"target_ref": "poi:gate"},
                    "on_success": "home",
                    "on_fail": "home",
                },
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
        },
    }


def build(tmp_path, policy="continue", *, doc=None):
    package = load_mission_package(doc or document(policy))
    now = [100.0]

    def factory(text, **kwargs):
        return OnboardLoop(
            camera=CameraModel(540, 540, 640, 480, 1280, 960),
            search_action=lambda pose, yaw: ActionPrimitive.hold(1),
            segment_clear=lambda pose, n, e: True,
            config=LoopConfig(**kwargs),
        )

    executor = MissionExecutor(
        package.plan, loop_factory=factory, refs=package.refs, config=ExecutorConfig()
    )
    log = OnboardMissionLog(tmp_path, package.mission_id)
    supervisor = MissionSupervisor(executor, package, log=log, clock=lambda: now[0])
    return supervisor, now, log


def event(supervisor, kind, **kwargs):
    return supervisor.ground_event({"mission_id": "sortie-1", "type": kind, **kwargs})


def signed_event(private_key, sequence, now, kind="heartbeat", **fields):
    payload = {
        "mission_id": "sortie-1",
        "type": kind,
        "sequence": sequence,
        "timestamp": now,
        "timestamp_version": 1,
        "timestamp_domain": "monotonic",
    }
    payload.update(fields)
    return sign_document(payload, private_key)


def secured_supervisor(tmp_path, *, max_sequence_jump=1024):
    supervisor, now, log = build(tmp_path)
    key = b"mission-test-key-01"
    supervisor._security_key = key
    supervisor.max_sequence_jump = max_sequence_jump
    return supervisor, now, log, key


@pytest.mark.parametrize(
    "policy,early,late",
    [
        ("continue", "continue", "continue"),
        ("continue_then_return", "continue", "return_now"),
        ("hold_then_return", "hold", "return_now"),
        ("return_now", "return_now", "return_now"),
        ("land_now", "land_now", "land_now"),
    ],
)
def test_all_policies_without_any_ground_station(tmp_path, policy, early, late):
    s, now, _ = build(tmp_path, policy)
    now[0] += 6
    assert s.evaluate() == early
    now[0] += 30
    assert s.evaluate() == late


def test_envelope_violation_preserves_latched_land_precedence(tmp_path):
    s, _now, _ = build(tmp_path, "continue")
    s._escalate("land_now", "existing terminal land")
    assert s.report_envelope_violation("no_fly") == "land_now"
    assert s.evaluate() == "land_now"
    records = [json.loads(line) for line in s.log.path.read_text(encoding="utf-8").splitlines()]
    assert any(record["kind"] == "envelope_violation" for record in records)


def test_envelope_violation_events_are_rate_limited(tmp_path):
    s, now, _ = build(tmp_path, "continue")
    s.report_envelope_violation("no_fly")
    s.report_envelope_violation("no_fly")
    records = [json.loads(line) for line in s.log.path.read_text(encoding="utf-8").splitlines()]
    assert sum(record["kind"] == "envelope_violation" for record in records) == 1
    now[0] += 5.0
    s.report_envelope_violation("no_fly")
    records = [json.loads(line) for line in s.log.path.read_text(encoding="utf-8").splitlines()]
    assert sum(record["kind"] == "envelope_violation" for record in records) == 2


def test_continue_survives_hours_of_silence(tmp_path):
    s, now, _ = build(tmp_path)
    for hours in range(1, 13):
        now[0] = 100 + hours * 3600
        assert s.evaluate() == "continue"


def test_silence_and_hold_deadlines(tmp_path):
    s, now, _ = build(tmp_path, "hold_then_return")
    assert event(s, "heartbeat")
    now[0] = 104
    assert s.evaluate() == "continue"
    now[0] = 105
    assert s.evaluate() == "hold"
    now[0] = 134.9
    assert s.evaluate() == "hold"
    now[0] = 135
    assert s.evaluate() == "return_now"
    assert event(s, "heartbeat")
    assert s.evaluate() == "return_now"


def test_reconnect_releases_hold_but_pause_needs_resume(tmp_path):
    s, now, _ = build(tmp_path, "hold_then_return")
    now[0] += 6
    assert s.evaluate() == "hold"
    assert event(s, "heartbeat")
    assert s.evaluate() == "continue"
    assert event(s, "pause")
    assert s.evaluate() == "hold"
    assert event(s, "heartbeat")
    assert s.evaluate() == "hold"
    assert event(s, "resume")
    assert s.evaluate() == "continue"


def test_pause_timeout_while_link_is_live(tmp_path):
    s, now, _ = build(tmp_path, "continue_then_return")
    assert event(s, "pause")
    now[0] += 29
    assert event(s, "heartbeat")
    assert s.evaluate() == "hold"
    now[0] += 1
    assert s.evaluate() == "return_now"


def test_abort_brakes_then_uses_uploaded_home_and_never_resumes(tmp_path):
    s, now, log = build(tmp_path)
    assert event(s, "abort")
    action, info = s.next_action(pose_ned=(30, 0, -20), yaw_deg=180)
    assert action.kind == "hold"
    assert info["supervisor_action"] == "return_now"
    action, _ = s.next_action(pose_ned=(30, 0, -20), yaw_deg=180)
    assert action.kind == "translate" and action.forward_m > 0
    assert s._recovery.home == (0, 0)
    assert event(s, "resume")
    assert s.evaluate() == "return_now"
    assert "explicit operator abort" in log.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("operator", POLICIES)
@pytest.mark.parametrize("update", POLICIES)
def test_policy_update_can_only_tighten(tmp_path, operator, update):
    s, now, _ = build(tmp_path, operator)
    accepted = event(s, "policy_update", policy={"action": update, "timeout_s": 20})
    assert accepted == (POLICIES.index(update) >= POLICIES.index(operator))
    assert POLICIES.index(s.policy) >= POLICIES.index(operator)
    now[0] += 100
    action = s.evaluate()
    if operator == "land_now":
        assert action == "land_now"
    elif operator == "return_now":
        assert action in {"return_now", "land_now"}


def test_timed_update_cannot_extend_deadline(tmp_path):
    s, now, _ = build(tmp_path, "continue_then_return")
    assert not event(s, "policy_update", policy={"action": s.policy, "timeout_s": 99})
    assert event(s, "policy_update", policy={"action": "hold_then_return", "timeout_s": 99})
    now[0] += 30
    assert s.evaluate() == "return_now"


@pytest.mark.parametrize(
    "estimate,reason",
    [
        ({"remaining_time_s": 60}, "time"),
        ({"remaining_energy_wh": 30}, "energy"),
        ({"low_confidence_count": 3}, "perception"),
    ],
)
def test_reserve_and_health_escalation_stays_latched(tmp_path, estimate, reason):
    s, now, _ = build(tmp_path)
    s.update_estimates(**estimate)
    assert s.evaluate() == "return_now"
    assert reason in s.reason
    s.update_estimates(remaining_time_s=9999, remaining_energy_wh=300, low_confidence_count=0)
    assert s.evaluate() == "return_now"
    assert event(s, "heartbeat")
    assert s.evaluate() == "return_now"


def test_elapsed_time_budget_is_enforced_without_telemetry(tmp_path):
    s, now, _ = build(tmp_path)
    now[0] += 86400 - 60
    assert s.evaluate() == "return_now"
    assert "time" in s.reason


def test_bad_perception_can_never_downgrade_land(tmp_path):
    s, now, _ = build(tmp_path, "land_now")
    s.update_estimates(low_confidence_count=4, remaining_energy_wh=0)
    now[0] += 6
    assert s.evaluate() == "land_now"


def test_oversight_step_and_high_risk_zone_entry(tmp_path):
    doc = document()
    doc["oversight_steps"] = ["gate"]
    s, now, _ = build(tmp_path / "step", doc=doc)
    now[0] += 6
    assert s.evaluate() == "return_now"
    assert "gate" in s.reason
    doc["oversight_steps"] = []
    doc["zones"] = {
        "zone:people": {
            "centre_ne": [10, 0],
            "radius_m": 2,
            "risk_tag": "people",
            "oversight_required": True,
        }
    }
    s, now, _ = build(tmp_path / "zone", doc=doc)
    now[0] += 6
    assert s.evaluate(pose_ned=(0, 0, -20)) == "continue"
    action, info = s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    assert action.kind == "hold"
    assert info["supervisor_action"] == "return_now"
    assert "zone:people" in info["supervisor_reason"]


def test_zone_tag_alone_does_not_override_operator(tmp_path):
    doc = document()
    doc["zones"] = {"zone:people": {"centre_ne": [0, 0], "radius_m": 2, "risk_tag": "people"}}
    s, now, _ = build(tmp_path, doc=doc)
    now[0] += 6
    assert s.evaluate(pose_ned=(0, 0, -20)) == "continue"


@pytest.mark.parametrize("kind", ["heartbeat", "abort", "pause", "resume", "policy_update"])
def test_mission_id_required_for_all_events(tmp_path, kind):
    s, now, _ = build(tmp_path)
    assert not s.ground_event({"type": kind, "mission_id": "different"})
    assert s.evaluate() == "continue"
    assert s.last_heartbeat is None


def test_heartbeat_timestamp_rejects_future_and_stale(tmp_path):
    s, now, _ = build(tmp_path)
    assert not event(s, "heartbeat", timestamp=101)
    assert not event(s, "heartbeat", timestamp=99)
    now[0] = 110
    assert event(s, "heartbeat", timestamp=109)
    assert not event(s, "heartbeat", timestamp=108)


@pytest.mark.parametrize("missing", ["home", "link_loss_policy", "budget", "operating_area"])
def test_preflight_requires_onboard_inputs(missing):
    doc = document()
    del doc[missing]
    with pytest.raises(ValueError):
        load_mission_package(doc)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["plan"]["steps"][0]["params"].update(target_ref="poi:unknown"),
        lambda d: d.update(home=[1000, 0]),
        lambda d: d["budget"].update(reserve_energy_wh=300),
        lambda d: d["plan"]["steps"][0]["params"].update(altitude_m=90),
        lambda d: d.update(oversight_steps=["unknown"]),
        lambda d: d["link_loss_policy"].update(action="automatic"),
        lambda d: d["places"].update({"poi:bad": [float("nan"), 0]}),
        lambda d: d["zones"].update({"zone:bad": {"centre_ne": [99, 0], "radius_m": 10}}),
    ],
)
def test_preflight_rejects_invalid_packages(change):
    doc = document()
    change(doc)
    with pytest.raises(ValueError):
        load_mission_package(doc)


def test_text_package_validates_compiled_refs_before_launch(tmp_path):
    doc = document()
    plan = doc.pop("plan")
    doc["task_text"] = "Visit the gate then return."
    package = load_mission_package(doc)
    assert package.plan is None
    from multidetect.autonomy.mission_plan import validate_mission_plan

    plan["steps"][0]["params"]["target_ref"] = "poi:missing"
    with pytest.raises(ValueError, match="unknown onboard"):
        package.validate_plan(validate_mission_plan(plan))


def test_risk_suggestions_do_not_replace_operator_choice():
    assert RISK_DEFAULTS == {
        "low": "continue",
        "medium": "continue_then_return",
        "high": "return_now",
    }
    for risk in RISK_DEFAULTS:
        doc = document("continue")
        doc["risk_level"] = risk
        package = load_mission_package(doc)
        assert package.document["link_loss_policy"]["action"] == "continue"


def test_package_file_and_continuous_offline_results(tmp_path):
    doc = document()
    path = tmp_path / "package.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert load_mission_package(path).mission_id == "sortie-1"
    s, now, log = build(tmp_path / "log", doc=copy.deepcopy(doc))
    s.next_action(pose_ned=(10, 0, -20), yaw_deg=0)
    records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["kind"] == "mission_started"
    assert any(r["kind"] == "decision" and r["reason"] for r in records)
    assert records[-1]["kind"] == "execution" and "report" in records[-1]
    s.finish()
    summary = json.loads(log.summary_path.read_text(encoding="utf-8"))
    assert summary["summary"]["status"] == "stopped"


def test_recovery_not_done_when_original_executor_finishes(tmp_path):
    s, now, _ = build(tmp_path)
    s.executor.done = True
    assert event(s, "abort")
    assert not s.done
    s.next_action(pose_ned=(30, 0, -20), yaw_deg=180)
    assert not s.done
    # The abort ends the mission: its one summary says so; recovery is not "completed".
    summary = json.loads(s.log.summary_path.read_text(encoding="utf-8"))["summary"]
    assert summary["status"] == "ended_early"
    assert summary["terminal_action"] == "return_now"
    assert summary["terminal_reason"] == "explicit operator abort"


def test_clock_regression_rejected(tmp_path):
    s, now, _ = build(tmp_path)
    now[0] += 10
    s.evaluate()
    now[0] -= 1
    with pytest.raises(ValueError, match="clock"):
        s.evaluate()


@pytest.mark.parametrize("policy", ["continue_then_return", "hold_then_return"])
def test_heartbeat_cannot_postpone_pause_deadline(tmp_path, policy):
    s, now, _ = build(tmp_path, policy)
    assert event(s, "pause")
    now[0] += 20
    assert event(s, "heartbeat")
    now[0] += 10
    assert s.evaluate() == "return_now"


def test_signed_replay_high_water_survives_restart_without_session_nonce(tmp_path):
    s, now, _log, key = secured_supervisor(tmp_path)
    assert s.ground_event(signed_event(key, 1, now[0]))
    state = tmp_path / "ground-replay-state.json"
    assert "session_nonce" not in json.loads(state.read_text(encoding="utf-8"))

    restarted, restarted_now, _log, restarted_key = secured_supervisor(tmp_path)
    assert restarted_key == key
    assert not restarted.ground_event(signed_event(key, 1, restarted_now[0]))
    assert restarted.ground_event(signed_event(key, 2, restarted_now[0]))


def test_sequence_wrap_and_bounded_forward_jump(tmp_path):
    s, now, _log, key = secured_supervisor(tmp_path, max_sequence_jump=4)
    state = tmp_path / "ground-replay-state.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "mission_id": "sortie-1",
                "last_sequence": 2**32 - 1,
                "session_nonce": "previous-session",
            }
        ),
        encoding="utf-8",
    )
    s._replay_state_loaded = False
    assert s.ground_event(signed_event(key, 0, now[0]))
    assert not s.ground_event(signed_event(key, 10, now[0], session_nonce=s.session_nonce))


def test_unix_heartbeat_uses_local_receipt_time_for_liveness(tmp_path):
    s, now, _log, key = secured_supervisor(tmp_path)
    unix_now = 1_800_000_000.0
    s.wall_clock = lambda: unix_now
    heartbeat = sign_document(
        {
            "mission_id": "sortie-1",
            "type": "heartbeat",
            "sequence": 1,
            "timestamp": unix_now,
            "timestamp_version": 1,
            "timestamp_domain": "unix",
        },
        key,
    )
    assert s.ground_event(heartbeat)
    assert s.last_heartbeat == now[0]
    assert s.connected
    now[0] += 6
    s._time()
    assert not s.connected


def test_signed_command_requires_versioned_timestamp_domain(tmp_path):
    s, now, _log, key = secured_supervisor(tmp_path)
    legacy = sign_document(
        {
            "mission_id": "sortie-1",
            "type": "heartbeat",
            "sequence": 1,
            "timestamp": now[0],
        },
        key,
    )
    assert not s.ground_event(legacy)
    assert s.last_heartbeat is None


def test_unsigned_events_are_rate_limited_before_state_mutation(tmp_path):
    s, now, _log = build(tmp_path)
    s.rate_limit = 1
    assert event(s, "pause")
    s.paused_at = None
    assert not event(s, "pause")
    assert s.paused_at is None


def test_replay_commit_failure_rolls_back_abort_state(tmp_path, monkeypatch):
    s, now, log, key = secured_supervisor(tmp_path)

    def fail_commit(path, document):
        raise OSError("disk unavailable")

    monkeypatch.setattr(s, "_atomic_json", fail_commit)
    assert not s.ground_event(signed_event(key, 1, now[0], kind="abort"))
    assert s._terminal is None
    assert s.action == "continue"
    assert "explicit operator abort" not in log.path.read_text(encoding="utf-8")


def test_remaining_time_estimate_ages_without_new_telemetry(tmp_path):
    s, now, _ = build(tmp_path)
    s.update_estimates(remaining_time_s=70)
    now[0] += 9
    assert s.evaluate() == "continue"
    now[0] += 1
    assert s.evaluate() == "return_now"


def test_stronger_policy_cannot_postpone_short_return_deadline(tmp_path):
    s, now, _ = build(tmp_path, doc=document("continue_then_return", timeout=2))
    assert not event(s, "policy_update", policy={"action": "hold_then_return", "timeout_s": 0})
    now[0] += 5
    assert s.evaluate() == "return_now"


def test_completed_return_recovery_can_escalate_to_land(tmp_path):
    s, now, _ = build(tmp_path, "return_now")
    now[0] += 6
    s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    assert s.done
    assert event(s, "policy_update", policy={"action": "land_now"})
    assert s.evaluate() == "land_now"
    assert not s.done
    s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    assert s._recovery_kind == "land"
    s.finish()
    # Exactly one final summary: written when the return latched; the later
    # land escalation is its own (downlinked) escalation record.
    records = [json.loads(line) for line in s.log.path.read_text(encoding="utf-8").splitlines()]
    summaries = [r for r in records if r["kind"] == "final_summary"]
    assert len(summaries) == 1
    assert summaries[0]["summary"]["terminal_action"] == "return_now"
    escalations = [r["action"] for r in records if r["kind"] == "escalation"]
    assert escalations[-1] == "land_now"


def test_supervisor_return_recovery_uses_visibility_graph(tmp_path):
    s, _now, _ = build(tmp_path, "return_now")
    s.executor.no_fly_zones = (((10.0, 0.0), 2.0),)
    s.action = "return_now"
    recovery = s._recover((20.0, 0.0, -20.0))
    path = recovery._return_home_detour((20.0, 0.0), (0.0, 0.0))
    assert path
    assert recovery.no_fly_zones == s.executor.no_fly_zones


def test_package_copies_input_before_admission():
    doc = document()
    package = load_mission_package(doc)
    doc["link_loss_policy"]["action"] = "land_now"
    doc["places"]["poi:gate"][0] = 999
    assert package.document["link_loss_policy"]["action"] == "continue"
    assert package.refs["poi:gate"] == (40, 0)


@pytest.mark.parametrize("ref", ["home", "poi:gate", "finding:1"])
def test_region_requires_uploaded_zone_even_for_builtin_refs(ref):
    doc = document()
    doc["plan"]["steps"][0].update(skill="area_search", params={"region": ref, "query": "car"})
    with pytest.raises(ValueError):
        load_mission_package(doc)


@pytest.mark.parametrize(
    "skill,params",
    [
        ("survey", {"region": "zone:all", "overlap": 0.2}),
        ("monitor", {"region": "zone:all", "query": "car", "duration_s": 30}),
        ("goto", {"target_ref": "track:1"}),
        ("follow", {"track_ref": "home", "query": "car"}),
    ],
)
def test_package_rejects_refs_the_skill_cannot_resolve(skill, params):
    doc = document()
    doc["plan"]["steps"][0].update(skill=skill, params=params)
    with pytest.raises(ValueError):
        load_mission_package(doc)


def test_repository_example_is_a_valid_self_contained_package():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "configs/onboard/mission-package-example.json"
    package = load_mission_package(path)
    assert package.plan is not None
    assert package.document["link_loss_policy"]["action"] == "continue_then_return"


def test_repeated_bad_verdicts_escalate_and_persist_before_next_action(tmp_path):
    s, now, log = build(tmp_path)
    for _ in range(3):
        s.apply_verdict("untracked", None, 0.2)
    records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert sum(r["kind"] == "perception_verdict" for r in records) == 3
    assert all("recorded_at" in r for r in records)
    assert s.evaluate() == "return_now"
    assert "perception" in s.reason


def test_a_slow_call_without_a_decision_logs_its_cause(tmp_path):
    supervisor, _, log = build(tmp_path)
    supervisor.record_perception_error("t3", "unparseable model output")
    rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["kind"] == "perception_error"
    assert rows[-1]["track_id"] == "t3" and rows[-1]["error"] == "unparseable model output"


def _summaries(log):
    records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    return [r["summary"] for r in records if r["kind"] == "final_summary"]


def test_early_return_writes_one_summary_with_partial_results(tmp_path):
    s, now, log = build(tmp_path)
    assert event(s, "heartbeat")
    # A count step still searching when the mission ends: its count is a lower bound.
    s.executor.counts["gate"] = 2
    s.executor.findings.append({"step": "gate", "query": "cone", "position_ne": (1.0, 2.0)})
    s.update_estimates(remaining_energy_wh=10)
    assert s.evaluate() == "return_now"
    (summary,) = _summaries(log)
    assert summary["status"] == "ended_early"
    assert summary["terminal_action"] == "return_now"
    assert summary["terminal_reason"] == "remaining energy at or below reserve"
    assert summary["findings"] == [{"step": "gate", "query": "cone", "position_ne": [1.0, 2.0]}]
    assert summary["counts"] == {"gate": 2}
    assert summary["count_lower_bounds"] == {"gate": True}
    assert summary["count_is_lower_bound"] is True
    # Further ticks, the recovery flight and a host stop never add a second one.
    now[0] += 1
    s.evaluate()
    s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    s.finish("stopped")
    assert len(_summaries(log)) == 1


def test_host_stop_and_plan_rejection_are_terminal_summaries(tmp_path):
    s, _now, log = build(tmp_path / "stop")
    s.finish("stopped")
    s.finish("stopped")
    (summary,) = _summaries(log)
    assert summary["status"] == "stopped" and summary["terminal_action"] is None
    assert summary["count_is_lower_bound"] is None

    s, _now, log = build(tmp_path / "reject")
    s.requires_approval, s.plan_approved = True, False
    assert s.reject_plan(s.plan_sha256)
    (summary,) = _summaries(log)
    assert summary["status"] == "plan_rejected"


def awaiting(tmp_path, policy="continue", *, doc=None):
    s, now, log = build(tmp_path, policy, doc=doc)
    s.requires_approval, s.plan_approved = True, False
    return s, now, log


def test_signed_supervisor_refuses_unsigned_or_replayed_plan_approval(tmp_path):
    s, now, _log, key = secured_supervisor(tmp_path)
    s.requires_approval, s.plan_approved = True, False
    # The old direct path approved without signature, sequence or rate limit.
    assert not s.approve_plan(s.plan_sha256)
    assert not event(s, "approve_plan", plan_sha256=s.plan_sha256)
    assert not s.plan_approved
    forged = signed_event(
        b"another-test-key-99", 1, now[0], "approve_plan", plan_sha256=s.plan_sha256
    )
    assert not s.ground_event(forged)
    approval = signed_event(key, 1, now[0], "approve_plan", plan_sha256=s.plan_sha256)
    assert s.ground_event(approval)
    assert s.plan_approved
    assert not s.ground_event(approval)  # replay of the same sequence


def test_transport_controls_never_carry_a_plan_decision(tmp_path):
    s, _now, _log, _key = secured_supervisor(tmp_path)
    s.requires_approval, s.plan_approved = True, False
    assert not s.ground_event(
        {"mission_id": "sortie-1", "type": "approve_plan", "plan_sha256": s.plan_sha256},
        transport_authenticated=True,
    )
    assert not s.plan_approved
    # Fixed-layout tunnel controls are authenticated by the tunnel itself.
    assert s.ground_event(
        {"mission_id": "sortie-1", "type": "heartbeat"}, transport_authenticated=True
    )
    assert s.last_heartbeat is not None


def test_plan_approval_is_rate_limited_and_mission_bound(tmp_path):
    s, now, _log = awaiting(tmp_path)
    assert not s.ground_event(
        {"mission_id": "other", "type": "approve_plan", "plan_sha256": s.plan_sha256}
    )
    s.rate_limit = 1
    assert event(s, "heartbeat")
    assert not event(s, "approve_plan", plan_sha256=s.plan_sha256)
    assert not s.plan_approved
    now[0] += s.rate_window_s
    assert event(s, "approve_plan", plan_sha256=s.plan_sha256)
    assert s.plan_approved


def test_awaiting_approval_still_evaluates_abort_link_and_energy(tmp_path):
    s, now, _log = awaiting(tmp_path)
    assert s.evaluate() == "hold" and s.reason == "awaiting operator plan approval"
    action, info = s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    assert info["supervisor_action"] == "hold"
    s.update_estimates(remaining_energy_wh=10)  # at or below the 30 Wh reserve
    assert s.evaluate() == "return_now"
    s, now, _log = awaiting(tmp_path / "abort")
    assert event(s, "abort")
    action, info = s.next_action(pose_ned=(0, 0, -20), yaw_deg=0)
    assert info["supervisor_action"] == "return_now"
    s, now, _log = awaiting(tmp_path / "link", "return_now")
    now[0] += 6
    assert s.evaluate() == "return_now"


def test_rejected_plan_lands_and_finishes(tmp_path):
    s, _now, log = awaiting(tmp_path)
    assert s.reject_plan(s.plan_sha256)
    assert s.evaluate() == "land_now"
    pose = (0.0, 0.0, -20.0)
    for _ in range(400):
        _action, info = s.next_action(pose_ned=pose, yaw_deg=0)
        assert info["supervisor_action"] == "land_now"
        pose = (0.0, 0.0, min(0.0, pose[2] + 1.0))
        if s.done:
            break
    assert s.done
    (summary,) = _summaries(log)
    assert summary["status"] == "plan_rejected" and summary["terminal_action"] == "land_now"
    assert not s.approve_plan(s.plan_sha256)


def test_plan_digest_binds_mission_id_and_package_content(tmp_path):
    first, _now, _log = build(tmp_path / "a")
    renamed = document()
    renamed["mission_id"] = "sortie-2"
    second, _now, _log = build(tmp_path / "b", doc=renamed)
    wider = document()
    wider["budget"]["time_s"] = 3600
    third, _now, _log = build(tmp_path / "c", doc=wider)
    assert first.executor.plan == second.executor.plan == third.executor.plan
    assert len({first.plan_sha256, second.plan_sha256, third.plan_sha256}) == 3


def test_supervisor_hands_the_executor_a_margin_that_holds_the_flight_home(tmp_path):
    s, now, _ = build(tmp_path)
    s.evaluate(pose_ned=(0.0, 0.0, -10.0))
    near = s.executor.energy_margin_s
    s.evaluate(pose_ned=(80.0, 0.0, -10.0))
    far = s.executor.energy_margin_s
    # Time budget only: the margin left for optional looks shrinks with the way home.
    assert near is not None and far is not None
    assert near < 86400 - 60 and far < near
    now[0] += 86400 - 60
    s.evaluate(pose_ned=(80.0, 0.0, -10.0))
    assert s.executor.energy_margin_s <= 0


def test_a_summary_rendering_error_still_writes_the_final_record(tmp_path, monkeypatch):
    import multidetect.autonomy.mission_supervisor as module

    def broken(*_args, **_kwargs):
        raise KeyError("plan")

    monkeypatch.setattr(module, "summarize_mission", broken)
    s, _now, log = build(tmp_path)
    s.finish("stopped")
    (summary,) = _summaries(log)
    assert summary["status"] == "stopped"
    assert "KeyError" in summary["summary_zh"] and "KeyError" in summary["operator_summary"]["error"]

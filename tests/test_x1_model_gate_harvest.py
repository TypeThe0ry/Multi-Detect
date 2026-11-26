import json
from copy import deepcopy

import pytest

from scripts.x1_closed_loop_harvest import harvest, merge_metrics
from scripts.build_x1_flight_loop_sft import policy
from multidetect.autonomy.mission_plan import MissionPlan
from multidetect.autonomy.x1_contingency_executor import DECISION_SCHEMA, REQUEST_SCHEMA
from multidetect.autonomy.x1_perception_tools import TOOL_SCHEMA


def ticket(event, *, index=0, results=()):
    target = event == "target_found"
    low = event == "battery_drop"
    tools = {
        "target_found": ["look", "target_status", "route_status"],
        "battery_drop": ["battery_status"],
    }.get(event, [])
    return {
        "schema": REQUEST_SCHEMA, "request_id": "test:7", "event_id": 7, "deadline_ms": 4000,
        "hold_refs": [], "capabilities": ["airframe:x1", "gimbal_control"],
        "actions": ["continue", "return", "land-now", "retask"], "mission_progress": {"phase": "cruise"},
        "state": {
            "t_ms": 1000, "mission_id": "mission", "revision": 1, "host_leg": {"progress": 1},
            "telemetry": {
                "vtol": {"state": "fw" if target else "mc"}, "fc": {}, "wind": {}, "terrain": {},
                "navigation": {"remaining_energy_wh": 36 if low else 75, "remaining_time_s": 400},
                "battery": {"pct": 20 if low else 90},
                "link": {"gcs": "down", "heartbeat_age_ms": 6000} if event == "link_loss" else {},
                "target": {"visible": target, "track_ref": "track:1"},
            },
            "events": [{"id": 7, "type": event, "t_ms": 999}] if event else [],
        },
        "perception": {"call_index": index, "tools": tools, "calls_remaining": max(len(tools) - index, 0),
                       "results": list(results)},
    }


def decision(q, action, params=None, **identity):
    return {"schema": DECISION_SCHEMA, "request_id": q["request_id"], "event_id": q["event_id"],
            "mission_id": q["state"]["mission_id"], "revision": q["state"]["revision"],
            "action": action, "params": {} if params is None else params, **identity}


def tool_result(tool, data):
    return {"tool": tool, "ok": True, "age_ms": 100, "data": data}


def trace(*turns, status="continued", action="continue", reason="no_new_command", latency=250):
    q = turns[0][0]
    envelope = {"request_id": q["request_id"]}
    rows = [{"kind": "request_opened", **envelope, "base_request": q}]
    for request, output in turns:
        index = request["perception"]["call_index"]
        rows.append({"kind": "turn_sent", **envelope, "request": request, "call_index": index,
                     "messages": [{"role": "user", "content": json.dumps(request)}]})
        if output is not None:
            rows.append({"kind": "turn_result", **envelope, "call_index": index, "latency_ms": 130,
                         "model_output": json.dumps(output) if isinstance(output, dict) else output})
    rows.append({"kind": "receipt", **envelope, "status": status, "action": action,
                 "reason": reason, "latency_ms": latency})
    return rows


def score(rows, **kwargs):
    return harvest(rows, case_id="gate", attempt=1, **kwargs)


def battery_turns():
    first = ticket("battery_drop")
    final = ticket("battery_drop", index=1, results=[
        tool_result("battery_status", {"remaining_wh": 36, "absolute_error_wh": 2}),
    ])
    assert policy(first)["tool"] == "battery_status"
    assert policy(final)["action"] == "return"
    return first, final


def target_turns():
    results = [
        tool_result("look", {"track_id": "track:1", "detections": [{"class": "survey_marker", "bearing_deg": 23}]}),
        tool_result("target_status", {"visible": True, "track_id": "track:1", "error_bound_m": 8}),
        tool_result("route_status", {"steps": [{"primitive": "fw_goto_waypoint"}]}),
    ]
    turns = [(q, policy(q)) for q in (ticket("target_found", index=i, results=results[:i]) for i in range(4))]
    assert [output.get("tool", output.get("action")) for _, output in turns] == [
        "look", "target_status", "route_status", "retask",
    ]
    return turns


def test_event_tool_first_requires_ordered_battery_tool_and_final_action():
    first, final = battery_turns()
    raw = trace((first, policy(first)), (final, policy(final)), status="accepted_unconfirmed", action="return")
    rows, m = score(raw, event_tool_first=True, check_battery_structure=True)
    assert rows == []
    assert m["event_tool_first_oracle_evaluated"] == m["event_tool_first_required"] == 1
    assert m["event_tool_first_compliant"] == 1
    assert m["battery_structure_eligible"] == m["battery_structure_checked"] == m["battery_structure_valid"] == 1
    assert m["identity_echo_checked"] == m["identity_echo_valid"] == 2
    assert m["deadline_on_time"] == 1 and m["turn_latency_ms_histogram"]["le_250"] == 2

    raw[2], raw[3] = raw[3], raw[2]
    rows, m = score(raw, event_tool_first=True)
    assert m["event_tool_first_compliant"] == 0
    assert "event_tool_first_no_final_action" in rows[0]["failure_reasons"]


def test_link_loss_oracle_evaluated_without_tool_obligation():
    q = ticket("link_loss")
    assert policy(q)["action"] == "return"
    rows, m = score(trace((q, policy(q)), status="accepted_unconfirmed", action="return"), event_tool_first=True)
    assert rows == []
    assert m["event_tool_first_oracle_evaluated"] == 1
    assert m["event_tool_first_required"] == m["event_tool_first_compliant"] == 0
    q["event_id"] = 2**52 + 1
    _, m = score(trace((q, policy(q))), event_tool_first=True)
    assert m["event_tool_first_oracle_evaluated"] == 0


def test_retask_matches_oracle_after_evidence_and_calls_package_validator():
    turns = target_turns()
    final, proposal = turns[-1]
    checked = []

    def validate(plan, q):
        assert isinstance(plan, MissionPlan)
        assert q is final
        assert plan.steps[-1].skill == "return_home"
        checked.append(plan)
        return None

    raw = trace(*turns, status="installed", action="retask")
    rows, m = score(raw, validate_retask=validate, check_retask_timing=True, event_tool_first=True)
    assert rows == [] and len(checked) == 1
    assert m["event_tool_first_required"] == m["event_tool_first_compliant"] == 1
    assert m["retasks_structure_package_checked"] == m["retasks_structure_valid"] == m["retasks_proposed"] == 1
    assert m["retasks_timing_checked"] == m["retasks_timing_valid"] == m["required_retasks_installed"] == 1

    rows, m = score(raw, validate_retask=lambda p, q: "outside_package", check_retask_timing=True)
    assert m["retasks_structure_valid"] == 0 and m["retasks_timing_valid"] == 1
    assert rows[-1]["retask_validation_reason"] == "outside_package"
    assert "retask_structure_invalid" in rows[-1]["failure_reasons"]

    first = turns[0][0]
    rows, m = score(trace((first, proposal), status="rejected", action="retask", reason="retask_requires_step_boundary"),
                    check_retask_timing=True, event_tool_first=True)
    assert m["retasks_structure_valid"] == m["retasks_timing_checked"] == 1
    assert m["retasks_timing_valid"] == m["event_tool_first_compliant"] == 0
    assert {"event_tool_first_missing", "retask_evidence_not_established"} <= set(rows[0]["failure_reasons"])


@pytest.mark.parametrize("variant", ["tool", "incomplete", "duplicates", "wrong_return_params"])
def test_malformed_battery_returns_and_retasks(variant):
    q, _ = battery_turns()
    if variant == "wrong_return_params":
        reply = decision(q, "return", {"tool": "battery_status"})
    else:
        plan = {"schema": TOOL_SCHEMA, "tool": "battery_status", "args": {}, "call_index": 0,
                "request_id": q["request_id"]} if variant != "incomplete" else {"plan_version": 1, "steps": []}
        reply = decision(q, "retask", {"plan": plan})
        if variant == "duplicates":
            # Seed 31's original wire has a retask followed by duplicate action/params for continue.
            reply = json.dumps(reply)[:-1] + ', "action": "continue", "params": {}}'
    rows, m = score(trace((q, reply), status="rejected", action=None, reason="tool_protocol_error"),
                    event_tool_first=True, check_retask_timing=True, check_battery_structure=True,
                    validate_retask=lambda *_: pytest.fail("callback received malformed plan"))
    assert m["schema_valid"] == m["retasks_structure_valid"] == m["battery_structure_valid"] == 0
    assert m["battery_structure_checked"] == m["event_tool_first_required"] == 1
    assert m["retasks_proposed"] == m["retasks_timing_checked"] == (variant != "wrong_return_params")
    assert "battery_structure_invalid" in rows[0]["failure_reasons"]
    assert json.loads(rows[0]["messages"][-1]["content"])["tool"] == "battery_status"


@pytest.mark.parametrize("field,value", [("request_id", "test:6"), ("event_id", 6), ("event_id", True)])
def test_identity_echo_checks_every_decision_turn(field, value):
    first, final = battery_turns()
    wrong = {**policy(final), field: value}
    rows, m = score(trace((first, policy(first)), (final, wrong), status="rejected", action="return",
                          reason="superseded_mission_or_event"), event_tool_first=True)
    assert m["identity_echo_checked"] == 2 and m["identity_echo_valid"] == 1
    assert m["event_tool_first_compliant"] == 0
    assert any({"identity_mismatch", "identity_echo_mismatch"} & set(row["failure_reasons"]) for row in rows)


def test_tool_wire_binds_request_id_and_call_index():
    first, final = battery_turns()
    for field, wrong_value in [("request_id", "test:6"), ("call_index", 1)]:
        wrong = {**policy(first), field: wrong_value}
        _, m = score(trace((first, wrong), (final, policy(final)), action="return"), event_tool_first=True)
        assert m["identity_valid"] == 1
        assert m["identity_echo_valid"] == (1 if field == "request_id" else 2)
        assert m["event_tool_first_required"] == 1 and m["event_tool_first_compliant"] == 0


@pytest.mark.parametrize("reason", ["unverified_clearance", "missing_or_unbound_motion_proof"])
def test_evidence_receipt_separate_from_model_failures(reason):
    q = ticket(None)
    q["trigger"] = "cadence"
    raw = trace((q, policy(q)), status="rejected", reason=reason)
    rows, m = score(raw)
    evidence, = rows
    assert evidence["failure_scope"] == "evidence_scope" and evidence["evidence_scope_reason"] == reason
    assert all(message["role"] != "assistant" for message in evidence["messages"])
    assert m["evidence_scope_rejections"] == 1 and m["evidence_scope_reason_counts"] == {reason: 1}
    assert m["model_failure_rows"] == 0 and m["failure_reason_counts"] == {}

    raw = trace((q, decision(q, "return")), status="rejected", action="return", reason=reason)
    rows, mixed = score(raw)
    model, evidence = rows
    assert {row["failure_scope"] for row in rows} == {"model", "evidence_scope"}
    assert model["sample_id"] != evidence["sample_id"]
    assert model["failure_reasons"] == ["action_mismatch"] and model["receipt"] is None
    assert mixed["model_failure_rows"] == mixed["evidence_scope_rejections"] == 1
    assert mixed["quiet_unnecessary_actions"] == 1
    assert merge_metrics([m, mixed])["evidence_scope_reason_counts"] == {reason: 2}


def test_non_evidence_veto_and_deadline_are_preserved():
    q = ticket("link_loss")
    raw = trace((q, policy(q)), status="rejected", action="return", reason="current_fw_airspeed_limit", latency=3001)
    rows, m = score(raw)
    assert rows[0]["failure_scope"] == "model"
    assert rows[0]["failure_reasons"] == ["receipt_not_admitted", "deadline_missed"]
    assert m["evidence_scope_rejections"] == 0 and m["deadline_missed"] == 1

    raw = deepcopy(raw)
    raw[-1].update(action="continue", reason="unverified_clearance")
    _, m = score(raw)
    assert m["evidence_scope_rejections"] == 0


def test_battery_structure_independent_of_tool_timing_and_identity():
    first, _ = battery_turns()
    early = decision(first, "return", event_id=6)
    rows, m = score(trace((first, early), status="rejected", action="return",
                          reason="tool_protocol_error"), event_tool_first=True, check_battery_structure=True)
    assert m["battery_structure_checked"] == m["battery_structure_valid"] == 1
    assert m["event_tool_first_compliant"] == m["identity_echo_valid"] == 0
    assert "battery_structure_invalid" not in rows[0]["failure_reasons"]


def test_disrupted_unsent_and_missing_results_are_infrastructure():
    q, _ = battery_turns()
    raw = trace((q, None), status="rejected", action=None, reason="preempted_by_event")
    for test_trace in (raw, [raw[0], raw[-1]]):
        rows, m = score(test_trace, event_tool_first=True, check_battery_structure=True)
        infrastructure, = rows
        assert infrastructure["failure_scope"] == "infrastructure"
        assert m["infrastructure_failure_rows"] == m["disrupted_requests"] == 1
        assert m["model_failure_rows"] == m["battery_structure_checked"] == 0
        assert m["battery_structure_eligible"] == 1
        assert all(message["role"] != "assistant" for message in infrastructure["messages"])

    raw = trace((q, decision(q, "return", request_id="previous")), status="rejected", action=None,
                reason="preempted_by_event")
    rows, m = score(raw, event_tool_first=True)
    model, infrastructure = rows
    assert model["failure_scope"] == "model" and "identity_mismatch" in model["failure_reasons"]
    assert infrastructure["failure_scope"] == "infrastructure"
    assert "request_disrupted" in infrastructure["failure_reasons"]
    assert "request_disrupted" not in model["failure_reasons"]
    assert m["infrastructure_failure_rows"] == m["model_failure_rows"] == 1


def test_battery_retask_structure_uses_package_validator_without_rewarding_early_timing():
    first, _ = battery_turns()
    plan = {"plan_version": 1, "goal": "Return home", "report": [], "steps": [
        {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
    ]}
    reply = decision(first, "retask", {"plan": plan})
    raw = trace((first, reply), status="rejected", action="retask", reason="retask_requires_step_boundary")
    _, m = score(raw, check_battery_structure=True, check_retask_timing=True,
                 validate_retask=lambda p, _: None if isinstance(p, MissionPlan) else "invalid_plan")
    assert m["battery_structure_checked"] == m["battery_structure_valid"] == 1
    assert m["retasks_structure_package_checked"] == 1
    assert m["retasks_timing_checked"] == 1 and m["retasks_timing_valid"] == 0
    _, m = score(raw, check_battery_structure=True, validate_retask=lambda *_: "outside_package")
    assert m["battery_structure_valid"] == 0

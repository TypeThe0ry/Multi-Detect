"""CPU-only admission checks with an explicit, synthetic execution witness."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA, HoldSite, RuntimeFrame, X1AirbornePlanAdapter, X1ContingencyExecutor,
    X1Envelope, decode_decision,
)
from multidetect.autonomy.x1_contingency_state import X1StateBuilder
from multidetect.autonomy.x1_motion_proof import ExecutionSweep, LandingPad, SweptLeg, X1MotionProofBuilder
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions
from test_x1_mission import _build, _package, _plan, _raise_vertex, profile, terrain  # noqa: F401

CAPABILITIES = frozenset({"airframe:x1"})


@pytest.fixture
def admitted(terrain, profile):  # noqa: F811
    package = _package(plan=_plan(), capabilities=["gimbal_control"])
    steps = _build(package, terrain, profile)
    builder = X1MotionProofBuilder(package, profile, terrain, steps, mission_radius_m=1250,
                                   terrain_error_m=1, obstacle_clearance=lambda box: 25)
    return package, steps, builder


def _frame(package):
    return RuntimeFrame(123, 1010, package.mission_id, 1, 1, False, True, "mc", 80, None, True,
                        {"here": HoldSite("mc")}, position_ne_amsl=(410, 0, 180),
                        active_motion_index=3, remaining_energy_wh=60, remaining_time_s=300)


def _leg(phase, start, end, seconds=60, padding=2):
    return SweptLeg(phase, start, end, padding, 1, seconds, 500)


def _home_legs(start):
    return (_leg("mc", start, (0, 0, 180), 90), _leg("landing", (0, 0, 180), (0, 0, 100), 50))


def _sweep(decision, frame, legs=None, **options):
    defaults = dict(max_bank_deg=20, max_climb_mps=2, max_descent_mps=2, max_acceleration_mps2=1,
                    max_angular_rate_dps=30, landing_pad=LandingPad((0, 0, 100), 8, 2),
                    failure_revalidation_enforced=True)
    defaults.update(options)
    return ExecutionSweep(frame.frame_id, decision.digest,
                          _home_legs(frame.position_ne_amsl) if legs is None else legs, **defaults)


def _request(package, profile, *, action="continue", params=None):
    calls = []
    adapter = X1AirbornePlanAdapter(1, replace_remaining=lambda *args: calls.append(args) is None)
    vehicle = SimpleNamespace(rtl=lambda: calls.append("rtl"), land=lambda: calls.append("land"),
                              set_mode=lambda mode: calls.append(mode), loiter_at=lambda *args, **kwargs: (
                                  calls.append((args, kwargs)) or SimpleNamespace(ok=True, ack_result=0)))
    ctl = X1ContingencyExecutor(adapter, vehicle, package, X1Envelope(profile), claim_native=lambda: True,
                                capabilities=CAPABILITIES)
    state = X1StateBuilder()
    state.event(1000, "target_lost", source="tracker")
    snapshot = state.snapshot(1000, mission_id=package.mission_id, revision=1)
    request = ctl.open_request(snapshot, run_id="test", hold_refs=("here", "orbit"))
    wire = json.dumps({"schema": DECISION_SCHEMA, "request_id": request["request_id"],
                       "event_id": request["event_id"], "mission_id": package.mission_id,
                       "revision": 1, "action": action, "params": params or {}})
    return ctl, calls, wire, decode_decision(wire, capabilities=CAPABILITIES)


def test_bound_compiled_mc_continue_admits_without_writing(admitted, profile):  # noqa: F811
    package, _, builder = admitted
    ctl, calls, wire, decision = _request(package, profile)
    frame = _frame(package)
    proof = builder.build(decision, frame, _sweep(decision, frame))
    assert proof.geofence_clear and proof.terrain_margin_m == 78
    assert proof.required_time_s == 140 and proof.required_energy_wh == pytest.approx(140 * 750 / 3600)
    assert ctl.apply(wire, frame, now_ms=1010, proof=proof).status == "continued"
    assert calls == []


@pytest.mark.parametrize("field", ["frame_id", "decision_digest"])
def test_wrong_binding_rejects_proof_and_source(admitted, profile, field):  # noqa: F811
    package, _, builder = admitted
    ctl, calls, wire, decision = _request(package, profile)
    frame = _frame(package)
    sweep = _sweep(decision, frame)
    bad = {field: 124 if field == "frame_id" else "f" * 64}
    proof = builder.build(decision, frame, sweep)
    assert builder.build(decision, frame, replace(sweep, **bad)).geofence_clear is False
    assert ctl.apply(wire, frame, now_ms=1010, proof=replace(proof, **bad)).reason == "missing_or_unbound_motion_proof"
    assert calls == []


@pytest.mark.parametrize(("failure", "reason"), [
    ("fence", "unverified_clearance"), ("no_fly", "unverified_clearance"),
    ("high_terrain", "unverified_clearance"), ("dem_gap", "unverified_clearance"),
    ("terrain_unknown", "unverified_clearance"), ("live_terrain_conflict", "unverified_clearance"),
    ("obstacle_near", "unverified_clearance"), ("obstacle_unknown", "unverified_clearance"),
    ("energy_low", "energy_or_reserve_limit"), ("energy_unknown", "energy_or_reserve_limit"),
    ("time_unknown", "time_or_reserve_limit"), ("time_low", "time_or_reserve_limit"),
    ("dynamics_unknown", "predicted_dynamics_limit"), ("dynamics_exceeded", "predicted_dynamics_limit"),
    ("landing_unknown", "unverified_clearance"), ("position_unknown", "unverified_clearance"),
])
def test_violation_or_missing_evidence_never_sends(admitted, profile, failure, reason):  # noqa: F811
    package, steps, nominal = admitted
    frame = _frame(package)
    zones = {"zone:restricted": {"centre_ne": [200, 0], "radius_m": 3, "risk_tag": "no_fly"}}
    if failure == "no_fly":
        package = _package(plan=_plan(), zones=zones)
    ctl, calls, wire, decision = _request(package, profile)
    sweep = _sweep(decision, frame)
    if failure == "energy_low":
        frame = replace(frame, remaining_energy_wh=25)
    elif failure == "energy_unknown":
        frame = replace(frame, remaining_energy_wh=None)
    elif failure == "time_unknown":
        frame = replace(frame, remaining_time_s=None)
    elif failure == "time_low":
        frame = replace(frame, remaining_time_s=160)
    elif failure == "live_terrain_conflict":
        frame = replace(frame, terrain_conflict=True)
    elif failure == "position_unknown":
        frame = replace(frame, position_ne_amsl=None)
    elif failure == "dynamics_unknown":
        sweep = replace(sweep, max_angular_rate_dps=None)
    elif failure == "dynamics_exceeded":
        sweep = replace(sweep, max_descent_mps=2.1)
    elif failure == "landing_unknown":
        sweep = replace(sweep, landing_pad=None)
    terrain = (_raise_vertex(nominal.terrain, 200, 0, 181) if failure == "high_terrain" else nominal.terrain)
    if failure == "dem_gap":
        terrain = replace(terrain, elevations_m=terrain.elevations_m[:11])
    builder = X1MotionProofBuilder(
        package, profile, terrain, steps, mission_radius_m=405 if failure == "fence" else 1250,
        terrain_error_m=None if failure == "terrain_unknown" else 1,
        obstacle_clearance=lambda box: {"obstacle_unknown": None, "obstacle_near": -1}.get(failure, 25),
    )
    assert ctl.apply(wire, frame, now_ms=1010, proof=builder.build(decision, frame, sweep)).reason == reason
    assert calls == []


@pytest.mark.parametrize(("action", "status", "expected"), [
    ("return", "sent_unconfirmed", "rtl"), ("land-now", "sent_unconfirmed", "land"),
    ("loiter", "sent_unconfirmed", "HOLD"), ("retask", "installed", None),
])
def test_supported_mc_actions_dispatch_only_selected_action(admitted, profile, action, status, expected):  # noqa: F811
    package, _, builder = admitted
    frame = _frame(package)
    params = {"hold_ref": "here"} if action == "loiter" else None
    if action == "retask":
        params = {"plan": {**_plan(), "steps": _plan()["steps"][1:]}}
    ctl, calls, wire, decision = _request(package, profile, action=action, params=params)
    sweep = _sweep(decision, frame)
    if action == "land-now":
        site = (*frame.position_ne_amsl[:2], 100)
        sweep = replace(sweep, legs=(_leg("landing", frame.position_ne_amsl, site),), landing_pad=LandingPad(site, 8, 2))
    elif action == "loiter":
        sweep = replace(sweep, legs=(_leg("mc", frame.position_ne_amsl, frame.position_ne_amsl, 5),),
                        hold_revalidation_enforced=True)
    assert ctl.apply(wire, frame, now_ms=1010, proof=builder.build(decision, frame, sweep)).status == status
    if action == "retask":
        assert len(calls) == 1 and calls[0][0] == decision.plan and calls[0][1] == 1
    else:
        assert calls == [expected]


@pytest.mark.parametrize("action", ["continue", "return", "retask"])
def test_fw_sweep_accounts_for_back_transition(admitted, profile, action):  # noqa: F811
    package, _, builder = admitted
    frame = replace(_frame(package), vtol_state="fw", airspeed_mps=15, active_motion_index=1,
                    position_ne_amsl=(300, 0, 180))
    params = None if action != "retask" else {"plan": {**_plan(), "steps": [
        {"id": "back", "skill": "back_transition", "params": {}, "on_success": "finish", "on_fail": "finish"},
        _plan()["steps"][1],
    ]}}
    ctl, calls, wire, decision = _request(package, profile, action=action, params=params)
    waypoint, runout = (400, 0, 180), (430, 0, 180)
    path = (_leg("fw", frame.position_ne_amsl, waypoint, 15),) if action == "continue" else ()
    path += (_leg("transition", path[-1].end if path else frame.position_ne_amsl, runout, 20), *_home_legs(runout))
    sweep = _sweep(decision, frame, path, min_airspeed_mps=12, max_airspeed_mps=18)
    proof = builder.build(decision, frame, sweep)
    assert proof.geofence_clear and proof.min_transition_agl_m == 78
    assert proof.required_energy_wh >= profile.endurance.transition_energy_wh
    status = {"continue": "continued", "return": "sent_unconfirmed", "retask": "installed"}
    assert ctl.apply(wire, frame, now_ms=1010, proof=proof).status == status[action]
    assert bool(calls) is (action != "continue")


def test_fw_orbit_covers_entire_circle_and_enforces_next_check(admitted, profile):  # noqa: F811
    package, _, builder = admitted
    frame = replace(_frame(package), position_ne_amsl=(0, 0, 180), vtol_state="fw", airspeed_mps=15,
                    hold_sites={"orbit": HoldSite("fw", 52.0, 5.0, 180, 60, 15)})
    ctl, calls, wire, decision = _request(package, profile, action="loiter", params={"hold_ref": "orbit"})
    path = (_leg("fw", frame.position_ne_amsl, frame.position_ne_amsl, 5, padding=60),)
    sweep = _sweep(decision, frame, path, min_airspeed_mps=12, max_airspeed_mps=18,
                   hold_revalidation_enforced=True)
    assert not builder.build(decision, frame, replace(sweep, hold_revalidation_enforced=False)).geofence_clear
    narrow = replace(sweep, legs=(replace(path[0], padding_ne_m=59),))
    assert not builder.build(decision, frame, narrow).geofence_clear
    assert ctl.apply(wire, frame, now_ms=1010, proof=builder.build(decision, frame, sweep)).status == "accepted_unconfirmed"
    assert calls == [(((52.0, 5.0), 60, 180), {"speed": 15})]


@pytest.mark.parametrize("outside", [False, True])
def test_fw_retask_checks_new_waypoint_and_failure_boundary(admitted, profile, outside):  # noqa: F811
    package, steps, original = admitted
    # Even a named package POI must fit inside the specific X1 mission radius.
    target = (450, 550) if outside else (450, 80)
    document = {**package.document, "places": {**package.document["places"], "poi:new": target}}
    package = replace(package, document=document, refs={**package.refs, "poi:new": target})
    builder = X1MotionProofBuilder(package, profile, original.terrain, steps, mission_radius_m=600,
                                   terrain_error_m=1, obstacle_clearance=lambda box: 25)
    frame = replace(_frame(package), vtol_state="fw", airspeed_mps=15, position_ne_amsl=(300, 0, 180))
    steps = [
        {"id": "new", "skill": "fw_goto_waypoint", "params": {"target_ref": "poi:new", "altitude_agl_m": 80},
         "on_success": "back", "on_fail": "finish"},
        {"id": "back", "skill": "back_transition", "params": {}, "on_success": "finish", "on_fail": "finish"},
        _plan()["steps"][1],
    ]
    ctl, calls, wire, decision = _request(package, profile, action="retask", params={"plan": {**_plan(), "steps": steps}})
    waypoint, runout = (*target, 180), (480, target[1], 180)
    path = (_leg("fw", frame.position_ne_amsl, waypoint, 20), _leg("transition", waypoint, runout, 20),
            *_home_legs(runout))
    sweep = _sweep(decision, frame, path, min_airspeed_mps=12, max_airspeed_mps=18)
    unbound_branch = replace(sweep, failure_revalidation_enforced=False)
    assert not builder.build(decision, frame, unbound_branch).geofence_clear
    proof = builder.build(decision, frame, sweep)
    assert proof.geofence_clear is not outside
    assert ctl.apply(wire, frame, now_ms=1010, proof=proof).status == ("rejected" if outside else "installed")
    assert bool(calls) is not outside


@pytest.mark.parametrize("case", ["proven", "unknown", "stale"])
def test_scenario_tick_uses_deployed_ingress_for_current_decision(admitted, profile, case):  # noqa: F811
    package, _, builder = admitted
    markers, calls, now = [], [], [1000]

    def proof(decision, frame):
        calls.append((decision, frame))
        return builder.build(decision, frame, _sweep(decision, frame) if case == "proven" else None)

    tick = DecisionTick(package, profile, recorded_decisions(({"action": "continue", "params": {}},)),
                        object(), markers.append, run_id="fixture", plan_digest="f" * 64,
                        now_ms=lambda: now[0], build_proof=proof)
    frame = _frame(package)
    try:
        tick.tick(replace(frame, t_ms=1000), events=(("target_lost", "tracker"),))
        tick.future.result(timeout=2)
        now[0] = 1511 if case == "stale" else 1010
        tick.tick(replace(frame, event_id=0))
    finally:
        tick.close()
    if case == "stale":
        assert not calls
    else:
        assert len(calls) == 1 and calls[0][1] == frame
    assert markers[-1]["reason"] == {
        "proven": "no_new_command", "unknown": "unverified_clearance", "stale": "decision_or_frame_stale",
    }[case]

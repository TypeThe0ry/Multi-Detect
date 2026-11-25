"""Deterministic CPU checks of the X1 validation and dispatch boundaries."""

import json
import math
import random
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_package import check_plan_against_package, load_mission_package
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep, validate_mission_plan
from multidetect.autonomy.motion_intent import resolve_airframe_profile
from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA,
    HoldSite,
    MotionProof,
    RuntimeFrame,
    X1AirbornePlanAdapter,
    X1ContingencyExecutor,
    X1Envelope,
    decode_decision,
)
from multidetect.autonomy.x1_mission import (
    X1PlanError,
    build_x1_mission_steps,
    x1_place_from_bearing,
)
from multidetect.autonomy.x1_plan import X1_CAPABILITY, one_waypoint_primitives_input
from multidetect.onboard_runtime.vehicle import CommandResult
from multidetect.terrain_dem import LocalDemGrid

ROOT = Path(__file__).resolve().parents[1]
LIMITS = json.loads(
    (ROOT / "configs/airframes/delta_tailsitter_gimbal.json").read_text(encoding="utf-8")
)
CAPABILITIES = frozenset({X1_CAPABILITY, "gimbal_control"})
MIN_LEG = max(50, 3 * LIMITS["cruise_airspeed_mps"])
RUNOUT = 85 + LIMITS["transition"]["timeout_s"] * (
    LIMITS["max_airspeed_mps"] + LIMITS["endurance"]["wind_margin_mps"]
)
EXTREMES = (math.nan, math.inf, -math.inf, 1e300, 10**400, -1, 0, True)


def package_doc(target=(250, 0), *, x1=False):
    area = {"north_min_m": -1600, "north_max_m": 1600, "east_min_m": -1600, "east_max_m": 1600}
    doc = {
        "mission_id": "x1-envelope",
        "home": [0, 0],
        "places": {"poi:yard": target},
        "zones": {},
        "routes": {},
        "oversight_steps": [],
        "risk_level": "low",
        "task_text": "Fly to the yard and return home",
        "link_loss_policy": {"action": "return_now", "timeout_s": 30},
        "operating_area": {**area, "min_altitude_m": 0, "max_altitude_m": 160},
        "budget": {"time_s": 1200, "reserve_time_s": 60, "energy_wh": 74, "reserve_energy_wh": 10},
    }
    if x1:
        doc.update(
            capabilities=sorted(CAPABILITIES),
            x1_planning={
                "world": "rw_envelope_test",
                "corridor": area,
                "max_distance_from_home_m": 1500,
                "terrain_rel_home_m": [-5, 5],
            },
        )
    return doc


def wire_plan(steps):
    return {"plan_version": 1, "goal": "Fly to the yard and recover", "steps": steps, "report": []}


def step(sid, skill, success=None, *, failure="home", **params):
    return {"id": sid, "skill": skill, "params": params, "on_success": success, "on_fail": failure}


def terminal():
    return step("home", "return_home", failure=None)


def x1_graph(agl=80):
    return wire_plan(
        [
            step("takeoff", "vtol_takeoff", "front", altitude_agl_m=agl),
            step("front", "fw_transition", "work"),
            step("work", "fw_goto_waypoint", "back", target_ref="poi:yard", altitude_agl_m=agl),
            step("back", "back_transition", "home"),
            terminal(),
        ]
    )


def legacy_graph(agl=80):
    return wire_plan(
        [step("work", "goto", "home", target_ref="poi:yard", altitude_m=agl), terminal()]
    )


@pytest.fixture(scope="module")
def profile():
    return resolve_airframe_profile(LIMITS["id"])


def dem(ridge_m=0):
    elevations = tuple(
        (100.0 + (ridge_m if n == 500 else 0.0),) * 33 for n in range(-1600, 1601, 100)
    )
    return LocalDemGrid(
        scenario_id="x1-envelope",
        origin_latitude_deg=52.0,
        origin_longitude_deg=5.0,
        origin_elevation_m=100,
        north_origin_m=-1600,
        east_origin_m=-1600,
        north_spacing_m=100,
        east_spacing_m=100,
        elevations_m=elevations,
        vertical_sigma_m=0.1,
        slope_sigma_m_per_m=0,
        source_sha256="0" * 64,
    )


def lower(pkg, terrain, profile, **kwargs):
    return build_x1_mission_steps(
        pkg, terrain=terrain, profile=profile, **{"mission_radius_m": 1500, **kwargs}
    )


def assert_executable(steps, terrain, target, agl):
    assert [s.primitive for s in steps] == [
        "vtol_takeoff",
        "fw_transition",
        "fw_goto_waypoint",
        "back_transition",
        "mc_return_to_home",
        "vtol_land",
    ]
    assert [s.source_step_id for s in steps] == ["work"] * 3 + ["home"] * 3
    height = steps[0].takeoff_height_m
    cruise_msl = terrain.sample_local(0, 0).elevation_m + height
    assert math.isfinite(height) and height >= LIMITS["transition"]["minimum_height_m"]
    assert 0 <= steps[1].required_mc_heading_deg < 360
    assert [s.command.mode for s in steps[1:5]] == ["FW", "FW", "MC", "MC"]
    outbound = steps[2].command
    assert LIMITS["min_airspeed_mps"] <= outbound.speed_mps <= LIMITS["max_airspeed_mps"]
    assert outbound.target_ned == pytest.approx((*target, -height))
    assert steps[4].command.target_ned == pytest.approx((0, 0, -height))
    assert cruise_msl - terrain.sample_local(*target).elevation_m == pytest.approx(agl)
    length = math.hypot(*target)
    assert length >= MIN_LEG
    north, east = (v / length for v in target)
    for index in range(101):
        along = -10 + (length + RUNOUT + 10) * index / 100
        for cross in (-55, -27.5, 0, 27.5, 55):
            point = (along * north - cross * east, along * east + cross * north)
            assert math.hypot(*point) <= 1500
            assert 40 - 1e-9 <= cruise_msl - terrain.sample_local(*point).elevation_m <= 120 + 1e-9


@pytest.mark.parametrize("seed", [0x5831, 0xA61, 0x4051])
def test_randomized_lowering_with_synthetic_ridges_and_valleys(seed, profile):
    rng = random.Random(seed)
    seen = set()
    cases = [
        (50, 0, 40, 0),
        (50, 0, 120, 0),
        (100, 0, 80, -40),
        (100, 0, 80, 40),
        (100, 0, 80, -40.01),
        (100, 0, 80, 40.01),
        (49.99, 0, 80, 0),
        (1100, 0, 80, 0),
    ]
    cases += [
        (
            rng.uniform(0, 1100),
            rng.uniform(-math.pi, math.pi),
            rng.uniform(38, 122),
            rng.choice([0, rng.uniform(-80, 80)]),
        )
        for _ in range(96)
    ]
    for distance, angle, agl, ridge in cases:
        target = (distance * math.cos(angle), distance * math.sin(angle))
        pkg = load_mission_package({**package_doc(target), "plan": legacy_graph(min(agl, 120))})
        # Legacy wire AGL has its own ceiling. Exercise the CPU lowering boundary directly too.
        outbound, home = pkg.plan.steps
        plan = replace(
            pkg.plan, steps=(replace(outbound, params={**outbound.params, "altitude_m": agl}), home)
        )
        terrain = dem(ridge)
        try:
            steps = lower(pkg, terrain, profile, plan=plan)
        except X1PlanError as exc:
            assert exc.code in {
                "AGL_OUT_OF_BOUNDS",
                "FW_LEG_TOO_SHORT",
                "TRANSITION_HEIGHT",
                "HOME_RELATIVE_ALTITUDE",
                "RUNOUT_OUTSIDE_FENCE",
                "CORRIDOR_AGL",
                "ENERGY_BUDGET",
            }
            outcome = exc.code
        else:
            assert_executable(steps, terrain, target, agl)
            outcome = "accepted"
        if angle == 0 and (distance, agl, ridge) in {(100, 80, 40.01), (100, 80, -40.01)}:
            assert outcome == "CORRIDOR_AGL"
        if angle == 0 and (distance, agl, ridge) in {
            (50, 40, 0),
            (50, 120, 0),
            (100, 80, -40),
            (100, 80, 40),
        }:
            assert outcome == "accepted"
        seen.add(outcome)
    assert {"accepted", "CORRIDOR_AGL", "FW_LEG_TOO_SHORT", "RUNOUT_OUTSIDE_FENCE"} <= seen


@pytest.mark.parametrize("value", EXTREMES + (math.nextafter(40, 0), math.nextafter(120, math.inf)))
def test_invalid_agl_and_distance_are_model_or_envelope_rejections(value, profile):
    with pytest.raises(ValueError, match="altitude_agl_m"):
        validate_mission_plan(x1_graph(value), capabilities=CAPABILITIES)
    package = load_mission_package({**package_doc(), "plan": legacy_graph()})
    out, home = package.plan.steps
    invalid = replace(
        package.plan, steps=(replace(out, params={**out.params, "altitude_m": value}), home)
    )
    with pytest.raises(X1PlanError, match="^AGL_OUT_OF_BOUNDS:"):
        lower(package, dem(), profile, plan=invalid)
    if value in (math.nextafter(40, 0), math.nextafter(120, math.inf)):
        return
    with pytest.raises(X1PlanError, match="^INVALID_DISTANCE:"):
        x1_place_from_bearing((0, 0), value, 90)
    with pytest.raises(X1PlanError, match="^INVALID_MISSION_RADIUS:"):
        lower(package, dem(), profile, mission_radius_m=value)
    with pytest.raises(X1PlanError, match="^INVALID_FW_MIN_LEG:"):
        lower(package, dem(), profile, fw_min_leg_m=value)
    context_package = package_doc(x1=True)
    context_package["x1_planning"]["max_distance_from_home_m"] = value
    with pytest.raises(ValueError, match="X1"):
        load_mission_package(context_package)
    if value not in (-1, 0):  # Signed NE coordinates are legal.
        with pytest.raises(ValueError, match="poi:yard"):
            load_mission_package(package_doc(target=(value, 0)))


@pytest.mark.parametrize(
    "defect", ["unknown", "duplicate", "no_terminal", "reversed", "wrong_success", "short"]
)
def test_packages_and_typed_graphs_reject_malformed_plan_sequences(defect, profile):
    rng = random.Random(0xA190)
    for _ in range(24):
        target = (rng.uniform(0, MIN_LEG) if defect == "short" else rng.uniform(200, 400), 0)
        doc = package_doc(target, x1=True)
        raw = x1_graph(rng.uniform(42, 115))
        rows = raw["steps"]
        if defect == "unknown":
            rows[2]["skill"] = rng.choice(["unregistered", "vtol_goto", "teleport"])
        elif defect == "duplicate":
            rows.insert(rng.randrange(len(rows)), deepcopy(rows[2]))
        elif defect == "no_terminal":
            # Keep the graph reachable so the missing terminal fails validation first.
            rows.pop()
            rows[3].update(on_success=None, on_fail=None)
            for row in rows[:3]:
                row["on_fail"] = "back"
        elif defect == "reversed":
            rows.reverse()
        elif defect == "wrong_success":
            rows[0]["on_success"] = "work"
        if defect in {"unknown", "duplicate", "no_terminal"}:
            with pytest.raises(
                ValueError,
                match={
                    "unknown": "unknown skill",
                    "duplicate": "duplicate step",
                    "no_terminal": "terminal",
                }[defect],
            ):
                load_mission_package({**doc, "plan": raw})
        compiled = MissionPlan(1, raw["goal"], tuple(MissionStep(**row) for row in rows), ())
        package = load_mission_package(doc)
        findings = check_plan_against_package(compiled, package)
        expected = (
            "X1_MIN_LEG"
            if defect == "short"
            else (
                "X1_PLAN_SCHEMA"
                if defect in {"unknown", "no_terminal", "duplicate"}
                else "X1_SEQUENCE"
            )
        )
        assert findings[0]["code"] == expected
        with pytest.raises(X1PlanError, match="^UNSUPPORTED_PLAN:"):
            lower(package, dem(), profile, plan=compiled)


@pytest.mark.parametrize("value", EXTREMES + (None, [], 39.9, 120.1))
def test_typed_plan_cannot_bypass_x1_slot_validation(value):
    package = load_mission_package(package_doc(x1=True))
    plan = validate_mission_plan(x1_graph(), capabilities=CAPABILITIES)
    work = replace(plan.steps[2], params={"target_ref": "poi:yard", "altitude_agl_m": value})
    edited = replace(plan, steps=(*plan.steps[:2], work, *plan.steps[3:]))
    assert check_plan_against_package(edited, package)[0]["code"] == "X1_PLAN_SCHEMA"


@pytest.mark.parametrize("field", ["id", "on_success", "on_fail", "target_ref"])
def test_typed_plan_rejects_unhashable_graph_and_reference_values(field):
    package = load_mission_package(package_doc(x1=True))
    plan = validate_mission_plan(x1_graph(), capabilities=CAPABILITIES)
    work = plan.steps[2]
    patch = {"params": {**work.params, "target_ref": []}} if field == "target_ref" else {field: []}
    invalid = replace(plan, steps=(*plan.steps[:2], replace(work, **patch), *plan.steps[3:]))
    assert check_plan_against_package(invalid, package)[0]["code"] == "X1_PLAN_SCHEMA"


def test_static_waypoint_adapter_and_lowering_share_package_limits(profile):
    rng = random.Random(0xDE4)
    for _ in range(32):
        target = (rng.uniform(100, 420), rng.uniform(-120, 120))
        agl = rng.uniform(42, 115)
        package = load_mission_package({**package_doc(target, x1=True), "plan": x1_graph(agl)})
        assert check_plan_against_package(package.plan, package) == []
        view, plan, radius = one_waypoint_primitives_input(package.plan, package, CAPABILITIES)
        assert_executable(
            lower(view, dem(), profile, plan=plan, mission_radius_m=radius), dem(), target, agl
        )

    doc = package_doc(x1=True)
    doc["x1_planning"]["max_distance_from_home_m"] = 750
    package = load_mission_package(doc)
    with pytest.raises(X1PlanError, match="^RUNOUT_OUTSIDE_FENCE:"):
        lower(package, dem(), profile, plan=validate_mission_plan(legacy_graph()))

    doc = package_doc(x1=True)
    doc["x1_planning"]["corridor"]["north_max_m"] = 650
    package = load_mission_package(doc)
    with pytest.raises(X1PlanError, match="^RUNOUT_OUTSIDE_FENCE:"):
        lower(package, dem(), profile, plan=validate_mission_plan(legacy_graph()))


def test_lowering_accounts_for_both_transitions(profile):
    doc = package_doc(target=(100, 0))
    doc["budget"].update(energy_wh=45, reserve_energy_wh=10)
    with pytest.raises(X1PlanError, match="^ENERGY_BUDGET:"):
        lower(load_mission_package({**doc, "plan": legacy_graph()}), dem(), profile)


def new_decision(profile, action, mode, params):
    pkg = load_mission_package(package_doc(x1=True))
    vehicle = Mock(
        spec=["rtl", "land", "loiter_at", "set_mode", "vtol_transition", "gimbal_control"]
    )
    for name in ("loiter_at", "vtol_transition", "gimbal_control"):
        getattr(vehicle, name).return_value = CommandResult("accepted", 0, 0)
    claim = Mock(return_value=True)
    install = Mock(return_value=True)
    runner = X1AirbornePlanAdapter(1, replace_remaining=install)
    controller = X1ContingencyExecutor(
        runner, vehicle, pkg, X1Envelope(profile), claim_native=claim, capabilities=CAPABILITIES
    )
    request = controller.open_request(
        {
            "schema": "x1.contingency.state.v1",
            "mission_id": pkg.mission_id,
            "revision": 1,
            "t_ms": 1000,
            "latest_event_id": 1,
            "telemetry": {},
            "events": [{"id": 1, "t_ms": 990}],
        },
        run_id="seeded",
        hold_refs=("hold:yard",),
    )
    raw = {
        "schema": DECISION_SCHEMA,
        "mission_id": pkg.mission_id,
        "revision": 1,
        "event_id": 1,
        "request_id": request["request_id"],
        "action": action,
        "params": params,
    }
    hold = HoldSite("mc") if mode == "mc" else HoldSite("fw", 52.0, 5.0, 180.0, 55.0, 15.0)
    frame = RuntimeFrame(
        3,
        1050,
        pkg.mission_id,
        1,
        1,
        False,
        True,
        mode,
        80,
        15,
        True,
        {"hold:yard": hold},
        front_abort_enabled=True,
        gimbal_available=True,
    )
    return controller, vehicle, install, claim, raw, frame


def proof_for(digest):
    return MotionProof(
        frame_id=3,
        decision_digest=digest,
        geofence_clear=True,
        obstacle_margin_m=4,
        terrain_margin_m=6,
        required_energy_wh=10,
        available_energy_wh=60,
        min_airspeed_mps=12,
        max_airspeed_mps=18,
        max_bank_deg=35,
        max_climb_mps=3,
        max_descent_mps=2,
        max_acceleration_mps2=2,
        max_angular_rate_dps=60,
        required_time_s=100,
        available_time_s=500,
        landing_site_clear=True,
        min_transition_agl_m=30,
    )


@pytest.mark.parametrize(
    "action",
    ["continue", "return", "land-now", "retask", "loiter", "gimbal-retarget", "abort-transition"],
)
@pytest.mark.parametrize("mode", ["mc", "fw", "front_transition", "back_transition", "unknown"])
def test_randomized_decisions_never_select_a_substitute(action, mode, profile):
    rng = random.Random(0xA174)
    for _ in range(6):
        params = {}
        if action == "retask":
            params["plan"] = wire_plan(
                [terminal()]
                if mode == "mc"
                else [
                    step(
                        "work",
                        "fw_goto_waypoint",
                        "back",
                        target_ref="poi:yard",
                        altitude_agl_m=rng.uniform(40, 120),
                    ),
                    step("back", "back_transition", "home"),
                    terminal(),
                ]
            )
        elif action == "loiter":
            params["hold_ref"] = "hold:yard"
        elif action == "gimbal-retarget":
            params = {"pitch_deg": rng.uniform(-135, 45), "yaw_deg": rng.uniform(-360, 360)}
        ctl, vehicle, install, claim, raw, frame = new_decision(profile, action, mode, params)
        wire = json.dumps(raw)
        decision = decode_decision(wire, capabilities=CAPABILITIES)
        saved = deepcopy(decision)
        proof = proof_for(decision.digest)
        assert ctl.envelope.veto(
            decision, frame, proof, ctl.package, check_airborne_plan=ctl.mission.check_airborne_plan
        ) in (
            None,
            *{
                "flight_mode_or_position_unverified",
                "gimbal_transition_interlock",
                "transition_interlock",
                "transition_abort_not_admitted",
                "land_now_requires_mc",
            },
        )
        assert decision == saved
        assert not (vehicle.mock_calls or install.called or claim.called)
        result = ctl.apply(wire, frame, proof=proof, now_ms=1100)
        assert result.action == action
        allowed = (
            mode
            in {
                "continue": {"mc", "fw", "front_transition", "back_transition"},
                "abort-transition": {"front_transition"},
                "land-now": {"mc"},
                "return": {"mc", "fw"},
                "retask": {"mc", "fw"},
                "loiter": {"mc", "fw"},
                "gimbal-retarget": {"mc", "fw"},
            }[action]
        )
        if not allowed:
            if action == "gimbal-retarget":
                expected = "gimbal_transition_interlock"
            elif mode == "unknown":
                expected = "flight_mode_or_position_unverified"
            elif action == "abort-transition":
                expected = "transition_abort_not_admitted"
            elif mode.endswith("transition"):
                expected = "transition_interlock"
            else:
                expected = "land_now_requires_mc"
            assert (result.status, result.reason) == ("rejected", expected)
            assert not (vehicle.mock_calls or install.called or claim.called)
            assert ctl.native_owner is None and ctl.mission.plan_version == 1
            continue

        assert (
            result.status
            == {
                "continue": "continued",
                "retask": "installed",
                "return": "sent_unconfirmed",
                "land-now": "sent_unconfirmed",
                "loiter": "sent_unconfirmed" if mode == "mc" else "accepted_unconfirmed",
                "abort-transition": "accepted_unconfirmed",
                "gimbal-retarget": "accepted_unconfirmed",
            }[action]
        )
        assert install.called == (action == "retask")
        assert ctl.mission.plan_version == (2 if install.called else 1)
        assert claim.called == (action in {"return", "land-now", "loiter", "abort-transition"})
        assert ctl.native_owner == (action if claim.called else None)
        if action == "retask":
            install.assert_called_once_with(saved.plan, 1)
        expected_calls = {
            "continue": [],
            "retask": [],
            "return": [("rtl", (), {})],
            "land-now": [("land", (), {})],
            "loiter": [("set_mode", ("HOLD",), {})]
            if mode == "mc"
            else [("loiter_at", ((52.0, 5.0), 55.0, 180.0), {"speed": 15.0})],
            "abort-transition": [("vtol_transition", ("mc",), {})],
            "gimbal-retarget": [("gimbal_control", (), params)],
        }
        assert vehicle.mock_calls == expected_calls[action]


def test_contingency_rejects_bad_state_and_inconsistent_or_out_of_range_proof(profile):
    rng = random.Random(0x4051)
    cases = [
        ("airspeed_mps", value, "current_fw_airspeed_limit") for value in (*EXTREMES, 11.99, 18.01)
    ]
    cases += [
        ("vtol_state", value, "flight_mode_or_position_unverified") for value in (None, [], {})
    ]
    cases += [
        ("max_bank_deg", value, "predicted_dynamics_limit") for value in (*EXTREMES[:-2], 35.01)
    ]
    cases += [
        ("min_transition_agl_m", value, "predicted_transition_agl_limit")
        for value in (*EXTREMES, 29.99)
    ]
    cases += [
        ("bad_range", rng.uniform(15.01, 18), "predicted_fw_airspeed_limit") for _ in range(16)
    ]
    rng.shuffle(cases)
    for field, value, reason in cases:
        ctl, vehicle, install, claim, raw, frame = new_decision(profile, "return", "fw", {})
        wire = json.dumps(raw)
        proof = proof_for(decode_decision(wire).digest)
        if field in {"airspeed_mps", "vtol_state"}:
            frame = replace(frame, **{field: value})
        elif field == "bad_range":
            proof = replace(proof, min_airspeed_mps=value, max_airspeed_mps=15)
        else:
            proof = replace(proof, **{field: value})
        result = ctl.apply(wire, frame, proof=proof, now_ms=1100)
        assert (result.status, result.reason, result.action) == ("rejected", reason, "return"), (
            field,
            value,
        )
        assert not (vehicle.mock_calls or install.called or claim.called)
        assert ctl.mission.plan_version == 1 and ctl.native_owner is None


@pytest.mark.parametrize("agl", EXTREMES)
def test_invalid_retask_wire_never_reaches_dispatch(agl, profile):
    ctl, vehicle, install, claim, raw, frame = new_decision(
        profile,
        "retask",
        "fw",
        {
            "plan": wire_plan(
                [
                    step(
                        "work",
                        "fw_goto_waypoint",
                        "back",
                        target_ref="poi:yard",
                        altitude_agl_m=agl,
                    ),
                    step("back", "back_transition", "home"),
                    terminal(),
                ]
            )
        },
    )
    result = ctl.apply(json.dumps(raw), frame, now_ms=1100)
    assert (result.status, result.reason) == ("rejected", "invalid_decision")
    assert not (vehicle.mock_calls or install.called or claim.called)
    assert ctl.mission.plan_version == 1


@pytest.mark.parametrize("hook", ["request_retask", "apply_onboard_replan"])
def test_stock_executor_cannot_install_x1_primitives_via_retask_hooks(hook):
    original = validate_mission_plan(wire_plan([terminal()]))
    candidate = validate_mission_plan(x1_graph(), capabilities=CAPABILITIES)
    executor = MissionExecutor(original, loop_factory=Mock())
    before = (executor.plan, executor.plan_version, executor._run, list(executor.mission_log))
    args = {"expected_version": 1} if hook == "apply_onboard_replan" else {}
    malformed = replace(original, steps=())
    for plan, reason in ((candidate, "SIM primitive adapter"), (malformed, "no steps")):
        with pytest.raises(ValueError, match=reason):
            getattr(executor, hook)(plan, **args)
        assert (executor.plan, executor.plan_version, executor._run, executor.mission_log) == before
        assert executor._pending_retask is None


@pytest.mark.parametrize("state", ["ready", "queued", "complete", "stale"])
def test_stock_executor_preserves_admitted_plan_and_state_interlocks(state):
    rng = random.Random(0xC0DE)
    original = validate_mission_plan(wire_plan([terminal()]))
    for _ in range(12):
        executor = MissionExecutor(original, loop_factory=Mock())
        altitude = rng.uniform(5, 100)
        candidate = validate_mission_plan(legacy_graph(altitude))
        if state == "queued":
            executor.request_retask(original)
        elif state == "complete":
            executor.done = True
        before = executor._run, executor._pending_retask
        if state == "ready":
            executor.apply_onboard_replan(candidate, expected_version=1)
            assert executor.plan is candidate and executor.plan_version == 2
            assert executor._run.step is candidate.steps[0]
        else:
            with pytest.raises(ValueError, match="mission state changed"):
                executor.apply_onboard_replan(
                    candidate, expected_version=2 if state == "stale" else 1
                )
            assert executor.plan is original and executor.plan_version == 1
            assert (executor._run, executor._pending_retask) == before

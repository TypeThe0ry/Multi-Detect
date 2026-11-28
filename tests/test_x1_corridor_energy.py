"""Regression tests for the two X1 corridor energy budgets.

DESIGN-corridor.md section 6 A1-A6. The real ``x1_static_violations`` and the
real ``X1MotionProofBuilder`` are used as oracles; the pure model in
``x1_corridor_energy`` must agree with them rather than the other way round.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep
from multidetect.autonomy.x1_contingency_executor import Decision, RuntimeFrame
from multidetect.autonomy.x1_corridor_energy import (
    X1EnergyConstants,
    default_constants,
    max_one_way_m,
    plan_layer_energy_wh,
    proof_layer_available_wh,
    proof_layer_requirement_wh,
    proof_layer_reserve_wh,
    reverse_wind_ground_speed,
)
from multidetect.autonomy.x1_motion_proof import (
    ExecutionSweep,
    LandingPad,
    SweptLeg,
    X1MotionProofBuilder,
)
from multidetect.autonomy.x1_plan import x1_static_violations
from multidetect.sim_teacher.x1_fw_native_rtl import suffix_power_w
from test_x1_mission import _build, _package, _plan, profile, terrain  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
LONG_RANGE = ROOT / "configs/simulation/x1-longrange-mission.json"
AIRFRAME = ROOT / "configs/airframes/delta_tailsitter_gimbal.json"
PLANNER = ROOT / "configs/onboard/x1-planner.json"

# The committed long-range fixture leaves only ~2925 m at AGL60 before the
# north-east corridor edge binds. Widen the test-only package so the energy
# limit itself (3012 m) is observable, as in DESIGN-corridor.md 3.2.
TEST_CORRIDOR_MAX_M = 3200.0
TEST_AREA_MAX_M = 4000.0


def _long_range_package(*, one_way_m: float | None = None) -> object:
    doc = json.loads(LONG_RANGE.read_text(encoding="utf-8"))
    doc["task_text"] = "fly to the far marker and return"
    doc["operating_area"]["north_max_m"] = TEST_AREA_MAX_M
    doc["operating_area"]["east_max_m"] = TEST_AREA_MAX_M
    doc["x1_planning"]["corridor"]["north_max_m"] = TEST_CORRIDOR_MAX_M
    doc["x1_planning"]["corridor"]["east_max_m"] = TEST_CORRIDOR_MAX_M
    if one_way_m is not None:
        doc["places"]["poi:far-marker"] = [float(one_way_m), 0.0]
    return load_mission_package(
        doc, mode="sim", allow_assumed_sim=True, assumed_sim_max_distance_m=4000.0)


def _long_range_plan(*, agl_m: float, fw_back: bool) -> MissionPlan:
    far = MissionStep(
        "far", "fw_goto_waypoint",
        {"target_ref": "poi:far-marker", "altitude_agl_m": agl_m},
        "back2" if fw_back else "back", "home")
    steps = [
        MissionStep("takeoff", "vtol_takeoff", {"altitude_agl_m": agl_m}, "fw", "home"),
        MissionStep("fw", "fw_transition", {}, "far", "home"),
        far,
    ]
    if fw_back:
        steps.append(MissionStep(
            "back2", "fw_goto_waypoint", {"target_ref": "home", "altitude_agl_m": agl_m},
            "back", "home"))
    steps.extend([
        MissionStep("back", "back_transition", {}, "home", "home"),
        MissionStep("home", "return_home", {}, None, None),
    ])
    return MissionPlan(1, "fly to the marker and return", tuple(steps), ())


def _violations(one_way_m: float, *, agl_m: float, fw_back: bool) -> list[dict]:
    package = _long_range_package(one_way_m=one_way_m)
    caps = frozenset(package.document["capabilities"])
    return x1_static_violations(_long_range_plan(agl_m=agl_m, fw_back=fw_back), package, caps)


def _max_passing(agl_m: float, *, fw_back: bool) -> float:
    low, high = 100.0, 3999.0
    assert not _violations(low, agl_m=agl_m, fw_back=fw_back)
    assert _violations(high, agl_m=agl_m, fw_back=fw_back)
    for _ in range(50):
        middle = (low + high) / 2.0
        if not _violations(middle, agl_m=agl_m, fw_back=fw_back):
            low = middle
        else:
            high = middle
    return low


@pytest.mark.parametrize(
    ("agl_m", "fw_back", "expected_m"),
    [
        (60.0, True, 3012.0),
        (85.0, True, 2581.0),
        (120.0, True, 1977.0),
        (60.0, False, 709.0),
        (85.0, False, 607.0),
        (120.0, False, 465.0),
    ],
)
def test_plan_layer_matches_static_violations(agl_m, fw_back, expected_m):
    c = default_constants()
    real = _max_passing(agl_m, fw_back=fw_back)
    assert abs(real - expected_m) <= 1.0, (agl_m, fw_back, real)
    model = max_one_way_m(
        "plan", agl_m=agl_m, ground_high_m=25.0,
        return_mode="fw" if fw_back else "mc", constants=c)
    assert model == pytest.approx(real, abs=0.01)


def test_plan_formula_is_static_admission_energy():
    """The analytic plan value equals the expression x1_static_violations uses."""
    c = default_constants()
    profile_doc = json.loads(AIRFRAME.read_text(encoding="utf-8"))
    endurance = profile_doc["endurance"]
    for agl_m, fw_back in [(60.0, True), (85.0, True), (120.0, False)]:
        distance = 900.0
        energy = plan_layer_energy_wh(
            distance, agl_m=agl_m, ground_high_m=25.0,
            return_mode="fw" if fw_back else "mc", constants=c)
        height = agl_m + 25.0
        fw_s = (2 * distance if fw_back else distance) / c.ground_fw_plan_mps
        mc_distance = c.runout_m if fw_back else distance + c.runout_m
        mc_s = mc_distance / c.ground_mc_plan_mps
        climb_s, descent_s = height / c.max_climb_rate_mps, height / c.max_descent_rate_mps
        expected = 2 * endurance["transition_energy_wh"] + (
            climb_s * endurance["climb_power_w"] + descent_s * endurance["descent_power_w"]
            + fw_s * endurance["cruise_power_w"] + mc_s * endurance["hover_power_w"]
        ) / 3600.0
        assert energy == pytest.approx(expected)


def test_proof_layer_budget_table():
    """DESIGN-corridor.md 3.3: all eleven proof-layer points, +/-2 m."""
    c = default_constants()
    cases = [
        (50.0, 344.0, None, 1097.0),
        (50.0, 344.0, 252.5, 1758.0),
        (50.0, 172.0, None, 2195.0),
        (50.0, 172.0, 252.5, 3516.0),
        (85.0, 344.0, None, 716.0),
        (85.0, 344.0, 252.5, 1377.0),
        (85.0, 172.0, None, 1432.0),
        (85.0, 172.0, 252.5, 2753.0),
        (110.0, 344.0, None, 443.0),
        (110.0, 344.0, 252.5, 1104.0),
        (110.0, 172.0, 252.5, 2208.0),
    ]
    for height, fw_power, standoff, expected in cases:
        if height == 110.0:
            agl_m, ground_high = 85.0, 25.0
        else:
            # Table groups use H = AGL + relief; keep the same height split as
            # the design (50 = AGL40/10, 85 = AGL60/25).
            agl_m, ground_high = (40.0, 10.0) if height == 50.0 else (60.0, 25.0)
        model = max_one_way_m(
            "proof", agl_m=agl_m, ground_high_m=ground_high,
            fw_peak_power_w=fw_power, standoff_m=standoff, constants=c)
        assert abs(model - expected) <= 2.0, (height, fw_power, standoff, model)
        budget = (
            proof_layer_available_wh(agl_m=agl_m, ground_high_m=ground_high, constants=c)
            - proof_layer_reserve_wh(constants=c))
        requirement = proof_layer_requirement_wh(
            model, agl_m=agl_m, ground_high_m=ground_high,
            fw_peak_power_w=fw_power, standoff_m=standoff, constants=c)
        assert requirement == pytest.approx(budget, abs=0.01)


def _proof_builder(package, terrain, profile):
    steps = _build(package, terrain, profile)
    return X1MotionProofBuilder(
        package, profile, terrain, steps, mission_radius_m=1250,
        terrain_error_m=1, obstacle_clearance=lambda box: 25)


def _proof_required_wh(*, one_way_m, agl_m, ground_high_m, fw_peak, mc_return_m, builder, home_z):
    """Ask the real MotionProof builder for the same remaining route's energy."""
    c = default_constants()
    height = agl_m + ground_high_m
    fw_s = 2.0 * one_way_m / c.ground_fw_plan_mps
    mc_s = mc_return_m / c.ground_mc_plan_mps
    landing_s = height / c.max_descent_rate_mps
    start = (0.0, 0.0, home_z + height)
    a = (10.0, 0.0, home_z + height)
    b = (10.0, 0.0, home_z + height)
    d = (0.0, 0.0, home_z + height)
    legs = (
        SweptLeg("fw", start, a, 10, 1, fw_s, fw_peak),
        SweptLeg("transition", a, b, 10, 1, c.transition_timeout_s, c.mc_peak_power_w),
        SweptLeg("mc", b, d, 10, 1, mc_s, c.mc_peak_power_w),
        SweptLeg("landing", d, (0.0, 0.0, home_z), 10, 1, landing_s, c.mc_peak_power_w),
    )
    decision = Decision("continue", "req", 7, builder.package.mission_id, 1, "d" * 64, {})
    frame = RuntimeFrame(
        frame_id=123, t_ms=1010, mission_id=builder.package.mission_id, revision=1,
        event_id=7, px4_failsafe=False, armed_in_air=True, vtol_state="fw", agl_m=height,
        airspeed_mps=15, position_ok=True, hold_sites={}, position_ne_amsl=legs[0].start,
        active_motion_index=1, terrain_conflict=True, remaining_energy_wh=60,
        remaining_time_s=300)
    sweep = ExecutionSweep(
        frame.frame_id, decision.digest, legs, max_bank_deg=20, max_climb_mps=2,
        max_descent_mps=2, max_acceleration_mps2=1, max_angular_rate_dps=30,
        landing_pad=LandingPad((0.0, 0.0, home_z), 8, 2), failure_revalidation_enforced=True)
    proof = builder.build(
        decision, frame, sweep, installed_continue=True,
        retask_anchors=tuple((leg.phase, leg.end) for leg in legs))
    return proof.required_energy_wh


def test_proof_model_matches_motion_proof_builder(terrain, profile):  # noqa: F811
    package = _package(plan=_plan())
    builder = _proof_builder(package, terrain, profile)
    home_z = terrain.sample_local(0.0, 0.0).elevation_m
    c = default_constants()
    for one_way_m, agl_m, ground_high, fw_power, standoff in [
        # DESIGN-corridor.md 3.3: all eleven points, H = AGL + relief.
        (1097.0, 40.0, 10.0, 344.0, None),
        (1758.0, 40.0, 10.0, 344.0, 252.5),
        (2195.0, 40.0, 10.0, 172.0, None),
        (3516.0, 40.0, 10.0, 172.0, 252.5),
        (716.0, 60.0, 25.0, 344.0, None),
        (1377.0, 60.0, 25.0, 344.0, 252.5),
        (1432.0, 60.0, 25.0, 172.0, None),
        (2753.0, 60.0, 25.0, 172.0, 252.5),
        (443.0, 85.0, 25.0, 344.0, None),
        (1104.0, 85.0, 25.0, 344.0, 252.5),
        (2208.0, 85.0, 25.0, 172.0, 252.5),
    ]:
        mc_return_m = max(standoff, c.runout_m - standoff) if standoff is not None else c.runout_m
        model = proof_layer_requirement_wh(
            one_way_m, agl_m=agl_m, ground_high_m=ground_high,
            fw_peak_power_w=fw_power, mc_return_m=mc_return_m, constants=c)
        real = _proof_required_wh(
            one_way_m=one_way_m, agl_m=agl_m, ground_high_m=ground_high,
            fw_peak=fw_power, mc_return_m=mc_return_m, builder=builder, home_z=home_z)
        assert real == pytest.approx(model)


def test_proof_precheck_rejects_before_takeoff():
    """OP-A at the standoff gate: 2 km passes planning but exceeds the proof budget."""
    c = default_constants()
    one_way_m = 2000.0
    conditions = dict(agl_m=60.0, ground_high_m=25.0, constants=c)
    route = dict(standoff_m=c.runout_m / 2.0, **conditions)
    assert plan_layer_energy_wh(one_way_m, **route) <= c.usable_energy_wh
    assert not _violations(one_way_m, agl_m=60.0, fw_back=True)
    budget = proof_layer_available_wh(**conditions) - proof_layer_reserve_wh(constants=c)
    assert proof_layer_requirement_wh(one_way_m, **route) > budget


@pytest.mark.parametrize(("agl_m", "fw_back"), [(60.0, True), (85.0, True), (120.0, False)])
def test_energy_one_metre_over_limit_is_rejected(agl_m, fw_back):
    c = default_constants()
    mode = "fw" if fw_back else "mc"
    dmax = max_one_way_m("plan", agl_m=agl_m, ground_high_m=25.0, return_mode=mode, constants=c)
    assert not _violations(dmax - 1.0, agl_m=agl_m, fw_back=fw_back)
    codes = {finding["code"] for finding in _violations(dmax + 1.0, agl_m=agl_m, fw_back=fw_back)}
    assert codes == {"X1_ENERGY"}


def test_proof_energy_one_metre_over_limit():
    c = default_constants()
    conditions = dict(agl_m=60.0, ground_high_m=25.0, constants=c)
    route = dict(standoff_m=c.runout_m / 2.0, **conditions)
    dmax = max_one_way_m("proof", **route)
    budget = proof_layer_available_wh(**conditions) - proof_layer_reserve_wh(constants=c)
    assert proof_layer_requirement_wh(dmax - 1.0, **route) <= budget
    assert proof_layer_requirement_wh(dmax + 1.0, **route) > budget


def test_reverse_wind_ground_speed_and_time_penalty():
    c = default_constants()
    calm = reverse_wind_ground_speed(constants=c)
    tailwind = reverse_wind_ground_speed(outbound_tailwind_mps=5.0, constants=c)
    assert calm == pytest.approx(c.cruise_airspeed_mps - c.wind_margin_mps)
    assert tailwind == pytest.approx(8.0)
    distance = 3000.0
    assert distance / tailwind == pytest.approx(1.5 * (distance / calm))
    assert distance / tailwind == pytest.approx(375.0)
    assert reverse_wind_ground_speed(outbound_tailwind_mps=7.0, constants=c) == 6.0
    with pytest.raises(ValueError, match="WIND_EXCEEDS_RETURN"):
        reverse_wind_ground_speed(outbound_tailwind_mps=8.0, constants=c)
    # A missing estimate is not a zero reading with uncertainty attached.
    assert reverse_wind_ground_speed(wind_sigma_mps=5.0, constants=c) == pytest.approx(calm)


def test_invariant_first_continue_implies_later_return():
    """Hold the wind bound fixed, with flown legs within their time/power bounds."""
    rng = random.Random(20261128)
    c = default_constants()
    for _ in range(500):
        agl_m = rng.uniform(40.0, 90.0)
        ground_high = rng.uniform(0.0, 25.0)
        standoff = rng.uniform(100.0, 400.0)
        fw_peak = rng.choice([c.fw_peak_power_w, c.loiter_power_w])
        reverse_ground = reverse_wind_ground_speed(
            outbound_tailwind_mps=rng.uniform(-5.0, 7.0), constants=c)
        proof_inputs = dict(
            agl_m=agl_m, ground_high_m=ground_high, standoff_m=standoff,
            fw_peak_power_w=fw_peak, fw_return_ground_speed_mps=reverse_ground, constants=c)
        available = proof_layer_available_wh(
            agl_m=agl_m, ground_high_m=ground_high, constants=c)
        reserve = proof_layer_reserve_wh(constants=c)
        limit = max_one_way_m("proof", **proof_inputs)
        assert limit > 0.0
        destination = rng.uniform(0.5, 0.999999) * limit
        assert proof_layer_requirement_wh(destination, **proof_inputs) <= available - reserve

        # Use prefix distance along the corridor, including outbound points where
        # the distance to home grows. Include the turn with no remaining outbound
        # energy to absorb a return-budget regression.
        for outbound_m, inbound_m in (
            (destination * rng.random(), 0.0),
            (destination, 0.0),
            (destination, destination * rng.random()),
        ):
            used_wh = fw_peak * (
                outbound_m / c.ground_fw_plan_mps + inbound_m / reverse_ground) / 3600.0
            return_wh = proof_layer_requirement_wh(
                outbound_m - inbound_m, fw_outbound_remaining_m=0.0, **proof_inputs)
            assert used_wh + return_wh <= available - reserve


def test_energy_calculations_do_not_read_configs(monkeypatch):
    c = default_constants()

    def fail_read(*args, **kwargs):
        pytest.fail("energy calculations must not read files")

    monkeypatch.setattr(Path, "read_text", fail_read)
    assert plan_layer_energy_wh(100.0, agl_m=60.0, constants=c) > 0
    assert proof_layer_requirement_wh(100.0, agl_m=60.0, constants=c) > 0
    assert max_one_way_m("proof", agl_m=60.0, constants=c) > 0
    assert reverse_wind_ground_speed(constants=c) > 0


def test_constants_match_pinned_sources():
    c = default_constants()
    assert c == X1EnergyConstants.from_configs()
    profile_doc = json.loads(AIRFRAME.read_text(encoding="utf-8"))
    planner_doc = json.loads(PLANNER.read_text(encoding="utf-8"))
    endurance = profile_doc["endurance"]
    fw_power, mc_power = suffix_power_w(endurance)
    assert c.fw_peak_power_w == fw_power == 344.0
    assert c.mc_peak_power_w == mc_power == 750.0
    assert c.transition_energy_wh == endurance["transition_energy_wh"]
    assert c.battery_wh == endurance["battery_wh"]
    assert c.wind_margin_mps == endurance["wind_margin_mps"]
    assert c.cruise_airspeed_mps == profile_doc["cruise_airspeed_mps"]
    assert c.max_airspeed_mps == profile_doc["max_airspeed_mps"]
    assert c.transition_timeout_s == profile_doc["transition"]["timeout_s"]
    assert c.transition_position_margin_m == planner_doc["transition_position_margin_m"]
    assert c.runout_m == pytest.approx(505.0)
    assert c.usable_energy_wh == pytest.approx(59.2)
    assert c.ground_fw_plan_mps == pytest.approx(12.0)
    assert c.ground_mc_plan_mps == pytest.approx(5.0)
    assert math.isclose(c.fw_peak_power_w, 2 * endurance["loiter_power_w"])

def test_max_one_way_with_corridor_template_fw_power():
    """344 W (2 x loiter) vs the corridor template's 500 W (power_est eta=0.75 P95)."""
    template = json.loads((ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json")
                          .read_text(encoding="utf-8"))["assumed_sim_corridor"]
    template_power = template["fw_leg_peak_power_w"]
    assert template_power == 500.0
    c = default_constants()
    # (H, AGL, relief, standoff, max one-way at 344 W, at template power), +/-2 m.
    for height, agl_m, ground_high, standoff, at_344, at_template in [
        (50.0, 40.0, 10.0, None, 1097.0, 755.0),
        (50.0, 40.0, 10.0, 252.5, 1758.0, 1209.5),
        (85.0, 60.0, 25.0, None, 716.0, 492.5),
        (85.0, 60.0, 25.0, 252.5, 1377.0, 947.0),
        (110.0, 85.0, 25.0, None, 443.0, 305.0),
        (110.0, 85.0, 25.0, 252.5, 1104.0, 759.5),
    ]:
        for power, expected in ((344.0, at_344), (template_power, at_template)):
            got = max_one_way_m(
                "proof", agl_m=agl_m, ground_high_m=ground_high,
                fw_peak_power_w=power, standoff_m=standoff, constants=c)
            assert abs(got - expected) <= 2.0, (height, standoff, power, got)
        # A higher FW leg power can only shrink the provable distance.
        assert at_template < at_344

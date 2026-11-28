"""C22 coverage for the X1 airborne adapter's corridor-only FW suffix veto."""

from dataclasses import replace

import pytest

from multidetect.autonomy.mission_package import CorridorBounds, MissionPackage
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep
from multidetect.autonomy.x1_contingency_executor import RuntimeFrame, X1AirbornePlanAdapter


HOME = (800.0, -300.0)
FAR = (1040.0, -120.0)  # 300 m from home, with neither axis exceeding standoff.


@pytest.fixture
def package():
    # Minimal package for the adapter. Home is deliberately outside the local origin.
    return MissionPackage(
        document={"home": HOME},
        refs={
            "poi:far": FAR,
            "poi:near": (830.0, -260.0),
            "poi:at-tolerance": (800.0, -47.49),  # 252.51 m from home.
            "poi:beyond-tolerance": (800.0, -47.48),  # 252.52 m from home.
            "poi:home-alias": HOME,
            "route:ends-far": (HOME, FAR),
            "route:ends-home": (FAR, HOME),
        },
        plan=None,
        mode="sim",
        assumed_sim_corridor=CorridorBounds(
            max_fw_legs=4,
            slice_m=100.0,
            standoff_m=252.5,
            standoff_gate_enabled=False,
            turn_pad_rule="2*R_min + fly_through + fw_tracking",
            fw_tracking_m=15.0,
            fw_leg_peak_power_w=500.0,
            mc_peak_power_w=750.0,
            min_ground_speed_mps=12.0,
            wind_sigma_mps=2.0,
            max_sweep_legs=256,
        ),
    )


@pytest.fixture
def fw_frame():
    return RuntimeFrame(
        frame_id=1, t_ms=100, mission_id="c22", revision=1, event_id=1,
        px4_failsafe=False, armed_in_air=True, vtol_state="fw", agl_m=60.0,
        airspeed_mps=15.0, position_ok=True, hold_sites={},
    )


def _goto(ref):
    return "fw_goto_waypoint", {"target_ref": ref, "altitude_agl_m": 60.0}


def _plan(*commands):
    ids = tuple(f"s{i}" for i in range(len(commands)))
    return MissionPlan(1, "retask in flight", tuple(
        MissionStep(sid, skill, params, ids[i + 1], ids[-1]) if i + 1 < len(ids)
        else MissionStep(sid, skill, params, None, None)
        for i, (sid, (skill, params)) in enumerate(zip(ids, commands, strict=True))
    ), ())


BACK = ("back_transition", {})
RETURN = ("return_home", {})


def test_live_retask_requires_fw_home_leg(package, fw_frame):
    plan = _plan(_goto("poi:near"), _goto("poi:far"), BACK, RETURN)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) == "invalid_fw_retask_suffix"


def test_live_retask_accepts_explicit_fw_home_leg(package, fw_frame):
    plan = _plan(_goto("poi:far"), _goto("home"), BACK, RETURN)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) is None


@pytest.mark.parametrize(("target", "reason"), [
    ("poi:near", None),
    ("poi:at-tolerance", None),
    ("poi:beyond-tolerance", "invalid_fw_retask_suffix"),
])
def test_live_retask_uses_last_fw_endpoint_and_standoff_tolerance(package, fw_frame, target, reason):
    plan = _plan(_goto("poi:far"), _goto(target), BACK, RETURN)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) == reason


@pytest.mark.parametrize(("route_ref", "reason"), [
    ("route:ends-far", "invalid_fw_retask_suffix"),
    ("route:ends-home", None),
])
def test_live_retask_uses_search_routes_last_point(package, fw_frame, route_ref, reason):
    plan = _plan(("x1_search_leg", {"route_ref": route_ref}), BACK, RETURN)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) == reason


@pytest.mark.parametrize("commands", [
    (_goto("poi:home-alias"), _goto("poi:far"), BACK, RETURN),
    (_goto("poi:far"), BACK, _goto("home"), RETURN),
], ids=["home-coordinate-alias", "home-goto-after-back-transition"])
def test_live_retask_rejects_invalid_home_leg(package, fw_frame, commands):
    plan = _plan(*commands)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) == "invalid_fw_retask_suffix"


def test_live_retask_preserves_legacy_single_waypoint_suffix(package, fw_frame):
    plan = _plan(_goto("poi:far"), BACK, RETURN)
    legacy = replace(package, assumed_sim_corridor=None)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame) is None
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=legacy) is None
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) == "invalid_fw_retask_suffix"


def test_home_visit_before_ending_far_does_not_satisfy_recovery(package, fw_frame):
    plan = _plan(_goto("home"), _goto("poi:far"), BACK, RETURN)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, fw_frame, package=package) == "invalid_fw_retask_suffix"


@pytest.mark.parametrize("position,reason", [((800, -300, 160), None), ((900, 0, 160), "invalid_fw_retask_suffix"),
                                            (None, "invalid_fw_retask_suffix")])
def test_no_fw_work_uses_live_fix(package, fw_frame, position, reason):
    plan = _plan(BACK, RETURN)
    frame = replace(fw_frame, position_ne_amsl=position)
    assert X1AirbornePlanAdapter.check_airborne_plan(plan, frame, package=package) == reason

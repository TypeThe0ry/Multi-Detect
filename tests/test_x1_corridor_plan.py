"""Static-admission corridor branch for X1 (DESIGN-corridor.md section 7 step 3).

The single-waypoint path is covered by the unchanged tests in
``test_x1_planner.py``; this file exercises only ``assumed_sim_corridor``
packages and keeps ``standoff_gate_enabled`` at its default ``False`` except
for the explicit dispatch test.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep
from multidetect.autonomy.x1_corridor_energy import default_constants, max_one_way_m
from multidetect.autonomy.x1_plan import corridor_primitives_input, x1_static_violations

ROOT = Path(__file__).resolve().parents[1]
CORRIDOR_PATH = ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json"
CORRIDOR = json.loads(CORRIDOR_PATH.read_text(encoding="utf-8"))["assumed_sim_corridor"]


def _package(places, *, terrain=(0.0, 10.0), corridor=CORRIDOR, area=(-3500.0, 3500.0),
             rect=(-3000.0, 3000.0), zones=None, budget=None, max_distance=4000.0):
    document = {
        "mission_id": "x1-corridor-test", "task_text": "fly the corridor",
        "risk_level": "low", "link_loss_policy": {"action": "return_now"},
        "home": [0.0, 0.0], "places": places, "zones": zones or {}, "routes": {},
        "ref_labels": {ref: ref for ref in places},
        "capabilities": ["airframe:x1", "gimbal_control"], "oversight_steps": [],
        "operating_area": {
            "north_min_m": area[0], "north_max_m": area[1],
            "east_min_m": area[0], "east_max_m": area[1],
            "min_altitude_m": 15.0, "max_altitude_m": 145.0,
        },
        "budget": budget or {
            "time_s": 2400.0, "reserve_time_s": 240.0,
            "energy_wh": 200.0, "reserve_energy_wh": 14.8,
        },
        "x1_planning": {
            "world": "rw_farm_iowa_w6km__clear_noon",
            "max_distance_from_home_m": max_distance,
            "corridor": {
                "north_min_m": rect[0], "north_max_m": rect[1],
                "east_min_m": rect[0], "east_max_m": rect[1],
            },
            "terrain_rel_home_m": list(terrain),
        },
    }
    return load_mission_package(
        document, mode="sim", allow_assumed_sim=True,
        assumed_sim_max_distance_m=max_distance, assumed_sim_corridor=corridor)


def _plan(refs, *, agls=None, takeoff_agl=60.0):
    """takeoff -> fw_transition -> fw_goto(refs...) -> back_transition -> return_home."""
    agls = agls or [takeoff_agl] * len(refs)
    steps = [
        MissionStep("takeoff", "vtol_takeoff", {"altitude_agl_m": takeoff_agl}, "fw", "home"),
        MissionStep("fw", "fw_transition", {}, "w0", "home"),
    ]
    for index, ref in enumerate(refs):
        success = f"w{index + 1}" if index + 1 < len(refs) else "back"
        steps.append(MissionStep(
            f"w{index}", "fw_goto_waypoint",
            {"target_ref": ref, "altitude_agl_m": agls[index]}, success, "home"))
    steps.append(MissionStep("back", "back_transition", {}, "home", "home"))
    steps.append(MissionStep("home", "return_home", {}, None, None))
    return MissionPlan(1, "fly the corridor", tuple(steps), ())


def _findings(package, refs, **kwargs):
    plan = _plan(refs, **kwargs)
    return x1_static_violations(plan, package, frozenset(package.document["capabilities"]))


def _codes(package, refs, **kwargs):
    return {finding["code"] for finding in _findings(package, refs, **kwargs)}


def _single_waypoint_plan():
    return MissionPlan(1, "yard", (
        MissionStep("takeoff", "vtol_takeoff", {"altitude_agl_m": 60.0}, "fw", "home"),
        MissionStep("fw", "fw_transition", {}, "w", "home"),
        MissionStep("w", "fw_goto_waypoint",
                    {"target_ref": "poi:yard", "altitude_agl_m": 60.0}, "back", "home"),
        MissionStep("back", "back_transition", {}, "home", "home"),
        MissionStep("home", "return_home", {}, None, None),
    ), ())


def test_corridor_template_defaults_to_gate_off():
    assert CORRIDOR["standoff_gate_enabled"] is False
    assert CORRIDOR["fw_leg_peak_power_w"] == 344.0
    assert CORRIDOR["standoff_m"] == 252.5


def test_default_standoff_gate_does_not_relax_the_mc_return_item():
    """The proof precheck keeps the runout MC item unless the gate is enabled."""
    constants = default_constants()
    runout_limit = max_one_way_m("proof", agl_m=60.0, ground_high_m=10.0, constants=constants)
    gate_limit = max_one_way_m(
        "proof", agl_m=60.0, ground_high_m=10.0, standoff_m=252.5, constants=constants)
    distance = (runout_limit + gate_limit) / 2.0
    package = _package({"poi:far": [distance, 0.0]})

    assert package.assumed_sim_corridor.standoff_gate_enabled is False
    findings = _findings(package, ["poi:far", "home"])
    assert "X1_ENERGY_PROOF" in {finding["code"] for finding in findings}
    # The planning layer still passes: the two budget layers stay separate.
    assert "X1_ENERGY" not in {finding["code"] for finding in findings}

    gated = replace(
        package,
        assumed_sim_corridor=replace(package.assumed_sim_corridor, standoff_gate_enabled=True))
    assert "X1_ENERGY_PROOF" not in _codes(gated, ["poi:far", "home"])


def test_proof_energy_one_metre_over_limit():
    constants = default_constants()
    limit = max_one_way_m("proof", agl_m=60.0, ground_high_m=10.0, constants=constants)
    under = _package({"poi:far": [limit - 1.0, 0.0]})
    over = _package({"poi:far": [limit + 1.0, 0.0]})
    assert "X1_ENERGY_PROOF" not in _codes(under, ["poi:far", "home"])
    assert "X1_ENERGY_PROOF" in _codes(over, ["poi:far", "home"])


def test_dogleg_energy_uses_path_length_not_displacement():
    straight = _package({"poi:a": [700.0, 0.0]})
    dogleg = _package({"poi:a": [0.0, 700.0], "poi:b": [700.0, 700.0]})
    assert "X1_ENERGY_PROOF" not in _codes(straight, ["poi:a", "home"])
    assert "X1_ENERGY_PROOF" in _codes(dogleg, ["poi:a", "poi:b", "home"])


def test_leg_count_over_max_is_rejected():
    package = _package({"poi:a": [300.0, 0.0], "poi:b": [500.0, 0.0]})
    tight = replace(
        package,
        assumed_sim_corridor=replace(package.assumed_sim_corridor, max_fw_legs=2))
    codes = _codes(tight, ["poi:a", "poi:b", "home"])
    assert "X1_CORRIDOR_LEGS" in codes


def test_short_leg_is_rejected():
    package = _package({"poi:a": [500.0, 0.0], "poi:b": [510.0, 0.0]})
    assert "X1_MIN_LEG" in _codes(package, ["poi:a", "poi:b", "home"])


def test_middle_segment_outside_corridor_reports_its_leg():
    package = _package(
        {"poi:a": [400.0, 0.0], "poi:b": [1100.0, 0.0], "poi:c": [0.0, 400.0]},
        rect=(-1000.0, 1000.0))
    findings = _findings(package, ["poi:a", "poi:b", "poi:c", "home"])
    messages = [finding["message_en"] for finding in findings]
    assert "X1_CORRIDOR" in {finding["code"] for finding in findings}
    assert any(message.startswith("leg 2 leaves") for message in messages)
    assert any(message.startswith("leg 3 leaves") for message in messages)
    assert not any(message.startswith("leg 1 leaves") for message in messages)
    assert not any(message.startswith("leg 4 leaves") for message in messages)


def test_middle_segment_through_no_fly_zone_is_rejected():
    zones = {"zone:restricted": {"centre_ne": [600.0, 0.0], "radius_m": 50.0,
                                 "risk_tag": "no-fly"}}
    package = _package({"poi:a": [400.0, 0.0], "poi:b": [800.0, 0.0]}, zones=zones)
    findings = _findings(package, ["poi:a", "poi:b", "home"])
    assert "X1_NO_FLY" in {finding["code"] for finding in findings}
    assert any(finding["message_en"].startswith("leg 2 intersects")
               for finding in findings if finding["code"] == "X1_NO_FLY")


def test_agl_out_of_band_on_one_leg_is_rejected():
    package = _package({"poi:a": [400.0, 0.0], "poi:b": [800.0, 0.0]}, terrain=(0.0, 30.0))
    assert "X1_TERRAIN" in _codes(package, ["poi:a", "poi:b", "home"], agls=[60.0, 120.0, 60.0])


def test_return_too_short_only_when_gate_is_enabled():
    # 300 m is short of standoff 252.5 + minimum leg 50, but the gate is off by default.
    package = _package({"poi:f": [300.0, 0.0]})
    assert "X1_RETURN_TOO_SHORT" not in _codes(package, ["poi:f", "home"])
    gated = replace(
        package,
        assumed_sim_corridor=replace(package.assumed_sim_corridor, standoff_gate_enabled=True))
    assert "X1_RETURN_TOO_SHORT" in _codes(gated, ["poi:f", "home"])


def test_turn_pad_outside_corridor_is_rejected():
    # A reversal at north=240 m needs a turn pad that reaches past north=300 m.
    package = _package({"poi:a": [240.0, 0.0], "poi:b": [100.0, 0.0]}, rect=(-300.0, 300.0))
    assert "X1_CORRIDOR_TURN" in _codes(package, ["poi:a", "poi:b", "home"])


def test_multi_waypoint_without_corridor_is_not_statically_rejected():
    package = _package({"poi:a": [300.0, 0.0], "poi:b": [500.0, 0.0]}, corridor=None)
    assert _findings(package, ["poi:a", "poi:b", "home"]) == []


def test_corridor_primitives_input_dispatch():
    package = _package({"poi:a": [300.0, 0.0], "poi:b": [500.0, 0.0]})
    plan = _plan(["poi:a", "poi:b", "home"])
    capabilities = frozenset(package.document["capabilities"])
    assert _findings(package, ["poi:a", "poi:b", "home"]) == []
    with pytest.raises(ValueError, match="CORRIDOR_NOT_ENABLED"):
        corridor_primitives_input(plan, package, capabilities)

    no_corridor = _package({"poi:a": [300.0, 0.0], "poi:b": [500.0, 0.0]}, corridor=None)
    with pytest.raises(ValueError, match="CORRIDOR_NOT_ENABLED"):
        corridor_primitives_input(plan, no_corridor, capabilities)

    single = _package({"poi:yard": [250.0, 0.0]})
    view, adapted, radius = corridor_primitives_input(
        _single_waypoint_plan(), single, frozenset(single.document["capabilities"]))
    assert view.plan == adapted and len(adapted.steps) == 2
    assert radius == single.document["x1_planning"]["max_distance_from_home_m"]

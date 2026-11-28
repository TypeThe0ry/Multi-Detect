"""CPU replay of the Iowa dogleg's geometry and fail-closed mode boundaries."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from multidetect.autonomy.mission_package import check_plan_against_package, load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.motion_intent import resolve_airframe_profile
from multidetect.autonomy.x1_contingency_executor import Decision, RuntimeFrame, X1Envelope
from multidetect.autonomy.x1_corridor import build_x1_corridor_steps
from multidetect.autonomy.x1_mission import X1_PROFILE_ID
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from multidetect.terrain_dem import load_local_dem_grid
from scripts.run_x1_autonomy_demo import verified_assumed_corridor

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json"
MISSION = ROOT / "configs/simulation/x1-corridor-dogleg-mission.json"
DEM = ROOT / "simulation/terrain/rw_farm_iowa_w6km__clear_noon.dem.json"


@pytest.fixture(scope="module", params=["47m-original", "75m-k-candidate"])
def route(request):
    original = request.param == "47m-original"
    # K is the adopted short dogleg: 75 m AGL, (60, 0) -> (30, 52). The old
    # 47 m route stays as the reject-side control. K's ~0.085 Wh model margin
    # on request #12 is a mechanism check, not a generalizable range budget.
    p1, p2, altitude = ((300, 0), (555, 190), 47) if original else ((60, 0), (30, 52), 75)
    radius, corridor = verified_assumed_corridor(TEMPLATE)
    bounds = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    sha = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    doc = json.loads(MISSION.read_text(encoding="utf-8"))
    doc["task_text"] = "Fly the two beacons and the home waypoint, then land."
    doc["places"].update({"poi:first-beacon": list(p1), "poi:second-beacon": list(p2)})
    terrain = load_local_dem_grid(DEM)
    profile = resolve_airframe_profile(X1_PROFILE_ID)
    scope = bounds["scope"]["scenarios"]["nominal"]
    battery = bounds["battery"]["value"]
    cap = bounds["battery_capacity_wh"]["value"]
    doc["budget"]["battery_capacity_wh"] = cap
    # Use the actual DEM and template; the inventory below is a test fixture, not a site survey.
    # This unit test does not authorize the trial geometry or replace the site inventory.
    doc["x1_live_evidence"] = {
        "basis": "ASSUMED_SIM", "assumed_bounds_sha256": sha,
        "scope_sha256": scope, "dem_sha256": terrain.source_sha256,
        "dem_error_m": bounds["dem_error_m"]["value"],
        "obstacle_coverage": [-2999.99, 2999.99, -2999.99, 2999.99, 118.23, 1318.23],
        "obstacles": [[-250.0, -240.0, -440.0, -430.0, 318.0, 325.0]],
        "landing_pad": bounds["landing_pad"]["value"],
        "battery": {"max_voltage_v": battery["max_voltage_v"],
                    "soc_error_pct": battery["nominal_soc_error_pct"],
                    "consumption_error_wh": battery["consumption_error_wh"]},
        "suffixes": {key: entry["value"] for key, entry in bounds["suffixes"].items()},
    }
    package = load_mission_package(doc, mode="sim", allow_assumed_sim=True,
                                   assumed_sim_max_distance_m=radius, assumed_sim_corridor=corridor)
    caps = frozenset(doc["capabilities"])
    route_steps = [
        ("takeoff", "vtol_takeoff", {"altitude_agl_m": altitude}),
        ("front", "fw_transition", {}),
        ("first", "fw_goto_waypoint", {"target_ref": "poi:first-beacon", "altitude_agl_m": altitude}),
        ("second", "fw_goto_waypoint", {"target_ref": "poi:second-beacon", "altitude_agl_m": altitude}),
        ("over_home", "fw_goto_waypoint", {"target_ref": "home", "altitude_agl_m": altitude}),
        ("back", "back_transition", {}),
        ("home", "return_home", {}),
    ]
    plan = validate_mission_plan({"plan_version": 1, "goal": "Iowa CPU clearance replay",
                                 "report": ["status"], "steps": [
        {"id": step_id, "skill": skill, "params": params,
         "on_success": route_steps[i + 1][0] if i + 1 < len(route_steps) else None,
         "on_fail": "home" if step_id != "home" else None}
        for i, (step_id, skill, params) in enumerate(route_steps)
    ]}, capabilities=caps)
    assert check_plan_against_package(plan, package, capabilities=caps) == []
    lowered = build_x1_corridor_steps(package, plan=plan, terrain=terrain, profile=profile,
                                      mission_radius_m=radius)
    assert lowered.runout_m == 505.0 and lowered.gate_ne is None
    evidence = LiveContinueEvidence(package, profile, terrain, lowered.steps, scope=scope,
                                    assumed_sim={"sha256": sha, "capacity_wh": cap}, corridor=lowered)
    return evidence, X1Envelope(profile), original


def _replay(route, index, mode, *, airspeed=15.0, energy=67.68, seconds=467.292):
    evidence, envelope, _ = route
    lowered, package = evidence.corridor, evidence.builder.package
    if index in {1, 2, 3}:
        leg = lowered.legs[index - 1]
        point = tuple((a + b) / 2 for a, b in zip(leg.start_amsl, leg.end_amsl, strict=True))
    elif index == 5:
        point = evidence._corridor_runout_end()
    else:
        point = lowered.legs[-1].end_amsl
    local = dict(x=point[0], y=point[1], z=-lowered.height_m, vx=airspeed, vy=0.0, vz=0.0)
    entered = evidence.check_entry(index, point, local)
    decision = Decision("continue", "cpu", 1, package.mission_id, 1, "d" * 64, {})
    frame = RuntimeFrame(1, 1000, package.mission_id, 1, 1, False, True, mode, lowered.height_m,
                         airspeed, True, {}, position_ne_amsl=entered, active_motion_index=index,
                         remaining_energy_wh=energy, remaining_time_s=seconds)
    proof = evidence.build_proof(decision, frame)
    return proof, envelope.veto(decision, frame, proof, package)


@pytest.mark.parametrize(("index", "energy", "seconds"), [
    (1, 67.68, 467.292), (2, 66.96, 447.316), (3, 64.08, 397.492), (4, 64.08, 388.092),
])
def test_dogleg_fw_cruise_continue_k_admitted_old_rejected(route, index, energy, seconds):
    """K admits every FW cruise entry; the old 47 m route stays rejected.

    Only FW cruise entries (leg indices 1-4, including the still-FW pre-
    back_transition entry 4) are covered. This is the coordinator's accepted
    criterion; no other phase is claimed.
    """
    _, _, original = route
    proof, veto = _replay(route, index, "fw", energy=energy, seconds=seconds)
    assert proof.geofence_clear and proof.landing_site_clear
    assert proof.obstacle_margin_m > 0
    assert math.isfinite(proof.required_energy_wh)
    if original:
        assert veto == "unverified_clearance"
        assert proof.terrain_margin_m == pytest.approx(-23.95, abs=0.01)
    else:
        assert veto is None
        assert proof.terrain_margin_m > 0
        assert energy - proof.required_energy_wh >= 14.8
        assert seconds - proof.required_time_s >= 90


@pytest.mark.parametrize(("index", "mode"), [
    (None, "mc"), (0, "mc"), (0, "front_transition"),
    (4, "back_transition"), (5, "mc"), (None, "mc"),
], ids=["takeoff", "before-front", "front-transition", "back-transition", "mc-return", "landing"])
def test_dogleg_geometry_does_not_create_a_missing_continue_proof(route, index, mode):
    """Takeoff/MC, front transition, back_transition, MC return and landing have
    no corridor-continue sweep: the proof builder is not covered for them. They
    stay unverified regardless of the K geometry; this is not a clearance
    observation and no such phase is claimed as admitted.
    """
    proof, veto = _replay(route, index, mode, airspeed=6.9)
    assert veto == "unverified_clearance"
    assert not proof.geofence_clear
    assert proof.terrain_margin_m is proof.obstacle_margin_m is proof.required_energy_wh is None


def test_dogleg_geometry_does_not_make_overspeed_admissible(route):
    _, veto = _replay(route, 3, "fw", airspeed=19.55)
    assert veto == "current_fw_airspeed_limit"


def test_mission_adopts_the_k_geometry_and_names():
    from scripts import run_x1_closed_loop_suite as suite

    document = json.loads(MISSION.read_text(encoding="utf-8"))
    assert document["places"] == {
        "poi:first-beacon": [60, 0],
        "poi:second-beacon": [30, 52],
    }
    assert document["ref_labels"] == {
        "poi:first-beacon": "first beacon",
        "poi:second-beacon": "second beacon",
    }
    assert document["home"] == [0.0, 0.0]
    # Home, fence rectangle, budget and reserve are intentionally unchanged.
    assert document["x1_planning"]["corridor"] == {
        "north_min_m": -650.0, "north_max_m": 900.0,
        "east_min_m": -650.0, "east_max_m": 650.0,
    }
    assert document["budget"] == {
        "time_s": 540.0, "reserve_time_s": 90.0,
        "energy_wh": 74.0, "reserve_energy_wh": 14.8,
    }
    assert "north" in suite.CORRIDOR_DOGLEG_SENTENCE
    assert "northeast" in suite.CORRIDOR_DOGLEG_SENTENCE
    assert "75 m AGL" in suite.CORRIDOR_DOGLEG_SENTENCE

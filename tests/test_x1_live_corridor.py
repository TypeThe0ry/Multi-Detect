"""Exercise corridor proofs through live entry, sweep generation and executor veto."""
import math
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_executor import X1AirbornePlanAdapter, X1Envelope
from multidetect.autonomy.x1_mission import lower_airborne_suffix
from multidetect.autonomy.x1_wind_estimate import WindReading
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from test_x1_corridor_retask_suffix import BACK, RETURN, _goto, _plan
from test_x1_live_clearance import SCOPE, _document
from test_x1_mission import _build as _build_single, _plan as _single_plan
from multidetect.autonomy.mission_plan import validate_mission_plan
from test_x1_motion_proof_corridor import (
    CORRIDOR, _decision, _frame, _lowering, _package, profile, terrain,
)

SHA = "a" * 64
LOCAL = {"x": 1, "y": 0, "z": -60, "vx": 8, "vy": 0, "vz": 0}
LIMITS = dict(vertical_tracking_m=10, orbit_tracking_m=40, fw_tracking_m=55,
              max_ground_speed_mps=21, finding_error_m=12, fw_s=40, orbit_entry_s=28,
              transition_s=12, mc_s=90, landing_s=85)


def _live(terrain, profile, *, corridor=CORRIDOR, failure=True, obstacles=(), places=None):
    places = {"poi:out": [500, 0], "poi:dogleg": [500, 500]} if places is None else places
    seed = _package(places, corridor=corridor)
    bounds = _document(terrain)["x1_live_evidence"]
    bounds.update(basis="ASSUMED_SIM", assumed_bounds_sha256=SHA,
                  obstacle_coverage=[-2000, 2000, -2000, 2000, 80, 250], obstacles=list(obstacles),
                  landing_pad={"radius_m": 20, "elevation_error_m": 10})
    bounds["suffixes"]["1"]["failure_revalidation_enforced"] = failure
    fw = bounds["suffixes"]["1"]
    bounds["suffixes"]["2"] = {**fw, "entry": [-5, 5, -5, 5, 155, 165], "legs": fw["legs"][1:]}
    document = seed.document
    document["budget"]["battery_capacity_wh"] = 74
    # The synthetic recipe uses the real 74 Wh profile for lowering and a wide
    # obstacle survey. Use generous frame budgets when testing geometry alone.
    document["budget"].update(energy_wh=74, reserve_energy_wh=14.8)
    document["x1_live_evidence"] = bounds
    package = load_mission_package(document, mode="sim", allow_assumed_sim=True,
                                   assumed_sim_max_distance_m=4000, assumed_sim_corridor=corridor)
    lowering = _lowering(package, [*places, "home"], terrain, profile)
    evidence = LiveContinueEvidence(package, profile, terrain, lowering.steps, scope=SCOPE,
                                    assumed_sim={"sha256": SHA, "capacity_wh": 74, "airborne_retask": LIMITS},
                                    corridor=lowering)
    return evidence, lowering, package


def _tick(evidence, package, point, index, mode, *, wind=None, route=None):
    frame = _frame(package, point, index, mode=mode)
    key = None
    if route is not None:
        evidence.installed_at = lambda _: route
        key = ("installed-continue", route.revision, route.plan_sha256, index)
        frame = replace(frame, revision=route.revision, installed_plan_sha256=route.plan_sha256)
    kwargs = {"corridor_wind": wind} if getattr(evidence, "corridor", None) is not None else {}
    admitted = evidence.check_entry(index, point, LOCAL, installed_key=key, **kwargs)
    frame = replace(frame, position_ne_amsl=admitted)
    decision = replace(_decision(package), revision=frame.revision)
    return evidence.build_proof(decision, frame), frame, decision


def _assert_open(proof):
    assert proof.geofence_clear and proof.landing_site_clear
    assert proof.required_time_s > 0 and proof.required_energy_wh > 0
    assert proof.obstacle_margin_m > 0 and proof.terrain_margin_m >= 0


@pytest.mark.parametrize("gate", [False, True])
def test_live_continue_every_command(terrain, profile, gate):
    evidence, lowering, package = _live(terrain, profile, corridor=replace(CORRIDOR, standoff_gate_enabled=gate))
    for leg in lowering.legs:
        point = tuple((x + y) / 2 for x, y in zip(leg.start_amsl, leg.end_amsl, strict=True))
        proof, frame, decision = _tick(evidence, package, point, leg.index, "fw")
        _assert_open(proof)
        sweep = evidence._corridor_continue_sweep(decision, frame, leg.index, point)
        assert sweep.legs[0].start == point
        assert [x.end for x in sweep.legs if x.phase == "fw" and x.command_end] == [
            rest.end_amsl for rest in lowering.legs[leg.index - 1:]]
        assert all(x.peak_power_w == 500 for x in sweep.legs if x.phase == "fw")
    last = lowering.legs[-1]
    _assert_open(_tick(evidence, package, last.end_amsl, last.index + 1, "fw")[0])
    _assert_open(_tick(evidence, package, evidence._corridor_runout_end(), last.index + 2, "mc")[0])
    point = (250, 0, lowering.cruise_msl)
    assert _tick(evidence, package, point, 2, "fw")[0].required_time_s is None
    assert _tick(evidence, package, point, 0, "fw")[0].required_time_s is None


def test_turn_disk_obstacle_and_failure_witness(terrain, profile):
    evidence, lowering, package = _live(terrain, profile, obstacles=([580, 585, -82, -78, 150, 170],))
    point = (250, 0, lowering.cruise_msl)
    proof, frame, decision = _tick(evidence, package, point, 1, "fw")
    assert proof.obstacle_margin_m == -1.0
    assert X1Envelope(profile).veto(decision, frame, proof, package) is not None
    evidence, _, package = _live(terrain, profile, failure=False)
    assert _tick(evidence, package, point, 1, "fw")[0].required_time_s is None


def test_live_continue_reverse_wind_and_later_ticks(terrain, profile):
    evidence, lowering, package = _live(terrain, profile, corridor=replace(CORRIDOR, standoff_gate_enabled=True))
    last = lowering.legs[-1]
    wind = WindReading((5 / math.sqrt(2), 5 / math.sqrt(2), None), 1_000_000_000, 0, None)
    stale = replace(wind, stamp_ns=-1_000_000_000)
    start = (250, 0, lowering.cruise_msl)
    def remaining(point, index, sample):
        proof, frame, decision = _tick(evidence, package, point, index, "fw", wind=sample)
        _assert_open(proof)
        assert X1Envelope(profile).veto(decision, frame, proof, package) is None
        return proof, evidence._corridor_continue_sweep(decision, frame, index, point)
    fallback, base = remaining(start, 1, stale)
    ref, _ = remaining(start, 1, None)
    assert fallback == ref
    first, adjusted = remaining(start, 1, wind)
    def last_fw_time(sweep):
        total = 0.0
        for leg in sweep.legs:
            if leg.phase != "fw":
                break
            total += leg.duration_s
            if leg.command_end and leg.end != last.end_amsl:
                total = 0.0
        return total
    assert last_fw_time(adjusted) == pytest.approx(1.5 * last_fw_time(base))
    assert first.required_energy_wh > fallback.required_energy_wh
    previous = first
    for fraction in (0.1, 0.4, 0.7, 1.0):
        point = tuple(a + (b - a) * fraction for a, b in zip(last.start_amsl, last.end_amsl, strict=True))
        later, _ = remaining(point, last.index, wind)
        assert later.required_time_s <= previous.required_time_s + 1e-9
        assert later.required_energy_wh <= previous.required_energy_wh + 1e-9
        previous = later
    too_strong = replace(wind, ned_mps=(8 / math.sqrt(2), 8 / math.sqrt(2), None))
    assert _tick(evidence, package, start, 1, "fw", wind=too_strong)[0].required_time_s is None


@pytest.mark.parametrize("gate", [False, True])
def test_live_continue_remaining_demand_is_monotone_by_leg(terrain, profile, gate):
    # A6: a passing first continue tick implies every later continue tick fits
    # the same energy/time budget as the route gets shorter.
    places = {"poi:out": [300, 0], "poi:dogleg": [300, 300]}
    evidence, lowering, package = _live(terrain, profile, places=places,
                                        corridor=replace(CORRIDOR, standoff_gate_enabled=gate))
    previous = None
    ticks = [(leg.start_amsl, leg.index, "fw") for leg in lowering.legs]
    ticks.append((lowering.legs[-1].end_amsl, len(lowering.legs) + 1, "fw"))
    ticks.append((evidence._corridor_runout_end(), len(lowering.legs) + 2, "mc"))
    for point, index, mode in ticks:
        proof, frame, decision = _tick(evidence, package, point, index, mode)
        _assert_open(proof)
        assert X1Envelope(profile).veto(decision, frame, proof, package) is None
        if previous is not None:
            assert proof.required_time_s <= previous.required_time_s + 1e-9
            assert proof.required_energy_wh <= previous.required_energy_wh + 1e-9
        previous = proof


def test_installed_continue_compares_only_command_ends(terrain, profile, monkeypatch):
    evidence, lowering, package = _live(terrain, profile)
    start = (140, 80, lowering.cruise_msl)
    base = _frame(package, start, 9, mode="fw")
    plan = _plan(_goto("poi:out"), _goto("poi:dogleg"), _goto("home"), BACK, RETURN)
    steps = lower_airborne_suffix(package, plan, base, terrain=terrain, profile=profile, limits=LIMITS)
    route = SimpleNamespace(steps=steps, plan_sha256=SHA, revision=2, boundary=9,
                            finding_step_ids=frozenset(), finding_error_m=None)
    original, frame, decision = _tick(evidence, package, start, 9, "fw", route=route)
    _assert_open(original)
    assert (original.plan_revision, original.plan_sha256) == (2, SHA)
    assert X1Envelope(profile).veto(decision, frame, replace(original, plan_sha256="b" * 64), package) == (
        "missing_or_unbound_motion_proof")
    real = evidence._airborne_legs

    def mutate(command_end):
        def changed(*args, **kwargs):
            legs, anchors = real(*args, **kwargs)
            i = next(i for i, leg in enumerate(legs) if leg.phase == "fw" and leg.command_end is command_end)
            n, e, z = legs[i].end
            point = (n, e + 0.5, z)
            legs[i:i+2] = [replace(legs[i], end=point), replace(legs[i+1], start=point)]
            return legs, anchors
        return changed
    monkeypatch.setattr(evidence, "_airborne_legs", mutate(False))
    _assert_open(evidence.build_proof(decision, frame))
    monkeypatch.setattr(evidence, "_airborne_legs", mutate(True))
    assert evidence.build_proof(decision, frame).required_time_s is None


@pytest.mark.parametrize("mode,action", [("fw", "return"), ("mc", "return"), ("mc", "land-now"), ("fw", "loiter")])
def test_native_recovery_and_hold_stay_closed(terrain, profile, mode, action):
    evidence, lowering, package = _live(terrain, profile)
    index = len(lowering.legs) + (1 if mode == "fw" else 2)
    _, frame, decision = _tick(evidence, package, (0, 0, lowering.cruise_msl), index, mode)
    proof = evidence.build_proof(replace(decision, action=action), frame)
    assert proof.required_energy_wh is None and not proof.geofence_clear


def test_live_retask_uses_adapter_and_stages_only_a_verified_shape(terrain, profile):
    evidence, lowering, package = _live(terrain, profile)
    index = len(lowering.legs) + 1
    point = lowering.legs[-1].end_amsl
    _, frame, decision = _tick(evidence, package, point, index, "fw")
    frame = replace(frame, retask_boundary=True)
    staged = []
    evidence.stage_retask = lambda *args, **kwargs: staged.append((args, kwargs))

    def retask(*commands):
        plan = _plan(*commands, BACK, RETURN)
        return replace(decision, action="retask", plan=plan, params={"plan": asdict(plan)})

    missing = retask(_goto("poi:out"))
    closed = evidence.build_proof(missing, frame)
    assert X1Envelope(profile).veto(missing, frame, closed, package,
                                    check_airborne_plan=X1AirbornePlanAdapter.check_airborne_plan) == (
        "invalid_fw_retask_suffix")
    assert staged == []
    decision = retask(_goto("poi:out"), _goto("home"))
    proof = evidence.build_proof(decision, frame)
    _assert_open(proof)
    assert X1Envelope(profile).veto(decision, frame, proof, package,
                                    check_airborne_plan=X1AirbornePlanAdapter.check_airborne_plan) is None
    assert len(staged) == 1
    assert evidence.reprove_installed_retask(decision, frame, staged[0][0][2]) == proof


def _single_waypoint_golden(terrain, profile):
    doc = _document(terrain)
    doc["x1_live_evidence"]["landing_pad"]["elevation_error_m"] = 10
    package = load_mission_package(doc, mode="sim")
    steps = _build_single(replace(package, plan=validate_mission_plan(_single_plan())), terrain, profile)
    evidence = LiveContinueEvidence(package, profile, terrain, steps, scope=SCOPE)
    assert getattr(evidence, "corridor", None) is None
    proof, _, _ = _tick(evidence, package, (205, 0, 180), 1, "fw")
    _assert_open(proof)
    return canonical_sha256({"steps": [asdict(step) for step in steps], "proof": asdict(proof)})


def test_single_waypoint_live_golden(terrain, profile):
    assert _single_waypoint_golden(terrain, profile) == "d975594ad8df4e2262cf24e593083cedd99589e0412f50a27677193ed8781cd0"


def test_demo_scenario_binds_the_same_corridor_lowering(terrain, profile, monkeypatch):
    from scripts import run_x1_autonomy_demo as demo

    evidence, lowering, package = _live(terrain, profile)
    monkeypatch.setattr(demo, "load_airframe_profile", lambda _: profile)
    monkeypatch.setattr(demo, "llama_decisions", lambda *_: None)
    args = SimpleNamespace(decision_cadence_s=1, decision_port=1, sentence="fly the corridor")
    mission = {"scenario_faults": (), "scenario_package": package, "terrain": terrain,
               "corridor_lowering": lowering, "assumed_sim": evidence.assumed_sim}
    scenario = demo.make_scenario(args, mission, lowering.steps,
                                  {"contingency_scenario": {"motion_scope_sha256": SCOPE}})
    assert scenario.motion_evidence.corridor is lowering
    first = lowering.legs[0]
    _assert_open(_tick(scenario.motion_evidence, package, first.start_amsl, first.index, "fw")[0])

"""Step-7 corridor native return and Hold obligations (design C23, E31-E33)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.x1_contingency_executor import X1Envelope
from multidetect.autonomy.x1_corridor import slice_leg
from multidetect.autonomy.x1_motion_proof import _segment_distance
from multidetect.sim_teacher.x1_fw_native_rtl import FwRtlBounds, PX4_RTL_COMMIT
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from multidetect.sim_teacher import x1_fw_native_rtl
from test_x1_live_clearance import SCOPE, _document
from test_x1_live_corridor import LIMITS
from test_x1_motion_proof_corridor import (
    CORRIDOR, _decision, _frame, _lowering, _package, profile, terrain,
)

HOLD = "hold:fw"


def _params(*, return_alt=60):
    return {
        "SYS_AUTOSTART": 4051, "VT_TYPE": 0, "RTL_TYPE": 0, "RTL_CONE_ANG": 0,
        "RTL_LAND_DELAY": 0, "RTL_APPR_FORCE": 0, "RTL_PLD_MD": 0,
        "MPC_XY_CRUISE": 5, "MPC_Z_V_AUTO_DN": 1.5, "MPC_LAND_SPEED": 0.7, "NAV_ACC_RAD": 2,
        "RTL_RETURN_ALT": return_alt, "RTL_DESCEND_ALT": 73, "RTL_LOITER_RAD": 80,
        "NAV_LOITER_RAD": 80, "VT_B_TRANS_DUR": 4, "VT_B_DEC_MSS": 2, "FW_R_LIM": 35,
        "FW_AIRSPD_MIN": 12, "FW_AIRSPD_TRIM": 15, "FW_AIRSPD_MAX": 18,
    }


def _manifest(scope, *, return_alt=60, fw_power=344, times=(15, 50, 30, 20, 5, 45, 110)):
    """Complete v2 manifest fields, kept as a plain dict for host-manifest tests."""
    return {
        "scope": scope, "px4_commit": PX4_RTL_COMMIT, "image_id": "sha256:" + "b" * 64,
        "validation_sha256": "c" * 64, "params": _params(return_alt=return_alt),
        "fw_tracking_m": 20, "fw_vertical_m": 3, "fw_acceptance_m": 10,
        "move_to_land_vertical_m": 2, "transition_vertical_m": 4,
        "mc_tracking_m": 12, "mc_vertical_m": 2, "landing_tracking_m": 4, "landing_vertical_m": 2,
        "max_ground_speed_mps": 27, "phase_time_s": list(times),
        "fw_peak_power_w": fw_power, "hover_peak_power_w": 750, "max_bank_deg": 35,
        "max_climb_mps": 3, "max_descent_mps": 2, "max_acceleration_mps2": 2,
        "max_angular_rate_dps": 60, "home_only_verified": True, "configuration_locked": True,
    }


def _v2(scope, *, return_alt=60, fw_power=344, times=(15, 50, 30, 20, 5, 45, 110)):
    return FwRtlBounds.from_host_manifest({
        **_manifest(scope, return_alt=return_alt, fw_power=fw_power, times=times),
        "connector_ground_speed_min_mps": 12.0, "manifest_version": 2,
    })


def _legacy(scope, *, return_alt=60):
    return FwRtlBounds.from_host_manifest(_manifest(scope, return_alt=return_alt))


def _midpoint(leg):
    return ((leg.start_ne[0] + leg.end_ne[0]) / 2.0,
            (leg.start_ne[1] + leg.end_ne[1]) / 2.0)


def _hold_raw(terrain, centre, cruise, steps):
    return {
        "mode": "fw", "steps": list(steps),
        "entry": [centre[0] - 105, centre[0] + 105, centre[1] - 105, centre[1] + 105,
                  cruise - 5, cruise + 5],
        "max_entry_speed_mps": 22.0, "padding_ne_m": 100.0, "padding_alt_m": 5.0,
        "duration_s": 60.0, "peak_power_w": 500.0,
        "dynamics": {"max_bank_deg": 35, "max_climb_mps": 2, "max_descent_mps": 2,
                     "max_acceleration_mps2": 1, "max_angular_rate_dps": 30},
        "centre": [centre[0], centre[1], cruise], "radius_m": 80.0,
        "tangential_speed_mps": 15.0, "airspeed": [12.0, 18.0],
    }


def _live(terrain, profile, places, *, fw_rtl=None, zones=None, hold_centre=None,
          review_kind="corridor", has_review=True, native_mc=False, approvals=None):
    seed = _package(places, corridor=CORRIDOR)
    seed_lowering = _lowering(seed, [*places, "home"], terrain, profile)
    if hold_centre is None:
        hold_centre = _midpoint(seed_lowering.legs[0])
    document = json.loads(json.dumps(seed.document))
    if zones is not None:
        document["zones"] = zones
    bounds = _document(terrain)["x1_live_evidence"]
    bounds.update(basis="ASSUMED_SIM", assumed_bounds_sha256="a" * 64,
                  obstacle_coverage=[-4000, 4000, -4000, 4000, 80, 300], obstacles=[],
                  landing_pad={"radius_m": 150, "elevation_error_m": 5})
    suffix = bounds["suffixes"]["1"]
    suffix["failure_revalidation_enforced"] = True
    bounds["suffixes"]["2"] = {**suffix, "entry": [-5, 5, -5, 5, 155, 165],
                               "legs": suffix["legs"][1:]}
    bounds["native"] = {"holds": {HOLD: _hold_raw(
        terrain, hold_centre, seed_lowering.cruise_msl, range(1, len(seed_lowering.legs) + 1))}}
    if native_mc:
        factory = _hold_raw(terrain, (0, 0), seed_lowering.cruise_msl, [1])
        bounds["native"]["land-now"] = {"3": {
            "max_entry_speed_mps": 5, "padding_ne_m": 4, "padding_alt_m": 2,
            "duration_s": 100, "peak_power_w": 750, "dynamics": factory["dynamics"],
        }}
        bounds["native"]["return"] = {"3": {**suffix, "entry": [-10, 10, -10, 10, 155, 165],
            "max_entry_speed_mps": 5, "legs": suffix["legs"][-2:], "airspeed": None}}
        bounds["native"]["holds"]["hold:local"] = {
            **{key: factory[key] for key in ("max_entry_speed_mps", "padding_alt_m", "duration_s", "dynamics")},
            "mode": "mc", "steps": [len(seed_lowering.legs) + 2],
            "padding_ne_m": 4, "peak_power_w": 750,
            "entry": [-10, 10, -10, 10, 155, 165],
        }
    document["budget"]["battery_capacity_wh"] = 74
    document["budget"].update(energy_wh=74, reserve_energy_wh=14.8)
    document["x1_live_evidence"] = bounds
    package = load_mission_package(
        document, mode="sim", allow_assumed_sim=True, assumed_sim_max_distance_m=4000,
        assumed_sim_corridor=CORRIDOR)
    lowering = _lowering(package, [*places, "home"], terrain, profile)
    assumed = {"sha256": "a" * 64, "capacity_wh": 74, "airborne_retask": LIMITS,
               "image_id": "sha256:" + "a" * 64, "parameters": {}}
    if fw_rtl is not None:
        if fw_rtl.manifest_version == 2 and has_review:
            review = json.dumps({
                "basis": "ASSUMED_SIM", "status": "reviewed", "scope": fw_rtl.scope,
                "image_id": fw_rtl.image_id, "px4_commit": fw_rtl.px4_commit,
                "fw_airspeed_parameters": {key: fw_rtl.params[key] for key in
                                          ("FW_AIRSPD_MIN", "FW_AIRSPD_TRIM", "FW_AIRSPD_MAX")},
                "scope_kind": review_kind, "bounds_manifest_version": 2,
                "assumed_bounds_sha256": assumed["sha256"],
                "bounds_sha256": fw_rtl.review_bounds_sha256(),
            }).encode()
            assumed["fw_rtl_review"] = review
            fw_rtl = replace(fw_rtl, validation_sha256=hashlib.sha256(review).hexdigest())
            if approvals is not None:
                approvals.add(fw_rtl.validation_sha256)
        assumed["fw_rtl"] = fw_rtl
    evidence = LiveContinueEvidence(package, profile, terrain, lowering.steps, scope=SCOPE,
                                    assumed_sim=assumed, corridor=lowering)
    evidence.fw_rtl_ready = evidence.fw_rtl is not None
    return evidence, lowering, package


@pytest.fixture
def approvals(monkeypatch):
    """Test-only stand-in for an independently reviewed validation list."""
    digests = set()
    monkeypatch.setattr(x1_fw_native_rtl, "_REVIEWED_CORRIDOR_VALIDATIONS", digests)
    return digests


def _local(point):
    return {"x": point[0], "y": point[1], "z": -(point[2] - 100.0),
            "vx": 15.0, "vy": 0.0, "vz": 0.0}


def _home_obs(terrain):
    return (0.0, 0.0, terrain.sample_local(0.0, 0.0).elevation_m)


PLACES = {"poi:a": [500, 0], "poi:b": [500, 500], "poi:c": [0, 500]}
FAR = {"poi:a": [2500, 0], "poi:b": [2500, 800], "poi:c": [0, 800]}


# E31 -- one chord slice crossing a no-fly zone must not offer a native return.
def test_native_rtl_chord_crossing_no_fly_zone_is_not_offered(terrain, profile, approvals):
    centre = (300, 120)
    zones = {"zone:nfz": {"centre_ne": list(centre), "radius_m": 3, "risk_tag": "no_fly"}}
    manifest = _v2(SCOPE, return_alt=45)
    clear, _, _ = _live(terrain, profile, PLACES, fw_rtl=manifest, hold_centre=(500, 250), approvals=approvals)
    evidence, lowering, package = _live(
        terrain, profile, PLACES, fw_rtl=manifest, zones=zones, hold_centre=(500, 250), approvals=approvals)
    assert evidence._corridor_rtl is not None
    assert evidence.builder.corridor_rtl_clear(1, (100.0, 0.0, lowering.cruise_msl))
    point = (500.0, 250.0, lowering.cruise_msl)
    entry, = [item for item in lowering.leg_slices[1] if evidence.builder._in_box(item.box(), point)]
    chord = slice_leg(entry.start, (0.0, 0.0, point[2]), 0.0, 0.0, 1.0, 500.0,
                      package.assumed_sim_corridor.slice_m)
    # The zone intersects the centreline in exactly one chord slice. Its
    # conservative tracking envelope can overlap more than one hazard box.
    assert sum(_segment_distance(piece.start[:2], piece.end[:2], centre) <= 6.0 for piece in chord) == 1
    assert clear.check_entry(2, point, _local(point), confirmed_hold_ref=HOLD,
                             observed_home=_home_obs(terrain)) == point
    assert ("return", 2) in clear._entry[1]
    assert not evidence.builder.corridor_rtl_clear(2, point)

    admitted = evidence.check_entry(2, point, _local(point), confirmed_hold_ref=HOLD,
                                    observed_home=_home_obs(terrain))
    assert admitted == point
    assert ("continue", 2) in evidence._entry[1]
    assert ("return", 2) not in evidence._entry[1]
    assert evidence._fw_rtl_entry is None

    frame = replace(_frame(package, admitted, 2, mode="fw"), native_hold_active=True,
                    hold_sites=evidence.hold_sites(2, "fw"), airspeed_mps=15.0)
    decision = replace(_decision(package), action="return")
    proof = evidence.build_proof(decision, frame)
    assert proof.required_time_s is None
    assert X1Envelope(profile).veto(decision, frame, proof, package) is not None


# E32 -- a legacy single-waypoint manifest is never corridor-ready; v2 scales with chord.
def test_fw_rtl_manifest_versions_and_distance_scaled_connector(terrain, profile):
    legacy = _legacy(SCOPE)
    assert legacy.manifest_version == 1
    assert legacy.valid_for(SCOPE, profile)
    assert legacy.corridor_ready(min_cruise_rel_home_m=60.0) is False
    assert not legacy.valid_for(SCOPE, profile, corridor=True)
    assert legacy.connector_time_s(3500.0) is None

    revised = _v2(SCOPE)
    assert revised.manifest_version == 2 and revised.corridor_ready(min_cruise_rel_home_m=60.0)
    assert revised.connector_time_s(3500.0) >= 3500.0 / revised.connector_ground_speed_min_mps
    assert revised.connector_time_s(3500.0) > revised.connector_time_s(1000.0)

    with pytest.raises(ValueError):
        FwRtlBounds.from_host_manifest({**_manifest(SCOPE), "manifest_version": 2,
                                        "connector_ground_speed_min_mps": None})
    with pytest.raises(ValueError):
        FwRtlBounds.from_host_manifest({**_manifest(SCOPE),
                                        "connector_ground_speed_min_mps": 12.0})

    evidence, lowering, package = _live(terrain, profile, PLACES, fw_rtl=legacy,
                                        hold_centre=(250, 0))
    assert evidence.fw_rtl is None and evidence._corridor_rtl is None
    point = (250.0, 0.0, lowering.cruise_msl)
    admitted = evidence.check_entry(1, point, _local(point), confirmed_hold_ref=HOLD,
                                    observed_home=_home_obs(terrain))
    assert admitted == point
    assert ("return", 1) not in evidence._entry[1]
    frame = replace(_frame(package, admitted, 1, mode="fw"), native_hold_active=True,
                    hold_sites=evidence.hold_sites(1, "fw"), airspeed_mps=15.0)
    decision = replace(_decision(package), action="return")
    assert evidence.build_proof(decision, frame).required_time_s is None


# E33 -- RTL_RETURN_ALT above the corridor cruise altitude would need a climb.
def test_rtl_return_alt_above_corridor_is_rejected(terrain, profile, approvals):
    evidence, lowering, package = _live(terrain, profile, PLACES, fw_rtl=_v2(SCOPE, return_alt=61),
                                        hold_centre=(250, 0), approvals=approvals)
    assert evidence.builder.corridor_min_cruise_rel_home_m == pytest.approx(60.0)
    assert evidence.builder.corridor_rtl_ready(return_alt_m=61.0, descend_alt_m=73.0, loiter_radius_m=80.0, pad=evidence.pad) is False
    assert evidence.builder.corridor_rtl_ready(return_alt_m=60.0, descend_alt_m=73.0, loiter_radius_m=80.0, pad=evidence.pad) is True
    assert evidence.fw_rtl is None and evidence._corridor_rtl is None
    assert evidence.hold_sites(1, "fw") == {}
    point = (250.0, 0.0, lowering.cruise_msl)
    evidence.check_entry(1, point, _local(point), confirmed_hold_ref=HOLD,
                         observed_home=_home_obs(terrain))
    assert ("return", 1) not in evidence._entry[1]


# R6 -- no reviewed corridor manifest means no corridor return, but continue works.
def test_corridor_return_closed_without_reviewed_manifest(terrain, profile):
    evidence, lowering, package = _live(terrain, profile, PLACES, fw_rtl=None, hold_centre=(250, 0))
    assert evidence.fw_rtl is None and evidence._corridor_rtl is None
    assert evidence.hold_sites(1, "fw") == {}
    point = (250.0, 0.0, lowering.cruise_msl)
    admitted = evidence.check_entry(1, point, _local(point), confirmed_hold_ref=HOLD,
                                    observed_home=_home_obs(terrain))
    assert admitted == point
    assert ("continue", 1) in evidence._entry[1] and ("return", 1) not in evidence._entry[1]
    frame = _frame(package, admitted, 1, mode="fw")
    proof = evidence.build_proof(_decision(package), frame)
    assert proof.geofence_clear and proof.required_time_s > 0


# C23 -- residence may fit while residence + post-Hold return does not.
def test_corridor_hold_requires_post_hold_return_energy(terrain, profile, approvals):
    evidence, lowering, package = _live(
        terrain, profile, FAR, fw_rtl=_v2(SCOPE, return_alt=45, times=(15, 50, 20, 12, 5, 20, 40)),
        hold_centre=(800, 0), approvals=approvals)
    assert evidence._corridor_rtl is not None
    point = (800.0, 0.0, lowering.cruise_msl)
    admitted = evidence.check_entry(1, point, _local(point))
    assert admitted == point
    assert ("hold", HOLD) in evidence._entry[1]
    frame = replace(_frame(package, admitted, 1, mode="fw"),
                    hold_sites=evidence.hold_sites(1, "fw"), airspeed_mps=15.0,
                    remaining_energy_wh=200.0)
    decision = replace(_decision(package), action="loiter", params={"hold_ref": HOLD})
    proof = evidence.build_proof(decision, frame)
    residence_wh = 60.0 * 500.0 / 3600.0
    reserve_wh = 14.8
    assert proof.required_energy_wh > residence_wh
    assert X1Envelope(profile).veto(decision, frame, proof, package) is None

    available = residence_wh + reserve_wh + 0.1
    assert available < proof.required_energy_wh + reserve_wh
    tight = replace(frame, remaining_energy_wh=available)
    closed = evidence.build_proof(decision, tight)
    assert closed.required_energy_wh == pytest.approx(proof.required_energy_wh)
    assert X1Envelope(profile).veto(decision, tight, closed, package) == "energy_or_reserve_limit"


# Corridor Hold stations are registered per FW leg, still capped at eight sites.
def test_corridor_hold_steps_cover_every_fw_leg(terrain, profile):
    from multidetect.sim_teacher.x1_live_clearance import parse_native

    raw = {"holds": {HOLD: _hold_raw(terrain, (0.0, 0.0), 160.0, [4])}}
    assert set(parse_native(raw, terrain, corridor_fw_legs=4)[2]) == {HOLD}
    with pytest.raises(ValueError):
        parse_native(raw, terrain, corridor_fw_legs=3)
    with pytest.raises(ValueError):
        parse_native({"holds": {f"hold:{i}": _hold_raw(terrain, (0.0, 0.0), 160.0, [1])
                                for i in range(9)}}, terrain, corridor_fw_legs=4)


# The home RTL loiter disk must fit the corridor and surveyed coverage.
def test_home_loiter_disk_must_fit_corridor(terrain, profile, approvals):
    bounds = replace(_v2(SCOPE), params={**_v2(SCOPE).params, "RTL_LOITER_RAD": 10_000.0})
    evidence, _, _ = _live(terrain, profile, PLACES, fw_rtl=bounds, hold_centre=(250, 0), approvals=approvals)
    assert evidence.fw_rtl is None
    assert bounds.valid_for(SCOPE, profile, corridor=True)
    assert evidence.builder.corridor_rtl_ready(return_alt_m=60.0, descend_alt_m=73.0, loiter_radius_m=10_000.0, pad=evidence.pad) is False
    assert evidence._corridor_rtl is None


@pytest.mark.parametrize("review_kind, has_review, scope", [
    ("corridor", False, SCOPE), ("iowa-single-waypoint", True, SCOPE), ("corridor", True, "0" * 64),
])
def test_unreviewed_or_mismatched_candidate_keeps_return_closed_and_continue_available(
        terrain, profile, approvals, review_kind, has_review, scope):
    evidence, lowering, package = _live(
        terrain, profile, FAR, fw_rtl=_v2(scope, return_alt=45), hold_centre=(800, 0),
        review_kind=review_kind, has_review=has_review, approvals=approvals)
    point = (800.0, 0.0, lowering.cruise_msl)
    assert evidence.check_entry(1, point, _local(point), confirmed_hold_ref=HOLD,
                                observed_home=_home_obs(terrain)) == point
    assert evidence.hold_sites(1, "fw") == {}
    assert ("return", 1) not in evidence._entry[1]
    assert evidence.build_proof(_decision(package), _frame(package, point, 1)).geofence_clear


def test_corridor_review_binds_the_entire_manifest(terrain, profile, approvals):
    evidence, _, _ = _live(terrain, profile, FAR, fw_rtl=_v2(SCOPE, return_alt=45), approvals=approvals)
    bounds, raw = evidence.fw_rtl, evidence.assumed_sim["fw_rtl_review"]
    assert bounds.reviewed_corridor(raw, assumed_sha256=evidence.assumed_sim["sha256"])
    assert not bounds.reviewed_corridor(raw + b" ", assumed_sha256=evidence.assumed_sim["sha256"])
    modified = replace(bounds, fw_tracking_m=bounds.fw_tracking_m + 1)
    assert not modified.reviewed_corridor(raw, assumed_sha256=evidence.assumed_sim["sha256"])
    assert not bounds.valid_for(SCOPE, profile)


def test_confirmed_hold_departure_requires_current_leg_and_proved_entry(terrain, profile, approvals):
    evidence, lowering, package = _live(terrain, profile, FAR, fw_rtl=_v2(SCOPE, return_alt=45),
                                        hold_centre=(800, 0), approvals=approvals)
    exit_box = evidence._corridor_hold_exits[1, HOLD]
    point = (800.0, exit_box[3] - 25.0, lowering.cruise_msl)
    # The tracked orbit has its own preflight-proved entry on this active leg.
    assert not evidence.builder.corridor_rtl_clear(1, point)
    assert evidence.check_entry(1, point, _local(point)) == point
    assert ("return", 1) not in evidence._entry[1]
    assert evidence.check_entry(1, point, _local(point), confirmed_hold_ref=HOLD,
                                observed_home=_home_obs(terrain)) == point
    assert ("return", 1) in evidence._entry[1]
    decision = replace(_decision(package), action="return")
    frame = replace(_frame(package, point, 1), native_hold_active=True)
    proof = evidence.build_proof(decision, frame)
    assert proof.geofence_clear and proof.required_time_s > 800 / 12
    for invalid in (replace(frame, native_hold_active=False), replace(frame, active_motion_index=2),
                    replace(frame, position_ne_amsl=(800.0, exit_box[3] + 1, point[2]))):
        assert evidence.build_proof(decision, invalid).required_time_s is None


def test_mc_terminal_recovery_is_independently_bounded(terrain, profile):
    evidence, lowering, package = _live(terrain, profile, PLACES, native_mc=True)
    index = len(lowering.legs) + 2
    point = (0.0, 0.0, lowering.cruise_msl)
    local = {**_local(point), "vx": 0.0}
    assert evidence.check_entry(index, point, local) == point
    frame = replace(_frame(package, point, index, mode="mc"), hold_sites=evidence.hold_sites(index, "mc"))
    land = replace(_decision(package), action="land-now")
    proof = evidence.build_proof(land, frame)
    assert proof.geofence_clear and proof.landing_site_clear
    assert X1Envelope(profile).veto(land, frame, proof, package) is None
    hold = replace(_decision(package), action="loiter", params={"hold_ref": "hold:local"})
    assert evidence.build_proof(hold, frame).geofence_clear
    rtl = replace(_decision(package), action="return")
    assert evidence.build_proof(rtl, frame).required_time_s is None
    for invalid in (replace(frame, vtol_state="fw"), replace(frame, vtol_state="back_transition"),
                    replace(frame, active_motion_index=index - 1)):
        assert evidence.build_proof(land, invalid).required_time_s is None
        assert evidence.build_proof(hold, invalid).required_time_s is None
    outside = (evidence.pad.radius_m, 0.0, point[2])
    evidence.check_entry(index, outside, {**_local(outside), "vx": 0.0})
    assert evidence.build_proof(land, replace(frame, position_ne_amsl=outside)).required_time_s is None


def test_hold_deadline_does_not_spend_return_time():
    from types import SimpleNamespace

    from multidetect.sim_teacher.x1_scenario import CollectorScenario

    scenario = CollectorScenario.__new__(CollectorScenario)
    scenario.native = None
    scenario._seen_admission = None
    scenario.record = lambda _: None
    wire = json.dumps({"params": {"hold_ref": HOLD}})
    scenario.decision = SimpleNamespace(last_admission=(
        SimpleNamespace(status="sent_unconfirmed", action="loiter"),
        SimpleNamespace(required_time_s=240.0), 1000, wire,
    ))
    scenario.motion_evidence = SimpleNamespace(corridor=object(), hold_residence_s=lambda _: 60.0)
    scenario._after_decision()
    assert scenario.hold["deadline_ms"] == 61_000
    events = []
    assert scenario._hold_events(51_000, events) is True and events == [("hold_revalidation", "executor")]
    assert scenario._hold_events(61_000, events) is False


def test_existing_corridor_host_template_preserves_terminal_mc_slot(terrain):
    from pathlib import Path

    from multidetect.sim_teacher.x1_live_clearance import parse_native

    template = json.loads((Path(__file__).resolve().parents[1]
                           / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json").read_text())
    raw = {section: {key: entry["value"] for key, entry in entries.items()}
           for section, entries in template["native"].items()}
    returns, landings, holds = parse_native(raw, terrain, corridor_fw_legs=4)
    assert set(returns) == set(landings) == {6}
    assert holds["hold:mc-here"].steps == {6}
    assert holds["hold:yard-orbit"].steps == {1}


def test_self_asserted_review_does_not_authorize_fw_or_mc_return(terrain, profile):
    evidence, lowering, package = _live(
        terrain, profile, FAR, fw_rtl=_v2(SCOPE, return_alt=45),
        hold_centre=(800, 0), native_mc=True)
    assert evidence.assumed_sim["fw_rtl_review"]
    assert evidence.fw_rtl is None
    alt = lowering.cruise_msl
    for index, mode, point, vx in ((1, "fw", (800.0, 0.0, alt), 15.0),
                                   (len(lowering.legs) + 2, "mc", (0.0, 0.0, alt), 0.0)):
        assert evidence.check_entry(index, point, {**_local(point), "vx": vx},
                                    confirmed_hold_ref=HOLD, observed_home=_home_obs(terrain)) == point
        assert ("return", index) not in evidence._entry[1]
        frame = replace(_frame(package, point, index, mode=mode), native_hold_active=mode == "fw")
        assert evidence.build_proof(replace(_decision(package), action="return"), frame).required_time_s is None

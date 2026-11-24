"""CPU-only unit tests for opt-in X1 FW short-leg merging and generation."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

from multidetect.sim_teacher.target_tracking import (
    WorldTarget, level_target_rotation, world_target_direction,
)
from scripts.realworld import collection_runtime
from scripts.realworld import long_range_flight as flight
from scripts.realworld import wide_collect_batch as batch
from scripts.realworld import x1_world_route as route
from scripts.realworld.x1_gimbal_eval import TRACK_FW_MODE, TRACK_SAMPLE, fw_legs


def _point_to_segment_dist(p, a, b):
    ab = (b[0] - a[0], b[1] - a[1])
    ap = (p[0] - a[0], p[1] - a[1])
    denom = ab[0] * ab[0] + ab[1] * ab[1]
    if denom <= 1e-12:
        return math.hypot(ap[0], ap[1])
    t = max(0.0, min(1.0, (ap[0] * ab[0] + ap[1] * ab[1]) / denom))
    return math.hypot(p[0] - (a[0] + t * ab[0]), p[1] - (a[1] + t * ab[1]))


def test_recommended_fw_min_leg_m():
    assert flight.recommended_fw_min_leg_m(15.0) == pytest.approx(50.0)
    assert flight.recommended_fw_min_leg_m(20.0) == pytest.approx(60.0)
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            flight.recommended_fw_min_leg_m(bad)


def test_merge_short_fw_legs_merges_12_turn_corner_and_preserves_endpoints():
    enu = route.margin_corner_route((-1, 1), 1, segments=12, offset=0)
    start = (0.0, 0.0, -85.0)
    targets = [(float(y), float(x), -85.0) for x, y in enu[1:-1]]
    caps = [15.0] * len(targets)

    orig_pts = [start, *targets]
    orig_legs = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(orig_pts, orig_pts[1:])]
    assert len(orig_legs) == 22
    assert sum(1 for length in orig_legs if length < 50.0) == 11

    merged, merged_caps, kept_idx = flight.merge_short_fw_legs(start, targets, 50.0, capture_radii=caps)
    assert len(merged) == 16
    assert len(merged_caps) == 16
    assert len(kept_idx) == 16
    assert merged[-1] == targets[-1]

    new_pts = [start, *merged]
    new_legs = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(new_pts, new_pts[1:])]
    assert min(new_legs) >= 50.0
    assert max(math.hypot(p[0], p[1]) for p in merged) == pytest.approx(
        max(math.hypot(p[0], p[1]) for p in targets), abs=1e-6
    )
    max_dev = max(
        min(_point_to_segment_dist(p, a, b) for a, b in zip(new_pts, new_pts[1:]))
        for p in orig_pts
    )
    assert max_dev < 10.0


def test_merge_short_fw_legs_preserves_terminal_and_unique_apex():
    start = (0.0, 0.0, -85.0)
    # Short leg into terminal waypoint should pop the preceding intermediate point, not the terminal.
    targets = [(100.0, 0.0, -85.0), (200.0, 0.0, -85.0), (220.0, 0.0, -85.0)]
    merged, _, _ = flight.merge_short_fw_legs(start, targets, 50.0)
    assert merged == [(100.0, 0.0, -85.0), (220.0, 0.0, -85.0)]

    # Unique farthest apex within min_leg_m of previous kept point should replace previous kept point.
    apex_targets = [
        (3980.0, 0.0, -85.0),
        (4010.0, 0.0, -85.0),  # 30 m past previous, unique farthest apex
        (3900.0, 100.0, -85.0),
        (300.0, 100.0, -85.0),
    ]
    merged_apex, _, _ = flight.merge_short_fw_legs(start, apex_targets, 50.0)
    assert (4010.0, 0.0, -85.0) in merged_apex
    assert merged_apex[-1] == apex_targets[-1]


def test_merge_short_fw_legs_noop_and_validation():
    start = (0.0, 0.0, -85.0)
    targets = [(200.0, 0.0, -85.0), (400.0, 0.0, -85.0)]
    merged, caps, idx = flight.merge_short_fw_legs(start, targets, 0.0, capture_radii=[15.0, 15.0])
    assert merged == targets and caps == [15.0, 15.0] and idx == [0, 1]
    with pytest.raises(ValueError):
        flight.merge_short_fw_legs(start, targets, -1.0)
    with pytest.raises(ValueError):
        flight.merge_short_fw_legs(start, targets, 50.0, capture_radii=[15.0])


def test_runtime_options_and_batch_fw_min_leg_m_opt_in(tmp_path: Path):
    base_job = {
        "site": "rw_farm_iowa",
        "altitude_m": 85,
        "time_of_day": "noon",
        "weather": "clear",
        "seed": 42,
    }
    delta_spec = {"airframe": batch.DELTA_AIRFRAME, "jobs": [base_job]}
    opt_in_spec = {"airframe": batch.DELTA_AIRFRAME, "jobs": [{**base_job, "fw_min_leg_m": 50}]}

    j_default, = batch.expand(delta_spec)["jobs"]
    j_opt_in, = batch.expand(opt_in_spec)["jobs"]
    assert "fw_min_leg_m" not in j_default["inputs"]
    assert "fw_min_leg_m" not in j_default["matrix"]
    assert j_opt_in["inputs"]["fw_min_leg_m"] == 50.0
    assert j_opt_in["matrix"]["fw_min_leg_m"] == 50.0
    assert j_default["job_id"] != j_opt_in["job_id"]

    # Rejected for non-Delta airframe and invalid values.
    with pytest.raises(ValueError, match="delta_tailsitter_gimbal_cam"):
        collection_runtime.runtime_options({"fw_min_leg_m": 50}, batch.AIRFRAME, "jobs[0]")
    for bad in (0, -5, True, "50", float("nan")):
        with pytest.raises(ValueError, match="fw_min_leg_m"):
            collection_runtime.runtime_options({"fw_min_leg_m": bad}, batch.DELTA_AIRFRAME, "jobs[0]")


def _make_flat_x1_bundle(tmp_path: Path, monkeypatch, *, min_leg_m: float | None = None):
    asset = tmp_path / "asset"
    (asset / "models/rw_fixture_short").mkdir(parents=True)
    world_id = "rw_fixture_short_w6km__clear_noon"
    world = asset / f"{world_id}.sdf"
    world.write_text(
        '<sdf version="1.9"><world name="default">'
        "<spherical_coordinates>"
        "<surface_model>EARTH_WGS84</surface_model>"
        "<world_frame_orientation>ENU</world_frame_orientation>"
        "<latitude_deg>42.015</latitude_deg>"
        "<longitude_deg>-93.71</longitude_deg>"
        "<elevation>100.0</elevation>"
        "<heading_deg>0</heading_deg>"
        "</spherical_coordinates>"
        "</world></sdf>",
        encoding="utf-8",
    )
    raw = asset / "raw/rw_fixture_short_w6km/raw"
    raw.mkdir(parents=True)
    dem = raw / "dem.npy"
    np.save(dem, np.full((route.COUNT + 1, route.COUNT + 1), 100.0, dtype=np.float32))
    layers = {
        "ground_low": np.zeros((route.COUNT, route.COUNT)),
        "ground_high": np.zeros((route.COUNT, route.COUNT)),
        "buildings_and_boxes": np.full((route.COUNT, route.COUNT), -np.inf),
        "canopy": np.full((route.COUNT, route.COUNT), -np.inf),
        "water_surface": np.full((route.COUNT, route.COUNT), -np.inf),
        "water": np.zeros((route.COUNT, route.COUNT), dtype=bool),
    }
    scene = {"site": "rw_fixture_short_w6km", "size_m": 6000, "lat": 42.015, "lon": -93.71, "elevation": 100.0}
    inputs = {"raw/rw_fixture_short_w6km/raw/dem.npy": route.file_sha256(dem)}
    counts = {"boxes": 0, "building_triangles": 0, "canopy_triangles": 0, "water_triangles": 0, "water_features": 0}
    monkeypatch.setattr(route, "load_layers", lambda *_args: (scene, dem, layers, counts, inputs))
    monkeypatch.setattr(route, "RUNNER_PYTHON", Path(sys.executable))
    receipt_path = route.preflight(asset, world, min_leg_m=min_leg_m)
    receipt = route.read_json(receipt_path)
    scenario = receipt_path.parent / receipt["scenario"]
    return asset, world, receipt_path, scenario


def test_load_mission_opt_in_merge_keeps_pins_and_gimbal_fw_scoring(tmp_path: Path, monkeypatch, capsys):
    asset, world, receipt_path, scenario = _make_flat_x1_bundle(tmp_path, monkeypatch, min_leg_m=None)
    dem_path = scenario.with_name(scenario.name.replace(".scenario.json", ".dem.json"))
    sidecar = flight.sidecar_path(scenario)
    original_sidecar_sha = route.file_sha256(sidecar)

    # Default (OFF): 22 legs, untouched.
    m_default = flight.load_mission(scenario, dem_path=dem_path)
    assert len(m_default["targets"]) == 22
    assert "fw_short_leg_merge" not in m_default

    # Opt-in (50 m): 16 legs, all >= 50 m, sidecar file hash unchanged.
    m_merged = flight.load_mission(scenario, dem_path=dem_path, fw_min_leg_m=50.0)
    assert route.file_sha256(sidecar) == original_sidecar_sha
    assert len(m_merged["targets"]) == 16
    assert m_merged["fw_short_leg_merge"]["original_legs"] == 22
    assert m_merged["fw_short_leg_merge"]["merged_legs"] == 16
    assert m_merged["fw_short_leg_merge"]["dropped_waypoints"] == 6
    assert m_merged["targets"][-1] == m_default["targets"][-1]
    assert m_merged["planned_max_distance_m"] >= 4000.0
    assert 40.0 <= m_merged["planned_agl_envelope_m"][0] <= m_merged["planned_agl_envelope_m"][1] <= 120.0

    # Plan + gimbal_eval_fw_targets + LongRangePolicy + fw_legs scoring all track the merged leg count.
    plan = replace(
        flight.collection_plan(scenario, tmp_path / "out", 5, m_merged),
        gimbal_mode="lock",
        gimbal_bridge_stabilize=True,
    )
    assert plan.steps_per_episode == 16 + 3
    diag = []
    eval_plan = flight.gimbal_eval_plan(plan, m_merged, fw_legs=True, fw_diagnostics=diag)
    assert eval_plan.gimbal_eval_fw_legs == 16
    assert eval_plan.gimbal_eval_track_step == 18
    assert len(eval_plan.gimbal_eval_fw_targets) == 16
    assert len(diag) == 16

    policy_obj = flight.LongRangePolicy(
        m_merged["targets"], m_merged["geometry"].loiter_radius_m, vtol=True,
        fw_speed_mps=15.0, gimbal_eval=True,
        fw_eval_switch_angles_deg=tuple(row["switch_los_deg"] for row in diag),
        fly_through_radius_m=m_merged["route_preflight"]["fly_through_radius_m"],
    )
    actions = [policy_obj.next_action() for _ in range(20)]
    assert [a.kind for a in actions] == [*["reposition"] * 16, "transition", "mc_reposition", "mc_hold", "stop"]

    # Verify x1_gimbal_eval.fw_legs scores the 16-leg merged route cleanly.
    home = m_merged["geometry"].home
    earth = 6_378_137.0
    east_radius = earth * math.cos(math.radians(home.latitude_deg))
    start_ned = (0.0, 0.0, -m_merged["geometry"].takeoff_altitude_m)
    leg_segments = list(zip([start_ned, *m_merged["targets"][:-1]], m_merged["targets"], strict=True))
    refs = [t.reference() for t in eval_plan.gimbal_eval_fw_targets]
    meta = {"horizon_stabilization": {"eval_fw_legs": 16, "eval_fw_target_refs": refs}}
    modes = [{"message_type": TRACK_FW_MODE, "step_index": i, "mode": "track", "target_ref": ref}
             for i, ref in enumerate(refs)]
    rows = []
    for idx in range(160):
        step = idx % 16
        a, b = leg_segments[step]
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, (a[2] + b[2]) / 2.0)
        yaw_deg = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        origin = WorldTarget(
            home.latitude_deg + math.degrees(mid[0] / earth),
            home.longitude_deg + math.degrees(mid[1] / east_radius),
            100.0 - mid[2],
        )
        target = eval_plan.gimbal_eval_fw_targets[step]
        rows.append({
            "message_type": TRACK_SAMPLE, "step_index": step,
            "fw_phase": "cruise" if idx < 128 else "turn",
            "sim_stamp_ns": idx * 10**8, "track_mode": "track",
            "sim_skew_ms": 0.0, "attitude_skew_ms": 0.0,
            "attitude": {"roll_deg": 0.0, "pitch_deg": -83.0, "yaw_deg": yaw_deg},
            "pose": {"lat_deg": origin.lat_deg, "lon_deg": origin.lon_deg, "alt_amsl_m": origin.alt_m,
                     "local_ned_m": (0.0, 0.0, 0.0), "global_position_skew_ms": 0.0},
            "gimbal_state": {"source": "gazebo_joint_state", "fresh": True},
            "camera_pose": {"age_ms": 0.0, "center_ned": (0.0, 0.0, 0.0),
                            "optical_to_world": level_target_rotation(world_target_direction(origin, target))},
            "camera_info": {"fx": 100, "fy": 100, "cx": 50, "cy": 40, "width": 100, "height": 80},
        })
    report = fw_legs(meta, [*modes, *rows])
    assert report["status"] == "scored"
    assert report["passed"] is True
    assert len(report["by_leg"]) == 16

    # Verify CLI --plan-only with --fw-min-leg-m 50 --gimbal-eval --gimbal-eval-fw.
    monkeypatch.setenv("MULTIDETECT_RTF_WORLD_ROOT", str(world.parent))
    monkeypatch.setenv("MULTIDETECT_RTF_MAP_ROOT", str(scenario.parent))
    monkeypatch.setenv("MULTIDETECT_RTF_TERRAIN_ROOT", str(scenario.parent))
    monkeypatch.setenv("MULTIDETECT_SIM_FUEL_RESOLVED_ROOT", str(asset / "models"))
    monkeypatch.delenv("MULTIDETECT_RTF_MODEL_ROOT", raising=False)
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    assert flight.main([
        "5", str(scenario), "--airframe", flight.DELTA,
        "--gimbal-bridge-stabilize", "--gimbal-eval", "--gimbal-eval-fw",
        "--fw-min-leg-m", "50", "--plan-only",
    ]) == 0
    out_plan = json.loads(capsys.readouterr().out)
    assert len(out_plan["fw_waypoints_home_ned_m"]) == 16
    assert out_plan["fw_short_leg_merge"]["merged_legs"] == 16
    assert out_plan["gimbal_eval"]["fw_legs"] == 16


def test_x1_world_route_preflight_min_leg_m_generates_18_leg_route(tmp_path: Path, monkeypatch):
    _, _, receipt_path, scenario = _make_flat_x1_bundle(tmp_path, monkeypatch, min_leg_m=45.0)
    dem_path = scenario.with_name(scenario.name.replace(".scenario.json", ".dem.json"))
    mission = flight.load_mission(scenario, dem_path=dem_path)
    assert len(mission["targets"]) == 18
    start = (0.0, 0.0, -mission["geometry"].takeoff_altitude_m)
    pts = [start, *mission["targets"]]
    legs = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    assert min(legs) >= 45.0
    assert "minleg45m" in mission["route_preflight"]["metrics"]["candidate"]
    assert route.policy.has_wide_course_margin([p[:2] for p in pts])
    assert route.policy.has_runout_margin(mission["route_preflight"]["metrics"]["fw_runout"])

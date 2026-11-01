"""Offline contract checks for the r21id-r4 generated identity worlds."""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from multidetect.sim_teacher.spawn_clearance import (
    spawn_clearance_report,
    teacher_intent_clearance_altitude,
    teacher_intent_clearance_route_points,
)

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_r15_worlds  # noqa: E402


def test_r21id_r4_dry_run_profile(capsys):
    assert generate_r15_worlds.main(["--profile", "r21id-r4", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 24
    assert payload["world_prefix"] == "gen_r21id_r4_w"
    assert payload["splits"] == {"train": 18, "val": 6, "test": 0}


def test_generated_r21id_r4_offline_contract():
    base = ROOT / "configs/scenarios/generated-r21id-r4"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text(encoding="utf-8"))
    assets = {row["slug"]: row for row in catalog["assets"]}
    assert index["count"] == 24
    assert index["profile"] == "r21id-r4"
    assert not any(row["split"] == "test" for row in index["worlds"])
    assert {slug: sum(row["target_combo"]["slug"] == slug for row in index["worlds"])
            for slug in ("pickup", "suv", "postbox", "walking_person", "ambulance")} == {
                "pickup": 6, "suv": 6, "postbox": 4, "walking_person": 4, "ambulance": 4,
            }

    small_counts = {"pickup": 0, "suv": 0, "postbox": 0}
    for row in index["worlds"]:
        wid = row["world_id"]
        truth = json.loads((base / f"{wid}.truth.json").read_text(encoding="utf-8"))
        runtime = json.loads((base / f"{wid}.runtime_input.json").read_text(encoding="utf-8"))
        intent = json.loads((base / f"{wid}.teacher_intent.json").read_text(encoding="utf-8"))["observation"]
        target = next(obj for obj in truth["targets"] if obj["true_target"])
        assert all(obj["asset_slug"] in assets for obj in truth["targets"])
        spawn = runtime["vehicle"]["spawn_pose"]
        assert spawn == {
            "x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.2,
            "roll_rad": 0.0, "pitch_rad": 0.0, "yaw_rad": 0.0,
        }
        assert intent["observation_waypoint"] == {"x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.0}
        assert runtime["vehicle"]["airframe_profile_id"] == "px4_multirotor_gazebo_v1"
        assert intent["observation_frames"] == 8
        assert [view["view"] for view in intent["views"]] == ["front", "oblique", "edge_truncated", "recovery"]
        geom = row["view_geometry"]
        slant = math.hypot(geom["ground_distance_m"], geom["camera_altitude_m"])
        assert math.isclose(geom["slant_distance_m"], slant, abs_tol=0.002)
        assert math.isclose(target["pose"]["x_east_m"], geom["ground_distance_m"], abs_tol=0.002)
        assert target["pose"]["y_north_m"] == 0.0
        assert intent["camera_altitude_m"] == geom["camera_altitude_m"]
        view_ground_distance = math.hypot(
            target["pose"]["x_east_m"] - intent["views"][0]["observation_waypoint"]["x_east_m"],
            target["pose"]["y_north_m"] - intent["views"][0]["observation_waypoint"]["y_north_m"],
        )
        computed = generate_r15_worlds.projected_short_edge_px(
            assets[target["asset_slug"]], view_ground_distance, geom["camera_altitude_m"]
        )
        assert math.isclose(computed, geom["target_short_side_px_estimate"], abs_tol=0.02)
        band = geom["target_short_side_px"]
        assert (8 <= computed < 12 if band == "8-12" else
                12 <= computed < 16 if band == "12-16" else
                24 <= computed < 32 if band == "24-32" else computed >= 32)
        if target["asset_slug"] in {"pickup", "suv"} and band in {"8-12", "12-16"}:
            assert intent["max_detection_range_m"] >= math.hypot(
                geom["ground_distance_m"], geom["camera_altitude_m"]
            ) + 4.9
        if target["asset_slug"] in small_counts and band in {"8-12", "12-16"}:
            small_counts[target["asset_slug"]] += 1
        if target["asset_slug"] in {"pickup", "suv"}:
            assert any(obj.get("confuser") and obj["asset_slug"] == ("suv" if target["asset_slug"] == "pickup" else "pickup")
                       for obj in truth["targets"])
            assert len(row["task_text_variants"]) == 2
        if target["asset_slug"] == "postbox":
            assert any(obj["asset_slug"] == "hatchback" for obj in truth["targets"])
        if target["asset_slug"] == "walking_person":
            assert any(obj["asset_slug"] == "postbox" for obj in truth["targets"])
        if target["asset_slug"] == "ambulance":
            assert "SUV" in row["task_text_en"]
            assert row["find_task_text_en"] == row["task_text_en"]
            assert any(obj.get("confuser") and obj["asset_slug"] == "suv" for obj in truth["targets"])
            assert row["split"] in {"train", "val"}
        sdf = (ROOT / "simulation/gazebo/worlds" / row["world_file"]).read_text(encoding="utf-8")
        assert f'<model name="{target["target_id"]}">' in sdf
        assert "https://" not in sdf and "http://" not in sdf
    assert small_counts == {"pickup": 4, "suv": 4, "postbox": 2}


def test_generated_r21id_r4_spawn_clearance_all_worlds():
    base = ROOT / "configs/scenarios/generated-r21id-r4"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    for row in index["worlds"]:
        wid = row["world_id"]
        runtime = json.loads((base / f"{wid}.runtime_input.json").read_text(encoding="utf-8"))
        intent = json.loads((base / f"{wid}.teacher_intent.json").read_text(encoding="utf-8"))["observation"]
        spawn = runtime["vehicle"]["spawn_pose"]
        truth = json.loads((base / f"{wid}.truth.json").read_text(encoding="utf-8"))
        target = next(row for row in truth["targets"] if row.get("true_target"))
        world_path = ROOT / "simulation/gazebo/worlds" / row["world_file"]
        declared_shift = float(row["view_geometry"].get("scene_shift_m", 0.0))
        target_radius = math.hypot(target["pose"]["x_east_m"], target["pose"]["y_north_m"])
        radial_east = target["pose"]["x_east_m"] / target_radius
        radial_north = target["pose"]["y_north_m"] / target_radius
        original_intent = json.loads(json.dumps(intent))
        for view in original_intent.get("views", []):
            point = view.get("observation_waypoint", {})
            point["x_east_m"] -= declared_shift * radial_east
            point["y_north_m"] -= declared_shift * radial_north
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / world_path.name
            original.write_bytes(
                subprocess.run(
                    ["git", "show", f"HEAD:{world_path.relative_to(ROOT).as_posix()}"],
                    check=True,
                    capture_output=True,
                ).stdout
            )
            computed_shift = generate_r15_worlds.r21id_r4_scene_shift(
                original,
                target_east_m=target["pose"]["x_east_m"] - declared_shift * radial_east,
                target_north_m=target["pose"]["y_north_m"] - declared_shift * radial_north,
                observation=original_intent,
            )
        assert computed_shift == declared_shift
        assert spawn["x_east_m"] == spawn["y_north_m"] == 0.0
        report = spawn_clearance_report(
            ROOT / "simulation/gazebo/worlds" / row["world_file"],
            spawn,
            climb_altitude_m=teacher_intent_clearance_altitude(intent),
            route_points=teacher_intent_clearance_route_points(intent),
        )
        assert report["clear"], (wid, report)


def test_generated_r21id_r4_good_worlds_are_byte_identical_to_head():
    base = ROOT / "configs/scenarios/generated-r21id-r4"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    suffixes = ("runtime_input.json", "truth.json", "expectations.json", "scenario.json", "teacher_intent.json")
    for row in index["worlds"]:
        if float(row["view_geometry"].get("scene_shift_m", 0.0)) != 0.0:
            continue
        wid = row["world_id"]
        paths = [base / f"{wid}.{suffix}" for suffix in suffixes]
        paths.append(ROOT / "simulation/gazebo/worlds" / row["world_file"])
        for path in paths:
            rel = path.relative_to(ROOT).as_posix()
            expected = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                check=True,
                capture_output=True,
            ).stdout
            assert path.read_bytes() == expected, rel

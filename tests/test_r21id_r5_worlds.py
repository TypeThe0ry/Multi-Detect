"""Offline contract checks for the r21id-r5 generated identity worlds."""

import json
import math
from pathlib import Path

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO
from multidetect.sim_teacher.policies import ExpertSearchPolicy, target_in_fov
from multidetect.sim_teacher.spawn_clearance import (
    spawn_clearance_report,
    teacher_intent_clearance_altitude,
    teacher_intent_clearance_route_points,
)

ROOT = Path(__file__).parents[1]


def test_r21id_r5_profile_shape():
    from scripts import generate_r15_worlds

    assert generate_r15_worlds.main(["--profile", "r21id-r5", "--dry-run"]) == 0


def test_generated_r21id_r5_contract_and_teacher_visibility(capsys):
    base = ROOT / "configs/scenarios/generated-r21id-r5"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text(encoding="utf-8"))
    assets = {row["slug"]: row for row in catalog["assets"]}
    assert index["profile"] == "r21id-r5"
    assert index["count"] == 12
    assert {row["split"] for row in index["worlds"]} == {"train", "val"}
    assert {row["target_combo"]["slug"] for row in index["worlds"]} == {
        "suv", "walking_person", "stop_sign", "hatchback"
    }
    directions = {
        (row["target_combo"]["slug"], row["paired_confusers"][0])
        for row in index["worlds"]
    }
    assert directions == {
        ("suv", "postbox"),
        ("walking_person", "jersey_barrier"),
        ("stop_sign", "jersey_barrier"),
        ("hatchback", "postbox"),
    }
    bands = {"8-12", "12-16", "16-24"}
    for row in index["worlds"]:
        wid = row["world_id"]
        truth = json.loads((base / f"{wid}.truth.json").read_text(encoding="utf-8"))
        runtime = json.loads((base / f"{wid}.runtime_input.json").read_text(encoding="utf-8"))
        intent = json.loads((base / f"{wid}.teacher_intent.json").read_text(encoding="utf-8"))["observation"]
        target = next(item for item in truth["targets"] if item.get("true_target"))
        confuser = next(item for item in truth["targets"] if item.get("confuser"))
        assert target["asset_slug"] in assets and confuser["asset_slug"] in assets
        assert confuser["asset_slug"] == row["paired_confusers"][0]
        assert row["view_geometry"]["target_short_side_px"] in bands
        assert len(row["task_text_variants"]) == 2
        assert {item["requested_category"] for item in row["task_text_variants"]} == {
            target["asset_slug"], confuser["asset_slug"]
        }
        assert [view["view"] for view in intent["views"]] == [
            "front", "oblique", "edge_truncated", "recovery"
        ]
        report = spawn_clearance_report(
            ROOT / "simulation/gazebo/worlds" / row["world_file"],
            runtime["vehicle"]["spawn_pose"],
            climb_altitude_m=teacher_intent_clearance_altitude(intent),
            route_points=teacher_intent_clearance_route_points(intent),
        )
        assert report["clear"], (wid, report)
        pose = target["pose"]
        target_ned = (float(pose["y_north_m"]), float(pose["x_east_m"]), -float(pose["z_up_m"]))
        altitude = float(intent["camera_altitude_m"])
        waypoints = tuple(
            (
                float(view["observation_waypoint"].get("y_north_m", 0.0)),
                float(view["observation_waypoint"].get("x_east_m", 0.0)),
                -float(view["observation_waypoint"].get("z_up_m", 0.0) or altitude),
            )
            for view in intent["views"]
        )
        policy = ExpertSearchPolicy(
            target_ned=target_ned,
            intrinsics=DEFAULT_CAMERA_INFO,
            image_size=(1280, 960),
            altitude_m=altitude,
            camera_pitch_rad=-float(intent["camera_pitch_rad"]),
            max_detection_range_m=float(intent.get("max_detection_range_m", 80.0)),
            observation_frames=int(intent["observation_frames"]),
            observation_waypoints=waypoints,
        )
        assert policy.observation_waypoints == waypoints
        for north, east, down in policy.observation_waypoints:
            yaw = math.degrees(math.atan2(target_ned[1] - east, target_ned[0] - north))
            visible, _, _ = target_in_fov(
                target_ned,
                (north, east, down),
                yaw,
                DEFAULT_CAMERA_INFO,
                (1280, 960),
                max_range_m=float(intent.get("max_detection_range_m", 80.0)),
                camera_pitch_rad=-float(intent["camera_pitch_rad"]),
            )
            assert visible, wid

"""Offline contract checks for the r21id-r7 diverse hard-reject worlds."""

import json
import math
from pathlib import Path

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO
from multidetect.sim_teacher.policies import target_in_fov
from multidetect.sim_teacher.spawn_clearance import (
    spawn_clearance_report,
    teacher_intent_clearance_altitude,
    teacher_intent_clearance_route_points,
)

ROOT = Path(__file__).parents[1]


def test_r21id_r7_profile_shape():
    from scripts import generate_r15_worlds

    assert generate_r15_worlds.main(["--profile", "r21id-r7", "--dry-run"]) == 0


def test_generated_r21id_r7_contract_and_teacher_visibility():
    base = ROOT / "configs/scenarios/generated-r21id-r7"
    index = json.loads((base / "index.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text(encoding="utf-8"))
    assets = {row["slug"]: row for row in catalog["assets"]}

    assert index["profile"] == "r21id-r7"
    assert index["count"] == 15
    assert {row["split"] for row in index["worlds"]} == {"train", "val"}
    assert {row["r7_family"] for row in index["worlds"]} == {
        "suv->postbox", "car->postbox", "person->jersey_barrier"
    }
    assert all(sum(row["r7_family"] == family for row in index["worlds"]) == 5 for family in {
        "suv->postbox", "car->postbox", "person->jersey_barrier"
    })
    assert all(sum(row["split"] == "val" and row["r7_family"] == family for row in index["worlds"]) == 1 for family in {
        "suv->postbox", "car->postbox", "person->jersey_barrier"
    })
    assert {row["target_combo"]["slug"] for row in index["worlds"]} >= {
        "suv", "hatchback", "hatchback_blue", "hatchback_red", "walking_person", "male_visitor"
    }
    assert {row["paired_confusers"][0] for row in index["worlds"]} >= {
        "postbox", "mailbox", "jersey_barrier"
    }

    bands = {"8-12", "12-16", "16-24", "24-32"}
    for row in index["worlds"]:
        wid = row["world_id"]
        truth = json.loads((base / f"{wid}.truth.json").read_text(encoding="utf-8"))
        runtime = json.loads((base / f"{wid}.runtime_input.json").read_text(encoding="utf-8"))
        intent = json.loads((base / f"{wid}.teacher_intent.json").read_text(encoding="utf-8"))["observation"]
        target = next(item for item in truth["targets"] if item.get("true_target"))
        confuser = next(item for item in truth["targets"] if item.get("confuser"))
        assert target["asset_slug"] in assets
        assert confuser["asset_slug"] in assets
        assert confuser["asset_slug"] == row["paired_confusers"][0]
        assert row["view_geometry"]["target_short_side_px"] in bands
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
        for view in intent["views"]:
            point = view["observation_waypoint"]
            observation = (
                float(point.get("y_north_m", 0.0)),
                float(point.get("x_east_m", 0.0)),
                -float(point.get("z_up_m", 0.0) or altitude),
            )
            yaw = math.degrees(math.atan2(target_ned[1] - observation[1], target_ned[0] - observation[0]))
            visible, _, _ = target_in_fov(
                target_ned,
                observation,
                yaw,
                DEFAULT_CAMERA_INFO,
                (1280, 960),
                max_range_m=float(intent.get("max_detection_range_m", 80.0)),
                camera_pitch_rad=-float(intent["camera_pitch_rad"]),
            )
            assert visible, (wid, view["view"])


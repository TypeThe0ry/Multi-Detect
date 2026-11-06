"""Offline contract checks for the balanced r9r rejection worlds."""

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO
from multidetect.simulator.camera_projection import project_target_bbox

ROOT = Path(__file__).parents[1]
SCENARIOS = ROOT / "configs/scenarios/generated-r21id-r9r"
VIEWS = ("front", "oblique", "edge_truncated", "recovery")


def test_r9r_profile_shape_and_dry_run(capsys):
    from scripts import generate_r15_worlds

    assert generate_r15_worlds.main(["--profile", "r21id-r9r", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "count": 48,
        "seed": 20261127,
        "world_prefix": "gen_r21id_r9r_w",
        "splits": {"train": 36, "val": 12, "test": 0},
    }


def test_generated_r9r_pair_balance_mesh_source_size_and_projection_floor():
    index = json.loads((SCENARIOS / "index.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text(encoding="utf-8"))
    sizes = {row["slug"]: tuple(float(v) for v in row["measured_size_m"]) for row in catalog["assets"]}

    assert index["profile"] == "r21id-r9r"
    assert len(index["worlds"]) == 48
    assert {row["r9r_direction"] for row in index["worlds"]} == {"car->mailbox", "suv->postbox"}
    assert all(sum(row["r9r_direction"] == pair and row["split"] == split for row in index["worlds"]) == 18 if split == "train" else 6 for pair in {"car->mailbox", "suv->postbox"} for split in {"train", "val"})

    minimums = []
    for row in index["worlds"]:
        wid = row["world_id"]
        truth = json.loads((SCENARIOS / f"{wid}.truth.json").read_text(encoding="utf-8"))
        intent = json.loads((SCENARIOS / f"{wid}.teacher_intent.json").read_text(encoding="utf-8"))
        confuser = next(item for item in truth["targets"] if item.get("confuser"))
        observation = intent["confuser_observation"]
        geometry = row["view_geometry"]["confuser_observation"]
        assert tuple(float(v) for v in confuser["size_m"]) == sizes[confuser["asset_slug"]]
        assert observation["front_face_axis"] == "+local_y"
        assert tuple(view["view"] for view in observation["views"]) == VIEWS
        assert tuple(view["view"] for view in geometry["per_view"]) == VIEWS
        assert geometry["mesh_scale"] == [1.0, 1.0, 1.0]
        assert geometry["source_mesh_size_m"] == list(sizes[confuser["asset_slug"]])
        sdf = ET.parse(ROOT / "simulation/gazebo/worlds" / row["world_file"])
        model = sdf.find(f".//model[@name='{confuser['target_id']}']")
        assert model is not None
        bounds = model.find("./link[@name='bounds']/collision/geometry/box/size")
        assert bounds is not None
        assert tuple(float(v) for v in bounds.text.split()) == sizes[confuser["asset_slug"]]
        scales = [tuple(float(v) for v in node.text.split()) for node in model.findall(".//mesh/scale")]
        assert scales and all(scale == (1.0, 1.0, 1.0) for scale in scales)

        pose = confuser["pose"]
        for view, predicted in zip(observation["views"], geometry["per_view"], strict=True):
            point = view["observation_waypoint"]
            dx = float(point["x_east_m"]) - float(pose["x_east_m"])
            dy = float(point["y_north_m"]) - float(pose["y_north_m"])
            distance = math.hypot(dx, dy)
            # +local-Y is the source mesh front and points toward the camera.
            front_east = -math.sin(float(pose["yaw_rad"]))
            front_north = math.cos(float(pose["yaw_rad"]))
            assert (front_east * dx + front_north * dy) / distance > 0.95
            yaw = math.degrees(math.atan2(float(pose["x_east_m"]) - point["x_east_m"], float(pose["y_north_m"]) - point["y_north_m"]))
            bbox = project_target_bbox(
                target_ned=(float(pose["y_north_m"]), float(pose["x_east_m"]), 0.0),
                pose={"local_ned_m": (point["y_north_m"], point["x_east_m"], -observation["camera_altitude_m"]), "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": yaw}},
                camera_info=DEFAULT_CAMERA_INFO,
                image_size=(DEFAULT_CAMERA_INFO["width"], DEFAULT_CAMERA_INFO["height"]),
                target_size_m=tuple(float(v) for v in confuser["size_m"]),
                target_yaw_rad=float(pose["yaw_rad"]),
                target_bottom_offset_m=float(confuser["bottom_offset_m"]),
            )
            assert bbox is not None
            side = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            assert side >= 8.0
            assert math.isclose(side, predicted["minimum_short_side_px"], abs_tol=0.01)
            minimums.append(side)

    assert min(minimums) >= 8.0


def test_r9r_confuser_route_labels_recovery_and_pairs_are_view_scoped():
    from scripts.build_slow_system_sft import (
        _confusable_negative_counter,
        _derive_view,
        _effective_confusable_negative_view_cap,
    )

    intent = json.loads(
        (SCENARIOS / "gen_r21id_r9r_w0000.teacher_intent.json").read_text(encoding="utf-8")
    )
    assert intent["observation"]["views"] == intent["confuser_observation"]["views"]
    recovery = next(item for item in intent["confuser_observation"]["views"] if item["view"] == "recovery")
    waypoint = recovery["observation_waypoint"]
    derived = _derive_view(
        {
            "pose": {
                "local_ned_m": (
                    waypoint["y_north_m"],
                    waypoint["x_east_m"],
                    -intent["confuser_observation"]["camera_altitude_m"],
                )
            }
        },
        intent,
        target_id="confuser-1",
    )
    assert derived["view"] == "recovery"
    assert _confusable_negative_counter("car->mailbox", {"view": "recovery"}, None) == (
        "confusable_pair:car->mailbox:reject", 30
    )
    assert _confusable_negative_counter("car->mailbox", {"view": "recovery"}, 30) == (
        "confusable_pair:car->mailbox:reject:recovery", 30
    )
    assert _effective_confusable_negative_view_cap({"profile": "r21id-r9r"}, None) == 30
    assert _effective_confusable_negative_view_cap({"profile": "r21id-r9q"}, None) is None
    assert _effective_confusable_negative_view_cap({"profile": "r21id-r9r"}, 12) == 12

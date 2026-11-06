import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO
from multidetect.simulator.camera_projection import project_target_bbox, visible_fraction


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "configs/scenarios/generated-r21id-r9q"
WORLDS = ROOT / "simulation/gazebo/worlds"
CATALOG = ROOT / "configs/sim-assets/fuel-catalog.json"
VIEWS = ("front", "oblique", "edge_truncated", "recovery")


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _confuser(truth):
    return next(row for row in truth["targets"] if row.get("confuser") and row["asset_slug"] == "mailbox")


def _predicted_view(confuser, waypoint, altitude):
    pose_data = confuser["pose"]
    east, north = float(pose_data["x_east_m"]), float(pose_data["y_north_m"])
    camera_east, camera_north = float(waypoint["x_east_m"]), float(waypoint["y_north_m"])
    yaw_deg = math.degrees(math.atan2(east - camera_east, north - camera_north))
    pose = {
        "local_ned_m": (camera_north, camera_east, -altitude),
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": yaw_deg},
    }
    bbox = project_target_bbox(
        target_ned=(north, east, float(pose_data["z_up_m"])),
        pose=pose,
        camera_info=DEFAULT_CAMERA_INFO,
        image_size=(DEFAULT_CAMERA_INFO["width"], DEFAULT_CAMERA_INFO["height"]),
        target_size_m=tuple(float(v) for v in confuser["size_m"]),
        target_yaw_rad=float(pose_data["yaw_rad"]),
        target_bottom_offset_m=float(confuser["bottom_offset_m"]),
    )
    assert bbox is not None
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    side = min(width, height)
    fraction = visible_fraction(
        pose=pose,
        target_ned=(north, east, float(pose_data["z_up_m"])),
        target_size_m=tuple(float(v) for v in confuser["size_m"]),
        target_yaw_rad=float(pose_data["yaw_rad"]),
        target_bottom_offset_m=float(confuser["bottom_offset_m"]),
    )
    return side, width * height, fraction, (bbox[0] >= 0 and bbox[1] >= 0 and bbox[2] <= 1280 and bbox[3] <= 960)


def test_r9q_mailbox_truth_route_and_mesh_predict_yield_in_every_view():
    index = _json(SCENARIOS / "index.json")
    catalog = _json(CATALOG)
    source_size = next(row["measured_size_m"] for row in catalog["assets"] if row["slug"] == "mailbox")
    assert index["profile"] == "r21id-r9q"
    assert len(index["worlds"]) == 4
    assert {row["r9q_direction"] for row in index["worlds"]} == {"car->mailbox"}

    minima = []
    for spec in index["worlds"]:
        world_id = spec["world_id"]
        truth = _json(SCENARIOS / f"{world_id}.truth.json")
        intent = _json(SCENARIOS / f"{world_id}.teacher_intent.json")["confuser_observation"]
        mailbox = _confuser(truth)
        size = tuple(float(v) for v in mailbox["size_m"])

        assert tuple(size) == tuple(source_size)
        assert mailbox["measured_size_m"] == mailbox["size_m"]

        sdf = ET.parse(WORLDS / f"{world_id}.sdf")
        model = sdf.find(f".//model[@name='{mailbox['target_id']}']")
        assert model is not None
        sdf_pose = [float(v) for v in model.findtext("pose").split()]
        assert sdf_pose[:2] == [round(mailbox["pose"]["x_east_m"], 3), round(mailbox["pose"]["y_north_m"], 3)]
        assert math.isclose(sdf_pose[5], mailbox["pose"]["yaw_rad"], abs_tol=1e-5)
        bounds = tuple(float(v) for v in model.findtext("./link[@name='bounds']/collision/geometry/box/size").split())
        assert all(math.isclose(a, b, abs_tol=1e-5) for a, b in zip(bounds, size, strict=True))
        scales = [tuple(float(v) for v in node.text.split()) for node in model.findall(".//mesh/scale")]
        assert len(scales) == 2 and all(scale == (1.0, 1.0, 1.0) for scale in scales)

        assert len(intent["views"]) == 4
        assert tuple(row["view"] for row in intent["views"]) == VIEWS
        assert intent["observation_frames"] == 8
        assert intent["camera_altitude_m"] == 3.0
        front_east, front_north = -math.sin(mailbox["pose"]["yaw_rad"]), math.cos(mailbox["pose"]["yaw_rad"])
        for view in intent["views"]:
            waypoint = view["observation_waypoint"]
            dx = waypoint["x_east_m"] - mailbox["pose"]["x_east_m"]
            dy = waypoint["y_north_m"] - mailbox["pose"]["y_north_m"]
            distance = math.hypot(dx, dy)
            # The mailbox's +local-Y front normal faces the approach camera.
            assert (front_east * dx + front_north * dy) / distance > 0.98
            side, area, visible, in_frame = _predicted_view(mailbox, waypoint, intent["camera_altitude_m"])
            assert side >= 8.0
            assert area >= 4.0
            assert visible >= 0.3
            assert in_frame
            minima.append(side)
            assert intent["max_detection_range_m"] >= math.hypot(distance, intent["camera_altitude_m"])

        geometry = spec["view_geometry"]["confuser_observation"]
        assert [row["view"] for row in geometry["per_view"]] == list(VIEWS)
        for predicted, actual in zip(geometry["per_view"], minima[-4:], strict=True):
            assert math.isclose(predicted["minimum_short_side_px"], actual, abs_tol=0.01)
        assert geometry["qualifying_frames_per_view"] == {view: 8 for view in VIEWS}
        assert geometry["mesh_scale"] == [1.0, 1.0, 1.0]

    assert min(minima) >= 8.0

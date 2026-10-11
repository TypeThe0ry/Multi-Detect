from __future__ import annotations

import json
from pathlib import Path

from multidetect.sim_teacher.policies import SDFObstacle, load_sdf_obstacles
from multidetect.simulator.camera_projection import visible_fraction

POSE = {"local_ned_m": [0.0, 0.0, -2.0], "attitude": {"yaw_deg": 0.0}}


def test_visibility_open_and_fully_hidden() -> None:
    obstacle = SDFObstacle(5.0, 0.0, 10.0, 4.0, 4.0)
    assert visible_fraction(
        pose=POSE, target_ned=(10.0, 10.0, 1.0), target_size_m=(2.0, 2.0, 2.0), obstacles=[obstacle]
    ) == 1.0
    assert visible_fraction(
        pose=POSE, target_ned=(10.0, 0.0, 1.0), target_size_m=(2.0, 2.0, 2.0), obstacles=[obstacle]
    ) == 0.0


def test_visibility_partial_and_short_obstacle() -> None:
    partial = SDFObstacle(5.0, 0.0, 1.8, 4.0, 4.0)
    value = visible_fraction(
        pose=POSE, target_ned=(10.0, 0.0, 2.0), target_size_m=(2.0, 2.0, 2.0), obstacles=[partial]
    )
    assert 0.0 < value < 1.0
    short = SDFObstacle(5.0, 0.0, 0.5, 4.0, 4.0)
    assert visible_fraction(
        pose=POSE, target_ned=(10.0, 0.0, 2.0), target_size_m=(2.0, 2.0, 2.0), obstacles=[short]
    ) == 1.0


def test_gen_v2_apartment_occludes_ambulance() -> None:
    root = Path(__file__).parents[1]
    world = "gen_v2_w0000"
    truth = json.loads(
        (root / "configs/scenarios/generated-v2" / f"{world}.truth.json").read_text()
    )["targets"][0]
    obstacles = load_sdf_obstacles(root / "simulation/gazebo/worlds" / f"{world}.sdf")
    obstacle = obstacles[1]
    target_n, target_e = truth["pose"]["y_north_m"], truth["pose"]["x_east_m"]
    camera_n = target_n + 2.0 * (obstacle.north_m - target_n)
    camera_e = target_e + 2.0 * (obstacle.east_m - target_e)
    pose = {"local_ned_m": [camera_n, camera_e, -5.0], "attitude": {"yaw_deg": 0.0}}
    assert visible_fraction(
        pose=pose,
        target_ned=(target_n, target_e, truth["pose"]["z_up_m"]),
        target_size_m=tuple(truth["size_m"]),
        target_yaw_rad=truth["pose"]["yaw_rad"],
        target_bottom_offset_m=truth["bottom_offset_m"],
        obstacles=obstacles,
    ) < 0.3

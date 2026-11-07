"""Every airframe named by a mission suite must resolve in the SITL collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.collect_sim_teacher_episodes import (
    _airframe_profile,
    _camera_sensor_topics,
    _rgb_camera_topics,
)

pytestmark = pytest.mark.onboard

ROOT = Path(__file__).resolve().parents[1]


def _suite_airframes():
    for path in sorted((ROOT / "configs" / "onboard").glob("suite-*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for mission in document.get("missions", []):
            if isinstance(mission, dict) and mission.get("airframe"):
                yield path.name, mission.get("id"), mission["airframe"], mission.get("takeoff_method")


@pytest.mark.parametrize("suite,mission,airframe,takeoff", list(_suite_airframes()))
def test_suite_airframe_resolves(suite, mission, airframe, takeoff):
    # A bare "rc_cessna" once slipped into suite-airframes.json and every
    # fixed-wing mission died at launch with "unknown airframe profile".
    _airframe_profile(airframe, takeoff)


def test_gimbal_nightly_suite_contains_expanded_gimbal_missions():
    import json

    suite = json.loads(
        (ROOT / "configs/onboard/suite-gimbal-r21idr3-r24plan.json").read_text(
            encoding="utf-8"
        )
    )
    expected_ids = {
        "gimbal-inspect-postbox",
        "gimbal-survey-site-overlap",
        "gimbal-count-cones",
        "gimbal-find-suv-land",
        "gimbal-monitor-backpacks",
        "gimbal-operator-pause-resume",
        "gimbal-count-fire-hydrants",
    }
    assert {mission["id"] for mission in suite["missions"]} == expected_ids
    assert all(mission["airframe"] == "x500_gimbal" for mission in suite["missions"])
    assert all(
        mission["recording"] == {
            "per_step": ["gimbal_joint_state", "propeller_joint_state"]
        }
        for mission in suite["missions"]
    )


def test_gimbal_variants_keep_non_gimbal_expectations_and_record_joint_state():
    import json

    gimbal = json.loads(
        (ROOT / "configs/onboard/suite-gimbal-r21idr3-r24plan.json").read_text(
            encoding="utf-8"
        )
    )
    main = json.loads(
        (ROOT / "configs/onboard/suite-main-r21idr3-r24plan.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {mission["id"]: mission for mission in main["missions"]}
    pairs = {
        "gimbal-find-suv-land": "find-suv-land",
        "gimbal-monitor-backpacks": "monitor-backpacks",
        "gimbal-operator-pause-resume": "operator-pause-resume",
        "gimbal-count-fire-hydrants": "count-fire-hydrants",
    }
    missions = {mission["id"]: mission for mission in gimbal["missions"]}
    for gimbal_id, base_id in pairs.items():
        variant = missions[gimbal_id]
        base = by_id[base_id]
        assert variant["expect"] == base["expect"]
        assert variant["world"] == base["world"]
        assert variant["index"] == base["index"]
        assert variant["recording"] == {
            "per_step": ["gimbal_joint_state", "propeller_joint_state"]
        }
    assert missions["gimbal-operator-pause-resume"]["mission_package"]["mission_id"] == (
        "gimbal-operator-pause-resume"
    )


def test_gimbal_rgb_topics_follow_the_px4_camera_link():
    assert _rgb_camera_topics(
        world="default", model="x500_gimbal_0", airframe="x500_gimbal"
    ) == (
        "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/image",
        "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/camera_info",
    )


def test_gimbal_sensor_topics_follow_merged_camera_structure():
    assert _camera_sensor_topics(
        world="default", model="x500_gimbal_0", airframe="x500_gimbal"
    ) == {
        "image": "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/image",
        "camera_info": "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/camera_info",
        "depth_image": "/world/default/model/x500_gimbal_0/link/depth_camera_link/sensor/depth_camera/depth_image",
        "depth_camera_info": "/world/default/model/x500_gimbal_0/link/depth_camera_link/sensor/depth_camera/camera_info",
        "segmentation": "/multidetect/teacher/segmentation/labels_map",
    }

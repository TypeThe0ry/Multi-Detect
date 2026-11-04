"""Every airframe named by a mission suite must resolve in the SITL collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.collect_sim_teacher_episodes import _airframe_profile, _rgb_camera_topics

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


def test_gimbal_nightly_suite_contains_three_gimbal_missions():
    import json

    suite = json.loads(
        (ROOT / "configs/onboard/suite-gimbal-r21idr3-r24plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert {mission["id"] for mission in suite["missions"]} == {
        "gimbal-inspect-postbox",
        "gimbal-survey-site-overlap",
        "gimbal-count-cones",
    }
    assert all(mission["airframe"] == "x500_gimbal" for mission in suite["missions"])


def test_gimbal_rgb_topics_follow_the_px4_camera_link():
    assert _rgb_camera_topics(
        world="default", model="x500_gimbal_0", airframe="x500_gimbal"
    ) == (
        "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/image",
        "/world/default/model/x500_gimbal_0/link/camera_link/sensor/camera/camera_info",
    )

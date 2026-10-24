"""Every airframe named by a mission suite must resolve in the SITL collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.collect_sim_teacher_episodes import _airframe_profile

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

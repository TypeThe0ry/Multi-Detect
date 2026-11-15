from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.realworld import long_range_flight as flight


def test_sidecar_name_and_target_distance_contract(tmp_path: Path) -> None:
    scenario = tmp_path / "demo.scenario.json"
    assert flight.sidecar_path(scenario).name == "demo.long_range.json"


def test_long_range_policy_emits_waypoints_then_stop() -> None:
    policy = flight.LongRangePolicy([(1.0, 2.0, -100.0), (3.0, 4.0, -100.0)], 80.0)
    first = policy.next_action()
    second = policy.next_action()
    final = policy.next_action()
    assert first.kind == "reposition"
    assert first.target_ned == (1.0, 2.0, -100.0)
    assert first.radius_m == 80.0
    assert second.target_ned == (3.0, 4.0, -100.0)
    assert final.kind == "stop"


def test_bind_target_adds_live_home_local_ned() -> None:
    action = flight.MotionCommand("reposition", target_ned=(100.0, -50.0, -80.0), radius_m=80.0)
    bound = flight.bind_target(action, (12.0, 7.0, 3.0))
    assert bound.target_ned == (112.0, -43.0, -77.0)


def test_load_mission_rejects_short_route_before_runtime(tmp_path: Path) -> None:
    scenario = tmp_path / "short.scenario.json"
    sidecar = flight.sidecar_path(scenario)
    sidecar.write_text(json.dumps({"schema_version": 1, "extent_m": 6000,
                                   "mission_compile_runtime": {}, "task": {}, "context": {},
                                   "minimum_max_distance_m": 4000}))
    with pytest.raises((KeyError, ValueError, TypeError)):
        flight.load_mission(scenario)


def test_plan_only_requires_reserved_slot_count(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "7")
    with pytest.raises(SystemExit):
        flight.main(["7", "missing.scenario.json", str(tmp_path), "--plan-only"])

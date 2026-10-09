from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.gazebo_capture_plan import (
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    build_capture_plan,
    load_capture_plan,
)


def test_mainline_capture_plan_uses_real_sdf_pose_only_for_offline_route() -> None:
    document = build_capture_plan(DEFAULT_MANIFEST)
    assert document["schema"] == "multidetect.gazebo-3d-capture-plan-v1"
    entries = {item["scenario_id"]: item for item in document["scenarios"]}
    urban = entries["urban_inspection_multirotor_001"]
    assert urban["status"] == "ready_to_execute"
    assert len(urban["capture_route"]) == 5
    assert all(point["altitude_m"] <= 12.0 for point in urban["capture_route"])
    assert urban["target_pose_used_for_runtime_model_input"] is False
    assert urban["target_pose_used_for_capture_route"] is False
    assert urban["target_model_id"] == "model://multidetect_search_target_v1"
    assert entries["river_rescue_vtol_001"]["capture_route"] == []
    assert entries["coastal_patrol_fixed_wing_001"]["capture_route"] == []


def test_capture_plan_loader_excludes_target_pose_from_runtime(tmp_path: Path) -> None:
    path = tmp_path / "capture-plan.json"
    path.write_text(
        json.dumps(build_capture_plan(DEFAULT_MANIFEST), ensure_ascii=False), encoding="utf-8"
    )
    loaded = load_capture_plan(path, scenario_id="urban_inspection_multirotor_001")
    assert loaded["route"]
    assert loaded["target_pose_excluded_from_runtime"] is True
    assert loaded["target_pose_excluded_from_model_input"] is True
    assert loaded["route_is_initial_mission_geometry_only"] is True
    assert "target_pose_enu_m" not in loaded


def test_capture_route_does_not_change_when_target_pose_changes(tmp_path: Path) -> None:
    document = build_capture_plan(DEFAULT_MANIFEST)
    changed = json.loads(json.dumps(document))
    changed["scenarios"][0]["target_pose_enu_m"] = {
        "east_m": -999.0,
        "north_m": 888.0,
        "up_m": 0.0,
        "yaw_rad": 0.0,
    }
    original_route = document["scenarios"][0]["capture_route"]
    assert changed["scenarios"][0]["capture_route"] == original_route


def test_capture_plan_loader_rejects_unsupported_vehicle_route() -> None:
    with pytest.raises(ValueError, match="not executable"):
        load_capture_plan(
            DEFAULT_OUTPUT,
            scenario_id="river_rescue_vtol_001",
            vehicle_class="vtol",
        )

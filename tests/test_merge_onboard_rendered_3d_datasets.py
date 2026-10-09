from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.merge_onboard_rendered_3d_datasets import DatasetMergeError, merge_datasets


def _dataset(path: Path, scenario: str, frame: str, rgb_hash: str) -> None:
    payload = {
        "schema_version": 1,
        "contract": "multidetect.onboard-rendered-3d-dataset-v1",
        "feature_names": ["f0"],
        "task_action_order": ["search"],
        "flight_action_order": ["hover"],
        "rows": [
            {
                "features": [0.0],
                "task_label": 0,
                "flight_label": 0,
                "regression": [0.0],
                "family": "rendered",
                "source": {
                    "scenario_id": scenario,
                    "frame_id": frame,
                    "rgb_payload_sha256": rgb_hash,
                    "depth_payload_sha256": "d" * 64,
                    "depth_median": 3.0,
                    "geometry_source": "camera_calibration_depth_pose_ned",
                    "event_kind": "target_lost",
                    "truth_injected": False,
                },
            }
        ],
        "source_manifests": [{"path": f"{scenario}.json", "sha256": "a" * 64}],
        "pixel_level_rendered": True,
        "truth_free": True,
        "truth_injected": False,
        "training_ready": False,
        "reviewed_annotation_manifest": None,
        "depth_required": True,
        "external_gazebo_runtime": True,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_merge_requires_multiple_scenes_and_preserves_review_gate(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _dataset(first, "urban_inspection_multirotor_001", "frame-0", "1" * 64)
    _dataset(second, "river_rescue_vtol_001", "frame-0", "2" * 64)
    merged = merge_datasets([first, second])
    assert merged["scene_ids"] == ["river_rescue_vtol_001", "urban_inspection_multirotor_001"]
    assert merged["training_ready"] is False
    assert merged["depth_rows"] == 2
    assert merged["calibrated_geometry_rows"] == 2
    assert merged["merge_policy"]["synthetic_rows_included"] is False


def test_duplicate_frame_identity_fails_closed(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _dataset(first, "urban_inspection_multirotor_001", "frame-0", "1" * 64)
    _dataset(second, "urban_inspection_multirotor_001", "frame-0", "1" * 64)
    with pytest.raises(DatasetMergeError, match="duplicate RGB frame identity"):
        merge_datasets([first, second])


def test_colour_fixture_fails_closed(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _dataset(first, "gazebo_red_green_search_001", "frame-0", "1" * 64)
    _dataset(second, "river_rescue_vtol_001", "frame-0", "2" * 64)
    with pytest.raises(DatasetMergeError, match="excluded colour fixture"):
        merge_datasets([first, second])

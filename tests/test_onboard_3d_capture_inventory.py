from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from audit_onboard_3d_capture_inventory import audit_inventory  # noqa: E402


def _write_capture(root: Path, *, current: bool, with_depth: bool) -> None:
    capture = root / ("current" if current else "stale")
    capture.mkdir(parents=True)
    rgb_file = capture / "frame-000000.bgr24"
    rgb_file.write_bytes(b"rgb")
    rgb = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "scenario_id": "scene_001",
        "scenario_manifest_hash": "a" * 64 if current else "b" * 64,
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "truth_loaded_by_runtime": False,
        },
        "frames": [
            {
                "file": rgb_file.name,
                "frame_id": "frame-0",
                "sequence": 0,
                "sha256": "c" * 64,
                "provenance": {"record_sha256": "d" * 64},
            }
        ],
    }
    (capture / "gazebo-bgr24-frame-set.manifest.json").write_text(
        json.dumps(rgb), encoding="utf-8"
    )
    if with_depth:
        depth_file = capture / "depth-000000.float32_m"
        depth_file.write_bytes(b"depth")
        depth = {
            "schema": "multidetect.gazebo-depth-companion-set",
            "scenario_id": "scene_001",
            "scenario_manifest_hash": rgb["scenario_manifest_hash"],
            "source_attribution": {"truth_loaded_by_runtime": False},
            "frames": [
                {
                    "file": depth_file.name,
                    "frame_id": "frame-0",
                    "sequence": 0,
                    "rgb_record_sha256": "d" * 64,
                }
            ],
        }
        (capture / "gazebo-depth-companion-set.manifest.json").write_text(
            json.dumps(depth), encoding="utf-8"
        )


def test_inventory_keeps_stale_or_unpaired_captures_out_of_training(tmp_path: Path) -> None:
    evaluation = tmp_path / "evaluation"
    _write_capture(evaluation, current=True, with_depth=True)
    _write_capture(evaluation, current=False, with_depth=False)
    mainline = tmp_path / "mainline.json"
    mainline.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "scenario_id": "scene_001",
                        "scenario_manifest": {"sha256": "a" * 64},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = audit_inventory(
        evaluation_root=evaluation,
        mainline_manifest=mainline,
        minimum_frames=1,
    )

    assert report["scanned_rgb_manifests"] == 2
    assert report["capture_eligible_count"] == 1
    assert report["training_ready_count"] == 0
    current = next(item for item in report["captures"] if item["capture_eligible"])
    stale = next(item for item in report["captures"] if not item["capture_eligible"])
    assert current["depth_companion"]["matches_rgb"] is True
    assert any("stale" in reason for reason in stale["reasons"])
    assert any("depth" in reason for reason in stale["reasons"])


def test_inventory_uses_runtime_canonical_scenario_hash(tmp_path: Path) -> None:
    scenario = tmp_path / "scene_001.scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario_id": "scene_001",
                "scenario_version": 1,
                "channels": {
                    "runtime_input": {"path": "runtime.json", "sha256": "a" * 64},
                    "truth": {"path": "truth.json", "sha256": "b" * 64},
                    "expectations": {"path": "expectations.json", "sha256": "c" * 64},
                },
            }
        ),
        encoding="utf-8",
    )
    from multidetect.simulation.index import load_scenario_index

    canonical_hash = load_scenario_index(scenario)[1].canonical_hash
    evaluation = tmp_path / "evaluation"
    capture = evaluation / "current"
    capture.mkdir(parents=True)
    rgb_file = capture / "frame-000000.bgr24"
    rgb_file.write_bytes(b"rgb")
    (capture / "gazebo-bgr24-frame-set.manifest.json").write_text(
        json.dumps(
            {
                "schema": "multidetect.gazebo-bgr24-frame-set",
                "scenario_id": "scene_001",
                "scenario_manifest_hash": canonical_hash,
                "source_attribution": {
                    "pixels": "actual_gazebo_camera_sensor_output",
                    "truth_loaded_by_runtime": False,
                },
                "frames": [
                    {
                        "file": rgb_file.name,
                        "frame_id": "frame-0",
                        "sequence": 0,
                        "provenance": {"record_sha256": "d" * 64},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    depth_file = capture / "depth-000000.float32_m"
    depth_file.write_bytes(b"depth")
    (capture / "gazebo-depth-companion-set.manifest.json").write_text(
        json.dumps(
            {
                "schema": "multidetect.gazebo-depth-companion-set",
                "scenario_id": "scene_001",
                "scenario_manifest_hash": canonical_hash,
                "source_attribution": {"truth_loaded_by_runtime": False},
                "frames": [
                    {
                        "file": depth_file.name,
                        "frame_id": "frame-0",
                        "sequence": 0,
                        "rgb_record_sha256": "d" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    mainline = tmp_path / "mainline.json"
    mainline.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "scenario_id": "scene_001",
                        "scenario_manifest": {
                            "path": scenario.name,
                            "sha256": "e" * 64,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = audit_inventory(
        evaluation_root=evaluation,
        mainline_manifest=mainline,
        minimum_frames=1,
    )

    assert report["capture_eligible_count"] == 1

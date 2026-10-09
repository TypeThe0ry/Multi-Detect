from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_bind_gazebo_annotation_staging import _documents

from scripts.adapt_gazebo_annotation_staging_to_frame_set import (
    AnnotationStagingAdapterError,
    adapt,
)
from scripts.bind_gazebo_annotation_staging import bind
from scripts.gazebo_3d_annotation_frame_set import load_annotation_frame_set


def test_adapter_produces_canonical_frame_set_for_builder(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    bound_path = tmp_path / "bound.json"
    bound = bind(
        staging_manifest_path=staging,
        observation_path=observations,
        output_path=bound_path,
    )
    rgb_hash = bound["frames"][0]["rgb_payload_sha256"]
    rgb_manifest = tmp_path / "rgb-manifest.json"
    rgb_manifest.write_text(
        json.dumps(
            {
                "schema": "multidetect.gazebo-bgr24-frame-set",
                "scenario_id": "urban_inspection_multirotor_001",
                "scenario_manifest_hash": "b" * 64,
                "frames": [
                    {
                            "sequence": 0,
                            "frame_id": "gazebo-sim-000000",
                            "file": "rgb/000000.bgr24",
                            "sha256": rgb_hash,
                        "provenance": {"captured_at_s": 0.0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "canonical.json"
    result = adapt(
        staging_bind_path=bound_path,
        rgb_manifest_path=rgb_manifest,
        output_path=output,
    )
    assert result["schema_version"] == 1
    loaded = load_annotation_frame_set(output, rgb_manifest=rgb_manifest)
    assert list(loaded) == [(0, "gazebo-sim-000000", rgb_hash)]
    assert loaded[(0, "gazebo-sim-000000", rgb_hash)]["bounding_box_3d"]["label_id"] == 42


def test_adapter_rejects_rgb_identity_drift(tmp_path: Path) -> None:
    staging, observations = _documents(tmp_path)
    bound_path = tmp_path / "bound.json"
    bind(
        staging_manifest_path=staging,
        observation_path=observations,
        output_path=bound_path,
    )
    rgb_manifest = tmp_path / "rgb-manifest.json"
    rgb_manifest.write_text(
        json.dumps(
            {
                "schema": "multidetect.gazebo-bgr24-frame-set",
                "scenario_id": "urban_inspection_multirotor_001",
                "scenario_manifest_hash": "b" * 64,
                "frames": [
                    {
                        "sequence": 0,
                        "frame_id": "gazebo-sim-000000",
                        "sha256": hashlib.sha256(b"wrong").hexdigest(),
                        "provenance": {"captured_at_s": 0.0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AnnotationStagingAdapterError, match="exactly bind"):
        adapt(
            staging_bind_path=bound_path,
            rgb_manifest_path=rgb_manifest,
            output_path=tmp_path / "canonical.json",
        )

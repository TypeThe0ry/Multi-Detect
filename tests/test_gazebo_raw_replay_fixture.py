from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from multidetect.backends import RawSensorReplayRecording
from multidetect.product_version import load_product_version
from scripts.create_gazebo_raw_replay_fixture import create_fixture


def _write_capture(root: Path, *, frame_count: int = 3) -> Path:
    frames: list[dict[str, object]] = []
    for index in range(frame_count):
        image = np.zeros((8, 10, 3), dtype=np.uint8)
        image[:, :, 0] = 20 + index
        image[:, :, 1] = 40
        image[:, :, 2] = 80
        encoded_ok, encoded = cv2.imencode(".ppm", image)
        assert encoded_ok
        name = f"sensor-frame-{index + 1:04d}.ppm"
        (root / name).write_bytes(encoded.tobytes())
        frames.append(
            {
                "index": index,
                "stamp_ns": 1_000_000_000 + index * 100_000_000,
                "width": 10,
                "height": 8,
                "persisted_path": name,
                "navsat": {
                    "latitude_deg": 47.397 + index * 0.000001,
                    "longitude_deg": 8.546 + index * 0.000001,
                    "altitude_m": 2.0,
                },
            }
        )
    capture = {
        "schema": "multidetect.gazebo-camera-capture",
        "schema_version": 1,
        "gate_passed": True,
        "frames": frames,
        "latest_telemetry": {},
        "image_topic": "/world/default/model/x500_mono_cam_0/link/camera_link/sensor/camera/image",
        "navsat_topic": (
            "/world/default/model/x500_mono_cam_0/link/base_link/"
            "sensor/navsat_sensor/navsat"
        ),
        "model_instance": "x500_mono_cam_0",
    }
    capture_path = root / "capture.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    return capture_path


def test_gazebo_capture_binds_to_verified_raw_replay_without_truth(tmp_path: Path) -> None:
    capture_directory = tmp_path / "capture"
    capture_directory.mkdir()
    _write_capture(capture_directory)
    output = tmp_path / "fixture"

    result = create_fixture(capture_directory, output)

    assert result["fixture_type"] == "multidetect.gazebo-harmonic-raw-replay"
    assert result["frame_count"] == 3
    assert result["telemetry_fallback_count"] == 0
    assert result["invalid_navsat_count"] == 0
    assert result["ground_truth_present"] is False
    manifest_path = Path(result["recording_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == result[
        "recording_manifest_sha256"
    ]
    recording = RawSensorReplayRecording.from_manifest(manifest_path)
    assert len(recording.frames) == 3
    assert len(recording.telemetry_samples) == 3
    assert recording.telemetry_samples[0].telemetry.latitude_deg == 47.397
    assert manifest["recording_hash"] == recording.recording_hash
    assert result["release_label"] == load_product_version().release_label


def test_gazebo_capture_requires_persisted_frames(tmp_path: Path) -> None:
    capture_directory = tmp_path / "capture"
    capture_directory.mkdir()
    capture = {
        "schema": "multidetect.gazebo-camera-capture",
        "gate_passed": True,
        "frames": [
            {"stamp_ns": 1, "width": 10, "height": 8},
            {"stamp_ns": 2, "width": 10, "height": 8},
            {"stamp_ns": 3, "width": 10, "height": 8},
        ],
    }
    (capture_directory / "capture.json").write_text(json.dumps(capture), encoding="utf-8")

    try:
        create_fixture(capture_directory, tmp_path / "fixture")
    except ValueError as exc:
        assert "--persist-frames" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("fixture conversion accepted frames without persisted pixels")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("latitude_deg", 90.0),
        ("longitude_deg", 181.0),
        ("altitude_m", 100_001.0),
    ),
)
def test_gazebo_capture_drops_out_of_domain_navsat_without_promoting_position(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    capture_directory = tmp_path / "capture"
    capture_directory.mkdir()
    capture_path = _write_capture(capture_directory)
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture["frames"][0]["navsat"][field] = value
    capture_path.write_text(json.dumps(capture), encoding="utf-8")

    result = create_fixture(capture_directory, tmp_path / "fixture")

    assert result["invalid_navsat_count"] == 1
    recording = RawSensorReplayRecording.from_manifest(Path(result["recording_manifest"]))
    assert recording.telemetry_samples[0].telemetry.position_healthy is False
    assert math.isnan(recording.telemetry_samples[0].telemetry.latitude_deg)
    assert math.isnan(recording.telemetry_samples[0].telemetry.longitude_deg)

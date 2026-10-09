from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from build_onboard_rendered_3d_dataset import (  # noqa: E402
    _depth_median_for_bbox,
    _sample_from_frame,
    _verify_gazebo_provenance,
    build_dataset,
)
from train_onboard_model import _rendered_3d_samples  # noqa: E402

from multidetect.multimodal_ranging import CameraCalibration  # noqa: E402


def test_invalidated_annotation_materialization_cannot_reenter_training(tmp_path: Path) -> None:
    invalidated = tmp_path / (
        "artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1/materialized-r4"
    )
    invalidated.mkdir(parents=True)
    with pytest.raises(ValueError, match="explicitly invalidated"):
        _verify_gazebo_provenance({}, invalidated)


def test_depth_semantics_match_live_adapter_for_optical_axis_z() -> None:
    values = np.full((2, 2), 10.0, dtype=np.float32)
    depth_frame = {
        "distance_semantics": "optical_axis_z_m",
        "fx_px": 1.0,
        "fy_px": 1.0,
        "cx_px": 1.0,
        "cy_px": 1.0,
    }
    observed = _depth_median_for_bbox(
        values,
        bbox=(0.5, 0.0, 1.0, 1.0),
        depth_frame=depth_frame,
    )
    # The persisted depth is optical-axis Z.  The live adapter multiplies
    # each pixel by the same ray norm before taking the robust median.
    assert observed == pytest.approx(10.0 * np.sqrt(1.5), rel=1.0e-6)


def test_optical_axis_conversion_accepts_read_only_persisted_depth_buffer() -> None:
    # Real FLOAT32 payloads are loaded with np.frombuffer and therefore expose
    # a read-only view; the converter must copy only the selected ROI before
    # applying the ray-slant calibration.
    values = np.frombuffer(np.full((2, 2), 10.0, dtype=np.float32).tobytes(), dtype=np.float32)
    values = values.reshape((2, 2))
    assert not values.flags.writeable
    observed = _depth_median_for_bbox(
        values,
        bbox=(0.5, 0.0, 1.0, 1.0),
        depth_frame={
            "distance_semantics": "optical_axis_z_m",
            "fx_px": 1.0,
            "fy_px": 1.0,
            "cx_px": 1.0,
            "cy_px": 1.0,
        },
    )
    assert observed == pytest.approx(10.0 * np.sqrt(1.5), rel=1.0e-6)


def test_depth_semantics_keep_ray_slant_values_unchanged() -> None:
    values = np.full((2, 2), 10.0, dtype=np.float32)
    observed = _depth_median_for_bbox(
        values,
        bbox=(0.5, 0.0, 1.0, 1.0),
        depth_frame={"distance_semantics": "ray_slant_m"},
    )
    assert observed == pytest.approx(10.0, rel=1.0e-6)


def test_optical_axis_depth_without_intrinsics_fails_closed() -> None:
    with pytest.raises(ValueError, match="camera intrinsics"):
        _depth_median_for_bbox(
            np.full((2, 2), 10.0, dtype=np.float32),
            bbox=(0.5, 0.0, 1.0, 1.0),
            depth_frame={"distance_semantics": "optical_axis_z_m"},
            camera_calibration=CameraCalibration(
                calibration_id="rgb-only",
                width_px=2,
                height_px=2,
                fx_px=1.0,
                fy_px=1.0,
                cx_px=1.0,
                cy_px=1.0,
            ),
        )


def test_depth_values_outside_persisted_sensor_range_are_rejected() -> None:
    observed = _depth_median_for_bbox(
        np.full((2, 2), 5_000.0, dtype=np.float32),
        bbox=(0.0, 0.0, 1.0, 1.0),
        depth_frame={
            "distance_semantics": "ray_slant_m",
            "minimum_depth_m": 0.4,
            "maximum_depth_m": 800.0,
        },
    )
    assert observed is None


def _rgb_manifest(tmp_path: Path) -> Path:
    payload = bytes(range(12))
    frame_path = tmp_path / "frame-000000.bgr24"
    frame_path.write_bytes(payload)
    payload_sha = hashlib.sha256(payload).hexdigest()
    document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "scenario_id": "urban_inspection_multirotor_001",
        "wire_version": 3,
        "manifest_sha256": "a" * 64,
        "scenario_manifest_hash": "b" * 64,
        "sensor_binding_hash": "c" * 64,
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "wire": "MDGZSIM3",
            "per_frame_wire_telemetry_persisted": True,
            "wire_record_recomputed_from_persisted_bytes": True,
        },
        "per_frame_sensor_evidence": {
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "repository_model_assets": {"assets_sha256": "d" * 64},
        "terrain_dem": {"file_sha256": "e" * 64},
        "claims": {"actual_gazebo_pixels_persisted": True},
        "frames": [
            {
                "file": frame_path.name,
                "frame_id": "gazebo-sim-000000",
                "sequence": 0,
                "sha256": payload_sha,
                "provenance": {
                    "width": 2,
                    "height": 2,
                    "payload_sha256": payload_sha,
                    "frame_telemetry": {
                        "gps_fix_type": 3,
                        "gps_horizontal_accuracy_m": 1.5,
                        "altitude_agl_m": 10.0,
                        "velocity_north_mps": 1.0,
                        "velocity_east_mps": 0.5,
                        "gps_observed_at_s": 1.0,
                        "altitude_observed_at_s": 1.0,
                        "velocity_observed_at_s": 1.0,
                        "battery_remaining_pct": 80.0,
                    },
                },
            }
        ],
    }
    manifest_path = tmp_path / "gazebo-bgr24-frame-set.manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    return manifest_path


def test_build_dataset_uses_pixels_and_telemetry_without_truth(tmp_path: Path) -> None:
    manifest = _rgb_manifest(tmp_path)
    document = build_dataset(
        [manifest],
        depth_manifests=[],
        detector_path=None,
        detector_confidence=0.25,
        max_frames_per_manifest=None,
    )

    assert document["pixel_level_rendered"] is True
    assert document["truth_free"] is True
    assert document["truth_injected"] is False
    assert document["depth_measurement_rows"] == 0
    assert document["calibrated_geometry_rows"] == 0
    assert document["depth_measurement_rows"] == 0
    assert len(document["rows"]) == 1
    assert document["event_coverage"]["contract"] == "multidetect.onboard-3d-event-coverage-v1"
    assert document["event_coverage"]["truth_free"] is True
    assert document["event_coverage"]["training_ready"] is False
    row = document["rows"][0]
    assert len(row["features"]) == 40
    assert row["features"][22] == 0.0
    assert row["source"]["truth_injected"] is False
    assert row["source"]["detector_count"] == 0
    assert row["source"]["event_kind"] == "target_lost"
    assert document["event_coverage"]["event_kind_counts"]["target_lost"] == 1


def test_rendered_target_label_uses_calibrated_depth_and_ned_geometry() -> None:
    class BBox:
        def rounded(self) -> tuple[float, float, float, float]:
            return (0.60, 0.25, 0.90, 0.75)

        @property
        def center(self) -> tuple[float, float]:
            return (0.75, 0.50)

    detection = SimpleNamespace(label="vehicle", confidence=0.90, bbox=BBox())
    detector = SimpleNamespace(detect=lambda _image: (detection,))
    frame = {
        "frame_id": "gazebo-sim-000001",
        "provenance": {
            "frame_telemetry": {
                "gps_fix_type": 3,
                "gps_horizontal_accuracy_m": 1.0,
                "altitude_agl_m": 10.0,
                "velocity_north_mps": 0.0,
                "velocity_east_mps": 0.0,
                "heading_deg": 0.0,
                "roll_deg": 0.0,
                "pitch_deg": 0.0,
                "attitude_observed_at_s": 1.0,
            }
        },
    }
    calibration = CameraCalibration(
        calibration_id="test-camera",
        width_px=2,
        height_px=2,
        fx_px=1.0,
        fy_px=1.0,
        cx_px=1.0,
        cy_px=1.0,
    )
    row = _sample_from_frame(
        scenario_id="urban_inspection_multirotor_001",
        vehicle_class="multirotor",
        frame=frame,
        image=np.zeros((2, 2, 3), dtype=np.uint8),
        detector=detector,
        depth_values=np.full((2, 2), 40.0, dtype=np.float32),
        camera_calibration=calibration,
        target_match_confirmed=True,
    )
    bearing = row["features"][14]
    distance_m = row["features"][15] * 5_000.0
    north = np.tanh(row["regression"][0])
    east = np.tanh(row["regression"][1])
    assert row["source"]["geometry_source"] == "camera_calibration_depth_pose_ned"
    assert distance_m == 40.0
    assert bearing > 0.0
    assert np.hypot(north, east) * 500.0 == pytest.approx(40.0, rel=1.0e-6)
    assert (east > 0.0) == (bearing > 0.0)


def test_calibrated_features_follow_ned_heading_and_horizontal_range() -> None:
    class BBox:
        def rounded(self) -> tuple[float, float, float, float]:
            return (0.60, 0.25, 0.90, 0.75)

        @property
        def center(self) -> tuple[float, float]:
            return (0.75, 0.50)

    detection = SimpleNamespace(label="vehicle", confidence=0.90, bbox=BBox())
    detector = SimpleNamespace(detect=lambda _image: (detection,))
    calibration = CameraCalibration(
        calibration_id="heading-camera",
        width_px=2,
        height_px=2,
        fx_px=1.0,
        fy_px=1.0,
        cx_px=1.0,
        cy_px=1.0,
        camera_offset_forward_m=2.0,
        camera_offset_right_m=1.0,
    )
    rows = []
    for heading in (0.0, 90.0, 180.0):
        rows.append(
            _sample_from_frame(
                scenario_id="urban_inspection_multirotor_001",
                vehicle_class="multirotor",
                frame={
                    "frame_id": f"heading-{heading}",
                    "provenance": {
                        "frame_telemetry": {
                            "gps_fix_type": 3,
                            "gps_horizontal_accuracy_m": 1.0,
                            "altitude_agl_m": 10.0,
                            "velocity_north_mps": 0.0,
                            "velocity_east_mps": 0.0,
                            "heading_deg": heading,
                            "roll_deg": 0.0,
                            "pitch_deg": 20.0,
                            "attitude_observed_at_s": 1.0,
                        }
                    },
                },
                image=np.zeros((2, 2, 3), dtype=np.uint8),
                detector=detector,
                depth_values=np.full((2, 2), 40.0, dtype=np.float32),
                camera_calibration=calibration,
                target_match_confirmed=True,
            )
        )

    for row in rows:
        north = np.tanh(row["regression"][0]) * 500.0
        east = np.tanh(row["regression"][1]) * 500.0
        assert row["features"][14] == pytest.approx(
            np.degrees(np.arctan2(east, north)) / 180.0,
            rel=1.0e-6,
        )
        assert row["features"][15] == pytest.approx(
            np.hypot(north, east) / 5_000.0,
            rel=1.0e-6,
        )
        assert row["features"][15] * 5_000.0 != pytest.approx(40.0, rel=1.0e-3)

    # Changing aircraft heading must rotate the NED label and the canonical
    # bearing feature together; it must not leave one input with three labels.
    assert rows[0]["features"] != rows[1]["features"]
    assert rows[0]["regression"] != rows[1]["regression"]


def test_uncalibrated_rendered_frame_never_invents_ned_bearing() -> None:
    class BBox:
        def rounded(self) -> tuple[float, float, float, float]:
            return (0.60, 0.25, 0.90, 0.75)

        @property
        def center(self) -> tuple[float, float]:
            return (0.75, 0.50)

    detection = SimpleNamespace(label="vehicle", confidence=0.90, bbox=BBox())
    detector = SimpleNamespace(detect=lambda _image: (detection,))
    row = _sample_from_frame(
        scenario_id="urban_inspection_multirotor_001",
        vehicle_class="multirotor",
        frame={
            "frame_id": "uncalibrated",
            "provenance": {
                "frame_telemetry": {
                    "gps_fix_type": 3,
                    "gps_horizontal_accuracy_m": 1.0,
                    "altitude_agl_m": 10.0,
                    "velocity_north_mps": 0.0,
                    "velocity_east_mps": 0.0,
                    "heading_deg": 90.0,
                    "gps_observed_at_s": 1.0,
                }
            },
        },
        image=np.zeros((2, 2, 3), dtype=np.uint8),
        detector=detector,
        depth_values=np.full((2, 2), 40.0, dtype=np.float32),
        camera_calibration=None,
    )

    assert row["source"]["geometry_source"] == "missing_camera_calibration"
    assert row["features"][14] == 0.0
    assert np.tanh(row["regression"][0]) == pytest.approx(0.0)
    assert np.tanh(row["regression"][1]) == pytest.approx(0.0)


def test_train_loader_rejects_truth_injected_rendered_dataset(tmp_path: Path) -> None:
    manifest = _rgb_manifest(tmp_path)
    document = build_dataset(
        [manifest],
        depth_manifests=[],
        detector_path=None,
        detector_confidence=0.25,
        max_frames_per_manifest=None,
    )
    document["truth_injected"] = True
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(document), encoding="utf-8")

    try:
        _rendered_3d_samples(dataset)
    except ValueError as exc:
        assert "truth_injected" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("truth-injected rendered dataset was accepted")


def test_build_dataset_frame_hash_and_shape_are_verified(tmp_path: Path) -> None:
    manifest = _rgb_manifest(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["frames"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    try:
        build_dataset(
            [manifest],
            depth_manifests=[],
            detector_path=None,
            detector_confidence=0.25,
            max_frames_per_manifest=None,
        )
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("RGB frame hash mismatch was accepted")


def test_depth_bound_build_fails_closed_without_companion(tmp_path: Path) -> None:
    manifest = _rgb_manifest(tmp_path)
    try:
        build_dataset(
            [manifest],
            depth_manifests=[],
            detector_path=None,
            detector_confidence=0.25,
            max_frames_per_manifest=None,
            require_depth=True,
        )
    except ValueError as exc:
        assert "depth companion is required" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("depth-unbound rendered dataset was accepted")


def test_mainline_binding_rejects_self_declared_hashes(tmp_path: Path) -> None:
    manifest = _rgb_manifest(tmp_path)
    try:
        build_dataset(
            [manifest],
            depth_manifests=[],
            detector_path=None,
            detector_confidence=0.25,
            max_frames_per_manifest=None,
            mainline_manifest=ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
        )
    except ValueError as exc:
        assert "approved mainline" in str(exc) or "scenario manifest hash" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("self-declared sensor hashes were accepted into the mainline")

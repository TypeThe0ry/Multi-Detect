from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import jsonschema
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from build_onboard_trajectory_staging import (  # noqa: E402
    _event_from_frame,
    build_staging_dataset,
)

from multidetect.autonomy import OnboardEventKind, OnboardTaskAction  # noqa: E402
from multidetect.multimodal_ranging import AircraftPose, CameraCalibration  # noqa: E402


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inputs(tmp_path: Path, *, frame_count: int = 2) -> tuple[Path, Path]:
    frames = []
    depth_frames = []
    for sequence in range(frame_count):
        rgb_payload = bytes((sequence + value) % 256 for value in range(12))
        rgb_name = f"frame-{sequence:06d}.bgr24"
        (tmp_path / rgb_name).write_bytes(rgb_payload)
        depth_payload = (float(10.0 + sequence)).hex().encode("ascii")
        # The staging reader expects FLOAT32_M bytes, not a text encoding.
        import struct

        depth_payload = struct.pack(
            "<4f", 10.0 + sequence, 10.0 + sequence, 10.0 + sequence, 10.0 + sequence
        )
        depth_name = f"depth-{sequence:06d}.float32_m"
        (tmp_path / depth_name).write_bytes(depth_payload)
        captured_at_s = 100.0 + sequence * 0.5
        frames.append(
            {
                "file": rgb_name,
                "frame_id": f"gazebo-sim-{sequence:06d}",
                "sequence": sequence,
                "sha256": _sha256(rgb_payload),
                "provenance": {
                    "width": 2,
                    "height": 2,
                    "payload_sha256": _sha256(rgb_payload),
                    "captured_at_s": captured_at_s,
                    "frame_telemetry": {
                        "latitude_deg": 1.3 + sequence * 0.00001,
                        "longitude_deg": 103.8,
                        "gps_fix_type": 3,
                        "gps_horizontal_accuracy_m": 1.5,
                        "altitude_agl_m": 12.0,
                        "velocity_north_mps": 2.0,
                        "velocity_east_mps": 0.5,
                        "ground_speed_mps": 2.0615528128,
                        "airspeed_mps": 3.0,
                        "battery_remaining_pct": 91.0,
                        "geofence_healthy": True,
                        "map_cache_available": True,
                        "gps_observed_at_s": captured_at_s,
                        "altitude_observed_at_s": captured_at_s,
                        "velocity_observed_at_s": captured_at_s,
                    },
                },
            }
        )
        depth_frames.append(
            {
                "sequence": sequence,
                "frame_id": f"gazebo-sim-{sequence:06d}",
                "file": depth_name,
                "sha256": _sha256(depth_payload),
                "encoding": "FLOAT32_M",
                "coordinate_space": "rgb_pixel_aligned",
                "registration_mode": "registered_to_rgb",
                "distance_semantics": "optical_axis_z_m",
                "fx_px": 1.0,
                "fy_px": 1.0,
                "cx_px": 1.0,
                "cy_px": 1.0,
                "width": 2,
                "height": 2,
                "stride_bytes": 8,
                "minimum_depth_m": 0.1,
                "maximum_depth_m": 3000.0,
            }
        )
    rgb_document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "scenario_id": "river_rescue_vtol_001",
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
        "per_frame_sensor_evidence": {"gps_and_pose_values_covered_by_wire_record_sha256": True},
        "repository_model_assets": {"assets_sha256": "d" * 64},
        "terrain_dem": {"file_sha256": "e" * 64},
        "claims": {"actual_gazebo_pixels_persisted": True},
        "frames": frames,
    }
    rgb_path = tmp_path / "rgb.manifest.json"
    rgb_path.write_text(json.dumps(rgb_document), encoding="utf-8")
    depth_path = tmp_path / "depth.manifest.json"
    depth_path.write_text(
        json.dumps(
            {
                "schema": "multidetect.gazebo-depth-companion-set",
                "frames": depth_frames,
            }
        ),
        encoding="utf-8",
    )
    
    return rgb_path, depth_path


def _build(tmp_path: Path, *, paths: tuple[Path, Path] | None = None, **kwargs):
    rgb, depth = _inputs(tmp_path) if paths is None else paths
    return build_staging_dataset(
        [rgb],
        depth_manifests=[depth],
        recording_manifests=[None],
        airframe_profile_id="px4_vtol_gazebo_v1",
        airframe_profile_sha256="f" * 64,
        task_contract_sha256="1" * 64,
        seed=20261005,
        split="train",
        detector_path=None,
        detector_confidence=0.25,
        **kwargs,
    )


def test_staging_rebuilds_shared_40_feature_contract_and_episode_links(tmp_path: Path) -> None:
    document = _build(tmp_path)
    assert document["contract"] == "multidetect.onboard-trajectory-staging-v1"
    assert document["training_ready"] is False
    assert document["truth_free"] is True
    episode = document["episodes"][0]
    transitions = episode["transitions"]
    assert len(transitions) == 2
    assert transitions[0]["next_frame_id"] == transitions[1]["frame_id"]
    assert transitions[1]["next_frame_id"] is None
    assert all(len(item["features"]) == 40 for item in transitions)
    assert all(item["label_status"] == "pending_independent_review" for item in transitions)
    assert document["recording_bound"] is False
    assert document["event_coverage"]["contract"] == "multidetect.onboard-3d-event-coverage-v1"
    assert document["event_coverage"]["event_kind_counts"] == {"target_lost": 2}

    schema = json.loads(
        (ROOT / "configs/schemas/onboard-trajectory-staging.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(document, schema)


def test_staging_and_reviewed_schema_keep_lifecycle_fields_distinct() -> None:
    staging_schema = json.loads(
        (ROOT / "configs/schemas/onboard-trajectory-staging.schema.json").read_text(
            encoding="utf-8"
        )
    )
    reviewed_schema = json.loads(
        (ROOT / "configs/schemas/onboard-trajectory-dataset.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator.check_schema(staging_schema)
    jsonschema.Draft202012Validator.check_schema(reviewed_schema)

    # Staging is deliberately incomplete: it must advertise that independent
    # review is still required and that its source material is not yet
    # materialized for training.
    assert staging_schema["properties"]["review_required"] == {"const": True}
    assert staging_schema["properties"]["materialized_sources"] == {"const": False}
    assert "recording_bound" in staging_schema["required"]

    # The reviewed contract is a different lifecycle state.  Its loader uses
    # the episode-level recording binding and root materialized-sources gate;
    # staging-only review_required/aggregate recording_bound must not become
    # required fields in the final artifact.
    reviewed_required = set(reviewed_schema["required"])
    assert {"review_receipt", "training_ready", "materialized_sources"} <= reviewed_required
    assert reviewed_schema["properties"]["materialized_sources"] == {"const": True}
    assert "review_required" not in reviewed_required
    assert "recording_bound" not in reviewed_required
    reviewed_episode = reviewed_schema["$defs"]["episode"]
    assert "recording_bound" in reviewed_episode["required"]
    assert reviewed_episode["properties"]["recording_bound"] == {"type": "boolean"}


def test_staging_rejects_non_monotonic_episode_timestamps(tmp_path: Path) -> None:
    rgb, depth = _inputs(tmp_path)
    raw = json.loads(rgb.read_text(encoding="utf-8"))
    raw["frames"][1]["provenance"]["captured_at_s"] = raw["frames"][0]["provenance"][
        "captured_at_s"
    ]
    rgb.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="strictly increasing"):
        _build(tmp_path, paths=(rgb, depth))


def test_staging_rejects_simulator_truth_fields(tmp_path: Path) -> None:
    rgb, depth = _inputs(tmp_path)
    raw = json.loads(rgb.read_text(encoding="utf-8"))
    raw["frames"][0]["provenance"]["target_world_position"] = {"x": 1.0}
    rgb.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="simulator truth"):
        build_staging_dataset(
            [rgb],
            depth_manifests=[depth],
            recording_manifests=[None],
            airframe_profile_id="px4_vtol_gazebo_v1",
            airframe_profile_sha256="f" * 64,
            task_contract_sha256="1" * 64,
            seed=20261005,
            split="train",
            detector_path=None,
            detector_confidence=0.25,
        )


def test_staging_rejects_missing_depth_frame(tmp_path: Path) -> None:
    rgb, depth = _inputs(tmp_path)
    raw = json.loads(depth.read_text(encoding="utf-8"))
    raw["frames"].pop()
    depth.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="no depth companion"):
        _build(tmp_path, paths=(rgb, depth))


def test_trajectory_event_uses_camera_info_depth_and_ned_pose() -> None:
    class BBox:
        def rounded(self) -> tuple[float, float, float, float]:
            return (0.60, 0.25, 0.90, 0.75)

        @property
        def center(self) -> tuple[float, float]:
            return (0.75, 0.50)

    detection = type("Detection", (), {"label": "vehicle", "confidence": 0.9, "bbox": BBox()})()
    detector = type("Detector", (), {"detect": lambda _self, _image: (detection,)})()
    calibration = CameraCalibration(
        calibration_id="trajectory-camera",
        width_px=2,
        height_px=2,
        fx_px=1.0,
        fy_px=1.0,
        cx_px=1.0,
        cy_px=1.0,
    )
    pose = AircraftPose(
        captured_at_s=10.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        heading_deg=0.0,
    )
    depth_frame = {
        "distance_semantics": "optical_axis_z_m",
        "fx_px": 1.0,
        "fy_px": 1.0,
        "cx_px": 1.0,
        "cy_px": 1.0,
    }
    event = _event_from_frame(
        image=np.zeros((2, 2, 3), dtype=np.uint8),
        depth_values=np.full((2, 2), 10.0, dtype=np.float32),
        frame_id="trajectory-frame-1",
        captured_at_s=10.0,
        detector=detector,
        task_phase=OnboardTaskAction.SEARCH,
        depth_frame=depth_frame,
        camera_calibration=calibration,
        aircraft_pose=pose,
        target_match_confirmed=True,
    )
    assert event.target_geometry_frame == "ned"
    assert event.target_distance_m == pytest.approx(10.0 * np.sqrt(1.5), rel=1.0e-6)
    assert event.target_bearing_deg is not None and event.target_bearing_deg > 0.0
    assert event.target_north_offset_m is not None
    assert event.target_east_offset_m is not None and event.target_east_offset_m > 0.0


def test_trajectory_event_does_not_invent_bearing_without_camera_info() -> None:
    class BBox:
        def rounded(self) -> tuple[float, float, float, float]:
            return (0.60, 0.25, 0.90, 0.75)

        @property
        def center(self) -> tuple[float, float]:
            return (0.75, 0.50)

    detection = type("Detection", (), {"label": "vehicle", "confidence": 0.9, "bbox": BBox()})()
    detector = type("Detector", (), {"detect": lambda _self, _image: (detection,)})()
    event = _event_from_frame(
        image=np.zeros((2, 2, 3), dtype=np.uint8),
        depth_values=np.full((2, 2), 10.0, dtype=np.float32),
        frame_id="trajectory-frame-2",
        captured_at_s=10.0,
        detector=detector,
        task_phase=OnboardTaskAction.SEARCH,
    )
    assert event.target_bearing_deg is None
    assert event.target_geometry_frame == "camera_relative"
    assert event.target_distance_m == pytest.approx(10.0, rel=1.0e-6)
    assert event.target_north_offset_m is None
    assert event.target_east_offset_m is None


@pytest.mark.parametrize(
    ("telemetry", "expected"),
    [
        ({"gps_fix_type": 2, "gps_horizontal_accuracy_m": 1.0}, OnboardEventKind.GPS_DEGRADED),
        ({"gps_fix_type": 3, "gps_horizontal_accuracy_m": 9.0}, OnboardEventKind.GPS_DEGRADED),
        (
            {"gps_fix_type": 3, "gps_horizontal_accuracy_m": 1.0, "battery_remaining_pct": 12.0},
            OnboardEventKind.BATTERY_LOW,
        ),
        (
            {"gps_fix_type": 3, "gps_horizontal_accuracy_m": 1.0, "obstacle_distance_m": 4.0},
            OnboardEventKind.OBSTACLE_RISK,
        ),
    ],
)
def test_trajectory_event_prioritizes_truth_free_telemetry_faults(telemetry, expected) -> None:
    event = _event_from_frame(
        image=np.zeros((2, 2, 3), dtype=np.uint8),
        depth_values=None,
        frame_id="trajectory-health-frame",
        captured_at_s=10.0,
        detector=None,
        telemetry=telemetry,
        task_phase=OnboardTaskAction.SEARCH,
    )
    assert event.kind is expected

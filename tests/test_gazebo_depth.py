from __future__ import annotations

import hashlib
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.gazebo_depth import (
    GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS,
    GAZEBO_DEPTH_SCHEMA,
    GAZEBO_DEPTH_SCHEMA_VERSION,
    GazeboDepthContractError,
    GazeboDepthFrame,
    GazeboDepthRangeSample,
    resolve_declared_gazebo_depth_sensor,
)
from multidetect.multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    DirectRangeSource,
    MultiModalRangingEngine,
    TargetImageObservation,
)
from multidetect.runtime import RuntimeMode
from multidetect.simulation import SensorRuntimeInput, load_runtime_scenario

ROOT = Path(__file__).resolve().parents[1]


def _sample(**changes: object) -> GazeboDepthRangeSample:
    value = GazeboDepthRangeSample(
        sensor_id="downward-depth",
        calibration_id="gazebo-multirotor-depth-v1",
        frame_id="gazebo-sim-000010",
        target_id="target-1",
        source_topic=(
            "/world/default/model/x500_mono_cam_0/link/downward_depth/"
            "sensor/depth/image"
        ),
        source_stamp_ns=10_000_000_000,
        frame_sensor_stamp_ns=10_020_000_000,
        captured_at_s=12.5,
        slant_range_m=42.0,
        sigma_m=1.5,
        valid_pixel_count=384,
    )
    return replace(value, **changes)


def test_gazebo_depth_sample_converts_to_shared_direct_range_measurement() -> None:
    sample = _sample()
    measurement = sample.measurement()
    assert measurement.source is DirectRangeSource.GAZEBO_DEPTH
    assert measurement.target_id == "target-1"
    assert measurement.slant_range_m == pytest.approx(42.0)
    assert measurement.absolute_scale_valid is True
    mapping = sample.to_mapping()
    assert mapping["schema"] == GAZEBO_DEPTH_SCHEMA
    assert mapping["schema_version"] == GAZEBO_DEPTH_SCHEMA_VERSION
    assert mapping["source_age_ns"] == 20_000_000
    assert mapping["distance_semantics"] == "ray_slant_m"
    assert mapping["truth_free"] is True
    assert mapping["flight_control_enabled"] is False


def test_gazebo_depth_frame_samples_real_metric_payload_without_truth() -> None:
    payload = b"".join(struct.pack("<H", value) for value in (4200,) * 16)
    frame = GazeboDepthFrame(
        sensor_id="downward-depth",
        calibration_id="gazebo-multirotor-depth-v1",
        frame_id="gazebo-sim-000010",
        source_topic="/world/default/model/x500_mono_cam_0/link/downward_depth/sensor/depth/image",
        width=4,
        height=4,
        stride_bytes=8,
        encoding="UINT16_MM",
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        source_stamp_ns=10_000_000_000,
        frame_sensor_stamp_ns=10_020_000_000,
        captured_at_s=12.5,
    )
    sample = frame.sample_target(
        target_id="target-1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        center_fraction=1.0,
        minimum_valid_pixels=16,
    )
    assert sample.slant_range_m == pytest.approx(4.2)
    assert sample.valid_pixel_count == 16
    assert sample.measurement().source is DirectRangeSource.GAZEBO_DEPTH
    assert frame.to_mapping()["truth_free"] is True


def test_gazebo_depth_sample_enters_the_existing_shared_fusion_engine() -> None:
    sample = _sample()
    solution = MultiModalRangingEngine().solve_direct(
        calibration=CameraCalibration(
            calibration_id="gazebo-multirotor-depth-v1",
            width_px=640,
            height_px=480,
            fx_px=400.0,
            fy_px=400.0,
            cx_px=320.0,
            cy_px=240.0,
            mount_pitch_down_deg=30.0,
        ),
        pose=AircraftPose(
            captured_at_s=12.5,
            roll_deg=0.0,
            pitch_deg=0.0,
            heading_deg=0.0,
        ),
        target=TargetImageObservation(
            target_id="target-1",
            frame_id="gazebo-sim-000010",
            captured_at_s=12.5,
            center_x=0.5,
            center_y=0.5,
        ),
        direct_measurements=(sample.measurement(),),
        now_s=12.55,
    )
    assert solution.sources == ("gazebo_depth",)
    assert solution.slant_range_m == pytest.approx(42.0)
    assert solution.source_contributions[0].source == "gazebo_depth"


def test_gazebo_depth_frame_rejects_payload_hash_or_geometry_drift() -> None:
    payload = b"\x01\x00" * 4
    common = {
        "sensor_id": "downward-depth",
        "calibration_id": "gazebo-multirotor-depth-v1",
        "frame_id": "gazebo-sim-000010",
        "source_topic": (
            "/world/default/model/x500_mono_cam_0/link/downward_depth/"
            "sensor/depth/image"
        ),
        "width": 2,
        "height": 2,
        "stride_bytes": 4,
        "encoding": "UINT16_MM",
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "source_stamp_ns": 10_000_000_000,
        "frame_sensor_stamp_ns": 10_020_000_000,
        "captured_at_s": 12.5,
    }
    with pytest.raises(GazeboDepthContractError, match="SHA-256"):
        GazeboDepthFrame(**{**common, "payload_sha256": "0" * 64})
    with pytest.raises(GazeboDepthContractError, match="payload length"):
        GazeboDepthFrame(**{**common, "width": 3, "stride_bytes": 6})


def test_gazebo_optical_axis_depth_is_normalized_to_ray_slant() -> None:
    payload = b"".join(struct.pack("<f", 4.0) for _ in range(4))
    frame = GazeboDepthFrame(
        sensor_id="downward-depth",
        calibration_id="gazebo-multirotor-depth-v1",
        frame_id="gazebo-sim-000010",
        source_topic="/world/default/model/x500_mono_cam_0/link/downward_depth/sensor/depth/image",
        width=2,
        height=2,
        stride_bytes=8,
        encoding="FLOAT32_M",
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        source_stamp_ns=10_000_000_000,
        frame_sensor_stamp_ns=10_020_000_000,
        captured_at_s=12.5,
        distance_semantics="optical_axis_z_m",
        fx_px=1.0,
        fy_px=1.0,
        cx_px=0.5,
        cy_px=0.5,
    )
    sample = frame.sample_target(
        target_id="target-1",
        bbox=(0.5, 0.5, 1.0, 1.0),
        center_fraction=1.0,
        minimum_valid_pixels=1,
    )
    assert sample.distance_semantics == "ray_slant_m"
    assert sample.slant_range_m == pytest.approx(4.0 * 3.0**0.5)

    with pytest.raises(GazeboDepthContractError, match="complete camera intrinsics"):
        GazeboDepthFrame(**{
            "sensor_id": "downward-depth",
            "calibration_id": "gazebo-multirotor-depth-v1",
            "frame_id": "gazebo-sim-000010",
            "source_topic": "/depth/image",
            "width": 2,
            "height": 2,
            "stride_bytes": 8,
            "encoding": "FLOAT32_M",
            "payload": payload,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "source_stamp_ns": 10_000_000_000,
            "frame_sensor_stamp_ns": 10_020_000_000,
            "captured_at_s": 12.5,
            "distance_semantics": "optical_axis_z_m",
        })


def test_gazebo_depth_mapping_round_trip_is_strict() -> None:
    restored = GazeboDepthRangeSample.from_mapping(_sample().to_mapping())
    assert restored == _sample()

    with pytest.raises(GazeboDepthContractError, match="unknown fields"):
        GazeboDepthRangeSample.from_mapping({**_sample().to_mapping(), "truth": {}})
    with pytest.raises(GazeboDepthContractError, match="missing required fields"):
        mapping = _sample().to_mapping()
        mapping.pop("distance_semantics")
        GazeboDepthRangeSample.from_mapping(mapping)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"source_stamp_ns": 10_030_000_000}, "future"),
        (
            {"source_stamp_ns": 10_000_000_000 - GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS - 1},
            "skew",
        ),
        ({"source_topic": "gazebo://not-a-topic"}, "absolute Gazebo topic"),
        ({"valid_pixel_count": 0}, "valid_pixel_count"),
        ({"absolute_scale_valid": "yes"}, "absolute_scale_valid"),
    ),
)
def test_gazebo_depth_sample_fails_closed_on_bad_provenance(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(GazeboDepthContractError, match=message):
        _sample(**changes)


def test_declared_depth_sensor_is_not_mistaken_for_a_live_topic() -> None:
    runtime = load_runtime_scenario(
        ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
        mode=RuntimeMode.SIM,
    )
    binding = resolve_declared_gazebo_depth_sensor(runtime.sensors)
    assert binding is not None
    assert binding.sensor_id == "downward-depth"
    assert binding.calibration_id == "gazebo-multirotor-depth-v1"
    assert binding.to_mapping()["transport_status"] == (
        "declared_only_until_bound_to_a_real_gazebo_topic"
    )


def test_declared_depth_sensor_rejects_duplicate_or_missing_calibration() -> None:
    sensors = (
        SensorRuntimeInput(
            sensor_id="depth-a",
            kind="depth_camera",
            stream_uri="gazebo://scenario/depth-a",
            calibration_id="depth-v1",
        ),
        SensorRuntimeInput(
            sensor_id="depth-b",
            kind="depth_camera",
            stream_uri="gazebo://scenario/depth-b",
            calibration_id="depth-v1",
        ),
    )
    with pytest.raises(GazeboDepthContractError, match="at most one"):
        resolve_declared_gazebo_depth_sensor(sensors)
    with pytest.raises(GazeboDepthContractError, match="calibration_id"):
        resolve_declared_gazebo_depth_sensor(
            (
                SensorRuntimeInput(
                    sensor_id="depth-a",
                    kind="depth_camera",
                    stream_uri="gazebo://scenario/depth-a",
                ),
            )
        )

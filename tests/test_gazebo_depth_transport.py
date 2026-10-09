from __future__ import annotations

import io
import struct
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.backends.gazebo_sim import GazeboSimBackend
from multidetect.multimodal_ranging import CameraCalibration
from multidetect.runtime import RuntimeMode
from multidetect.simulation import SensorRuntimeInput, load_runtime_scenario
from scripts.gazebo_depth_wire import (
    GazeboDepthWireError,
    build_depth_wire_record,
    decode_depth_wire_blob,
)
from scripts.gazebo_sensor_stream import WIRE_HEADER_BYTES, build_wire_frame, unpack_header

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


class _FakeProcess:
    def __init__(self, stream: bytes) -> None:
        self.stdout = io.BytesIO(stream)
        self.stderr = io.BytesIO()
        self.returncode = 0

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


def _calibration() -> CameraCalibration:
    return CameraCalibration(
        calibration_id="gazebo-front-camera-v1",
        width_px=2,
        height_px=1,
        fx_px=1.0,
        fy_px=1.0,
        cx_px=1.0,
        cy_px=0.5,
    )


def _scenario_with_depth():
    scenario = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    return replace(
        scenario,
        sensors=(
            *(sensor for sensor in scenario.sensors if sensor.kind != "depth_camera"),
            SensorRuntimeInput(
                sensor_id="downward-depth",
                kind="depth_camera",
                stream_uri=f"gazebo://{scenario.scenario_id}/downward-depth",
                calibration_id="gazebo-depth-v1",
            ),
        ),
    )


def _rgb_record() -> bytes:
    return build_wire_frame(
        sequence=0,
        sensor_stamp_ns=1_000_000_000,
        width=2,
        height=1,
        camera_info_stamp_ns=999_999_997,
        navsat_stamp_ns=999_999_998,
        odometry_stamp_ns=999_999_999,
        latitude_deg=47.397971,
        longitude_deg=8.546164,
        altitude_m=488.2,
        position_east_m=1.0,
        position_north_m=2.0,
        position_up_m=3.0,
        orientation_x=0.0,
        orientation_y=0.0,
        orientation_z=0.0,
        orientation_w=1.0,
        velocity_forward_mps=0.0,
        velocity_left_mps=0.0,
        velocity_up_mps=0.0,
        attitude_covariance_enu_rad2=(0.01, 0.0, 0.0, 0.0, 0.02, 0.0, 0.0, 0.0, 0.03),
        bgr_payload=b"\x01\x02\x03\x04\x05\x06",
    )


def _depth_record(*, backend: GazeboSimBackend, rgb: bytes, digest: str | None = None) -> bytes:
    header = unpack_header(rgb[:WIRE_HEADER_BYTES])
    metadata = dict(backend.frame_source.depth_contract or {})
    metadata.pop("sensor_id")
    metadata.update(
        {
            "sequence": 0,
            "rgb_sensor_stamp_ns": 1_000_000_000,
            "rgb_record_sha256": digest or header.sha256.hex(),
            "source_stamp_ns": 1_000_000_000,
            "stride_bytes": 8,
        }
    )
    return build_depth_wire_record(
        metadata=metadata,
        payload=struct.pack("<ff", 5.0, 5.0),
    )


def _backend(*, popen_factory=None) -> GazeboSimBackend:
    kwargs = {
        "scenario_input": _scenario_with_depth(),
        "camera_calibration": _calibration(),
        "docker_executable": Path(sys.executable).resolve(),
        "container_name": "owned-gazebo-sim",
        "frame_count": 1,
        "airframe_profile_hash": "a" * 64,
        "container_image_sha256": "b" * 64,
        "stream_script_sha256": "c" * 64,
        "model_assets_sha256": "d" * 64,
        "expected_width": 2,
        "expected_height": 1,
    }
    if popen_factory is not None:
        kwargs["popen_factory"] = popen_factory
    return GazeboSimBackend(**kwargs)


def test_depth_wire_binds_rgb_digest_and_rejects_tampering() -> None:
    backend = _backend()
    rgb = _rgb_record()
    record = _depth_record(backend=backend, rgb=rgb)
    frame = decode_depth_wire_blob(
        record,
        expected_sequence=0,
        expected_rgb_sensor_stamp_ns=1_000_000_000,
        expected_rgb_record_sha256=unpack_header(rgb[:WIRE_HEADER_BYTES]).sha256.hex(),
        expected_source_topic=backend.frame_source.depth_topic,
        expected_calibration_id="gazebo-depth-v1",
        expected_calibration_sha256=backend.frame_source.depth_calibration_sha256,
    )
    assert frame.encoding == "FLOAT32_M"
    assert frame.metadata["registration_mode"] == "registered_to_rgb"
    with pytest.raises(GazeboDepthWireError, match="RGB record digest"):
        decode_depth_wire_blob(
            record,
            expected_sequence=0,
            expected_rgb_sensor_stamp_ns=1_000_000_000,
            expected_rgb_record_sha256="f" * 64,
            expected_source_topic=backend.frame_source.depth_topic,
            expected_calibration_id="gazebo-depth-v1",
            expected_calibration_sha256=backend.frame_source.depth_calibration_sha256,
        )


def test_gazebo_frame_source_joins_depth_and_samples_exact_frame() -> None:
    backend = _backend()
    rgb = _rgb_record()
    process = _FakeProcess(rgb + _depth_record(backend=backend, rgb=rgb))
    backend = _backend(popen_factory=lambda *_args, **_kwargs: process)
    # Rebuild the companion against the second backend's identical binding.
    rgb = _rgb_record()
    process.stdout = io.BytesIO(rgb + _depth_record(backend=backend, rgb=rgb))
    backend.frame_source.open()
    captured = backend.frame_source.read()
    measurement = backend.frame_source.depth_measurement_for(
        target_id="target-1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        frame_id=captured.frame_id,
        minimum_valid_pixels=1,
    )
    assert measurement is not None
    assert measurement.source.value == "gazebo_depth"
    assert measurement.target_id == "target-1"
    assert measurement.slant_range_m == pytest.approx(5.5901699437)
    assert backend.frame_source.depth_measurement_for(
        target_id="target-1",
        bbox=(0.0, 0.0, 1.0, 1.0),
        frame_id="gazebo-sim-999999",
        minimum_valid_pixels=1,
    ) is None
    backend.frame_source.close()
    assert backend.frame_source.require_complete().depth_companion_count == 1


def test_depth_companion_is_required_when_contract_is_enabled() -> None:
    backend = _backend(popen_factory=lambda *_args, **_kwargs: _FakeProcess(_rgb_record()))
    backend.frame_source.open()
    with pytest.raises(RuntimeError, match="depth companion|closed its framed stream"):
        backend.frame_source.read()
    backend.frame_source.close()

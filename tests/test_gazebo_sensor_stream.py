from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import sys
import threading
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from multidetect.gazebo_fault_verification import (
    SensorFaultDiagnosticError,
    verify_sensor_fault_diagnostic,
)
from scripts.gazebo_depth_wire import read_depth_wire_frame
from scripts.gazebo_sensor_stream import (
    FLAG_CAMERA_INFO,
    FLAG_NAVSAT,
    FLAG_ODOMETRY,
    GAZEBO_PIXEL_FORMAT_L_INT8,
    GAZEBO_PIXEL_FORMAT_RGB_INT8,
    GAZEBO_PIXEL_FORMAT_RGBA_INT8,
    GAZEBO_SOURCE_ROW_ORDER,
    GAZEBO_VERTICAL_FLIP_APPLIED,
    MAX_DIMENSION,
    MAX_SOURCE_SKEW_NS,
    MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS,
    PIXEL_FORMAT_BGR24,
    PIXEL_FORMAT_MASK,
    PIXEL_FORMAT_SHIFT,
    WIRE_HEADER,
    WIRE_HEADER_BYTES,
    WIRE_MAGIC,
    WIRE_ROW_ORDER,
    WIRE_VERSION,
    CausalSourceBuffer,
    WireOrderValidator,
    _normalize_depth_float32,
    _odometry_from_message,
    _frame_gimbal_state,
    _frame_joint_state,
    _joint_sample_from_message,
    _parse_depth_contract,
    _runtime_pixel_format_name,
    _shutdown_transport_node,
    build_wire_frame,
    decode_wire_blob,
    normalize_bgr24,
    pack_header,
    parse_wire_frame,
    read_wire_frame,
    unpack_header,
    validate_camera_info_semantics,
)
from scripts.gazebo_sensor_stream import main as sensor_stream_main

TEST_ATTITUDE_COVARIANCE = (
    0.0001,
    0.0,
    0.0,
    0.0,
    0.0004,
    0.0,
    0.0,
    0.0,
    0.0009,
)


def test_gimbal_state_gate_rejects_missing_and_stale_samples() -> None:
    assert _frame_gimbal_state(None, 1_000_000_000) is None
    sample = {
        "stamp_ns": 800_000_000,
        "joints": {
            "cgo3_camera_joint": {"position_rad": 0.1, "velocity_rad_s": 0.2},
            "cgo3_vertical_arm_joint": {"position_rad": 0.3, "velocity_rad_s": 0.4},
            "cgo3_horizontal_arm_joint": {"position_rad": 0.5, "velocity_rad_s": 0.6},
        },
    }
    assert _frame_gimbal_state(sample, 1_000_000_001) is None
    assert _frame_gimbal_state({**sample, "joints": {}}, 1_000_000_000) is None


def test_gimbal_state_gate_records_fresh_joint_values_and_separate_timestamps() -> None:
    sample = _joint_sample_from_message(
        _gazebo_message(
            stamp_ns=950_000_000,
            joint=[
                SimpleNamespace(name=name, axis1=SimpleNamespace(position=pos, velocity=vel))
                for name, pos, vel in (
                    ("cgo3_camera_joint", 0.1, 0.2),
                    ("cgo3_vertical_arm_joint", 0.3, 0.4),
                    ("cgo3_horizontal_arm_joint", 0.5, 0.6),
                )
            ],
        )
    )
    state = _frame_gimbal_state(sample, 1_000_000_000)
    assert state is not None
    assert state["timestamp_ns"] == 1_000_000_000
    assert state["source_timestamp_ns"] == 950_000_000
    assert state["source_skew_ns"] == 50_000_000
    assert state["position_rad"] == {"pitch": 0.1, "yaw": 0.3, "roll": 0.5}
    assert state["velocity_rad_s"] == {"pitch": 0.2, "yaw": 0.4, "roll": 0.6}


def test_joint_state_gate_records_x500_rotors_with_the_rgb_timestamp() -> None:
    sample = {
        "stamp_ns": 950_000_000,
        "joints": {
            **{
                f"rotor_{index}_joint": {
                    "position_rad": float(index) + 0.1,
                    "velocity_rad_s": (-1.0 if index % 2 else 1.0) * (10.0 + index),
                }
                for index in range(4)
            },
            "unrelated_joint": {"position_rad": 99.0, "velocity_rad_s": 0.0},
        },
    }
    state = _frame_joint_state(sample, 1_000_000_000)
    assert state is not None
    assert state["timestamp_ns"] == 1_000_000_000
    assert state["source_timestamp_ns"] == 950_000_000
    propeller = state["propeller_joint_state"]
    assert propeller["joint_names"] == [f"rotor_{index}_joint" for index in range(4)]
    assert propeller["angles_rad"] == pytest.approx([0.1, 1.1, 2.1, 3.1])
    assert propeller["spin_rad_s"] == pytest.approx([10.0, -11.0, 12.0, -13.0])
    assert propeller["velocity_slowdown"] == 10


def test_gimbal_joint_sample_contains_both_camera_axes_and_all_rotors() -> None:
    joints = {
        **{
            f"rotor_{index}_joint": {
                "position_rad": float(index),
                "velocity_rad_s": float(index + 1),
            }
            for index in range(4)
        },
        "cgo3_camera_joint": {"position_rad": 0.1, "velocity_rad_s": 0.2},
        "cgo3_vertical_arm_joint": {"position_rad": 0.3, "velocity_rad_s": 0.4},
        "cgo3_horizontal_arm_joint": {"position_rad": 0.5, "velocity_rad_s": 0.6},
    }
    state = _frame_joint_state(
        {"stamp_ns": 900, "joints": joints}, 1_000, require_complete=True
    )

    assert state is not None
    assert state["gimbal_joint_state"]["position_rad"] == {
        "pitch": 0.1,
        "yaw": 0.3,
        "roll": 0.5,
    }
    assert state["propeller_joint_state"]["angles_rad"] == [0.0, 1.0, 2.0, 3.0]
    assert state["propeller_joint_state"]["spin_rad_s"] == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("missing", ["rotors", "gimbal"])
def test_gimbal_frame_contract_rejects_partial_actuator_state(missing: str) -> None:
    joints = {
        **{
            f"rotor_{index}_joint": {"position_rad": 0.0, "velocity_rad_s": 1.0}
            for index in range(4)
        },
        "cgo3_camera_joint": {"position_rad": 0.1, "velocity_rad_s": 0.0},
        "cgo3_vertical_arm_joint": {"position_rad": 0.2, "velocity_rad_s": 0.0},
        "cgo3_horizontal_arm_joint": {"position_rad": 0.3, "velocity_rad_s": 0.0},
    }
    if missing == "rotors":
        del joints["rotor_2_joint"]
    else:
        del joints["cgo3_vertical_arm_joint"]

    state = _frame_joint_state(
        {"stamp_ns": 900, "joints": joints}, 1_000, require_complete=True
    )
    assert state is None


def test_rotor_state_is_optional_for_legacy_joint_samples() -> None:
    sample = {
        "stamp_ns": 950_000_000,
        "joints": {"other_joint": {"position_rad": 0.0, "velocity_rad_s": 0.0}},
    }
    assert _frame_joint_state(sample, 1_000_000_000) is None
    assert _frame_joint_state(sample, 1_100_000_001) is None


def _frame(
    *,
    sequence: int = 0,
    stamp_ns: int = 10,
    odometry_stamp_ns: int = 4,
    payload: bytes | None = None,
    orientation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    attitude_covariance: tuple[float, ...] = TEST_ATTITUDE_COVARIANCE,
) -> bytes:
    bgr = payload if payload is not None else bytes((3, 2, 1, 6, 5, 4))
    return build_wire_frame(
        sequence=sequence,
        sensor_stamp_ns=stamp_ns,
        width=2,
        height=1,
        camera_info_stamp_ns=2,
        navsat_stamp_ns=3,
        odometry_stamp_ns=odometry_stamp_ns,
        latitude_deg=47.397742,
        longitude_deg=8.545594,
        altitude_m=488.2,
        position_east_m=1.0,
        position_north_m=2.0,
        position_up_m=3.0,
        orientation_x=orientation[0],
        orientation_y=orientation[1],
        orientation_z=orientation[2],
        orientation_w=orientation[3],
        velocity_forward_mps=4.0,
        velocity_left_mps=5.0,
        velocity_up_mps=6.0,
        attitude_covariance_enu_rad2=attitude_covariance,
        bgr_payload=bgr,
    )


class _BinaryStdoutCapture:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


def _gazebo_message(*, stamp_ns: int, **fields: object) -> SimpleNamespace:
    pose_with_covariance = fields.get("pose_with_covariance")
    if pose_with_covariance is not None and not hasattr(pose_with_covariance, "covariance"):
        pose_with_covariance.covariance = SimpleNamespace(
            data=[
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0001,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0004,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0009,
            ]
        )
    sec, nsec = divmod(stamp_ns, 1_000_000_000)
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nsec=nsec)),
        **fields,
    )


def _camera_info_message(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "width": 1280,
        "height": 960,
        "intrinsics": SimpleNamespace(
            k=[539.93636802453, 0.0, 640.0, 0.0, 539.93636802453, 480.0, 0.0, 0.0, 1.0]
        ),
        "projection": SimpleNamespace(
            p=[
                539.93636802453,
                0.0,
                640.0,
                0.0,
                0.0,
                539.93636802453,
                480.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
            ]
        ),
        "rectification_matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "distortion": SimpleNamespace(model=0, k=[0.0] * 5),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _validate_camera_info(message: SimpleNamespace) -> dict[str, object]:
    return validate_camera_info_semantics(
        message,
        expected_width=1280,
        expected_height=960,
        expected_fx_px=539.93636802453,
        expected_fy_px=539.93636802453,
        expected_cx_px=640.0,
        expected_cy_px=480.0,
        expected_distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
    )


def test_camera_info_semantics_match_repository_pinhole_calibration() -> None:
    result = _validate_camera_info(_camera_info_message())
    assert result["status"] == "validated_k_p_rectification_plumb_bob_distortion"
    assert result["distortion_model"] == "PLUMB_BOB"


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"width": 640}, "geometry"),
        ({"intrinsics": SimpleNamespace(k=[1.0] * 8)}, r"K\[9\]"),
        ({"projection": SimpleNamespace(p=[1.0] * 11)}, r"P\[12\]"),
        ({"rectification_matrix": [1.0] * 8}, r"R\[9\]"),
        ({"distortion": SimpleNamespace(model=1, k=[0.0] * 5)}, "PLUMB_BOB"),
        ({"distortion": SimpleNamespace(model=0, k=[0.0] * 4)}, r"D\[5\]"),
        (
            {
                "intrinsics": SimpleNamespace(
                    k=[540.0, 0.0, 640.0, 0.0, 539.93636802453, 480.0, 0.0, 0.0, 1.0]
                )
            },
            "CameraInfo K",
        ),
        (
            {
                "projection": SimpleNamespace(
                    p=[
                        539.93636802453,
                        0.0,
                        640.0,
                        1.0,
                        0.0,
                        539.93636802453,
                        480.0,
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                        0.0,
                    ]
                )
            },
            "CameraInfo P",
        ),
        ({"rectification_matrix": [1.0, 0.0, 0.0, 0.0, 0.9, 0.0, 0.0, 0.0, 1.0]}, "CameraInfo R"),
        ({"distortion": SimpleNamespace(model=0, k=[0.1, 0.0, 0.0, 0.0, 0.0])}, "CameraInfo D"),
    ),
)
def test_camera_info_semantics_fail_closed(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_camera_info(_camera_info_message(**change))


def _run_fake_gazebo_helper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    odometry_behavior: str,
    topics: tuple[str, str, str, str] | None = None,
    validate_camera_info: bool = False,
    fault_source: str | None = None,
    fault_start_image: int = 0,
    fault_duration_images: int = 0,
    frame_count: int = 1,
) -> tuple[int, bytes, list[dict[str, object]]]:
    class CameraInfo:
        pass

    class Image:
        pass

    class NavSat:
        pass

    class OdometryWithCovariance:
        pass

    class FakeNode:
        def __init__(self) -> None:
            self.callbacks: dict[str, object] = {}
            self.unsubscribed_topics: list[str] = []
            self.background_threads: list[threading.Thread] = []

        def subscribe(self, message_type: type[object], _topic: str, callback: object) -> bool:
            name = message_type.__name__
            self.callbacks[name] = callback
            if name == "CameraInfo":
                callback(  # type: ignore[operator]
                    _gazebo_message(
                        stamp_ns=(
                            3_000_000_001
                            if odometry_behavior == "future_sources"
                            else 1_000_000_001
                        ),
                        width=2,
                        height=1,
                        intrinsics=SimpleNamespace(k=[1.0, 0.0, 1.0, 0.0, 1.0, 0.5, 0.0, 0.0, 1.0]),
                        projection=SimpleNamespace(
                            p=[1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.5, 0.0, 0.0, 0.0, 1.0, 0.0]
                        ),
                        rectification_matrix=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                        distortion=SimpleNamespace(model=0, k=[0.0] * 5),
                    )
                )
                if odometry_behavior == "causal_history":
                    callback(  # type: ignore[operator]
                        _gazebo_message(stamp_ns=3_000_000_001, width=3, height=1)
                    )
            elif name == "NavSat":
                callback(  # type: ignore[operator]
                    _gazebo_message(
                        stamp_ns=(
                            3_000_000_002
                            if odometry_behavior == "future_sources"
                            else 1_000_000_002
                        ),
                        latitude_deg=47.397742,
                        longitude_deg=8.545594,
                        altitude=488.2,
                    )
                )
                if odometry_behavior == "causal_history":
                    callback(  # type: ignore[operator]
                        _gazebo_message(
                            stamp_ns=3_000_000_002,
                            latitude_deg=48.0,
                            longitude_deg=9.0,
                            altitude=999.0,
                        )
                    )
                elif odometry_behavior == "duplicate_navsat":
                    callback(  # type: ignore[operator]
                        _gazebo_message(
                            stamp_ns=1_000_000_002,
                            latitude_deg=47.397742,
                            longitude_deg=8.545594,
                            altitude=488.2,
                        )
                    )
                elif odometry_behavior == "out_of_order_navsat":
                    callback(  # type: ignore[operator]
                        _gazebo_message(
                            stamp_ns=1_000_000_001,
                            latitude_deg=47.397742,
                            longitude_deg=8.545594,
                            altitude=488.2,
                        )
                    )
                if odometry_behavior in {"missing", "subscribe_failure"}:
                    self._emit_image()
            elif name == "OdometryWithCovariance":
                if odometry_behavior == "subscribe_failure":
                    return False
                if odometry_behavior == "zero":
                    callback(_gazebo_message(stamp_ns=0))  # type: ignore[operator]
                    self._emit_image()
                elif odometry_behavior == "invalid":
                    callback(  # type: ignore[operator]
                        SimpleNamespace(
                            header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nsec=1_000_000_000))
                        )
                    )
                    self._emit_image()
                elif odometry_behavior == "startup_velocity_then_valid":
                    for stamp_ns, velocity_up_mps in (
                        (1_000_000_000, 3_000.0),
                        (1_000_000_003, 6.0),
                    ):
                        callback(  # type: ignore[operator]
                            _gazebo_message(
                                stamp_ns=stamp_ns,
                                pose_with_covariance=SimpleNamespace(
                                    pose=SimpleNamespace(
                                        position=SimpleNamespace(x=1.0, y=2.0, z=12.0),
                                        orientation=SimpleNamespace(
                                            x=0.0,
                                            y=0.0,
                                            z=0.0,
                                            w=1.0,
                                        ),
                                    )
                                ),
                                twist_with_covariance=SimpleNamespace(
                                    twist=SimpleNamespace(
                                        linear=SimpleNamespace(
                                            x=4.0,
                                            y=5.0,
                                            z=velocity_up_mps,
                                        )
                                    )
                                ),
                            )
                        )
                    self._emit_image()
                elif odometry_behavior == "startup_velocity_exhausted":
                    for index in range(MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS + 1):
                        callback(  # type: ignore[operator]
                            _gazebo_message(
                                stamp_ns=1_000_000_000 + index,
                                pose_with_covariance=SimpleNamespace(
                                    pose=SimpleNamespace(
                                        position=SimpleNamespace(x=1.0, y=2.0, z=20.0),
                                        orientation=SimpleNamespace(
                                            x=0.0,
                                            y=0.0,
                                            z=0.0,
                                            w=1.0,
                                        ),
                                    )
                                ),
                                twist_with_covariance=SimpleNamespace(
                                    twist=SimpleNamespace(
                                        linear=SimpleNamespace(x=4.0, y=5.0, z=3_000.0)
                                    )
                                ),
                            )
                        )
                    self._emit_image()
                elif odometry_behavior in {
                    "valid",
                    "causal_history",
                    "future_sources",
                    "stale_navsat_then_fresh",
                    "fault_recovery_navsat",
                    "long_fault_image",
                }:
                    callback(  # type: ignore[operator]
                        _gazebo_message(
                            stamp_ns=(
                                3_000_000_003
                                if odometry_behavior == "future_sources"
                                else 1_000_000_003
                            ),
                            pose_with_covariance=SimpleNamespace(
                                pose=SimpleNamespace(
                                    position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
                                    orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                                )
                            ),
                            twist_with_covariance=SimpleNamespace(
                                twist=SimpleNamespace(linear=SimpleNamespace(x=4.0, y=5.0, z=6.0))
                            ),
                        )
                    )
                    if odometry_behavior == "causal_history":
                        callback(  # type: ignore[operator]
                            _gazebo_message(
                                stamp_ns=3_000_000_003,
                                pose_with_covariance=SimpleNamespace(
                                    pose=SimpleNamespace(
                                        position=SimpleNamespace(x=101.0, y=102.0, z=103.0),
                                        orientation=SimpleNamespace(
                                            x=0.0,
                                            y=0.0,
                                            z=0.0,
                                            w=1.0,
                                        ),
                                    )
                                ),
                                twist_with_covariance=SimpleNamespace(
                                    twist=SimpleNamespace(
                                        linear=SimpleNamespace(x=104.0, y=105.0, z=106.0)
                                    )
                                ),
                            )
                        )
                    if odometry_behavior == "stale_navsat_then_fresh":
                        self._emit_image(stamp_ns=3_000_000_000)
                        self.callbacks["NavSat"](  # type: ignore[operator]
                            _gazebo_message(
                                stamp_ns=3_050_000_002,
                                latitude_deg=47.397743,
                                longitude_deg=8.545595,
                                altitude=488.3,
                            )
                        )
                        callback(  # type: ignore[operator]
                            _gazebo_message(
                                stamp_ns=3_050_000_003,
                                pose_with_covariance=SimpleNamespace(
                                    pose=SimpleNamespace(
                                        position=SimpleNamespace(x=2.0, y=3.0, z=4.0),
                                        orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                                    )
                                ),
                                twist_with_covariance=SimpleNamespace(
                                    twist=SimpleNamespace(
                                        linear=SimpleNamespace(x=5.0, y=6.0, z=7.0)
                                    )
                                ),
                            )
                        )
                        self._emit_image(stamp_ns=3_100_000_000)
                    elif odometry_behavior == "fault_recovery_navsat":
                        # The default case injects on the first image.  The
                        # delayed case deliberately emits one normal frame
                        # before the fault to cover the pre-window timeline
                        # boundary, then proves cache invalidation and fresh
                        # recovery on a later frame.
                        if fault_start_image == 1:
                            self._emit_image(stamp_ns=2_000_000_000)
                            self.callbacks["NavSat"](
                                _gazebo_message(
                                    stamp_ns=2_100_000_002,
                                    latitude_deg=47.397743,
                                    longitude_deg=8.545595,
                                    altitude=488.3,
                                )
                            )
                            self._emit_image(stamp_ns=3_000_000_000)
                            self.callbacks["NavSat"](
                                _gazebo_message(
                                    stamp_ns=3_100_000_002,
                                    latitude_deg=47.397744,
                                    longitude_deg=8.545596,
                                    altitude=488.4,
                                )
                            )
                            callback(
                                _gazebo_message(
                                    stamp_ns=2_900_000_003,
                                    pose_with_covariance=SimpleNamespace(
                                        pose=SimpleNamespace(
                                            position=SimpleNamespace(x=2.0, y=3.0, z=4.0),
                                            orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                                        )
                                    ),
                                    twist_with_covariance=SimpleNamespace(
                                        twist=SimpleNamespace(
                                            linear=SimpleNamespace(x=5.0, y=6.0, z=7.0)
                                        )
                                    ),
                                )
                            )
                            self._emit_image(stamp_ns=4_000_000_000)
                        else:
                            self._emit_image(stamp_ns=2_000_000_000)
                            self.callbacks["NavSat"](
                                _gazebo_message(
                                    stamp_ns=2_100_000_002,
                                    latitude_deg=47.397743,
                                    longitude_deg=8.545595,
                                    altitude=488.3,
                                )
                            )
                            self._emit_image(stamp_ns=3_000_000_000)
                            self.callbacks["NavSat"](
                                _gazebo_message(
                                    stamp_ns=3_100_000_002,
                                    latitude_deg=47.397744,
                                    longitude_deg=8.545596,
                                    altitude=488.4,
                                )
                            )
                            callback(
                                _gazebo_message(
                                    stamp_ns=2_900_000_003,
                                    pose_with_covariance=SimpleNamespace(
                                        pose=SimpleNamespace(
                                            position=SimpleNamespace(x=2.0, y=3.0, z=4.0),
                                            orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                                        )
                                    ),
                                    twist_with_covariance=SimpleNamespace(
                                        twist=SimpleNamespace(
                                            linear=SimpleNamespace(x=5.0, y=6.0, z=7.0)
                                        )
                                    ),
                                )
                            )
                            self._emit_image(stamp_ns=4_000_000_000)
                            self.callbacks["NavSat"](
                                _gazebo_message(
                                    stamp_ns=4_100_000_002,
                                    latitude_deg=47.397745,
                                    longitude_deg=8.545597,
                                    altitude=488.5,
                                )
                            )
                            callback(
                                _gazebo_message(
                                    stamp_ns=4_900_000_003,
                                    pose_with_covariance=SimpleNamespace(
                                        pose=SimpleNamespace(
                                            position=SimpleNamespace(x=3.0, y=4.0, z=5.0),
                                            orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                                        )
                                    ),
                                    twist_with_covariance=SimpleNamespace(
                                        twist=SimpleNamespace(
                                            linear=SimpleNamespace(x=6.0, y=7.0, z=8.0)
                                        )
                                    ),
                                )
                            )
                            self._emit_image(stamp_ns=5_000_000_000)
                    elif odometry_behavior == "long_fault_image":
                        total_images = frame_count + fault_duration_images

                        def emit_long_fault_images() -> None:
                            for index in range(total_images):
                                self._emit_image(
                                    stamp_ns=1_100_000_000 + (index + 1) * 1_000_000
                                )

                        worker = threading.Thread(
                            target=emit_long_fault_images,
                            name="fake-gazebo-long-fault-images",
                        )
                        self.background_threads.append(worker)
                        worker.start()
                    else:
                        self._emit_image()
            return True

        def unsubscribe(self, topic: str) -> bool:
            self.unsubscribed_topics.append(topic)
            for worker in self.background_threads:
                worker.join(timeout=1.0)
            return True

        def _emit_image(self, *, stamp_ns: int = 2_000_000_000) -> None:
            callback = self.callbacks["Image"]
            callback(  # type: ignore[operator]
                _gazebo_message(
                    stamp_ns=stamp_ns,
                    width=2,
                    height=1,
                    step=6,
                    pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
                    data=bytes((1, 2, 3, 4, 5, 6)),
                )
            )

    modules: dict[str, ModuleType] = {}
    for name in (
        "gz",
        "gz.msgs10",
        "gz.msgs10.camera_info_pb2",
        "gz.msgs10.image_pb2",
        "gz.msgs10.navsat_pb2",
        "gz.msgs10.odometry_with_covariance_pb2",
        "gz.transport13",
    ):
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["gz.msgs10.camera_info_pb2"].CameraInfo = CameraInfo  # type: ignore[attr-defined]
    modules["gz.msgs10.image_pb2"].Image = Image  # type: ignore[attr-defined]
    modules["gz.msgs10.navsat_pb2"].NavSat = NavSat  # type: ignore[attr-defined]
    modules[
        "gz.msgs10.odometry_with_covariance_pb2"
    ].OdometryWithCovariance = OdometryWithCovariance  # type: ignore[attr-defined]
    modules["gz.transport13"].Node = FakeNode  # type: ignore[attr-defined]

    stdout = _BinaryStdoutCapture()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    argv = [
        "gazebo_sensor_stream.py",
        "--frames",
        str(frame_count),
        "--timeout-seconds",
        "0.5",
    ]
    if fault_source is not None:
        argv.extend(
            (
                "--fault-source",
                fault_source,
                "--fault-start-image",
                str(fault_start_image),
                "--fault-duration-images",
                str(fault_duration_images),
            )
        )
    if validate_camera_info:
        argv.extend(
            (
                "--expected-camera-calibration-json",
                json.dumps(
                    {
                        "width_px": 2,
                        "height_px": 1,
                        "fx_px": 1.0,
                        "fy_px": 1.0,
                        "cx_px": 1.0,
                        "cy_px": 0.5,
                        "distortion": [0.0] * 5,
                    }
                ),
            )
        )
    if topics is not None:
        argv.extend(
            (
                "--image-topic",
                topics[0],
                "--camera-info-topic",
                topics[1],
                "--navsat-topic",
                topics[2],
                "--odometry-topic",
                topics[3],
            )
        )
    monkeypatch.setattr(sys, "argv", argv)
    exit_code = sensor_stream_main()
    diagnostics = [json.loads(line) for line in stderr.getvalue().splitlines() if line]
    return exit_code, stdout.buffer.getvalue(), diagnostics


def test_helper_enforces_configured_camera_info_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A complete helper run may claim semantic validation only after the live
    # CameraInfo callback matches the host-bound repository calibration.
    exit_code, _blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="valid",
        validate_camera_info=True,
    )
    assert exit_code == 0
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["camera_info_intrinsics_semantic_validation"] == (
        "validated_k_p_rectification_plumb_bob_distortion"
    )


def _depth_contract_json() -> str:
    return json.dumps(
        {
            "sensor_id": "downward-depth",
            "source_topic": (
                "/world/test_world/model/test_model/link/depth_camera_link/"
                "sensor/depth_camera/depth_image"
            ),
            "calibration_id": "rgb-depth-v1",
            "calibration_sha256": "a" * 64,
            "registration_mode": "registered_to_rgb",
            "coordinate_space": "rgb_pixel_aligned",
            "encoding": "FLOAT32_M",
            "distance_semantics": "optical_axis_z_m",
            "width": 2,
            "height": 1,
            "minimum_depth_m": 0.2,
            "maximum_depth_m": 100.0,
            "fx_px": 1.0,
            "fy_px": 1.0,
            "cx_px": 1.0,
            "cy_px": 0.5,
        }
    )


def test_depth_contract_is_exactly_scoped_and_strict() -> None:
    contract = _parse_depth_contract(
        _depth_contract_json(), world="test_world", model="test_model"
    )
    assert contract is not None
    assert contract["source_topic"].endswith("/depth_camera/depth_image")
    assert contract["depth_camera_info_topic"].endswith("/depth_camera/camera_info")

    with pytest.raises(ValueError, match="exact scoped depth topic"):
        _parse_depth_contract(
            _depth_contract_json().replace("test_world", "other_world"),
            world="test_world",
            model="test_model",
        )
    duplicate = _depth_contract_json().replace(
        '"sensor_id": "downward-depth",',
        '"sensor_id": "downward-depth", "sensor_id": "duplicate",',
    )
    with pytest.raises(ValueError, match="duplicate field"):
        _parse_depth_contract(duplicate, world="test_world", model="test_model")


def test_depth_normalizer_requires_runtime_r_float32_descriptor() -> None:
    enum_value = SimpleNamespace(name="R_FLOAT32")
    descriptor = SimpleNamespace(
        fields_by_name={
            "pixel_format_type": SimpleNamespace(
                enum_type=SimpleNamespace(values_by_number={6: enum_value})
            )
        }
    )
    message = _gazebo_message(
        stamp_ns=2_000_000_000,
        width=2,
        height=1,
        step=8,
        pixel_format_type=6,
        data=b"\x00\x00\x80?\x00\x00@@",
    )
    message.DESCRIPTOR = descriptor
    contract = _parse_depth_contract(
        _depth_contract_json(), world="test_world", model="test_model"
    )
    assert contract is not None
    assert _runtime_pixel_format_name(message) == "R_FLOAT32"
    assert _normalize_depth_float32(message, contract=contract) == message.data
    message.DESCRIPTOR = SimpleNamespace(
        fields_by_name={
            "pixel_format_type": SimpleNamespace(
                enum_type=SimpleNamespace(values_by_number={6: SimpleNamespace(name="RGB_INT8")})
            )
        }
    )
    with pytest.raises(ValueError, match="must be R_FLOAT32"):
        _normalize_depth_float32(message, contract=contract)


@pytest.mark.parametrize(
    ("depth_stamp_ns", "expected_exit", "expected_counter"),
    (
        (1_999_999_999, 0, "depth"),
        (1_899_999_999, 2, "depth_stale_for_rgb"),
    ),
)
def test_enabled_depth_callback_emits_rgb_then_immediate_depth_companion(
    monkeypatch: pytest.MonkeyPatch,
    depth_stamp_ns: int,
    expected_exit: int,
    expected_counter: str,
) -> None:
    """The live callback seam must bind a causal depth frame to each RGB blob."""

    class CameraInfo:
        pass

    class Image:
        pass

    class NavSat:
        pass

    class OdometryWithCovariance:
        pass

    enum_descriptor = SimpleNamespace(
        fields_by_name={
            "pixel_format_type": SimpleNamespace(
                enum_type=SimpleNamespace(values_by_number={6: SimpleNamespace(name="R_FLOAT32")})
            )
        }
    )

    def depth_image(stamp_ns: int) -> SimpleNamespace:
        message = _gazebo_message(
            stamp_ns=stamp_ns,
            width=2,
            height=1,
            step=8,
            pixel_format_type=6,
            data=struct.pack("<2f", 2.0, 3.0),
        )
        message.DESCRIPTOR = enum_descriptor
        return message

    def camera_info(stamp_ns: int) -> SimpleNamespace:
        return _gazebo_message(
            stamp_ns=stamp_ns,
            width=2,
            height=1,
            intrinsics=SimpleNamespace(k=[1.0, 0.0, 1.0, 0.0, 1.0, 0.5, 0.0, 0.0, 1.0]),
            projection=SimpleNamespace(
                p=[1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.5, 0.0, 0.0, 0.0, 1.0, 0.0]
            ),
            rectification_matrix=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            distortion=SimpleNamespace(model=0, k=[0.0] * 5),
        )

    class FakeNode:
        def __init__(self) -> None:
            self.image_callback: object | None = None

        def subscribe(self, message_type: type[object], topic: str, callback: object) -> bool:
            name = message_type.__name__
            if topic.endswith("/depth_camera/camera_info"):
                callback(camera_info(1_000_000_000))  # type: ignore[operator]
            elif topic.endswith("/depth_camera/depth_image"):
                callback(depth_image(depth_stamp_ns))  # type: ignore[operator]
            elif name == "Image":
                self.image_callback = callback
            elif name == "CameraInfo":
                callback(camera_info(1_000_000_001))  # type: ignore[operator]
            elif name == "NavSat":
                callback(
                    _gazebo_message(
                        stamp_ns=1_000_000_002,
                        latitude_deg=47.397742,
                        longitude_deg=8.545594,
                        altitude=488.2,
                    )
                )  # type: ignore[operator]
            elif name == "OdometryWithCovariance":
                callback(
                    _gazebo_message(
                        stamp_ns=1_000_000_003,
                        pose_with_covariance=SimpleNamespace(
                            pose=SimpleNamespace(
                                position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
                                orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
                            )
                        ),
                        twist_with_covariance=SimpleNamespace(
                            twist=SimpleNamespace(linear=SimpleNamespace(x=4.0, y=5.0, z=6.0))
                        ),
                    )
                )  # type: ignore[operator]
                rgb = _gazebo_message(
                    stamp_ns=2_000_000_000,
                    width=2,
                    height=1,
                    step=6,
                    pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
                    data=bytes((1, 2, 3, 4, 5, 6)),
                )
                # The image callback is the first Image callback registered for
                # the RGB topic; execute it after all causal sources are ready.
                assert self.image_callback is not None
                self.image_callback(rgb)  # type: ignore[operator]
            return True

        def unsubscribe(self, _topic: str) -> bool:
            return True

    modules: dict[str, ModuleType] = {}
    for name in (
        "gz",
        "gz.msgs10",
        "gz.msgs10.camera_info_pb2",
        "gz.msgs10.image_pb2",
        "gz.msgs10.navsat_pb2",
        "gz.msgs10.odometry_with_covariance_pb2",
        "gz.transport13",
    ):
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["gz.msgs10.camera_info_pb2"].CameraInfo = CameraInfo  # type: ignore[attr-defined]
    modules["gz.msgs10.image_pb2"].Image = Image  # type: ignore[attr-defined]
    modules["gz.msgs10.navsat_pb2"].NavSat = NavSat  # type: ignore[attr-defined]
    modules["gz.msgs10.odometry_with_covariance_pb2"].OdometryWithCovariance = (  # type: ignore[attr-defined]
        OdometryWithCovariance
    )
    modules["gz.transport13"].Node = FakeNode  # type: ignore[attr-defined]

    stdout = _BinaryStdoutCapture()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gazebo_sensor_stream.py",
            "--frames",
            "1",
            "--timeout-seconds",
            "0.5",
            "--world",
            "test_world",
            "--model-instance",
            "test_model",
            "--depth-contract-json",
            _depth_contract_json(),
        ],
    )
    exit_code = sensor_stream_main()
    assert exit_code == expected_exit
    blob = stdout.buffer.getvalue()
    if expected_exit == 0:
        rgb_header, _rgb_payload = parse_wire_frame(blob[: WIRE_HEADER_BYTES + 6])
        companion = read_depth_wire_frame(
            io.BytesIO(blob[WIRE_HEADER_BYTES + 6 :]),
            expected_sequence=rgb_header.sequence,
            expected_rgb_sensor_stamp_ns=rgb_header.sensor_stamp_ns,
            expected_rgb_record_sha256=rgb_header.sha256.hex(),
            expected_source_topic=(
                "/world/test_world/model/test_model/link/depth_camera_link/"
                "sensor/depth_camera/depth_image"
            ),
            expected_calibration_id="rgb-depth-v1",
            expected_calibration_sha256="a" * 64,
        )
        assert companion.payload == struct.pack("<2f", 2.0, 3.0)
    diagnostics = [json.loads(line) for line in stderr.getvalue().splitlines() if line]
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["depth_enabled"] is True
    assert finished["message_counts"]["depth"] == 1
    assert finished["message_counts"][expected_counter] >= (1 if expected_exit else 0)
    if expected_exit == 0:
        assert rgb_header.sequence == 0


def test_normalize_rgb_to_packed_bgr24_with_step_padding() -> None:
    # Two RGB rows, with two bytes of padding at the end of each row.
    rgb = bytes(
        (
            1,
            2,
            3,
            4,
            5,
            6,
            0xAA,
            0xBB,
            7,
            8,
            9,
            10,
            11,
            12,
            0xCC,
            0xDD,
        )
    )
    assert normalize_bgr24(
        rgb,
        width=2,
        height=2,
        step=8,
        pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
    ) == bytes((3, 2, 1, 6, 5, 4, 9, 8, 7, 12, 11, 10))


def test_live_gazebo_row_order_contract_preserves_top_to_bottom_transport() -> None:
    assert GAZEBO_SOURCE_ROW_ORDER == "top_to_bottom"
    assert WIRE_ROW_ORDER == "top_to_bottom"
    assert GAZEBO_VERTICAL_FLIP_APPLIED is False


def test_normalize_explicit_vertical_flip_reverses_rows_after_color_conversion() -> None:
    # Keep an explicit transform test for callers that intentionally consume a
    # bottom-to-top fixture; the live Gazebo path does not select this mode.
    rgb = bytes((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12))
    assert normalize_bgr24(
        rgb,
        width=2,
        height=2,
        step=6,
        pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
        flip_vertical=True,
    ) == bytes((9, 8, 7, 12, 11, 10, 3, 2, 1, 6, 5, 4))


def test_normalize_rejects_non_boolean_vertical_flip_mode() -> None:
    with pytest.raises(ValueError, match="flip_vertical must be a boolean"):
        normalize_bgr24(
            bytes((1, 2, 3)),
            width=1,
            height=1,
            step=3,
            pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
            flip_vertical=1,  # type: ignore[arg-type]
        )


def test_normalize_mono_replicates_luma_and_ignores_padding() -> None:
    mono = bytes((10, 20, 0xFF, 30, 40, 0xEE))
    assert normalize_bgr24(
        mono,
        width=2,
        height=2,
        step=3,
        pixel_format_type=GAZEBO_PIXEL_FORMAT_L_INT8,
    ) == bytes((10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40))


def test_normalize_rgba_drops_alpha_and_swaps_red_blue() -> None:
    rgba = bytes((1, 2, 3, 99, 4, 5, 6, 88))
    assert normalize_bgr24(
        rgba,
        width=2,
        height=1,
        step=8,
        pixel_format_type=GAZEBO_PIXEL_FORMAT_RGBA_INT8,
    ) == bytes((3, 2, 1, 6, 5, 4))


def test_fixed_header_round_trip_contains_version_geometry_telemetry_and_digest() -> None:
    packet = _frame()
    assert WIRE_MAGIC == b"MDGZSIM3"
    assert WIRE_VERSION == 3
    assert WIRE_HEADER_BYTES == WIRE_HEADER.size == 276
    header = unpack_header(packet[:WIRE_HEADER_BYTES])
    assert header.sequence == 0
    assert header.sensor_stamp_ns == 10
    assert (header.width, header.height, header.stride, header.payload_bytes) == (2, 1, 6, 6)
    assert header.camera_info_stamp_ns == 2
    assert header.navsat_stamp_ns == 3
    assert header.odometry_stamp_ns == 4
    assert header.latitude_deg == pytest.approx(47.397742)
    assert header.longitude_deg == pytest.approx(8.545594)
    assert header.altitude_m == pytest.approx(488.2)
    assert (header.position_east_m, header.position_north_m, header.position_up_m) == (
        1.0,
        2.0,
        3.0,
    )
    assert (
        header.orientation_x,
        header.orientation_y,
        header.orientation_z,
        header.orientation_w,
    ) == (0.0, 0.0, 0.0, 1.0)
    assert (
        header.velocity_forward_mps,
        header.velocity_left_mps,
        header.velocity_up_mps,
    ) == (4.0, 5.0, 6.0)
    assert header.attitude_covariance_enu_rad2 == pytest.approx(
        (0.0001, 0.0, 0.0, 0.0, 0.0004, 0.0, 0.0, 0.0, 0.0009)
    )
    assert header.flags & (FLAG_CAMERA_INFO | FLAG_NAVSAT | FLAG_ODOMETRY) == 7
    assert (header.flags & PIXEL_FORMAT_MASK) >> PIXEL_FORMAT_SHIFT == PIXEL_FORMAT_BGR24
    digest_input = packet[: WIRE_HEADER_BYTES - 32] + (b"\x00" * 32)
    assert header.sha256 == hashlib.sha256(digest_input + packet[WIRE_HEADER_BYTES:]).digest()
    assert pack_header(header) == packet[:WIRE_HEADER_BYTES]


def _raw_odometry_message(covariance: object) -> SimpleNamespace:
    return SimpleNamespace(
        pose_with_covariance=SimpleNamespace(
            pose=SimpleNamespace(
                position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
                orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            ),
            covariance=SimpleNamespace(data=covariance),
        ),
        twist_with_covariance=SimpleNamespace(
            twist=SimpleNamespace(linear=SimpleNamespace(x=4.0, y=5.0, z=6.0))
        ),
    )


def test_odometry_extracts_angular_block_from_pose_covariance() -> None:
    covariance = [float(index) / 1000.0 for index in range(36)]
    angular = (
        0.010,
        0.002,
        0.003,
        0.002,
        0.020,
        0.004,
        0.003,
        0.004,
        0.030,
    )
    for row in range(3):
        for column in range(3):
            covariance[(row + 3) * 6 + column + 3] = angular[row * 3 + column]

    extracted = _odometry_from_message(_raw_odometry_message(covariance))

    assert extracted[-9:] == pytest.approx(angular)


@pytest.mark.parametrize("covariance", (None, (), (0.0,) * 35, (0.0,) * 37))
def test_odometry_rejects_missing_or_non_6x6_pose_covariance(covariance: object) -> None:
    with pytest.raises(ValueError, match="covariance"):
        _odometry_from_message(_raw_odometry_message(covariance))


@pytest.mark.parametrize(
    ("covariance", "message"),
    (
        ((math.nan,) + (0.0,) * 8, "must be finite"),
        ((1.0, 0.25, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), "symmetric"),
        ((1.0, 2.0, 0.0, 2.0, 1.0, 0.0, 0.0, 0.0, 1.0), "semidefinite"),
    ),
)
def test_wire_builder_rejects_invalid_attitude_covariance(
    covariance: tuple[float, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _frame(attitude_covariance=covariance)


def test_odometry_pose_velocity_is_required_and_exposed() -> None:
    header, _ = parse_wire_frame(_frame())
    assert header.odometry_stamp_ns == 4
    assert header.odometry_present is True
    assert header.flags & FLAG_ODOMETRY
    assert header.camera_info_present is True
    assert header.navsat_present is True


def test_odometry_presence_flag_and_timestamp_are_both_required() -> None:
    header = unpack_header(_frame()[:WIRE_HEADER_BYTES])
    with pytest.raises(ValueError, match="missing required synchronized telemetry"):
        pack_header(replace(header, flags=header.flags & ~FLAG_ODOMETRY))
    with pytest.raises(ValueError, match="timestamps must be positive"):
        pack_header(replace(header, odometry_stamp_ns=0))


@pytest.mark.parametrize(
    ("odometry_behavior", "invalid_count"),
    (
        ("missing", 0),
        ("subscribe_failure", 0),
        ("zero", 1),
        ("invalid", 1),
    ),
)
def test_helper_fails_closed_without_usable_required_odometry(
    monkeypatch: pytest.MonkeyPatch,
    odometry_behavior: str,
    invalid_count: int,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior=odometry_behavior,
    )

    assert exit_code == 2
    assert blob == b""

    if odometry_behavior == "subscribe_failure":
        assert any(
            item["event"] == "gazebo_sensor_stream_subscription_failed" for item in diagnostics
        )
    else:
        started = next(
            item for item in diagnostics if item["event"] == "gazebo_sensor_stream_started"
        )
        finished = next(
            item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
        )
        assert started["odometry_required"] is True
        assert started["full_odometry_pose_velocity_fusion_claimed"] is False
        assert started["full_odometry_pose_velocity_fusion_gate"] == ("pending_synchronized_frames")
        assert finished["gate_passed"] is False
        assert finished["odometry_required"] is True
        assert finished["odometry_timestamp_frame_count"] == 0
        assert finished["full_odometry_pose_velocity_fusion_claimed"] is False
        assert finished["message_counts"]["odometry_invalid"] == invalid_count


def test_helper_emits_full_required_odometry_pose_velocity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="valid",
    )

    assert exit_code == 0
    started = next(item for item in diagnostics if item["event"] == "gazebo_sensor_stream_started")
    assert started["source_row_order"] == GAZEBO_SOURCE_ROW_ORDER
    assert started["wire_row_order"] == WIRE_ROW_ORDER
    assert started["vertical_flip_applied"] is GAZEBO_VERTICAL_FLIP_APPLIED
    assert started["full_odometry_pose_velocity_fusion_claimed"] is False
    [(header, _payload)] = decode_wire_blob(blob, expected_frames=1)
    assert header.odometry_stamp_ns == 1_000_000_003
    assert header.odometry_present is True
    assert (header.position_east_m, header.position_north_m, header.position_up_m) == (
        1.0,
        2.0,
        3.0,
    )
    assert (
        header.velocity_forward_mps,
        header.velocity_left_mps,
        header.velocity_up_mps,
    ) == (4.0, 5.0, 6.0)
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["odometry_present"] is True
    assert finished["odometry_timestamp_frame_count"] == 1
    assert finished["full_odometry_pose_velocity_fusion_claimed"] is True
    assert finished["source_row_order"] == GAZEBO_SOURCE_ROW_ORDER
    assert finished["wire_row_order"] == WIRE_ROW_ORDER
    assert finished["vertical_flip_applied"] is GAZEBO_VERTICAL_FLIP_APPLIED


def test_helper_discards_bounded_initial_spawn_velocity_before_first_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="startup_velocity_then_valid",
    )

    assert exit_code == 0
    [(header, _payload)] = decode_wire_blob(blob, expected_frames=1)
    assert header.position_up_m == pytest.approx(12.0)
    assert header.velocity_up_mps == pytest.approx(6.0)
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["gate_passed"] is True
    assert finished["message_counts"]["odometry_invalid"] == 1
    assert finished["message_counts"]["odometry_startup_velocity_rejected"] == 1
    assert finished["odometry_present"] is True
    assert finished["odometry_timestamp_frame_count"] == 1
    assert finished["full_odometry_pose_velocity_fusion_claimed"] is True
    assert finished["source_row_order"] == GAZEBO_SOURCE_ROW_ORDER
    assert finished["wire_row_order"] == WIRE_ROW_ORDER
    assert finished["vertical_flip_applied"] is GAZEBO_VERTICAL_FLIP_APPLIED


def test_helper_fails_closed_when_startup_velocity_rejection_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="startup_velocity_exhausted",
    )

    assert exit_code == 2
    assert blob == b""
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["gate_passed"] is False
    assert finished["accepted_frames"] == 0
    assert finished["message_counts"]["odometry_invalid"] == (
        MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS + 1
    )
    assert finished["message_counts"]["odometry_startup_velocity_rejected"] == (
        MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS
    )
    assert "velocity exceeds the safety bound" in finished["error"]


def test_helper_uses_all_explicit_topics_without_default_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topics = (
        "/world/bound_world/model/bound_model_0/link/camera_link/sensor/camera/image",
        "/world/bound_world/model/bound_model_0/link/camera_link/sensor/camera/camera_info",
        "/world/bound_world/model/bound_model_0/link/base_link/sensor/navsat_sensor/navsat",
        "/model/bound_model_0/odometry_with_covariance",
    )

    exit_code, _blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="valid",
        topics=topics,
    )

    assert exit_code == 0
    started = next(item for item in diagnostics if item["event"] == "gazebo_sensor_stream_started")
    assert (
        tuple(
            started[name]
            for name in (
                "image_topic",
                "camera_info_topic",
                "navsat_topic",
                "odometry_topic",
            )
        )
        == topics
    )


def test_helper_selects_nearest_causal_samples_instead_of_latest_future_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="causal_history",
    )

    assert exit_code == 0
    [(header, _payload)] = decode_wire_blob(blob, expected_frames=1)
    assert header.sensor_stamp_ns == 2_000_000_000
    assert header.camera_info_stamp_ns == 1_000_000_001
    assert header.navsat_stamp_ns == 1_000_000_002
    assert header.odometry_stamp_ns == 1_000_000_003
    assert header.latitude_deg == pytest.approx(47.397742)
    assert (header.position_east_m, header.position_north_m, header.position_up_m) == (
        1.0,
        2.0,
        3.0,
    )
    started = next(item for item in diagnostics if item["event"] == "gazebo_sensor_stream_started")
    assert started["source_association_policy"] == "nearest_source_at_or_before_image"
    assert started["future_source_samples_allowed"] is False
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["causal_source_association_claimed"] is True


def test_helper_rejects_future_only_source_samples_without_emitting_a_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="future_sources",
    )

    assert exit_code == 2
    assert blob == b""
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["accepted_frames"] == 0
    assert finished["causal_source_association_claimed"] is False
    assert finished["message_counts"]["future_source_rejections"] == 1
    assert "timed out" in finished["error"]


def test_helper_discards_transient_stale_dynamic_binding_then_accepts_fresh_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="stale_navsat_then_fresh",
    )

    assert exit_code == 0
    [(header, _payload)] = decode_wire_blob(blob, expected_frames=1)
    assert header.sensor_stamp_ns == 3_100_000_000
    assert header.navsat_stamp_ns == 3_050_000_002
    assert header.odometry_stamp_ns == 3_050_000_003
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert finished["gate_passed"] is True
    assert finished["message_counts"]["stale_dynamic_source_rejections"] == 1
    assert finished["message_counts"]["stale_navsat_rejections"] == 1
    assert finished["message_counts"]["stale_odometry_rejections"] == 1


def test_helper_fault_window_never_emits_cached_navsat_before_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="fault_recovery_navsat",
        fault_source="navsat",
        fault_start_image=1,
        fault_duration_images=1,
    )

    assert exit_code == 0
    [(header, _payload)] = decode_wire_blob(blob, expected_frames=1)
    assert header.sensor_stamp_ns == 4_000_000_000
    assert header.navsat_stamp_ns == 3_100_000_002
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    fault = finished["fault_injection"]
    assert fault["dynamic_source_policy"] == (
        "reject_during_window_and_until_post_window_fresh_sample"
    )
    assert fault["dropped_counts"]["fault_dropped_navsat"] == 1
    assert fault["window_rejected_frame_count"] == 1
    assert fault["stale_source_rejection_count"] == 1
    assert fault["emitted_during_window_count"] == 0
    assert fault["timeline_overflow"] is False
    assert [event["event"] for event in fault["timeline"]] == [
        "frame_rejected_fault_window",
        "source_dropped",
        "frame_rejected_waiting_for_fresh_source",
        "source_fresh_after_window",
        "wire_emitted",
        "wire_emitted_after_recovery",
    ]
    assert fault["cache_cleared"] is True
    assert fault["source_epoch"] == 1
    assert fault["timeline"][1]["source_epoch"] == 1
    assert fault["timeline"][2]["source_epoch"] == 1
    assert fault["timeline"][3]["source_epoch"] == 1
    assert fault["timeline"][4]["source_epoch"] == 1
    assert fault["timeline"][5]["source_epoch"] == 1
    assert all(
        event["wire_sequence"] is None
        for event in fault["timeline"]
        if event["event"] not in {"wire_emitted", "wire_emitted_after_recovery"}
    )
    assert fault["recovery_observed"] is True


def test_helper_fault_window_preserves_pre_window_wire_before_cache_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="fault_recovery_navsat",
        fault_source="navsat",
        fault_start_image=2,
        fault_duration_images=1,
        frame_count=2,
    )

    assert exit_code == 0
    headers = [header for header, _payload in decode_wire_blob(blob, expected_frames=2)]
    assert [header.sensor_stamp_ns for header in headers] == [2_000_000_000, 5_000_000_000]
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    fault = finished["fault_injection"]
    assert fault["source_epoch"] == 1
    assert [event["event"] for event in fault["timeline"]] == [
        "wire_emitted",
        "frame_rejected_fault_window",
        "source_dropped",
        "frame_rejected_waiting_for_fresh_source",
        "source_fresh_after_window",
        "wire_emitted",
        "wire_emitted_after_recovery",
    ]
    assert fault["timeline"][0]["source_epoch"] == 0
    assert fault["timeline"][5]["source_epoch"] == 1


def test_helper_long_image_fault_preserves_wire_timeline_and_verifies_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_count = 120
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="long_fault_image",
        fault_source="image",
        fault_start_image=1,
        fault_duration_images=1,
        frame_count=frame_count,
    )

    assert exit_code == 0
    headers = [
        header for header, _payload in decode_wire_blob(blob, expected_frames=frame_count)
    ]
    assert [header.sequence for header in headers] == list(range(frame_count))

    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    fault = finished["fault_injection"]
    timeline = fault["timeline"]
    wire_events = [event for event in timeline if event["event"] == "wire_emitted"]
    assert len(wire_events) == frame_count
    assert [event["wire_sequence"] for event in wire_events] == list(range(frame_count))
    recovery_events = [
        event for event in timeline if event["event"] == "wire_emitted_after_recovery"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0]["wire_sequence"] == 0
    assert fault["timeline_overflow"] is False

    stderr_tail = "\n".join(json.dumps(item, sort_keys=True) for item in diagnostics)
    verification = verify_sensor_fault_diagnostic(
        stderr_tail,
        fault_source="image",
        fault_start_image=1,
        fault_duration_images=1,
    )
    assert verification["timeline_overflow"] is False
    assert verification["dropped_source_messages"] == 1
    assert verification["recovery_observed"] is True


def test_helper_long_image_fault_overflow_is_rejected_by_shared_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior="long_fault_image",
        fault_source="image",
        fault_start_image=1,
        fault_duration_images=65,
        frame_count=4,
    )

    assert exit_code == 0
    assert len(decode_wire_blob(blob, expected_frames=4)) == 4
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    fault = finished["fault_injection"]
    assert fault["timeline_overflow"] is True
    stderr_tail = "\n".join(json.dumps(item, sort_keys=True) for item in diagnostics)
    with pytest.raises(SensorFaultDiagnosticError):
        verify_sensor_fault_diagnostic(
            stderr_tail,
            fault_source="image",
            fault_start_image=1,
            fault_duration_images=65,
        )


@pytest.mark.parametrize("behavior", ("duplicate_navsat", "out_of_order_navsat"))
def test_helper_rejects_duplicate_or_out_of_order_source_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
) -> None:
    exit_code, blob, diagnostics = _run_fake_gazebo_helper(
        monkeypatch,
        odometry_behavior=behavior,
    )

    assert exit_code == 2
    assert blob == b""
    finished = next(
        item for item in diagnostics if item["event"] == "gazebo_sensor_stream_finished"
    )
    assert "NavSat timestamps must be strictly increasing" in finished["error"]


def test_causal_source_buffer_rejects_duplicate_and_out_of_order_callbacks() -> None:
    samples = CausalSourceBuffer("NavSat", maximum_samples=4)
    samples.append({"stamp_ns": 100, "value": "old"})
    samples.append({"stamp_ns": 300, "value": "future"})

    assert samples.latest_at_or_before(99) is None
    assert samples.latest_at_or_before(200) == {"stamp_ns": 100, "value": "old"}
    assert samples.latest_at_or_before(300) == {"stamp_ns": 300, "value": "future"}

    with pytest.raises(ValueError, match="strictly increasing"):
        samples.append({"stamp_ns": 300, "value": "duplicate"})
    with pytest.raises(ValueError, match="strictly increasing"):
        samples.append({"stamp_ns": 250, "value": "out-of-order"})
    assert samples.last_stamp_ns == 300


def test_causal_source_buffer_clear_invalidates_history_but_keeps_stamp_order() -> None:
    samples = CausalSourceBuffer("NavSat", maximum_samples=4)
    samples.append({"stamp_ns": 100, "value": "pre-fault"})

    assert samples.epoch == 0
    assert samples.ready is True
    assert samples.clear_samples() == 1
    assert samples.epoch == 1
    assert samples.ready is False
    assert samples.latest_at_or_before(200) is None

    with pytest.raises(ValueError, match="strictly increasing"):
        samples.append({"stamp_ns": 100, "value": "replayed"})

    samples.append({"stamp_ns": 200, "value": "post-fault"})
    assert samples.ready is True
    assert samples.latest_at_or_before(200) == {
        "stamp_ns": 200,
        "value": "post-fault",
    }


def test_transport_shutdown_unsubscribes_topics_and_reports_missing_binding() -> None:
    class Node:
        def __init__(self) -> None:
            self.topics: list[str] = []

        def unsubscribe(self, topic: str) -> bool:
            self.topics.append(topic)
            return True

    node = Node()
    assert (
        _shutdown_transport_node(
            node,
            ("/image", "/camera", "/navsat", "/image"),
        )
        == ()
    )
    assert node.topics == ["/navsat", "/camera", "/image"]

    errors = _shutdown_transport_node(object(), ("/image",))
    assert errors == ("gz.transport13.Node.unsubscribe is unavailable",)


def test_parse_and_stream_reader_verify_digest_and_exact_lengths() -> None:
    packet = _frame()
    parsed_header, parsed_payload = parse_wire_frame(packet)
    streamed_header, streamed_payload = read_wire_frame(io.BytesIO(packet))
    assert streamed_header == parsed_header
    assert streamed_payload == parsed_payload

    corrupted = bytearray(packet)
    corrupted[-1] ^= 0xFF
    with pytest.raises(ValueError, match="SHA-256"):
        parse_wire_frame(corrupted)
    with pytest.raises(ValueError, match="ended early"):
        read_wire_frame(io.BytesIO(packet[:-1]))
    with pytest.raises(ValueError, match="length mismatch"):
        parse_wire_frame(packet + b"diagnostic leaked to stdout")


@pytest.mark.parametrize(
    ("width", "height", "step", "data", "message"),
    [
        (0, 1, 0, b"", "positive width"),
        (2, 1, 5, b"12345", "step is shorter"),
        (2, 2, 6, b"short", r"step \* height"),
    ],
)
def test_normalizer_rejects_bad_geometry(
    width: int,
    height: int,
    step: int,
    data: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_bgr24(
            data,
            width=width,
            height=height,
            step=step,
            pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
        )


def test_header_rejects_inconsistent_geometry_and_wrong_pixel_format() -> None:
    packet = _frame()
    header = unpack_header(packet[:WIRE_HEADER_BYTES])
    with pytest.raises(ValueError, match="stride"):
        pack_header(replace(header, stride=header.stride + 1))
    with pytest.raises(ValueError, match="payload length"):
        pack_header(replace(header, payload_bytes=header.payload_bytes + 3))
    with pytest.raises(ValueError, match="pixel format"):
        pack_header(replace(header, flags=header.flags & ~PIXEL_FORMAT_MASK))


def test_wire_order_validator_rejects_gaps_duplicates_and_nonincreasing_timestamps() -> None:
    first, _ = parse_wire_frame(_frame(sequence=0, stamp_ns=100))
    second, _ = parse_wire_frame(_frame(sequence=1, stamp_ns=101))
    order = WireOrderValidator()
    order.accept(first)
    order.accept(second)
    assert order.next_sequence == 2
    assert order.previous_sensor_stamp_ns == 101

    duplicate_stamp, _ = parse_wire_frame(_frame(sequence=2, stamp_ns=101))
    with pytest.raises(ValueError, match="strictly increasing"):
        order.accept(duplicate_stamp)
    assert order.next_sequence == 2

    gap, _ = parse_wire_frame(_frame(sequence=3, stamp_ns=102))
    with pytest.raises(ValueError, match="sequence mismatch"):
        order.accept(gap)


@pytest.mark.parametrize(
    "field_name",
    ("camera_info_stamp_ns", "navsat_stamp_ns", "odometry_stamp_ns"),
)
def test_wire_rejects_any_required_source_from_the_future(field_name: str) -> None:
    header = unpack_header(_frame(sequence=0, stamp_ns=100)[:WIRE_HEADER_BYTES])
    with pytest.raises(ValueError, match="timestamp is from the future"):
        pack_header(replace(header, **{field_name: 101}))


def test_wire_source_age_accepts_exact_limit_and_rejects_one_nanosecond_over() -> None:
    sensor_stamp_ns = 2_000_000_000
    exact_boundary = sensor_stamp_ns - MAX_SOURCE_SKEW_NS
    common = {
        "sequence": 0,
        "sensor_stamp_ns": sensor_stamp_ns,
        "width": 1,
        "height": 1,
        "camera_info_stamp_ns": exact_boundary,
        "navsat_stamp_ns": exact_boundary,
        "odometry_stamp_ns": exact_boundary,
        "latitude_deg": 47.0,
        "longitude_deg": 8.0,
        "altitude_m": 400.0,
        "position_east_m": 0.0,
        "position_north_m": 0.0,
        "position_up_m": 0.0,
        "orientation_x": 0.0,
        "orientation_y": 0.0,
        "orientation_z": 0.0,
        "orientation_w": 1.0,
        "velocity_forward_mps": 0.0,
        "velocity_left_mps": 0.0,
        "velocity_up_mps": 0.0,
        "attitude_covariance_enu_rad2": TEST_ATTITUDE_COVARIANCE,
        "bgr_payload": b"\x00\x00\x00",
    }
    packet = build_wire_frame(**common)
    header, _payload = parse_wire_frame(packet)
    assert sensor_stamp_ns - header.navsat_stamp_ns == MAX_SOURCE_SKEW_NS

    with pytest.raises(ValueError, match="timestamp skew exceeds"):
        build_wire_frame(
            **{
                **common,
                "navsat_stamp_ns": exact_boundary - 1,
            }
        )


def test_wire_allows_session_static_camera_info_but_keeps_it_causal() -> None:
    packet = build_wire_frame(
        sequence=0,
        sensor_stamp_ns=8_000_000_000,
        width=1,
        height=1,
        camera_info_stamp_ns=1,
        navsat_stamp_ns=7_999_999_999,
        odometry_stamp_ns=7_999_999_999,
        latitude_deg=47.0,
        longitude_deg=8.0,
        altitude_m=400.0,
        position_east_m=0.0,
        position_north_m=0.0,
        position_up_m=0.0,
        orientation_x=0.0,
        orientation_y=0.0,
        orientation_z=0.0,
        orientation_w=1.0,
        velocity_forward_mps=0.0,
        velocity_left_mps=0.0,
        velocity_up_mps=0.0,
        attitude_covariance_enu_rad2=TEST_ATTITUDE_COVARIANCE,
        bgr_payload=b"\x00\x00\x00",
    )
    header, _payload = parse_wire_frame(packet)
    assert header.camera_info_stamp_ns == 1
    assert header.sensor_stamp_ns - header.camera_info_stamp_ns > MAX_SOURCE_SKEW_NS


def test_wire_rejects_zero_sensor_or_readiness_timestamp() -> None:
    with pytest.raises(ValueError, match="timestamps must be positive"):
        build_wire_frame(
            sequence=0,
            sensor_stamp_ns=0,
            width=1,
            height=1,
            camera_info_stamp_ns=1,
            navsat_stamp_ns=1,
            odometry_stamp_ns=1,
            latitude_deg=47.0,
            longitude_deg=8.0,
            altitude_m=400.0,
            position_east_m=0.0,
            position_north_m=0.0,
            position_up_m=0.0,
            orientation_x=0.0,
            orientation_y=0.0,
            orientation_z=0.0,
            orientation_w=1.0,
            velocity_forward_mps=0.0,
            velocity_left_mps=0.0,
            velocity_up_mps=0.0,
            attitude_covariance_enu_rad2=TEST_ATTITUDE_COVARIANCE,
            bgr_payload=b"\x00\x00\x00",
        )


@pytest.mark.parametrize(
    ("latitude", "longitude", "altitude"),
    [
        (math.nan, 8.5, 400.0),
        (-90.0, 8.5, 400.0),
        (90.0, 8.5, 400.0),
        (47.0, math.inf, 400.0),
        (47.0, 8.5, -math.inf),
    ],
)
def test_wire_rejects_nonfinite_navsat(
    latitude: float,
    longitude: float,
    altitude: float,
) -> None:
    expected = r"finite|outside \(-90, 90\)"
    with pytest.raises(ValueError, match=expected):
        build_wire_frame(
            sequence=0,
            sensor_stamp_ns=1,
            width=1,
            height=1,
            camera_info_stamp_ns=1,
            navsat_stamp_ns=1,
            odometry_stamp_ns=1,
            latitude_deg=latitude,
            longitude_deg=longitude,
            altitude_m=altitude,
            position_east_m=0.0,
            position_north_m=0.0,
            position_up_m=0.0,
            orientation_x=0.0,
            orientation_y=0.0,
            orientation_z=0.0,
            orientation_w=1.0,
            velocity_forward_mps=0.0,
            velocity_left_mps=0.0,
            velocity_up_mps=0.0,
            attitude_covariance_enu_rad2=TEST_ATTITUDE_COVARIANCE,
            bgr_payload=b"\x00\x00\x00",
        )


def test_normalizer_rejects_oversize_before_touching_the_short_payload() -> None:
    with pytest.raises(ValueError, match="hard safety bound"):
        normalize_bgr24(
            b"",
            width=MAX_DIMENSION,
            height=MAX_DIMENSION,
            step=MAX_DIMENSION * 3,
            pixel_format_type=GAZEBO_PIXEL_FORMAT_RGB_INT8,
        )


def test_decode_bounded_blob_requires_exact_frame_count_and_no_trailing_stdout() -> None:
    blob = _frame(sequence=0, stamp_ns=10) + _frame(sequence=1, stamp_ns=20)
    decoded = decode_wire_blob(blob, expected_frames=2)
    assert [header.sequence for header, _ in decoded] == [0, 1]
    with pytest.raises(ValueError, match="trailing bytes"):
        decode_wire_blob(blob + b"stderr-only diagnostic", expected_frames=2)
    with pytest.raises(ValueError, match="ended early"):
        decode_wire_blob(blob, expected_frames=3)


def test_unpack_header_rejects_magic_version_and_declared_header_size() -> None:
    packet = _frame()
    values = list(WIRE_HEADER.unpack(packet[:WIRE_HEADER_BYTES]))

    bad_magic = list(values)
    bad_magic[0] = b"BADMAGIC"
    with pytest.raises(ValueError, match="magic"):
        unpack_header(WIRE_HEADER.pack(*bad_magic))

    bad_version = list(values)
    bad_version[1] = WIRE_VERSION + 1
    with pytest.raises(ValueError, match="version"):
        unpack_header(WIRE_HEADER.pack(*bad_version))

    bad_size = list(values)
    bad_size[2] = WIRE_HEADER_BYTES - 1
    with pytest.raises(ValueError, match="header-size"):
        unpack_header(WIRE_HEADER.pack(*bad_size))

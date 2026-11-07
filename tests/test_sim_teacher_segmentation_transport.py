"""CPU checks of exact-time joins and lossless optional segmentation transport."""

import io
import struct
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.sim_teacher.streams import SensorStreamReader
from scripts.gazebo_segmentation_wire import (
    build_segmentation_wire_record,
    normalize_labels_map,
    read_segmentation_wire_frame,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts.sim_teacher_frame_server import (  # noqa: E402
    Candidate,
    ExactSegmentationPairer,
    LatestCandidate,
    OdometrySamplePolicy,
    ODOMETRY_VELOCITY_BOUND_ERROR,
    _frame_joint_state,
    encode_candidate,
)


def _candidate(stamp=100):
    return Candidate(
        stamp, 2, 1, b"\x01\x02\x03\x04\x05\x06", stamp,
        {"stamp_ns": stamp, "latitude_deg": 1.0, "longitude_deg": 2.0, "altitude_m": 3.0},
        {"stamp_ns": stamp, "position_east_m": 0, "position_north_m": 0,
         "position_up_m": 3, "orientation_x": 0, "orientation_y": 0,
         "orientation_z": 0, "orientation_w": 1, "velocity_forward_mps": 0,
         "velocity_left_mps": 0, "velocity_up_mps": 0,
         "attitude_covariance_enu_rad2": (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0)},
        stamp, struct.pack("<ff", 1, 2), 2, 1,
    )


def _contract():
    return {"source_topic": "/depth", "calibration_id": "depth-v1",
            "calibration_sha256": "a" * 64, "registration_mode": "registered_to_rgb",
            "coordinate_space": "rgb_pixel_aligned", "distance_semantics": "optical_axis_z_m",
            "minimum_depth_m": 0.1, "maximum_depth_m": 100, "fx_px": 1,
            "fy_px": 1, "cx_px": 1, "cy_px": 0.5}


@pytest.mark.parametrize("seg_first", [True, False])
def test_join_requires_exact_stamp_and_handles_both_callback_orders(seg_first):
    latest = LatestCandidate()
    pairer = ExactSegmentationPairer(latest, maximum_samples=2)
    candidate = _candidate()
    pairer.put_seg(99, 2, 1, bytes(6), "/seg")
    assert latest.latest is None
    if seg_first:
        pairer.put_seg(100, 2, 1, bytes([255, 1, 42, 2, 0, 43]), "/seg")
        pairer.put_rgb(candidate)
    else:
        pairer.put_rgb(candidate)
        assert latest.latest is None
        pairer.put_seg(100, 2, 1, bytes([255, 1, 42, 2, 0, 43]), "/seg")
    assert latest.latest.segmentation == bytes([255, 1, 42, 2, 0, 43])
    for stamp in range(101, 120):
        pairer.put_rgb(_candidate(stamp))
    assert len(pairer.rgb) == 2
    pairer.put_seg(99, 2, 1, bytes(6), "/seg")
    assert latest.latest.stamp_ns == 100


def test_one_odometry_velocity_spike_is_dropped_then_stream_recovers() -> None:
    policy = OdometrySamplePolicy(max_consecutive_rejections=3)

    assert policy.reject(ValueError(ODOMETRY_VELOCITY_BOUND_ERROR)) == "drop"
    assert policy.velocity_rejections == 1
    policy.accepted()
    assert policy.consecutive_rejections == 0
    assert policy.reject(ValueError(ODOMETRY_VELOCITY_BOUND_ERROR)) == "drop"
    assert policy.velocity_rejections == 2


def test_persistent_odometry_velocity_spikes_fail_closed() -> None:
    policy = OdometrySamplePolicy(max_consecutive_rejections=3)

    assert policy.reject(ValueError(ODOMETRY_VELOCITY_BOUND_ERROR)) == "drop"
    assert policy.reject(ValueError(ODOMETRY_VELOCITY_BOUND_ERROR)) == "drop"
    assert policy.reject(ValueError(ODOMETRY_VELOCITY_BOUND_ERROR)) == "fail"
    assert policy.velocity_rejections == 3
    assert policy.max_observed_consecutive_rejections == 3


def test_raw_labels_map_strips_padding_without_channel_swap():
    message = SimpleNamespace(width=2, height=1, step=8, pixel_format_type=3,
                              data=bytes([255, 1, 42, 2, 0, 43, 99, 99]))
    assert normalize_labels_map(message) == bytes([255, 1, 42, 2, 0, 43])
    message.pixel_format_type = 8
    with pytest.raises(ValueError, match="RGB_INT8"):
        normalize_labels_map(message)


def test_optional_companion_roundtrip_and_identical_nearest_resize():
    labels = bytes([255, 1, 42, 2, 0, 43])
    candidate = replace(_candidate(), segmentation=labels, segmentation_topic="/seg")
    record = encode_candidate(candidate, 0, _contract(), (1, 1), (1, 1), "FLOAT32_M")
    reader = SensorStreamReader(io.BytesIO(record), depth_binding=_contract(),
                                segmentation_topic="/seg", max_frames=1).start()
    sample = reader.wait_first_frame(2)
    assert sample.rgb == bytes([1, 2, 3])
    np.testing.assert_array_equal(sample.segmentation, [[[255, 1, 42]]])
    assert sample.segmentation_info["source_stamp_ns"] == sample.sensor_stamp_ns
    reader.stop()
    # Disabled mode still parses exactly the existing RGB/depth record.
    record = encode_candidate(_candidate(), 0, _contract(), (2, 1), (2, 1), "FLOAT32_M")
    reader = SensorStreamReader(io.BytesIO(record), depth_binding=_contract(), max_frames=1).start()
    assert reader.wait_first_frame(2).segmentation is None
    reader.stop()


def test_gimbal_joint_angles_are_timestamp_bound_and_roundtrip_with_depth():
    sample = {
        "stamp_ns": 95,
        "joints": {
            "c11_yaw_joint": {"position_rad": 0.1, "velocity_rad_s": 0.0},
            "c11_roll_joint": {"position_rad": -0.2, "velocity_rad_s": 0.0},
            "c11_pitch_joint": {"position_rad": 0.3, "velocity_rad_s": 0.0},
        },
    }
    joint_state = _frame_joint_state(sample, 100)
    assert joint_state is not None
    assert joint_state["source_skew_ns"] == 5
    assert joint_state["gimbal_joint_state"]["position_rad"] == {
        "yaw": 0.1,
        "roll": -0.2,
        "pitch": 0.3,
    }

    candidate = replace(_candidate(), joint_state=joint_state)
    record = encode_candidate(candidate, 0, _contract(), (2, 1), (2, 1), "FLOAT32_M")
    reader = SensorStreamReader(io.BytesIO(record), depth_binding=_contract(), max_frames=1).start()
    decoded = reader.wait_first_frame(2)
    assert decoded.joint_state == joint_state
    reader.stop()


def test_companion_rejects_stale_missing_corrupt_or_wrong_rgb():
    metadata = {"encoding": "PANOPTIC_RGB8", "width": 1, "height": 1,
                "sequence": 0, "rgb_sensor_stamp_ns": 100, "source_stamp_ns": 100,
                "rgb_record_sha256": "a" * 64, "source_topic": "/seg"}
    arguments = {"sequence": 0, "stamp_ns": 100, "rgb_sha256": "a" * 64,
                 "width": 1, "height": 1, "source_topic": "/seg"}
    with pytest.raises(ValueError, match="exact RGB"):
        build_segmentation_wire_record({**metadata, "source_stamp_ns": 99}, bytes(3))
    record = build_segmentation_wire_record(metadata, bytes([255, 1, 42]))
    for raw, changes in [(b"", {}), (record[:-1] + b"x", {}),
                         (record, {"stamp_ns": 101}), (record, {"rgb_sha256": "b" * 64})]:
        with pytest.raises(ValueError):
            read_segmentation_wire_frame(io.BytesIO(raw), **{**arguments, **changes})


def test_collector_overlay_and_channel_are_opt_in(tmp_path, monkeypatch):
    import json

    from scripts import collect_sim_teacher_episodes as collector

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"sensors": [{"kind": "rgb_camera"}]}))
    plan = collector.CollectionPlan("scenario", str(runtime), "truth", "explore", 1, 1, 0, "out")
    monkeypatch.setattr(collector, "build_docker_simulator_plan",
                        lambda *args, **kwargs: SimpleNamespace(command=["docker", "run", "image"]))
    environment = SimpleNamespace(name="episode")
    assert not any("model-overlays" in item for item in collector.docker_command(plan))
    assert "--segmentation" not in collector._camera_stream_command(plan, environment)
    enabled = replace(plan, segmentation=True)
    command = collector.docker_command(enabled)
    assert command[command.index("--mount") + 1].endswith(
        "target=/opt/multidetect-gazebo-models/x500_mono_cam,readonly"
    )
    assert "model-overlays" in command[command.index("--mount") + 1]
    assert "--segmentation" in collector._camera_stream_command(enabled, environment)

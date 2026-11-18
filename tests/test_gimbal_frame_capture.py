from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
import pytest

from multidetect.camera_mount import CameraMount
from multidetect.sim_teacher.streams import (
    MavlinkReader,
    MavlinkState,
    SensorSample,
    build_observation,
    validate_gimbal_frame_observation,
)
from multidetect.sim_teacher.gimbal_recording import check_gimbal_manifest as _gimbal_manifest_check
from multidetect.sim_teacher.gimbal_recording import DeltaCameraGeometry, collector_camera_mount
from scripts import collect_sim_teacher_episodes as collector

ROOT = Path(__file__).resolve().parents[1]


def _frame(stamp_ns: int = 1_000_000_000) -> SensorSample:
    return SensorSample(
        rgb=b"rgb", width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1}, pose={}, velocity=[0, 0, 0],
        sensor_stamp_ns=stamp_ns, received_monotonic_ns=20, received_wall_time=1.0,
    )


def _state() -> MavlinkState:
    return MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"x": 10.0, "y": 20.0, "z": -30.0, "time_boot_ms": 1000},
            "ATTITUDE": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0, "time_boot_ms": 1000},
            "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 30000},
            "GPS_RAW_INT": {"fix_type": 3},
        },
        received_monotonic_ns={"GIMBAL_DEVICE_ATTITUDE_STATUS": 20},
    )


def _gimbal_sample(stamp_ms: int = 1000) -> dict:
    return {
        "message": {
            "q": [1.0, 0.0, 0.0, 0.0], "flags": 2,
            "gimbal_device_id": 1, "time_boot_ms": stamp_ms,
        },
        "received_monotonic_ns": 20,
        "timestamp_ms": float(stamp_ms),
        "skew_ms": 0.0,
    }


def _observation(sample=None, stamp_ns=1_000_000_000):
    if sample is not None:
        sample = dict(sample)
        sample["skew_ms"] = sample["timestamp_ms"] - stamp_ns / 1_000_000.0
    return build_observation(
        _frame(stamp_ns), _state(), camera_mount=CameraMount.gimbal(
            offset_body_ned_m=(1.0, 2.0, 3.0)
        ), gimbal_sample=sample,
    )


def test_nearest_mavlink_gimbal_sample_is_selected_by_simulation_timestamp():
    reader = MavlinkReader(object())
    reader._gimbal_events.extend([
        (1, "GIMBAL_DEVICE_ATTITUDE_STATUS", {"q": [1, 0, 0, 0], "gimbal_device_id": 1}, 10, 995),
        (2, "GIMBAL_DEVICE_ATTITUDE_STATUS", {"q": [1, 0, 0, 0], "gimbal_device_id": 1}, 20, 1001),
        (3, "GIMBAL_DEVICE_ATTITUDE_STATUS", {"q": [1, 0, 0, 0], "gimbal_device_id": 1,
                                              "_source_system": 9}, 21, 1000),
        (4, "GIMBAL_DEVICE_ATTITUDE_STATUS", {"q": [1, 0, 0, 0], "gimbal_device_id": 2}, 22, 1000),
        (5, "GIMBAL_DEVICE_ATTITUDE_STATUS", {"q": [1, 0, 0, 0], "gimbal_device_id": 1,
                                              "failure_flags": 1}, 23, 1000),
    ])
    selected = reader.gimbal_sample_at(1_000_000_000)
    assert selected is not None
    assert selected["timestamp_ms"] == 1001
    assert selected["skew_ms"] == 1


def test_fresh_feedback_composes_camera_pose_from_known_geometry():
    observation = _observation(_gimbal_sample())
    validate_gimbal_frame_observation(observation)
    assert observation["gimbal_state"]["reference_frame"] == "world"
    assert observation["gimbal_state"]["source"] == "GIMBAL_DEVICE_ATTITUDE_STATUS"
    assert observation["gimbal_state"]["age_ms"] == 0
    assert observation["camera_pose"]["center_ned"] == [11.0, 22.0, -27.0]
    assert observation["camera_pose"]["optical_to_world"] == pytest.approx(
        [1, 0, 0, 0, 1, 0, 0, 0, 1]
    )
    assert observation["camera_pose"]["reference_frame"] == "NED"


def test_missing_and_stale_gimbal_feedback_are_rejected():
    with pytest.raises(ValueError, match="gimbal_state_missing"):
        validate_gimbal_frame_observation(_observation(None))
    stale = _observation(_gimbal_sample(1000), stamp_ns=4_000_000_000)
    with pytest.raises(ValueError, match="gimbal_state_stale"):
        validate_gimbal_frame_observation(stale)


def test_episode_manifest_check_rejects_any_missing_or_stale_frame(tmp_path):
    good = _observation(_gimbal_sample())
    stale = _observation(_gimbal_sample(1000), stamp_ns=4_000_000_000)
    manifest = tmp_path / "manifest.jsonl"
    for observation in (good, stale):
        observation.pop("rgb")
    manifest.write_text(
        json.dumps({"frame_index": 0, **good}) + "\n"
        + json.dumps({"frame_index": 1, **stale}) + "\n",
        encoding="utf-8",
    )
    result = _gimbal_manifest_check(manifest)
    assert result["passed"] is False
    assert result["frame_count"] == 2
    assert result["stale_frames"] == 1


def test_actual_delta_profile_resolves_gimbal_without_camera_mount_field():
    profile = collector._airframe_profile("delta_tailsitter_gimbal_cam")
    assert profile.camera_mount is None
    mount = collector_camera_mount(profile)
    assert mount.kind == "gimbal"
    assert mount.limits.pitch_max_rad == pytest.approx(math.radians(135))
    assert collector_camera_mount(collector._airframe_profile("rc_cessna_mono_cam")) is None


@pytest.mark.parametrize("pitch,expected_center,expected_axis", [
    (0.0, (10.206, 20.0, -30.308), (1.0, 0.0, 0.0)),
    (-math.pi / 2, (10.006, 20.0, -30.108), (0.0, 0.0, 1.0)),
])
def test_delta_joint_chain_optical_center_includes_pivots_and_sensor_offset(pitch, expected_center, expected_axis):
    geometry = DeltaCameraGeometry.from_sdf(
        ROOT / "simulation/gazebo/models-wide-area/delta_tailsitter_gimbal_cam/model.sdf"
    )
    sensor = _frame()
    sensor.joint_state = {"source_timestamp_ns": 1_000_000_000, "gimbal_joint_state": {
        "position_rad": {"roll": 0.0, "pitch": pitch, "yaw": 0.0},
        "source": "gazebo_joint_state",
        "timestamp_ns": 1_000_000_000, "fresh": True,
    }}
    obs = build_observation(sensor, _state(), camera_mount=CameraMount.gimbal(), camera_geometry=geometry)
    validate_gimbal_frame_observation(obs)
    camera = obs["camera_pose"]
    assert camera["center_ned"] == pytest.approx(expected_center)
    assert camera["optical_to_world"][::3] == pytest.approx(expected_axis, abs=1e-10)
    assert obs["gimbal_state"]["source"] == "gazebo_joint_state"
    assert camera["extrinsics_source"].endswith("model.sdf")


def test_delta_camera_composition_rotates_body_lever_arm_and_optical_axis():
    geometry = DeltaCameraGeometry.from_sdf(
        ROOT / "simulation/gazebo/models-wide-area/delta_tailsitter_gimbal_cam/model.sdf"
    )
    obs = {"pose": {"local_ned_m": [10, 20, -30]},
           "attitude": {"roll_deg": 0, "pitch_deg": 0, "yaw_deg": 90}}
    camera = geometry.camera_pose(obs, {"roll": 0, "pitch": 0, "yaw": 0})
    assert camera["center_ned"] == pytest.approx([10, 20.206, -30.308])
    assert camera["optical_to_world"][::3] == pytest.approx([0, 1, 0], abs=1e-10)


@pytest.mark.parametrize("skew_ms,accepted", [(2000, True), (2001, False), (-2000, True), (-2001, False)])
def test_two_second_gate_uses_absolute_timestamp_distance(skew_ms, accepted):
    sample = _gimbal_sample(4000 + skew_ms)
    state = _state()
    for name in ("LOCAL_POSITION_NED", "ATTITUDE"):
        state.messages[name]["time_boot_ms"] = 4000
    obs = build_observation(_frame(4_000_000_000), state, camera_mount=CameraMount.gimbal(),
                            gimbal_sample={**sample, "skew_ms": skew_ms}, gimbal_max_age_s=2)
    if accepted:
        validate_gimbal_frame_observation(obs)
    else:
        with pytest.raises(ValueError, match="gimbal_state_stale"):
            validate_gimbal_frame_observation(obs)


def test_mavlink_fallback_uses_real_earth_flag_and_sdf_geometry():
    geometry = DeltaCameraGeometry.from_sdf(
        ROOT / "simulation/gazebo/models-wide-area/delta_tailsitter_gimbal_cam/model.sdf"
    )
    sample = _gimbal_sample()
    # Gazebo camera quaternion yaw is rotated by spawn; undo exactly once.
    sample["message"].update(flags=64, q=[math.sqrt(0.5), 0, 0, math.sqrt(0.5)])
    obs = build_observation(_frame(), _state(), camera_mount=CameraMount.gimbal(),
                            gimbal_sample=sample, camera_geometry=geometry,
                            gimbal_yaw_correction_deg=-90)
    validate_gimbal_frame_observation(obs)
    assert obs["gimbal_state"]["yaw_rad"] == pytest.approx(0, abs=1e-10)
    assert obs["camera_pose"]["center_ned"] == pytest.approx([10.206, 20, -30.308])
    assert obs["camera_pose"]["optical_to_world"][::3] == pytest.approx([1, 0, 0], abs=1e-10)


def test_vehicle_heading_quaternion_does_not_apply_body_pitch_twice():
    geometry = DeltaCameraGeometry.from_sdf(
        ROOT / "simulation/gazebo/models-wide-area/delta_tailsitter_gimbal_cam/model.sdf"
    )
    state = _state()
    state.messages["ATTITUDE"].update(pitch=-math.pi / 3, yaw=math.pi / 2)
    sample = _gimbal_sample()
    sample["message"]["flags"] = 32
    obs = build_observation(_frame(), state, camera_mount=CameraMount.gimbal(),
                            gimbal_sample=sample, camera_geometry=geometry)
    validate_gimbal_frame_observation(obs)
    assert obs["camera_pose"]["optical_to_world"][::3] == pytest.approx([0, 1, 0], abs=1e-10)


def test_malformed_joint_sidecar_cannot_leave_generic_zero_offset_camera_pose():
    geometry = DeltaCameraGeometry.from_sdf(
        ROOT / "simulation/gazebo/models-wide-area/delta_tailsitter_gimbal_cam/model.sdf"
    )
    sensor = _frame()
    sensor.joint_state = {"gimbal_joint_state": {
        "position_rad": {"roll": 0, "pitch": 0, "yaw": 0}, "fresh": True,
        "source": "gazebo_joint_state", "timestamp_ns": sensor.sensor_stamp_ns,
    }}
    obs = build_observation(sensor, _state(), camera_mount=CameraMount.gimbal(),
                            gimbal_sample=_gimbal_sample(), camera_geometry=geometry)
    with pytest.raises(ValueError, match="camera_pose_missing"):
        validate_gimbal_frame_observation(obs)


def test_camera_pose_rejects_stale_vehicle_attitude_even_with_fresh_gimbal():
    geometry = DeltaCameraGeometry.from_sdf(
        ROOT / "simulation/gazebo/models-wide-area/delta_tailsitter_gimbal_cam/model.sdf"
    )
    state = _state()
    state.messages["LOCAL_POSITION_NED"]["time_boot_ms"] = 4000
    sample = _gimbal_sample(4000)
    obs = build_observation(_frame(4_000_000_000), state, camera_mount=CameraMount.gimbal(),
                            gimbal_sample=sample, camera_geometry=geometry)
    assert obs["gimbal_state"]["age_ms"] == 0
    assert obs["camera_pose"]["age_ms"] == 3000
    with pytest.raises(ValueError, match="camera_pose_stale"):
        validate_gimbal_frame_observation(obs)


def test_long_range_policy_uses_configured_cruise_pitch():
    from scripts.realworld.long_range_flight import LongRangePolicy

    assert LongRangePolicy([(1, 2, -85)], 80, vtol=True).cruise_gimbal_target["pitch"] == -45
    assert LongRangePolicy([(1, 2, -85)], 80, vtol=True, gimbal_pitch_deg=-30).cruise_gimbal_target["pitch"] == -30


def test_writer_cannot_hide_rejected_frame_when_previous_frames_are_good(tmp_path):
    import numpy as np
    from multidetect.sim_teacher.continuous_video import ContinuousVideoWriter

    writer = ContinuousVideoWriter(tmp_path, hz=1, max_bytes=1_000_000, require_gimbal=True)
    good = _observation(_gimbal_sample())
    good["rgb"] = np.zeros((2, 2, 3), dtype=np.uint8)
    assert writer.write(good, action_index=0)
    bad = _observation(_gimbal_sample(2000), stamp_ns=2_000_000_000)
    bad.update(rgb=good["rgb"], camera_pose=None)
    with pytest.raises(ValueError, match="camera_pose_missing"):
        writer.write(bad, action_index=0)
    result = writer.close()["gimbal_check"]
    assert result["passed"] is False
    assert result["frame_count"] == 1
    assert result["capture_errors"] == ["camera_pose_missing"]


def test_cruise_hold_refresh_requires_fresh_post_command_readback():
    calls, records = [], []
    state = SimpleNamespace(vehicle_time_boot_ms=1000, get=lambda name: {"yaw": 0.0})

    class Session:
        last_command_started_ns = 100
        latest_attitude = None

        def poll(self):
            return "ready"

        def command(self, *args):
            calls.append(args)
            self.last_command_started_ns += 1

    session = Session()
    hold = collector._LongRangeGimbalHold(session, SimpleNamespace(state=state),
                                         {"pitch": -45, "yaw": 0, "roll": 0}, records.append,
                                         clock=lambda: 1.0)
    hold.tick()
    assert calls[0][1:3] == (-45, 0)
    q = [math.cos(math.pi / 8), 0, -math.sin(math.pi / 8), 0]
    session.latest_attitude = ({"q": q, "flags": 64, "time_boot_ms": 1100}, 102)
    state.vehicle_time_boot_ms = 1100
    hold.tick()
    assert hold.readback_count == 1
    assert records[0]["status"] == "reached"
    state.vehicle_time_boot_ms = 2000
    hold.tick()
    assert len(calls) == 2
    state.vehicle_time_boot_ms = 4101
    with pytest.raises(RuntimeError, match="readback missing, stale"):
        hold.tick()


@pytest.mark.parametrize("damage", [None, "gimbal", "camera", "stale", "check", "forged", "nan"])
def test_batch_admission_checks_every_frame_even_if_summary_claims_success(tmp_path, damage):
    from scripts.realworld import wide_collect_batch as batch

    episode = tmp_path / "flight/episodes/episode-test"
    (episode / "video/rgb").mkdir(parents=True)
    identity = {"container_id": "abc", "container_name": "test-slot-5-capture"}
    meta = {"status": "complete", "simulator_container": identity,
            "gimbal_check": {"passed": True, "frame_count": 2}}
    frames = []
    for index in range(2):
        frame = _observation(_gimbal_sample())
        frame.pop("rgb")
        frame["frame_index"] = index
        frame["gimbal_state"]["stabilization"] = {
            "mode": "lock",
            "measurement_fresh": True,
            "horizon_feasible": True,
            "horizon_error_deg": 0.0,
            "horizon_degraded": False,
            "yaw_degraded": False,
            "yaw_error_deg": 0.0,
        }
        frames.append(frame)
        (episode / f"video/rgb/{index}.jpg").write_bytes(b"image")
    if damage in {"gimbal", "forged"}:
        frames[1]["gimbal_state"] = None
    elif damage == "camera":
        frames[1]["camera_pose"] = None
    elif damage == "stale":
        frames[1]["gimbal_state"]["age_ms"] = 2001
    elif damage == "nan":
        frames[1]["gimbal_state"]["age_ms"] = float("nan")
    elif damage == "check":
        meta.pop("gimbal_check")
    (episode / "episode.json").write_text(json.dumps(meta))
    (episode / "video/manifest.jsonl").write_text("\n".join(map(json.dumps, frames)) + "\n")
    raw = {**identity, "name": "job", "gpu": 1, "valid_round": True, "route_completed": True,
           "airframe": batch.DELTA_AIRFRAME, "rtf_window_median": 1,
           "rtf_delta_sim_over_delta_real": 1, "max_distance_from_spawn_m": 4100}
    (tmp_path / "result.json").write_text(json.dumps(raw))
    job = {"job_id": "job", "slot": 5, "matrix": {"airframe": batch.DELTA_AIRFRAME},
           "inputs": {"max_artifact_bytes": 1_000_000}}
    verdict = batch.evaluate(tmp_path, job, {"gpu": 1}, 0)
    assert verdict["accepted"] is (damage is None)
    assert ("gimbal_state_missing" in verdict["errors"]) is (damage is not None)



def test_world_target_position_uses_amsl_and_global_sample_clock():
    state = _state()
    state.messages["GLOBAL_POSITION_INT"].update(alt=340000, time_boot_ms=950)
    obs = build_observation(_frame(), state)
    assert obs["pose"]["alt_m"] == 30.0
    assert obs["pose"]["alt_amsl_m"] == 340.0
    assert obs["pose"]["global_position_skew_ms"] == 50.0
    del state.messages["GLOBAL_POSITION_INT"]["alt"]
    assert build_observation(_frame(), state)["pose"]["alt_amsl_m"] is None

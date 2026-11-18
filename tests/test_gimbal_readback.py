from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.camera_mount import _mat_mul, _mat_transpose, rpy_rotation
from multidetect.sim_teacher.gimbal_readback import attitude_readback
from multidetect.sim_teacher.streams import MavlinkReader
from scripts import collect_sim_teacher_episodes as collector

TARGET = {"yaw": 0.0, "roll": 0.0, "pitch": -45.0}


def quaternion(matrix):
    # Convert via a separate, trace/diagonal based formula, not the decoder.
    import numpy as np
    m = np.array(matrix).reshape(3, 3)
    k = np.array([
        [m[0, 0]-m[1, 1]-m[2, 2], m[1, 0]+m[0, 1], m[2, 0]+m[0, 2], m[2, 1]-m[1, 2]],
        [m[1, 0]+m[0, 1], m[1, 1]-m[0, 0]-m[2, 2], m[2, 1]+m[1, 2], m[0, 2]-m[2, 0]],
        [m[2, 0]+m[0, 2], m[2, 1]+m[1, 2], m[2, 2]-m[0, 0]-m[1, 1], m[1, 0]-m[0, 1]],
        [m[2, 1]-m[1, 2], m[0, 2]-m[2, 0], m[1, 0]-m[0, 1], m.trace()],
    ]) / 3
    values, vectors = np.linalg.eigh(k)
    x, y, z, w = vectors[:, values.argmax()]
    return [float(w), float(x), float(y), float(z)]


def rotation(roll=0, pitch=0, yaw=0):
    return rpy_rotation(roll_rad=math.radians(roll), pitch_rad=math.radians(pitch), yaw_rad=math.radians(yaw))


def bridge_status(angles, body, *, flags=64, correction=-135):
    joints = _mat_mul(_mat_mul(rotation(yaw=angles["yaw"]), rotation(roll=angles["roll"])),
                      rotation(pitch=angles["pitch"]))
    world = _mat_mul(rotation(**body), joints)
    raw = _mat_mul(rotation(yaw=-correction), world) if flags == 64 else joints
    return {"q": quaternion(raw), "flags": flags, "gimbal_device_id": 0, "time_boot_ms": 1000}


def attitude(body):
    return {**{key: math.radians(value) for key, value in body.items()}, "time_boot_ms": 1000}


@pytest.mark.parametrize("flags", [32, 64])
@pytest.mark.parametrize("body", [
    {"roll": 0, "pitch": -80, "yaw": 45},
    {"roll": 37, "pitch": -80, "yaw": -179},
    {"roll": -25, "pitch": -100, "yaw": 130},
])
def test_tailsitter_bridge_full_rotation_recovers_c11_joints(flags, body):
    target = {"yaw": 12.0, "roll": 15.0, "pitch": -45.0}
    status = bridge_status(target, body, flags=flags)
    result = attitude_readback(status, target, vehicle_attitude=attitude(body),
                               yaw_correction_deg=-135, gazebo_body_joints=True)
    assert result["actual_deg"] == pytest.approx(target, abs=1e-6)
    assert max(result["error_deg"].values()) < 1e-6
    assert result["raw_q"] == status["q"]
    assert result["comparison_frame"] == "body_FRD_C11_ZXY"


@pytest.mark.parametrize("flags", [0, 16, 32, 64, 32 | 16, 64 | 4 | 8])
def test_protocol_vehicle_frame_is_heading_only_even_with_body_pitch_minus80(flags):
    body = {"roll": 22, "pitch": -80, "yaw": 40}
    earth = bool(flags & 64 or (not flags & 32 and flags & 16))
    q = quaternion(rotation(pitch=-45, yaw=40 if earth else 0))
    result = attitude_readback({"q": q, "flags": flags}, TARGET, vehicle_attitude=attitude(body))
    assert result["actual_deg"] == pytest.approx(TARGET, abs=1e-6)
    assert max(result["error_deg"].values()) < 1e-6


@pytest.mark.parametrize("flags", [32, 64])
@pytest.mark.parametrize("axis", ["yaw", "roll", "pitch"])
def test_wrong_joint_attitude_still_rejected(flags, axis):
    body = {"roll": 35, "pitch": -80, "yaw": 170}
    wrong = {**TARGET, axis: TARGET[axis] + 20}
    result = attitude_readback(bridge_status(wrong, body, flags=flags), TARGET,
                               vehicle_attitude=attitude(body), yaw_correction_deg=-135,
                               gazebo_body_joints=True)
    assert result["error_deg"][axis] == pytest.approx(20)
    assert max(result["error_deg"].values()) > 4


def test_protocol_mode_does_not_admit_body_joint_target_as_horizon_lock():
    body = {"roll": 35, "pitch": -80, "yaw": 170}
    status = bridge_status(TARGET, body)
    result = attitude_readback(status, TARGET, vehicle_attitude=attitude(body), yaw_correction_deg=-135)
    assert max(result["error_deg"].values()) > 4


@pytest.mark.parametrize("damage", ["missing_body", "partial_body", "nan_body", "nan_q", "zero_q", "flags", "device_failure"])
def test_invalid_or_unresolved_readback_fails_closed_and_keeps_raw(damage):
    body = {"roll": 0, "pitch": -80, "yaw": 45}
    status = bridge_status(TARGET, body)
    vehicle = attitude(body)
    if damage == "missing_body": vehicle = None
    if damage == "partial_body": vehicle.pop("pitch")
    if damage == "nan_body": vehicle["roll"] = math.nan
    if damage == "nan_q": status["q"][0] = math.nan
    if damage == "zero_q": status["q"] = [0, 0, 0, 0]
    if damage == "flags": status["flags"] = 96
    if damage == "device_failure": status["failure_flags"] = 1
    result = attitude_readback(status, TARGET, vehicle_attitude=vehicle, gazebo_body_joints=True)
    assert result["error_deg"] is None
    assert result["readback_error"]
    assert result["raw_q"] == status["q"]
    assert result["flags"] == status["flags"]
    assert result["gimbal_device_id"] == 0


def test_reader_attitude_alignment_rejects_missing_or_widely_separated_samples():
    reader = MavlinkReader(SimpleNamespace())
    assert reader.attitude_at(1000) is None
    for stamp, yaw in [(900, 179), (1100, -179)]:
        reader._record_pose_message("ATTITUDE", {"time_boot_ms": stamp, "roll": 0, "pitch": math.radians(-80),
                                                 "yaw": math.radians(yaw)}, stamp, 0)
    sample = reader.attitude_at(1000)
    assert sample["pitch"] == pytest.approx(math.radians(-80))
    assert abs(sample["yaw"]) == pytest.approx(math.pi)
    assert sample["time_boot_ms"] == 1000
    assert reader.attitude_at(1400) is None
    assert reader.attitude_at(1000, max_skew_ms=50) is None


def test_live_readback_uses_device_time_not_latest_body_pose():
    reader = MavlinkReader(SimpleNamespace())
    body = {"roll": 0, "pitch": -80, "yaw": 45}
    status = bridge_status(TARGET, body)
    for stamp in [900, 1100]:
        reader._record_pose_message("ATTITUDE", {**attitude(body), "time_boot_ms": stamp}, stamp, 0)
    reader.state.messages["ATTITUDE"] = {"roll": 0, "pitch": 0, "yaw": 0}
    result = collector._gimbal_readback(reader, status, TARGET, yaw_correction_deg=-135, gazebo_body_joints=True)
    assert max(result["error_deg"].values()) < 1e-6
    assert result["vehicle_attitude_time_boot_ms"] == 1000


def test_simulation_mode_is_explicit_not_inferred_from_device_zero_or_flags64():
    profile = collector._airframe_profile("delta_tailsitter_gimbal_cam")
    assert collector._uses_gazebo_gimbal_body_joints(profile)
    assert not collector._uses_gazebo_gimbal_body_joints(SimpleNamespace(camera=SimpleNamespace(gimbal={})))


def test_recorded_iowa_smoke_and_large_tilt_pass_but_wrong_target_fails():
    fixture = json.loads((Path(__file__).parent / "fixtures/gimbal_readback_iowa.json").read_text())
    for case in fixture["cases"]:
        result = attitude_readback(case["status"], fixture["target_deg"],
                                   vehicle_attitude=case["attitude"],
                                   yaw_correction_deg=fixture["yaw_correction_deg"], gazebo_body_joints=True)
        assert max(result["error_deg"].values()) <= 4, case["name"]
        wrong = attitude_readback(case["status"], {**fixture["target_deg"], "pitch": -25},
                                  vehicle_attitude=case["attitude"],
                                  yaw_correction_deg=fixture["yaw_correction_deg"], gazebo_body_joints=True)
        assert max(wrong["error_deg"].values()) > 4, case["name"]
        if case["name"] == "transition_large_tilt":
            protocol = attitude_readback(case["status"], fixture["target_deg"],
                                         vehicle_attitude=case["attitude"],
                                         yaw_correction_deg=fixture["yaw_correction_deg"])
            assert protocol["error_deg"]["roll"] > 170


@pytest.mark.parametrize("wrong", [False, True])
def test_managed_primitive_and_timeout_ack_use_same_frame(wrong):
    body = {"roll": 35, "pitch": -80, "yaw": 170}
    status = bridge_status({**TARGET, "pitch": -25 if wrong else -45}, body)
    clock, commands = [0.0], []

    class Session:
        pending = None
        error = None
        phase = "ready"
        last_command_started_ns = 100
        latest_attitude = (status, 200)

        def command(self, *args):
            commands.append(args)

        def poll(self):
            return self.phase

    session = Session()
    reader = SimpleNamespace(state={"ATTITUDE": attitude(body)})
    profile = collector._airframe_profile("delta_tailsitter_gimbal_cam")

    def sleep(_):
        clock[0] += .1

    result = collector._execute_managed_gimbal_action(
        collector.ActionPrimitive.gimbal(pitch_deg=-45), session=session, reader=reader, profile=profile,
        check=lambda: None, yaw_correction_deg=-135, timeout_s=.25, clock=lambda: clock[0], sleep=sleep)
    assert result.status == ("timeout" if wrong else "success")
    assert result.ack["raw_q"] == status["q"]
    assert result.ack["flags"] == 64
    assert result.ack["gimbal_device_id"] == 0
    assert result.ack["command_flags"] == 32  # bridge body joints; no unsupported horizon-lock claim
    assert commands[0][5] == 32
    assert result.ack["comparison_frame"] == "body_FRD_C11_ZXY"
    assert result.ack["actual_deg"]["pitch"] == pytest.approx(-25 if wrong else -45)
    assert result.ack["error_deg"]["pitch"] == pytest.approx(20 if wrong else 0)


def test_hold_uses_full_body_conversion_and_records_failed_readback():
    body = {"roll": 35, "pitch": -80, "yaw": 170}
    calls, records = [], []
    state = SimpleNamespace(vehicle_time_boot_ms=1000, get=lambda _: attitude(body))

    class Session:
        last_command_started_ns = 100
        latest_attitude = None

        def poll(self):
            return "ready"

        def command(self, *args):
            calls.append(args)
            self.last_command_started_ns += 1

    session = Session()
    hold = collector._LongRangeGimbalHold(session, SimpleNamespace(state=state), TARGET, records.append,
                                         yaw_correction_deg=-135, gazebo_body_joints=True, clock=lambda: 1)
    hold.tick()
    assert calls[0][5] == 32
    good = bridge_status(TARGET, body)
    session.latest_attitude = (good, 200)
    hold.tick()
    assert hold.readback_count == 1
    assert records[0]["comparison_frame"] == "body_FRD_C11_ZXY"
    state.vehicle_time_boot_ms = 2000
    hold.tick()
    bad = bridge_status({**TARGET, "pitch": -25}, body)
    bad["time_boot_ms"] = 4001
    session.latest_attitude = (bad, 300)
    state.vehicle_time_boot_ms = 4001
    with pytest.raises(RuntimeError, match="readback missing, stale"):
        hold.tick()
    assert records[-1]["status"] == "timeout"
    assert records[-1]["raw_q"] == bad["q"]
    assert records[-1]["error_deg"]["pitch"] == pytest.approx(20)


def test_smoke_path_uses_same_bridge_comparison_and_command_flags(monkeypatch):
    import time

    body = {"roll": 35, "pitch": -80, "yaw": 170}
    state = {"ATTITUDE": attitude(body), "received_monotonic_ns": {}}
    sent = []

    class Mav:
        def command_long_send(self, *args):
            sent.append(args)
            if args[2] == 1000:
                state["GIMBAL_DEVICE_ATTITUDE_STATUS"] = bridge_status(TARGET, body)
                state["received_monotonic_ns"]["GIMBAL_DEVICE_ATTITUDE_STATUS"] = time.monotonic_ns()

    connection = SimpleNamespace(mav=Mav(), target_system=1, target_component=1)
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)

    def ack(_self, command, _timeout):
        if command == 1001:
            state["GIMBAL_MANAGER_STATUS"] = {"primary_control_sysid": 255, "primary_control_compid": 190}
            state["received_monotonic_ns"]["GIMBAL_MANAGER_STATUS"] = time.monotonic_ns()
        return {"result": 0}

    monkeypatch.setattr(collector._ReaderAckLink, "wait_command_ack", ack)
    result = collector._command_and_wait_gimbal(connection, reader, TARGET, timeout_s=.2,
                                                yaw_correction_deg=-135, gazebo_body_joints=True)
    assert result["status"] == "reached"
    assert result["actual_deg"] == pytest.approx(TARGET, abs=1e-6)
    assert result["command_flags"] == sent[-1][8] == 32
    assert result["raw_q"] == state["GIMBAL_DEVICE_ATTITUDE_STATUS"]["q"]


def test_attitude_readback_interpolates_roll_across_tailsitter_wrap():
    reader = MavlinkReader(SimpleNamespace())
    for stamp, roll in [(900, 179), (1100, -179)]:
        reader._record_pose_message("ATTITUDE", {"time_boot_ms": stamp, "roll": math.radians(roll),
                                                 "pitch": math.radians(-80), "yaw": 0}, stamp, 0)
    assert abs(reader.attitude_at(1000)["roll"]) == pytest.approx(math.pi)


def test_unavailable_aligned_attitude_does_not_fall_back_to_latest():
    reader = MavlinkReader(SimpleNamespace())
    body = {"roll": 0, "pitch": -80, "yaw": 45}
    reader.state.messages["ATTITUDE"] = attitude(body)
    status = bridge_status(TARGET, body)
    result = collector._gimbal_readback(reader, status, TARGET, yaw_correction_deg=-135, gazebo_body_joints=True)
    assert result["error_deg"] is None
    assert result["readback_error"] == "earth to body readback requires full vehicle attitude"
    assert result["raw_q"] == status["q"]

"""Measured, frame-aligned gimbal video admission and camera kinematics."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from multidetect.camera_mount import (
    CameraMount, GimbalLimits, _mat_mul, _mat_transpose, _mat_vec, rpy_rotation,
)

DEFAULT_MAX_AGE_MS = 2_000.0


def collector_camera_mount(profile: Any) -> CameraMount | None:
    """Resolve both supported profile declarations without losing gimbal capability."""
    if profile is None:
        return None
    if profile.camera_mount is not None:
        return profile.camera_mount.to_camera_mount()
    if profile.camera.mount != "gimbal":
        return None
    config = profile.camera.gimbal
    bounds = config["angle_range_deg"]
    return CameraMount.gimbal(limits=GimbalLimits(
        pitch_min_rad=-math.radians(bounds["pitch"][1]),
        pitch_max_rad=-math.radians(bounds["pitch"][0]),
        yaw_min_rad=math.radians(bounds["yaw"][0]),
        yaw_max_rad=math.radians(bounds["yaw"][1]),
        roll_min_rad=math.radians(bounds["roll"][0]),
        roll_max_rad=math.radians(bounds["roll"][1]),
    ))


@dataclass(frozen=True)
class DeltaCameraGeometry:
    """SDF zero-link poses and revolute pivots, converted from FLU to FRD.

    The C11 chain has coincident zero-angle link origins. Rotations about the
    displaced roll/pitch pivots move the optical center as well as its axes.
    """

    base_frd: tuple[float, ...]
    pivots_frd: tuple[tuple[float, ...], ...]
    sensor_frd: tuple[float, ...]
    source: str

    @classmethod
    def from_sdf(cls, path: Path) -> DeltaCameraGeometry:
        model = ET.parse(path).getroot().find("model")
        if model is None:
            raise ValueError("camera geometry model missing")

        def translation(element: ET.Element | None) -> tuple[float, float, float]:
            if element is None:
                raise ValueError("camera geometry pose missing")
            values = tuple(float(value) for value in (element.text or "").split())
            if (len(values) != 6 or not all(math.isfinite(value) for value in values)
                    or any(abs(value) > 1e-12 for value in values[3:])):
                raise ValueError("C11 camera geometry requires zero static rotations")
            return (values[0], -values[1], -values[2])

        base = translation(model.find("link[@name='c11_mount_link']/pose"))
        pivots = []
        for axis, expected in (("yaw", "0 0 -1"), ("roll", "1 0 0"), ("pitch", "0 -1 0")):
            joint = model.find(f"joint[@name='c11_{axis}_joint']")
            if joint is None or joint.findtext("axis/xyz") != expected:
                raise ValueError("unsupported C11 joint axis")
            child = joint.findtext("child")
            if translation(model.find(f"link[@name='{child}']/pose")) != base:
                raise ValueError("C11 zero-angle link origins must coincide")
            pivots.append(translation(joint.find("pose")))
        sensor = translation(model.find("link[@name='camera_link']/sensor[@name='camera']/pose"))
        return cls(base, tuple(pivots), sensor, str(path))

    def center_body(self, angles_rad: Mapping[str, float]) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Apply T(pivot) R(joint) T(-pivot) for yaw, roll, pitch."""
        rotation = rpy_rotation(roll_rad=0, pitch_rad=0, yaw_rad=0)
        center = self.base_frd
        for axis, pivot in zip(("yaw", "roll", "pitch"), self.pivots_frd, strict=True):
            joint = rpy_rotation(**{f"{name}_rad": angles_rad[axis] if axis == name else 0.0
                                    for name in ("roll", "pitch", "yaw")})
            shifted = _mat_vec(joint, pivot)
            displacement = _mat_vec(rotation, tuple(pivot[i] - shifted[i] for i in range(3)))
            center = tuple(center[i] + displacement[i] for i in range(3))
            rotation = _mat_mul(rotation, joint)
        sensor = _mat_vec(rotation, self.sensor_frd)
        return tuple(center[i] + sensor[i] for i in range(3)), rotation

    def camera_pose(self, observation: Mapping[str, Any], angles_rad: Mapping[str, float], *,
                    world_rotation: tuple[float, ...] | None = None) -> dict[str, Any]:
        center_body, relative_rotation = self.center_body(angles_rad)
        body_rotation = rpy_rotation(**{
            f"{axis}_rad": math.radians(observation["attitude"][f"{axis}_deg"])
            for axis in ("roll", "pitch", "yaw")
        })
        offset = _mat_vec(body_rotation, center_body)
        position = observation["pose"]["local_ned_m"]
        return {
            "center_ned": [position[i] + offset[i] for i in range(3)],
            "optical_to_world": list(world_rotation or _mat_mul(body_rotation, relative_rotation)),
            "reference_frame": "NED",
            "optical_frame": "+X forward, +Y right, +Z down",
            "source": "vehicle_pose_x_measured_gimbal_x_sdf_extrinsics",
            "extrinsics_source": self.source,
            "extrinsics": {"base_frd_m": self.base_frd, "pivots_frd_m": self.pivots_frd,
                           "sensor_frd_m": self.sensor_frd, "joint_order": ["yaw", "roll", "pitch"]},
        }

    def apply(self, observation: dict[str, Any], joint: Any, *, max_age_ms: float) -> None:
        """Prefer the RGB sidecar's actual source time and physical joint angles."""
        stamp = observation["frame_sensor_stamp_ns"]
        # A failed kinematic reconstruction must never leave the generic,
        # zero-lever-arm camera pose looking like valid C11 geometry.
        observation.pop("camera_pose", None)
        if isinstance(joint, Mapping) and isinstance(joint.get("position_rad"), Mapping):
            angles = joint["position_rad"]
            source_ns = joint.get("source_timestamp_ns")
            if (not all(finite(angles.get(axis)) for axis in ("yaw", "roll", "pitch"))
                    or not finite(source_ns) or joint.get("source") != "gazebo_joint_state"):
                return
            skew = (source_ns - stamp) / 1e6
            age = abs(skew)
            _, relative = self.center_body(angles)
            observation["gimbal_state"] = {
                "roll_rad": math.atan2(relative[7], relative[8]),
                "pitch_rad": math.asin(max(-1.0, min(1.0, relative[6]))),
                "yaw_rad": math.atan2(relative[3], relative[0]),
                "pitch_convention": "positive down", "euler_order": "ZYX",
                "joint_position_rad": dict(angles), "rotation": list(relative),
                "reference_frame": "body_FRD", "yaw_frame": "body",
                "source": "gazebo_joint_state", "timestamp_ns": source_ns,
                "frame_timestamp_ns": stamp, "timestamp_clock": "gazebo_simulation",
                "age_ms": age, "skew_ms": skew, "fresh": joint.get("fresh") is True and age <= max_age_ms,
            }
            if observation["gimbal_state"]["fresh"]:
                camera = self.camera_pose(observation, angles)
                camera.update(age_ms=age, skew_ms=skew)
                observation["camera_pose"] = camera
        else:
            state = observation.get("gimbal_state")
            if not isinstance(state, Mapping) or not state.get("fresh"):
                return
            rotation = state["rotation"]
            body = rpy_rotation(**{f"{axis}_rad": math.radians(observation["attitude"][f"{axis}_deg"])
                                   for axis in ("roll", "pitch", "yaw")})
            if state["yaw_frame"] == "world":
                relative = _mat_mul(_mat_transpose(body), rotation)
                world = rotation
            elif state["yaw_frame"] == "body":
                relative, world = rotation, _mat_mul(body, rotation)
            else:
                observation.pop("camera_pose", None)
                return
            # C11 uses Rz(yaw) Rx(roll) Ry(pitch), with roll limited to +/-45 deg.
            angles = {"yaw": math.atan2(-relative[1], relative[4]),
                      "roll": math.asin(max(-1.0, min(1.0, relative[7]))),
                      "pitch": math.atan2(-relative[6], relative[8])}
            camera = self.camera_pose(observation, angles, world_rotation=world)
            camera.update(age_ms=state["age_ms"], skew_ms=state["skew_ms"])
            observation["camera_pose"] = camera


def finite(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def validate_gimbal_frame_observation(observation: Mapping[str, Any], *,
                                      max_age_ms: float = DEFAULT_MAX_AGE_MS) -> None:
    state, camera = observation.get("gimbal_state"), observation.get("camera_pose")
    if not isinstance(state, Mapping):
        raise ValueError("gimbal_state_missing")
    if not finite(state.get("age_ms")) or not 0 <= state["age_ms"] <= max_age_ms:
        raise ValueError("gimbal_state_stale")
    frame_ns = observation.get("frame_sensor_stamp_ns", observation.get("sensor_stamp_ns"))
    source_ns = state.get("timestamp_ns")
    if (type(frame_ns) is not int or type(source_ns) is not int or min(frame_ns, source_ns) < 0
            or state.get("frame_timestamp_ns") != frame_ns):
        raise ValueError("gimbal_state_missing")
    expected_skew = (source_ns - frame_ns) / 1e6
    if (not finite(state.get("skew_ms")) or abs(state["skew_ms"]) > max_age_ms
            or abs(expected_skew - state["skew_ms"]) > 1e-6
            or abs(abs(expected_skew) - state["age_ms"]) > 1e-6
            or state.get("fresh") is not True
            or not all(finite(state.get(f"{axis}_rad")) for axis in ("roll", "pitch", "yaw"))
            or state.get("reference_frame") not in {"body_FRD", "NED", "body", "world"}
            or state.get("source") not in {"gazebo_joint_state", "GIMBAL_DEVICE_ATTITUDE_STATUS"}):
        raise ValueError("gimbal_state_missing")
    if not isinstance(camera, Mapping):
        raise ValueError("camera_pose_missing")
    for field, size in (("center_ned", 3), ("optical_to_world", 9)):
        values = camera.get(field)
        if not isinstance(values, (list, tuple)) or len(values) != size or not all(map(finite, values)):
            raise ValueError("camera_pose_missing")
    if camera.get("reference_frame") != "NED" or not camera.get("extrinsics_source"):
        raise ValueError("camera_pose_missing")
    if not finite(camera.get("age_ms")) or not 0 <= camera["age_ms"] <= max_age_ms:
        raise ValueError("camera_pose_stale")


def check_gimbal_manifest(path: str | Path, *, max_age_ms: float = DEFAULT_MAX_AGE_MS) -> dict[str, Any]:
    frames = missing = stale = invalid = 0
    failures = []
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("manifest_invalid")
                    if row.get("event") is not None:
                        continue
                    frames += 1
                    validate_gimbal_frame_observation(row, max_age_ms=max_age_ms)
                except (ValueError, TypeError) as exc:
                    reason = str(exc)
                    if reason in {"gimbal_state_stale", "camera_pose_stale"}:
                        stale += 1
                    elif reason in {"gimbal_state_missing", "camera_pose_missing"}:
                        missing += 1
                    else:
                        invalid += 1
                    if len(failures) < 20:
                        failures.append({"line": number, "reason": reason})
    except (OSError, UnicodeError) as exc:
        invalid += 1
        failures.append({"reason": "manifest_missing", "detail": str(exc)})
    return {"passed": frames > 0 and missing + stale + invalid == 0,
            "frame_count": frames, "missing_frames": missing, "stale_frames": stale,
            "invalid_rows": invalid, "max_age_ms": max_age_ms, "failures": failures}

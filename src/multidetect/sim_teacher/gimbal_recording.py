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
X1_TRACK_SAMPLE_HZ = 10
X1_TRACK_TRANSIENT_SIM_S = 20  # measured settling: error < 2 deg by ~9-11 s (ROOTCAUSE.md)
X1_TRACK_MIN_LOCK_RATE = 0.9  # a tracking hold below this geometric lock rate is an unacceptable eval run
X1_TRACK_PREVIEW_S = 0.30  # body-rate lead for the host-side track preview (GETMETA tuning: 0.3 s / 30 deg beat 0.2 and 0.4)
X1_TRACK_PREVIEW_MAX_DEG = 30.0  # cap for the track preview lead


# Fixed-wing (cruise / turn) tracking caliber, opt-in beside the MC hover hold. Thresholds are provisional
# starting points for the first flight (hover steady state converges to < 2 deg; FW adds ~18 m/s body motion):
X1_FW_TURN_ENTER_ROLL_DEG = 15.0  # |roll| at or above this is a turn leg (PX4 bank limit is 35 deg)
X1_FW_TURN_EXIT_ROLL_DEG = 8.0  # hysteresis: back to cruise only below this
X1_FW_VTOL_STATE = 4  # MAV_VTOL_STATE_FW
X1_FW_CRUISE_MAX_P95_DEG = 5.0  # cruise pointing error P95
X1_FW_TURN_MAX_P95_DEG = 10.0  # turn pointing error P95 (yaw rate ~18 deg/s at 35 deg bank, 18 m/s)
X1_FW_MIN_LOCK_RATE = X1_TRACK_MIN_LOCK_RATE  # geometric in-view rate, both phases
X1_FW_MIN_SAMPLES = {"cruise": 100, "turn": 20}  # scored samples at 10 Hz (10 s / 2 s of flight)
X1_FW_MAX_UNWIND_FRACTION = 0.10  # samples taken while the yaw-unwind had dropped track mode
X1_FW_MIN_REACHABLE_COVERAGE = 0.80
X1_FW_PLANNING_MARGIN_DEG = 5.0


class X1FwPhase:
    """Label fixed-wing samples ``cruise`` / ``turn`` from the MAVLink bank angle and VTOL state.

    ``None`` outside fixed-wing flight (transition, MC) or without a finite roll; ``turn`` uses hysteresis.
    """

    def __init__(self):
        self.turning = False

    def update(self, roll_deg, vtol_state):
        if vtol_state != X1_FW_VTOL_STATE or type(roll_deg) not in (int, float) or not math.isfinite(roll_deg):
            self.turning = False
            return None
        bank = abs(roll_deg)
        self.turning = bank >= (X1_FW_TURN_EXIT_ROLL_DEG if self.turning else X1_FW_TURN_ENTER_ROLL_DEG)
        return "turn" if self.turning else "cruise"


def x1_track_preview(environ=None):
    """(lead seconds, max lead radians) for the eval track pose; overridable for tuning flights."""
    import os
    env = os.environ if environ is None else environ
    lead = float(env.get("MULTIDETECT_X1_TRACK_PREVIEW_S", X1_TRACK_PREVIEW_S))
    cap = float(env.get("MULTIDETECT_X1_TRACK_PREVIEW_MAX_DEG", X1_TRACK_PREVIEW_MAX_DEG))
    if not (math.isfinite(lead) and 0.0 <= lead <= 1.0 and math.isfinite(cap) and 0.0 < cap <= 90.0):
        raise ValueError("invalid X1 track preview override")
    return lead, math.radians(cap)


def x1_track_preaim(track_step, index, action) -> bool:
    """True on the MC return step just before the eval hold: aim the gimbal at the target early.

    Engaging track mode only at the hold start meant a 17-19 deg slew inside the measured window
    (ROOTCAUSE.md). Entering track mode during the hover return lets the gimbal settle first.
    """
    return (type(track_step) is int and index == track_step - 1
            and getattr(action, "reason", None) == "hover_return_to_home")


class X1YawUnwind:
    """Unwind an accumulated gimbal yaw winding while the pre-aimed hover return is still under way.

    Tracking a target through a turning return can wind the C11 yaw joint to its +-2pi stop (the bridge
    then has to jump up to ~257 deg at the hold start). Between ``ENTER_RAD`` and the stop, drop to
    lock/follow (relative yaw 0) until the joint is back near zero, then resume tracking; bounded in time.
    """

    ENTER_RAD = math.radians(150.0)
    EXIT_RAD = math.radians(20.0)
    MAX_S = 6.0

    def __init__(self):
        self.active = False
        self.started_s = None

    def step(self, yaw_rad, now_s):
        """Return "lock" to start unwinding, "track" to resume tracking, or None."""
        if yaw_rad is None or not math.isfinite(yaw_rad):
            return None
        if not self.active:
            if abs(yaw_rad) >= self.ENTER_RAD:
                self.active, self.started_s = True, now_s
                return "lock"
            return None
        if abs(yaw_rad) <= self.EXIT_RAD or now_s - self.started_s > self.MAX_S:
            self.active, self.started_s = False, None
            return "track"
        return None


def x1_eval_yaw_unwind(track_step, index, action, *, fw_leg):
    """Use the MC pre-aim policy only; the FW bridge retains its own feasible winding."""
    if not fw_leg and x1_track_preaim(track_step, index, action):
        return X1YawUnwind()
    return None


class X1TrackCapture:
    """Collect distinct, simulation-timed samples using the existing sensor pipe.

    When the reader advertises ``meta_supported`` the samples come from the pixel-free
    ``GETMETA`` path (``request_meta`` / ``latest_meta``, a few ms per round trip, one
    request kept in flight); otherwise the legacy pixel path (``request`` / ``latest``)
    is used unchanged. Both paths skip ahead instead of chasing a backlog.
    """

    META_RETRY_S = 1.0  # re-ask if a metadata answer has not arrived this long after the request

    def __init__(self, start_sim_ns: int | None):
        self.period_ns = 1_000_000_000 // X1_TRACK_SAMPLE_HZ
        self.due_ns = start_sim_ns
        self.next_request_wall_s = 0.0
        self._meta_asked: tuple[int, float] | None = None  # (due_ns asked for, wall_s)

    def tick(self, reader, *, sim_ns, wall_s):
        if self.due_ns is None or sim_ns is None or sim_ns < self.due_ns:
            return None
        if getattr(reader, "meta_supported", False) and hasattr(reader, "request_meta"):
            return self._tick_meta(reader, sim_ns, wall_s)
        sample = reader.latest
        if sample is not None and self.due_ns <= sample.sensor_stamp_ns <= sim_ns:
            # A request/response server delivers ~3 frames/s, below the 10 Hz source. Do not chase
            # the backlog frame by frame: skip ahead to "now" so the samples span the whole hold.
            self.due_ns = max(sample.sensor_stamp_ns + self.period_ns, sim_ns)
            return sample
        if wall_s >= self.next_request_wall_s:
            self.next_request_wall_s = wall_s + self.period_ns / 1e9
            # The frame server returns the first frame strictly after its argument, so ask for
            # due - 1 to get the frame stamped exactly at due (the old request skipped every other one).
            reader.request(max(self.due_ns - 1, 0))
        return None

    def _tick_meta(self, reader, sim_ns, wall_s):
        sample = getattr(reader, "latest_meta", None)
        if sample is not None and self.due_ns <= sample.sensor_stamp_ns <= sim_ns:
            self.due_ns = max(sample.sensor_stamp_ns + self.period_ns, sim_ns)
            self._ask_meta(reader, wall_s)  # pipeline: next request leaves before the next tick
            return sample
        asked = self._meta_asked
        if asked is None or asked[0] != self.due_ns or wall_s - asked[1] >= self.META_RETRY_S:
            self._ask_meta(reader, wall_s)
        return None

    def _ask_meta(self, reader, wall_s):
        # Strictly-after server semantics: ask for due - 1 to receive the frame stamped at due.
        self._meta_asked = (self.due_ns, wall_s)
        reader.request_meta(max(self.due_ns - 1, 0))


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

    Zero-angle link origins may coincide (v0, pivots from joint poses) or be offset (v1 real geometry, pivots at
    link origin plus joint pose). Rotations about the displaced pivots move the optical center as well as its axes.
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

        def sub(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
            return tuple(a[i] - b[i] for i in range(3))

        def add(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
            return tuple(a[i] + b[i] for i in range(3))

        base = translation(model.find("link[@name='c11_mount_link']/pose"))
        pivots = []
        for axis, expected in (("yaw", "0 0 -1"), ("roll", "1 0 0"), ("pitch", "0 -1 0")):
            joint = model.find(f"joint[@name='c11_{axis}_joint']")
            if joint is None or joint.findtext("axis/xyz") != expected:
                raise ValueError("unsupported C11 joint axis")
            child = joint.findtext("child")
            # The pivot sits at joint pose (in the child frame) from the child link origin; both are kept relative to
            # the mount origin so links may be offset (real-geometry gimbal_extrinsics=v1) or coincident (v0).
            link_origin = translation(model.find(f"link[@name='{child}']/pose"))
            pivots.append(add(sub(link_origin, base), translation(joint.find("pose"))))
        camera_origin = translation(model.find("link[@name='camera_link']/pose"))
        sensor = add(sub(camera_origin, base),
                     translation(model.find("link[@name='camera_link']/sensor[@name='camera']/pose")))
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

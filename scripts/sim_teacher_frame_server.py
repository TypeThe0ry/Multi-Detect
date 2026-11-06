#!/usr/bin/env python3
# ruff: noqa: E501
"""Collector-owned latest-wins Gazebo frame server.

stdin accepts ``GET <minimum Gazebo simulation nanoseconds>`` per line.
Each request produces one MDGZSIM3 record and its MDGZDEP1 companion on
stdout. Gazebo callbacks only replace one complete synchronized candidate;
they never write to stdout or wait for the host pipe. Diagnostics use stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from gazebo_depth_wire import ROTOR_VELOCITY_SLOWDOWN, build_depth_wire_record
from gazebo_segmentation_wire import build_segmentation_wire_record, normalize_labels_map
from gazebo_sensor_stream import (
    DEPTH_MAX_SOURCE_SKEW_NS,
    MAX_DEPTH_HISTORY_SAMPLES,
    MAX_SOURCE_SKEW_NS,
    WIRE_HEADER_BYTES,
    CausalSourceBuffer,
    _normalize_depth_float32,
    _odometry_from_message,
    _parse_depth_contract,
    _parse_expected_camera_calibration,
    _shutdown_transport_node,
    _stamp_ns,
    _validate_depth_contract_matches_rgb,
    _validate_navsat,
    build_wire_frame,
    normalize_bgr24,
    unpack_header,
    validate_camera_info_semantics,
)

MAX_CONSECUTIVE_ODOMETRY_VELOCITY_REJECTIONS = 3
ODOMETRY_VELOCITY_BOUND_ERROR = "wire Odometry velocity exceeds the safety bound"
MAX_JOINT_STATE_SKEW_NS = 100_000_000
ROTOR_JOINT_NAMES = tuple(f"rotor_{index}_joint" for index in range(4))
GIMBAL_JOINT_NAMES = {
    "pitch": "cgo3_camera_joint",
    "yaw": "cgo3_vertical_arm_joint",
    "roll": "cgo3_horizontal_arm_joint",
}


class OdometrySamplePolicy:
    """Drop isolated velocity spikes while failing closed on a persistent stream fault."""

    def __init__(self, max_consecutive_rejections: int = MAX_CONSECUTIVE_ODOMETRY_VELOCITY_REJECTIONS) -> None:
        if max_consecutive_rejections < 1:
            raise ValueError("max_consecutive_rejections must be positive")
        self.max_consecutive_rejections = max_consecutive_rejections
        self.consecutive_rejections = 0
        self.velocity_rejections = 0
        self.max_observed_consecutive_rejections = 0

    def accepted(self) -> None:
        self.consecutive_rejections = 0

    def reject(self, error: Exception) -> str:
        """Return ``drop`` for a recoverable velocity spike or ``fail`` otherwise."""
        if str(error) != ODOMETRY_VELOCITY_BOUND_ERROR:
            return "fail"
        self.velocity_rejections += 1
        self.consecutive_rejections += 1
        self.max_observed_consecutive_rejections = max(
            self.max_observed_consecutive_rejections,
            self.consecutive_rejections,
        )
        return (
            "fail"
            if self.consecutive_rejections >= self.max_consecutive_rejections
            else "drop"
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    stamp_ns: int
    width: int
    height: int
    bgr: bytes
    camera_stamp_ns: int
    navsat: dict[str, Any]
    odometry: dict[str, Any]
    depth_stamp_ns: int
    depth: bytes
    depth_width: int
    depth_height: int
    segmentation: bytes | None = None
    segmentation_topic: str | None = None
    joint_state: dict[str, Any] | None = None


class ExactSegmentationPairer:
    """Bounded timestamp join; accept either callback order without stale reuse.

    Callers serialize access with the image callback lock. Unpaired RGB and
    labels maps are discarded as the bounded pending histories advance.
    """

    def __init__(self, latest: LatestCandidate, maximum_samples: int = 8) -> None:
        self.latest = latest
        self.maximum_samples = maximum_samples
        self.rgb: dict[int, Candidate] = {}
        self.seg: dict[int, tuple[int, int, bytes, str]] = {}
        self.last_published_stamp = -1

    def put_rgb(self, candidate: Candidate) -> None:
        self.rgb[candidate.stamp_ns] = candidate
        self._join(candidate.stamp_ns)

    def put_seg(self, stamp: int, width: int, height: int, payload: bytes, topic: str) -> None:
        self.seg[stamp] = (width, height, payload, topic)
        self._join(stamp)

    def _join(self, stamp: int) -> None:
        from dataclasses import replace

        if stamp in self.rgb and stamp in self.seg:
            candidate = self.rgb.pop(stamp)
            width, height, payload, topic = self.seg.pop(stamp)
            if (width, height) != (candidate.width, candidate.height):
                raise ValueError("segmentation geometry differs from RGB")
            if stamp > self.last_published_stamp:
                self.latest.put(replace(candidate, segmentation=payload, segmentation_topic=topic))
                self.last_published_stamp = stamp
        for history in (self.rgb, self.seg):
            for old in sorted(history):
                if old <= self.last_published_stamp or len(history) > self.maximum_samples:
                    del history[old]


class LatestCandidate:
    """One complete RGB/depth sample; replacement never blocks callbacks."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.latest: Candidate | None = None
        self.received = 0
        self.dropped = 0
        self.error: str | None = None

    def put(self, candidate: Candidate) -> None:
        with self.condition:
            if self.latest is not None:
                self.dropped += 1
            self.latest = candidate
            self.received += 1
            self.condition.notify_all()

    def fail(self, error: str) -> None:
        with self.condition:
            if self.error is None:
                self.error = error
            self.condition.notify_all()

    def wait_after(self, stamp_ns: int, timeout_s: float) -> Candidate:
        deadline = time.monotonic() + timeout_s
        with self.condition:
            while True:
                if self.error is not None:
                    raise RuntimeError(self.error)
                if self.latest is not None and self.latest.stamp_ns > stamp_ns:
                    return self.latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no synchronized frame newer than {stamp_ns}")
                self.condition.wait(remaining)


def _size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0 or width > 4096 or height > 4096:
        raise argparse.ArgumentTypeError("size is outside supported range")
    return width, height


def scale_bgr24(payload: bytes, width: int, height: int, size: tuple[int, int]) -> bytes:
    """Nearest-neighbour BGR resize without a container NumPy dependency."""
    out_width, out_height = size
    if (width, height) == size:
        return payload
    if out_width > width or out_height > height:
        raise ValueError("RGB upscaling is not supported")
    output = bytearray(out_width * out_height * 3)
    for y in range(out_height):
        source_row = (y * height // out_height) * width * 3
        target_row = y * out_width * 3
        for x in range(out_width):
            source = source_row + (x * width // out_width) * 3
            target = target_row + x * 3
            output[target : target + 3] = payload[source : source + 3]
    return bytes(output)


def scale_depth(
    payload: bytes,
    width: int,
    height: int,
    size: tuple[int, int],
    encoding: str,
) -> bytes:
    """Nearest-neighbour float32 depth; UINT16_MM uses zero for no return."""
    import struct

    out_width, out_height = size
    if out_width > width or out_height > height:
        raise ValueError("depth upscaling is not supported")
    if (width, height) == size and encoding == "FLOAT32_M":
        return payload
    output = bytearray(out_width * out_height * (2 if encoding == "UINT16_MM" else 4))
    for y in range(out_height):
        for x in range(out_width):
            source = ((y * height // out_height) * width + x * width // out_width) * 4
            value = struct.unpack_from("<f", payload, source)[0]
            target = (y * out_width + x) * (2 if encoding == "UINT16_MM" else 4)
            if encoding == "UINT16_MM":
                millimetres = round(value * 1000) if math.isfinite(value) and value > 0 else 0
                struct.pack_into("<H", output, target, min(65535, millimetres))
            else:
                struct.pack_into("<f", output, target, value)
    return bytes(output)


def encode_candidate(
    candidate: Candidate,
    sequence: int,
    depth_contract: dict[str, Any],
    rgb_size: tuple[int, int],
    depth_size: tuple[int, int],
    depth_encoding: str,
) -> bytes:
    rgb = scale_bgr24(candidate.bgr, candidate.width, candidate.height, rgb_size)
    navsat = candidate.navsat
    odometry = candidate.odometry
    rgb_record = build_wire_frame(
        sequence=sequence,
        sensor_stamp_ns=candidate.stamp_ns,
        width=rgb_size[0],
        height=rgb_size[1],
        camera_info_stamp_ns=candidate.camera_stamp_ns,
        navsat_stamp_ns=int(navsat["stamp_ns"]),
        odometry_stamp_ns=int(odometry["stamp_ns"]),
        latitude_deg=float(navsat["latitude_deg"]),
        longitude_deg=float(navsat["longitude_deg"]),
        altitude_m=float(navsat["altitude_m"]),
        position_east_m=float(odometry["position_east_m"]),
        position_north_m=float(odometry["position_north_m"]),
        position_up_m=float(odometry["position_up_m"]),
        orientation_x=float(odometry["orientation_x"]),
        orientation_y=float(odometry["orientation_y"]),
        orientation_z=float(odometry["orientation_z"]),
        orientation_w=float(odometry["orientation_w"]),
        velocity_forward_mps=float(odometry["velocity_forward_mps"]),
        velocity_left_mps=float(odometry["velocity_left_mps"]),
        velocity_up_mps=float(odometry["velocity_up_mps"]),
        attitude_covariance_enu_rad2=tuple(odometry["attitude_covariance_enu_rad2"]),
        bgr_payload=rgb,
    )
    rgb_header = unpack_header(rgb_record[:WIRE_HEADER_BYTES])
    depth_payload = scale_depth(
        candidate.depth,
        candidate.depth_width,
        candidate.depth_height,
        depth_size,
        depth_encoding,
    )
    output_maximum = min(float(depth_contract["maximum_depth_m"]), 65.535) if depth_encoding == "UINT16_MM" else float(depth_contract["maximum_depth_m"])
    metadata = {
        "sequence": sequence,
        "rgb_sensor_stamp_ns": candidate.stamp_ns,
        "rgb_record_sha256": rgb_header.sha256.hex(),
        "source_stamp_ns": candidate.depth_stamp_ns,
        "source_topic": depth_contract["source_topic"],
        "calibration_id": depth_contract["calibration_id"],
        "calibration_sha256": depth_contract["calibration_sha256"],
        "registration_mode": depth_contract["registration_mode"],
        "coordinate_space": depth_contract["coordinate_space"],
        "encoding": depth_encoding,
        "distance_semantics": depth_contract["distance_semantics"],
        "width": depth_size[0],
        "height": depth_size[1],
        "stride_bytes": depth_size[0] * (2 if depth_encoding == "UINT16_MM" else 4),
        "minimum_depth_m": depth_contract["minimum_depth_m"],
        "maximum_depth_m": output_maximum,
        "fx_px": float(depth_contract["fx_px"]) * depth_size[0] / candidate.depth_width,
        "fy_px": float(depth_contract["fy_px"]) * depth_size[1] / candidate.depth_height,
        "cx_px": float(depth_contract["cx_px"]) * depth_size[0] / candidate.depth_width,
        "cy_px": float(depth_contract["cy_px"]) * depth_size[1] / candidate.depth_height,
    }
    if candidate.joint_state is not None:
        metadata["joint_state"] = candidate.joint_state
    output = rgb_record + build_depth_wire_record(metadata=metadata, payload=depth_payload)
    if candidate.segmentation is not None:
        segmentation_payload = scale_bgr24(
            candidate.segmentation, candidate.width, candidate.height, rgb_size
        )
        output += build_segmentation_wire_record(
            {"sequence": sequence, "rgb_sensor_stamp_ns": candidate.stamp_ns,
             "source_stamp_ns": candidate.stamp_ns, "rgb_record_sha256": rgb_header.sha256.hex(),
             "source_topic": candidate.segmentation_topic, "encoding": "PANOPTIC_RGB8",
             "width": rgb_size[0], "height": rgb_size[1],
             "instance_id_channels": [0, 1], "label_id_channel": 2},
            segmentation_payload,
        )
    return output


def _joint_sample_from_message(message: Any) -> dict[str, Any]:
    """Normalize a Gazebo JointStatePublisher Model message."""
    stamp_ns = _stamp_ns(message)
    joints: dict[str, dict[str, float]] = {}
    for joint in getattr(message, "joint", ()):
        name = str(getattr(joint, "name", ""))
        axis = getattr(joint, "axis1", None)
        if not name or axis is None:
            continue
        position = float(getattr(axis, "position", math.nan))
        velocity = float(getattr(axis, "velocity", math.nan))
        if math.isfinite(position) and math.isfinite(velocity):
            joints[name] = {"position_rad": position, "velocity_rad_s": velocity}
    if not joints:
        raise ValueError("joint state message contains no finite axis-1 samples")
    return {"stamp_ns": stamp_ns, "joints": joints}


def _frame_joint_state(sample: dict[str, Any] | None, frame_stamp_ns: int) -> dict[str, Any] | None:
    """Bind the newest causal actuator sample to one RGB timestamp."""
    if sample is None:
        return None
    source_stamp_ns = int(sample["stamp_ns"])
    skew_ns = int(frame_stamp_ns) - source_stamp_ns
    if skew_ns < 0 or skew_ns > MAX_JOINT_STATE_SKEW_NS:
        return None
    joints = sample["joints"]
    result: dict[str, Any] = {
        "timestamp_ns": int(frame_stamp_ns),
        "source_timestamp_ns": source_stamp_ns,
        "source_skew_ns": skew_ns,
        "gimbal_joint_state": None,
        "propeller_joint_state": None,
    }
    if all(name in joints for name in ROTOR_JOINT_NAMES):
        rotor_samples = [joints[name] for name in ROTOR_JOINT_NAMES]
        result["propeller_joint_state"] = {
            "timestamp_ns": int(frame_stamp_ns),
            "joint_names": list(ROTOR_JOINT_NAMES),
            "angles_rad": [float(value["position_rad"]) for value in rotor_samples],
            "spin_rad_s": [float(value["velocity_rad_s"]) for value in rotor_samples],
            "velocity_slowdown": ROTOR_VELOCITY_SLOWDOWN,
            "source": "gazebo_joint_state",
        }
    if all(name in joints for name in GIMBAL_JOINT_NAMES.values()):
        result["gimbal_joint_state"] = {
            "timestamp_ns": int(frame_stamp_ns),
            "position_rad": {
                axis: float(joints[name]["position_rad"])
                for axis, name in GIMBAL_JOINT_NAMES.items()
            },
            "velocity_rad_s": {
                axis: float(joints[name]["velocity_rad_s"])
                for axis, name in GIMBAL_JOINT_NAMES.items()
            },
            "source": "gazebo_joint_state",
            "fresh": True,
        }
    if result["gimbal_joint_state"] is None and result["propeller_joint_state"] is None:
        return None
    return result


def _diagnostic(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", default="default")
    parser.add_argument("--model-instance", default="x500_mono_cam_0")
    parser.add_argument("--image-topic")
    parser.add_argument("--camera-info-topic")
    parser.add_argument("--navsat-topic")
    parser.add_argument("--odometry-topic")
    parser.add_argument("--segmentation", action="store_true")
    parser.add_argument("--segmentation-topic", default="/multidetect/teacher/segmentation/labels_map")
    parser.add_argument("--expected-camera-calibration-json", required=True)
    parser.add_argument("--depth-contract-json", required=True)
    parser.add_argument("--rgb-size", type=_size, default=(640, 480))
    parser.add_argument("--depth-size", type=_size, default=(320, 240))
    parser.add_argument("--depth-encoding", choices=("UINT16_MM", "FLOAT32_M"), default="UINT16_MM")
    parser.add_argument("--request-timeout-s", type=float, default=15.0)
    # Large worlds load meshes and textures for a minute before the first render.
    parser.add_argument("--first-frame-timeout-s", type=float, default=None)
    args = parser.parse_args()
    expected = _parse_expected_camera_calibration(args.expected_camera_calibration_json)
    source_contract = _parse_depth_contract(
        args.depth_contract_json, world=args.world, model=args.model_instance
    )
    _validate_depth_contract_matches_rgb(source_contract, expected)
    if source_contract is None:
        raise SystemExit("depth contract is required for SIM teacher frames")
    contract = dict(source_contract)
    contract["output_width"] = args.depth_size[0]
    contract["output_height"] = args.depth_size[1]
    if not math.isfinite(args.request_timeout_s) or args.request_timeout_s <= 0:
        raise SystemExit("--request-timeout-s must be positive")
    if args.depth_encoding == "UINT16_MM" and float(contract["maximum_depth_m"]) > 65.535:
        # UINT16_MM saturates beyond 65.535 m; finite nearer values remain exact to 1 mm.
        _diagnostic("sim_teacher_depth_saturation", maximum_m=65.535)

    from gz.msgs10.camera_info_pb2 import CameraInfo
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.model_pb2 import Model
    from gz.msgs10.navsat_pb2 import NavSat
    from gz.msgs10.odometry_with_covariance_pb2 import OdometryWithCovariance
    from gz.transport13 import Node

    world, model = args.world, args.model_instance
    image_topic = (
        args.image_topic or f"/world/{world}/model/{model}/link/camera_link/sensor/camera/image"
    )
    camera_topic = (
        args.camera_info_topic
        or f"/world/{world}/model/{model}/link/camera_link/sensor/camera/camera_info"
    )
    navsat_topic = (
        args.navsat_topic
        or f"/world/{world}/model/{model}/link/base_link/sensor/navsat_sensor/navsat"
    )
    odometry_topic = args.odometry_topic or f"/model/{model}/odometry_with_covariance"
    depth_topic = str(contract["source_topic"])
    depth_info_topic = str(contract["depth_camera_info_topic"])
    joint_state_topic = f"/world/{world}/model/{model}/joint_state"
    buffers = {
        "camera": CausalSourceBuffer("CameraInfo"),
        "navsat": CausalSourceBuffer("NavSat"),
        "odometry": CausalSourceBuffer("Odometry"),
        "depth_info": CausalSourceBuffer(
            "DepthCameraInfo", maximum_samples=MAX_DEPTH_HISTORY_SAMPLES
        ),
        "depth": CausalSourceBuffer("DepthImage", maximum_samples=MAX_DEPTH_HISTORY_SAMPLES),
    }
    joint_states = CausalSourceBuffer("JointStatePublisher", maximum_samples=128)
    state_lock = threading.Lock()
    image_lock = threading.Lock()
    latest = LatestCandidate()
    pairer = ExactSegmentationPairer(latest) if args.segmentation else None
    stats = {
        "images": 0,
        "incomplete": 0,
        "stale_sources": 0,
        "depth_missing": 0,
        "odometry_invalid": 0,
        "odometry_velocity_rejected": 0,
        "odometry_max_consecutive_velocity_rejections": 0,
        "joint_state_invalid": 0,
        "joint_state_frames": 0,
        "joint_state_missing_or_stale": 0,
    }
    odometry_policy = OdometrySamplePolicy()

    def guard(name: str, operation: Any) -> None:
        try:
            operation()
        except Exception as exc:
            latest.fail(f"{name} rejected: {exc}")

    def on_camera(message: CameraInfo) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            validation = validate_camera_info_semantics(message, **expected)
            with state_lock:
                buffers["camera"].append(
                    {
                        "stamp_ns": stamp,
                        "width": validation["width"],
                        "height": validation["height"],
                    }
                )

        guard("CameraInfo", update)

    def on_depth_info(message: CameraInfo) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            validation = validate_camera_info_semantics(
                message,
                expected_width=int(contract["width"]),
                expected_height=int(contract["height"]),
                expected_fx_px=float(contract["fx_px"]),
                expected_fy_px=float(contract["fy_px"]),
                expected_cx_px=float(contract["cx_px"]),
                expected_cy_px=float(contract["cy_px"]),
                expected_distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
            )
            with state_lock:
                buffers["depth_info"].append(
                    {
                        "stamp_ns": stamp,
                        "width": validation["width"],
                        "height": validation["height"],
                    }
                )

        guard("Depth CameraInfo", update)

    def on_navsat(message: NavSat) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            latitude, longitude, altitude = _validate_navsat(
                message.latitude_deg, message.longitude_deg, message.altitude
            )
            with state_lock:
                buffers["navsat"].append(
                    {
                        "stamp_ns": stamp,
                        "latitude_deg": latitude,
                        "longitude_deg": longitude,
                        "altitude_m": altitude,
                    }
                )

        guard("NavSat", update)

    def on_odometry(message: OdometryWithCovariance) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            try:
                fields = _odometry_from_message(message)
            except Exception as exc:
                with state_lock:
                    stats["odometry_invalid"] += 1
                    action = odometry_policy.reject(exc)
                    stats["odometry_velocity_rejected"] = odometry_policy.velocity_rejections
                    stats[
                        "odometry_max_consecutive_velocity_rejections"
                    ] = odometry_policy.max_observed_consecutive_rejections
                if action == "drop":
                    _diagnostic(
                        "sim_teacher_odometry_dropped",
                        stamp_ns=stamp,
                        reason=str(exc),
                        consecutive_rejections=odometry_policy.consecutive_rejections,
                    )
                    return
                raise
            names = (
                "position_east_m",
                "position_north_m",
                "position_up_m",
                "orientation_x",
                "orientation_y",
                "orientation_z",
                "orientation_w",
                "velocity_forward_mps",
                "velocity_left_mps",
                "velocity_up_mps",
            )
            row = dict(zip(names, fields[:10], strict=True))
            row["attitude_covariance_enu_rad2"] = fields[10:]
            row["stamp_ns"] = stamp
            with state_lock:
                odometry_policy.accepted()
                buffers["odometry"].append(row)

        guard("Odometry", update)

    def on_depth(message: Image) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            payload = _normalize_depth_float32(message, contract=contract)
            with state_lock:
                buffers["depth"].append({"stamp_ns": stamp, "payload": payload})

        guard("Depth Image", update)

    def on_joint_state(message: Model) -> None:
        try:
            sample = _joint_sample_from_message(message)
            with state_lock:
                joint_states.append(sample)
        except Exception as exc:
            with state_lock:
                stats["joint_state_invalid"] += 1
            _diagnostic("sim_teacher_joint_state_dropped", reason=str(exc))

    def on_image(message: Image) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            with image_lock:
                with state_lock:
                    stats["images"] += 1
                    selected = {
                        name: buffer.latest_at_or_before(stamp) for name, buffer in buffers.items()
                    }
                    if any(item is None for item in selected.values()):
                        stats["incomplete"] += 1
                        return
                    camera = selected["camera"]
                    navsat = selected["navsat"]
                    odometry = selected["odometry"]
                    depth = selected["depth"]
                    depth_info = buffers["depth_info"].latest_at_or_before(int(depth["stamp_ns"]))
                    if depth_info is None:
                        stats["depth_missing"] += 1
                        return
                    if any(
                        stamp - int(selected[name]["stamp_ns"]) > MAX_SOURCE_SKEW_NS
                        for name in ("navsat", "odometry")
                    ):
                        stats["stale_sources"] += 1
                        return
                    if stamp - int(depth["stamp_ns"]) > DEPTH_MAX_SOURCE_SKEW_NS:
                        stats["depth_missing"] += 1
                        return
                    if (int(camera["width"]), int(camera["height"])) != (
                        int(message.width),
                        int(message.height),
                    ):
                        raise ValueError("RGB image geometry differs from CameraInfo")
                    joint_state = _frame_joint_state(
                        joint_states.latest_at_or_before(stamp), stamp
                    )
                    if joint_state is None:
                        stats["joint_state_missing_or_stale"] += 1
                    else:
                        stats["joint_state_frames"] += 1
                    copied = (dict(camera), dict(navsat), dict(odometry), dict(depth))
                bgr = normalize_bgr24(
                    message.data,
                    width=int(message.width),
                    height=int(message.height),
                    step=int(message.step),
                    pixel_format_type=int(message.pixel_format_type),
                )
                camera, navsat, odometry, depth = copied
                (pairer.put_rgb if pairer is not None else latest.put)(
                    Candidate(
                        stamp,
                        int(message.width),
                        int(message.height),
                        bgr,
                        int(camera["stamp_ns"]),
                        navsat,
                        odometry,
                        int(depth["stamp_ns"]),
                        bytes(depth["payload"]),
                        int(contract["width"]),
                        int(contract["height"]),
                        joint_state=joint_state,
                    )
                )

        guard("RGB Image", update)

    def on_segmentation(message: Image) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            payload = normalize_labels_map(message)
            with image_lock:
                assert pairer is not None
                pairer.put_seg(stamp, int(message.width), int(message.height), payload,
                               args.segmentation_topic)

        guard("Segmentation Image", update)

    node = Node()
    subscriptions = [
        (CameraInfo, depth_info_topic, on_depth_info),
        (Image, depth_topic, on_depth),
        (Image, image_topic, on_image),
        (CameraInfo, camera_topic, on_camera),
        (NavSat, navsat_topic, on_navsat),
        (OdometryWithCovariance, odometry_topic, on_odometry),
        (Model, joint_state_topic, on_joint_state),
    ]
    if args.segmentation:
        subscriptions.append((Image, args.segmentation_topic, on_segmentation))
    topics = tuple(topic for _, topic, _ in subscriptions)
    for message_type, topic, callback in subscriptions:
        if not node.subscribe(message_type, topic, callback):
            _diagnostic("sim_teacher_frame_subscription_failed", topic=topic)
            _shutdown_transport_node(node, topics, drain_lock=image_lock)
            return 2
    _diagnostic(
        "sim_teacher_frame_server_started",
        image_topic=image_topic,
        depth_topic=depth_topic,
        joint_state_topic=joint_state_topic,
        rgb_size=args.rgb_size,
        depth_size=args.depth_size,
        depth_encoding=args.depth_encoding,
    )
    sequence = 0
    last_emitted_stamp = -1
    try:
        for line in sys.stdin.buffer:
            parts = line.decode("ascii").strip().split()
            if len(parts) != 2 or parts[0] != "GET" or not parts[1].isdecimal():
                raise ValueError("expected GET <non-negative simulation nanoseconds>")
            requested_stamp = int(parts[1])
            candidate = latest.wait_after(
                max(last_emitted_stamp, requested_stamp - 1),
                args.first_frame_timeout_s
                if args.first_frame_timeout_s and last_emitted_stamp < 0
                else args.request_timeout_s,
            )
            packet = encode_candidate(
                candidate, sequence, contract, args.rgb_size, args.depth_size, args.depth_encoding
            )
            sys.stdout.buffer.write(packet)
            sys.stdout.buffer.flush()
            last_emitted_stamp = candidate.stamp_ns
            sequence += 1
    except (BrokenPipeError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        latest.fail(str(exc))
    finally:
        teardown = _shutdown_transport_node(node, topics, drain_lock=image_lock)
        _diagnostic(
            "sim_teacher_frame_server_finished",
            emitted=sequence,
            synchronized=latest.received,
            dropped=latest.dropped,
            rejected=stats,
            joint_state_topic=joint_state_topic,
            error=latest.error,
            teardown_errors=teardown,
        )
    return 0 if latest.error is None and not teardown else 2


if __name__ == "__main__":
    raise SystemExit(main())

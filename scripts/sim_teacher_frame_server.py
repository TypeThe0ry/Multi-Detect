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

from gazebo_depth_wire import build_depth_wire_record
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
    return rgb_record + build_depth_wire_record(metadata=metadata, payload=depth_payload)


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
    parser.add_argument("--expected-camera-calibration-json", required=True)
    parser.add_argument("--depth-contract-json", required=True)
    parser.add_argument("--rgb-size", type=_size, default=(640, 480))
    parser.add_argument("--depth-size", type=_size, default=(320, 240))
    parser.add_argument("--depth-encoding", choices=("UINT16_MM", "FLOAT32_M"), default="UINT16_MM")
    parser.add_argument("--request-timeout-s", type=float, default=15.0)
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
    buffers = {
        "camera": CausalSourceBuffer("CameraInfo"),
        "navsat": CausalSourceBuffer("NavSat"),
        "odometry": CausalSourceBuffer("Odometry"),
        "depth_info": CausalSourceBuffer(
            "DepthCameraInfo", maximum_samples=MAX_DEPTH_HISTORY_SAMPLES
        ),
        "depth": CausalSourceBuffer("DepthImage", maximum_samples=MAX_DEPTH_HISTORY_SAMPLES),
    }
    state_lock = threading.Lock()
    image_lock = threading.Lock()
    latest = LatestCandidate()
    stats = {"images": 0, "incomplete": 0, "stale_sources": 0, "depth_missing": 0}

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
            fields = _odometry_from_message(message)
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
                buffers["odometry"].append(row)

        guard("Odometry", update)

    def on_depth(message: Image) -> None:
        def update() -> None:
            stamp = _stamp_ns(message)
            payload = _normalize_depth_float32(message, contract=contract)
            with state_lock:
                buffers["depth"].append({"stamp_ns": stamp, "payload": payload})

        guard("Depth Image", update)

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
                    copied = (dict(camera), dict(navsat), dict(odometry), dict(depth))
                bgr = normalize_bgr24(
                    message.data,
                    width=int(message.width),
                    height=int(message.height),
                    step=int(message.step),
                    pixel_format_type=int(message.pixel_format_type),
                )
                camera, navsat, odometry, depth = copied
                latest.put(
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
                    )
                )

        guard("RGB Image", update)

    node = Node()
    subscriptions = [
        (CameraInfo, depth_info_topic, on_depth_info),
        (Image, depth_topic, on_depth),
        (Image, image_topic, on_image),
        (CameraInfo, camera_topic, on_camera),
        (NavSat, navsat_topic, on_navsat),
        (OdometryWithCovariance, odometry_topic, on_odometry),
    ]
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
                max(last_emitted_stamp, requested_stamp - 1), args.request_timeout_s
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
            error=latest.error,
            teardown_errors=teardown,
        )
    return 0 if latest.error is None and not teardown else 2


if __name__ == "__main__":
    raise SystemExit(main())

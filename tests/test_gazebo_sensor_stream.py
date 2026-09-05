from __future__ import annotations

import hashlib
import io
import json
import math
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

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
    _shutdown_transport_node,
    build_wire_frame,
    decode_wire_blob,
    normalize_bgr24,
    pack_header,
    parse_wire_frame,
    read_wire_frame,
    unpack_header,
)
from scripts.gazebo_sensor_stream import main as sensor_stream_main


def _frame(
    *,
    sequence: int = 0,
    stamp_ns: int = 10,
    odometry_stamp_ns: int = 4,
    payload: bytes | None = None,
    orientation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
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
    sec, nsec = divmod(stamp_ns, 1_000_000_000)
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nsec=nsec)),
        **fields,
    )


def _run_fake_gazebo_helper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    odometry_behavior: str,
    topics: tuple[str, str, str, str] | None = None,
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
                elif odometry_behavior in {"valid", "causal_history", "future_sources"}:
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
                    self._emit_image()
            return True

        def unsubscribe(self, topic: str) -> bool:
            self.unsubscribed_topics.append(topic)
            return True

        def _emit_image(self) -> None:
            callback = self.callbacks["Image"]
            callback(  # type: ignore[operator]
                _gazebo_message(
                    stamp_ns=2_000_000_000,
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
    argv = ["gazebo_sensor_stream.py", "--frames", "1", "--timeout-seconds", "0.5"]
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
    assert WIRE_MAGIC == b"MDGZSIM2"
    assert WIRE_VERSION == 2
    assert WIRE_HEADER_BYTES == WIRE_HEADER.size == 204
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
    assert header.flags & (FLAG_CAMERA_INFO | FLAG_NAVSAT | FLAG_ODOMETRY) == 7
    assert (header.flags & PIXEL_FORMAT_MASK) >> PIXEL_FORMAT_SHIFT == PIXEL_FORMAT_BGR24
    digest_input = packet[: WIRE_HEADER_BYTES - 32] + (b"\x00" * 32)
    assert header.sha256 == hashlib.sha256(digest_input + packet[WIRE_HEADER_BYTES:]).digest()
    assert pack_header(header) == packet[:WIRE_HEADER_BYTES]


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

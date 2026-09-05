from __future__ import annotations

import hashlib
import io
import json
import struct
from collections import deque
from types import SimpleNamespace

import pytest

from multidetect.clock import ManualClock
from multidetect.operator_link import (
    RuntimeBackendHealth,
    RuntimeMode,
    RuntimeStatusMessage,
    VideoGeometry,
)
from multidetect.operator_protocol import RuntimeStatusAck
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig
from multidetect.operator_udp import (
    UdpRuntimeStatusAckReceipt,
    UdpRuntimeStatusSendReceipt,
)
from multidetect.qgc_replay import (
    REPLAY_RTSP_EVENT_SCHEMA,
    REPLAY_RTSP_IPC_HEADER_BYTES,
    REPLAY_RTSP_IPC_MAGIC,
    REPLAY_RTSP_IPC_VERSION,
    REPLAY_RTSP_MESSAGE_EOS,
    REPLAY_RTSP_MESSAGE_FRAME,
    FramePacer,
    LiveFramePacer,
    ReplayRtspFrameAck,
    ReplayRtspFrameWriterAdapter,
    ReplayRtspPublisherStats,
    ReplayVideoGeometry,
    SubprocessReplayRtspPublisher,
    SynchronizedQgcOperatorBridge,
    SynchronizedReplayOperatorBridge,
    WallClockReplayPacer,
    recording_video_geometry,
)


class _FakeWallClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wall_pacer_never_advances_replay_manual_clock() -> None:
    wall = _FakeWallClock()
    replay_clock = ManualClock(initial_monotonic_s=7.0)
    pacer = WallClockReplayPacer(
        playback_rate=2.0,
        wall_monotonic=wall.monotonic,
        sleep=wall.sleep,
    )

    pacer.pace(20.0)
    pacer.pace(22.0)

    assert wall.sleeps == [1.0]
    assert replay_clock.monotonic() == 7.0
    assert pacer.stats.paced_frame_count == 2
    assert pacer.stats.slept_seconds == 1.0


def test_live_frame_pacer_validates_time_without_sleeping(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "multidetect.qgc_replay.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    pacer: FramePacer = LiveFramePacer()

    pacer.pace(10.0)
    pacer.pace(10.025)

    assert sleep_calls == []
    assert pacer.stats.paced_frame_count == 2
    assert pacer.stats.slept_seconds == 0.0
    assert pacer.stats.maximum_lag_seconds == 0.0
    assert pacer.stats.playback_rate == 1.0


@pytest.mark.parametrize("captured_at_s", [float("nan"), float("inf"), float("-inf")])
def test_live_frame_pacer_rejects_non_finite_time(captured_at_s: float) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        LiveFramePacer().pace(captured_at_s)


@pytest.mark.parametrize("captured_at_s", [2.0, 1.5])
def test_live_frame_pacer_requires_strictly_increasing_time(captured_at_s: float) -> None:
    pacer = LiveFramePacer()
    pacer.pace(2.0)

    with pytest.raises(ValueError, match="strictly increasing"):
        pacer.pace(captured_at_s)


def test_ipc_v1_writer_emits_complete_bgr_frame_ack_and_strict_eos() -> None:
    np = pytest.importorskip("numpy")
    image = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    geometry = ReplayVideoGeometry.bgr24(3, 2)
    events = deque(
        (
            {
                "event": "frame_accepted",
                "sequence": 0,
                "captured_at_ns": 1_250_000_000,
                "payload_bytes": 18,
            },
            {
                "event": "summary",
                "frame_count": 1,
                "bytes": 18,
                "first_captured_at_ns": 1_250_000_000,
                "last_captured_at_ns": 1_250_000_000,
            },
        )
    )
    observed_events: list[str] = []

    def wait_for_event(expected: str, timeout_s: float):
        assert timeout_s == 0.5
        observed_events.append(expected)
        event = events.popleft()
        assert event["event"] == expected
        return event

    ticks = iter((1_000_000_000, 1_004_000_000))
    output = io.BytesIO()
    writer = ReplayRtspFrameWriterAdapter(
        output,
        geometry=geometry,
        wait_for_event=wait_for_event,
        acknowledgement_timeout_s=0.5,
        monotonic_ns=lambda: next(ticks),
    )

    acknowledgement = writer.write_frame(image, captured_at_s=1.25)
    writer.write_eos()

    assert acknowledgement == ReplayRtspFrameAck(
        sequence=0,
        captured_at_ns=1_250_000_000,
        payload_bytes=18,
        acknowledged_at_monotonic_ns=1_004_000_000,
    )
    assert observed_events == ["frame_accepted", "summary"]
    payload = output.getvalue()
    assert len(payload) == REPLAY_RTSP_IPC_HEADER_BYTES + 18 + REPLAY_RTSP_IPC_HEADER_BYTES
    header = struct.unpack("<8sHHIQQIIIIQ32s", payload[:88])
    assert header[:4] == (
        REPLAY_RTSP_IPC_MAGIC,
        REPLAY_RTSP_IPC_VERSION,
        REPLAY_RTSP_MESSAGE_FRAME,
        REPLAY_RTSP_IPC_HEADER_BYTES,
    )
    assert header[4:11] == (0, 1_250_000_000, 3, 2, 9, 1, 18)
    assert header[11] == hashlib.sha256(image.tobytes()).digest()
    assert payload[88:106] == image.tobytes()
    eos = struct.unpack("<8sHHIQQIIIIQ32s", payload[106:194])
    assert eos[:6] == (
        REPLAY_RTSP_IPC_MAGIC,
        REPLAY_RTSP_IPC_VERSION,
        REPLAY_RTSP_MESSAGE_EOS,
        REPLAY_RTSP_IPC_HEADER_BYTES,
        1,
        1_250_000_000,
    )
    assert eos[6:11] == (3, 2, 9, 1, 0)
    assert eos[11] == b"\0" * 32
    assert writer.stats(helper_exit_code=0).summary_received is True


def test_ipc_writer_rejects_geometry_stride_and_timestamp_drift() -> None:
    np = pytest.importorskip("numpy")
    geometry = ReplayVideoGeometry.bgr24(3, 2)
    accepted = deque(
        (
            {
                "event": "frame_accepted",
                "sequence": 0,
                "captured_at_ns": 1_000_000_000,
                "payload_bytes": 18,
            },
        )
    )
    writer = ReplayRtspFrameWriterAdapter(
        io.BytesIO(),
        geometry=geometry,
        wait_for_event=lambda _event, _timeout: accepted.popleft(),
        acknowledgement_timeout_s=1.0,
        monotonic_ns=lambda: 1,
    )
    with pytest.raises(ValueError, match="shape"):
        writer.write_frame(np.zeros((2, 4, 3), dtype=np.uint8), captured_at_s=1.0)
    non_contiguous = np.zeros((2, 6, 3), dtype=np.uint8)[:, ::2, :]
    with pytest.raises(ValueError, match="C-contiguous"):
        writer.write_frame(non_contiguous, captured_at_s=1.0)
    writer.write_frame(np.zeros((2, 3, 3), dtype=np.uint8), captured_at_s=1.0)
    with pytest.raises(ValueError, match="strictly increasing"):
        writer.write_frame(np.zeros((2, 3, 3), dtype=np.uint8), captured_at_s=1.0)


class _CaptureStream(io.BytesIO):
    def close(self) -> None:
        self.was_closed = True


class _FakeProcess:
    def __init__(self, stdout_payload: bytes) -> None:
        self.stdin = _CaptureStream()
        self.stdout = io.BytesIO(stdout_payload)
        self.stderr = io.BytesIO()
        self.return_code = None

    def poll(self):
        return self.return_code

    def wait(self, timeout=None):
        assert timeout is not None
        self.return_code = 0
        return 0

    def terminate(self) -> None:
        self.return_code = -15

    def kill(self) -> None:
        self.return_code = -9


def _helper_event(event: str, **fields: object) -> bytes:
    return (
        json.dumps(
            {
                "schema": REPLAY_RTSP_EVENT_SCHEMA,
                "schema_version": 1,
                "version": 1,
                "event": event,
                **fields,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_subprocess_publisher_requires_ready_client_and_each_frame_ack(tmp_path) -> None:
    np = pytest.importorskip("numpy")
    helper = tmp_path / "MultiDetectReplayRtspPublisher.exe"
    helper.write_bytes(b"fixture")
    stdout_payload = b"".join(
        (
            _helper_event(
                "server_ready",
                bind="127.0.0.1",
                port=8554,
                mount="/multidetect-replay",
                url="rtsp://127.0.0.1:8554/multidetect-replay",
                width=4,
                height=2,
            ),
            _helper_event("client_ready", client_count=1),
            _helper_event(
                "frame_accepted",
                sequence=0,
                captured_at_ns=1_000_000_000,
                payload_bytes=24,
            ),
            _helper_event(
                "summary",
                frame_count=1,
                bytes=24,
                first_captured_at_ns=1_000_000_000,
                last_captured_at_ns=1_000_000_000,
            ),
        )
    )
    process = _FakeProcess(stdout_payload)
    launched: dict[str, object] = {}

    def popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return process

    ticks = iter((10, 20))
    publisher = SubprocessReplayRtspPublisher(
        helper,
        bind_host="127.0.0.1",
        port=8554,
        mount="/multidetect-replay",
        geometry=ReplayVideoGeometry.bgr24(4, 2),
        popen_factory=popen,
        monotonic_ns=lambda: next(ticks),
    )
    publisher.start()
    publisher.publish_frame(np.zeros((2, 4, 3), dtype=np.uint8), captured_at_s=1.0)
    publisher.close()

    assert launched["command"][0] == str(helper.resolve())
    assert "--client-timeout-ms" in launched["command"]
    assert launched["kwargs"]["shell"] is False
    assert publisher.stats.frame_count == 1
    assert publisher.stats.eos_sent is True
    assert publisher.stats.summary_received is True
    assert publisher.stats.helper_exit_code == 0
    assert process.stdin.getvalue().startswith(REPLAY_RTSP_IPC_MAGIC)


def test_subprocess_publisher_rejects_unversioned_ready_event(tmp_path) -> None:
    helper = tmp_path / "MultiDetectReplayRtspPublisher.exe"
    helper.write_bytes(b"fixture")
    process = _FakeProcess(b'{"event":"server_ready"}\n')
    publisher = SubprocessReplayRtspPublisher(
        helper,
        bind_host="127.0.0.1",
        port=8554,
        mount="/multidetect-replay",
        geometry=ReplayVideoGeometry.bgr24(4, 2),
        popen_factory=lambda *_args, **_kwargs: process,
    )
    with pytest.raises(RuntimeError, match="schema/version"):
        publisher.start()


def test_subprocess_publisher_preflights_h264_geometry_and_mount(tmp_path) -> None:
    helper = tmp_path / "MultiDetectReplayRtspPublisher.exe"
    helper.write_bytes(b"fixture")

    with pytest.raises(ValueError, match="must both be even"):
        SubprocessReplayRtspPublisher(
            helper,
            bind_host="127.0.0.1",
            port=8554,
            mount="/multidetect-replay",
            geometry=ReplayVideoGeometry.bgr24(3, 2),
        )
    for mount in ("/.", "/..", "/valid/../escape", "/trailing/"):
        with pytest.raises(ValueError, match="contained URL path"):
            SubprocessReplayRtspPublisher(
                helper,
                bind_host="127.0.0.1",
                port=8554,
                mount=mount,
                geometry=ReplayVideoGeometry.bgr24(4, 2),
            )

    publisher = SubprocessReplayRtspPublisher(
        helper,
        bind_host="127.0.0.1",
        port=8554,
        mount="/valid._~-mount",
        geometry=ReplayVideoGeometry.bgr24(4, 2),
    )
    assert publisher.rtsp_url == "rtsp://127.0.0.1:8554/valid._~-mount"


def test_recording_preflight_rejects_variable_geometry_and_nanosecond_collision() -> None:
    first = SimpleNamespace(
        captured_at_s=1.0,
        image=SimpleNamespace(width=4, height=2),
    )
    second = SimpleNamespace(
        captured_at_s=2.0,
        image=SimpleNamespace(width=6, height=2),
    )
    with pytest.raises(ValueError, match="fixed frame geometry"):
        recording_video_geometry(SimpleNamespace(frames=(first, second)))

    second.image.width = 4
    second.captured_at_s = 1.0 + 0.1e-9
    with pytest.raises(ValueError, match="nanosecond"):
        recording_video_geometry(SimpleNamespace(frames=(first, second)))

    first.image.width = 3
    second.image.width = 3
    second.captured_at_s = 2.0
    with pytest.raises(ValueError, match="must both be even"):
        recording_video_geometry(SimpleNamespace(frames=(first, second)))


class _Transport:
    def __init__(self, order: list[str], *, acknowledgement_mode: str = "valid") -> None:
        self.order = order
        self.peer = ("127.0.0.1", 14580)
        self.acknowledgement_mode = acknowledgement_mode
        self.started = False
        self.closed = False
        self.runtime_statuses = []

    def start_background(self) -> None:
        self.started = True

    def active_metadata_peer(self):
        return self.peer

    def active_heartbeat_peer(self):
        return self.peer

    def poll_selection(self):
        return None

    def poll_authorization_decision(self):
        return None

    def poll_approach_confirmation(self):
        return None

    def poll_payload_target_confirmation(self):
        return None

    def poll_error(self):
        return None

    def set_authorization_challenge(self, _status) -> None:
        return None

    def set_approach_challenge(self, _status) -> None:
        return None

    def set_payload_target_challenge(self, _status) -> None:
        return None

    def publish_runtime_status(self, status, *, peer) -> None:
        self.order.append("metadata")
        self.runtime_statuses.append((status, peer))

    def publish_runtime_status_for_acceptance(
        self,
        status,
        *,
        peer,
    ) -> UdpRuntimeStatusSendReceipt:
        self.publish_runtime_status(status, peer=peer)
        return UdpRuntimeStatusSendReceipt(
            acknowledged_runtime_sequence=status.sequence,
            run_hash64=status.run_hash64,
            remote=peer,
            sent_at_monotonic_ns=1_010_000_000,
        )

    def wait_runtime_status_ack(
        self,
        send_receipt: UdpRuntimeStatusSendReceipt,
        *,
        timeout_s: float,
    ) -> UdpRuntimeStatusAckReceipt:
        assert timeout_s == pytest.approx(1.0)
        if self.acknowledgement_mode == "timeout":
            raise TimeoutError("fixture withheld the QGC acceptance ACK")
        acknowledged_sequence = send_receipt.acknowledged_runtime_sequence
        run_hash64 = send_receipt.run_hash64
        remote = send_receipt.remote
        if self.acknowledgement_mode == "wrong_sequence":
            acknowledged_sequence += 1
        elif self.acknowledgement_mode == "wrong_run":
            run_hash64 += 1
        elif self.acknowledgement_mode == "wrong_peer":
            remote = ("127.0.0.1", remote[1] + 1)
        acknowledged_at_monotonic_ns = 1_015_000_000
        if self.acknowledgement_mode == "over_timeout":
            acknowledged_at_monotonic_ns = (
                send_receipt.sent_at_monotonic_ns + 1_000_000_001
            )
        self.order.append("acceptance_ack")
        return UdpRuntimeStatusAckReceipt(
            acknowledgement=RuntimeStatusAck(
                acknowledged_runtime_sequence=acknowledged_sequence,
                run_hash64=run_hash64,
            ),
            remote=remote,
            sent_at_monotonic_ns=send_receipt.sent_at_monotonic_ns,
            acknowledged_at_monotonic_ns=acknowledged_at_monotonic_ns,
        )

    def close(self) -> None:
        self.closed = True


class _Publisher:
    def __init__(self, order: list[str], *, ack_ns: int = 1_000_000_000) -> None:
        self.order = order
        self.ack_ns = ack_ns
        self.started = False
        self.closed = False
        self.frames = 0

    def start(self) -> None:
        self.started = True

    def publish_frame(self, _image_bgr, *, captured_at_s: float) -> ReplayRtspFrameAck:
        self.order.append("video")
        acknowledgement = ReplayRtspFrameAck(
            sequence=self.frames,
            captured_at_ns=round(captured_at_s * 1_000_000_000),
            payload_bytes=18,
            acknowledged_at_monotonic_ns=self.ack_ns,
        )
        self.frames += 1
        return acknowledgement

    def close(self) -> None:
        self.closed = True

    @property
    def stats(self) -> ReplayRtspPublisherStats:
        return ReplayRtspPublisherStats(
            frame_count=self.frames,
            payload_bytes=self.frames * 18,
            first_captured_at_ns=1_000_000_000 if self.frames else None,
            last_captured_at_ns=1_000_000_000 if self.frames else None,
            maximum_ack_latency_ms=1.0,
            eos_sent=self.closed,
            summary_received=self.closed,
            helper_exit_code=0 if self.closed else None,
        )


def _runtime_status(
    mode: RuntimeMode = RuntimeMode.REPLAY,
    *,
    sequence: int = 0,
    run_hash64: int = 2,
) -> RuntimeStatusMessage:
    return RuntimeStatusMessage(
        sequence=sequence,
        mode=mode,
        backend_health=RuntimeBackendHealth.HEALTHY,
        scenario_seed=None,
        scenario_hash64=None,
        airframe_profile_hash64=1,
        run_hash64=run_hash64,
        scenario_present=False,
        ground_truth_isolated=True,
        hardware_outputs_present=False,
        profile_valid=True,
        produced_at_s=1.0,
    )


def test_synchronized_bridge_acks_each_video_before_actual_signed_metadata() -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    transport = _Transport(order)
    publisher = _Publisher(order)
    wall = _FakeWallClock()
    bridge = SynchronizedReplayOperatorBridge(
        transport,
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=publisher,
        pacer=WallClockReplayPacer(
            wall_monotonic=wall.monotonic,
            sleep=wall.sleep,
        ),
        expected_frame_count=2,
        wall_monotonic=wall.monotonic,
        wall_monotonic_ns=lambda: 1_010_000_000,
        sleep=wall.sleep,
    )
    bridge.start()
    bridge.process_frame(
        tracks=(),
        frame_id="frame-1",
        captured_at_s=1.0,
        produced_at_s=1.0,
        runtime_status=_runtime_status(sequence=1),
        image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
    )
    bridge.process_frame(
        tracks=(),
        frame_id="frame-2",
        captured_at_s=2.0,
        produced_at_s=2.0,
        runtime_status=_runtime_status(sequence=2),
        image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
    )
    bridge.close()

    assert order == [
        "video",
        "metadata",
        "acceptance_ack",
        "video",
        "metadata",
        "acceptance_ack",
    ]
    assert len(transport.runtime_statuses) == 2
    assert transport.runtime_statuses[0][1] == ("127.0.0.1", 14580)
    evidence = bridge.require_complete()
    assert evidence.complete is True
    assert evidence.maximum_ack_to_metadata_skew_ms == 10.0
    assert evidence.metadata_send_count == 2
    assert evidence.metadata_acceptance_ack_count == 2
    assert evidence.metadata_barrier_count == 2
    assert evidence.maximum_metadata_acceptance_ack_latency_ms == 5.0
    assert evidence.maximum_frame_to_acceptance_latency_ms == 15.0
    assert evidence.acceptance_ack_peer_host == "127.0.0.1"
    assert evidence.acceptance_ack_peer_port == 14580
    assert evidence.first_acknowledged_runtime_sequence == 1
    assert evidence.last_acknowledged_runtime_sequence == 2
    assert evidence.unique_acknowledged_runtime_sequence_count == 2
    assert evidence.acknowledged_runtime_run_hash64_hex == "0000000000000002"
    assert evidence.first_sent_runtime_sequence == 1
    assert evidence.last_sent_runtime_sequence == 2
    assert evidence.unique_sent_runtime_sequence_count == 2
    assert evidence.sent_runtime_sequences_contiguous is True
    assert evidence.acknowledged_runtime_sequences_contiguous is True
    assert evidence.metadata_barrier_count_semantics == (
        "legacy_alias_of_canonical_post_adoption_type24_ack_count"
    )
    mapping = evidence.to_mapping()
    assert mapping["metadata_barrier_count"] == mapping["metadata_acceptance_ack_count"]
    assert json.loads(json.dumps(mapping))["acknowledged_runtime_run_hash64_hex"] == (
        "0000000000000002"
    )
    assert evidence.hardware_outputs_present is False
    assert evidence.pixhawk_attached is False


def test_generic_synchronized_qgc_bridge_accepts_live_sim_pacer() -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    transport = _Transport(order)
    publisher = _Publisher(order)
    pacer = LiveFramePacer()
    bridge = SynchronizedQgcOperatorBridge(
        transport,
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=publisher,
        pacer=pacer,
        expected_frame_count=1,
        wall_monotonic_ns=lambda: 1_010_000_000,
    )

    bridge.start()
    bridge.process_frame(
        tracks=(),
        frame_id="sim-frame-1",
        captured_at_s=12.5,
        produced_at_s=12.5,
        runtime_status=_runtime_status(RuntimeMode.SIM, sequence=1),
        image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
    )
    bridge.close()

    assert order == ["video", "metadata", "acceptance_ack"]
    assert pacer.stats.paced_frame_count == 1
    assert pacer.stats.slept_seconds == 0.0
    assert transport.runtime_statuses[0][0].mode is RuntimeMode.SIM
    assert bridge.require_complete().complete is True


def test_replay_bridge_keeps_wall_clock_pacer_type_contract() -> None:
    with pytest.raises(TypeError, match="pacer must be WallClockReplayPacer"):
        SynchronizedReplayOperatorBridge(
            _Transport([]),
            OperatorTargetLock(
                VideoGeometry("camera-main", 3, 2),
                TargetLockConfig(frozenset({"flame"})),
            ),
            frame_publisher=_Publisher([]),
            pacer=LiveFramePacer(),  # type: ignore[arg-type]
            expected_frame_count=1,
        )


def test_synchronized_bridge_fails_closed_on_skew_or_non_loopback_peer() -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    transport = _Transport(order)
    bridge = SynchronizedReplayOperatorBridge(
        transport,
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=_Publisher(order),
        pacer=WallClockReplayPacer(),
        expected_frame_count=1,
        maximum_ack_to_metadata_skew_ms=5.0,
        wall_monotonic_ns=lambda: 1_010_000_000,
    )
    bridge.start()
    with pytest.raises(RuntimeError, match="skew exceeded"):
        bridge.process_frame(
            tracks=(),
            frame_id="frame-1",
            captured_at_s=1.0,
            produced_at_s=1.0,
            runtime_status=_runtime_status(sequence=1),
            image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
        )
    skew_evidence = bridge.evidence
    assert skew_evidence.metadata_send_count == 1
    assert skew_evidence.metadata_acceptance_ack_count == 0
    assert skew_evidence.metadata_barrier_count == 0
    assert skew_evidence.first_acknowledged_runtime_sequence is None
    assert skew_evidence.last_acknowledged_runtime_sequence is None
    assert skew_evidence.unique_acknowledged_runtime_sequence_count == 0
    assert skew_evidence.acknowledged_runtime_run_hash64_hex is None
    assert skew_evidence.first_sent_runtime_sequence == 1
    assert skew_evidence.last_sent_runtime_sequence == 1
    assert skew_evidence.unique_sent_runtime_sequence_count == 1
    assert skew_evidence.sent_runtime_sequences_contiguous is True
    assert skew_evidence.acknowledged_runtime_sequences_contiguous is False
    bridge.close()

    other = _Transport([])
    other.peer = ("192.0.2.10", 14580)
    non_loopback = SynchronizedReplayOperatorBridge(
        other,
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=_Publisher([]),
        pacer=WallClockReplayPacer(),
        expected_frame_count=1,
    )
    with pytest.raises(RuntimeError, match="loopback-only"):
        non_loopback.start()
    assert other.closed is True


@pytest.mark.parametrize(
    "acknowledgement_mode",
    ["timeout", "wrong_sequence", "wrong_run", "wrong_peer", "over_timeout"],
)
def test_synchronized_bridge_never_counts_missing_or_mismatched_acceptance_ack(
    acknowledgement_mode: str,
) -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    bridge = SynchronizedReplayOperatorBridge(
        _Transport(order, acknowledgement_mode=acknowledgement_mode),
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=_Publisher(order),
        pacer=WallClockReplayPacer(),
        expected_frame_count=1,
    )
    bridge.start()

    with pytest.raises(RuntimeError, match="RuntimeStatusAcceptanceAck"):
        bridge.process_frame(
            tracks=(),
            frame_id="frame-1",
            captured_at_s=1.0,
            produced_at_s=1.0,
            runtime_status=_runtime_status(sequence=1),
            image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
        )

    evidence = bridge.evidence
    assert evidence.video_frame_count == 1
    assert evidence.metadata_send_count == 1
    assert evidence.metadata_acceptance_ack_count == 0
    assert evidence.metadata_barrier_count == 0
    assert evidence.first_acknowledged_runtime_sequence is None
    assert evidence.last_acknowledged_runtime_sequence is None
    assert evidence.unique_acknowledged_runtime_sequence_count == 0
    assert evidence.acknowledged_runtime_run_hash64_hex is None
    assert evidence.first_sent_runtime_sequence == 1
    assert evidence.last_sent_runtime_sequence == 1
    assert evidence.unique_sent_runtime_sequence_count == 1
    assert evidence.sent_runtime_sequences_contiguous is True
    assert evidence.acknowledged_runtime_sequences_contiguous is False
    assert evidence.complete is False
    bridge.close()


def test_synchronized_bridge_requires_gap_free_sent_and_acknowledged_sequences() -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    bridge = SynchronizedReplayOperatorBridge(
        _Transport(order),
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=_Publisher(order),
        pacer=WallClockReplayPacer(),
        expected_frame_count=2,
    )
    bridge.start()
    for frame_index, runtime_sequence in enumerate((10, 12), start=1):
        bridge.process_frame(
            tracks=(),
            frame_id=f"frame-{frame_index}",
            captured_at_s=float(frame_index),
            produced_at_s=float(frame_index),
            runtime_status=_runtime_status(sequence=runtime_sequence),
            image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
        )
    bridge.close()

    evidence = bridge.evidence
    assert evidence.metadata_send_count == 2
    assert evidence.metadata_acceptance_ack_count == 2
    assert evidence.metadata_barrier_count == 2
    assert evidence.first_sent_runtime_sequence == 10
    assert evidence.last_sent_runtime_sequence == 12
    assert evidence.first_acknowledged_runtime_sequence == 10
    assert evidence.last_acknowledged_runtime_sequence == 12
    assert evidence.unique_sent_runtime_sequence_count == 2
    assert evidence.unique_acknowledged_runtime_sequence_count == 2
    assert evidence.sent_runtime_sequences_contiguous is False
    assert evidence.acknowledged_runtime_sequences_contiguous is False
    assert evidence.complete is False
    with pytest.raises(RuntimeError, match="did not complete every"):
        bridge.require_complete()


def test_synchronized_bridge_rejects_duplicate_runtime_sequence_before_next_video() -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    bridge = SynchronizedReplayOperatorBridge(
        _Transport(order),
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=_Publisher(order),
        pacer=WallClockReplayPacer(),
        expected_frame_count=2,
    )
    bridge.start()
    frame = {
        "tracks": (),
        "frame_id": "frame-1",
        "captured_at_s": 1.0,
        "produced_at_s": 1.0,
        "runtime_status": _runtime_status(sequence=1),
        "image_bgr": np.zeros((2, 3, 3), dtype=np.uint8),
    }
    bridge.process_frame(**frame)

    with pytest.raises(RuntimeError, match="duplicate or out of order"):
        bridge.process_frame(
            **{
                **frame,
                "frame_id": "frame-2",
                "captured_at_s": 2.0,
                "produced_at_s": 2.0,
            }
        )

    assert bridge.evidence.video_frame_count == 1
    assert bridge.evidence.metadata_barrier_count == 1
    bridge.close()


def test_synchronized_bridge_does_not_publish_video_without_runtime_barrier() -> None:
    np = pytest.importorskip("numpy")
    order: list[str] = []
    publisher = _Publisher(order)
    wall = _FakeWallClock()
    bridge = SynchronizedReplayOperatorBridge(
        _Transport(order),
        OperatorTargetLock(
            VideoGeometry("camera-main", 3, 2),
            TargetLockConfig(frozenset({"flame"})),
        ),
        frame_publisher=publisher,
        pacer=WallClockReplayPacer(
            wall_monotonic=wall.monotonic,
            sleep=wall.sleep,
        ),
        expected_frame_count=1,
        wall_monotonic=wall.monotonic,
        sleep=wall.sleep,
    )
    bridge.start()

    with pytest.raises(ValueError, match="runtime status barrier"):
        bridge.process_frame(
            tracks=(),
            frame_id="frame-1",
            captured_at_s=1.0,
            produced_at_s=1.0,
            image_bgr=np.zeros((2, 3, 3), dtype=np.uint8),
        )

    assert order == []
    assert publisher.frames == 0
    assert bridge.evidence.metadata_barrier_count == 0
    bridge.close()

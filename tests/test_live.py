from __future__ import annotations

import socket
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

import pytest

import multidetect.live as live_module
from multidetect.alerts import (
    AuthenticatedUdpAlertReceiver,
    LoopbackAcknowledgedAlertTransport,
    RecordingAlertPublisher,
    RetryingAcknowledgedAlertPublisher,
    SqliteAlertDeduplicationStore,
    SqliteAlertOutbox,
    UdpAcknowledgedAlertTransport,
)
from multidetect.config import MissionConfig
from multidetect.domain import BoundingBox, Detection, MissionPhase, SensorKind, VehicleTelemetry
from multidetect.evaluation import JsonlPredictionWriter, load_prediction_jsonl
from multidetect.live import LiveMissionRunner, LiveRunConfig
from multidetect.mission import MissionController
from multidetect.operator_bridge import LiveOperatorBridge
from multidetect.operator_link import SelectionAction, TargetSelectionCommand, VideoGeometry
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig
from multidetect.telemetry import FailClosedTelemetryProvider
from multidetect.vision import CapturedFrame

ROOT = Path(__file__).resolve().parents[1]


class _FrameSource:
    def __init__(self, frame_count: int) -> None:
        self._frame_count = frame_count
        self._next = 0
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def read(self) -> CapturedFrame:
        self._next += 1
        return CapturedFrame(
            frame_id=f"live-{self._next}",
            captured_at_s=time.monotonic(),
            image_bgr=object(),
            width=640,
            height=480,
        )

    def close(self) -> None:
        self.closed = True


class _Detector:
    def detect(self, _image) -> tuple[Detection, ...]:
        return (
            Detection(
                "flame",
                0.95,
                BoundingBox(0.2, 0.2, 0.4, 0.5),
                SensorKind.RGB,
                "test-model",
            ),
        )

    def covers_labels(self, _labels) -> bool:
        return False


class _FailingPublisher:
    def publish(self, _alert) -> None:
        raise OSError("simulated data-link failure")


class _FailingDetector(_Detector):
    def detect(self, _image) -> tuple[Detection, ...]:
        raise RuntimeError("simulated inference failure")


class _SafetyDetector(_Detector):
    def covers_labels(self, _labels) -> bool:
        return True


class _SafeTelemetryProvider:
    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        del now_s
        return VehicleTelemetry(
            altitude_agl_m=20.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=1.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=True,
            release_zone_clear=True,
            person_detector_healthy=True,
        )


class _ObservedLifecycleTelemetryProvider:
    def __init__(self) -> None:
        self._sample = 0

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        del now_s
        self._sample += 1
        armed = self._sample >= 2
        sequence = 2 if self._sample >= 3 else 0
        return VehicleTelemetry(
            altitude_agl_m=20.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=12.0,
            in_allowed_zone=None,
            geofence_healthy=None,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=None,
            release_zone_clear=None,
            armed=armed,
            flight_mode="AUTO" if armed else "MANUAL",
            mission_sequence=sequence,
        )


class _ScriptedPayloadUI:
    def render(self, _captured, **state):
        if state["pending_authorization"]:
            return "approve"
        if state["deployment_ready"]:
            return "simulate_payload"
        return None

    def close(self) -> None:
        pass


def _patrol_config() -> MissionConfig:
    return replace(
        MissionConfig.from_json(ROOT / "configs/missions/fire_patrol.demo.json"),
        minimum_track_time_seconds=0.0,
        require_thermal_corroboration=False,
    )


def _payload_config() -> MissionConfig:
    return replace(
        MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json"),
        minimum_track_time_seconds=0.0,
        require_thermal_corroboration=False,
    )


def test_live_patrol_delivers_confirmed_alert_without_payload() -> None:
    publisher = RecordingAlertPublisher()
    source = _FrameSource(4)
    runner = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=source,
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        alert_publisher=publisher,
    )

    result = runner.run()

    assert result.processed_frames == 4
    assert result.alert_delivery_count == 1
    assert result.alert_delivery_failure_count == 0
    assert result.average_fps > 0
    assert result.capture_latency_p95_ms >= 0
    assert result.inference_latency_p95_ms >= 0
    assert result.camera_reconnect_count == 0
    assert len(publisher.alerts()) == 1
    assert source.opened is True
    assert source.closed is True


def test_live_runner_connects_remote_selection_to_continuous_track_status() -> None:
    geometry = VideoGeometry("camera-main", 640, 480)

    class _OperatorTransport:
        def __init__(self) -> None:
            self.commands = deque(
                [
                    (
                        TargetSelectionCommand(
                            command_id="11111111-1111-4111-8111-111111111111",
                            session_id="22222222-2222-4222-8222-222222222222",
                            sequence=1,
                            action=SelectionAction.SELECT,
                            geometry=geometry,
                            issued_at_s=100.0,
                            expires_at_s=103.0,
                            bbox=BoundingBox(0.15, 0.15, 0.45, 0.55),
                        ),
                        ("192.168.144.11", 14580),
                    )
                ]
            )
            self.published = []
            self.closed = False

        def start_background(self) -> None:
            pass

        def poll_selection(self):
            return self.commands.popleft() if self.commands else None

        def poll_error(self):
            return None

        def publish_track_status(self, status, *, peer) -> None:
            self.published.append((status, peer))

        def close(self) -> None:
            self.closed = True

    transport = _OperatorTransport()
    bridge = LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            geometry,
            TargetLockConfig(frozenset(_patrol_config().target_classes)),
        ),
    )
    mission = MissionController(_patrol_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(3),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=False),
        operator_bridge=bridge,
    ).run()

    assert result.remote_selection_count == 1
    assert result.remote_tracking_status_count == 3
    assert result.remote_transport_error_count == 0
    assert len(transport.published) == 3
    assert {status.target_id for status, _peer in transport.published} == {"track-000001"}
    assert transport.closed is True
    assert any(
        event.event_type == "operator.remote_tracking_status" for event in mission.audit.events()
    )


def test_live_patrol_audits_alert_delivery_failure_and_keeps_running() -> None:
    mission = MissionController(_patrol_config())
    runner = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        alert_publisher=_FailingPublisher(),
    )

    result = runner.run()

    assert result.alert_delivery_count == 0
    assert result.alert_delivery_failure_count == 1
    assert any(event.event_type == "alert.delivery_failed" for event in mission.audit.events())


def test_live_patrol_retries_persisted_alert_on_next_run(tmp_path) -> None:
    outbox = SqliteAlertOutbox(tmp_path / "alerts.sqlite3")
    first = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        alert_publisher=_FailingPublisher(),
        alert_outbox=outbox,
    ).run()
    assert first.alert_delivery_failure_count == 1
    assert len(outbox.pending_alerts()) == 1

    publisher = RecordingAlertPublisher()
    second = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(1),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=1, display=False),
        alert_publisher=publisher,
        alert_outbox=outbox,
    ).run()

    assert second.retried_alert_count == 1
    assert second.alert_delivery_count == 1
    assert len(publisher.alerts()) == 1
    assert outbox.pending_alerts() == ()
    outbox.close()


def test_live_outbox_marks_delivered_only_after_correlated_ack(tmp_path) -> None:
    outbox = SqliteAlertOutbox(tmp_path / "alerts.sqlite3")
    transport = LoopbackAcknowledgedAlertTransport(fail_first_attempts=1)
    publisher = RetryingAcknowledgedAlertPublisher(
        transport,
        maximum_attempts=2,
        initial_backoff_seconds=0,
    )
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        alert_publisher=publisher,
        alert_outbox=outbox,
    ).run()

    assert result.alert_delivery_count == 1
    assert transport.attempt_count == 2
    assert outbox.pending_alerts() == ()
    outbox.close()


def test_live_patrol_delivers_over_authenticated_udp_to_persistent_ground_store(
    tmp_path,
) -> None:
    key = b"live-integration-alert-key-at-least-32-bytes"
    outbox = SqliteAlertOutbox(tmp_path / "aircraft-outbox.sqlite3")
    ground_store = SqliteAlertDeduplicationStore(tmp_path / "ground-received.sqlite3")
    received = []
    with AuthenticatedUdpAlertReceiver(
        bind_host="127.0.0.1",
        port=0,
        hmac_key=key,
        receiver_id="ground-1",
        expected_sender_id="aircraft-1",
        receive_timeout_seconds=2.0,
        deduplication_store=ground_store,
    ) as receiver:
        worker = threading.Thread(target=lambda: received.append(receiver.receive()))
        worker.start()
        publisher = RetryingAcknowledgedAlertPublisher(
            UdpAcknowledgedAlertTransport(
                host="127.0.0.1",
                port=receiver.local_address[1],
                hmac_key=key,
                sender_id="aircraft-1",
                receiver_id="ground-1",
                acknowledgement_timeout_seconds=2.0,
            ),
            maximum_attempts=1,
        )
        mission = MissionController(_patrol_config())
        result = LiveMissionRunner(
            mission=mission,
            frame_source=_FrameSource(4),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=4, display=False),
            alert_publisher=publisher,
            alert_outbox=outbox,
        ).run()
        worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert result.alert_delivery_count == 1
    assert result.alert_delivery_failure_count == 0
    assert outbox.pending_alerts() == ()
    assert len(received) == 1
    alert_event = next(
        event for event in mission.audit.events() if event.event_type == "alert.fire_confirmed"
    )
    assert received[0].document["alert_id"] == alert_event.details["alert_id"]
    assert received[0].duplicate is False
    outbox.close()
    ground_store.close()


def test_authenticated_udp_pending_alert_recovers_after_ground_restart(tmp_path) -> None:
    key = b"live-restart-alert-key-at-least-32-bytes"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    outbox = SqliteAlertOutbox(tmp_path / "aircraft-outbox.sqlite3")
    offline_publisher = RetryingAcknowledgedAlertPublisher(
        UdpAcknowledgedAlertTransport(
            host="127.0.0.1",
            port=port,
            hmac_key=key,
            sender_id="aircraft-1",
            receiver_id="ground-1",
            acknowledgement_timeout_seconds=0.05,
        ),
        maximum_attempts=1,
    )
    first = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        alert_publisher=offline_publisher,
        alert_outbox=outbox,
    ).run()
    (pending,) = outbox.pending_alerts()

    ground_store = SqliteAlertDeduplicationStore(tmp_path / "ground-received.sqlite3")
    received = []
    with AuthenticatedUdpAlertReceiver(
        bind_host="127.0.0.1",
        port=port,
        hmac_key=key,
        receiver_id="ground-1",
        expected_sender_id="aircraft-1",
        receive_timeout_seconds=2.0,
        deduplication_store=ground_store,
    ) as receiver:
        worker = threading.Thread(target=lambda: received.append(receiver.receive()))
        worker.start()
        online_publisher = RetryingAcknowledgedAlertPublisher(
            UdpAcknowledgedAlertTransport(
                host="127.0.0.1",
                port=port,
                hmac_key=key,
                sender_id="aircraft-1",
                receiver_id="ground-1",
                acknowledgement_timeout_seconds=2.0,
            ),
            maximum_attempts=1,
        )
        second = LiveMissionRunner(
            mission=MissionController(_patrol_config()),
            frame_source=_FrameSource(1),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=1, display=False),
            alert_publisher=online_publisher,
            alert_outbox=outbox,
        ).run()
        worker.join(timeout=2.0)

    assert first.alert_delivery_failure_count == 1
    assert worker.is_alive() is False
    assert second.retried_alert_count == 1
    assert second.alert_delivery_count == 1
    assert outbox.pending_alerts() == ()
    assert len(received) == 1
    assert received[0].document["alert_id"] == pending.alert_id
    outbox.close()
    ground_store.close()


def test_live_payload_mode_completes_explicit_fake_hil_cycle(monkeypatch) -> None:
    monkeypatch.setattr(live_module, "OpenCVAuthorizationUI", _ScriptedPayloadUI)
    mission = MissionController(_payload_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(5),
        detector=_SafetyDetector(),
        telemetry_provider=_SafeTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=5,
            display=True,
            simulate_payload_cycle=True,
        ),
    ).run()

    assert result.authorization_count == 1
    assert result.simulated_payload_cycle_count == 1
    assert mission.fake_payload_port.request_count == 1
    assert mission.state.phase is MissionPhase.SEARCHING
    assert any(
        event.event_type == "operator.simulated_payload_cycle_requested"
        for event in mission.audit.events()
    )


def test_live_patrol_records_every_prediction_frame(tmp_path) -> None:
    path = tmp_path / "predictions.jsonl"
    writer = JsonlPredictionWriter(path)
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        prediction_writer=writer,
    ).run()
    writer.close()

    frames = load_prediction_jsonl(path)
    assert result.processed_frames == 4
    assert len(frames) == 4
    assert all(len(frame.detections) == 1 for frame in frames)


def test_live_inference_failure_is_audited_and_not_treated_as_empty_detection() -> None:
    mission = MissionController(_patrol_config())
    runner = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(1),
        detector=_FailingDetector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=1, display=False),
    )

    with pytest.raises(RuntimeError, match="inference failure"):
        runner.run()

    assert any(
        event.event_type == "perception.inference_failed" for event in mission.audit.events()
    )


def test_live_observes_pixhawk_lifecycle_without_sending_flight_commands() -> None:
    mission = MissionController(_patrol_config())
    publisher = RecordingAlertPublisher()
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(6),
        detector=_Detector(),
        telemetry_provider=_ObservedLifecycleTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=6,
            display=False,
            observe_pixhawk_lifecycle=True,
            task_area_mission_sequence=2,
        ),
        alert_publisher=publisher,
    ).run()

    assert result.processed_frames == 6
    assert len(publisher.alerts()) == 1
    assert mission.state.phase is MissionPhase.SEARCHING
    assert tuple(transition.event for transition in mission.state.history[:2]) == (
        "launch",
        "arrive_task_area",
    )
    start_event = next(
        event
        for event in mission.audit.events()
        if event.event_type == "mission.pixhawk_lifecycle_observation_started"
    )
    assert start_event.details["flight_commands_enabled"] is False

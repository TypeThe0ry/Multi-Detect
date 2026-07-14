from __future__ import annotations

import json
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
from multidetect.live import LiveMissionRunner, LiveRunConfig, OpenCVAuthorizationUI
from multidetect.manual_tracking import OpenCVManualTargetTracker
from multidetect.mission import MissionController
from multidetect.operator_bridge import LiveOperatorBridge
from multidetect.operator_link import (
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    VideoGeometry,
)
from multidetect.operator_mavlink import (
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import OperatorTunnelCodec
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig
from multidetect.operator_udp import UdpOperatorSelectionServer, UdpOperatorSessionClient
from multidetect.payload_confirmation_hil import (
    PayloadConfirmationHilCodec,
    PayloadConfirmationHilMessage,
)
from multidetect.payload_confirmation_udp import (
    UdpPayloadConfirmationHilReceiver,
    UdpPayloadConfirmationHilSender,
)
from multidetect.payload_hil_cycle import InertPayloadHilCycleCoordinator
from multidetect.payload_hil_mission import MissionPayloadHilAdapter
from multidetect.payload_hil_protocol import PayloadHilCodec, PayloadHilRequestGuard
from multidetect.payload_hil_udp import UdpInertPayloadHilController, UdpPayloadHilClient
from multidetect.pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from multidetect.pixhawk_hil import FixedWingTelemetryHilConfig, FixedWingTelemetryHilEmitter
from multidetect.telemetry import AuthenticatedZoneTelemetryProvider, FailClosedTelemetryProvider
from multidetect.vision import CapturedFrame
from multidetect.zone_evidence import FileZoneEvidenceProvider, sign_zone_evidence_document

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "message"),
    [
        ("max_frames", True, "max_frames"),
        ("performance_window_frames", 1.5, "performance_window_frames"),
        ("task_area_mission_sequence", True, "mission sequence"),
    ],
)
def test_live_config_rejects_implicitly_coerced_numeric_values(
    field_name: str, invalid_value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        LiveRunConfig(**{field_name: invalid_value})


def test_live_config_requires_explicit_simulation_for_automatic_hil_cycle() -> None:
    with pytest.raises(ValueError, match="requires simulate_payload_cycle"):
        LiveRunConfig(auto_simulate_payload_cycle=True)


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


class _StableManualTrackerBackend:
    def __init__(self) -> None:
        self._bbox = (0.0, 0.0, 1.0, 1.0)

    def init(self, _image, bbox) -> bool:
        self._bbox = tuple(float(value) for value in bbox)
        return True

    def update(self, _image):
        return True, self._bbox


def _stable_manual_tracker(geometry: VideoGeometry) -> OpenCVManualTargetTracker:
    return OpenCVManualTargetTracker(
        geometry,
        tracker_factory=_StableManualTrackerBackend,
    )


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


class _LivePayloadHilCycle:
    def __init__(self, mission: MissionController) -> None:
        self.mission = mission
        self.execute_count = 0
        self.closed = False

    def execute(self, *, now_s: float) -> object:
        release_id = self.mission.request_simulated_deployment(now_s=now_s)
        self.mission.report_simulated_execution(release_id=release_id, now_s=time.monotonic())
        self.mission.report_independent_confirmation(
            release_id=release_id,
            source_id="test-independent-hil-sensor",
            now_s=time.monotonic(),
        )
        self.execute_count += 1
        return object()

    def close(self) -> None:
        self.closed = True


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
    assert result.steady_source_fps > 0
    assert result.steady_processing_fps > 0
    assert result.startup_to_first_frame_seconds >= 0
    assert result.capture_latency_p95_ms >= 0
    assert result.frame_age_at_inference_p95_ms >= 0
    assert result.inference_latency_p95_ms >= 0
    assert result.camera_reconnect_count == 0
    assert result.capture_queue_high_watermark == 0
    assert result.capture_queue_backpressure_count == 0
    assert result.captured_frame_count == 4
    assert len(publisher.alerts()) == 1
    assert source.opened is True
    assert source.closed is True


def test_live_runner_keeps_capture_and_mission_event_clocks_separate() -> None:
    class _PrefetchedFrameSource(_FrameSource):
        def read(self) -> CapturedFrame:
            self._next += 1
            return CapturedFrame(
                frame_id=f"prefetched-{self._next}",
                captured_at_s=float(self._next),
                image_bgr=object(),
                width=640,
                height=480,
            )

    runner = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_PrefetchedFrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
    )

    result = runner.run()

    assert result.processed_frames == 4
    assert result.alert_delivery_count == 1


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
            self.mission_published = []
            self.safety_published = []
            self.closed = False

        def start_background(self) -> None:
            pass

        def poll_selection(self):
            return self.commands.popleft() if self.commands else None

        def poll_error(self):
            return None

        def publish_track_status(self, status, *, peer) -> None:
            self.published.append((status, peer))

        def publish_mission_status(self, status, *, peer) -> None:
            self.mission_published.append((status, peer))

        def publish_safety_status(self, status, *, peer) -> None:
            self.safety_published.append((status, peer))

        def close(self) -> None:
            self.closed = True

    transport = _OperatorTransport()
    bridge = LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            geometry,
            TargetLockConfig(frozenset(_payload_config().target_classes)),
        ),
        manual_tracker_factory=_stable_manual_tracker,
    )
    mission = MissionController(_payload_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(5),
        detector=_SafetyDetector(),
        telemetry_provider=_SafeTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=5,
            display=False,
            person_safety_evidence_qualified=True,
        ),
        operator_bridge=bridge,
    ).run()

    assert result.remote_selection_count == 1
    assert result.remote_tracking_status_count == 5
    assert 1 <= result.remote_mission_status_count <= 5
    assert 1 <= result.remote_safety_status_count <= 5
    assert result.remote_transport_error_count == 0
    assert len(transport.published) == 5
    assert len(transport.mission_published) == result.remote_mission_status_count
    assert len(transport.safety_published) == result.remote_safety_status_count
    assert all(status.advisory_only for status, _peer in transport.mission_published)
    assert all(status.advisory_only for status, _peer in transport.safety_published)
    assert {status.target_id for status, _peer in transport.published} == {"track-000001"}
    assert transport.closed is True
    assert any(
        event.event_type == "operator.remote_tracking_status" for event in mission.audit.events()
    )
    assert any(
        event.event_type == "operator.remote_safety_status" for event in mission.audit.events()
    )


def test_remote_manual_tracking_without_detections_cannot_create_mission_events() -> None:
    geometry = VideoGeometry("camera-main", 640, 480)

    class _NoDetectionDetector:
        def detect(self, _image) -> tuple[Detection, ...]:
            return ()

        def covers_labels(self, _labels) -> bool:
            return False

    class _Backend:
        def __init__(self) -> None:
            self.updates = [
                (True, (72.0, 52.0, 256.0, 192.0)),
                (True, (80.0, 56.0, 256.0, 192.0)),
                (True, (88.0, 60.0, 256.0, 192.0)),
            ]

        def init(self, _image, _bbox) -> bool:
            return True

        def update(self, _image):
            return self.updates.pop(0)

    class _Transport:
        def __init__(self) -> None:
            self.commands = deque(
                [
                    (
                        TargetSelectionCommand(
                            command_id="77777777-7777-4777-8777-777777777777",
                            session_id="88888888-8888-4888-8888-888888888888",
                            sequence=1,
                            action=SelectionAction.SELECT,
                            geometry=geometry,
                            issued_at_s=100.0,
                            expires_at_s=103.0,
                            bbox=BoundingBox(0.1, 0.1, 0.5, 0.5),
                        ),
                        ("192.168.144.11", 14580),
                    )
                ]
            )
            self.published = []

        def start_background(self) -> None:
            pass

        def poll_selection(self):
            return self.commands.popleft() if self.commands else None

        def poll_error(self):
            return None

        def publish_track_status(self, status, *, peer) -> None:
            self.published.append((status, peer))

        def publish_mission_status(self, _status, *, peer) -> None:
            del peer

        def publish_safety_status(self, _status, *, peer) -> None:
            del peer

        def close(self) -> None:
            pass

    backend = _Backend()
    transport = _Transport()
    mission = MissionController(_patrol_config())
    publisher = RecordingAlertPublisher()
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_NoDetectionDetector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        alert_publisher=publisher,
        operator_bridge=LiveOperatorBridge(
            transport,
            OperatorTargetLock(
                geometry,
                TargetLockConfig(frozenset(_patrol_config().target_classes)),
            ),
            manual_tracker_factory=lambda current_geometry: OpenCVManualTargetTracker(
                current_geometry,
                tracker_factory=lambda: backend,
            ),
        ),
    ).run()

    assert result.remote_selection_count == 1
    assert result.remote_tracking_status_count == 4
    assert all(status.label == "manual" for status, _peer in transport.published)
    assert result.alert_delivery_count == 0
    assert result.authorization_count == 0
    assert mission.fake_payload_port.request_count == 0
    assert mission.status().phase is MissionPhase.SEARCHING
    assert publisher.alerts() == ()


def test_live_runner_closes_pixhawk_remote_authorization_and_two_channel_hil(
    tmp_path: Path,
) -> None:
    geometry = VideoGeometry("camera-main", 640, 480)
    operator_key = b"live-combined-operator-key-material-at-least-32-bytes"
    mavlink_key = b"L" * 32

    def operator_adapter(endpoint: OperatorMavlinkEndpoint) -> OperatorMavlinkTunnelAdapter:
        return OperatorMavlinkTunnelAdapter(
            OperatorTunnelCodec(hmac_key=operator_key, geometries=(geometry,)),
            endpoint,
            signing_key=mavlink_key,
            signing_link_id=endpoint.local_component_id,
            initial_signing_timestamp=4_000_000 + endpoint.local_system_id,
        )

    transport = UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=operator_adapter(OperatorMavlinkEndpoint(1, 191, 255, 190)),
        guard=SelectionCommandGuard(geometry),
    )
    g20_adapter = operator_adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))
    operator_receipts: dict[str, object] = {}

    def g20_operator() -> None:
        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=transport.bound_address[1],
            mavlink=g20_adapter,
            retry_interval_s=0.1,
            maximum_attempts=5,
        ) as session:
            issued_at_s = time.time()
            selection = TargetSelectionCommand(
                command_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=geometry,
                issued_at_s=issued_at_s,
                expires_at_s=issued_at_s + 3.0,
                bbox=BoundingBox(0.15, 0.15, 0.45, 0.55),
            )
            operator_receipts["selection"] = session.deliver(selection)
            challenge = session.receive_authorization_challenge(timeout_s=2.0)
            decision_issued_at_s = time.time()
            decision = AuthorizationDecisionCommand(
                command_token=101,
                session_token=102,
                challenge_token=challenge.challenge_token,
                mission_token=challenge.mission_token,
                target_token=challenge.target_token,
                scene_token=challenge.scene_token,
                ruleset_token=challenge.ruleset_token,
                payload_slot_token=challenge.payload_slot_token,
                target_revision=challenge.target_revision,
                decision=AuthorizationDecision.APPROVE,
                operator_token=103,
                sequence=2,
                issued_at_s=decision_issued_at_s,
                expires_at_s=min(decision_issued_at_s + 1.0, challenge.expires_at_s),
            )
            operator_receipts["authorization"] = session.deliver_authorization_decision(decision)

    mission = MissionController(_payload_config())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        pixhawk_port = port_probe.getsockname()[1]
    pixhawk = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            endpoint=f"udpin:127.0.0.1:{pixhawk_port}",
            stale_after_seconds=0.5,
        )
    )
    evidence_now_s = time.monotonic()
    pixhawk.snapshot(now_s=evidence_now_s)
    zone_key = b"live-combined-zone-evidence-key-at-least-32-bytes"
    zone_report = tmp_path / "zone-evidence.json"
    zone_document: dict[str, object] = {
        "protocol_version": 1,
        "observed_at_s": evidence_now_s,
        "source_id": "live-combined-independent-zone-monitor",
        "mission_id": mission.config.mission_id,
        "sequence": 1,
        "key_id": "live-combined-zone-v1",
        "latitude_deg": 31.123456,
        "longitude_deg": 121.654321,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "release_zone_clear": True,
    }
    zone_document["signature_hmac_sha256"] = sign_zone_evidence_document(
        zone_document,
        hmac_key=zone_key,
    )
    zone_report.write_text(json.dumps(zone_document), encoding="utf-8")
    telemetry = AuthenticatedZoneTelemetryProvider(
        pixhawk,
        FileZoneEvidenceProvider(
            zone_report,
            hmac_key=zone_key,
            expected_key_id="live-combined-zone-v1",
        ),
        mission_id=mission.config.mission_id,
        maximum_age_s=2.0,
        maximum_position_delta_m=25.0,
    )
    pixhawk_emitter = FixedWingTelemetryHilEmitter(
        FixedWingTelemetryHilConfig(
            endpoint=f"udpout:127.0.0.1:{pixhawk_port}",
            rate_hz=40.0,
            altitude_agl_m=20.0,
            ground_speed_mps=1.0,
            mission_sequence=3,
        )
    )
    request_codec = PayloadHilCodec(
        hmac_key=b"live-auto-request-key-material-at-least-32-bytes",
        expected_key_id="live-auto-request-v1",
    )
    result_codec = PayloadHilCodec(
        hmac_key=b"live-auto-result-key-material-at-least-32-bytes",
        expected_key_id="live-auto-result-v1",
    )
    confirmation_codec = PayloadConfirmationHilCodec(
        hmac_key=b"live-auto-confirm-key-material-at-least-32-bytes",
        expected_key_id="live-auto-confirm-v1",
    )
    with UdpInertPayloadHilController(
        bind_host="127.0.0.1",
        port=0,
        request_codec=request_codec,
        result_codec=result_codec,
        request_guard=PayloadHilRequestGuard(
            mission_id=mission.config.mission_id,
            module_id="live-auto-controller",
            installed_slots={"payload-1": "fire_suppression_agent"},
            maximum_age_s=1.0,
        ),
    ) as controller:
        confirmation_receiver = UdpPayloadConfirmationHilReceiver(
            bind_host="127.0.0.1",
            port=0,
        )

        def controller_and_sensor() -> None:
            results = controller.serve_once(simulate_inert_execution=True)
            executed = results[-1]
            UdpPayloadConfirmationHilSender(
                host=confirmation_receiver.local_address[0],
                port=confirmation_receiver.local_address[1],
                codec=confirmation_codec,
            ).send(
                PayloadConfirmationHilMessage(
                    mission_id=mission.config.mission_id,
                    sensor_id="live-auto-bay-sensor",
                    release_id=executed.release_id,
                    payload_slot_id=executed.payload_slot_id,
                    payload_absent=True,
                    sensor_healthy=True,
                    observed_at_s=time.monotonic(),
                    sequence=1,
                    key_id="live-auto-confirm-v1",
                )
            )

        controller_worker = threading.Thread(target=controller_and_sensor)
        controller_worker.start()
        operator_worker = threading.Thread(target=g20_operator)
        operator_worker.start()
        pixhawk_worker = threading.Thread(
            target=pixhawk_emitter.run,
            kwargs={"duration_s": 1.0},
        )
        pixhawk_worker.start()
        payload_cycle = InertPayloadHilCycleCoordinator(
            mission=mission,
            controller_adapter=MissionPayloadHilAdapter(
                mission=mission,
                client=UdpPayloadHilClient(
                    host=controller.local_address[0],
                    port=controller.local_address[1],
                    request_codec=request_codec,
                    result_codec=result_codec,
                    response_timeout_s=0.5,
                    maximum_attempts=2,
                ),
                module_id="live-auto-controller",
                request_key_id="live-auto-request-v1",
            ),
            confirmation_receiver=confirmation_receiver,
            confirmation_codec=confirmation_codec,
            controller_module_id="live-auto-controller",
            allowed_confirmation_sensor_ids=frozenset({"live-auto-bay-sensor"}),
            confirmation_timeout_s=0.5,
        )

        class _TimedFrameSource(_FrameSource):
            def read(self) -> CapturedFrame:
                time.sleep(0.025)
                return super().read()

        result = LiveMissionRunner(
            mission=mission,
            frame_source=_TimedFrameSource(24),
            detector=_SafetyDetector(),
            telemetry_provider=telemetry,
            config=LiveRunConfig(
                max_frames=24,
                display=False,
                simulate_payload_cycle=True,
                auto_simulate_payload_cycle=True,
                observe_pixhawk_lifecycle=True,
                task_area_mission_sequence=3,
                allowed_auto_modes=("AUTO",),
                person_safety_evidence_qualified=True,
            ),
            operator_bridge=LiveOperatorBridge(
                transport,
                OperatorTargetLock(
                    geometry,
                    TargetLockConfig(frozenset(_payload_config().target_classes)),
                ),
                manual_tracker_factory=_stable_manual_tracker,
            ),
            payload_hil_cycle=payload_cycle,
        ).run()
        controller_worker.join(timeout=2.0)
        operator_worker.join(timeout=2.0)
        pixhawk_worker.join(timeout=2.0)

    assert result.authorization_count == 1
    assert result.simulated_payload_cycle_count == 1
    assert mission.status().phase is MissionPhase.SEARCHING
    assert mission.status().active_release_id is None
    assert mission.fake_payload_port.request_count == 1
    assert controller_worker.is_alive() is False
    assert operator_worker.is_alive() is False
    assert pixhawk_worker.is_alive() is False
    assert operator_receipts["selection"].acknowledgement.accepted is True
    assert operator_receipts["authorization"].acknowledgement.accepted is True
    assert pixhawk_emitter.message_count > 0
    assert pixhawk.messages_transmitted == 0
    assert pixhawk.is_read_only is True
    assert controller.command_messages_sent == 0
    assert controller.physical_release_enabled is False
    assert any(
        event.event_type == "operator.remote_authorization_applied"
        for event in mission.audit.events()
    )
    assert any(
        event.event_type == "hil.auto_simulated_payload_cycle_requested"
        for event in mission.audit.events()
    )
    assert any(event.event_type == "payload.release_confirmed" for event in mission.audit.events())
    assert any(
        event.event_type == "mission.pixhawk_lifecycle_observation_started"
        for event in mission.audit.events()
    )


def test_pixhawk_link_loss_invalidates_remote_authorization_before_auto_hil() -> None:
    geometry = VideoGeometry("camera-main", 640, 480)
    peer = ("192.0.2.20", 14580)

    class _DelayedDecisionTransport:
        def __init__(self) -> None:
            now_s = time.time()
            self.selections = deque(
                [
                    (
                        TargetSelectionCommand(
                            command_id="88888888-8888-4888-8888-888888888888",
                            session_id="99999999-9999-4999-8999-999999999999",
                            sequence=1,
                            action=SelectionAction.SELECT,
                            geometry=geometry,
                            issued_at_s=now_s,
                            expires_at_s=now_s + 3.0,
                            bbox=BoundingBox(0.15, 0.15, 0.45, 0.55),
                        ),
                        peer,
                    )
                ]
            )
            self.decisions = deque()
            self.challenge_sent = False

        def start_background(self) -> None:
            pass

        def poll_selection(self):
            return self.selections.popleft() if self.selections else None

        def poll_authorization_decision(self):
            return self.decisions.popleft() if self.decisions else None

        def set_authorization_challenge(self, _status) -> None:
            pass

        def poll_error(self):
            return None

        def publish_track_status(self, _status, *, peer) -> None:
            del peer

        def publish_mission_status(self, _status, *, peer) -> None:
            del peer

        def publish_safety_status(self, _status, *, peer) -> None:
            del peer

        def publish_authorization_challenge(self, status, *, peer) -> None:
            if self.challenge_sent:
                return
            self.challenge_sent = True
            self.decisions.append(
                (
                    AuthorizationDecisionCommand(
                        command_token=501,
                        session_token=502,
                        challenge_token=status.challenge_token,
                        mission_token=status.mission_token,
                        target_token=status.target_token,
                        scene_token=status.scene_token,
                        ruleset_token=status.ruleset_token,
                        payload_slot_token=status.payload_slot_token,
                        target_revision=status.target_revision,
                        decision=AuthorizationDecision.APPROVE,
                        operator_token=503,
                        sequence=2,
                        issued_at_s=status.produced_at_s,
                        expires_at_s=min(status.produced_at_s + 1.0, status.expires_at_s),
                    ),
                    peer,
                )
            )

        def close(self) -> None:
            pass

    class _LinkLossTelemetryProvider:
        def __init__(self) -> None:
            self.samples = 0

        def snapshot(self, *, now_s: float) -> VehicleTelemetry:
            del now_s
            self.samples += 1
            link_healthy = self.samples <= 4
            return VehicleTelemetry(
                altitude_agl_m=20.0,
                roll_deg=0.0,
                pitch_deg=0.0,
                ground_speed_mps=1.0,
                in_allowed_zone=True,
                geofence_healthy=True,
                position_healthy=True,
                link_healthy=link_healthy,
                flight_mode_allows_deploy=None,
                release_zone_clear=True,
                armed=True,
                flight_mode="AUTO",
                mission_sequence=3,
            )

    mission = MissionController(_payload_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(8),
        detector=_SafetyDetector(),
        telemetry_provider=_LinkLossTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=8,
            display=False,
            simulate_payload_cycle=True,
            auto_simulate_payload_cycle=True,
            observe_pixhawk_lifecycle=True,
            task_area_mission_sequence=3,
            allowed_auto_modes=("AUTO",),
            person_safety_evidence_qualified=True,
        ),
        operator_bridge=LiveOperatorBridge(
            _DelayedDecisionTransport(),
            OperatorTargetLock(
                geometry,
                TargetLockConfig(frozenset(_payload_config().target_classes)),
            ),
            manual_tracker_factory=_stable_manual_tracker,
        ),
    ).run()

    assert result.authorization_count == 0
    assert result.simulated_payload_cycle_count == 0
    assert mission.fake_payload_port.request_count == 0
    assert mission.status().phase is MissionPhase.SEARCHING
    assert any(
        event.event_type == "operator.remote_authorization_rejected"
        for event in mission.audit.events()
    )


def test_live_camera_mouse_selection_uses_continuous_target_lock(monkeypatch) -> None:
    geometry = VideoGeometry("local-camera", 640, 480)

    class _Backend:
        def __init__(self) -> None:
            self.initial_bbox = None
            self.update_count = 0

        def init(self, _image, bbox) -> bool:
            self.initial_bbox = bbox
            return True

        def update(self, _image):
            self.update_count += 1
            return True, (128.0, 96.0, 128.0, 144.0)

    backend = _Backend()

    def _manual_tracker(geometry):
        return OpenCVManualTargetTracker(
            geometry,
            tracker_factory=lambda: backend,
        )

    class _SelectionUI:
        statuses = []

        def __init__(self) -> None:
            self.sent = False

        def consume_target_command(self, captured, *, now_s):
            if self.sent:
                return None
            self.sent = True
            return TargetSelectionCommand(
                command_id="33333333-3333-4333-8333-333333333333",
                session_id="44444444-4444-4444-8444-444444444444",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=geometry,
                issued_at_s=now_s,
                expires_at_s=now_s + 3.0,
                bbox=BoundingBox(0.15, 0.15, 0.45, 0.55),
                displayed_frame_id=captured.frame_id,
            )

        def render(self, _captured, **state):
            self.statuses.append(state["local_track_status"])
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr(live_module, "OpenCVAuthorizationUI", _SelectionUI)
    monkeypatch.setattr(live_module, "OpenCVManualTargetTracker", _manual_tracker)
    mission = MissionController(_patrol_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(3),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=True),
    ).run()

    assert result.local_selection_count == 1
    assert result.local_tracking_status_count == 3
    assert len(_SelectionUI.statuses) == 3
    assert all(status.state is TrackingState.TRACKING for status in _SelectionUI.statuses)
    assert {status.target_id for status in _SelectionUI.statuses} == {"track-000001"}
    assert backend.initial_bbox == (128, 96, 128, 144)
    assert backend.update_count == 2
    assert any(
        event.event_type == "operator.local_target_selection" for event in mission.audit.events()
    )


def test_live_camera_shadow_tracker_takes_over_detector_dropout(monkeypatch) -> None:
    geometry = VideoGeometry("local-camera", 640, 480)

    class _DropoutDetector(_Detector):
        def __init__(self) -> None:
            self._frame = 0

        def detect(self, image) -> tuple[Detection, ...]:
            self._frame += 1
            return super().detect(image) if self._frame == 1 else ()

    class _Backend:
        def init(self, _image, _bbox) -> bool:
            return True

        def update(self, _image):
            return True, (160.0, 110.0, 128.0, 144.0)

    backend = _Backend()

    def _manual_tracker(current_geometry):
        return OpenCVManualTargetTracker(
            current_geometry,
            tracker_factory=lambda: backend,
        )

    def _fast_loss_config(allowed_labels):
        return TargetLockConfig(
            allowed_labels,
            lost_after_s=1e-9,
        )

    class _SelectionUI:
        statuses = []

        def __init__(self) -> None:
            self.sent = False

        def consume_target_command(self, captured, *, now_s):
            if self.sent:
                return None
            self.sent = True
            return TargetSelectionCommand(
                command_id="99999999-9999-4999-8999-999999999999",
                session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=geometry,
                issued_at_s=now_s,
                expires_at_s=now_s + 3.0,
                bbox=BoundingBox(0.15, 0.15, 0.45, 0.55),
                displayed_frame_id=captured.frame_id,
            )

        def render(self, _captured, **state):
            self.statuses.append(state["local_track_status"])
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr(live_module, "OpenCVAuthorizationUI", _SelectionUI)
    monkeypatch.setattr(live_module, "OpenCVManualTargetTracker", _manual_tracker)
    monkeypatch.setattr(live_module, "TargetLockConfig", _fast_loss_config)
    mission = MissionController(_patrol_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_DropoutDetector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=True),
    ).run()

    assert result.local_selection_count == 1
    assert result.local_tracking_status_count == 2
    assert _SelectionUI.statuses[0].label == "flame"
    assert _SelectionUI.statuses[1].state is TrackingState.TRACKING
    assert _SelectionUI.statuses[1].label == "manual"
    assert _SelectionUI.statuses[1].bbox == BoundingBox(0.25, 110 / 480, 0.45, 254 / 480)
    assert mission.fake_payload_port.request_count == 0


def test_camera_view_converts_drag_switch_and_right_click_cancel_to_commands() -> None:
    class _CV2Events:
        EVENT_LBUTTONDOWN = 1
        EVENT_MOUSEMOVE = 2
        EVENT_LBUTTONUP = 3
        EVENT_RBUTTONUP = 4

    ui = OpenCVAuthorizationUI.__new__(OpenCVAuthorizationUI)
    ui._cv2 = _CV2Events()
    ui._video_width = 640
    ui._video_height = 480
    ui._drag_start = None
    ui._drag_current = None
    ui._pending_selection = None
    ui._last_selection = None
    ui._pending_cancel = False
    ui._has_selection = False
    ui._session_id = "55555555-5555-4555-8555-555555555555"
    ui._selection_sequence = 0
    captured = CapturedFrame("mouse-1", 100.0, object(), 640, 480)

    ui._on_mouse(_CV2Events.EVENT_LBUTTONDOWN, 64, 48, 0, None)
    ui._on_mouse(_CV2Events.EVENT_MOUSEMOVE, 320, 240, 0, None)
    ui._on_mouse(_CV2Events.EVENT_LBUTTONUP, 320, 240, 0, None)
    selected = ui.consume_target_command(captured, now_s=100.0)

    assert selected is not None
    assert selected.action is SelectionAction.SELECT
    assert selected.bbox == BoundingBox(0.1, 0.1, 0.5, 0.5)

    ui._on_mouse(_CV2Events.EVENT_LBUTTONDOWN, 128, 96, 0, None)
    ui._on_mouse(_CV2Events.EVENT_LBUTTONUP, 384, 288, 0, None)
    switched = ui.consume_target_command(captured, now_s=101.0)
    assert switched is not None
    assert switched.action is SelectionAction.SWITCH

    ui._on_mouse(_CV2Events.EVENT_RBUTTONUP, 0, 0, 0, None)
    cancelled = ui.consume_target_command(captured, now_s=102.0)
    assert cancelled is not None
    assert cancelled.action is SelectionAction.CANCEL
    assert cancelled.bbox is None


def test_camera_view_expands_small_but_valid_drag_for_tracker_initialization() -> None:
    class _CV2Events:
        EVENT_LBUTTONDOWN = 1
        EVENT_MOUSEMOVE = 2
        EVENT_LBUTTONUP = 3
        EVENT_RBUTTONUP = 4

    ui = OpenCVAuthorizationUI.__new__(OpenCVAuthorizationUI)
    ui._cv2 = _CV2Events()
    ui._video_width = 640
    ui._video_height = 480
    ui._drag_start = None
    ui._drag_current = None
    ui._pending_selection = None
    ui._last_selection = None
    ui._pending_cancel = False
    ui._has_selection = False
    ui._session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    ui._selection_sequence = 0

    ui._on_mouse(_CV2Events.EVENT_LBUTTONDOWN, 100, 100, 0, None)
    ui._on_mouse(_CV2Events.EVENT_LBUTTONUP, 103, 103, 0, None)
    command = ui.consume_target_command(
        CapturedFrame("tiny-drag", 100.0, object(), 640, 480),
        now_s=100.0,
    )

    assert command is not None
    assert command.bbox is not None
    assert (command.bbox.x2 - command.bbox.x1) * 640 >= 23.9
    assert (command.bbox.y2 - command.bbox.y1) * 480 >= 23.9


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
            person_safety_evidence_qualified=True,
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


def test_live_payload_mode_delegates_to_explicit_authenticated_hil_cycle(monkeypatch) -> None:
    monkeypatch.setattr(live_module, "OpenCVAuthorizationUI", _ScriptedPayloadUI)
    mission = MissionController(_payload_config())
    cycle = _LivePayloadHilCycle(mission)
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(5),
        detector=_SafetyDetector(),
        telemetry_provider=_SafeTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=5,
            display=True,
            simulate_payload_cycle=True,
            person_safety_evidence_qualified=True,
        ),
        payload_hil_cycle=cycle,
    ).run()

    assert result.simulated_payload_cycle_count == 1
    assert cycle.execute_count == 1
    assert cycle.closed is True
    assert mission.state.phase is MissionPhase.SEARCHING
    request_event = next(
        event
        for event in mission.audit.events()
        if event.event_type == "operator.simulated_payload_cycle_requested"
    )
    assert request_event.details["authenticated_controller_hil"] is True


def test_unqualified_person_detector_classes_remain_fail_closed() -> None:
    mission = MissionController(_payload_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_SafetyDetector(),
        telemetry_provider=_SafeTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=4,
            display=False,
            simulate_payload_cycle=True,
            auto_simulate_payload_cycle=True,
        ),
    ).run()

    assert result.authorization_count == 0
    assert result.simulated_payload_cycle_count == 0
    assert mission.fake_payload_port.request_count == 0
    assert mission.state.phase is MissionPhase.SEARCHING
    denied = [
        event
        for event in mission.audit.events()
        if event.event_type == "safety.all_candidates_denied"
    ]
    assert denied
    assert "person-safety detector is not healthy" in str(denied[-1].details)


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

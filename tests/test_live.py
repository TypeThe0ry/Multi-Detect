from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

import numpy as np
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
from multidetect.live import (
    LiveMissionRunner,
    LiveRangingConfig,
    LiveRunConfig,
    OpenCVAuthorizationUI,
)
from multidetect.manual_tracking import OpenCVManualTargetTracker
from multidetect.mission import MissionController
from multidetect.monocular_avoidance import (
    CollisionRiskState,
    MonocularAvoidanceAssessment,
    VisionZone,
    ZoneCollisionRisk,
)
from multidetect.multimodal_ranging import (
    CameraCalibration,
    MultiModalRangingEngine,
    RangeValidity,
)
from multidetect.operator_bridge import LiveOperatorBridge, OperatorBridgeResult
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
from multidetect.operator_status import build_range_status_message
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig
from multidetect.operator_udp import UdpOperatorSelectionServer, UdpOperatorSessionClient
from multidetect.patrol_advisory import PatrolAdvisoryEngine
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
from multidetect.rgb_fire_corroboration import (
    IndependentRgbFireCorroborationConfig,
    IndependentRgbFireCorroborator,
)
from multidetect.selection_target_pool import UnifiedSelectionTargetPool
from multidetect.semantic_environment import (
    AsyncSemanticContextRunner,
    SemanticRegion,
)
from multidetect.short_term_tracking import (
    ShortTermTrackingResult,
    ShortTermTrackingStatus,
)
from multidetect.telemetry import AuthenticatedZoneTelemetryProvider, FailClosedTelemetryProvider
from multidetect.tracking_evaluation import (
    JsonlIdentityPredictionWriter,
    load_identity_prediction_jsonl,
)
from multidetect.unified_tracking import (
    AppearanceEmbedding,
    CameraMotionEstimate,
    TargetMotionHint,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackState,
)
from multidetect.vision import CapturedFrame, DetectorEnsemble
from multidetect.zone_evidence import FileZoneEvidenceProvider, sign_zone_evidence_document

ROOT = Path(__file__).resolve().parents[1]


def test_stale_non_primary_target_range_reason_is_wire_registered() -> None:
    solution = live_module._invalid_live_range_solution(
        target_id="target-stale",
        frame_id="frame-stale",
        calibration_id="camera-main",
        now_s=10.0,
        reason="target_not_freshly_observed",
    )

    status = build_range_status_message(
        sequence=1,
        solution=solution,
        source_captured_at_s=9.9,
    )

    assert status.reasons == ("target_not_freshly_observed",)


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "message"),
    [
        ("max_frames", True, "max_frames"),
        ("performance_window_frames", 1.5, "performance_window_frames"),
        ("task_area_mission_sequence", True, "mission sequence"),
        ("person_reid_frame_stride", True, "person ReID frame stride"),
        ("vehicle_reid_frame_stride", 31, "vehicle ReID frame stride"),
        ("reid_maximum_interval_s", 2.1, "ReID maximum interval"),
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


def test_operator_selection_uses_current_unified_person_and_vehicle_tracks() -> None:
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    pool.update(
        frame_id="operator-unified-1",
        captured_at_s=1.0,
        observations=(
            TargetObservation("person", 0.91, BoundingBox(0.1, 0.1, 0.3, 0.8)),
            TargetObservation("car", 0.86, BoundingBox(0.5, 0.4, 0.9, 0.75)),
        ),
    )
    update = pool.update(
        frame_id="operator-unified-2",
        captured_at_s=1.1,
        observations=(
            TargetObservation("person", 0.92, BoundingBox(0.1, 0.1, 0.3, 0.8)),
            TargetObservation("car", 0.87, BoundingBox(0.5, 0.4, 0.9, 0.75)),
        ),
    )

    adapted = live_module._operator_track_snapshots(update.tracks)

    assert {track.label for track in adapted} == {"person", "car"}
    assert all(track.confirmed for track in adapted)
    assert {track.track_id for track in adapted} == {track.track_id for track in update.tracks}


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


class _LabelDetector:
    def __init__(self, *labels: str) -> None:
        self.labels = labels

    def detect(self, _image) -> tuple[Detection, ...]:
        return tuple(
            Detection(
                label,
                0.90 - index * 0.01,
                BoundingBox(
                    0.1 + index * 0.3,
                    0.2,
                    0.3 + index * 0.3,
                    0.7,
                ),
                SensorKind.RGB,
                "common-object-test-model",
            )
            for index, label in enumerate(self.labels)
        )

    def covers_labels(self, labels) -> bool:
        return set(labels).issubset(self.labels)


def _exclusive_lck_pool(
    label: str,
    bbox: BoundingBox,
) -> tuple[UnifiedTargetPool, UnifiedSelectionTargetPool]:
    now_s = time.monotonic()
    target_pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    target_pool.update(
        frame_id="locked-preload",
        captured_at_s=now_s - 0.01,
        observations=(TargetObservation(label=label, confidence=0.95, bbox=bbox),),
    )
    # A new target starts in DETECTED even when the pool uses one confirmed hit;
    # promote it only after a fresh follow-up observation has made its state and
    # quality eligible for an exclusive LCK route.
    target_pool.update(
        frame_id="locked-preload-confirmed",
        captured_at_s=now_s - 0.005,
        observations=(TargetObservation(label=label, confidence=0.95, bbox=bbox),),
    )
    selection_pool = UnifiedSelectionTargetPool(target_pool)
    command = TargetSelectionCommand(
        command_id=f"lock-{label}",
        session_id="lock-model-routing-session",
        sequence=1,
        action=SelectionAction.PROMOTE_LCK,
        geometry=VideoGeometry("camera-main", 640, 480),
        issued_at_s=now_s,
        expires_at_s=now_s + 1.0,
        bbox=bbox,
    )
    selection_pool.consume_bridge_result(
        OperatorBridgeResult(
            accepted_command_count=1,
            published_statuses=(),
            published_mission_statuses=(),
            published_safety_statuses=(),
            accepted_authorization_decisions=(),
            published_authorization_challenges=(),
            transport_errors=(),
            accepted_selection_commands=((command, ("127.0.0.1", 14580)),),
        ),
        now_s=now_s + 0.001,
    )
    assert selection_pool.exclusive_lock_track_id is not None
    return target_pool, selection_pool


class _IdleOperatorTransport:
    def start_background(self) -> None:
        pass

    def poll_selection(self):
        return None

    def poll_error(self):
        return None

    def publish_track_status(self, _status, *, peer) -> None:
        pass

    def publish_mission_status(self, _status, *, peer) -> None:
        pass

    def publish_safety_status(self, _status, *, peer) -> None:
        pass

    def close(self) -> None:
        pass


def _idle_operator_bridge() -> LiveOperatorBridge:
    geometry = VideoGeometry("camera-main", 640, 480)
    return LiveOperatorBridge(
        _IdleOperatorTransport(),
        OperatorTargetLock(
            geometry,
            TargetLockConfig(frozenset({"person", "car", "vehicle", "chair", "flame"})),
        ),
    )


class _ScriptedMonocularAvoidance:
    def __init__(self, states: tuple[CollisionRiskState, ...]) -> None:
        self._states = deque(states)
        self.calls = 0

    def update(
        self,
        _image,
        *,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
    ) -> MonocularAvoidanceAssessment:
        self.calls += 1
        state = self._states.popleft()
        zones = tuple(
            ZoneCollisionRisk(
                zone=zone,
                state=state,
                feature_count=30,
                outward_feature_count=(10 if state is not CollisionRiskState.CLEAR else 0),
                ttc_s=(1.0 if state is CollisionRiskState.AVOID else None),
                confidence=0.9,
            )
            for zone in VisionZone
        )
        return MonocularAvoidanceAssessment(
            frame_id=frame_id,
            state=state,
            zones=zones,
            captured_at_s=captured_at_s,
            produced_at_s=max(captured_at_s, produced_at_s),
            data_age_s=max(0.0, produced_at_s - captured_at_s),
            frame_interval_s=0.05,
            valid_feature_count=90,
            rotation_compensated=True,
            processing_time_ms=4.0,
        )


class _FailingMonocularAvoidance:
    def update(self, _image, **_kwargs) -> MonocularAvoidanceAssessment:
        raise RuntimeError("synthetic avoidance failure")


class _FailingPersonReId:
    def encode_detections(self, _image, _detections):
        raise RuntimeError("synthetic ReID failure")


class _ScriptedPersonReId:
    def __init__(self) -> None:
        self.calls = 0

    def encode_detections(self, _image, detections):
        self.calls += 1
        return tuple(
            TargetObservation.from_detection(
                detection,
                appearance=(
                    AppearanceEmbedding((1.0, 0.1, 0.0, 0.0))
                    if detection.label == "person"
                    else None
                ),
                appearance_reliable=detection.label == "person",
            )
            for detection in detections
        )


class _ScriptedVehicleReId:
    def __init__(self) -> None:
        self.calls = 0

    def encode_detections(self, _image, detections):
        self.calls += 1
        return tuple(
            TargetObservation.from_detection(
                detection,
                appearance=(
                    AppearanceEmbedding((0.0, 1.0, 0.1, 0.0, 0.0))
                    if detection.label in {"car", "bus", "truck", "vehicle"}
                    else None
                ),
                appearance_reliable=detection.label in {"car", "bus", "truck", "vehicle"},
            )
            for detection in detections
        )


class _ScriptedAircraftAppearance:
    def __init__(self) -> None:
        self.calls = 0
        self.config = type(
            "Config",
            (),
            {"allowed_labels": frozenset({"aircraft", "airplane", "plane"})},
        )()

    def encode_detections(self, _image, detections):
        self.calls += 1
        return tuple(
            TargetObservation.from_detection(
                detection,
                appearance=(
                    AppearanceEmbedding((0.0, 0.0, 1.0, 0.1))
                    if detection.label in self.config.allowed_labels
                    else None
                ),
                appearance_reliable=detection.label in self.config.allowed_labels,
            )
            for detection in detections
        )


class _FailingVehicleReId:
    def encode_detections(self, _image, _detections):
        raise RuntimeError("synthetic vehicle ReID failure")


class _ScriptedShortTermTracker:
    def __init__(self) -> None:
        self.calls = 0
        self.synchronized = []
        self.update_kwargs = []

    def update_frame(self, _image, **_kwargs) -> ShortTermTrackingResult:
        self.calls += 1
        self.update_kwargs.append(dict(_kwargs))
        hints = (
            ()
            if self.calls == 1
            else (
                TargetMotionHint(
                    track_id="target-000001",
                    residual_dx=0.0,
                    residual_dy=0.0,
                    confidence=0.9,
                ),
            )
        )
        return ShortTermTrackingResult(
            status=(
                ShortTermTrackingStatus.WARMUP if self.calls == 1 else ShortTermTrackingStatus.OK
            ),
            hints=hints,
            attempted_track_count=0 if self.calls == 1 else 1,
            optical_flow_hint_count=0 if self.calls == 1 else 1,
            template_hint_count=0,
            processing_time_ms=2.0,
            frame_interval_s=None if self.calls == 1 else 0.05,
        )

    def synchronize_tracks(self, tracks, **_kwargs) -> None:
        self.synchronized.append(tuple(tracks))


class _FailingShortTermTracker:
    def update_frame(self, _image, **_kwargs) -> ShortTermTrackingResult:
        raise RuntimeError("synthetic short-term tracking failure")

    def synchronize_tracks(self, _tracks) -> None:
        pass


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


class _RgbFireVerifier:
    def detect(self, _image) -> tuple[Detection, ...]:
        return (
            Detection(
                "flame",
                0.93,
                BoundingBox(0.2, 0.2, 0.4, 0.5),
                SensorKind.RGB,
                "test-rgb-fire-verifier",
            ),
        )


class _FailingRgbFireVerifier:
    def detect(self, _image) -> tuple[Detection, ...]:
        raise RuntimeError("synthetic RGB fire verifier failure")


def _rgb_fire_verification_kwargs(*, failing: bool = False) -> dict[str, object]:
    return {
        "rgb_fire_verifier": (_FailingRgbFireVerifier() if failing else _RgbFireVerifier()),
        "rgb_fire_corroborator": IndependentRgbFireCorroborator(
            IndependentRgbFireCorroborationConfig(
                evidence_qualified=True,
                primary_artifact_sha256="1" * 64,
                verifier_artifact_sha256="2" * 64,
            )
        ),
    }


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


class _TimestampedRangingTelemetryProvider:
    def __init__(self, *, include_timestamps: bool = True) -> None:
        self.include_timestamps = include_timestamps

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        observed_at_s = now_s if self.include_timestamps else float("nan")
        return VehicleTelemetry(
            altitude_agl_m=50.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=15.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=False,
            release_zone_clear=None,
            heading_deg=20.0,
            attitude_observed_at_s=observed_at_s,
            position_observed_at_s=observed_at_s,
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
            self.range_published = []
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

        def publish_range_status(self, status, *, peer) -> None:
            self.range_published.append((status, peer))

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
    unified_target_pool = UnifiedTargetPool()
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(5),
        detector=_SafetyDetector(),
        **_rgb_fire_verification_kwargs(),
        telemetry_provider=_TimestampedRangingTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=5,
            display=False,
            person_safety_evidence_qualified=True,
        ),
        operator_bridge=bridge,
        unified_target_pool=unified_target_pool,
        selection_target_pool=UnifiedSelectionTargetPool(unified_target_pool),
        ranging_engine=MultiModalRangingEngine(),
        ranging_config=_live_ranging_config(),
    ).run()

    assert result.remote_selection_count == 1
    assert result.remote_tracking_status_count == 5
    assert 1 <= result.remote_mission_status_count <= 5
    assert 1 <= result.remote_safety_status_count <= 5
    assert result.remote_transport_error_count == 0
    assert result.remote_range_status_count >= 1
    assert result.selection_target_pool_binding_count == 1
    assert result.selection_target_pool_error_count == 0
    assert unified_target_pool.primary_track_id is not None
    assert sum(track.locked for track in unified_target_pool.snapshots()) == 1
    assert len(transport.published) == 5
    assert len(transport.mission_published) == result.remote_mission_status_count
    assert len(transport.safety_published) == result.remote_safety_status_count
    assert len(transport.range_published) == result.remote_range_status_count
    assert all(status.advisory_only for status, _peer in transport.mission_published)
    assert all(status.advisory_only for status, _peer in transport.safety_published)
    assert all(
        status.validity is RangeValidity.DEGRADED for status, _peer in transport.range_published
    )
    assert all(
        status.flight_control_enabled is False for status, _peer in transport.range_published
    )
    assert {status.target_id for status, _peer in transport.published} == {"target-000001"}
    assert transport.closed is True
    assert any(
        event.event_type == "operator.remote_tracking_status" for event in mission.audit.events()
    )
    assert any(
        event.event_type == "operator.remote_safety_status" for event in mission.audit.events()
    )
    assert any(
        event.event_type == "operator.remote_range_status" for event in mission.audit.events()
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
            **_rgb_fire_verification_kwargs(),
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
        **_rgb_fire_verification_kwargs(),
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
        **_rgb_fire_verification_kwargs(),
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
        **_rgb_fire_verification_kwargs(),
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
        **_rgb_fire_verification_kwargs(),
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


def test_required_rgb_fire_verifier_absence_keeps_payload_path_fail_closed() -> None:
    mission = MissionController(_payload_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_SafetyDetector(),
        telemetry_provider=_SafeTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=4,
            display=False,
            person_safety_evidence_qualified=True,
        ),
    ).run()

    assert result.processed_frames == 4
    assert result.authorization_count == 0
    assert result.rgb_fire_verifier_unavailable_frame_count == 4
    assert result.rgb_fire_verifier_inference_count == 0
    assert any(
        event.event_type == "perception.rgb_fire_verifier_unavailable"
        for event in mission.audit.events()
    )


def test_rgb_fire_verifier_failure_preserves_patrol_but_denies_payload_path() -> None:
    mission = MissionController(_payload_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_SafetyDetector(),
        **_rgb_fire_verification_kwargs(failing=True),
        telemetry_provider=_SafeTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=4,
            display=False,
            person_safety_evidence_qualified=True,
        ),
    ).run()

    assert result.processed_frames == 4
    assert result.authorization_count == 0
    assert result.rgb_fire_verifier_assessment_count == 4
    assert result.rgb_fire_verifier_failure_count == 4
    assert result.rgb_fire_verifier_corroborated_detection_count == 0
    assert any(
        event.event_type == "perception.rgb_fire_verifier_failed"
        for event in mission.audit.events()
    )


def test_rgb_fire_verifier_skips_non_fire_frames_without_counting_a_failure() -> None:
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_LabelDetector("person"),
        **_rgb_fire_verification_kwargs(failing=True),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
    ).run()

    assert result.processed_frames == 4
    assert result.rgb_fire_verifier_assessment_count == 0
    assert result.rgb_fire_verifier_skipped_no_candidate_frame_count == 4
    assert result.rgb_fire_verifier_inference_count == 0
    assert result.rgb_fire_verifier_failure_count == 0


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


def test_live_target_pool_records_every_identity_prediction_frame(tmp_path: Path) -> None:
    path = tmp_path / "identity-tracks.jsonl"
    writer = JsonlIdentityPredictionWriter(path)
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        unified_target_pool=UnifiedTargetPool(),
        identity_prediction_writer=writer,
    ).run()
    writer.close()

    frames = load_identity_prediction_jsonl(path)
    assert result.processed_frames == 4
    assert result.identity_tracking_log_frame_count == 4
    assert result.identity_tracking_log_error_count == 0
    assert result.identity_tracking_log_disabled_after_error is False
    assert len(frames) == 4
    assert [frame.frame_id for frame in frames] == [f"live-{index}" for index in range(1, 5)]
    assert all(frame.tracks for frame in frames)
    assert {frame.tracks[0].track_id for frame in frames} == {"target-000001"}


def test_live_identity_prediction_writer_requires_target_pool(tmp_path: Path) -> None:
    writer = JsonlIdentityPredictionWriter(tmp_path / "identity-tracks.jsonl")
    try:
        with pytest.raises(ValueError, match="requires the unified target pool"):
            LiveMissionRunner(
                mission=MissionController(_patrol_config()),
                frame_source=_FrameSource(1),
                detector=_Detector(),
                telemetry_provider=FailClosedTelemetryProvider(),
                config=LiveRunConfig(max_frames=1, display=False),
                identity_prediction_writer=writer,
            )
    finally:
        writer.close()


def test_live_identity_log_failure_is_isolated_from_perception() -> None:
    class FailingIdentityWriter:
        def __init__(self) -> None:
            self.calls = 0

        def append(self, **_kwargs) -> None:
            self.calls += 1
            raise OSError("disk full")

    mission = MissionController(_patrol_config())
    writer = FailingIdentityWriter()
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(3),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=False),
        unified_target_pool=UnifiedTargetPool(),
        identity_prediction_writer=writer,  # type: ignore[arg-type]
    ).run()

    assert result.processed_frames == 3
    assert result.unified_target_pool_update_count == 3
    assert result.identity_tracking_log_frame_count == 0
    assert result.identity_tracking_log_error_count == 1
    assert result.identity_tracking_log_disabled_after_error is True
    assert writer.calls == 1
    failures = [
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.identity_prediction_log_failed"
    ]
    assert len(failures) == 1
    assert failures[0].details["perception_continues"] is True
    assert failures[0].details["flight_control_enabled"] is False


def test_live_monocular_avoidance_is_advisory_and_audits_state_changes() -> None:
    mission = MissionController(_patrol_config())
    avoidance = _ScriptedMonocularAvoidance(
        (
            CollisionRiskState.CLEAR,
            CollisionRiskState.CLEAR,
            CollisionRiskState.AVOID,
        )
    )

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(3),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=False),
        monocular_avoidance=avoidance,
    ).run()

    assert avoidance.calls == 3
    assert result.monocular_avoidance_assessment_count == 3
    assert result.monocular_avoidance_avoid_count == 1
    assert result.monocular_avoidance_invalid_count == 0
    assert result.monocular_avoidance_latency_p95_ms >= 4.0
    changes = [
        event for event in mission.audit.events() if event.event_type == "avoidance.state_changed"
    ]
    assert [event.details["state"] for event in changes] == ["clear", "avoid"]
    assert all(event.details["advisory_only"] is True for event in changes)
    assert all(event.details["flight_control_enabled"] is False for event in changes)


def test_live_monocular_avoidance_failure_fails_closed_without_stopping_detection() -> None:
    mission = MissionController(_patrol_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        monocular_avoidance=_FailingMonocularAvoidance(),
    ).run()

    assert result.processed_frames == 2
    assert result.monocular_avoidance_assessment_count == 2
    assert result.monocular_avoidance_invalid_count == 2
    assert result.monocular_avoidance_error_count == 2
    failures = [
        event
        for event in mission.audit.events()
        if event.event_type == "avoidance.processing_failed"
    ]
    assert len(failures) == 2
    assert all(event.details["flight_control_enabled"] is False for event in failures)


def test_live_unified_target_pool_updates_metadata_without_enabling_control() -> None:
    mission = MissionController(_patrol_config())
    target_pool = UnifiedTargetPool()

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        unified_target_pool=target_pool,
    ).run()

    assert result.processed_frames == 4
    assert result.unified_target_pool_update_count == 4
    assert result.unified_target_pool_error_count == 0
    assert result.unified_target_pool_maximum_track_count == 1
    assert result.unified_target_pool_created_track_count == 1
    assert result.unified_target_pool_association_p95_ms >= 0.0
    changes = [
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.unified_target_pool_changed"
    ]
    assert len(changes) == 1
    assert changes[0].details["metadata_only"] is True
    assert changes[0].details["flight_control_enabled"] is False


def _seed_locked_primary_target(pool: UnifiedTargetPool) -> None:
    seeded_at_s = time.monotonic()
    update = pool.update(
        frame_id="range-seed",
        captured_at_s=seeded_at_s,
        observations=tuple(
            TargetObservation.from_detection(detection) for detection in _Detector().detect(None)
        ),
    )
    pool.lock(update.created_track_ids[0], now_s=seeded_at_s)


def _live_ranging_config() -> LiveRangingConfig:
    return LiveRangingConfig(
        calibration=CameraCalibration(
            calibration_id="camera-main-test-v1",
            width_px=640,
            height_px=480,
            fx_px=500.0,
            fy_px=500.0,
            cx_px=320.0,
            cy_px=240.0,
            mount_pitch_down_deg=35.0,
        )
    )


def test_live_primary_target_ranging_is_timestamped_degraded_and_read_only() -> None:
    mission = MissionController(_patrol_config())
    target_pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    _seed_locked_primary_target(target_pool)

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(1),
        detector=_Detector(),
        telemetry_provider=_TimestampedRangingTelemetryProvider(),
        config=LiveRunConfig(max_frames=1, display=False),
        unified_target_pool=target_pool,
        ranging_engine=MultiModalRangingEngine(),
        ranging_config=_live_ranging_config(),
    ).run()

    assert result.ranging_assessment_count == 1
    assert result.ranging_valid_count == 0
    assert result.ranging_degraded_count == 1
    assert result.ranging_invalid_count == 0
    assert result.ranging_error_count == 0
    solution_events = [
        event
        for event in mission.audit.events()
        if event.event_type == "ranging.primary_target_solution"
    ]
    assert len(solution_events) == 1
    details = solution_events[0].details
    assert details["validity"] == RangeValidity.DEGRADED.value
    assert details["slant_range_m"] is not None
    assert details["slant_range_ci95_m"] is not None
    assert details["sources"] == ("pixhawk_agl", "camera_ground")
    assert details["advisory_only"] is True
    assert details["flight_control_enabled"] is False
    assert details["physical_release_enabled"] is False


def test_live_ranging_isolates_one_target_failure_from_other_target_metadata() -> None:
    """One malformed target must not blank metric metadata for its neighbours."""

    class _TwoTargetDetector:
        def detect(self, _image) -> tuple[Detection, ...]:
            return (
                Detection(
                    "person",
                    0.94,
                    BoundingBox(0.10, 0.15, 0.30, 0.82),
                    SensorKind.RGB,
                    "two-target-test-model",
                ),
                Detection(
                    "car",
                    0.91,
                    BoundingBox(0.55, 0.42, 0.88, 0.76),
                    SensorKind.RGB,
                    "two-target-test-model",
                ),
            )

        def covers_labels(self, _labels) -> bool:
            return False

    class _PacedFrameSource(_FrameSource):
        def read(self) -> CapturedFrame:
            # Let the bridge's bounded target-pool heartbeat elapse so the
            # second, non-empty snapshot is published through the real path.
            time.sleep(0.12)
            return super().read()

    class _FirstTargetBearingFailureEngine(MultiModalRangingEngine):
        @staticmethod
        def relative_bearing_deg(*, calibration, target) -> float:
            if target.center_x < 0.5:
                raise RuntimeError("synthetic first-target bearing failure")
            return MultiModalRangingEngine.relative_bearing_deg(
                calibration=calibration,
                target=target,
            )

    class _TargetPoolTransport:
        def __init__(self) -> None:
            issued_at_s = time.monotonic()
            geometry = VideoGeometry("camera-main", 640, 480)
            self._commands = deque(
                (
                    (
                        TargetSelectionCommand(
                            command_id="11111111-1111-4111-8111-111111111118",
                            session_id="22222222-2222-4222-8222-222222222228",
                            sequence=1,
                            action=SelectionAction.SELECT,
                            geometry=geometry,
                            issued_at_s=issued_at_s,
                            expires_at_s=issued_at_s + 3.0,
                            bbox=BoundingBox(0.55, 0.42, 0.88, 0.76),
                        ),
                        ("127.0.0.1", 14580),
                    ),
                )
            )
            self.target_pool_published = []

        def start_background(self) -> None:
            pass

        def poll_selection(self):
            return self._commands.popleft() if self._commands else None

        def poll_error(self):
            return None

        def publish_track_status(self, _status, *, peer) -> None:
            pass

        def publish_mission_status(self, _status, *, peer) -> None:
            pass

        def publish_safety_status(self, _status, *, peer) -> None:
            pass

        def publish_range_status(self, _status, *, peer) -> None:
            pass

        def publish_target_pool_status(self, status, *, peer) -> None:
            self.target_pool_published.append((status, peer))

        def close(self) -> None:
            pass

    geometry = VideoGeometry("camera-main", 640, 480)
    transport = _TargetPoolTransport()
    bridge = LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            geometry,
            TargetLockConfig(frozenset({"person", "car"})),
        ),
    )
    mission = MissionController(_patrol_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_PacedFrameSource(3),
        detector=_TwoTargetDetector(),
        telemetry_provider=_TimestampedRangingTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=False),
        operator_bridge=bridge,
        unified_target_pool=UnifiedTargetPool(
            UnifiedTargetPoolConfig(minimum_confirmed_hits=1)
        ),
        ranging_engine=_FirstTargetBearingFailureEngine(),
        ranging_config=_live_ranging_config(),
    ).run()

    assert result.ranging_error_count >= 1
    assert result.unified_target_pool_maximum_track_count == 2, result
    errors = [
        event
        for event in mission.audit.events()
        if event.event_type == "ranging.processing_failed"
    ]
    assert errors
    assert all(event.details["target_id"] != "" for event in errors)
    assert all(event.details["isolated_target_failure"] is True for event in errors)

    assert transport.target_pool_published, result
    latest_entries = {
        entry.label: entry
        for status, _peer in transport.target_pool_published
        for entry in status.entries
    }
    published_metrics = [
        (
            status.pool_revision,
            tuple(
                (
                    entry.label,
                    entry.relative_bearing_deg,
                    entry.estimated_range_m,
                    entry.target_speed_mps,
                )
                for entry in status.entries
            ),
        )
        for status, _peer in transport.target_pool_published
    ]
    assert {"person", "car"}.issubset(latest_entries), published_metrics
    assert latest_entries["person"].relative_bearing_deg is None
    assert latest_entries["car"].relative_bearing_deg is not None, published_metrics
    assert latest_entries["car"].estimated_range_m is not None, published_metrics
    assert latest_entries["car"].target_speed_mps is not None, published_metrics


def test_live_ranging_rejects_telemetry_without_observation_timestamps() -> None:
    mission = MissionController(_patrol_config())
    target_pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    _seed_locked_primary_target(target_pool)

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(1),
        detector=_Detector(),
        telemetry_provider=_TimestampedRangingTelemetryProvider(include_timestamps=False),
        config=LiveRunConfig(max_frames=1, display=False),
        unified_target_pool=target_pool,
        ranging_engine=MultiModalRangingEngine(),
        ranging_config=_live_ranging_config(),
    ).run()

    assert result.ranging_assessment_count == 1
    assert result.ranging_invalid_count == 1
    solution = next(
        event
        for event in mission.audit.events()
        if event.event_type == "ranging.primary_target_solution"
    )
    assert solution.details["validity"] == RangeValidity.INVALID.value
    assert solution.details["reasons"] == ("pixhawk_pose_or_timestamp_unavailable",)
    assert solution.details["slant_range_m"] is None


def test_live_ranging_requires_target_pool_and_complete_configuration() -> None:
    mission = MissionController(_patrol_config())
    common = {
        "mission": mission,
        "frame_source": _FrameSource(1),
        "detector": _Detector(),
        "telemetry_provider": _TimestampedRangingTelemetryProvider(),
        "config": LiveRunConfig(max_frames=1, display=False),
    }

    with pytest.raises(ValueError, match="supplied together"):
        LiveMissionRunner(**common, ranging_engine=MultiModalRangingEngine())
    with pytest.raises(ValueError, match="unified target pool"):
        LiveMissionRunner(
            **common,
            ranging_engine=MultiModalRangingEngine(),
            ranging_config=_live_ranging_config(),
        )


def test_live_passes_visual_camera_motion_into_unified_association() -> None:
    motion = CameraMotionEstimate(
        dx=0.035,
        dy=-0.02,
        scale=1.04,
        confidence=0.9,
        rotation_deg=-10.0,
        aspect_ratio=16.0 / 9.0,
        affine=(1.03, 0.035, -0.02, 0.97),
    )

    class _MovingDetector(_Detector):
        def __init__(self) -> None:
            self._centers = deque(((0.20, 0.35), motion.transform_point(0.20, 0.35)))

        def detect(self, _image) -> tuple[Detection, ...]:
            center_x, center_y = self._centers.popleft()
            return (
                Detection(
                    "flame",
                    0.95,
                    BoundingBox(center_x - 0.04, center_y - 0.05, center_x + 0.04, center_y + 0.05),
                    SensorKind.RGB,
                    "test-model",
                ),
            )

    class _CameraMotionAvoidance:
        def __init__(self) -> None:
            self._calls = 0

        def update(
            self,
            _image,
            *,
            frame_id: str,
            captured_at_s: float,
            produced_at_s: float,
        ) -> MonocularAvoidanceAssessment:
            self._calls += 1
            return MonocularAvoidanceAssessment(
                frame_id=frame_id,
                state=CollisionRiskState.CLEAR,
                zones=(),
                captured_at_s=captured_at_s,
                produced_at_s=max(captured_at_s, produced_at_s),
                data_age_s=max(0.0, produced_at_s - captured_at_s),
                frame_interval_s=0.05,
                valid_feature_count=50,
                rotation_compensated=True,
                processing_time_ms=1.0,
                camera_motion_dx=(motion.dx if self._calls == 2 else 0.0),
                camera_motion_dy=(motion.dy if self._calls == 2 else 0.0),
                camera_motion_scale=(motion.scale if self._calls == 2 else 1.0),
                camera_motion_confidence=0.9,
                camera_motion_rotation_deg=(motion.rotation_deg if self._calls == 2 else 0.0),
                camera_motion_aspect_ratio=motion.aspect_ratio,
                camera_motion_affine=(motion.affine if self._calls == 2 else None),
            )

    target_pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.1,
            maximum_center_distance=0.06,
        )
    )
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(2),
        detector=_MovingDetector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        monocular_avoidance=_CameraMotionAvoidance(),
        unified_target_pool=target_pool,
    ).run()

    assert result.unified_target_pool_created_track_count == 1
    assert result.unified_target_pool_maximum_track_count == 1


def test_live_person_reid_failure_falls_back_to_motion_only() -> None:
    mission = MissionController(_patrol_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_LabelDetector("person"),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        unified_target_pool=UnifiedTargetPool(),
        person_reid_encoder=_FailingPersonReId(),
    ).run()

    assert result.processed_frames == 2
    assert result.person_reid_failure_count == 2
    assert result.unified_target_pool_update_count == 2
    failures = [
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.person_reid_failed"
    ]
    assert len(failures) == 2
    assert all(event.details["fallback"] == "motion_only" for event in failures)
    assert all(event.details["identity_recovery_enabled"] is False for event in failures)
    assert all(event.details["flight_control_enabled"] is False for event in failures)


def test_live_reid_stable_cadence_skips_frames_without_disabling_target_pool() -> None:
    reid = _ScriptedPersonReId()
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(5),
        detector=_LabelDetector("person"),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=5,
            display=False,
            person_reid_frame_stride=3,
            reid_maximum_interval_s=2.0,
        ),
        unified_target_pool=UnifiedTargetPool(),
        person_reid_encoder=reid,
    ).run()

    assert result.processed_frames == 5
    assert reid.calls == 2
    assert result.person_reid_inference_count == 2
    assert result.person_reid_skipped_frame_count == 3
    assert result.person_reid_forced_recovery_count == 0
    assert result.person_reid_latency_p50_ms >= 0.0
    assert result.person_reid_latency_p95_ms >= result.person_reid_latency_p50_ms
    assert result.unified_target_pool_update_count == 5


@pytest.mark.parametrize(
    (
        "label",
        "expected_profile",
        "expected_person_calls",
        "expected_vehicle_calls",
        "expected_aircraft_calls",
        "expected_specialized_reid",
    ),
    (
        ("person", "person_specialist", 4, 0, 0, True),
        ("car", "vehicle_specialist", 0, 4, 0, True),
        ("airplane", "aircraft_specialist", 0, 0, 4, True),
    ),
)
def test_exclusive_lck_runs_only_matching_reid_domain_at_full_frame_rate(
    label: str,
    expected_profile: str,
    expected_person_calls: int,
    expected_vehicle_calls: int,
    expected_aircraft_calls: int,
    expected_specialized_reid: bool,
) -> None:
    bbox = BoundingBox(0.1, 0.2, 0.3, 0.7)
    target_pool, selection_pool = _exclusive_lck_pool(label, bbox)
    person_reid = _ScriptedPersonReId()
    vehicle_reid = _ScriptedVehicleReId()
    aircraft_appearance = _ScriptedAircraftAppearance()
    short_term_tracker = _ScriptedShortTermTracker()
    mission = MissionController(_patrol_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(4),
        detector=_LabelDetector(label),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=4,
            display=False,
            person_reid_frame_stride=30,
            vehicle_reid_frame_stride=30,
            reid_maximum_interval_s=2.0,
        ),
        operator_bridge=_idle_operator_bridge(),
        unified_target_pool=target_pool,
        selection_target_pool=selection_pool,
        person_reid_encoder=person_reid,
        vehicle_reid_encoder=vehicle_reid,
        aircraft_appearance_encoder=aircraft_appearance,
        short_term_tracker=short_term_tracker,
    ).run()

    assert person_reid.calls == expected_person_calls
    assert vehicle_reid.calls == expected_vehicle_calls
    assert aircraft_appearance.calls == expected_aircraft_calls
    assert result.person_reid_inference_count == expected_person_calls
    assert result.vehicle_reid_inference_count == expected_vehicle_calls
    assert short_term_tracker.calls == 4
    profile_events = [
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.lock_model_profile_changed"
    ]
    assert profile_events[0].details["profile"] == expected_profile
    assert profile_events[0].details["specialized_reid_enabled"] is expected_specialized_reid
    assert profile_events[0].details["fallback"] == "arbitrary_object_tracker"


def test_unclassified_lck_pauses_learned_detectors_and_uses_arbitrary_tracker() -> None:
    class _CommonDetector:
        class_names = ("person", "car", "chair")

        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _image):
            self.calls += 1
            return (Detection("chair", 0.95, BoundingBox(0.1, 0.2, 0.3, 0.7)),)

    bbox = BoundingBox(0.1, 0.2, 0.3, 0.7)
    target_pool, selection_pool = _exclusive_lck_pool("chair", bbox)
    common_detector = _CommonDetector()
    detector = DetectorEnsemble((common_detector,))
    person_reid = _ScriptedPersonReId()
    vehicle_reid = _ScriptedVehicleReId()
    short_term_tracker = _ScriptedShortTermTracker()
    mission = MissionController(_patrol_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=detector,
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        operator_bridge=_idle_operator_bridge(),
        unified_target_pool=target_pool,
        selection_target_pool=selection_pool,
        person_reid_encoder=person_reid,
        vehicle_reid_encoder=vehicle_reid,
        short_term_tracker=short_term_tracker,
    ).run()

    assert common_detector.calls == 0
    assert detector.active_detector_count == 0
    assert person_reid.calls == vehicle_reid.calls == 0
    assert short_term_tracker.calls == 2
    assert result.short_term_tracking_update_count == 2
    profile_events = [
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.lock_model_profile_changed"
    ]
    assert profile_events[0].details["profile"] == "arbitrary_object_fallback"
    assert profile_events[0].details["active_detector_count"] == 0
    assert profile_events[0].details["generic_tracker_enabled"] is True


def test_aircraft_lck_keeps_matching_common_detector_and_appearance_route_active() -> None:
    class _CommonDetector:
        class_names = ("person", "airplane", "chair")

        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _image):
            self.calls += 1
            return (Detection("airplane", 0.95, BoundingBox(0.1, 0.2, 0.3, 0.7)),)

    bbox = BoundingBox(0.1, 0.2, 0.3, 0.7)
    target_pool, selection_pool = _exclusive_lck_pool("airplane", bbox)
    common_detector = _CommonDetector()
    detector = DetectorEnsemble((common_detector,))
    aircraft_appearance = _ScriptedAircraftAppearance()
    mission = MissionController(_patrol_config())

    LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(3),
        detector=detector,
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=False),
        operator_bridge=_idle_operator_bridge(),
        unified_target_pool=target_pool,
        selection_target_pool=selection_pool,
        aircraft_appearance_encoder=aircraft_appearance,
    ).run()

    assert common_detector.calls == 3
    assert detector.active_detector_count == 1
    assert aircraft_appearance.calls == 3
    profile_event = next(
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.lock_model_profile_changed"
    )
    assert profile_event.details["family"] == "aircraft"
    assert profile_event.details["profile"] == "aircraft_specialist"
    assert profile_event.details["detector_route_applied"] is True
    assert profile_event.details["active_detector_count"] == 1


def test_fire_lck_keeps_only_fire_detector_route_active() -> None:
    class _CommonDetector:
        class_names = ("person", "flame", "chair")

        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _image):
            self.calls += 1
            return (Detection("flame", 0.95, BoundingBox(0.1, 0.2, 0.3, 0.7)),)

    bbox = BoundingBox(0.1, 0.2, 0.3, 0.7)
    target_pool, selection_pool = _exclusive_lck_pool("flame", bbox)
    common_detector = _CommonDetector()
    detector = DetectorEnsemble((common_detector,))
    mission = MissionController(_patrol_config())

    LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(3),
        detector=detector,
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=3, display=False),
        operator_bridge=_idle_operator_bridge(),
        unified_target_pool=target_pool,
        selection_target_pool=selection_pool,
    ).run()

    assert common_detector.calls == 3
    assert detector.active_detector_count == 1
    profile_event = next(
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.lock_model_profile_changed"
    )
    assert profile_event.details["family"] == "fire"
    assert profile_event.details["profile"] == "fire_specialist"
    assert profile_event.details["detector_route_applied"] is True
    assert profile_event.details["active_detector_count"] == 1


def test_live_reid_skips_absent_identity_domains_without_fake_inference_counts() -> None:
    person_reid = _ScriptedPersonReId()
    vehicle_reid = _ScriptedVehicleReId()
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(4),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        unified_target_pool=UnifiedTargetPool(),
        person_reid_encoder=person_reid,
        vehicle_reid_encoder=vehicle_reid,
    ).run()

    assert person_reid.calls == 0
    assert vehicle_reid.calls == 0
    assert result.person_reid_inference_count == 0
    assert result.vehicle_reid_inference_count == 0
    assert result.person_reid_skipped_frame_count == 0
    assert result.vehicle_reid_skipped_frame_count == 0
    assert result.person_reid_no_candidate_frame_count == 4
    assert result.vehicle_reid_no_candidate_frame_count == 4
    assert result.person_reid_latency_p95_ms == 0.0
    assert result.vehicle_reid_latency_p95_ms == 0.0
    assert result.unified_target_pool_update_count == 4


def test_live_reid_forces_identity_pass_when_candidate_returns_after_occlusion() -> None:
    class _OcclusionThenPersonDetector:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, image) -> tuple[Detection, ...]:
            self.calls += 1
            if self.calls == 1:
                return _Detector().detect(image)
            return _LabelDetector("person").detect(image)

        def covers_labels(self, _labels) -> bool:
            return False

    target_pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=1.0,
        )
    )
    person_detection = _LabelDetector("person").detect(None)[0]
    now_s = time.monotonic()
    for index, captured_at_s in enumerate((now_s - 0.6, now_s - 0.5), start=1):
        target_pool.update(
            frame_id=f"preload-{index}",
            captured_at_s=captured_at_s,
            observations=(TargetObservation.from_detection(person_detection),),
        )
    assert target_pool.snapshots()[0].state is UnifiedTrackState.TRACKING

    person_reid = _ScriptedPersonReId()
    result = LiveMissionRunner(
        mission=MissionController(_patrol_config()),
        frame_source=_FrameSource(2),
        detector=_OcclusionThenPersonDetector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=2,
            display=False,
            person_reid_frame_stride=3,
            reid_maximum_interval_s=2.0,
        ),
        operator_bridge=_idle_operator_bridge(),
        unified_target_pool=target_pool,
        person_reid_encoder=person_reid,
    ).run()

    assert person_reid.calls == 1
    assert result.person_reid_no_candidate_frame_count == 1
    assert result.person_reid_inference_count == 1
    assert result.person_reid_forced_recovery_count == 1
    assert result.person_reid_skipped_frame_count == 0


def test_reid_recovery_overrides_stable_frame_cadence() -> None:
    due, forced = live_module._reid_inference_due(
        frame_index=1,
        now_s=10.01,
        last_inference_at_s=10.0,
        frame_stride=3,
        frame_phase=0,
        maximum_interval_s=0.1,
        recovery_required=True,
    )
    assert due is True
    assert forced is True

    due, forced = live_module._reid_inference_due(
        frame_index=3,
        now_s=10.01,
        last_inference_at_s=10.0,
        frame_stride=3,
        frame_phase=0,
        maximum_interval_s=0.1,
        recovery_required=True,
    )
    assert due is True
    assert forced is False


def test_reid_domains_can_stagger_stable_inference_frames() -> None:
    person_due, _ = live_module._reid_inference_due(
        frame_index=2,
        now_s=10.02,
        last_inference_at_s=10.0,
        frame_stride=2,
        frame_phase=0,
        maximum_interval_s=0.1,
        recovery_required=False,
    )
    vehicle_due, _ = live_module._reid_inference_due(
        frame_index=2,
        now_s=10.02,
        last_inference_at_s=10.0,
        frame_stride=2,
        frame_phase=1,
        maximum_interval_s=0.1,
        recovery_required=False,
    )
    assert person_due is True
    assert vehicle_due is False


def test_target_pool_metadata_refreshes_at_distinct_det_trk_and_lck_cadences() -> None:
    assert live_module._target_pool_status_interval_s(
        normal_interval_s=0.1,
        operator_trk_interval_s=0.05,
        exclusive_lock_interval_s=1.0 / 30.0,
        operator_tracked_target_count=0,
        exclusive_lock_track_id=None,
    ) == pytest.approx(0.1)
    assert live_module._target_pool_status_interval_s(
        normal_interval_s=0.1,
        operator_trk_interval_s=0.05,
        exclusive_lock_interval_s=1.0 / 30.0,
        operator_tracked_target_count=2,
        exclusive_lock_track_id=None,
    ) == pytest.approx(0.05)
    assert live_module._target_pool_status_interval_s(
        normal_interval_s=0.1,
        operator_trk_interval_s=0.05,
        exclusive_lock_interval_s=1.0 / 30.0,
        operator_tracked_target_count=1,
        exclusive_lock_track_id="target-000001",
    ) == pytest.approx(1.0 / 30.0)


def test_live_common_objects_share_target_pool_but_only_person_receives_reid() -> None:
    class _CommonObjectDetector:
        def detect(self, _image) -> tuple[Detection, ...]:
            return (
                Detection(
                    "person",
                    0.93,
                    BoundingBox(0.10, 0.20, 0.25, 0.70),
                    SensorKind.RGB,
                    "coco-candidate",
                ),
                Detection(
                    "car",
                    0.90,
                    BoundingBox(0.55, 0.45, 0.85, 0.72),
                    SensorKind.RGB,
                    "coco-candidate",
                ),
                Detection(
                    "building",
                    0.88,
                    BoundingBox(0.30, 0.10, 0.70, 0.60),
                    SensorKind.RGB,
                    "environment-candidate",
                ),
            )

        def covers_labels(self, labels) -> bool:
            return set(labels).issubset({"person", "car", "building"})

    mission = MissionController(_patrol_config())
    target_pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    reid = _ScriptedPersonReId()
    vehicle_reid = _ScriptedVehicleReId()
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_CommonObjectDetector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        operator_bridge=_idle_operator_bridge(),
        unified_target_pool=target_pool,
        person_reid_encoder=reid,
        vehicle_reid_encoder=vehicle_reid,
    ).run()

    snapshots = {track.label: track for track in target_pool.snapshots()}
    assert result.processed_frames == 2
    assert result.unified_target_pool_created_track_count == 3
    assert reid.calls == 2
    assert vehicle_reid.calls == 2
    assert snapshots["person"].appearance_sample_count == 2
    assert snapshots["car"].appearance_sample_count == 2
    assert snapshots["building"].appearance_sample_count == 0
    assert snapshots["person"].actionable is True
    assert snapshots["car"].actionable is True
    assert all(track.locked is False for track in snapshots.values())
    assert all(track.primary is False for track in snapshots.values())


class _NumpyFrameSource(_FrameSource):
    def __init__(self, frame_count: int, *, delay_s: float = 0.0) -> None:
        super().__init__(frame_count)
        self.delay_s = delay_s

    def read(self) -> CapturedFrame:
        if self.delay_s:
            time.sleep(self.delay_s)
        self._next += 1
        return CapturedFrame(
            frame_id=f"semantic-{self._next}",
            captured_at_s=time.monotonic(),
            image_bgr=np.zeros((32, 48, 3), dtype=np.uint8),
            width=48,
            height=32,
        )


class _BuildingSemanticModel:
    def infer(self, _image_bgr):
        return (
            SemanticRegion(
                label="building",
                class_id=2,
                bbox=BoundingBox(0.1, 0.1, 0.7, 0.8),
                pixel_count=500,
                frame_area_fraction=0.25,
                bbox_fill_fraction=0.6,
            ),
        )


def test_live_semantic_context_is_bounded_advisory_metadata_not_target_identity() -> None:
    mission = MissionController(_patrol_config())
    target_pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    semantic_runner = AsyncSemanticContextRunner(
        _BuildingSemanticModel(),
        minimum_interval_s=1.0,
    )
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_NumpyFrameSource(4, delay_s=0.01),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(
            max_frames=4,
            display=False,
            semantic_context_maximum_age_s=0.005,
        ),
        unified_target_pool=target_pool,
        semantic_context_runner=semantic_runner,
    ).run()

    assert result.processed_frames == 4
    assert result.semantic_context_submitted_frame_count == 1
    assert result.semantic_context_interval_skipped_frame_count == 3
    assert result.semantic_context_valid_frame_count == 1
    assert result.semantic_context_invalid_frame_count == 0
    assert result.semantic_context_stale_count == 1
    assert result.semantic_context_shutdown_clean is True
    assert {track.label for track in target_pool.snapshots()} == {"flame"}
    updates = [
        event
        for event in mission.audit.events()
        if event.event_type == "perception.semantic_context_updated"
    ]
    assert len(updates) == 1
    assert updates[0].details["regions"][0]["label"] == "building"
    assert updates[0].details["confidence_available"] is False
    assert updates[0].details["target_pool_identity_authority"] is False
    assert updates[0].details["flight_control_enabled"] is False
    assert updates[0].details["physical_release_enabled"] is False


class _FailingLiveSemanticModel:
    def infer(self, _image_bgr):
        raise ValueError("backend detail")


def test_live_semantic_context_failure_does_not_interrupt_primary_perception() -> None:
    mission = MissionController(_patrol_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_NumpyFrameSource(4, delay_s=0.01),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=4, display=False),
        semantic_context_runner=AsyncSemanticContextRunner(
            _FailingLiveSemanticModel(),
            minimum_interval_s=1.0,
        ),
    ).run()

    assert result.processed_frames == 4
    assert result.alert_delivery_count == 1
    assert result.semantic_context_invalid_frame_count == 1
    assert result.semantic_context_valid_frame_count == 0
    updates = [
        event
        for event in mission.audit.events()
        if event.event_type == "perception.semantic_context_updated"
    ]
    assert len(updates) == 1
    assert updates[0].details["state"] == "INVALID"
    assert updates[0].details["error_type"] == "ValueError"
    assert "backend detail" not in str(dict(updates[0].details))


def test_live_vehicle_reid_failure_preserves_person_reid_and_fails_closed() -> None:
    mission = MissionController(_patrol_config())
    target_pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(1),
        detector=_LabelDetector("person", "car"),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=1, display=False),
        unified_target_pool=target_pool,
        person_reid_encoder=_ScriptedPersonReId(),
        vehicle_reid_encoder=_FailingVehicleReId(),
    ).run()

    assert result.vehicle_reid_failure_count == 1
    failure = next(
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.vehicle_reid_failed"
    )
    assert failure.details["fallback"] == "motion_and_other_reid_domains"
    assert failure.details["vehicle_identity_recovery_enabled"] is False
    assert failure.details["flight_control_enabled"] is False


def test_live_person_reid_requires_unified_target_pool() -> None:
    with pytest.raises(ValueError, match="unified target pool"):
        LiveMissionRunner(
            mission=MissionController(_patrol_config()),
            frame_source=_FrameSource(1),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=1, display=False),
            person_reid_encoder=_FailingPersonReId(),
        )


def test_live_vehicle_reid_requires_unified_target_pool() -> None:
    with pytest.raises(ValueError, match="vehicle ReID requires"):
        LiveMissionRunner(
            mission=MissionController(_patrol_config()),
            frame_source=_FrameSource(1),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=1, display=False),
            vehicle_reid_encoder=_FailingVehicleReId(),
        )


def test_live_patrol_advisory_is_read_only_and_requires_target_pool() -> None:
    mission = MissionController(_patrol_config())
    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        unified_target_pool=UnifiedTargetPool(),
        patrol_advisory_engine=PatrolAdvisoryEngine(),
    ).run()

    assert result.patrol_advisory_assessment_count == 2
    assert result.patrol_return_to_observe_count == 0
    assert result.patrol_advisory_error_count == 0
    event = next(
        item for item in mission.audit.events() if item.event_type == "patrol.state_changed"
    )
    assert event.details["phase"] == "patrol"
    assert event.details["advisory_only"] is True
    assert event.details["flight_control_enabled"] is False

    with pytest.raises(ValueError, match="patrol advisory requires"):
        LiveMissionRunner(
            mission=MissionController(_patrol_config()),
            frame_source=_FrameSource(1),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=1, display=False),
            patrol_advisory_engine=PatrolAdvisoryEngine(),
        )


def test_live_short_term_tracking_supplies_prediction_hints_only() -> None:
    mission = MissionController(_patrol_config())
    short_term_tracker = _ScriptedShortTermTracker()

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        unified_target_pool=UnifiedTargetPool(),
        short_term_tracker=short_term_tracker,
    ).run()

    assert result.processed_frames == 2
    assert result.short_term_tracking_update_count == 2
    assert result.short_term_tracking_error_count == 0
    assert result.short_term_tracking_optical_flow_hint_count == 1
    assert result.short_term_tracking_template_hint_count == 0
    assert result.short_term_tracking_accepted_hint_count == 1
    assert result.short_term_tracking_rejected_hint_count == 0
    assert result.short_term_tracking_latency_p95_ms >= 2.0
    assert len(short_term_tracker.synchronized) == 2
    assert all(
        arguments["prefer_background_motion"] is True
        for arguments in short_term_tracker.update_kwargs
    )


def test_live_short_term_tracking_failure_falls_back_to_kalman_prediction() -> None:
    mission = MissionController(_patrol_config())

    result = LiveMissionRunner(
        mission=mission,
        frame_source=_FrameSource(2),
        detector=_Detector(),
        telemetry_provider=FailClosedTelemetryProvider(),
        config=LiveRunConfig(max_frames=2, display=False),
        unified_target_pool=UnifiedTargetPool(),
        short_term_tracker=_FailingShortTermTracker(),
    ).run()

    assert result.processed_frames == 2
    assert result.short_term_tracking_update_count == 0
    assert result.short_term_tracking_error_count == 2
    assert result.unified_target_pool_update_count == 2
    failures = [
        event
        for event in mission.audit.events()
        if event.event_type == "tracking.short_term_failed"
    ]
    assert len(failures) == 2
    assert all(event.details["fallback"] == "kalman_prediction_only" for event in failures)
    assert all(event.details["identity_observation_created"] is False for event in failures)
    assert all(event.details["flight_control_enabled"] is False for event in failures)


def test_live_short_term_tracking_requires_unified_target_pool() -> None:
    with pytest.raises(ValueError, match="short-term tracking requires"):
        LiveMissionRunner(
            mission=MissionController(_patrol_config()),
            frame_source=_FrameSource(1),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=1, display=False),
            short_term_tracker=_ScriptedShortTermTracker(),
        )


def test_live_selection_target_pool_requires_operator_bridge() -> None:
    unified_target_pool = UnifiedTargetPool()
    with pytest.raises(ValueError, match="requires the operator bridge"):
        LiveMissionRunner(
            mission=MissionController(_patrol_config()),
            frame_source=_FrameSource(1),
            detector=_Detector(),
            telemetry_provider=FailClosedTelemetryProvider(),
            config=LiveRunConfig(max_frames=1, display=False),
            unified_target_pool=unified_target_pool,
            selection_target_pool=UnifiedSelectionTargetPool(unified_target_pool),
        )


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

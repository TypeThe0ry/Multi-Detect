from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from multidetect.backends import (
    RawReplayFrame,
    RawReplayImageReference,
    RawReplayTelemetrySample,
    RawSensorReplayBackend,
    RawSensorReplayRecording,
)
from multidetect.config import MissionConfig
from multidetect.domain import VehicleTelemetry
from multidetect.integrated_replay import (
    IntegratedReplayIdentity,
    build_integrated_replay_session,
)
from multidetect.live import LiveRunConfig
from multidetect.mission import MissionController
from multidetect.operator_link import RuntimeBackendHealth
from multidetect.runtime import RuntimeMode, load_runtime_run_report

ROOT = Path(__file__).resolve().parents[1]
MISSION_PATH = ROOT / "configs/missions/fire_patrol.demo.json"
MISSION_HASH = hashlib.sha256(MISSION_PATH.read_bytes()).hexdigest()
PERCEPTION_HASH = hashlib.sha256(b"production-perception-fixture-v1").hexdigest()


@dataclass(frozen=True)
class _Pixels:
    height: int
    width: int
    payload: bytes

    @property
    def shape(self) -> tuple[int, int, int]:
        return (self.height, self.width, 3)


class _NoDetectionProductionDetector:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def detect(self, image: _Pixels) -> tuple[object, ...]:
        self.payloads.append(image.payload)
        return ()

    def covers_labels(self, _labels: object) -> bool:
        return False


def _decode(payload: bytes, reference: RawReplayImageReference) -> _Pixels:
    return _Pixels(reference.height, reference.width, payload)


def _telemetry(*, observed_at_s: float) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=20.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=0.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=False,
        release_zone_clear=None,
        latitude_deg=1.3521,
        longitude_deg=103.8198,
        gps_fix_type=3,
        satellites_visible=15,
        altitude_observed_at_s=observed_at_s,
        attitude_observed_at_s=observed_at_s,
        position_observed_at_s=observed_at_s,
    )


def _backend(tmp_path: Path) -> RawSensorReplayBackend:
    tmp_path.mkdir(parents=True, exist_ok=True)
    payloads = (b"raw-frame-one", b"raw-frame-two")
    frames: list[RawReplayFrame] = []
    for index, payload in enumerate(payloads, start=1):
        path = (tmp_path / f"frame-{index:04d}.png").resolve()
        path.write_bytes(payload)
        frames.append(
            RawReplayFrame(
                frame_id=f"frame-{index:04d}",
                captured_at_s=float(index),
                image=RawReplayImageReference(
                    relative_path=path.name,
                    resolved_path=path,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    byte_count=len(payload),
                    width=3,
                    height=2,
                    encoding="png",
                ),
            )
        )
    recording = RawSensorReplayRecording(
        recording_id="integrated-replay-fixture",
        airframe_profile_hash="a" * 64,
        started_at_utc=datetime(2026, 8, 25, tzinfo=timezone.utc),
        event_stream_sha256=hashlib.sha256(b"verified-events").hexdigest(),
        event_stream_bytes=len(b"verified-events"),
        frames=tuple(frames),
        telemetry_samples=(
            RawReplayTelemetrySample(1.0, _telemetry(observed_at_s=1.0)),
            RawReplayTelemetrySample(1.5, _telemetry(observed_at_s=1.5)),
        ),
    )
    return RawSensorReplayBackend(recording, image_decoder=_decode)


def _run_once(tmp_path: Path):
    backend = _backend(tmp_path)
    detector = _NoDetectionProductionDetector()
    mission = MissionController(MissionConfig.from_json(MISSION_PATH))
    session = build_integrated_replay_session(
        backend=backend,
        mission=mission,
        detector=detector,
        mission_config_hash=MISSION_HASH,
        perception_identity_hash=PERCEPTION_HASH,
    )
    result = session.run()
    audit = tuple(
        (event.sequence, event.timestamp_s, event.event_type, dict(event.details))
        for event in mission.audit.events()
    )
    return backend, detector, session, result, audit


def test_integrated_replay_consumes_raw_frames_through_live_core_and_binds_status(
    tmp_path: Path,
) -> None:
    backend, detector, session, result, _audit = _run_once(tmp_path)

    assert result.processed_frames == backend.frame_count == 2
    assert detector.payloads == [b"raw-frame-one", b"raw-frame-two"]
    assert session.runner.runtime_descriptor is backend.descriptor
    assert session.runner.clock is backend.clock
    assert session.runner.frame_source is backend.frame_source
    assert session.runner.telemetry_provider is backend.telemetry_provider
    assert session.runtime_status.mode is RuntimeMode.REPLAY
    assert session.runtime_status.backend_health is RuntimeBackendHealth.HEALTHY
    assert session.runtime_status.ground_truth_isolated is True
    assert session.runtime_status.hardware_outputs_present is False
    assert session.runtime_status.profile_valid is True
    assert session.runtime_status.run_hash64 == int(session.identity.run_identity_hash[:16], 16)
    report = session.runtime_run_report
    assert report.descriptor_hash == backend.descriptor.canonical_hash
    assert report.input_hash == backend.input_hash
    assert report.processed_frame_ids == ("frame-0001", "frame-0002")
    assert report.processed_frame_count == backend.frame_count
    assert report.stream_exhausted is True
    assert report.max_frames is None
    assert report.complete is True
    assert report.termination_reason == "stream_exhausted"
    assert report.started_at_s == 0.0
    assert report.finished_at_s == 2.0
    assert result.capture_latency_p50_ms == 0.0
    assert result.inference_latency_p50_ms == 0.0

    events = tuple(event[2] for event in _audit)
    assert "camera.stream_exhausted" in events


def test_integrated_replay_repeats_identity_result_and_core_audit_deterministically(
    tmp_path: Path,
) -> None:
    first = _run_once(tmp_path / "first")
    second = _run_once(tmp_path / "second")

    assert first[2].identity == second[2].identity
    assert first[2].identity.run_identity_hash == second[2].identity.run_identity_hash
    assert asdict(first[3]) == asdict(second[3])
    assert first[4] == second[4]
    assert first[2].runtime_run_report == second[2].runtime_run_report


def test_integrated_replay_report_round_trips_and_session_is_single_pass(tmp_path: Path) -> None:
    backend, _detector, session, _result, _audit = _run_once(tmp_path)

    destination = tmp_path / "runtime-report.json"
    session.write_runtime_run_report(destination)
    assert load_runtime_run_report(destination) == session.runtime_run_report

    with pytest.raises(RuntimeError, match="single-pass"):
        session.run()


def test_integrated_replay_identity_changes_with_mission_or_perception() -> None:
    baseline = IntegratedReplayIdentity(
        runtime_descriptor_hash="a" * 64,
        recording_hash="b" * 64,
        mission_config_hash="c" * 64,
        perception_identity_hash="d" * 64,
    )
    mission_change = IntegratedReplayIdentity(
        runtime_descriptor_hash="a" * 64,
        recording_hash="b" * 64,
        mission_config_hash="e" * 64,
        perception_identity_hash="d" * 64,
    )
    perception_change = IntegratedReplayIdentity(
        runtime_descriptor_hash="a" * 64,
        recording_hash="b" * 64,
        mission_config_hash="c" * 64,
        perception_identity_hash="f" * 64,
    )

    assert baseline.run_identity_hash != mission_change.run_identity_hash
    assert baseline.run_identity_hash != perception_change.run_identity_hash


@pytest.mark.parametrize(
    "config",
    (
        LiveRunConfig(max_frames=1, display=False),
        LiveRunConfig(max_frames=2, display=True),
        LiveRunConfig(
            max_frames=2,
            display=False,
            observe_pixhawk_lifecycle=True,
            task_area_mission_sequence=0,
        ),
    ),
)
def test_integrated_replay_rejects_prefix_visual_or_live_lifecycle_modes(
    tmp_path: Path,
    config: LiveRunConfig,
) -> None:
    with pytest.raises(ValueError):
        build_integrated_replay_session(
            backend=_backend(tmp_path),
            mission=MissionController(MissionConfig.from_json(MISSION_PATH)),
            detector=_NoDetectionProductionDetector(),
            mission_config_hash=MISSION_HASH,
            perception_identity_hash=PERCEPTION_HASH,
            config=config,
        )


def test_integrated_replay_rejects_clock_status_and_flight_output_overrides(
    tmp_path: Path,
) -> None:
    base = {
        "backend": _backend(tmp_path),
        "mission": MissionController(MissionConfig.from_json(MISSION_PATH)),
        "detector": _NoDetectionProductionDetector(),
        "mission_config_hash": MISSION_HASH,
        "perception_identity_hash": PERCEPTION_HASH,
    }
    with pytest.raises(TypeError, match="owns performance_clock"):
        build_integrated_replay_session(**base, performance_clock=object())
    with pytest.raises(TypeError, match="owns runtime_status"):
        build_integrated_replay_session(**base, runtime_status=object())
    with pytest.raises(ValueError, match="flight-control executor"):
        build_integrated_replay_session(**base, fixed_wing_aim_executor=object())

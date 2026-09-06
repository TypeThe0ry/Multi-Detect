from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
from multidetect.aircraft_appearance import HandcraftedAircraftAppearanceEncoder
from multidetect.domain import MissionPhase
from multidetect.qgc_replay import (
    ReplayRtspPublisherStats,
    SynchronizedReplayEvidence,
)
from multidetect.selection_target_pool import UnifiedSelectionTargetPool
from multidetect.unified_tracking import UnifiedTargetPool

ROOT = Path(__file__).resolve().parents[1]
MISSION_PATH = ROOT / "configs/missions/fire_patrol.demo.json"


class _FakeBackend:
    instance: _FakeBackend | None = None

    def __init__(self) -> None:
        image = SimpleNamespace(width=4, height=2)
        self.recording = SimpleNamespace(
            recording_id="qgc-cli-fixture",
            recording_hash="b" * 64,
            frames=(
                SimpleNamespace(captured_at_s=1.0, image=image),
                SimpleNamespace(captured_at_s=2.0, image=image),
            ),
        )
        self.descriptor = SimpleNamespace(canonical_hash="c" * 64)
        self.frame_count = 2
        self.clock = SimpleNamespace(monotonic=lambda: 2.0)
        self.closed = False
        type(self).instance = self

    @classmethod
    def from_manifest(cls, path: Path, *, expected_manifest_sha256: str):
        assert path == Path("recording.manifest.json")
        assert expected_manifest_sha256 == "a" * 64
        return cls()

    def close(self) -> None:
        self.closed = True


class _FakePerception:
    def __init__(self) -> None:
        self.detector = object()
        self.rgb_fire_verifier = None
        self.rgb_fire_corroborator = None
        self.semantic_context_runner = None
        self.all_configured_artifacts_verified = True
        self.all_configured_artifacts_production_approved = True
        self.model_set_sha256 = "d" * 64
        self.configured_model_components = ("primary",)
        self.synthetic_hil_components = ()
        self.person_safety_evidence_qualified = True
        self.closed = False

    def close(self) -> bool:
        self.closed = True
        return True


class _FakeTransport:
    instance: _FakeTransport | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        type(self).instance = self

    def close(self) -> None:
        self.closed = True


class _FakePublisher:
    instance: _FakePublisher | None = None

    def __init__(self, executable, **kwargs) -> None:
        self.executable = executable
        self.kwargs = kwargs
        self.frames = 0
        self.closed = False
        type(self).instance = self

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://127.0.0.1:{self.kwargs['port']}{self.kwargs['mount']}"

    @property
    def stats(self) -> ReplayRtspPublisherStats:
        return ReplayRtspPublisherStats(
            frame_count=self.frames,
            payload_bytes=self.frames * 24,
            first_captured_at_ns=1_000_000_000 if self.frames else None,
            last_captured_at_ns=2_000_000_000 if self.frames else None,
            maximum_ack_latency_ms=2.0,
            eos_sent=self.closed,
            summary_received=self.closed,
            helper_exit_code=0 if self.closed else None,
        )


class _FakeSynchronizedBridge:
    instance: _FakeSynchronizedBridge | None = None

    def __init__(self, transport, target_lock, **kwargs) -> None:
        self.transport = transport
        self.target_lock = target_lock
        self.frame_publisher = kwargs["frame_publisher"]
        self.pacer = kwargs["pacer"]
        self.expected_frame_count = kwargs["expected_frame_count"]
        self.kwargs = kwargs
        self.runtime_run_hash64_hex: str | None = None
        self.closed = False
        type(self).instance = self

    def simulate_run(self) -> None:
        self.frame_publisher.frames = self.expected_frame_count
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.frame_publisher.closed = True
        self.transport.close()

    def require_complete(self) -> SynchronizedReplayEvidence:
        assert self.closed
        return SynchronizedReplayEvidence(
            expected_frame_count=self.expected_frame_count,
            video_frame_count=self.expected_frame_count,
            metadata_barrier_count=self.expected_frame_count,
            maximum_ack_to_metadata_skew_ms=4.0,
            allowed_ack_to_metadata_skew_ms=self.kwargs["maximum_ack_to_metadata_skew_ms"],
            qgc_peer_host="127.0.0.1",
            qgc_peer_port=14580,
            complete=True,
            metadata_send_count=self.expected_frame_count,
            metadata_acceptance_ack_count=self.expected_frame_count,
            maximum_metadata_acceptance_ack_latency_ms=3.0,
            maximum_frame_to_acceptance_latency_ms=7.0,
            allowed_metadata_acceptance_ack_latency_ms=(
                self.kwargs["metadata_acceptance_timeout_s"] * 1000.0
            ),
            acceptance_ack_peer_host="127.0.0.1",
            acceptance_ack_peer_port=14580,
            first_acknowledged_runtime_sequence=1,
            last_acknowledged_runtime_sequence=self.expected_frame_count,
            unique_acknowledged_runtime_sequence_count=self.expected_frame_count,
            acknowledged_runtime_run_hash64_hex=self.runtime_run_hash64_hex,
            first_sent_runtime_sequence=1,
            last_sent_runtime_sequence=self.expected_frame_count,
            unique_sent_runtime_sequence_count=self.expected_frame_count,
            sent_runtime_sequences_contiguous=True,
            acknowledged_runtime_sequences_contiguous=True,
        )


@dataclass(frozen=True)
class _FakeResult:
    processed_frames: int = 2
    final_phase: MissionPhase = MissionPhase.SEARCHING


def test_integrated_replay_qgc_cli_reuses_production_core_and_target_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    perception = _FakePerception()
    captured: dict[str, object] = {}

    def fake_factory(args, config):
        captured["perception_args"] = args
        captured["mission_id"] = config.mission_id
        return perception

    def fake_adapter(**kwargs):
        captured["adapter_kwargs"] = kwargs
        return object()

    def fake_session_builder(**kwargs):
        captured["session_kwargs"] = kwargs
        identity = cli_module.IntegratedReplayIdentity(
            runtime_descriptor_hash="c" * 64,
            recording_hash="b" * 64,
            mission_config_hash=kwargs["mission_config_hash"],
            perception_identity_hash=kwargs["perception_identity_hash"],
        )

        def run():
            kwargs["operator_bridge"].runtime_run_hash64_hex = identity.run_identity_hash[:16]
            kwargs["operator_bridge"].simulate_run()
            return _FakeResult()

        return SimpleNamespace(identity=identity, run=run)

    monkeypatch.setattr(cli_module, "RawSensorReplayBackend", _FakeBackend)
    monkeypatch.setattr(cli_module, "build_production_perception", fake_factory)
    monkeypatch.setattr(cli_module, "_operator_mavlink_adapter", fake_adapter)
    monkeypatch.setattr(cli_module, "UdpOperatorSelectionServer", _FakeTransport)
    monkeypatch.setattr(cli_module, "SubprocessReplayRtspPublisher", _FakePublisher)
    monkeypatch.setattr(
        cli_module,
        "SynchronizedReplayOperatorBridge",
        _FakeSynchronizedBridge,
    )
    monkeypatch.setattr(cli_module, "build_integrated_replay_session", fake_session_builder)
    result_path = tmp_path / "qgc-result.json"

    exit_code = cli_module.main(
        [
            "integrated-replay-qgc",
            str(MISSION_PATH),
            "recording.manifest.json",
            "--recording-manifest-sha256",
            "a" * 64,
            "--rtsp-helper",
            "MultiDetectReplayRtspPublisher.exe",
            "--rtsp-port",
            "8554",
            "--operator-udp-port",
            "14581",
            "--metadata-acceptance-ack-timeout-seconds",
            "1.75",
            "--operator-hmac-key-env",
            "QGC_HMAC",
            "--mavlink-signing-key-hex-env",
            "QGC_MAVLINK",
            "--result-out",
            str(result_path),
            "--perception-args",
            "--onnx-model",
            "primary.onnx",
            "--model-manifest",
            "primary.manifest.json",
            "--unified-target-pool-maximum-tracks",
            "12",
            "--warmup-iterations",
            "0",
        ]
    )

    assert exit_code == 0
    parsed = captured["perception_args"]
    assert parsed.command == "live-camera"
    assert parsed.unified_target_pool is True
    assert parsed.unified_target_pool_maximum_tracks == 12
    adapter_kwargs = captured["adapter_kwargs"]
    assert adapter_kwargs["operator_hmac_key_env"] == "QGC_HMAC"
    assert adapter_kwargs["mavlink_signing_key_hex_env"] == "QGC_MAVLINK"
    assert adapter_kwargs["geometry"].width == 4
    assert adapter_kwargs["geometry"].height == 2

    session_kwargs = captured["session_kwargs"]
    assert session_kwargs["detector"] is perception.detector
    assert isinstance(session_kwargs["unified_target_pool"], UnifiedTargetPool)
    assert session_kwargs["unified_target_pool"].config.maximum_tracks == 12
    assert isinstance(session_kwargs["selection_target_pool"], UnifiedSelectionTargetPool)
    assert isinstance(
        session_kwargs["aircraft_appearance_encoder"],
        HandcraftedAircraftAppearanceEncoder,
    )
    assert "fixed_wing_aim_executor" not in session_kwargs
    assert session_kwargs["config"].display is False
    assert session_kwargs["config"].max_frames == 2
    assert (
        session_kwargs["mission_config_hash"]
        == hashlib.sha256(MISSION_PATH.read_bytes()).hexdigest()
    )
    assert perception.closed is True
    assert _FakeBackend.instance is not None and _FakeBackend.instance.closed is True
    assert _FakeTransport.instance is not None and _FakeTransport.instance.closed is True
    assert _FakeSynchronizedBridge.instance is not None
    assert _FakeSynchronizedBridge.instance.kwargs["metadata_acceptance_timeout_s"] == 1.75

    document = json.loads(result_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["event"] == "integrated_replay_qgc_finished"
    assert "qgc_synchronized_visual_gate" not in document
    assert document["qgc_transport_acceptance_gate"] == (
        "passed_rtsp_frame_ack_and_runtime_status_post_adoption_ack"
    )
    assert document["operator_visual_acceptance"] == "not_claimed"
    assert document["machine_visual_smoke"] == "not_run_by_cli"
    protocol = document["synchronization_protocol"]
    assert protocol["runtime_status_message_type"] == 23
    assert protocol["runtime_status_acceptance_ack_message_type"] == 24
    assert protocol["type24_scope"] == "native_controller_post_adoption_only"
    assert protocol["runtime_mode_policy_acceptance_proved"] is False
    assert protocol["qml_rendering_proved"] is False
    assert protocol["operator_visual_acceptance"] == "not_claimed"
    assert document["synchronization"]["complete"] is True
    assert document["synchronization"]["metadata_send_count"] == 2
    assert document["synchronization"]["metadata_acceptance_ack_count"] == 2
    assert document["synchronization"]["metadata_barrier_count"] == 2
    assert document["synchronization"]["first_sent_runtime_sequence"] == 1
    assert document["synchronization"]["last_sent_runtime_sequence"] == 2
    assert document["synchronization"]["sent_runtime_sequences_contiguous"] is True
    assert document["synchronization"]["acknowledged_runtime_sequences_contiguous"] is True
    assert (
        document["synchronization"]["acknowledged_runtime_run_hash64_hex"]
        == (document["run_identity_hash"][:16])
    )
    assert document["hardware_outputs_present"] is False
    assert document["pixhawk_attached"] is False
    assert document["actuator_outputs_present"] is False
    stdout = capsys.readouterr().out
    assert '"integrated_replay_qgc_started"' in stdout
    assert '"integrated_replay_qgc_finished"' in stdout


def test_e4_target_pool_builder_is_the_same_builder_used_by_live_parser() -> None:
    parsed = cli_module._integrated_replay_perception_namespace(
        MISSION_PATH,
        [
            "--onnx-model",
            "primary.onnx",
            "--model-manifest",
            "primary.manifest.json",
            "--unified-target-pool",
            "--unified-target-pool-maximum-tracks",
            "23",
            "--unified-target-pool-kalman-gate-sigma",
            "3.5",
        ],
        additional_destinations=cli_module._INTEGRATED_REPLAY_QGC_DESTINATIONS,
    )
    live_pool = cli_module._unified_target_pool_from_args(parsed)
    e4_pool = cli_module._unified_target_pool_from_args(parsed, force_enabled=True)

    assert live_pool is not None and e4_pool is not None
    assert live_pool.config == e4_pool.config
    assert live_pool.config.maximum_tracks == 23
    assert live_pool.config.kalman_gate_sigma == 3.5


def test_integrated_replay_qgc_fails_before_opening_backend_for_non_loopback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_module.main(
        [
            "integrated-replay-qgc",
            str(MISSION_PATH),
            "recording.manifest.json",
            "--recording-manifest-sha256",
            "a" * 64,
            "--rtsp-helper",
            "helper.exe",
            "--rtsp-bind-host",
            "0.0.0.0",
            "--rtsp-port",
            "8554",
            "--operator-udp-port",
            "14581",
            "--operator-hmac-key-env",
            "QGC_HMAC",
            "--mavlink-signing-key-hex-env",
            "QGC_MAVLINK",
            "--perception-args",
            "--onnx-model",
            "primary.onnx",
            "--model-manifest",
            "primary.manifest.json",
        ]
    )

    assert exit_code == 1
    assert "must bind to 127.0.0.1" in capsys.readouterr().err


def test_integrated_replay_qgc_namespace_still_rejects_pixhawk_options() -> None:
    with pytest.raises(ValueError, match="production perception options only"):
        cli_module._integrated_replay_perception_namespace(
            MISSION_PATH,
            [
                "--onnx-model",
                "primary.onnx",
                "--model-manifest",
                "primary.manifest.json",
                "--pixhawk-endpoint",
                "udp:127.0.0.1:14550",
            ],
            additional_destinations=cli_module._INTEGRATED_REPLAY_QGC_DESTINATIONS,
        )

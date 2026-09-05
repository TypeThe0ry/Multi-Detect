from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
from multidetect.aircraft_appearance import HandcraftedAircraftAppearanceEncoder
from multidetect.domain import MissionPhase
from multidetect.qgc_replay import (
    ReplayRtspPublisherStats,
    SynchronizedQgcEvidence,
)
from multidetect.runtime import (
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeMode,
    RuntimeProfile,
)
from multidetect.selection_target_pool import UnifiedSelectionTargetPool
from multidetect.simulation import load_runtime_scenario
from multidetect.terrain_dem import load_local_dem_grid
from multidetect.unified_tracking import UnifiedTargetPool

ROOT = Path(__file__).resolve().parents[1]
MISSION_PATH = ROOT / "configs/missions/fire_patrol.demo.json"
SCENARIO_PATH = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
TERRAIN_DEM_PATH = ROOT / "simulation/terrain/river_rescue_vtol_001.dem.json"
RANGING_CALIBRATION_PATH = ROOT / "simulation/gazebo/calibration/gazebo-front-camera-v1.json"


class _FakeBackend:
    instance: _FakeBackend | None = None

    def __init__(self, **kwargs: object) -> None:
        scenario_input = kwargs["scenario_input"]
        airframe_profile_hash = kwargs["airframe_profile_hash"]
        assert isinstance(airframe_profile_hash, str)
        self.kwargs = kwargs
        self.scenario_input = scenario_input
        self.frame_count = kwargs["frame_count"]
        self.sensor_binding_hash = "e" * 64
        self.descriptor = RuntimeDescriptor(
            mode=RuntimeMode.SIM,
            profile=RuntimeProfile.sim(),
            backend_id="gazebo-sim-cli-test",
            airframe_profile_hash=airframe_profile_hash,
            scenario_id=scenario_input.scenario_id,
            scenario_manifest_hash=scenario_input.scenario_manifest_hash,
            scenario_seed=scenario_input.seed,
            recording_hash=None,
            deterministic=False,
            fault_injection_enabled=False,
        )
        self.clock = SimpleNamespace(monotonic=lambda: 42.0)
        self.closed = False
        type(self).instance = self

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

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False
        type(self).instance = self

    def close(self) -> None:
        self.closed = True


class _FakePublisher:
    instance: _FakePublisher | None = None

    def __init__(self, executable: Path, **kwargs: object) -> None:
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

    def __init__(self, transport: object, target_lock: object, **kwargs: object) -> None:
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
        assert self.expected_frame_count == 2
        self.pacer.pace(1.0)
        self.pacer.pace(2.0)
        self.frame_publisher.frames = self.expected_frame_count
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.frame_publisher.closed = True
        self.transport.close()

    def require_complete(self) -> SynchronizedQgcEvidence:
        assert self.closed
        return SynchronizedQgcEvidence(
            expected_frame_count=self.expected_frame_count,
            video_frame_count=self.expected_frame_count,
            metadata_barrier_count=self.expected_frame_count,
            maximum_ack_to_metadata_skew_ms=4.0,
            allowed_ack_to_metadata_skew_ms=self.kwargs["maximum_ack_to_metadata_skew_ms"],
            qgc_peer_host="127.0.0.1",
            qgc_peer_port=14580,
            complete=True,
            ground_truth_isolated=True,
            hardware_outputs_present=False,
            pixhawk_attached=False,
            actuator_outputs_present=False,
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
class _FakeSensorTransport:
    expected_frame_count: int = 2
    frame_count: int = 2
    odometry_timestamp_frame_count: int = 2
    payload_bytes: int = 48
    first_sensor_stamp_ns: int = 1_000_000_000
    last_sensor_stamp_ns: int = 2_000_000_000
    complete: bool = True
    odometry_required: bool = True
    full_odometry_pose_velocity_fusion_claimed: bool = True

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _FakeResult:
    processed_frames: int = 2
    final_phase: MissionPhase = MissionPhase.SEARCHING


def test_integrated_sim_qgc_cli_is_genuine_sim_and_reuses_production_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    perception = _FakePerception()
    captured: dict[str, object] = {}

    def fake_factory(args: object, config: object) -> _FakePerception:
        captured["perception_args"] = args
        captured["mission_id"] = config.mission_id
        return perception

    def fake_adapter(**kwargs: object) -> object:
        captured["adapter_kwargs"] = kwargs
        return object()

    def fake_session_builder(**kwargs: object) -> object:
        captured["session_kwargs"] = kwargs
        backend = kwargs["backend"]
        captured["descriptor"] = backend.descriptor
        ranging_identity_hash = kwargs.get("ranging_identity_hash")
        identity = cli_module.IntegratedSimIdentity(
            runtime_descriptor_hash=backend.descriptor.canonical_hash,
            scenario_manifest_hash=backend.scenario_input.scenario_manifest_hash,
            sensor_binding_hash=backend.sensor_binding_hash,
            mission_config_hash=kwargs["mission_config_hash"],
            perception_identity_hash=kwargs["perception_identity_hash"],
            ranging_identity_hash=ranging_identity_hash,
            schema_version=2 if ranging_identity_hash is not None else 1,
        )

        def run() -> tuple[_FakeResult, _FakeSensorTransport]:
            kwargs["operator_bridge"].runtime_run_hash64_hex = identity.run_identity_hash[:16]
            kwargs["operator_bridge"].simulate_run()
            return _FakeResult(), _FakeSensorTransport()

        return SimpleNamespace(identity=identity, run=run)

    monkeypatch.setattr(cli_module, "GazeboSimBackend", _FakeBackend)
    monkeypatch.setattr(cli_module, "build_production_perception", fake_factory)
    monkeypatch.setattr(cli_module, "_operator_mavlink_adapter", fake_adapter)
    monkeypatch.setattr(cli_module, "UdpOperatorSelectionServer", _FakeTransport)
    monkeypatch.setattr(cli_module, "SubprocessReplayRtspPublisher", _FakePublisher)
    monkeypatch.setattr(
        cli_module,
        "SynchronizedQgcOperatorBridge",
        _FakeSynchronizedBridge,
    )
    monkeypatch.setattr(cli_module, "build_integrated_sim_session", fake_session_builder)

    result_path = tmp_path / "sim-qgc-result.json"
    audit_path = tmp_path / "sim-qgc-audit.jsonl"
    docker_path = Path(sys.executable).resolve()
    command = [
        "integrated-sim-qgc",
        str(MISSION_PATH),
        str(SCENARIO_PATH),
        "--docker-executable",
        str(docker_path),
        "--container-name",
        "owned-sim-container",
        "--container-image-sha256",
        "a" * 64,
        "--stream-script-sha256",
        "b" * 64,
        "--model-assets-sha256",
        "c" * 64,
        "--airframe-profile-sha256",
        "f" * 64,
        "--terrain-dem",
        str(TERRAIN_DEM_PATH),
        "--frame-count",
        "2",
        "--frame-width",
        "4",
        "--frame-height",
        "2",
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
        "--audit-out",
        str(audit_path),
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
    exit_code = cli_module.main(command)

    assert exit_code == 0
    assert _FakeBackend.instance is not None
    assert _FakeBackend.instance.kwargs["docker_executable"] == docker_path
    assert _FakeBackend.instance.closed is True
    terrain_dem = _FakeBackend.instance.kwargs["terrain_dem"]
    assert terrain_dem.scenario_id == "river_rescue_vtol_001"  # type: ignore[union-attr]
    descriptor = captured["descriptor"]
    assert descriptor.mode is RuntimeMode.SIM
    assert descriptor.profile == RuntimeProfile.sim()
    assert descriptor.recording_hash is None
    assert RuntimeCapability.FLIGHT_CONTROL_OUTPUT not in descriptor.profile.capabilities

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
    assert (
        session_kwargs["selection_target_pool"].target_pool is session_kwargs["unified_target_pool"]
    )
    assert isinstance(
        session_kwargs["aircraft_appearance_encoder"],
        HandcraftedAircraftAppearanceEncoder,
    )
    assert "fixed_wing_aim_executor" not in session_kwargs
    assert session_kwargs["config"].observe_pixhawk_lifecycle is False
    assert session_kwargs["config"].display is False
    assert session_kwargs["config"].max_frames == 2
    assert (
        session_kwargs["mission_config_hash"]
        == hashlib.sha256(MISSION_PATH.read_bytes()).hexdigest()
    )
    assert perception.closed is True
    assert _FakeTransport.instance is not None and _FakeTransport.instance.closed is True
    assert _FakePublisher.instance is not None and _FakePublisher.instance.closed is True
    assert _FakeSynchronizedBridge.instance is not None
    assert _FakeSynchronizedBridge.instance.pacer.stats.paced_frame_count == 2
    assert _FakeSynchronizedBridge.instance.kwargs["metadata_acceptance_timeout_s"] == 1.75

    document = json.loads(result_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["event"] == "integrated_sim_qgc_finished"
    assert document["runtime_mode"] == "sim"
    assert document["gate_passed"] is True
    assert document["scenario_id"] == "river_rescue_vtol_001"
    assert "recording_hash" not in document
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
    assert document["ground_truth_isolated"] is True
    assert document["hardware_outputs_present"] is False
    assert document["real_pixhawk_contacted"] is False
    assert document["actuator_outputs_present"] is False
    assert document["flight_control_enabled"] is False
    assert document["odometry_required"] is True
    assert document["full_odometry_pose_velocity_fusion_claimed"] is True
    assert document["terrain_dem_sha256"] == terrain_dem.source_sha256  # type: ignore[union-attr]
    assert document["terrain_agl_reference"] == ("GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL")
    assert document["odometry_local_up_used_as_agl"] is False
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
    assert document["synchronization"]["hardware_outputs_present"] is False
    assert document["sensor_transport"]["complete"] is True
    assert document["sensor_transport"]["odometry_timestamp_frame_count"] == 2
    assert document["sensor_transport"]["odometry_required"] is True
    assert document["sensor_transport"]["full_odometry_pose_velocity_fusion_claimed"] is True
    assert document["live_frame_pacer"]["paced_frame_count"] == 2
    assert document["result"]["processed_frames"] == 2

    audit_records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    synchronized_audit = next(
        record
        for record in audit_records
        if record["event_type"] == "sim.qgc_synchronized_complete"
    )
    assert synchronized_audit["session_id"] == document["run_identity_hash"]
    assert synchronized_audit["details"]["complete"] is True
    assert synchronized_audit["details"]["synchronization_evidence_schema_version"] == 2
    assert synchronized_audit["details"]["synchronization_protocol"] == protocol
    assert synchronized_audit["details"]["metadata_send_count"] == 2
    assert synchronized_audit["details"]["metadata_acceptance_ack_count"] == 2
    assert synchronized_audit["details"]["metadata_barrier_count"] == 2
    assert synchronized_audit["details"]["sensor_transport"]["complete"] is True
    assert synchronized_audit["details"]["odometry_required"] is True
    assert synchronized_audit["details"]["full_odometry_pose_velocity_fusion_claimed"] is True
    assert synchronized_audit["details"]["terrain_dem_sha256"] == document["terrain_dem_sha256"]
    assert synchronized_audit["details"]["odometry_local_up_used_as_agl"] is False
    assert synchronized_audit["details"]["sensor_transport"]["odometry_timestamp_frame_count"] == 2
    assert synchronized_audit["details"]["metadata_only"] is True
    assert synchronized_audit["details"]["flight_control_enabled"] is False
    assert synchronized_audit["details"]["physical_release_enabled"] is False

    strict_result_path = tmp_path / "sim-qgc-strict-result.json"
    strict_audit_path = tmp_path / "sim-qgc-strict-audit.jsonl"
    strict_command = list(command)
    strict_command[strict_command.index("--frame-width") + 1] = "1280"
    strict_command[strict_command.index("--frame-height") + 1] = "960"
    strict_command[strict_command.index("--result-out") + 1] = str(strict_result_path)
    strict_command[strict_command.index("--audit-out") + 1] = str(strict_audit_path)
    perception_index = strict_command.index("--perception-args")
    strict_command[perception_index:perception_index] = [
        "--ranging-calibration",
        str(RANGING_CALIBRATION_PATH),
        "--require-target-geolocation",
    ]

    strict_exit_code = cli_module.main(strict_command)

    assert strict_exit_code == 2
    strict_document = json.loads(strict_result_path.read_text(encoding="utf-8"))
    assert strict_document["schema_version"] == 3
    assert strict_document["gate_passed"] is False
    strict_acceptance = strict_document["target_geolocation_acceptance"]
    assert strict_acceptance["configured"] is True
    assert strict_acceptance["required"] is True
    assert strict_acceptance["gate_passed"] is False
    assert "no_valid_or_degraded_primary_range" in strict_acceptance["failure_reasons"]
    assert strict_acceptance["qgc_range_geolocation_adoption"] == "not_proved"
    strict_session_kwargs = captured["session_kwargs"]
    assert strict_session_kwargs["ranging_identity_hash"] == strict_acceptance["binding_sha256"]
    assert strict_session_kwargs["ranging_engine"] is not None
    assert strict_session_kwargs["ranging_config"] is not None
    assert strict_session_kwargs["adaptive_ranging_policy"] is not None

    stdout = capsys.readouterr().out
    assert '"integrated_sim_qgc_started"' in stdout
    assert '"integrated_sim_qgc_finished"' in stdout


def test_integrated_sim_qgc_fails_before_opening_backend_for_non_loopback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_module.main(
        [
            "integrated-sim-qgc",
            str(MISSION_PATH),
            str(SCENARIO_PATH),
            "--docker-executable",
            sys.executable,
            "--container-name",
            "must-not-open",
            "--container-image-sha256",
            "a" * 64,
            "--stream-script-sha256",
            "b" * 64,
            "--model-assets-sha256",
            "c" * 64,
            "--airframe-profile-sha256",
            "f" * 64,
            "--terrain-dem",
            str(TERRAIN_DEM_PATH),
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


def _ranging_namespace(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "ranging_calibration": RANGING_CALIBRATION_PATH,
        "require_target_geolocation": False,
        "frame_width": 1280,
        "frame_height": 960,
        "ranging_agl_sigma_m": 1.5,
        "ranging_roll_sigma_deg": 0.3,
        "ranging_pitch_sigma_deg": 0.3,
        "ranging_heading_sigma_deg": 1.0,
        "ranging_target_center_sigma_px": 2.0,
        "ranging_minimum_distance_m": 0.4,
        "ranging_maximum_distance_m": 800.0,
        "ranging_vehicle_profile": "auto",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_integrated_sim_ranging_builder_binds_calibration_dem_and_shared_graph() -> None:
    runtime_input = load_runtime_scenario(SCENARIO_PATH, mode=RuntimeMode.SIM)
    terrain_dem = load_local_dem_grid(TERRAIN_DEM_PATH)

    engine, config, policy, binding, binding_hash = cli_module._build_integrated_sim_ranging(
        _ranging_namespace(),
        runtime_input=runtime_input,
        terrain_dem=terrain_dem,
    )

    assert engine is not None
    assert config is not None and config.terrain_dem is terrain_dem
    assert policy is not None
    assert binding is not None
    assert binding["algorithm"] == "shared-production-multimodal-ranging"
    assert binding["calibration"]["calibration_id"] == "gazebo-front-camera-v1"
    assert binding["terrain_dem"]["sha256"] == terrain_dem.source_sha256
    assert binding["advisory_only"] is True
    assert binding["flight_control_enabled"] is False
    assert binding_hash == cli_module.canonical_sha256(binding)


def test_integrated_sim_ranging_builder_rejects_wrong_calibration_id(
    tmp_path: Path,
) -> None:
    runtime_input = load_runtime_scenario(SCENARIO_PATH, mode=RuntimeMode.SIM)
    terrain_dem = load_local_dem_grid(TERRAIN_DEM_PATH)
    document = json.loads(RANGING_CALIBRATION_PATH.read_text(encoding="utf-8"))
    document["calibration"]["calibration_id"] = "wrong-camera"
    wrong_path = tmp_path / "wrong-camera.json"
    wrong_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match scenario RGB camera"):
        cli_module._build_integrated_sim_ranging(
            _ranging_namespace(ranging_calibration=wrong_path),
            runtime_input=runtime_input,
            terrain_dem=terrain_dem,
        )


def test_integrated_sim_ranging_builder_rejects_wire_geometry_mismatch() -> None:
    runtime_input = load_runtime_scenario(SCENARIO_PATH, mode=RuntimeMode.SIM)
    terrain_dem = load_local_dem_grid(TERRAIN_DEM_PATH)

    with pytest.raises(ValueError, match="does not match Gazebo wire geometry"):
        cli_module._build_integrated_sim_ranging(
            _ranging_namespace(frame_width=640),
            runtime_input=runtime_input,
            terrain_dem=terrain_dem,
        )


def test_integrated_sim_ranging_acceptance_requires_shared_range_and_geo_publish() -> None:
    audit = cli_module.AuditLog()
    audit.append(
        "ranging.primary_target_solution",
        12.0,
        {
            "target_id": "target-a",
            "frame_id": "frame-12",
            "validity": "degraded",
            "sources": ("dem_gps", "camera_ground"),
            "target_geolocation": {
                "available": True,
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
            },
        },
    )
    audit.append(
        "operator.remote_range_status",
        12.01,
        {
            "target_id": "target-a",
            "source_frame_id": "frame-12",
            "validity": "degraded",
            "source_age_s": 0.01,
            "data_freshness_s": 0.01,
        },
    )
    audit.append(
        "operator.remote_target_geolocation_status",
        12.01,
        {
            "target_id": "target-a",
            "source_frame_id": "frame-12",
            "available": True,
            "source_age_s": 0.01,
        },
    )
    result = SimpleNamespace(
        remote_range_status_count=1,
        remote_target_geolocation_status_count=1,
        ranging_error_count=0,
        remote_transport_error_count=0,
    )

    acceptance = cli_module._integrated_sim_ranging_acceptance(
        result=result,
        audit_log=audit,
        binding={"schema_version": 1},
        binding_hash="a" * 64,
        required=True,
    )

    assert acceptance["gate_passed"] is True
    assert acceptance["status"] == "passed"
    assert acceptance["qualified_primary_solution_count"] == 1
    assert acceptance["camera_ground_solution_count"] == 1
    assert acceptance["available_geolocation_count"] == 1
    assert acceptance["operator_range_publish_count"] == 1
    assert acceptance["operator_target_geolocation_publish_count"] == 1
    assert acceptance["source_bound_operator_range_geolocation_count"] == 1
    assert acceptance["qgc_range_geolocation_adoption"] == "not_proved"
    assert acceptance["operator_visual_acceptance"] == "not_claimed"


def test_integrated_sim_ranging_acceptance_rejects_cross_frame_publication_counts() -> None:
    audit = cli_module.AuditLog()
    audit.append(
        "ranging.primary_target_solution",
        12.0,
        {
            "target_id": "target-a",
            "frame_id": "frame-12",
            "validity": "valid",
            "sources": ("camera_ground",),
            "target_geolocation": {"available": True},
        },
    )
    audit.append(
        "operator.remote_range_status",
        12.1,
        {
            "target_id": "target-a",
            "source_frame_id": "different-frame",
            "validity": "valid",
            "source_age_s": 0.1,
            "data_freshness_s": 0.1,
        },
    )
    audit.append(
        "operator.remote_target_geolocation_status",
        12.1,
        {
            "target_id": "target-a",
            "source_frame_id": "different-frame",
            "available": True,
            "source_age_s": 0.1,
        },
    )

    acceptance = cli_module._integrated_sim_ranging_acceptance(
        result=SimpleNamespace(
            remote_range_status_count=1,
            remote_target_geolocation_status_count=1,
            ranging_error_count=0,
            remote_transport_error_count=0,
        ),
        audit_log=audit,
        binding={"schema_version": 1},
        binding_hash="a" * 64,
        required=True,
    )

    assert acceptance["gate_passed"] is False
    assert acceptance["source_bound_operator_range_geolocation_count"] == 0
    assert "no_source_bound_operator_range_geolocation" in acceptance["failure_reasons"]


def test_integrated_sim_qgc_strict_geolocation_requires_calibration_before_backend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden_backend(**_kwargs: object) -> object:
        raise AssertionError("backend must not be constructed")

    monkeypatch.setattr(cli_module, "GazeboSimBackend", forbidden_backend)
    exit_code = cli_module.main(
        [
            "integrated-sim-qgc",
            str(MISSION_PATH),
            str(SCENARIO_PATH),
            "--docker-executable",
            sys.executable,
            "--container-name",
            "must-not-open",
            "--container-image-sha256",
            "a" * 64,
            "--stream-script-sha256",
            "b" * 64,
            "--model-assets-sha256",
            "c" * 64,
            "--airframe-profile-sha256",
            "f" * 64,
            "--terrain-dem",
            str(TERRAIN_DEM_PATH),
            "--require-target-geolocation",
            "--rtsp-helper",
            "helper.exe",
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
    assert "requires --ranging-calibration" in capsys.readouterr().err

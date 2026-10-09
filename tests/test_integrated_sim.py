from __future__ import annotations

import hashlib
import io
import struct
import sys
from pathlib import Path

import pytest

from multidetect.adaptive_ranging import AdaptiveRangingPolicy
from multidetect.backends.gazebo_sim import GazeboSimBackend
from multidetect.config import MissionConfig
from multidetect.integrated_sim import IntegratedSimIdentity, build_integrated_sim_session
from multidetect.live import LiveRangingConfig
from multidetect.mission import MissionController
from multidetect.multimodal_ranging import (
    CameraCalibration,
    MultiModalRangingEngine,
    load_camera_calibration,
)
from multidetect.operator_link import RuntimeBackendHealth
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario
from multidetect.simulator import SimulatorPerceptionBridge
from multidetect.terrain_dem import load_local_dem_grid
from multidetect.unified_tracking import UnifiedTargetPool
from scripts.gazebo_depth_wire import build_depth_wire_record
from scripts.gazebo_sensor_stream import WIRE_HEADER_BYTES, build_wire_frame, unpack_header

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
MISSION = ROOT / "configs/missions/fire_patrol.demo.json"
TERRAIN_DEM = ROOT / "simulation/terrain/river_rescue_vtol_001.dem.json"
TERRAIN_GRID = load_local_dem_grid(TERRAIN_DEM)
RANGING_CALIBRATION = ROOT / "simulation/gazebo/calibration/gazebo-front-camera-v1.json"


class _Process:
    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1


class _Detector:
    def __init__(self) -> None:
        self.frames = 0

    def detect(self, image: object) -> tuple[object, ...]:
        assert image.shape == (1, 2, 3)  # type: ignore[attr-defined]
        self.frames += 1
        return ()

    def covers_labels(self, labels: object) -> bool:
        del labels
        return False


def _wire(sequence: int, stamp_ns: int) -> bytes:
    payload = bytes((sequence + 1, 2, 3, 4, 5, 6))
    return build_wire_frame(
        sequence=sequence,
        sensor_stamp_ns=stamp_ns,
        width=2,
        height=1,
        camera_info_stamp_ns=stamp_ns - 3,
        navsat_stamp_ns=stamp_ns - 2,
        odometry_stamp_ns=stamp_ns - 1,
        latitude_deg=TERRAIN_GRID.origin_latitude_deg,
        longitude_deg=TERRAIN_GRID.origin_longitude_deg,
        altitude_m=TERRAIN_GRID.origin_elevation_m + 10.0,
        position_east_m=1.0,
        position_north_m=2.0,
        position_up_m=3.0,
        orientation_x=0.0,
        orientation_y=0.0,
        orientation_z=0.0,
        orientation_w=1.0,
        velocity_forward_mps=0.0,
        velocity_left_mps=0.0,
        velocity_up_mps=0.0,
        attitude_covariance_enu_rad2=(0.0,) * 9,
        bgr_payload=payload,
    )


def _depth_companion(backend: GazeboSimBackend, rgb_record: bytes) -> bytes:
    """Build the required depth companion for the current scenario fixture."""

    contract = dict(backend.frame_source.depth_contract or {})
    assert contract
    contract.pop("sensor_id")
    header = unpack_header(rgb_record[:WIRE_HEADER_BYTES])
    contract.update(
        {
            "sequence": header.sequence,
            "rgb_sensor_stamp_ns": header.sensor_stamp_ns,
            "rgb_record_sha256": header.sha256.hex(),
            "source_stamp_ns": header.sensor_stamp_ns,
            "stride_bytes": 8,
        }
    )
    return build_depth_wire_record(
        metadata=contract,
        payload=struct.pack("<ff", 5.0, 5.0),
    )


def _wire_stream(backend: GazeboSimBackend) -> bytes:
    records = []
    for sequence, stamp_ns in enumerate((1_000_000_000, 1_033_333_333)):
        rgb = _wire(sequence, stamp_ns)
        records.extend((rgb, _depth_companion(backend, rgb)))
    return b"".join(records)


def _backend(process: _Process, *, frame_count: int = 2) -> GazeboSimBackend:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    terrain_dem = TERRAIN_GRID
    return GazeboSimBackend(
        scenario_input=runtime_input,
        camera_calibration=CameraCalibration(
            calibration_id="gazebo-front-camera-v1",
            width_px=2,
            height_px=1,
            fx_px=1.0,
            fy_px=1.0,
            cx_px=1.0,
            cy_px=0.5,
        ),
        docker_executable=Path(sys.executable).resolve(),
        container_name="owned-integrated-sim",
        frame_count=frame_count,
        airframe_profile_hash="a" * 64,
        container_image_sha256="b" * 64,
        stream_script_sha256="c" * 64,
        model_assets_sha256="d" * 64,
        terrain_dem=terrain_dem,
        expected_width=2,
        expected_height=1,
        popen_factory=lambda *_args, **_kwargs: process,
    )


def _build_kwargs(backend: GazeboSimBackend) -> dict[str, object]:
    del backend
    return {
        "mission_config_hash": hashlib.sha256(MISSION.read_bytes()).hexdigest(),
        "perception_identity_hash": "e" * 64,
    }


def test_integrated_sim_uses_shared_core_with_required_full_odometry() -> None:
    process = _Process(b"")
    backend = _backend(process)
    process.stdout = io.BytesIO(_wire_stream(backend))
    detector = _Detector()
    mission = MissionController(MissionConfig.from_json(MISSION))
    session = build_integrated_sim_session(
        backend=backend,
        mission=mission,
        detector=detector,
        **_build_kwargs(backend),
    )

    result, transport = session.run()

    assert result.processed_frames == detector.frames == 2
    assert transport.complete is True
    assert transport.odometry_timestamp_frame_count == 2
    assert transport.odometry_required is True
    assert transport.full_odometry_pose_velocity_fusion_claimed is True
    assert session.runtime_status.mode is RuntimeMode.SIM
    assert session.runtime_status.backend_health is RuntimeBackendHealth.HEALTHY
    assert session.runtime_status.ground_truth_isolated is True
    assert session.runtime_status.hardware_outputs_present is False
    assert session.runtime_status.profile_valid is True
    assert session.runner.runtime_descriptor is backend.descriptor
    assert backend.descriptor.recording_hash is None
    # The integrated SIM builder binds one observation-only production bridge
    # by default.  It must consume the same LiveMissionRunner frame stream,
    # without creating a second transport or simulator-truth input.
    assert isinstance(session.perception_bridge, SimulatorPerceptionBridge)
    assert session.runner.simulator_perception_sink is not None
    assert result.simulator_perception_feed_count == 2
    assert result.simulator_perception_error_count == 0
    perception_evidence = session.perception_evidence()
    assert perception_evidence["attached"] is True
    assert perception_evidence["observations"] == 2
    assert perception_evidence["truth_free"] is True
    assert perception_evidence["secondary_transport_emitted"] is False


def test_integrated_sim_accepts_one_explicit_perception_bridge() -> None:
    process = _Process(b"")
    backend = _backend(process)
    bridge = SimulatorPerceptionBridge()
    session = build_integrated_sim_session(
        backend=backend,
        mission=MissionController(MissionConfig.from_json(MISSION)),
        detector=_Detector(),
        simulator_perception_bridge=bridge,
        **_build_kwargs(backend),
    )

    assert session.perception_bridge is bridge
    assert session.runner.simulator_perception_sink == bridge.feed_result


def test_integrated_sim_accepts_custom_sink_without_creating_a_bridge() -> None:
    process = _Process(b"")
    backend = _backend(process)
    process.stdout = io.BytesIO(_wire_stream(backend))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def sink(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    session = build_integrated_sim_session(
        backend=backend,
        mission=MissionController(MissionConfig.from_json(MISSION)),
        detector=_Detector(),
        simulator_perception_sink=sink,
        **_build_kwargs(backend),
    )

    assert session.perception_bridge is None
    assert session.runner.simulator_perception_sink is sink
    assert session.perception_evidence()["attached"] is False
    result, _transport = session.run()
    assert result.simulator_perception_feed_count == 2
    assert len(calls) == 2
    for args, kwargs in calls:
        assert len(args) == 4
        # No unified pool is configured in this fixture, so the sink must not
        # receive a dangling selected id alongside an empty track collection.
        assert kwargs["tracks"] == ()
        assert kwargs["selected_track_id"] is None


def test_integrated_sim_rejects_bridge_and_sink_double_binding() -> None:
    backend = _backend(_Process(b""))
    with pytest.raises(TypeError, match="mutually exclusive"):
        build_integrated_sim_session(
            backend=backend,
            mission=MissionController(MissionConfig.from_json(MISSION)),
            detector=_Detector(),
            simulator_perception_bridge=SimulatorPerceptionBridge(),
            simulator_perception_sink=lambda *_args, **_kwargs: None,
            **_build_kwargs(backend),
        )


def test_integrated_sim_binds_complete_shared_ranging_graph_into_v2_identity() -> None:
    backend = _backend(_Process(b""))
    engine = MultiModalRangingEngine()
    ranging_config = LiveRangingConfig(
        calibration=load_camera_calibration(RANGING_CALIBRATION),
        terrain_dem=TERRAIN_GRID,
    )
    policy = AdaptiveRangingPolicy()
    ranging_hash = "f" * 64

    session = build_integrated_sim_session(
        backend=backend,
        mission=MissionController(MissionConfig.from_json(MISSION)),
        detector=_Detector(),
        ranging_identity_hash=ranging_hash,
        ranging_engine=engine,
        ranging_config=ranging_config,
        adaptive_ranging_policy=policy,
        unified_target_pool=UnifiedTargetPool(),
        **_build_kwargs(backend),
    )

    assert isinstance(session.identity, IntegratedSimIdentity)
    assert session.identity.schema_version == 2
    assert session.identity.ranging_identity_hash == ranging_hash
    assert session.identity.to_mapping()["ranging_identity_hash"] == ranging_hash
    assert session.runner.ranging_engine is engine
    assert session.runner.ranging_config is ranging_config
    assert session.runner.adaptive_ranging_policy is policy


@pytest.mark.parametrize(
    "missing_component",
    ("ranging_engine", "ranging_config", "adaptive_ranging_policy"),
)
def test_integrated_sim_rejects_partial_ranging_graph(missing_component: str) -> None:
    backend = _backend(_Process(b""))
    components: dict[str, object | None] = {
        "ranging_engine": MultiModalRangingEngine(),
        "ranging_config": LiveRangingConfig(
            calibration=load_camera_calibration(RANGING_CALIBRATION),
            terrain_dem=TERRAIN_GRID,
        ),
        "adaptive_ranging_policy": AdaptiveRangingPolicy(),
    }
    components[missing_component] = None

    with pytest.raises(ValueError, match="engine, config and adaptive policy together"):
        build_integrated_sim_session(
            backend=backend,
            mission=MissionController(MissionConfig.from_json(MISSION)),
            detector=_Detector(),
            ranging_identity_hash="f" * 64,
            **components,
            **_build_kwargs(backend),
        )


def test_integrated_sim_rejects_ranging_identity_without_components() -> None:
    backend = _backend(_Process(b""))

    with pytest.raises(ValueError, match="components and ranging identity"):
        build_integrated_sim_session(
            backend=backend,
            mission=MissionController(MissionConfig.from_json(MISSION)),
            detector=_Detector(),
            ranging_identity_hash="f" * 64,
            **_build_kwargs(backend),
        )


def test_integrated_sim_rejects_ranging_components_without_identity() -> None:
    backend = _backend(_Process(b""))

    with pytest.raises(ValueError, match="components and ranging identity"):
        build_integrated_sim_session(
            backend=backend,
            mission=MissionController(MissionConfig.from_json(MISSION)),
            detector=_Detector(),
            ranging_engine=MultiModalRangingEngine(),
            ranging_config=LiveRangingConfig(
                calibration=load_camera_calibration(RANGING_CALIBRATION),
                terrain_dem=TERRAIN_GRID,
            ),
            adaptive_ranging_policy=AdaptiveRangingPolicy(),
            **_build_kwargs(backend),
        )

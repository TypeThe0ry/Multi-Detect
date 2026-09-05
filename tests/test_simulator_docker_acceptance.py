from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_simulator_docker_acceptance as module
from scripts.gazebo_sensor_stream import build_wire_frame
from scripts.run_simulator_docker import build_docker_simulator_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _completed(argv: list[str], *, code: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _inspect_record(plan: object, *, running: bool = True) -> dict[str, object]:
    model_root = Path(plan.model_root).resolve()  # type: ignore[attr-defined]
    world_path = Path(plan.world_path).resolve()  # type: ignore[attr-defined]
    scripts_root = Path(plan.scripts_root).resolve()  # type: ignore[attr-defined]
    environment = [
        f"{key}={value}"
        for key, value in plan.environment  # type: ignore[attr-defined]
    ]
    return {
        "Id": "container-id-123",
        "Name": f"/{plan.container_name}",  # type: ignore[attr-defined]
        "Config": {
            "Image": plan.image,  # type: ignore[attr-defined]
            "Labels": {"multidetect.purpose": "simulator-docker"},
            "Env": environment,
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "PortBindings": {
                f"{plan.host_control_port}/udp": [  # type: ignore[attr-defined]
                    {"HostIp": "127.0.0.1", "HostPort": str(plan.host_control_port)}  # type: ignore[attr-defined]
                ],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(model_root),
                    "Target": "/opt/multidetect-gazebo-models",
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": str(world_path),
                    "Target": "/opt/px4-gazebo/share/gz/worlds/default.sdf",
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": str(scripts_root),
                    "Target": "/workspace/scripts",
                    "ReadOnly": True,
                },
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(model_root),
                "Destination": "/opt/multidetect-gazebo-models",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(world_path),
                "Destination": "/opt/px4-gazebo/share/gz/worlds/default.sdf",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(scripts_root),
                "Destination": "/workspace/scripts",
                "RW": False,
            },
        ],
        "State": {"Running": running},
    }


class _Message:
    def __init__(self, kind: str, **fields: object) -> None:
        self.kind = kind
        self.__dict__.update(fields)

    def get_type(self) -> str:
        return self.kind


class _Connection:
    def __init__(self) -> None:
        self.messages = iter(
            (
                _Message(
                    "HEARTBEAT",
                    type=2,
                    autopilot=3,
                    base_mode=0,
                    custom_mode=0,
                    system_status=3,
                ),
                _Message(
                    "GPS_RAW_INT",
                    fix_type=3,
                    satellites_visible=12,
                    lat=13_000_000,
                    lon=103_800_500,
                    alt=5000,
                ),
                _Message(
                    "GLOBAL_POSITION_INT",
                    lat=13_000_000,
                    lon=103_800_500,
                    relative_alt=5000,
                    hdg=9000,
                ),
            )
        )
        self.sent_heartbeats = 0
        self.closed = False
        self.mav = SimpleNamespace(heartbeat_send=self._heartbeat_send)

    def _heartbeat_send(self, *_args: object) -> None:
        self.sent_heartbeats += 1

    def recv_match(self, *, blocking: bool = True, timeout: float = 0.2):  # noqa: ARG002
        try:
            return next(self.messages)
        except StopIteration:
            return None

    def close(self) -> None:
        self.closed = True


def _telemetry_factory(connections: list[_Connection]):
    def factory(endpoint: str) -> module.DockerTelemetryProbe:
        connection = _Connection()
        connections.append(connection)
        return module.DockerTelemetryProbe(
            endpoint,
            connection_factory=lambda _endpoint, **_kwargs: connection,
            mavutil_module=SimpleNamespace(
                mavlink=SimpleNamespace(MAV_TYPE_GCS=6, MAV_AUTOPILOT_INVALID=8)
            ),
        )

    return factory


def test_dry_run_is_plan_only_and_never_invokes_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object):
        calls.append(argv)
        return _completed(argv, code=99, stderr="runner must not be called")

    evidence = tmp_path / "dry.json"
    payload = module.run_acceptance(
        SCENARIO,
        evidence,
        execute=False,
        runner=runner,
    )
    assert payload["gate_passed"] is True
    assert payload["preflight_gate_passed"] is True
    assert payload["runtime_gate_passed"] is None
    assert payload["execution_requested"] is False
    assert payload["container_runtime"]["docker_invoked"] is False
    assert payload["visual"]["status"] == "not_claimed"
    assert payload["hardware"]["real_pixhawk_contacted"] is False
    persistence = payload["sensor_transport"]["pixel_persistence"]
    assert persistence["requested"] is False
    assert persistence["status"] == "disabled"
    assert persistence["persisted_frame_count"] == 0
    assert persistence["manifest"] is None
    assert persistence["actual_gazebo_pixels_persisted"] is False
    assert calls == []
    assert json.loads(evidence.read_text(encoding="utf-8"))["gate_passed"] is True


def test_dry_run_records_sensor_capture_as_planned_without_claiming_it(
    tmp_path: Path,
) -> None:
    payload = module.run_acceptance(
        SCENARIO,
        tmp_path / "sensor-plan.json",
        execute=False,
        sensor_frame_count=3,
    )
    assert payload["gate_passed"] is True
    assert payload["sensor_transport_gate_passed"] is None
    sensor = payload["sensor_transport"]
    assert sensor["status"] == "planned_not_executed"
    assert sensor["requested"] is True
    assert sensor["requested_frame_count"] == 3
    assert sensor["qualified"] is False
    assert sensor["full_odometry_pose_velocity_fusion_claimed"] is False
    assert sensor["terrain_agl_reference"] == ("GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL")
    assert sensor["odometry_local_up_used_as_agl"] is False


def test_dry_run_records_explicit_pixel_output_without_creating_it(tmp_path: Path) -> None:
    frame_output = tmp_path / "planned-raw-frames"
    payload = module.run_acceptance(
        SCENARIO,
        tmp_path / "sensor-persistence-plan.json",
        execute=False,
        sensor_frame_count=2,
        sensor_frame_output_dir=frame_output,
    )

    persistence = payload["pixel_persistence"]
    assert persistence == payload["sensor_transport"]["pixel_persistence"]
    assert persistence["requested"] is True
    assert persistence["status"] == "planned_not_executed"
    assert persistence["output_directory"] == str(frame_output.resolve())
    assert persistence["persisted_frame_count"] == 0
    assert persistence["actual_gazebo_pixels_persisted"] is False
    assert persistence["qgc_rendering"] == "not_claimed"
    assert persistence["manual_visual_acceptance"] == "not_claimed"
    assert persistence["hardware"] == "not_claimed"
    assert not frame_output.exists()


def test_sensor_pixel_output_rejects_nonempty_directory_before_execution(
    tmp_path: Path,
) -> None:
    frame_output = tmp_path / "occupied"
    frame_output.mkdir()
    sentinel = frame_output / "keep.bin"
    sentinel.write_bytes(b"user-owned")
    calls: list[list[str]] = []

    with pytest.raises(module.DockerSimulatorAcceptanceError, match="must be empty"):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "must-not-exist.json",
            execute=True,
            sensor_frame_count=1,
            sensor_frame_output_dir=frame_output,
            runner=lambda argv, **_kwargs: calls.append(argv),
        )

    assert calls == []
    assert sentinel.read_bytes() == b"user-owned"


def test_sensor_pixel_output_requires_a_requested_frame(tmp_path: Path) -> None:
    with pytest.raises(
        module.DockerSimulatorAcceptanceError,
        match="requires sensor_frame_count > 0",
    ):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "invalid-output.json",
            sensor_frame_output_dir=tmp_path / "raw-frames",
        )


def test_raw_bgr24_persistence_is_exact_hash_bound_and_tamper_evident(
    tmp_path: Path,
) -> None:
    np = pytest.importorskip("numpy")
    raw_pixels = bytes((1, 2, 3, 4, 5, 6))
    payload_sha256 = hashlib.sha256(raw_pixels).hexdigest()
    frame = module.CapturedFrame(
        frame_id="gazebo-sim-000000",
        captured_at_s=50.0,
        image_bgr=np.frombuffer(raw_pixels, dtype=np.uint8).reshape((1, 2, 3)),
        width=2,
        height=1,
    )
    provenance = module.GazeboSimFrameProvenance(
        frame_id=frame.frame_id,
        sequence=0,
        captured_at_s=frame.captured_at_s,
        captured_at_clock="host_monotonic",
        sensor_stamp_ns=2_000_000_000,
        camera_info_stamp_ns=1_999_999_997,
        navsat_stamp_ns=1_999_999_998,
        odometry_stamp_ns=1_999_999_999,
        camera_info_source_skew_ns=3,
        navsat_source_skew_ns=2,
        odometry_source_skew_ns=1,
        record_sha256="a" * 64,
        payload_sha256=payload_sha256,
        width=2,
        height=1,
        stride=6,
        payload_bytes=6,
        flags=0x00010007,
        pixel_format="BGR24",
    )
    output_directory = tmp_path / "raw-frames"
    module._prepare_frame_output_directory(output_directory)

    record = module._persist_gazebo_frame(
        output_directory=output_directory,
        frame=frame,
        provenance=provenance,
    )

    raw_path = output_directory / "frame-000000.bgr24"
    assert raw_path.read_bytes() == raw_pixels
    assert record["bytes"] == len(raw_pixels)
    assert record["sha256"] == payload_sha256
    assert record["wire_payload_sha256"] == payload_sha256
    assert record["wire_record_sha256"] == "a" * 64
    assert record["raw_bytes_sha256_matches_wire_payload"] is True
    assert record["provenance"]["navsat_source_skew_ns"] == 2

    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
    )
    model_assets = module._model_assets_provenance(
        plan.model_root,
        plan.model_name,
        repository_root=plan.repository_root,
    )
    manifest_record = module._write_frame_set_manifest(
        output_directory=output_directory,
        plan=plan,
        sensor_binding_hash="b" * 64,
        model_assets=model_assets,
        records=[record],
    )
    manifest_path = output_directory / module.GAZEBO_BGR24_FRAME_SET_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    claimed_manifest_sha256 = manifest.pop("manifest_sha256")
    assert claimed_manifest_sha256 == module.canonical_sha256(manifest)
    assert manifest_record["manifest_sha256"] == claimed_manifest_sha256
    assert manifest_record["file_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert manifest["frame_count"] == 1
    assert manifest["total_payload_bytes"] == len(raw_pixels)
    assert manifest["frames"][0]["wire_record_sha256"] == "a" * 64
    assert manifest["claims"] == {
        "actual_gazebo_pixels_persisted": True,
        "detector_evaluation": "not_claimed",
        "hardware": "not_claimed",
        "manual_visual_acceptance": "not_claimed",
        "production_model_approval": "not_claimed",
        "qgc_rendering": "not_claimed",
    }
    assert manifest["source_attribution"] == {
        "environment": "software_simulation",
        "pixels": "actual_gazebo_camera_sensor_output",
        "source_row_order": module.GAZEBO_SIM_SOURCE_ROW_ORDER,
        "repository_model_asset_file_count": model_assets["file_count"],
        "repository_model_assets_sha256": model_assets["assets_sha256"],
        "truth_loaded_by_runtime": False,
        "vertical_flip_applied": module.GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        "wire": "MDGZSIM3",
        "wire_row_order": module.GAZEBO_SIM_WIRE_ROW_ORDER,
    }
    assert manifest["repository_model_assets"] == model_assets
    assert {
        item["path"] for item in manifest["repository_model_assets"]["files"]
    } >= {
        "multidetect_coco_car_billboard_v1/materials/textures/"
        "coco-car-positive-control-v1.png",
        "multidetect_coco_car_billboard_v1/meshes/car_billboard.mtl",
        "multidetect_coco_car_billboard_v1/meshes/car_billboard.obj",
    }

    with pytest.raises(module.DockerSimulatorAcceptanceError, match="overwrite"):
        module._persist_gazebo_frame(
            output_directory=output_directory,
            frame=frame,
            provenance=provenance,
        )
    raw_path.write_bytes(b"tampered")
    with pytest.raises(module.DockerSimulatorAcceptanceError, match="integrity"):
        module._verify_persisted_gazebo_frame(output_directory, record)


def test_raw_bgr24_persistence_rejects_pixels_changed_after_wire_validation(
    tmp_path: Path,
) -> None:
    np = pytest.importorskip("numpy")
    original = bytes((1, 2, 3, 4, 5, 6))
    changed = np.frombuffer(bytearray(original), dtype=np.uint8).reshape((1, 2, 3))
    changed[0, 0, 0] = 99
    frame = module.CapturedFrame(
        frame_id="gazebo-sim-000000",
        captured_at_s=1.0,
        image_bgr=changed,
        width=2,
        height=1,
    )
    provenance = module.GazeboSimFrameProvenance(
        frame_id=frame.frame_id,
        sequence=0,
        captured_at_s=1.0,
        captured_at_clock="host_monotonic",
        sensor_stamp_ns=10,
        camera_info_stamp_ns=7,
        navsat_stamp_ns=8,
        odometry_stamp_ns=9,
        camera_info_source_skew_ns=3,
        navsat_source_skew_ns=2,
        odometry_source_skew_ns=1,
        record_sha256="a" * 64,
        payload_sha256=hashlib.sha256(original).hexdigest(),
        width=2,
        height=1,
        stride=6,
        payload_bytes=6,
        flags=0x00010007,
        pixel_format="BGR24",
    )
    output_directory = tmp_path / "changed"
    module._prepare_frame_output_directory(output_directory)

    with pytest.raises(
        module.DockerSimulatorAcceptanceError,
        match="no longer match",
    ):
        module._persist_gazebo_frame(
            output_directory=output_directory,
            frame=frame,
            provenance=provenance,
        )
    assert list(output_directory.iterdir()) == []


def test_sensor_transport_emits_provenance_and_hash_bound_raw_frame_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
    )
    terrain_dem = module.load_local_dem_grid(plan.dem_path)
    raw_pixels = bytes((10, 20, 30, 40, 50, 60))
    packet = build_wire_frame(
        sequence=0,
        sensor_stamp_ns=2_000_000_000,
        width=2,
        height=1,
        camera_info_stamp_ns=1_999_999_997,
        navsat_stamp_ns=1_999_999_998,
        odometry_stamp_ns=1_999_999_999,
        latitude_deg=terrain_dem.origin_latitude_deg,
        longitude_deg=terrain_dem.origin_longitude_deg,
        altitude_m=terrain_dem.origin_elevation_m + 10.0,
        position_east_m=0.0,
        position_north_m=0.0,
        position_up_m=10.0,
        orientation_x=0.0,
        orientation_y=0.0,
        orientation_z=0.0,
        orientation_w=1.0,
        velocity_forward_mps=0.0,
        velocity_left_mps=0.0,
        velocity_up_mps=0.0,
        attitude_covariance_enu_rad2=(0.0,) * 9,
        bgr_payload=raw_pixels,
    )

    class _SensorProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(packet)
            self.stderr = io.BytesIO(b"")
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

    real_backend = module.GazeboSimBackend
    process = _SensorProcess()

    def backend_factory(**kwargs: object):
        return real_backend(
            **kwargs,
            popen_factory=lambda *_args, **_kwargs: process,
        )

    monkeypatch.setattr(module, "GazeboSimBackend", backend_factory)
    monkeypatch.setattr(
        module,
        "_camera_calibration_provenance",
        lambda **_kwargs: {
            "schema_version": 1,
            "calibration_id": "gazebo-front-camera-v1",
            "repository_path": "simulation/gazebo/calibration/test.json",
            "file_sha256": "e" * 64,
            "calibration": {
                "calibration_id": "gazebo-front-camera-v1",
                "width_px": 2,
                "height_px": 1,
                "fx_px": 1.0,
                "fy_px": 1.0,
                "cx_px": 1.0,
                "cy_px": 0.5,
            },
            "source": "repository_calibration_hash_bound",
            "camera_info_intrinsics_semantic_validation": (
                "pending_k_p_distortion_validation"
            ),
        },
    )
    frame_output = tmp_path / "actual-gazebo-pixels"

    result = module._capture_gazebo_sensor_transport(
        plan=plan,
        manifest_path=SCENARIO,
        docker_executable=str(Path(sys.executable).resolve()),
        frame_count=1,
        expected_width=2,
        expected_height=1,
        timeout_s=2.0,
        frame_output_directory=frame_output,
    )

    assert result["qualified"] is True
    assert result["source_row_order"] == module.GAZEBO_SIM_SOURCE_ROW_ORDER
    assert result["wire_row_order"] == module.GAZEBO_SIM_WIRE_ROW_ORDER
    assert result["vertical_flip_applied"] is module.GAZEBO_SIM_VERTICAL_FLIP_APPLIED
    assert result["camera_calibration"]["camera_info_intrinsics_semantic_validation"] == (
        module.GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
    )
    assert result["frames"][0]["frame_id"] == "gazebo-sim-000000"
    assert result["frames"][0]["sensor_stamp_ns"] == 2_000_000_000
    assert result["frames"][0]["camera_info_source_skew_ns"] == 3
    assert result["frames"][0]["navsat_source_skew_ns"] == 2
    assert result["frames"][0]["odometry_source_skew_ns"] == 1
    assert result["frames"][0]["record_sha256"]
    assert result["frames"][0]["payload_sha256"] == hashlib.sha256(raw_pixels).hexdigest()
    assert result["frames"][0]["raw_bytes_sha256_matches_wire_payload"] is True
    persistence = result["pixel_persistence"]
    assert persistence["status"] == "passed"
    assert persistence["persisted_frame_count"] == 1
    assert persistence["raw_bytes_sha256_matches_wire_payload"] is True
    assert persistence["actual_gazebo_pixels_persisted"] is True
    assert persistence["qgc_rendering"] == "not_claimed"
    assert persistence["manual_visual_acceptance"] == "not_claimed"
    assert persistence["hardware"] == "not_claimed"
    assert (frame_output / "frame-000000.bgr24").read_bytes() == raw_pixels
    manifest_path = frame_output / module.GAZEBO_BGR24_FRAME_SET_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = manifest.pop("manifest_sha256")
    assert manifest_sha256 == module.canonical_sha256(manifest)
    assert manifest["schema_version"] == 2
    assert manifest["sensor_binding_hash"] == result["sensor_binding_hash"]
    assert manifest["frames"][0]["sha256"] == hashlib.sha256(raw_pixels).hexdigest()
    assert manifest["frames"][0]["wire_record_sha256"] == result["frames"][0]["record_sha256"]
    provenance = manifest["frames"][0]["provenance"]
    assert provenance["wire_unsigned_header_hex"]
    assert provenance["wire_telemetry"]["latitude_deg"] == pytest.approx(
        terrain_dem.origin_latitude_deg
    )
    assert provenance["frame_telemetry"]["altitude_agl_m"] == pytest.approx(10.0)
    assert provenance["frame_telemetry"]["heading_deg"] == pytest.approx(90.0)
    assert manifest["claims"]["per_frame_sensor_geolocation_input"] == "passed"
    assert manifest["claims"]["target_geolocation_evaluation"] == "not_claimed"
    assert manifest["source_attribution"]["camera_info_intrinsics_semantic_validation"] == (
        module.GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
    )
    assert manifest["source_attribution"]["source_row_order"] == (
        module.GAZEBO_SIM_SOURCE_ROW_ORDER
    )
    assert manifest["source_attribution"]["wire_row_order"] == module.GAZEBO_SIM_WIRE_ROW_ORDER
    assert manifest["source_attribution"]["vertical_flip_applied"] is (
        module.GAZEBO_SIM_VERTICAL_FLIP_APPLIED
    )


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    (
        ("sensor_frame_count", -1, "sensor_frame_count"),
        ("sensor_frame_count", True, "sensor_frame_count"),
        ("sensor_width", 0, "sensor_width"),
        ("sensor_height", 16_385, "sensor_height"),
    ),
)
def test_sensor_capture_arguments_are_bounded(
    tmp_path: Path,
    keyword: str,
    value: object,
    message: str,
) -> None:
    kwargs = {keyword: value}
    with pytest.raises(module.DockerSimulatorAcceptanceError, match=message):
        module.run_acceptance(
            SCENARIO,
            tmp_path / "invalid-sensor-argument.json",
            **kwargs,
        )


def test_telemetry_probe_requires_heartbeat_and_qualified_gps() -> None:
    connection = _Connection()
    probe = module.DockerTelemetryProbe(
        "udpin:127.0.0.1:14541",
        connection_factory=lambda _endpoint, **_kwargs: connection,
        mavutil_module=SimpleNamespace(
            mavlink=SimpleNamespace(MAV_TYPE_GCS=6, MAV_AUTOPILOT_INVALID=8)
        ),
    )
    result = probe.observe(1.0)
    assert result["qualified"] is True
    assert result["heartbeat_observed"] is True
    assert result["gps_observed"] is True
    assert result["gcs_heartbeat_sent"] >= 1
    assert connection.closed is True


def test_execute_starts_observes_and_always_cleans_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = tmp_path / "job"
    plan = build_docker_simulator_plan(SCENARIO, repository_root=ROOT, job_dir=job)
    calls: list[list[str]] = []
    inspect_count = 0

    def runner(argv: list[str], **_kwargs: object):
        nonlocal inspect_count
        calls.append(argv)
        args = argv[1:]
        if args[:3] == ["container", "inspect", plan.container_name]:
            inspect_count += 1
            if inspect_count == 1:
                return _completed(argv, code=1, stderr="No such container")
            return _completed(argv, stdout=json.dumps([_inspect_record(plan)]))
        if args[:3] == ["image", "inspect", plan.image]:
            return _completed(argv, stdout="sha256:image-id\n")
        if args[:1] == ["run"]:
            return _completed(argv, stdout="container-id-123\n")
        if args[:1] == ["logs"]:
            return _completed(argv, stdout="PX4 startup\n")
        if args[:1] == ["stop"]:
            return _completed(argv, stdout=plan.container_name + "\n")
        if args[:1] == ["rm"]:
            return _completed(argv, stdout=plan.container_name + "\n")
        raise AssertionError(f"unexpected docker call: {argv}")

    connections: list[_Connection] = []
    sensor_calls: list[dict[str, object]] = []

    def capture_sensor_transport(**kwargs: object) -> dict[str, object]:
        sensor_calls.append(kwargs)
        return {
            "status": "passed",
            "requested": True,
            "requested_frame_count": 2,
            "qualified": True,
            "wire_version": 3,
            "odometry_required": True,
            "full_odometry_pose_velocity_fusion_claimed": True,
            "terrain_agl_reference": "GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL",
            "odometry_local_up_used_as_agl": False,
            "truth_free": True,
            "pixel_persistence": {
                "requested": True,
                "status": "passed",
                "output_directory": str((tmp_path / "raw-frames").resolve()),
                "persisted_frame_count": 2,
                "raw_bytes_sha256_matches_wire_payload": True,
                "manifest": {"manifest_sha256": "f" * 64},
                "actual_gazebo_pixels_persisted": True,
                "qgc_rendering": "not_claimed",
                "manual_visual_acceptance": "not_claimed",
                "hardware": "not_claimed",
            },
        }

    monkeypatch.setattr(
        module,
        "_capture_gazebo_sensor_transport",
        capture_sensor_transport,
    )
    evidence = tmp_path / "acceptance.json"
    payload = module.run_acceptance(
        SCENARIO,
        evidence,
        execute=True,
        timeout_s=5.0,
        docker="docker-fake",
        container_name=plan.container_name,
        sensor_frame_count=2,
        sensor_frame_output_dir=tmp_path / "raw-frames",
        runner=runner,
        telemetry_probe_factory=_telemetry_factory(connections),
    )
    assert payload["gate_passed"] is True
    assert payload["runtime_gate_passed"] is True
    runtime = payload["container_runtime"]
    assert runtime["status"] == "passed"
    assert runtime["container_running_observed"] is True
    assert runtime["cleanup"]["attempted"] is True
    assert runtime["cleanup"]["removed"] is True
    assert payload["telemetry"]["qualified"] is True
    assert payload["sensor_transport_gate_passed"] is True
    assert payload["sensor_transport"]["qualified"] is True
    assert payload["sensor_transport"]["wire_version"] == 3
    assert payload["sensor_transport"]["terrain_agl_reference"] == (
        "GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL"
    )
    assert payload["sensor_transport"]["odometry_local_up_used_as_agl"] is False
    assert sensor_calls and sensor_calls[0]["frame_count"] == 2
    assert sensor_calls[0]["frame_output_directory"] == (tmp_path / "raw-frames").resolve()
    assert sensor_calls[0]["plan"].scripts_root == ROOT / "scripts"  # type: ignore[union-attr]
    assert payload["pixel_persistence"]["status"] == "passed"
    assert payload["pixel_persistence"]["raw_bytes_sha256_matches_wire_payload"] is True
    assert payload["safety"]["flight_control_commands_sent"] == 0
    assert payload["visual"]["qgc_fly_view_visual_acceptance"] == "not_claimed"
    assert connections and connections[0].closed is True
    assert any(call[1] == "run" for call in calls)
    assert any(call[1] == "stop" for call in calls)
    assert any(call[1] == "rm" for call in calls)
    assert evidence.is_file()


def test_execute_failure_still_attempts_stop_and_remove(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
        container_name="failure-sim",
    )
    calls: list[list[str]] = []
    inspect_count = 0

    def runner(argv: list[str], **_kwargs: object):
        nonlocal inspect_count
        calls.append(argv)
        args = argv[1:]
        if args[:3] == ["container", "inspect", plan.container_name]:
            inspect_count += 1
            if inspect_count == 1:
                return _completed(argv, code=1)
            return _completed(argv, stdout=json.dumps([_inspect_record(plan)]))
        if args[:3] == ["image", "inspect", plan.image]:
            return _completed(argv, stdout="sha256:image-id\n")
        if args[:1] == ["run"]:
            return _completed(argv, stdout="container-id-123\n")
        if args[:1] in (["logs"], ["stop"], ["rm"]):
            return _completed(argv, stdout="ok\n")
        raise AssertionError(f"unexpected docker call: {argv}")

    class _NoGpsConnection(_Connection):
        def __init__(self) -> None:
            self.messages = iter((_Message("HEARTBEAT", type=2),))
            self.sent_heartbeats = 0
            self.closed = False
            self.mav = SimpleNamespace(heartbeat_send=self._heartbeat_send)

    def telemetry_factory(endpoint: str) -> module.DockerTelemetryProbe:
        connection = _NoGpsConnection()
        return module.DockerTelemetryProbe(
            endpoint,
            connection_factory=lambda _endpoint, **_kwargs: connection,
            mavutil_module=SimpleNamespace(
                mavlink=SimpleNamespace(MAV_TYPE_GCS=6, MAV_AUTOPILOT_INVALID=8)
            ),
        )

    payload = module.run_acceptance(
        SCENARIO,
        tmp_path / "failed.json",
        execute=True,
        timeout_s=1.0,
        docker="docker-fake",
        container_name=plan.container_name,
        runner=runner,
        telemetry_probe_factory=telemetry_factory,
    )
    assert payload["gate_passed"] is False
    assert payload["runtime_gate_passed"] is False
    assert payload["failure"]
    assert payload["container_runtime"]["cleanup"]["attempted"] is True
    assert payload["container_runtime"]["cleanup"]["removed"] is True
    assert any(call[1] == "stop" for call in calls)
    assert any(call[1] == "rm" for call in calls)


def test_docker_run_failure_reclaims_partially_created_named_container(tmp_path: Path) -> None:
    """A daemon can leave a Created container when network setup fails."""

    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "job",
        container_name="run-failure-sim",
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object):
        calls.append(argv)
        args = argv[1:]
        if args[:3] == ["container", "inspect", plan.container_name]:
            return _completed(argv, code=1, stderr="No such container")
        if args[:3] == ["image", "inspect", plan.image]:
            return _completed(argv, stdout="sha256:image-id\n")
        if args[:1] == ["run"]:
            return _completed(argv, code=125, stderr="port is already allocated")
        if args[:1] in (["logs"], ["stop"], ["rm"]):
            return _completed(argv, stdout="ok\n")
        raise AssertionError(f"unexpected docker call: {argv}")

    payload = module.run_acceptance(
        SCENARIO,
        tmp_path / "run-failure.json",
        execute=True,
        timeout_s=5.0,
        docker="docker-fake",
        container_name=plan.container_name,
        runner=runner,
        telemetry_probe_factory=lambda _endpoint: pytest.fail("telemetry must not start"),
    )
    assert payload["gate_passed"] is False
    assert payload["container_runtime"]["docker_invoked"] is True
    cleanup = payload["container_runtime"]["cleanup"]
    assert cleanup["attempted"] is True
    assert cleanup["removed"] is True
    assert any(call[1] == "stop" for call in calls)
    assert any(call[1] == "rm" for call in calls)


def test_owned_container_rejects_writable_mount(tmp_path: Path) -> None:
    plan = build_docker_simulator_plan(SCENARIO, repository_root=ROOT, job_dir=tmp_path / "job")
    record = _inspect_record(plan)
    record["Mounts"][0]["RW"] = True  # type: ignore[index]
    with pytest.raises(module.DockerSimulatorAcceptanceError, match="readonly"):
        module._validate_owned_container(record, plan)


def test_owned_container_rejects_published_telemetry_port(tmp_path: Path) -> None:
    """Keep Docker Desktop's telemetry leg outbound-only to avoid UDP hairpinning."""

    plan = build_docker_simulator_plan(SCENARIO, repository_root=ROOT, job_dir=tmp_path / "job")
    record = _inspect_record(plan)
    record["HostConfig"]["PortBindings"][f"{plan.host_telemetry_port}/udp"] = [  # type: ignore[index]
        {"HostIp": "127.0.0.1", "HostPort": str(plan.host_telemetry_port)}
    ]
    with pytest.raises(module.DockerSimulatorAcceptanceError, match="telemetry_port_not_published"):
        module._validate_owned_container(record, plan)


def test_cli_defaults_to_no_external_execution(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "cli.json"
    code = module.main(
        [
            "--scenario-manifest",
            str(SCENARIO),
            "--evidence",
            str(out),
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["execution_requested"] is False
    assert printed["container_runtime"]["docker_invoked"] is False
    assert json.loads(out.read_text(encoding="utf-8")) == printed


def test_cli_accepts_explicit_sensor_frame_output_directory_as_plan_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "cli-persistence.json"
    frame_output = tmp_path / "cli-raw-frames"
    code = module.main(
        [
            "--scenario-manifest",
            str(SCENARIO),
            "--evidence",
            str(out),
            "--sensor-frames",
            "1",
            "--sensor-frame-output-dir",
            str(frame_output),
        ]
    )

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["execution_requested"] is False
    assert printed["pixel_persistence"]["requested"] is True
    assert printed["pixel_persistence"]["status"] == "planned_not_executed"
    assert printed["pixel_persistence"]["output_directory"] == str(frame_output.resolve())
    assert not frame_output.exists()

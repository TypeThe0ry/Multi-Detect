from __future__ import annotations

import copy
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.backends.gazebo_sim import (
    GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
    GazeboSimWireTelemetryProvenance,
    gazebo_sim_ned_frd_kinematics,
    read_gazebo_sim_wire_frame,
)
from multidetect.domain import BoundingBox, Detection
from multidetect.gazebo_target_geolocation_acceptance import (
    EVALUATOR_SOURCE_PATH,
    GazeboTargetGeolocationConfig,
    GazeboTargetGeolocationInputError,
    evaluate_gazebo_target_geolocation,
    verify_gazebo_target_geolocation_evidence,
    write_evidence_exclusive,
)
from multidetect.model_manifest import sha256_file
from multidetect.multimodal_ranging import (
    AircraftPose,
    MultiModalRangingEngine,
    RangeValidity,
    TargetImageObservation,
    load_camera_calibration,
)
from multidetect.release_parity import DEFAULT_FILE_LIST
from multidetect.simulation.index import load_verified_channel
from multidetect.simulator.stack import resolve_repository_model_asset_bundle
from multidetect.target_geolocation import target_geolocation_from_ned_offset
from multidetect.target_image_anchor import target_image_anchor
from multidetect.terrain_dem import dem_gps_terrain_observation, load_local_dem_grid
from scripts.gazebo_sensor_stream import build_wire_frame

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_MANIFEST = (
    ROOT / "configs/models/ultralytics_yolo26n_coco80_v56_quarantined_candidate.manifest.json"
)


@dataclass(frozen=True, slots=True)
class _Case:
    root: Path
    frame_set: Path
    scenario: Path
    model: Path
    model_manifest: Path
    detection: Detection
    frame_count: int


class _FakeDetector:
    provider_names = ("FixtureExecutionProvider",)
    provider_selection = None

    def __init__(self, detection: Detection) -> None:
        self.detection = detection
        self.calls = 0

    def detect(self, _image_bgr: Any) -> tuple[Detection, ...]:
        self.calls += 1
        return (self.detection,)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _build_case(
    tmp_path: Path,
    *,
    altitude_agl_m: float = 10.0,
    mount_pitch_down_deg: float = 45.0,
    detection_bbox: BoundingBox | None = None,
) -> _Case:
    detection_bbox = detection_bbox or BoundingBox(0.40, 0.55, 0.60, 0.82)
    root = tmp_path / "repo"
    (root / "pyproject.toml").parent.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    version_manifest = root / "configs/product-version.json"
    version_manifest.parent.mkdir(parents=True, exist_ok=True)
    version_manifest.write_text(
        (ROOT / "configs/product-version.json").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    # The evaluator binds the five canonical release-parity groups.  Fixture
    # checkouts therefore provide a minimal file for every explicit source
    # path; their contents are irrelevant, but changing one must invalidate a
    # previously generated acceptance document.
    for group, paths in DEFAULT_FILE_LIST.items():
        for relative_path in paths:
            parity_source = root / relative_path
            parity_source.parent.mkdir(parents=True, exist_ok=True)
            parity_source.write_text(
                f"fixture parity source: {group}:{relative_path}\n",
                encoding="utf-8",
                newline="\n",
            )
    evaluator_source = root / EVALUATOR_SOURCE_PATH
    evaluator_source.parent.mkdir(parents=True, exist_ok=True)
    evaluator_source.write_text(
        "# fixture geolocation evaluator source\n",
        encoding="utf-8",
        newline="\n",
    )
    model_asset = root / "simulation/gazebo/models/fixture/model.sdf"
    model_asset.parent.mkdir(parents=True, exist_ok=True)
    model_asset.write_text("<sdf version='1.10'><model name='fixture'/></sdf>\n", encoding="utf-8")

    calibration_document = {
        "schema_version": 1,
        "calibration": {
            "calibration_id": "fixture-camera-v1",
            "width_px": 64,
            "height_px": 48,
            "fx_px": 50.0,
            "fy_px": 50.0,
            "cx_px": 32.0,
            "cy_px": 24.0,
            "mount_pitch_down_deg": mount_pitch_down_deg,
            "mount_yaw_right_deg": 0.0,
            "mount_roll_clockwise_deg": 0.0,
            "k1": 0.0,
            "k2": 0.0,
            "p1": 0.0,
            "p2": 0.0,
            "k3": 0.0,
            "boresight_sigma_deg": 0.01,
            "camera_offset_forward_m": 0.0,
            "camera_offset_right_m": 0.0,
            "camera_offset_down_m": 0.0,
            "camera_offset_sigma_m": 0.001,
        },
    }
    calibration_path = root / "simulation/gazebo/calibration/fixture-camera-v1.json"
    _write_json(calibration_path, calibration_document)
    calibration = load_camera_calibration(calibration_path)

    scenario_id = "fixture_gazebo_geolocation_001"
    dem_document = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "coordinate_reference": "WGS84",
        "vertical_datum": "MSL",
        "origin": {
            "latitude_deg": 1.3,
            "longitude_deg": 103.8005,
            "elevation_m": 25.0,
        },
        "north_origin_m": -100.0,
        "east_origin_m": -100.0,
        "north_spacing_m": 100.0,
        "east_spacing_m": 100.0,
        "elevation_m": [
            [25.0, 25.0, 25.0],
            [25.0, 25.0, 25.0],
            [25.0, 25.0, 25.0],
        ],
        "vertical_sigma_m": 0.02,
        "slope_sigma_m_per_m": 0.0001,
    }
    dem_path = root / "simulation/terrain/fixture.dem.json"
    _write_json(dem_path, dem_document)
    terrain_dem = load_local_dem_grid(dem_path)

    model_path = root / "artifacts/models/fixture.onnx"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"fixture model bytes")
    model_manifest = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    model_manifest["export"]["artifact_path"] = "artifacts/models/fixture.onnx"
    model_manifest["export"]["artifact_sha256"] = sha256_file(model_path)
    model_manifest_path = root / "configs/models/fixture.manifest.json"
    _write_json(model_manifest_path, model_manifest)

    detection = Detection(
        label="car",
        confidence=0.90,
        bbox=detection_bbox,
        model_version=model_manifest["model_version"],
    )
    anchor = target_image_anchor(
        label=detection.label,
        bbox=detection.bbox,
        image_width_px=calibration.width_px,
        image_height_px=calibration.height_px,
        minimum_sigma_px=2.0,
    )
    pose = AircraftPose(
        captured_at_s=100.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        heading_deg=90.0,
        roll_sigma_deg=0.3,
        pitch_sigma_deg=0.3,
        heading_sigma_deg=1.0,
    )
    target = TargetImageObservation(
        target_id="fixture-car",
        frame_id="fixture-frame-000000",
        captured_at_s=100.0,
        center_x=anchor.x,
        center_y=anchor.y,
        center_sigma_px=anchor.sigma_px,
    )
    terrain = dem_gps_terrain_observation(
        terrain_dem,
        latitude_deg=1.3,
        longitude_deg=103.8005,
        gps_absolute_altitude_m=25.0 + altitude_agl_m,
        gps_horizontal_sigma_m=0.05,
        gps_vertical_sigma_m=0.05,
        captured_at_s=100.0,
    )
    solution = MultiModalRangingEngine().solve(
        calibration=calibration,
        pose=pose,
        target=target,
        vertical_measurements=(),
        terrain_observation=terrain,
        direct_measurements=(),
        fusion_profile="outdoor-multimodal-v1",
        vehicle_profile="multirotor",
        navigation_state="gps-aided",
        motion_regime="stationary",
        now_s=100.0,
    )
    if solution.validity is RangeValidity.INVALID:
        truth_latitude = 1.3
        truth_longitude = 103.80055
    else:
        geolocation = target_geolocation_from_ned_offset(
            aircraft_latitude_deg=1.3,
            aircraft_longitude_deg=103.8005,
            north_offset_m=solution.north_offset_m or 0.0,
            east_offset_m=solution.east_offset_m or 0.0,
            aircraft_horizontal_sigma_m=0.05,
            ground_range_ci95_m=solution.ground_range_ci95_m,
            ground_range_m=solution.ground_range_m,
            bearing_sigma_deg=solution.bearing_sigma_deg,
            aircraft_observed_at_s=100.0,
            target_captured_at_s=100.0,
            aircraft_velocity_north_mps=0.0,
            aircraft_velocity_east_mps=0.0,
            aircraft_velocity_observed_at_s=100.0,
        )
        truth_latitude = geolocation.latitude_deg
        truth_longitude = geolocation.longitude_deg

    scenario_root = root / "configs/scenarios/validation"
    runtime_path = scenario_root / "fixture.runtime.json"
    expectations_path = scenario_root / "fixture.expectations.json"
    truth_path = scenario_root / "fixture.truth.json"
    runtime = {"fixture": "runtime-channel-is-never-read-by-this-evaluator"}
    expectations = {"fixture": "expectation-channel-is-never-read-by-this-evaluator"}
    truth = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "scenario_version": 1,
        "targets": [
            {
                "target_id": "fixture-car-truth",
                "label": "car",
                "position": {
                    "latitude_deg": truth_latitude,
                    "longitude_deg": truth_longitude,
                    "altitude_m": 25.0,
                    "altitude_reference": "MSL",
                },
                "behavior": "stationary_fixture",
            }
        ],
    }
    _write_json(runtime_path, runtime)
    _write_json(expectations_path, expectations)
    _write_json(truth_path, truth)
    scenario = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "scenario_version": 1,
        "channels": {
            "runtime_input": {
                "path": runtime_path.name,
                "sha256": canonical_sha256(runtime),
            },
            "truth": {"path": truth_path.name, "sha256": canonical_sha256(truth)},
            "expectations": {
                "path": expectations_path.name,
                "sha256": canonical_sha256(expectations),
            },
        },
    }
    scenario_path = scenario_root / "fixture.scenario.json"
    _write_json(scenario_path, scenario)

    assets = resolve_repository_model_asset_bundle(
        root / "simulation/gazebo/models", repository_root=root
    ).portable_mapping()
    model_assets = {
        **assets,
        "active_model_name": "fixture",
        "active_model_file_count": 1,
    }
    frame_directory = root / "artifacts/evaluation/fixture-frames"
    frame_directory.mkdir(parents=True, exist_ok=True)
    frame_rows: list[dict[str, Any]] = []
    previous_stamp: int | None = None
    for sequence in range(2):
        payload = bytes([sequence + 1]) * (64 * 48 * 3)
        stamp_ns = 1_000_000_000 + sequence * 50_000_000
        packet = build_wire_frame(
            sequence=sequence,
            sensor_stamp_ns=stamp_ns,
            width=64,
            height=48,
            camera_info_stamp_ns=stamp_ns,
            navsat_stamp_ns=stamp_ns,
            odometry_stamp_ns=stamp_ns,
            latitude_deg=1.3,
            longitude_deg=103.8005,
            altitude_m=25.0 + altitude_agl_m,
            position_east_m=0.0,
            position_north_m=0.0,
            position_up_m=altitude_agl_m,
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
        wire = read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=sequence,
            expected_width=64,
            expected_height=48,
            previous_sensor_stamp_ns=previous_stamp,
        )
        previous_stamp = wire.sensor_stamp_ns
        frame_name = f"frame-{sequence:06d}.bgr24"
        frame_path = frame_directory / frame_name
        frame_path.write_bytes(payload)
        captured_at_s = 100.0 + sequence * 0.05
        roll, pitch, heading, velocity_north, velocity_east, _ = gazebo_sim_ned_frd_kinematics(wire)
        frame_telemetry = {
            "latitude_deg": wire.latitude_deg,
            "longitude_deg": wire.longitude_deg,
            "gps_absolute_altitude_m": wire.altitude_m,
            "altitude_agl_m": altitude_agl_m,
            "altitude_reference": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
            "local_north_m": wire.position_north_m,
            "local_east_m": wire.position_east_m,
            "local_down_m": -wire.position_up_m,
            "roll_deg": roll,
            "pitch_deg": pitch,
            "heading_deg": heading,
            "velocity_north_mps": velocity_north,
            "velocity_east_mps": velocity_east,
            "gps_fix_type": 3,
            "satellites_visible": 12,
            "gps_horizontal_accuracy_m": 0.05,
            "gps_vertical_accuracy_m": 0.05,
            "gps_observed_at_s": captured_at_s,
            "attitude_observed_at_s": captured_at_s,
            "velocity_observed_at_s": captured_at_s,
            "altitude_observed_at_s": captured_at_s,
        }
        provenance = {
            "frame_id": f"fixture-frame-{sequence:06d}",
            "sequence": sequence,
            "captured_at_s": captured_at_s,
            "captured_at_clock": "host_monotonic",
            "sensor_stamp_ns": stamp_ns,
            "camera_info_stamp_ns": stamp_ns,
            "navsat_stamp_ns": stamp_ns,
            "odometry_stamp_ns": stamp_ns,
            "camera_info_source_skew_ns": 0,
            "navsat_source_skew_ns": 0,
            "odometry_source_skew_ns": 0,
            "record_sha256": wire.record_sha256,
            "payload_sha256": sha256_file(frame_path),
            "width": 64,
            "height": 48,
            "stride": 64 * 3,
            "payload_bytes": len(payload),
            "flags": wire.flags,
            "pixel_format": "BGR24",
            "wire_unsigned_header_hex": wire.unsigned_header.hex(),
            "wire_telemetry": GazeboSimWireTelemetryProvenance.from_wire(wire).to_mapping(),
            "frame_telemetry": frame_telemetry,
        }
        frame_rows.append(
            {
                "file": frame_name,
                "frame_id": provenance["frame_id"],
                "sequence": sequence,
                "bytes": len(payload),
                "sha256": sha256_file(frame_path),
                "wire_payload_sha256": sha256_file(frame_path),
                "wire_record_sha256": wire.record_sha256,
                "raw_bytes_sha256_matches_wire_payload": True,
                "provenance": provenance,
            }
        )
    calibration_provenance = {
        "schema_version": 1,
        "calibration_id": calibration.calibration_id,
        "repository_path": calibration_path.relative_to(root).as_posix(),
        "file_sha256": sha256_file(calibration_path),
        "calibration": asdict(calibration),
        "source": "repository_calibration_hash_bound",
        "camera_info_intrinsics_semantic_validation": "pending_k_p_distortion_validation",
    }
    dem_provenance = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "repository_path": dem_path.relative_to(root).as_posix(),
        "file_sha256": sha256_file(dem_path),
        "coordinate_reference": "WGS84",
        "vertical_datum": "MSL",
        "altitude_reference": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
        "odometry_local_up_used_as_agl": False,
        "origin": dem_document["origin"],
        "grid": {
            "north_origin_m": -100.0,
            "east_origin_m": -100.0,
            "north_spacing_m": 100.0,
            "east_spacing_m": 100.0,
            "rows": 3,
            "columns": 3,
            "vertical_sigma_m": 0.02,
            "slope_sigma_m_per_m": 0.0001,
        },
    }
    frame_set = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "generated_at_utc": "2026-09-05T00:00:00Z",
        "scenario_id": scenario_id,
        "scenario_manifest_hash": canonical_sha256(scenario),
        "sensor_binding_hash": "a" * 64,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": len(frame_rows),
        "total_payload_bytes": sum(item["bytes"] for item in frame_rows),
        "frames": frame_rows,
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "environment": "software_simulation",
            "wire": "MDGZSIM3",
            "source_row_order": "top_to_bottom",
            "wire_row_order": "top_to_bottom",
            "vertical_flip_applied": False,
            "repository_model_assets_sha256": assets["assets_sha256"],
            "repository_model_asset_file_count": assets["file_count"],
            "truth_loaded_by_runtime": False,
            "per_frame_wire_telemetry_persisted": True,
            "wire_record_recomputed_from_persisted_bytes": True,
            "camera_intrinsics_source": "repository_calibration_hash_bound",
            "camera_info_intrinsics_semantic_validation": "pending_k_p_distortion_validation",
            "terrain_source": "repository_dem_hash_bound",
        },
        "repository_model_assets": model_assets,
        "claims": {
            "actual_gazebo_pixels_persisted": True,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
            "detector_evaluation": "not_claimed",
            "production_model_approval": "not_claimed",
            "per_frame_sensor_geolocation_input": "passed",
            "target_geolocation_evaluation": "not_claimed",
        },
        "camera_calibration": calibration_provenance,
        "terrain_dem": dem_provenance,
        "per_frame_sensor_evidence": {
            "schema_version": 1,
            "record_count": len(frame_rows),
            "wire_header_encoding": "lowercase_hex_zero_digest_field",
            "wire_record_hash_algorithm": "sha256(zero_digest_header || raw_bgr24)",
            "source_association": "nearest_source_at_or_before_image",
            "maximum_source_skew_ns": 1_500_000_000,
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
            "derived_ned_values_recomputed_before_seal": True,
            "agl_source": GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
        },
    }
    frame_set["manifest_sha256"] = canonical_sha256(frame_set)
    frame_set_path = frame_directory / "gazebo-bgr24-frame-set.manifest.json"
    _write_json(frame_set_path, frame_set)
    return _Case(
        root=root,
        frame_set=frame_set_path,
        scenario=scenario_path,
        model=model_path,
        model_manifest=model_manifest_path,
        detection=detection,
        frame_count=len(frame_rows),
    )


def _config(case: _Case) -> GazeboTargetGeolocationConfig:
    return GazeboTargetGeolocationConfig(
        repository_root=case.root,
        frame_set_manifest_path=case.frame_set,
        scenario_manifest_path=case.scenario,
        model_path=case.model,
        model_manifest_path=case.model_manifest,
        providers=("FixtureExecutionProvider",),
        maximum_horizontal_error_m=0.5,
        maximum_multiframe_stability_m=0.1,
        minimum_one_sigma_coverage=1.0,
        minimum_range_ci95_coverage=1.0,
    )


def test_two_frame_camera_to_wgs84_gate_is_truth_isolated(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    detector = _FakeDetector(case.detection)
    factory_calls = 0

    def factory(_config: Any) -> _FakeDetector:
        nonlocal factory_calls
        factory_calls += 1
        return detector

    def truth_loader(path: Path, reference: Any) -> dict[str, Any]:
        assert factory_calls == 1
        assert detector.calls == case.frame_count
        return load_verified_channel(path, reference, name="truth")

    document = evaluate_gazebo_target_geolocation(
        _config(case),
        detector_factory=factory,
        truth_loader=truth_loader,
    )

    assert document["gate_passed"] is True
    assert document["schema_version"] == 2
    assert factory_calls == 1
    assert detector.calls == case.frame_count
    assert document["truth_isolation"]["truth_channel_loaded_after_all_predictions"] is True
    assert document["execution_provider"]["shared_session_across_all_frames"] is True
    assert document["scoring"]["geolocated_frame_count"] == case.frame_count
    assert document["scoring"]["maximum_horizontal_error_m"] == pytest.approx(0.0)
    assert document["scoring"]["one_sigma_coverage"] == pytest.approx(1.0)
    assert document["scoring"]["range_ci95_coverage"] == pytest.approx(1.0)
    assert document["claims"]["environment"] == "software_simulation"
    assert document["claims"]["hardware"] == "not_claimed"
    assert document["model_identity"]["production_approved"] is False
    assert document["evaluator_identity"]["module_path"] == EVALUATOR_SOURCE_PATH
    assert document["regime"]["vehicle_altitude_agl_m"]["mean"] == pytest.approx(10.0)
    assert document["regime"]["truth_ground_range_m"]["mean"] > 0.0
    assert document["regime"]["terrain_at_truth"]["slope_deg"] == pytest.approx(0.0)
    evidence_hash = document.pop("evidence_sha256")
    assert canonical_sha256(document) == evidence_hash


def test_evidence_verifier_reopens_inputs_and_reproduces_result(tmp_path: Path) -> None:
    case = _build_case(tmp_path)

    def factory(_config: Any) -> _FakeDetector:
        return _FakeDetector(case.detection)

    document = evaluate_gazebo_target_geolocation(_config(case), detector_factory=factory)
    verification = verify_gazebo_target_geolocation_evidence(
        document,
        repository_root=case.root,
        detector_factory=factory,
    )

    assert verification["passed"] is True
    assert verification["reproduced"] is True
    assert verification["reasons"] == []
    assert verification["evidence_sha256"] == document["evidence_sha256"]
    assert verification["reproduced_result_sha256"] == document["deterministic_result_sha256"]


def test_evidence_verifier_fails_closed_after_bound_core_change(tmp_path: Path) -> None:
    case = _build_case(tmp_path)

    def factory(_config: Any) -> _FakeDetector:
        return _FakeDetector(case.detection)

    document = evaluate_gazebo_target_geolocation(_config(case), detector_factory=factory)
    bound_core_source = case.root / DEFAULT_FILE_LIST["core_algorithm"][0]
    bound_core_source.write_text(
        bound_core_source.read_text(encoding="utf-8") + "changed\n",
        encoding="utf-8",
        newline="\n",
    )

    verification = verify_gazebo_target_geolocation_evidence(
        document,
        repository_root=case.root,
        detector_factory=factory,
    )

    assert verification["passed"] is False
    assert verification["reproduced"] is False
    assert (
        "evidence does not reproduce from the current bound inputs and shared contracts"
        in verification["reasons"]
    )
    assert (
        "release identity or shared parity does not match the current checkout"
        in verification["reasons"]
    )


def test_evidence_verifier_fails_closed_after_evaluator_source_change(tmp_path: Path) -> None:
    case = _build_case(tmp_path)

    def factory(_config: Any) -> _FakeDetector:
        return _FakeDetector(case.detection)

    document = evaluate_gazebo_target_geolocation(_config(case), detector_factory=factory)
    evaluator_source = case.root / EVALUATOR_SOURCE_PATH
    evaluator_source.write_text(
        evaluator_source.read_text(encoding="utf-8") + "# changed\n",
        encoding="utf-8",
        newline="\n",
    )

    verification = verify_gazebo_target_geolocation_evidence(
        document,
        repository_root=case.root,
        detector_factory=factory,
    )

    assert verification["passed"] is False
    assert verification["reproduced"] is False
    assert "evaluator source SHA-256 does not match the current checkout" in verification["reasons"]
    assert (
        "evidence does not reproduce from the current bound inputs and shared contracts"
        in verification["reasons"]
    )


def test_near_horizon_ground_anchor_fails_closed_without_fake_coordinate(
    tmp_path: Path,
) -> None:
    case = _build_case(
        tmp_path,
        altitude_agl_m=0.25,
        mount_pitch_down_deg=10.0,
        detection_bbox=BoundingBox(0.38, 0.20, 0.62, 0.36),
    )
    detector = _FakeDetector(case.detection)
    document = evaluate_gazebo_target_geolocation(
        _config(case), detector_factory=lambda _config: detector
    )

    assert document["gate_passed"] is False
    assert document["scoring"]["matching_detection_frame_count"] == case.frame_count
    assert document["scoring"]["geolocated_frame_count"] == 0
    assert all(frame["predicted_target"] is None for frame in document["frames"])
    assert {frame["range_solution"]["reasons"][0] for frame in document["frames"]} <= {
        "target_ray_does_not_intersect_ground_safely",
        "terrain_uncertainty_unbounded",
        "target_ray_uncertainty_crosses_horizon",
    }


def test_tampered_frame_is_rejected_before_detector_construction(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    first_frame = case.frame_set.parent / "frame-000000.bgr24"
    first_frame.write_bytes(first_frame.read_bytes()[:-1] + b"x")
    factory_calls = 0

    def factory(_config: Any) -> _FakeDetector:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeDetector(case.detection)

    with pytest.raises(GazeboTargetGeolocationInputError, match="payload identity mismatch"):
        evaluate_gazebo_target_geolocation(_config(case), detector_factory=factory)
    assert factory_calls == 0


def test_exclusive_writer_never_replaces_existing_evidence(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    output.write_text("original\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_evidence_exclusive(output, {"gate_passed": False})
    assert output.read_text(encoding="utf-8") == "original\n"


def test_manifest_tamper_is_rejected_before_detector_construction(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    raw = json.loads(case.frame_set.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(raw)
    tampered["source_attribution"]["truth_loaded_by_runtime"] = True
    tampered["manifest_sha256"] = canonical_sha256(
        {key: value for key, value in tampered.items() if key != "manifest_sha256"}
    )
    _write_json(case.frame_set, tampered)

    with pytest.raises(GazeboTargetGeolocationInputError, match="truth-isolation"):
        evaluate_gazebo_target_geolocation(
            _config(case), detector_factory=lambda _config: _FakeDetector(case.detection)
        )

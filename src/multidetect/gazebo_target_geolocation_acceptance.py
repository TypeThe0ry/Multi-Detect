"""Truth-isolated Gazebo camera-to-WGS84 acceptance evaluation.

This module closes the software-only evidence chain from actual persisted
Gazebo BGR24 pixels through one quarantined detector session, the production
range solver, and the production WGS-84 projection.  The scenario truth
channel is intentionally loaded only after every prediction has completed.

The resulting document is a narrow SIM algorithm result.  It never approves
the candidate model, renders QGC, controls an aircraft, or claims HIL/REAL.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    strict_json_loads,
)
from .backends.gazebo_sim import (
    GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
    GazeboSimWireTelemetryProvenance,
    gazebo_sim_ned_frd_kinematics,
    reconstruct_gazebo_sim_wire_frame,
)
from .domain import Detection
from .geolocation_accuracy import wgs84_distance_m
from .inference_provider import provider_selection_document
from .mavlink_gps import (
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_SATELLITES_MIN,
    MAVLINK_SATELLITES_MAX,
)
from .model_manifest import ModelManifestError, sha256_file, verify_model_manifest
from .multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    MultiModalRangingEngine,
    RangeSolution,
    RangeValidity,
    TargetImageObservation,
    load_camera_calibration,
)
from .release_parity import ReleaseParityError, build_release_parity_document
from .simulation.index import load_scenario_index, load_verified_channel
from .simulation.truth import ScenarioTruth
from .simulator.stack import SimulatorStackError, resolve_repository_model_asset_bundle
from .target_geolocation import TargetGeolocation, target_geolocation_from_ned_offset
from .target_image_anchor import TargetImageAnchor, target_image_anchor
from .terrain_dem import (
    LocalDemGrid,
    TerrainDemError,
    dem_gps_terrain_observation,
    load_local_dem_grid,
)
from .vision import OnnxRawYoloConfig, OnnxRawYoloDetector

EVIDENCE_SCHEMA = "multidetect.gazebo-target-geolocation-acceptance"
EVIDENCE_SCHEMA_VERSION = 2
FRAME_SET_SCHEMA = "multidetect.gazebo-bgr24-frame-set"
FRAME_SET_SCHEMA_VERSION = 2
RAW_YOLO_NATIVE_OUTPUT = "ultralytics_raw_xywh_class_scores"
MAX_SOURCE_SKEW_NS = 1_500_000_000
EVALUATOR_SOURCE_PATH = "src/multidetect/gazebo_target_geolocation_acceptance.py"


class GazeboTargetGeolocationInputError(ValueError):
    """Raised when immutable evaluation inputs fail their contracts."""


class _Detector(Protocol):
    provider_names: tuple[str, ...]
    provider_selection: Any

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]: ...


DetectorFactory = Callable[[OnnxRawYoloConfig], _Detector]
TruthLoader = Callable[[Path, Any], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class GazeboTargetGeolocationConfig:
    repository_root: Path
    frame_set_manifest_path: Path
    scenario_manifest_path: Path
    model_path: Path
    model_manifest_path: Path
    expected_label: str = "car"
    confidence_threshold: float = 0.25
    nms_iou_threshold: float = 0.45
    maximum_detections: int = 300
    providers: tuple[str, ...] = ("CPUExecutionProvider",)
    minimum_anchor_sigma_px: float = 2.0
    roll_sigma_deg: float = 0.3
    pitch_sigma_deg: float = 0.3
    heading_sigma_deg: float = 1.0
    maximum_horizontal_error_m: float = 8.0
    maximum_multiframe_stability_m: float = 2.0
    minimum_one_sigma_coverage: float = 0.60
    minimum_range_ci95_coverage: float = 0.80

    def __post_init__(self) -> None:
        label = self.expected_label.strip().lower()
        if not label:
            raise GazeboTargetGeolocationInputError("expected_label cannot be empty")
        object.__setattr__(self, "expected_label", label)
        for name in ("confidence_threshold", "nms_iou_threshold"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise GazeboTargetGeolocationInputError(f"{name} must be in [0, 1]")
        for name in (
            "minimum_anchor_sigma_px",
            "maximum_horizontal_error_m",
            "maximum_multiframe_stability_m",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise GazeboTargetGeolocationInputError(f"{name} must be positive")
        for name in ("roll_sigma_deg", "pitch_sigma_deg", "heading_sigma_deg"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise GazeboTargetGeolocationInputError(f"{name} must be finite and non-negative")
        for name in ("minimum_one_sigma_coverage", "minimum_range_ci95_coverage"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise GazeboTargetGeolocationInputError(f"{name} must be in [0, 1]")
        if (
            isinstance(self.maximum_detections, bool)
            or not isinstance(self.maximum_detections, int)
            or self.maximum_detections <= 0
        ):
            raise GazeboTargetGeolocationInputError("maximum_detections must be a positive integer")
        if not self.providers or any(
            not isinstance(item, str) or not item.strip() for item in self.providers
        ):
            raise GazeboTargetGeolocationInputError(
                "providers must contain at least one non-empty provider name"
            )


@dataclass(frozen=True, slots=True)
class _FrameTelemetry:
    latitude_deg: float
    longitude_deg: float
    gps_absolute_altitude_m: float
    altitude_agl_m: float
    local_north_m: float
    local_east_m: float
    local_down_m: float
    roll_deg: float
    pitch_deg: float
    heading_deg: float
    velocity_north_mps: float
    velocity_east_mps: float
    gps_fix_type: int
    satellites_visible: int
    gps_horizontal_accuracy_m: float
    gps_vertical_accuracy_m: float
    gps_observed_at_s: float
    attitude_observed_at_s: float
    velocity_observed_at_s: float
    altitude_observed_at_s: float


@dataclass(frozen=True, slots=True)
class _VerifiedFrame:
    frame_id: str
    sequence: int
    path: Path
    payload: bytes
    payload_sha256: str
    wire_record_sha256: str
    width: int
    height: int
    captured_at_s: float
    sensor_stamp_ns: int
    navsat_stamp_ns: int
    odometry_stamp_ns: int
    telemetry: _FrameTelemetry


@dataclass(frozen=True, slots=True)
class _VerifiedFrameSet:
    path: Path
    file_sha256: str
    manifest_sha256: str
    scenario_id: str
    scenario_manifest_hash: str
    sensor_binding_hash: str
    camera_calibration_path: Path
    camera_calibration_sha256: str
    calibration: CameraCalibration
    terrain_dem_path: Path
    terrain_dem_sha256: str
    terrain_dem: LocalDemGrid
    model_assets_sha256: str
    frames: tuple[_VerifiedFrame, ...]


@dataclass(frozen=True, slots=True)
class _Prediction:
    frame: _VerifiedFrame
    detections: tuple[Detection, ...]
    matching_detections: tuple[Detection, ...]
    anchor: TargetImageAnchor | None
    range_solution: RangeSolution | None
    geolocation: TargetGeolocation | None
    processing_error: str | None


def evaluate_gazebo_target_geolocation(
    config: GazeboTargetGeolocationConfig,
    *,
    detector_factory: DetectorFactory = OnnxRawYoloDetector,
    ranging_engine: MultiModalRangingEngine | None = None,
    truth_loader: TruthLoader | None = None,
) -> dict[str, Any]:
    """Evaluate every frame before opening the independent scenario truth channel."""

    try:
        repository_root = _repository_root(config.repository_root)
        evaluator_source_path = _repository_path(
            repository_root,
            EVALUATOR_SOURCE_PATH,
            "geolocation evaluator source",
        )
        evaluator_source_sha256 = sha256_file(evaluator_source_path)
        release_parity = build_release_parity_document(repository_root)
        frame_set_path = _regular_file(config.frame_set_manifest_path, "frame-set manifest")
        scenario_path = _regular_file(config.scenario_manifest_path, "scenario manifest")
        model_path = _regular_file(config.model_path, "model artifact")
        model_manifest_path = _regular_file(config.model_manifest_path, "model manifest")

        # This index contains only channel identities and hashes.  The truth
        # channel itself stays unopened until all predictions below are done.
        resolved_scenario_path, scenario_index = load_scenario_index(scenario_path)
        frame_set = _verify_frame_set(
            frame_set_path,
            repository_root=repository_root,
            scenario_id=scenario_index.scenario_id,
            scenario_manifest_hash=scenario_index.canonical_hash,
        )
        model_manifest = _load_json_object(model_manifest_path, "model manifest")
        verified_model = verify_model_manifest(
            model_manifest_path,
            model_path,
            expected_model_role="safety_object_evidence",
            expected_output_format="N_x_6",
            expected_native_output_format=RAW_YOLO_NATIVE_OUTPUT,
            require_production_approved=False,
        )
        if verified_model.status != "quarantined" or verified_model.production_approved:
            raise GazeboTargetGeolocationInputError(
                "geolocation acceptance requires an explicitly quarantined candidate model"
            )
        if config.expected_label not in verified_model.class_names:
            raise GazeboTargetGeolocationInputError(
                f"expected label {config.expected_label!r} is absent from the model manifest"
            )
        input_shape = _model_input_shape(model_manifest)
    except GazeboTargetGeolocationInputError:
        raise
    except (
        AutonomyContractError,
        ModelManifestError,
        OSError,
        ReleaseParityError,
        SimulatorStackError,
        TerrainDemError,
        TypeError,
        ValueError,
    ) as exc:
        raise GazeboTargetGeolocationInputError(str(exc)) from exc

    detector_config = OnnxRawYoloConfig(
        model_path=model_path,
        class_names=verified_model.class_names,
        input_width=input_shape[3],
        input_height=input_shape[2],
        confidence_threshold=float(config.confidence_threshold),
        iou_threshold=float(config.nms_iou_threshold),
        maximum_detections=config.maximum_detections,
        providers=tuple(item.strip() for item in config.providers),
        model_version=verified_model.model_version,
    )
    detector = detector_factory(detector_config)
    engine = ranging_engine or MultiModalRangingEngine()
    np = _require_numpy()
    predictions: list[_Prediction] = []
    for frame in frame_set.frames:
        image_bgr = np.frombuffer(frame.payload, dtype=np.uint8).reshape(
            (frame.height, frame.width, 3)
        )
        detections = tuple(detector.detect(image_bgr))
        matching = tuple(item for item in detections if item.label == config.expected_label)
        anchor: TargetImageAnchor | None = None
        solution: RangeSolution | None = None
        geolocation: TargetGeolocation | None = None
        processing_error: str | None = None
        if len(matching) == 1:
            try:
                detection = matching[0]
                anchor = target_image_anchor(
                    label=detection.label,
                    bbox=detection.bbox,
                    image_width_px=frame.width,
                    image_height_px=frame.height,
                    minimum_sigma_px=float(config.minimum_anchor_sigma_px),
                )
                telemetry = frame.telemetry
                target = TargetImageObservation(
                    target_id=f"{config.expected_label}-{frame.frame_id}",
                    frame_id=frame.frame_id,
                    captured_at_s=frame.captured_at_s,
                    center_x=anchor.x,
                    center_y=anchor.y,
                    center_sigma_px=anchor.sigma_px,
                )
                pose = AircraftPose(
                    captured_at_s=telemetry.attitude_observed_at_s,
                    roll_deg=telemetry.roll_deg,
                    pitch_deg=telemetry.pitch_deg,
                    heading_deg=telemetry.heading_deg % 360.0,
                    roll_sigma_deg=float(config.roll_sigma_deg),
                    pitch_sigma_deg=float(config.pitch_sigma_deg),
                    heading_sigma_deg=float(config.heading_sigma_deg),
                )
                terrain = dem_gps_terrain_observation(
                    frame_set.terrain_dem,
                    latitude_deg=telemetry.latitude_deg,
                    longitude_deg=telemetry.longitude_deg,
                    gps_absolute_altitude_m=telemetry.gps_absolute_altitude_m,
                    gps_horizontal_sigma_m=telemetry.gps_horizontal_accuracy_m,
                    gps_vertical_sigma_m=telemetry.gps_vertical_accuracy_m,
                    captured_at_s=telemetry.gps_observed_at_s,
                )
                now_s = max(
                    frame.captured_at_s,
                    telemetry.gps_observed_at_s,
                    telemetry.attitude_observed_at_s,
                )
                solution = engine.solve(
                    calibration=frame_set.calibration,
                    pose=pose,
                    target=target,
                    vertical_measurements=(),
                    terrain_observation=terrain,
                    direct_measurements=(),
                    fusion_profile="outdoor-multimodal-v1",
                    vehicle_profile="multirotor",
                    navigation_state="gps-aided",
                    motion_regime="stationary",
                    now_s=now_s,
                )
                if solution.validity is not RangeValidity.INVALID:
                    if solution.north_offset_m is None or solution.east_offset_m is None:
                        raise RuntimeError("qualified range solution omitted its NED target offset")
                    geolocation = target_geolocation_from_ned_offset(
                        aircraft_latitude_deg=telemetry.latitude_deg,
                        aircraft_longitude_deg=telemetry.longitude_deg,
                        north_offset_m=solution.north_offset_m,
                        east_offset_m=solution.east_offset_m,
                        aircraft_horizontal_sigma_m=(telemetry.gps_horizontal_accuracy_m),
                        ground_range_ci95_m=solution.ground_range_ci95_m,
                        ground_range_m=solution.ground_range_m,
                        bearing_sigma_deg=solution.bearing_sigma_deg,
                        aircraft_observed_at_s=telemetry.gps_observed_at_s,
                        target_captured_at_s=frame.captured_at_s,
                        aircraft_velocity_north_mps=telemetry.velocity_north_mps,
                        aircraft_velocity_east_mps=telemetry.velocity_east_mps,
                        aircraft_velocity_observed_at_s=(telemetry.velocity_observed_at_s),
                    )
            except (RuntimeError, TerrainDemError, TypeError, ValueError) as exc:
                processing_error = f"{type(exc).__name__}: {exc}"
        predictions.append(
            _Prediction(
                frame=frame,
                detections=detections,
                matching_detections=matching,
                anchor=anchor,
                range_solution=solution,
                geolocation=geolocation,
                processing_error=processing_error,
            )
        )

    # All detector and geolocation calls are complete at this point.  Only the
    # evaluator receives the independent truth mapping.
    try:
        if truth_loader is None:
            truth_raw = load_verified_channel(
                resolved_scenario_path, scenario_index.truth, name="truth"
            )
        else:
            truth_raw = truth_loader(resolved_scenario_path, scenario_index.truth)
        if canonical_sha256(dict(truth_raw)) != scenario_index.truth.sha256:
            raise GazeboTargetGeolocationInputError(
                "truth channel canonical hash does not match the scenario manifest"
            )
        truth = ScenarioTruth.from_mapping(truth_raw)
        if (
            truth.scenario_id != scenario_index.scenario_id
            or truth.scenario_version != scenario_index.scenario_version
        ):
            raise GazeboTargetGeolocationInputError(
                "truth channel identity does not match the scenario manifest"
            )
        truth_targets = tuple(
            item for item in truth.targets if item.label.strip().lower() == config.expected_label
        )
        if len(truth_targets) != 1:
            raise GazeboTargetGeolocationInputError(
                "truth channel must contain exactly one target matching expected_label"
            )
        truth_target = truth_targets[0]
    except GazeboTargetGeolocationInputError:
        raise
    except (AutonomyContractError, OSError, TypeError, ValueError) as exc:
        raise GazeboTargetGeolocationInputError(str(exc)) from exc

    scored_frames = [
        _score_prediction(
            item, truth_target.position.latitude_deg, truth_target.position.longitude_deg
        )
        for item in predictions
    ]
    geolocated_scores = [item for item in scored_frames if item["horizontal_error_m"] is not None]
    detection_gate = all(len(item.matching_detections) == 1 for item in predictions)
    geolocation_gate = len(geolocated_scores) == len(predictions)
    errors = [float(item["horizontal_error_m"]) for item in geolocated_scores]
    one_sigma_values = [bool(item["inside_one_sigma"]) for item in geolocated_scores]
    ci95_values = [
        bool(item["truth_ground_range_inside_ci95"])
        for item in geolocated_scores
        if item["truth_ground_range_inside_ci95"] is not None
    ]
    stability_m = _maximum_pairwise_stability_m(predictions)
    maximum_error_m = max(errors) if errors else None
    mean_error_m = sum(errors) / len(errors) if errors else None
    one_sigma_coverage = sum(one_sigma_values) / len(one_sigma_values) if one_sigma_values else None
    ci95_coverage = sum(ci95_values) / len(ci95_values) if ci95_values else None
    accuracy_gate = maximum_error_m is not None and maximum_error_m <= float(
        config.maximum_horizontal_error_m
    )
    stability_gate = stability_m is not None and stability_m <= float(
        config.maximum_multiframe_stability_m
    )
    one_sigma_gate = one_sigma_coverage is not None and one_sigma_coverage >= float(
        config.minimum_one_sigma_coverage
    )
    ci95_gate = ci95_coverage is not None and ci95_coverage >= float(
        config.minimum_range_ci95_coverage
    )
    gate_passed = all(
        (
            detection_gate,
            geolocation_gate,
            accuracy_gate,
            stability_gate,
            one_sigma_gate,
            ci95_gate,
        )
    )
    truth_surface = frame_set.terrain_dem.sample_wgs84(
        truth_target.position.latitude_deg,
        truth_target.position.longitude_deg,
    )
    terrain_slope_m_per_m = math.hypot(
        truth_surface.north_slope_m_per_m,
        truth_surface.east_slope_m_per_m,
    )

    provider_names = tuple(str(item) for item in getattr(detector, "provider_names", ()))
    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "gazebo_actual_pixels_per_frame_camera_to_wgs84_software_acceptance",
        "evaluator_identity": {
            "module_path": EVALUATOR_SOURCE_PATH,
            "module_sha256": evaluator_source_sha256,
        },
        "release_identity": {
            "product": release_parity["product"],
            "release_label": release_parity["release_label"],
            "runtime_mode": "sim",
            "source_files_canonical_sha256": canonical_sha256(release_parity["source_files"]),
            "parity": release_parity["parity"],
        },
        "scenario": {
            "scenario_id": scenario_index.scenario_id,
            "scenario_version": scenario_index.scenario_version,
            "manifest_path": _repository_display_path(resolved_scenario_path, repository_root),
            "manifest_canonical_sha256": scenario_index.canonical_hash,
            "truth_channel_path": scenario_index.truth.path,
            "truth_channel_canonical_sha256": scenario_index.truth.sha256,
        },
        "frame_set_identity": {
            "manifest_path": _repository_display_path(frame_set.path, repository_root),
            "manifest_file_sha256": frame_set.file_sha256,
            "manifest_canonical_sha256": frame_set.manifest_sha256,
            "scenario_manifest_sha256_bound": frame_set.scenario_manifest_hash,
            "sensor_binding_sha256": frame_set.sensor_binding_hash,
            "frame_count": len(frame_set.frames),
            "all_frames_verified_before_inference": True,
            "wire_version": 3,
            "wire": "MDGZSIM3",
            "pixel_format": "BGR24",
            "actual_gazebo_pixels": True,
            "per_frame_wire_record_reconstructed": True,
            "per_frame_gps_pose_agl_verified": True,
            "camera_calibration": {
                "calibration_id": frame_set.calibration.calibration_id,
                "path": _repository_display_path(
                    frame_set.camera_calibration_path, repository_root
                ),
                "sha256": frame_set.camera_calibration_sha256,
            },
            "terrain_dem": {
                "scenario_id": frame_set.terrain_dem.scenario_id,
                "path": _repository_display_path(frame_set.terrain_dem_path, repository_root),
                "sha256": frame_set.terrain_dem_sha256,
            },
            "repository_model_assets_sha256": frame_set.model_assets_sha256,
        },
        "model_identity": {
            "candidate": True,
            "quarantine_enforced": True,
            "status": verified_model.status,
            "model_id": verified_model.model_id,
            "model_version": verified_model.model_version,
            "model_role": verified_model.model_role,
            "native_output_format": RAW_YOLO_NATIVE_OUTPUT,
            "artifact_path": _repository_display_path(model_path, repository_root),
            "artifact_sha256": verified_model.artifact_sha256,
            "manifest_path": _repository_display_path(model_manifest_path, repository_root),
            "manifest_file_sha256": sha256_file(model_manifest_path),
            "production_approved": False,
            "accuracy_validated_for_production": False,
        },
        "truth_isolation": {
            "verified": True,
            "scenario_index_loaded_before_inference": True,
            "truth_channel_loaded_after_all_predictions": True,
            "prediction_count_before_truth_load": len(predictions),
            "truth_supplied_to_detector": False,
            "truth_supplied_to_ranging_engine": False,
            "truth_used_only_for_post_prediction_scoring": True,
            "runtime_truth_loaded": False,
            "truth_target_id": truth_target.target_id,
            "truth_target_label": truth_target.label,
            "truth_altitude_excluded_from_horizontal_score": True,
        },
        "execution_provider": {
            "requested": list(config.providers),
            "selected": list(provider_names),
            "selection": provider_selection_document(getattr(detector, "provider_selection", None)),
            "detector_instances": 1,
            "shared_session_across_all_frames": True,
            "inference_count": len(predictions),
        },
        "thresholds": {
            "detector_confidence": float(config.confidence_threshold),
            "nms_iou": float(config.nms_iou_threshold),
            "maximum_detections": config.maximum_detections,
            "expected_label": config.expected_label,
            "required_matching_detections_per_frame": 1,
            "minimum_anchor_sigma_px": float(config.minimum_anchor_sigma_px),
            "maximum_horizontal_error_m": float(config.maximum_horizontal_error_m),
            "maximum_multiframe_stability_m": float(config.maximum_multiframe_stability_m),
            "minimum_one_sigma_coverage": float(config.minimum_one_sigma_coverage),
            "minimum_range_ci95_coverage": float(config.minimum_range_ci95_coverage),
        },
        "frames": scored_frames,
        "regime": {
            "vehicle_altitude_agl_m": _metric_summary(
                [item.frame.telemetry.altitude_agl_m for item in predictions]
            ),
            "truth_ground_range_m": _metric_summary(
                [float(item["truth_ground_range_m"]) for item in scored_frames]
            ),
            "terrain_at_truth": {
                "elevation_msl_m": truth_surface.elevation_m,
                "north_slope_m_per_m": truth_surface.north_slope_m_per_m,
                "east_slope_m_per_m": truth_surface.east_slope_m_per_m,
                "slope_m_per_m": terrain_slope_m_per_m,
                "slope_deg": math.degrees(math.atan(terrain_slope_m_per_m)),
            },
        },
        "scoring": {
            "frame_count": len(predictions),
            "matching_detection_frame_count": sum(
                len(item.matching_detections) == 1 for item in predictions
            ),
            "geolocated_frame_count": len(geolocated_scores),
            "maximum_horizontal_error_m": maximum_error_m,
            "mean_horizontal_error_m": mean_error_m,
            "one_sigma_coverage": one_sigma_coverage,
            "range_ci95_coverage": ci95_coverage,
            "maximum_pairwise_prediction_distance_m": stability_m,
            "gates": {
                "exactly_one_expected_detection_per_frame": detection_gate,
                "all_frames_geolocated": geolocation_gate,
                "horizontal_accuracy": accuracy_gate,
                "one_sigma_coverage": one_sigma_gate,
                "range_ci95_coverage": ci95_gate,
                "multiframe_stability": stability_gate,
            },
        },
        "claims": {
            "environment": "software_simulation",
            "sim_algorithm_acceptance": gate_passed,
            "production_model_approval": "not_claimed",
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
            "outdoor_gnss": "not_claimed",
            "hil": "not_claimed",
            "real": "not_claimed",
            "flight": "not_claimed",
        },
        "production_approved": False,
        "qgc_visual_acceptance": "not_claimed",
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": gate_passed,
    }
    document["deterministic_result_sha256"] = _deterministic_result_sha256(document)
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def verify_gazebo_target_geolocation_evidence(
    evidence: Mapping[str, Any] | str | Path,
    *,
    repository_root: str | Path,
    detector_factory: DetectorFactory = OnnxRawYoloDetector,
    require_gate_passed: bool = True,
) -> dict[str, Any]:
    """Reopen every bound input and reproduce one acceptance document.

    Verification is intentionally stronger than checking the embedded digest:
    it resolves the scenario, frame-set, model and manifest beneath the current
    checkout, recomputes current release parity, runs inference/ranging/WGS-84
    again, and compares the timestamp-independent result hash.  This also makes
    an evidence file stale as soon as any bound shared contract changes.
    """

    reasons: list[str] = []
    try:
        root = _repository_root(Path(repository_root))
        if isinstance(evidence, (str, Path)):
            evidence_path = _regular_file(Path(evidence), "geolocation evidence")
            document: Mapping[str, Any] = _load_json_object(evidence_path, "geolocation evidence")
        elif isinstance(evidence, Mapping):
            document = evidence
        else:
            raise GazeboTargetGeolocationInputError(
                "geolocation evidence must be an object or a JSON file path"
            )

        if document.get("schema") != EVIDENCE_SCHEMA:
            reasons.append("evidence schema does not match the verifier contract")
        if (
            type(document.get("schema_version")) is not int
            or document.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        ):
            reasons.append("evidence schema_version does not match the verifier contract")
        if document.get("scope") != (
            "gazebo_actual_pixels_per_frame_camera_to_wgs84_software_acceptance"
        ):
            reasons.append("evidence scope does not match the verifier contract")

        evaluator_identity = _mapping(document.get("evaluator_identity"), "evaluator_identity")
        if evaluator_identity.get("module_path") != EVALUATOR_SOURCE_PATH:
            reasons.append("evaluator module path does not match the verifier contract")
        evaluator_source = _repository_path(
            root,
            evaluator_identity.get("module_path"),
            "geolocation evaluator source",
        )
        claimed_evaluator_sha256 = _sha256_text(
            evaluator_identity.get("module_sha256"),
            "evaluator_identity.module_sha256",
        )
        if sha256_file(evaluator_source) != claimed_evaluator_sha256:
            reasons.append("evaluator source SHA-256 does not match the current checkout")

        evaluated_at = document.get("evaluated_at_utc")
        if not isinstance(evaluated_at, str) or not evaluated_at.endswith("Z"):
            reasons.append("evaluated_at_utc must be an ISO-8601 UTC timestamp")
        else:
            try:
                datetime.fromisoformat(evaluated_at.removesuffix("Z") + "+00:00")
            except ValueError:
                reasons.append("evaluated_at_utc must be an ISO-8601 UTC timestamp")

        claimed_evidence_hash = document.get("evidence_sha256")
        if not _is_sha256(claimed_evidence_hash):
            reasons.append("evidence_sha256 is missing or invalid")
        else:
            unhashed = dict(document)
            unhashed.pop("evidence_sha256", None)
            if canonical_sha256(unhashed) != claimed_evidence_hash:
                reasons.append("evidence_sha256 does not match the evidence document")

        claimed_result_hash = document.get("deterministic_result_sha256")
        if not _is_sha256(claimed_result_hash):
            reasons.append("deterministic_result_sha256 is missing or invalid")
        elif _deterministic_result_sha256(document) != claimed_result_hash:
            reasons.append(
                "deterministic_result_sha256 does not match the timestamp-independent document"
            )

        frame_set_identity = _mapping(document.get("frame_set_identity"), "frame_set_identity")
        scenario = _mapping(document.get("scenario"), "scenario")
        model_identity = _mapping(document.get("model_identity"), "model_identity")
        execution_provider = _mapping(document.get("execution_provider"), "execution_provider")
        thresholds = _mapping(document.get("thresholds"), "thresholds")
        requested_providers = execution_provider.get("requested")
        if not isinstance(requested_providers, list):
            raise GazeboTargetGeolocationInputError("execution_provider.requested must be an array")

        replay_config = GazeboTargetGeolocationConfig(
            repository_root=root,
            frame_set_manifest_path=_repository_path(
                root, frame_set_identity.get("manifest_path"), "frame-set manifest"
            ),
            scenario_manifest_path=_repository_path(
                root, scenario.get("manifest_path"), "scenario manifest"
            ),
            model_path=_repository_path(
                root, model_identity.get("artifact_path"), "model artifact"
            ),
            model_manifest_path=_repository_path(
                root, model_identity.get("manifest_path"), "model manifest"
            ),
            expected_label=thresholds.get("expected_label"),  # type: ignore[arg-type]
            confidence_threshold=thresholds.get("detector_confidence"),  # type: ignore[arg-type]
            nms_iou_threshold=thresholds.get("nms_iou"),  # type: ignore[arg-type]
            maximum_detections=thresholds.get("maximum_detections"),  # type: ignore[arg-type]
            providers=tuple(requested_providers),
            minimum_anchor_sigma_px=thresholds.get("minimum_anchor_sigma_px"),  # type: ignore[arg-type]
            maximum_horizontal_error_m=thresholds.get("maximum_horizontal_error_m"),  # type: ignore[arg-type]
            maximum_multiframe_stability_m=thresholds.get("maximum_multiframe_stability_m"),  # type: ignore[arg-type]
            minimum_one_sigma_coverage=thresholds.get("minimum_one_sigma_coverage"),  # type: ignore[arg-type]
            minimum_range_ci95_coverage=thresholds.get("minimum_range_ci95_coverage"),  # type: ignore[arg-type]
        )
        reproduced = evaluate_gazebo_target_geolocation(
            replay_config,
            detector_factory=detector_factory,
        )
        reproduced_result_hash = reproduced["deterministic_result_sha256"]
        if claimed_result_hash != reproduced_result_hash:
            reasons.append(
                "evidence does not reproduce from the current bound inputs and shared contracts"
            )
        if document.get("release_identity") != reproduced.get("release_identity"):
            reasons.append("release identity or shared parity does not match the current checkout")
        if document.get("gate_passed") is not reproduced.get("gate_passed"):
            reasons.append("gate_passed does not match deterministic recomputation")
        if require_gate_passed and document.get("gate_passed") is not True:
            reasons.append("evidence gate_passed is not true")
    except (
        AutonomyContractError,
        GazeboTargetGeolocationInputError,
        ModelManifestError,
        OSError,
        ReleaseParityError,
        SimulatorStackError,
        TerrainDemError,
        TypeError,
        ValueError,
    ) as exc:
        reasons.append(f"evidence could not be reproduced: {exc}")
        claimed_evidence_hash = None
        claimed_result_hash = None
        reproduced_result_hash = None
        document = {}

    passed = not reasons
    verification: dict[str, Any] = {
        "schema": "multidetect.gazebo-target-geolocation-acceptance-verification",
        "schema_version": 1,
        "evidence_schema": EVIDENCE_SCHEMA,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_sha256": claimed_evidence_hash,
        "deterministic_result_sha256": claimed_result_hash,
        "reproduced_result_sha256": reproduced_result_hash,
        "evidence_gate_passed": document.get("gate_passed"),
        "require_gate_passed": require_gate_passed,
        "reproduced": reproduced_result_hash == claimed_result_hash
        if _is_sha256(reproduced_result_hash) and _is_sha256(claimed_result_hash)
        else False,
        "passed": passed,
        "reasons": reasons,
    }
    verification["verification_sha256"] = canonical_sha256(verification)
    return verification


def build_input_error_document(message: str) -> dict[str, Any]:
    """Build fail-closed evidence for a precondition or truth-contract error."""

    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "input_contract_error",
        "error": {
            "type": "GazeboTargetGeolocationInputError",
            "message": str(message),
        },
        "claims": _negative_claims(),
        "production_approved": False,
        "qgc_visual_acceptance": "not_claimed",
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": False,
    }
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def build_runtime_error_document(message: str, *, error_type: str) -> dict[str, Any]:
    """Build fail-closed evidence for detector/session/runtime failures."""

    document = build_input_error_document(message)
    document.pop("evidence_sha256")
    document["status"] = "runtime_error"
    document["error"] = {"type": str(error_type), "message": str(message)}
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def write_evidence_exclusive(path: str | Path, document: Mapping[str, Any]) -> Path:
    """Create one immutable evidence file without overwriting an existing run."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise FileExistsError(f"refusing to write evidence through a symlink: {output}")
    payload = (
        json.dumps(
            dict(document),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    created = False
    try:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Remove only a newly created, incomplete output from this call.  An
        # existing evidence file is never opened and therefore never removed.
        if created:
            output.unlink(missing_ok=True)
        raise
    return output


def _verify_frame_set(
    path: Path,
    *,
    repository_root: Path,
    scenario_id: str,
    scenario_manifest_hash: str,
) -> _VerifiedFrameSet:
    raw = _load_json_object(path, "frame-set manifest")
    if raw.get("schema") != FRAME_SET_SCHEMA or raw.get("schema_version") != 2:
        raise GazeboTargetGeolocationInputError(
            "geolocation evaluation requires a schema-v2 Gazebo BGR24 frame set"
        )
    manifest_sha = _sha256_text(raw.get("manifest_sha256"), "frame-set manifest_sha256")
    without_hash = dict(raw)
    without_hash.pop("manifest_sha256", None)
    if canonical_sha256(without_hash) != manifest_sha:
        raise GazeboTargetGeolocationInputError("frame-set canonical SHA-256 is invalid")
    if raw.get("scenario_id") != scenario_id:
        raise GazeboTargetGeolocationInputError("frame-set scenario_id does not match scenario")
    if raw.get("scenario_manifest_hash") != scenario_manifest_hash:
        raise GazeboTargetGeolocationInputError(
            "frame-set scenario manifest hash does not match the supplied scenario"
        )
    if raw.get("wire_version") != 3 or raw.get("pixel_format") != "BGR24":
        raise GazeboTargetGeolocationInputError(
            "frame-set must use MDGZSIM3 wire version 3 and BGR24 pixels"
        )
    sensor_binding_hash = _sha256_text(
        raw.get("sensor_binding_hash"), "frame-set sensor_binding_hash"
    )
    source = _mapping(raw.get("source_attribution"), "frame-set source_attribution")
    required_source = {
        "pixels": "actual_gazebo_camera_sensor_output",
        "environment": "software_simulation",
        "wire": "MDGZSIM3",
        "source_row_order": "top_to_bottom",
        "wire_row_order": "top_to_bottom",
        "vertical_flip_applied": False,
        "truth_loaded_by_runtime": False,
        "per_frame_wire_telemetry_persisted": True,
        "wire_record_recomputed_from_persisted_bytes": True,
        "camera_intrinsics_source": "repository_calibration_hash_bound",
        "terrain_source": "repository_dem_hash_bound",
    }
    if any(source.get(key) != expected for key, expected in required_source.items()):
        raise GazeboTargetGeolocationInputError(
            "frame-set source attribution does not preserve the Gazebo/truth-isolation contract"
        )
    claims = _mapping(raw.get("claims"), "frame-set claims")
    if (
        claims.get("actual_gazebo_pixels_persisted") is not True
        or claims.get("per_frame_sensor_geolocation_input") != "passed"
        or claims.get("target_geolocation_evaluation") != "not_claimed"
    ):
        raise GazeboTargetGeolocationInputError(
            "frame-set claims do not preserve the pre-geolocation evidence boundary"
        )
    for claim in (
        "qgc_rendering",
        "manual_visual_acceptance",
        "hardware",
        "detector_evaluation",
        "production_model_approval",
    ):
        if claims.get(claim) != "not_claimed":
            raise GazeboTargetGeolocationInputError(
                f"frame-set claims.{claim} must remain not_claimed"
            )
    sensor_evidence = _mapping(raw.get("per_frame_sensor_evidence"), "per_frame_sensor_evidence")
    if (
        sensor_evidence.get("schema_version") != 1
        or sensor_evidence.get("wire_header_encoding") != "lowercase_hex_zero_digest_field"
        or sensor_evidence.get("wire_record_hash_algorithm")
        != "sha256(zero_digest_header || raw_bgr24)"
        or sensor_evidence.get("source_association") != "nearest_source_at_or_before_image"
        or sensor_evidence.get("maximum_source_skew_ns") != MAX_SOURCE_SKEW_NS
        or sensor_evidence.get("gps_and_pose_values_covered_by_wire_record_sha256") is not True
        or sensor_evidence.get("derived_ned_values_recomputed_before_seal") is not True
        or sensor_evidence.get("agl_source") != GAZEBO_SIM_TERRAIN_AGL_REFERENCE
    ):
        raise GazeboTargetGeolocationInputError(
            "frame-set per-frame sensor evidence contract is incomplete"
        )

    calibration_provenance = _mapping(raw.get("camera_calibration"), "frame-set camera_calibration")
    calibration_path = _repository_file_from_provenance(
        repository_root,
        calibration_provenance,
        label="camera calibration",
    )
    calibration_sha = sha256_file(calibration_path)
    if calibration_sha != calibration_provenance.get("file_sha256"):
        raise GazeboTargetGeolocationInputError("camera calibration SHA-256 mismatch")
    calibration = load_camera_calibration(calibration_path)
    if (
        calibration_provenance.get("calibration_id") != calibration.calibration_id
        or calibration_provenance.get("calibration") != asdict(calibration)
        or calibration_provenance.get("source") != "repository_calibration_hash_bound"
    ):
        raise GazeboTargetGeolocationInputError(
            "camera calibration provenance does not match the repository calibration"
        )

    dem_provenance = _mapping(raw.get("terrain_dem"), "frame-set terrain_dem")
    dem_path = _repository_file_from_provenance(
        repository_root,
        dem_provenance,
        label="terrain DEM",
    )
    dem_sha = sha256_file(dem_path)
    terrain_dem = load_local_dem_grid(dem_path)
    if (
        dem_sha != dem_provenance.get("file_sha256")
        or dem_sha != terrain_dem.source_sha256
        or dem_provenance.get("scenario_id") != scenario_id
        or terrain_dem.scenario_id != scenario_id
        or dem_provenance.get("coordinate_reference") != "WGS84"
        or dem_provenance.get("vertical_datum") != "MSL"
        or dem_provenance.get("altitude_reference") != GAZEBO_SIM_TERRAIN_AGL_REFERENCE
        or dem_provenance.get("odometry_local_up_used_as_agl") is not False
    ):
        raise GazeboTargetGeolocationInputError(
            "terrain DEM provenance does not match the repository DEM"
        )

    declared_assets = _mapping(raw.get("repository_model_assets"), "repository_model_assets")
    actual_assets = resolve_repository_model_asset_bundle(
        repository_root / "simulation" / "gazebo" / "models",
        repository_root=repository_root,
    ).portable_mapping()
    for key in ("schema_version", "file_count", "files", "assets_sha256"):
        if declared_assets.get(key) != actual_assets.get(key):
            raise GazeboTargetGeolocationInputError(
                f"repository Gazebo model asset bundle mismatch at {key}"
            )
    model_assets_sha = _sha256_text(
        declared_assets.get("assets_sha256"), "repository_model_assets.assets_sha256"
    )
    if source.get("repository_model_assets_sha256") != model_assets_sha or source.get(
        "repository_model_asset_file_count"
    ) != declared_assets.get("file_count"):
        raise GazeboTargetGeolocationInputError(
            "frame-set model asset attribution does not match its bound asset bundle"
        )

    frame_rows = raw.get("frames")
    if not isinstance(frame_rows, list) or not frame_rows:
        raise GazeboTargetGeolocationInputError("frame-set frames must be non-empty")
    if raw.get("frame_count") != len(frame_rows):
        raise GazeboTargetGeolocationInputError("frame-set frame_count is inconsistent")
    if sensor_evidence.get("record_count") != len(frame_rows):
        raise GazeboTargetGeolocationInputError(
            "per-frame sensor evidence record_count is inconsistent"
        )
    frames: list[_VerifiedFrame] = []
    total_bytes = 0
    previous_sensor_stamp_ns: int | None = None
    seen_ids: set[str] = set()
    expected_filenames: set[str] = set()
    for index, value in enumerate(frame_rows):
        row = _mapping(value, f"frames[{index}]")
        frame_id = _nonempty_text(row.get("frame_id"), f"frames[{index}].frame_id")
        if frame_id in seen_ids:
            raise GazeboTargetGeolocationInputError("frame-set frame_id values must be unique")
        seen_ids.add(frame_id)
        if row.get("sequence") != index:
            raise GazeboTargetGeolocationInputError(
                "frame-set sequence must be zero-based and contiguous"
            )
        filename = _nonempty_text(row.get("file"), f"frames[{index}].file")
        if Path(filename).name != filename or filename in expected_filenames:
            raise GazeboTargetGeolocationInputError("frame-set filenames must be unique basenames")
        expected_filenames.add(filename)
        frame_path = _contained_regular_file(path.parent, path.parent / filename, "frame")
        payload = frame_path.read_bytes()
        payload_sha = sha256_file(frame_path)
        expected_payload_sha = _sha256_text(row.get("sha256"), f"frames[{index}].sha256")
        if (
            payload_sha != expected_payload_sha
            or row.get("wire_payload_sha256") != payload_sha
            or row.get("raw_bytes_sha256_matches_wire_payload") is not True
            or row.get("bytes") != len(payload)
        ):
            raise GazeboTargetGeolocationInputError(
                f"frame-set payload identity mismatch for {frame_id}"
            )
        provenance = _mapping(row.get("provenance"), f"frames[{index}].provenance")
        width = _positive_int(provenance.get("width"), f"frames[{index}].width")
        height = _positive_int(provenance.get("height"), f"frames[{index}].height")
        if (
            width != calibration.width_px
            or height != calibration.height_px
            or provenance.get("stride") != width * 3
            or provenance.get("payload_bytes") != width * height * 3
            or len(payload) != width * height * 3
            or provenance.get("pixel_format") != "BGR24"
            or provenance.get("frame_id") != frame_id
            or provenance.get("sequence") != index
            or provenance.get("captured_at_clock") != "host_monotonic"
            or provenance.get("payload_sha256") != payload_sha
        ):
            raise GazeboTargetGeolocationInputError(
                f"frame-set geometry/provenance mismatch for {frame_id}"
            )
        captured_at_s = _nonnegative_real(
            provenance.get("captured_at_s"), f"frames[{index}].captured_at_s"
        )
        sensor_stamp = _positive_int(
            provenance.get("sensor_stamp_ns"), f"frames[{index}].sensor_stamp_ns"
        )
        unsigned_header_hex = _nonempty_text(
            provenance.get("wire_unsigned_header_hex"),
            f"frames[{index}].wire_unsigned_header_hex",
        )
        if unsigned_header_hex != unsigned_header_hex.lower():
            raise GazeboTargetGeolocationInputError(
                f"{frame_id} wire header must use canonical lowercase hexadecimal"
            )
        wire = reconstruct_gazebo_sim_wire_frame(
            unsigned_header_hex=unsigned_header_hex,
            payload_bgr24=payload,
            expected_sequence=index,
            expected_width=width,
            expected_height=height,
            previous_sensor_stamp_ns=previous_sensor_stamp_ns,
        )
        previous_sensor_stamp_ns = wire.sensor_stamp_ns
        if (
            sensor_stamp != wire.sensor_stamp_ns
            or provenance.get("camera_info_stamp_ns") != wire.camera_info_stamp_ns
            or provenance.get("navsat_stamp_ns") != wire.navsat_stamp_ns
            or provenance.get("odometry_stamp_ns") != wire.odometry_stamp_ns
            or provenance.get("flags") != wire.flags
        ):
            raise GazeboTargetGeolocationInputError(
                f"{frame_id} manifest timestamps/flags do not match its MDGZSIM3 header"
            )
        wire_record_sha = _sha256_text(
            row.get("wire_record_sha256"), f"frames[{index}].wire_record_sha256"
        )
        if (
            wire.record_sha256 != wire_record_sha
            or provenance.get("record_sha256") != wire_record_sha
            or provenance.get("wire_telemetry")
            != GazeboSimWireTelemetryProvenance.from_wire(wire).to_mapping()
        ):
            raise GazeboTargetGeolocationInputError(
                f"MDGZSIM3 wire reconstruction mismatch for {frame_id}"
            )
        telemetry = _verify_frame_telemetry(
            provenance,
            wire=wire,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            terrain_dem=terrain_dem,
        )
        frames.append(
            _VerifiedFrame(
                frame_id=frame_id,
                sequence=index,
                path=frame_path,
                payload=payload,
                payload_sha256=payload_sha,
                wire_record_sha256=wire_record_sha,
                width=width,
                height=height,
                captured_at_s=captured_at_s,
                sensor_stamp_ns=sensor_stamp,
                navsat_stamp_ns=wire.navsat_stamp_ns,
                odometry_stamp_ns=wire.odometry_stamp_ns,
                telemetry=telemetry,
            )
        )
        total_bytes += len(payload)
    if raw.get("total_payload_bytes") != total_bytes:
        raise GazeboTargetGeolocationInputError("frame-set total_payload_bytes is inconsistent")
    directory_entries = {entry.name for entry in path.parent.iterdir()}
    if directory_entries != expected_filenames | {path.name}:
        raise GazeboTargetGeolocationInputError(
            "frame-set directory contains an unexpected or missing entry"
        )
    return _VerifiedFrameSet(
        path=path,
        file_sha256=sha256_file(path),
        manifest_sha256=manifest_sha,
        scenario_id=scenario_id,
        scenario_manifest_hash=scenario_manifest_hash,
        sensor_binding_hash=sensor_binding_hash,
        camera_calibration_path=calibration_path,
        camera_calibration_sha256=calibration_sha,
        calibration=calibration,
        terrain_dem_path=dem_path,
        terrain_dem_sha256=dem_sha,
        terrain_dem=terrain_dem,
        model_assets_sha256=model_assets_sha,
        frames=tuple(frames),
    )


def _verify_frame_telemetry(
    provenance: Mapping[str, Any],
    *,
    wire: Any,
    frame_id: str,
    captured_at_s: float,
    terrain_dem: LocalDemGrid,
) -> _FrameTelemetry:
    raw = _mapping(provenance.get("frame_telemetry"), f"{frame_id}.frame_telemetry")
    roll, pitch, heading, velocity_north, velocity_east, _velocity_down = (
        gazebo_sim_ned_frd_kinematics(wire)
    )
    derived = {
        "latitude_deg": wire.latitude_deg,
        "longitude_deg": wire.longitude_deg,
        "gps_absolute_altitude_m": wire.altitude_m,
        "local_north_m": wire.position_north_m,
        "local_east_m": wire.position_east_m,
        "local_down_m": -wire.position_up_m,
        "roll_deg": roll,
        "pitch_deg": pitch,
        "heading_deg": heading,
        "velocity_north_mps": velocity_north,
        "velocity_east_mps": velocity_east,
    }
    if any(raw.get(key) != expected for key, expected in derived.items()):
        raise GazeboTargetGeolocationInputError(
            f"{frame_id} derived GPS/NED telemetry is not wire-bound"
        )
    if raw.get("altitude_reference") != GAZEBO_SIM_TERRAIN_AGL_REFERENCE:
        raise GazeboTargetGeolocationInputError(
            f"{frame_id} altitude reference is not the repository DEM"
        )
    gps_fix_type = _nonnegative_int(raw.get("gps_fix_type"), f"{frame_id}.gps_fix_type")
    satellites = _nonnegative_int(raw.get("satellites_visible"), f"{frame_id}.satellites_visible")
    if (
        not MAVLINK_QUALIFIED_FIX_TYPE_MIN <= gps_fix_type <= MAVLINK_GPS_FIX_TYPE_MAX
        or not MAVLINK_QUALIFIED_SATELLITES_MIN <= satellites <= MAVLINK_SATELLITES_MAX
    ):
        raise GazeboTargetGeolocationInputError(
            f"{frame_id} GPS quality is below the geolocation gate"
        )
    numeric = {
        key: _nonnegative_real(raw.get(key), f"{frame_id}.{key}")
        for key in (
            "altitude_agl_m",
            "gps_horizontal_accuracy_m",
            "gps_vertical_accuracy_m",
            "gps_observed_at_s",
            "attitude_observed_at_s",
            "velocity_observed_at_s",
            "altitude_observed_at_s",
        )
    }
    if (
        numeric["gps_horizontal_accuracy_m"] > GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
        or numeric["gps_vertical_accuracy_m"] > GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M
    ):
        raise GazeboTargetGeolocationInputError(
            f"{frame_id} GPS accuracy exceeds the canonical outdoor geolocation gate"
        )
    source_stamps = {
        "camera_info": wire.camera_info_stamp_ns,
        "navsat": wire.navsat_stamp_ns,
        "odometry": wire.odometry_stamp_ns,
    }
    for source_name, source_stamp in source_stamps.items():
        skew = wire.sensor_stamp_ns - source_stamp
        if not 0 <= skew <= MAX_SOURCE_SKEW_NS:
            raise GazeboTargetGeolocationInputError(
                f"{frame_id} {source_name} source is stale or from the future"
            )
        declared = provenance.get(f"{source_name}_source_skew_ns")
        if declared != skew:
            raise GazeboTargetGeolocationInputError(
                f"{frame_id} {source_name} source skew is inconsistent"
            )
    expected_gps_observed = (
        captured_at_s - (wire.sensor_stamp_ns - wire.navsat_stamp_ns) / 1_000_000_000.0
    )
    expected_odom_observed = (
        captured_at_s - (wire.sensor_stamp_ns - wire.odometry_stamp_ns) / 1_000_000_000.0
    )
    if (
        not math.isclose(numeric["gps_observed_at_s"], expected_gps_observed, abs_tol=1e-9)
        or not math.isclose(numeric["attitude_observed_at_s"], expected_odom_observed, abs_tol=1e-9)
        or not math.isclose(numeric["velocity_observed_at_s"], expected_odom_observed, abs_tol=1e-9)
        or not math.isclose(numeric["altitude_observed_at_s"], expected_gps_observed, abs_tol=1e-9)
    ):
        raise GazeboTargetGeolocationInputError(
            f"{frame_id} host/source timestamp mapping is inconsistent"
        )
    terrain = dem_gps_terrain_observation(
        terrain_dem,
        latitude_deg=wire.latitude_deg,
        longitude_deg=wire.longitude_deg,
        gps_absolute_altitude_m=wire.altitude_m,
        gps_horizontal_sigma_m=numeric["gps_horizontal_accuracy_m"],
        gps_vertical_sigma_m=numeric["gps_vertical_accuracy_m"],
        captured_at_s=numeric["gps_observed_at_s"],
    )
    if not math.isclose(terrain.height_m, numeric["altitude_agl_m"], abs_tol=1e-9):
        raise GazeboTargetGeolocationInputError(
            f"{frame_id} AGL does not match NavSat MSL minus the bound DEM"
        )
    return _FrameTelemetry(
        **{key: float(value) for key, value in derived.items()},
        altitude_agl_m=numeric["altitude_agl_m"],
        gps_fix_type=gps_fix_type,
        satellites_visible=satellites,
        gps_horizontal_accuracy_m=numeric["gps_horizontal_accuracy_m"],
        gps_vertical_accuracy_m=numeric["gps_vertical_accuracy_m"],
        gps_observed_at_s=numeric["gps_observed_at_s"],
        attitude_observed_at_s=numeric["attitude_observed_at_s"],
        velocity_observed_at_s=numeric["velocity_observed_at_s"],
        altitude_observed_at_s=numeric["altitude_observed_at_s"],
    )


def _score_prediction(
    prediction: _Prediction,
    truth_latitude_deg: float,
    truth_longitude_deg: float,
) -> dict[str, Any]:
    frame = prediction.frame
    telemetry = frame.telemetry
    geolocation = prediction.geolocation
    solution = prediction.range_solution
    truth_ground_range_m = wgs84_distance_m(
        latitude1_deg=telemetry.latitude_deg,
        longitude1_deg=telemetry.longitude_deg,
        latitude2_deg=truth_latitude_deg,
        longitude2_deg=truth_longitude_deg,
    )
    horizontal_error_m: float | None = None
    inside_one_sigma: bool | None = None
    if geolocation is not None:
        horizontal_error_m = wgs84_distance_m(
            latitude1_deg=geolocation.latitude_deg,
            longitude1_deg=geolocation.longitude_deg,
            latitude2_deg=truth_latitude_deg,
            longitude2_deg=truth_longitude_deg,
        )
        inside_one_sigma = horizontal_error_m <= geolocation.horizontal_sigma_m
    ci95_contains: bool | None = None
    if solution is not None and solution.ground_range_ci95_m is not None:
        ci95_contains = (
            solution.ground_range_ci95_m[0]
            <= truth_ground_range_m
            <= solution.ground_range_ci95_m[1]
        )
    return {
        "frame_id": frame.frame_id,
        "sequence": frame.sequence,
        "frame_sha256": frame.payload_sha256,
        "wire_record_sha256": frame.wire_record_sha256,
        "captured_at_s": frame.captured_at_s,
        "sensor_stamp_ns": frame.sensor_stamp_ns,
        "navsat_stamp_ns": frame.navsat_stamp_ns,
        "odometry_stamp_ns": frame.odometry_stamp_ns,
        "telemetry": asdict(telemetry),
        "detections": [_detection_mapping(item) for item in prediction.detections],
        "matching_detection_count": len(prediction.matching_detections),
        "anchor": prediction.anchor.to_mapping() if prediction.anchor is not None else None,
        "range_solution": _range_solution_mapping(solution),
        "predicted_target": (
            {
                "latitude_deg": geolocation.latitude_deg,
                "longitude_deg": geolocation.longitude_deg,
                "horizontal_sigma_m": geolocation.horizontal_sigma_m,
            }
            if geolocation is not None
            else None
        ),
        "truth_ground_range_m": truth_ground_range_m,
        "horizontal_error_m": horizontal_error_m,
        "inside_one_sigma": inside_one_sigma,
        "truth_ground_range_inside_ci95": ci95_contains,
        "processing_error": prediction.processing_error,
    }


def _range_solution_mapping(solution: RangeSolution | None) -> dict[str, Any] | None:
    if solution is None:
        return None
    return {
        "validity": solution.validity.value,
        "reasons": list(solution.reasons),
        "sources": list(solution.sources),
        "rejected_sources": list(solution.rejected_sources),
        "slant_range_m": solution.slant_range_m,
        "ground_range_m": solution.ground_range_m,
        "slant_range_ci95_m": (
            list(solution.slant_range_ci95_m) if solution.slant_range_ci95_m is not None else None
        ),
        "ground_range_ci95_m": (
            list(solution.ground_range_ci95_m) if solution.ground_range_ci95_m is not None else None
        ),
        "relative_bearing_deg": solution.relative_bearing_deg,
        "absolute_bearing_deg": solution.absolute_bearing_deg,
        "bearing_sigma_deg": solution.bearing_sigma_deg,
        "north_offset_m": solution.north_offset_m,
        "east_offset_m": solution.east_offset_m,
        "data_freshness_s": solution.data_freshness_s,
        "sensor_consistency": solution.sensor_consistency,
        "fusion_profile": solution.fusion_profile,
        "vehicle_profile": solution.vehicle_profile,
        "navigation_state": solution.navigation_state,
        "motion_regime": solution.motion_regime,
        "advisory_only": solution.advisory_only,
        "flight_control_enabled": solution.flight_control_enabled,
        "physical_release_enabled": solution.physical_release_enabled,
    }


def _detection_mapping(detection: Detection) -> dict[str, Any]:
    return {
        "label": detection.label,
        "confidence": detection.confidence,
        "normalized_bbox_xyxy": list(detection.bbox.rounded(9)),
        "model_version": detection.model_version,
    }


def _maximum_pairwise_stability_m(predictions: list[_Prediction]) -> float | None:
    coordinates = [item.geolocation for item in predictions if item.geolocation is not None]
    if len(coordinates) < 2:
        return None
    return max(
        wgs84_distance_m(
            latitude1_deg=left.latitude_deg,
            longitude1_deg=left.longitude_deg,
            latitude2_deg=right.latitude_deg,
            longitude2_deg=right.longitude_deg,
        )
        for index, left in enumerate(coordinates)
        for right in coordinates[index + 1 :]
    )


def _model_input_shape(manifest: Mapping[str, Any]) -> tuple[int, int, int, int]:
    input_contract = _mapping(manifest.get("input"), "model manifest input")
    expected = {
        "sensor": "rgb",
        "color_space": "RGB",
        "tensor_layout": "NCHW",
        "dtype": "float32",
        "resize": "letterbox",
    }
    if any(input_contract.get(key) != value for key, value in expected.items()):
        raise GazeboTargetGeolocationInputError(
            "candidate input must be rgb/RGB/NCHW/float32/letterbox"
        )
    shape = input_contract.get("shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in shape)
        or shape[0] != 1
        or shape[1] != 3
        or shape[2] <= 0
        or shape[3] <= 0
    ):
        raise GazeboTargetGeolocationInputError(
            "candidate input.shape must be [1, 3, positive_height, positive_width]"
        )
    return tuple(shape)  # type: ignore[return-value]


def _repository_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if (
        not root.is_dir()
        or not (root / "simulation" / "gazebo").is_dir()
        or not (root / "pyproject.toml").is_file()
    ):
        raise GazeboTargetGeolocationInputError(
            f"repository_root is not a Multi-Detect checkout: {root}"
        )
    return root


def _repository_file_from_provenance(
    repository_root: Path,
    provenance: Mapping[str, Any],
    *,
    label: str,
) -> Path:
    relative = _nonempty_text(provenance.get("repository_path"), f"{label}.repository_path")
    portable = PurePosixPath(relative)
    if portable.is_absolute() or ".." in portable.parts:
        raise GazeboTargetGeolocationInputError(f"{label} repository path is not contained")
    candidate = repository_root.joinpath(*portable.parts)
    return _contained_regular_file(repository_root, candidate, label)


def _contained_regular_file(root: Path, path: Path, label: str) -> Path:
    if path.is_symlink():
        raise GazeboTargetGeolocationInputError(f"{label} must not be a symlink: {path}")
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise GazeboTargetGeolocationInputError(f"{label} escapes its root: {path}") from exc
    if not resolved.is_file():
        raise GazeboTargetGeolocationInputError(f"{label} is not a regular file: {resolved}")
    return resolved


def _regular_file(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise GazeboTargetGeolocationInputError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise GazeboTargetGeolocationInputError(f"{label} is not a regular file: {resolved}")
    return resolved


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = strict_json_loads(path.read_bytes(), name=label)
    except (AutonomyContractError, OSError, TypeError) as exc:
        raise GazeboTargetGeolocationInputError(f"unable to load {label}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GazeboTargetGeolocationInputError(f"{label} must be a JSON object")
    return raw


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GazeboTargetGeolocationInputError(f"{label} must be an object")
    return value


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GazeboTargetGeolocationInputError(f"{label} must be non-empty text")
    return value.strip()


def _sha256_text(value: Any, label: str) -> str:
    text = _nonempty_text(value, label).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise GazeboTargetGeolocationInputError(f"{label} must be a SHA-256 digest")
    return text


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GazeboTargetGeolocationInputError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GazeboTargetGeolocationInputError(f"{label} must be a non-negative integer")
    return value


def _nonnegative_real(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise GazeboTargetGeolocationInputError(f"{label} must be finite and non-negative")
    return float(value)


def _repository_display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _repository_path(root: Path, value: Any, label: str) -> Path:
    relative = _nonempty_text(value, f"{label} path")
    portable = PurePosixPath(relative)
    if portable.is_absolute() or ".." in portable.parts:
        raise GazeboTargetGeolocationInputError(f"{label} path is not repository-contained")
    return _contained_regular_file(root, root.joinpath(*portable.parts), label)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _deterministic_result_sha256(document: Mapping[str, Any]) -> str:
    deterministic = dict(document)
    deterministic.pop("evaluated_at_utc", None)
    deterministic.pop("deterministic_result_sha256", None)
    deterministic.pop("evidence_sha256", None)
    return canonical_sha256(deterministic)


def _metric_summary(values: list[float]) -> dict[str, float]:
    if not values or any(not math.isfinite(value) for value in values):
        raise GazeboTargetGeolocationInputError("regime metrics require finite samples")
    return {
        "minimum": min(values),
        "maximum": max(values),
        "mean": sum(values) / len(values),
    }


def _negative_claims() -> dict[str, Any]:
    return {
        "environment": "software_simulation",
        "sim_algorithm_acceptance": False,
        "production_model_approval": "not_claimed",
        "qgc_rendering": "not_claimed",
        "manual_visual_acceptance": "not_claimed",
        "hardware": "not_claimed",
        "outdoor_gnss": "not_claimed",
        "hil": "not_claimed",
        "real": "not_claimed",
        "flight": "not_claimed",
    }


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency-specific
        raise GazeboTargetGeolocationInputError(
            "NumPy is required for Gazebo BGR24 geolocation evaluation"
        ) from exc
    return np


__all__ = [
    "EVIDENCE_SCHEMA",
    "EVIDENCE_SCHEMA_VERSION",
    "EVALUATOR_SOURCE_PATH",
    "GazeboTargetGeolocationConfig",
    "GazeboTargetGeolocationInputError",
    "build_input_error_document",
    "build_runtime_error_document",
    "evaluate_gazebo_target_geolocation",
    "verify_gazebo_target_geolocation_evidence",
    "write_evidence_exclusive",
]

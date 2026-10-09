"""Build truth-free OnboardAI samples from persisted Gazebo 3-D RGB frames.

The high-level OnboardAI model consumes the same structured vector on the
Jetson and in SIM.  This tool turns persisted Gazebo pixels plus the telemetry
published beside each frame into that vector.  It deliberately never opens a
scenario truth file and never copies a target pose or a simulator label into a
sample.  A detector is optional, but when supplied it is the same ONNX
detector boundary used by the live Jetson-shaped path.

The result is a training input, not a release or hardware claim.  A small
number of rendered frames is useful for plumbing and regression, but it is not
enough to promote a policy without an independent, objective-matched holdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_INVALIDATIONS = ROOT / "configs/training/gazebo-capture-provenance-invalidations-v1.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import (  # noqa: E402
    ONBOARD_MODEL_ACTION_ORDER,
    ONBOARD_MODEL_EVENT_KINDS,
    ONBOARD_MODEL_FEATURE_NAMES,
    ONBOARD_MODEL_TASK_ACTION_ORDER,
    ContextSnapshot,
    GeoPoint,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardModelInput,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from multidetect.multimodal_ranging import (  # noqa: E402
    AircraftPose,
    CameraCalibration,
    TargetImageObservation,
    _camera_offset_ned,
    _target_rays,
)
from multidetect.perception_factory import COCO80_CLASS_NAMES  # noqa: E402
from multidetect.simulator.stack import stack_plan_from_scenario  # noqa: E402
from multidetect.vision import OnnxRawYoloConfig, OnnxRawYoloDetector  # noqa: E402
from scripts.gazebo_3d_annotation_frame_set import (  # noqa: E402
    AnnotationFrameSetError,
    load_annotation_frame_set,
)
from scripts.verify_gazebo_3d_mainline_manifest import verify_manifest  # noqa: E402

VEHICLE_LIMITS: dict[str, tuple[float, float, float]] = {
    "multirotor": (18.0, 60.0, 180.0),
    "vtol": (32.0, 110.0, 420.0),
    "fixed-wing": (42.0, 150.0, 900.0),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, float(value)))


def _event_vector(event: OnboardEventKind) -> list[float]:
    return [float(event is candidate) for candidate in ONBOARD_MODEL_EVENT_KINDS]


def _phase_vector(phase: OnboardTaskAction) -> list[float]:
    return [float(phase is candidate) for candidate in ONBOARD_MODEL_TASK_ACTION_ORDER]


def _read_frame(manifest: dict[str, Any], root: Path, frame: dict[str, Any]) -> np.ndarray:
    provenance = frame.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("frame provenance is missing")
    width = provenance.get("width")
    height = provenance.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ValueError("frame geometry is invalid")
    relative = frame.get("file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("frame file is missing")
    path = root / relative
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = path.read_bytes()
    expected_size = width * height * 3
    if len(payload) != expected_size:
        raise ValueError(f"RGB payload size mismatch for {path.name}")
    expected_hash = frame.get("sha256") or provenance.get("payload_sha256")
    if not isinstance(expected_hash, str) or hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError(f"RGB payload hash mismatch for {path.name}")
    return np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3)


def _load_depth_manifest(path: Path | None) -> dict[tuple[int, str], dict[str, Any]]:
    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema") != "multidetect.gazebo-depth-companion-set"
    ):
        raise ValueError("depth manifest schema is unsupported")
    result: dict[tuple[int, str], dict[str, Any]] = {}
    for frame in document.get("frames", []):
        if not isinstance(frame, dict):
            raise ValueError("depth manifest frame must be an object")
        sequence = frame.get("sequence")
        frame_id = frame.get("frame_id")
        if not isinstance(sequence, int) or not isinstance(frame_id, str):
            raise ValueError("depth frame identity is invalid")
        result[(sequence, frame_id)] = frame
    return result


def _read_depth_values(
    depth_root: Path | None,
    frame: dict[str, Any] | None,
    *,
    width: int,
    height: int,
) -> np.ndarray | None:
    if depth_root is None or frame is None:
        return None
    relative = frame.get("file")
    if not isinstance(relative, str):
        return None
    path = depth_root / relative
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = path.read_bytes()
    expected_hash = frame.get("sha256")
    if not isinstance(expected_hash, str) or hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError(f"depth payload hash mismatch for {path.name}")
    values = np.frombuffer(payload, dtype=np.float32)
    if values.size != width * height:
        raise ValueError(f"depth payload size mismatch for {path.name}")
    return values.reshape(height, width)


def _depth_median_for_bbox(
    values: np.ndarray | None,
    *,
    bbox: tuple[float, float, float, float] | None,
    depth_frame: Mapping[str, Any] | None = None,
    camera_calibration: CameraCalibration | None = None,
) -> float | None:
    """Return the detector-box ray range using the live depth semantics.

    Gazebo's depth sensor publishes ``optical_axis_z_m`` while the OnboardAI
    runtime consumes ``ray_slant_m``.  Keeping the conversion here identical
    to :class:`OnboardFramePerceptionAdapter` prevents a pixel-level training
    row from teaching a different range than the Jetson path observes.
    """

    if values is None or bbox is None:
        return None
    height, width = values.shape
    x1, y1, x2, y2 = bbox
    left = max(0, min(width - 1, int(math.floor(x1 * width))))
    top = max(0, min(height - 1, int(math.floor(y1 * height))))
    right = max(left + 1, min(width, int(math.ceil(x2 * width))))
    bottom = max(top + 1, min(height, int(math.ceil(y2 * height))))
    # ``values`` is backed by a read-only ``np.frombuffer`` view of the
    # persisted FLOAT32 payload.  The optical-axis-to-ray conversion below is
    # intentionally in-place for efficiency, so take a writable copy of the
    # selected ROI before applying calibration.
    finite = values[top:bottom, left:right].astype(np.float32, copy=True)
    if depth_frame is None:
        distance_semantics = "ray_slant_m"
    else:
        distance_semantics = depth_frame.get("distance_semantics")
        if not isinstance(distance_semantics, str) or not distance_semantics:
            raise ValueError("depth frame distance_semantics is required")
    if distance_semantics == "optical_axis_z_m":

        def _calibration_value(name: str) -> float | None:
            # Do not substitute RGB calibration here.  The depth contract is
            # registered to RGB but its intrinsics are still an explicit part
            # of the persisted depth frame; missing values must fail closed.
            raw = depth_frame.get(name) if depth_frame is not None else None
            if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                return None
            return float(raw)

        fx_px = _calibration_value("fx_px")
        fy_px = _calibration_value("fy_px")
        cx_px = _calibration_value("cx_px")
        cy_px = _calibration_value("cy_px")
        if (
            fx_px is None
            or fy_px is None
            or cx_px is None
            or cy_px is None
            or fx_px <= 0.0
            or fy_px <= 0.0
        ):
            raise ValueError("optical_axis_z_m depth requires finite positive camera intrinsics")
        columns = np.arange(left, right, dtype=np.float32) + 0.5
        rows = np.arange(top, bottom, dtype=np.float32) + 0.5
        ray_x = (columns - cx_px) / fx_px
        ray_y = (rows - cy_px) / fy_px
        finite *= np.sqrt(1.0 + ray_y[:, None] ** 2 + ray_x[None, :] ** 2)
    elif distance_semantics != "ray_slant_m":
        raise ValueError(f"unsupported depth distance_semantics: {distance_semantics!r}")
    finite = finite[np.isfinite(finite) & (finite > 0.0)]
    if depth_frame is not None:
        minimum_raw = depth_frame.get("minimum_depth_m", 0.4)
        maximum_raw = depth_frame.get("maximum_depth_m", 800.0)
        if (
            not isinstance(minimum_raw, (int, float))
            or not isinstance(maximum_raw, (int, float))
            or not math.isfinite(float(minimum_raw))
            or not math.isfinite(float(maximum_raw))
            or float(minimum_raw) < 0.0
            or float(maximum_raw) < float(minimum_raw)
        ):
            raise ValueError("depth range bounds are invalid")
        finite = finite[(finite >= float(minimum_raw)) & (finite <= float(maximum_raw))]
    if finite.size == 0:
        return None
    return float(np.median(finite))


def _detector_for(path: Path | None, confidence: float) -> OnnxRawYoloDetector | None:
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(path)
    return OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=path,
            class_names=tuple(COCO80_CLASS_NAMES),
            input_width=640,
            input_height=640,
            confidence_threshold=confidence,
            providers=("CPUExecutionProvider",),
            model_version=f"rendered-3d-detector:{path.name}",
        )
    )


def _load_mainline_bindings(path: Path) -> dict[str, dict[str, str]]:
    """Load and hash-pin the approved 3-D world/model set."""

    root = ROOT.resolve()
    manifest_path = path.resolve(strict=True)
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("mainline manifest must be inside the repository") from exc
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"mainline manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(document, dict):
        raise ValueError("mainline manifest must be a JSON object")
    try:
        verify_manifest(manifest_path, repository_root=root)
    except Exception as exc:  # pragma: no cover - verifier supplies the detail
        raise ValueError(f"mainline manifest verification failed: {exc}") from exc
    result: dict[str, dict[str, str]] = {}
    for raw in document.get("scenarios", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("scenario_id"), str):
            raise ValueError("mainline scenario entry is invalid")
        scenario_id = raw["scenario_id"]
        scenario_path = root / str(raw["scenario_manifest"]["path"])
        dem_path = root / str(raw["dem_asset"]["path"])
        plan = stack_plan_from_scenario(
            scenario_path,
            repo_root=root,
            tool_capabilities={"gazebo": False, "px4": False},
        )
        result[scenario_id] = {
            # The runtime loader binds ScenarioRuntimeInput to the canonical
            # scenario-document hash, not the raw JSON byte hash.  Captured
            # MDGZSIM3 frames carry that same canonical value.  Keep the raw
            # file hash separately as an asset-integrity fact so the two hash
            # domains cannot be silently confused again.
            "scenario_manifest_sha256": plan.scenario_manifest_hash,
            "scenario_file_sha256": _sha256(scenario_path),
            "dem_sha256": _sha256(dem_path),
            "world_sdf_sha256": _sha256(plan.world_path),
            "vehicle_model_sha256": _sha256(plan.model_path),
            "vehicle_config_sha256": _sha256(plan.assets.model_config_path),
            "stack_assets_sha256": plan.assets.aggregate_sha256,
            "model_root_assets_sha256": plan.assets.model_root.aggregate_sha256,
        }
    if len(result) < 3:
        raise ValueError("mainline manifest has fewer than three approved scenarios")
    return result


def _verify_gazebo_provenance(
    document: dict[str, Any],
    path: Path,
    *,
    mainline_bindings: dict[str, dict[str, str]] | None = None,
) -> None:
    """Reject hand-written pixel claims that lack the sealed 3-D sensor chain."""

    invalidation_document = json.loads(PROVENANCE_INVALIDATIONS.read_text(encoding="utf-8"))
    if not isinstance(invalidation_document, dict):
        raise ValueError("Gazebo provenance invalidation registry is not an object")
    for entry in invalidation_document.get("entries", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path_suffix"), str):
            raise ValueError("Gazebo provenance invalidation registry entry is invalid")
        suffix = entry["path_suffix"].replace("\\", "/").lower()
        normalized_path = path.as_posix().lower().rstrip("/")
        if normalized_path.endswith(suffix):
            replacement = entry.get("replacement", "a new capture")
            raise ValueError(
                f"Gazebo materialization is explicitly invalidated: {path}; "
                f"use {replacement}"
            )

    claims = document.get("claims")
    attribution = document.get("source_attribution")
    sensor_evidence = document.get("per_frame_sensor_evidence")
    scenario_hash = document.get("scenario_manifest_hash")
    model_assets = document.get("repository_model_assets")
    terrain = document.get("terrain_dem")
    if not isinstance(claims, dict) or claims.get("actual_gazebo_pixels_persisted") is not True:
        raise ValueError(f"manifest is not an actual persisted Gazebo pixel set: {path}")
    if document.get("wire_version") != 3:
        raise ValueError(f"Gazebo wire-v3 provenance is missing: {path}")
    if not isinstance(attribution, dict) or any(
        attribution.get(key) != expected
        for key, expected in {
            "pixels": "actual_gazebo_camera_sensor_output",
            "wire": "MDGZSIM3",
            "per_frame_wire_telemetry_persisted": True,
            "wire_record_recomputed_from_persisted_bytes": True,
        }.items()
    ):
        raise ValueError(f"sealed Gazebo sensor attribution is missing: {path}")
    if (
        not isinstance(sensor_evidence, dict)
        or sensor_evidence.get("gps_and_pose_values_covered_by_wire_record_sha256") is not True
    ):
        raise ValueError(f"wire-bound Gazebo telemetry evidence is missing: {path}")
    for value, label in (
        (scenario_hash, "scenario_manifest_hash"),
        (document.get("sensor_binding_hash"), "sensor_binding_hash"),
        (document.get("manifest_sha256"), "manifest_sha256"),
    ):
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"{label} is not a sealed SHA-256: {path}")
    if not isinstance(model_assets, dict) or not isinstance(model_assets.get("assets_sha256"), str):
        raise ValueError(f"repository model asset hash is missing: {path}")
    if not isinstance(terrain, dict) or not isinstance(terrain.get("file_sha256"), str):
        raise ValueError(f"terrain hash is missing: {path}")
    if mainline_bindings is not None:
        scenario_id = document.get("scenario_id")
        expected = mainline_bindings.get(str(scenario_id))
        if expected is None:
            raise ValueError(f"scenario is not in the approved Gazebo mainline: {scenario_id}")
        if scenario_hash != expected["scenario_manifest_sha256"]:
            raise ValueError(
                f"scenario manifest hash is not bound to the approved mainline: {path}"
            )
        if model_assets.get("assets_sha256") != expected["model_root_assets_sha256"]:
            raise ValueError(f"repository model asset bundle is not the approved mainline: {path}")
        if terrain.get("file_sha256") != expected["dem_sha256"]:
            raise ValueError(f"terrain asset is not the approved mainline: {path}")
        binding = document.get("mainline_asset_binding")
        if not isinstance(binding, dict):
            raise ValueError(f"mainline asset binding is missing: {path}")
        for field in (
            "world_sdf_sha256",
            "vehicle_model_sha256",
            "vehicle_config_sha256",
            "stack_assets_sha256",
            "model_root_assets_sha256",
        ):
            if binding.get(field) != expected[field]:
                raise ValueError(f"mainline asset binding {field} does not match: {path}")


def _sample_from_frame(
    *,
    scenario_id: str,
    vehicle_class: str,
    frame: dict[str, Any],
    image: np.ndarray,
    detector: OnnxRawYoloDetector | None,
    depth_values: np.ndarray | None,
    camera_calibration: CameraCalibration | None = None,
    depth_frame: Mapping[str, Any] | None = None,
    target_match_confirmed: bool = False,
) -> dict[str, object]:
    provenance = frame["provenance"]
    telemetry = provenance.get("frame_telemetry")
    if not isinstance(telemetry, dict):
        raise ValueError("rendered frame has no telemetry")
    fix_type = float(telemetry.get("gps_fix_type", 0.0))
    horizontal_accuracy = float(telemetry.get("gps_horizontal_accuracy_m", 100.0)) / 100.0
    ground_speed = math.hypot(
        float(telemetry.get("velocity_north_mps", 0.0)),
        float(telemetry.get("velocity_east_mps", 0.0)),
    )
    airspeed = float(telemetry.get("airspeed_mps", ground_speed))
    detections = tuple(detector.detect(image)) if detector is not None else ()
    selected = max(detections, key=lambda item: item.confidence, default=None)
    depth_m = _depth_median_for_bbox(
        depth_values,
        bbox=None if selected is None else selected.bbox.rounded(),
        depth_frame=depth_frame,
        camera_calibration=camera_calibration,
    )
    if (
        camera_calibration is not None
        and selected is not None
        and (
            camera_calibration.width_px != image.shape[1]
            or camera_calibration.height_px != image.shape[0]
        )
    ):
        raise ValueError("camera calibration dimensions must match the RGB frame")
    confidence = 0.0 if selected is None else _clip(selected.confidence)
    gps_fraction = _clip(fix_type / 6.0)
    accuracy_fraction = _clip(horizontal_accuracy)
    event_kind = OnboardEventKind.TARGET_LOST if selected is None else OnboardEventKind.TARGET_MOVED
    hard_hold = gps_fraction < 0.5 or accuracy_fraction > 0.06
    task = OnboardTaskAction.HOLD if hard_hold else OnboardTaskAction.SEARCH
    flight = OnboardFlightAction.HOLD_POSITION if hard_hold else OnboardFlightAction.MOVE_RELATIVE
    speed_fraction = _clip(airspeed / 50.0)
    altitude = _clip(float(telemetry.get("altitude_agl_m", 0.0)) / 120.0)
    north = 0.0
    east = 0.0
    geometry_source = "unavailable"
    target_geometry_frame = "camera_relative"
    target_bearing_deg: float | None = None
    target_distance_m = depth_m
    # A detector box is only a candidate.  The live Jetson adapter projects a
    # box into NED/GPS only after nearest-target, confidence and Prompt
    # attribute gates pass.  The rendered builder has no Prompt/tracker state,
    # so it must remain camera-relative by default; otherwise a generic COCO
    # false positive would become a fabricated target location in training.
    projection_gate = (
        not hard_hold
        and selected is not None
        and depth_m is not None
        and bool(target_match_confirmed)
    )
    if projection_gate:
        if camera_calibration is not None:
            telemetry_timestamp = float(
                telemetry.get("attitude_observed_at_s", telemetry.get("captured_at_s", 0.0))
            )
            pose = AircraftPose(
                captured_at_s=telemetry_timestamp,
                roll_deg=float(telemetry.get("roll_deg", 0.0)),
                pitch_deg=float(telemetry.get("pitch_deg", 0.0)),
                heading_deg=float(telemetry.get("heading_deg", 0.0)) % 360.0,
                roll_sigma_deg=float(telemetry.get("attitude_roll_sigma_deg", 0.3)),
                pitch_sigma_deg=float(telemetry.get("attitude_pitch_sigma_deg", 0.3)),
                heading_sigma_deg=float(telemetry.get("attitude_heading_sigma_deg", 1.0)),
                uncertainty_source=str(
                    telemetry.get(
                        "attitude_uncertainty_source",
                        "gazebo_rendered_frame_telemetry",
                    )
                ),
            )
            target = TargetImageObservation(
                target_id=str(frame.get("frame_id", "rendered-target")),
                frame_id=str(frame.get("frame_id", "rendered-frame")),
                captured_at_s=telemetry_timestamp,
                center_x=float(selected.bbox.center[0]),
                center_y=float(selected.bbox.center[1]),
            )
            _body_ray, ned_ray = _target_rays(camera_calibration, pose, target)
            camera_offset = _camera_offset_ned(camera_calibration, pose)
            north = camera_offset[0] + depth_m * ned_ray[0]
            east = camera_offset[1] + depth_m * ned_ray[1]
            # The labelled vector is the same deterministic camera/depth/NED
            # transform used by the live Jetson-shaped perception adapter.
            geometry_source = "camera_calibration_depth_pose_ned"
            horizontal_distance_m = math.hypot(north, east)
            if horizontal_distance_m > 1.0e-6:
                target_geometry_frame = "ned"
                target_bearing_deg = math.degrees(math.atan2(east, north))
                target_distance_m = horizontal_distance_m
            else:
                # A vertical/degenerate projection has no horizontal bearing;
                # keep it fail-closed instead of inventing one.
                north = east = 0.0
                geometry_source = "degenerate_camera_calibration_depth_pose_ned"
        else:
            # A legacy RGB-only plumbing fixture has no calibrated pose.  It
            # may still be inspected, but must not masquerade as a metric 3-D
            # training sample.  Keep the row's motion target zero and expose
            # the missing geometry in provenance instead of inventing a sign.
            geometry_source = "missing_camera_calibration"
    elif selected is not None and depth_m is not None and camera_calibration is None:
        # Keep the legacy diagnostic explicit even when the Prompt geometry
        # gate is closed; this row is not a metric NED training observation.
        geometry_source = "missing_camera_calibration"
    if hard_hold:
        altitude = 0.0
        speed_fraction = 0.0
    captured_at_s = max(
        0.0,
        float(
            telemetry.get(
                "captured_at_s",
                telemetry.get("attitude_observed_at_s", telemetry.get("gps_observed_at_s", 0.0)),
            )
        ),
    )
    captured_at_utc = (
        (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=captured_at_s))
        .isoformat()
        .replace("+00:00", "Z")
    )
    context = ContextSnapshot(
        snapshot_id=f"rendered-{frame.get('frame_id', 'frame')}",
        captured_at_s=captured_at_s,
        captured_at_utc=captured_at_utc,
        aircraft_position=GeoPoint(0.0, 0.0),
        home_position=GeoPoint(0.0, 0.0),
        airframe_profile_id=f"px4_{vehicle_class}_gazebo_v1",
        battery_remaining_pct=float(telemetry.get("battery_remaining_pct", 80.0)),
        estimated_flight_time_s=2_520.0,
        ground_speed_mps=ground_speed,
        airspeed_mps=airspeed,
        wind_north_mps=float(telemetry.get("wind_north_mps", 0.0)),
        wind_east_mps=float(telemetry.get("wind_east_mps", 0.0)),
        gps_fix_type=max(0, min(6, int(round(fix_type)))),
        gps_horizontal_accuracy_m=float(telemetry.get("gps_horizontal_accuracy_m", 100.0)),
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=depth_m is not None,
    )
    event = OnboardRuntimeEvent(
        event_id=str(frame.get("frame_id", "rendered-event")),
        kind=event_kind,
        observed_at_s=captured_at_s,
        severity=70 if hard_hold else 0,
        target_bearing_deg=target_bearing_deg,
        target_geometry_frame=target_geometry_frame,
        target_distance_m=target_distance_m,
        target_north_offset_m=north if target_geometry_frame == "ned" else None,
        target_east_offset_m=east if target_geometry_frame == "ned" else None,
        target_confidence=confidence,
        target_match_score=0.0,
        target_class="vehicle" if selected is not None else None,
        target_class_match=None,
        target_color_match=None,
        route_progress_fraction=0.10,
        task_phase=OnboardTaskAction.SEARCH,
    )
    features = list(OnboardModelInput(context, event).feature_vector())
    if len(features) != len(ONBOARD_MODEL_FEATURE_NAMES):
        raise AssertionError("rendered feature order diverged from onboard contract")
    regression = [
        math.atanh(max(-0.98, min(0.98, north / 500.0))),
        math.atanh(max(-0.98, min(0.98, east / 500.0))),
        math.log(max(1.0e-4, altitude) / max(1.0e-4, 1.0 - altitude)),
        math.log(max(1.0e-4, speed_fraction) / max(1.0e-4, 1.0 - speed_fraction)),
    ]
    return {
        "features": features,
        "task_label": ONBOARD_MODEL_TASK_ACTION_ORDER.index(task),
        "flight_label": ONBOARD_MODEL_ACTION_ORDER.index(flight),
        "regression": regression,
        "family": f"rendered_3d_{scenario_id}_{vehicle_class}_{event_kind.value}",
        "source": {
            "scenario_id": scenario_id,
            "frame_id": frame.get("frame_id"),
            "sequence": frame.get("sequence"),
            "rgb_payload_sha256": frame.get("sha256"),
            "detector_count": len(detections),
            "detector_label": None if selected is None else selected.label,
            "detector_confidence": confidence,
            "depth_median": depth_m,
            "depth_distance_semantics": (
                "ray_slant_m" if depth_frame is None else depth_frame.get("distance_semantics")
            ),
            "geometry_source": geometry_source,
            "target_match_confirmed": bool(target_match_confirmed),
            "projection_gate": bool(projection_gate),
            "event_kind": event_kind.value,
            "truth_injected": False,
        },
    }


def build_dataset(
    manifests: list[Path],
    *,
    depth_manifests: list[Path],
    detector_path: Path | None,
    detector_confidence: float,
    max_frames_per_manifest: int | None,
    require_depth: bool = False,
    mainline_manifest: Path | None = None,
    annotation_manifests: list[Path] | None = None,
    annotation_frame_set: Path | None = None,
    require_annotation_supervision: bool = False,
) -> dict[str, object]:
    if not manifests:
        raise ValueError("at least one RGB manifest is required")
    if len(depth_manifests) not in {0, len(manifests)}:
        raise ValueError("depth manifests must be omitted or match RGB manifest count")
    if annotation_frame_set is not None:
        if annotation_manifests:
            raise ValueError("use annotation_frame_set or annotation_manifests, not both")
        annotation_manifests = [annotation_frame_set]
    annotation_manifests = list(annotation_manifests or [])
    if require_annotation_supervision and not annotation_manifests:
        raise ValueError(
            "require_annotation_supervision requires a hash-bound Gazebo 3-D annotation frame set"
        )
    if annotation_manifests and len(annotation_manifests) not in {1, len(manifests)}:
        raise ValueError(
            "annotation frame sets must be omitted, one shared set, or match RGB count"
        )
    detector = _detector_for(detector_path, detector_confidence)
    mainline_bindings = (
        None if mainline_manifest is None else _load_mainline_bindings(mainline_manifest)
    )
    rows: list[dict[str, object]] = []
    source_documents: list[dict[str, object]] = []
    for index, manifest_path in enumerate(manifests):
        manifest_path = manifest_path.resolve()
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("schema") != "multidetect.gazebo-bgr24-frame-set"
        ):
            raise ValueError(f"unsupported RGB manifest: {manifest_path}")
        _verify_gazebo_provenance(
            document,
            manifest_path,
            mainline_bindings=mainline_bindings,
        )
        scenario_id = str(document.get("scenario_id", "unknown"))
        vehicle_class = (
            "fixed-wing"
            if "fixed_wing" in scenario_id or "fixed-wing" in scenario_id
            else "vtol"
            if "vtol" in scenario_id
            else "multirotor"
        )
        depth_path = depth_manifests[index].resolve() if depth_manifests else None
        annotation_path: Path | None = None
        annotation_index: dict[tuple[int, str, str], dict[str, Any]] = {}
        if annotation_manifests:
            annotation_path = (
                annotation_manifests[0]
                if len(annotation_manifests) == 1
                else annotation_manifests[index]
            ).resolve()
            try:
                annotation_index = load_annotation_frame_set(
                    annotation_path,
                    rgb_manifest=manifest_path,
                )
            except AnnotationFrameSetError as exc:
                raise ValueError(f"annotation frame-set binding failed: {exc}") from exc
        if require_depth and depth_path is None:
            raise ValueError(f"depth companion is required for {manifest_path}")
        depth_index = _load_depth_manifest(depth_path)
        depth_root = None if depth_path is None else depth_path.parent
        frame_list = document.get("frames")
        if not isinstance(frame_list, list) or not frame_list:
            raise ValueError(f"RGB manifest has no frames: {manifest_path}")
        limit = (
            len(frame_list)
            if max_frames_per_manifest is None
            else min(len(frame_list), max_frames_per_manifest)
        )
        for frame in frame_list[:limit]:
            if not isinstance(frame, dict):
                raise ValueError("RGB manifest frame must be an object")
            image = _read_frame(document, manifest_path.parent, frame)
            provenance = frame["provenance"]
            telemetry = provenance.get("frame_telemetry")
            if not isinstance(telemetry, dict):
                raise ValueError(f"frame telemetry is missing for {manifest_path.name}")
            # These values are the minimum sensor-contract fields needed to
            # form an observation.  Missing values must fail closed instead of
            # silently becoming a healthy vehicle state.
            required_telemetry = (
                "gps_fix_type",
                "gps_horizontal_accuracy_m",
                "velocity_north_mps",
                "velocity_east_mps",
                "altitude_agl_m",
                "gps_observed_at_s",
                "altitude_observed_at_s",
                "velocity_observed_at_s",
            )
            for field in required_telemetry:
                value = telemetry.get(field)
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError(f"required telemetry field {field} is invalid")
            depth_frame = depth_index.get(
                (int(frame.get("sequence", -1)), str(frame.get("frame_id", "")))
            )
            if require_depth and depth_frame is None:
                raise ValueError(
                    f"RGB frame {frame.get('frame_id')} has no matching depth companion"
                )
            depth_values = _read_depth_values(
                depth_root,
                depth_frame,
                width=int(provenance["width"]),
                height=int(provenance["height"]),
            )
            annotation_key = (
                int(frame.get("sequence", -1)),
                str(frame.get("frame_id", "")),
                str(frame.get("sha256", "")),
            )
            annotation = annotation_index.get(annotation_key)
            if annotation_manifests and annotation is None:
                # Exact identity is mandatory.  There is intentionally no
                # timestamp-nearest fallback, because it can label a
                # different RGB payload after a dropped or reordered frame.
                raise ValueError(
                    "annotation frame does not exactly bind RGB sequence/frame/hash: "
                    f"{frame.get('sequence')}/{frame.get('frame_id')}"
                )
            raw_camera_calibration = document.get("camera_calibration")
            raw_calibration = (
                raw_camera_calibration.get("calibration")
                if isinstance(raw_camera_calibration, dict)
                else None
            )
            camera_calibration = (
                CameraCalibration(**raw_calibration) if isinstance(raw_calibration, dict) else None
            )
            row = _sample_from_frame(
                scenario_id=scenario_id,
                vehicle_class=vehicle_class,
                frame=frame,
                image=image,
                detector=detector,
                depth_values=depth_values,
                camera_calibration=camera_calibration,
                depth_frame=depth_frame,
            )
            source = row["source"]
            if not isinstance(source, dict):
                raise ValueError("rendered row source provenance is invalid")
            source["rgb_manifest_sha256"] = _sha256(manifest_path)
            source["scenario_manifest_hash"] = document.get("scenario_manifest_hash")
            source["depth_payload_sha256"] = (
                None if depth_frame is None else depth_frame.get("sha256")
            )
            # Use the immutable RGB payload hash as the smallest split unit.
            # Repeated augmentation of the same frame therefore cannot leak
            # across train and evaluation partitions.
            source["split_group"] = (
                f"{scenario_id}:{vehicle_class}:{frame.get('sha256', frame.get('frame_id'))}"
            )
            if annotation is not None:
                bbox = annotation["bounding_box_3d"]
                source["annotation_frame_bound"] = True
                source["annotation_label_id"] = bbox["label_id"]
                source["annotation_topics"] = {
                    "bounding_box_3d": bbox["topic"],
                    "instance_segmentation": annotation["instance_segmentation"]["topic"],
                }
                # Annotation remains a separate offline-supervision payload;
                # _sample_from_frame above never receives it, so runtime
                # policy features/events cannot consume simulator truth.
                row["annotation_supervision"] = {
                    "label_id": bbox["label_id"],
                    "class_name": "search_target",
                    "normalized_bbox": list(bbox["normalized_bbox"]),
                    "range_3d_m": list(bbox["range_3d_m"]),
                    "source": "gazebo_capture_only_annotation",
                    "runtime_input": False,
                }
            rows.append(row)
        source_documents.append(
            {
                "path": manifest_path.as_posix(),
                "sha256": _sha256(manifest_path),
                "scenario_id": scenario_id,
                "frame_count_used": limit,
                "depth_manifest": None
                if depth_path is None
                else {"path": depth_path.as_posix(), "sha256": _sha256(depth_path)},
                "annotation_frame_set": None
                if annotation_path is None
                else {
                    "path": annotation_path.as_posix(),
                    "sha256": _sha256(annotation_path),
                    "frame_count": len(annotation_index),
                },
            }
        )
    if require_annotation_supervision:
        missing = [
            index
            for index, row in enumerate(rows)
            if not isinstance(row.get("annotation_supervision"), dict)
            or row.get("source", {}).get("annotation_frame_bound") is not True
        ]
        if missing:
            raise ValueError(
                "required Gazebo 3-D annotation supervision is missing for rendered rows: "
                + ", ".join(str(index) for index in missing[:8])
            )
    return {
        "schema_version": 1,
        "contract": "multidetect.onboard-rendered-3d-dataset-v1",
        "feature_names": list(ONBOARD_MODEL_FEATURE_NAMES),
        "task_action_order": [item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER],
        "flight_action_order": [item.value for item in ONBOARD_MODEL_ACTION_ORDER],
        "rows": rows,
        "source_manifests": source_documents,
        "detector": None
        if detector_path is None
        else {
            "path": detector_path.resolve().as_posix(),
            "sha256": _sha256(detector_path.resolve()),
            "confidence_threshold": detector_confidence,
        },
        "pixel_level_rendered": True,
        "truth_free": True,
        "labels_from": "sensor_contract_heuristic_plumbing_only",
        "training_ready": False,
        "reviewed_annotation_manifest": None,
        "annotation_supervision_only": bool(annotation_manifests),
        "annotation_runtime_input": False,
        "depth_required": require_depth,
        "depth_rows": sum(
            1
            for row in rows
            if isinstance(row.get("source"), dict) and row["source"].get("depth_payload_sha256")
        ),
        "depth_measurement_rows": sum(
            1
            for row in rows
            if isinstance(row.get("source"), dict) and row["source"].get("depth_median") is not None
        ),
        "calibrated_geometry_rows": sum(
            1
            for row in rows
            if isinstance(row.get("source"), dict)
            and row["source"].get("geometry_source") == "camera_calibration_depth_pose_ned"
        ),
        "truth_injected": False,
        "external_gazebo_runtime": True,
        "jetson_hardware_evidence": False,
        "pixhawk_hardware_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "event_coverage": {
            "contract": "multidetect.onboard-3d-event-coverage-v1",
            "event_kind_counts": dict(
                sorted(
                    Counter(
                        row.get("source", {}).get("event_kind")
                        for row in rows
                        if isinstance(row, dict)
                        and isinstance(row.get("source"), dict)
                        and isinstance(row["source"].get("event_kind"), str)
                    ).items()
                )
            ),
            "truth_free": True,
            "training_ready": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rgb-manifest", type=Path, action="append", required=True)
    parser.add_argument("--depth-manifest", type=Path, action="append", default=[])
    parser.add_argument(
        "--annotation-frame-set",
        type=Path,
        action="append",
        default=[],
        help=(
            "optional strict capture-only Gazebo annotation frame set; provide one shared "
            "set or one per RGB manifest"
        ),
    )
    parser.add_argument("--detector", type=Path, default=None)
    parser.add_argument("--detector-confidence", type=float, default=0.25)
    parser.add_argument("--max-frames-per-manifest", type=int, default=None)
    parser.add_argument(
        "--require-depth",
        action="store_true",
        help="fail unless every RGB frame has a hash-verified companion depth frame",
    )
    parser.add_argument(
        "--require-annotation-supervision",
        action="store_true",
        help=(
            "fail unless every rendered RGB frame is exactly bound to the persisted "
            "capture-only Gazebo 3-D annotation frame set"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mainline-manifest",
        type=Path,
        default=ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
        help="hash-pinned repository Gazebo mainline required for training capture",
    )
    args = parser.parse_args(argv)
    if args.max_frames_per_manifest is not None and args.max_frames_per_manifest <= 0:
        raise ValueError("--max-frames-per-manifest must be positive")
    if not math.isfinite(args.detector_confidence) or not 0.0 <= args.detector_confidence <= 1.0:
        raise ValueError("--detector-confidence must be in [0, 1]")
    document = build_dataset(
        args.rgb_manifest,
        depth_manifests=args.depth_manifest,
        detector_path=args.detector,
        detector_confidence=args.detector_confidence,
        max_frames_per_manifest=args.max_frames_per_manifest,
        require_depth=args.require_depth,
        mainline_manifest=args.mainline_manifest,
        annotation_manifests=args.annotation_frame_set,
        require_annotation_supervision=args.require_annotation_supervision,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": args.output.as_posix(), "rows": len(document["rows"])},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Hash-bound three-control evaluation for a quarantined raw YOLO candidate.

The evaluator intentionally answers one narrow question: does one immutable
candidate model find one independently annotated target while remaining quiet
on a blank image and on the same frame with that annotation region occluded?
It is not an accuracy benchmark, a production approval, a QGC visual test, or
hardware evidence.

Ground-truth annotations are parsed only after their source-frame identity is
verified.  They are used by this module for scoring and for constructing the
occlusion control; the detector receives pixels only.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    strict_json_loads,
)
from .backends.gazebo_sim import (
    GazeboSimWireTelemetryProvenance,
    gazebo_sim_ned_frd_kinematics,
    reconstruct_gazebo_sim_wire_frame,
)
from .domain import BoundingBox, Detection
from .inference_provider import provider_selection_document
from .model_manifest import ModelManifestError, sha256_file, verify_model_manifest
from .vision import OnnxRawYoloConfig, OnnxRawYoloDetector

EVIDENCE_SCHEMA = "multidetect.candidate-detector-controls"
EVIDENCE_SCHEMA_VERSION = 1
ANNOTATION_SCHEMA = "multidetect.candidate-detector-control-annotation"
FRAME_SET_SCHEMA = "multidetect.gazebo-bgr24-frame-set"
RAW_YOLO_NATIVE_OUTPUT = "ultralytics_raw_xywh_class_scores"
MAX_SOURCE_SKEW_NS = 1_500_000_000


class CandidateDetectorControlsInputError(ValueError):
    """Raised before inference when an identity or input contract is invalid."""


class _Detector(Protocol):
    provider_names: tuple[str, ...]
    provider_selection: Any

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]: ...


DetectorFactory = Callable[[OnnxRawYoloConfig], _Detector]


@dataclass(frozen=True, slots=True)
class CandidateDetectorControlsConfig:
    model_path: Path
    model_manifest_path: Path
    frame_path: Path
    annotation_path: Path
    frame_width: int | None = None
    frame_height: int | None = None
    frame_set_manifest_path: Path | None = None
    confidence_threshold: float = 0.25
    positive_min_confidence: float = 0.25
    positive_min_iou: float = 0.50
    nms_iou_threshold: float = 0.45
    maximum_detections: int = 300
    providers: tuple[str, ...] = ("CPUExecutionProvider",)

    def __post_init__(self) -> None:
        for name, value in (
            ("confidence_threshold", self.confidence_threshold),
            ("positive_min_confidence", self.positive_min_confidence),
            ("positive_min_iou", self.positive_min_iou),
            ("nms_iou_threshold", self.nms_iou_threshold),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CandidateDetectorControlsInputError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise CandidateDetectorControlsInputError(f"{name} must be in [0, 1]")
        if self.positive_min_confidence < self.confidence_threshold:
            raise CandidateDetectorControlsInputError(
                "positive_min_confidence cannot be below confidence_threshold"
            )
        if (
            isinstance(self.maximum_detections, bool)
            or not isinstance(self.maximum_detections, int)
            or self.maximum_detections <= 0
        ):
            raise CandidateDetectorControlsInputError(
                "maximum_detections must be a positive integer"
            )
        if (self.frame_width is None) != (self.frame_height is None):
            raise CandidateDetectorControlsInputError(
                "raw frame width and height must be supplied together"
            )
        for name, value in (
            ("frame_width", self.frame_width),
            ("frame_height", self.frame_height),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise CandidateDetectorControlsInputError(f"{name} must be a positive integer")
        if not self.providers:
            raise CandidateDetectorControlsInputError("at least one provider must be requested")
        if any(not isinstance(item, str) or not item.strip() for item in self.providers):
            raise CandidateDetectorControlsInputError(
                "providers must contain non-empty provider names"
            )


@dataclass(frozen=True, slots=True)
class _Annotation:
    frame_id: str
    frame_sha256: str
    expected_label: str
    bbox: BoundingBox
    document_sha256: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class _FrameSetBinding:
    supplied: bool
    verified: bool
    manifest_sha256: str | None
    scenario_id: str | None
    scenario_manifest_hash: str | None
    sensor_binding_hash: str | None
    wire_version: int | None
    actual_gazebo_pixels_persisted: bool
    telemetry_causal_binding: bool
    runtime_truth_loaded: bool | None
    selected_wire_record_sha256: str | None
    selected_sensor_stamp_ns: int | None
    selected_navsat_stamp_ns: int | None
    selected_odometry_stamp_ns: int | None
    maximum_source_skew_ns: int | None
    source_row_order: str | None
    wire_row_order: str | None
    vertical_flip_applied: bool | None


def evaluate_candidate_detector_controls(
    config: CandidateDetectorControlsConfig,
    *,
    detector_factory: DetectorFactory = OnnxRawYoloDetector,
) -> dict[str, Any]:
    """Run present, zero-blank, and annotation-occluded controls.

    Every file/hash/geometry check completes before the detector factory is
    called.  The factory is called once, and the same detector session receives
    all three pixel arrays.
    """

    try:
        model_path = _regular_file(config.model_path, "model")
        manifest_path = _regular_file(config.model_manifest_path, "model manifest")
        frame_path = _regular_file(config.frame_path, "frame")
        annotation_path = _regular_file(config.annotation_path, "annotation")
        frame_set_path = (
            _regular_file(config.frame_set_manifest_path, "frame-set manifest")
            if config.frame_set_manifest_path is not None
            else None
        )

        manifest = _load_json_object(manifest_path, "model manifest")
        verified_model = _verify_candidate_manifest(manifest, manifest_path, model_path)
        annotation = _load_annotation(annotation_path)
        frame_digest = sha256_file(frame_path)
        if annotation.frame_sha256 != frame_digest:
            raise CandidateDetectorControlsInputError(
                "annotation frame_sha256 does not match the selected frame"
            )

        frame_bgr, width, height = _load_frame_bgr(
            frame_path,
            width=config.frame_width,
            height=config.frame_height,
        )
        _verify_optional_annotation_geometry(
            _load_json_object(annotation_path, "annotation"),
            width=width,
            height=height,
        )
        if annotation.expected_label not in verified_model.class_names:
            raise CandidateDetectorControlsInputError(
                f"annotation expected_label {annotation.expected_label!r} is absent from manifest"
            )
        frame_set = _verify_frame_set_binding(
            frame_set_path,
            frame_path=frame_path,
            frame_id=annotation.frame_id,
            frame_sha256=frame_digest,
            width=width,
            height=height,
        )
    except CandidateDetectorControlsInputError:
        raise
    except (AutonomyContractError, ModelManifestError, OSError, ValueError) as exc:
        raise CandidateDetectorControlsInputError(str(exc)) from exc

    input_shape = _manifest_input_shape(manifest)
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

    np = _require_numpy()
    blank_bgr = np.zeros_like(frame_bgr)
    occluded_bgr = frame_bgr.copy()
    left, top, right, bottom = _bbox_pixel_bounds(annotation.bbox, width=width, height=height)
    occluded_bgr[top:bottom, left:right] = 114

    present_detections = tuple(detector.detect(frame_bgr))
    blank_detections = tuple(detector.detect(blank_bgr))
    occluded_detections = tuple(detector.detect(occluded_bgr))

    present_matches = _matching_detections(
        present_detections,
        label=annotation.expected_label,
        bbox=annotation.bbox,
        minimum_iou=float(config.positive_min_iou),
    )
    qualifying_present = tuple(
        item
        for item in present_matches
        if item[0].confidence >= float(config.positive_min_confidence)
    )
    occluded_matches = _matching_detections(
        occluded_detections,
        label=annotation.expected_label,
        bbox=annotation.bbox,
        minimum_iou=float(config.positive_min_iou),
    )

    present_passed = bool(qualifying_present)
    blank_passed = not blank_detections
    occluded_passed = not occluded_matches
    gate_passed = present_passed and blank_passed and occluded_passed

    provider_names = tuple(str(item) for item in getattr(detector, "provider_names", ()))
    selection = provider_selection_document(getattr(detector, "provider_selection", None))
    model_manifest_sha256 = sha256_file(manifest_path)
    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "single_frame_quarantined_candidate_three_control_smoke",
        "model_identity": {
            "candidate": True,
            "raw_ultralytics_onnx": True,
            "native_output_format": RAW_YOLO_NATIVE_OUTPUT,
            "manifest_verified": True,
            "manifest_status": verified_model.status,
            "quarantine_enforced": True,
            "model_id": verified_model.model_id,
            "model_version": verified_model.model_version,
            "model_role": verified_model.model_role,
            "artifact_sha256": verified_model.artifact_sha256,
            "manifest_sha256": model_manifest_sha256,
            "production_approved": False,
            "accuracy_validated": False,
        },
        "frame_identity": {
            "verified_before_inference": True,
            "frame_id": annotation.frame_id,
            "frame_sha256": frame_digest,
            "annotation_sha256": annotation.document_sha256,
            "width": width,
            "height": height,
            "pixel_format": "BGR24",
            "payload_bytes": int(frame_bgr.nbytes),
            "frame_set_manifest_supplied": frame_set.supplied,
            "frame_set_manifest_verified": frame_set.verified,
            "frame_set_manifest_sha256": frame_set.manifest_sha256,
            "selected_wire_record_sha256": frame_set.selected_wire_record_sha256,
        },
        "provenance": {
            "gazebo_pixels_attribution": (
                "hash_bound_persisted_gazebo_bgr24_frame_set"
                if frame_set.actual_gazebo_pixels_persisted
                else "not_proved_without_hash_bound_frame_set"
            ),
            "actual_gazebo_pixels_persisted": frame_set.actual_gazebo_pixels_persisted,
            "telemetry_causal_binding": frame_set.telemetry_causal_binding,
            "telemetry_causal_binding_status": (
                "hash_bound_wire_v3_nonfuture_sources_within_1_5s"
                if frame_set.telemetry_causal_binding
                else "not_proved"
            ),
            "scenario_id": frame_set.scenario_id,
            "scenario_manifest_hash": frame_set.scenario_manifest_hash,
            "sensor_binding_hash": frame_set.sensor_binding_hash,
            "wire_version": frame_set.wire_version,
            "sensor_stamp_ns": frame_set.selected_sensor_stamp_ns,
            "navsat_stamp_ns": frame_set.selected_navsat_stamp_ns,
            "odometry_stamp_ns": frame_set.selected_odometry_stamp_ns,
            "maximum_source_skew_ns": frame_set.maximum_source_skew_ns,
            "runtime_truth_loaded": frame_set.runtime_truth_loaded,
            "source_row_order": frame_set.source_row_order,
            "wire_row_order": frame_set.wire_row_order,
            "vertical_flip_applied": frame_set.vertical_flip_applied,
        },
        "truth_isolation": {
            "verified": True,
            "annotation_independent_of_model": True,
            "annotation_supplied_to_detector": False,
            "frame_set_runtime_truth_loaded": frame_set.runtime_truth_loaded,
            "gazebo_runtime_truth_isolation_verified": (
                frame_set.runtime_truth_loaded is False if frame_set.supplied else False
            ),
            "annotation_used_for": ["present_scoring", "occlusion_mask"],
            "detector_inputs": ["present_pixels", "zero_blank_pixels", "occluded_pixels"],
        },
        "execution_provider": {
            "requested": list(config.providers),
            "selected": list(provider_names),
            "selection": selection,
            "detector_instances": 1,
            "shared_session_across_controls": True,
            "inference_count": 3,
        },
        "thresholds": {
            "detector_confidence": float(config.confidence_threshold),
            "positive_min_confidence": float(config.positive_min_confidence),
            "positive_min_iou": float(config.positive_min_iou),
            "nms_iou": float(config.nms_iou_threshold),
            "maximum_detections": config.maximum_detections,
        },
        "annotation": {
            "frame_id": annotation.frame_id,
            "expected_label": annotation.expected_label,
            "normalized_bbox": list(annotation.bbox.rounded(9)),
            "independent_of_model": True,
            "source_kind": annotation.source_kind,
            "occlusion_fill_bgr": [114, 114, 114],
            "occlusion_pixel_bounds_xyxy": [left, top, right, bottom],
        },
        "controls": {
            "present": _control_document(
                present_detections,
                passed=present_passed,
                annotation=annotation,
                qualifying_matches=qualifying_present,
                failure_reason=(
                    None
                    if present_passed
                    else "expected label/confidence/IoU threshold was not met"
                ),
            ),
            "blank": _control_document(
                blank_detections,
                passed=blank_passed,
                annotation=annotation,
                qualifying_matches=(),
                failure_reason=(None if blank_passed else "blank image produced detections"),
            ),
            "occluded": _control_document(
                occluded_detections,
                passed=occluded_passed,
                annotation=annotation,
                qualifying_matches=occluded_matches,
                failure_reason=(
                    None
                    if occluded_passed
                    else "occluded image retained an annotation-matching expected target"
                ),
            ),
        },
        "production_approved": False,
        "accuracy_validated": False,
        "qgc_visual_acceptance": "not_claimed",
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": gate_passed,
    }
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def build_input_error_document(message: str) -> dict[str, Any]:
    """Return a bounded machine document for CLI input-contract failures."""

    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": "input_contract_error",
        "error": {"type": "CandidateDetectorControlsInputError", "message": str(message)},
        "model_identity": {
            "candidate": True,
            "raw_ultralytics_onnx": True,
            "manifest_verified": False,
            "quarantine_enforced": False,
            "production_approved": False,
            "accuracy_validated": False,
        },
        "provenance": {
            "gazebo_pixels_attribution": "not_proved",
            "actual_gazebo_pixels_persisted": False,
            "telemetry_causal_binding": False,
            "telemetry_causal_binding_status": "not_proved",
        },
        "truth_isolation": {"verified": False, "annotation_supplied_to_detector": False},
        "qgc_visual_acceptance": "not_claimed",
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "production_approved": False,
        "accuracy_validated": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": False,
    }
    document["evidence_sha256"] = canonical_sha256(document)
    return document


def build_runtime_error_document(message: str, *, error_type: str) -> dict[str, Any]:
    """Return fail-closed machine evidence when session construction/inference fails."""

    document = build_input_error_document(message)
    without_hash = dict(document)
    without_hash.pop("evidence_sha256", None)
    without_hash["status"] = "runtime_error"
    without_hash["error"] = {"type": str(error_type), "message": str(message)}
    without_hash["evidence_sha256"] = canonical_sha256(without_hash)
    return without_hash


def write_evidence(path: str | Path, document: Mapping[str, Any]) -> Path:
    """Atomically persist one finite, canonicalizable JSON evidence document."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
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
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _regular_file(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise CandidateDetectorControlsInputError(
            f"{label} must be an existing regular, non-symlink file: {candidate}"
        )
    return candidate.resolve()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_bytes(), name=label)
    except (AutonomyContractError, OSError) as exc:
        raise CandidateDetectorControlsInputError(str(exc)) from exc
    if not isinstance(value, dict):
        raise CandidateDetectorControlsInputError(f"{label} must be a JSON object")
    return value


def _verify_candidate_manifest(
    manifest: Mapping[str, Any], manifest_path: Path, model_path: Path
) -> Any:
    if model_path.suffix.lower() != ".onnx":
        raise CandidateDetectorControlsInputError("candidate model artifact must be ONNX")
    verified = verify_model_manifest(
        manifest_path,
        model_path,
        expected_output_coordinates="letterbox_xyxy_px",
        expected_output_format="N_x_6",
        expected_native_output_format=RAW_YOLO_NATIVE_OUTPUT,
        require_production_approved=False,
    )
    if verified.status != "quarantined":
        raise CandidateDetectorControlsInputError(
            "candidate model manifest status must be exactly 'quarantined'"
        )
    governance = _mapping(manifest.get("governance"), "model manifest governance")
    if governance.get("production_approved") is not False or verified.production_approved:
        raise CandidateDetectorControlsInputError(
            "candidate model manifest production_approved must be exactly false"
        )
    source = _mapping(manifest.get("source"), "model manifest source")
    if source.get("provenance_review") != "pending":
        raise CandidateDetectorControlsInputError(
            "candidate model source provenance_review must remain pending"
        )
    native = _mapping(
        _mapping(manifest.get("output"), "model manifest output").get("native_export"),
        "model manifest native export",
    )
    if native.get("nms_embedded") is not False:
        raise CandidateDetectorControlsInputError(
            "raw candidate manifest must declare nms_embedded=false"
        )
    export = _mapping(manifest.get("export"), "model manifest export")
    if export.get("artifact_format") != "onnx":
        raise CandidateDetectorControlsInputError(
            "candidate model manifest export.artifact_format must be 'onnx'"
        )
    _manifest_input_shape(manifest)
    return verified


def _manifest_input_shape(manifest: Mapping[str, Any]) -> tuple[int, int, int, int]:
    input_contract = _mapping(manifest.get("input"), "model manifest input")
    if (
        input_contract.get("sensor") != "rgb"
        or input_contract.get("color_space") != "RGB"
        or input_contract.get("tensor_layout") != "NCHW"
        or input_contract.get("dtype") != "float32"
        or input_contract.get("resize") != "letterbox"
    ):
        raise CandidateDetectorControlsInputError(
            "candidate input must be rgb/RGB/NCHW/float32/letterbox"
        )
    raw_shape = input_contract.get("shape")
    if (
        not isinstance(raw_shape, list)
        or len(raw_shape) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in raw_shape)
        or raw_shape[0] != 1
        or raw_shape[1] != 3
        or raw_shape[2] <= 0
        or raw_shape[3] <= 0
    ):
        raise CandidateDetectorControlsInputError(
            "candidate input.shape must be [1, 3, positive_height, positive_width]"
        )
    return tuple(raw_shape)  # type: ignore[return-value]


def _load_annotation(path: Path) -> _Annotation:
    raw = _load_json_object(path, "annotation")
    if "schema" in raw and raw.get("schema") != ANNOTATION_SCHEMA:
        raise CandidateDetectorControlsInputError("annotation schema is unsupported")
    if "schema_version" in raw and raw.get("schema_version") != 1:
        raise CandidateDetectorControlsInputError("annotation schema_version is unsupported")
    if raw.get("independent_of_model") is not True:
        raise CandidateDetectorControlsInputError(
            "annotation independent_of_model must be exactly true"
        )
    for prohibited in (
        "derived_from_model",
        "model_generated",
        "model_assisted",
        "detector_generated",
    ):
        if prohibited in raw and raw.get(prohibited) is not False:
            raise CandidateDetectorControlsInputError(
                f"annotation {prohibited} must be absent or exactly false"
            )
    frame_id = _nonempty_text(raw.get("frame_id"), "annotation frame_id")
    frame_sha256 = _sha256_text(raw.get("frame_sha256"), "annotation frame_sha256")
    expected_label = _nonempty_text(raw.get("expected_label"), "annotation expected_label").lower()
    bbox_value = raw.get("normalized_bbox", raw.get("bbox_normalized"))
    bbox = _normalized_bbox(bbox_value)
    source_kind = (
        _nonempty_text(raw.get("source_kind"), "annotation source_kind")
        if "source_kind" in raw
        else "unspecified"
    )
    return _Annotation(
        frame_id=frame_id,
        frame_sha256=frame_sha256,
        expected_label=expected_label,
        bbox=bbox,
        document_sha256=sha256_file(path),
        source_kind=source_kind,
    )


def _verify_optional_annotation_geometry(
    raw: Mapping[str, Any], *, width: int, height: int
) -> None:
    for key, expected in (("width", width), ("height", height)):
        if key in raw and raw[key] != expected:
            raise CandidateDetectorControlsInputError(
                f"annotation {key} does not match selected frame geometry"
            )
    if "pixel_format" in raw and str(raw["pixel_format"]).upper() != "BGR24":
        raise CandidateDetectorControlsInputError(
            "annotation pixel_format must be BGR24 when supplied"
        )


def _load_frame_bgr(path: Path, *, width: int | None, height: int | None) -> tuple[Any, int, int]:
    np = _require_numpy()
    if path.suffix.lower() == ".bgr24":
        if width is None or height is None:
            raise CandidateDetectorControlsInputError(
                ".bgr24 input requires explicit positive --width and --height"
            )
        payload = path.read_bytes()
        expected = width * height * 3
        if len(payload) != expected:
            raise CandidateDetectorControlsInputError(
                f"raw BGR24 byte length mismatch: expected {expected}, received {len(payload)}"
            )
        image = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3)).copy()
        return image, width, height
    if width is not None or height is not None:
        raise CandidateDetectorControlsInputError(
            "--width/--height are reserved for raw .bgr24 input"
        )
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency-specific
        raise CandidateDetectorControlsInputError(
            "OpenCV is required to decode non-.bgr24 frames"
        ) from exc
    payload = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(payload, cv2.IMREAD_COLOR)
    if image is None or getattr(image, "ndim", 0) != 3 or image.shape[2] != 3:
        raise CandidateDetectorControlsInputError("frame is not a decodable three-channel image")
    frame_height, frame_width = (int(value) for value in image.shape[:2])
    return image, frame_width, frame_height


def _verify_frame_set_binding(
    path: Path | None,
    *,
    frame_path: Path,
    frame_id: str,
    frame_sha256: str,
    width: int,
    height: int,
) -> _FrameSetBinding:
    if path is None:
        return _FrameSetBinding(
            supplied=False,
            verified=False,
            manifest_sha256=None,
            scenario_id=None,
            scenario_manifest_hash=None,
            sensor_binding_hash=None,
            wire_version=None,
            actual_gazebo_pixels_persisted=False,
            telemetry_causal_binding=False,
            runtime_truth_loaded=None,
            selected_wire_record_sha256=None,
            selected_sensor_stamp_ns=None,
            selected_navsat_stamp_ns=None,
            selected_odometry_stamp_ns=None,
            maximum_source_skew_ns=None,
            source_row_order=None,
            wire_row_order=None,
            vertical_flip_applied=None,
        )

    raw = _load_json_object(path, "frame-set manifest")
    frame_set_schema_version = raw.get("schema_version")
    if raw.get("schema") != FRAME_SET_SCHEMA or frame_set_schema_version not in {1, 2}:
        raise CandidateDetectorControlsInputError("frame-set manifest schema is unsupported")
    claimed_manifest_sha = _sha256_text(raw.get("manifest_sha256"), "frame-set manifest_sha256")
    without_hash = dict(raw)
    without_hash.pop("manifest_sha256", None)
    if canonical_sha256(without_hash) != claimed_manifest_sha:
        raise CandidateDetectorControlsInputError("frame-set manifest_sha256 is invalid")
    if raw.get("wire_version") != 3 or raw.get("pixel_format") != "BGR24":
        raise CandidateDetectorControlsInputError(
            "frame-set manifest must bind wire_version=3 and pixel_format=BGR24"
        )
    scenario_id = _nonempty_text(raw.get("scenario_id"), "frame-set scenario_id")
    scenario_manifest_hash = _sha256_text(
        raw.get("scenario_manifest_hash"), "frame-set scenario_manifest_hash"
    )
    sensor_binding_hash = _sha256_text(
        raw.get("sensor_binding_hash"), "frame-set sensor_binding_hash"
    )
    source_attribution = _mapping(raw.get("source_attribution"), "frame-set source_attribution")
    required_source_attribution = {
        "pixels": "actual_gazebo_camera_sensor_output",
        "environment": "software_simulation",
        "wire": "MDGZSIM3",
        "truth_loaded_by_runtime": False,
    }
    if any(
        source_attribution.get(key) != expected
        for key, expected in required_source_attribution.items()
    ):
        raise CandidateDetectorControlsInputError(
            "frame-set source_attribution must preserve Gazebo/MDGZSIM3/truth-isolation identity"
        )
    row_order_contract = {
        "source_row_order": "top_to_bottom",
        "wire_row_order": "top_to_bottom",
        "vertical_flip_applied": False,
    }
    supplied_row_order_keys = row_order_contract.keys() & source_attribution.keys()
    if supplied_row_order_keys and (
        supplied_row_order_keys != row_order_contract.keys()
        or any(
            source_attribution.get(key) != expected for key, expected in row_order_contract.items()
        )
    ):
        raise CandidateDetectorControlsInputError(
            "frame-set row-order attribution must preserve top-to-bottom pixels without a flip"
        )
    if frame_set_schema_version == 2:
        for key, expected in (
            ("per_frame_wire_telemetry_persisted", True),
            ("wire_record_recomputed_from_persisted_bytes", True),
            ("camera_intrinsics_source", "repository_calibration_hash_bound"),
            ("terrain_source", "repository_dem_hash_bound"),
        ):
            if source_attribution.get(key) != expected:
                raise CandidateDetectorControlsInputError(
                    f"frame-set v2 source_attribution.{key} is invalid"
                )
        _mapping(raw.get("camera_calibration"), "frame-set camera_calibration")
        _mapping(raw.get("terrain_dem"), "frame-set terrain_dem")
        sensor_evidence = _mapping(
            raw.get("per_frame_sensor_evidence"),
            "frame-set per_frame_sensor_evidence",
        )
        if (
            sensor_evidence.get("record_count") != raw.get("frame_count")
            or sensor_evidence.get("gps_and_pose_values_covered_by_wire_record_sha256") is not True
            or sensor_evidence.get("derived_ned_values_recomputed_before_seal") is not True
            or sensor_evidence.get("attitude_covariance_record_count") != raw.get("frame_count")
            or sensor_evidence.get("derived_attitude_sigmas_persisted") is not True
            or sensor_evidence.get("attitude_uncertainty_source")
            != "gazebo_odometry_covariance_with_configured_floor"
        ):
            raise CandidateDetectorControlsInputError(
                "frame-set v2 per-frame sensor evidence contract is incomplete"
            )
    frames = raw.get("frames")
    if not isinstance(frames, list) or not frames:
        raise CandidateDetectorControlsInputError("frame-set frames must be a non-empty array")
    if raw.get("frame_count") != len(frames):
        raise CandidateDetectorControlsInputError("frame-set frame_count does not match frames")
    selected: Mapping[str, Any] | None = None
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    total_bytes = 0
    for index, item in enumerate(frames):
        row = _mapping(item, f"frame-set frames[{index}]")
        candidate_id = _nonempty_text(row.get("frame_id"), f"frames[{index}].frame_id")
        sequence = _nonnegative_int(row.get("sequence"), f"frames[{index}].sequence")
        if candidate_id in seen_ids or sequence in seen_sequences:
            raise CandidateDetectorControlsInputError(
                "frame-set frame_id and sequence values must be unique"
            )
        seen_ids.add(candidate_id)
        seen_sequences.add(sequence)
        total_bytes += _positive_int(row.get("bytes"), f"frames[{index}].bytes")
        if candidate_id == frame_id:
            if selected is not None:
                raise CandidateDetectorControlsInputError(
                    "frame-set contains more than one selected frame identity"
                )
            selected = row
    if raw.get("total_payload_bytes") != total_bytes:
        raise CandidateDetectorControlsInputError(
            "frame-set total_payload_bytes does not match frames"
        )
    if selected is None:
        raise CandidateDetectorControlsInputError(
            "annotation frame_id is absent from the supplied frame-set manifest"
        )

    filename = _nonempty_text(selected.get("file"), "selected frame file")
    if Path(filename).name != filename:
        raise CandidateDetectorControlsInputError("frame-set frame file must be a basename")
    expected_path = (path.parent / filename).resolve()
    if expected_path != frame_path.resolve():
        raise CandidateDetectorControlsInputError(
            "selected frame path does not match the frame-set manifest location"
        )
    selected_sha = _sha256_text(selected.get("sha256"), "selected frame sha256")
    wire_payload_sha = _sha256_text(
        selected.get("wire_payload_sha256"), "selected wire_payload_sha256"
    )
    if selected_sha != frame_sha256 or wire_payload_sha != frame_sha256:
        raise CandidateDetectorControlsInputError(
            "selected frame bytes do not match frame-set or wire payload identity"
        )
    if selected.get("raw_bytes_sha256_matches_wire_payload") is not True:
        raise CandidateDetectorControlsInputError(
            "frame-set did not prove raw bytes equal the wire payload"
        )
    if selected.get("bytes") != frame_path.stat().st_size:
        raise CandidateDetectorControlsInputError(
            "selected frame byte count does not match frame-set manifest"
        )
    wire_record_sha = _sha256_text(
        selected.get("wire_record_sha256"), "selected wire_record_sha256"
    )
    provenance = _mapping(selected.get("provenance"), "selected frame provenance")
    if provenance.get("frame_id") != frame_id or provenance.get("sequence") != selected.get(
        "sequence"
    ):
        raise CandidateDetectorControlsInputError(
            "selected frame identity does not match its provenance record"
        )
    if (
        _sha256_text(provenance.get("record_sha256"), "selected provenance record_sha256")
        != wire_record_sha
        or _sha256_text(provenance.get("payload_sha256"), "selected provenance payload_sha256")
        != frame_sha256
    ):
        raise CandidateDetectorControlsInputError(
            "selected frame hashes do not match provenance record/payload identity"
        )
    if (
        provenance.get("width") != width
        or provenance.get("height") != height
        or provenance.get("stride") != width * 3
        or provenance.get("payload_bytes") != width * height * 3
        or provenance.get("pixel_format") != "BGR24"
    ):
        raise CandidateDetectorControlsInputError(
            "selected frame geometry does not match frame-set provenance"
        )
    captured_at_s = provenance.get("captured_at_s")
    if (
        isinstance(captured_at_s, bool)
        or not isinstance(captured_at_s, (int, float))
        or not math.isfinite(float(captured_at_s))
        or float(captured_at_s) < 0.0
        or provenance.get("captured_at_clock") != "host_monotonic"
    ):
        raise CandidateDetectorControlsInputError(
            "selected frame capture clock must be finite host_monotonic"
        )
    sensor_stamp = _nonnegative_int(provenance.get("sensor_stamp_ns"), "selected sensor_stamp_ns")
    camera_info_stamp = _nonnegative_int(
        provenance.get("camera_info_stamp_ns"), "selected camera_info_stamp_ns"
    )
    navsat_stamp = _nonnegative_int(provenance.get("navsat_stamp_ns"), "selected navsat_stamp_ns")
    odometry_stamp = _nonnegative_int(
        provenance.get("odometry_stamp_ns"), "selected odometry_stamp_ns"
    )
    source_stamps = {
        "camera_info": camera_info_stamp,
        "navsat": navsat_stamp,
        "odometry": odometry_stamp,
    }
    skews: list[int] = []
    for source, source_stamp in source_stamps.items():
        skew = sensor_stamp - source_stamp
        if skew < 0 or (source != "camera_info" and skew > MAX_SOURCE_SKEW_NS):
            raise CandidateDetectorControlsInputError(
                f"selected {source} timestamp is future or dynamic telemetry is "
                "more than 1.5s behind the image"
            )
        declared = _declared_source_skew(provenance, source)
        if declared is None or declared != skew:
            raise CandidateDetectorControlsInputError(
                f"selected {source} source skew is missing or inconsistent"
            )
        if source != "camera_info":
            skews.append(skew)
    if frame_set_schema_version == 2:
        unsigned_header_hex = _nonempty_text(
            provenance.get("wire_unsigned_header_hex"),
            "selected wire_unsigned_header_hex",
        )
        try:
            wire = reconstruct_gazebo_sim_wire_frame(
                unsigned_header_hex=unsigned_header_hex,
                payload_bgr24=frame_path.read_bytes(),
                expected_sequence=int(selected["sequence"]),
                expected_width=width,
                expected_height=height,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise CandidateDetectorControlsInputError(
                f"selected frame wire record reconstruction failed: {exc}"
            ) from exc
        if wire.record_sha256 != wire_record_sha:
            raise CandidateDetectorControlsInputError(
                "selected frame reconstructed wire SHA-256 does not match the manifest"
            )
        wire_telemetry = _mapping(
            provenance.get("wire_telemetry"),
            "selected wire_telemetry",
        )
        if wire_telemetry != GazeboSimWireTelemetryProvenance.from_wire(wire).to_mapping():
            raise CandidateDetectorControlsInputError(
                "selected frame wire telemetry does not match its MDGZSIM3 header"
            )
        frame_telemetry = _mapping(
            provenance.get("frame_telemetry"),
            "selected frame_telemetry",
        )
        roll, pitch, heading, velocity_north, velocity_east, _velocity_down = (
            gazebo_sim_ned_frd_kinematics(wire)
        )
        derived_values = {
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
        if any(frame_telemetry.get(key) != expected for key, expected in derived_values.items()):
            raise CandidateDetectorControlsInputError(
                "selected frame derived NED/GPS telemetry does not match its wire record"
            )
        for key in (
            "altitude_agl_m",
            "gps_horizontal_accuracy_m",
            "gps_vertical_accuracy_m",
            "gps_observed_at_s",
            "attitude_observed_at_s",
            "velocity_observed_at_s",
            "altitude_observed_at_s",
        ):
            value = frame_telemetry.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise CandidateDetectorControlsInputError(
                    f"selected frame_telemetry.{key} must be finite and non-negative"
                )
    claims = _mapping(raw.get("claims"), "frame-set claims")
    if claims.get("actual_gazebo_pixels_persisted") is not True:
        raise CandidateDetectorControlsInputError(
            "frame-set claims must prove actual_gazebo_pixels_persisted=true"
        )
    for claim in (
        "qgc_rendering",
        "manual_visual_acceptance",
        "hardware",
        "detector_evaluation",
        "production_model_approval",
    ):
        if claims.get(claim) != "not_claimed":
            raise CandidateDetectorControlsInputError(
                f"frame-set claims.{claim} must remain 'not_claimed'"
            )

    return _FrameSetBinding(
        supplied=True,
        verified=True,
        manifest_sha256=claimed_manifest_sha,
        scenario_id=scenario_id,
        scenario_manifest_hash=scenario_manifest_hash,
        sensor_binding_hash=sensor_binding_hash,
        wire_version=3,
        actual_gazebo_pixels_persisted=True,
        telemetry_causal_binding=True,
        runtime_truth_loaded=False,
        selected_wire_record_sha256=wire_record_sha,
        selected_sensor_stamp_ns=sensor_stamp,
        selected_navsat_stamp_ns=navsat_stamp,
        selected_odometry_stamp_ns=odometry_stamp,
        maximum_source_skew_ns=max(skews),
        source_row_order=(
            str(source_attribution["source_row_order"])
            if "source_row_order" in source_attribution
            else None
        ),
        wire_row_order=(
            str(source_attribution["wire_row_order"])
            if "wire_row_order" in source_attribution
            else None
        ),
        vertical_flip_applied=(
            bool(source_attribution["vertical_flip_applied"])
            if "vertical_flip_applied" in source_attribution
            else None
        ),
    )


def _declared_source_skew(provenance: Mapping[str, Any], source: str) -> int | None:
    for key in (f"{source}_source_skew_ns", f"{source}_skew_ns"):
        if key in provenance:
            return _nonnegative_int(provenance.get(key), f"selected {key}")
    return None


def _normalized_bbox(value: Any) -> BoundingBox:
    if isinstance(value, Mapping):
        coordinates = tuple(value.get(key) for key in ("x1", "y1", "x2", "y2"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        coordinates = tuple(value)
    else:
        raise CandidateDetectorControlsInputError(
            "annotation normalized_bbox must be [x1,y1,x2,y2] or an XYXY object"
        )
    if len(coordinates) != 4 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in coordinates
    ):
        raise CandidateDetectorControlsInputError(
            "annotation normalized_bbox must contain four numeric coordinates"
        )
    try:
        return BoundingBox(*(float(item) for item in coordinates))
    except ValueError as exc:
        raise CandidateDetectorControlsInputError(str(exc)) from exc


def _bbox_pixel_bounds(bbox: BoundingBox, *, width: int, height: int) -> tuple[int, int, int, int]:
    left = max(0, min(width - 1, math.floor(bbox.x1 * width)))
    top = max(0, min(height - 1, math.floor(bbox.y1 * height)))
    right = max(left + 1, min(width, math.ceil(bbox.x2 * width)))
    bottom = max(top + 1, min(height, math.ceil(bbox.y2 * height)))
    return left, top, right, bottom


def _matching_detections(
    detections: Sequence[Detection],
    *,
    label: str,
    bbox: BoundingBox,
    minimum_iou: float,
) -> tuple[tuple[Detection, float], ...]:
    matches = [
        (detection, detection.bbox.iou(bbox))
        for detection in detections
        if detection.label == label and detection.bbox.iou(bbox) >= minimum_iou
    ]
    matches.sort(key=lambda item: (item[0].confidence, item[1]), reverse=True)
    return tuple(matches)


def _control_document(
    detections: Sequence[Detection],
    *,
    passed: bool,
    annotation: _Annotation,
    qualifying_matches: Sequence[tuple[Detection, float]],
    failure_reason: str | None,
) -> dict[str, Any]:
    return {
        "passed": passed,
        "failure_reason": failure_reason,
        "detection_count": len(detections),
        "expected_label": annotation.expected_label,
        "qualifying_expected_match_count": len(qualifying_matches),
        "best_expected_match": (
            _detection_document(qualifying_matches[0][0], iou=qualifying_matches[0][1])
            if qualifying_matches
            else None
        ),
        "detections": [_detection_document(item) for item in detections],
    }


def _detection_document(detection: Detection, *, iou: float | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": detection.label,
        "confidence": detection.confidence,
        "normalized_bbox": list(detection.bbox.rounded(9)),
        "model_version": detection.model_version,
    }
    if iou is not None:
        result["annotation_iou"] = iou
    return result


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency-specific
        raise CandidateDetectorControlsInputError(
            "numpy is required for detector controls"
        ) from exc
    return np


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CandidateDetectorControlsInputError(f"{name} must be an object")
    return value


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateDetectorControlsInputError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256_text(value: Any, name: str) -> str:
    text = _nonempty_text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CandidateDetectorControlsInputError(f"{name} must be 64 lowercase hex characters")
    return text


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CandidateDetectorControlsInputError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CandidateDetectorControlsInputError(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "ANNOTATION_SCHEMA",
    "CandidateDetectorControlsConfig",
    "CandidateDetectorControlsInputError",
    "EVIDENCE_SCHEMA",
    "EVIDENCE_SCHEMA_VERSION",
    "FRAME_SET_SCHEMA",
    "build_input_error_document",
    "build_runtime_error_document",
    "evaluate_candidate_detector_controls",
    "write_evidence",
]

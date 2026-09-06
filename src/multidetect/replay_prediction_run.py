"""Fixed-model prediction materialization for reviewed deployment replay sets."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_sha256,
    require_text,
    strict_json_loads,
)
from .backends.raw_replay import RawSensorReplayRecording, ReplayFrameSource
from .clock import ManualClock
from .evaluation import JsonlPredictionWriter, load_prediction_jsonl
from .inference_provider import provider_selection_document
from .model_manifest import ModelManifestError, sha256_file, verify_model_manifest
from .replay_annotation_workspace import verify_finalized_replay_annotations
from .replay_set import REPLAY_SET_STRATA, build_replay_set_manifest, load_replay_set_manifest
from .vision import OnnxNx6Config, OnnxNx6Detector, OnnxRawYoloConfig, OnnxRawYoloDetector

PREDICTION_RECEIPT_NAME = "prediction-run.receipt.json"
ANNOTATION_MANIFEST_NAME = "annotation-source.replay-set.manifest.json"
_MODEL_FORMATS = {
    "post_nms_nx6": "post_nms_N_x_6",
    "ultralytics_raw": "ultralytics_raw_xywh_class_scores",
}


def run_replay_predictions(
    reviewed_root: str | Path,
    output: str | Path,
    *,
    model_path: str | Path,
    model_manifest_path: str | Path,
    model_format: str = "auto",
    confidence_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    maximum_detections: int = 300,
    providers: Sequence[str] = (),
    warmup_iterations: int = 1,
    detector_factory: Callable[[object], object] | None = None,
    image_decoder: Callable[[bytes, object], object] | None = None,
) -> dict[str, object]:
    """Run one verified model over all reviewed frames into a new atomic bundle.

    ``detector_factory`` and ``image_decoder`` are dependency-injection seams for
    deterministic tests.  CLI callers never expose them, and the receipt records
    whether either seam was used.
    """

    source = _regular_directory(reviewed_root, "reviewed replay root")
    annotation_report = verify_finalized_replay_annotations(source)
    source_manifest_object = load_replay_set_manifest(
        source / "replay-set.manifest.json", require_complete=True
    )
    artifact = _regular_file(model_path, "model artifact")
    manifest_file = _regular_file(model_manifest_path, "model manifest")
    manifest = _json_object(manifest_file, "model manifest")
    selected_format = _resolve_model_format(model_format, manifest)
    native_format = _MODEL_FORMATS[selected_format]
    output_coordinates = _manifest_output_coordinates(manifest)
    if selected_format == "ultralytics_raw" and output_coordinates != "letterbox_xyxy_px":
        raise AutonomyContractError("raw Ultralytics replay inference requires letterbox_xyxy_px")
    try:
        verified_model = verify_model_manifest(
            manifest_file,
            artifact,
            expected_output_coordinates=output_coordinates,
            expected_output_format="N_x_6",
            expected_native_output_format=native_format,
            require_production_approved=False,
        )
    except ModelManifestError as exc:
        raise AutonomyContractError(str(exc)) from exc
    if _canonical_classes(verified_model.class_names) != source_manifest_object.class_names:
        raise AutonomyContractError(
            "canonical model classes do not exactly match reviewed replay class_names"
        )
    input_height, input_width = _manifest_input_dimensions(manifest)
    confidence = _ratio(confidence_threshold, "confidence_threshold")
    iou = _ratio(iou_threshold, "iou_threshold")
    maximum = _positive_int(maximum_detections, "maximum_detections")
    warmups = _nonnegative_int(warmup_iterations, "warmup_iterations")
    requested_providers = tuple(require_text(item, "provider").strip() for item in providers)
    if len(requested_providers) != len(set(requested_providers)):
        raise AutonomyContractError("providers must be unique")

    output_path = Path(output).absolute()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise AutonomyContractError(f"prediction output already exists: {output_path}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=output_path.parent))
    test_double_used = detector_factory is not None or image_decoder is not None
    try:
        _copy_reviewed_bundle(source, temporary)
        model_directory = temporary / "model"
        model_directory.mkdir()
        copied_model = model_directory / f"artifact{artifact.suffix.lower()}"
        copied_manifest = model_directory / "manifest.json"
        shutil.copyfile(artifact, copied_model)
        shutil.copyfile(manifest_file, copied_manifest)

        config: object
        if selected_format == "ultralytics_raw":
            config = OnnxRawYoloConfig(
                model_path=copied_model,
                class_names=verified_model.class_names,
                input_width=input_width,
                input_height=input_height,
                confidence_threshold=confidence,
                iou_threshold=iou,
                maximum_detections=maximum,
                providers=requested_providers,
                model_version=verified_model.model_version,
            )
        else:
            config = OnnxNx6Config(
                model_path=copied_model,
                class_names=verified_model.class_names,
                input_width=input_width,
                input_height=input_height,
                confidence_threshold=confidence,
                providers=requested_providers,
                model_version=verified_model.model_version,
                output_coordinates=output_coordinates,
            )
        detector = (
            detector_factory(config)
            if detector_factory is not None
            else _build_detector(selected_format, config)
        )
        actual_warmups = 0
        if warmups and callable(getattr(detector, "warmup", None)):
            detector.warmup(iterations=warmups)
            actual_warmups = warmups

        prediction_artifacts: list[dict[str, object]] = []
        all_latencies: list[float] = []
        for stratum in REPLAY_SET_STRATA:
            recording_path = temporary / stratum / "recording.manifest.json"
            recording = RawSensorReplayRecording.from_manifest(recording_path)
            clock = ManualClock(initial_utc=recording.started_at_utc)
            frame_source = ReplayFrameSource(recording, clock, image_decoder=image_decoder)
            prediction_path = temporary / stratum / "predictions.jsonl"
            writer = JsonlPredictionWriter(prediction_path)
            latencies: list[float] = []
            try:
                for frame in frame_source:
                    started_ns = time.perf_counter_ns()
                    detections = tuple(detector.detect(frame.image_bgr))
                    unknown_labels = sorted(
                        {item.label for item in detections}
                        - set(source_manifest_object.class_names)
                    )
                    if unknown_labels:
                        raise AutonomyContractError(
                            "detector emitted labels outside reviewed replay class_names: "
                            + ", ".join(unknown_labels)
                        )
                    latency_ms = max(0.0, (time.perf_counter_ns() - started_ns) / 1_000_000.0)
                    writer.append(
                        frame_id=frame.frame_id,
                        captured_at_s=frame.captured_at_s,
                        detections=detections,
                        inference_latency_ms=latency_ms,
                    )
                    latencies.append(latency_ms)
            finally:
                writer.close()
                frame_source.close()
            payload = prediction_path.read_bytes()
            prediction_artifacts.append(
                {
                    "stratum": stratum,
                    "path": f"{stratum}/predictions.jsonl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "frame_count": len(latencies),
                    "latency": _latency_summary(latencies),
                }
            )
            all_latencies.extend(latencies)

        source_manifest = temporary / ANNOTATION_MANIFEST_NAME
        source_manifest_bytes = source_manifest.read_bytes()
        source_manifest_object = load_replay_set_manifest(source_manifest, require_complete=True)
        replay_manifest_path = temporary / "replay-set.manifest.json"
        replay_manifest = build_replay_set_manifest(
            temporary,
            replay_manifest_path,
            dataset_id=source_manifest_object.dataset_id,
            dataset_version=source_manifest_object.dataset_version,
            class_names=source_manifest_object.class_names,
            source_kind="deployment_recording",
            annotations_reviewed=True,
        )
        selection = provider_selection_document(getattr(detector, "provider_selection", None))
        receipt: dict[str, object] = {
            "schema_version": 1,
            "event": "replay_predictions_materialized",
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "annotation_source": {
                "review_receipt_canonical_hash": annotation_report["canonical_hash"],
                "manifest_path": ANNOTATION_MANIFEST_NAME,
                "manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
                "manifest_canonical_hash": source_manifest_object.canonical_hash,
            },
            "model": {
                "artifact_path": copied_model.relative_to(temporary).as_posix(),
                "artifact_sha256": verified_model.artifact_sha256,
                "artifact_bytes": copied_model.stat().st_size,
                "manifest_path": copied_manifest.relative_to(temporary).as_posix(),
                "manifest_sha256": sha256_file(copied_manifest),
                "model_id": verified_model.model_id,
                "model_version": verified_model.model_version,
                "status": verified_model.status,
                "production_approved": verified_model.production_approved,
                "synthetic_hil_only": verified_model.synthetic_hil_only,
                "model_role": verified_model.model_role,
                "intended_use": verified_model.intended_use,
                "class_names": list(verified_model.class_names),
                "output_coordinates": verified_model.output_coordinates,
                "output_format": verified_model.output_format,
                "native_output_format": native_format,
            },
            "inference": {
                "model_format": selected_format,
                "input_width": input_width,
                "input_height": input_height,
                "confidence_threshold": confidence,
                "host_nms_applied": selected_format == "ultralytics_raw",
                "iou_threshold": iou if selected_format == "ultralytics_raw" else None,
                "maximum_detections": maximum if selected_format == "ultralytics_raw" else None,
                "warmup_iterations": actual_warmups,
                "requested_providers": list(requested_providers),
                "active_providers": list(getattr(detector, "provider_names", ())),
                "provider_selection": selection,
                "shared_runtime_pipeline": True,
                "test_double_used": test_double_used,
            },
            "runtime": _runtime_versions(),
            "prediction_artifacts": prediction_artifacts,
            "latency": _latency_summary(all_latencies),
            "replay_manifest_sha256": sha256_file(replay_manifest_path),
            "replay_manifest_canonical_hash": replay_manifest.canonical_hash,
            "claims": {
                "annotations_reviewed": True,
                "predictions_complete": True,
                "runtime_inference_executed": not test_double_used,
                "accuracy_validated": False,
                "release_ready": False,
            },
        }
        receipt["canonical_hash"] = canonical_sha256(receipt)
        _write_json(temporary / PREDICTION_RECEIPT_NAME, receipt)
        verify_replay_predictions(temporary)
        if output_path.exists() or output_path.is_symlink():
            raise AutonomyContractError(f"prediction output already exists: {output_path}")
        os.rename(temporary, output_path)
        return verify_replay_predictions(output_path)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_replay_predictions(root: str | Path) -> dict[str, object]:
    """Reopen the annotation, model, prediction, and manifest provenance chain."""

    root_path = _regular_directory(root, "prediction bundle")
    annotation = verify_finalized_replay_annotations(
        root_path, replay_manifest_name=ANNOTATION_MANIFEST_NAME
    )
    receipt = _json_object(root_path / PREDICTION_RECEIPT_NAME, "prediction receipt")
    required = {
        "schema_version",
        "event",
        "generated_at_utc",
        "annotation_source",
        "model",
        "inference",
        "runtime",
        "prediction_artifacts",
        "latency",
        "replay_manifest_sha256",
        "replay_manifest_canonical_hash",
        "claims",
        "canonical_hash",
    }
    if set(receipt) != required or receipt["schema_version"] != 1:
        raise AutonomyContractError("prediction receipt has unknown/missing fields or schema")
    unsigned = dict(receipt)
    observed_canonical = require_sha256(unsigned.pop("canonical_hash"), "canonical_hash")
    if canonical_sha256(unsigned) != observed_canonical:
        raise AutonomyContractError("prediction receipt canonical_hash mismatch")
    if receipt["event"] != "replay_predictions_materialized":
        raise AutonomyContractError("prediction receipt event is invalid")
    _utc_timestamp(receipt["generated_at_utc"], "generated_at_utc")

    source = _mapping(receipt["annotation_source"], "annotation_source")
    expected_source_fields = {
        "review_receipt_canonical_hash",
        "manifest_path",
        "manifest_sha256",
        "manifest_canonical_hash",
    }
    if set(source) != expected_source_fields:
        raise AutonomyContractError("annotation_source inventory is invalid")
    if source["review_receipt_canonical_hash"] != annotation["canonical_hash"]:
        raise AutonomyContractError("prediction annotation receipt binding mismatch")
    annotation_manifest = _contained_file(root_path, source["manifest_path"], "annotation manifest")
    annotation_manifest_object = load_replay_set_manifest(
        annotation_manifest, require_complete=True
    )
    if sha256_file(annotation_manifest) != require_sha256(
        source["manifest_sha256"], "manifest_sha256"
    ) or annotation_manifest_object.canonical_hash != require_sha256(
        source["manifest_canonical_hash"], "manifest_canonical_hash"
    ):
        raise AutonomyContractError("prediction annotation manifest binding mismatch")

    model = _mapping(receipt["model"], "model")
    model_fields = {
        "artifact_path",
        "artifact_sha256",
        "artifact_bytes",
        "manifest_path",
        "manifest_sha256",
        "model_id",
        "model_version",
        "status",
        "production_approved",
        "synthetic_hil_only",
        "model_role",
        "intended_use",
        "class_names",
        "output_coordinates",
        "output_format",
        "native_output_format",
    }
    if set(model) != model_fields:
        raise AutonomyContractError("prediction model inventory is invalid")
    model_artifact = _contained_file(root_path, model["artifact_path"], "model artifact")
    model_manifest = _contained_file(root_path, model["manifest_path"], "model manifest")
    if (
        sha256_file(model_artifact) != require_sha256(model["artifact_sha256"], "artifact_sha256")
        or model_artifact.stat().st_size
        != require_int(model["artifact_bytes"], "artifact_bytes", minimum=1)
        or sha256_file(model_manifest)
        != require_sha256(model["manifest_sha256"], "model manifest sha256")
    ):
        raise AutonomyContractError("prediction model artifact binding mismatch")
    inference = _mapping(receipt["inference"], "inference")
    inference_fields = {
        "model_format",
        "input_width",
        "input_height",
        "confidence_threshold",
        "host_nms_applied",
        "iou_threshold",
        "maximum_detections",
        "warmup_iterations",
        "requested_providers",
        "active_providers",
        "provider_selection",
        "shared_runtime_pipeline",
        "test_double_used",
    }
    if set(inference) != inference_fields:
        raise AutonomyContractError("prediction inference inventory is invalid")
    model_format = require_text(inference.get("model_format"), "model_format")
    if (
        model_format not in _MODEL_FORMATS
        or model["native_output_format"] != _MODEL_FORMATS[model_format]
    ):
        raise AutonomyContractError("prediction model format binding mismatch")
    try:
        verified_model = verify_model_manifest(
            model_manifest,
            model_artifact,
            expected_output_coordinates=model["output_coordinates"],
            expected_output_format="N_x_6",
            expected_native_output_format=model["native_output_format"],
            require_production_approved=False,
        )
    except ModelManifestError as exc:
        raise AutonomyContractError(str(exc)) from exc
    observed_identity = {
        "model_id": verified_model.model_id,
        "model_version": verified_model.model_version,
        "status": verified_model.status,
        "production_approved": verified_model.production_approved,
        "synthetic_hil_only": verified_model.synthetic_hil_only,
        "model_role": verified_model.model_role,
        "intended_use": verified_model.intended_use,
        "class_names": list(verified_model.class_names),
        "output_coordinates": verified_model.output_coordinates,
        "output_format": verified_model.output_format,
    }
    if any(model[key] != value for key, value in observed_identity.items()):
        raise AutonomyContractError("prediction model identity mismatch")
    manifest_document = _json_object(model_manifest, "model manifest")
    height, width = _manifest_input_dimensions(manifest_document)
    if inference.get("input_width") != width or inference.get("input_height") != height:
        raise AutonomyContractError("prediction input dimensions do not match model manifest")
    test_double_used = inference.get("test_double_used")
    if type(test_double_used) is not bool or inference.get("shared_runtime_pipeline") is not True:
        raise AutonomyContractError("prediction inference provenance is invalid")
    _ratio(inference.get("confidence_threshold"), "confidence_threshold")
    host_nms = model_format == "ultralytics_raw"
    if inference.get("host_nms_applied") is not host_nms:
        raise AutonomyContractError("prediction host NMS provenance is invalid")
    if host_nms:
        _ratio(inference.get("iou_threshold"), "iou_threshold")
        _positive_int(inference.get("maximum_detections"), "maximum_detections")
    elif (
        inference.get("iou_threshold") is not None
        or inference.get("maximum_detections") is not None
    ):
        raise AutonomyContractError("embedded-NMS predictions must not claim host NMS settings")
    _nonnegative_int(inference.get("warmup_iterations"), "warmup_iterations")
    requested = inference.get("requested_providers")
    active = inference.get("active_providers")
    if not isinstance(requested, list) or not isinstance(active, list):
        raise AutonomyContractError("prediction provider inventory is invalid")
    for name, values in (("requested_providers", requested), ("active_providers", active)):
        normalized = [require_text(item, name) for item in values]
        if len(normalized) != len(set(normalized)):
            raise AutonomyContractError(f"{name} must be unique")

    artifacts = receipt["prediction_artifacts"]
    if not isinstance(artifacts, list) or [
        item.get("stratum") for item in artifacts if isinstance(item, dict)
    ] != list(REPLAY_SET_STRATA):
        raise AutonomyContractError("prediction artifact inventory is invalid")
    all_latencies: list[float] = []
    total_frames = 0
    for item in artifacts:
        entry = _mapping(item, "prediction artifact")
        if set(entry) != {"stratum", "path", "sha256", "bytes", "frame_count", "latency"}:
            raise AutonomyContractError("prediction artifact entry is invalid")
        stratum = require_text(entry["stratum"], "stratum")
        expected_path = f"{stratum}/predictions.jsonl"
        if entry["path"] != expected_path:
            raise AutonomyContractError("prediction artifact path is invalid")
        path = _contained_file(root_path, entry["path"], "prediction artifact")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != require_sha256(
            entry["sha256"], "prediction sha256"
        ) or len(payload) != require_int(entry["bytes"], "prediction bytes", minimum=1):
            raise AutonomyContractError(f"prediction artifact hash mismatch: {expected_path}")
        predictions = load_prediction_jsonl(path)
        recording = RawSensorReplayRecording.from_manifest(
            root_path / stratum / "recording.manifest.json"
        )
        if tuple(frame.frame_id for frame in predictions) != tuple(
            frame.frame_id for frame in recording.frames
        ):
            raise AutonomyContractError(f"prediction frame IDs do not match recording: {stratum}")
        if len(predictions) != require_int(entry["frame_count"], "frame_count", minimum=1):
            raise AutonomyContractError("prediction frame count mismatch")
        unknown_labels = sorted(
            {detection.label for frame in predictions for detection in frame.detections}
            - set(annotation_manifest_object.class_names)
        )
        if unknown_labels:
            raise AutonomyContractError(
                "prediction labels are outside reviewed replay class_names: "
                + ", ".join(unknown_labels)
            )
        latencies = [frame.inference_latency_ms for frame in predictions]
        if _mapping(entry["latency"], "latency") != _latency_summary(latencies):
            raise AutonomyContractError("prediction latency summary mismatch")
        all_latencies.extend(latencies)
        total_frames += len(predictions)
    if _mapping(receipt["latency"], "latency") != _latency_summary(all_latencies):
        raise AutonomyContractError("aggregate prediction latency summary mismatch")

    replay_manifest_path = root_path / "replay-set.manifest.json"
    replay_manifest = load_replay_set_manifest(replay_manifest_path, require_complete=True)
    if _canonical_classes(verified_model.class_names) != replay_manifest.class_names:
        raise AutonomyContractError("prediction model/replay class_names binding mismatch")
    if (
        sha256_file(replay_manifest_path)
        != require_sha256(receipt["replay_manifest_sha256"], "replay_manifest_sha256")
        or replay_manifest.canonical_hash
        != require_sha256(
            receipt["replay_manifest_canonical_hash"], "replay_manifest_canonical_hash"
        )
        or replay_manifest.claims.get("contains_predictions") is not True
    ):
        raise AutonomyContractError("prediction replay manifest binding mismatch")
    expected_claims = {
        "annotations_reviewed": True,
        "predictions_complete": True,
        "runtime_inference_executed": not test_double_used,
        "accuracy_validated": False,
        "release_ready": False,
    }
    if receipt["claims"] != expected_claims:
        raise AutonomyContractError("prediction receipt claims are invalid")
    return {
        "event": "replay_predictions_verified",
        "output": str(root_path),
        "frame_count": total_frames,
        "model_id": verified_model.model_id,
        "model_version": verified_model.model_version,
        "model_artifact_sha256": verified_model.artifact_sha256,
        "model_manifest_sha256": require_sha256(model["manifest_sha256"], "model manifest sha256"),
        "synthetic_hil_only": verified_model.synthetic_hil_only,
        "predictions_complete": True,
        "runtime_inference_executed": not test_double_used,
        "accuracy_validated": False,
        "release_ready": False,
        "canonical_hash": observed_canonical,
    }


def _build_detector(model_format: str, config: object) -> object:
    if model_format == "ultralytics_raw":
        if not isinstance(config, OnnxRawYoloConfig):
            raise TypeError("raw detector config is invalid")
        return OnnxRawYoloDetector(config)
    if not isinstance(config, OnnxNx6Config):
        raise TypeError("Nx6 detector config is invalid")
    return OnnxNx6Detector(config)


def _copy_reviewed_bundle(source: Path, destination: Path) -> None:
    intake = _json_object(source / "intake.receipt.json", "intake receipt")
    review = _json_object(source / "annotation-review.receipt.json", "annotation receipt")
    relative_paths = {
        "intake.receipt.json",
        "annotation-review.receipt.json",
        "annotation-workspace.reviewed.json",
    }
    for entry in intake.get("entries", []):
        for artifact in entry.get("artifacts", []):
            relative_paths.add(_relative_path(artifact["path"], "intake artifact path"))
    for artifact in review.get("truth_artifacts", []):
        relative_paths.add(_relative_path(artifact["path"], "truth artifact path"))
    for relative in sorted(relative_paths):
        source_file = _contained_file(source, relative, "reviewed artifact")
        target = destination / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
    shutil.copyfile(source / "replay-set.manifest.json", destination / ANNOTATION_MANIFEST_NAME)


def _resolve_model_format(requested: str, manifest: Mapping[str, object]) -> str:
    native = _mapping(_mapping(manifest.get("output"), "output").get("native_export"), "native")
    native_format = require_text(native.get("format"), "native output format")
    inferred = next((key for key, value in _MODEL_FORMATS.items() if value == native_format), None)
    if inferred is None:
        raise AutonomyContractError(f"unsupported model native output format: {native_format}")
    normalized = require_text(requested, "model_format").lower()
    if normalized == "auto":
        return inferred
    if normalized not in _MODEL_FORMATS or normalized != inferred:
        raise AutonomyContractError("model_format does not match model manifest")
    return normalized


def _manifest_input_dimensions(manifest: Mapping[str, object]) -> tuple[int, int]:
    contract = _mapping(manifest.get("input"), "model input")
    if (
        contract.get("sensor") != "rgb"
        or contract.get("color_space") != "RGB"
        or contract.get("tensor_layout") != "NCHW"
        or contract.get("dtype") != "float32"
        or contract.get("resize") != "letterbox"
    ):
        raise AutonomyContractError("model input must be rgb/RGB/NCHW/float32/letterbox")
    shape = contract.get("shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in shape)
        or shape[:2] != [1, 3]
        or shape[2] <= 0
        or shape[3] <= 0
    ):
        raise AutonomyContractError("model input.shape must be [1,3,height,width]")
    return shape[2], shape[3]


def _manifest_output_coordinates(manifest: Mapping[str, object]) -> str:
    output = _mapping(manifest.get("output"), "model output")
    adapter = _mapping(output.get("adapter_contract"), "adapter contract")
    return require_text(adapter.get("box_format"), "box_format")


def _canonical_classes(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        "flame" if value.strip().lower() == "fire" else value.strip().lower() for value in values
    )


def _utc_timestamp(value: object, name: str) -> str:
    text = require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyContractError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AutonomyContractError(f"{name} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_versions() -> dict[str, object]:
    def version(name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable_name": Path(sys.executable).name,
        "packages": {
            "multidetect": version("multidetect"),
            "numpy": version("numpy"),
            "opencv-python": version("opencv-python"),
            "onnxruntime": version("onnxruntime"),
            "onnxruntime-gpu": version("onnxruntime-gpu"),
        },
    }


def _latency_summary(values: Sequence[float]) -> dict[str, object]:
    if not values:
        raise AutonomyContractError("latency summary requires at least one frame")
    normalized = sorted(float(item) for item in values)
    if any(not math.isfinite(item) or item < 0 for item in normalized):
        raise AutonomyContractError("latencies must be finite and non-negative")
    p50 = normalized[math.ceil(0.50 * len(normalized)) - 1]
    p95 = normalized[math.ceil(0.95 * len(normalized)) - 1]
    return {
        "sample_count": len(normalized),
        "p50_ms": p50,
        "p95_ms": p95,
        "maximum_ms": normalized[-1],
        "total_ms": sum(normalized),
    }


def _ratio(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AutonomyContractError(f"{name} must be a finite number in [0,1]")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise AutonomyContractError(f"{name} must be a finite number in [0,1]")
    return normalized


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AutonomyContractError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AutonomyContractError(f"{name} must be a non-negative integer")
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AutonomyContractError(f"{name} must be an object")
    return value


def _json_object(path: str | Path, name: str) -> dict[str, Any]:
    source = _regular_file(path, name)
    value = strict_json_loads(source.read_bytes(), name=name)
    if not isinstance(value, dict):
        raise AutonomyContractError(f"{name} must be an object")
    return value


def _relative_path(value: object, name: str) -> str:
    text = require_text(value, name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise AutonomyContractError(f"{name} must be a contained relative path")
    return path.as_posix()


def _contained_file(root: Path, value: object, name: str) -> Path:
    relative = _relative_path(value, name)
    path = root / Path(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise AutonomyContractError(f"{name} must be an existing regular file: {relative}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise AutonomyContractError(f"{name} escapes bundle root") from exc
    return resolved


def _regular_file(value: str | Path, name: str) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise AutonomyContractError(f"{name} must be an existing regular file: {path}")
    return path.resolve(strict=True)


def _regular_directory(value: str | Path, name: str) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise AutonomyContractError(f"{name} must be an existing non-symlink directory: {path}")
    return path.resolve(strict=True)


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise AutonomyContractError(f"output already exists: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                document, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["run_replay_predictions", "verify_replay_predictions"]

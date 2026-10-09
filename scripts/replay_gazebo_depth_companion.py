"""Replay a sealed Gazebo RGB/Depth companion set through the metric-depth model.

This is an offline, truth-free-at-runtime analysis tool.  The Gazebo depth
payload is used only as an explicitly labelled software-SIM comparison
reference; it is never converted into a field calibration document and never
changes the runtime model configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort

# Keep direct CLI invocation reproducible without requiring an editable install
# or a caller-managed PYTHONPATH, matching the repository's verifier scripts.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402


class DepthCompanionReplayError(ValueError):
    """Raised when a sealed companion set cannot be replayed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DepthCompanionReplayError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DepthCompanionReplayError(f"manifest root must be an object: {path}")
    return value


def _verify_manifest(path: Path) -> dict[str, Any]:
    document = _json(path)
    declared = document.get("manifest_sha256")
    if not isinstance(declared, str) or len(declared) != 64:
        raise DepthCompanionReplayError(f"manifest_sha256 is missing: {path}")
    unsigned = dict(document)
    unsigned.pop("manifest_sha256", None)
    if canonical_sha256(unsigned) != declared.lower():
        raise DepthCompanionReplayError(f"manifest canonical hash mismatch: {path}")
    if document.get("frame_count") != len(document.get("frames", [])):
        raise DepthCompanionReplayError(f"manifest frame count mismatch: {path}")
    return document


def _resolve_child(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise DepthCompanionReplayError(f"{label} must be a file name")
    resolved = (root / value).resolve()
    if resolved.parent != root.resolve():
        raise DepthCompanionReplayError(f"{label} escapes its manifest directory")
    return resolved


def _valid_values(values: np.ndarray) -> np.ndarray:
    return values[np.isfinite(values) & (values > 0.0)]


def _center(values: np.ndarray, fraction: float = 0.5) -> np.ndarray:
    height, width = values.shape[:2]
    y_margin = int(height * (1.0 - fraction) / 2.0)
    x_margin = int(width * (1.0 - fraction) / 2.0)
    return values[y_margin : height - y_margin, x_margin : width - x_margin]


def _stats(values: np.ndarray) -> dict[str, float | int | None]:
    valid = _valid_values(values.astype(np.float32, copy=False))
    if valid.size == 0:
        return {"valid_count": 0, "median_m": None, "minimum_m": None, "maximum_m": None}
    return {
        "valid_count": int(valid.size),
        "median_m": float(np.median(valid)),
        "minimum_m": float(np.min(valid)),
        "maximum_m": float(np.max(valid)),
    }


def _model_depth(session: ort.InferenceSession, bgr: np.ndarray) -> np.ndarray:
    input_meta = session.get_inputs()[0]
    shape = input_meta.shape
    if len(shape) != 4 or shape[0] != 1 or shape[1] != 3:
        raise DepthCompanionReplayError(f"unsupported model input shape: {shape}")
    input_height = int(shape[2])
    input_width = int(shape[3])
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    # Keep this preprocessing byte-for-byte aligned with
    # ``MetricDepthEstimator.estimate``.  The runtime uses cubic resize and
    # samples the model's native dense output grid; changing either detail
    # would make this replay diagnostic describe a different pipeline.
    resized = cv2.resize(rgb, (input_width, input_height), interpolation=cv2.INTER_CUBIC)
    tensor = resized.astype(np.float32) / 255.0
    tensor = (tensor - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)) / np.asarray(
        [0.229, 0.224, 0.225], dtype=np.float32
    )
    tensor = np.ascontiguousarray(np.transpose(tensor, (2, 0, 1))[None, ...])
    output = session.run(None, {input_meta.name: tensor})
    if len(output) != 1:
        raise DepthCompanionReplayError("metric-depth model must expose one output")
    prediction = np.asarray(output[0], dtype=np.float32).squeeze()
    if prediction.ndim != 2 or not np.isfinite(prediction).any():
        raise DepthCompanionReplayError(f"invalid model output shape or values: {prediction.shape}")
    return prediction


def replay_companion_set(
    rgb_manifest_path: Path,
    depth_manifest_path: Path,
    model_path: Path,
) -> dict[str, Any]:
    rgb_manifest_path = rgb_manifest_path.resolve(strict=True)
    depth_manifest_path = depth_manifest_path.resolve(strict=True)
    model_path = model_path.resolve(strict=True)
    rgb_manifest = _verify_manifest(rgb_manifest_path)
    depth_manifest = _verify_manifest(depth_manifest_path)
    if rgb_manifest.get("schema") != "multidetect.gazebo-bgr24-frame-set":
        raise DepthCompanionReplayError("unexpected RGB manifest schema")
    if depth_manifest.get("schema") != "multidetect.gazebo-depth-companion-set":
        raise DepthCompanionReplayError("unexpected Depth manifest schema")
    rgb_frames = rgb_manifest.get("frames")
    depth_frames = depth_manifest.get("frames")
    if not isinstance(rgb_frames, list) or not isinstance(depth_frames, list):
        raise DepthCompanionReplayError("manifest frames must be arrays")
    if len(rgb_frames) == 0 or len(rgb_frames) != len(depth_frames):
        raise DepthCompanionReplayError("RGB/Depth frame counts must be equal and nonzero")
    if depth_manifest.get("encoding") != "FLOAT32_M":
        raise DepthCompanionReplayError("replay currently requires FLOAT32_M depth")

    providers = ["CPUExecutionProvider"]
    session = ort.InferenceSession(str(model_path), providers=providers)
    per_frame: list[dict[str, Any]] = []
    absolute_errors: list[np.ndarray] = []
    squared_errors: list[np.ndarray] = []
    for rgb_record, depth_record in zip(rgb_frames, depth_frames, strict=True):
        if not isinstance(rgb_record, dict) or not isinstance(depth_record, dict):
            raise DepthCompanionReplayError("frame records must be objects")
        if (
            rgb_record.get("frame_id") != depth_record.get("frame_id")
            or rgb_record.get("sequence") != depth_record.get("sequence")
            or rgb_record.get("wire_record_sha256") != depth_record.get("rgb_record_sha256")
        ):
            raise DepthCompanionReplayError("RGB/Depth pair binding mismatch")
        rgb_path = _resolve_child(
            rgb_manifest_path.parent, rgb_record.get("file"), label="RGB file"
        )
        depth_path = _resolve_child(
            depth_manifest_path.parent, depth_record.get("file"), label="Depth file"
        )
        rgb_bytes = rgb_path.read_bytes()
        depth_bytes = depth_path.read_bytes()
        if len(rgb_bytes) != int(rgb_record.get("bytes", -1)) or _sha256(
            rgb_path
        ) != rgb_record.get("sha256"):
            raise DepthCompanionReplayError(f"RGB payload integrity mismatch: {rgb_path}")
        if len(depth_bytes) != int(depth_record.get("bytes", -1)) or _sha256(
            depth_path
        ) != depth_record.get("sha256"):
            raise DepthCompanionReplayError(f"Depth payload integrity mismatch: {depth_path}")
        width = int(depth_record.get("width", 0))
        height = int(depth_record.get("height", 0))
        if len(rgb_bytes) != width * height * 3 or len(depth_bytes) != width * height * 4:
            raise DepthCompanionReplayError("RGB/Depth geometry does not match payload size")
        bgr = np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(height, width, 3)
        reference = np.frombuffer(depth_bytes, dtype="<f4").reshape(height, width)
        predicted = _model_depth(session, bgr)
        # Runtime metric-depth keeps the model-native output grid.  Align the
        # explicitly-labelled Gazebo comparison reference to that grid rather
        # than introducing a post-model resize that production does not use.
        reference_aligned = cv2.resize(
            reference,
            (predicted.shape[1], predicted.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        reference_center = _center(reference_aligned)
        predicted_center = _center(predicted)
        valid = (
            np.isfinite(reference_center)
            & np.isfinite(predicted_center)
            & (reference_center > 0.0)
            & (predicted_center > 0.0)
        )
        if not valid.any():
            raise DepthCompanionReplayError("no valid RGB/Depth comparison pixels")
        errors = np.abs(predicted_center[valid] - reference_center[valid])
        absolute_errors.append(errors)
        squared_errors.append(np.square(predicted_center[valid] - reference_center[valid]))
        per_frame.append(
            {
                "frame_id": rgb_record.get("frame_id"),
                "sequence": rgb_record.get("sequence"),
                "rgb_record_sha256": rgb_record.get("wire_record_sha256"),
                "depth_payload_sha256": depth_record.get("sha256"),
                "reference_center_stats": _stats(reference_center),
                "model_center_stats": _stats(predicted_center),
                "comparison_valid_count": int(valid.sum()),
                "raw_absolute_error_median": float(np.median(errors)),
                "raw_absolute_error_mean": float(np.mean(errors)),
                "raw_rmse_m": float(
                    np.sqrt(np.mean(np.square(predicted_center[valid] - reference_center[valid])))
                ),
            }
        )
    all_abs = np.concatenate(absolute_errors)
    all_sq = np.concatenate(squared_errors)
    return {
        "schema": "multidetect.gazebo-depth-companion-model-replay",
        "schema_version": 1,
        "runtime_mode": "offline_sim_replay",
        "rgb_manifest": {
            "path": str(rgb_manifest_path),
            "file_sha256": _sha256(rgb_manifest_path),
            "manifest_sha256": rgb_manifest["manifest_sha256"],
        },
        "depth_manifest": {
            "path": str(depth_manifest_path),
            "file_sha256": _sha256(depth_manifest_path),
            "manifest_sha256": depth_manifest["manifest_sha256"],
        },
        "model": {
            "path": str(model_path),
            "file_sha256": _sha256(model_path),
            "provider": providers,
            "input": session.get_inputs()[0].shape,
            "output": session.get_outputs()[0].shape,
            "runtime_preprocessing": {
                "color_conversion": "BGR_to_RGB",
                "resize_interpolation": "INTER_CUBIC",
                "normalization": {
                    "scale": "1/255",
                    "mean": [0.485, 0.456, 0.406],
                    "std": [0.229, 0.224, 0.225],
                },
                "tensor_layout": "NCHW",
                "output_grid": "model_native_dense_grid",
            },
        },
        "frame_count": len(per_frame),
        "per_frame": per_frame,
        "comparison": {
            "center_fraction": 0.5,
            "valid_pixel_count": int(all_abs.size),
            "raw_absolute_error_median": float(np.median(all_abs)),
            "raw_absolute_error_mean": float(np.mean(all_abs)),
            "raw_rmse_m": float(np.sqrt(np.mean(all_sq))),
            "calibration_fit_applied": False,
            "field_calibration_created": False,
            "outdoor_distance_accuracy_claimed": False,
            "metric_depth_model_approved": False,
        },
        "truth_boundary": {
            "gazebo_depth_used_as_comparison_reference": True,
            "truth_used_by_runtime_or_model": False,
            "hardware_acceptance": "not_claimed",
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
        },
    }


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    unsigned = dict(receipt)
    unsigned.pop("canonical_sha256", None)
    document = {**unsigned, "canonical_sha256": canonical_sha256(unsigned)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rgb-manifest", type=Path, required=True)
    parser.add_argument("--depth-manifest", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    receipt = replay_companion_set(args.rgb_manifest, args.depth_manifest, args.model)
    _write_receipt(args.out, receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

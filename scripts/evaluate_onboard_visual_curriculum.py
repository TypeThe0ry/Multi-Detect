"""Compare visual candidates on identical held-out 3-D pixels, not flight truth."""

from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.evaluation import evaluate_detections, evaluation_document  # noqa: E402
from multidetect.vision import OnnxRawYoloConfig, OnnxRawYoloDetector  # noqa: E402
from scripts.evaluate_common_detector_tiling import _load_yolo_truth, _predict  # noqa: E402
from scripts.render_onboard_sdf_blender_curriculum import (  # noqa: E402
    geometry_sha256,
    load_target,
)
from scripts.render_onboard_sdf_blender_dataset import visible_bbox, yolo_label  # noqa: E402
from scripts.train_onboard_sdf_visual_candidate import (  # noqa: E402
    bound_file,
    sha256,
    verify_dataset,
)

SOURCE_PATHS = (
    "scripts/evaluate_onboard_visual_curriculum.py",
    "scripts/evaluate_common_detector_tiling.py",
    "scripts/train_onboard_sdf_visual_candidate.py",
    "scripts/render_onboard_sdf_blender_dataset.py",
    "scripts/render_onboard_sdf_blender_curriculum.py",
    "src/multidetect/evaluation.py",
    "src/multidetect/vision.py",
)


def verify_source_names(raw: str) -> None:
    try:
        names = ast.literal_eval(raw)
    except (ValueError, SyntaxError) as exc:
        raise ValueError("invalid ONNX source class metadata") from exc
    if (
        not isinstance(names, dict)
        or len(names) != 1
        or any(type(key) is not int for key in names)
        or names != {0: "search_target"}
    ):
        raise ValueError("model source classes are not the declared search_target candidate")


def verify_model_contract(path: Path) -> None:
    import onnx

    graph = onnx.load_model(str(path), load_external_data=False)
    if any(weight.data_location == onnx.TensorProto.EXTERNAL for weight in graph.graph.initializer):
        raise ValueError("unbound external ONNX weights are unsupported")
    metadata = {item.key: item.value for item in graph.metadata_props}
    if sum(item.key == "names" for item in graph.metadata_props) != 1:
        raise ValueError("candidate must carry one unambiguous source class mapping")
    verify_source_names(metadata.get("names", ""))
    if len(graph.graph.output) != 1:
        raise ValueError("candidate must expose one traditional raw ONNX output")
    shape = [dim.dim_value for dim in graph.graph.output[0].type.tensor_type.shape.dim]
    if len(shape) != 3 or shape[:2] != [1, 5] or shape[2] <= 0:
        raise ValueError("candidate must expose traditional single-class raw ONNX output")


def measured_condition(present: bool, visible: int, amodal: int) -> str:
    if not isinstance(present, bool) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (visible, amodal)
    ):
        raise ValueError("invalid rendered visibility counts")
    if not present:
        if visible or amodal:
            raise ValueError("absent target has rendered pixels")
        return "absent"
    if not amodal:
        if visible:
            raise ValueError("visible target is missing from amodal render")
        return "out_of_frame"
    if not visible:
        return "fully_occluded"
    if visible > amodal * 1.02 + 2:
        raise ValueError("visible mask exceeds amodal reference beyond raster tolerance")
    return "partially_occluded" if visible < amodal else "clear"


def verify_visibility(frame: dict, base: Path, cv2: Any) -> tuple[str, float | None]:
    details = frame.get("curriculum")
    if not isinstance(details, dict):
        raise ValueError("curriculum evaluation requires measured per-frame provenance")
    masks, binary, pixels = [], [], []
    for prefix in ("mask", "amodal_mask"):
        path = bound_file(base, frame[f"{prefix}_path"], frame[f"{prefix}_sha256"])
        mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if mask is None or mask.shape != (frame["height"], frame["width"]):
            raise ValueError("invalid rendered mask dimensions")
        if not ((mask == 0) | (mask == 255)).all():
            raise ValueError("rendered masks must be binary")
        masks.append(int((mask != 0).sum()))
        binary.append(mask != 0)
        pixels.append(mask)
    visible, amodal = masks
    if visible != frame["visible_target_pixels"] or amodal != details["amodal_target_pixels"]:
        raise ValueError("rendered visibility count drift")
    overlap = int((binary[0] & binary[1]).sum())
    outside = visible - overlap
    if outside:
        raise ValueError("visible pixels outside amodal reference: inconsistent mask")
    fraction = None if not amodal else overlap / amodal
    declared = details.get("visible_fraction")
    if (fraction is None) != (declared is None) or (
        fraction is not None
        and (not isinstance(declared, (int, float)) or not math.isclose(fraction, declared))
    ):
        raise ValueError("rendered visibility fraction drift")
    # Recompute grouping independently. Planned conditions never define success.
    if details.get("visible_amodal_overlap_pixels") != overlap:
        raise ValueError("rendered mask overlap drift")
    bbox = visible_bbox(pixels[0].reshape(-1).tobytes(), frame["width"], frame["height"])
    label = bound_file(base, frame["label_path"], frame["label_sha256"])
    if label.read_text(encoding="utf-8") != yolo_label(bbox, frame["width"], frame["height"]):
        raise ValueError("YOLO supervision is not derived from the actual visible mask")
    if frame.get("visible_bbox_xyxy_px") != (None if bbox is None else list(bbox)):
        raise ValueError("declared visible bbox disagrees with actual pixels")
    condition = measured_condition(frame["target_present"], visible, amodal)
    if details.get("measured_condition") != condition:
        raise ValueError("declared visibility condition disagrees with rendered pixels")
    return condition, fraction


def verify_geometry_provenance(document: dict) -> dict[str, dict[str, str]]:
    """Recompute geometry from bound SDF bytes, not manifest fingerprint claims."""
    inventory: dict[str, dict[str, str]] = {}
    split_paths: dict[str, set[str]] = {name: set() for name in ("train", "val", "test")}
    split_geometries: dict[str, set[str]] = {name: set() for name in split_paths}
    for frame in document["frames"]:
        details = frame.get("curriculum")
        model = frame.get("target_model")
        sources = frame.get("source_hashes")
        if (
            not isinstance(details, dict)
            or not isinstance(model, dict)
            or not isinstance(sources, dict)
        ):
            raise ValueError("frame must bind its actual curriculum target model")
        relative = details.get("target_model_path")
        expected_hash = details.get("target_model_sha256")
        expected_geometry = details.get("target_geometry_sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_hash, str)
            or not isinstance(expected_geometry, str)
            or sources.get(relative) != expected_hash
            or model.get("path") != relative
            or model.get("sha256") != expected_hash
            or model.get("geometry_sha256") != expected_geometry
        ):
            raise ValueError("curriculum target path and identity disagree with bound source/model")
        if relative not in inventory:
            actual_source = bound_file(ROOT, relative, expected_hash)
            actual_geometry = geometry_sha256(load_target(ROOT, relative))
            inventory[relative] = {
                "path": relative,
                "sha256": sha256(actual_source),
                "geometry_sha256": actual_geometry,
            }
        actual = inventory[relative]
        if actual["sha256"] != expected_hash or actual["geometry_sha256"] != expected_geometry:
            raise ValueError("declared target geometry disagrees with actual bound SDF primitives")
        split = frame["split"]
        if split not in split_paths:
            raise ValueError("unknown geometry split")
        split_paths[split].add(relative)
        split_geometries[split].add(actual["geometry_sha256"])
    if not split_paths["test"]:
        raise ValueError("no held-out test geometry")
    if split_paths["test"] & (split_paths["train"] | split_paths["val"]):
        raise ValueError("test target geometry overlaps train or validation")
    if split_geometries["test"] & (split_geometries["train"] | split_geometries["val"]):
        raise ValueError("test geometry identity overlaps despite different model paths")
    return inventory


def summarize_groups(truth: list, predictions: list, groups: dict, confidence: float) -> dict:
    report = {
        "overall": evaluation_document(
            evaluate_detections(
                tuple(truth),
                tuple(predictions),
                iou_threshold=0.5,
                confidence_threshold=confidence,
            )
        ),
        "by_measured_condition": {},
    }
    for name, indices in sorted(groups.items()):
        report["by_measured_condition"][name] = evaluation_document(
            evaluate_detections(
                tuple(truth[index] for index in indices),
                tuple(predictions[index] for index in indices),
                iou_threshold=0.5,
                confidence_threshold=confidence,
            )
        )
    return report


def evaluate(
    manifest: Path,
    models: dict[str, Path],
    output: Path,
    *,
    confidence: float = 0.25,
    input_size: int = 640,
) -> dict:
    if output.exists():
        raise FileExistsError("refusing to overwrite paired evaluation evidence")
    if not 0 < confidence <= 1 or input_size <= 0:
        raise ValueError("invalid confidence or input size")
    document, _ = verify_dataset(manifest)
    manifest_hash = sha256(manifest)
    runtime_sources = {path: sha256(ROOT / path) for path in SOURCE_PATHS}
    frames = [frame for frame in document["frames"] if frame["split"] == "test"]
    if not frames:
        raise ValueError("no held-out test frames")
    geometries = verify_geometry_provenance(document)
    test_models = {frame["curriculum"]["target_model_path"] for frame in frames}
    import cv2

    truth, images, visibility = [], [], []
    groups: dict[str, list[int]] = {}
    base = manifest.resolve().parent
    for index, frame in enumerate(frames):
        condition, fraction = verify_visibility(frame, base, cv2)
        groups.setdefault(condition, []).append(index)
        image_path = bound_file(base, frame["image_path"], frame["image_sha256"])
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (frame["height"], frame["width"]):
            raise ValueError("invalid held-out RGB dimensions")
        label = bound_file(base, frame["label_path"], frame["label_sha256"])
        ground_truth = _load_yolo_truth(frame["frame_id"], label, ("search_target",))
        if bool(ground_truth.objects) != bool(frame["visible_target_pixels"]):
            raise ValueError("visible supervision disagrees with actual mask")
        truth.append(ground_truth)
        images.append(image)
        visibility.append(
            {
                "frame_id": frame["frame_id"],
                "measured_condition": condition,
                "visible_fraction": fraction,
                "planned_condition": frame["curriculum"]["planned_condition"],
            }
        )
    results = {}
    for name, path in models.items():
        model_hash = sha256(path)
        verify_model_contract(path)
        detector = OnnxRawYoloDetector(
            OnnxRawYoloConfig(
                model_path=path,
                class_names=("search_target",),
                input_width=input_size,
                input_height=input_size,
                confidence_threshold=0.1,
                iou_threshold=0.45,
                providers=("CPUExecutionProvider",),
            )
        )
        detector.warmup(iterations=2)
        predictions = [
            _predict(detector, image, frame["frame_id"])
            for image, frame in zip(images, frames, strict=True)
        ]
        if sha256(path) != model_hash:
            raise ValueError("model changed during evaluation")
        results[name] = {
            "path": str(path.resolve()),
            "sha256": model_hash,
            "providers": list(detector.provider_names),
            **summarize_groups(truth, predictions, groups, confidence),
        }
    verify_dataset(manifest)
    if sha256(manifest) != manifest_hash:
        raise ValueError("manifest changed during evaluation")
    if any(sha256(ROOT / path) != digest for path, digest in runtime_sources.items()):
        raise ValueError("evaluation runtime source changed during inference")
    result = {
        "contract": "multidetect.onboard-visual-curriculum-paired-evaluation-v1",
        "dataset": {"path": str(manifest.resolve()), "sha256": manifest_hash},
        "test_frames": len(frames),
        "test_geometry_disjoint": True,
        "test_model_paths": sorted(test_models),
        "verified_test_geometry": [geometries[path] for path in sorted(test_models)],
        "geometry_verification": "recomputed_from_hash_bound_target_sdf_primitives",
        "evaluation_confidence": confidence,
        "iou_threshold": 0.5,
        "model_class_names": ["search_target"],
        "runtime_sources": runtime_sources,
        "condition_counts": {name: len(indices) for name, indices in groups.items()},
        "frame_visibility": visibility,
        "models": results,
        "comparison_scope": "same_held_out_pixels_same_runtime_same_thresholds",
        "original_detector_changed": False,
        "onboard_policy_reference_changed": False,
        "gazebo_capture": False,
        "autonomous_flight_verified": False,
        "candidate_promoted": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = evaluate(
        args.manifest, {"parent": args.parent, "candidate": args.candidate}, args.output
    )
    print(
        json.dumps(
            {
                "test_frames": result["test_frames"],
                "models": {
                    name: item["overall"]["overall"] for name, item in result["models"].items()
                },
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

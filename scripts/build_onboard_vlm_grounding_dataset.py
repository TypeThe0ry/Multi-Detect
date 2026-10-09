"""Draft reviewed-only RGB reference grounding supervision from sealed 3-D clips.

This does not train, infer actions, create telemetry, or claim identity/flight
success. Visible masks are offline label evidence; no mask, scene, source ID,
condition, or metric position is admitted to the model-facing input.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from scripts import audit_onboard_visual_dataset as audit  # noqa: E402
from scripts import build_onboard_vlm_shadow_cases as shadow  # noqa: E402

trainer = shadow.trainer
evaluator = shadow.evaluator
replay = shadow.replay
CONTRACT = "multidetect.onboard-vlm-grounding-dataset-v1"
REVIEW_CONTRACT = "multidetect.onboard-vlm-grounding-independent-review-v1"
BUILDER_ID = "scripts.build_onboard_vlm_grounding_dataset"
DEFAULT_MANIFEST = shadow.DEFAULT_MANIFEST
DEFAULT_MANIFEST_SHA256 = shadow.DEFAULT_MANIFEST_SHA256
SPLITS = ("train", "val", "test")
POLICY = {
    "positive_min_visible_pixels": 128,
    "positive_min_visible_fraction": 0.10,
    "negative_visible_pixels": 0,
    "ambiguous_nonzero_policy": "exclude_from_supervised_samples",
    "reference_min_visible_pixels": 256,
    "reference_min_visible_fraction": 0.25,
    "reference_min_bbox_side_px": 8,
    "reference_min_visible_crop_fill_fraction": 0.10,
    "bbox_convention": "xyxy_half_open",
    "reference_selection": "earliest_qualified_frame_within_same_original_clip",
    "current_selection": "strictly_later_than_reference",
    "supervision_semantics": "low_level_rendered_visibility_not_identity_confirmation",
}
PROMPT = (
    "参考图像中的可见实体是本次观察的指代对象。比较参考图像与当前图像，"
    "仅报告当前是否有足够可见证据支持该指代对象；不确定时用not_confirmed，"
    "不要把它解释成目标不存在。GPS、NED和飞机运动均无已提供证据，保持unknown。"
    "只返回约定的事实JSON，不输出类别、米制位置、飞行动作或执行成功。"
)
UNKNOWN_CONTEXT = {"gps": "unknown", "ned": "unknown", "aircraft_motion": "unknown"}
FALSE_FLAGS = (
    "training_ready",
    "training_performed",
    "gazebo_capture",
    "sensor_clock_claimed",
    "flight_action_labels_created",
    "autonomous_flight_verified",
    "hardware_acceptance",
    "candidate_promoted",
    "onboard_policy_reference_changed",
    "final_release_allowed",
)
SOURCE_MODULES = {
    **shadow.SOURCE_MODULES,
    "scripts/build_onboard_vlm_shadow_cases.py": shadow,
    "scripts/audit_onboard_visual_dataset.py": audit,
}
SOURCE_PATHS = tuple(
    sorted(
        {
            "scripts/build_onboard_vlm_grounding_dataset.py",
            "tests/test_build_onboard_vlm_grounding_dataset.py",
            *shadow.SOURCE_PATHS,
            *audit.SOURCE_PATHS,
        }
    )
)
IMAGE_FIELDS = {"image_id", "role", "path", "sha256", "bytes", "width", "height", "encoding"}
SAMPLE_FIELDS = {
    "sample_id",
    "split",
    "model_input",
    "assistant_target",
    "model_input_sha256",
    "assistant_target_sha256",
    "provenance",
}
REVIEW_CHECKS = (
    "source_bytes_reverified",
    "visible_mask_labels_checked",
    "reference_crops_checked",
    "model_input_truth_isolated",
    "split_boundaries_checked",
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("source and output must remain inside the repository")
    return resolved.relative_to(ROOT.resolve()).as_posix()


def _snapshot(path: Path, bindings: dict[Path, str], expected: str | None = None) -> str:
    path = path.resolve(strict=True)
    digest = trainer.sha256(path)
    if (expected is not None and digest != expected) or (
        path in bindings and bindings[path] != digest
    ):
        raise ValueError(f"bound file identity changed: {path}")
    bindings[path] = digest
    return digest


def _verify(bindings: dict[Path, str]) -> None:
    for path, digest in bindings.items():
        if not path.is_file() or trainer.sha256(path) != digest:
            raise ValueError(f"bound source changed during preparation: {path}")


def _sources(bindings: dict[Path, str]) -> dict[str, str]:
    sources = {name: _snapshot(ROOT / name, bindings) for name in SOURCE_PATHS}
    _snapshot(Path(__file__), bindings, sources["scripts/build_onboard_vlm_grounding_dataset.py"])
    for name, module in SOURCE_MODULES.items():
        _snapshot(Path(module.__file__), bindings, sources[name])
    return sources


def _label(frame: dict) -> str | None:
    pixels = frame["visible_target_pixels"]
    fraction = frame["visibility_measurement"]["visible_fraction"]
    if pixels == POLICY["negative_visible_pixels"]:
        return "not_confirmed"
    if (
        pixels >= POLICY["positive_min_visible_pixels"]
        and fraction is not None
        and fraction >= POLICY["positive_min_visible_fraction"]
    ):
        return "visible"
    return None


def _reference_ok(frame: dict) -> bool:
    bbox = frame["visible_bbox_xyxy_px"]
    fraction = frame["visibility_measurement"]["visible_fraction"]
    if bbox is None or fraction is None:
        return False
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return (
        frame["visible_target_pixels"] >= POLICY["reference_min_visible_pixels"]
        and fraction >= POLICY["reference_min_visible_fraction"]
        and min(width, height) >= POLICY["reference_min_bbox_side_px"]
        and frame["visible_target_pixels"] / (width * height)
        >= POLICY["reference_min_visible_crop_fill_fraction"]
    )


def _rgb(frame: dict, base: Path) -> bytes:
    path = trainer.bound_file(base, frame["image_path"], frame["image_sha256"])
    payload = path.read_bytes()
    if _digest(payload) != frame["image_sha256"]:
        raise ValueError("RGB changed between hash and read")
    shadow._validate_rgb(payload, frame)
    return payload


def _crop(frame: dict, payload: bytes) -> bytes:
    from PIL import Image

    # The old renderer already returns half-open bounds; no second +1.
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        crop = image.crop(tuple(frame["visible_bbox_xyxy_px"]))
        output = io.BytesIO()
        crop.save(output, format="PNG")
        return output.getvalue()


def _frame_provenance(frame: dict) -> dict:
    return {
        "frame_id": frame["frame_id"],
        "frame_index": frame["frame_index"],
        "clip_id": frame["clip_id"],
        "original_split": frame["split"],
        "image_path": frame["image_path"],
        "image_sha256": frame["image_sha256"],
        "mask_path": frame["mask_path"],
        "mask_sha256": frame["mask_sha256"],
        "label_path": frame["label_path"],
        "label_sha256": frame["label_sha256"],
        "amodal_mask_path": frame["amodal_mask_path"],
        "amodal_mask_sha256": frame["amodal_mask_sha256"],
        "captured_at_s": frame["captured_at_s"],
        "dt_s": frame["dt"],
        "timestamp_semantics": frame["timestamp_semantics"],
        "visible_target_pixels": frame["visible_target_pixels"],
        "visible_bbox_xyxy_px": frame["visible_bbox_xyxy_px"],
        "visibility_measurement": deepcopy(frame["visibility_measurement"]),
    }


def _image_record(identifier: str, role: str, payload: bytes, width: int, height: int) -> dict:
    return {
        "image_id": identifier,
        "role": role,
        "path": f"images/{identifier}.png",
        "sha256": _digest(payload),
        "bytes": len(payload),
        "width": width,
        "height": height,
        "encoding": "png",
    }


def _construct(
    manifest: Path, expected_sha256: str, output: Path, parent_models: tuple[dict, ...]
) -> tuple[dict, dict[str, bytes], dict[Path, str]]:
    manifest = manifest.resolve(strict=True)
    _relative(manifest)
    _relative(output)
    if output.is_relative_to(manifest.parent) or manifest.is_relative_to(output):
        raise ValueError("output must be independent of source dataset")
    bindings: dict[Path, str] = {}
    sources = _sources(bindings)
    manifest_sha = _snapshot(manifest, bindings, expected_sha256)
    payload = manifest.read_bytes()
    if _digest(payload) != manifest_sha:
        raise ValueError("manifest changed before parse")
    strict = shadow._strict_json(payload)
    document, _ = trainer.verify_dataset(manifest)
    if document != strict:
        raise ValueError("manifest changed between strict and legacy validation")
    if any(name in document and document[name] is not False for name in FALSE_FLAGS):
        raise ValueError("source cannot assert flight or training acceptance")
    for path, digest in audit._bindings(document, manifest.parent).items():
        _snapshot(path, bindings, digest)
    normalized = [shadow._normalise_frame(frame) for frame in document["frames"]]
    geometry = evaluator.verify_geometry_provenance({"frames": normalized})
    import cv2

    for frame in normalized:
        evaluator.verify_visibility(frame, manifest.parent, cv2)
        if type(frame.get("target_visible")) is not bool or frame["target_visible"] != (
            frame["visible_target_pixels"] > 0
        ):
            raise ValueError("source visibility flag does not match the visible mask")
    groups = replay.validate_clips(document)
    parents = []
    for parent in parent_models:
        if not isinstance(parent, dict) or set(parent) != {"path", "sha256"}:
            raise ValueError("parent model references require exact path/sha256 fields")
        path = trainer.bound_file(ROOT, parent["path"], parent["sha256"])
        _snapshot(path, bindings, parent["sha256"])
        parents.append({**parent, "bytes": path.stat().st_size})
    samples, inventory, assets = [], [], {}
    for clip, frames in groups:
        if any(shadow._scope(frame) != shadow._scope(frames[0]) for frame in frames):
            raise ValueError("clip crosses source/geometry/world/split/clock identity")
        if frames[0]["timestamp_semantics"] != shadow.CLOCK_SEMANTICS:
            raise ValueError("only explicitly declared rendered sample time is admitted")
        for key in ("world_path", "world_sha256", "source_hashes", "timestamp_semantics"):
            if key in clip and clip[key] != frames[0].get(key):
                raise ValueError("clip inventory differs from its frame identities")
        references = [frame for frame in frames if _reference_ok(frame)]
        item = {
            "source_clip_id": clip["clip_id"],
            "split": clip["split"],
            "source_frame_count": len(frames),
            "reference_frame_id": None,
            "included_frame_ids": [],
            "ambiguous_excluded_frame_ids": [],
            "before_or_at_reference_frame_ids": [],
            "skip_reason": None,
        }
        inventory.append(item)
        if not references:
            item["skip_reason"] = "no_qualified_visible_reference_within_original_clip"
            continue
        reference = references[0]
        item["reference_frame_id"] = reference["frame_id"]
        reference_id = canonical_sha256(
            {"manifest": manifest_sha, "clip": clip["clip_id"], "kind": "reference"}
        )[:32]
        reference_image_id = canonical_sha256(
            {"reference": reference_id, "frame": reference["frame_id"]}
        )[:32]
        reference_payload = _crop(reference, _rgb(reference, manifest.parent))
        box = reference["visible_bbox_xyxy_px"]
        reference_image = _image_record(
            reference_image_id, "reference", reference_payload, box[2] - box[0], box[3] - box[1]
        )
        for current in frames:
            if current["frame_index"] <= reference["frame_index"]:
                item["before_or_at_reference_frame_ids"].append(current["frame_id"])
                continue
            label = _label(current)
            if label is None:
                item["ambiguous_excluded_frame_ids"].append(current["frame_id"])
                continue
            item["included_frame_ids"].append(current["frame_id"])
            image_id = canonical_sha256(
                {"manifest": manifest_sha, "frame": current["frame_id"], "kind": "current"}
            )[:32]
            current_payload = _rgb(current, manifest.parent)
            current_image = _image_record(
                image_id, "current", current_payload, current["width"], current["height"]
            )
            for record, image_payload in (
                (reference_image, reference_payload),
                (current_image, current_payload),
            ):
                if record["path"] in assets and assets[record["path"]] != image_payload:
                    raise ValueError("opaque image identity collision")
                assets[record["path"]] = image_payload
            model_input = {
                "prompt": PROMPT,
                "target_reference_id": reference_id,
                "images": [deepcopy(reference_image), current_image],
                "context": deepcopy(UNKNOWN_CONTEXT),
            }
            target = {
                "target_reference_id": reference_id,
                "current_target_visibility": label,
                **UNKNOWN_CONTEXT,
            }
            provenance = {
                "source_clip_id": clip["clip_id"],
                "original_split": clip["split"],
                "world": {"path": current["world_path"], "sha256": current["world_sha256"]},
                "target_geometry": geometry[current["target_model"]["path"]],
                "source_hashes": deepcopy(current["source_hashes"]),
                "reference": _frame_provenance(reference),
                "current": _frame_provenance(current),
                "reference_crop_xyxy_half_open": box,
                "reference_crop_kind": "unaltered_RGB_rectangle_not_mask_composite",
                "label_policy_sha256": canonical_sha256(POLICY),
                "label_source": "verified_rendered_visible_mask_and_measured_fraction",
                "semantic_identity_confirmed": False,
                "amodal_truth_model_visible": False,
                "aircraft_context": None,
            }
            samples.append(
                {
                    "sample_id": canonical_sha256({"input": model_input, "kind": "grounding"})[:32],
                    "split": current["split"],
                    "model_input": model_input,
                    "assistant_target": target,
                    "model_input_sha256": canonical_sha256(model_input),
                    "assistant_target_sha256": canonical_sha256(target),
                    "provenance": provenance,
                }
            )
    if len({sample["sample_id"] for sample in samples}) != len(samples):
        raise ValueError("duplicate sample identity")
    counts = {split: sum(sample["split"] == split for sample in samples) for split in SPLITS}
    clip_counts = {
        split: sum(
            item["split"] == split and bool(item["included_frame_ids"]) for item in inventory
        )
        for split in SPLITS
    }
    result = {
        "schema_version": 1,
        "contract": CONTRACT,
        "builder_id": BUILDER_ID,
        "repository_root": ROOT.resolve().as_posix(),
        "output_directory": _relative(output),
        "source_manifest": {
            "path": _relative(manifest),
            "sha256": manifest_sha,
            "bytes": len(payload),
        },
        "parent_models": parents,
        "runtime_sources": sources,
        "label_policy": deepcopy(POLICY),
        "review_status": "pending_independent_fact_review",
        "model_context": None,
        "model_input_allowlist": [
            "prompt",
            "opaque_target_reference_id",
            "opaque_image_ids",
            "image_roles",
            "actual_RGB_bytes",
            "context_unknown",
        ],
        "transport_image_metadata_model_visible": False,
        "supervision_scope": (
            "low_level_visible_reference_grounding_not_semantic_identity_or_actions"
        ),
        "test_status": "previously_diagnosed_development_holdout_not_blind_benchmark",
        "sample_count": len(samples),
        "split_samples": counts,
        "split_clip_counts": clip_counts,
        "split_labels": {
            split: dict(
                Counter(
                    sample["assistant_target"]["current_target_visibility"]
                    for sample in samples
                    if sample["split"] == split
                )
            )
            for split in SPLITS
        },
        "ambiguous_excluded_count": sum(
            len(item["ambiguous_excluded_frame_ids"]) for item in inventory
        ),
        "skipped_clip_count": sum(item["skip_reason"] is not None for item in inventory),
        "independent_source_manifest_count": 1,
        "max_model_images": 2,
        "development_limitations": [
            "windows_are_not_independent_videos",
            "few_independent_train_clips_no_generalization_claim",
            "visibility_threshold_not_human_recognition_or_identity_review",
            "unknown_context_is_not_defaulted_telemetry",
        ],
        "clip_inventory": inventory,
        "samples": samples,
        **{name: False for name in FALSE_FLAGS},
    }
    _verify(bindings)
    return result, assets, bindings


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
    if path.read_bytes() != payload:
        raise ValueError("output bytes changed while publishing draft")


def build_grounding_dataset(
    manifest: Path,
    expected_manifest_sha256: str,
    output_directory: Path,
    *,
    include_two_frame: bool = False,
    parent_models: tuple[dict, ...] = (),
) -> dict:
    """Publish only a pending-review draft; no existing artifact is overwritten."""
    if include_two_frame is not False:
        raise ValueError("v1 supports exactly reference/current, not three-image history")
    output = output_directory.resolve()
    if output.exists():
        raise FileExistsError("refusing to overwrite an existing grounding output")
    document, assets, bindings = _construct(
        manifest, expected_manifest_sha256, output, parent_models
    )
    output.mkdir(parents=True, exist_ok=False)
    for relative, payload in assets.items():
        _write(output / relative, payload)
    _verify(bindings)
    pending = output / "manifest.pending-review.json"
    _write(
        pending,
        (
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8"),
    )
    validate_grounding_dataset(pending, trainer.sha256(pending))
    return document


def validate_review_receipt(path: Path, manifest: Path, document: dict) -> dict:
    payload = path.read_bytes()
    receipt = shadow._strict_json(payload)
    fields = {
        "schema_version",
        "contract",
        "dataset_sha256",
        "dataset_bytes",
        "reviewer",
        "reviewed_sample_ids",
        "approved_for_development_training",
        *REVIEW_CHECKS,
    }
    if set(receipt) != fields or type(receipt["schema_version"]) is not int:
        raise ValueError("independent review receipt fields are invalid")
    if (
        receipt["schema_version"] != 1
        or receipt["contract"] != REVIEW_CONTRACT
        or receipt["dataset_sha256"] != trainer.sha256(manifest)
        or type(receipt["dataset_bytes"]) is not int
        or receipt["dataset_bytes"] != manifest.stat().st_size
        or not isinstance(receipt["reviewer"], str)
        or not receipt["reviewer"].strip()
        or receipt["reviewer"] == BUILDER_ID
        or receipt["reviewed_sample_ids"]
        != sorted(sample["sample_id"] for sample in document["samples"])
        or receipt["approved_for_development_training"] is not True
        or any(receipt[name] is not True for name in REVIEW_CHECKS)
    ):
        raise ValueError("independent fact review does not authorize these exact dataset bytes")
    if path.read_bytes() != payload:
        raise ValueError("review receipt changed during validation")
    return receipt


def validate_grounding_dataset(
    manifest: Path,
    expected_sha256: str | None = None,
    *,
    review_receipt: Path | None = None,
    require_review: bool = False,
) -> dict:
    """Reopen the original clips, all labels/masks, and exact RGB crop pixels."""
    if type(require_review) is not bool:
        raise ValueError("require_review must be boolean")
    manifest = manifest.resolve(strict=True)
    payload = manifest.read_bytes()
    if expected_sha256 is not None and _digest(payload) != expected_sha256:
        raise ValueError("grounding manifest SHA256 mismatch")
    document = shadow._strict_json(payload)
    if document.get("contract") != CONTRACT or document.get("repository_root") != ROOT.as_posix():
        raise ValueError("unknown grounding dataset contract or repository root")
    output = ROOT / document["output_directory"]
    if manifest.parent != output.resolve():
        raise ValueError("manifest must remain in its declared exclusive output")
    reference = document["source_manifest"]
    source = trainer.bound_file(ROOT, reference["path"], reference["sha256"])
    parents = tuple(
        {"path": parent["path"], "sha256": parent["sha256"]} for parent in document["parent_models"]
    )
    expected, assets, bindings = _construct(source, reference["sha256"], output, parents)
    if expected != document:
        raise ValueError("grounding source, split, policy, label or model input identity changed")
    for relative, original in assets.items():
        path = trainer.bound_file(output, relative, _digest(original))
        if path.read_bytes() != original:
            raise ValueError("RGB/crop output differs from original visible reference pixels")
        _snapshot(path, bindings, _digest(original))
    if require_review and review_receipt is None:
        raise ValueError("independent fact review receipt required before optimizer")
    if review_receipt is not None:
        validate_review_receipt(review_receipt, manifest, document)
    if manifest.read_bytes() != payload:
        raise ValueError("grounding manifest changed during source revalidation")
    _verify(bindings)
    return document


def model_sample_input(sample: dict, dataset_root: Path) -> dict[str, Any]:
    """Only actual image bytes and factual task input, never provenance or SHA text."""
    if set(sample) != SAMPLE_FIELDS:
        raise ValueError("sample contains unknown fields")
    data = sample["model_input"]
    if (
        set(data) != {"prompt", "target_reference_id", "images", "context"}
        or data["prompt"] != PROMPT
        or data["context"] != UNKNOWN_CONTEXT
        or [image["role"] for image in data["images"]] != ["reference", "current"]
        or canonical_sha256(data) != sample["model_input_sha256"]
    ):
        raise ValueError("model input is not the bounded RGB-only contract")
    images = []
    for image in data["images"]:
        if set(image) != IMAGE_FIELDS:
            raise ValueError("image contains unknown model-visible fields")
        path = trainer.bound_file(dataset_root, image["path"], image["sha256"])
        payload = path.read_bytes()
        if len(payload) != image["bytes"] or _digest(payload) != image["sha256"]:
            raise ValueError("model image bytes changed")
        shadow._validate_rgb(payload, image)
        images.append(
            {"image_id": image["image_id"], "role": image["role"], "image_bytes": payload}
        )
    return {
        "prompt": data["prompt"],
        "target_reference_id": data["target_reference_id"],
        "images": images,
        "context": deepcopy(UNKNOWN_CONTEXT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / DEFAULT_MANIFEST)
    parser.add_argument("--manifest-sha256", default=DEFAULT_MANIFEST_SHA256)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--parent-file", nargs=2, action="append", default=[], metavar=("REPOSITORY_PATH", "SHA256")
    )
    args = parser.parse_args(argv)
    result = build_grounding_dataset(
        args.manifest,
        args.manifest_sha256,
        args.output_dir,
        parent_models=tuple({"path": path, "sha256": sha} for path, sha in args.parent_file),
    )
    print(
        json.dumps(
            {
                "split_samples": result["split_samples"],
                "split_clip_counts": result["split_clip_counts"],
                "ambiguous_excluded_count": result["ambiguous_excluded_count"],
                "training_ready": False,
                "review_status": result["review_status"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

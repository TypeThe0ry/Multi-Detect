"""Create a pending, train-only RGB grounding increment; never author actions.

The old reviewed train/val/test samples and model inputs are copied exactly.
Additional images are selected only from the original rendered training split.
References match source world and SDF geometry, not a physical instance or a
temporal trajectory. Masks/amodal geometry remain offline label evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from scripts import build_onboard_vlm_grounding_dataset as base  # noqa: E402

trainer = base.trainer
evaluator = base.evaluator
CONTRACT = "multidetect.onboard-vlm-grounding-increment-dataset-v1"
REVIEW_CONTRACT = "multidetect.onboard-vlm-grounding-increment-independent-review-v1"
BUILDER_ID = "scripts.build_onboard_vlm_grounding_increment_dataset"
SOURCE = "artifacts/training/onboard-blender-sdf-replay-merged-20261007-r4-r2/manifest.json"
SOURCE_SHA256 = "4704587af437ddc9ed022a265c59aac1cf3dbf258b3c50aa14e0a24c354e12fd"
BASE = "artifacts/training/onboard-vlm-grounding-20261007-r1/manifest.pending-review.json"
BASE_SHA256 = "3cf95e401e3be5b9acd0d11e4b8aaee7fe4296e41b2889655fead7a2f34b6cfe"
ADAPTER = (
    "artifacts/models/onboard-vlm-grounding-lora-dev-20261007-r1/adapter/adapter_model.safetensors"
)
ADAPTER_SHA256 = "e31d71f1b0b89136574728603ef611fbd77bd6d0edb6d4445c803987640120bb"
BUCKETS = ("visible", "fully_occluded", "absent")
REVIEW_CHECKS = (
    *base.REVIEW_CHECKS,
    "new_train_only",
    "heldout_inputs_unchanged",
    "original_train_not_duplicated",
    "non_temporal_pairing_disclosed",
    "parent_adapter_identity_checked",
)


def _relative(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("source and output must remain inside the repository")
    return resolved.relative_to(ROOT.resolve()).as_posix()


def _bind(path: Path, expected: str, bindings: dict[Path, str]) -> dict:
    path = path.resolve(strict=True)
    digest = base._snapshot(path, bindings, expected)
    return {"path": _relative(path), "bytes": path.stat().st_size, "sha256": digest}


def _runtime_sources(bindings: dict[Path, str]) -> dict:
    paths = (
        Path(__file__),
        Path(__file__).resolve().parents[1]
        / "tests/test_build_onboard_vlm_grounding_increment_dataset.py",
        Path(base.__file__),
        Path(trainer.__file__),
        Path(evaluator.__file__),
        Path(base.shadow.__file__),
        Path(base.audit.__file__),
    )
    return {
        path.resolve().as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": base._snapshot(path, bindings),
        }
        for path in paths
    }


def _frame_key(frame: dict) -> tuple[str, str]:
    return frame["world_sha256"], frame["target_model"]["geometry_sha256"]


def _frame_provenance(frame: dict) -> dict:
    result = {
        key: deepcopy(frame.get(key))
        for key in (
            "frame_id",
            "image_path",
            "image_sha256",
            "mask_path",
            "mask_sha256",
            "amodal_mask_path",
            "amodal_mask_sha256",
            "label_path",
            "label_sha256",
            "visible_bbox_xyxy_px",
            "visible_target_pixels",
            "visibility_measurement",
            "merge_provenance",
            "world_path",
            "world_sha256",
            "target_model",
        )
    }
    result.update(
        original_split=frame["split"],
        temporal_pair_claimed=False,
        physical_instance_identity_claimed=False,
    )
    return result


def _audit_source(manifest: Path, bindings: dict[Path, str]) -> dict:
    """Reopen ALL partitions for integrity; selection still uses train only."""
    payload = manifest.read_bytes()
    strict = base.shadow._strict_json(payload)
    document, _ = trainer.verify_dataset(manifest)
    if document != strict:
        raise ValueError("source changed between strict and legacy validation")
    for key in base.FALSE_FLAGS:
        if key in document and document[key] is not False:
            raise ValueError("source asserts training/flight/authority acceptance")
    for path, digest in base.audit._bindings(document, manifest.parent).items():
        base._snapshot(path, bindings, digest)
    evaluator.verify_geometry_provenance(document)
    import cv2

    identifiers, pixels = set(), {}
    for frame in document["frames"]:
        if frame["frame_id"] in identifiers:
            raise ValueError("duplicate source frame ID")
        identifiers.add(frame["frame_id"])
        image = cv2.imread(str(manifest.parent / frame["image_path"]), cv2.IMREAD_UNCHANGED)
        if image is None or image.shape != (frame["height"], frame["width"], 3):
            raise ValueError("actual source RGB dimensions/channels differ")
        if image.dtype.name != "uint8":
            raise ValueError("actual source RGB must be uint8")
        evaluator.verify_visibility(frame, manifest.parent, cv2)
        if frame.get("target_visible") is not (frame["visible_target_pixels"] > 0):
            raise ValueError("source visibility flag differs from actual mask")
        digest = frame["image_sha256"]
        if digest in pixels:
            raise ValueError("duplicate source RGB is not independent supervision")
        pixels[digest] = frame["split"]
    if base._digest(manifest.read_bytes()) != base._digest(payload):
        raise ValueError("source manifest changed during image audit")
    return document


def _rank(seed: int, kind: str, frame: dict) -> str:
    return canonical_sha256(
        {"seed": seed, "kind": kind, "rgb": frame["image_sha256"], "frame": frame["frame_id"]}
    )


def _negative_scene(frame: dict) -> dict:
    """Reject a second complete target geometry; not a perceptual similarity test."""
    primitives = frame.get("scene_primitives")
    if not isinstance(primitives, list) or not primitives:
        raise ValueError("zero-mask negatives require the actual primitive scene inventory")
    targets, groups = {}, {}
    for primitive in primitives:
        if type(primitive.get("target")) is not bool:
            raise ValueError("scene primitive target attribution must be explicit")
        name = primitive.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("scene primitives require original entity names")
        parts = name.split("/")
        group = "/".join(parts[:2]) if parts[0] == "curriculum" else parts[0]
        signature = canonical_sha256(
            {"geometry": primitive["geometry"], "dimensions_m": primitive["dimensions_m"]}
        )
        inventory = targets if primitive["target"] else groups
        inventory.setdefault(group, []).append(signature)
    if len(targets) != 1 or len(next(iter(targets.values()))) < 2:
        raise ValueError("negative requires one explicitly attributed compound target entity")
    target = sorted(next(iter(targets.values())))
    if any(sorted(parts) == target for parts in groups.values()):
        raise ValueError("negative scene contains another complete target geometry")
    return {
        "scene_primitives_sha256": canonical_sha256(primitives),
        "compound_target_entity_count": 1,
        "target_primitive_count": len(target),
        "second_exact_geometry_entity_found": False,
        "perceptual_distractor_similarity_review_required": True,
    }


def _choose_frames(source: dict, old: dict, maximum: int, seed: int) -> tuple:
    if type(maximum) is not int or not 3 <= maximum <= 128:
        raise ValueError("max_new_train_samples must be an integer from 3 to 128")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an explicit bounded non-bool integer")
    train = [frame for frame in source["frames"] if frame["split"] == "train"]
    held = [sample for sample in old["samples"] if sample["split"] != "train"]
    for field, extract in (
        ("image_sha256", lambda s: s["provenance"]["current"]["image_sha256"]),
        ("world_sha256", lambda s: s["provenance"]["world"]["sha256"]),
    ):
        if {frame[field] for frame in train} & {extract(sample) for sample in held}:
            raise ValueError("increment train source overlaps original holdout RGB/world")
    if {frame["target_model"]["geometry_sha256"] for frame in train} & {
        sample["provenance"]["target_geometry"]["geometry_sha256"]
        for sample in held
        if sample["split"] == "test"
    }:
        raise ValueError("increment train geometry overlaps original test geometry")
    old_current = {
        sample["provenance"]["current"]["image_sha256"]
        for sample in old["samples"]
        if sample["split"] == "train"
    }
    references = {}
    for frame in train:
        if base._reference_ok(frame):
            references.setdefault(_frame_key(frame), []).append(frame)
    pools = {kind: [] for kind in BUCKETS}
    exclusions = {
        "old_train_current_rgb": [],
        "ambiguous": [],
        "no_reference": [],
        "positive_reference_unqualified": [],
    }
    for frame in train:
        if frame["image_sha256"] in old_current:
            exclusions["old_train_current_rgb"].append(frame["frame_id"])
            continue
        label = base._label(frame)
        if label is None:
            exclusions["ambiguous"].append(frame["frame_id"])
            continue
        if _frame_key(frame) not in references:
            exclusions["no_reference"].append(frame["frame_id"])
            continue
        condition = frame["curriculum"]["measured_condition"]
        bucket = "visible" if label == "visible" else condition
        if bucket not in pools:
            raise ValueError("zero-mask negative requires measured absent or fully_occluded")
        if bucket == "visible" and not base._reference_ok(frame):
            exclusions["positive_reference_unqualified"].append(frame["frame_id"])
            continue
        if bucket != "visible":
            _negative_scene(frame)
        pools[bucket].append(frame)
    quotas = {kind: maximum // 3 + (index < maximum % 3) for index, kind in enumerate(BUCKETS)}
    selected = []
    for kind in BUCKETS:
        ranked = sorted(pools[kind], key=lambda frame: _rank(seed, kind, frame))
        if len(ranked) < quotas[kind]:
            raise ValueError(f"insufficient factual train examples for {kind}")
        for frame in ranked[: quotas[kind]]:
            if kind == "visible":
                selected.append((kind, frame, frame))
                continue
            choices = [
                ref
                for ref in references[_frame_key(frame)]
                if ref["image_sha256"] != frame["image_sha256"]
            ]
            if not choices:
                raise ValueError("reference must come from a distinct qualified train RGB")
            reference = min(choices, key=lambda ref: _rank(seed, frame["image_sha256"], ref))
            selected.append((kind, frame, reference))
    return selected, exclusions, quotas, {kind: len(pool) for kind, pool in pools.items()}


def _construct(
    output: Path,
    *,
    source_manifest: Path,
    expected_source_sha256: str,
    base_manifest: Path,
    expected_base_sha256: str,
    base_review_receipt: Path,
    parent_adapter: Path,
    expected_parent_adapter_sha256: str,
    max_new_train_samples: int,
    seed: int,
) -> tuple[dict, dict, dict]:
    output, source_manifest, base_manifest = (
        output.resolve(),
        source_manifest.resolve(strict=True),
        base_manifest.resolve(strict=True),
    )
    _relative(output)
    for path in (source_manifest, base_manifest):
        if output.is_relative_to(path.parent) or path.is_relative_to(output):
            raise ValueError("output must be independent of both sealed datasets")
    bindings = {}
    runtime = _runtime_sources(bindings)
    source_binding = _bind(source_manifest, expected_source_sha256, bindings)
    old_binding = _bind(base_manifest, expected_base_sha256, bindings)
    old = base.validate_grounding_dataset(
        base_manifest, expected_base_sha256, review_receipt=base_review_receipt, require_review=True
    )
    receipt_binding = _bind(
        base_review_receipt, base._digest(base_review_receipt.read_bytes()), bindings
    )
    parent = _bind(parent_adapter, expected_parent_adapter_sha256, bindings)
    source = _audit_source(source_manifest, bindings)
    selected, exclusions, quotas, pool_counts = _choose_frames(
        source, old, max_new_train_samples, seed
    )
    assets, samples = {}, deepcopy(old["samples"])
    for sample in samples:
        for image in sample["model_input"]["images"]:
            path = trainer.bound_file(base_manifest.parent, image["path"], image["sha256"])
            payload = path.read_bytes()
            if len(payload) != image["bytes"]:
                raise ValueError("old exact model image byte length changed")
            base._snapshot(path, bindings, image["sha256"])
            assets[image["path"]] = payload
    increments = []
    for bucket, frame, reference in selected:
        ref_id = canonical_sha256(
            {"source": source_binding["sha256"], "reference_rgb": reference["image_sha256"]}
        )[:32]
        crop = base._crop(reference, base._rgb(reference, source_manifest.parent))
        box = reference["visible_bbox_xyxy_px"]
        ref_image = base._image_record(
            canonical_sha256({"increment_reference_image": ref_id})[:32],
            "reference",
            crop,
            box[2] - box[0],
            box[3] - box[1],
        )
        payload = base._rgb(frame, source_manifest.parent)
        image = base._image_record(
            canonical_sha256({"increment_current_image": frame["image_sha256"]})[:32],
            "current",
            payload,
            frame["width"],
            frame["height"],
        )
        for record, image_bytes in ((ref_image, crop), (image, payload)):
            if record["path"] in assets and assets[record["path"]] != image_bytes:
                raise ValueError("opaque output image collision")
            assets[record["path"]] = image_bytes
        model_input = {
            "prompt": base.PROMPT,
            "target_reference_id": ref_id,
            "images": [ref_image, image],
            "context": deepcopy(base.UNKNOWN_CONTEXT),
        }
        target = {
            "target_reference_id": ref_id,
            "current_target_visibility": base._label(frame),
            **base.UNKNOWN_CONTEXT,
        }
        provenance = {
            "source_clip_id": "non_temporal_"
            + canonical_sha256({"source": source_binding["sha256"], "frame": frame["frame_id"]})[
                :32
            ],
            "original_split": "train",
            "world": {"path": frame["world_path"], "sha256": frame["world_sha256"]},
            "target_geometry": {
                "path": frame["target_model"]["path"],
                "sha256": frame["target_model"]["sha256"],
                "geometry_sha256": frame["target_model"]["geometry_sha256"],
            },
            "source_hashes": deepcopy(frame["source_hashes"]),
            "current": _frame_provenance(frame),
            "reference": _frame_provenance(reference),
            "selection_bucket": bucket,
            "reference_crop_xyxy_half_open": deepcopy(box),
            "reference_crop_kind": "unaltered_RGB_rectangle_not_mask_composite",
            "reference_pairing_semantics": (
                "same_frame_visual_grounding_not_identity_or_temporal"
                if bucket == "visible"
                else "non_temporal_same_world_and_geometry_zero_mask"
            ),
            "negative_scene_check": None if bucket == "visible" else _negative_scene(frame),
            "semantic_identity_confirmed": False,
            "physical_instance_identity_claimed": False,
            "amodal_truth_model_visible": False,
            "aircraft_context": None,
            "label_source": "verified_rendered_visible_mask_and_measured_fraction",
            "label_policy_sha256": canonical_sha256(base.POLICY),
        }
        sample = {
            "sample_id": canonical_sha256({"input": model_input, "kind": "grounding_increment"})[
                :32
            ],
            "split": "train",
            "model_input": model_input,
            "assistant_target": target,
            "model_input_sha256": canonical_sha256(model_input),
            "assistant_target_sha256": canonical_sha256(target),
            "provenance": provenance,
        }
        samples.append(sample)
        increments.append(sample["sample_id"])
    if len({sample["sample_id"] for sample in samples}) != len(samples):
        raise ValueError("duplicate sample IDs")
    result = {
        "schema_version": 1,
        "contract": CONTRACT,
        "builder_id": BUILDER_ID,
        "repository_root": ROOT.resolve().as_posix(),
        "output_directory": _relative(output),
        "base_manifest": old_binding,
        "base_review_receipt": receipt_binding,
        "source_manifest": source_binding,
        "parent_adapter": parent,
        "parent_models": deepcopy(old["parent_models"]),
        "runtime_sources": runtime,
        "label_policy": deepcopy(base.POLICY),
        "seed": seed,
        "max_new_train_samples": max_new_train_samples,
        "new_train_sample_ids": increments,
        "preserved_sample_ids": [sample["sample_id"] for sample in old["samples"]],
        "sample_count": len(samples),
        "samples": samples,
        "split_samples": dict(Counter(sample["split"] for sample in samples)),
        "split_labels": {
            split: dict(
                Counter(
                    sample["assistant_target"]["current_target_visibility"]
                    for sample in samples
                    if sample["split"] == split
                )
            )
            for split in ("train", "val", "test")
        },
        "selection": {
            "predeclared_buckets": list(BUCKETS),
            "quotas": quotas,
            "candidate_pool_counts": pool_counts,
            "exclusions": exclusions,
            "holdout_outputs_used_for_selection": False,
        },
        "boundaries": {
            "old_samples_and_model_inputs_exact": True,
            "original_train_current_rgb_not_relabelled": True,
            "new_samples_original_train_only": True,
            "heldout_rgb_world_overlap": False,
            "validation_geometry_shared_with_train": bool(
                {
                    f["target_model"]["geometry_sha256"]
                    for f in source["frames"]
                    if f["split"] == "train"
                }
                & {
                    s["provenance"]["target_geometry"]["geometry_sha256"]
                    for s in old["samples"]
                    if s["split"] == "val"
                }
            ),
            "test_geometry_shared_with_train": False,
            "non_temporal_reference_pairing": True,
            "same_instance_or_motion_supervision": False,
            "new_independent_world_or_blind_benchmark": False,
            "positive_reference_is_same_frame_visible_crop": True,
            "negative_reference_identity_is_geometry_not_instance": True,
        },
        "supervision_scope": "low_level_rendered_visibility_not_identity_or_actions",
        "model_context": None,
        "max_model_images": 2,
        "model_input_allowlist": deepcopy(old["model_input_allowlist"]),
        "review_status": "pending_independent_fact_review",
        "test_status": "unchanged_previously_diagnosed_holdout_not_blind_benchmark",
        **{flag: False for flag in base.FALSE_FLAGS},
    }
    base._verify(bindings)
    return result, assets, bindings


def build_increment_dataset(
    output_dir: Path,
    *,
    source_manifest: Path = ROOT / SOURCE,
    expected_source_sha256: str = SOURCE_SHA256,
    base_manifest: Path = ROOT / BASE,
    expected_base_sha256: str = BASE_SHA256,
    base_review_receipt: Path | None = None,
    parent_adapter: Path = ROOT / ADAPTER,
    expected_parent_adapter_sha256: str = ADAPTER_SHA256,
    max_new_train_samples: int = 128,
    seed: int = 20261019,
) -> Path:
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("output directory must be new; sealed data is never overwritten")
    if any(parent.is_symlink() for parent in output_dir.absolute().parents):
        raise ValueError("output cannot use a symbolic-link ancestor")
    output = output_dir.resolve()
    document, assets, bindings = _construct(
        output,
        source_manifest=source_manifest,
        expected_source_sha256=expected_source_sha256,
        base_manifest=base_manifest,
        expected_base_sha256=expected_base_sha256,
        base_review_receipt=base_review_receipt or base_manifest.parent / "review-receipt.json",
        parent_adapter=parent_adapter,
        expected_parent_adapter_sha256=expected_parent_adapter_sha256,
        max_new_train_samples=max_new_train_samples,
        seed=seed,
    )
    output.mkdir(parents=True, exist_ok=False)
    for relative, payload in assets.items():
        base._write(output / relative, payload)
    base._verify(bindings)
    manifest = output / "manifest.pending-review.json"
    base._write(
        manifest,
        (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8"
        ),
    )
    return manifest


def validate_grounding_increment_dataset(
    manifest: Path,
    expected_sha256: str | None = None,
    *,
    review_receipt: Path | None = None,
    require_review: bool = False,
) -> dict:
    if type(require_review) is not bool:
        raise ValueError("require_review must be boolean")
    manifest = manifest.resolve(strict=True)
    payload = manifest.read_bytes()
    if expected_sha256 is not None and base._digest(payload) != expected_sha256:
        raise ValueError("increment manifest SHA256 mismatch")
    document = base.shadow._strict_json(payload)
    if document.get("contract") != CONTRACT or document.get("repository_root") != ROOT.as_posix():
        raise ValueError("unknown increment contract or repository root")
    output = ROOT / document["output_directory"]
    if manifest.parent != output.resolve():
        raise ValueError("manifest must remain inside its exclusive output")
    expected, assets, bindings = _construct(
        output,
        source_manifest=ROOT / document["source_manifest"]["path"],
        expected_source_sha256=document["source_manifest"]["sha256"],
        base_manifest=ROOT / document["base_manifest"]["path"],
        expected_base_sha256=document["base_manifest"]["sha256"],
        base_review_receipt=ROOT / document["base_review_receipt"]["path"],
        parent_adapter=ROOT / document["parent_adapter"]["path"],
        expected_parent_adapter_sha256=document["parent_adapter"]["sha256"],
        max_new_train_samples=document["max_new_train_samples"],
        seed=document["seed"],
    )
    if expected != document:
        raise ValueError("increment source, selection, label, holdout or model input changed")
    for relative, original in assets.items():
        path = trainer.bound_file(output, relative, base._digest(original))
        if path.read_bytes() != original:
            raise ValueError("increment RGB/crop differs from the original actual bytes")
        base._snapshot(path, bindings, base._digest(original))
    if require_review and review_receipt is None:
        raise ValueError("independent increment fact review required before optimizer")
    if review_receipt is not None:
        review_payload = review_receipt.read_bytes()
        review = base.shadow._strict_json(review_payload)
        fields = {
            "contract",
            "reviewer",
            "dataset_sha256",
            "dataset_bytes",
            "reviewed_sample_ids",
            "approved_for_development_training",
            *REVIEW_CHECKS,
        }
        if (
            set(review) != fields
            or review["contract"] != REVIEW_CONTRACT
            or not isinstance(review["reviewer"], str)
            or not review["reviewer"].strip()
            or review["reviewer"] == BUILDER_ID
            or review["dataset_sha256"] != base._digest(payload)
            or review["dataset_bytes"] != len(payload)
            or review["reviewed_sample_ids"] != sorted(s["sample_id"] for s in document["samples"])
            or review["approved_for_development_training"] is not True
            or any(review[field] is not True for field in REVIEW_CHECKS)
        ):
            raise ValueError("independent increment review does not authorize these exact bytes")
        if review_receipt.read_bytes() != review_payload:
            raise ValueError("independent review changed during validation")
    if manifest.read_bytes() != payload:
        raise ValueError("increment manifest changed during validation")
    base._verify(bindings)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, default=ROOT / SOURCE)
    parser.add_argument("--source-manifest-sha256", default=SOURCE_SHA256)
    parser.add_argument("--base-manifest", type=Path, default=ROOT / BASE)
    parser.add_argument("--base-manifest-sha256", default=BASE_SHA256)
    parser.add_argument("--base-review-receipt", type=Path)
    parser.add_argument("--parent-adapter", type=Path, default=ROOT / ADAPTER)
    parser.add_argument("--parent-adapter-sha256", default=ADAPTER_SHA256)
    parser.add_argument("--max-new-train-samples", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args(argv)
    path = build_increment_dataset(
        args.output_dir,
        source_manifest=args.source_manifest,
        expected_source_sha256=args.source_manifest_sha256,
        base_manifest=args.base_manifest,
        expected_base_sha256=args.base_manifest_sha256,
        base_review_receipt=args.base_review_receipt,
        parent_adapter=args.parent_adapter,
        expected_parent_adapter_sha256=args.parent_adapter_sha256,
        max_new_train_samples=args.max_new_train_samples,
        seed=args.seed,
    )
    print(
        json.dumps(
            {
                "manifest": path.as_posix(),
                "sha256": trainer.sha256(path),
                "review_status": "pending_independent_fact_review",
                "training_ready": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

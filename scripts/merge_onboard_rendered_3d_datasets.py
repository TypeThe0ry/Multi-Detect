"""Merge independent real Gazebo RGB/Depth datasets for OnboardAI review.

This is a dataset-manifest operation, not a training shortcut.  Every input
must be a truth-free, pixel-level, depth-bound capture from the repository's
3-D mainline.  The result deliberately stays ``training_ready=false`` until
the existing independent review/seal step approves every frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-rendered-3d-dataset-merge-v1"
SOURCE_CONTRACT = "multidetect.onboard-rendered-3d-dataset-v1"
EXCLUDED_MARKERS = ("red_car", "red_green", "color", "colour")


class DatasetMergeError(ValueError):
    """Raised when capture datasets cannot be safely merged."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetMergeError(f"{label} must be an object")
    return value


def _load(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetMergeError(f"dataset is not valid JSON: {path}") from exc
    document = _object(document, f"dataset {path}")
    if document.get("contract") != SOURCE_CONTRACT:
        raise DatasetMergeError(f"unsupported rendered dataset contract: {path}")
    required_false = (
        ("truth_free", True),
        ("truth_injected", False),
        ("pixel_level_rendered", True),
        ("training_ready", False),
        ("depth_required", True),
        ("external_gazebo_runtime", True),
        ("candidate_promoted", False),
        ("final_release_allowed", False),
    )
    for field, expected in required_false:
        if document.get(field) is not expected:
            raise DatasetMergeError(f"{path.name}: {field} must be {expected!r}")
    if document.get("reviewed_annotation_manifest") is not None:
        raise DatasetMergeError(f"{path.name}: reviewed dataset cannot be merged here")
    rows = document.get("rows")
    manifests = document.get("source_manifests")
    if not isinstance(rows, list) or not rows:
        raise DatasetMergeError(f"{path.name}: rows are empty")
    if not isinstance(manifests, list) or not manifests:
        raise DatasetMergeError(f"{path.name}: source_manifests are empty")
    return document


def _row_identity(row: object, index: int, dataset: Path) -> tuple[str, str, str]:
    raw = _object(row, f"{dataset.name} row {index}")
    source = _object(raw.get("source"), f"{dataset.name} row {index}.source")
    scenario = source.get("scenario_id")
    frame = source.get("frame_id")
    payload = source.get("rgb_payload_sha256")
    if not all(isinstance(value, str) and value for value in (scenario, frame, payload)):
        raise DatasetMergeError(f"{dataset.name} row {index} has no immutable frame identity")
    if (
        len(payload) != 64
        or payload.lower() != payload
        or any(c not in "0123456789abcdef" for c in payload)
    ):
        raise DatasetMergeError(f"{dataset.name} row {index} RGB payload hash is invalid")
    if source.get("truth_injected") is not False:
        raise DatasetMergeError(f"{dataset.name} row {index} is not truth-free")
    if not isinstance(source.get("depth_payload_sha256"), str):
        raise DatasetMergeError(f"{dataset.name} row {index} has no depth payload binding")
    return scenario, frame, payload


def merge_datasets(paths: list[Path]) -> dict[str, Any]:
    if len(paths) < 2:
        raise DatasetMergeError("at least two independent rendered datasets are required")
    resolved = [path.expanduser().resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise DatasetMergeError("the same rendered dataset was supplied more than once")
    documents = [_load(path) for path in resolved]
    first = documents[0]
    contract_fields = ("feature_names", "task_action_order", "flight_action_order")
    for field in contract_fields:
        expected = first.get(field)
        for path, document in zip(resolved[1:], documents[1:], strict=True):
            if document.get(field) != expected:
                raise DatasetMergeError(f"shared OnboardAI contract mismatch: {field} in {path}")

    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    source_manifests: list[dict[str, Any]] = []
    manifest_keys: set[tuple[object, object]] = set()
    dataset_bindings: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    scene_ids: set[str] = set()
    depth_rows = 0
    depth_measurement_rows = 0
    calibrated_geometry_rows = 0

    for path, document in zip(resolved, documents, strict=True):
        dataset_rows = document["rows"]
        assert isinstance(dataset_rows, list)
        dataset_bindings.append(
            {"path": path.as_posix(), "sha256": _sha256(path), "row_count": len(dataset_rows)}
        )
        for index, raw in enumerate(dataset_rows):
            identity = _row_identity(raw, index, path)
            if identity in identities:
                raise DatasetMergeError(f"duplicate RGB frame identity across datasets: {identity}")
            identities.add(identity)
            row = dict(_object(raw, f"{path.name} row {index}"))
            source = _object(row.get("source"), f"{path.name} row {index}.source")
            scenario_id = identity[0]
            if any(marker in scenario_id.lower() for marker in EXCLUDED_MARKERS):
                raise DatasetMergeError(
                    f"excluded colour fixture scene in {path.name}: {scenario_id}"
                )
            scene_ids.add(scenario_id)
            event = source.get("event_kind")
            if isinstance(event, str):
                event_counts[event] += 1
            if isinstance(source.get("depth_payload_sha256"), str):
                depth_rows += 1
            if (
                isinstance(source.get("depth_median"), (int, float))
                and float(source["depth_median"]) > 0.0
            ):
                depth_measurement_rows += 1
            if source.get("geometry_source") == "camera_calibration_depth_pose_ned":
                calibrated_geometry_rows += 1
            rows.append(row)
        manifests = document["source_manifests"]
        assert isinstance(manifests, list)
        for manifest in manifests:
            item = _object(manifest, f"{path.name} source manifest")
            key = (item.get("path"), item.get("sha256"))
            if key not in manifest_keys:
                manifest_keys.add(key)
                source_manifests.append(dict(item))

    if len(scene_ids) < 2:
        raise DatasetMergeError("merged capture must contain at least two independent 3-D scenes")
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "feature_names": list(first["feature_names"]),
        "task_action_order": list(first["task_action_order"]),
        "flight_action_order": list(first["flight_action_order"]),
        "rows": rows,
        "source_datasets": dataset_bindings,
        "source_manifests": source_manifests,
        "scene_ids": sorted(scene_ids),
        "pixel_level_rendered": True,
        "truth_free": True,
        "truth_injected": False,
        "labels_from": "sensor_contract_heuristic_plumbing_only_pending_independent_review",
        "training_ready": False,
        "reviewed_annotation_manifest": None,
        "annotation_supervision_only": False,
        "annotation_runtime_input": False,
        "depth_required": True,
        "depth_rows": depth_rows,
        "depth_measurement_rows": depth_measurement_rows,
        "calibrated_geometry_rows": calibrated_geometry_rows,
        "external_gazebo_runtime": True,
        "jetson_hardware_evidence": False,
        "pixhawk_hardware_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "event_coverage": {
            "contract": "multidetect.onboard-3d-event-coverage-v1",
            "event_kind_counts": dict(sorted(event_counts.items())),
            "truth_free": True,
            "training_ready": False,
        },
        "merge_policy": {
            "contract": "multidetect.onboard-rendered-3d-dataset-merge-v1",
            "exact_frame_identity": "scenario_id+frame_id+rgb_payload_sha256",
            "duplicate_frames_rejected": True,
            "review_required_before_training": True,
            "synthetic_rows_included": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = merge_datasets(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "rows": len(document["rows"]),
                "scenes": document["scene_ids"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DatasetMergeError", "merge_datasets"]

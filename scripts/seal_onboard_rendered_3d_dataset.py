#!/usr/bin/env python3
"""Seal an independently reviewed Gazebo RGB/Depth corpus for OnboardAI.

The rendered-data builder intentionally produces a plumbing-only dataset.  This
command is the explicit human-review boundary: it consumes one review JSONL
record per rendered row, verifies that every record is bound to the exact RGB
and FLOAT32-depth payload hashes, applies the reviewed labels to a new copy,
and writes a hash-bound receipt beside that copy.  It never reads simulator
truth, creates labels from a world file, promotes a model, or enables flight
control.

The command requires ``--confirm-reviewed`` so a capture cannot accidentally
become training-ready merely because a file exists.  The annotator and
reviewer must be different identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import (  # noqa: E402
    ONBOARD_MODEL_ACTION_ORDER,
    ONBOARD_MODEL_TASK_ACTION_ORDER,
)

CONTRACT = "multidetect.onboard-rendered-3d-review-v1"
RECEIPT_SCHEMA = "multidetect.onboard-rendered-3d-review-receipt"
RENDERED_DATASET_CONTRACTS = frozenset(
    {
        "multidetect.onboard-rendered-3d-dataset-v1",
        "multidetect.onboard-rendered-3d-dataset-merge-v1",
    }
)
REVIEW_ROW_KEYS = frozenset(
    {
        "row_index",
        "frame_id",
        "rgb_payload_sha256",
        "depth_payload_sha256",
        "task_label",
        "flight_label",
        "regression",
        "decision",
    }
)
FORBIDDEN_REVIEW_KEYS = frozenset(
    {
        "truth",
        "ground_truth",
        "target_world_position",
        "target_model_id",
        "nearest_object_truth",
        "scenario_truth",
    }
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _resolve_inside(path: Path, raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} path is missing")
    candidate = (path.parent / raw).resolve()
    try:
        candidate.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the dataset directory") from exc
    return candidate


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_reviewed_at(value: str) -> None:
    if not value.strip() or not value.endswith("Z"):
        raise ValueError("reviewed-at-utc must be a non-empty UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("reviewed-at-utc is not an ISO-8601 timestamp") from exc


def _load_review_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"review JSONL cannot be read: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"review JSONL line {line_number} is invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != REVIEW_ROW_KEYS:
            raise ValueError(f"review JSONL line {line_number} has non-canonical fields")
        if FORBIDDEN_REVIEW_KEYS.intersection(value):
            raise ValueError(f"review JSONL line {line_number} contains simulator truth")
        row_index = value.get("row_index")
        if isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0:
            raise ValueError(f"review JSONL line {line_number} row_index is invalid")
        if value.get("decision") != "approve":
            raise ValueError(f"review JSONL line {line_number} is not approved")
        if not isinstance(value.get("frame_id"), str) or not value["frame_id"].strip():
            raise ValueError(f"review JSONL line {line_number} frame_id is invalid")
        for field in ("rgb_payload_sha256", "depth_payload_sha256"):
            _require_sha256(value.get(field), f"review JSONL line {line_number} {field}")
        for field, limit in (
            ("task_label", len(ONBOARD_MODEL_TASK_ACTION_ORDER)),
            ("flight_label", len(ONBOARD_MODEL_ACTION_ORDER)),
        ):
            label = value.get(field)
            if isinstance(label, bool) or not isinstance(label, int) or not 0 <= label < limit:
                raise ValueError(f"review JSONL line {line_number} {field} is invalid")
        regression = value.get("regression")
        if (
            not isinstance(regression, list)
            or len(regression) != 4
            or not all(
                isinstance(item, (int, float)) and math.isfinite(float(item)) for item in regression
            )
        ):
            raise ValueError(f"review JSONL line {line_number} regression is invalid")
        rows.append(value)
    if not rows:
        raise ValueError("review JSONL contains no rows")
    rows.sort(key=lambda item: int(item["row_index"]))
    return rows


def _verify_dataset_input(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("contract") not in RENDERED_DATASET_CONTRACTS:
        raise ValueError("input is not the rendered 3-D dataset contract")
    if document.get("pixel_level_rendered") is not True:
        raise ValueError("input is not pixel-level rendered")
    if document.get("truth_free") is not True or document.get("truth_injected") is not False:
        raise ValueError("input is not truth-free")
    if document.get("depth_required") is not True:
        raise ValueError("input does not require FLOAT32 depth")
    if document.get("external_gazebo_runtime") is not True:
        raise ValueError("input is not bound to an external Gazebo runtime")
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("input rendered dataset rows are empty")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("source"), dict):
            raise ValueError(f"input rendered row {index} is missing source provenance")
        source = row["source"]
        if not isinstance(source.get("frame_id"), str) or not source["frame_id"].strip():
            raise ValueError(f"input rendered row {index} frame_id is invalid")
        _require_sha256(source.get("rgb_payload_sha256"), f"input rendered row {index} RGB hash")
        _require_sha256(
            source.get("depth_payload_sha256"), f"input rendered row {index} depth hash"
        )
        if source.get("truth_injected") is not False:
            raise ValueError(f"input rendered row {index} has truth provenance")
    return rows


def seal_dataset(
    input_path: Path,
    review_path: Path,
    output_path: Path,
    *,
    annotator: str,
    reviewer: str,
    reviewed_at_utc: str,
    acknowledge_review: bool,
    force: bool = False,
) -> dict[str, Any]:
    if not acknowledge_review:
        raise ValueError("--confirm-reviewed is required before sealing training data")
    if not annotator.strip() or not reviewer.strip() or annotator == reviewer:
        raise ValueError("annotator and reviewer must be non-empty and different")
    _validate_reviewed_at(reviewed_at_utc)
    input_path = input_path.resolve()
    review_path = review_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file() or not review_path.is_file():
        raise ValueError("input dataset and review JSONL must exist")
    if output_path.exists() and not force:
        raise ValueError(f"output exists; pass --force to replace: {output_path}")
    document = _load_object(input_path, "input rendered dataset")
    rows = _verify_dataset_input(document)
    review_rows = _load_review_rows(review_path)
    if [int(item["row_index"]) for item in review_rows] != list(range(len(rows))):
        raise ValueError("review rows must cover every rendered row exactly once in row order")
    seen_rgb: set[str] = set()
    for index, (dataset_row, review_row) in enumerate(zip(rows, review_rows, strict=True)):
        source = dataset_row["source"]
        if review_row["frame_id"] != source["frame_id"]:
            raise ValueError(f"review frame binding mismatch at row {index}")
        if review_row["rgb_payload_sha256"] != source["rgb_payload_sha256"]:
            raise ValueError(f"review RGB binding mismatch at row {index}")
        if review_row["depth_payload_sha256"] != source["depth_payload_sha256"]:
            raise ValueError(f"review depth binding mismatch at row {index}")
        if review_row["rgb_payload_sha256"] in seen_rgb:
            raise ValueError("duplicate RGB payload in reviewed rendered rows")
        seen_rgb.add(review_row["rgb_payload_sha256"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    review_copy = output_path.parent / f"{output_path.stem}.reviewed-rows.jsonl"
    receipt_path = output_path.parent / f"{output_path.stem}.review-receipt.json"
    review_payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in review_rows
    ).encode("utf-8")
    review_copy.write_bytes(review_payload)

    source_manifest_hashes = [
        item.get("sha256")
        for item in document.get("source_manifests", [])
        if isinstance(item, dict) and isinstance(item.get("sha256"), str)
    ]
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "schema_version": 1,
        "contract": CONTRACT,
        "event": "onboard_rendered_3d_rows_reviewed",
        "source_dataset_sha256": _sha256(input_path),
        "source_manifest_sha256": source_manifest_hashes,
        "review_manifest": {
            "path": review_copy.name,
            "sha256": _sha256(review_copy),
            "bytes": len(review_payload),
            "row_count": len(review_rows),
            "canonical_sha256": _canonical_sha256({"rows": review_rows}),
        },
        "annotator": annotator,
        "reviewer": reviewer,
        "reviewed_at_utc": reviewed_at_utc,
        "independent_review_recorded": True,
        "claims": {
            "labels_complete": True,
            "truth_free_inputs": True,
            "no_scenario_truth_labels": True,
            "payload_bindings_verified": True,
            "release_ready": False,
        },
    }
    unsigned = dict(receipt)
    receipt["canonical_hash"] = _canonical_sha256(unsigned)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    sealed = dict(document)
    sealed_rows = [dict(row) for row in rows]
    for sealed_row, review_row in zip(sealed_rows, review_rows, strict=True):
        sealed_row["task_label"] = review_row["task_label"]
        sealed_row["flight_label"] = review_row["flight_label"]
        sealed_row["regression"] = list(review_row["regression"])
    sealed["rows"] = sealed_rows
    sealed["labels_from"] = "independent_reviewed_3d_rows"
    sealed["training_ready"] = True
    sealed["reviewed_annotation_manifest"] = {
        "path": receipt_path.name,
        "sha256": _sha256(receipt_path),
    }
    sealed["candidate_promoted"] = False
    sealed["final_release_allowed"] = False
    output_path.write_text(
        json.dumps(sealed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "output": output_path.as_posix(),
        "review_receipt": receipt_path.as_posix(),
        "review_manifest": review_copy.as_posix(),
        "rows": len(sealed_rows),
        "training_ready": True,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--review-jsonl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--annotator", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewed-at-utc", required=True)
    parser.add_argument("--confirm-reviewed", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = seal_dataset(
            args.input,
            args.review_jsonl,
            args.output,
            annotator=args.annotator,
            reviewer=args.reviewer,
            reviewed_at_utc=args.reviewed_at_utc,
            acknowledge_review=args.confirm_reviewed,
            force=args.force,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

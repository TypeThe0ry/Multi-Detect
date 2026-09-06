"""Human annotation workspace bound to an immutable deployment replay intake."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_sha256,
    require_text,
)
from .backends.raw_replay import RawSensorReplayRecording
from .domain import BoundingBox
from .replay_set import REPLAY_SET_STRATA, build_replay_set_manifest, load_replay_set_manifest
from .replay_set_intake import INTAKE_RECEIPT_NAME, verify_deployment_replay_intake

ANNOTATION_WORKSPACE_SCHEMA_VERSION = 1


def create_replay_annotation_workspace(
    intake_root: str | Path,
    output: str | Path,
    *,
    class_names: Sequence[str] = ("flame", "smoke"),
) -> dict[str, object]:
    """Create a non-overwriting editable task; empty annotations stay unannotated."""

    intake = Path(intake_root).resolve(strict=True)
    intake_report = verify_deployment_replay_intake(intake)
    output_path = Path(output).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise AutonomyContractError(f"annotation workspace already exists: {output_path}")
    classes = _class_names(class_names)
    frames = _expected_frames(intake)
    document: dict[str, object] = {
        "schema_version": ANNOTATION_WORKSPACE_SCHEMA_VERSION,
        "intake_canonical_hash": intake_report["canonical_hash"],
        "dataset_id": intake_report["dataset_id"],
        "dataset_version": intake_report["dataset_version"],
        "class_names": list(classes),
        "assignment_hash": _assignment_hash(frames, intake_report, classes),
        "annotator": None,
        "reviewer": None,
        "reviewed_at_utc": None,
        "frames": frames,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(output_path, document)
    return inspect_replay_annotation_workspace(intake, output_path)


def inspect_replay_annotation_workspace(
    intake_root: str | Path,
    workspace: str | Path,
) -> dict[str, object]:
    """Recompute assignment identity and report completion without approving it."""

    intake = Path(intake_root).resolve(strict=True)
    intake_report = verify_deployment_replay_intake(intake)
    document = _load_json(workspace, "annotation workspace")
    parsed = _validate_workspace(
        document,
        intake_report=intake_report,
        expected_frames=_expected_frames(intake),
        require_complete=False,
    )
    status_counts = {"unannotated": 0, "positive": 0, "negative": 0}
    for frame in parsed["frames"]:
        status_counts[frame["annotation"]["status"]] += 1
    complete = status_counts["unannotated"] == 0
    identities_distinct = bool(parsed["annotator"] and parsed["reviewer"]) and (
        str(parsed["annotator"]).casefold() != str(parsed["reviewer"]).casefold()
    )
    return {
        "event": "replay_annotation_workspace_inspected",
        "assignment_hash": parsed["assignment_hash"],
        "frame_count": len(parsed["frames"]),
        "status_counts": status_counts,
        "all_frames_annotated": complete,
        "annotator_reviewer_distinct": identities_distinct,
        "finalization_ready": complete
        and identities_distinct
        and parsed["reviewed_at_utc"] is not None,
        "release_ready": False,
    }


def finalize_replay_annotation_workspace(
    intake_root: str | Path,
    workspace: str | Path,
    output: str | Path,
) -> dict[str, object]:
    """Atomically export reviewed truth plus a replay manifest into a new directory."""

    intake = Path(intake_root).resolve(strict=True)
    intake_report = verify_deployment_replay_intake(intake)
    workspace_path = Path(workspace)
    if workspace_path.is_symlink():
        raise AutonomyContractError("annotation workspace must not be a symlink")
    document = _load_json(workspace_path, "annotation workspace")
    parsed = _validate_workspace(
        document,
        intake_report=intake_report,
        expected_frames=_expected_frames(intake),
        require_complete=True,
    )
    output_path = Path(output).absolute()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise AutonomyContractError(f"annotation output already exists: {output_path}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=output_path.parent))
    try:
        intake_receipt = _load_json(intake / INTAKE_RECEIPT_NAME, "intake receipt")
        shutil.copyfile(intake / INTAKE_RECEIPT_NAME, temporary / INTAKE_RECEIPT_NAME)
        for entry in intake_receipt["entries"]:
            for artifact in entry["artifacts"]:
                relative = _relative_path(artifact["path"], "intake artifact path")
                source = intake / Path(*PurePosixPath(relative).parts)
                if source.is_symlink():
                    raise AutonomyContractError("intake artifacts must not become symlinks")
                destination = temporary / Path(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

        truth_refs: list[dict[str, object]] = []
        for stratum in REPLAY_SET_STRATA:
            rows = [frame for frame in parsed["frames"] if frame["stratum"] == stratum]
            payload = _truth_payload(rows)
            truth_path = temporary / stratum / "ground-truth.jsonl"
            truth_path.write_bytes(payload)
            truth_refs.append(
                {
                    "stratum": stratum,
                    "path": f"{stratum}/ground-truth.jsonl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "frame_count": len(rows),
                }
            )
        manifest_path = temporary / "replay-set.manifest.json"
        manifest = build_replay_set_manifest(
            temporary,
            manifest_path,
            dataset_id=parsed["dataset_id"],
            dataset_version=parsed["dataset_version"],
            class_names=parsed["class_names"],
            source_kind="deployment_recording",
            annotations_reviewed=True,
        )
        workspace_bytes = workspace_path.resolve(strict=True).read_bytes()
        (temporary / "annotation-workspace.reviewed.json").write_bytes(workspace_bytes)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "event": "replay_annotations_finalized",
            "intake_canonical_hash": intake_report["canonical_hash"],
            "assignment_hash": parsed["assignment_hash"],
            "workspace_sha256": hashlib.sha256(workspace_bytes).hexdigest(),
            "annotator": parsed["annotator"],
            "reviewer": parsed["reviewer"],
            "reviewed_at_utc": parsed["reviewed_at_utc"],
            "frame_count": len(parsed["frames"]),
            "truth_artifacts": truth_refs,
            "replay_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "replay_manifest_canonical_hash": manifest.canonical_hash,
            "claims": {
                "annotations_complete": True,
                "independent_review_recorded": True,
                "predictions_complete": False,
                "accuracy_validated": False,
                "release_ready": False,
            },
        }
        receipt["canonical_hash"] = canonical_sha256(receipt)
        _write_json_atomic(temporary / "annotation-review.receipt.json", receipt)
        verify_deployment_replay_intake(temporary)
        verify_finalized_replay_annotations(temporary)
        if output_path.exists() or output_path.is_symlink():
            raise AutonomyContractError(f"annotation output already exists: {output_path}")
        os.rename(temporary, output_path)
        return verify_finalized_replay_annotations(output_path)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_finalized_replay_annotations(
    root: str | Path,
    *,
    replay_manifest_name: str = "replay-set.manifest.json",
) -> dict[str, object]:
    """Reopen every annotation input/output binding and reject a modified bundle."""

    requested_root = Path(root)
    if requested_root.is_symlink():
        raise AutonomyContractError("finalized annotation root must not be a symlink")
    root_path = requested_root.resolve(strict=True)
    intake_report = verify_deployment_replay_intake(root_path)
    receipt_path = root_path / "annotation-review.receipt.json"
    workspace_path = root_path / "annotation-workspace.reviewed.json"
    if receipt_path.is_symlink() or workspace_path.is_symlink():
        raise AutonomyContractError("annotation receipt/workspace must not be a symlink")
    receipt = _load_json(receipt_path, "annotation review receipt")
    required = {
        "schema_version",
        "event",
        "intake_canonical_hash",
        "assignment_hash",
        "workspace_sha256",
        "annotator",
        "reviewer",
        "reviewed_at_utc",
        "frame_count",
        "truth_artifacts",
        "replay_manifest_sha256",
        "replay_manifest_canonical_hash",
        "claims",
        "canonical_hash",
    }
    if set(receipt) != required or receipt["schema_version"] != 1:
        raise AutonomyContractError(
            "annotation review receipt has unknown/missing fields or schema"
        )
    unsigned = dict(receipt)
    observed_canonical = require_sha256(unsigned.pop("canonical_hash"), "canonical_hash")
    if canonical_sha256(unsigned) != observed_canonical:
        raise AutonomyContractError("annotation review receipt canonical_hash mismatch")
    if receipt["event"] != "replay_annotations_finalized":
        raise AutonomyContractError("annotation review receipt event is invalid")
    workspace_bytes = workspace_path.read_bytes()
    if hashlib.sha256(workspace_bytes).hexdigest() != require_sha256(
        receipt["workspace_sha256"], "workspace_sha256"
    ):
        raise AutonomyContractError("reviewed annotation workspace hash mismatch")
    workspace = _validate_workspace(
        _load_json(workspace_path, "reviewed annotation workspace"),
        intake_report=intake_report,
        expected_frames=_expected_frames(root_path),
        require_complete=True,
    )
    if (
        receipt["intake_canonical_hash"] != intake_report["canonical_hash"]
        or receipt["assignment_hash"] != workspace["assignment_hash"]
        or receipt["annotator"] != workspace["annotator"]
        or receipt["reviewer"] != workspace["reviewer"]
        or receipt["reviewed_at_utc"] != workspace["reviewed_at_utc"]
        or require_int(receipt["frame_count"], "frame_count", minimum=1) != len(workspace["frames"])
    ):
        raise AutonomyContractError("annotation review receipt identity mismatch")
    claims = receipt["claims"]
    expected_claims = {
        "annotations_complete": True,
        "independent_review_recorded": True,
        "predictions_complete": False,
        "accuracy_validated": False,
        "release_ready": False,
    }
    if claims != expected_claims:
        raise AutonomyContractError("annotation review receipt claims are invalid")
    truth = receipt["truth_artifacts"]
    if (
        not isinstance(truth, list)
        or not all(isinstance(item, dict) for item in truth)
        or [item.get("stratum") for item in truth] != list(REPLAY_SET_STRATA)
    ):
        raise AutonomyContractError("annotation truth artifact inventory is invalid")
    for item in truth:
        if not isinstance(item, dict) or set(item) != {
            "stratum",
            "path",
            "sha256",
            "bytes",
            "frame_count",
        }:
            raise AutonomyContractError("annotation truth artifact entry is invalid")
        relative = _relative_path(item["path"], "truth artifact path")
        if relative != f"{item['stratum']}/ground-truth.jsonl":
            raise AutonomyContractError("annotation truth artifact path is invalid")
        truth_path = root_path / Path(*PurePosixPath(relative).parts)
        if truth_path.is_symlink():
            raise AutonomyContractError("annotation truth artifacts must not be symlinks")
        payload = truth_path.read_bytes()
        rows = [frame for frame in workspace["frames"] if frame["stratum"] == item["stratum"]]
        if (
            hashlib.sha256(payload).hexdigest()
            != require_sha256(item["sha256"], "truth artifact sha256")
            or len(payload) != require_int(item["bytes"], "truth artifact bytes", minimum=1)
            or require_int(item["frame_count"], "truth artifact frame_count", minimum=1)
            != len(rows)
            or payload != _truth_payload(rows)
        ):
            raise AutonomyContractError(f"annotation truth artifact hash mismatch: {relative}")
    manifest_relative = _relative_path(replay_manifest_name, "replay_manifest_name")
    if PurePosixPath(manifest_relative).parent != PurePosixPath("."):
        raise AutonomyContractError("replay_manifest_name must be a root-level filename")
    manifest_path = root_path / manifest_relative
    if manifest_path.is_symlink():
        raise AutonomyContractError("annotation replay manifest must not be a symlink")
    manifest_bytes = manifest_path.read_bytes()
    manifest = load_replay_set_manifest(manifest_path, require_complete=True)
    if hashlib.sha256(manifest_bytes).hexdigest() != require_sha256(
        receipt["replay_manifest_sha256"], "replay_manifest_sha256"
    ) or manifest.canonical_hash != require_sha256(
        receipt["replay_manifest_canonical_hash"], "replay_manifest_canonical_hash"
    ):
        raise AutonomyContractError("annotation replay manifest binding mismatch")
    return {
        "event": "replay_annotations_verified",
        "output": str(root_path),
        "frame_count": len(workspace["frames"]),
        "manifest_canonical_hash": manifest.canonical_hash,
        "annotations_reviewed": True,
        "predictions_complete": False,
        "accuracy_validated": False,
        "release_ready": False,
        "canonical_hash": observed_canonical,
    }


def _validate_workspace(
    document: Mapping[str, object],
    *,
    intake_report: Mapping[str, object],
    expected_frames: Sequence[Mapping[str, object]],
    require_complete: bool,
) -> dict[str, object]:
    required = {
        "schema_version",
        "intake_canonical_hash",
        "dataset_id",
        "dataset_version",
        "class_names",
        "assignment_hash",
        "annotator",
        "reviewer",
        "reviewed_at_utc",
        "frames",
    }
    if set(document) != required or document["schema_version"] != 1:
        raise AutonomyContractError("annotation workspace has unknown/missing fields or schema")
    if document["intake_canonical_hash"] != intake_report["canonical_hash"]:
        raise AutonomyContractError("annotation workspace intake binding mismatch")
    if (
        document["dataset_id"] != intake_report["dataset_id"]
        or document["dataset_version"] != intake_report["dataset_version"]
    ):
        raise AutonomyContractError("annotation workspace dataset identity mismatch")
    classes = _class_names(document["class_names"])
    raw_frames = document["frames"]
    if not isinstance(raw_frames, list) or not raw_frames:
        raise AutonomyContractError("annotation workspace frames must be a non-empty array")
    frames: list[dict[str, object]] = []
    for index, raw in enumerate(raw_frames):
        if not isinstance(raw, dict) or set(raw) != {"stratum", "frame_id", "image", "annotation"}:
            raise AutonomyContractError(f"frames[{index}] has unknown or missing fields")
        stratum = require_text(raw["stratum"], f"frames[{index}].stratum")
        frame_id = require_text(raw["frame_id"], f"frames[{index}].frame_id")
        image = raw["image"]
        if not isinstance(image, dict) or set(image) != {"path", "sha256", "width", "height"}:
            raise AutonomyContractError(f"frames[{index}].image is invalid")
        annotation = raw["annotation"]
        if not isinstance(annotation, dict) or set(annotation) != {"status", "objects"}:
            raise AutonomyContractError(f"frames[{index}].annotation is invalid")
        status = annotation["status"]
        objects = annotation["objects"]
        if status == "unannotated":
            if objects is not None:
                raise AutonomyContractError("unannotated frames must use objects=null")
            if require_complete:
                raise AutonomyContractError("every frame must be explicitly positive or negative")
        elif status in {"positive", "negative"}:
            objects = _objects(objects, classes, index=index)
            if status == "positive" and not objects:
                raise AutonomyContractError("positive frames require at least one object")
            if status == "negative" and objects:
                raise AutonomyContractError("negative frames must have an empty objects array")
        else:
            raise AutonomyContractError(
                "annotation status must be unannotated, positive, or negative"
            )
        frames.append(
            {
                **raw,
                "stratum": stratum,
                "frame_id": frame_id,
                "annotation": {"status": status, "objects": objects},
            }
        )
    identities = [
        {"stratum": frame["stratum"], "frame_id": frame["frame_id"], "image": frame["image"]}
        for frame in frames
    ]
    expected_identities = [
        {"stratum": frame["stratum"], "frame_id": frame["frame_id"], "image": frame["image"]}
        for frame in expected_frames
    ]
    if identities != expected_identities:
        raise AutonomyContractError("annotation frame assignment does not match verified intake")
    assignment_hash = require_text(document["assignment_hash"], "assignment_hash").lower()
    if assignment_hash != _assignment_hash(frames, intake_report, classes):
        raise AutonomyContractError("annotation workspace assignment_hash mismatch")
    annotator = _optional_text(document["annotator"], "annotator")
    reviewer = _optional_text(document["reviewer"], "reviewer")
    reviewed_at = _reviewed_at(document["reviewed_at_utc"])
    if require_complete:
        if annotator is None or reviewer is None or annotator.casefold() == reviewer.casefold():
            raise AutonomyContractError("annotator and reviewer must be present and distinct")
        if reviewed_at is None:
            raise AutonomyContractError("reviewed_at_utc is required for finalization")
    return {
        **document,
        "class_names": classes,
        "frames": frames,
        "annotator": annotator,
        "reviewer": reviewer,
        "reviewed_at_utc": reviewed_at,
        "assignment_hash": assignment_hash,
    }


def _expected_frames(intake: Path) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    for stratum in REPLAY_SET_STRATA:
        recording = RawSensorReplayRecording.from_manifest(
            intake / stratum / "recording.manifest.json"
        )
        for frame in recording.frames:
            frames.append(
                {
                    "stratum": stratum,
                    "frame_id": frame.frame_id,
                    "image": {
                        "path": f"{stratum}/{frame.image.relative_path}",
                        "sha256": frame.image.sha256,
                        "width": frame.image.width,
                        "height": frame.image.height,
                    },
                    "annotation": {"status": "unannotated", "objects": None},
                }
            )
    return frames


def _assignment_hash(
    frames: Sequence[Mapping[str, object]], intake: Mapping[str, object], classes: Sequence[str]
) -> str:
    identities = [
        {"stratum": f["stratum"], "frame_id": f["frame_id"], "image": f["image"]} for f in frames
    ]
    return canonical_sha256(
        {
            "schema_version": 1,
            "intake_canonical_hash": intake["canonical_hash"],
            "dataset_id": intake["dataset_id"],
            "dataset_version": intake["dataset_version"],
            "class_names": list(classes),
            "frames": identities,
        }
    )


def _objects(raw: object, classes: Sequence[str], *, index: int) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise AutonomyContractError(f"frames[{index}].objects must be an array")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"label", "bbox"}:
            raise AutonomyContractError("annotation objects require exactly label and bbox")
        label = require_text(item["label"], "object.label").lower()
        label = "flame" if label == "fire" else label
        if label not in classes:
            raise AutonomyContractError(f"annotation label is not in class_names: {label}")
        bbox = item["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(float(v))
                for v in bbox
            )
        ):
            raise AutonomyContractError("annotation bbox must contain four finite numbers")
        box = BoundingBox(*(float(value) for value in bbox))
        result.append({"label": label, "bbox": [box.x1, box.y1, box.x2, box.y2]})
    return result


def _truth_payload(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(
        (
            json.dumps(
                {"frame_id": row["frame_id"], "objects": row["annotation"]["objects"]},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def _class_names(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise AutonomyContractError("class_names must be an array")
    values = tuple(
        (
            "flame"
            if require_text(v, "class_name").lower() == "fire"
            else require_text(v, "class_name").lower()
        )
        for v in raw
    )
    if not values or len(values) != len(set(values)):
        raise AutonomyContractError("class_names must be non-empty and unique")
    return values


def _optional_text(raw: object, name: str) -> str | None:
    return None if raw is None else require_text(raw, name)


def _reviewed_at(raw: object) -> str | None:
    if raw is None:
        return None
    text = require_text(raw, "reviewed_at_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyContractError("reviewed_at_utc must be an ISO-8601 timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AutonomyContractError("reviewed_at_utc must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative_path(raw: object, name: str) -> str:
    text = require_text(raw, name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise AutonomyContractError(f"{name} must be relative and contained")
    return path.as_posix()


def _load_json(path: str | Path, name: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    source = Path(path)
    if source.is_symlink():
        raise AutonomyContractError(f"{name} must not be a symlink")
    try:
        value = json.loads(
            source.read_text(encoding="utf-8-sig"),
            parse_constant=lambda v: (_ for _ in ()).throw(ValueError(f"non-finite {v}")),
            object_pairs_hook=pairs,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AutonomyContractError(f"{name} is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AutonomyContractError(f"{name} must be an object")
    return value


def _write_json_atomic(path: Path, document: Mapping[str, object]) -> None:
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
        if path.exists() or path.is_symlink():
            raise AutonomyContractError(f"output already exists: {path}")
        os.rename(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "create_replay_annotation_workspace",
    "inspect_replay_annotation_workspace",
    "finalize_replay_annotation_workspace",
    "verify_finalized_replay_annotations",
]

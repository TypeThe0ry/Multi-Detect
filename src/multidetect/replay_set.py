"""Strict, hash-bound manifest for stratified detector replay sets.

The deployment-domain gate needs more than a directory full of images.  A
replay set must bind the raw-sensor recording, reviewed ground truth and (when
available) a prediction stream to one stable dataset identity.  This module
keeps that binding deliberately separate from model-quality approval: a
structurally complete set still reports ``accuracy_validated=false`` until the
normal evaluator produces a reviewed report.

The manifest is intentionally read-only.  Loading it never opens a camera,
Pixhawk, network socket or actuator.  It does re-verify every referenced file
and re-loads the raw recording through :class:`RawSensorReplayRecording`, so a
changed image/event stream or frame misalignment fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_bool,
    require_int,
    require_schema_version,
    require_sha256,
    require_string_array,
    require_text,
    strict_object,
)
from .backends.raw_replay import RawSensorReplayRecording
from .evaluation import load_ground_truth_jsonl, load_prediction_jsonl

REPLAY_SET_STRATA = (
    "day",
    "night",
    "haze",
    "reflection",
    "smoke_only",
    "no_fire",
)
# Every positive deployment condition needs at least one independently
# labelled object and one detector output.  ``no_fire`` is intentionally the
# negative-only control stratum and therefore is not part of this list.  Keep
# the policy next to the canonical stratum names so the evaluator, readiness
# report and release tooling cannot silently disagree about the denominators.
REPLAY_SET_POSITIVE_STRATA = (
    "day",
    "night",
    "haze",
    "reflection",
    "smoke_only",
)
REPLAY_SET_SOURCE_KINDS = (
    "deployment_recording",
    "lab_recording",
    "synthetic_demo",
)
_CLAIM_FIELDS = (
    "contains_ground_truth",
    "contains_predictions",
    "deployment_domain_evidence_complete",
    "accuracy_validated",
)
_PATH_PATTERN = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\\]+$")


@dataclass(frozen=True, slots=True)
class ReplaySetArtifactReference:
    """One relative file reference with an externally checkable digest."""

    path: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _contained_path(self.path, "artifact.path"))
        object.__setattr__(self, "sha256", require_sha256(self.sha256, "artifact.sha256"))
        object.__setattr__(
            self,
            "byte_count",
            require_int(self.byte_count, "artifact.bytes", minimum=1),
        )

    @classmethod
    def from_mapping(cls, raw: object, *, name: str) -> ReplaySetArtifactReference:
        value = strict_object(
            raw,
            name=name,
            required=frozenset({"path", "sha256", "bytes"}),
        )
        return cls(
            path=_contained_path(value["path"], f"{name}.path"),
            sha256=require_sha256(value["sha256"], f"{name}.sha256"),
            byte_count=require_int(value["bytes"], f"{name}.bytes", minimum=1),
        )

    @classmethod
    def from_file(cls, path: Path, *, root: Path) -> ReplaySetArtifactReference:
        source = path.resolve(strict=True)
        root = root.resolve(strict=True)
        try:
            relative = source.relative_to(root).as_posix()
        except ValueError as exc:
            raise AutonomyContractError(
                "replay-set artifact must stay below the manifest root"
            ) from exc
        payload = source.read_bytes()
        return cls(
            path=_contained_path(relative, "artifact.path"),
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
        )

    def resolve(self, root: Path, *, name: str = "artifact") -> Path:
        root = root.resolve(strict=True)
        raw_candidate = root / PurePosixPath(self.path)
        current = root
        for part in PurePosixPath(self.path).parts:
            current /= part
            if current.is_symlink():
                raise AutonomyContractError(f"{name} path must not be a symbolic link")
        candidate = raw_candidate.resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise AutonomyContractError(f"{name} path escapes replay-set root") from exc
        if not candidate.is_file():
            raise AutonomyContractError(f"{name} file does not exist: {self.path}")
        return candidate

    def verify(self, root: Path, *, name: str) -> Path:
        source = self.resolve(root, name=name)
        payload = source.read_bytes()
        observed = hashlib.sha256(payload).hexdigest()
        if len(payload) != self.byte_count:
            raise AutonomyContractError(
                f"{name} byte count mismatch: expected {self.byte_count}, observed {len(payload)}"
            )
        if observed != self.sha256:
            raise AutonomyContractError(
                f"{name} hash mismatch: expected {self.sha256}, observed {observed}"
            )
        return source

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.byte_count}


@dataclass(frozen=True, slots=True)
class ReplaySetEntry:
    """One condition/content stratum in a replay dataset."""

    entry_id: str
    strata: tuple[str, ...]
    recording_manifest: ReplaySetArtifactReference
    ground_truth: ReplaySetArtifactReference
    frame_count: int
    source_kind: str
    annotations_reviewed: bool
    predictions: ReplaySetArtifactReference | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", require_text(self.entry_id, "entry_id"))
        if not isinstance(self.strata, Sequence) or isinstance(
            self.strata, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("entry.strata must be an array")
        normalized = tuple(
            require_text(item, f"entry.strata[{index}]").lower()
            for index, item in enumerate(self.strata)
        )
        if not normalized:
            raise AutonomyContractError("entry.strata must not be empty")
        if len(normalized) != len(set(normalized)):
            raise AutonomyContractError(f"entry {self.entry_id} contains duplicate strata")
        unsupported = sorted(set(normalized) - set(REPLAY_SET_STRATA))
        if unsupported:
            raise AutonomyContractError(
                f"entry {self.entry_id} has unsupported stratum: {unsupported[0]}"
            )
        object.__setattr__(self, "strata", tuple(sorted(normalized)))
        if not isinstance(self.recording_manifest, ReplaySetArtifactReference):
            raise AutonomyContractError("entry.recording_manifest must be an artifact reference")
        if not isinstance(self.ground_truth, ReplaySetArtifactReference):
            raise AutonomyContractError("entry.ground_truth must be an artifact reference")
        object.__setattr__(
            self,
            "frame_count",
            require_int(self.frame_count, "entry.frame_count", minimum=1),
        )
        source_kind = require_text(self.source_kind, "entry.source_kind").lower()
        if source_kind not in REPLAY_SET_SOURCE_KINDS:
            raise AutonomyContractError(
                f"entry.source_kind must be one of: {', '.join(REPLAY_SET_SOURCE_KINDS)}"
            )
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(
            self,
            "annotations_reviewed",
            require_bool(self.annotations_reviewed, "entry.annotations_reviewed"),
        )
        if self.predictions is not None and not isinstance(
            self.predictions, ReplaySetArtifactReference
        ):
            raise AutonomyContractError("entry.predictions must be an artifact reference or null")

    @classmethod
    def from_mapping(cls, raw: object, *, name: str = "ReplaySetEntry") -> ReplaySetEntry:
        value = strict_object(
            raw,
            name=name,
            required=frozenset(
                {
                    "entry_id",
                    "strata",
                    "recording_manifest",
                    "ground_truth",
                    "frame_count",
                    "source_kind",
                    "annotations_reviewed",
                }
            ),
            optional=frozenset({"predictions"}),
        )
        strata = require_string_array(value["strata"], f"{name}.strata")
        return cls(
            entry_id=require_text(value["entry_id"], f"{name}.entry_id"),
            strata=strata,
            recording_manifest=ReplaySetArtifactReference.from_mapping(
                value["recording_manifest"], name=f"{name}.recording_manifest"
            ),
            ground_truth=ReplaySetArtifactReference.from_mapping(
                value["ground_truth"], name=f"{name}.ground_truth"
            ),
            frame_count=require_int(value["frame_count"], f"{name}.frame_count", minimum=1),
            source_kind=require_text(value["source_kind"], f"{name}.source_kind"),
            annotations_reviewed=require_bool(
                value["annotations_reviewed"], f"{name}.annotations_reviewed"
            ),
            predictions=(
                ReplaySetArtifactReference.from_mapping(
                    value["predictions"], name=f"{name}.predictions"
                )
                if "predictions" in value
                else None
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "entry_id": self.entry_id,
            "strata": list(self.strata),
            "recording_manifest": self.recording_manifest.to_mapping(),
            "ground_truth": self.ground_truth.to_mapping(),
            "frame_count": self.frame_count,
            "source_kind": self.source_kind,
            "annotations_reviewed": self.annotations_reviewed,
        }
        if self.predictions is not None:
            result["predictions"] = self.predictions.to_mapping()
        return result


@dataclass(frozen=True, slots=True)
class ReplaySetManifest:
    """Validation-complete stratified replay-set identity."""

    dataset_id: str
    dataset_version: str
    class_names: tuple[str, ...]
    required_strata: tuple[str, ...]
    entries: tuple[ReplaySetEntry, ...]
    claims: Mapping[str, bool]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "dataset_id", require_text(self.dataset_id, "dataset_id"))
        object.__setattr__(
            self,
            "dataset_version",
            require_text(self.dataset_version, "dataset_version"),
        )
        class_names = tuple(
            _canonical_label(require_text(item, f"class_names[{index}]"))
            for index, item in enumerate(self.class_names)
        )
        if not class_names:
            raise AutonomyContractError("class_names must not be empty")
        if len(class_names) != len(set(class_names)):
            raise AutonomyContractError("class_names must be unique")
        object.__setattr__(self, "class_names", class_names)
        required = tuple(
            require_text(item, f"required_strata[{index}]").lower()
            for index, item in enumerate(self.required_strata)
        )
        if not required:
            raise AutonomyContractError("required_strata must not be empty")
        if len(required) != len(set(required)):
            raise AutonomyContractError("required_strata must be unique")
        unsupported = sorted(set(required) - set(REPLAY_SET_STRATA))
        if unsupported:
            raise AutonomyContractError(f"unsupported required stratum: {unsupported[0]}")
        object.__setattr__(self, "required_strata", tuple(sorted(required)))
        entries = tuple(self.entries)
        if any(not isinstance(item, ReplaySetEntry) for item in entries):
            raise AutonomyContractError("entries must contain ReplaySetEntry values")
        entry_ids = tuple(item.entry_id for item in entries)
        if len(entry_ids) != len(set(entry_ids)):
            raise AutonomyContractError("replay-set entry_id values must be unique")
        if any(not set(item.strata).issubset(set(required)) for item in entries):
            raise AutonomyContractError("entry strata must be declared in required_strata")
        object.__setattr__(self, "entries", entries)
        claims = strict_object(
            self.claims,
            name="claims",
            required=frozenset(_CLAIM_FIELDS),
        )
        normalized_claims = MappingProxyType(
            {name: require_bool(claims[name], f"claims.{name}") for name in _CLAIM_FIELDS}
        )
        expected = _derive_claims(
            entries,
            required_strata=tuple(sorted(required)),
        )
        if dict(normalized_claims) != expected:
            raise AutonomyContractError(
                "claims do not match replay-set contents; regenerate the manifest"
            )
        object.__setattr__(self, "claims", normalized_claims)

    @classmethod
    def from_mapping(
        cls,
        raw: object,
        *,
        manifest_path: str | Path,
        require_complete: bool = False,
    ) -> ReplaySetManifest:
        value = strict_object(
            raw,
            name="ReplaySetManifest",
            required=frozenset(
                {
                    "schema_version",
                    "dataset_id",
                    "dataset_version",
                    "class_names",
                    "required_strata",
                    "entries",
                    "claims",
                }
            ),
        )
        source = Path(manifest_path).resolve(strict=True)
        manifest = cls(
            schema_version=require_schema_version(value["schema_version"]),
            dataset_id=require_text(value["dataset_id"], "dataset_id"),
            dataset_version=require_text(value["dataset_version"], "dataset_version"),
            class_names=require_string_array(value["class_names"], "class_names"),
            required_strata=require_string_array(value["required_strata"], "required_strata"),
            entries=tuple(
                ReplaySetEntry.from_mapping(item, name=f"entries[{index}]")
                for index, item in enumerate(_require_array(value["entries"], "entries"))
            ),
            claims=value["claims"],
        )
        manifest._verify_referenced_files(source.parent)
        if require_complete and not manifest.is_complete:
            missing = ", ".join(manifest.missing_strata) or "review/recording coverage"
            raise AutonomyContractError(f"replay-set deployment gate is incomplete: {missing}")
        return manifest

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def missing_strata(self) -> tuple[str, ...]:
        covered = {stratum for item in self.entries for stratum in item.strata}
        return tuple(stratum for stratum in self.required_strata if stratum not in covered)

    @property
    def is_complete(self) -> bool:
        return bool(self.claims["deployment_domain_evidence_complete"])

    def coverage(self) -> dict[str, dict[str, int]]:
        result = {stratum: {"entry_count": 0, "frame_count": 0} for stratum in self.required_strata}
        for entry in self.entries:
            for stratum in entry.strata:
                result[stratum]["entry_count"] += 1
                result[stratum]["frame_count"] += entry.frame_count
        return result

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "class_names": list(self.class_names),
            "required_strata": list(self.required_strata),
            "entries": [item.to_mapping() for item in self.entries],
            "claims": dict(self.claims),
        }

    def _verify_referenced_files(self, root: Path) -> None:
        expected_classes = set(self.class_names)
        for entry in self.entries:
            recording_manifest_path = entry.recording_manifest.verify(
                root, name=f"entry {entry.entry_id} recording_manifest"
            )
            recording = RawSensorReplayRecording.from_manifest(recording_manifest_path)
            ground_truth_path = entry.ground_truth.verify(
                root, name=f"entry {entry.entry_id} ground_truth"
            )
            try:
                truth = load_ground_truth_jsonl(ground_truth_path)
            except (OSError, ValueError) as exc:
                raise AutonomyContractError(
                    f"entry {entry.entry_id} ground_truth is invalid: {exc}"
                ) from exc
            frame_ids = tuple(frame.frame_id for frame in recording.frames)
            truth_ids = tuple(frame.frame_id for frame in truth)
            if truth_ids != frame_ids:
                raise AutonomyContractError(
                    f"entry {entry.entry_id} ground_truth frame IDs do not match recording"
                )
            if entry.frame_count != len(frame_ids):
                raise AutonomyContractError(
                    f"entry {entry.entry_id} frame_count does not match recording"
                )
            for frame in truth:
                for item in frame.objects:
                    if item.label not in expected_classes:
                        raise AutonomyContractError(
                            f"entry {entry.entry_id} ground_truth label {item.label!r} "
                            "is not in class_names"
                        )
            if entry.predictions is not None:
                predictions_path = entry.predictions.verify(
                    root, name=f"entry {entry.entry_id} predictions"
                )
                try:
                    predictions = load_prediction_jsonl(predictions_path)
                except (OSError, ValueError) as exc:
                    raise AutonomyContractError(
                        f"entry {entry.entry_id} predictions are invalid: {exc}"
                    ) from exc
                prediction_ids = tuple(frame.frame_id for frame in predictions)
                if prediction_ids != frame_ids:
                    raise AutonomyContractError(
                        f"entry {entry.entry_id} prediction frame IDs do not match recording"
                    )
                for frame in predictions:
                    for item in frame.detections:
                        if item.label not in expected_classes:
                            raise AutonomyContractError(
                                f"entry {entry.entry_id} prediction label {item.label!r} "
                                "is not in class_names"
                            )


def load_replay_set_manifest(
    path: str | Path,
    *,
    require_complete: bool = False,
) -> ReplaySetManifest:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise AutonomyContractError("replay-set manifest path must not be a symbolic link")
    source = requested.resolve(strict=True)
    try:
        payload = source.read_bytes()
        raw = _strict_json_loads(payload, name="replay-set manifest")
    except OSError as exc:
        raise AutonomyContractError(
            f"replay-set manifest is not valid UTF-8 JSON: {source}"
        ) from exc
    return ReplaySetManifest.from_mapping(
        raw,
        manifest_path=source,
        require_complete=require_complete,
    )


def _strict_json_loads(payload: bytes, *, name: str) -> object:
    """Decode a manifest without duplicate-key or non-finite-number ambiguity.

    ``json.loads`` otherwise keeps the last duplicate key and accepts the
    JavaScript-style ``NaN``/``Infinity`` extensions.  Both behaviours are
    unsafe for hash-bound release evidence: a later field can silently replace
    a reviewed value, or a non-finite threshold can evade a numeric gate.  The
    UTF-8 BOM is accepted for compatibility with Windows tools, while all
    other malformed input remains fail-closed as an autonomy contract error.
    """

    def reject_constant(value: str) -> None:
        raise AutonomyContractError(f"{name} contains non-finite JSON number {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AutonomyContractError(f"{name} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutonomyContractError(f"{name} is not valid UTF-8 JSON: {exc}") from exc


def build_replay_set_manifest(
    root: str | Path,
    output: str | Path,
    *,
    dataset_id: str,
    dataset_version: str,
    class_names: Sequence[str] = ("flame", "smoke"),
    required_strata: Sequence[str] = REPLAY_SET_STRATA,
    source_kind: str = "lab_recording",
    annotations_reviewed: bool = False,
    allow_incomplete: bool = False,
    overwrite: bool = False,
) -> ReplaySetManifest:
    """Build a manifest from ``root/<stratum>/`` recording directories.

    Each present stratum directory must contain ``recording.manifest.json``
    and ``ground-truth.jsonl``.  ``predictions.jsonl`` is optional and is
    hash-bound when present.  Missing directories are only tolerated with
    ``allow_incomplete=True``; an incomplete manifest always keeps its gate
    claim false.
    """

    root_path = Path(root).resolve(strict=True)
    if not root_path.is_dir():
        raise AutonomyContractError(f"replay-set root is not a directory: {root_path}")
    output_path = Path(output).resolve()
    if output_path.parent != root_path:
        raise AutonomyContractError("replay-set manifest output must be directly inside root")
    if output_path.exists() and not overwrite:
        raise AutonomyContractError(f"replay-set manifest already exists: {output_path}")
    required = tuple(
        require_text(item, f"required_strata[{index}]").lower()
        for index, item in enumerate(required_strata)
    )
    if not required:
        raise AutonomyContractError("required_strata must not be empty")
    if len(required) != len(set(required)):
        raise AutonomyContractError("required_strata must be unique")
    if set(required) - set(REPLAY_SET_STRATA):
        raise AutonomyContractError("required_strata contains an unsupported value")
    normalized_source = require_text(source_kind, "source_kind").lower()
    if normalized_source not in REPLAY_SET_SOURCE_KINDS:
        raise AutonomyContractError(
            f"source_kind must be one of: {', '.join(REPLAY_SET_SOURCE_KINDS)}"
        )
    reviewed = require_bool(annotations_reviewed, "annotations_reviewed")
    entries: list[ReplaySetEntry] = []
    missing: list[str] = []
    for stratum in sorted(required):
        directory = root_path / stratum
        if not directory.is_dir():
            missing.append(stratum)
            continue
        recording_path = directory / "recording.manifest.json"
        truth_path = directory / "ground-truth.jsonl"
        if not recording_path.is_file() or not truth_path.is_file():
            if allow_incomplete:
                missing.append(stratum)
                continue
            raise AutonomyContractError(
                f"stratum {stratum} requires recording.manifest.json and ground-truth.jsonl"
            )
        recording_ref = ReplaySetArtifactReference.from_file(recording_path, root=root_path)
        truth_ref = ReplaySetArtifactReference.from_file(truth_path, root=root_path)
        recording = RawSensorReplayRecording.from_manifest(recording_path)
        truth = load_ground_truth_jsonl(truth_path)
        frame_ids = tuple(frame.frame_id for frame in recording.frames)
        if tuple(frame.frame_id for frame in truth) != frame_ids:
            raise AutonomyContractError(f"stratum {stratum} truth frame IDs do not match recording")
        prediction_path = directory / "predictions.jsonl"
        prediction_ref: ReplaySetArtifactReference | None = None
        if prediction_path.is_file():
            predictions = load_prediction_jsonl(prediction_path)
            if tuple(frame.frame_id for frame in predictions) != frame_ids:
                raise AutonomyContractError(
                    f"stratum {stratum} prediction frame IDs do not match recording"
                )
            prediction_ref = ReplaySetArtifactReference.from_file(prediction_path, root=root_path)
        entries.append(
            ReplaySetEntry(
                entry_id=stratum,
                strata=(stratum,),
                recording_manifest=recording_ref,
                ground_truth=truth_ref,
                frame_count=len(frame_ids),
                source_kind=normalized_source,
                annotations_reviewed=reviewed,
                predictions=prediction_ref,
            )
        )
    if missing and not allow_incomplete:
        raise AutonomyContractError(
            f"replay-set is missing required stratum(s): {', '.join(missing)}"
        )
    manifest = ReplaySetManifest(
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        class_names=tuple(class_names),
        required_strata=tuple(required),
        entries=tuple(entries),
        claims=_derive_claims(tuple(entries), required_strata=tuple(sorted(required))),
    )
    _atomic_write_json(output_path, manifest.to_mapping(), overwrite=overwrite)
    return load_replay_set_manifest(output_path)


def replay_set_validation_document(
    manifest: ReplaySetManifest,
    *,
    manifest_path: str | Path,
) -> dict[str, object]:
    """Return a report that distinguishes structure from deployment approval."""

    path = Path(manifest_path).resolve(strict=True)
    payload = path.read_bytes()
    return {
        "event": "replay_set_manifest_validated",
        "schema_version": manifest.schema_version,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "manifest_path": str(path),
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "canonical_hash": manifest.canonical_hash,
        "entry_count": len(manifest.entries),
        "coverage": manifest.coverage(),
        "missing_strata": list(manifest.missing_strata),
        "claims": dict(manifest.claims),
        "structurally_valid": True,
        "gate_passed": manifest.is_complete,
        "accuracy_validated": False,
    }


def _derive_claims(
    entries: Sequence[ReplaySetEntry],
    *,
    required_strata: tuple[str, ...],
) -> dict[str, bool]:
    covered = {stratum for entry in entries for stratum in entry.strata}
    complete = (
        bool(entries)
        and set(required_strata).issubset(covered)
        and all(
            entry.source_kind == "deployment_recording" and entry.annotations_reviewed
            for entry in entries
        )
    )
    return {
        "contains_ground_truth": bool(entries),
        "contains_predictions": bool(entries)
        and all(entry.predictions is not None for entry in entries),
        "deployment_domain_evidence_complete": complete,
        "accuracy_validated": False,
    }


def _canonical_label(value: str) -> str:
    normalized = value.strip().lower()
    return "flame" if normalized == "fire" else normalized


def _contained_path(value: object, name: str) -> str:
    text = require_text(value, name).replace("\\", "/")
    if not _PATH_PATTERN.fullmatch(text):
        raise AutonomyContractError(f"{name} must be a relative contained path")
    parsed = PurePosixPath(text)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise AutonomyContractError(f"{name} must be a relative contained path")
    return parsed.as_posix()


def _require_array(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AutonomyContractError(f"{name} must be an array")
    return value


def _atomic_write_json(path: Path, document: Mapping[str, object], *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise AutonomyContractError(f"replay-set manifest already exists: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
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
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "REPLAY_SET_POSITIVE_STRATA",
    "REPLAY_SET_SOURCE_KINDS",
    "REPLAY_SET_STRATA",
    "ReplaySetArtifactReference",
    "ReplaySetEntry",
    "ReplaySetManifest",
    "build_replay_set_manifest",
    "load_replay_set_manifest",
    "replay_set_validation_document",
]

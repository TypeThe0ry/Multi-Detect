"""Hash-bound binding for the three-airframe simulator scene streams.

``scene_state`` validates one truth-free, replayable stream.  This module
adds the aggregate boundary used by release and CI tooling: exactly one
stream for VTOL, fixed-wing and multirotor, all from the same prerelease
channel and all independently re-opened before the aggregate is accepted.

The binding is deliberately a software/SIM evidence record.  It proves that
the same scene-stream contract is available for all three airframes; it does
not claim that Gazebo, QGC rendering, Pixhawk hardware or a human visual
check ran.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..autonomy.serialization import AutonomyContractError, strict_json_loads
from .scene_state import SimulatorSceneStream
from .scene_state import verify_file as verify_scene_stream_file

SCHEMA_VERSION = 1
EVENT = "simulator_scene_stream_set_verified"
FAILED_EVENT = "simulator_scene_stream_set_verification_failed"
REQUIRED_MODES = ("fixed-wing", "multirotor", "vtol")
SCENARIO_MODES = {
    "coastal_patrol_fixed_wing_001": "fixed-wing",
    "urban_inspection_multirotor_001": "multirotor",
    "river_rescue_vtol_001": "vtol",
}

_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "release_label",
        "runtime_mode",
        "evidence",
        "scenario_ids",
        "modes",
        "gate_passed",
        "deterministic",
        "truth_free",
        "external_runtime_acceptance",
        "visual_acceptance",
        "hardware_acceptance",
        "checks",
    }
)
_REQUIRED_RECORD_KEYS = frozenset(
    {
        "path",
        "sha256",
        "bytes",
        "scenario_id",
        "mode",
        "scenario_version",
        "scenario_manifest_hash",
        "canonical_sha256",
        "source_evidence_hash",
        "snapshot_count",
        "gate_passed",
    }
)
_CHECK_NAMES = (
    "evidence_count",
    "all_airframes",
    "unique_scenarios",
    "unique_modes",
    "all_gate_passed",
    "all_deterministic",
    "all_truth_free",
    "all_snapshots_nonempty",
    "records_hash_bound",
)


class SimulatorSceneStreamSetError(ValueError):
    """Raised when a three-airframe scene-stream binding is invalid."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_path(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        import os

        return os.path.relpath(path, base).replace(os.sep, "/")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        raw = strict_json_loads(
            path.read_bytes(),
            name=f"simulator scene-stream set {path}",
        )
    except (OSError, UnicodeError, AutonomyContractError, TypeError, ValueError) as exc:
        raise SimulatorSceneStreamSetError(f"cannot load JSON object {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SimulatorSceneStreamSetError(f"scene-stream set must be an object: {path}")
    return dict(raw)


def _normalise_mode(value: object) -> str:
    if not isinstance(value, str):
        raise SimulatorSceneStreamSetError("scene stream mode must be a string")
    mode = value.strip().lower().replace("_", "-")
    if mode not in REQUIRED_MODES:
        raise SimulatorSceneStreamSetError(
            f"scene stream mode must be one of {list(REQUIRED_MODES)!r}"
        )
    return mode


def _record_from_stream(
    source: Path,
    stream: SimulatorSceneStream,
    *,
    manifest_directory: Path,
) -> dict[str, Any]:
    expected_mode = SCENARIO_MODES.get(stream.scenario_id)
    if expected_mode is None:
        raise SimulatorSceneStreamSetError(
            f"unsupported scene-stream scenario identity: {stream.scenario_id!r}"
        )
    if stream.mode != expected_mode:
        raise SimulatorSceneStreamSetError(
            f"scene-stream mode {stream.mode!r} does not match {stream.scenario_id!r}"
        )
    return {
        "path": _relative_path(source, manifest_directory),
        "sha256": _sha256(source),
        "bytes": source.stat().st_size,
        "scenario_id": stream.scenario_id,
        "mode": stream.mode,
        "scenario_version": stream.scenario_version,
        "scenario_manifest_hash": stream.scenario_manifest_hash,
        "canonical_sha256": stream.canonical_hash,
        "source_evidence_hash": stream.source_evidence_hash,
        "snapshot_count": len(stream.snapshots),
        "gate_passed": True,
    }


def build_binding(
    evidence_paths: Sequence[str | Path],
    *,
    manifest_directory: str | Path,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Verify and bind exactly one scene stream for each supported airframe."""

    if isinstance(evidence_paths, (str, bytes, bytearray)) or not isinstance(
        evidence_paths, Sequence
    ):
        raise SimulatorSceneStreamSetError("evidence_paths must be a sequence")
    if len(evidence_paths) != 3:
        raise SimulatorSceneStreamSetError(
            "scene-stream set requires exactly three evidence files"
        )
    output_dir = Path(manifest_directory).expanduser().resolve()
    records: list[dict[str, Any]] = []
    streams: list[SimulatorSceneStream] = []
    seen_paths: set[Path] = set()
    seen_ids: set[str] = set()
    seen_modes: set[str] = set()
    release_label: str | None = None

    for value in evidence_paths:
        source = Path(value).expanduser().resolve()
        if source in seen_paths:
            raise SimulatorSceneStreamSetError(f"scene-stream evidence is reused: {source}")
        if not source.is_file():
            raise SimulatorSceneStreamSetError(f"scene-stream evidence does not exist: {source}")
        seen_paths.add(source)
        verification = verify_scene_stream_file(
            source,
            expected_release_label=expected_release_label,
        )
        if verification.get("gate_passed") is not True:
            detail = "; ".join(
                str(reason)
                for reason in verification.get("reasons", [])
                if isinstance(reason, str) and reason
            )
            raise SimulatorSceneStreamSetError(
                f"scene-stream verifier did not pass for {source}: {detail}"
            )
        stream = SimulatorSceneStream.load_json(source)
        mode = _normalise_mode(stream.mode)
        if stream.scenario_id in seen_ids:
            raise SimulatorSceneStreamSetError(
                f"scene-stream scenario IDs must be unique: {stream.scenario_id}"
            )
        if mode in seen_modes:
            raise SimulatorSceneStreamSetError(
                f"scene-stream vehicle modes must be unique: {mode}"
            )
        if release_label is None:
            release_label = stream.release_label
        elif stream.release_label != release_label:
            raise SimulatorSceneStreamSetError(
                "scene-stream release labels must match across all airframes"
            )
        seen_ids.add(stream.scenario_id)
        seen_modes.add(mode)
        streams.append(stream)
        records.append(
            _record_from_stream(
                source,
                stream,
                manifest_directory=output_dir,
            )
        )

    records.sort(key=lambda item: str(item["mode"]))
    checks = {
        "evidence_count": len(records) == 3,
        "all_airframes": sorted(seen_modes) == list(REQUIRED_MODES),
        "unique_scenarios": len(seen_ids) == 3,
        "unique_modes": len(seen_modes) == 3,
        "all_gate_passed": all(item["gate_passed"] is True for item in records),
        "all_deterministic": all(stream.deterministic is True for stream in streams),
        "all_truth_free": all(stream.truth_free is True for stream in streams),
        "all_snapshots_nonempty": all(
            len(stream.snapshots) >= 1 for stream in streams
        ),
        "records_hash_bound": all(
            _is_sha256(item["sha256"])
            and _is_sha256(item["canonical_sha256"])
            and _is_sha256(item["source_evidence_hash"])
            and item["bytes"] > 0
            for item in records
        ),
    }
    if not all(checks.values()):
        failed = ", ".join(name for name, value in checks.items() if not value)
        raise SimulatorSceneStreamSetError(
            f"scene-stream set binding checks failed: {failed}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "release_label": release_label,
        "runtime_mode": "sim",
        "evidence": records,
        "scenario_ids": sorted(seen_ids),
        "modes": sorted(seen_modes),
        "gate_passed": True,
        "deterministic": True,
        "truth_free": True,
        "external_runtime_acceptance": "not_claimed",
        "visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "checks": checks,
    }


def verify_binding(
    document: Mapping[str, Any],
    *,
    manifest_directory: str | Path,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Re-open every source in an aggregate binding and recompute its checks."""

    if not isinstance(document, Mapping):
        raise SimulatorSceneStreamSetError("scene-stream set must be an object")
    reasons: list[str] = []
    unknown = sorted(set(document).difference(_REQUIRED_KEYS))
    if unknown:
        reasons.append(f"unknown top-level field(s): {unknown!r}")
    missing = sorted(_REQUIRED_KEYS.difference(document))
    if missing:
        reasons.append(f"missing top-level field(s): {missing!r}")
    if document.get("schema_version") != SCHEMA_VERSION:
        reasons.append("schema_version must be 1")
    if document.get("event") != EVENT:
        reasons.append(f"event must be {EVENT}")
    if document.get("runtime_mode") != "sim":
        reasons.append("runtime_mode must be sim")
    if document.get("gate_passed") is not True:
        reasons.append("gate_passed must be true")
    for field in (
        "deterministic",
        "truth_free",
    ):
        if document.get(field) is not True:
            reasons.append(f"{field} must be true")
    for field in (
        "external_runtime_acceptance",
        "visual_acceptance",
        "hardware_acceptance",
    ):
        if document.get(field) != "not_claimed":
            reasons.append(f"{field} must remain not_claimed")
    release_label = document.get("release_label")
    if not isinstance(release_label, str) or not release_label:
        reasons.append("release_label must be a non-empty string")
    elif expected_release_label is not None and release_label != expected_release_label:
        reasons.append("release_label does not match expected product version")

    raw_evidence = document.get("evidence")
    if not isinstance(raw_evidence, list):
        reasons.append("evidence must be an array")
        raw_evidence = []
    if len(raw_evidence) != 3:
        reasons.append("evidence must contain exactly three records")
    base = Path(manifest_directory).expanduser().resolve()
    records: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    seen_ids: set[str] = set()
    seen_modes: set[str] = set()
    streams: list[SimulatorSceneStream] = []
    for index, raw_record in enumerate(raw_evidence):
        prefix = f"evidence[{index}]"
        if not isinstance(raw_record, Mapping):
            reasons.append(f"{prefix} must be an object")
            continue
        unknown_record = sorted(set(raw_record).difference(_REQUIRED_RECORD_KEYS))
        if unknown_record:
            reasons.append(f"unknown {prefix} field(s): {unknown_record!r}")
        path_value = raw_record.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            reasons.append(f"{prefix}.path is missing or empty")
            continue
        source = Path(path_value).expanduser()
        if not source.is_absolute():
            source = base / source
        try:
            source = source.resolve()
        except (OSError, RuntimeError) as exc:
            reasons.append(f"{prefix}.path cannot be resolved: {exc}")
            continue
        if source in seen_paths:
            reasons.append(f"{prefix}.path is reused")
        seen_paths.add(source)
        if not source.is_file():
            reasons.append(f"{prefix} file does not exist")
            continue
        actual_sha = _sha256(source)
        actual_bytes = source.stat().st_size
        if not _is_sha256(raw_record.get("sha256")):
            reasons.append(f"{prefix}.sha256 must be a SHA-256 digest")
        elif actual_sha.lower() != str(raw_record["sha256"]).lower():
            reasons.append(f"{prefix}.sha256 does not match source")
        if type(raw_record.get("bytes")) is not int or raw_record["bytes"] < 0:
            reasons.append(f"{prefix}.bytes must be a non-negative integer")
        elif actual_bytes != raw_record["bytes"]:
            reasons.append(f"{prefix}.bytes does not match source")
        try:
            verification = verify_scene_stream_file(
                source,
                expected_release_label=expected_release_label,
            )
            stream = SimulatorSceneStream.load_json(source)
        except (OSError, UnicodeError, AutonomyContractError, TypeError, ValueError) as exc:
            reasons.append(f"{prefix} verifier failed: {exc}")
            continue
        if verification.get("gate_passed") is not True:
            reasons.extend(
                f"{prefix}: {reason}"
                for reason in verification.get("reasons", [])
                if isinstance(reason, str) and reason
            )
        try:
            mode = _normalise_mode(stream.mode)
        except SimulatorSceneStreamSetError as exc:
            reasons.append(f"{prefix}: {exc}")
            continue
        expected_mode = SCENARIO_MODES.get(stream.scenario_id)
        if expected_mode != mode:
            reasons.append(f"{prefix}.scenario_id does not match mode")
        if isinstance(release_label, str) and stream.release_label != release_label:
            reasons.append(f"{prefix}.release_label does not match source")
        scenario_id = raw_record.get("scenario_id")
        if scenario_id != stream.scenario_id:
            reasons.append(f"{prefix}.scenario_id does not match source")
        elif scenario_id in seen_ids:
            reasons.append(f"duplicate scenario_id: {scenario_id}")
        else:
            seen_ids.add(scenario_id)
        if raw_record.get("mode") != mode:
            reasons.append(f"{prefix}.mode does not match source")
        elif mode in seen_modes:
            reasons.append(f"duplicate mode: {mode}")
        else:
            seen_modes.add(mode)
        try:
            expected_record = _record_from_stream(source, stream, manifest_directory=base)
        except SimulatorSceneStreamSetError as exc:
            reasons.append(f"{prefix}: {exc}")
            continue
        if raw_record.get("path") != expected_record["path"]:
            reasons.append(f"{prefix}.path does not match source")
        for field in (
            "scenario_version",
            "scenario_manifest_hash",
            "canonical_sha256",
            "source_evidence_hash",
            "snapshot_count",
            "gate_passed",
        ):
            if raw_record.get(field) != expected_record[field]:
                reasons.append(f"{prefix}.{field} does not match source")
        records.append(expected_record)
        streams.append(stream)

    records.sort(key=lambda item: str(item["mode"]))
    computed_checks = {
        "evidence_count": len(raw_evidence) == 3,
        "all_airframes": sorted(seen_modes) == list(REQUIRED_MODES),
        "unique_scenarios": len(seen_ids) == 3,
        "unique_modes": len(seen_modes) == 3,
        "all_gate_passed": all(item["gate_passed"] is True for item in records)
        and len(records) == 3,
        "all_deterministic": all(stream.deterministic is True for stream in streams)
        and len(streams) == 3,
        "all_truth_free": all(stream.truth_free is True for stream in streams)
        and len(streams) == 3,
        "all_snapshots_nonempty": all(len(stream.snapshots) >= 1 for stream in streams)
        and len(streams) == 3,
        "records_hash_bound": all(
            _is_sha256(item.get("sha256"))
            and _is_sha256(item.get("canonical_sha256"))
            and _is_sha256(item.get("source_evidence_hash"))
            for item in records
        )
        and len(records) == 3,
    }
    declared_ids = document.get("scenario_ids")
    declared_modes = document.get("modes")
    if declared_ids != sorted(seen_ids):
        reasons.append("scenario_ids do not match source records")
    if declared_modes != sorted(seen_modes):
        reasons.append("modes do not match source records")
    declared_checks = document.get("checks")
    if not isinstance(declared_checks, Mapping):
        reasons.append("checks must be an object")
    elif dict(declared_checks) != computed_checks:
        reasons.append("checks do not match recomputed source records")
    if any(value is not True for value in computed_checks.values()):
        reasons.append("computed checks must all be true")
    return {
        "event": "simulator_scene_stream_set_revalidated",
        "schema_version": SCHEMA_VERSION,
        "release_label": release_label,
        "runtime_mode": "sim",
        "evidence": records,
        "scenario_ids": sorted(seen_ids),
        "modes": sorted(seen_modes),
        "checks": computed_checks,
        "gate_passed": not reasons,
        "reasons": reasons,
    }


def verify_binding_file(
    path: str | Path,
    *,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Load and revalidate one persisted aggregate binding."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise SimulatorSceneStreamSetError(f"binding does not exist: {source}")
    return verify_binding(
        _load_object(source),
        manifest_directory=source.parent,
        expected_release_label=expected_release_label,
    )


def main(argv: list[str] | None = None) -> int:
    """Bind three streams with ``--bind`` or revalidate one binding file."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, nargs="+")
    parser.add_argument("--bind", action="store_true")
    parser.add_argument("--manifest-directory", type=Path, default=Path.cwd())
    parser.add_argument("--expected-release-label")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.bind:
            if len(args.evidence) != 3:
                raise SimulatorSceneStreamSetError("--bind requires exactly three evidence files")
            result = build_binding(
                args.evidence,
                manifest_directory=args.manifest_directory,
                expected_release_label=args.expected_release_label,
            )
            result["event"] = EVENT
        else:
            if len(args.evidence) != 1:
                raise SimulatorSceneStreamSetError(
                    "single-file revalidation accepts exactly one binding file; use --bind"
                )
            result = verify_binding_file(
                args.evidence[0],
                expected_release_label=args.expected_release_label,
            )
    except (
        OSError,
        UnicodeError,
        AutonomyContractError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
        result = {
            "event": FAILED_EVENT,
            "schema_version": SCHEMA_VERSION,
            "gate_passed": False,
            "checks": {"readable_json": False},
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("gate_passed") is True else 1


__all__ = [
    "EVENT",
    "FAILED_EVENT",
    "REQUIRED_MODES",
    "SCENARIO_MODES",
    "SCHEMA_VERSION",
    "SimulatorSceneStreamSetError",
    "build_binding",
    "main",
    "verify_binding",
    "verify_binding_file",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

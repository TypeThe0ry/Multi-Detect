"""Create immutable current-evidence copies after a core release-parity change.

The current OnboardAI trainer is part of the canonical core-algorithm source
set.  Changing it therefore changes ``configs/release-parity.json``.  This
helper updates only the active prerelease evidence references, preserves every
historical file, and never starts Gazebo, PX4, QGC, Docker, MAVLink, HIL, or
hardware.  It emits an audit copy first; ``--apply`` replaces the authoritative
progress index only after the audit copy has been checked by the normal
verifier.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402

PARITY_PATH = ROOT / "configs" / "release-parity.json"
PROGRESS_PATH = ROOT / "docs" / "current-progress.json"
PRODUCT_VERSION_PATH = ROOT / "configs" / "product-version.json"
PRODUCT_VERSION_KEYS = ("release_label", "numeric", "python", "channel")
ACTIVE_EVIDENCE_KEYS = (
    "replay_set_positive_fixture_20260924",
    "field_link_read_only_current_probe",
    "replay_set_readiness_e7",
)

CURRENT_KEYS = (
    "v030_pre1_foundation_acceptance_20261003_r5",
    "combined_sim_qgc_external_vtol_pass_20261003_r4",
    "external_mission_matrix_20260927_r6",
    "shared_policy_observed_session_20260924",
    "shared_policy_observed_session_20260927_r17",
    "shared_policy_current_vtol_campaign_20260928_r2",
    "shared_policy_external_parent_campaign_20260927_r14",
    "shared_policy_external_vtol_sensor_campaign_20261003_r14",
    "shared_policy_runtime_matrix_20260927_r3",
    "shared_policy_campaign_receipts_20260925",
    "shared_policy_trained_artifact_external_vtol_campaign_20261001_r1",
    "external_docker_ai_flight_parity_receipt_20260927_r2",
    "external_docker_ai_flight_acceptance_20260927_r1",
    "external_fixed_wing_shared_policy_depth_mission_20260930",
    "external_multirotor_shared_policy_depth_mission_20260930",
    "external_shared_policy_pair_20260927_r1",
    "shared_policy_vehicle_matrix_20260927_r2",
)


def load(path: Path) -> Any:
    return decode(path.read_bytes(), path)


def decode(payload: bytes, path: Path) -> Any:
    value = strict_json_loads(payload, name=str(path))
    # The decoder rejects NaN literals; serialization also rejects 1e999.
    json.dumps(value, allow_nan=False)
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def parity_binding() -> dict[str, Any]:
    document = load(PARITY_PATH)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY_PATH.stat().st_size,
        "file_sha256": sha256(PARITY_PATH),
        "hashes": document["parity"],
        "release_label": document["release_label"],
    }


def product_version_binding(document: Any, parity: dict[str, Any]) -> dict[str, Any]:
    """Carry the canonical version manifest into the index, never a guessed label."""

    if not isinstance(document, dict) or any(key not in document for key in PRODUCT_VERSION_KEYS):
        raise ValueError("product version manifest is incomplete")
    if document["release_label"] != parity["release_label"]:
        raise ValueError("product version and release parity labels differ")
    return {key: document[key] for key in PRODUCT_VERSION_KEYS}


def rebind_value(
    value: Any,
    parity: dict[str, Any],
    *,
    context: str = "",
    old_hashes: dict[str, Any] | None = None,
) -> Any:
    """Rebind parity-shaped fields without touching unrelated artifact hashes."""

    hashes = parity["hashes"]
    if isinstance(value, list):
        return [
            rebind_value(item, parity, context=context, old_hashes=old_hashes) for item in value
        ]
    if not isinstance(value, dict):
        if context in {"release_parity_file_sha256", "release_parity_sha256"}:
            return parity["file_sha256"]
        if context == "release_parity_core_algorithm_hash":
            return hashes["core_algorithm_hash"]
        return value

    result: dict[str, Any] = {}
    for key, child in value.items():
        next_context = key if isinstance(key, str) else context
        parity_context = context in {
            "release_parity",
            "parity_file",
            "release_parity_snapshot",
            "parity",
        } or key in {"release_parity", "parity_file", "release_parity_snapshot"}
        if key in {"release_parity_file_sha256", "release_parity_sha256"}:
            result[key] = parity["file_sha256"]
        elif key == "release_parity_core_algorithm_hash":
            result[key] = hashes["core_algorithm_hash"]
        elif key == "release_parity_bytes":
            result[key] = parity["bytes"]
        elif parity_context and key in {"file_sha256", "sha256"}:
            result[key] = parity["file_sha256"]
        elif parity_context and key == "bytes":
            result[key] = parity["bytes"]
        elif parity_context and key == "release_label" and "release_label" in parity:
            # The label is part of the parity file identity bound above; the
            # evidence's own top-level release label is never rewritten.
            result[key] = parity["release_label"]
        elif key in hashes and (
            parity_context or (old_hashes is not None and child == old_hashes.get(key))
        ):
            result[key] = hashes[key]
        elif parity_context and key == "hashes" and isinstance(child, dict):
            result[key] = {name: hashes.get(name, item) for name, item in child.items()}
        elif key == "release_parity_snapshot" and isinstance(child, dict):
            snapshot = rebind_value(
                child, parity, context="release_parity_snapshot", old_hashes=old_hashes
            )
            for point in ("start", "end"):
                if isinstance(snapshot.get(point), dict):
                    snapshot[point]["bytes"] = parity["bytes"]
                    snapshot[point]["file_sha256"] = parity["file_sha256"]
                    snapshot[point]["hashes"] = dict(hashes)
            result[key] = snapshot
        else:
            result[key] = rebind_value(child, parity, context=next_context, old_hashes=old_hashes)
    return result


def update_paths(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [update_paths(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: update_paths(child, mapping) for key, child in value.items()}
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        if Path(value).is_absolute():
            try:
                relative = Path(value).resolve().relative_to(ROOT.resolve()).as_posix()
            except (OSError, ValueError):
                return value
            if relative in mapping:
                return str(ROOT / mapping[relative])
    return value


def binding_fields(value: dict[str, Any], key: str) -> set[str]:
    """Identify sibling file/canonical bindings, not mere output path metadata."""
    prefixes = {key}
    if key == "path":
        prefixes.add("")
    if key == "local_path":
        prefixes.add("file")
    elif key.endswith("_local_path"):
        stem = key.removesuffix("_local_path")
        prefixes.update({stem, stem + "_file"})
    elif key.endswith("_path"):
        prefixes.add(key.removesuffix("_path"))
    fields = {
        f"{prefix}_{suffix}" if prefix else suffix
        for prefix in prefixes
        for suffix in ("bytes", "file_bytes", "sha256", "file_sha256", "canonical_sha256")
    }
    if key == "evidence":
        fields.add("receipt_sha256")
    return fields & value.keys()


def byte_dependencies(value: Any) -> set[Path]:
    """Only bindings requiring final child bytes constrain the write order.

    Runtime reports commonly contain ``paths.evidence`` pointing to themselves,
    or diagnostic backlinks with no bytes/hash. Those paths are remapped, but
    they do not require a child file to have been written first.
    """
    found: set[Path] = set()
    if isinstance(value, list):
        for child in value:
            found.update(byte_dependencies(child))
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and binding_fields(value, key):
                found.update(candidate_paths(child))
            found.update(byte_dependencies(child))
    return found


def refresh_file_bindings(
    value: Any,
    mapping: dict[str, str],
    *,
    _targets: dict[str, Path] | None = None,
) -> Any:
    """Refresh progress-index identities for paths moved to immutable copies."""

    if _targets is None:
        _targets = {}
        for target in mapping.values():
            path = ROOT / target
            for alias in (target, str(path), path.as_posix()):
                _targets[os.path.normcase(alias)] = path
    if isinstance(value, list):
        return [refresh_file_bindings(item, mapping, _targets=_targets) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: refresh_file_bindings(child, mapping, _targets=_targets)
        for key, child in value.items()
    }
    for key, child in list(result.items()):
        if not isinstance(child, str) or not binding_fields(result, key):
            continue
        # Commands/stdout and other unrecognized strings are never paths. Only
        # exact known target aliases may touch the filesystem, even when an
        # unrelated field happens to have a *_sha256 sibling.
        path = _targets.get(os.path.normcase(child))
        if path is None:
            continue
        if not path.is_file():
            raise ValueError(f"mapped child has not been written: {child}")
        file_digest = sha256(path)
        document = load(path)
        if key == "evidence":
            if "evidence_bytes" in result:
                result["evidence_bytes"] = path.stat().st_size
            if "evidence_file_sha256" in result:
                result["evidence_file_sha256"] = file_digest
                if isinstance(document, dict) and isinstance(document.get("sha256"), str):
                    result["evidence_sha256"] = document["sha256"]
            elif "evidence_sha256" in result:
                result["evidence_sha256"] = file_digest
            if "receipt_sha256" in result and isinstance(document, dict):
                result["receipt_sha256"] = document.get("receipt_sha256", result["receipt_sha256"])
        elif key == "source_evidence":
            if "source_evidence_bytes" in result:
                result["source_evidence_bytes"] = path.stat().st_size
            if "source_evidence_sha256" in result:
                result["source_evidence_sha256"] = file_digest
        elif key in {"receipt", "parent_receipt", "candidate_receipt"} or key.endswith("_receipt"):
            if f"{key}_bytes" in result:
                result[f"{key}_bytes"] = path.stat().st_size
            if f"{key}_file_sha256" in result:
                result[f"{key}_file_sha256"] = file_digest
            if f"{key}_sha256" in result:
                result[f"{key}_sha256"] = file_digest
            if f"{key}_canonical_sha256" in result and isinstance(document, dict):
                result[f"{key}_canonical_sha256"] = document.get(
                    "receipt_sha256", result[f"{key}_canonical_sha256"]
                )
            if key == "receipt" and "receipt_file_sha256" in result and isinstance(document, dict):
                result["receipt_sha256"] = document.get(
                    "receipt_sha256", result.get("receipt_sha256")
                )
        elif key == "campaign_summary":
            if "campaign_summary_bytes" in result:
                result["campaign_summary_bytes"] = path.stat().st_size
            if "campaign_summary_sha256" in result:
                result["campaign_summary_sha256"] = file_digest
        elif key == "summary":
            if "summary_sha256" in result:
                result["summary_sha256"] = file_digest
        elif key == "runtime_evidence":
            if "runtime_evidence_bytes" in result:
                result["runtime_evidence_bytes"] = path.stat().st_size
            if "runtime_evidence_sha256" in result:
                result["runtime_evidence_sha256"] = file_digest
        elif key == "campaign_input" and "campaign_input_sha256" in result:
            result["campaign_input_sha256"] = file_digest
        elif key == "schema":
            if "schema_bytes" in result:
                result["schema_bytes"] = path.stat().st_size
            if "schema_sha256" in result:
                result["schema_sha256"] = file_digest
        # Generic identities: {path, bytes, sha256}, local_path, *_local_path,
        # and report/manifest *_path fields used by the active evidence refs.
        prefixes = [key] if key not in {"evidence", "source_evidence", "receipt"} else []
        if key == "path":
            prefixes.append("")
        if key == "local_path":
            prefixes.append("file")
        elif key.endswith("_local_path"):
            stem = key.removesuffix("_local_path")
            prefixes.extend([stem, stem + "_file"])
        elif key.endswith("_path"):
            prefixes.append(key.removesuffix("_path"))
        for prefix in prefixes:
            for suffix, replacement in (
                ("bytes", path.stat().st_size),
                ("file_bytes", path.stat().st_size),
                ("sha256", file_digest),
                ("file_sha256", file_digest),
            ):
                field = f"{prefix}_{suffix}" if prefix else suffix
                if field in result:
                    # A receipt canonical identity is not a file identity.
                    if field == "receipt_sha256" and isinstance(document, dict):
                        result[field] = document.get(field, file_digest)
                    elif (
                        field == "evidence_sha256"
                        and "evidence_file_sha256" in result
                        and isinstance(document, dict)
                        and "sha256" in document
                    ):
                        result[field] = document["sha256"]
                    else:
                        result[field] = replacement
    return result


def recompute_self_hashes(original: Any, rebound: Any) -> Any:
    if not isinstance(original, dict) or not isinstance(rebound, dict):
        return rebound
    for field in ("receipt_sha256", "sha256", "canonical_sha256"):
        if field not in rebound or not isinstance(rebound[field], str):
            continue
        unsigned_original = {key: item for key, item in original.items() if key != field}
        if field == "receipt_sha256" or original[field] == canonical(unsigned_original):
            unsigned = {key: item for key, item in rebound.items() if key != field}
            rebound[field] = canonical(unsigned)
    return rebound


def candidate_paths(value: Any) -> set[Path]:
    found: set[Path] = set()
    if isinstance(value, list):
        for item in value:
            found.update(candidate_paths(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(candidate_paths(item))
    elif (
        isinstance(value, str)
        and not value.startswith("http")
        and ("/" in value or "\\" in value)
        and value.lower().endswith(".json")
    ):
        try:
            candidate = (ROOT / value).resolve()
            is_file = candidate.is_file()
        except (OSError, ValueError):
            is_file = False
            candidate = ROOT
        if is_file:
            try:
                candidate.relative_to(ROOT.resolve())
            except ValueError:
                pass
            else:
                found.add(candidate)
    return found


def target_for(source: Path, used: set[Path], directory: Path) -> Path:
    if source == PARITY_PATH or source == PROGRESS_PATH:
        raise ValueError(f"cannot rebind authoritative file: {source}")
    identity = hashlib.sha256(source.relative_to(ROOT).as_posix().encode()).hexdigest()[:12]
    target = directory / f"{source.stem[:40]}-{identity}.json"
    if target in used or target.exists():
        raise FileExistsError(f"immutable audit target already exists: {target}")
    return target


def repo_path(path: Path) -> Path:
    resolved = (ROOT / path).resolve()
    resolved.relative_to(ROOT.resolve())
    if resolved == ROOT.resolve():
        raise ValueError("repository root is not an artifact path")
    return resolved


def snapshot_bindings(snapshots: dict[Path, bytes]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in sorted(snapshots.items())
    ]


def check_snapshots(snapshots: dict[Path, bytes]) -> None:
    for path, payload in snapshots.items():
        if path.read_bytes() != payload:
            raise ValueError(f"source changed during retained audit: {path}")


def check_source_bindings(bindings: list[dict[str, Any]]) -> None:
    for binding in bindings:
        path = repo_path(Path(binding["path"]))
        payload = path.read_bytes()
        if (
            len(payload) != binding["bytes"]
            or hashlib.sha256(payload).hexdigest() != binding["sha256"]
        ):
            raise ValueError(f"retained audit source identity changed: {path}")


def child_first_order(originals: dict[Path, Any], selected: set[Path]) -> list[Path]:
    ordered: list[Path] = []
    visiting: set[Path] = set()
    visited: set[Path] = set()

    def visit(source: Path) -> None:
        if source in visiting:
            raise ValueError(f"audit dependency cycle: {source}")
        if source in visited:
            return
        visiting.add(source)
        for child in sorted(byte_dependencies(originals[source]) & selected):
            visit(child)
        visiting.remove(source)
        visited.add(source)
        ordered.append(source)

    for source in sorted(selected):
        visit(source)
    return ordered


def retained_audit(original: Any, source: Path, payload: bytes) -> Any:
    if not isinstance(original, dict):
        raise ValueError(f"current audit copy must be a JSON object: {source}")
    original["retained_audit_source"] = {
        "path": source.relative_to(ROOT).as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    original["scope"] = "retained_files_audit"
    if original.get("evidence_scope") != "retained_layers_file_audit":
        original["evidence_scope"] = "retained_files_audit"
    original["runtime_source_parity_verified"] = False
    original["current_runtime_parity_proven"] = False
    original["external_sim_rerun_performed"] = False
    if "runtime_parity_snapshot_verified" in original:
        original["runtime_parity_snapshot_verified"] = False
    return original


def verify_staged(path: Path) -> dict[str, Any]:
    from scripts.verify_current_progress_index import verify

    return verify(ROOT, path, check_source_parity=True)


def promote(path: Path) -> dict[str, Any]:
    """Promote exactly the existing, fully verified bytes; never regenerate."""
    path = repo_path(path)
    if path in {PARITY_PATH, PROGRESS_PATH}:
        raise ValueError("promote requires a distinct staged index")
    payload = path.read_bytes()
    document = decode(payload, path)
    audit = document.get("parity_rebind_audit", {})
    bindings = audit.get("source_files")
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("staged index has no retained source seal")
    before = PROGRESS_PATH.read_bytes()
    check_source_bindings(bindings)
    result = verify_staged(path)
    if result.get("status") != "passed":
        raise ValueError("staged index verification did not pass")
    check_source_bindings(bindings)
    if path.read_bytes() != payload or PROGRESS_PATH.read_bytes() != before:
        raise ValueError("staged/current progress changed during verification")
    backup = PROGRESS_PATH.with_name(
        f"current-progress-before-{hashlib.sha256(before).hexdigest()[:12]}.json"
    )
    if backup.exists():
        if backup.read_bytes() != before:
            raise ValueError("existing progress backup identity differs")
    else:
        with backup.open("xb") as stream:
            stream.write(before)
    fd, temporary = tempfile.mkstemp(prefix=".current-progress-", dir=PROGRESS_PATH.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        check_source_bindings(bindings)
        if path.read_bytes() != payload or PROGRESS_PATH.read_bytes() != before:
            raise ValueError("staged/current progress changed before promotion")
        os.replace(temporary, PROGRESS_PATH)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return {"promoted": True, "sha256": hashlib.sha256(payload).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifact-directory", type=Path)
    parser.add_argument("--promote", type=Path, help="verify and promote an existing staged index")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy the verified rebound index over current-progress.json",
    )
    args = parser.parse_args(argv)
    if args.promote is not None:
        if args.apply or args.output is not None or args.artifact_directory is not None:
            parser.error("--promote cannot be combined with generation arguments")
        print(json.dumps(promote(args.promote), sort_keys=True))
        return 0
    if args.output is None or args.artifact_directory is None:
        parser.error("generation requires --output and --artifact-directory")
    output = repo_path(args.output)
    directory = repo_path(args.artifact_directory)
    if output in {PARITY_PATH, PROGRESS_PATH} or output.exists():
        raise FileExistsError(f"staged output must be new: {output}")

    snapshots = {
        PROGRESS_PATH: PROGRESS_PATH.read_bytes(),
        PARITY_PATH: PARITY_PATH.read_bytes(),
        PRODUCT_VERSION_PATH: PRODUCT_VERSION_PATH.read_bytes(),
    }
    progress = decode(snapshots[PROGRESS_PATH], PROGRESS_PATH)
    parity_document = decode(snapshots[PARITY_PATH], PARITY_PATH)
    product_version = product_version_binding(
        decode(snapshots[PRODUCT_VERSION_PATH], PRODUCT_VERSION_PATH), parity_document
    )
    for paths in parity_document["source_files"].values():
        for relative in paths:
            source = repo_path(Path(relative))
            snapshots[source] = source.read_bytes()
    parity = parity_binding()
    old_hashes = progress.get("release_parity", {}).get("hashes", {})
    current: dict[str, Any] = {}
    for key in CURRENT_KEYS:
        if key in progress:
            current[key] = copy.deepcopy(progress[key])
    if isinstance(progress.get("evidence"), dict):
        current["evidence"] = {
            key: copy.deepcopy(progress["evidence"][key])
            for key in ACTIVE_EVIDENCE_KEYS
            if key in progress["evidence"]
        }

    sources = set(candidate_paths(current))
    originals: dict[Path, Any] = {}
    for source in sorted(sources):
        if source in {PARITY_PATH, PROGRESS_PATH}:
            continue
        snapshots[source] = source.read_bytes()
        originals[source] = decode(snapshots[source], source)

    mapping: dict[str, str] = {}
    used: set[Path] = set()
    # First add every file whose own parity identity changed.  Then repeatedly
    # add documents whose nested paths now point at one of those immutable
    # copies (for example, the Mission matrix references the fixed-wing and
    # multirotor evidence files).  This keeps source hashes and nested paths
    # consistent without rewriting historical evidence in place.
    while True:
        changed = False
        for source, original in tuple(originals.items()):
            rebound = recompute_self_hashes(
                original, rebind_value(copy.deepcopy(original), parity, old_hashes=old_hashes)
            )
            # Model metadata, contracts and physical recording identities are
            # retained sources, never renamed into derived parity evidence.
            relative_parts = source.relative_to(ROOT).parts
            preserved = relative_parts[0] == "configs" or relative_parts[:2] == (
                "artifacts",
                "models",
            )
            preserved = preserved or (
                isinstance(original, dict) and "recording" in str(original.get("schema", ""))
            )
            if preserved:
                rebound = copy.deepcopy(original)
            candidate = original if preserved else update_paths(rebound, mapping)
            if candidate == original or source.relative_to(ROOT).as_posix() in mapping:
                nested_values = candidate_paths(candidate)
            else:
                target = target_for(source, used, directory)
                used.add(target)
                mapping[source.relative_to(ROOT).as_posix()] = target.relative_to(ROOT).as_posix()
                changed = True
                nested_values = candidate_paths(candidate)
            for nested in nested_values:
                if nested in {PARITY_PATH, PROGRESS_PATH} or nested in originals:
                    continue
                snapshots[nested] = nested.read_bytes()
                originals[nested] = decode(snapshots[nested], nested)
                changed = True
        if not changed:
            break

    documents: dict[Path, tuple[Path, Any, Any]] = {}
    for source_text, target_text in mapping.items():
        source = ROOT / source_text
        target = ROOT / target_text
        original = originals[source]
        rebound = recompute_self_hashes(
            original, rebind_value(copy.deepcopy(original), parity, old_hashes=old_hashes)
        )
        documents[source] = (target, original, rebound)

    ordered = child_first_order(originals, set(documents))
    check_snapshots(snapshots)
    for source in ordered:
        target, original, rebound = documents[source]
        rebound = update_paths(rebound, mapping)
        rebound = refresh_file_bindings(rebound, mapping)
        rebound = retained_audit(rebound, source, snapshots[source])
        rebound = recompute_self_hashes(original, rebound)
        write_json(target, rebound)

    rebound_current = refresh_file_bindings(
        update_paths(rebind_value(current, parity, old_hashes=old_hashes), mapping), mapping
    )
    rebound_progress = copy.deepcopy(progress)
    for key in CURRENT_KEYS:
        if key in rebound_progress and key in rebound_current:
            rebound_progress[key] = rebound_current[key]
    if "evidence" in rebound_current:
        rebound_progress["evidence"].update(rebound_current["evidence"])
    rebound_progress["product_version"] = product_version
    rebound_progress["release_parity"] = parity
    rebound_progress["parity_rebind_audit"] = {
        "scope": "retained_files_audit",
        "runtime_source_parity_verified": False,
        "external_sim_rerun_performed": False,
        "source_files": snapshot_bindings(snapshots),
    }
    check_snapshots(snapshots)
    write_json(output, rebound_progress)
    check_snapshots(snapshots)

    if args.apply:
        promote(output)

    print(
        json.dumps(
            {
                "parity_file_sha256": parity["file_sha256"],
                "parity_core_algorithm_hash": parity["hashes"]["core_algorithm_hash"],
                "rebound_progress": output.relative_to(ROOT).as_posix(),
                "artifact_copies": len(documents),
                "applied": args.apply,
                "mapping": mapping,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

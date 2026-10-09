"""Audit persisted Gazebo 3-D sensor captures before OnboardAI training.

The repository contains many historical RGB manifests.  This tool does not
try to make them compatible: it checks each capture against the current
entity-based 3-D mainline, finds a matching FLOAT32 depth companion, and
reports why a capture is or is not admissible for the next training step.

The audit is metadata-only.  It never loads simulator truth, creates labels,
starts Gazebo/PX4, or promotes a model.  A capture can be ``capture_eligible``
while still not being ``training_ready``; reviewed episode labels and an
independent holdout remain separate gates in ``train_onboard_model.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVALUATION_ROOT = ROOT / "artifacts" / "evaluation"
DEFAULT_MAINLINE = ROOT / "configs" / "scenarios" / "gazebo-3d-mainline-v1.manifest.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "evaluation" / "onboard-3d-capture-inventory.json"
RGB_NAME = "gazebo-bgr24-frame-set.manifest.json"
DEPTH_NAME = "gazebo-depth-companion-set.manifest.json"
CONTRACT = "multidetect.onboard-3d-capture-inventory-v1"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulation.index import load_scenario_index  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _current_bindings(path: Path) -> dict[str, str]:
    document = _load_json(path)
    if document is None or not isinstance(document.get("scenarios"), list):
        raise ValueError("mainline manifest must contain a scenarios list")
    bindings: dict[str, str] = {}
    for item in document["scenarios"]:
        if not isinstance(item, dict):
            raise ValueError("mainline scenario entries must be objects")
        scenario_id = item.get("scenario_id")
        scenario_manifest = item.get("scenario_manifest")
        digest = scenario_manifest.get("sha256") if isinstance(scenario_manifest, dict) else None
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ValueError("mainline scenario_id is invalid")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"mainline scenario hash is invalid: {scenario_id}")
        # The checked-in mainline records the raw file SHA for deterministic
        # asset auditing, while the simulator runtime carries the scenario
        # channel index's canonical hash.  They are deliberately different
        # contracts; inventory comparison must use the same canonical hash as
        # the runtime whenever the referenced scenario file is available.
        runtime_digest = digest
        scenario_path_value = (
            scenario_manifest.get("path")
            if isinstance(scenario_manifest, dict)
            else None
        )
        if isinstance(scenario_path_value, str) and scenario_path_value:
            candidates = (
                (ROOT / scenario_path_value).resolve(),
                (path.parent / scenario_path_value).resolve(),
            )
            for scenario_path in candidates:
                if not scenario_path.is_file():
                    continue
                try:
                    runtime_digest = load_scenario_index(scenario_path)[1].canonical_hash
                except (OSError, ValueError, TypeError, KeyError):
                    runtime_digest = digest
                break
        if scenario_id in bindings and bindings[scenario_id] != runtime_digest:
            raise ValueError(f"mainline scenario is bound to multiple hashes: {scenario_id}")
        bindings[scenario_id] = runtime_digest
    if not bindings:
        raise ValueError("mainline manifest has no scenarios")
    return bindings


def _candidate_depth_paths(rgb_path: Path, depth_paths: list[Path]) -> list[Path]:
    """Return deterministic nearby depth candidates, then all inventory files."""

    candidates: list[Path] = []
    for candidate in (
        rgb_path.parent / DEPTH_NAME,
        rgb_path.parent.parent / DEPTH_NAME,
        rgb_path.parent.parent / "depth" / DEPTH_NAME,
        rgb_path.parent.parent.parent / "depth" / DEPTH_NAME,
    ):
        if candidate.is_file() and candidate not in candidates:
            candidates.append(candidate)
    for candidate in depth_paths:
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _frame_key(frame: dict[str, Any]) -> tuple[int, str]:
    return (int(frame.get("sequence", -1)), str(frame.get("frame_id", "")))


def _depth_matches_rgb(
    rgb_path: Path,
    rgb: dict[str, Any],
    depth_path: Path,
    depth: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    if depth.get("schema") != "multidetect.gazebo-depth-companion-set":
        reasons.append("depth schema is not MDGZDEP1")
    if depth.get("scenario_id") != rgb.get("scenario_id"):
        reasons.append("depth scenario_id does not match RGB")
    if depth.get("scenario_manifest_hash") != rgb.get("scenario_manifest_hash"):
        reasons.append("depth scenario manifest hash does not match RGB")
    if depth.get("source_attribution", {}).get("truth_loaded_by_runtime") is not False:
        reasons.append("depth provenance does not explicitly exclude simulator truth")
    rgb_frames = rgb.get("frames") if isinstance(rgb.get("frames"), list) else []
    depth_frames = depth.get("frames") if isinstance(depth.get("frames"), list) else []
    rgb_by_key = {_frame_key(frame): frame for frame in rgb_frames if isinstance(frame, dict)}
    depth_by_key = {_frame_key(frame): frame for frame in depth_frames if isinstance(frame, dict)}
    missing = sorted(set(rgb_by_key) - set(depth_by_key))
    if missing:
        reasons.append(f"depth companion missing {len(missing)} RGB frame(s)")
    mismatched_rgb = 0
    for key, rgb_frame in rgb_by_key.items():
        depth_frame = depth_by_key.get(key)
        if depth_frame is None:
            continue
        rgb_record = (rgb_frame.get("provenance") or {}).get("record_sha256")
        if rgb_record and depth_frame.get("rgb_record_sha256") != rgb_record:
            mismatched_rgb += 1
    if mismatched_rgb:
        reasons.append(f"depth/RGB record binding mismatch in {mismatched_rgb} frame(s)")
    depth_files_missing = sum(
        1
        for frame in depth_frames
        if isinstance(frame, dict)
        and not (depth_path.parent / str(frame.get("file", ""))).is_file()
    )
    if depth_files_missing:
        reasons.append(f"{depth_files_missing} depth payload file(s) are missing")
    return (
        not reasons and bool(rgb_by_key) and len(rgb_by_key) == len(depth_by_key),
        reasons,
        {
            "path": depth_path.resolve().relative_to(ROOT.resolve()).as_posix()
            if depth_path.resolve().is_relative_to(ROOT.resolve())
            else depth_path.resolve().as_posix(),
            "sha256": _sha256(depth_path),
            "frame_count": len(depth_frames),
            "matched_frame_count": len(set(rgb_by_key) & set(depth_by_key)),
            "depth_payload_files_missing": depth_files_missing,
        },
    )


def _audit_rgb(
    path: Path,
    *,
    depth_paths: list[Path],
    bindings: dict[str, str],
    minimum_frames: int,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path.resolve().relative_to(ROOT.resolve()).as_posix()
        if path.resolve().is_relative_to(ROOT.resolve())
        else path.resolve().as_posix(),
        "file_sha256": _sha256(path),
        "schema": None,
        "scenario_id": None,
        "scenario_manifest_hash": None,
        "frame_count": 0,
        "capture_eligible": False,
        "training_ready": False,
        "depth_companion": None,
        "reasons": [],
    }
    document = _load_json(path)
    if document is None:
        record["reasons"] = ["RGB manifest cannot be parsed as a JSON object"]
        return record
    record["schema"] = document.get("schema")
    record["scenario_id"] = document.get("scenario_id")
    record["scenario_manifest_hash"] = document.get("scenario_manifest_hash")
    frames = document.get("frames") if isinstance(document.get("frames"), list) else []
    record["frame_count"] = len(frames)
    reasons: list[str] = []
    if document.get("schema") != "multidetect.gazebo-bgr24-frame-set":
        reasons.append("RGB schema is not MDGZSIM3")
    scenario_id = document.get("scenario_id")
    expected_hash = bindings.get(scenario_id) if isinstance(scenario_id, str) else None
    if expected_hash is None:
        reasons.append("scenario is not in the current entity-based 3-D mainline")
    elif document.get("scenario_manifest_hash") != expected_hash:
        reasons.append("scenario manifest hash is stale against the current mainline")
    source = document.get("source_attribution")
    if not isinstance(source, dict) or source.get("truth_loaded_by_runtime") is not False:
        reasons.append("RGB provenance does not explicitly exclude simulator truth")
    if not isinstance(source, dict) or source.get("pixels") != "actual_gazebo_camera_sensor_output":
        reasons.append("RGB provenance is not an actual Gazebo camera stream")
    if len(frames) < minimum_frames:
        reasons.append(f"fewer than {minimum_frames} RGB frames")
    if len({_frame_key(frame) for frame in frames if isinstance(frame, dict)}) != len(frames):
        reasons.append("RGB frame ids/sequences are not unique")
    payloads_missing = sum(
        1
        for frame in frames
        if isinstance(frame, dict) and not (path.parent / str(frame.get("file", ""))).is_file()
    )
    if payloads_missing:
        reasons.append(f"{payloads_missing} RGB payload file(s) are missing")
    depth_candidates: list[dict[str, Any]] = []
    nearby_paths = set(_candidate_depth_paths(path, []))
    for depth_path in _candidate_depth_paths(path, depth_paths):
        depth = _load_json(depth_path)
        if depth is None:
            continue
        if depth_path not in nearby_paths and (
            depth.get("scenario_id") != document.get("scenario_id")
            or depth.get("scenario_manifest_hash") != document.get("scenario_manifest_hash")
        ):
            continue
        matches, depth_reasons, summary = _depth_matches_rgb(path, document, depth_path, depth)
        summary["matches_rgb"] = matches
        if depth_reasons:
            summary["reasons"] = depth_reasons
        depth_candidates.append(summary)
        if matches:
            record["depth_companion"] = summary
            break
    if record["depth_companion"] is None:
        reasons.append("no complete, hash-bound FLOAT32 depth companion")
    if not reasons:
        record["capture_eligible"] = True
        record["reasons"] = [
            "current mainline RGB + complete depth companion; "
            "still needs reviewed trajectory labels"
        ]
    else:
        record["reasons"] = reasons
    record["depth_candidates"] = depth_candidates[:8]
    record["depth_candidates_considered"] = len(depth_candidates)
    record["training_ready"] = False
    return record


def audit_inventory(
    *,
    evaluation_root: Path = DEFAULT_EVALUATION_ROOT,
    mainline_manifest: Path = DEFAULT_MAINLINE,
    minimum_frames: int = 100,
) -> dict[str, Any]:
    if minimum_frames <= 0:
        raise ValueError("minimum_frames must be positive")
    evaluation_root = evaluation_root.expanduser().resolve()
    mainline_manifest = mainline_manifest.expanduser().resolve()
    bindings = _current_bindings(mainline_manifest)
    paths = sorted(evaluation_root.rglob(RGB_NAME)) if evaluation_root.is_dir() else []
    depth_paths = sorted(evaluation_root.rglob(DEPTH_NAME)) if evaluation_root.is_dir() else []
    captures = [
        _audit_rgb(
            path,
            depth_paths=depth_paths,
            bindings=bindings,
            minimum_frames=minimum_frames,
        )
        for path in paths
    ]
    eligible = [record for record in captures if record["capture_eligible"]]
    reason_counts = Counter(
        reason for record in captures for reason in record.get("reasons", []) if reason
    )
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "mainline_manifest": {
            "path": mainline_manifest.resolve().relative_to(ROOT.resolve()).as_posix()
            if mainline_manifest.is_relative_to(ROOT)
            else mainline_manifest.as_posix(),
            "sha256": _sha256(mainline_manifest),
            "scenario_ids": sorted(bindings),
        },
        "evaluation_root": evaluation_root.relative_to(ROOT).as_posix()
        if evaluation_root.is_relative_to(ROOT)
        else evaluation_root.as_posix(),
        "minimum_frames_for_capture_eligibility": minimum_frames,
        "scanned_rgb_manifests": len(captures),
        "capture_eligible_count": len(eligible),
        "training_ready_count": 0,
        "reason_counts": dict(sorted(reason_counts.items())),
        "captures": captures,
        "training_gate": {
            "requires_current_mainline_entity_assets": True,
            "requires_rgb_camera_payloads": True,
            "requires_float32_depth_companions": True,
            "requires_wire_telemetry": True,
            "requires_reviewed_episode_trajectory": True,
            "requires_independent_holdout": True,
            "truth_json_used": False,
            "colour_fixture_used_as_training_label": False,
            "candidate_promoted": False,
            "final_release_allowed": False,
        },
        "audit_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-root", type=Path, default=DEFAULT_EVALUATION_ROOT)
    parser.add_argument("--mainline-manifest", type=Path, default=DEFAULT_MAINLINE)
    parser.add_argument("--minimum-frames", type=int, default=100)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    document = audit_inventory(
        evaluation_root=args.evaluation_root,
        mainline_manifest=args.mainline_manifest,
        minimum_frames=args.minimum_frames,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

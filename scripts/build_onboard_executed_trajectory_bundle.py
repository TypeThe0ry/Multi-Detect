"""Bind recorded teacher/action/observation fields to original files, offline only.

File integrity and exact field projection are not producer authentication, sensor
calibration, flight authorization or expert review. Nothing here constructs an
Actor input, runs a model, fits normalization, or grants training admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import (  # noqa: E402
    AutonomyContractError,
    canonical_sha256,
    require_sha256,
    strict_json_loads,
    strict_object,
)

CONTRACT = "multidetect.onboard-executed-trajectory-bundle-v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 128
MAX_STEPS = 512


def json_pointer(value, pointer):
    """Strict RFC6901 projection; never infer a record from its filename."""
    if type(pointer) is not str or len(pointer) > 2048:
        raise AutonomyContractError("Bounded JSON pointer required")
    if pointer == "":
        return value
    if not pointer.startswith("/") or pointer.count("/") > 32:
        raise AutonomyContractError("Absolute bounded JSON pointer required")
    for token in pointer.split("/")[1:]:
        if re.search(r"~(?![01])", token):
            raise AutonomyContractError("Invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if type(value) is dict:
            if token not in value:
                raise AutonomyContractError("JSON pointer key is absent")
            value = value[token]
        elif type(value) is list:
            if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                raise AutonomyContractError("Canonical JSON array index required")
            index = int(token)
            if index >= len(value):
                raise AutonomyContractError("JSON pointer array index is absent")
            value = value[index]
        else:
            raise AutonomyContractError("JSON pointer crosses a scalar")
    return value


def _sha(value):
    if type(value) is not str or require_sha256(value, "sha256") != value:
        raise AutonomyContractError("Canonical lowercase SHA-256 required")
    return value


def _hash_file(path, maximum):
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise AutonomyContractError("Nonempty bounded source file required")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > maximum:
                raise AutonomyContractError("Source byte budget exceeded")
            digest.update(chunk)
    return size, digest.hexdigest()


def _read_bounded(path, maximum):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not 0 < len(raw) <= maximum:
        raise AutonomyContractError("Read byte budget exceeded")
    return raw


class SourceVerifier:
    """Bounded per-file integrity plus original JSON field verification."""

    def __init__(self, artifact_root):
        self.root = Path(artifact_root).resolve(strict=True)
        if not self.root.is_dir():
            raise AutonomyContractError("artifact_root must be a directory")
        self.files = {}
        self.documents = {}
        self.total_bytes = 0
        self.projections = 0

    def verify_file(self, binding):
        path_text = binding["path"]
        if type(path_text) is not str or "\x00" in path_text:
            raise AutonomyContractError("Source path must be text without NUL")
        path = Path(path_text)
        windows = PureWindowsPath(path_text)
        if ".." in path.parts or ".." in windows.parts:
            raise AutonomyContractError("Source path cannot traverse")
        if windows.drive and not path.is_absolute():
            raise AutonomyContractError("Drive-relative source is forbidden")
        path = (path if path.is_absolute() else self.root / path).resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise AutonomyContractError("Source must remain within artifact_root")
        declared_size, declared_sha = binding["bytes"], _sha(binding["sha256"])
        if type(declared_size) is not int or not 0 < declared_size <= MAX_SOURCE_BYTES:
            raise AutonomyContractError("Invalid declared source byte budget")
        key = path.as_posix()
        expected = {"path": key, "bytes": declared_size, "sha256": declared_sha}
        if key in self.files:
            if self.files[key] != expected:
                raise AutonomyContractError("Conflicting source declarations")
            return path
        if len(self.files) >= MAX_FILES or self.total_bytes + declared_size > MAX_TOTAL_BYTES:
            raise AutonomyContractError("Aggregate source budget exceeded")
        size, sha = _hash_file(path, MAX_SOURCE_BYTES)
        if (size, sha) != (declared_size, declared_sha):
            raise AutonomyContractError("Actual source bytes/SHA differ")
        self.files[key] = expected
        self.total_bytes += size
        return path

    def verify_projection(self, node):
        binding = node["source"]
        path = self.verify_file(binding)
        pointer = binding["json_pointer"]
        if pointer is None:
            raise AutonomyContractError("Action/pose/outcome needs an explicit JSON pointer")
        key = path.as_posix()
        if key not in self.documents:
            if self.files[key]["bytes"] > MAX_JSON_BYTES:
                raise AutonomyContractError("Projected JSON source exceeds record budget")
            raw = _read_bounded(path, MAX_JSON_BYTES)
            if hashlib.sha256(raw).hexdigest() != self.files[key]["sha256"]:
                raise AutonomyContractError("JSON source changed before projection")
            self.documents[key] = strict_json_loads(raw, name="trajectory original source")
        source = json_pointer(self.documents[key], pointer)
        expected = {name: value for name, value in node.items() if name != "source"}
        if type(source) is not dict or not set(expected) <= set(source):
            raise AutonomyContractError("Original source lacks declared projection fields")
        actual = {name: source[name] for name in expected}
        if canonical_sha256(actual) != canonical_sha256(expected):
            raise AutonomyContractError("Recorded projection differs from original source")
        self.projections += 1

    def verify_observation(self, observation):
        if observation is None:
            return
        self.verify_file(observation["image_source"])
        self.verify_projection(observation["pose"])
        self.verify_projection(observation)

    def close(self):
        for value in self.files.values():
            if _hash_file(Path(value["path"]), MAX_SOURCE_BYTES) != (
                value["bytes"],
                value["sha256"],
            ):
                raise AutonomyContractError("Source changed during bundle verification")
        return list(self.files.values())


def verify_manifest(manifest, *, artifact_root):
    """Return source-bound records; no field turns producer/flight gates true."""
    from multidetect.autonomy.onboard_executed_trajectory import ExecutedTrajectoryStep

    raw = strict_object(
        manifest,
        name="executed trajectory manifest",
        required=frozenset({"schema_version", "contract", "steps"}),
    )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise AutonomyContractError("Unsupported manifest schema version")
    if raw["contract"] != CONTRACT:
        raise AutonomyContractError("Unsupported manifest contract")
    rows = raw["steps"]
    if type(rows) is not list or not 0 < len(rows) <= MAX_STEPS:
        raise AutonomyContractError("Bounded nonempty step list required")
    verifier = SourceVerifier(artifact_root)
    parsed, ids, commands, episodes = [], set(), set(), {}
    for row in rows:
        step = ExecutedTrajectoryStep.from_mapping(row)
        if step.step_id in ids:
            raise AutonomyContractError("Duplicate step_id")
        ids.add(step.step_id)
        if step.submitted is not None:
            command = (step.vehicle_id, step.submitted.command_id)
            if command in commands:
                raise AutonomyContractError("Duplicate submitted command identity")
            commands.add(command)
        domain = (step.vehicle_id, step.clock_id)
        if step.episode_id in episodes and episodes[step.episode_id] != domain:
            raise AutonomyContractError("Episode vehicle or clock domain changed")
        episodes[step.episode_id] = domain
        for name in ("proposed", "approved", "submitted", "outcome"):
            if row[name] is not None:
                verifier.verify_projection(row[name])
        for name in ("before", "after"):
            verifier.verify_observation(row[name])
        parsed.append(step)
    sources = verifier.close()
    normalized = {
        "schema_version": 1,
        "contract": CONTRACT,
        "steps": deepcopy(rows),
    }
    audit = {
        "schema_version": 1,
        "contract": "multidetect.onboard-executed-trajectory-source-audit-v1",
        "status": "source_bound_structural_evidence_not_training_or_flight_admission",
        "manifest_canonical_sha256": canonical_sha256(manifest),
        "normalized_canonical_sha256": canonical_sha256(normalized),
        "emitted_numeric_representation": "original_verified_JSON_not_dataclass_numeric_coercion",
        "steps": [
            {
                "step_id": step.step_id,
                "audit": step.audit_status(),
                "observed_motion": (
                    step.observed_motion()
                    if step.before is not None and step.after is not None
                    else None
                ),
            }
            for step in parsed
        ],
        "verified_files": sources,
        "verified_unique_file_count": len(sources),
        "verified_unique_file_bytes": verifier.total_bytes,
        "verified_record_projection_count": verifier.projections,
        "source_integrity_and_projections_verified": True,
        "source_producer_authenticated": False,
        "pixel_content_decoded": False,
        "sensor_calibration_verified": False,
        "expert_labels_reviewed": False,
        "runtime_Actor_created": False,
        "truth_or_future_sent_to_runtime": False,
        "normalization_fitted": False,
        "training_ready": False,
        "train_admitted": False,
        "execution_authority": False,
        "flight_control_commands_sent": 0,
        "candidate_promoted": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    return normalized, audit


def _write_new(path, document):
    payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise AutonomyContractError("Output byte budget exceeded")
    with path.open("xb") as stream:
        if stream.write(payload) != len(payload):
            raise OSError("Short trajectory bundle write")
        stream.flush()
        os.fsync(stream.fileno())
    return len(payload), hashlib.sha256(payload).hexdigest()


def build_bundle(manifest_path, manifest_sha256, *, artifact_root, output_directory):
    root = Path(artifact_root).resolve(strict=True)
    source = Path(manifest_path).resolve(strict=True)
    if not source.is_relative_to(root):
        raise AutonomyContractError("Manifest must be inside artifact_root")
    size, sha = _hash_file(source, MAX_MANIFEST_BYTES)
    if sha != _sha(manifest_sha256):
        raise AutonomyContractError("Manifest SHA differs from explicit admission hash")
    payload = _read_bounded(source, MAX_MANIFEST_BYTES)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha:
        raise AutonomyContractError("Manifest changed before parse")
    normalized, audit = verify_manifest(
        strict_json_loads(payload, name="executed trajectory manifest"), artifact_root=root
    )
    if _hash_file(source, MAX_MANIFEST_BYTES) != (size, sha):
        raise AutonomyContractError("Manifest changed during verification")
    audit["manifest_file"] = {"path": source.as_posix(), "bytes": size, "sha256": sha}
    output = Path(output_directory).resolve()
    if output == root or not output.is_relative_to(root):
        raise AutonomyContractError("Output must be a new directory inside artifact_root")
    output.mkdir(exist_ok=False)
    normalized_size, normalized_sha = _write_new(output / "steps.json", normalized)
    for value in audit["verified_files"] + [audit["manifest_file"]]:
        limit = MAX_MANIFEST_BYTES if value is audit["manifest_file"] else MAX_SOURCE_BYTES
        if _hash_file(Path(value["path"]), limit) != (value["bytes"], value["sha256"]):
            raise AutonomyContractError("Input changed before final audit publication")
    if _hash_file(output / "steps.json", MAX_MANIFEST_BYTES) != (normalized_size, normalized_sha):
        raise AutonomyContractError("Normalized output differs from expected bytes/SHA")
    audit["normalized_file"] = {
        "path": (output / "steps.json").as_posix(),
        "bytes": normalized_size,
        "sha256": normalized_sha,
    }
    audit_expected = _write_new(output / "source-audit.json", audit)
    if _hash_file(output / "source-audit.json", MAX_MANIFEST_BYTES) != audit_expected:
        raise AutonomyContractError("Audit output differs from expected bytes/SHA")
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        audit = build_bundle(
            args.manifest,
            args.manifest_sha256,
            artifact_root=args.artifact_root,
            output_directory=args.output_directory,
        )
    except (AutonomyContractError, OSError, ValueError, TypeError) as exc:
        print(
            json.dumps({"status": "rejected", "reason": str(exc), "train_admitted": False}),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": audit["status"],
                "steps": len(audit["steps"]),
                "verified_files": audit["verified_unique_file_count"],
                "train_admitted": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

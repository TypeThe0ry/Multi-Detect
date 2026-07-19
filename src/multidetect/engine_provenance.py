from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ALLOWED_HASHES = {"sha256", "sha384"}


def _digest(path: Path, algorithm: str) -> str:
    if algorithm not in _ALLOWED_HASHES:
        raise ValueError(f"unsupported source hash algorithm: {algorithm}")
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip("\x00\r\n ")
    except OSError:
        return None
    return value or None


def _command_output(command: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout + "\n" + result.stderr).strip()
    return output or None


def _first_matching_line(text: str | None, needle: str) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        if needle.lower() in line.lower():
            return line.strip()
    return None


def collect_target_runtime(*, trtexec: Path) -> dict[str, str | None]:
    cuda_version = _read_text(Path("/usr/local/cuda/version.txt"))
    if cuda_version is None:
        cuda_version_json = _read_text(Path("/usr/local/cuda/version.json"))
        if cuda_version_json:
            try:
                cuda_document = json.loads(cuda_version_json)
                cuda_version = str(cuda_document["cuda"]["version"])
            except (KeyError, TypeError, json.JSONDecodeError):
                cuda_version = None
    if cuda_version is None:
        cuda_version = _first_matching_line(_command_output(("nvcc", "--version")), "release")
    if cuda_version is None:
        cuda_version = _command_output(("dpkg-query", "-W", "-f=${Version}", "cuda-cudart-12-2"))

    trtexec_help = _command_output((str(trtexec), "--help"))
    tensorrt_version = _first_matching_line(trtexec_help, "TensorRT v")
    if tensorrt_version is None:
        tensorrt_version = _command_output(("dpkg-query", "-W", "-f=${Version}", "libnvinfer8"))

    return {
        "machine": platform.machine() or None,
        "python": platform.python_version() or None,
        "jetson_model": _read_text(Path("/proc/device-tree/model")),
        "l4t_release": _read_text(Path("/etc/nv_tegra_release")),
        "cuda_version": cuda_version,
        "tensorrt_version": tensorrt_version,
    }


def create_engine_provenance(
    *,
    engine: Path,
    source_model: Path,
    source_hash_algorithm: str,
    expected_source_digest: str,
    precision: str,
    min_shapes: str | None,
    opt_shapes: str | None,
    max_shapes: str | None,
    trtexec: Path,
    runtime: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if not engine.is_file() or engine.stat().st_size <= 0:
        raise ValueError(f"TensorRT engine is missing or empty: {engine}")
    if not source_model.is_file() or source_model.stat().st_size <= 0:
        raise ValueError(f"source model is missing or empty: {source_model}")
    algorithm = source_hash_algorithm.lower().strip()
    expected = expected_source_digest.lower().strip()
    actual = _digest(source_model, algorithm)
    if actual != expected:
        raise ValueError("source model digest does not match the pinned artifact")

    target_runtime = runtime if runtime is not None else collect_target_runtime(trtexec=trtexec)
    required_runtime = (
        "machine",
        "jetson_model",
        "l4t_release",
        "cuda_version",
        "tensorrt_version",
    )
    missing = [key for key in required_runtime if not target_runtime.get(key)]
    if missing:
        raise ValueError("target runtime provenance is incomplete: " + ", ".join(missing))

    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine": {
            "filename": engine.name,
            "size_bytes": engine.stat().st_size,
            "sha256": _digest(engine, "sha256"),
        },
        "source_model": {
            "filename": source_model.name,
            "size_bytes": source_model.stat().st_size,
            "hash_algorithm": algorithm,
            "digest": actual,
        },
        "target_runtime": target_runtime,
        "build_contract": {
            "precision": precision,
            "min_shapes": min_shapes,
            "opt_shapes": opt_shapes,
            "max_shapes": max_shapes,
            "trtexec": str(trtexec),
        },
        "capabilities": {
            "perception_metadata_only": True,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
        },
    }


def verify_engine_provenance(
    *,
    provenance: Path,
    engine: Path,
    source_model: Path,
    trtexec: Path,
    runtime: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    try:
        document = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read TensorRT provenance: {provenance}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported TensorRT provenance schema")

    engine_record = document.get("engine")
    source_record = document.get("source_model")
    recorded_runtime = document.get("target_runtime")
    capabilities = document.get("capabilities")
    if not all(isinstance(item, dict) for item in (engine_record, source_record, recorded_runtime)):
        raise ValueError("TensorRT provenance is missing artifact or runtime records")
    if capabilities != {
        "perception_metadata_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }:
        raise ValueError("TensorRT provenance capability boundary is unsafe")

    if not engine.is_file() or engine.stat().st_size != engine_record.get("size_bytes"):
        raise ValueError("TensorRT engine size does not match its provenance")
    engine_name_matches = engine.name == engine_record.get("filename")
    engine_digest_matches = _digest(engine, "sha256") == engine_record.get("sha256")
    if not engine_name_matches or not engine_digest_matches:
        raise ValueError("TensorRT engine digest does not match its provenance")

    algorithm = source_record.get("hash_algorithm")
    recorded_digest = source_record.get("digest")
    if not isinstance(algorithm, str) or not isinstance(recorded_digest, str):
        raise ValueError("source-model digest contract is missing")
    if not source_model.is_file() or source_model.stat().st_size != source_record.get("size_bytes"):
        raise ValueError("source-model size does not match TensorRT provenance")
    if (
        source_model.name != source_record.get("filename")
        or _digest(source_model, algorithm) != recorded_digest
    ):
        raise ValueError("source-model digest does not match TensorRT provenance")

    current_runtime = runtime if runtime is not None else collect_target_runtime(trtexec=trtexec)
    for key in ("machine", "jetson_model", "l4t_release", "cuda_version", "tensorrt_version"):
        recorded = recorded_runtime.get(key)
        current = current_runtime.get(key)
        if recorded != current or not recorded:
            raise ValueError(f"target runtime does not match TensorRT provenance: {key}")
    return document


def write_engine_provenance(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--trtexec", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage target-bound TensorRT provenance")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="write a new provenance document")
    _add_artifact_arguments(write)
    write.add_argument("--source-hash-algorithm", choices=sorted(_ALLOWED_HASHES), required=True)
    write.add_argument("--expected-source-digest", required=True)
    write.add_argument("--precision", required=True)
    write.add_argument("--min-shapes")
    write.add_argument("--opt-shapes")
    write.add_argument("--max-shapes")
    write.add_argument("--out", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify artifacts against this target runtime")
    _add_artifact_arguments(verify)
    verify.add_argument("--provenance", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify":
        verify_engine_provenance(
            provenance=args.provenance,
            engine=args.engine,
            source_model=args.source_model,
            trtexec=args.trtexec,
        )
        return 0
    document = create_engine_provenance(
        engine=args.engine,
        source_model=args.source_model,
        source_hash_algorithm=args.source_hash_algorithm,
        expected_source_digest=args.expected_source_digest,
        precision=args.precision,
        min_shapes=args.min_shapes,
        opt_shapes=args.opt_shapes,
        max_shapes=args.max_shapes,
        trtexec=args.trtexec,
    )
    write_engine_provenance(args.out, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

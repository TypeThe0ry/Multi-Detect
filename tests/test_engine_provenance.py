from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.engine_provenance import (
    create_engine_provenance,
    verify_engine_provenance,
    write_engine_provenance,
)


def _runtime() -> dict[str, str | None]:
    return {
        "machine": "aarch64",
        "python": "3.10.12",
        "jetson_model": "NVIDIA Jetson Orin NX Engineering Reference Developer Kit",
        "l4t_release": "# R36 (release), REVISION: 4.3",
        "cuda_version": "Cuda compilation tools, release 12.6",
        "tensorrt_version": "TensorRT v100300",
    }


def test_engine_provenance_binds_source_target_and_disabled_capabilities(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"pinned-onnx")
    engine = tmp_path / "model.engine"
    engine.write_bytes(b"target-bound-engine")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()

    document = create_engine_provenance(
        engine=engine,
        source_model=source,
        source_hash_algorithm="sha256",
        expected_source_digest=expected,
        precision="fp16",
        min_shapes="input:1x3x256x128",
        opt_shapes="input:8x3x256x128",
        max_shapes="input:10x3x256x128",
        trtexec=Path("/usr/src/tensorrt/bin/trtexec"),
        runtime=_runtime(),
    )

    assert document["engine"]["sha256"] == hashlib.sha256(engine.read_bytes()).hexdigest()
    assert document["source_model"]["digest"] == expected
    assert document["target_runtime"]["machine"] == "aarch64"
    assert document["build_contract"]["max_shapes"] == "input:10x3x256x128"
    assert document["capabilities"] == {
        "perception_metadata_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def test_engine_provenance_rejects_source_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"different")
    engine = tmp_path / "model.engine"
    engine.write_bytes(b"engine")

    with pytest.raises(ValueError, match="pinned artifact"):
        create_engine_provenance(
            engine=engine,
            source_model=source,
            source_hash_algorithm="sha384",
            expected_source_digest="0" * 96,
            precision="fp16",
            min_shapes=None,
            opt_shapes=None,
            max_shapes=None,
            trtexec=Path("trtexec"),
            runtime=_runtime(),
        )


def test_engine_provenance_requires_target_runtime_identity(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"source")
    engine = tmp_path / "model.engine"
    engine.write_bytes(b"engine")

    with pytest.raises(ValueError, match="jetson_model"):
        create_engine_provenance(
            engine=engine,
            source_model=source,
            source_hash_algorithm="sha256",
            expected_source_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
            precision="fp16",
            min_shapes=None,
            opt_shapes=None,
            max_shapes=None,
            trtexec=Path("trtexec"),
            runtime={**_runtime(), "jetson_model": None},
        )


def test_engine_provenance_write_is_valid_json(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "engine.provenance.json"
    write_engine_provenance(output, {"schema_version": 1, "safe": True})
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "safe": True,
        "schema_version": 1,
    }


def test_engine_provenance_verifies_same_artifacts_and_target(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"source")
    engine = tmp_path / "model.engine"
    engine.write_bytes(b"engine")
    document = create_engine_provenance(
        engine=engine,
        source_model=source,
        source_hash_algorithm="sha256",
        expected_source_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
        precision="fp16",
        min_shapes=None,
        opt_shapes=None,
        max_shapes=None,
        trtexec=Path("trtexec"),
        runtime=_runtime(),
    )
    provenance = tmp_path / "model.engine.provenance.json"
    write_engine_provenance(provenance, document)

    verified = verify_engine_provenance(
        provenance=provenance,
        engine=engine,
        source_model=source,
        trtexec=Path("trtexec"),
        runtime=_runtime(),
    )
    assert verified["engine"]["sha256"] == document["engine"]["sha256"]


def test_engine_provenance_rejects_engine_or_target_drift(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"source")
    engine = tmp_path / "model.engine"
    engine.write_bytes(b"engine")
    document = create_engine_provenance(
        engine=engine,
        source_model=source,
        source_hash_algorithm="sha256",
        expected_source_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
        precision="fp16",
        min_shapes=None,
        opt_shapes=None,
        max_shapes=None,
        trtexec=Path("trtexec"),
        runtime=_runtime(),
    )
    provenance = tmp_path / "model.engine.provenance.json"
    write_engine_provenance(provenance, document)

    engine.write_bytes(b"changed-engine")
    with pytest.raises(ValueError, match="engine size"):
        verify_engine_provenance(
            provenance=provenance,
            engine=engine,
            source_model=source,
            trtexec=Path("trtexec"),
            runtime=_runtime(),
        )

    engine.write_bytes(b"engine")
    with pytest.raises(ValueError, match="tensorrt_version"):
        verify_engine_provenance(
            provenance=provenance,
            engine=engine,
            source_model=source,
            trtexec=Path("trtexec"),
            runtime={**_runtime(), "tensorrt_version": "TensorRT v999999"},
        )

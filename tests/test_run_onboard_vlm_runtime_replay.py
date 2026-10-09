from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_probe_onboard_vlm import _fixture

from scripts import run_onboard_vlm_runtime_replay as replay


def _args(tmp_path):
    _, manifest = _fixture(tmp_path)
    return SimpleNamespace(
        repository_root=tmp_path,
        output_directory=tmp_path / "output",
        cases=manifest,
        cases_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        case_id=["fixture-one"],
        snapshot=tmp_path / "89644892e4d85e24eaac8bacfd4f463576704203",
        model_sha256="b" * 64,
        max_new_tokens=384,
        max_total_pixels=401408,
        deadline_s=30,
    )


def test_replay_never_overwrites_old_evidence_before_loading_model(tmp_path):
    args = _args(tmp_path)
    args.output_directory.mkdir()
    marker = args.output_directory / "original"
    marker.write_bytes(b"original")
    with pytest.raises(FileExistsError, match="overwrite"):
        replay.run(args)
    assert marker.read_bytes() == b"original"


def test_bad_case_and_manifest_identity_reject_before_creating_output(tmp_path):
    args = _args(tmp_path)
    args.case_id = ["unknown"]
    with pytest.raises(ValueError, match="case identity"):
        replay.run(args)
    assert not args.output_directory.exists()
    args.cases_sha256 = "0" * 64
    with pytest.raises(ValueError, match="SHA-256"):
        replay.run(args)


def test_artifact_record_has_actual_bytes_and_hash(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes(json.dumps({"x": 1}).encode())
    record = replay._record(path)
    assert record["bytes"] == path.stat().st_size
    assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["path"] == str(path.resolve())
    assert Path(record["path"]).is_file()

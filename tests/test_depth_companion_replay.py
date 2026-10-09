from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from scripts.replay_gazebo_depth_companion import (
    DepthCompanionReplayError,
    _resolve_child,
    _verify_manifest,
)


def _write_manifest(path: Path, *, tamper: bool = False) -> None:
    document: dict[str, object] = {
        "schema": "multidetect.test-manifest",
        "schema_version": 1,
        "frame_count": 0,
        "frames": [],
    }
    document["manifest_sha256"] = canonical_sha256(document)
    if tamper:
        document["frame_count"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")


def test_companion_replay_manifest_requires_canonical_hash(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest)
    assert _verify_manifest(manifest)["frame_count"] == 0

    tampered = tmp_path / "tampered.json"
    _write_manifest(tampered, tamper=True)
    with pytest.raises(DepthCompanionReplayError, match="canonical hash mismatch"):
        _verify_manifest(tampered)


def test_companion_replay_rejects_manifest_child_escape(tmp_path: Path) -> None:
    with pytest.raises(DepthCompanionReplayError, match="file name"):
        _resolve_child(tmp_path, "..\\outside.bin", label="payload")

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy import (
    OnboardModelReferenceError,
    load_onboard_model_reference,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "configs/training/onboard-policy-current-reference-v1.json"


@pytest.mark.usefixtures("require_current_onboard_model")
def test_current_reference_hash_binds_r33_and_mainline_3d_manifest() -> None:
    reference = load_onboard_model_reference(REFERENCE, repository_root=ROOT)

    assert reference.model_id == "onboard-policy-r33-balanced-bearing-3d-dev"
    assert reference.model_version.endswith("onboard-policy-r33-balanced-bearing-3d-dev")
    assert reference.feature_width == 40
    assert reference.model_path.name == "onboard_policy_r33.onnx"
    assert reference.gazebo_manifest_path.name == "gazebo-3d-mainline-v1.manifest.json"
    assert reference.selected is True
    assert reference.final_release_allowed is False
    assert reference.external_gazebo_evidence is False
    assert reference.jetson_evidence is False


def test_reference_rejects_a_changed_model_digest(tmp_path: Path) -> None:
    document = json.loads(REFERENCE.read_text(encoding="utf-8"))
    repository = tmp_path / "repository"
    repository.mkdir()
    for key in ("model", "training_config", "training_report", "gazebo_3d_manifest"):
        raw = f"synthetic {key}\n".encode()
        path = repository / key
        path.write_bytes(raw)
        document[key] = {"path": key, "sha256": hashlib.sha256(raw).hexdigest()}
    in_repo = repository / REFERENCE.name
    in_repo.write_text(json.dumps(document), encoding="utf-8")
    assert load_onboard_model_reference(in_repo, repository_root=repository).model_sha256 == (
        document["model"]["sha256"]
    )
    document["model"]["sha256"] = "0" * 64
    tampered = tmp_path / REFERENCE.name
    tampered.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OnboardModelReferenceError, match="inside the repository"):
        load_onboard_model_reference(tampered, repository_root=repository)

    # Copying the reference into the repository keeps the path-boundary check
    # intact while proving that a digest mismatch fails closed.
    in_repo.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(OnboardModelReferenceError, match="SHA-256 mismatch"):
        load_onboard_model_reference(in_repo, repository_root=repository)

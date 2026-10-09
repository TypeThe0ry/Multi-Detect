from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.create_external_shared_policy_pair_receipt import create_receipt

ROOT = Path(__file__).resolve().parents[1]
FIXED = ROOT / (
    "artifacts/evaluation/external-fixed-wing-mission-20260927-r12-"
    "shared-policy-parity-20261001-depth-r6.json"
)
MULTI = ROOT / (
    "artifacts/evaluation/external-multirotor-mission-20260927-r6-"
    "shared-policy-parity-20261001-depth-r6.json"
)


def _current_parity_bound_copy(root: Path, source: Path, name: str) -> Path:
    """Keep the historical pair fixture, but bind its receipt test to current parity."""

    document = json.loads(source.read_text(encoding="utf-8"))
    parity_path = root / "configs/release-parity.json"
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    document["release_parity"] = {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": parity_path.stat().st_size,
        "file_sha256": hashlib.sha256(parity_path.read_bytes()).hexdigest(),
        "hashes": parity["parity"],
        "release_label": json.loads(
            (root / "configs/product-version.json").read_text(encoding="utf-8")
        )["release_label"],
    }
    destination = root / "artifacts" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document), encoding="utf-8")
    return destination


def test_shared_policy_pair_receipt_binds_one_ranker_and_artifact(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    (sandbox / "configs").mkdir(parents=True)
    (sandbox / "configs/release-parity.json").write_bytes(
        (ROOT / "configs/release-parity.json").read_bytes()
    )
    (sandbox / "configs/product-version.json").write_bytes(
        (ROOT / "configs/product-version.json").read_bytes()
    )
    fixed = _current_parity_bound_copy(sandbox, FIXED, "fixed.json")
    multi = _current_parity_bound_copy(sandbox, MULTI, "multi.json")
    receipt = create_receipt(root=sandbox, fixed_wing_evidence=fixed, multirotor_evidence=multi)
    assert receipt["same_ranker"] is True
    assert receipt["same_policy_artifact"] is True
    assert receipt["current_release_parity_bound"] is True
    assert receipt["policy_applied_to_actuator"] is False
    assert receipt["flight_started"] is False
    assert receipt["final_release_allowed"] is False
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    from scripts.create_external_shared_policy_pair_receipt import _canonical_sha256

    assert receipt["receipt_sha256"] == _canonical_sha256(unsigned)


def test_shared_policy_pair_rejects_different_policy_identity(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    (sandbox / "configs").mkdir(parents=True)
    (sandbox / "configs/release-parity.json").write_bytes(
        (ROOT / "configs/release-parity.json").read_bytes()
    )
    (sandbox / "configs/product-version.json").write_bytes(
        (ROOT / "configs/product-version.json").read_bytes()
    )
    current_multi = _current_parity_bound_copy(sandbox, MULTI, "multi.json")
    altered = json.loads(current_multi.read_text(encoding="utf-8"))
    altered["policy_artifact_sha256"] = "0" * 64
    altered_path = tmp_path / "altered.json"
    altered_path.write_text(json.dumps(altered), encoding="utf-8")
    fixed = _current_parity_bound_copy(sandbox, FIXED, "fixed.json")
    with pytest.raises(ValueError, match="policy decision binding|policy artifact identity"):
        create_receipt(root=sandbox, fixed_wing_evidence=fixed, multirotor_evidence=altered_path)

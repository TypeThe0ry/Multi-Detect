from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.create_shared_policy_vehicle_matrix_receipt import (
    create_shared_policy_vehicle_receipt,
)
from scripts.verify_shared_policy_vehicle_matrix_receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
MATRIX = PROGRESS["shared_policy_vehicle_matrix_20260927_r2"]
RECEIPT = ROOT / MATRIX["evidence"]
VTOL = ROOT / MATRIX["vtol_receipt"]
FIXED = ROOT / PROGRESS["external_fixed_wing_shared_policy_depth_mission_20260930"]["evidence"]
MULTIROTOR = ROOT / PROGRESS[
    "external_multirotor_shared_policy_depth_mission_20260930"
]["evidence"]


def test_shared_policy_vehicle_matrix_proves_one_ranker_with_profile_bound_artifacts() -> None:
    result = verify_receipt(root=ROOT, receipt_path=RECEIPT)
    assert result["status"] == "passed"
    assert result["same_high_level_ranker"] is True
    assert result["same_policy_artifact"] is False
    assert result["unified_policy_model_proven"] is True
    assert result["final_release_allowed"] is False


def test_shared_policy_vehicle_matrix_rebuilds_current_sources() -> None:
    current = json.loads(RECEIPT.read_text(encoding="utf-8"))
    receipt = create_shared_policy_vehicle_receipt(
        root=ROOT,
        vtol_receipt=VTOL,
        fixed_wing_evidence=FIXED,
        multirotor_evidence=MULTIROTOR,
        generated_at_utc=current["generated_at_utc"],
    )
    assert receipt == current
    assert receipt["same_high_level_ranker"] is True
    assert receipt["policy_identity_scope"] == (
        "shared_ranker_with_airframe_bound_policy_artifacts"
    )


def test_shared_policy_vehicle_matrix_rejects_tampered_hash(tmp_path: Path) -> None:
    document = json.loads(RECEIPT.read_text(encoding="utf-8"))
    document["same_high_level_ranker"] = False
    tampered = tmp_path / "receipt.json"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="receipt_sha256 mismatch"):
        verify_receipt(root=ROOT, receipt_path=tampered)

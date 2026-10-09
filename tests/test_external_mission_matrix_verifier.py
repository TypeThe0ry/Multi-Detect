from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_external_mission_matrix_receipt import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
RECEIPT = ROOT / PROGRESS["external_mission_matrix_20260927_r6"]["evidence"]


def test_external_mission_matrix_verifier_rebuilds_current_evidence() -> None:
    result = verify_receipt(root=ROOT, receipt_path=RECEIPT)
    assert result["status"] == "passed"
    assert result["same_external_mission_path"] is True
    assert result["same_core_compiler_and_transaction"] is True
    assert result["flight_started"] is False
    assert result["physical_output_enabled"] is False
    assert result["final_release_allowed"] is False


def test_external_mission_matrix_verifier_rejects_tampered_receipt(tmp_path: Path) -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    receipt["flight_started"] = True
    tampered = tmp_path / "receipt.json"
    tampered.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="receipt_sha256 mismatch"):
        verify_receipt(root=ROOT, receipt_path=tampered)

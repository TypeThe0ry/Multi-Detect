from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/onboard-policy-r37-3d-model-teacher-20261006.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_r37_evidence_binds_candidate_and_reports_real_training_boundary() -> None:
    document = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    training = document["training"]
    sources = document["sources"]
    results = document["results"]
    claims = document["claims"]

    model = ROOT / training["artifact"]
    config = ROOT / training["training_config"]
    report = ROOT / training["training_report"]
    holdout = ROOT / sources["structured_holdout"]
    generalization = ROOT / sources["generalization_report"]

    assert document["status"] == "candidate_not_promoted"
    assert training["parent_model_sha256"] == (
        "727d0e71834311d8aa51dfe072e08ec249062b1945d9501394f5d89ad7a16ee6"
    )
    assert training["artifact_sha256"] == _sha256(model)
    assert training["training_config_sha256"] == _sha256(config)
    assert training["training_report_sha256"] == _sha256(report)
    assert sources["structured_holdout_sha256"] == _sha256(holdout)
    assert sources["generalization_report_sha256"] == _sha256(generalization)
    assert training["colour_identity_used_by_3d_teacher"] is False
    assert training["legacy_target_fixture"] is False
    assert training["rendered_sensor_rows"] == 0
    assert training["reviewed_trajectory_rows"] == 0
    assert results["generalization_guarded_failures"] == []
    assert results["generalization_class_color_invariance"] is True
    assert claims["same_onboardai_contract"] is True
    assert claims["pixel_level_rendered_training"] is False
    assert claims["external_gazebo_runtime"] is False
    assert claims["candidate_promoted"] is False
    assert claims["final_release_allowed"] is False


def test_r37_does_not_replace_current_reference() -> None:
    reference = json.loads(
        (ROOT / "configs/training/onboard-policy-current-reference-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert reference["model_id"] == "onboard-policy-r33-balanced-bearing-3d-dev"
    assert reference["model_id"] != "onboard-policy-r37-3d-model-teacher-balanced-dev"

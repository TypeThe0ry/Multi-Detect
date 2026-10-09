from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_r90_summary_does_not_claim_visual_training_or_promotion() -> None:
    evidence = json.loads(
        (ROOT / "docs/evidence/onboard-policy-r90-training-20261007.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["candidate"]["split"]["test"] == 0
    assert evidence["candidate"]["rendered_rgb_rows"] == 0
    assert evidence["pixel_level_rendered_training"] is False
    assert evidence["candidate_promoted"] is False
    assert evidence["paired_evaluation"]["navigation_geometry_passed"] is False
    assert evidence["paired_evaluation"]["independent_visual_holdout"] is False
    for field in (
        "external_gazebo_runtime",
        "physical_performance_claimed",
        "jetson_evidence",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert evidence[field] is False
    current = json.loads(
        (ROOT / "configs/training/onboard-policy-current-reference-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert current["model"]["sha256"] == evidence["parent"]["sha256"]


def test_r90_local_training_artifacts_match_durable_summary_if_available() -> None:
    evidence = json.loads(
        (ROOT / "docs/evidence/onboard-policy-r90-training-20261007.json").read_text(
            encoding="utf-8"
        )
    )
    for key in ("parent", "candidate", "training_report", "training_config", "paired_evaluation"):
        bound = evidence[key]
        path = ROOT / bound["path"]
        if not path.is_file():
            continue  # Large local artifacts need not exist in a clean clone.
        assert hashlib.sha256(path.read_bytes()).hexdigest() == bound["sha256"]
        if "bytes" in bound:
            assert path.stat().st_size == bound["bytes"]
    paired_path = ROOT / evidence["paired_evaluation"]["path"]
    if paired_path.is_file():
        paired = json.loads(paired_path.read_text(encoding="utf-8"))
        for label in ("parent", "candidate"):
            assert paired[label]["sha256"] == evidence[label]["sha256"]
            assert (
                paired[label]["sensor"]["task_accuracy"]
                == evidence["paired_evaluation"][f"{label}_sensor_task_accuracy"]
            )
            assert (
                paired[label]["generalization"]["raw_head_passed"]
                == evidence["paired_evaluation"][f"{label}_raw_generalization_passed"]
            )

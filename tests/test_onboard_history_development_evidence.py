from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "onboard_history_policy_development_20261007"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bound(ref: dict) -> Path | None:
    path = (ROOT / ref["path"]).resolve()
    path.relative_to(ROOT.resolve())
    if not path.is_file():
        assert ref["path"].startswith("artifacts/")
        return None
    assert path.stat().st_size == ref["bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
    return path


def _evidence() -> dict:
    entry = _json(ROOT / "docs/current-progress.json")[ENTRY]
    path = _bound(
        {
            "path": entry["evidence"],
            "bytes": entry["evidence_bytes"],
            "sha256": entry["evidence_sha256"],
        }
    )
    assert path is not None
    return _json(path)


def test_history_development_does_not_claim_learning_flight_or_promotion():
    evidence = _evidence()
    entry = _json(ROOT / "docs/current-progress.json")[ENTRY]
    for key in (
        "temporal_training_performed",
        "history_learning_verified",
        "flight_action_labels_created",
        "onboard_policy_reference_changed",
        "original_detector_changed",
        "training_ready",
        "gazebo_capture",
        "autonomous_flight_verified",
        "candidate_promoted",
        "physical_performance_claimed",
        "hil_real_evidence",
        "jetson_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert entry[key] is False
        assert evidence[key] is False
    assert evidence["history_contract"]["steps"] == 8
    assert evidence["history_contract"]["future_observation_inputs"] is False


def test_history_source_and_available_native_outputs_are_bound():
    evidence = _evidence()
    for ref in evidence["source_files"]:
        assert _bound(ref) is not None
    for ref in evidence["scaffold"]["artifacts"]:
        _bound(ref)
    verification = _bound(evidence["runtime_verification"]["report"])
    if verification is not None:
        native = _json(verification)
        assert native["passed"] is True
        assert native["raw_parent_parity_probes"] == 128
        assert native["typed_scoped_inference_observations"] == 12
        assert native["onnx_metadata_matches_untrained_report_and_actual_parent"] is True
        assert native["temporal_training_performed"] is False
        for ref in native["inputs"]:
            _bound(ref)
    report = _bound(evidence["scaffold"]["training_report"])
    if report is not None:
        raw = _json(report)
        assert raw["method"] == "parent_weight_migration_only"
        assert raw["epochs"] == 0
        assert raw["optimizer_created"] is False
        assert raw["temporal_training_performed"] is False
        assert raw["parent_model_sha256"] == evidence["scaffold"]["parent_model_sha256"]


def test_pending_trajectory_rejection_is_not_a_training_result():
    rejection = _evidence()["formal_training_attempt"]
    assert rejection["native_exit_code"] == 1
    assert rejection["error"] == "Onboard trajectory review receipt is missing"
    assert rejection["optimizer_created"] is False
    assert rejection["output_directory_created"] is False
    _bound(rejection["dataset"])


def test_current_reference_was_not_replaced_by_history_scaffold():
    evidence = _evidence()
    reference = _bound(evidence["current_reference"])
    assert reference is not None
    current = _json(reference)
    assert current["model"]["sha256"] == evidence["scaffold"]["parent_model_sha256"]


def test_human_progress_links_to_history_receipt():
    for name in (
        "README.md",
        "docs/current-progress.md",
        "docs/onboard-training-status.md",
        "docs/implementation-roadmap.md",
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "onboard-history-policy-development-20261007.json" in text
    assert (ROOT / "README.md").stat().st_size < 50000

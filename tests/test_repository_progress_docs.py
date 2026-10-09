from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_parity_rebind_writer_emits_stable_lf_and_refuses_overwrite(tmp_path: Path) -> None:
    from scripts.rebind_current_parity_artifacts_current import write_new

    path = tmp_path / "generated.json"
    write_new(path, {"schema": 1, "ok": True})
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    with pytest.raises(FileExistsError):
        write_new(path, {"schema": 1, "ok": False})


def test_current_progress_matches_version_and_release_parity() -> None:
    progress = _json(ROOT / "docs/current-progress.json")
    version = _json(ROOT / "configs/product-version.json")
    parity_path = ROOT / "configs/release-parity.json"
    parity = _json(parity_path)

    assert progress["schema_version"] == 1
    assert progress["product_version"] == {
        "release_label": version["release_label"],
        "numeric": version["numeric"],
        "python": version["python"],
        "channel": version["channel"],
    }
    assert progress["release_parity"]["bytes"] == parity_path.stat().st_size
    assert progress["release_parity"]["file_sha256"] == _sha256(parity_path)
    assert progress["release_parity"]["hashes"] == parity["parity"]


def test_current_progress_keeps_prerelease_and_claim_boundaries_explicit() -> None:
    progress = _json(ROOT / "docs/current-progress.json")
    audit = _json(ROOT / progress["evidence"]["release_gate_report"]["audit_summary_path"])

    assert progress["product_version"]["channel"] == "prerelease"
    assert progress["release_status"] == {
        "final_version_released": False,
        "gate_passed": False,
        "release_allowed": False,
        "blocker_count": len(audit["release_blocked_reasons"]),
    }
    assert audit["gate_passed"] is False
    assert audit["release_allowed"] is False
    assert audit["blocker_count"] == len(audit["release_blocked_reasons"]) > 0
    assert progress["model_status"]["status"] == "quarantined"
    assert progress["model_status"]["production_approved"] is False
    assert progress["model_status"]["accuracy_validated_for_production"] is False
    for claim in (
        "qgc_manual_visual_acceptance",
        "hardware_acceptance",
        "outdoor_gnss_acceptance",
        "hil_acceptance",
        "real_acceptance",
        "flight_acceptance",
    ):
        assert progress["claims"][claim] == "not_claimed"


def test_release_gate_audit_matches_parity_and_available_raw_report() -> None:
    progress = _json(ROOT / "docs/current-progress.json")
    reference = progress["evidence"]["release_gate_report"]
    audit = _json(ROOT / reference["audit_summary_path"])
    parity = _json(ROOT / "configs/release-parity.json")["parity"]
    assert audit["current_parity"] == parity
    assert reference["blocker_count"] == audit["blocker_count"]
    assert reference["current_release_parity_bound"] == audit["current_release_parity_bound"]
    mismatches = audit["mode_parity_mismatches"]
    assert set(mismatches) == {"replay", "sim", "hil", "real"}
    assert audit["current_release_parity_bound"] == (not any(mismatches.values()))
    assert reference["file_sha256"] == audit["report_sha256"]
    report_path = ROOT / audit["report_path"]
    # Source artifacts are deliberately not required in a clean clone. When
    # present, validate the durable audit against bytes, reasons and modes.
    if report_path.is_file():
        assert _sha256(report_path) == audit["report_sha256"]
        assert report_path.stat().st_size == audit["report_bytes"]
        report = _json(report_path)
        assert report["release_blocked_reasons"] == audit["release_blocked_reasons"]
        for mode, fields in mismatches.items():
            assert set(fields) == {
                key
                for key, value in parity.items()
                if report["modes"][mode]["parity"][key] != value
            }


def test_release_gate_entry_page_tracks_current_audit_and_external_matrix() -> None:
    page = (ROOT / "docs/release-gate.md").read_text(encoding="utf-8")
    progress = _json(ROOT / "docs/current-progress.json")
    audit_path = progress["evidence"]["release_gate_report"]["audit_summary_path"]
    assert audit_path in page
    assert "external-fixed-wing-depth-shared-policy-20261003-current-r2.json" in page
    assert "external-multirotor-depth-shared-policy-20261003-current-r2.json" in page
    assert "shared-policy-current-vtol-campaign-20261001-r31.json" in page
    assert "hardware_link_ready=false" in page


def test_readme_is_a_bounded_entry_page_with_durable_status_links() -> None:
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    archive = ROOT / "docs/history/README-development-log-through-v56.md"

    assert readme_path.stat().st_size < 50_000
    assert "docs/current-progress.md" in readme
    assert "docs/current-progress.json" in readme
    assert "docs/development-update-policy.md" in readme
    assert "CHANGELOG.md" in readme
    assert "docs/implementation-roadmap.md" in readme
    assert archive.is_file()
    assert archive.stat().st_size > 500_000


def test_incremental_core_drift_is_explicit_without_rewriting_history() -> None:
    audit = _json(ROOT / "docs/evidence/release-parity-drift-20260908.json")
    historical = _json(ROOT / "docs/evidence/release-gate-audit-v197.json")
    assert audit["report_sha256"] == historical["report_sha256"]
    assert audit["current_release_parity_bound"] is False
    assert audit["release_allowed"] is False
    assert all(
        fields == ["core_algorithm_hash"] for fields in audit["mode_parity_mismatches"].values()
    )
    assert (
        audit["current_parity"]["core_algorithm_hash"]
        != historical["current_parity"]["core_algorithm_hash"]
    )


def test_sim_recomputation_inventory_keeps_sources_and_claim_boundaries() -> None:
    audit = _json(ROOT / "docs/evidence/sim-recomputation-v196.json")
    # v196 is immutable historical evidence, not a claim against every future
    # core. Its recorded parity must agree with that historical gate audit.
    historical = _json(ROOT / "docs/evidence/release-gate-audit-v197.json")
    assert audit["current_parity"] == historical["current_parity"]
    assert audit["matrix_sources"] == 3
    assert audit["campaign_positive_runs"] == 6
    assert audit["campaign_fault_runs"] == 9
    assert audit["source_bytes_match_historical_bindings"] is True
    assert audit["final_release_allowed"] is False
    assert len(audit["outputs"]) == 4
    for item in audit["outputs"] + audit["response_sources"]:
        path = ROOT / item["path"]
        if path.is_file():
            assert path.stat().st_size == item["bytes"]
            assert _sha256(path) == item["sha256"]
    for item in audit["outputs"]:
        assert item["gate_passed"] is True

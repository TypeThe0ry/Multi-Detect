from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import create_current_release_gate_audit as audit


def _write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verified_field_report(*_args: object, **_kwargs: object) -> dict[str, bool]:
    return {"gate_passed": True}


def _fixture(tmp_path: Path, *, projection_gate_passed: bool | None) -> tuple[Path, Path, Path]:
    diagnostic_path = tmp_path / "interface-diagnostic.json"
    _write_json(diagnostic_path, {"schema_version": 1, "read_only": True})
    field_path = tmp_path / "field-preflight.json"
    _write_json(
        field_path,
        {
            "summary": {"hardware_link_ready": False},
            "interface_diagnostic": {
                "required": True,
                "report_path": diagnostic_path.name,
                "report_sha256": _sha256(diagnostic_path),
                "report_bytes": diagnostic_path.stat().st_size,
                "verified": True,
                "read_only": True,
            },
        },
    )
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, {})
    checks: dict[str, object] = {}
    if projection_gate_passed is not None:
        checks["interface_diagnostic"] = {
            "gate_passed": projection_gate_passed,
            "read_only": True,
            "report_path": str(diagnostic_path),
            "report_sha256": _sha256(diagnostic_path),
            "report_bytes": diagnostic_path.stat().st_size,
        }
    report_path = tmp_path / "release-report.json"
    _write_json(
        report_path,
        {
            "manifest_path": str(manifest_path),
            "evaluated_at_utc": "2026-10-02T12:00:00Z",
            "product_version": {"release_label": "0.3.0-pre.8"},
            "gate_passed": False,
            "release_allowed": False,
            "release_blocked_reasons": ["synthetic blocker"],
            "modes": {
                mode: {"parity": {"core_algorithm_hash": "a"}}
                for mode in ("replay", "sim", "hil", "real")
            },
            "real_field_link": {
                "report": {
                    "path": str(field_path),
                    "sha256": _sha256(field_path),
                    "bytes": field_path.stat().st_size,
                    "checks": checks,
                }
            },
        },
    )
    parity_path = tmp_path / "parity.json"
    _write_json(parity_path, {"parity": {"core_algorithm_hash": "a"}})
    return report_path, parity_path, field_path


def test_audit_uses_verified_raw_binding_when_projection_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path, parity_path, _ = _fixture(tmp_path, projection_gate_passed=None)
    monkeypatch.setattr(audit, "verify_field_link_report", _verified_field_report)

    result = audit.create_audit(tmp_path, report_path, parity_path)

    assert result["field_link_probe"]["interface_diagnostic_path"] == "interface-diagnostic.json"
    assert result["field_link_probe"]["read_only"] is True
    assert result["runtime_reexecuted"] is False
    assert result["parity_rebind_only"] is True
    assert result["evidence_scope"] == "retained_files_audit"
    assert result["source_runtime_evaluated_at_utc"] == result["evaluated_at_utc"]
    assert result["current_parity_file"] == {
        "path": "parity.json",
        "bytes": parity_path.stat().st_size,
        "sha256": _sha256(parity_path),
    }


def test_audit_rejects_explicitly_failed_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path, parity_path, _ = _fixture(tmp_path, projection_gate_passed=False)
    monkeypatch.setattr(audit, "verify_field_link_report", _verified_field_report)

    with pytest.raises(ValueError, match="projection is not verified"):
        audit.create_audit(tmp_path, report_path, parity_path)


def test_audit_rejects_unverified_raw_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, field_path = _fixture(tmp_path, projection_gate_passed=None)
    field = json.loads(field_path.read_text(encoding="utf-8"))
    field["interface_diagnostic"]["verified"] = False
    _write_json(field_path, field)
    monkeypatch.setattr(audit, "verify_field_link_report", _verified_field_report)

    with pytest.raises(ValueError, match="verified and read-only"):
        audit._field_probe(tmp_path, field_path)


def test_audit_rejects_current_probe_reusing_historical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path, parity_path, field_path = _fixture(tmp_path, projection_gate_passed=None)
    monkeypatch.setattr(audit, "verify_field_link_report", _verified_field_report)

    with pytest.raises(ValueError, match="distinct evidence artifact"):
        audit.create_audit(tmp_path, report_path, parity_path, field_path)

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.rebind_combined_target_evidence_cards import rebind

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT
    / "artifacts/evaluation/combined-sim-qgc-20260905T175019Z-64536/combined-evidence.json"
)
AUDIO = (
    ROOT
    / "artifacts/evaluation/combined-sim-qgc-20260905T175019Z-64536/integrated-sim-qgc.audit.jsonl"
)
PARITY = ROOT / "configs/release-parity.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rebind(
    *, source: Path, audit: Path, output: Path, report_path: Path, root: Path = ROOT
) -> dict[str, object]:
    return rebind(
        source=source,
        audit=audit,
        parity=PARITY,
        output=output,
        report_path=report_path,
        root=root,
        source_sha256=_sha256(source),
        audit_sha256=_sha256(audit),
    )


def test_rebind_derives_standalone_cards_without_mutating_source(tmp_path: Path) -> None:
    output = tmp_path / "historical-card-bundle.json"
    report_path = tmp_path / "rebind-report.json"
    source_bytes = SOURCE.read_bytes()
    source_document = json.loads(source_bytes)
    result = _rebind(
        source=SOURCE,
        audit=AUDIO,
        output=output,
        report_path=report_path,
    )

    assert SOURCE.read_bytes() == source_bytes
    assert result["bundle"]["evidence_kind"] == "historical_observation_cards_only"
    assert result["bundle"]["card_reconstruction_passed"] is True
    assert result["bundle"]["target_evidence_card_count"] == 336
    assert result["bundle"]["derivation"]["runtime_reexecuted"] is False
    assert result["bundle"]["claims"]["current_external_sim_acceptance"] is False
    assert result["bundle"]["source"]["source_release_parity"] == source_document[
        "hashes"
    ]["release_parity"]

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["source"]["combined_evidence"]["sha256"] == _sha256(SOURCE)
    assert document["source"]["integrated_audit"]["sha256"] == _sha256(AUDIO)
    assert len(document["target_evidence_cards"]) == 336
    assert document["target_evidence_cards"][0]["available"] is True
    assert all(
        item["label_sequence"] < item["primary_sequence"]
        for item in document["card_source_events"]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["target_evidence_card_count"] == 336
    assert report["runtime_reexecuted"] is False
    assert report["current_external_sim_acceptance"] is False


def test_rebind_refuses_count_drift(tmp_path: Path) -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    source["integrated_sim_qgc"]["target_geolocation_acceptance"][
        "available_geolocation_count"
    ] = 337
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="audit geolocation count"):
        _rebind(
            source=source_path,
            audit=AUDIO,
            output=tmp_path / "out.json",
            report_path=tmp_path / "report.json",
        )


def test_rebind_refuses_audit_hash_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        rebind(
            source=SOURCE,
            audit=AUDIO,
            parity=PARITY,
            output=tmp_path / "out.json",
            report_path=tmp_path / "report.json",
            root=ROOT,
            source_sha256=_sha256(SOURCE),
            audit_sha256="0" * 64,
        )


def test_rebind_refuses_audit_session_drift(tmp_path: Path) -> None:
    lines = AUDIO.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["session_id"] = "0" * 64
    lines[0] = json.dumps(event)
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    source["artifacts"]["integrated_audit"] = str(audit_path)
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="session_id"):
        _rebind(
            source=source_path,
            audit=audit_path,
            output=tmp_path / "out.json",
            report_path=tmp_path / "report.json",
        )


def test_rebind_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "out.json"
    output.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        _rebind(
            source=SOURCE,
            audit=AUDIO,
            output=output,
            report_path=tmp_path / "report.json",
        )

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    ReleaseGateManifestError,
    build_release_gate_manifest,
    create_release_gate_manifest,
    write_release_gate_manifest,
)
from multidetect.release_gate_manifest import main as manifest_main
from scripts.run_px4_airframe_transaction_acceptance import main as transaction_main

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"
SCHEMA_PATH = REPO_ROOT / "configs" / "schemas" / "release-gate-manifest.schema.json"
MODES = ("replay", "sim", "hil", "real")
PARITY = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}


def _write_mode_evidence(root: Path) -> dict[str, Path]:
    evidence: dict[str, Path] = {}
    for mode in MODES:
        path = root / "evidence" / f"{mode}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "gate_passed": True,
                    "runtime_mode": mode,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
    return evidence


def _complete_statuses() -> dict[str, dict[str, str]]:
    statuses: dict[str, dict[str, str]] = {}
    for mode in MODES:
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
        }
    return statuses


def _transaction_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "transaction" / "airframes.json"
    assert transaction_main(["--out", str(path)]) == 0
    return path


def test_release_manifest_binds_and_reopens_all_three_airframe_journals(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    transaction = _transaction_evidence(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"

    document = create_release_gate_manifest(
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_airframe_transaction_evidence=transaction,
    )

    binding = document["px4_airframe_transaction"]
    assert binding["evidence_path"] == "../transaction/airframes.json"
    assert binding["evidence_sha256"] == hashlib.sha256(transaction.read_bytes()).hexdigest()
    assert binding["evidence_bytes"] == transaction.stat().st_size
    assert binding["vehicle_classes"] == ["fixed-wing", "multirotor", "vtol"]
    assert all(binding["checks"].values())

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is True
    assert report["px4_airframe_transaction"]["present"] is True
    assert report["px4_airframe_transaction"]["passed"] is True
    assert report["px4_airframe_transaction"]["verification"]["gate_passed"] is True


def test_release_gate_rejects_a_tampered_airframe_journal_after_binding(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    transaction = _transaction_evidence(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_airframe_transaction_evidence=transaction,
    )
    write_release_gate_manifest(output, document)

    source = json.loads(transaction.read_text(encoding="utf-8"))
    journal_path = Path(source["entries"][1]["mission_journal"]["path"])
    journal_path.write_bytes(journal_path.read_bytes() + b'{"record_type":"partial-tail"')

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["px4_airframe_transaction"]["passed"] is False
    assert any(
        "px4_airframe_transaction" in reason
        and ("SHA-256" in reason or "verifier" in reason or "entry" in reason)
        for reason in report["release_blocked_reasons"]
    )


def test_final_release_requires_three_airframe_transaction_binding(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    create_release_gate_manifest(
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
    )
    release_version = tmp_path / "product-version-release.json"
    release_version.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0",
                "numeric": "0.3.0",
                "python": "0.3.0",
                "channel": "release",
            }
        ),
        encoding="utf-8",
    )
    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["release_label"] = "0.3.0"
    output.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=release_version)
    assert report["release_allowed"] is False
    assert any(
        "final release requires a hash-bound three-airframe PX4 Mission transaction acceptance"
        in reason
        for reason in report["release_blocked_reasons"]
    )


def test_manifest_writer_rejects_failed_or_malformed_transaction_evidence(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    transaction = tmp_path / "transaction.json"
    transaction.write_text(
        json.dumps(
            {
                "schema": "multidetect.px4-airframe-mission-transaction-acceptance",
                "schema_version": 1,
                "runtime_mode": "SIM",
                "release_label": "0.3.0-pre.8",
                "gate_passed": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReleaseGateManifestError, match="px4_airframe_transaction evidence"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "manifest.json",
            version_manifest_path=VERSION_MANIFEST,
            px4_airframe_transaction_evidence=transaction,
        )


def test_manifest_cli_binds_transaction_evidence(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    transaction = _transaction_evidence(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "manifest" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--px4-airframe-transaction-evidence",
        str(transaction),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert manifest_main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["px4_airframe_transaction"]["gate_passed"] is True


def test_release_gate_schema_declares_px4_transaction_binding() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "px4_airframe_transaction" in schema["properties"]
    definition = schema["$defs"]["px4AirframeTransactionRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["runtime_mode"]["const"] == "sim"
    assert definition["properties"]["gate_passed"]["const"] is True
    assert definition["properties"]["physical_output_enabled"]["const"] is False
    assert definition["properties"]["vehicle_classes"]["uniqueItems"] is True

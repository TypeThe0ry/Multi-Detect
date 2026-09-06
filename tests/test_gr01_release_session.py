from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from multidetect import gr01_release_plan as plan_module
from multidetect.evidence_bundle_transfer import create_evidence_transfer_inventory
from multidetect.gr01_bench import Gr01BenchConfig, run_gr01_link_bench
from multidetect.gr01_release_handoff import create_gr01_release_session_handoff
from multidetect.gr01_release_plan import create_gr01_release_session_plan
from multidetect.gr01_release_review import (
    INVENTORY_NAME,
    create_gr01_release_session_source_attestation,
    review_gr01_release_session_bundle,
    snapshot_gr01_release_session_bundle,
    verify_gr01_release_session_review_receipt,
    verify_gr01_release_session_source_attestation,
)
from multidetect.gr01_release_session import (
    Gr01ReleaseSessionError,
    create_gr01_release_session_record,
    main,
    verify_gr01_release_session_record,
)
from multidetect.operator_link import VideoGeometry
from multidetect.operator_protocol import SelectionAck, SelectionAckReason
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import create_release_gate_manifest

ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = ROOT / "configs" / "product-version.json"
SESSION_SCHEMA = ROOT / "configs" / "schemas" / "gr01-release-session.schema.json"
REVIEW_SCHEMA = ROOT / "configs" / "schemas" / "gr01-release-session-review.schema.json"
SOURCE_ATTESTATION_SCHEMA = (
    ROOT / "configs" / "schemas" / "gr01-release-session-source-attestation.schema.json"
)
MODES = ("replay", "sim", "hil", "real")
PARITY = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}
REVIEW_SIGNING_KEY_ENV = "MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX_TEST"
REVIEW_SIGNING_KEY_HEX = "11" * 32
OPERATOR_SIGNING_KEY_ENV = "MULTIDETECT_GR01_OPERATOR_ED25519_SEED_HEX_TEST"
OPERATOR_SIGNING_KEY_HEX = "22" * 32


def _review_signing(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv(REVIEW_SIGNING_KEY_ENV, REVIEW_SIGNING_KEY_HEX)
    return {
        "reviewer": "reviewer-b",
        "reviewer_key_id": "field-reviewer-b-2026",
        "reviewer_signing_key_environment": REVIEW_SIGNING_KEY_ENV,
    }


def _source_attestation(
    inventory: Path,
    output: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    monkeypatch.setenv(OPERATOR_SIGNING_KEY_ENV, OPERATOR_SIGNING_KEY_HEX)
    return create_gr01_release_session_source_attestation(
        inventory,
        output,
        operator="operator-a",
        operator_key_id="field-operator-a-2026",
        operator_signing_key_environment=OPERATOR_SIGNING_KEY_ENV,
    )


class _PassingSession:
    remote = ("192.168.144.20", 14580)
    local_source_address = "192.168.144.10"
    maximum_attempts = 3

    @staticmethod
    def deliver(_command: object) -> SimpleNamespace:
        return SimpleNamespace(
            acknowledgement=SelectionAck(
                command_id="00000000-0000-4000-8000-000000000001",
                accepted=True,
                reason=SelectionAckReason.ACCEPTED,
                acknowledged_sequence=1,
            ),
            attempts=1,
            elapsed_s=0.02,
        )


def _evidence(tmp_path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for mode in MODES:
        path = tmp_path / "evidence" / f"{mode}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "gate_passed": True,
                    "runtime_mode": mode,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result[mode] = path
    return result


def _statuses() -> dict[str, dict[str, str]]:
    return {
        mode: {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": ("not_required" if mode in {"replay", "sim"} else "passed"),
        }
        for mode in MODES
    }


def _sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    observed_at: datetime | None = None,
    evaluated_at: datetime | None = None,
    max_age_s: float = 900.0,
    planned_minimum_round_trips: int = 3,
    relocated_origin_plan: bool = False,
) -> tuple[Path, Path, Path, Path, Path]:
    observed_at = observed_at or datetime.now(timezone.utc)
    evaluated_at = evaluated_at or observed_at + timedelta(seconds=1)
    bench_document = run_gr01_link_bench(
        _PassingSession(),
        VideoGeometry("camera-main", 1280, 720),
        Gr01BenchConfig(
            minimum_round_trips=3,
            hardware_mode=True,
            hardware_id="GR01-OPERATOR-001",
            expected_source_cidr="192.168.144.0/24",
        ),
        wall_clock=lambda: 1_000.0,
        observed_at=lambda: observed_at,
    )
    bench = tmp_path / "session" / "gr01-link-bench.json"
    bench.parent.mkdir(parents=True, exist_ok=True)
    bench.write_text(json.dumps(bench_document, sort_keys=True) + "\n", encoding="utf-8")

    identity = plan_module._source_identity(ROOT)
    identity["repository"]["tracked_dirty"] = False
    identity["release_parity"]["source_verified"] = True
    monkeypatch.setattr(plan_module, "_source_identity", lambda _root: identity)
    monkeypatch.setenv("MULTIDETECT_OPERATOR_HMAC_KEY", "present-for-test")
    monkeypatch.setenv("MULTIDETECT_MAVLINK_SIGNING_KEY_HEX", "present-for-test")
    plan = tmp_path / "session" / "gr01-release-session-plan.json"
    repository_output = ROOT / "tmp"
    repository_output.mkdir(parents=True, exist_ok=True)
    create_gr01_release_session_plan(
        plan,
        host_address="192.168.144.20",
        port=14580,
        hardware_id="GR01-OPERATOR-001",
        expected_source_cidr="192.168.144.0/24",
        operator_hmac_environment="MULTIDETECT_OPERATOR_HMAC_KEY",
        mavlink_signing_environment="MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        minimum_round_trips=planned_minimum_round_trips,
        maximum_packet_loss_rate=0.01,
        maximum_ack_latency_p95_ms=500.0,
        maximum_attempts=3,
        maximum_evidence_age_s=max_age_s,
        repository_root=ROOT,
        python_path=Path(sys.executable),
        output_root=repository_output,
    )
    if relocated_origin_plan:
        plan_document = json.loads(plan.read_text(encoding="utf-8"))
        plan_document["paths"] = {
            "repository_root": "Z:\\origin\\Multi-Detect",
            "python": "Z:\\origin\\python.exe",
            "output_root": "Z:\\origin\\artifacts",
        }
        plan_document["secret_environment"]["operator_hmac"]["present"] = False
        plan_document["secret_environment"]["mavlink_signing"]["present"] = False
        plan_document["ready_to_execute"] = False
        plan_document["reasons"] = [
            "required secret environment variable is not set: MULTIDETECT_OPERATOR_HMAC_KEY",
            "required secret environment variable is not set: MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        ]
        plan.write_text(json.dumps(plan_document, sort_keys=True) + "\n", encoding="utf-8")
    handoff = tmp_path / "session" / "gr01-release-session-handoff.json"
    create_gr01_release_session_handoff(
        handoff,
        source_plan=plan,
        target_repository_root=ROOT,
        target_python=Path(sys.executable),
        target_output_root=repository_output,
        host_id="GR01-SESSION-TEST",
    )

    evidence = _evidence(tmp_path)
    manifest = tmp_path / "session" / "release-gate" / "manifest.json"
    create_release_gate_manifest(
        output_path=manifest,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_gr01_operator_link_bench_report=bench,
        real_gr01_operator_link_max_age_s=max_age_s,
    )
    report = tmp_path / "session" / "release-gate" / "report.json"
    report.write_text(
        json.dumps(
            evaluate_release_gate(
                manifest,
                version_manifest_path=VERSION_MANIFEST,
                evaluated_at_utc=evaluated_at.isoformat().replace("+00:00", "Z"),
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return plan, handoff, bench, manifest, report


def test_record_is_schema_valid_and_independently_reverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    record = tmp_path / "session" / "gr01-release-session.json"

    document = create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
        session_id="gr01-release-session-20260906T120000Z",
    )

    schema = json.loads(SESSION_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
        document
    )
    assert document["gate_passed"] is True
    assert document["release_plan"]["configuration_matches_bench_and_e7"] is True
    assert document["release_handoff"]["configuration_matches_plan_bench_and_e7"] is True
    assert document["claims"] == {
        "gr01_binding_verified": True,
        "handoff_binding_verified": True,
        "overall_release_allowed": False,
        "hardware_flight_acceptance": "not_claimed",
        "physical_release_enabled": False,
        "flight_control_enabled": False,
        "payload_release_requested": False,
    }
    verification = verify_gr01_release_session_record(
        record,
        version_manifest_path=VERSION_MANIFEST,
    )
    assert verification["gate_passed"] is True
    assert verification["reasons"] == []


def test_verifier_rejects_source_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    record = tmp_path / "session" / "gr01-release-session.json"
    create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    bench.write_text(bench.read_text(encoding="utf-8") + " ", encoding="utf-8")

    verification = verify_gr01_release_session_record(
        record,
        version_manifest_path=VERSION_MANIFEST,
    )
    assert verification["gate_passed"] is False
    assert any("does not match" in reason for reason in verification["reasons"])


def test_verifier_rejects_release_plan_configuration_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, handoff, bench, manifest, report = _sources(
        tmp_path,
        monkeypatch,
        planned_minimum_round_trips=4,
    )
    with pytest.raises(Gr01ReleaseSessionError, match="does not match bench and E7"):
        create_gr01_release_session_record(
            tmp_path / "session" / "gr01-release-session.json",
            release_plan=plan,
            release_handoff=handoff,
            bench_report=bench,
            release_manifest=manifest,
            release_report=report,
            version_manifest_path=VERSION_MANIFEST,
        )


def test_relocated_origin_plan_is_authorized_only_by_ready_target_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, handoff, bench, manifest, report = _sources(
        tmp_path,
        monkeypatch,
        relocated_origin_plan=True,
    )
    record = tmp_path / "session" / "gr01-release-session.json"
    document = create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )

    assert document["release_plan"]["ready_to_execute"] is False
    assert document["release_handoff"]["ready_to_execute"] is True
    assert (
        verify_gr01_release_session_record(
            record,
            version_manifest_path=VERSION_MANIFEST,
        )["gate_passed"]
        is True
    )


def test_verifier_rejects_handoff_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    record = tmp_path / "session" / "gr01-release-session.json"
    create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    handoff_document = json.loads(handoff.read_text(encoding="utf-8"))
    handoff_document["host_id"] = "TAMPERED-HOST"
    handoff.write_text(json.dumps(handoff_document) + "\n", encoding="utf-8")

    verification = verify_gr01_release_session_record(
        record,
        version_manifest_path=VERSION_MANIFEST,
    )
    assert verification["gate_passed"] is False
    assert any("handoff" in reason for reason in verification["reasons"])


def test_creator_rejects_expired_gr01_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_at = datetime.now(timezone.utc)
    plan, handoff, bench, manifest, report = _sources(
        tmp_path,
        monkeypatch,
        observed_at=observed_at,
        evaluated_at=observed_at + timedelta(seconds=31),
        max_age_s=30.0,
    )

    with pytest.raises(Gr01ReleaseSessionError, match="binding failed"):
        create_gr01_release_session_record(
            tmp_path / "session" / "gr01-release-session.json",
            release_plan=plan,
            release_handoff=handoff,
            bench_report=bench,
            release_manifest=manifest,
            release_report=report,
            version_manifest_path=VERSION_MANIFEST,
        )


def test_verifier_rejects_record_metadata_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    record = tmp_path / "session" / "gr01-release-session.json"
    create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    document = json.loads(record.read_text(encoding="utf-8"))
    document["session_id"] = 7
    record.write_text(json.dumps(document) + "\n", encoding="utf-8")

    verification = verify_gr01_release_session_record(
        record,
        version_manifest_path=VERSION_MANIFEST,
    )
    assert verification["gate_passed"] is False
    assert "session_id must be a non-empty string" in verification["reasons"]


def test_creator_is_write_new_and_cli_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    record = tmp_path / "session" / "gr01-release-session.json"
    record.write_text("reserved\n", encoding="utf-8")

    with pytest.raises(Gr01ReleaseSessionError, match="already exists"):
        create_gr01_release_session_record(
            record,
            release_plan=plan,
            release_handoff=handoff,
            bench_report=bench,
            release_manifest=manifest,
            release_report=report,
            version_manifest_path=VERSION_MANIFEST,
        )
    assert main(["verify", str(record)]) == 1
    assert "gate_passed" in capsys.readouterr().out


def test_field_snapshot_and_second_machine_portable_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, handoff, bench, manifest, report = _sources(
        tmp_path,
        monkeypatch,
        relocated_origin_plan=True,
    )
    source_root = tmp_path / "session"
    record = source_root / "gr01-release-session.json"
    create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
        session_id="gr01-portable-review-test",
    )
    inventory = source_root / INVENTORY_NAME
    snapshot = snapshot_gr01_release_session_bundle(
        source_root,
        inventory,
        version_manifest_path=VERSION_MANIFEST,
        bundle_id="12345678-1234-5678-9234-567812345678",
    )
    source_attestation_path = tmp_path / "source-attestation.json"
    source_attestation = _source_attestation(inventory, source_attestation_path, monkeypatch)
    source_schema = json.loads(SOURCE_ATTESTATION_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(source_schema)
    Draft202012Validator(
        source_schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(source_attestation)
    operator_public_key = source_attestation["operator"]["public_key_ed25519_hex"]
    assert (
        verify_gr01_release_session_source_attestation(
            source_attestation_path,
            inventory,
            expected_operator_public_key_hex=operator_public_key,
        )
        == source_attestation
    )
    with pytest.raises(ValueError, match="operator public key is not trusted"):
        verify_gr01_release_session_source_attestation(
            source_attestation_path,
            inventory,
            expected_operator_public_key_hex="00" * 32,
        )
    tampered_source_path = tmp_path / "tampered-source-attestation.json"
    tampered_source = json.loads(source_attestation_path.read_text(encoding="utf-8"))
    tampered_source["signature"]["signature_ed25519_hex"] = "00" * 64
    tampered_source_path.write_text(json.dumps(tampered_source) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Ed25519 signature is invalid"):
        verify_gr01_release_session_source_attestation(
            tampered_source_path,
            inventory,
            expected_operator_public_key_hex=operator_public_key,
        )
    destination = tmp_path / "second-machine-session"
    shutil.copytree(source_root, destination)
    monkeypatch.setenv(REVIEW_SIGNING_KEY_ENV, REVIEW_SIGNING_KEY_HEX)
    with pytest.raises(ValueError, match="roles and keys must be distinct"):
        review_gr01_release_session_bundle(
            destination / INVENTORY_NAME,
            source_attestation_path,
            destination,
            tmp_path / "same-identity-transfer.json",
            tmp_path / "same-identity-review.json",
            expected_inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(),
            expected_operator_public_key_hex=operator_public_key,
            reviewer="operator-a",
            reviewer_key_id="field-reviewer-b-2026",
            reviewer_signing_key_environment=REVIEW_SIGNING_KEY_ENV,
        )
    assert not (tmp_path / "same-identity-transfer.json").exists()
    monkeypatch.setenv(REVIEW_SIGNING_KEY_ENV, OPERATOR_SIGNING_KEY_HEX)
    with pytest.raises(ValueError, match="roles and keys must be distinct"):
        review_gr01_release_session_bundle(
            destination / INVENTORY_NAME,
            source_attestation_path,
            destination,
            tmp_path / "same-key-transfer.json",
            tmp_path / "same-key-review.json",
            expected_inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(),
            expected_operator_public_key_hex=operator_public_key,
            reviewer="reviewer-b",
            reviewer_key_id="field-reviewer-b-2026",
            reviewer_signing_key_environment=REVIEW_SIGNING_KEY_ENV,
        )
    assert not (tmp_path / "same-key-transfer.json").exists()
    transfer_receipt = tmp_path / "transfer-receipt.json"
    review_receipt = tmp_path / "portable-review.json"
    review = review_gr01_release_session_bundle(
        destination / INVENTORY_NAME,
        source_attestation_path,
        destination,
        transfer_receipt,
        review_receipt,
        expected_inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest().upper(),
        expected_operator_public_key_hex=operator_public_key,
        **_review_signing(monkeypatch),
    )

    schema = json.loads(REVIEW_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(review)
    assert snapshot["file_count"] == 6
    assert review["transfer"]["byte_exact_copy"] is True
    assert review["transfer"]["inventory_hash_matches_expected"] is True
    assert (
        review["transfer"]["expected_inventory_file_sha256"]
        == review["transfer"]["inventory_file_sha256"]
    )
    assert review["session"]["source_count"] == 5
    assert review["session"]["five_source_hashes_verified"] is True
    assert review["session"]["bench_recomputed"] is True
    assert review["reviewer"]["identity"] == "reviewer-b"
    assert review["source_attestation"]["operator_identity"] == "operator-a"
    assert review["signature"]["algorithm"] == "Ed25519"
    assert review["claims"] == {
        "portable_session_integrity_verified": True,
        "source_inventory_signature_verified": True,
        "operator_key_possession_verified": True,
        "operator_human_identity_verified": False,
        "operator_reviewer_separation_verified": True,
        "review_signature_verified": True,
        "reviewer_key_possession_verified": True,
        "reviewer_human_identity_verified": False,
        "target_environment_reverified": False,
        "canonical_e7_sources_recomputed": False,
        "hardware_flight_acceptance": "not_claimed",
        "physical_release_enabled": False,
        "flight_control_enabled": False,
        "payload_release_requested": False,
    }
    expected_public_key = review["reviewer"]["public_key_ed25519_hex"]
    assert (
        verify_gr01_release_session_review_receipt(
            review_receipt,
            expected_reviewer_public_key_hex=expected_public_key,
        )
        == review
    )
    with pytest.raises(ValueError, match="public key is not trusted"):
        verify_gr01_release_session_review_receipt(
            review_receipt,
            expected_reviewer_public_key_hex="00" * 32,
        )
    changed = json.loads(review_receipt.read_text(encoding="utf-8"))
    changed["signature"]["signature_ed25519_hex"] = "00" * 64
    review_receipt.write_text(json.dumps(changed) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Ed25519 signature is invalid"):
        verify_gr01_release_session_review_receipt(
            review_receipt,
            expected_reviewer_public_key_hex=expected_public_key,
        )
    review_receipt.write_text(json.dumps(review) + "\n", encoding="utf-8")
    changed = json.loads(review_receipt.read_text(encoding="utf-8"))
    changed["claims"]["target_environment_reverified"] = True
    review_receipt.write_text(json.dumps(changed) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="claim boundary"):
        verify_gr01_release_session_review_receipt(
            review_receipt,
            expected_reviewer_public_key_hex=expected_public_key,
        )


def test_portable_review_rejects_changed_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    source_root = tmp_path / "session"
    create_gr01_release_session_record(
        source_root / "gr01-release-session.json",
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    snapshot_gr01_release_session_bundle(
        source_root,
        source_root / INVENTORY_NAME,
        version_manifest_path=VERSION_MANIFEST,
    )
    source_attestation_path = tmp_path / "changed-source-attestation.json"
    source_attestation = _source_attestation(
        source_root / INVENTORY_NAME, source_attestation_path, monkeypatch
    )
    destination = tmp_path / "changed-copy"
    shutil.copytree(source_root, destination)
    (destination / "gr01-link-bench.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed="):
        review_gr01_release_session_bundle(
            destination / INVENTORY_NAME,
            source_attestation_path,
            destination,
            tmp_path / "changed-transfer.json",
            tmp_path / "changed-review.json",
            expected_inventory_sha256=hashlib.sha256(
                (destination / INVENTORY_NAME).read_bytes()
            ).hexdigest(),
            expected_operator_public_key_hex=source_attestation["operator"][
                "public_key_ed25519_hex"
            ],
            **_review_signing(monkeypatch),
        )
    assert not (tmp_path / "changed-review.json").exists()


def test_portable_semantic_review_rejects_reinventoried_record_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    source_root = tmp_path / "session"
    record = source_root / "gr01-release-session.json"
    create_gr01_release_session_record(
        record,
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    changed = json.loads(record.read_text(encoding="utf-8"))
    changed["release_plan"]["sha256"] = "0" * 64
    record.write_text(json.dumps(changed) + "\n", encoding="utf-8")
    create_evidence_transfer_inventory(source_root, source_root / INVENTORY_NAME)
    source_attestation_path = tmp_path / "reinventoried-source-attestation.json"
    source_attestation = _source_attestation(
        source_root / INVENTORY_NAME, source_attestation_path, monkeypatch
    )
    destination = tmp_path / "reinventoried-copy"
    shutil.copytree(source_root, destination)

    with pytest.raises(ValueError, match="release_plan file identity"):
        review_gr01_release_session_bundle(
            destination / INVENTORY_NAME,
            source_attestation_path,
            destination,
            tmp_path / "reinventoried-transfer.json",
            tmp_path / "reinventoried-review.json",
            expected_inventory_sha256=hashlib.sha256(
                (destination / INVENTORY_NAME).read_bytes()
            ).hexdigest(),
            expected_operator_public_key_hex=source_attestation["operator"][
                "public_key_ed25519_hex"
            ],
            **_review_signing(monkeypatch),
        )


def test_portable_review_requires_matching_out_of_band_inventory_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    source_root = tmp_path / "session"
    create_gr01_release_session_record(
        source_root / "gr01-release-session.json",
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    snapshot_gr01_release_session_bundle(
        source_root,
        source_root / INVENTORY_NAME,
        version_manifest_path=VERSION_MANIFEST,
    )
    inventory = source_root / INVENTORY_NAME
    source_attestation_path = tmp_path / "wrong-anchor-source-attestation.json"
    source_attestation = _source_attestation(inventory, source_attestation_path, monkeypatch)
    destination = tmp_path / "wrong-anchor-copy"
    shutil.copytree(source_root, destination)
    transfer_receipt = tmp_path / "wrong-anchor-transfer.json"
    review_receipt = tmp_path / "wrong-anchor-review.json"

    with pytest.raises(ValueError, match="does not match trusted value"):
        review_gr01_release_session_bundle(
            destination / INVENTORY_NAME,
            source_attestation_path,
            destination,
            transfer_receipt,
            review_receipt,
            expected_inventory_sha256="0" * 64,
            expected_operator_public_key_hex=source_attestation["operator"][
                "public_key_ed25519_hex"
            ],
            **_review_signing(monkeypatch),
        )
    assert not transfer_receipt.exists()
    assert not review_receipt.exists()


@pytest.mark.parametrize(
    ("signing_key_value", "message"),
    [(None, "environment variable is missing"), ("not-a-key", "64 hexadecimal characters")],
)
def test_portable_review_rejects_missing_or_invalid_signing_key_before_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_key_value: str | None,
    message: str,
) -> None:
    plan, handoff, bench, manifest, report = _sources(tmp_path, monkeypatch)
    source_root = tmp_path / "session"
    create_gr01_release_session_record(
        source_root / "gr01-release-session.json",
        release_plan=plan,
        release_handoff=handoff,
        bench_report=bench,
        release_manifest=manifest,
        release_report=report,
        version_manifest_path=VERSION_MANIFEST,
    )
    inventory = source_root / INVENTORY_NAME
    snapshot_gr01_release_session_bundle(
        source_root,
        inventory,
        version_manifest_path=VERSION_MANIFEST,
    )
    source_attestation_path = tmp_path / "unsigned-source-attestation.json"
    source_attestation = _source_attestation(inventory, source_attestation_path, monkeypatch)
    destination = tmp_path / "unsigned-copy"
    shutil.copytree(source_root, destination)
    key_environment = "MULTIDETECT_MISSING_OR_INVALID_REVIEW_KEY_TEST"
    monkeypatch.delenv(key_environment, raising=False)
    if signing_key_value is not None:
        monkeypatch.setenv(key_environment, signing_key_value)
    transfer_receipt = tmp_path / "unsigned-transfer.json"
    review_receipt = tmp_path / "unsigned-review.json"

    with pytest.raises(ValueError, match=message):
        review_gr01_release_session_bundle(
            destination / INVENTORY_NAME,
            source_attestation_path,
            destination,
            transfer_receipt,
            review_receipt,
            expected_inventory_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(),
            expected_operator_public_key_hex=source_attestation["operator"][
                "public_key_ed25519_hex"
            ],
            reviewer="reviewer-b",
            reviewer_key_id="field-reviewer-b-2026",
            reviewer_signing_key_environment=key_environment,
        )
    assert not transfer_receipt.exists()
    assert not review_receipt.exists()


def test_portable_review_repository_entry_point_has_all_modes() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_gr01_release_session_review.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "snapshot" in completed.stdout
    assert "attest-source" in completed.stdout
    assert "review" in completed.stdout
    assert "verify" in completed.stdout
    review_help = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_gr01_release_session_review.py"),
            "review",
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert review_help.returncode == 0, review_help.stderr
    assert "--expected-inventory-sha256" in review_help.stdout
    assert "--source-attestation" in review_help.stdout
    assert "--expected-operator-public-key-hex" in review_help.stdout
    assert "--reviewer" in review_help.stdout
    assert "--reviewer-key-id" in review_help.stdout
    assert "--reviewer-signing-key-env" in review_help.stdout
    verify_help = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_gr01_release_session_review.py"),
            "verify",
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify_help.returncode == 0, verify_help.stderr
    assert "--expected-reviewer-public-key-hex" in verify_help.stdout

from __future__ import annotations

import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect import gr01_release_plan as plan_module
from multidetect.gr01_release_handoff import (
    Gr01ReleaseHandoffError,
    create_gr01_release_session_handoff,
    main,
    verify_gr01_release_session_handoff,
    verify_gr01_release_session_handoff_configuration,
    verify_gr01_release_session_handoff_portable,
)
from multidetect.gr01_release_plan import create_gr01_release_session_plan

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable).resolve()
SCHEMA = ROOT / "configs" / "schemas" / "gr01-release-session-handoff.schema.json"


def _source_plan(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, object]]:
    identity = plan_module._source_identity(ROOT)
    identity["repository"]["tracked_dirty"] = False
    identity["release_parity"]["source_verified"] = True
    monkeypatch.setattr(plan_module, "_source_identity", lambda _root: deepcopy(identity))
    monkeypatch.delenv("MULTIDETECT_OPERATOR_HMAC_KEY", raising=False)
    monkeypatch.delenv("MULTIDETECT_MAVLINK_SIGNING_KEY_HEX", raising=False)
    plan = directory / "source-plan.json"
    create_gr01_release_session_plan(
        plan,
        host_address="192.168.144.20",
        port=14580,
        hardware_id="GR01-OPERATOR-001",
        expected_source_cidr="192.168.144.0/24",
        operator_hmac_environment="MULTIDETECT_OPERATOR_HMAC_KEY",
        mavlink_signing_environment="MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        minimum_round_trips=100,
        maximum_packet_loss_rate=0.01,
        maximum_ack_latency_p95_ms=500.0,
        maximum_attempts=3,
        maximum_evidence_age_s=900.0,
        repository_root=ROOT,
        python_path=PYTHON,
        output_root=ROOT / "tmp",
    )
    return plan, identity


def _create(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    secrets_present: bool = False,
) -> tuple[Path, Path, dict[str, object]]:
    plan, identity = _source_plan(directory, monkeypatch)
    plan_document = json.loads(plan.read_text(encoding="utf-8"))
    plan_document["paths"] = {
        "repository_root": "Z:\\origin\\Multi-Detect",
        "python": "Z:\\origin\\python.exe",
        "output_root": "Z:\\origin\\artifacts",
    }
    plan.write_text(json.dumps(plan_document, sort_keys=True) + "\n", encoding="utf-8")
    if secrets_present:
        monkeypatch.setenv("MULTIDETECT_OPERATOR_HMAC_KEY", "target-present")
        monkeypatch.setenv("MULTIDETECT_MAVLINK_SIGNING_KEY_HEX", "target-present")
    receipt = directory / "handoff.json"
    document = create_gr01_release_session_handoff(
        receipt,
        source_plan=plan,
        target_repository_root=ROOT,
        target_python=PYTHON,
        target_output_root=ROOT / "tmp",
        host_id="FIELD-GCS-01",
    )
    return plan, receipt, document


def _match(receipt: Path, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "host_address": "192.168.144.20",
        "port": 14580,
        "hardware_id": "GR01-OPERATOR-001",
        "expected_source_cidr": "192.168.144.99/24",
        "operator_hmac_environment": "MULTIDETECT_OPERATOR_HMAC_KEY",
        "mavlink_signing_environment": "MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        "minimum_round_trips": 100,
        "maximum_packet_loss_rate": 0.01,
        "maximum_ack_latency_p95_ms": 500.0,
        "maximum_attempts": 3,
        "maximum_evidence_age_s": 900.0,
    }
    arguments.update(overrides)
    return verify_gr01_release_session_handoff_configuration(receipt, **arguments)


def test_handoff_is_path_relocated_source_bound_and_schema_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-", dir=ROOT / "tmp") as name:
        plan, receipt, document = _create(Path(name), monkeypatch)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)

        assert (
            "Z:\\origin" in json.loads(plan.read_text(encoding="utf-8"))["paths"]["repository_root"]
        )
        assert document["target_paths"]["repository_root"] == str(ROOT)
        assert document["source_equivalent"] is True
        assert document["ready_to_execute"] is False
        assert len(document["reasons"]) == 2
        verification = verify_gr01_release_session_handoff(receipt)
        assert verification["gate_passed"] is True
        assert verification["reasons"] == []
        portable = verify_gr01_release_session_handoff_portable(receipt)
        assert portable["gate_passed"] is True
        assert portable["target_environment_reverified"] is False


def test_handoff_can_be_ready_only_with_target_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-ready-", dir=ROOT / "tmp") as name:
        _plan, receipt, document = _create(
            Path(name),
            monkeypatch,
            secrets_present=True,
        )
        assert document["ready_to_execute"] is True
        assert document["reasons"] == []
        verification = verify_gr01_release_session_handoff(
            receipt,
            check_environment=True,
            require_ready=True,
        )
        assert verification["gate_passed"] is True

        match = _match(receipt)
        assert match["gate_passed"] is True
        assert match["reasons"] == []
        assert len(match["requested_configuration_sha256"]) == 64


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"port": 14581}, "configuration does not match"),
        ({"minimum_round_trips": 101}, "configuration does not match"),
        ({"operator_hmac_environment": "DIFFERENT_KEY"}, "secret names do not match"),
    ],
)
def test_handoff_requested_execution_match_fails_closed_before_network(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-match-", dir=ROOT / "tmp") as name:
        _plan, receipt, _document = _create(
            Path(name),
            monkeypatch,
            secrets_present=True,
        )
        match = _match(receipt, **overrides)
        assert match["gate_passed"] is False
        assert any(message in reason for reason in match["reasons"])


def test_handoff_reports_target_source_mismatch_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-mismatch-", dir=ROOT / "tmp") as name:
        plan, identity = _source_plan(Path(name), monkeypatch)
        mismatch = deepcopy(identity)
        mismatch["repository"]["git_head"] = "f" * 40
        monkeypatch.setattr(plan_module, "_source_identity", lambda _root: deepcopy(mismatch))
        receipt = Path(name) / "handoff.json"
        document = create_gr01_release_session_handoff(
            receipt,
            source_plan=plan,
            target_repository_root=ROOT,
            target_python=PYTHON,
            target_output_root=ROOT / "tmp",
            host_id="FIELD-GCS-02",
        )

        assert document["source_equivalent"] is False
        assert document["ready_to_execute"] is False
        assert "git_head_matches" in document["reasons"]
        verification = verify_gr01_release_session_handoff(receipt, require_ready=True)
        assert verification["gate_passed"] is False
        assert "handoff is not ready to execute" in verification["reasons"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document["source_identity"]["repository"].update(git_head="BAD"),
            "repository identity",
        ),
        (
            lambda document: document["source_identity"]["tools"][
                "scripts_run_gr01_release_session_ps1"
            ].update(bytes=True),
            "tool scripts_run_gr01_release_session_ps1 identity",
        ),
    ],
)
def test_handoff_rejects_malformed_source_identity_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    message: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-invalid-", dir=ROOT / "tmp") as name:
        directory = Path(name)
        plan, _identity = _source_plan(directory, monkeypatch)
        plan_document = json.loads(plan.read_text(encoding="utf-8"))
        mutate(plan_document)
        plan.write_text(json.dumps(plan_document) + "\n", encoding="utf-8")
        receipt = directory / "handoff.json"
        with pytest.raises(Gr01ReleaseHandoffError, match=message):
            create_gr01_release_session_handoff(
                receipt,
                source_plan=plan,
                target_repository_root=ROOT,
                target_python=PYTHON,
                target_output_root=ROOT / "tmp",
                host_id="FIELD-GCS-INVALID",
            )
        assert not receipt.exists()


def test_handoff_detects_source_plan_and_environment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-drift-", dir=ROOT / "tmp") as name:
        plan, receipt, _document = _create(Path(name), monkeypatch)
        monkeypatch.setenv("MULTIDETECT_OPERATOR_HMAC_KEY", "appeared-after-handoff")
        environment = verify_gr01_release_session_handoff(receipt, check_environment=True)
        assert environment["gate_passed"] is False

        plan.write_text(plan.read_text(encoding="utf-8") + " ", encoding="utf-8")
        source = verify_gr01_release_session_handoff(receipt)
        assert source["gate_passed"] is False


def test_portable_handoff_recomputes_stored_target_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-portable-", dir=ROOT / "tmp") as name:
        _plan, receipt, document = _create(
            Path(name),
            monkeypatch,
            secrets_present=True,
        )
        document["target_identity"]["git_head"] = "f" * 40
        document["checks"]["git_head_matches"] = True
        receipt.write_text(json.dumps(document) + "\n", encoding="utf-8")

        verification = verify_gr01_release_session_handoff_portable(receipt)
        assert verification["gate_passed"] is False
        assert any(
            "target Git" in reason or "checks" in reason for reason in verification["reasons"]
        )


def test_handoff_is_write_new_and_target_output_must_stay_in_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-write-", dir=ROOT / "tmp") as name:
        plan, receipt, _document = _create(Path(name), monkeypatch)
        with pytest.raises(Gr01ReleaseHandoffError, match="already exists"):
            create_gr01_release_session_handoff(
                receipt,
                source_plan=plan,
                target_repository_root=ROOT,
                target_python=PYTHON,
                target_output_root=ROOT / "tmp",
                host_id="FIELD-GCS-01",
            )
        with pytest.raises(Gr01ReleaseHandoffError, match="escapes target repository"):
            create_gr01_release_session_handoff(
                Path(name) / "outside.json",
                source_plan=plan,
                target_repository_root=ROOT,
                target_python=PYTHON,
                target_output_root=ROOT.parent,
                host_id="FIELD-GCS-01",
            )


def test_handoff_cli_create_allows_diagnostic_receipt_and_verify_is_strict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with tempfile.TemporaryDirectory(prefix="gr01-handoff-cli-", dir=ROOT / "tmp") as name:
        directory = Path(name)
        plan, _identity = _source_plan(directory, monkeypatch)
        receipt = directory / "handoff.json"
        assert (
            main(
                [
                    "create",
                    "--source-plan",
                    str(plan),
                    "--repository-root",
                    str(ROOT),
                    "--python",
                    str(PYTHON),
                    "--output-root",
                    str(ROOT / "tmp"),
                    "--host-id",
                    "FIELD-GCS-CLI",
                    "--out",
                    str(receipt),
                ]
            )
            == 0
        )
        assert "ready_to_execute" in capsys.readouterr().out
        assert main(["verify", str(receipt), "--require-ready"]) == 1
        assert "handoff is not ready" in capsys.readouterr().out

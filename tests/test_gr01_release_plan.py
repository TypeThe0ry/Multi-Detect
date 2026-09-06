from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect import gr01_release_plan as plan_module
from multidetect.gr01_release_plan import (
    Gr01ReleasePlanError,
    create_gr01_release_session_plan,
    main,
    verify_gr01_release_session_plan,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "configs" / "schemas" / "gr01-release-session-plan.schema.json"


def _create(directory: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, object]]:
    monkeypatch.delenv("MULTIDETECT_OPERATOR_HMAC_KEY", raising=False)
    monkeypatch.delenv("MULTIDETECT_MAVLINK_SIGNING_KEY_HEX", raising=False)
    output = directory / "plan.json"
    document = create_gr01_release_session_plan(
        output,
        host_address="192.168.144.20",
        port=14580,
        hardware_id="GR01-PLAN-TEST",
        expected_source_cidr="192.168.144.99/24",
        operator_hmac_environment="MULTIDETECT_OPERATOR_HMAC_KEY",
        mavlink_signing_environment="MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        minimum_round_trips=100,
        maximum_packet_loss_rate=0.01,
        maximum_ack_latency_p95_ms=500.0,
        maximum_attempts=3,
        maximum_evidence_age_s=900.0,
        repository_root=ROOT,
        python_path=sys.executable,
        output_root=directory,
    )
    return output, document


def test_plan_is_source_bound_schema_valid_and_reverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gr01-plan-unit-", dir=temp_root) as name:
        output, document = _create(Path(name), monkeypatch)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)

        assert document["expected_source_cidr"] == "192.168.144.0/24"
        assert document["ready_to_execute"] is False
        assert len(document["source_identity"]["tools"]) == 4
        verification = verify_gr01_release_session_plan(output)
        assert verification["gate_passed"] is True
        assert verification["reasons"] == []


def test_verifier_rejects_tool_projection_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gr01-plan-tamper-", dir=temp_root) as name:
        output, document = _create(Path(name), monkeypatch)
        tools = document["source_identity"]["tools"]
        first = next(iter(tools.values()))
        first["sha256"] = "0" * 64
        output.write_text(json.dumps(document) + "\n", encoding="utf-8")

        verification = verify_gr01_release_session_plan(output)
        assert verification["gate_passed"] is False
        assert verification["reasons"] == [
            "plan does not match current source and configuration recomputation"
        ]


def test_environment_recheck_detects_presence_change(monkeypatch: pytest.MonkeyPatch) -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gr01-plan-env-", dir=temp_root) as name:
        output, _document = _create(Path(name), monkeypatch)
        monkeypatch.setenv("MULTIDETECT_OPERATOR_HMAC_KEY", "present-after-plan")

        verification = verify_gr01_release_session_plan(output, check_environment=True)
        assert verification["gate_passed"] is False


def test_require_ready_rejects_diagnostic_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gr01-plan-not-ready-", dir=temp_root) as name:
        output, _document = _create(Path(name), monkeypatch)

        verification = verify_gr01_release_session_plan(output, require_ready=True)
        assert verification["gate_passed"] is False
        assert "plan is not ready to execute" in verification["reasons"]


def test_ready_requires_secrets_clean_git_and_verified_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    identity = plan_module._source_identity(ROOT)
    identity["repository"]["tracked_dirty"] = False
    identity["release_parity"]["source_verified"] = True
    monkeypatch.setattr(plan_module, "_source_identity", lambda _root: identity)
    monkeypatch.setenv("MULTIDETECT_OPERATOR_HMAC_KEY", "present")
    monkeypatch.setenv("MULTIDETECT_MAVLINK_SIGNING_KEY_HEX", "present")
    with tempfile.TemporaryDirectory(prefix="gr01-plan-ready-", dir=temp_root) as name:
        output = Path(name) / "plan.json"
        document = create_gr01_release_session_plan(
            output,
            host_address="192.168.144.20",
            port=14580,
            hardware_id="GR01-PLAN-TEST",
            expected_source_cidr="192.168.144.0/24",
            operator_hmac_environment="MULTIDETECT_OPERATOR_HMAC_KEY",
            mavlink_signing_environment="MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
            minimum_round_trips=100,
            maximum_packet_loss_rate=0.01,
            maximum_ack_latency_p95_ms=500.0,
            maximum_attempts=3,
            maximum_evidence_age_s=900.0,
            repository_root=ROOT,
            python_path=sys.executable,
            output_root=Path(name),
        )
        assert document["ready_to_execute"] is True
        assert document["reasons"] == []


def test_creator_is_write_new_and_cli_rejects_invalid_plan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gr01-plan-write-new-", dir=temp_root) as name:
        output, _document = _create(Path(name), monkeypatch)
        with pytest.raises(Gr01ReleasePlanError, match="already exists"):
            create_gr01_release_session_plan(
                output,
                host_address="192.168.144.20",
                port=14580,
                hardware_id="GR01-PLAN-TEST",
                expected_source_cidr="192.168.144.0/24",
                operator_hmac_environment="A",
                mavlink_signing_environment="B",
                minimum_round_trips=100,
                maximum_packet_loss_rate=0.01,
                maximum_ack_latency_p95_ms=500.0,
                maximum_attempts=3,
                maximum_evidence_age_s=900.0,
                repository_root=ROOT,
                python_path=sys.executable,
                output_root=Path(name),
            )
        output.write_text("not-json\n", encoding="utf-8")
        assert main(["verify", str(output)]) == 1
        assert "gate_passed" in capsys.readouterr().out

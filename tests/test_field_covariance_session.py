from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect import field_covariance_journal as journal
from multidetect import field_covariance_session as session
from multidetect.compat import UTC
from multidetect.field_covariance_campaign import REQUIRED_COMBINATIONS


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _campaign(workspace: Path) -> Path:
    entries: list[dict[str, object]] = []
    for index, (mode, vehicle) in enumerate(REQUIRED_COMBINATIONS):
        source = workspace / "sources" / mode / vehicle
        source.mkdir(parents=True)
        mission = source / "mission.json"
        feedback = source / "mission-feedback.json"
        profile = source / "runtime-profile.json"
        for path in (mission, feedback, profile):
            path.write_text("{}", encoding="utf-8")
        entries.append(
            {
                "runtime_mode": mode,
                "vehicle_class": vehicle,
                "endpoint": f"serial:COM{index + 3}",
                "baud": 57600,
                "mission_path": str(mission.relative_to(workspace)),
                "mission_feedback_path": str(feedback.relative_to(workspace)),
                "runtime_profile_path": str(profile.relative_to(workspace)),
                "report_path": f"reports/{mode}/{vehicle}.json",
            }
        )
    path = workspace / "campaign.json"
    path.write_text(
        json.dumps(
            {
                "schema": "multidetect.pixhawk-covariance-field-campaign/v1",
                "schema_version": 1,
                "release_label": "0.3.0-pre.8",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _install_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, object]:
    workspace = tmp_path / "received-workspace"
    campaign_path = _campaign(workspace)
    config = session.load_campaign_config(campaign_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"receipt":true}', encoding="utf-8")
    receipt = {
        "receipt_path": str(receipt_path.resolve()),
        "destination_workspace_root": str(workspace.resolve()),
        "receipt_sha256": "a" * 64,
        "workspace_sha256": "b" * 64,
        "gate_passed": True,
    }
    monkeypatch.setattr(
        session,
        "verify_field_covariance_workspace_receipt",
        lambda path: dict(receipt),
    )
    monkeypatch.setattr(
        journal,
        "verify_field_covariance_workspace_receipt",
        lambda path: dict(receipt),
    )

    def status(current: object) -> dict[str, object]:
        records: list[dict[str, object]] = []
        for leaf in current.leaves:
            if leaf.report_path.exists():
                records.append(
                    {
                        "runtime_mode": leaf.runtime_mode,
                        "vehicle_class": leaf.vehicle_class,
                        "report_path": str(leaf.report_path),
                        "status": "passed",
                        "gate_passed": True,
                        "report_sha256": _sha(leaf.report_path),
                        "report_bytes": leaf.report_path.stat().st_size,
                        "evidence_sha256": "c" * 64,
                        "records_verified": 12,
                        "firmware_identity_hash": "d" * 64,
                    }
                )
            else:
                records.append(
                    {
                        "runtime_mode": leaf.runtime_mode,
                        "vehicle_class": leaf.vehicle_class,
                        "report_path": str(leaf.report_path),
                        "status": "pending",
                        "gate_passed": False,
                        "reason": "report absent",
                    }
                )
        passed = sum(item["status"] == "passed" for item in records)
        pending = [item for item in records if item["status"] == "pending"]
        return {
            "records": records,
            "passed_count": passed,
            "pending_count": len(pending),
            "failed_count": 0,
            "next_leaf": pending[0] if pending else None,
            "complete": not pending,
        }

    monkeypatch.setattr(session, "campaign_status", status)

    def capture(current: object, **kwargs: object) -> tuple[int, dict[str, object]]:
        probe_path = kwargs["probe_report_out"]
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text('{"online":true}', encoding="utf-8")
        leaf = current.leaves[0]
        leaf.report_path.parent.mkdir(parents=True, exist_ok=True)
        leaf.report_path.write_text('{"covariance":true}', encoding="utf-8")
        return 0, {
            **status(current),
            "capture_process_exit_code": 0,
            "capture_verified": True,
            "online_preflight_verified_before_capture": True,
        }

    monkeypatch.setattr(session, "capture_next", capture)
    monkeypatch.setattr(
        session,
        "load_online_covariance_preflight_observation",
        lambda path, **kwargs: {
            "runtime_mode": "hil",
            "vehicle_class": "fixed-wing",
            "evidence_sha256": "e" * 64,
            "source_binding": {"campaign_config_path": str(campaign_path.resolve())},
        },
    )
    return receipt_path, workspace, config


def test_schema_is_valid_and_closed() -> None:
    schema = json.loads(session.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["receipt"]["additionalProperties"] is False
    assert schema["properties"]["claims"]["additionalProperties"] is False


def test_run_next_writes_probe_then_seals_reverifiable_success_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _, _ = _install_runtime(tmp_path, monkeypatch)
    journal = tmp_path / "journal"

    exit_code, result = session.run_next_field_covariance_session(
        receipt,
        journal,
        created_at=datetime(2026, 9, 6, 9, 0, tzinfo=UTC),
        attempt_id="bench-a",
    )

    assert exit_code == 0
    assert result["gate_passed"] is True
    assert result["campaign"]["leaf_ordinal"] == 1
    assert result["campaign"]["runtime_mode"] == "hil"
    assert result["campaign"]["vehicle_class"] == "fixed-wing"
    assert len(result["capture_lease"]["lease_id"]) == 32
    assert len(result["capture_lease"]["lease_sha256"]) == 64
    assert result["online_preflight"]["verified_before_capture"] is True
    assert result["capture_result"] == {
        "capture_process_exit_code": 0,
        "capture_verified": True,
        "passed_count_after": 1,
        "pending_count_after": 5,
        "campaign_complete_after": False,
    }
    assert result["claims"] == {
        "release_eligible": False,
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "flight_acceptance": False,
    }
    assert Path(result["record_path"]).is_file()
    assert len(list(journal.glob("*.online-preflight.json"))) == 1
    assert len(list(journal.glob("*.session.json"))) == 1
    assert not list(journal.glob(".*.capture-lease"))

    verified = session.verify_field_covariance_session_record(result["record_path"])
    assert verified["record_sha256"] == result["record_sha256"]


def test_old_session_record_remains_valid_after_later_leaf_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _, config = _install_runtime(tmp_path, monkeypatch)
    _, result = session.run_next_field_covariance_session(
        receipt,
        tmp_path / "journal",
        attempt_id="first-leaf",
    )
    second = config.leaves[1].report_path
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text('{"covariance":true}', encoding="utf-8")

    assert session.verify_field_covariance_session_record(result["record_path"])[
        "gate_passed"
    ] is True


def test_session_verifier_rejects_record_receipt_probe_and_report_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _, _ = _install_runtime(tmp_path, monkeypatch)
    _, result = session.run_next_field_covariance_session(
        receipt,
        tmp_path / "journal",
    )
    record_path = Path(result["record_path"])
    original_record = record_path.read_bytes()
    record = json.loads(original_record)

    changed = dict(record)
    changed["created_at_utc"] = "2026-09-06T10:00:00Z"
    record_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(session.FieldCovarianceSessionError, match="record digest"):
        session.verify_field_covariance_session_record(record_path)

    record_path.write_bytes(original_record)
    receipt.write_text("changed", encoding="utf-8")
    with pytest.raises(session.FieldCovarianceSessionError, match="receipt file changed"):
        session.verify_field_covariance_session_record(record_path)

    receipt.write_text('{"receipt":true}', encoding="utf-8")
    probe = Path(record["online_preflight"]["path"])
    probe.write_text("changed", encoding="utf-8")
    with pytest.raises(session.FieldCovarianceSessionError, match="preflight changed"):
        session.verify_field_covariance_session_record(record_path)

    probe.write_text('{"online":true}', encoding="utf-8")
    report = Path(record["covariance_report"]["path"])
    report.write_text("changed", encoding="utf-8")
    with pytest.raises(session.FieldCovarianceSessionError, match="report changed"):
        session.verify_field_covariance_session_record(record_path)


def test_failed_capture_retains_probe_but_writes_no_success_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _, _ = _install_runtime(tmp_path, monkeypatch)

    def fail_capture(current: object, **kwargs: object) -> tuple[int, dict[str, object]]:
        del current
        probe = kwargs["probe_report_out"]
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text('{"gate":false}', encoding="utf-8")
        return 1, {"capture_started": False, "capture_verified": False}

    monkeypatch.setattr(session, "capture_next", fail_capture)
    exit_code, result = session.run_next_field_covariance_session(
        receipt,
        tmp_path / "journal",
        attempt_id="blocked",
    )

    assert exit_code == 1
    assert result["gate_passed"] is False
    assert result["record_written"] is False
    assert len(list((tmp_path / "journal").glob("*.online-preflight.json"))) == 1
    assert not list((tmp_path / "journal").glob("*.session.json"))
    assert not list((tmp_path / "journal").glob(".*.capture-lease"))


def test_capture_callback_runs_under_exclusive_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _, config = _install_runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    original_capture = session.capture_next

    def capture_under_lease(
        current: object, **kwargs: object
    ) -> tuple[int, dict[str, object]]:
        status = journal.capture_lease_status(receipt, journal_path)
        assert status["state"] == "held"
        with pytest.raises(journal.FieldCovarianceJournalError, match="already held"):
            journal.acquire_capture_lease(
                receipt,
                journal_path,
                leaf_ordinal=1,
                runtime_mode="hil",
                vehicle_class="fixed-wing",
            )
        return original_capture(current, **kwargs)

    monkeypatch.setattr(session, "capture_next", capture_under_lease)
    exit_code, result = session.run_next_field_covariance_session(
        receipt,
        journal_path,
        attempt_id="exclusive",
    )

    assert exit_code == 0
    assert result["campaign"]["leaf_ordinal"] == 1
    assert journal.capture_lease_status(receipt, journal_path)["state"] == "absent"
    assert config.leaves[0].report_path.is_file()


def test_session_rejects_journal_inside_workspace_and_unsafe_attempt_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, workspace, _ = _install_runtime(tmp_path, monkeypatch)
    with pytest.raises(session.FieldCovarianceSessionError, match="outside workspace"):
        session.run_next_field_covariance_session(
            receipt,
            workspace / "journal",
            attempt_id="inside",
        )
    with pytest.raises(session.FieldCovarianceSessionError, match="safe file-name"):
        session.run_next_field_covariance_session(
            receipt,
            tmp_path / "journal",
            attempt_id="../escape",
        )

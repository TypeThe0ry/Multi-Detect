from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect import field_covariance_journal as journal
from multidetect import field_covariance_session as session
from multidetect.compat import UTC
from multidetect.field_covariance_campaign import REQUIRED_COMBINATIONS


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, dict[str, object], object]:
    workspace = tmp_path / "received-workspace"
    workspace.mkdir()
    campaign_path = workspace / "campaign.json"
    campaign_path.write_text('{"campaign":true}', encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"receipt":true}', encoding="utf-8")
    receipt = {
        "receipt_path": str(receipt_path.resolve()),
        "destination_workspace_root": str(workspace.resolve()),
        "receipt_sha256": "a" * 64,
        "workspace_sha256": "b" * 64,
        "gate_passed": True,
    }
    config = SimpleNamespace(
        leaves=[SimpleNamespace(key=key) for key in REQUIRED_COMBINATIONS]
    )
    monkeypatch.setattr(
        journal,
        "verify_field_covariance_workspace_receipt",
        lambda path: dict(receipt),
    )
    monkeypatch.setattr(journal, "load_campaign_config", lambda path: config)
    return workspace, campaign_path, receipt_path, receipt, config


def _acquire(
    receipt_path: Path, journal_path: Path, *, lease_id: str = "1" * 32
) -> journal.CaptureLease:
    return journal.acquire_capture_lease(
        receipt_path,
        journal_path,
        leaf_ordinal=1,
        runtime_mode="hil",
        vehicle_class="fixed-wing",
        acquired_at=datetime(2026, 9, 6, 1, 2, 3, tzinfo=UTC),
        lease_id=lease_id,
    )


def _validate_schema(document: dict[str, object]) -> None:
    schema = json.loads(journal.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def test_capture_lease_is_atomic_visible_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt_path, _, _ = _runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    lease = _acquire(receipt_path, journal_path)
    _validate_schema(lease.document)
    assert (lease.directory / journal.LEASE_FILE).is_file()
    assert not list((journal_path / journal.CANDIDATES_DIR).iterdir())
    status = journal.capture_lease_status(receipt_path, journal_path)
    assert status["state"] == "held"
    assert status["lease"]["lease_id"] == "1" * 32
    assert status["owner_process_state"] == "alive"
    with pytest.raises(journal.FieldCovarianceJournalError, match="already held"):
        _acquire(receipt_path, journal_path, lease_id="2" * 32)
    lease.release()
    assert journal.capture_lease_status(receipt_path, journal_path)["state"] == "absent"
    assert lease.released is True


def test_incomplete_candidate_is_visible_but_never_blocks_active_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt_path, _, _ = _runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    stale = journal_path / journal.CANDIDATES_DIR / ("f" * 32)
    stale.mkdir(parents=True)

    lease = _acquire(receipt_path, journal_path)
    status = journal.capture_lease_status(receipt_path, journal_path)

    assert status["state"] == "held"
    assert status["nonblocking_candidate_count"] == 1
    assert lease.directory.is_dir()
    lease.release()


def test_release_atomically_unblocks_and_preserves_released_lease_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt_path, _, _ = _runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    lease = _acquire(receipt_path, journal_path)
    lease.release()

    assert lease.released is True
    assert not lease.directory.exists()
    assert lease.retained_directory is not None
    assert (lease.retained_directory / journal.LEASE_FILE).is_file()
    replacement = _acquire(receipt_path, journal_path, lease_id="2" * 32)
    assert replacement.directory.is_dir()
    replacement.release()


def test_capture_lease_refuses_tampered_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt_path, _, _ = _runtime(tmp_path, monkeypatch)
    lease = _acquire(receipt_path, tmp_path / "journal")
    lease_path = lease.directory / journal.LEASE_FILE
    document = json.loads(lease_path.read_text(encoding="utf-8"))
    document["lease_id"] = "2" * 32
    lease_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(journal.FieldCovarianceJournalError):
        lease.release()
    assert lease.directory.is_dir()


def test_recovery_requires_exact_dead_owner_and_preserves_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt_path, _, _ = _runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    lease = _acquire(receipt_path, journal_path)
    with pytest.raises(journal.FieldCovarianceJournalError, match="does not match"):
        journal.recover_dead_capture_lease(
            receipt_path,
            journal_path,
            expected_lease_id="2" * 32,
            reason="wrong lease",
        )
    with pytest.raises(journal.FieldCovarianceJournalError, match="not proven dead"):
        journal.recover_dead_capture_lease(
            receipt_path,
            journal_path,
            expected_lease_id="1" * 32,
            reason="owner is alive",
        )
    monkeypatch.setattr(journal, "_owner_process_state", lambda owner: "dead")
    recovered = journal.recover_dead_capture_lease(
        receipt_path,
        journal_path,
        expected_lease_id="1" * 32,
        reason="verified operator recovery",
        recovered_at=datetime(2026, 9, 6, 2, 3, 4, tzinfo=UTC),
    )
    archive = Path(recovered["archive_directory"])
    assert archive.is_dir()
    assert (archive / journal.LEASE_FILE).is_file()
    recovery_path = archive / journal.RECOVERY_FILE
    assert recovery_path.is_file()
    _validate_schema(json.loads(recovery_path.read_text(encoding="utf-8")))
    assert recovered["lease_file_sha256"] == _sha(archive / journal.LEASE_FILE)
    assert not lease.directory.exists()
    status = journal.capture_lease_status(receipt_path, journal_path)
    assert status["recovered_lease_count"] == 1


def test_interrupted_recovery_is_resumable_only_with_same_exact_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, receipt_path, _, _ = _runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    lease = _acquire(receipt_path, journal_path)
    monkeypatch.setattr(journal, "_owner_process_state", lambda owner: "dead")
    real_rename = journal.os.rename
    interrupted = False

    def interrupt_once(source: object, destination: object) -> None:
        nonlocal interrupted
        if not interrupted and Path(source) == lease.directory:
            interrupted = True
            raise OSError("simulated power interruption before recovery publish")
        real_rename(source, destination)

    monkeypatch.setattr(journal.os, "rename", interrupt_once)
    with pytest.raises(OSError, match="simulated power interruption"):
        journal.recover_dead_capture_lease(
            receipt_path,
            journal_path,
            expected_lease_id="1" * 32,
            reason="same exact recovery",
            recovered_at=datetime(2026, 9, 6, 2, 3, 4, tzinfo=UTC),
        )
    assert (lease.directory / journal.RECOVERY_FILE).is_file()
    with pytest.raises(journal.FieldCovarianceJournalError, match="exact retry"):
        journal.recover_dead_capture_lease(
            receipt_path,
            journal_path,
            expected_lease_id="1" * 32,
            reason="different recovery",
        )

    recovered = journal.recover_dead_capture_lease(
        receipt_path,
        journal_path,
        expected_lease_id="1" * 32,
        reason="same exact recovery",
    )
    archive = Path(recovered["archive_directory"])
    assert archive.is_dir()
    assert (archive / journal.LEASE_FILE).is_file()
    assert (archive / journal.RECOVERY_FILE).is_file()
    assert not lease.directory.exists()


def _session_record(
    path: Path,
    probe_path: Path,
    receipt: dict[str, object],
    ordinal: int,
    capture_lease: dict[str, object],
) -> dict[str, object]:
    mode, vehicle = REQUIRED_COMBINATIONS[ordinal - 1]
    return {
        "receipt": {
            "receipt_sha256": receipt["receipt_sha256"],
            "workspace_sha256": receipt["workspace_sha256"],
        },
        "campaign": {
            "leaf_ordinal": ordinal,
            "runtime_mode": mode,
            "vehicle_class": vehicle,
        },
        "online_preflight": {"path": str(probe_path.resolve())},
        "capture_lease": capture_lease,
        "record_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
    }


def _install_index_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
) -> tuple[Path, Path, dict[str, object], list[dict[str, object]], dict[str, object]]:
    _, campaign_path, receipt_path, receipt, config = _runtime(tmp_path, monkeypatch)
    journal_path = tmp_path / "journal"
    journal_path.mkdir()
    released_parent = journal_path / journal.RELEASED_DIR
    released_parent.mkdir()
    records: list[dict[str, object]] = []
    by_path: dict[Path, dict[str, object]] = {}
    for ordinal in range(1, count + 1):
        mode, vehicle = REQUIRED_COMBINATIONS[ordinal - 1]
        record_path = journal_path / f"{ordinal:02d}-{mode}-{vehicle}.session.json"
        probe_path = journal_path / f"{ordinal:02d}-{mode}-{vehicle}.online-preflight.json"
        record_path.write_text("{}", encoding="utf-8")
        probe_path.write_text("{}", encoding="utf-8")
        lease_id = f"{ordinal:032x}"
        lease = journal._with_digest(
            {
                "schema": journal.LEASE_SCHEMA,
                "schema_version": journal.SCHEMA_VERSION,
                "lease_id": lease_id,
                "acquired_at_utc": "2026-09-06T01:02:03Z",
                "owner": {"hostname": "capture-host", "pid": ordinal},
                "receipt": {
                    "path": str(receipt_path.resolve()),
                    "file_sha256": _sha(receipt_path),
                    "receipt_sha256": receipt["receipt_sha256"],
                    "workspace_sha256": receipt["workspace_sha256"],
                },
                "campaign": {
                    "path": str(campaign_path.resolve()),
                    "file_sha256": _sha(campaign_path),
                    "leaf": {
                        "leaf_ordinal": ordinal,
                        "runtime_mode": mode,
                        "vehicle_class": vehicle,
                    },
                },
                "claims": {
                    "release_eligible": False,
                    "hardware_acceptance": False,
                    "flight_acceptance": False,
                },
            },
            "lease_sha256",
        )
        released = released_parent / lease_id
        released.mkdir()
        (released / journal.LEASE_FILE).write_text(json.dumps(lease), encoding="utf-8")
        verified = _session_record(record_path, probe_path, receipt, ordinal, lease)
        records.append(verified)
        by_path[record_path.resolve()] = verified
    blocked = journal_path / "blocked.online-preflight.json"
    blocked.write_text("{}", encoding="utf-8")
    status = {
        "records": [],
        "failed_count": 0,
        "passed_count": count,
        "pending_count": len(REQUIRED_COMBINATIONS) - count,
        "complete": count == len(REQUIRED_COMBINATIONS),
        "next_leaf": (
            None
            if count == len(REQUIRED_COMBINATIONS)
            else {
                "runtime_mode": REQUIRED_COMBINATIONS[count][0],
                "vehicle_class": REQUIRED_COMBINATIONS[count][1],
            }
        ),
    }
    monkeypatch.setattr(journal, "campaign_status", lambda current: dict(status))
    monkeypatch.setattr(
        session,
        "verify_field_covariance_session_record",
        lambda path: dict(by_path[Path(path).resolve()]),
    )
    return journal_path, receipt_path, receipt, records, status


def test_journal_index_reconciles_contiguous_prefix_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, records, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=2
    )
    index = journal.build_field_covariance_journal_index(
        receipt_path,
        journal_path,
        indexed_at=datetime(2026, 9, 6, 3, 4, 5, tzinfo=UTC),
    )
    _validate_schema(index)
    assert index["passed_count"] == 2
    assert index["blocked_probe_count"] == 1
    assert index["next_leaf"]["leaf_ordinal"] == 3
    assert [item["record_sha256"] for item in index["records"]] == [
        item["record_sha256"] for item in records
    ]
    assert all(item["lease_archive_kind"] == "released" for item in index["records"])
    assert all(item["recovery_path"] is None for item in index["records"])
    assert all(item["recovery_file_sha256"] is None for item in index["records"])
    output = tmp_path / "journal-index.json"
    written = journal.write_field_covariance_journal_index(
        receipt_path, journal_path, output
    )
    assert written["index_path"] == str(output.resolve())
    verified = journal.verify_field_covariance_journal_index(output)
    assert verified["gate_passed"] is True


def test_journal_index_rejects_gap_and_campaign_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, _, status = _install_index_runtime(
        tmp_path, monkeypatch, count=2
    )
    first = next(journal_path.glob("01-*.session.json"))
    first.unlink()
    with pytest.raises(journal.FieldCovarianceJournalError, match="contiguous prefix"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)
    first.write_text("{}", encoding="utf-8")
    status["passed_count"] = 3
    status["pending_count"] = 3
    monkeypatch.setattr(journal, "campaign_status", lambda current: dict(status))
    with pytest.raises(journal.FieldCovarianceJournalError, match="count does not match"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)


def test_journal_index_requires_preserved_released_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, _, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=1
    )
    released = journal_path / journal.RELEASED_DIR / f"{1:032x}"
    (released / journal.LEASE_FILE).unlink()
    released.rmdir()

    with pytest.raises(journal.FieldCovarianceJournalError, match="exactly one"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)


def test_journal_index_accepts_recovered_lease_after_session_commit_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, _, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=1
    )
    lease_id = f"{1:032x}"
    released = journal_path / journal.RELEASED_DIR / lease_id
    active = journal._lease_directory(journal_path, "b" * 64)
    released.rename(active)

    with pytest.raises(journal.FieldCovarianceJournalError, match="exactly one"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)

    monkeypatch.setattr(journal, "_owner_process_state", lambda owner: "dead")
    recovered = journal.recover_dead_capture_lease(
        receipt_path,
        journal_path,
        expected_lease_id=lease_id,
        reason="owner died after session commit",
        recovered_at=datetime(2026, 9, 6, 4, 5, 6, tzinfo=UTC),
    )
    assert Path(recovered["archive_directory"]).parent.name == journal.RECOVERED_DIR

    index = journal.build_field_covariance_journal_index(receipt_path, journal_path)
    _validate_schema(index)
    record = index["records"][0]
    assert record["lease_archive_kind"] == "recovered"
    assert Path(record["lease_path"]).is_file()
    assert Path(record["recovery_path"]).is_file()
    assert record["recovery_file_sha256"] == _sha(Path(record["recovery_path"]))

    output = tmp_path / "recovered-journal-index.json"
    journal.write_field_covariance_journal_index(receipt_path, journal_path, output)
    assert journal.verify_field_covariance_journal_index(output)["gate_passed"] is True


def test_journal_index_rejects_ambiguous_released_and_recovered_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, _, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=1
    )
    lease_id = f"{1:032x}"
    released = journal_path / journal.RELEASED_DIR / lease_id
    recovered = journal_path / journal.RECOVERED_DIR / lease_id
    recovered.parent.mkdir()
    shutil.copytree(released, recovered)

    with pytest.raises(journal.FieldCovarianceJournalError, match="exactly one"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)


def test_journal_index_rejects_tampered_recovery_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, _, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=1
    )
    lease_id = f"{1:032x}"
    released = journal_path / journal.RELEASED_DIR / lease_id
    active = journal._lease_directory(journal_path, "b" * 64)
    released.rename(active)
    monkeypatch.setattr(journal, "_owner_process_state", lambda owner: "dead")
    recovered = journal.recover_dead_capture_lease(
        receipt_path,
        journal_path,
        expected_lease_id=lease_id,
        reason="owner died after session commit",
    )
    recovery_path = Path(recovered["archive_directory"]) / journal.RECOVERY_FILE
    document = json.loads(recovery_path.read_text(encoding="utf-8"))
    document["reason"] = "tampered recovery reason"
    recovery_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(journal.FieldCovarianceJournalError, match="digest mismatch"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_id", "2" * 32),
        ("lease_file_sha256", "d" * 64),
        ("receipt_sha256", "c" * 64),
    ],
)
def test_journal_index_rejects_redigested_mismatched_recovery_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    journal_path, receipt_path, _, _, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=1
    )
    lease_id = f"{1:032x}"
    released = journal_path / journal.RELEASED_DIR / lease_id
    active = journal._lease_directory(journal_path, "b" * 64)
    released.rename(active)
    monkeypatch.setattr(journal, "_owner_process_state", lambda owner: "dead")
    recovered = journal.recover_dead_capture_lease(
        receipt_path,
        journal_path,
        expected_lease_id=lease_id,
        reason="owner died after session commit",
    )
    recovery_path = Path(recovered["archive_directory"]) / journal.RECOVERY_FILE
    document = json.loads(recovery_path.read_text(encoding="utf-8"))
    if field == "receipt_sha256":
        document["receipt"][field] = value
    else:
        document[field] = value
    unsigned = dict(document)
    unsigned.pop("recovery_sha256")
    document["recovery_sha256"] = journal._canonical_sha256(unsigned)
    recovery_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(journal.FieldCovarianceJournalError, match="does not match"):
        journal.build_field_covariance_journal_index(receipt_path, journal_path)


def test_journal_index_rejects_snapshot_tamper_and_campaign_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, _, _, status = _install_index_runtime(
        tmp_path, monkeypatch, count=2
    )
    output = tmp_path / "journal-index.json"
    journal.write_field_covariance_journal_index(receipt_path, journal_path, output)
    document = json.loads(output.read_text(encoding="utf-8"))
    document["pending_count"] = 3
    without_digest = dict(document)
    without_digest.pop("index_sha256")
    document["index_sha256"] = journal._canonical_sha256(without_digest)
    output.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(journal.FieldCovarianceJournalError, match="progression"):
        journal.verify_field_covariance_journal_index(output)

    output.unlink()
    journal.write_field_covariance_journal_index(receipt_path, journal_path, output)
    status["passed_count"] = 1
    monkeypatch.setattr(journal, "campaign_status", lambda current: dict(status))
    with pytest.raises(journal.FieldCovarianceJournalError, match="regressed"):
        journal.verify_field_covariance_journal_index(output)


def test_journal_index_cannot_be_written_inside_received_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_path, receipt_path, receipt, _, _ = _install_index_runtime(
        tmp_path, monkeypatch, count=0
    )
    output = Path(str(receipt["destination_workspace_root"])) / "index.json"
    with pytest.raises(journal.FieldCovarianceJournalError, match="outside workspace"):
        journal.write_field_covariance_journal_index(receipt_path, journal_path, output)

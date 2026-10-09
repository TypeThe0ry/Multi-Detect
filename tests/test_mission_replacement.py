from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.mission_replacement import (
    MissionReplacementBinding,
    MissionReplacementError,
    MissionReplacementJournal,
    MissionReplacementState,
    MissionReplacementTransaction,
)


def _binding() -> MissionReplacementBinding:
    return MissionReplacementBinding(
        replacement_id="replacement-1",
        parent_transaction_id="parent-transaction",
        parent_mission_sha256="a" * 64,
        event_sha256="b" * 64,
        context_sha256="c" * 64,
        new_mission_sha256="d" * 64,
        new_protocol_sha256="e" * 64,
        airframe_profile_sha256="f" * 64,
        parent_journal_sha256="1" * 64,
        observed_current_sequence=2,
        resume_sequence=3,
        source_system=1,
        source_component=1,
        reason="target moved",
    )


class _Transport:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        bad_cursor: bool = False,
        cursor_command: str = "MAV_CMD_NAV_WAYPOINT",
    ) -> None:
        self.fail_at = fail_at
        self.bad_cursor = bad_cursor
        self.cursor_command = cursor_command
        self.calls: list[str] = []

    def _ok(self, name: str) -> dict[str, object]:
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"{name} failed")
        return {"confirmed": True, "step": name}

    def request_pause(self, binding):
        return self._ok("request_pause")

    def confirm_paused(self, binding):
        return self._ok("confirm_paused")

    def upload_replacement(self, binding):
        return self._ok("upload_replacement")

    def verify_readback(self, binding):
        return self._ok("verify_readback")

    def commit_replacement(self, binding):
        return self._ok("commit_replacement")

    def request_resume_cursor(self, binding):
        return self._ok("request_resume_cursor")

    def confirm_resume_cursor(self, binding):
        result = self._ok("confirm_resume_cursor")
        result.update(
            {
                "sequence": binding.resume_sequence - (1 if self.bad_cursor else 0),
                "mission_sha256": binding.new_mission_sha256,
                "source_system": binding.source_system,
                "source_component": binding.source_component,
                "mission_item_command": self.cursor_command,
            }
        )
        return result

    def request_resume(self, binding):
        return self._ok("request_resume")

    def confirm_resumed(self, binding):
        result = self._ok("confirm_resumed")
        result.update(
            {
                "mission_sha256": binding.new_mission_sha256,
                "source_system": binding.source_system,
                "source_component": binding.source_component,
                "auto_mission": True,
            }
        )
        return result

    def fallback(self, binding, mode):
        result = self._ok(f"fallback_{mode}")
        result.update(
            {
                "source_system": binding.source_system,
                "source_component": binding.source_component,
            }
        )
        return result


def test_replacement_requires_parent_permit_and_valid_handoff() -> None:
    with pytest.raises(MissionReplacementError, match="start_permitted"):
        MissionReplacementTransaction(
            _binding(), parent_state="committed", transport=_Transport()
        )
    with pytest.raises(ValueError, match="takeoff"):
        replace(_binding(), resume_sequence=0)


def test_replacement_happy_path_is_hash_bound_and_resumes_after_cursor_proof(
    tmp_path: Path,
) -> None:
    journal = MissionReplacementJournal(tmp_path / "replacement.jsonl")
    transport = _Transport()
    transaction = MissionReplacementTransaction(
        _binding(),
        parent_state="start_permitted",
        transport=transport,
        journal=journal,
    )

    receipt = transaction.execute()

    assert receipt["state"] == MissionReplacementState.RESUMED.value
    assert receipt["binding_sha256"] == _binding().canonical_hash
    assert [record.state for record in journal.records] == [
        MissionReplacementState.AUTHORIZED,
        MissionReplacementState.PAUSE_REQUESTED,
        MissionReplacementState.PAUSED,
        MissionReplacementState.UPLOADING,
        MissionReplacementState.UPLOAD_CONFIRMED,
        MissionReplacementState.READBACK_VERIFIED,
        MissionReplacementState.COMMIT_REQUESTED,
        MissionReplacementState.COMMITTED,
        MissionReplacementState.CURSOR_REQUESTED,
        MissionReplacementState.CURSOR_CONFIRMED,
        MissionReplacementState.RESUME_REQUESTED,
        MissionReplacementState.RESUMED,
    ]
    assert "fallback_return" not in transport.calls
    reloaded = MissionReplacementJournal(tmp_path / "replacement.jsonl")
    assert reloaded.state is MissionReplacementState.RESUMED
    assert reloaded.records[-1].record_sha256 == journal.records[-1].record_sha256


def test_replacement_failure_falls_back_without_claiming_resume() -> None:
    transport = _Transport(fail_at="verify_readback")
    transaction = MissionReplacementTransaction(
        _binding(),
        parent_state="start_permitted",
        transport=transport,
        fallback_mode="hold",
    )

    receipt = transaction.execute()

    assert receipt["state"] == MissionReplacementState.HOLD_REQUIRED.value
    assert transaction.terminal is True
    assert "fallback_hold" in transport.calls
    assert "request_resume" not in transport.calls


def test_replacement_cursor_mismatch_is_fail_closed() -> None:
    transport = _Transport(bad_cursor=True)
    transaction = MissionReplacementTransaction(
        _binding(),
        parent_state="start_permitted",
        transport=transport,
    )

    receipt = transaction.execute()

    assert receipt["state"] == MissionReplacementState.RETURN_REQUIRED.value
    assert "fallback_return" in transport.calls
    assert "request_resume" not in transport.calls


def test_replacement_journal_detects_hash_tampering(tmp_path: Path) -> None:
    path = tmp_path / "replacement.jsonl"
    journal = MissionReplacementJournal(path)
    journal.append(
        MissionReplacementState.AUTHORIZED,
        "replacement_authorized",
        {"confirmed": True},
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["confirmed"] = False
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(MissionReplacementError, match="corrupt"):
        MissionReplacementJournal(path)


def test_replacement_recovery_is_descriptive_and_never_replays_commands() -> None:
    journal = MissionReplacementJournal(binding=_binding())
    journal.append(
        MissionReplacementState.AUTHORIZED,
        "replacement_authorized",
        {"binding": _binding().to_mapping(), "binding_sha256": _binding().canonical_hash},
    )
    journal.append(
        MissionReplacementState.PAUSE_REQUESTED,
        "pause_requested",
        {"intent": True, "binding_sha256": _binding().canonical_hash},
    )
    transport = _Transport()
    transaction = MissionReplacementTransaction(
        _binding(),
        parent_state="start_permitted",
        transport=transport,
        journal=journal,
    )

    recovery = transaction.recover()

    assert recovery["recovery_required"] is True
    assert recovery["next_action"] == "confirm_paused_or_fallback"
    assert recovery["transport_called"] is False
    assert transport.calls == []
    with pytest.raises(MissionReplacementError, match="already started"):
        transaction.execute()


def test_replacement_partial_tail_is_marked_for_recovery(tmp_path: Path) -> None:
    path = tmp_path / "replacement.jsonl"
    journal = MissionReplacementJournal(path)
    journal.append(
        MissionReplacementState.AUTHORIZED,
        "replacement_authorized",
        {"confirmed": True},
    )
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"partial"')

    reloaded = MissionReplacementJournal(path)

    assert reloaded.recovery_required is True
    assert reloaded.records[-1].state is MissionReplacementState.AUTHORIZED
    assert "incomplete" in (reloaded.recovery_reason or "")


def test_replacement_forbidden_cursor_falls_back() -> None:
    transaction = MissionReplacementTransaction(
        _binding(),
        parent_state="start_permitted",
        transport=_Transport(cursor_command="MAV_CMD_NAV_LAND"),
    )

    receipt = transaction.execute()

    assert receipt["state"] == MissionReplacementState.RETURN_REQUIRED.value
    assert "request_resume" not in transaction.transport.calls


def test_replacement_receipt_matches_checked_in_schema() -> None:
    transaction = MissionReplacementTransaction(
        _binding(),
        parent_state="start_permitted",
        transport=_Transport(),
    )
    receipt = transaction.execute()
    schema = json.loads(
        Path("configs/schemas/mission-replacement-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(receipt)

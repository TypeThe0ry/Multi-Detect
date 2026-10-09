from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_onboard_continuation_binding import _journal, _proof_for
from test_onboard_realtime_bridge import _bridge, _context
from test_onboard_vlm_admission import _anchor, _clearance, _current

from multidetect.autonomy import onboard_continuation_authoring as authoring
from multidetect.autonomy.onboard_continuation_admission import (
    ContinuationAuthoringEvidence,
    admit_anchored_continuation,
    prepare_anchored_continuation,
)
from multidetect.autonomy.onboard_continuation_authoring import (
    OriginalHostAuthoringRecord,
    SealedOriginalHostAuthoring,
    begin_original_host_authoring,
    seal_original_host_authoring,
)
from multidetect.autonomy.onboard_multimodal import OnboardMultimodalScope
from multidetect.autonomy.onboard_vlm_admission import ExecutionAdmissionPolicy
from multidetect.autonomy.serialization import canonical_json, canonical_sha256
from multidetect.clock import ManualClock
from multidetect.mission_journal import MissionTransactionState

SOURCE = "fixture-ordinary-host-authoring"


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _inputs(tmp_path):
    bridge, _, candidates, runtimes = _bridge()
    executor = bridge.event_loop.adapter.executor
    context = _context(1.0)
    task = candidates[0].task
    scope = OnboardMultimodalScope(
        session_id=str(uuid4()),
        prompt_sha256=executor.prompt.canonical_hash,
        aircraft_id=executor.prompt.aircraft_id,
        selected_task_id=task.task_id,
        clock_epoch="fixture-host-epoch",
        airframe_profile_id=context.airframe_profile_id,
        airframe_profile_sha256=context.airframe_profile_hash,
        vehicle_class="multirotor",
        home_sha256=context.home_position.canonical_hash,
    )
    return dict(
        task=task,
        context=context,
        runtime=runtimes[task.task_id],
        scope=scope,
        policy_artifact=executor.shared_policy.artifact,
        clock=ManualClock(1.01),
        clock_epoch=scope.clock_epoch,
        source_id=SOURCE,
        mission_id="fixture-authored-parent",
        output_dir=tmp_path,
        record_name="original-authoring.json",
    )


def _begin(tmp_path, **changes):
    values = _inputs(tmp_path)
    values.update(changes)
    return begin_original_host_authoring(**values), values


def _binding(tmp_path, record, values, **changes):
    directory = tmp_path / "transaction"
    directory.mkdir()
    binding = _journal(
        directory,
        record.compilation.compiled_mission.executable_mission,
        values["task"],
        values["context"],
        values["scope"],
        runtime=values["runtime"],
        **changes,
    )
    return replace(binding, bound_at_s=1.1)


def _sealed(tmp_path):
    record, values = _begin(tmp_path)
    binding = _binding(tmp_path, record, values)
    values["clock"].advance_to(1.2)
    result = seal_original_host_authoring(
        record,
        binding,
        clock=values["clock"],
        clock_epoch=values["clock_epoch"],
        output_dir=tmp_path,
        seal_name="sealed-authoring.json",
    )
    return result, record, binding, values


def _raw_record(tmp_path, record, transform):
    raw = record.to_mapping()
    transform(raw)
    payload = canonical_json(raw).encode()
    path = tmp_path / "modified-begin.json"
    path.write_bytes(payload)
    return OriginalHostAuthoringRecord(path, payload, _sha(payload))


def _raw_seal(tmp_path, result, transform):
    raw = result.to_mapping()
    transform(raw)
    payload = canonical_json(raw).encode()
    path = tmp_path / "modified-seal.json"
    path.write_bytes(payload)
    return SealedOriginalHostAuthoring(path, payload, _sha(payload))


def _encoded(payload):
    return {
        "bytes": len(payload),
        "sha256": _sha(payload),
        "original_bytes_base64": base64.b64encode(payload).decode(),
    }


def test_begin_compiles_original_typed_inputs_before_transaction_and_detaches_runtime(tmp_path):
    values = _inputs(tmp_path)
    record = begin_original_host_authoring(**values)
    raw = record.to_mapping()
    assert raw["observed_at_s"] == raw["compilation_completed_at_s"] == 1.01
    assert raw["task"] == values["task"].to_mapping()
    assert raw["context"] == values["context"].to_mapping()
    assert raw["runtime"] == values["runtime"]
    assert raw["policy_artifact"] == values["policy_artifact"].to_mapping()
    assert record.compilation.plan.context_snapshot_hash == values["context"].canonical_hash
    assert all(value is False for value in raw["boundary"].values())
    assert all(
        Path(row["path"]).stat().st_size == row["bytes"]
        and _sha(Path(row["path"]).read_bytes()) == row["sha256"]
        for row in raw["source_files"]
    )
    values["runtime"]["safety_policy"]["geofence_radius_m"] = 1.0
    assert record.to_mapping()["runtime"] == raw["runtime"]
    assert record.path.read_bytes() == record.source_receipt_bytes
    assert (
        OriginalHostAuthoringRecord.from_path(
            record.path, expected_sha256=record.source_receipt_sha256
        )
        == record
    )


def test_seal_preserves_begin_time_original_context_journal_and_default_empty_trust(tmp_path):
    sealed, record, binding, values = _sealed(tmp_path)
    raw = sealed.to_mapping()
    declaration = sealed.evidence.to_mapping()
    assert raw["sealed_at_s"] == 1.2
    assert declaration["observed_at_s"] == 1.01  # Never replaced with 1.2 or binding time.
    assert declaration["journal_context_sha256"] == values["context"].canonical_hash
    assert declaration["runtime_sha256"] == canonical_sha256(values["runtime"])
    assert declaration["policy_artifact_sha256"] == values["policy_artifact"].canonical_hash
    assert declaration["parent_transaction_id"] == raw["journal_identity"]["transaction_id"]
    assert declaration["parent_mission_sha256"] == binding.mission.canonical_hash
    assert declaration["parent_wire_sha256"] == record.compilation.wire_hash
    assert (
        base64.b64decode(raw["begin_original"]["original_bytes_base64"])
        == record.source_receipt_bytes
    )
    assert (
        base64.b64decode(raw["journal_original"]["original_bytes_base64"])
        == binding.journal_path.read_bytes()
    )
    assert sealed.evidence.trusted_source_ids == ()
    assert all(value is False for value in raw["boundary"].values())
    assert sealed.canonical_hash == canonical_sha256(raw)
    assert (
        SealedOriginalHostAuthoring.from_path(
            sealed.path, expected_sha256=sealed.source_receipt_sha256
        )
        == sealed
    )


@pytest.mark.parametrize("at_s", [0.99, float("nan"), float("inf"), True])
def test_begin_rejects_future_context_nonfinite_or_bool_clock(tmp_path, at_s):
    class BadClock:
        def monotonic(self):
            return at_s

        def utc_now(self):
            raise AssertionError("UTC must not be mixed into host epoch")

    with pytest.raises(ValueError):
        _begin(tmp_path, clock=BadClock())
    assert not tuple(tmp_path.iterdir())


def test_clock_epoch_is_explicit_and_never_guessed_from_utc_or_session(tmp_path):
    with pytest.raises(ValueError, match="epoch"):
        _begin(tmp_path, clock_epoch="other")
    assert not tuple(tmp_path.iterdir())


def test_compile_clock_regression_does_not_publish(tmp_path):
    class RegressingClock:
        def __init__(self):
            self.values = iter([1.02, 1.01])

        def monotonic(self):
            return next(self.values)

        def utc_now(self):
            raise AssertionError("not required")

    with pytest.raises(ValueError, match="regressed"):
        _begin(tmp_path, clock=RegressingClock())
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["task", "profile", "home", "unknown", "artifact"])
def test_original_typed_identity_must_agree_before_publishing(tmp_path, change):
    values = _inputs(tmp_path)
    if change == "task":
        values["task"] = replace(values["task"], task_id="other")
    elif change == "artifact":
        values["policy_artifact"] = replace(
            values["policy_artifact"], airframe_profile_hash="a" * 64
        )
    elif change == "unknown":
        values["scope"] = replace(values["scope"], home_sha256=None)
    else:
        values["scope"] = replace(
            values["scope"],
            **{
                "home_sha256" if change == "home" else "airframe_profile_sha256": "a" * 64,
            },
        )
    with pytest.raises(ValueError):
        begin_original_host_authoring(**values)
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    "changes",
    [
        {"bound_at_s": 1.0},
        {"bound_at_s": 1.3},
        {"clock_epoch": "other"},
        {"vehicle_identity_sha256": "a" * 64},
        {"journal_sha256": "a" * 64},
    ],
)
def test_seal_exact_binding_epoch_vehicle_hash_and_begin_bound_now_order(tmp_path, changes):
    record, values = _begin(tmp_path)
    binding = replace(_binding(tmp_path, record, values), **changes)
    values["clock"].advance_to(1.2)
    with pytest.raises(ValueError):
        seal_original_host_authoring(
            record,
            binding,
            clock=values["clock"],
            clock_epoch=values["clock_epoch"],
            output_dir=tmp_path,
            seal_name="refused.json",
        )
    assert not (tmp_path / "refused.json").exists()


@pytest.mark.parametrize(
    "state", [MissionTransactionState.AUTHORIZED, MissionTransactionState.COMMITTED]
)
def test_original_start_permit_required_not_install_or_caller_bool(tmp_path, state):
    record, values = _begin(tmp_path)
    binding = _binding(tmp_path, record, values, state=state)
    values["clock"].advance_to(1.2)
    with pytest.raises(ValueError, match="START_PERMITTED"):
        seal_original_host_authoring(
            record,
            binding,
            clock=values["clock"],
            clock_epoch=values["clock_epoch"],
            output_dir=tmp_path,
            seal_name="refused.json",
        )


@pytest.mark.parametrize(
    "field",
    ["prompt_hash", "context_snapshot_hash", "plan_hash", "geofence_hash", "task_spec_hash"],
)
def test_real_journal_identity_closes_original_compile_context_and_full_runtime(tmp_path, field):
    record, values = _begin(tmp_path)
    binding = _binding(
        tmp_path,
        record,
        values,
        identity_changes=lambda identity: replace(
            identity, bindings=replace(identity.bindings, **{field: "a" * 64})
        ),
    )
    values["clock"].advance_to(1.2)
    with pytest.raises(ValueError, match="journal identity"):
        seal_original_host_authoring(
            record,
            binding,
            clock=values["clock"],
            clock_epoch=values["clock_epoch"],
            output_dir=tmp_path,
            seal_name="refused.json",
        )


def test_begin_and_journal_original_bytes_not_amended_at_seal(tmp_path):
    record, values = _begin(tmp_path)
    binding = _binding(tmp_path, record, values)
    before = (record.path.read_bytes(), binding.journal_path.read_bytes())
    values["clock"].advance_to(1.2)
    seal_original_host_authoring(
        record,
        binding,
        clock=values["clock"],
        clock_epoch=values["clock_epoch"],
        output_dir=tmp_path,
        seal_name="sealed.json",
    )
    assert before == (record.path.read_bytes(), binding.journal_path.read_bytes())


@pytest.mark.parametrize("which", ["record", "journal"])
def test_changed_original_source_rejected_without_publishing(tmp_path, which):
    record, values = _begin(tmp_path)
    binding = _binding(tmp_path, record, values)
    path = record.path if which == "record" else binding.journal_path
    path.write_bytes(path.read_bytes() + b"\n")
    values["clock"].advance_to(1.2)
    with pytest.raises(ValueError):
        seal_original_host_authoring(
            record,
            binding,
            clock=values["clock"],
            clock_epoch=values["clock_epoch"],
            output_dir=tmp_path,
            seal_name="refused.json",
        )
    assert not (tmp_path / "refused.json").exists()


@pytest.mark.parametrize(
    "field", ["context", "task", "runtime", "compilation", "source_files", "boundary"]
)
def test_reopened_begin_requires_canonical_original_outputs_and_source_snapshot(tmp_path, field):
    record, _ = _begin(tmp_path)

    def alter(raw):
        if field == "context":
            raw[field]["battery_remaining_pct"] = 80
        elif field == "task":
            raw[field]["target_classes"] = ["different_target"]
        elif field == "runtime":
            raw[field]["safety_policy"]["geofence_radius_m"] = 1
        elif field == "compilation":
            raw[field]["wire_hash"] = "a" * 64
        elif field == "source_files":
            raw[field][0]["sha256"] = "a" * 64
        else:
            raw[field]["execution_authority"] = 0

    with pytest.raises(ValueError):
        _raw_record(tmp_path, record, alter)


@pytest.mark.parametrize(
    "name",
    [
        "../outside.json",
        "..\\outside.json",
        "C:evil.json",
        "CON.json",
        "NUL",
        "trailing.",
        "trailing ",
        "bad\nname",
        ".",
        "",
    ],
)
def test_safe_create_only_filename_no_escape_or_windows_alias(tmp_path, name):
    with pytest.raises(ValueError):
        _begin(tmp_path, record_name=name)
    assert not tuple(tmp_path.iterdir())


def test_existing_user_output_never_overwritten_or_removed(tmp_path):
    protected = tmp_path / "original-authoring.json"
    protected.write_bytes(b"existing user evidence")
    with pytest.raises(FileExistsError):
        _begin(tmp_path)
    assert protected.read_bytes() == b"existing user evidence"
    assert list(tmp_path.iterdir()) == [protected]


def test_atomic_publish_race_keeps_winner_and_removes_only_own_temporary(tmp_path, monkeypatch):
    def concurrent_winner(source, destination):
        destination.write_bytes(b"other concurrent producer")
        raise FileExistsError("race")

    monkeypatch.setattr(authoring.os, "link", concurrent_winner)
    with pytest.raises(FileExistsError):
        _begin(tmp_path)
    assert (tmp_path / "original-authoring.json").read_bytes() == b"other concurrent producer"
    assert not tuple(tmp_path.glob(".authoring-*"))


def test_hardlink_unsupported_fails_no_unsafe_replace_fallback(tmp_path, monkeypatch):
    def unavailable(*args):
        raise OSError("atomic create-only hardlink unavailable")

    monkeypatch.setattr(authoring.os, "link", unavailable)
    with pytest.raises(OSError):
        _begin(tmp_path)
    assert not tuple(tmp_path.iterdir())


def test_published_file_is_complete_and_fsynced_before_atomic_visibility(tmp_path, monkeypatch):
    actual_link = os.link
    actual_fsync = os.fsync
    seen = []

    def fsync(descriptor):
        seen.append("fsync")
        return actual_fsync(descriptor)

    def link(source, destination):
        assert seen and not destination.exists()
        assert source.read_bytes().startswith(b"{") and source.read_bytes().endswith(b"}")
        seen.append("publish")
        return actual_link(source, destination)

    monkeypatch.setattr(authoring.os, "fsync", fsync)
    monkeypatch.setattr(authoring.os, "link", link)
    record, _ = _begin(tmp_path)
    assert record.path.exists() and seen.index("fsync") < seen.index("publish")
    assert not tuple(tmp_path.glob(".authoring-*"))


def test_source_drift_during_compile_is_rejected_before_publication(tmp_path, monkeypatch):
    original_snapshot = authoring._source_snapshot
    calls = 0

    def drift():
        nonlocal calls
        calls += 1
        value = original_snapshot()
        if calls > 1:
            value[0]["sha256"] = "a" * 64
        return value

    monkeypatch.setattr(authoring, "_source_snapshot", drift)
    with pytest.raises(ValueError, match="source drift"):
        _begin(tmp_path)
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("field", ["clock_epoch", "sealed_at_s", "journal_identity", "boundary"])
def test_seal_reopen_cannot_change_clock_journal_identity_or_authority(tmp_path, field):
    sealed, *_ = _sealed(tmp_path)

    def alter(raw):
        if field == "clock_epoch":
            raw[field] = "other"
        elif field == "sealed_at_s":
            raw[field] = 0
        elif field == "journal_identity":
            raw[field]["transaction_id"] = "other"
        else:
            raw[field]["authenticated_source_verified"] = True

    with pytest.raises(ValueError):
        _raw_seal(tmp_path, sealed, alter)


def test_existing_strict_evidence_uses_preserved_begin_not_generated_seal_time(tmp_path):
    sealed, *_ = _sealed(tmp_path)

    def alter(raw):
        payload = sealed.evidence.to_mapping()
        payload["observed_at_s"] = raw["sealed_at_s"]
        raw["evidence_original"] = _encoded(canonical_json(payload).encode())

    with pytest.raises(ValueError, match="begin lineage"):
        _raw_seal(tmp_path, sealed, alter)


def test_two_phase_producer_is_usable_by_existing_admission_only_with_external_trust(tmp_path):
    anchor = _anchor()
    bridge, *_ = _bridge()
    clock = ManualClock(anchor.source.context.captured_at_s)
    record = begin_original_host_authoring(
        anchor.task,
        anchor.source.context,
        anchor.runtime,
        anchor.source.scope,
        bridge.event_loop.adapter.executor.shared_policy.artifact,
        clock=clock,
        clock_epoch=anchor.source.scope.clock_epoch,
        source_id=SOURCE,
        mission_id="fixture-new-parent",
        output_dir=tmp_path,
        record_name="begin.json",
    )
    mission = record.compilation.compiled_mission.executable_mission
    anchor = replace(anchor, source=replace(anchor.source, mission_sha256=mission.canonical_hash))
    current = _current(anchor)
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    proof = _proof_for(
        transaction,
        current.context,
        current.scope,
        mission,
        journal_context=anchor.source.context,
        task=anchor.task,
        runtime=anchor.runtime,
    )
    clock.advance_to(2.2)
    sealed = seal_original_host_authoring(
        record,
        proof.mission_binding,
        clock=clock,
        clock_epoch=current.scope.clock_epoch,
        output_dir=tmp_path,
        seal_name="seal.json",
    )
    parent = proof.validate_binding(
        current_scope=current.scope, current_context=current.context, now_s=2.2
    )
    compiled = prepare_anchored_continuation(
        anchor, current, parent=parent, mission_id="child", now_s=2.2
    )
    current = replace(
        current,
        clearance=_clearance(
            current.context, current.event, anchor.target, wire_hash=compiled.wire_hash
        ),
    )
    policy = ExecutionAdmissionPolicy(trusted_clearance_source_ids=("fixture-segment-clearance",))
    with pytest.raises(ValueError, match="unknown/untrusted"):
        admit_anchored_continuation(
            anchor,
            current,
            parent_evidence=proof,
            authoring_evidence=sealed.evidence,
            mission_id="child",
            now_s=2.2,
            policy=policy,
        )
    # Explicit test-host allowlist simulates source registration, not producer authority.
    trusted = ContinuationAuthoringEvidence(
        sealed.evidence.source_receipt_bytes, sealed.evidence.source_receipt_sha256, (SOURCE,)
    )
    admitted = admit_anchored_continuation(
        anchor,
        current,
        parent_evidence=proof,
        authoring_evidence=trusted,
        mission_id="child",
        now_s=2.2,
        policy=policy,
    )
    assert admitted.compilation.compiled_mission.executable_mission.mission_id == "child"
    assert admitted.to_mapping()["execution_authority"] is False
    assert admitted.to_mapping()["authenticated_original_authoring_source_verified"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("bound_at_s", 1.0),
        ("bound_at_s", 1.3),
        ("clock_epoch", "other"),
        ("vehicle_identity_sha256", "a" * 64),
        ("task_sha256", "a" * 64),
        ("mission_sha256", "a" * 64),
    ],
)
def test_sealed_binding_time_and_identity_cannot_be_rewritten(tmp_path, field, value):
    sealed, *_ = _sealed(tmp_path)
    with pytest.raises(ValueError):
        _raw_seal(tmp_path, sealed, lambda raw: raw["mission_binding"].update({field: value}))


def test_host_clock_regression_during_seal_does_not_publish(tmp_path):
    record, values = _begin(tmp_path)
    binding = _binding(tmp_path, record, values)

    class RegressingClock:
        def __init__(self):
            self.values = iter([1.2, 1.19])

        def monotonic(self):
            return next(self.values)

        def utc_now(self):
            raise AssertionError("do not convert clocks")

    with pytest.raises(ValueError, match="regressed during validation"):
        seal_original_host_authoring(
            record,
            binding,
            clock=RegressingClock(),
            clock_epoch=values["clock_epoch"],
            output_dir=tmp_path,
            seal_name="refused.json",
        )
    assert not (tmp_path / "refused.json").exists()


def test_compile_end_must_precede_binding_not_only_begin(tmp_path):
    values = _inputs(tmp_path)

    class StepClock:
        def __init__(self):
            self.values = iter([1.01, 1.15])

        def monotonic(self):
            return next(self.values)

        def utc_now(self):
            raise AssertionError("host monotonic clock only")

    record = begin_original_host_authoring(**{**values, "clock": StepClock()})
    binding = _binding(tmp_path, record, values)
    with pytest.raises(ValueError, match="time differs"):
        seal_original_host_authoring(
            record,
            binding,
            clock=ManualClock(1.2),
            clock_epoch=values["clock_epoch"],
            output_dir=tmp_path,
            seal_name="refused.json",
        )


@pytest.mark.parametrize("field", ["policy_artifact", "runtime", "task", "context"])
def test_seal_embedded_original_begin_and_declared_identity_cross_join(tmp_path, field):
    sealed, *_ = _sealed(tmp_path)

    def alter(raw):
        begin = authoring.strict_json_loads(
            base64.b64decode(raw["begin_original"]["original_bytes_base64"])
        )
        if field == "policy_artifact":
            begin[field]["policy_version"] = "other"
        elif field == "runtime":
            begin[field]["safety_policy"]["geofence_radius_m"] = 1
        elif field == "task":
            begin[field]["target_classes"] = ["different"]
        else:
            begin[field]["battery_remaining_pct"] = 80
        raw["begin_original"] = _encoded(canonical_json(begin).encode())

    with pytest.raises(ValueError):
        _raw_seal(tmp_path, sealed, alter)


def test_seal_embedded_original_journal_cannot_change_even_if_blob_hash_updated(tmp_path):
    sealed, *_ = _sealed(tmp_path)

    def alter(raw):
        original = base64.b64decode(raw["journal_original"]["original_bytes_base64"])
        raw["journal_original"] = _encoded(
            original.replace(b'"source_system":1', b'"source_system":2')
        )
        declaration = sealed.evidence.to_mapping()
        declaration["parent_journal_sha256"] = raw["journal_original"]["sha256"]
        raw["evidence_original"] = _encoded(canonical_json(declaration).encode())

    with pytest.raises(ValueError):
        _raw_seal(tmp_path, sealed, alter)


@pytest.mark.parametrize("stage", ["begin", "seal"])
def test_no_overwrite_or_removal_of_preexisting_user_file_at_each_stage(tmp_path, stage):
    if stage == "begin":
        destination = tmp_path / "original-authoring.json"
        destination.write_bytes(b"saved user file")
        with pytest.raises(FileExistsError):
            _begin(tmp_path)
    else:
        record, values = _begin(tmp_path)
        binding = _binding(tmp_path, record, values)
        destination = tmp_path / "seal.json"
        destination.write_bytes(b"saved user file")
        with pytest.raises(FileExistsError):
            seal_original_host_authoring(
                record,
                binding,
                clock=ManualClock(1.2),
                clock_epoch=values["clock_epoch"],
                output_dir=tmp_path,
                seal_name="seal.json",
            )
    assert destination.read_bytes() == b"saved user file"


@pytest.mark.parametrize("alias", ["symlink", "junction"])
def test_output_directory_alias_rejected_before_create(tmp_path, monkeypatch, alias):
    name = "is_symlink" if alias == "symlink" else "is_junction"
    original = getattr(Path, name, lambda self: False)
    monkeypatch.setattr(
        Path, name, lambda self: True if self == tmp_path else original(self), raising=False
    )
    with pytest.raises(ValueError, match="symlink/junction"):
        _begin(tmp_path)
    assert not tuple(tmp_path.iterdir())


def test_missing_output_directory_not_automatically_created(tmp_path):
    missing = tmp_path / "new-directory"
    with pytest.raises(ValueError, match="directory must exist"):
        _begin(tmp_path, output_dir=missing)
    assert not missing.exists()


@pytest.mark.parametrize("kind", [OriginalHostAuthoringRecord, SealedOriginalHostAuthoring])
def test_reopened_file_exact_external_hash_required(tmp_path, kind):
    sealed, record, *_ = _sealed(tmp_path)
    value = record if kind is OriginalHostAuthoringRecord else sealed
    with pytest.raises(ValueError, match="SHA"):
        kind.from_path(value.path, expected_sha256="a" * 64)


def test_no_mutable_original_byte_alias_or_sha_repair(tmp_path):
    record, *_ = _begin(tmp_path)
    with pytest.raises(ValueError, match="immutable"):
        OriginalHostAuthoringRecord(
            record.path, bytearray(record.source_receipt_bytes), record.source_receipt_sha256
        )
    with pytest.raises(ValueError, match="SHA"):
        OriginalHostAuthoringRecord(record.path, record.source_receipt_bytes, "a" * 64)

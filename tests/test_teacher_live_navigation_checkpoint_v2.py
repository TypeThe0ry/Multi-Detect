"""Actual local V4 prefix/CRC fixtures; no SIM flight or real native evidence."""

from __future__ import annotations

import importlib.util
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy import teacher_live_navigation_checkpoint_v2 as api
from multidetect.autonomy.serialization import canonical_json
from scripts.onboard_teacher_telemetry_capture_v4 import TeacherTelemetryCaptureV4


def load_helpers(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wire = load_helpers("test_teacher_live_navigation_checkpoint.py", "_CP_v2_original_wire_helpers")
budget_helpers = load_helpers("test_onboard_teacher_sortie_budget_v1.py", "_CP_v2_budget_helpers")


@pytest.fixture
def live_v4_fixture(tmp_path):
    source = wire.live_fixture.__wrapped__(tmp_path)
    args, old_capture, clock, _ = next(source)
    budget = replace(
        budget_helpers.budget.__wrapped__(),
        clock_id=clock.session_id,
        started_at_s=99,
        protocol_packet_reserve=128,
        evidence_bytes=16 * 1024 * 1024,
    )
    capture = TeacherTelemetryCaptureV4(
        tmp_path / "NEW-V4-journals",
        budget=budget,
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id=old_capture.transaction_id,
        local_origin_id=old_capture.local_origin_id,
    )
    sender = wire.Sender(None)
    for batch, boot in enumerate((10100, 10300)):
        samples = []
        for index, message in enumerate(wire.message_set(boot) + wire.health_messages()):
            message.pack(sender)
            sender.seq = (sender.seq + 1) % 256
            samples.append((message, 100.16 + batch * 0.2 + index * 0.001))
        capture.append_batch(samples)
    prefix = api.capture_teacher_committed_prefix_v2(
        capture,
        artifact_root=tmp_path,
        output_directory=tmp_path / "V4-prefix",
        clock=clock,
        navigation_before=args["navigation_before"],
    )
    new_args = {
        **args,
        "prefix_source": prefix,
        "output_directory": tmp_path / "V4-CP",
        "live_capture": capture,
    }
    try:
        yield new_args, capture, clock, budget
    finally:
        if not capture._closed:
            capture.abort("CPU V4 checkpoint cleanup")
        source.close()


def test_exact_V4_budget_to_original_context_and_origin(live_v4_fixture):
    args, capture, clock, budget = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    assert type(checkpoint) is api.LiveTeacherNavigationCheckpointV2
    assert checkpoint.budget == budget and budget.record_limit > 16384
    assert checkpoint.observed_context.captured_at_s == 100.36
    assert checkpoint._live_capture is capture and checkpoint._live_clock is clock
    assert not capture._closed and not any(checkpoint.to_mapping()["claims"].values())
    assert not checkpoint.observed_context.geofence_valid
    assert not checkpoint.observed_context.dem_available
    assert (
        checkpoint.origin.to_mapping()["observed_context_sha256"]
        == checkpoint.observed_context.canonical_hash
    )


def test_readonly_RX_historical_reproduction_does_not_call_open(live_v4_fixture):
    args, capture, _, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    outputs = []

    def replay():
        try:
            outputs.append(
                api.reproduce_teacher_live_navigation_checkpoint_v2(
                    checkpoint,
                    artifact_root=args["artifact_root"],
                    clock_id=args["clock_id"],
                )
            )
        except BaseException as error:
            outputs.append(error)

    thread = threading.Thread(target=replay)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and outputs == [checkpoint]
    assert capture._failure is None and not capture._closed


def test_current_validator_wrong_owner_even_at_original_time(live_v4_fixture):
    args, capture, _, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    errors = []

    def bad_current():
        try:
            api.validate_live_teacher_navigation_checkpoint_v2(
                checkpoint,
                artifact_root=args["artifact_root"],
                now_s=checkpoint.qualified_at_s,
                clock_id=args["clock_id"],
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=bad_current)
    thread.start()
    thread.join(timeout=5)
    assert len(errors) == 1 and "another thread" in str(errors[0])
    assert capture._failure  # Existing ownership poison is NOT relaxed.


def test_expired_historical_receipt_can_reproduce_but_cannot_be_live(live_v4_fixture):
    args, _, clock, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    clock.value = 103
    assert (
        api.reproduce_teacher_live_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            clock_id=args["clock_id"],
        )
        is checkpoint
    )
    with pytest.raises(ValueError, match="lifetime"):
        api.validate_live_teacher_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=args["clock_id"],
        )


def test_source_budget_tamper_refused(live_v4_fixture):
    args, _, _, _ = live_v4_fixture
    prefix = json.loads((args["artifact_root"] / args["prefix_source"].path).read_bytes())
    path = args["artifact_root"] / prefix["budget_source"]["path"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        api.qualify_teacher_live_navigation_checkpoint_v2(**args)


def test_navigation_selection_cannot_omit_a_committed_window_packet(live_v4_fixture):
    args, _, _, _ = live_v4_fixture
    prefix = json.loads((args["artifact_root"] / args["prefix_source"].path).read_bytes())
    prefix["selected_navigation_rows"]["GLOBAL_POSITION_INT"].pop()
    with pytest.raises(ValueError, match="selection omitted"):
        api._read_prefixes_v2(args["artifact_root"], prefix)


def test_streaming_prefix_reader_rejects_full_memory_and_truncated_offsets(live_v4_fixture):
    args, _, _, _ = live_v4_fixture
    prefix = json.loads((args["artifact_root"] / args["prefix_source"].path).read_bytes())
    journals = api._read_prefixes_v2(args["artifact_root"], prefix)
    original = journals["raw"][1]
    assert not isinstance(original, bytes) and len(original) == journals["raw"][0].bytes
    with pytest.raises(ValueError, match="outside finite"):
        original[0:65537]
    with pytest.raises(ValueError, match="outside finite"):
        original[len(original) : len(original) + 1]


def test_legacy_writer_cannot_claim_lifecycle_v2(live_v4_fixture):
    args, capture, clock, _ = live_v4_fixture
    old = api.frozen
    from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3

    legacy = TeacherTelemetryCaptureV3(
        args["artifact_root"] / "legacy-refusal",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id="legacy",
        local_origin_id="legacy",
    )
    try:
        with pytest.raises(ValueError, match="Exact NEW"):
            api.capture_teacher_committed_prefix_v2(
                legacy,
                artifact_root=args["artifact_root"],
                output_directory=args["artifact_root"] / "forbidden",
                clock=clock,
                navigation_before=args["navigation_before"],
            )
        assert capture.budget.record_limit > 16384 and old.MAXIMUM_PREFIX_BYTES == 64 * 1024 * 1024
    finally:
        legacy.abort("CPU legacy refusal cleanup")


def test_prefix_full_byte_hash_detects_same_length_projection_change(live_v4_fixture):
    args, _, _, _ = live_v4_fixture
    prefix = json.loads((args["artifact_root"] / args["prefix_source"].path).read_bytes())
    path = Path(prefix["committed_prefixes"]["projection"]["path"])
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + b" ")
    with pytest.raises(ValueError, match="prefix changed"):
        api._read_prefixes_v2(args["artifact_root"], prefix)


def test_original_qualified_clock_not_renewed_by_reproduction(live_v4_fixture):
    args, _, clock, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    original = canonical_json(checkpoint.to_mapping())
    clock.value += 0.2
    result = api.reproduce_teacher_live_navigation_checkpoint_v2(
        checkpoint,
        artifact_root=args["artifact_root"],
        clock_id=args["clock_id"],
    )
    assert result is checkpoint and canonical_json(result.to_mapping()) == original


def test_new_committed_generation_requires_new_current_checkpoint(live_v4_fixture):
    args, capture, clock, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    clock.value += 0.1
    heartbeat = wire.health_messages()[0]
    heartbeat.pack(wire.Sender(None))
    capture.append_batch([(heartbeat, clock.value)])
    assert (
        api.reproduce_teacher_live_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            clock_id=args["clock_id"],
        )
        is checkpoint
    )
    with pytest.raises(ValueError, match="generation changed"):
        api.validate_live_teacher_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=args["clock_id"],
        )


def test_closed_writer_historical_reproduction_cannot_acquire_current(live_v4_fixture):
    args, capture, clock, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    capture.seal()
    assert (
        api.reproduce_teacher_live_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            clock_id=args["clock_id"],
        )
        is checkpoint
    )
    with pytest.raises(RuntimeError, match="closed"):
        api.validate_live_teacher_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=args["clock_id"],
        )


def test_actual_clock_expiry_during_readonly_reproduction_still_refuses_live(
    live_v4_fixture,
    monkeypatch,
):
    args, _, clock, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    reproduce = api.reproduce_teacher_live_navigation_checkpoint_v2

    def slow(*positional, **keywords):
        result = reproduce(*positional, **keywords)
        clock.value = checkpoint.expires_at_s
        return result

    monkeypatch.setattr(api, "reproduce_teacher_live_navigation_checkpoint_v2", slow)
    with pytest.raises(ValueError, match="expired while"):
        api.validate_live_teacher_navigation_checkpoint_v2(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=args["clock_id"],
        )


def test_record_selection_keeps_latest_old_anchor_and_all_current_changes():
    def row(index, kind, at):
        return {
            "record_index": index,
            "message_type": kind,
            "received_at_s": at,
            "source_accepted": True,
        }

    records = [
        row(0, "HOME_POSITION", 90),
        row(1, "HOME_POSITION", 98),
        row(2, "GPS_GLOBAL_ORIGIN", 99),
        row(3, "GLOBAL_POSITION_INT", 99.9),
        row(4, "HOME_POSITION", 100.1),
        row(5, "HOME_POSITION", 101),
    ]
    assert [r["record_index"] for r in api._selected_records(records, 100)] == [1, 2, 3, 4, 5]


def test_V4_checkpoint_really_retains_context_indices_above_16384(tmp_path):
    # Actual independently packed CPU bytes through the real V4 dual-commit
    # writer, not a changed maximum_packets label or reindexed metadata.
    original = wire.live_fixture.__wrapped__(tmp_path)
    args, old_capture, clock, _ = next(original)
    budget = replace(
        budget_helpers.budget.__wrapped__(),
        clock_id=clock.session_id,
        started_at_s=20,
        preflight_s=100,
    )
    clock.value = 20.05
    capture = TeacherTelemetryCaptureV4(
        tmp_path / "V4-many-records",
        budget=budget,
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id=old_capture.transaction_id,
        local_origin_id=old_capture.local_origin_id,
    )
    try:
        sender = wire.Sender(None)
        heartbeat = wire.health_messages()[0]
        heartbeat.pack(sender)
        for batch in range(1025):
            received = 20 + batch * 0.075
            clock.value = received + 0.05
            capture.append_batch([(heartbeat, received)] * 16)
        assert len(capture._records) == 16400
        clock.value = 100.51
        for batch, boot in enumerate((10100, 10300)):
            samples = []
            for index, message in enumerate(wire.message_set(boot) + wire.health_messages()):
                message.pack(sender)
                sender.seq = (sender.seq + 1) % 256
                samples.append((message, 100.16 + batch * 0.2 + index * 0.001))
            capture.append_batch(samples)
        prefix = api.capture_teacher_committed_prefix_v2(
            capture,
            artifact_root=tmp_path,
            output_directory=tmp_path / "many-prefix",
            clock=clock,
            navigation_before=args["navigation_before"],
        )
        checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(
            **{
                **args,
                "prefix_source": prefix,
                "output_directory": tmp_path / "many-checkpoint",
                "live_capture": capture,
            }
        )
        assert min(checkpoint.record_indices.values()) >= 16400
        assert checkpoint.observed_context.captured_at_s == 100.36
        assert len(capture._records) == 16424 and not capture._closed
    finally:
        if not capture._closed:
            capture.abort("CPU absolute-index checkpoint cleanup")
        original.close()


def test_checkpoint_profiling_retains_real_original_stage_times(live_v4_fixture):
    args, _, clock, _ = live_v4_fixture
    checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    timing = json.loads((args["output_directory"] / "qualification-timing.json").read_bytes())
    assert timing["started_at_s"] == timing["finished_at_s"] == clock.value
    assert timing["semantic_check_completed"] and timing["error"] is None
    assert timing["diagnostic_not_an_admission_receipt"]
    assert 1 <= len(timing["stages"]) <= 16
    assert all(
        stage["duration_s"] == stage["finished_at_s"] - stage["started_at_s"]
        for stage in timing["stages"]
    )
    assert checkpoint.qualified_at_s == args["now_s"]
    assert not any(timing["claims"].values())


def test_checkpoint_profiling_retains_failure_without_an_admission(live_v4_fixture):
    args, _, _, _ = live_v4_fixture
    with pytest.raises(ValueError) as refusal:
        api.qualify_teacher_live_navigation_checkpoint_v2(**{**args, "owner_token": "changed"})
    timing = json.loads((args["output_directory"] / "qualification-timing.json").read_bytes())
    assert not timing["semantic_check_completed"]
    assert timing["error"] == f"{type(refusal.value).__name__}: {refusal.value}"
    assert not (args["output_directory"] / "live-navigation-checkpoint.json").exists()
    assert not any(timing["claims"].values())


def test_diagnostic_fsync_time_is_not_excluded_from_original_TTL(live_v4_fixture, monkeypatch):
    args, _, clock, _ = live_v4_fixture
    save = api._save_json

    def slow_diagnostic(root, path, value, ledger=None):
        result = save(root, path, value, ledger)
        if path.name == "qualification-timing.json":
            clock.value = 103
        return result

    monkeypatch.setattr(api, "_save_json", slow_diagnostic)
    with pytest.raises(ValueError, match="expired during diagnostic persistence"):
        api.qualify_teacher_live_navigation_checkpoint_v2(**args)
    original = json.loads(
        (args["output_directory"] / "live-navigation-checkpoint.json").read_bytes()
    )
    assert original["qualified_at_s"] == args["now_s"] < original["expires_at_s"] < clock.value
    assert not any(original["claims"].values())


def test_all_NEW_checkpoint_artifacts_precharged_snapshots_charged_only_by_writer(live_v4_fixture):
    from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
    from scripts.onboard_teacher_sortie_artifact_ledger_v1 import TeacherSortieArtifactLedger

    args, capture, clock, budget = live_v4_fixture
    root = args["artifact_root"]
    ledger = TeacherSortieArtifactLedger(
        budget=budget,
        clock=clock.monotonic,
        camera_directory=root / "camera-owned",
        evidence_directory=root / "evidence-owned",
    )
    try:
        before = capture._snapshot_charged
        prefix = api.capture_teacher_committed_prefix_v2(
            capture,
            artifact_root=root,
            output_directory=ledger.evidence_directory / "prefix",
            clock=clock,
            navigation_before=args["navigation_before"],
            artifact_ledger=ledger,
        )
        checkpoint = api.qualify_teacher_live_navigation_checkpoint_v2(
            **{
                **args,
                "prefix_source": prefix,
                "output_directory": ledger.evidence_directory / "checkpoint",
                "artifact_ledger": ledger,
            }
        )
        assert capture._snapshot_charged > before
        snapshot_paths = [
            p for p in capture.directory.rglob("*") if p.is_file() and "snapshot" in str(p)
        ]
        assert snapshot_paths and all(
            not p.is_relative_to(ledger.evidence_directory) for p in snapshot_paths
        )
        for source in (
            prefix,
            checkpoint.checkpoint_source,
            EvidenceBinding.from_mapping(checkpoint.to_mapping()["input_source"]),
        ):
            assert ledger.require_source(source, artifact_root=root, category="evidence") is source
        assert ledger.capacity()["remaining_evidence_bytes"] > 0
        rows = [json.loads(raw) for raw in ledger._journal_path.read_bytes().splitlines()]
        reservations = {
            row["reservation"]["path"]: index
            for index, row in enumerate(rows)
            if row.get("event") == "reserved-before-write"
        }
        commits = {
            row["source"]["path"]: index
            for index, row in enumerate(rows)
            if row.get("event") == "committed-original-source"
        }
        assert reservations.keys() == commits.keys() and len(commits) == 7
        assert all(reservations[path] < commits[path] for path in reservations)
        assert not any(checkpoint.to_mapping()["claims"].values())
    finally:
        ledger.close()


def test_foreign_ledger_clock_rejected_before_NEW_checkpoint_files(live_v4_fixture):
    from scripts.onboard_teacher_sortie_artifact_ledger_v1 import TeacherSortieArtifactLedger

    args, capture, clock, budget = live_v4_fixture
    root = args["artifact_root"]
    ledger = TeacherSortieArtifactLedger(
        budget=budget,
        clock=lambda: clock.value,
        camera_directory=root / "foreign-camera",
        evidence_directory=root / "foreign-evidence",
    )
    try:
        target = ledger.evidence_directory / "forbidden"
        with pytest.raises(ValueError, match="ledger/clock"):
            api.capture_teacher_committed_prefix_v2(
                capture,
                artifact_root=root,
                output_directory=target,
                clock=clock,
                navigation_before=args["navigation_before"],
                artifact_ledger=ledger,
            )
        assert not target.exists() and not ledger._pending
    finally:
        ledger.close()

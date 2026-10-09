"""Isolated CPU queue/deadline tests, explicitly NOT V8 source admission.

Production V8 construction refuses the old fixture's generic report. Here
the tested boundary is ONLY queue scheduling after a main-thread verifier;
source-validation test doubles cannot count as actual/live authorization.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_onboard_teacher_mission_execution_v1 as frozen_tests

from multidetect.autonomy.serialization import canonical_json
from scripts import onboard_teacher_mission_execution_v2 as api
from scripts.onboard_teacher_mission_live_revalidation_v1 import TeacherMissionLiveRevalidation


@pytest.fixture
def harness(tmp_path):
    yield from frozen_tests.harness.__wrapped__(tmp_path)


@pytest.fixture
def queue_seam(harness, monkeypatch):
    h = harness
    execution = h.bind(send_timeout_s=0.1)
    # ONLY test the new queue method. This does not call or assert success of
    # V8 construction/admission, and cannot mint a production lease or Grant.
    execution.__class__ = api.TeacherMissionExecutionV2
    execution._live_inputs = execution._original = None
    execution._live_evidence = []
    execution._live_receipt = None
    execution._live_phase = execution._phase_started_at_s = None
    frozen_tests.ready(h, execution)

    def prepare(kind):
        execution._live_phase = kind
        execution._phase_started_at_s = h.clock()
        execution._live_receipt = TeacherMissionLiveRevalidation(
            canonical_json({"CPU_queue_test_only": True, "expires_at_s": h.clock() + 2})
        )

    monkeypatch.setattr(execution, "_prepare_phase", prepare)
    monkeypatch.setattr(execution, "_verify_live_receipt", lambda *_: h.clock())
    return h, execution


def test_deadline_is_after_source_inspection_and_intention_fsync(queue_seam, monkeypatch):
    h, execution = queue_seam
    inspect = execution._runtime_now
    save = execution._save

    def slow_inspect():
        h.clock.offset += 0.4  # Greater than the API send timeout.
        inspect()

    def slow_intention(name, value):
        result = save(name, value)
        if name.endswith(".intent.json"):
            h.clock.offset += 0.4
        return result

    monkeypatch.setattr(execution, "_runtime_now", slow_inspect)
    monkeypatch.setattr(execution, "_save", slow_intention)
    before = h.clock()
    result = execution._request("mission-mode")
    assert result["API_attempted_at_s"] >= before + 0.8
    assert result["API_returned"] is True
    assert result["delivery_verified"] is False
    assert h.connection.mav.calls[-1][0] == "set_mode_send"
    assert h.connection.mav.calls[-1][2] != threading.get_ident()


def test_fsync_expiring_source_lease_never_queues_send(queue_seam, monkeypatch):
    h, execution = queue_seam
    prepare, save = execution._prepare_phase, execution._save

    def short_lease(kind):
        prepare(kind)
        execution._live_receipt = TeacherMissionLiveRevalidation(
            canonical_json({"CPU_queue_test_only": True, "expires_at_s": h.clock() + 0.05})
        )

    def delayed(name, value):
        result = save(name, value)
        if name.endswith(".intent.json"):
            h.clock.offset += 0.1
        return result

    monkeypatch.setattr(execution, "_prepare_phase", short_lease)
    monkeypatch.setattr(execution, "_save", delayed)
    with pytest.raises(ValueError, match="No current phase time"):
        execution._request("mission-mode")
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_send_owner_rechecks_current_source_before_API(queue_seam, monkeypatch):
    h, execution = queue_seam

    def changed_source(kind, now=None):
        if threading.get_ident() != execution._owner:
            raise ValueError("CPU current source changed before API")
        return h.clock()

    monkeypatch.setattr(execution, "_verify_live_receipt", changed_source)
    with pytest.raises(ValueError, match="source changed"):
        execution._request("mission-mode")
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_current_persistent_state_cannot_change_after_revalidation(queue_seam, monkeypatch):
    h, execution = queue_seam
    prepare = execution._prepare_phase

    def changed_state(kind):
        prepare(kind)
        h.persistent.transaction._state = frozen_tests.MissionTransactionState.COMMITTED

    monkeypatch.setattr(execution, "_prepare_phase", changed_state)
    with pytest.raises(ValueError, match="binding differs"):
        execution._request("mission-mode")
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_V8_queue_overflow_fails_closed_without_controls(queue_seam, monkeypatch):
    h, execution = queue_seam

    def full(_):
        raise queue.Full

    monkeypatch.setattr(h.receiver._commands, "put_nowait", full)
    with pytest.raises(RuntimeError, match="queue exhausted"):
        execution._request("mission-mode")
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_cancel_during_main_source_collection_never_queues_send(queue_seam, monkeypatch):
    h, execution = queue_seam
    monkeypatch.setattr(execution, "_prepare_phase", lambda _: execution.cancel())
    with pytest.raises(RuntimeError, match="poisoned"):
        execution._request("mission-mode")
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_invalid_control_is_rejected_before_provider(queue_seam, monkeypatch):
    h, execution = queue_seam
    monkeypatch.setattr(
        execution, "_prepare_phase", lambda _: pytest.fail("must not call provider")
    )
    with pytest.raises(ValueError, match="three existing finite"):
        execution._request("arbitrary-command")
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


@pytest.fixture
def writer_seam(harness):
    # Test the live-handle guard without creating a V8 receipt/permission.
    execution = api.TeacherMissionExecutionV2.__new__(api.TeacherMissionExecutionV2)
    execution._transport = harness.transport
    execution.intent = harness.intent
    execution._owner = threading.get_ident()
    checkpoint = SimpleNamespace(
        _live_capture=harness.capture,
        _live_clock=SimpleNamespace(session_id=harness.intent.clock_id, monotonic=harness.clock),
        committed_prefixes={
            stage: {
                "prefix_bytes": harness.capture._sizes[stage],
                "prefix_sha256": harness.capture._digests[stage].hexdigest(),
                "commits": dict(harness.capture._commits[stage]),
                "file_device": Path(harness.capture._streams[stage].name).stat().st_dev,
                "file_inode": Path(harness.capture._streams[stage].name).stat().st_ino,
            }
            for stage in ("raw", "projection")
        },
    )
    return execution, SimpleNamespace(navigation_checkpoint=checkpoint), harness


def test_live_handle_guard_accepts_only_original_open_writer_identity(writer_seam):
    execution, inputs, _ = writer_seam
    execution._check_live_writer(inputs)
    # This asserts only identity/idle state, not source or authorization PASS.


@pytest.mark.parametrize(
    "change",
    [
        "different-writer",
        "closed",
        "failed",
        "busy",
        "foreign-owner",
        "clock-domain",
        "clock-callable",
    ],
)
def test_live_handle_guard_rejects_writer_or_clock_replacement(writer_seam, change):
    execution, inputs, h = writer_seam
    checkpoint = inputs.navigation_checkpoint
    if change == "different-writer":
        checkpoint._live_capture = SimpleNamespace(**h.capture.__dict__)
    elif change == "closed":
        h.capture.abort("CPU handle guard test")
    elif change == "failed":
        h.capture._failure = "CPU writer failure"
    elif change == "busy":
        h.capture._busy = True
    elif change == "foreign-owner":
        execution._owner += 1
    elif change == "clock-domain":
        checkpoint._live_clock.session_id = "foreign-clock-domain"
    else:
        checkpoint._live_clock.monotonic = lambda: h.clock()
    with pytest.raises(ValueError, match="exact open writer"):
        execution._check_live_writer(inputs)
    assert not any(
        m in {"set_mode_send", "command_long_send"} for m, _, _ in h.connection.mav.calls
    )
    if change == "busy":
        # Restore fixture bookkeeping only, so its existing teardown can close.
        h.capture._busy = False


@pytest.mark.parametrize("stage", ["raw", "projection"])
@pytest.mark.parametrize("change", ["length", "digest", "unverified-commit"])
def test_execution_phase_requires_exact_dual_committed_generation(writer_seam, stage, change):
    execution, inputs, h = writer_seam
    if change == "length":
        h.capture._sizes[stage] += 1
    elif change == "digest":
        h.capture._digests[stage].update(b"CPU unknown current tail")
    else:
        h.capture._commits[stage]["attempted"] += 1
    with pytest.raises(ValueError, match="dual-committed source generation changed"):
        execution._check_live_writer(inputs)
    assert not any(
        m in {"set_mode_send", "command_long_send"} for m, _, _ in h.connection.mav.calls
    )


@pytest.mark.parametrize("stage", ["raw", "projection"])
def test_execution_phase_refuses_an_unverified_physical_journal_tail(writer_seam, stage):
    execution, inputs, h = writer_seam
    with Path(h.capture._streams[stage].name).open("ab") as stream:
        stream.write(b"CPU unverified external tail")
    with pytest.raises(ValueError, match="dual-committed source generation changed"):
        execution._check_live_writer(inputs)
    assert not any(
        m in {"set_mode_send", "command_long_send"} for m, _, _ in h.connection.mav.calls
    )

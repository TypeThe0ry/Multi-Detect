"""Actual CPU files and fsyncs; no runtime, native success or Grant claim."""

import hashlib
import threading
from dataclasses import replace

import pytest
from test_onboard_teacher_sortie_budget_v1 import budget as budget_fixture
from test_onboard_teacher_telemetry_capture_v4 import Clock

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from scripts.onboard_teacher_sortie_artifact_ledger_v1 import (
    LedgerFileBinding,
    TeacherSortieArtifactLedger,
)

budget = budget_fixture


@pytest.fixture
def ledger(tmp_path, budget):
    clock = Clock()
    value = TeacherSortieArtifactLedger(
        budget=budget,
        clock=clock,
        camera_directory=tmp_path / "camera",
        evidence_directory=tmp_path / "evidence",
    )
    try:
        yield value, clock
    finally:
        if not value._closed:
            value.close()


def commit(ledger, category, path, raw=b"actual-original-bytes", maximum_bytes=100):
    reservation = ledger.reserve_write(category, path, maximum_bytes=maximum_bytes)
    assert not path.exists()
    journal = ledger._journal_path.read_bytes()
    assert b"reserved-before-write" in journal and str(path).replace("\\", "/").encode() in journal
    path.write_bytes(raw)
    binding = LedgerFileBinding(path.as_posix(), len(raw), hashlib.sha256(raw).hexdigest())
    ledger.commit_write(reservation, binding)
    return binding


def test_prewrite_charge_no_refund_actualfiles_and_source(ledger):
    value, _ = ledger
    original = value.capacity()
    path = value._roots["camera"] / "original.bin"
    binding = commit(value, "camera", path, maximum_bytes=1000)
    after = value.capacity()
    assert original["remaining_camera_bytes"] - after["remaining_camera_bytes"] == 1000
    value.require_source(
        EvidenceBinding(path.name, binding.bytes, binding.sha256, "CPU", ""),
        artifact_root=path.parent,
        category="camera",
    )


def test_honest_zero_byte_native_stderr_retained(ledger):
    value, _ = ledger
    binding = commit(
        value, "evidence", value._roots["evidence"] / "stderr.bin", raw=b"", maximum_bytes=1
    )
    assert binding.bytes == 0 and value.capacity()["remaining_evidence_bytes"] > 0


@pytest.mark.parametrize(
    "change", ["pending", "unknown", "missing", "replaced", "changed", "journal-tail"]
)
def test_untracked_or_modified_actualsources_refuse_capacity(ledger, change):
    value, _ = ledger
    path = value._roots["camera"] / "original.bin"
    if change == "pending":
        value.reserve_write("camera", path, maximum_bytes=100)
    else:
        commit(value, "camera", path)
        if change == "unknown":
            (path.parent / "unknown.bin").write_bytes(b"native-output-not-charged")
        elif change == "missing":
            path.unlink()
        elif change == "replaced":
            path.rename(path.with_suffix(".saved"))
            path.write_bytes(b"actual-original-bytes")
        elif change == "changed":
            path.write_bytes(b"different-original")
        else:
            with value._journal_path.open("ab") as stream:
                stream.write(b"uncommitted-tail")
    with pytest.raises(ValueError):
        value.capacity()


def test_no_source_claim_without_prewrite_reservation(ledger):
    value, _ = ledger
    path = value._roots["camera"] / "foreign.bin"
    path.write_bytes(b"already-written")
    with pytest.raises(ValueError, match="Fresh"):
        value.reserve_write("camera", path, maximum_bytes=100)
    with pytest.raises(ValueError, match="not committed"):
        value.require_source(
            EvidenceBinding(
                path.name, 14, hashlib.sha256(path.read_bytes()).hexdigest(), "CPU", ""
            ),
            artifact_root=path.parent,
        )


def test_mismatched_binding_keeps_charge_and_pending(ledger):
    value, _ = ledger
    path = value._roots["camera"] / "original.bin"
    reservation = value.reserve_write("camera", path, maximum_bytes=100)
    path.write_bytes(b"original")
    with pytest.raises(ValueError, match="SHA"):
        value.commit_write(reservation, LedgerFileBinding(path.as_posix(), 8, "0" * 64))
    with pytest.raises(ValueError, match="Pending"):
        value.capacity()
    assert value._charged["camera"] == 100


def test_expiry_regression_and_forged_reservation_refused(ledger):
    value, clock = ledger
    path = value._roots["camera"] / "original.bin"
    reservation = value.reserve_write("camera", path, maximum_bytes=100)
    path.write_bytes(b"original")
    binding = LedgerFileBinding(path.as_posix(), 8, hashlib.sha256(b"original").hexdigest())
    with pytest.raises(TypeError, match="Exact"):
        value.commit_write(replace(reservation), binding)
    clock.value = 100.0
    with pytest.raises(ValueError, match="regressed"):
        value.capacity()
    clock.value = value.budget.deadline_s
    with pytest.raises(ValueError, match="exhausted"):
        value.capacity()


def test_unbound_thread_cannot_reserve_or_claim_capacity(ledger):
    value, _ = ledger
    errors = []

    def foreign():
        try:
            value.reserve_write("camera", value._roots["camera"] / "bad.bin", maximum_bytes=100)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert not value._pending


def test_scope_escape_and_overallocation_refuse_beforewrite(ledger):
    value, _ = ledger
    for path, size in (
        (value._roots["camera"].parent / "escape.bin", 100),
        (value._roots["camera"] / "large.bin", value.budget.camera_bytes + 1),
    ):
        with pytest.raises(ValueError):
            value.reserve_write("camera", path, maximum_bytes=size)
        assert not path.exists()

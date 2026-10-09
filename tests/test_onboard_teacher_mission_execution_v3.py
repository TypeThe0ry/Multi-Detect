"""Bounded CPU resource/intent seams; explicitly NOT complete Mission authority."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_onboard_teacher_mission_transport_v1 import NOW
from test_onboard_teacher_mission_transport_v1 import transaction as transaction_fixture
from test_onboard_teacher_sortie_artifact_ledger_v1 import ledger as ledger_fixture
from test_onboard_teacher_sortie_budget_v1 import budget as budget_fixture

from multidetect.autonomy.serialization import canonical_json
from scripts import onboard_teacher_mission_execution_v3 as api
from scripts.onboard_teacher_mission_live_revalidation_v2 import TeacherMissionLiveRevalidation

budget = budget_fixture
ledger = ledger_fixture
transaction = transaction_fixture


@pytest.fixture
def capacity_seam(ledger, transaction):
    ledger, clock = ledger
    # This __new__ exercises ONLY resource mathematics with real ledger files.
    # The full constructor/source finding/current lease is not admitted here.
    value = api.TeacherMissionExecutionV3.__new__(api.TeacherMissionExecutionV3)
    value._ledger = ledger
    value.budget = ledger.budget
    value.intent = SimpleNamespace(clock_id=ledger.budget.clock_id)
    value._transport = SimpleNamespace(
        _capture=SimpleNamespace(_attempts=[], _charged_bytes=0),
        _transaction=transaction.persistent,
        _utc=lambda: NOW,
    )
    value._scope = lambda **_: clock()
    value._live_receipt = TeacherMissionLiveRevalidation(
        canonical_json({"current_budget": {"full_route_estimated_duration_s": 100}})
    )
    return value, ledger, clock


def test_current_normalARM_capacity_math_excludes_completed_mode(capacity_seam):
    value, _, clock = capacity_seam
    clock.value = 155
    value._assert_start_capacity("normal-arm")
    with pytest.raises(ValueError, match="Insufficient"):
        value._assert_start_capacity("mission-mode")


@pytest.mark.parametrize(
    "change", ["time", "records", "telemetry", "camera", "evidence", "pending", "short-grant"]
)
def test_no_remaining_start_capacity_refuses(capacity_seam, change):
    value, ledger, clock = capacity_seam
    if change == "time":
        clock.value = 156
    elif change == "records":
        value._transport._capture._attempts = [None] * 50000
    elif change == "telemetry":
        value._transport._capture._charged_bytes = 500 * 1024 * 1024
    elif change in {"camera", "evidence"}:
        ledger.reserve_write(change, ledger._roots[change] / "reserved.bin", maximum_bytes=100)
        # An incomplete native/write operation never counts as valid capacity.
    elif change == "pending":
        ledger.reserve_write("camera", ledger.camera_directory / "partial.bin", maximum_bytes=100)
    else:
        value._transport._utc = lambda: NOW + timedelta(minutes=14)
    with pytest.raises(ValueError):
        value._assert_start_capacity("normal-arm")


def test_execution_intent_is_NEW600s_and_immutable_original_budget(budget):
    fields = dict(
        transaction_id="CPU-only",
        image="CPU-fixture",
        clock_id=budget.clock_id,
        approved_at_s=budget.started_at_s,
        expires_at_s=budget.deadline_s,
        explicitly_authorized=True,
        return_abort_authorized=True,
        budget_sha256=budget.canonical_hash,
    )
    for key in (
        "journal_identity_sha256",
        "start_permit_sha256",
        "context_snapshot_sha256",
        "context_admission_sha256",
        "airframe_profile_sha256",
        "runtime_attestation_sha256",
        "container_id",
    ):
        fields[key] = "1" * 64
    intent = api.TeacherMissionExecutionIntentV3(**fields)
    assert intent.expires_at_s - intent.approved_at_s == 235
    assert api.TeacherMissionExecutionIntentV3.from_mapping(intent.to_mapping()) == intent
    with pytest.raises(ValueError, match="ten minutes"):
        api.TeacherMissionExecutionIntentV3(**{**fields, "expires_at_s": 701})


def test_non_exact_legacy_transport_doesnot_bootstrap_NEW_execution():
    with pytest.raises(TypeError, match="V6"):
        api.TeacherMissionExecutionV3(
            transport=object(),
            permit=None,
            context=None,
            admission=None,
            runtime_attestation=None,
            intent=None,
            runtime_recheck=lambda: None,
            evidence_directory="unused",
            artifact_root="unused",
            original_inputs=None,
            live_input_provider=lambda **_: None,
            artifact_ledger=None,
        )

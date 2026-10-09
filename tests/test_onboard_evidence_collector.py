from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from test_onboard_agent import _task
from test_onboard_vlm_admission import POLICY, _anchor, _current
from test_onboard_vlm_provider import _raw, _source
from test_px4_mission_feedback import _mission

from multidetect.autonomy.onboard_adapter import OnboardMissionRevision
from multidetect.autonomy.onboard_agent import (
    OnboardDecision,
    OnboardModelProposal,
    OnboardResponse,
    OnboardTaskAction,
)
from multidetect.autonomy.onboard_evidence_collector import (
    TELEMETRY_CONTRACT,
    OnboardAcceptanceTrace,
    OnboardEvidenceCollector,
    OnboardEvidenceCollectorError,
    OnboardMissionTelemetryBinding,
    OnboardTelemetryObservation,
)
from multidetect.autonomy.onboard_multimodal import MissionEvidenceLedger
from multidetect.autonomy.onboard_task_state import OnboardTaskReceipt
from multidetect.autonomy.onboard_vlm_admission import admit_anchored_intent
from multidetect.autonomy.onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
    serialize_onboard_vlm_messages,
)
from multidetect.autonomy.serialization import canonical_json
from multidetect.autonomy.task_policy_runtime import TaskPolicyRuntimeBinding
from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionJournalIdentity,
    MissionTransactionState,
)
from multidetect.px4_mission import MissionStartPermit, MissionTransactionBindings

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
VEHICLE_HASH = "e" * 64


def _bytes(value):
    return canonical_json(value).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _setup():
    original, context, event, scope = _source()
    collector = OnboardEvidenceCollector(
        scope,
        expected_system_id=1,
        expected_component_id=1,
        vehicle_identity_sha256=VEHICLE_HASH,
    )
    frame = original.model_input.frames[0]
    collector.add_observation(frame)
    request = OnboardVlmRuntimeRequest(
        replace(original.model_input, ledger=collector.snapshot()),
        source_event=event,
    )
    raw = _raw(
        request,
        {
            "response": "replan",
            "reason": "fixture proposal not model inference",
            "confidence": 0.9,
            "task_action": "search",
            "flight_plan": {
                "action": "search",
                "north_offset_m": 10.0,
                "east_offset_m": 0.0,
                "altitude_m": 35.0,
                "speed_mps": None,
                "duration_s": None,
                "reference_id": None,
            },
        },
    )
    proposed = collector.record_proposal(
        request,
        raw,
        current_scope=scope,
        completed_at_s=10.05,
        inference_latency_ms=50.0,
    )
    response = parse_onboard_vlm_runtime_response(
        raw,
        request,
        current_scope=scope,
        completed_at_s=10.05,
        inference_latency_ms=50.0,
    )
    mission = _mission()
    model = OnboardModelProposal(
        response.output.response,
        response.output.reason,
        response.output.confidence,
        "fixture-base-vlm",
        "fixture-v2",
        "f" * 64,
        "fixture-provider",
        50,
        response.output.flight_plan,
        response.output.task_action,
    )
    binding = TaskPolicyRuntimeBinding(
        scope.vehicle_class,
        context.canonical_hash,
        scope.airframe_profile_sha256,
        "select",
        "fixture accepted compilation",
        scope.selected_task_id,
        None,
        None,
        mission.platform_plan_hash,
        mission.canonical_hash,
        "d" * 64,
        True,
        True,
        produced_at_s=context.captured_at_s,
    )
    decision = OnboardDecision(
        scope.prompt_sha256,
        context.canonical_hash,
        event.canonical_hash,
        OnboardResponse.REPLAN,
        model.reason,
        binding,
        model_proposal=model,
    )
    task_receipt = OnboardTaskReceipt(
        0,
        OnboardTaskAction.SEARCH,
        event.event_id,
        event.canonical_hash,
        event.observed_at_s,
        context.canonical_hash,
        None,
        None,
        None,
        None,
        None,
        None,
        model.model_id,
        model.model_version,
        model.model_sha256,
        model.provider,
        context.gps_fix_type,
        context.gps_horizontal_accuracy_m,
    )
    revision = OnboardMissionRevision(
        1,
        event.kind.value,
        event,
        decision,
        mission,
        True,
        False,
        False,
        flight_plan_dispatched=True,
        task_receipt=task_receipt,
    )
    payload = _bytes(revision.to_mapping())
    trace = OnboardAcceptanceTrace(
        proposed.proposal_id,
        revision,
        context,
        event,
        payload,
        _sha(payload),
        10.06,
        scope.clock_epoch,
    )
    return collector, request, raw, trace


def _journal(
    tmp_path, trace, *, state=MissionTransactionState.START_PERMITTED, identity_change=None
):
    task = _task()
    mission = trace.revision.mission
    bindings = MissionTransactionBindings(
        trace.revision.decision.prompt_sha256,
        task.canonical_hash,
        mission.platform_plan_hash,
        trace.context.canonical_hash,
        mission.airframe_profile_hash,
        "1" * 64,
        VEHICLE_HASH,
        "2" * 64,
        "3" * 64,
    )
    identity = MissionJournalIdentity(
        "fixture-tx-1",
        mission.canonical_hash,
        "d" * 64,
        "4" * 64,
        bindings,
        len(mission.items),
        mission.airframe_adapter_id,
    )
    if identity_change:
        identity = identity_change(identity)
    path = tmp_path / "fixture-journal.jsonl"
    with DurableMissionJournal.create(path, identity, now_utc=NOW) as journal:
        if state is not MissionTransactionState.AUTHORIZED:
            journal.record_transition(
                MissionTransactionState.UPLOADING,
                payload={"protocol_hash": identity.protocol_hash, "item_count": 3},
                now_utc=NOW,
            )
            for index in range(3):
                journal.record_item_request(index, now_utc=NOW)
            journal.record_transition(
                MissionTransactionState.UPLOADED,
                payload={
                    "ack_type": 0,
                    "mission_type": 0,
                    "source_system": 1,
                    "source_component": 1,
                },
                now_utc=NOW,
            )
            journal.record_transition(
                MissionTransactionState.READBACK_VERIFIED,
                payload={"protocol_hash": identity.protocol_hash, "item_count": 3},
                now_utc=NOW,
            )
            journal.record_transition(
                MissionTransactionState.COMMITTED,
                payload={"protocol_hash": identity.protocol_hash},
                now_utc=NOW,
            )
            if state is MissionTransactionState.START_PERMITTED:
                journal.record_transition(
                    state,
                    payload={
                        "transaction_id": identity.transaction_id,
                        "executable_mission_hash": identity.executable_mission_hash,
                        "protocol_hash": identity.protocol_hash,
                    },
                    now_utc=NOW,
                )
    original = path.read_bytes()
    return OnboardMissionTelemetryBinding(
        mission,
        task,
        path,
        len(original),
        _sha(original),
        VEHICLE_HASH,
        10.07,
        trace.clock_epoch,
    )


def _ready(tmp_path):
    collector, request, raw, trace = _setup()
    collector.record_acceptance(trace)
    binding = _journal(tmp_path, trace)
    collector.bind_vehicle_mission(trace.proposal_id, binding)
    return collector, request, raw, trace, binding


def _observation(binding, at_s, kind, *, message_id=None, **fields):
    raw = {
        "schema_version": 1,
        "contract": TELEMETRY_CONTRACT,
        "message_id": message_id or f"{kind}-{at_s}",
        "observed_at_s": at_s,
        "clock_epoch": binding.clock_epoch,
        "source_id": "host-vehicle-receiver-v1",
        "source_system_id": 1,
        "source_component_id": 1,
        "mission_sha256": binding.mission.canonical_hash,
        "transaction_id": "fixture-tx-1",
        "message": {"message_type": kind, **fields},
    }
    payload = _bytes(raw)
    return OnboardTelemetryObservation(payload, _sha(payload))


def _ingest(collector, trace, binding, at_s, kind, **fields):
    return collector.ingest_vehicle_message(
        trace.proposal_id,
        _observation(binding, at_s, kind, **fields),
        now_s=at_s,
    )


def _execute(collector, trace, binding):
    assert (
        _ingest(collector, trace, binding, 10.10, "HEARTBEAT", base_mode=129, custom_mode=67371008)
        is None
    )
    assert _ingest(collector, trace, binding, 10.11, "EXTENDED_SYS_STATE", landed_state=2) is None
    return _ingest(collector, trace, binding, 10.12, "MISSION_CURRENT", seq=0)


def test_exact_causal_lifecycle_is_available_to_next_real_image_request(tmp_path):
    collector, request, _, trace, binding = _ready(tmp_path)
    assert [item.stage for item in collector.snapshot().receipts] == ["proposed", "accepted"]
    journal_before = binding.journal_path.read_bytes()
    executed = _execute(collector, trace, binding)
    assert executed.stage == "executed"
    assert _ingest(collector, trace, binding, 10.20, "MISSION_CURRENT", seq=2) is None
    assert _ingest(collector, trace, binding, 10.21, "MISSION_ITEM_REACHED", seq=2) is None
    assert (
        _ingest(collector, trace, binding, 10.22, "HEARTBEAT", base_mode=1, custom_mode=67371008)
        is None
    )
    completed = _ingest(collector, trace, binding, 10.23, "EXTENDED_SYS_STATE", landed_state=1)
    assert completed.stage == "completed"
    ledger = collector.snapshot()
    assert [item.stage for item in ledger.receipts] == [
        "proposed",
        "accepted",
        "executed",
        "completed",
    ]
    assert collector.receipt_revision == 4
    for prior, current in zip(ledger.receipts, ledger.receipts[1:], strict=False):
        assert current.prior_evidence_id == prior.evidence_id
    assert ledger.receipts[1].source_receipt_bytes == trace.source_receipt_bytes
    proof = json.loads(completed.source_receipt_bytes)
    assert base64.b64decode(proof["journal_original"]["original_bytes_base64"]) == journal_before
    assert proof["outcome"]["reason"] == "terminal_reached_landed_disarmed"
    assert proof["execution_authority"] is False
    assert binding.journal_path.read_bytes() == journal_before
    originals = [
        base64.b64decode(item["original_bytes_base64"]) for item in proof["received_observations"]
    ]
    assert len(originals) == 7
    frame = request.model_input.frames[0]
    next_context = replace(frame.context, snapshot_id="fixture-next", captured_at_s=10.3)
    next_frame = replace(
        frame, frame_id="fixture-next-rgb", captured_at_s=10.3, context=next_context
    )
    collector.add_observation(next_frame)
    next_input = replace(
        request.model_input,
        frames=(next_frame,),
        ledger=collector.snapshot(),
        requested_at_s=10.3,
        deadline_at_s=11.3,
    )
    messages = serialize_onboard_vlm_messages(
        OnboardVlmRuntimeRequest(
            next_input,
            host_reviewed_receipt_sources=ledger.trusted_receipt_sources,
        )
    )
    metadata = json.loads(messages[1]["content"][-1]["text"])
    assert [item["stage"] for item in metadata["host_reviewed_receipts"]] == [
        "proposed",
        "accepted",
        "executed",
        "completed",
    ]


def test_install_permit_and_model_text_never_create_execution(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    assert collector.receipt_revision == 2
    with pytest.raises(TypeError):
        collector.bind_vehicle_mission(
            trace.proposal_id,
            MissionStartPermit(
                "fixture-tx-1",
                binding.mission.canonical_hash,
                "d" * 64,
            ),
        )
    assert _ingest(collector, trace, binding, 10.1, "MISSION_CURRENT", seq=0) is None
    assert collector.receipt_revision == 2
    with pytest.raises(TypeError):
        collector.ingest_vehicle_message(trace.proposal_id, {"status": "completed"}, now_s=10.2)


@pytest.mark.parametrize("kind", ["missing-field", "completion", "truth", "nan", "duplicate"])
def test_strict_receive_receipts_reject_claims_and_bad_json(kind):
    _, _, _, trace = _setup()
    fake_binding = type(
        "Binding", (), {"clock_epoch": trace.clock_epoch, "mission": trace.revision.mission}
    )()
    observation = _observation(fake_binding, 10.1, "MISSION_CURRENT", seq=0)
    raw = json.loads(observation.source_receipt_bytes)
    if kind == "missing-field":
        del raw["clock_epoch"]
    elif kind == "completion":
        raw["completed"] = True
    elif kind == "truth":
        raw["scenario_truth"] = {}
    elif kind == "nan":
        raw["observed_at_s"] = float("nan")
    payload = json.dumps(raw).encode()
    if kind == "duplicate":
        payload = payload.replace(b'"schema_version": 1', b'"schema_version": 1,"schema_version":1')
    with pytest.raises(ValueError):
        OnboardTelemetryObservation(payload, _sha(payload))


@pytest.mark.parametrize(
    "field,value",
    [
        ("clock_epoch", "different-clock"),
        ("source_system_id", 2),
        ("source_component_id", 2),
        ("source_id", "unregistered"),
        ("mission_sha256", "1" * 64),
        ("transaction_id", "other-tx"),
        ("observed_at_s", 10.0),
    ],
)
def test_source_scope_mission_and_order_fail_closed(tmp_path, field, value):
    collector, _, _, trace, binding = _ready(tmp_path)
    raw = _observation(binding, 10.1, "MISSION_CURRENT", seq=0).to_mapping()
    raw[field] = value
    payload = _bytes(raw)
    with pytest.raises(ValueError):
        collector.ingest_vehicle_message(
            trace.proposal_id, OnboardTelemetryObservation(payload, _sha(payload)), now_s=10.1
        )
    assert collector.receipt_revision == 2
    with pytest.raises(OnboardEvidenceCollectorError, match="faulted"):
        _ingest(collector, trace, binding, 10.2, "MISSION_CURRENT", seq=0)


def test_future_stale_clock_regression_and_duplicate_collision(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    obs = _observation(binding, 10.1, "MISSION_CURRENT", seq=0)
    with pytest.raises(ValueError):
        collector.ingest_vehicle_message(trace.proposal_id, obs, now_s=10.0)
    assert collector.ingest_vehicle_message(trace.proposal_id, obs, now_s=10.1) is None
    assert collector.ingest_vehicle_message(trace.proposal_id, obs, now_s=10.2) is None
    assert collector.receipt_revision == 2
    collision = _observation(
        binding, 10.2, "MISSION_CURRENT", message_id="MISSION_CURRENT-10.1", seq=1
    )
    with pytest.raises(OnboardEvidenceCollectorError, match="collision"):
        collector.ingest_vehicle_message(trace.proposal_id, collision, now_s=10.2)


@pytest.mark.parametrize("stamp,now", [(10.1, 12.0), (9.9, 10.1)])
def test_stale_or_regressed_observations_never_upgrade(tmp_path, stamp, now):
    collector, _, _, trace, binding = _ready(tmp_path)
    with pytest.raises(OnboardEvidenceCollectorError):
        collector.ingest_vehicle_message(
            trace.proposal_id, _observation(binding, stamp, "MISSION_CURRENT", seq=0), now_s=now
        )
    assert collector.receipt_revision == 2


def _queued_execute(collector, trace, binding):
    heartbeat = _observation(binding, 10.10, "HEARTBEAT", base_mode=129, custom_mode=67371008)
    assert collector.ingest_vehicle_message(trace.proposal_id, heartbeat, now_s=10.12) is None
    assert (
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 10.11, "EXTENDED_SYS_STATE", landed_state=2),
            now_s=10.13,
        )
        is None
    )
    return collector.ingest_vehicle_message(
        trace.proposal_id,
        _observation(binding, 10.12, "MISSION_CURRENT", seq=0),
        now_s=10.14,
    )


def test_20ms_queue_preserves_source_and_processing_clocks_through_completion(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    executed = _queued_execute(collector, trace, binding)
    assert executed.stage == "executed"
    assert executed.observed_at_s == 10.14
    proof = json.loads(executed.source_receipt_bytes)
    assert proof["monitor_clock_semantics"] == "host_processing_time_s"
    assert proof["feedback"]["current_observed_at_s"] == 10.14
    assert proof["last_source_observed_at_s"] == 10.12
    assert proof["last_processed_at_s"] == 10.14
    pairs = [(10.10, 10.12), (10.11, 10.13), (10.12, 10.14)]
    for receipt, (source, processed) in zip(proof["received_observations"], pairs, strict=True):
        original = base64.b64decode(receipt["original_bytes_base64"])
        assert _sha(original) == receipt["sha256"]
        assert json.loads(original)["observed_at_s"] == source
        assert receipt["source_observed_at_s"] == source
        assert receipt["processed_at_s"] == processed
    # Ahead REACHED retains source time but canonical confirmation is host time.
    later = [
        (10.20, 10.22, "MISSION_ITEM_REACHED", {"seq": 2}),
        (10.21, 10.23, "MISSION_CURRENT", {"seq": 2}),
        (10.22, 10.24, "HEARTBEAT", {"base_mode": 1, "custom_mode": 67371008}),
        (10.23, 10.25, "EXTENDED_SYS_STATE", {"landed_state": 1}),
    ]
    completed = None
    for source, processed, kind, fields in later:
        completed = collector.ingest_vehicle_message(
            trace.proposal_id, _observation(binding, source, kind, **fields), now_s=processed
        )
    assert completed.stage == "completed"
    assert completed.observed_at_s == 10.25
    proof = json.loads(completed.source_receipt_bytes)
    assert proof["reconciliations"][0]["received_at_s"] == 10.22
    assert proof["reconciliations"][0]["confirmed_at_s"] == 10.23
    assert proof["received_observations"][3]["source_observed_at_s"] == 10.20
    assert collector.receipt_revision == 4


@pytest.mark.parametrize(
    "source,processed,duplicate",
    [(10.09, 10.15, False), (10.13, 10.135, False), (10.10, 10.135, True)],
    ids=["source-regression", "processing-regression", "retry-processing-regression"],
)
def test_source_and_processing_regressions_remain_fail_closed(
    tmp_path, source, processed, duplicate
):
    collector, _, _, trace, binding = _ready(tmp_path)
    _queued_execute(collector, trace, binding)
    observation = (
        _observation(binding, source, "HEARTBEAT", base_mode=129, custom_mode=67371008)
        if duplicate
        else _observation(binding, source, "MISSION_CURRENT", seq=0)
    )
    with pytest.raises(OnboardEvidenceCollectorError, match="clock|order"):
        collector.ingest_vehicle_message(trace.proposal_id, observation, now_s=processed)
    assert collector.receipt_revision == 3
    with pytest.raises(OnboardEvidenceCollectorError, match="faulted"):
        _ingest(collector, trace, binding, 10.3, "MISSION_CURRENT", seq=0)


def test_processing_time_never_freshens_old_original_lifecycle_half(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    assert (
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 10.1, "HEARTBEAT", base_mode=129, custom_mode=67371008),
            now_s=11.59,
        )
        is None
    )
    assert (
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 11.6, "EXTENDED_SYS_STATE", landed_state=2),
            now_s=11.61,
        )
        is None
    )
    assert _ingest(collector, trace, binding, 11.62, "MISSION_CURRENT", seq=0) is None
    assert collector.receipt_revision == 2
    assert (
        _ingest(
            collector, trace, binding, 11.63, "HEARTBEAT", base_mode=129, custom_mode=67371008
        ).stage
        == "executed"
    )


def test_processing_completion_cannot_upgrade_stale_original_landed_disarmed_half(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    _queued_execute(collector, trace, binding)
    assert _ingest(collector, trace, binding, 10.24, "MISSION_CURRENT", seq=2) is None
    assert _ingest(collector, trace, binding, 10.25, "MISSION_ITEM_REACHED", seq=2) is None
    assert (
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 10.26, "HEARTBEAT", base_mode=1, custom_mode=67371008),
            now_s=11.71,
        )
        is None
    )
    assert (
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 11.72, "MISSION_CURRENT", seq=2),
            now_s=11.73,
        )
        is None
    )
    with pytest.raises(OnboardEvidenceCollectorError, match="fresh original source"):
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 11.78, "EXTENDED_SYS_STATE", landed_state=1),
            now_s=11.79,
        )
    assert collector.receipt_revision == 3
    with pytest.raises(OnboardEvidenceCollectorError, match="faulted"):
        _ingest(collector, trace, binding, 11.8, "HEARTBEAT", base_mode=1, custom_mode=67371008)


def test_queue_delay_does_not_extend_original_pending_reached_confirmation_deadline(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    _execute(collector, trace, binding)
    assert (
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 10.2, "MISSION_ITEM_REACHED", seq=2),
            now_s=11.69,
        )
        is None
    )
    with pytest.raises(OnboardEvidenceCollectorError, match="bound mismatch"):
        collector.ingest_vehicle_message(
            trace.proposal_id,
            _observation(binding, 11.7, "MISSION_CURRENT", seq=2),
            now_s=11.71,
        )
    assert collector.receipt_revision == 3


@pytest.mark.parametrize("part", ["raw", "scope", "task", "context", "event", "receipt", "output"])
def test_acceptance_exact_input_and_adapter_receipts_cannot_be_relabelled(part):
    collector, _, _, trace = _setup()
    if part == "raw":
        with pytest.raises(ValueError):
            replace(trace, source_receipt_bytes=b"{}")
        return
    if part == "scope":
        bad = replace(trace, clock_epoch="other")
    elif part == "context":
        bad = replace(trace, context=replace(trace.context, ground_speed_mps=1.0))
    elif part == "event":
        bad = replace(trace, event=replace(trace.event, event_id="other"))
    else:
        revision = trace.revision
        if part == "task":
            revision = replace(
                revision,
                decision=replace(
                    revision.decision,
                    binding=replace(revision.decision.binding, selected_task_id="task-2"),
                ),
            )
        elif part == "receipt":
            revision = replace(revision, task_receipt=None)
        else:
            revision = replace(
                revision,
                decision=replace(
                    revision.decision,
                    model_proposal=replace(revision.decision.model_proposal, reason="different"),
                ),
            )
        payload = _bytes(revision.to_mapping())
        bad = replace(
            trace,
            revision=revision,
            source_receipt_bytes=payload,
            source_receipt_sha256=_sha(payload),
        )
    with pytest.raises(ValueError):
        collector.record_acceptance(bad)
    assert collector.receipt_revision == 1


def test_acceptance_is_idempotent_and_bytes_are_preserved():
    collector, _, _, trace = _setup()
    first = collector.record_acceptance(trace)
    assert collector.record_acceptance(trace) is first
    assert collector.receipt_revision == 2
    with pytest.raises(ValueError, match="already consumed"):
        collector.record_acceptance(replace(trace, observed_at_s=10.08))


def test_old_acceptance_retry_cannot_restore_superseded_mission(tmp_path):
    collector, request, raw, trace, binding = _ready(tmp_path)
    first = collector.snapshot().receipts[-1]
    next_request = replace(
        request,
        model_input=replace(request.model_input, ledger=collector.snapshot(), requested_at_s=10.1),
        host_reviewed_receipt_sources=collector.snapshot().trusted_receipt_sources,
    )
    proposed = collector.record_proposal(
        next_request,
        _raw(next_request, json.loads(raw)["output"]),
        current_scope=collector.scope,
        completed_at_s=10.15,
        inference_latency_ms=50,
    )
    mission = replace(trace.revision.mission, mission_id="new-committed-mission")
    revision = replace(
        trace.revision,
        sequence=2,
        mission=mission,
        decision=replace(
            trace.revision.decision,
            binding=replace(
                trace.revision.decision.binding,
                executable_mission_sha256=mission.canonical_hash,
            ),
        ),
    )
    payload = _bytes(revision.to_mapping())
    newer = replace(
        trace,
        proposal_id=proposed.proposal_id,
        revision=revision,
        source_receipt_bytes=payload,
        source_receipt_sha256=_sha(payload),
        observed_at_s=10.16,
    )
    collector.record_acceptance(newer)
    assert collector.record_acceptance(trace) is first
    assert collector.receipt_revision == 4
    with pytest.raises(OnboardEvidenceCollectorError, match="superseded"):
        _ingest(collector, trace, binding, 10.17, "MISSION_CURRENT", seq=0)


@pytest.mark.parametrize(
    "field",
    [
        "prompt_hash",
        "task_spec_hash",
        "context_snapshot_hash",
        "airframe_profile_hash",
        "vehicle_identity_hash",
    ],
)
def test_journal_binding_and_original_hash_fail_closed(tmp_path, field):
    collector, _, _, trace = _setup()
    collector.record_acceptance(trace)
    binding = _journal(
        tmp_path,
        trace,
        identity_change=lambda identity: replace(
            identity,
            bindings=replace(identity.bindings, **{field: "9" * 64}),
        ),
    )
    with pytest.raises(OnboardEvidenceCollectorError):
        collector.bind_vehicle_mission(trace.proposal_id, binding)
    assert collector.receipt_revision == 2


def test_uncommitted_journal_and_changed_bytes_cannot_bind(tmp_path):
    collector, _, _, trace = _setup()
    collector.record_acceptance(trace)
    binding = _journal(tmp_path, trace, state=MissionTransactionState.AUTHORIZED)
    with pytest.raises(ValueError, match="committed"):
        collector.bind_vehicle_mission(trace.proposal_id, binding)
    with pytest.raises(ValueError, match="bytes/hash"):
        collector.bind_vehicle_mission(trace.proposal_id, replace(binding, journal_sha256="0" * 64))


def test_completion_requires_post_terminal_landed_disarmed_and_no_model_report(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    _execute(collector, trace, binding)
    assert (
        _ingest(collector, trace, binding, 10.2, "HEARTBEAT", base_mode=1, custom_mode=67371008)
        is None
    )
    assert _ingest(collector, trace, binding, 10.21, "EXTENDED_SYS_STATE", landed_state=1) is None
    assert _ingest(collector, trace, binding, 10.22, "MISSION_CURRENT", seq=2) is None
    assert _ingest(collector, trace, binding, 10.23, "MISSION_ITEM_REACHED", seq=2) is None
    assert collector.receipt_revision == 3
    assert (
        _ingest(collector, trace, binding, 10.24, "HEARTBEAT", base_mode=129, custom_mode=67371008)
        is None
    )
    assert _ingest(collector, trace, binding, 10.25, "EXTENDED_SYS_STATE", landed_state=1) is None
    completed = _ingest(
        collector, trace, binding, 10.26, "HEARTBEAT", base_mode=1, custom_mode=67371008
    )
    assert completed.stage == "completed"


def test_ahead_reached_uses_existing_monitor_reconciliation_not_backdated_stamp(tmp_path):
    collector, _, _, trace, binding = _ready(tmp_path)
    _execute(collector, trace, binding)
    assert _ingest(collector, trace, binding, 10.2, "MISSION_ITEM_REACHED", seq=2) is None
    assert _ingest(collector, trace, binding, 10.21, "MISSION_CURRENT", seq=2) is None
    assert (
        _ingest(collector, trace, binding, 10.22, "HEARTBEAT", base_mode=1, custom_mode=67371008)
        is None
    )
    completed = _ingest(collector, trace, binding, 10.23, "EXTENDED_SYS_STATE", landed_state=1)
    proof = json.loads(completed.source_receipt_bytes)
    assert proof["reconciliations"][0]["received_at_s"] == 10.2
    assert proof["reconciliations"][0]["confirmed_at_s"] == 10.21


def test_original_wire_bytes_are_optional_explicit_and_hash_bound(tmp_path):
    _, _, _, trace, binding = _ready(tmp_path)
    raw = _observation(binding, 10.1, "MISSION_CURRENT", seq=0).to_mapping()
    packet = b"fixture wire bytes, not physical telemetry"
    raw.update(
        wire_bytes_base64=base64.b64encode(packet).decode(),
        wire_bytes=len(packet),
        wire_sha256=_sha(packet),
    )
    payload = _bytes(raw)
    observation = OnboardTelemetryObservation(payload, _sha(payload))
    assert base64.b64decode(observation.to_mapping()["wire_bytes_base64"]) == packet
    del raw["wire_sha256"]
    payload = _bytes(raw)
    with pytest.raises(ValueError, match="complete"):
        OnboardTelemetryObservation(payload, _sha(payload))


def test_observation_scope_and_immutable_bytes_cannot_cross_collector():
    collector, request, _, _ = _setup()
    frame = request.model_input.frames[0]
    assert collector.add_observation(frame) is False
    with pytest.raises(ValueError, match="scope"):
        collector.add_observation(
            replace(frame, scope=replace(frame.scope, clock_epoch="different"))
        )
    with pytest.raises(ValueError, match="collision"):
        pixels = b"\x04\x05\x06"
        collector.add_observation(replace(frame, image_bytes=pixels, image_sha256=_sha(pixels)))
    assert not any(
        hasattr(collector, name) for name in ("arm", "send", "execute", "install", "mark_completed")
    )


def _async_acceptance():
    anchor = _anchor()
    collector = OnboardEvidenceCollector(
        anchor.request.model_input.scope,
        expected_system_id=1,
        expected_component_id=1,
        vehicle_identity_sha256=VEHICLE_HASH,
    )
    collector.add_observation(anchor.request.model_input.frames[0])
    request = replace(
        anchor.request,
        model_input=replace(
            anchor.request.model_input,
            ledger=collector.snapshot(),
        ),
    )
    parsed = parse_onboard_vlm_runtime_response(
        anchor.result.raw_output,
        request,
        current_scope=request.model_input.scope,
        completed_at_s=anchor.result.completed_at_s,
        inference_latency_ms=anchor.result.inference_latency_ms,
    )
    result = replace(anchor.result, request_sha256=request.canonical_hash, response=parsed)
    anchor = replace(anchor, request=request, result=result)
    proposed = collector.record_proposal(
        request,
        result.raw_output,
        current_scope=request.model_input.scope,
        completed_at_s=result.completed_at_s,
        inference_latency_ms=result.inference_latency_ms,
    )
    current = _current(anchor)
    admission = admit_anchored_intent(
        anchor, current, now_s=2.2, mission_id="fixture-new", policy=POLICY
    )
    proposal = admission.proposal()
    compilation = admission.compilation
    context, event = current.context, current.event
    binding = TaskPolicyRuntimeBinding(
        "multirotor",
        context.canonical_hash,
        context.airframe_profile_hash,
        "select",
        "software admitted fixture",
        anchor.task.task_id,
        None,
        None,
        compilation.plan.canonical_hash,
        compilation.executable_mission_hash,
        compilation.wire_hash,
        True,
        True,
        produced_at_s=context.captured_at_s,
    )
    decision = OnboardDecision(
        request.model_input.scope.prompt_sha256,
        context.canonical_hash,
        event.canonical_hash,
        proposal.response,
        proposal.reason,
        binding,
        model_proposal=proposal,
    )
    task_receipt = OnboardTaskReceipt(
        0,
        OnboardTaskAction.SEARCH,
        event.event_id,
        event.canonical_hash,
        event.observed_at_s,
        context.canonical_hash,
        None,
        None,
        None,
        None,
        None,
        None,
        proposal.model_id,
        proposal.model_version,
        proposal.model_sha256,
        proposal.provider,
        context.gps_fix_type,
        context.gps_horizontal_accuracy_m,
    )
    revision = OnboardMissionRevision(
        2,
        event.kind.value,
        event,
        decision,
        compilation.compiled_mission.executable_mission,
        True,
        False,
        False,
        flight_plan_dispatched=True,
        task_receipt=task_receipt,
    )
    original = _bytes(revision.to_mapping())
    trace = OnboardAcceptanceTrace(
        proposed.proposal_id,
        revision,
        context,
        event,
        original,
        _sha(original),
        2.2,
        request.model_input.scope.clock_epoch,
        execution_admission=admission,
    )
    return collector, request, trace


def test_async_acceptance_keeps_original_source_and_effective_plan_separate_in_next_input():
    collector, request, trace = _async_acceptance()
    receipt = collector.record_acceptance(trace)
    source = json.loads(receipt.source_receipt_bytes)
    assert (
        base64.b64decode(source["original_revision"]["original_bytes_base64"])
        == trace.source_receipt_bytes
    )
    original_raw = base64.b64decode(source["original_model_response"]["original_bytes_base64"])
    assert original_raw == collector.snapshot().receipts[0].source_receipt_bytes
    provenance = source["execution_admission"]
    assert provenance["source"]["original_output"]["flight_plan"]["north_offset_m"] == 20
    assert provenance["effective_plan"]["north_offset_m"] == pytest.approx(17.0, abs=0.001)
    assert provenance["source"]["source"]["context_sha256"] != trace.context.canonical_hash
    assert provenance["execution_snapshot"]["context_sha256"] == trace.context.canonical_hash
    assert source["effective_plan_origin"] == "host_geodetic_reprojection"
    assert source["original_model_output_rewritten"] is False
    frame = request.model_input.frames[0]
    next_frame = replace(
        frame,
        frame_id="next-actual-fixture-rgb",
        captured_at_s=2.3,
        context=replace(trace.context, captured_at_s=2.3),
    )
    collector.add_observation(next_frame)
    next_input = replace(
        request.model_input,
        frames=(next_frame,),
        ledger=collector.snapshot(),
        requested_at_s=2.3,
        deadline_at_s=3.3,
    )
    messages = serialize_onboard_vlm_messages(
        OnboardVlmRuntimeRequest(
            next_input,
            host_reviewed_receipt_sources=collector.snapshot().trusted_receipt_sources,
        )
    )
    metadata = json.loads(messages[1]["content"][-1]["text"])
    accepted_fact = metadata["host_reviewed_receipts"][1]
    assert accepted_fact["source_receipt"]["execution_admission"] == provenance
    assert [item.stage for item in collector.snapshot().receipts] == ["proposed", "accepted"]


@pytest.mark.parametrize("change", ["fake-type", "plan", "current", "result", "wire", "policy"])
def test_fake_or_relabelled_async_admission_cannot_create_accepted_receipt(change):
    collector, _, trace = _async_acceptance()
    admission = trace.execution_admission
    if change == "fake-type":
        with pytest.raises(TypeError):
            replace(trace, execution_admission=object())
        return
    if change == "plan":
        admission = replace(
            admission, effective_plan=replace(admission.effective_plan, north_offset_m=0)
        )
    elif change == "current":
        admission = replace(
            admission,
            current=replace(
                admission.current,
                context=replace(
                    admission.current.context,
                    ground_speed_mps=7,
                ),
            ),
        )
    elif change == "result":
        # Deliberately bypass frozen dataclass construction: collector must reopen it.
        object.__setattr__(
            admission.anchor.result, "raw_output", admission.anchor.result.raw_output + "x"
        )
    elif change == "wire":
        object.__setattr__(admission.compilation.compiled_mission, "wire_items", ())
    else:
        admission = replace(
            admission, policy=replace(admission.policy, trusted_clearance_source_ids=())
        )
    with pytest.raises((ValueError, TypeError)):
        collector.record_acceptance(replace(trace, execution_admission=admission))
    assert collector.receipt_revision == 1


def test_identical_proposal_retry_is_not_a_new_memory_or_model_execution():
    collector, request, raw, _ = _setup()
    receipt = collector.record_proposal(
        request, raw, current_scope=collector.scope, completed_at_s=10.05, inference_latency_ms=50.0
    )
    assert receipt is collector.snapshot().receipts[0]
    assert collector.receipt_revision == 1
    with pytest.raises(ValueError):
        collector.record_proposal(
            request,
            raw.replace("fixture proposal", "other proposal"),
            current_scope=collector.scope,
            completed_at_s=10.05,
            inference_latency_ms=50.0,
        )


def _pending_request():
    original, _, _, scope = _source()
    collector = OnboardEvidenceCollector(scope, expected_system_id=1, expected_component_id=1)
    collector.add_observation(original.model_input.frames[0])
    request = replace(
        original, model_input=replace(original.model_input, ledger=collector.snapshot())
    )
    return collector, request


def test_pending_source_frame10_then_new1001_then_result1005_is_a_real_prefix():
    collector, request = _pending_request()
    frame = request.model_input.frames[0]
    collector.add_observation(
        replace(
            frame,
            frame_id="new-10.01",
            captured_at_s=10.01,
            context=replace(frame.context, captured_at_s=10.01),
        )
    )
    receipt = collector.record_proposal(
        request,
        _raw(request),
        current_scope=collector.scope,
        completed_at_s=10.05,
        inference_latency_ms=50,
    )
    assert receipt.frame_ids == (frame.frame_id,)
    assert len(collector.snapshot().frames) == 2
    assert receipt.stage == "proposed"
    assert collector.receipt_revision == 1


@pytest.mark.parametrize(
    "change", ["foreign-frame", "skipped-prefix", "trusted-sources", "reordered"]
)
def test_foreign_skipped_or_reordered_source_memory_cannot_become_proposed(change):
    collector, request = _pending_request()
    frame = request.model_input.frames[0]
    later = replace(
        frame,
        frame_id="next",
        captured_at_s=10.01,
        context=replace(frame.context, captured_at_s=10.01),
    )
    collector.add_observation(later)
    frames = (frame,)
    inputs = (frame,)
    sources = collector.snapshot().trusted_receipt_sources
    if change == "foreign-frame":
        pixels = b"\x04\x05\x06"
        frames = inputs = (replace(frame, image_bytes=pixels, image_sha256=_sha(pixels)),)
    elif change == "skipped-prefix":
        frames = inputs = (later,)
    elif change == "trusted-sources":
        sources = ()
    else:
        with pytest.raises(ValueError):
            MissionEvidenceLedger(
                collector.scope, frames=(later, frame), trusted_receipt_sources=sources
            )
        assert collector.receipt_revision == 0
        return
    ledger = MissionEvidenceLedger(collector.scope, frames=frames, trusted_receipt_sources=sources)
    bad = replace(
        request,
        model_input=replace(
            request.model_input, frames=inputs, ledger=ledger, requested_at_s=10.02
        ),
    )
    with pytest.raises(OnboardEvidenceCollectorError, match="prefix"):
        collector.record_proposal(
            bad,
            _raw(bad),
            current_scope=collector.scope,
            completed_at_s=10.05,
            inference_latency_ms=30,
        )
    assert collector.receipt_revision == 0


def test_receipt_prefix_must_include_exact_original_wrapper_and_source_bytes():
    collector, request, _, _ = _setup()
    receipt = collector.snapshot().receipts[0]
    wrapper = json.loads(receipt.receipt_bytes)
    fake_source = b"{}"
    wrapper["source_receipt_sha256"] = _sha(fake_source)
    encoded = _bytes(wrapper)
    counterfeit = replace(
        receipt,
        receipt_bytes=encoded,
        receipt_sha256=_sha(encoded),
        source_receipt_bytes=fake_source,
    )
    bad_ledger = replace(collector.snapshot(), receipts=(counterfeit,))
    bad = replace(
        request, model_input=replace(request.model_input, ledger=bad_ledger, requested_at_s=10.1)
    )
    with pytest.raises(OnboardEvidenceCollectorError, match="prefix"):
        collector.record_proposal(
            bad,
            _raw(bad),
            current_scope=collector.scope,
            completed_at_s=10.15,
            inference_latency_ms=50,
        )
    assert collector.receipt_revision == 1


def test_observation_registry_is_bounded_and_never_silently_truncates_citations():
    collector, request = _pending_request()
    frame = request.model_input.frames[0]
    for index in range(1, 64):
        stamp = 10 + index / 1000
        collector.add_observation(
            replace(
                frame,
                frame_id=f"frame-{index}",
                captured_at_s=stamp,
                context=replace(frame.context, captured_at_s=stamp),
            )
        )
    with pytest.raises(OnboardEvidenceCollectorError, match="full"):
        collector.add_observation(
            replace(
                frame,
                frame_id="extra",
                captured_at_s=11,
                context=replace(frame.context, captured_at_s=11),
            )
        )
    assert len(collector.snapshot().frames) == 64
    assert collector.snapshot().frames[0] == frame


def test_failed_receipt_commit_faults_advanced_monitor_instead_of_replaying_it(
    tmp_path, monkeypatch
):
    collector, _, _, trace, binding = _ready(tmp_path)
    assert (
        _ingest(collector, trace, binding, 10.1, "HEARTBEAT", base_mode=129, custom_mode=67371008)
        is None
    )
    assert _ingest(collector, trace, binding, 10.11, "EXTENDED_SYS_STATE", landed_state=2) is None

    def failed_commit(*args):
        raise OnboardEvidenceCollectorError("fixture receipt commit failure")

    monkeypatch.setattr(collector, "_append", failed_commit)
    with pytest.raises(OnboardEvidenceCollectorError, match="commit failure"):
        _ingest(collector, trace, binding, 10.12, "MISSION_CURRENT", seq=0)
    with pytest.raises(OnboardEvidenceCollectorError, match="faulted"):
        _ingest(collector, trace, binding, 10.13, "MISSION_CURRENT", seq=0)
    assert collector.receipt_revision == 2

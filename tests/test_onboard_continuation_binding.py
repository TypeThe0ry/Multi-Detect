from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from test_onboard_realtime_bridge import _bridge, _context, _runtime, _task

from multidetect.autonomy.contracts import GeoPoint, MissionCommand
from multidetect.autonomy.onboard_continuation import (
    CONTINUATION_ADAPTER_HASH,
    CONTINUATION_ADAPTER_ID,
    compile_multirotor_continuation,
)
from multidetect.autonomy.onboard_continuation_binding import (
    RECEIVE_CONTRACT,
    ContinuationChildEvidence,
    ContinuationParentEvidence,
    ContinuationReceivedFact,
)
from multidetect.autonomy.onboard_evidence_collector import (
    TELEMETRY_CONTRACT,
    OnboardMissionTelemetryBinding,
    OnboardTelemetryObservation,
)
from multidetect.autonomy.onboard_multimodal import OnboardMultimodalScope
from multidetect.autonomy.serialization import canonical_json, canonical_sha256
from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionJournalIdentity,
    MissionTransactionState,
)
from multidetect.mission_planning import parse_mission_compile_runtime
from multidetect.px4_mission import (
    PX4_AUTO_MISSION_CUSTOM_MODE,
    CompiledPx4Mission,
    MissionTransactionBindings,
    compile_px4_mission_items,
    px4_wire_hash,
)

VEHICLE_HASH = "e" * 64
SOURCE = "fixture-original-receiver"
TX = "fixture-parent-transaction"
NOW_UTC = datetime(2026, 10, 7, tzinfo=timezone.utc)


def _bytes(raw):
    return canonical_json(raw).encode()


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _fact(raw, kind=ContinuationReceivedFact):
    payload = _bytes(raw)
    return kind(payload, _sha(payload))


def _change(fact, *, message=None, **changes):
    raw = fact.to_mapping()
    raw.update(changes)
    if message:
        raw["message"].update(message)
    return _fact(raw, type(fact))


def _journal(
    tmp_path, mission, task, context, scope, *, state=None, identity_changes=None, runtime=None
):
    _, safety = parse_mission_compile_runtime(
        _runtime() if runtime is None else runtime, "multirotor"
    )
    bindings = MissionTransactionBindings(
        scope.prompt_sha256,
        task.canonical_hash,
        mission.platform_plan_hash,
        context.canonical_hash,
        mission.airframe_profile_hash,
        safety.geofence_hash,
        VEHICLE_HASH,
        "2" * 64,
        "3" * 64,
    )
    identity = MissionJournalIdentity(
        TX,
        mission.canonical_hash,
        px4_wire_hash(compile_px4_mission_items(mission)),
        "4" * 64,
        bindings,
        len(mission.items),
        mission.airframe_adapter_id,
    )
    if identity_changes:
        identity = identity_changes(identity)
    path = tmp_path / "original-parent-journal.jsonl"
    with DurableMissionJournal.create(path, identity, now_utc=NOW_UTC) as journal:
        if state is not MissionTransactionState.AUTHORIZED:
            journal.record_transition(
                MissionTransactionState.UPLOADING,
                payload={
                    "protocol_hash": identity.protocol_hash,
                    "item_count": identity.mission_item_count,
                },
                now_utc=NOW_UTC,
            )
            for index in range(identity.mission_item_count):
                journal.record_item_request(index, now_utc=NOW_UTC)
            journal.record_transition(
                MissionTransactionState.UPLOADED,
                payload={
                    "ack_type": 0,
                    "mission_type": 0,
                    "source_system": 1,
                    "source_component": 1,
                },
                now_utc=NOW_UTC,
            )
            journal.record_transition(
                MissionTransactionState.READBACK_VERIFIED,
                payload={
                    "protocol_hash": identity.protocol_hash,
                    "item_count": identity.mission_item_count,
                },
                now_utc=NOW_UTC,
            )
            journal.record_transition(
                MissionTransactionState.COMMITTED,
                payload={"protocol_hash": identity.protocol_hash},
                now_utc=NOW_UTC,
            )
            if state is not MissionTransactionState.COMMITTED:
                journal.record_transition(
                    MissionTransactionState.START_PERMITTED,
                    payload={
                        "transaction_id": identity.transaction_id,
                        "executable_mission_hash": identity.executable_mission_hash,
                        "protocol_hash": identity.protocol_hash,
                    },
                    now_utc=NOW_UTC,
                )
    payload = path.read_bytes()
    return OnboardMissionTelemetryBinding(
        mission,
        task,
        path,
        len(payload),
        _sha(payload),
        VEHICLE_HASH,
        context.captured_at_s,
        scope.clock_epoch,
    )


def _receive(scope, binding, at_s, message, message_id, *, mission=None, transaction_id=TX):
    normalized = (
        OnboardMultimodalScope.from_inference_scope(scope)
        if not isinstance(scope, OnboardMultimodalScope)
        else scope
    )
    return _fact(
        {
            "schema_version": 1,
            "contract": RECEIVE_CONTRACT,
            "message_id": message_id,
            "scope_sha256": normalized.canonical_hash,
            "session_id": scope.session_id,
            "aircraft_id": scope.aircraft_id,
            "observed_at_s": at_s,
            "clock_epoch": scope.clock_epoch,
            "source_id": SOURCE,
            "source_system_id": 1,
            "source_component_id": 1,
            "vehicle_identity_sha256": VEHICLE_HASH,
            "mission_sha256": (mission or binding.mission).canonical_hash,
            "transaction_id": transaction_id,
            "message": message,
        }
    )


def _proof_for(
    tmp_path, context, scope, parentmission, *, journal_context=None, task=None, runtime=None
):
    """Software fixture with actual journal/wire identities, never vehicle proof.

    Cross-module tests must supply the real task and prepare ContextSnapshot if
    different from this fixture. All received values are explicit test facts.
    """
    task = _task() if task is None else task
    journal_context = (
        replace(
            context,
            snapshot_id="fixture-original-context",
            captured_at_s=context.captured_at_s - 0.1,
        )
        if journal_context is None
        else journal_context
    )
    binding = _journal(tmp_path, parentmission, task, journal_context, scope, runtime=runtime)
    at_s = context.captured_at_s
    telemetry = []
    for offset, kind, body in (
        (-0.03, "HEARTBEAT", {"base_mode": 129, "custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE}),
        (-0.02, "EXTENDED_SYS_STATE", {"landed_state": 2}),
        (-0.01, "MISSION_CURRENT", {"seq": 1}),
    ):
        telemetry.append(
            _fact(
                {
                    "schema_version": 1,
                    "contract": TELEMETRY_CONTRACT,
                    "message_id": f"fixture-{kind}",
                    "observed_at_s": at_s + offset,
                    "clock_epoch": scope.clock_epoch,
                    "source_id": SOURCE,
                    "source_system_id": 1,
                    "source_component_id": 1,
                    "mission_sha256": parentmission.canonical_hash,
                    "transaction_id": TX,
                    "message": {"message_type": kind, **body},
                },
                OnboardTelemetryObservation,
            )
        )
    # Explicit synthetic HOME AMSL fixture, not manufactured production telemetry.
    home_alt_mm = (
        round(context.home_position.altitude_m * 1000)
        if context.home_position.altitude_reference == "amsl"
        and context.home_position.altitude_m is not None
        else 2000
    )
    relative_alt_mm = round(context.aircraft_position.altitude_m * 1000)
    navigation = (
        _receive(
            scope,
            binding,
            at_s - 0.05,
            {
                "message_type": "HOME_POSITION",
                "latitude": round(context.home_position.latitude_deg * 1e7),
                "longitude": round(context.home_position.longitude_deg * 1e7),
                "altitude": home_alt_mm,
                "time_usec": 987654321,
            },
            "fixture-home",
        ),
        _receive(
            scope,
            binding,
            at_s - 0.04,
            {
                "message_type": "PARAM_VALUE",
                "param_id": "MIS_TKO_LAND_REQ",
                "param_type": 6,
                "param_count": 100,
                "param_index": 25,
                "parameter_encoding": "bytewise",
                "raw_value_hex": "00000000",
            },
            "fixture-parameter",
        ),
        _receive(
            scope,
            binding,
            at_s,
            {
                "message_type": "GLOBAL_POSITION_INT",
                "lat": round(context.aircraft_position.latitude_deg * 1e7),
                "lon": round(context.aircraft_position.longitude_deg * 1e7),
                "alt": home_alt_mm + relative_alt_mm,
                "relative_alt": relative_alt_mm,
                "time_boot_ms": 73400,
            },
            "fixture-position",
        ),
    )
    return ContinuationParentEvidence(
        scope,
        binding,
        journal_context,
        context,
        tuple(telemetry),
        navigation,
        (SOURCE,),
    )


def _parent_evidence(tmp_path):
    bridge, _, candidates, _ = _bridge()
    original = _context(1.0)
    mission = bridge.prepare(original).mission
    current = replace(
        _context(2.0, altitude_m=35.0),
        aircraft_position=GeoPoint(1.3001, 103.80005, 35.0, "relative_home"),
    )
    scope = bridge.event_loop.adapter.executor.model_session_scope(current)
    return _proof_for(
        tmp_path, current, scope, mission, journal_context=original, task=candidates[0].task
    )


def _validate(proof, now_s=2.1):
    return proof.validate_binding(
        current_scope=proof.scope,
        current_context=(
            proof.parent.current_context
            if isinstance(proof, ContinuationChildEvidence)
            else proof.current_context
        ),
        now_s=now_s,
    )


def _child(tmp_path):
    parent = _parent_evidence(tmp_path)
    compilation = compile_multirotor_continuation(
        parent.mission_binding.task,
        parent.current_context,
        GeoPoint(1.3003, 103.80005, 35.0, "relative_home"),
        runtime=_runtime(),
        mission_id="fixture-child-mission",
        parent=_validate(parent),
        now_s=2.0,
        source_intent_sha256="5" * 64,
    )
    compiled = compilation.compiled_mission
    receives = [
        _receive(
            parent.scope,
            parent.mission_binding,
            2.01,
            {
                "message_type": "MISSION_COUNT",
                "count": 3,
                "mission_type": 0,
                "target_system": 255,
                "target_component": 190,
            },
            "fixture-child-count",
            mission=compiled.executable_mission,
            transaction_id="fixture-child-tx",
        )
    ]
    for index, item in enumerate(compiled.wire_items):
        receives.append(
            _receive(
                parent.scope,
                parent.mission_binding,
                2.02 + index * 0.01,
                {
                    "message_type": "MISSION_ITEM_INT",
                    **item.to_mapping(),
                    "target_system": 255,
                    "target_component": 190,
                    "current": int(index == 1),
                },
                f"fixture-child-item-{index}",
                mission=compiled.executable_mission,
                transaction_id="fixture-child-tx",
            )
        )
    cursor = _receive(
        parent.scope,
        parent.mission_binding,
        2.05,
        {"message_type": "MISSION_CURRENT", "seq": 1},
        "fixture-child-cursor",
        mission=compiled.executable_mission,
        transaction_id="fixture-child-tx",
    )
    return ContinuationChildEvidence(
        parent.scope,
        parent,
        compiled,
        tuple(receives),
        cursor,
        "fixture-child-tx",
        (SOURCE,),
        255,
        190,
    )


def test_original_parent_reopens_scope_journal_and_actual_receive_bytes(tmp_path):
    proof = _parent_evidence(tmp_path)
    original = proof.mission_binding.journal_path.read_bytes()
    parent = _validate(proof)
    assert parent.parent_mission_id == proof.mission_binding.mission.mission_id
    assert parent.parent_mission_sha256 == proof.mission_binding.mission.canonical_hash
    assert parent.parent_wire_sha256 == px4_wire_hash(
        compile_px4_mission_items(proof.mission_binding.mission)
    )
    assert parent.parent_journal_sha256 == _sha(original)
    assert parent.observed_at_s == 1.99  # Not refreshed to processing now=2.1.
    assert parent.observed_current_sequence == 1
    raw = proof.to_mapping()
    assert base64.b64decode(raw["journal_original"]["original_bytes_base64"]) == original
    for source, sealed in zip(proof.navigation_facts, raw["navigation_original"], strict=True):
        assert base64.b64decode(sealed["original_bytes_base64"]) == source.source_receipt_bytes
        assert sealed["sha256"] == source.source_receipt_sha256
    assert proof.canonical_hash == canonical_sha256(raw)
    assert raw["source_authentication"] == "host_owned_receiver_not_proven_by_hash"
    for key in (
        "execution_authority",
        "transport_created",
        "parameter_writes_performed",
        "replacement_parent_chain_verified",
        "generation_cancellation_verified",
    ):
        assert raw[key] is False
    assert _validate(proof) == parent
    assert proof.mission_binding.journal_path.read_bytes() == original


@pytest.mark.parametrize("now", [1.9, 3.46, float("nan"), float("inf"), True])
def test_parent_context_and_each_receiver_stamp_freshness_not_freshened(tmp_path, now):
    with pytest.raises(ValueError):
        _validate(_parent_evidence(tmp_path), now)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "unregistered"},
        {"source_system_id": 2},
        {"source_component_id": 2},
        {"clock_epoch": "foreign"},
        {"mission_sha256": "a" * 64},
        {"transaction_id": "foreign"},
        {"observed_at_s": 2.2},
        {"observed_at_s": 0.9},
        {"message_id": "fixture-home"},
    ],
)
def test_airborne_receiver_identity_order_and_clock_fail_closed(tmp_path, changes):
    proof = _parent_evidence(tmp_path)
    telemetry = list(proof.telemetry)
    telemetry[1] = _change(telemetry[1], **changes)
    with pytest.raises(ValueError):
        _validate(replace(proof, telemetry=tuple(telemetry)))


@pytest.mark.parametrize(
    "index,body",
    [
        (0, {"base_mode": 1}),
        (0, {"base_mode": 128}),
        (0, {"custom_mode": 0}),
        (1, {"landed_state": 1}),
        (2, {"seq": 0}),
        (2, {"seq": 999}),
    ],
)
def test_actual_armed_mode_inair_and_navigation_cursor_required(tmp_path, index, body):
    proof = _parent_evidence(tmp_path)
    observations = list(proof.telemetry)
    observations[index] = _change(observations[index], message=body)
    with pytest.raises(ValueError):
        _validate(replace(proof, telemetry=tuple(observations)))


@pytest.mark.parametrize(
    "changes",
    [
        {"scope_sha256": "a" * 64},
        {"session_id": "foreign"},
        {"aircraft_id": "foreign"},
        {"vehicle_identity_sha256": "a" * 64},
        {"clock_epoch": "foreign"},
        {"transaction_id": "foreign"},
        {"mission_sha256": "a" * 64},
        {"source_id": "unregistered"},
        {"source_system_id": 2},
        {"source_component_id": 2},
        {"observed_at_s": 2.2},
        {"observed_at_s": 1.92},
        {"message_id": "fixture-HEARTBEAT"},
    ],
)
def test_navigation_session_vehicle_scope_and_order_bound(tmp_path, changes):
    proof = _parent_evidence(tmp_path)
    navigation = list(proof.navigation_facts)
    navigation[1] = _change(navigation[1], **changes)
    with pytest.raises(ValueError):
        _validate(replace(proof, navigation_facts=tuple(navigation)))


@pytest.mark.parametrize(
    "index,body",
    [
        (0, {"latitude": 13100000}),
        (0, {"altitude": 5000}),
        (2, {"lat": 13100000}),
        (2, {"relative_alt": 34000}),
        (2, {"alt": 35000}),
        (1, {"param_id": "OTHER"}),
        (1, {"param_type": 9}),
        (1, {"raw_value_hex": "03000000"}),
        (
            1,
            {
                "raw_value_hex": struct.pack("<f", float("nan")).hex(),
                "parameter_encoding": "c_cast",
            },
        ),
    ],
)
def test_home_altitude_param_original_measurements_must_match(tmp_path, index, body):
    proof = _parent_evidence(tmp_path)
    navigation = list(proof.navigation_facts)
    navigation[index] = _change(navigation[index], message=body)
    with pytest.raises(ValueError):
        _validate(replace(proof, navigation_facts=tuple(navigation)))


def test_explicit_ccast_zero_and_distinct_sensor_clocks_are_not_host_clock(tmp_path):
    proof = _parent_evidence(tmp_path)
    navigation = list(proof.navigation_facts)
    navigation[0] = _change(navigation[0], message={"time_usec": 2**63})
    navigation[1] = _change(navigation[1], message={"parameter_encoding": "c_cast"})
    navigation[2] = _change(navigation[2], message={"time_boot_ms": 2**32 - 1})
    assert _validate(replace(proof, navigation_facts=tuple(navigation))) == _validate(proof)


def test_position_requires_exact_context_time_not_timestamp_nearest(tmp_path):
    proof = _parent_evidence(tmp_path)
    navigation = (
        *proof.navigation_facts[:2],
        _change(proof.navigation_facts[2], observed_at_s=2.001),
    )
    with pytest.raises(ValueError, match="differs from context"):
        _validate(replace(proof, navigation_facts=navigation))


@pytest.mark.parametrize(
    "field",
    [
        "prompt_sha256",
        "selected_task_id",
        "aircraft_id",
        "home_sha256",
        "clock_epoch",
        "session_id",
    ],
)
def test_current_scope_change_needs_new_actual_bound_proof(tmp_path, field):
    proof = _parent_evidence(tmp_path)
    value = "a" * 64 if "sha256" in field else "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValueError):
        proof.validate_binding(
            current_scope=replace(proof.scope, **{field: value}),
            current_context=proof.current_context,
            now_s=2.1,
        )


def test_empty_trust_registry_is_not_permission_from_hash(tmp_path):
    with pytest.raises(ValueError):
        _validate(replace(_parent_evidence(tmp_path), trusted_source_ids=()))


@pytest.mark.parametrize("field", ["telemetry", "navigation_facts", "trusted_source_ids"])
def test_mutable_external_aliases_rejected(tmp_path, field):
    proof = _parent_evidence(tmp_path)
    with pytest.raises(ValueError):
        replace(proof, **{field: list(getattr(proof, field))})


def test_original_journal_mutation_is_detected_on_hash_and_revalidation(tmp_path):
    proof = _parent_evidence(tmp_path)
    path = proof.mission_binding.journal_path
    # Only test-owned temporary data is mutated; original repository artifacts are never touched.
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="original bytes"):
        _validate(proof)
    with pytest.raises(ValueError, match="original bytes"):
        _ = proof.canonical_hash


@pytest.mark.parametrize(
    "state", [MissionTransactionState.AUTHORIZED, MissionTransactionState.COMMITTED]
)
def test_permit_and_intact_complete_parent_journal_required(tmp_path, state):
    proof = _parent_evidence(tmp_path)
    separate = tmp_path / "incomplete"
    separate.mkdir()
    binding = _journal(
        separate,
        proof.mission_binding.mission,
        proof.mission_binding.task,
        proof.journal_context,
        proof.scope,
        state=state,
    )
    with pytest.raises(ValueError, match="START_PERMITTED"):
        _validate(replace(proof, mission_binding=binding))


@pytest.mark.parametrize(
    "field",
    [
        "prompt_hash",
        "task_spec_hash",
        "context_snapshot_hash",
        "vehicle_identity_hash",
        "airframe_profile_hash",
    ],
)
def test_journal_identity_not_merely_start_permitted_state(tmp_path, field):
    proof = _parent_evidence(tmp_path)
    separate = tmp_path / "mismatch"
    separate.mkdir()
    binding = _journal(
        separate,
        proof.mission_binding.mission,
        proof.mission_binding.task,
        proof.journal_context,
        proof.scope,
        identity_changes=lambda identity: replace(
            identity, bindings=replace(identity.bindings, **{field: "a" * 64})
        ),
    )
    with pytest.raises(ValueError, match="identity"):
        _validate(replace(proof, mission_binding=binding))


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b"{}"])
def test_receive_strict_json_has_no_claimed_bool_fallback(raw):
    with pytest.raises(ValueError):
        ContinuationReceivedFact(raw, _sha(raw))


def test_original_receive_sha_wire_provenance_and_extra_bool_rejected(tmp_path):
    fact = _parent_evidence(tmp_path).navigation_facts[0]
    with pytest.raises(ValueError, match="SHA"):
        ContinuationReceivedFact(fact.source_receipt_bytes, "a" * 64)
    with pytest.raises(ValueError):
        _change(fact, confirmed=True)
    with pytest.raises(ValueError):
        _change(fact, wire_bytes=1)
    packet = b"fixture-original-decoded-packet"
    checked = _change(
        fact,
        wire_bytes=len(packet),
        wire_sha256=_sha(packet),
        wire_bytes_base64=base64.b64encode(packet).decode(),
    )
    assert checked.to_mapping()["wire_sha256"] == _sha(packet)
    with pytest.raises(ValueError):
        _change(checked, wire_sha256="a" * 64)


def test_real_new_compiler_child_readback_cursor_stays_receive_only(tmp_path):
    proof = _child(tmp_path)
    original = proof.parent.mission_binding.journal_path.read_bytes()
    mission = proof.compiled_mission.executable_mission
    assert mission.airframe_adapter_id == CONTINUATION_ADAPTER_ID
    assert mission.airframe_adapter_hash == CONTINUATION_ADAPTER_HASH
    assert mission.airframe_adapter_id != proof.parent.mission_binding.mission.airframe_adapter_id
    result = _validate(proof)
    assert result["resume_sequence"] == 1
    assert result["child_wire_sha256"] == proof.compiled_mission.wire_hash
    assert result["parent_evidence_sha256"] == proof.parent.canonical_hash
    assert result["readback_execution_current_normalized"] is True
    assert result["readback_directional_target_normalized"] is True
    assert result["readback_cursor_sequence"] == 1
    for field in (
        "execution_authority",
        "transport_created",
        "child_commit_permit_verified",
        "resume_auto_mission_verified",
        "generation_cancellation_verified",
    ):
        assert result[field] is False
    assert proof.parent.mission_binding.journal_path.read_bytes() == original


@pytest.mark.parametrize("changes", [{"seq": 0}, {"seq": 2}, {"seq": 999}])
def test_child_cursor_exactly_one_not_takeoff_terminal_or_arbitrary(tmp_path, changes):
    proof = _child(tmp_path)
    with pytest.raises(ValueError):
        _validate(replace(proof, cursor_fact=_change(proof.cursor_fact, message=changes)))


@pytest.mark.parametrize(
    "field,value",
    [("x", 13100000), ("command", 22), ("current", 0), ("z", 34.0), ("autocontinue", 0)],
)
def test_child_readback_exact_original_wire_including_current_flags(tmp_path, field, value):
    proof = _child(tmp_path)
    readback = list(proof.readback_facts)
    with pytest.raises(ValueError):
        readback[2] = _change(readback[2], message={field: value})
        _validate(replace(proof, readback_facts=tuple(readback)))


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at_s": 2.2},
        {"observed_at_s": 1.8},
        {"mission_sha256": "a" * 64},
        {"transaction_id": TX},
        {"vehicle_identity_sha256": "a" * 64},
        {"scope_sha256": "a" * 64},
        {"source_id": "foreign"},
        {"message_id": "fixture-child-item-0"},
    ],
)
def test_child_cursor_same_actual_vehicle_generation_and_order(tmp_path, changes):
    proof = _child(tmp_path)
    with pytest.raises(ValueError):
        _validate(replace(proof, cursor_fact=_change(proof.cursor_fact, **changes)))


@pytest.mark.parametrize("adapter_field", ["airframe_adapter_id", "airframe_adapter_hash"])
def test_child_exact_continuation_adapter_not_old_or_forged(tmp_path, adapter_field):
    proof = _child(tmp_path)
    mission = replace(
        proof.compiled_mission.executable_mission,
        **{adapter_field: "a" * 64 if "hash" in adapter_field else "foreign"},
    )
    compiled = CompiledPx4Mission(mission, compile_px4_mission_items(mission))
    with pytest.raises(ValueError, match="child must"):
        _validate(replace(proof, compiled_mission=compiled))


def test_replacement_parent_cannot_silently_reuse_ordinary_journal_contract(tmp_path):
    proof = _child(tmp_path)
    binding = replace(
        proof.parent.mission_binding, mission=proof.compiled_mission.executable_mission
    )
    with pytest.raises(ValueError, match="ordinary multirotor|RESUMED chain"):
        _validate(replace(proof.parent, mission_binding=binding))


def test_mapping_roundtrip_does_not_depend_on_reordered_json_or_secret_metadata(tmp_path):
    proof = _parent_evidence(tmp_path)
    mapping = json.loads(canonical_json(proof.to_mapping()))
    assert mapping["trusted_source_ids"] == [SOURCE]
    assert "secret" not in mapping
    assert mapping["journal_identity"]["executable_mission_hash"] == (
        proof.mission_binding.mission.canonical_hash
    )


@pytest.mark.parametrize("field", ["protocol_hash", "airframe_adapter_id", "mission_item_count"])
def test_journal_full_actual_mission_protocol_and_count_identity(tmp_path, field):
    proof = _parent_evidence(tmp_path)
    separate = tmp_path / "identity-actual"
    separate.mkdir()
    value = (
        "a" * 64
        if field == "protocol_hash"
        else len(proof.mission_binding.mission.items) - 1
        if field == "mission_item_count"
        else "foreign-adapter"
    )
    binding = _journal(
        separate,
        proof.mission_binding.mission,
        proof.mission_binding.task,
        proof.journal_context,
        proof.scope,
        identity_changes=lambda identity: replace(identity, **{field: value}),
    )
    with pytest.raises(ValueError, match="identity"):
        _validate(replace(proof, mission_binding=binding))


@pytest.mark.parametrize(
    "changes",
    [
        {"airframe_adapter_id": "foreign"},
        {"airframe_adapter_hash": "a" * 64},
    ],
)
def test_parent_original_multirotor_adapter_not_any_takeoff_land_strings(tmp_path, changes):
    proof = _parent_evidence(tmp_path)
    mission = replace(proof.mission_binding.mission, **changes)
    with pytest.raises(ValueError, match="ordinary multirotor"):
        _validate(replace(proof, mission_binding=replace(proof.mission_binding, mission=mission)))


@pytest.mark.parametrize("bound_at", [0.99, 2.01])
def test_original_context_binding_and_current_host_time_order(tmp_path, bound_at):
    proof = _parent_evidence(tmp_path)
    with pytest.raises(ValueError, match="clock order"):
        _validate(
            replace(proof, mission_binding=replace(proof.mission_binding, bound_at_s=bound_at))
        )


def test_child_readback_cannot_predate_actual_current_position(tmp_path):
    proof = _child(tmp_path)
    readback = (_change(proof.readback_facts[0], observed_at_s=1.995), *proof.readback_facts[1:])
    with pytest.raises(ValueError, match="predates"):
        _validate(replace(proof, readback_facts=readback))


@pytest.mark.parametrize(
    "command",
    [
        MissionCommand.TAKEOFF,
        MissionCommand.LAND,
        MissionCommand.VTOL_TAKEOFF,
        MissionCommand.VTOL_LAND,
    ],
)
def test_child_no_lifecycle_or_vtol_by_enum_even_with_self_consistent_wire(tmp_path, command):
    proof = _child(tmp_path)
    mission = proof.compiled_mission.executable_mission
    items = (mission.items[0], replace(mission.items[1], command=command), mission.items[2])
    altered = replace(mission, items=items)
    compiled = CompiledPx4Mission(altered, compile_px4_mission_items(altered))
    with pytest.raises(ValueError, match="child must"):
        _validate(replace(proof, compiled_mission=compiled))


def test_compile_dispatch_lineage_is_explicitly_not_granted(tmp_path):
    assert _validate(_child(tmp_path))["compile_dispatch_causal_lineage_verified"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"parameter_encoding": "unknown"},
        {"raw_value_hex": "0"},
        {"raw_value_hex": "GG000000"},
        {"param_type": True},
        {"param_index": 100},
    ],
)
def test_param_encoding_requires_original_bytes_and_explicit_type(tmp_path, body):
    proof = _parent_evidence(tmp_path)
    with pytest.raises(ValueError):
        _change(proof.navigation_facts[1], message=body)


def test_fixture_actual_geofence_hash_is_not_compile_runtime_or_environment_hash(tmp_path):
    proof = _parent_evidence(tmp_path)
    _, safety = parse_mission_compile_runtime(_runtime(), "multirotor")
    identity = proof.to_mapping()["journal_identity"]
    assert identity["bindings"]["geofence_hash"] == safety.geofence_hash
    assert identity["bindings"]["runtime_descriptor_hash"] == "2" * 64
    assert identity["bindings"]["runtime_provenance_hash"] == "3" * 64


def test_px4_inbound_gcs_target_and_actual_cursor_retained_without_changing_upload(tmp_path):
    proof = _child(tmp_path)
    outbound = proof.compiled_mission.to_mapping()
    originals = tuple(item.source_receipt_bytes for item in proof.readback_facts)
    result = _validate(proof)
    assert result["receiver_identity"] == {"system_id": 255, "component_id": 190}
    assert result["comparison_normalization_fields"] == [
        "target_system",
        "target_component",
        "current",
    ]
    assert result["readback_cursor_sequence"] == 1
    assert [item.to_mapping()["message"]["current"] for item in proof.readback_facts[1:]] == [
        0,
        1,
        0,
    ]
    assert all(
        item.to_mapping()["message"]["target_system"] == 255 for item in proof.readback_facts
    )
    for payload, encoded in zip(originals, result["readback_original"], strict=True):
        assert base64.b64decode(encoded["original_bytes_base64"]) == payload
        assert encoded["sha256"] == _sha(payload)
    assert proof.compiled_mission.to_mapping() == outbound
    assert [item.current for item in proof.compiled_mission.wire_items] == [1, 0, 0]
    assert all(
        item.target_system == item.target_component == 1
        for item in proof.compiled_mission.wire_items
    )


@pytest.mark.parametrize("state", [(1, 0, 0), (0, 1, 0)])
def test_readback_before_or_after_cursor_selection_with_later_real_cursor_one(tmp_path, state):
    proof = _child(tmp_path)
    receipts = (
        proof.readback_facts[0],
        *(
            _change(item, message={"current": value})
            for item, value in zip(proof.readback_facts[1:], state, strict=True)
        ),
    )
    result = _validate(replace(proof, readback_facts=receipts))
    assert result["readback_cursor_sequence"] == state.index(1)
    assert result["resume_sequence"] == 1
    assert result["child_wire_sha256"] == proof.compiled_mission.wire_hash
    assert result["resume_auto_mission_verified"] is False


@pytest.mark.parametrize(
    "state", [(0, 0, 0), (1, 1, 0), (0, 0, 1), (1, 0, 1), (-1, 1, 0), (0, 2, 0)]
)
def test_current_flags_not_arbitrarily_erased_to_make_hash_match(tmp_path, state):
    proof = _child(tmp_path)
    with pytest.raises(ValueError):
        receipts = (
            proof.readback_facts[0],
            *(
                _change(item, message={"current": value})
                for item, value in zip(proof.readback_facts[1:], state, strict=True)
            ),
        )
        _validate(replace(proof, readback_facts=receipts))


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "target_system", 254),
        (0, "target_component", 191),
        (1, "target_system", 254),
        (2, "target_component", 191),
        (3, "target_system", 1),
        (1, "target_system", 0),
        (1, "target_component", 256),
        (1, "target_system", True),
    ],
)
def test_receiver_target_checked_before_directional_normalization(tmp_path, index, field, value):
    proof = _child(tmp_path)
    with pytest.raises(ValueError):
        receipts = list(proof.readback_facts)
        receipts[index] = _change(receipts[index], message={field: value})
        _validate(replace(proof, readback_facts=tuple(receipts)))


@pytest.mark.parametrize(
    "system,component",
    [
        (None, None),
        (255, None),
        (None, 190),
        (0, 190),
        (255, 0),
        (True, 190),
        (256, 190),
        (1, 1),
        (254, 190),
        (255, 191),
    ],
)
def test_receiver_is_explicit_known_host_binding_not_self_reported(tmp_path, system, component):
    proof = _child(tmp_path)
    with pytest.raises(ValueError):
        _validate(replace(proof, receiver_system_id=system, receiver_component_id=component))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_system_id": 255},
        {"source_component_id": 190},
        {"source_id": "unregistered"},
        {"vehicle_identity_sha256": "a" * 64},
        {"transaction_id": TX},
        {"mission_sha256": "a" * 64},
    ],
)
def test_correct_gcs_recipient_never_substitutes_actual_px4_sender(tmp_path, changes):
    proof = _child(tmp_path)
    receives = list(proof.readback_facts)
    receives[1] = _change(receives[1], **changes)
    with pytest.raises(ValueError):
        _validate(replace(proof, readback_facts=tuple(receives)))


@pytest.mark.parametrize(
    "changes",
    [{"current": True}, {"sequence": True}, {"frame": 3}, {"unknown": 0}, {"mission_type": 1}],
)
def test_receive_only_item_parser_remains_strict_not_outbound_coercion(tmp_path, changes):
    proof = _child(tmp_path)
    with pytest.raises(ValueError):
        _change(proof.readback_facts[1], message=changes)


def test_actual_wire_float32_precision_still_uses_old_canonical_hash(tmp_path):
    proof = _child(tmp_path)
    receipts = [proof.readback_facts[0]]
    for item in proof.readback_facts[1:]:
        message = item.to_mapping()["message"]
        values = {
            key: struct.unpack("<f", struct.pack("<f", message[key]))[0]
            for key in ("param1", "param2", "param3", "param4", "z")
        }
        receipts.append(_change(item, message=values))
    result = _validate(replace(proof, readback_facts=tuple(receipts)))
    assert result["child_wire_sha256"] == proof.compiled_mission.wire_hash

"""CPU single-owner queues and real temporary journals, never external SIM flight."""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_onboard_teacher_mission_transport_v1 as fixtures

from multidetect.autonomy.contracts import ContextProvenance, ContextValueSource
from multidetect.autonomy.mission_context_admission import ContextV2MissionAdmission
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.clock import HighResolutionSystemClock
from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionJournalIdentity,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from multidetect.px4_external_mission import readback_mission_int, upload_mission_int
from multidetect.px4_mission import Px4MissionTransaction, compile_executable_mission
from multidetect.px4_mission_acceptance import _build_transaction_documents
from scripts import onboard_teacher_mission_execution_v1 as api
from scripts import onboard_teacher_mission_transport_v1 as transfer
from scripts.onboard_teacher_gazebo_session_v4 import ReadOnlyDiagnosticSender
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3
from scripts.run_px4_airframe_transaction_acceptance import _compile_fixture


class Mav(fixtures.Mav):
    def set_mode_send(self, *args):
        self._send("set_mode_send", args)


@pytest.fixture
def harness(tmp_path):
    clock, connection, runtime = fixtures.Clock(), fixtures.Connection(), fixtures.attestation()
    connection.mav = Mav()
    original, platform, geometry, policy = _compile_fixture("multirotor")
    compiled = compile_executable_mission(
        vehicle_class="multirotor",
        platform_plan=platform,
        context_snapshot=original,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash="b" * 64,
    )
    unknown = ContextValueSource("unknown")
    context = replace(
        original,
        schema_version=2,
        captured_at_s=clock(),
        airframe_profile_hash="b" * 64,
        provenance=ContextProvenance("SIM", unknown, unknown, unknown),
        airspeed_mps=None,
        estimated_flight_time_s=None,
        wind_north_mps=None,
        wind_east_mps=None,
    )
    # This CPU issuer is deliberately isolated from the production module.
    # It does not claim actual user approval, runtime, sensors, or flight.
    report, bindings, grant = _build_transaction_documents(
        vehicle_class="multirotor",
        compiled=compiled,
        context_snapshot=context,
        platform_plan=platform,
        policy=policy,
        transaction_id="CPU-normal-teacher-controls",
        now_utc=fixtures.NOW,
    )
    core = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=fixtures.NOW,
    )
    journal = DurableMissionJournal.create(
        tmp_path / "mission.jsonl", identity=core, now_utc=fixtures.NOW
    )
    persistent = PersistentPx4MissionTransaction(core, journal)
    capture = TeacherTelemetryCaptureV3(
        tmp_path / "telemetry",
        clock=clock,
        clock_id=fixtures.CLOCK_ID,
        transaction_id=core._transaction_id,
        local_origin_id="CPU-unqualified-no-flight-origin",
    )
    holder = {}
    diagnostics = ReadOnlyDiagnosticSender(connection, clock, fixtures.CLOCK_ID, lambda: 60)

    def dispatch(commands):
        if commands and type(commands[0]) is api._ExecutionCommand:
            return holder["execution"]._dispatch(commands)
        if commands and type(commands[0]) is transfer._SendCommand:
            return holder["transport"]._dispatch(commands)
        return diagnostics(commands)

    receiver = transfer._ConcurrentReceiverV6(
        lambda timeout: connection.recv_match(blocking=True, timeout=timeout),
        clock,
        request_sender=dispatch,
    ).start()
    transport = transfer.TeacherMissionTransport(
        receiver=receiver,
        connection=connection,
        compiled=compiled,
        transaction=persistent,
        capture=capture,
        intent=fixtures.intent_for(persistent, compiled, runtime, clock),
        runtime_attestation=runtime,
        clock=clock,
        clock_id=fixtures.CLOCK_ID,
        remaining=lambda: 60,
        utc_clock=lambda: fixtures.NOW,
    )
    holder["transport"] = transport
    plan = persistent.begin_upload(now_utc=fixtures.NOW)
    # Real CPU protocol interleaving uses the same finite helpers and dual journals.
    h = SimpleNamespace(
        clock=clock,
        connection=connection,
        runtime=runtime,
        compiled=compiled,
        context=context,
        persistent=persistent,
        capture=capture,
        receiver=receiver,
        transport=transport,
        plan=plan,
        tmp_path=tmp_path,
        messages=[],
    )

    def enqueue(kind, **kwargs):
        message = fixtures.RawMessage(kind, **kwargs)

        def before_decode():
            assert capture._commits["raw"]["verified"] > 0
            assert threading.get_ident() == receiver._owner

        message.before_decode = before_decode
        h.messages.append(message)
        connection.inbox.put(message)
        return message

    h.enqueue = enqueue
    fixtures.automatic_px4(h, plan)
    upload_mission_int(
        transport.connection,
        persistent,
        plan,
        receive=transport.receive,
        clock=clock,
        now_utc=fixtures.NOW,
        gcs_identity=(255, 190),
        message_timeout_s=0.1,
    )
    readback_mission_int(
        transport.connection,
        persistent,
        plan,
        receive=transport.receive,
        clock=clock,
        now_utc=fixtures.NOW,
        gcs_identity=(255, 190),
        message_timeout_s=0.1,
    )
    persistent.commit(now_utc=fixtures.NOW)
    permit = persistent.permit_start(now_utc=fixtures.NOW)
    at = clock()
    admission = ContextV2MissionAdmission(
        context_snapshot_sha256=context.canonical_hash,
        task_spec_sha256=bindings.task_spec_hash,
        geometry_sha256=canonical_sha256(geometry.to_mapping()),
        safety_policy_sha256=canonical_sha256(policy.to_mapping()),
        airframe_profile_id=context.airframe_profile_id,
        airframe_profile_sha256="b" * 64,
        runtime_attestation_sha256=runtime.canonical_hash,
        planning_qualification_sha256="a" * 64,
        runtime_kind="SIM",
        vehicle_class="multirotor",
        clock_id=fixtures.CLOCK_ID,
        observed_at_s=context.captured_at_s,
        qualified_at_s=at,
        expires_at_s=context.captured_at_s + 2,
    )
    identity = MissionJournalIdentity.from_transaction(core)
    intent = api.TeacherMissionExecutionIntent(
        transaction_id=identity.transaction_id,
        journal_identity_sha256=identity.canonical_hash,
        start_permit_sha256=canonical_sha256(api._permit_mapping(permit)),
        context_snapshot_sha256=context.canonical_hash,
        context_admission_sha256=admission.canonical_hash,
        airframe_profile_sha256="b" * 64,
        runtime_attestation_sha256=runtime.canonical_hash,
        container_id=runtime.container_id,
        image=runtime.image,
        clock_id=fixtures.CLOCK_ID,
        approved_at_s=at,
        expires_at_s=at + 8,
        explicitly_authorized=True,
        return_abort_authorized=True,
    )
    inputs = dict(
        transport=transport,
        permit=permit,
        context=context,
        admission=admission,
        runtime_attestation=runtime,
        intent=intent,
        runtime_recheck=lambda: runtime,
        evidence_directory=tmp_path / "execution",
        artifact_root=tmp_path,
        send_timeout_s=0.2,
    )

    def bind(**overrides):
        execution = api.TeacherMissionExecution(**{**inputs, **overrides})
        holder["execution"] = execution
        return execution

    h.bind, h.inputs, h.intent, h.permit, h.admission = bind, inputs, intent, permit, admission
    try:
        yield h
    finally:
        current = holder.get("execution")
        if current:
            current.close()
        transport.close()
        receiver.stop()
        persistent.close()
        if not capture._closed:
            capture.abort("CPU-normal-control-fixture-finished")


def heartbeat(h, *, armed=False, mode=None, **overrides):
    fields = dict(
        type=2,
        autopilot=12,
        base_mode=1 | (128 if armed else 0),
        custom_mode=h.permit.to_set_mode_args()[2] if mode is None else mode,
    )
    return h.enqueue("HEARTBEAT", target=None, **{**fields, **overrides})


def ground(h, state=1):
    return h.enqueue("EXTENDED_SYS_STATE", target=None, landed_state=state, vtol_state=0)


def sensors(h, **overrides):
    fields = dict(
        onboard_control_sensors_present=api._REQUIRED_SENSORS,
        onboard_control_sensors_enabled=api._REQUIRED_SENSORS,
        onboard_control_sensors_health=api._REQUIRED_SENSORS | (1 << 28),
    )
    return h.enqueue("SYS_STATUS", target=None, **{**fields, **overrides})


def ready(h, execution):
    heartbeat(h)
    ground(h)
    sensors(h)
    fixtures.wait_until(lambda: h.receiver._queue.qsize() >= 3)
    execution.poll(0.01)


def automatic_control(
    h, *, ack_result=0, armed_observed=True, abort_terminal=True, interleave=False
):
    def respond(method, args):
        if method == "set_mode_send":
            assert args == h.permit.to_set_mode_args()
            heartbeat(h)
            ground(h)
            sensors(h)
        elif method == "command_long_send" and args[2] == 400:
            assert args == api._ARM_ARGS and args[5] == 0.0
            if interleave:
                h.enqueue("COMMAND_ACK", command=400, result=0, source=(2, 1))
                h.enqueue("COMMAND_ACK", command=400, result=0, target=(0, 0))
                h.enqueue("COMMAND_ACK", command=400, result=0, target=(255, 191))
                h.enqueue("COMMAND_ACK", command=20, result=0)
            h.enqueue("COMMAND_ACK", command=400, result=ack_result)
            if armed_observed:
                heartbeat(h, armed=True)
        elif method == "command_long_send" and args[2] == 20:
            assert args == api._RTL_ARGS
            h.enqueue("COMMAND_ACK", command=20, result=0)
            heartbeat(h, armed=True, mode=api.AUTO_RTL_CUSTOM_MODE)
            if abort_terminal:
                heartbeat(h, armed=False, mode=api.AUTO_RTL_CUSTOM_MODE)
                ground(h)

    h.connection.mav.on_call = respond


def test_full_CPU_upload_commit_permit_normal_arm_abort_and_owned_thread(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h, interleave=True)
    started = execution.start(phase_timeout_s=0.2)
    assert started["armed_state_observed"] is True
    assert not any(started["claims"].values())
    assert h.persistent.state is MissionTransactionState.START_PERMITTED
    ended = execution.abort_return(phase_timeout_s=0.2)
    assert ended["status"] == "return-abort-landed-disarmed-observed"
    assert ended["error"] is None
    owners = {t for _, _, t in h.connection.mav.calls}
    assert owners == set(h.connection.read_threads) and len(owners) == 1
    assert threading.get_ident() not in owners
    controls = [args for method, args, _ in h.connection.mav.calls if method == "command_long_send"]
    assert controls == [api._ARM_ARGS, api._RTL_ARGS]
    for attempt in ended["API_attempts"]:
        assert attempt["API_returned"] is True and attempt["delivery_verified"] is False
        for key in ("intention_receipt", "attempt_receipt", "result_receipt"):
            binding = attempt[key]
            raw = Path(binding["path"]).read_bytes()
            assert len(raw) == binding["bytes"]
            assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    assert all(set(m.decode_threads) == {threading.get_ident()} for m in h.messages)
    assert h.capture._commits["raw"]["verified"] == h.capture._commits["projection"]["verified"]
    execution.close()
    h.transport.close()
    h.receiver.stop()
    assert h.receiver.to_mapping()["thread_exited"] is True
    assert h.receiver.to_mapping()["failure"] is None


@pytest.mark.parametrize(
    "name,value",
    [
        ("journal_identity_sha256", "c" * 64),
        ("start_permit_sha256", "c" * 64),
        ("context_snapshot_sha256", "c" * 64),
        ("context_admission_sha256", "c" * 64),
        ("airframe_profile_sha256", "c" * 64),
        ("runtime_attestation_sha256", "c" * 64),
        ("container_id", "c" * 64),
        ("image", "other@sha256:" + "c" * 64),
        ("clock_id", "foreign-domain"),
        ("transaction_id", "foreign-transaction"),
    ],
)
def test_binding_scope_changes_rejected_before_evidence_or_send(harness, name, value):
    h = harness
    before = copy.deepcopy(h.connection.mav.calls)
    with pytest.raises(ValueError, match="binding differs"):
        h.bind(intent=replace(h.intent, **{name: value}))
    assert h.connection.mav.calls == before
    assert not (h.tmp_path / "execution").exists()


def test_runtime_reinspection_mismatch_refused_before_control(harness):
    h = harness
    with pytest.raises(ValueError, match="reinspection differs"):
        h.bind(runtime_recheck=lambda: replace(h.runtime, container_id="f" * 64))
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_stale_context_denied_without_rewriting_original_time(harness):
    h = harness
    h.clock.offset = 3
    with pytest.raises(ValueError, match="stale"):
        h.bind()
    assert not (h.tmp_path / "execution").exists()


@pytest.mark.parametrize("result", [1, 2, 4, 5])
def test_normal_ARM_rejection_never_becomes_armed_evidence(harness, result):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h, ack_result=result, armed_observed=False)
    with pytest.raises(RuntimeError, match="rejected normal command"):
        execution.start(phase_timeout_s=0.1)
    assert execution.to_mapping()["armed_state_observed"] is False
    assert h.receiver.to_mapping()["failure"] is not None


def test_ACK_acceptance_without_armed_heartbeat_is_not_flight(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h, armed_observed=False)
    with pytest.raises(RuntimeError, match="armed AUTO.MISSION observation timeout"):
        execution.start(phase_timeout_s=0.05)
    assert execution.to_mapping()["armed_state_observed"] is False


@pytest.mark.parametrize("target", [(0, 0), (255, 191), (1, 1)])
def test_broadcast_and_foreign_ACK_do_not_advance(harness, target):
    h = harness
    execution = h.bind()
    ready(h, execution)
    auto = automatic_control(h)
    del auto
    handler = h.connection.mav.on_call

    def respond(method, args):
        if method == "command_long_send" and args[2] == 400:
            h.enqueue("COMMAND_ACK", command=400, result=0, target=target)
            heartbeat(h, armed=True)
        else:
            handler(method, args)

    h.connection.mav.on_call = respond
    with pytest.raises(RuntimeError, match="command 400 ACK observation timeout"):
        execution.start(phase_timeout_s=0.05)
    assert execution.to_mapping()["armed_state_observed"] is False


@pytest.mark.parametrize(
    "field",
    [
        "onboard_control_sensors_health",
        "onboard_control_sensors_present",
        "onboard_control_sensors_enabled",
    ],
)
def test_prearm_sensor_failures_do_not_send_MODE_or_ARM(harness, field):
    h = harness
    execution = h.bind()
    heartbeat(h)
    ground(h)
    sensors(h, **{field: 0})
    fixtures.wait_until(lambda: h.receiver._queue.qsize() >= 3)
    with pytest.raises(ValueError, match="prearm/sensor health"):
        execution.start(phase_timeout_s=0.05)
    assert not any(
        m in {"set_mode_send", "command_long_send"} for m, _, _ in h.connection.mav.calls
    )


def test_send_intent_is_durable_before_API_and_dispatch_not_forgable(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h)
    handler = h.connection.mav.on_call

    def verify(method, args):
        if method in {"set_mode_send", "command_long_send"}:
            number = len(execution._attempts) - 1
            for suffix in ("intent", "attempt"):
                document = json.loads(
                    (h.tmp_path / "execution" / f"control-{number:02d}.{suffix}.json").read_bytes()
                )
                assert document["delivery_verified"] is False
                if suffix == "attempt":
                    assert document["API_attempted"] is False
                    assert document["API_attempted_at_s"] is None
        handler(method, args)

    h.connection.mav.on_call = verify
    execution.start(phase_timeout_s=0.2)
    assert len(execution._attempts) == 2
    forged = api._ExecutionCommand("return-abort", h.clock(), h.clock() + 1, threading.Event(), {})
    with pytest.raises(RuntimeError, match="exclusive RX thread"):
        execution._dispatch((forged,))


def test_API_timeout_has_unknown_delivery_and_no_retry(harness):
    h = harness
    execution = h.bind(send_timeout_s=0.5)
    ready(h, execution)
    release = threading.Event()
    h.connection.mav.on_call = lambda *_: release.wait(2)
    try:
        with pytest.raises(RuntimeError, match="delivery unknown"):
            execution.start(phase_timeout_s=0.1)
        assert execution.to_mapping()["armed_state_observed"] is False
        with pytest.raises(RuntimeError, match="single-use"):
            execution.start(phase_timeout_s=0.1)
    finally:
        release.set()
        fixtures.wait_until(lambda: h.receiver.to_mapping()["thread_exited"])


def test_return_abort_is_active_only_and_terminal_timeout_is_not_success(harness):
    h = harness
    execution = h.bind()
    with pytest.raises(RuntimeError, match="current active"):
        execution.abort_return(phase_timeout_s=0.05)
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_return_without_ground_disarm_is_not_converged(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h, abort_terminal=False)
    execution.start(phase_timeout_s=0.2)
    with pytest.raises(RuntimeError, match="landed and disarmed after RTL"):
        execution.abort_return(phase_timeout_s=0.05)
    assert execution.to_mapping()["status"] != "return-abort-landed-disarmed-observed"


def test_fsync_failure_before_control_API_denies_send(harness, monkeypatch):
    h = harness
    execution = h.bind()
    ready(h, execution)
    import os

    monkeypatch.setattr(os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("CPU-fsync-failure")))
    with pytest.raises(OSError, match="CPU-fsync-failure"):
        execution.start(phase_timeout_s=0.1)
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_changed_GCS_metadata_rejected_before_control(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    h.connection.mav.srcComponent = 191
    with pytest.raises(Exception, match="identity"):
        execution.start(phase_timeout_s=0.1)
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


def test_nonpermitted_transaction_and_wrong_permit_are_refused(harness):
    h = harness
    h.persistent.transaction._state = MissionTransactionState.COMMITTED
    with pytest.raises(ValueError, match="binding differs"):
        h.bind()
    h.persistent.transaction._state = MissionTransactionState.START_PERMITTED
    with pytest.raises(ValueError, match="binding differs"):
        h.bind(permit=replace(h.permit, protocol_hash="f" * 64))


def test_explicit_scope_bools_and_finite_intent_are_strict(harness):
    h = harness
    for value in (False, 1, "true"):
        with pytest.raises((ValueError, TypeError)):
            replace(h.intent, explicitly_authorized=value)
    with pytest.raises(ValueError, match="60 seconds"):
        replace(h.intent, expires_at_s=h.intent.approved_at_s + 61)


def test_production_module_never_imports_fixture_issuer_or_force_controls():
    text = Path(api.__file__).read_text(encoding="utf-8")
    assert "_build_transaction_documents" not in text
    assert "21196" not in text
    assert "run_external_px4_mission_transaction" not in text
    assert api._ARM_ARGS[4:6] == (1.0, 0.0)


def test_original_context_expiring_during_mode_transition_never_arms(harness, monkeypatch):
    h = harness
    execution = h.bind()
    ready(h, execution)
    original_time = h.context.captured_at_s
    automatic_control(h)
    record = execution._record

    def mode_observed(state, row):
        record(state, row)
        if state == "mode-observed":
            h.clock.offset += 2.1

    monkeypatch.setattr(execution, "_record", mode_observed)
    with pytest.raises(ValueError, match="stale"):
        execution.start(phase_timeout_s=0.1)
    assert h.context.captured_at_s == original_time
    assert not any(m == "command_long_send" for m, _, _ in h.connection.mav.calls)
    assert execution.to_mapping()["armed_state_observed"] is False


@pytest.mark.parametrize("missing", ["mode-heartbeat", "post-mode-sensors"])
def test_exact_mode_and_new_mode_health_are_both_required_before_arm(harness, missing):
    h = harness
    execution = h.bind()
    ready(h, execution)

    def respond(method, _):
        if method == "set_mode_send":
            if missing != "mode-heartbeat":
                heartbeat(h)
            if missing != "post-mode-sensors":
                sensors(h)

    h.connection.mav.on_call = respond
    with pytest.raises(RuntimeError, match="AUTO.MISSION grounded prearm"):
        execution.start(phase_timeout_s=0.05)
    assert not any(m == "command_long_send" for m, _, _ in h.connection.mav.calls)


def test_admission_cannot_relabel_original_observation_time(harness):
    h = harness
    changed = replace(
        h.admission,
        observed_at_s=h.admission.observed_at_s - 0.01,
        expires_at_s=h.admission.expires_at_s - 0.01,
    )
    intent = replace(h.intent, context_admission_sha256=changed.canonical_hash)
    with pytest.raises(ValueError, match="binding differs"):
        h.bind(admission=changed, intent=intent)
    assert not (h.tmp_path / "execution").exists()


def test_fresh_runtime_reinspection_changes_after_mode_never_arms(harness):
    h = harness
    current = [h.runtime]
    execution = h.bind(runtime_recheck=lambda: current[0])
    ready(h, execution)
    automatic_control(h)
    handler = h.connection.mav.on_call

    def respond(method, args):
        handler(method, args)
        if method == "set_mode_send":
            current[0] = replace(h.runtime, container_id="d" * 64)

    h.connection.mav.on_call = respond
    with pytest.raises(ValueError, match="reinspection differs"):
        execution.start(phase_timeout_s=0.1)
    assert not any(m == "command_long_send" for m, _, _ in h.connection.mav.calls)


def test_command_queue_overflow_poisoned_before_API(harness, monkeypatch):
    h = harness
    execution = h.bind()
    ready(h, execution)

    def full(_):
        raise queue.Full

    monkeypatch.setattr(h.receiver._commands, "put_nowait", full)
    with pytest.raises(RuntimeError, match="queue exhausted"):
        execution.start(phase_timeout_s=0.1)
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)
    assert execution.to_mapping()["error"] is not None
    assert (h.tmp_path / "execution" / "control-00.intent.json").is_file()


def test_expired_after_attempt_fsync_does_not_enter_API(harness, monkeypatch):
    h = harness
    execution = h.bind()
    ready(h, execution)
    save = execution._save

    def delayed(name, value):
        receipt = save(name, value)
        if name.endswith(".attempt.json"):
            h.clock.offset += 2.1
        return receipt

    monkeypatch.setattr(execution, "_save", delayed)
    with pytest.raises(ValueError, match="stale"):
        execution.start(phase_timeout_s=0.1)
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)
    assert execution.to_mapping()["armed_state_observed"] is False
    assert execution.to_mapping()["API_attempts"][0]["API_attempted"] is False


def test_unsolicited_pre_API_ACK_and_foreign_source_cannot_authorize_arm(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    h.enqueue("COMMAND_ACK", command=400, result=0)
    fixtures.wait_until(lambda: h.receiver._queue.qsize() >= 1)
    execution.poll(0.01)
    automatic_control(h)
    handler = h.connection.mav.on_call

    def respond(method, args):
        if method == "command_long_send" and args[2] == 400:
            h.enqueue("COMMAND_ACK", command=400, result=0, source=(1, 2))
            heartbeat(h, armed=True)
        else:
            handler(method, args)

    h.connection.mav.on_call = respond
    with pytest.raises(RuntimeError, match="command 400 ACK observation timeout"):
        execution.start(phase_timeout_s=0.05)
    assert execution.to_mapping()["armed_state_observed"] is False


def test_latest_disarmed_HEARTBEAT_cannot_be_overridden_by_earlier_armed(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h)
    handler = h.connection.mav.on_call

    def respond(method, args):
        handler(method, args)
        if method == "command_long_send" and args[2] == 400:
            heartbeat(h, armed=False)

    h.connection.mav.on_call = respond
    with pytest.raises(RuntimeError, match="armed AUTO.MISSION observation timeout"):
        execution.start(phase_timeout_s=0.05)
    assert execution.to_mapping()["armed_state_observed"] is False


def test_abort_cannot_control_other_mode_or_replay_after_terminal(harness):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h)
    execution.start(phase_timeout_s=0.1)
    heartbeat(h, armed=True, mode=(3 << 24) | (4 << 16))
    fixtures.wait_until(lambda: h.receiver._queue.qsize() >= 1)
    execution.poll(0.01)
    with pytest.raises(ValueError, match="exact Mission observation"):
        execution.abort_return(phase_timeout_s=0.1)
    assert all(args[2] != 20 for m, args, _ in h.connection.mav.calls if m == "command_long_send")


def test_cancel_stops_unique_owner_and_never_sends_start(harness):
    h = harness
    execution = h.bind()
    execution.cancel()
    with pytest.raises(RuntimeError, match="poisoned"):
        execution.start(phase_timeout_s=0.1)
    h.receiver.stop()
    assert h.receiver.to_mapping()["thread_exited"] is True
    assert not any(m == "set_mode_send" for m, _, _ in h.connection.mav.calls)


@pytest.mark.parametrize("result", [True, "0", 0.0])
@pytest.mark.timing(reason="RX thread and durable journals share a 200ms real-time API deadline")
def test_malformed_source_ACK_does_not_advance(harness, result):
    h = harness
    execution = h.bind()
    ready(h, execution)
    automatic_control(h, ack_result=result, armed_observed=False)
    with pytest.raises(ValueError, match="Malformed COMMAND_ACK"):
        execution.start(phase_timeout_s=0.1)
    assert execution.to_mapping()["armed_state_observed"] is False


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return fixtures.session_fixtures.template.__wrapped__(tmp_path_factory)


@pytest.fixture
def session_plan(tmp_path, template):
    return fixtures.session_fixtures.plan.__wrapped__(tmp_path, template)


def test_V7_owned_lifecycle_reuses_unique_RX_for_finite_transfer_and_controls(
    harness, session_plan, tmp_path, monkeypatch
):
    # NativeDocker owns this fixture; host bridge discovery is not part of it.
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")
    h = harness
    h.transport.close()
    h.receiver.stop()
    clock, connection = HighResolutionSystemClock(), fixtures.Connection()
    connection.mav = Mav()
    docker = fixtures.session_fixtures.helpers.NativeDocker(session_plan)
    backend = fixtures.session_fixtures.helpers.Backend(session_plan, clock)
    connects = []

    def connect(endpoint, **kwargs):
        connects.append((endpoint, kwargs))
        return connection

    session = api.OwnedTeacherGazeboSessionV7(
        session_plan,
        execute=True,
        clock=clock,
        runner=docker,
        connection_factory=connect,
        camera_factory=lambda *_: backend,
        port_probe=lambda _: None,
    )
    capture = TeacherTelemetryCaptureV3(
        tmp_path / "V7-telemetry",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id=h.intent.transaction_id,
        local_origin_id="CPU-unqualified-origin-not-live",
    )
    try:
        with session:
            receiver = session.start_receiver()
            runtime = session.attest_qualified(telemetry_qualified=True)  # CPU Docker only.
            transport = session.bind_mission_transport(
                transaction=h.persistent,
                compiled=h.compiled,
                capture=capture,
                intent=fixtures.intent_for(
                    h.persistent, h.compiled, runtime, clock.monotonic, clock.session_id
                ),
                runtime_attestation=runtime,
                utc_clock=lambda: fixtures.NOW,
            )
            at = clock.monotonic()
            admission = replace(
                h.admission,
                clock_id=clock.session_id,
                runtime_attestation_sha256=runtime.canonical_hash,
                qualified_at_s=at,
            )
            intent = replace(
                h.intent,
                clock_id=clock.session_id,
                context_admission_sha256=admission.canonical_hash,
                runtime_attestation_sha256=runtime.canonical_hash,
                container_id=runtime.container_id,
                image=runtime.image,
                approved_at_s=at,
                expires_at_s=at + 8,
            )
            inputs = {
                **h.inputs,
                "intent": intent,
                "admission": admission,
                "runtime_attestation": runtime,
                "evidence_directory": tmp_path / "V7-execution",
            }
            del inputs["transport"], inputs["runtime_recheck"]
            execution = session.bind_mission_execution(**inputs)
            owned = SimpleNamespace(
                connection=connection, receiver=receiver, permit=h.permit, messages=[]
            )

            def enqueue(kind, **fields):
                message = fixtures.RawMessage(kind, **fields)
                owned.messages.append(message)
                connection.inbox.put(message)
                return message

            owned.enqueue = enqueue
            ready(owned, execution)
            automatic_control(owned)
            execution.start(phase_timeout_s=0.2)
            execution.abort_return(phase_timeout_s=0.2)
            assert transport._receiver is receiver and execution._transport is transport
            assert type(receiver) is transfer._ConcurrentReceiverV6
            with pytest.raises(RuntimeError, match="binding required"):
                session.bind_mission_execution(**inputs)
        report = session.to_mapping()
        assert report["contract"] == "multidetect.onboard-teacher-gazebo-session-v7"
        assert report["mission_execution"]["closed"] is True
        assert report["mission_execution"]["status"] == "return-abort-landed-disarmed-observed"
        assert not any(report["mission_execution"]["claims"].values())
        assert receiver.to_mapping()["thread_exited"] is True
        assert connection.closed is True and docker.current is None
        assert connects[0][1]["source_system"] == 255
        assert connects[0][1]["source_component"] == 190
        assert {owner for _, _, owner in connection.mav.calls} == set(connection.read_threads)
        assert len(set(connection.read_threads)) == 1
    finally:
        if not capture._closed:
            capture.abort("CPU-V7-fixture-finished")

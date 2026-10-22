"""CPU-only byte fixtures, policy admission, telemetry, and live UDP dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_operator import MAX_LOG_DOCUMENT_BYTES, MissionOperatorAdapter
from multidetect.autonomy.mission_operator_protocol import (
    ACK,
    CHUNK,
    CONTROL,
    TELEMETRY_ACK,
    TIGHTEN,
    document_chunks,
    mission_token,
)
from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import AuthenticationKey, sign_document
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
from multidetect.operator_link import SelectionCommandGuard, VideoGeometry
from multidetect.operator_protocol import (
    OperatorProtocolError,
    OperatorTunnelCodec,
    RuntimeStatusAck,
    WireMessageType,
)

KEY = b"x" * 32
GEOMETRY = VideoGeometry("camera-main", 1280, 720)


def codec():
    return OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,))


def document():
    return json.loads(
        Path("configs/onboard/mission-package-example.json").read_text(encoding="utf-8")
    )


def supervisor(tmp_path, *, package=None, now=None, downlink=False):
    package = package or load_mission_package(document())
    now = now or [100.0]
    executor = MissionExecutor(package.plan, loop_factory=lambda *a, **kw: None, refs=package.refs)
    log = OnboardMissionLog(tmp_path, package.mission_id)
    queue = DownlinkQueue(log, None) if downlink else None
    return (
        MissionSupervisor(executor, package, log=log, clock=lambda: now[0])
        if queue is None
        else MissionSupervisor(executor, package, log=log, downlink=queue, clock=lambda: now[0])
    )


def deliver(adapter, kind, body, sequence=1, *, sent_at_s=1.0, peer=None):
    packet = adapter.codec.decode(
        adapter.codec.encode_mission(kind, body, sequence=sequence, sent_at_s=sent_at_s)
    )
    return adapter.handle(packet, peer=peer)


def accepted(adapter, frames):
    return bool(ACK.unpack_from(adapter.codec.decode(frames[0]).message.body)[3])


def upload(adapter, payload, *, token=42, sequence=1):
    replies = []
    for index, message in enumerate(document_chunks(payload, token)):
        replies.append(deliver(adapter, 28, message.body, sequence + index))
    return replies


PACKAGE_KEY = AuthenticationKey("hmac-sha256", b"p" * 32, "default")


def inline(job):
    job()


def uploading(**kwargs):
    """An upload-capable adapter: package key configured, planner run inline."""
    kwargs.setdefault("package_verify_key", PACKAGE_KEY)
    kwargs.setdefault("planner_runner", inline)
    return MissionOperatorAdapter(codec(), **kwargs)


def encoded_document(doc=None, *, signed=True):
    value = doc or document()
    if signed:
        value = sign_document(value, PACKAGE_KEY)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_cpp_golden_v2_abort_and_v1_unchanged():
    wire = codec()
    frame = wire.encode_mission(
        30, CONTROL.pack(0x0102030405060708, 0x1112131415161718), sequence=7, sent_at_s=1.0
    )
    assert (
        frame.hex() == "4d44021e00000000000700000000000003e8001001020304050607081112131415161718"
        "d3491dd7c5ac843f059f9b22c1ac5c03"
    )
    assert wire.decode(frame).message.message_type == 30
    legacy = wire.encode_runtime_status_ack(RuntimeStatusAck(3, 4), sequence=8, sent_at_s=1)
    assert legacy[2] == 1
    assert wire.decode(legacy).message == RuntimeStatusAck(3, 4)
    for original, version in ((frame, 1), (legacy, 2)):
        signed = original[:2] + bytes([version]) + original[3:-16]
        wrong = signed + hmac.digest(KEY, signed, "sha256")[:16]
        with pytest.raises(OperatorProtocolError, match="version"):
            wire.decode(wrong)
    tampered = frame[:24] + bytes([frame[24] ^ 1]) + frame[25:]
    with pytest.raises(OperatorProtocolError, match="authentication"):
        wire.decode(tampered)


@pytest.mark.parametrize(
    "kind,body",
    [
        (30, CONTROL.pack(0, 1)),
        (33, TIGHTEN.pack(1, 1, 5, 0)),
        (33, TIGHTEN.pack(1, 1, 0, float("nan"))),
        (29, ACK.pack(1, 30, 1, 1)),
        (29, ACK.pack(1, 30, 0, 2)),
        (29, ACK.pack(1, 30, 0, 1) + b"\xff"),
        (36, TELEMETRY_ACK.pack(0, 0)),
        (36, TELEMETRY_ACK.pack(1, 1490)),
        (29, ACK.pack(1, 28, 1490, 1)),
        (28, CHUNK.pack(1, 0, 2, 1, b"x" * 32) + b"x"),
        (28, CHUNK.pack(1, 0, 1, 65537, b"x" * 32) + b"x"),
    ],
)
def test_body_bounds_fail_closed(kind, body):
    with pytest.raises((ValueError, UnicodeError)):
        codec().encode_mission(kind, body, sequence=1, sent_at_s=1)


def test_upload_ack_only_after_admission_and_idempotent_final_retry(tmp_path):
    installed = []

    def loader(package):
        value = supervisor(tmp_path, package=package)
        installed.append(value)
        return value

    adapter = uploading(package_loader=loader, preflight_allowed=lambda: True)
    payload = encoded_document()
    messages = list(document_chunks(payload, 42))
    for index, message in enumerate(messages):
        assert accepted(adapter, deliver(adapter, 28, message.body, index + 1))
        assert len(installed) == int(index == len(messages) - 1)
    assert adapter.supervisor is installed[0]
    assert accepted(adapter, deliver(adapter, 28, messages[-1].body, len(messages)))
    assert len(installed) == 1


def test_signed_ground_command_chunks_reach_onboard_verifier(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    command_key = AuthenticationKey("hmac-sha256", KEY, "default")
    active._security_key = command_key
    active._require_signature = True
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    command = sign_document(
        {
            "mission_id": active.package.mission_id,
            "type": "heartbeat",
            "sequence": 1,
            "timestamp": now[0],
            "timestamp_version": 1,
            "timestamp_domain": "monotonic",
        },
        command_key,
    )
    payload = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    messages = list(document_chunks(payload, 77, message_type=37))
    for index, message in enumerate(messages):
        assert accepted(adapter, deliver(adapter, 37, message.body, index + 1))
    assert active.last_heartbeat == now[0]


def test_upload_fails_closed_without_flight_gate_and_armed_transition(tmp_path):
    adapter = MissionOperatorAdapter(codec())
    first = next(document_chunks(encoded_document(), 42))
    assert not accepted(adapter, deliver(adapter, 28, first.body))
    gate = [True]
    calls = []

    def loader(package):
        calls.append(package)
        return supervisor(tmp_path, package=package)

    adapter = uploading(package_loader=loader, preflight_allowed=lambda: gate[0])
    messages = list(document_chunks(encoded_document(), 43))
    for index, message in enumerate(messages[:-1]):
        assert accepted(adapter, deliver(adapter, 28, message.body, index + 1))
    gate[0] = False
    assert not accepted(adapter, deliver(adapter, 28, messages[-1].body, len(messages)))
    assert not calls


@pytest.mark.parametrize("mutation", ["replacement", "in_place", "refs", "plan", "executor"])
def test_loader_cannot_substitute_same_id_or_mutate_admitted_package(tmp_path, mutation):
    def loader(package):
        if mutation == "replacement":
            changed = deepcopy(package.document)
            changed["budget"]["time_s"] += 1
            package = load_mission_package(changed)
        elif mutation == "in_place":
            package.document["link_loss_policy"] = {"action": "continue", "timeout_s": 0}
        elif mutation == "refs":
            package.refs["poi:gate"] = (0.0, 0.0)
        elif mutation == "plan":
            package.document["plan"]["goal"] = "silently replaced goal"
        value = supervisor(tmp_path, package=package)
        if mutation == "executor":
            changed = deepcopy(package.document)
            changed["plan"]["goal"] = "silently replaced executor"
            value.executor.plan = load_mission_package(changed).plan
        return value

    adapter = uploading(package_loader=loader, preflight_allowed=lambda: True)
    assert not accepted(adapter, upload(adapter, encoded_document())[-1])
    assert adapter.supervisor is None


def test_task_only_upload_accepts_onboard_compiled_executor_without_changing_package(tmp_path):
    compiled = load_mission_package(document()).plan
    task = document()
    del task["plan"]
    loaded = []

    def loader(package):
        assert package.plan is None
        executor = MissionExecutor(compiled, loop_factory=lambda *a, **kw: None, refs=package.refs)
        value = MissionSupervisor(
            executor, package, log=OnboardMissionLog(tmp_path, package.mission_id)
        )
        loaded.append(value)
        return value

    adapter = uploading(package_loader=loader, preflight_allowed=lambda: True)
    assert accepted(adapter, upload(adapter, encoded_document(task))[-1])
    assert adapter.supervisor is loaded[0]
    assert adapter.supervisor.package.plan is None
    assert adapter.supervisor.executor.plan == compiled


@pytest.mark.parametrize(
    "payload", [b'{"mission_id":"a","mission_id":"b"}', b'{"x":NaN}', b"[]", b"{}", b"\xff"]
)
def test_invalid_json_or_package_never_installs(payload):
    calls = []
    adapter = uploading(
        package_loader=lambda package: calls.append(package),
        preflight_allowed=lambda: True,
    )
    replies = upload(adapter, payload)
    assert not accepted(adapter, replies[-1])
    assert not calls


def test_factory_failure_and_digest_mismatch_fail_ack():
    def reject(_):
        raise RuntimeError("onboard compile rejected task")

    adapter = uploading(package_loader=reject, preflight_allowed=lambda: True)
    assert not accepted(adapter, upload(adapter, encoded_document())[-1])
    adapter = uploading(package_loader=reject, preflight_allowed=lambda: True)
    messages = list(document_chunks(encoded_document(), 99))
    for index, message in enumerate(messages):
        body = message.body[:16] + b"z" * 32 + message.body[48:]
        reply = deliver(adapter, 28, body, index + 1)
    assert not accepted(adapter, reply)


def test_out_of_order_conflicting_duplicate_stale_sequence_and_timeout():
    now = [100.0]
    adapter = uploading(
        package_loader=lambda p: None, preflight_allowed=lambda: True, clock=lambda: now[0]
    )
    parts = list(document_chunks(encoded_document(), 42))
    assert not accepted(adapter, deliver(adapter, 28, parts[1].body, 1))
    assert accepted(adapter, deliver(adapter, 28, parts[0].body, 2))
    assert accepted(adapter, deliver(adapter, 28, parts[0].body, 2))
    conflict = parts[0].body[:-1] + bytes([parts[0].body[-1] ^ 1])
    assert not accepted(adapter, deliver(adapter, 28, conflict, 2))
    assert not accepted(adapter, deliver(adapter, 28, parts[1].body, 1))
    now[0] += 31
    assert not accepted(adapter, deliver(adapter, 28, parts[1].body, 3))
    assert adapter._upload is None


def test_controls_use_local_clock_and_cannot_relax_policy_or_resume_abort(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    adapter = MissionOperatorAdapter(
        codec(), supervisor=active, clock=lambda: now[0], preflight_allowed=lambda: False
    )
    mission = mission_token(active.package.mission_id)
    assert accepted(adapter, deliver(adapter, 34, CONTROL.pack(mission, 1), 1, sent_at_s=9_000_000))
    assert active.last_heartbeat == 100
    now[0] += 1
    assert accepted(adapter, deliver(adapter, 34, CONTROL.pack(mission, 1), 1))
    assert active.last_heartbeat == 100  # Retransmission is not a new heartbeat.
    assert not accepted(adapter, deliver(adapter, 34, CONTROL.pack(mission ^ 1, 1), 1))
    assert accepted(adapter, deliver(adapter, 31, CONTROL.pack(mission, 2), 2))
    assert active.paused_at == 101
    assert accepted(adapter, deliver(adapter, 32, CONTROL.pack(mission, 3), 3))
    assert active.paused_at is None
    assert not accepted(adapter, deliver(adapter, 33, TIGHTEN.pack(mission, 4, 0, 0), 4))
    assert accepted(adapter, deliver(adapter, 33, TIGHTEN.pack(mission, 5, 2, 10), 5))
    assert active.policy == "hold_then_return"
    assert not accepted(adapter, deliver(adapter, 33, TIGHTEN.pack(mission, 6, 2, 11), 6))
    assert accepted(adapter, deliver(adapter, 30, CONTROL.pack(mission, 7), 7))
    assert accepted(adapter, deliver(adapter, 32, CONTROL.pack(mission, 8), 8))
    assert active.evaluate() == "return_now"


def test_status_contains_actual_estimates_and_restart_safe_sequence(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    adapter = MissionOperatorAdapter(codec(), supervisor=active, preflight_allowed=lambda: False)
    first = adapter.status_document()
    assert first["kind"] == "status" and first["flight_active"] is True
    assert first["remaining_energy_wh"] is None
    active.update_estimates(remaining_time_s=1, remaining_energy_wh=77)
    now[0] += 2
    second = adapter.status_document()
    assert second["remaining_time_s"] == 0
    assert second["remaining_energy_wh"] == 77
    assert second["sequence"] > first["sequence"]


def test_reconnect_uses_forward_sequence_epoch_without_resetting_replay_barrier(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    mission = mission_token(active.package.mission_id)
    assert accepted(adapter, deliver(adapter, 34, CONTROL.pack(mission, 1), 1_000_000, peer="old"))
    now[0] += 6
    # A restarted GCS seeds its serial counter from the later wall-clock epoch.
    assert accepted(adapter, deliver(adapter, 34, CONTROL.pack(mission, 2), 1_006_000, peer="new"))
    now[0] += 1
    assert not accepted(
        adapter, deliver(adapter, 31, CONTROL.pack(mission, 3), 1_000_001, peer="new")
    )
    assert active.paused_at is None
    assert active.last_heartbeat == 106


def test_large_log_has_bounded_summary_and_full_content_hash(tmp_path):
    active = supervisor(tmp_path)
    adapter = MissionOperatorAdapter(codec(), supervisor=active)
    record = {
        "kind": "decision",
        "mission_id": active.package.mission_id,
        "sequence": 17,
        "action": "return_now",
        "reason": "reserve reached",
        "large_payload": "数据" * 100000,
    }
    pending = adapter._new_telemetry(record)
    payload = b"".join(message.body[CHUNK.size :] for message in pending.messages)
    decoded = json.loads(payload)
    original = json.dumps(
        record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(payload) <= MAX_LOG_DOCUMENT_BYTES
    assert decoded["mission_id"] == active.package.mission_id and decoded["sequence"] == 17
    assert decoded["action"] == "return_now" and decoded["reason"] == "reserve reached"
    assert decoded["detail_truncated"] is True
    assert decoded["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert decoded["original_bytes"] == len(original)
    assert len(pending.messages) <= 35


def test_telemetry_stop_wait_ack_retry_and_persistent_log_cursor(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    sent = []
    adapter.tick(sent.append)
    assert len(sent) == 1
    adapter.tick(sent.append)
    assert len(sent) == 1
    now[0] += 1
    adapter.tick(sent.append)
    assert (
        len(sent) == 2
        and adapter.codec.decode(sent[0]).message.body == adapter.codec.decode(sent[1]).message.body
    )
    pending = adapter._telemetry
    adapter._next_status = float("inf")
    parts = []
    while adapter._telemetry is pending:
        packet = adapter.codec.decode(sent[-1])
        token, index, _, _, _ = CHUNK.unpack_from(packet.message.body)
        parts.append(packet.message.body[CHUNK.size :])
        deliver(adapter, 36, TELEMETRY_ACK.pack(token, index), 1)
        adapter.tick(sent.append)
    status = json.loads(b"".join(parts))
    assert status["kind"] == "status"
    adapter._next_status = float("inf")
    outcome = []
    record = {
        "kind": "decision",
        "mission_id": active.package.mission_id,
        "sequence": 2,
        "action": "hold",
    }
    worker = threading.Thread(target=lambda: outcome.append(adapter.send_log(record)))
    worker.start()
    while worker.is_alive():
        adapter.tick(sent.append)
        pending = adapter._telemetry
        if pending is not None:
            deliver(adapter, 36, TELEMETRY_ACK.pack(pending.token, pending.index), 1)
        worker.join(timeout=0.001)
    assert outcome == [True]
    adapter.close()


def test_signed_mavlink_udp_endpoint_dispatches_mission_messages(tmp_path):
    pytest.importorskip("pymavlink")
    from multidetect.operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
    from multidetect.operator_udp import UdpOperatorSelectionServer

    def transport(endpoint):
        return OperatorMavlinkTunnelAdapter(
            codec(),
            endpoint,
            signing_key=b"U" * 32,
            signing_link_id=endpoint.local_component_id,
            initial_signing_timestamp=4_000_000,
        )

    onboard = transport(OperatorMavlinkEndpoint(1, 191, 255, 190))
    ground = transport(OperatorMavlinkEndpoint(255, 190, 1, 191))
    active = supervisor(tmp_path)
    mission = MissionOperatorAdapter(onboard.codec, supervisor=active)
    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=onboard,
        guard=SelectionCommandGuard(GEOMETRY),
        receive_timeout_s=1,
        mission_adapter=mission,
    ) as server:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.settimeout(1)
            payload = ground.codec.encode_mission(
                31,
                CONTROL.pack(mission_token(active.package.mission_id), 5),
                sequence=1,
                sent_at_s=1,
            )
            channel.sendto(
                ground.wrap_authenticated_operator_payload(payload), server.bound_address
            )
            server.serve_once()
            frame = channel.recv(512)
            packet = ground.codec.decode(
                ground.decode_authenticated_datagram(frame).operator_payload
            )
            assert packet.message_type == WireMessageType.MISSION_ACK
            assert ACK.unpack_from(packet.message.body)[3] == 1
            assert active.paused_at is not None


def test_downlink_cursor_advances_only_after_all_chunk_acknowledgements(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now, downlink=True)
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    active.ground_event({"mission_id": active.package.mission_id, "type": "heartbeat"})
    active.update_estimates(remaining_energy_wh=70)
    received = []

    def sender(frame):
        packet = adapter.codec.decode(frame)
        token, index, _, _, _ = CHUNK.unpack_from(packet.message.body)
        received.append((token, index))
        deliver(adapter, 36, TELEMETRY_ACK.pack(token, index))

    try:
        deadline = time.monotonic() + 3
        while active.downlink.acknowledged_sequence < 3 and time.monotonic() < deadline:
            adapter.tick(sender)
            threading.Event().wait(0.002)
        assert active.downlink.acknowledged_sequence == 3
        cursor = json.loads(active.log.cursor_path.read_text(encoding="utf-8"))
        assert cursor["sequence"] == 3
        active.downlink.close()
        restored = DownlinkQueue(active.log, None)
        try:
            assert restored.acknowledged_sequence == 3
        finally:
            restored.close()
    finally:
        adapter.close()
        active.downlink.close()


def _queue_log(adapter, record):
    worker = threading.Thread(target=adapter.send_log, args=(record,), daemon=True)
    worker.start()
    deadline = time.monotonic() + 2
    while adapter._queued_log is None and time.monotonic() < deadline:
        time.sleep(0.002)
    assert adapter._queued_log is not None
    return worker


@pytest.mark.parametrize(("kind", "first"), [("finding", "finding"), ("decision", "status")])
def test_priority_log_does_not_wait_behind_a_due_status(tmp_path, kind, first):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    frames = []
    adapter.tick(frames.append)  # binds the sender; sends the first status
    adapter._telemetry = None
    adapter._last_was_status = False
    adapter._next_status = 0.0  # status is due again
    worker = _queue_log(adapter, {"mission_id": "m", "sequence": 9, "kind": kind})
    try:
        adapter.tick(frames.append)
        token, index, count, length, _ = CHUNK.unpack_from(
            adapter.codec.decode(frames[-1]).message.body
        )
        pending = adapter._telemetry
        assert pending is not None and index == 0
        document = b"".join(message.body[CHUNK.size :][:44] for message in pending.messages)[
            :length
        ]
        assert json.loads(document)["kind"] == first
    finally:
        adapter.close()
        worker.join(2)


def test_telemetry_acks_have_their_own_rate_bucket(tmp_path):
    pytest.importorskip("pymavlink")
    from multidetect.operator_mavlink import OperatorMavlinkEndpoint, OperatorMavlinkTunnelAdapter
    from multidetect.operator_udp import UdpOperatorSelectionServer

    onboard = OperatorMavlinkTunnelAdapter(
        codec(),
        OperatorMavlinkEndpoint(1, 191, 255, 190),
        signing_key=b"U" * 32,
        signing_link_id=191,
        initial_signing_timestamp=4_000_000,
    )
    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=onboard,
        guard=SelectionCommandGuard(GEOMETRY),
        receive_timeout_s=1,
    ) as server:
        peer = ("127.0.0.1", 40000)
        heartbeats = sum(
            server._allow_command(peer, WireMessageType.MISSION_HEARTBEAT, now_s=5.0)
            for _ in range(100)
        )
        acks = sum(
            server._allow_command(peer, WireMessageType.MISSION_TELEMETRY_ACK, now_s=5.0)
            for _ in range(100)
        )
    # A 1 Hz status is ~20 chunks; at 20 ACKs/s the dropped ACKs stalled the
    # downlink on its 1 s resend (SITL run7).
    assert heartbeats == 20 and acks == 100


def test_unsigned_package_is_refused_even_without_a_configured_key(tmp_path):
    calls = []

    def loader(package):
        calls.append(package)
        return supervisor(tmp_path, package=package)

    adapter = uploading(package_loader=loader, preflight_allowed=lambda: True)
    assert not accepted(adapter, upload(adapter, encoded_document(signed=False))[-1])
    keyless = MissionOperatorAdapter(
        codec(), package_loader=loader, preflight_allowed=lambda: True, planner_runner=inline
    )
    assert not accepted(keyless, upload(keyless, encoded_document(signed=False), token=43)[-1])
    assert not accepted(keyless, upload(keyless, encoded_document(), token=44)[-1])
    assert not calls and adapter.supervisor is None and keyless.supervisor is None
    assert accepted(adapter, upload(adapter, encoded_document(), token=45, sequence=100)[-1])
    assert len(calls) == 1


def _command(adapter, document, *, token, sequence):
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    replies = [
        deliver(adapter, 37, message.body, sequence + index)
        for index, message in enumerate(document_chunks(payload, token, message_type=37))
    ]
    return accepted(adapter, replies[-1])


def test_plan_commands_take_the_signed_ground_event_path(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    command_key = AuthenticationKey("hmac-sha256", KEY, "default")
    active._security_key = command_key
    active.requires_approval, active.plan_approved = True, False
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    unsigned = {
        "mission_id": active.package.mission_id,
        "type": "approve_plan",
        "plan_sha256": active.plan_sha256,
    }
    assert not _command(adapter, unsigned, token=1, sequence=1)
    assert not active.plan_approved

    def signed(sequence, **fields):
        return sign_document(
            {
                **unsigned,
                "sequence": sequence,
                "timestamp": now[0],
                "timestamp_version": 1,
                "timestamp_domain": "monotonic",
                **fields,
            },
            command_key,
        )

    assert not _command(adapter, signed(1, mission_id="other"), token=2, sequence=20)
    assert not _command(adapter, signed(2, plan_sha256="0" * 64), token=3, sequence=40)
    assert not active.plan_approved
    assert _command(adapter, signed(3), token=4, sequence=60)
    assert active.plan_approved


def test_candidate_is_installed_only_after_recheck_and_approval_gate(tmp_path):
    gate = [True]
    installed = []

    def loader(package):
        value = supervisor(tmp_path, package=package)
        gate[0] = False  # the aircraft armed while the plan was compiling
        return value

    adapter = uploading(
        package_loader=loader,
        preflight_allowed=lambda: gate[0],
        package_installer=installed.append,
        requires_approval=True,
    )
    assert not accepted(adapter, upload(adapter, encoded_document())[-1])
    assert not installed and adapter.supervisor is None

    seen = []

    def install(value):
        seen.append((value.requires_approval, value.plan_approved))
        installed.append(value)

    adapter = uploading(
        package_loader=lambda package: supervisor(tmp_path / "ok", package=package),
        preflight_allowed=lambda: True,
        package_installer=install,
        requires_approval=True,
    )
    assert accepted(adapter, upload(adapter, encoded_document(), token=50)[-1])
    assert seen == [(True, False)] and adapter.supervisor is installed[0]


def test_sealed_adapter_refuses_new_and_in_flight_packages(tmp_path):
    adapter = uploading(
        package_loader=lambda package: supervisor(tmp_path, package=package),
        preflight_allowed=lambda: True,
    )
    adapter.seal()
    assert not accepted(adapter, upload(adapter, encoded_document())[-1])
    assert adapter.supervisor is None
    jobs = []
    adapter = uploading(
        package_loader=lambda package: supervisor(tmp_path / "late", package=package),
        preflight_allowed=lambda: True,
        planner_runner=jobs.append,
    )
    assert accepted(adapter, upload(adapter, encoded_document(), token=60)[-1])
    adapter.seal()
    jobs[0]()
    assert adapter.supervisor is None


def test_planning_runs_off_the_receive_thread(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path / "current", now=now)
    release = threading.Event()

    def slow_loader(package):
        release.wait(10)
        return supervisor(tmp_path / "new", package=package)

    adapter = MissionOperatorAdapter(
        codec(),
        supervisor=active,
        package_loader=slow_loader,
        preflight_allowed=lambda: True,
        package_verify_key=PACKAGE_KEY,
        clock=lambda: now[0],
    )
    messages = list(document_chunks(encoded_document(), 70))
    try:
        for index, message in enumerate(messages[:-1]):
            assert accepted(adapter, deliver(adapter, 28, message.body, index + 1))
        replies = []
        receive = threading.Thread(
            target=lambda: replies.append(deliver(adapter, 28, messages[-1].body, len(messages)))
        )
        receive.start()
        receive.join(2)
        assert not receive.is_alive(), "planning blocked the receive thread"
        assert accepted(adapter, replies[0])
        mission = mission_token(active.package.mission_id)
        assert accepted(adapter, deliver(adapter, 34, CONTROL.pack(mission, 1), len(messages) + 1))
        assert adapter.supervisor is active
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while adapter.supervisor is active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter.supervisor is not active
    sent = []
    adapter.tick(sent.append)
    verdicts = [
        ACK.unpack_from(packet.message.body)
        for packet in map(adapter.codec.decode, sent)
        if packet.message_type == 29
    ]
    assert verdicts == [(70, 28, len(messages) - 1, 1)]
    adapter.close()


def test_oversized_status_is_bounded_not_dropped(tmp_path):
    now = [100.0]
    active = supervisor(tmp_path, now=now)
    active.reason = "原因" * 400
    for index in range(80):
        active.executor.counts[f"count_step_{index:03d}"] = index
    adapter = MissionOperatorAdapter(codec(), supervisor=active, clock=lambda: now[0])
    sent = []
    adapter.tick(sent.append)  # raised ValueError: status silently stopped
    pending = adapter._telemetry
    assert sent and pending is not None
    payload = b"".join(message.body[CHUNK.size :] for message in pending.messages)
    status = json.loads(payload)
    assert len(payload) <= 1200
    assert status["kind"] == "status" and status["truncated"] is True
    assert status["mission_id"] == active.package.mission_id
    adapter.close()


def test_final_summary_pages_fit_the_byte_budget_for_chinese_text(tmp_path):
    active = supervisor(tmp_path)
    adapter = MissionOperatorAdapter(codec(), supervisor=active)
    summary = {"findings": ["发现一个橙色锥桶" * 200, 'quote " and \\ slash' * 50]}
    pending = adapter._new_telemetry(
        {"kind": "final_summary", "mission_id": active.package.mission_id, "summary": summary}
    )
    pages, offset = [], 0
    while offset < len(pending.messages):
        _token, _index, count, length, _digest = CHUNK.unpack_from(pending.messages[offset].body)
        raw = b"".join(
            message.body[CHUNK.size :] for message in pending.messages[offset : offset + count]
        )
        assert len(raw) == length <= MAX_LOG_DOCUMENT_BYTES
        pages.append(json.loads(raw))
        offset += count
    assert len(pages) > 1 and {page["pages"] for page in pages} == {len(pages)}
    assert json.loads("".join(page["body"] for page in pages)) == summary


def test_status_of_a_rejected_plan_is_not_awaiting_approval(tmp_path):
    active = supervisor(tmp_path)
    active.requires_approval, active.plan_approved = True, False
    adapter = MissionOperatorAdapter(codec(), supervisor=active)
    assert adapter.status_document()["state"] == "awaiting_approval"
    assert active.reject_plan(active.plan_sha256)
    status = adapter.status_document()
    assert status["state"] == "returning" and status["action"] == "land_now"

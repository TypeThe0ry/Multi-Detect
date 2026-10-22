"""Mission operator adapter for the existing authenticated Jetson endpoint.

Upload admission is deliberately dependency injected: the aircraft runtime must
provide an authoritative disarmed/preflight gate and a package loader which
compiles task text on board and returns the actually installed supervisor.
Absent either dependency, uploads fail closed. No network operation runs on the
flight executor thread. Telemetry retries run from the endpoint's receive loop;
durable log delivery waits only in DownlinkQueue's existing daemon worker.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, RLock
from typing import Any

from multidetect.operator_protocol import DecodedOperatorPacket, OperatorTunnelCodec

from .mission_operator_protocol import (
    ACK,
    CHUNK,
    CONTROL,
    MAX_COMMAND_DOCUMENT_BYTES,
    MAX_DOCUMENT_BYTES,
    TELEMETRY_ACK,
    TIGHTEN,
    MissionWireMessage,
    document_chunks,
    mission_token,
)
from .mission_package import POLICIES, MissionPackage, load_mission_package
from .mission_supervisor import MissionSupervisor
from .serialization import strict_json_loads

MAX_LOG_DOCUMENT_BYTES = 1536
MAX_STATUS_DOCUMENT_BYTES = 1200


@dataclass
class _Upload:
    token: int
    count: int
    total: int
    digest: bytes
    touched: float
    parts: list[bytes] = field(default_factory=list)


@dataclass
class _CommandUpload:
    token: int
    count: int
    total: int
    digest: bytes
    touched: float
    parts: list[bytes] = field(default_factory=list)


@dataclass
class _Telemetry:
    messages: list[MissionWireMessage]
    token: int
    complete: Event = field(default_factory=Event)
    index: int = 0
    attempts: int = 0
    sent: float = float("-inf")
    accepted: bool = False


class MissionOperatorAdapter:
    """Bounded, replay-aware mission session, independent of MAVLink framing.

    ``package_loader`` is an atomic aircraft-runtime operation: recheck the
    aircraft is disarmed, compile/admit a task-only plan locally, install its
    executor and return its supervisor, or raise without altering the old
    mission. The adapter rechecks ``preflight_allowed`` at first and final chunk.
    The loader must preserve the admitted package; a compiled task-only plan
    belongs in ``supervisor.executor.plan``, leaving ``package.plan`` as None.
    ``handle`` and ``tick`` are driven by the UDP server, never by flight I/O.
    """

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        *,
        package_loader: Callable[[MissionPackage], MissionSupervisor] | None = None,
        preflight_allowed: Callable[[], bool] | None = None,
        supervisor: MissionSupervisor | None = None,
        requires_approval: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        upload_timeout_s: float = 30.0,
    ) -> None:
        if not 0 < upload_timeout_s <= 300:
            raise ValueError("upload timeout must be in (0, 300]")
        self.codec, self.package_loader = codec, package_loader
        self.requires_approval = requires_approval
        self.preflight_allowed = preflight_allowed
        self.supervisor = supervisor
        self.clock, self.upload_timeout_s = clock, upload_timeout_s
        self._lock = RLock()
        self._upload: _Upload | None = None
        self._command_upload: _CommandUpload | None = None
        self._completed_commands: OrderedDict[
            int, tuple[bytes, int, int, tuple[bytes, ...], bool, str]
        ] = OrderedDict()
        self._completed: OrderedDict[int, tuple[bytes, int, int, tuple[bytes, ...]]] = OrderedDict()
        self._receipts: OrderedDict[tuple[int, int], tuple[bytes, bool, str]] = OrderedDict()
        # ACK replay identity is checked before touching the stop-and-wait
        # transfer.  Keep it bounded because ACKs are network-controlled.
        self._telemetry_ack_replay: OrderedDict[tuple[int, int, int], None] = OrderedDict()
        self._telemetry_ack_highwater: dict[int, tuple[int, int]] = {}
        self._last_sequence: int | None = None
        self._sequence = 0
        self._status_sequence = 0
        self._next_status = 0.0
        self._transfer_token = max(1, time.time_ns() & ((1 << 64) - 1))
        self._telemetry: _Telemetry | None = None
        self._queued_log: _Telemetry | None = None
        self._last_was_status = False
        self._closed = False
        self._sender: Callable[[bytes], object] | None = None
        self._peer: object | None = None
        self._peer_seen = float("-inf")
        if supervisor is not None:
            self.bind_supervisor(supervisor)

    def bind_supervisor(self, supervisor: MissionSupervisor) -> None:
        with self._lock:
            if self.supervisor is not None and self.supervisor is not supervisor:
                old = self.supervisor.downlink
                if old is not None:
                    old.set_connected(False)
                    old.set_sender(None)
            self.supervisor = supervisor
            if supervisor.downlink is not None:
                supervisor.downlink.set_sender(self.send_log)

    def _preflight(self) -> bool:
        return self.preflight_allowed is not None and self.preflight_allowed() is True

    def _frame(self, kind: int, body: bytes) -> bytes:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return self.codec.encode_mission(
            kind, body, sequence=self._sequence, sent_at_s=self.clock()
        )

    def _ack(self, token: int, kind: int, index: int, accepted: bool, detail: str) -> bytes:
        detail_bytes = detail.encode("utf-8")[:80].decode("utf-8", errors="ignore").encode("utf-8")
        return self._frame(29, ACK.pack(token, kind, index, int(accepted)) + detail_bytes)

    def handle(self, packet: DecodedOperatorPacket, *, peer: object = None) -> list[bytes]:
        if not isinstance(packet.message, MissionWireMessage):
            return []
        kind, body = int(packet.message_type), packet.message.body
        with self._lock:
            now = self.clock()
            if self._upload is not None and now - self._upload.touched > self.upload_timeout_s:
                self._retire_upload()
            if (
                self._command_upload is not None
                and now - self._command_upload.touched > self.upload_timeout_s
            ):
                self._command_upload = None
            if self._peer is not None and peer != self._peer and now - self._peer_seen <= 5:
                # Bind acknowledgements and transfers to the live authenticated peer.
                return []
            self._peer, self._peer_seen = peer, now
            if kind == 36:
                token, index = TELEMETRY_ACK.unpack(body)
                replay_id = (packet.sequence, token, index)
                if replay_id in self._telemetry_ack_replay:
                    return []
                pending = self._telemetry
                if pending is None or token != pending.token or index != pending.index:
                    return []
                previous = self._telemetry_ack_highwater.get(token)
                if previous is not None:
                    previous_sequence, previous_index = previous
                    delta = (packet.sequence - previous_sequence) & 0xFFFFFFFF
                    if delta >= 0x80000000 or (delta == 0 and index <= previous_index):
                        return []
                # Commit the replay identity only after all checks above and
                # immediately before the single valid state transition.
                self._telemetry_ack_replay[replay_id] = None
                while len(self._telemetry_ack_replay) > 512:
                    self._telemetry_ack_replay.popitem(last=False)
                self._telemetry_ack_highwater[token] = (packet.sequence, index)
                pending.index += 1
                pending.attempts = 0
                pending.sent = float("-inf")
                if pending.index == len(pending.messages):
                    pending.accepted = True
                    pending.complete.set()
                    self._telemetry = None
                return []
            if kind not in (28, 30, 31, 32, 33, 34, 37):
                return []
            token, index = (
                CHUNK.unpack_from(body)[0:2]
                if kind in (28, 37)
                else (CONTROL.unpack_from(body)[1], 0)
            )
            if kind not in (28, 37):
                cached = self._receipts.get((kind, token))
                if cached is not None:
                    previous, accepted, detail = cached
                    return [
                        self._ack(
                            token,
                            kind,
                            index,
                            accepted and body == previous,
                            detail if body == previous else "请求标识冲突",
                        )
                    ]
            elif kind == 28 and token in self._completed:
                digest, count, total, hashes = self._completed[token]
                _, _, requested_count, requested_total, requested_digest = CHUNK.unpack_from(body)
                accepted = (
                    (count, total, digest) == (requested_count, requested_total, requested_digest)
                    and index < len(hashes)
                    and hashlib.sha256(body[CHUNK.size :]).digest() == hashes[index]
                )
                return [
                    self._ack(
                        token,
                        kind,
                        index,
                        accepted,
                        "任务包已接纳" if accepted else "上传标识已过期或冲突",
                    )
                ]
            elif kind == 28 and (
                self._upload is not None
                and token == self._upload.token
                and index < len(self._upload.parts)
            ):
                metadata = CHUNK.unpack_from(body)
                upload = self._upload
                accepted = (
                    metadata[2:] == (upload.count, upload.total, upload.digest)
                    and upload.parts[index] == body[CHUNK.size :]
                )
                return [
                    self._ack(
                        token, kind, index, accepted, "分块已接收" if accepted else "分块内容冲突"
                    )
                ]
            elif kind == 37 and token in self._completed_commands:
                digest, count, total, hashes, was_accepted, detail = self._completed_commands[token]
                _, _, requested_count, requested_total, requested_digest = CHUNK.unpack_from(body)
                matches = (
                    (count, total, digest) == (requested_count, requested_total, requested_digest)
                    and index < len(hashes)
                    and hashlib.sha256(body[CHUNK.size :]).digest() == hashes[index]
                )
                return [
                    self._ack(
                        token,
                        kind,
                        index,
                        matches and was_accepted,
                        detail if matches else "conflicting ground command transfer",
                    )
                ]
            elif kind == 37 and (
                self._command_upload is not None
                and token == self._command_upload.token
                and index < len(self._command_upload.parts)
            ):
                upload = self._command_upload
                metadata = CHUNK.unpack_from(body)
                accepted = (
                    metadata[2:] == (upload.count, upload.total, upload.digest)
                    and upload.parts[index] == body[CHUNK.size :]
                )
                return [
                    self._ack(
                        token,
                        kind,
                        index,
                        accepted,
                        "ground command chunk already received"
                        if accepted
                        else "conflicting ground command chunk",
                    )
                ]
            if self._last_sequence is not None:
                delta = (packet.sequence - self._last_sequence) & 0xFFFFFFFF
                if delta == 0 or delta >= 0x80000000:
                    return [self._ack(token, kind, index, False, "消息序号已过期")]
            self._last_sequence = packet.sequence
            try:
                if kind == 28:
                    accepted, detail = self._receive_chunk(body, now)
                elif kind == 37:
                    accepted, detail = self._receive_command_chunk(body, now)
                else:
                    accepted, detail = self._ground_event(kind, body)
                    self._receipts[(kind, token)] = (body, accepted, detail)
                    while len(self._receipts) > 256:
                        self._receipts.popitem(last=False)
            except Exception as exc:
                accepted, detail = False, "拒绝: " + str(exc)
                if kind == 28 and self._upload is not None and self._upload.token == token:
                    self._retire_upload(rejected=True)
            return [self._ack(token, kind, index, accepted, detail)]

    def _receive_command_chunk(self, body: bytes, now: float) -> tuple[bool, str]:
        token, index, count, total, digest = CHUNK.unpack_from(body)
        if total > MAX_COMMAND_DOCUMENT_BYTES:
            raise ValueError("ground command exceeds transfer limit")
        if self._command_upload is None:
            if index != 0:
                raise ValueError("ground command must start at the first chunk")
            self._command_upload = _CommandUpload(token, count, total, digest, now)
        upload = self._command_upload
        if (token, count, total, digest) != (
            upload.token,
            upload.count,
            upload.total,
            upload.digest,
        ):
            raise ValueError("conflicting ground command transfer")
        if index < len(upload.parts):
            if upload.parts[index] != body[CHUNK.size :]:
                raise ValueError("conflicting ground command chunk")
            return True, "ground command chunk already received"
        if index != len(upload.parts):
            raise ValueError("ground command chunks must be ordered")
        upload.parts.append(body[CHUNK.size :])
        upload.touched = now
        if len(upload.parts) < count:
            return True, "ground command chunk received"
        payload = b"".join(upload.parts)
        self._command_upload = None
        if len(payload) != total or hashlib.sha256(payload).digest() != digest:
            raise ValueError("ground command digest mismatch")
        accepted, detail = self._ground_event_json(payload)
        self._completed_commands[token] = (
            digest,
            count,
            total,
            tuple(hashlib.sha256(part).digest() for part in upload.parts),
            accepted,
            detail,
        )
        while len(self._completed_commands) > 256:
            self._completed_commands.popitem(last=False)
        return accepted, detail

    def _retire_upload(self, *, rejected: bool = True) -> None:
        upload, self._upload = self._upload, None
        if upload is not None:
            hashes = (
                () if rejected else tuple(hashlib.sha256(part).digest() for part in upload.parts)
            )
            self._completed[upload.token] = (
                upload.digest,
                -1 if rejected else upload.count,
                upload.total,
                hashes,
            )
            while len(self._completed) > 256:
                self._completed.popitem(last=False)

    def _receive_chunk(self, body: bytes, now: float) -> tuple[bool, str]:
        token, index, count, total, digest = CHUNK.unpack_from(body)
        if self.package_loader is None or not self._preflight():
            raise ValueError("仅可在机载确认的飞行前状态上传任务包")
        if self._upload is None:
            if index != 0:
                raise ValueError("必须从首个分块开始上传")
            self._upload = _Upload(token, count, total, digest, now)
        upload = self._upload
        if (token, count, total, digest) != (
            upload.token,
            upload.count,
            upload.total,
            upload.digest,
        ):
            raise ValueError("已有其他上传或分块元数据冲突")
        if index != len(upload.parts):
            raise ValueError("分块顺序错误")
        upload.parts.append(body[CHUNK.size :])
        upload.touched = now
        if len(upload.parts) < count:
            return True, "分块已接收"
        payload = b"".join(upload.parts)
        if len(payload) != total or hashlib.sha256(payload).digest() != digest:
            raise ValueError("任务包摘要不一致")
        package = load_mission_package(strict_json_loads(payload, name="mission package"))
        if not self._preflight():
            raise ValueError("飞行状态已变化，拒绝替换任务")
        # Frozen dataclasses still contain mutable dictionaries. Snapshot before
        # invoking aircraft code so in-place mutation cannot change the proof.
        admitted_document = deepcopy(package.document)
        admitted_refs = deepcopy(package.refs)
        admitted_plan = deepcopy(package.plan)
        supervisor = self.package_loader(package)
        if (
            not isinstance(supervisor, MissionSupervisor)
            or supervisor.package.document != admitted_document
            or supervisor.package.refs != admitted_refs
            or supervisor.package.plan != admitted_plan
            or (admitted_plan is not None and supervisor.executor.plan != admitted_plan)
        ):
            raise ValueError("机载加载器未安装对应任务监督器")
        supervisor.package.validate_plan(supervisor.executor.plan)
        if self.requires_approval is not None:
            supervisor.requires_approval = bool(self.requires_approval)
            supervisor.plan_approved = not supervisor.requires_approval
        self.bind_supervisor(supervisor)
        supervisor.log.append(
            "plan",
            supervisor.clock(),
            plan_sha256=supervisor.plan_sha256,
            task_text=supervisor.package.document.get("task_text") or supervisor.executor.plan.goal,
            requires_approval=supervisor.requires_approval,
            steps=[
                {
                    "id": s.id,
                    "skill": s.skill,
                    "params": dict(s.params),
                    "on_success": s.on_success,
                    "on_fail": s.on_fail,
                }
                for s in supervisor.executor.plan.steps
            ],
            **(
                {"origin": supervisor.package.document["origin"]}
                if "origin" in supervisor.package.document
                else {}
            ),
        )
        if supervisor.downlink is not None:
            supervisor.downlink.notify()
        self._retire_upload(rejected=False)
        self._next_status = 0
        return True, "任务包已通过机载验证并接纳"

    def _ground_event(self, kind: int, body: bytes) -> tuple[bool, str]:
        supervisor = self.supervisor
        mission, _ = CONTROL.unpack_from(body)
        if supervisor is None or mission != mission_token(supervisor.package.mission_id):
            return False, "任务标识不匹配或没有机载任务"
        event: dict[str, Any] = {
            "mission_id": supervisor.package.mission_id,
            "type": {30: "abort", 31: "pause", 32: "resume", 33: "policy_update", 34: "heartbeat"}[
                kind
            ],
        }
        if kind == 33:
            _, _, policy, timeout = TIGHTEN.unpack(body)
            event["policy"] = {"action": POLICIES[policy], "timeout_s": timeout}
        # Omitting timestamp makes MissionSupervisor sample its own local clock.
        accepted = supervisor.ground_event(event)
        return accepted, "已接纳" if accepted else "机载监督器拒绝事件或策略放宽"

    def _ground_event_json(self, payload: bytes) -> tuple[bool, str]:
        supervisor = self.supervisor
        if supervisor is None:
            return False, "no active onboard mission"
        event = strict_json_loads(payload, name="ground command")
        if not isinstance(event, dict):
            raise ValueError("ground command must be a JSON object")
        if event.get("type") in {"approve_plan", "reject_plan"}:
            digest = event.get("plan_sha256")
            accepted = (
                supervisor.approve_plan(digest)
                if event["type"] == "approve_plan"
                else supervisor.reject_plan(digest)
            )
            return (
                accepted,
                "plan command accepted" if accepted else "plan digest mismatch or stale",
            )
        accepted = supervisor.ground_event(event)
        return (
            accepted,
            "ground command accepted" if accepted else "ground supervisor rejected command",
        )

    def _new_telemetry(self, document: dict[str, Any]) -> _Telemetry:
        payload = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if document.get("kind") == "final_summary" and len(payload) > MAX_LOG_DOCUMENT_BYTES:
            # The summary body is intentionally split as JSON text.  Ground
            # stations concatenate pages in order and parse only once all
            # pages have arrived, so no partial result is displayed.
            summary = document.get("summary", document)
            body = json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            page_budget = MAX_LOG_DOCUMENT_BYTES - 180
            chunks = [
                body[offset : offset + page_budget] for offset in range(0, len(body), page_budget)
            ]
            pages = len(chunks)
            page_docs = [
                {
                    "kind": "final_summary_page",
                    "mission_id": document.get("mission_id"),
                    "page": index,
                    "pages": pages,
                    "body": chunk,
                }
                for index, chunk in enumerate(chunks, 1)
            ]
            payload = json.dumps(
                page_docs[0], ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            # A telemetry transfer carries one JSON document.  Queue all page
            # documents as one bounded transfer sequence.
            self._transfer_token = (self._transfer_token + 1) & ((1 << 64) - 1) or 1
            token = self._transfer_token
            messages: list[MissionWireMessage] = []
            for page in page_docs:
                encoded = json.dumps(
                    page, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                messages.extend(document_chunks(encoded, token, message_type=35))
            return _Telemetry(messages, token)
        if document.get("kind") == "status" and len(payload) > MAX_STATUS_DOCUMENT_BYTES:
            raise ValueError("status document exceeds 1200-byte protocol bound")
        if document.get("kind") != "status" and len(payload) > MAX_LOG_DOCUMENT_BYTES:
            # A <=35-chunk log yields to current status within about 3.5 seconds
            # at the endpoint's 100 ms tick. Full evidence remains in JSONL.
            summary = {
                key: document[key]
                for key in ("mission_id", "sequence", "kind", "timestamp", "recorded_at")
                if key in document
            }
            summary.update(
                detail_truncated=True,
                original_sha256=hashlib.sha256(payload).hexdigest(),
                original_bytes=len(payload),
            )
            for key in ("action", "reason", "policy", "event_type"):
                value = document.get(key)
                if isinstance(value, str):
                    summary[key] = value.encode("utf-8")[:192].decode("utf-8", errors="ignore")
            excerpt = payload[:384]
            while True:
                summary["detail"] = excerpt.decode("utf-8", errors="replace")
                encoded = json.dumps(
                    summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                if len(encoded) <= MAX_LOG_DOCUMENT_BYTES:
                    payload = encoded
                    break
                if not excerpt:
                    raise ValueError("mission log identity exceeds bounded monitoring size")
                excerpt = excerpt[: len(excerpt) // 2]
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise ValueError("mission telemetry exceeds transfer limit")
        self._transfer_token = (self._transfer_token + 1) & ((1 << 64) - 1) or 1
        token = self._transfer_token
        return _Telemetry(list(document_chunks(payload, token, message_type=35)), token)

    def send_log(self, record: dict[str, Any]) -> bool:
        """DownlinkQueue callback; waits only in that queue's daemon worker."""
        with self._lock:
            if self._closed or self._sender is None or self._queued_log is not None:
                return False
            pending = self._new_telemetry(record)
            self._queued_log = pending
        pending.complete.wait(timeout=30)
        with self._lock:
            if self._telemetry is pending:
                self._telemetry = None
            if self._queued_log is pending:
                self._queued_log = None
            return pending.accepted

    def status_document(self) -> dict[str, Any] | None:
        supervisor = self.supervisor
        if supervisor is None:
            return None
        now = supervisor.clock()
        remaining = supervisor.package.document["budget"]["time_s"] - (now - supervisor.started)
        if supervisor._remaining_time is not None:
            remaining = min(
                remaining, supervisor._remaining_time - (now - supervisor._time_estimated_at)
            )
        self._status_sequence = max(self._status_sequence + 1, time.time_ns() // 1_000_000)
        executor = supervisor.executor
        current = executor.current_step
        run = getattr(executor, "_run", None)
        progress = None
        if run is not None:
            budget = max(1, int(getattr(executor.config, "step_budget", 1)))
            progress = min(1.0, max(0.0, float(getattr(run, "visited", 0)) / budget))
        state = (
            "awaiting_approval"
            if not supervisor.plan_approved
            else (
                "done"
                if executor.done
                else ("returning" if supervisor.action.startswith("return") else "running")
            )
        )
        document = {
            "v": 2,
            "kind": "status",
            "mission_id": supervisor.package.mission_id,
            "sequence": self._status_sequence,
            "seq": self._status_sequence,
            "t": now,
            "state": state,
            "supervisor": {
                "action": supervisor.action,
                "reason": supervisor.reason or "",
                "policy": supervisor.policy,
                "timeout_s": supervisor.timeout_s,
                "link": "up" if supervisor.connected else "down",
                "link_silent_s": max(0.0, now - (supervisor.last_heartbeat or now)),
            },
            "budget": {
                "time_left_s": max(0.0, remaining),
                "energy_left_wh": supervisor._remaining_energy,
            },
            "step": {
                "id": current.id if current else None,
                "skill": current.skill if current else None,
                "state": run.state if run is not None else ("done" if executor.done else None),
                "index": next(
                    (i for i, item in enumerate(executor.plan.steps) if item.id == current.id), 0
                )
                if current
                else 0,
                "total": len(executor.plan.steps),
                "progress": progress,
                "primitives": len(getattr(run.loop, "history", ())) if run is not None else 0,
                "budget": getattr(executor.config, "step_budget", None),
                "query": current.params.get("query") if current else None,
            },
            "counts": {
                key: {"n": value, "lower_bound": bool(executor.count_is_lower_bound)}
                for key, value in executor.counts.items()
            },
            "findings_total": len(executor.findings),
            "alerts_total": len(executor.alerts),
            "action": supervisor.action,
            "reason": supervisor.reason,
            "policy": supervisor.policy,
            "timeout_s": supervisor.timeout_s,
            "remaining_time_s": max(0.0, remaining),
            "remaining_energy_wh": supervisor._remaining_energy,
            # Unknown gate is conservative, never proof of preflight permission.
            "flight_active": not self._preflight(),
        }
        return document

    def tick(self, sender: Callable[[bytes], object] | None) -> None:
        """Send at most one chunk per receive-loop tick, with bounded retries."""
        with self._lock:
            self._sender = sender
            now = self.clock()
            supervisor = self.supervisor
            if self._upload is not None and now - self._upload.touched > self.upload_timeout_s:
                self._retire_upload()
            if supervisor is not None and supervisor.downlink is not None:
                supervisor.downlink.set_connected(sender is not None and supervisor.connected)
            if sender is None or self._closed:
                return
            pending = self._telemetry
            if pending is not None and pending.attempts >= 5 and now - pending.sent >= 1:
                pending.complete.set()
                self._telemetry = pending = None
            status_due = now >= self._next_status
            if (
                pending is None
                and self._queued_log is not None
                and (not status_due or self._last_was_status)
            ):
                self._telemetry = pending = self._queued_log
                self._queued_log = None
                self._last_was_status = False
            if pending is None and status_due:
                document = self.status_document()
                self._next_status = now + 1
                if document is not None:
                    self._telemetry = pending = self._new_telemetry(document)
                    self._last_was_status = True
            if pending is not None and now - pending.sent >= 1:
                message = pending.messages[pending.index]
                frame = self._frame(35, message.body)
                pending.attempts += 1
                pending.sent = now
                sender(frame)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._telemetry is not None:
                self._telemetry.complete.set()
            if self._queued_log is not None:
                self._queued_log.complete.set()
            if self.supervisor is not None and self.supervisor.downlink is not None:
                self.supervisor.downlink.set_connected(False)
                self.supervisor.downlink.set_sender(None)

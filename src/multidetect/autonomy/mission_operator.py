"""Mission operator adapter for the existing authenticated Jetson endpoint.

Upload admission is deliberately dependency injected: the aircraft runtime must
provide an authoritative disarmed/preflight gate, a package verification key and
a package loader which compiles task text on board and returns a candidate
supervisor.  Absent any of them, uploads fail closed.  Unsigned packages are
always refused.  Onboard planning runs on a worker thread, never on the UDP
receive thread, and a candidate is installed only after it has been checked and
the preflight gate has been re-read under the adapter lock.  No network
operation runs on the flight executor thread. Telemetry retries run from the
endpoint's receive loop; durable log delivery waits only in DownlinkQueue's
existing daemon worker.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
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
from .mission_security import AuthenticationKey, load_key
from .mission_supervisor import MissionSupervisor, count_lower_bounds
from .onboard_mission_log import is_priority_record
from .serialization import strict_json_loads

MAX_LOG_DOCUMENT_BYTES = 1536
MAX_STATUS_DOCUMENT_BYTES = 1200


def _compact(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _byte_pages(body: str, mission_id: Any) -> list[str]:
    """Split summary JSON text so every encoded page fits the log byte budget.

    Pages were cut by characters, so a Chinese summary produced ~4 KB pages.
    The budget counts each character as it is encoded inside the page JSON
    (UTF-8 and string escaping) and never splits a character.
    """
    overhead = len(
        _compact(
            {
                "kind": "final_summary_page",
                "mission_id": mission_id,
                "page": 999_999,
                "pages": 999_999,
                "body": "",
            }
        )
    )
    budget = MAX_LOG_DOCUMENT_BYTES - overhead
    if budget < 16:
        raise ValueError("mission id leaves no room for final summary pages")
    pages: list[str] = []
    start, used = 0, 0
    for offset, char in enumerate(body):
        size = len(_compact(char)) - 2
        if used + size > budget:
            pages.append(body[start:offset])
            start, used = offset, 0
        used += size
    pages.append(body[start:])
    return pages


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value.encode("utf-8")) > limit:
        return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore") + "…"
    return value


def _bounded_status(document: dict[str, Any]) -> bytes:
    """Fit a status into the 1200-byte status bound instead of dropping it.

    Long free text is clipped first, then per-step counts are replaced by a
    total; identity, state, supervisor action and flight gate always remain.
    """
    status = deepcopy(document)
    status["truncated"] = True
    for key in ("reason", "action", "policy"):
        status[key] = _clip(status.get(key), 96)
    supervisor = status.get("supervisor")
    if isinstance(supervisor, dict):
        supervisor["reason"] = _clip(supervisor.get("reason"), 96)
        supervisor["action"] = _clip(supervisor.get("action"), 32)
    step = status.get("step")
    if isinstance(step, dict):
        for key in ("id", "skill", "query", "state"):
            step[key] = _clip(step.get(key), 48)
    payload = _compact(status)
    if len(payload) <= MAX_STATUS_DOCUMENT_BYTES:
        return payload
    counts = status.pop("counts", {}) or {}
    status["counts_total"] = sum(
        int(item.get("n", 0)) for item in counts.values() if isinstance(item, dict)
    )
    status["counts_steps"] = len(counts)
    payload = _compact(status)
    if len(payload) <= MAX_STATUS_DOCUMENT_BYTES:
        return payload
    minimal = {
        key: status.get(key)
        for key in (
            "v",
            "kind",
            "sequence",
            "seq",
            "t",
            "state",
            "action",
            "reason",
            "remaining_time_s",
            "remaining_energy_wh",
            "flight_active",
            "findings_total",
            "alerts_total",
            "counts_total",
            "truncated",
        )
    }
    minimal["mission_id"] = _clip(status.get("mission_id"), 256)
    payload = _compact(minimal)
    if len(payload) > MAX_STATUS_DOCUMENT_BYTES:
        raise ValueError("status identity exceeds the 1200-byte protocol bound")
    return payload


@dataclass
class _Upload:
    token: int
    count: int
    total: int
    digest: bytes
    touched: float
    parts: list[bytes] = field(default_factory=list)


@dataclass
class _Compile:
    """A fully received and verified package being planned off the receive thread."""

    upload: _Upload
    package: MissionPackage
    document: dict[str, Any]
    refs: dict[str, Any]
    plan: Any
    done: bool = False
    report_async: bool = False
    accepted: bool = False
    detail: str = ""


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
    priority: bool = False


class MissionOperatorAdapter:
    """Bounded, replay-aware mission session, independent of MAVLink framing.

    ``package_loader`` compiles/admits a verified package and returns a
    candidate supervisor without altering the running mission; it runs on a
    planner worker (``planner_runner``, a new thread by default) because
    onboard planning can take minutes.  ``package_installer`` then makes the
    candidate the aircraft's mission; the adapter calls it under its lock only
    after checking the candidate, re-reading ``preflight_allowed`` and setting
    the plan-approval state, so no host thread can see an unchecked or
    not-yet-approval-gated supervisor.  A legacy loader without an installer
    must install nothing itself.  The loader must preserve the admitted package;
    a compiled task-only plan belongs in ``supervisor.executor.plan``, leaving
    ``package.plan`` as None.  ``seal`` stops all further package admission (the
    host calls it right before arming).  ``handle`` and ``tick`` are driven by
    the UDP server, never by flight I/O.
    """

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        *,
        package_loader: Callable[[MissionPackage], MissionSupervisor] | None = None,
        preflight_allowed: Callable[[], bool] | None = None,
        supervisor: MissionSupervisor | None = None,
        requires_approval: bool | None = None,
        package_verify_key_path: str | None = None,
        package_verify_key: AuthenticationKey | None = None,
        package_installer: Callable[[MissionSupervisor], object] | None = None,
        runtime_mode: str | None = None,
        planner_runner: Callable[[Callable[[], None]], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        upload_timeout_s: float = 30.0,
    ) -> None:
        if not 0 < upload_timeout_s <= 300:
            raise ValueError("upload timeout must be in (0, 300]")
        self.codec, self.package_loader = codec, package_loader
        self.package_installer = package_installer
        self.requires_approval = requires_approval
        self.package_verify_key_path = package_verify_key_path
        if package_verify_key is not None and not isinstance(package_verify_key, AuthenticationKey):
            raise TypeError("package_verify_key must be an AuthenticationKey")
        self._package_key = package_verify_key or (
            load_key(package_verify_key_path) if package_verify_key_path is not None else None
        )
        self.runtime_mode = runtime_mode
        self.planner_runner = planner_runner or self._planner_thread
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
        self._compiling: _Compile | None = None
        self._sealed = False
        self._outbox: list[bytes] = []
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

    @staticmethod
    def _planner_thread(job: Callable[[], None]) -> None:
        Thread(target=job, name="mission-package-planner", daemon=True).start()

    def seal(self) -> MissionSupervisor | None:
        """Stop admitting packages and return the mission that will fly.

        The host calls this after the plan is approved and before arming, so a
        package arriving in between can no longer replace the approved one.  A
        package still being planned is refused when its planner returns.
        """
        with self._lock:
            self._sealed = True
            if self._upload is not None:
                self._retire_upload()
            return self.supervisor

    def announce_plan(self, supervisor: MissionSupervisor) -> None:
        """Log (and so downlink) the plan the operator approves or rejects."""
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
                # Match the header of the chunk in flight: a paged summary
                # carries one token per page with indexes restarting at 0.
                if pending is None or (token, index) != tuple(
                    CHUNK.unpack_from(pending.messages[pending.index].body)[0:2]
                ):
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
            elif (
                kind == 28 and self._compiling is not None and token == self._compiling.upload.token
            ):
                upload = self._compiling.upload
                metadata = CHUNK.unpack_from(body)
                accepted = (
                    metadata[2:] == (upload.count, upload.total, upload.digest)
                    and index < len(upload.parts)
                    and upload.parts[index] == body[CHUNK.size :]
                )
                return [
                    self._ack(
                        token,
                        kind,
                        index,
                        accepted,
                        "任务包已接收，机载编译中" if accepted else "分块内容冲突",
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
                elif kind == 34 and self.supervisor is None:
                    # A console heartbeats before (and while) its package is
                    # admitted.  Without a mission there is nothing to keep
                    # alive: drop it unanswered and uncached, so it neither
                    # counts as a mission heartbeat nor reads as a refusal.
                    return []
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
        if self._package_key is None:
            raise ValueError("机载未配置任务包验证密钥，拒绝上传")
        if self._sealed:
            raise ValueError("任务已锁定起飞，拒绝替换")
        if self._compiling is not None:
            raise ValueError("机载正在编译上一任务包")
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
        # Signature and the complete package contract are checked here, before
        # any planning.  An unsigned package is never admitted.
        package = load_mission_package(
            strict_json_loads(payload, name="mission package"),
            key=self._package_key,
            require_signature=True,
            mode=self.runtime_mode,
        )
        if not self._preflight():
            raise ValueError("飞行状态已变化，拒绝替换任务")
        # Frozen dataclasses still contain mutable dictionaries. Snapshot before
        # invoking aircraft code so in-place mutation cannot change the proof.
        job = _Compile(
            upload,
            package,
            deepcopy(package.document),
            deepcopy(package.refs),
            deepcopy(package.plan),
        )
        self._upload, self._compiling = None, job
        self.planner_runner(lambda: self._plan(job))
        if job.done:  # an inline planner (tests, replay) has already finished
            return job.accepted, job.detail
        # The planner commits under this lock, so it cannot finish before the
        # flag is set; its verdict is sent as a second ACK for the last chunk.
        job.report_async = True
        return True, "任务包已接收，机载编译中"

    def _plan(self, job: _Compile) -> None:
        """Planner worker: compile without the adapter lock, then commit under it."""
        try:
            candidate: MissionSupervisor | None = self.package_loader(job.package)
            error: Exception | None = None
        except Exception as exc:  # reported to the operator, never raised
            candidate, error = None, exc
        with self._lock:
            try:
                if error is not None:
                    raise error
                self._admit(job, candidate)
                job.accepted, job.detail = True, "任务包已通过机载验证并接纳"
            except Exception as exc:
                job.accepted, job.detail = False, "拒绝: " + str(exc)
            upload = job.upload
            self._compiling = None
            self._completed[upload.token] = (
                upload.digest,
                upload.count if job.accepted else -1,
                upload.total,
                tuple(hashlib.sha256(part).digest() for part in upload.parts)
                if job.accepted
                else (),
            )
            while len(self._completed) > 256:
                self._completed.popitem(last=False)
            job.done = True
            if job.report_async and not self._closed:
                self._outbox.append(
                    self._ack(upload.token, 28, upload.count - 1, job.accepted, job.detail)
                )
                del self._outbox[:-16]

    def _admit(self, job: _Compile, supervisor: MissionSupervisor | None) -> None:
        """Check a candidate and install it; called with the adapter lock held."""
        if self._closed:
            raise ValueError("操作员链路已关闭")
        if (
            not isinstance(supervisor, MissionSupervisor)
            or supervisor.package.document != job.document
            or supervisor.package.refs != job.refs
            or supervisor.package.plan != job.plan
            or (job.plan is not None and supervisor.executor.plan != job.plan)
        ):
            raise ValueError("机载加载器未安装对应任务监督器")
        supervisor.package.validate_plan(supervisor.executor.plan)
        # Planning can take minutes: the aircraft must still be on the ground,
        # and the host must not have sealed the approved mission for take-off.
        if not self._preflight():
            raise ValueError("飞行状态已变化，拒绝替换任务")
        if self._sealed:
            raise ValueError("任务已锁定起飞，拒绝替换")
        if self.requires_approval is not None:
            supervisor.requires_approval = bool(self.requires_approval)
        # Gate before the host can see the candidate.
        supervisor.plan_approved = not supervisor.requires_approval
        if self.package_installer is not None:
            self.package_installer(supervisor)
        self.bind_supervisor(supervisor)
        self.announce_plan(supervisor)
        self._next_status = 0

    def _ground_event(self, kind: int, body: bytes) -> tuple[bool, str]:
        """Fixed-layout controls; the operator tunnel has authenticated them."""
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
        accepted = supervisor.ground_event(event, transport_authenticated=True)
        return accepted, "已接纳" if accepted else "机载监督器拒绝事件或策略放宽"

    def _ground_event_json(self, payload: bytes) -> tuple[bool, str]:
        supervisor = self.supervisor
        if supervisor is None:
            return False, "no active onboard mission"
        event = strict_json_loads(payload, name="ground command")
        if not isinstance(event, dict):
            raise ValueError("ground command must be a JSON object")
        # Plan decisions take the same path as every other ground command:
        # mission id, signature, timestamp window, sequence high-water, rate
        # limit and the supervisor's ground lock.
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
            chunks = _byte_pages(body, document.get("mission_id"))
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
            # A telemetry transfer carries one JSON document, so every page is
            # its own transfer (own token, chunk indexes from 0) queued as one
            # stop-and-wait sequence.  Sharing one token made the ground ACK
            # page 2 chunk 0 as (token, 0) while the sequence waited for its
            # global index, and a GCS that had completed that token ignored it:
            # page 1 was resent forever and page 2 never arrived (e2e run9).
            messages: list[MissionWireMessage] = []
            first_token = None
            for page in page_docs:
                encoded = json.dumps(
                    page, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                self._transfer_token = (self._transfer_token + 1) & ((1 << 64) - 1) or 1
                first_token = first_token or self._transfer_token
                messages.extend(document_chunks(encoded, self._transfer_token, message_type=35))
            return _Telemetry(messages, first_token)
        if document.get("kind") == "status" and len(payload) > MAX_STATUS_DOCUMENT_BYTES:
            # Raising here dropped every status for the rest of the mission.
            payload = _bounded_status(document)
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
            pending.priority = is_priority_record(record)
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
        # A rejected plan or a return/land latched while waiting is no longer
        # "awaiting approval": the aircraft is recovering.
        terminal = getattr(supervisor, "_terminal", None)
        state = (
            "awaiting_approval"
            if not supervisor.plan_approved and terminal is None
            else (
                "done"
                if executor.done
                else (
                    "returning"
                    if terminal is not None or supervisor.action.startswith("return")
                    else "running"
                )
            )
        )
        lower_bounds = count_lower_bounds(executor)
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
                key: {"n": value, "lower_bound": lower_bounds.get(key, True)}
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
            outbox, self._outbox = self._outbox, []
            for frame in outbox:  # planner verdicts for uploads that were compiling
                sender(frame)
            pending = self._telemetry
            if pending is not None and pending.attempts >= 5 and now - pending.sent >= 1:
                pending.complete.set()
                self._telemetry = pending = None
            status_due = now >= self._next_status
            queued = self._queued_log
            # Status and logs alternate when both are due; a finding, alert,
            # escalation or final summary never waits behind a status.
            if (
                pending is None
                and queued is not None
                and (not status_due or self._last_was_status or queued.priority)
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

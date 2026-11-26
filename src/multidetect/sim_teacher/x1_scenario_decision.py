"""Collector-side Altair request and receipt plumbing for X1 SITL."""
from __future__ import annotations

import json
import math
from collections import Counter, deque
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event
from types import SimpleNamespace
from urllib.request import Request, urlopen

from multidetect.autonomy.x1_contingency_executor import (
    Admission,
    DECISION_SCHEMA,
    MAX_DECISION_AGE_MS,
    MAX_DECISION_BYTES,
    X1AirbornePlanAdapter,
    X1ContingencyExecutor,
    X1Envelope,
)
from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads
from multidetect.autonomy.x1_contingency_state import X1StateBuilder
from multidetect.autonomy.x1_perception_tools import TOOL_SCHEMA, ToolProtocolError

from .x1_scenario_observation import injected_target_reading

QUIET_CADENCE_FLIGHT_STATE_REASONS = frozenset({
    "px4_owns_or_flight_state_unknown",
    "flight_mode_or_position_unverified",
})


def _emit_audit(audit, **record):
    try:
        audit(deepcopy(record))
    except Exception:
        # An observer must not make a request or receipt fail.
        pass


def llama_decisions(port, sentence, *, audit=None):
    """Return a backend that can synchronously audit each outbound model turn."""
    def post(request):
        tools = ""
        if "perception" in request:
            tools = (
                f" If you need one more observation and perception.tools lists its name, you may instead "
                f"return one {TOOL_SCHEMA} JSON object with schema, tool, args={{}}, request_id and "
                "call_index (echo perception.call_index). The next request has the same state and ordered "
                "tool results. Treat ok=false as unavailable, never as a negative observation. "
                "When the evidence is sufficient or no calls remain, return an action object."
            )
        body = {"model": "local", "temperature": 0, "max_tokens": 512, "messages": [
            {"role": "system", "content": (
                f"You are the onboard Altair planner. Return one {DECISION_SCHEMA} JSON object with "
                "schema, request_id, event_id, mission_id, revision, action and params. Echo the current "
                "IDs from the request and state (event_id comes from the request, not latest_event_id). Actions: continue/return/land-now/abort-transition with "
                "empty params, loiter with hold_ref, retask with plan, gimbal-retarget with pitch_deg and "
                "yaw_deg. Select only a defensible action given the observations and available evidence." + tools)},
            {"role": "user", "content": json.dumps({"task": sentence, "request": request})},
        ]}
        req = Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(body).encode(),
                      {"Content-Type": "application/json"})
        if audit is not None:
            perception = request.get("perception")
            call_index = perception.get("call_index", 0) if isinstance(perception, dict) else 0
            identity = {"request_id": request.get("request_id"), "call_index": call_index}
            _emit_audit(audit, kind="turn_sent", **identity, request=request,
                        messages=body["messages"], body=body, body_json=req.data.decode("utf-8"))
            started_ns = time.monotonic_ns()
        wire = None
        backend_error = None
        try:
            with urlopen(req, timeout=2.5) as response:
                wire = json.loads(response.read(65_536))["choices"][0]["message"]["content"]
        except Exception as exc:
            if audit is not None:
                backend_error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            if audit is not None:
                _emit_audit(audit, kind="turn_result", **identity,
                            latency_ms=(time.monotonic_ns() - started_ns) / 1_000_000,
                            model_output=wire if backend_error is None else None, backend_error=backend_error)
        return wire
    return post


def recorded_decisions(decisions):
    """Yield recorded actions while binding IDs to the current CPU fixture request."""
    choices = iter(decisions)

    def post(request):
        decision = next(choices)
        state = request["state"]
        return json.dumps({**decision, "schema": DECISION_SCHEMA, "request_id": request["request_id"],
                           "event_id": request["event_id"], "mission_id": state["mission_id"],
                           "revision": state["revision"]})
    return post


def _is_retask(wire):
    if isinstance(wire, str):
        wire = wire.encode("utf-8", errors="surrogatepass")
    if not isinstance(wire, bytes) or len(wire) > MAX_DECISION_BYTES:
        return False
    try:
        value = strict_json_loads(wire, name="Altair response")
    except (ValueError, RecursionError):
        return False
    return isinstance(value, dict) and value.get("action") == "retask"


class _ReplayPort:
    """Record dispatcher calls in memory; this port cannot reach a vehicle."""

    def __init__(self):
        self.commands = []

    def rtl(self):
        self.commands.append(("rtl",))

    def land(self):
        self.commands.append(("land",))

    def set_mode(self, mode):
        self.commands.append(("set_mode", mode))

    def loiter_at(self, centre, radius, altitude, *, speed):
        self.commands.append(("loiter_at", centre, radius, altitude, speed))
        return SimpleNamespace(ok=True, ack_result=0)  # Synthetic ACK, never a readback.

    def gimbal_control(self, *, pitch_deg, yaw_deg):
        self.commands.append(("gimbal_control", pitch_deg, yaw_deg))
        return SimpleNamespace(ok=True, ack_result=0)


class _LivePort:
    """The collector's gimbal ingress plus the native handoff writer."""

    def __init__(self, gimbal, native):
        self.gimbal, self.native = gimbal, native

    def gimbal_control(self, **angles):
        if self.gimbal is None:
            return SimpleNamespace(ok=False, ack_result=None)
        return self.gimbal.gimbal_control(**angles)

    def rtl(self):
        return self.native.rtl()

    def land(self):
        return self.native.land()

    def set_mode(self, mode):
        return self.native.set_mode(mode)

    def loiter_at(self, centre, radius, altitude, *, speed):
        return self.native.loiter_at(centre, radius, altitude, speed=speed)


class DecisionTick:
    """Submit on a worker. Validate and apply on the serialized collector tick.

    Without ``native_port`` a live tick cannot claim the route writer, so every
    native action is rejected with ``native_handoff_not_available``.
    """

    def __init__(self, package, profile, backend, gimbal_port, record, *, run_id, plan_digest,
                 revision=1, peer=(1, 1), now_ms=lambda: time.monotonic_ns() // 1_000_000,
                 build_proof=None, cpu_replay=False, native_port=None, decision_cadence_s=0, tool_runner=None,
                 replace_remaining=None, recorded_cadence_followup=False, audit=None, target_cadence_only=False):
        if not math.isfinite(decision_cadence_s) or decision_cadence_s < 0:
            raise ValueError("decision cadence must be finite and nonnegative")
        if cpu_replay and (gimbal_port is not None or native_port is not None):
            raise ValueError("CPU replay cannot take a live port")
        replay_port = _ReplayPort() if cpu_replay else None
        self.replay_commands = replay_port.commands if replay_port is not None else None
        self.native = native_port
        if replay_port is not None:
            port, claim = replay_port, (lambda: True)
        elif native_port is not None:
            port, claim = _LivePort(gimbal_port, native_port), native_port.claim
        else:
            port, claim = gimbal_port, (lambda: False)
        self.builder = X1StateBuilder(vehicle_system=peer[0], vehicle_component=peer[1])
        self.executor = X1ContingencyExecutor(
            X1AirbornePlanAdapter(revision, replace_remaining=replace_remaining or (lambda *_: False)),
            port, package, X1Envelope(profile), claim_native=claim,
            capabilities=frozenset(package.document["capabilities"]),
        )
        self.mission_id = package.mission_id
        self.revision = revision
        self.backend, self.record, self.now_ms = backend, record, now_ms
        self.audit = audit
        self.tool_runner = tool_runner
        self.tool_cancel = None
        self.run_id, self.plan_digest = run_id, plan_digest
        self.build_proof = build_proof
        evidence = package.document.get("x1_live_evidence", {})
        self.provenance = {key: evidence[key] for key in ("basis", "assumed_bounds_sha256") if key in evidence}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x1-altair-decision")
        self.future = None
        self.pending = None
        self.last_requested = 0
        self.last_submit_ms = None
        self.cadence_s = decision_cadence_s
        self.recorded_cadence_followup = recorded_cadence_followup
        self.target_cadence_only = target_cadence_only
        self.last_followup_event = 0
        self.recent_receipts = deque(maxlen=4)
        self.quiet_cadence_rejected_continue = Counter()
        self.last_admission = None
        self.installed_retask = None

    def _mark(self, kind, **fields):
        self.record({"message_type": f"scenario_decision_{kind}",
                     "receive_monotonic_ns": time.monotonic_ns(), **fields})

    def tick(self, frame, *, mav_rows=(), observations=(), events=(), event_current=True, wind=None,
             mission_progress=None, mode=None, cadence_active=True, step_boundary=None, perception=None):
        now = self.now_ms()
        for row in sorted(mav_rows, key=lambda row: row["receive_monotonic_ns"]):
            stamp = row["receive_monotonic_ns"] // 1_000_000
            # The builder clock includes our previous snapshot. Admit each new
            # sample once and only if it arrived after that snapshot.
            if getattr(self, "last_tick_ms", 0) < stamp <= now:
                self.builder.ingest_mavlink(stamp, row)
        for group, values, source in observations:
            self.builder.observe(now, group, values, source=source)
        for event, source in events:
            confidence = wind.confidence if event == "wind_step" and wind is not None else None
            self.builder.event(now, event, source=source, confidence=confidence)
        snapshot = self.builder.snapshot(now, mission_id=self.mission_id, revision=self.revision)
        if wind is not None:
            # This tick's selected reading already carries its own kinematic timestamp.
            snapshot["telemetry"]["wind"] = wind.telemetry(now)
        self.last_tick_ms = now
        event_id = snapshot["latest_event_id"]
        event_due = (event_current and snapshot["events"] and event_id != self.last_requested
                     and now - snapshot["events"][-1]["t_ms"] <= MAX_DECISION_AGE_MS)
        target_cadence_due = (self.target_cadence_only and event_due and event_id != self.last_followup_event
                              and snapshot["events"][-1]["type"] == "target_found")
        if self.target_cadence_only and event_due and snapshot["events"][-1]["type"] == "target_found":
            event_due = False
        phase = mission_progress["phase"] if mission_progress is not None else None
        remaining_sha = (mission_progress["remaining_plan_sha256"]
                         if mission_progress is not None else self.plan_digest)
        mav_leg = snapshot["telemetry"]["leg"]
        signature = {
            "event": event_id,
            "step": (frame.active_motion_index, mav_leg["seq"] if mav_leg else None, remaining_sha),
            "mode": (mode, frame.vtol_state, phase, frame.px4_failsafe, frame.armed_in_air,
                     self.executor.native_owner),
            "revision": (frame.mission_id, frame.revision, self.executor.mission.plan_version),
        }
        if self.pending is not None:
            request, offered, previous, offered_detection = self.pending
            cadence = request.get("trigger") == "cadence"
            changed = next((key for key in ("step", "mode", "revision") if signature[key] != previous[key]), None)
            retask = False
            if (step_boundary is not None and self.future.done() and not self.future.cancelled()
                    and self.future.exception() is None):
                reply = self.future.result()
                retask = _is_retask(reply.wire if self.tool_runner is not None else reply)
            previous_index = previous["step"][0]
            # The planned phase may change when the cursor advances. The
            # actual PX4 mode, VTOL state, command owner and revision may not.
            safe_boundary = (
                retask and step_boundary and changed == "step"
                and type(previous_index) is int and frame.active_motion_index == previous_index + 1
                and signature["step"][1] == previous["step"][1]
                and event_id == previous["event"] and (cadence_active if cadence else event_current)
                and signature["mode"][:2] == previous["mode"][:2]
                and signature["mode"][3:] == previous["mode"][3:]
                and signature["revision"] == previous["revision"]
            )
            if cadence and event_id != previous["event"]:
                self._finish(request, now, Admission("rejected", "preempted_by_event"))
            elif cadence and ((changed and not safe_boundary) or not cadence_active):
                self._finish(request, now, Admission("rejected", f"cadence_{changed or 'flight_state'}_changed"))
            elif (self.cadence_s > 0 or retask) and changed and not safe_boundary:
                self._finish(request, now, Admission("rejected", f"event_{changed}_changed"))
            elif now > request["deadline_ms"]:
                self._finish(request, now, Admission("rejected", "decision_timeout"))
            elif self.future.done():
                if retask and step_boundary is False and (cadence_active if cadence else event_current):
                    return
                current_id = request["event_id"] if cadence else event_id if event_current else 0
                finding = (offered_detection if offered_detection == injected_target_reading(perception) else None)
                self._apply_reply(request, offered, replace(frame, event_id=current_id), now, finding=finding)
                if self.revision != frame.revision:
                    self.future = None
                    return
        # A running call may not be cancellable. Drain it without accepting its
        # reply, then give the current event first use of the single worker.
        if self.future is not None and self.future.done():
            self.future = None
        if self.future is not None or (self.tool_runner is not None and self.tool_runner.backend_busy):
            return
        interval_s = max(1, self.cadence_s) * (1 if phase == "cruise" else 0.5)
        # The recorded cadence variant gets one immediate follow-up after the
        # event ticket drains, within the same short-lived sample's age budget.
        followup = (self.recorded_cadence_followup and perception is not None and event_current
                    and event_id == self.last_requested and event_id > self.last_followup_event
                    and snapshot["events"] and snapshot["events"][-1]["type"] == "target_found")
        cadence_due = (self.cadence_s > 0 and cadence_active and mission_progress is not None
                       and (followup or target_cadence_due or self.last_submit_ms is None
                            or (now - self.last_submit_ms) / 1000 >= interval_s))
        if not event_due and not cadence_due:
            return
        holds = dict(frame.hold_sites)
        progress = None if event_due else {**mission_progress, "recent_receipts": list(self.recent_receipts)}
        request = self.executor.open_request(snapshot, run_id=self.run_id, hold_refs=tuple(holds),
                                             remaining_plan_sha256=self.plan_digest if event_due else remaining_sha,
                                             mission_progress=progress)
        if self.audit is not None:
            _emit_audit(self.audit, kind="request_opened", request_id=request["request_id"], request=request)
        if event_due:
            self.last_requested = event_id
        elif followup or target_cadence_due:
            self.last_followup_event = event_id
        if event_due or target_cadence_due or (step_boundary is not None and perception is not None):
            # The audit record has the original sample, not the backend input.
            sample = {**request, "scenario_observation": perception} if perception is not None else request
            self._mark("request", request=sample)
        self.last_submit_ms = now
        self.pending = request, holds, signature, injected_target_reading(perception)
        if self.tool_runner is None:
            self.future = self.pool.submit(self.backend, request)
        else:
            self.tool_cancel = Event()
            self.future = self.pool.submit(self.tool_runner.run_window, request, self.backend,
                                           request["deadline_ms"], cancelled=self.tool_cancel)

    def _apply_reply(self, request, offered, frame, now, *, finding):
        proof = None
        proved = None
        injected_results = ()

        def record_proof(decision, current_frame):
            nonlocal proof, proved
            proof = self.build_proof(decision, current_frame)
            proved = decision, current_frame
            return proof

        try:
            reply = self.future.result()
            if self.tool_runner is not None:
                reply.check_fresh(now)
                wire = reply.wire
                injected_results = reply.injected_results
            else:
                wire = reply
        except ToolProtocolError as exc:
            reason = str(exc)
            receipt_reason = "decision_timeout" if reason == "window_timeout" else "tool_protocol_error"
            self._finish(request, now, Admission("rejected", receipt_reason))
            self._mark("error", request_id=request["request_id"], trigger=request.get("trigger", "event"),
                       error=type(exc).__name__, reason=reason)
            return
        except TimeoutError:
            self._finish(request, now, Admission("rejected", "decision_timeout"))
            return
        except Exception as exc:
            self._finish(request, now, Admission("rejected", "backend_error"), error=type(exc).__name__)
            return
        if isinstance(wire, str) and len(wire.encode("utf-8", errors="surrogatepass")) > MAX_DECISION_BYTES:
            wire = ""
        if _is_retask(wire) and frame.retask_finding_ne is not None and (
            finding is None or (self.tool_runner is not None and not any(
                row["tool"] == "target_status" and row["data"] == finding.data("target_status")
                for row in injected_results
            ))
        ):
            frame = replace(frame, retask_finding_ne=None, retask_finding_error_m=None)
        holds = {ref: site for ref, site in frame.hold_sites.items() if offered.get(ref) == site}
        admission = self.executor.apply(wire, replace(frame, hold_sites=holds),
                                        now_ms=now, build_proof=record_proof if self.build_proof else None)
        wire = wire.decode("utf-8", errors="replace") if isinstance(wire, bytes) else wire if isinstance(wire, str) else None
        self.last_admission = admission, proof, now, wire
        cadence_continue = request.get("trigger") == "cadence" and admission.action == "continue"
        trace = {}
        if self.provenance and not cadence_continue:
            trace["motion_proof"] = asdict(proof) if proof is not None else None
        if not cadence_continue:
            trace["wire"] = wire
            if injected_results:
                trace["injected_tool_results"] = injected_results
        self._finish(request, now, admission, **trace)
        if admission.status == "installed":
            self.installed_retask = proved
            self.revision = self.executor.mission.plan_version
            self.plan_digest = canonical_sha256(json.loads(wire)["params"]["plan"])
            self.record({"message_type": "scenario_plan_replaced", "request_id": request["request_id"],
                         "previous_revision": frame.revision, "revision": self.revision,
                         "step_index": frame.active_motion_index, "plan_sha256": self.plan_digest,
                         "receive_monotonic_ns": time.monotonic_ns()})

    def _finish(self, request, now, admission, **trace):
        if self.tool_cancel is not None:
            self.tool_cancel.set()
            self.tool_cancel = None
        self.executor.discard_request(request["request_id"])
        self.pending = None
        receipt = {"request_id": request["request_id"], "event_id": request["event_id"],
                   "trigger": request.get("trigger", "event"), **asdict(admission)}
        self.recent_receipts.append(receipt)
        suppressed = (request.get("trigger") == "cadence" and admission.action == "continue"
                      and admission.status == "rejected" and admission.reason in QUIET_CADENCE_FLIGHT_STATE_REASONS)
        latency_ms = now - request["state"]["t_ms"]
        if suppressed:
            self.quiet_cadence_rejected_continue[admission.reason] += 1
        else:
            progress = request.get("mission_progress")
            if progress is not None:
                trace.update(phase=progress["phase"], step_index=progress["step_index"])
            self._mark("receipt", **receipt, latency_ms=latency_ms, **self.provenance, **trace)
        if self.audit is not None:
            _emit_audit(self.audit, kind="receipt", request_id=receipt["request_id"],
                        status=admission.status, action=admission.action, reason=admission.reason,
                        latency_ms=latency_ms, suppressed=suppressed)

    def close(self):
        if self.pending is not None:
            request, _, _, _ = self.pending
            self._finish(request, self.now_ms(), Admission("rejected", "collector_exit"))
        if self.quiet_cadence_rejected_continue:
            self._mark("cadence_summary",
                       quiet_continue_rejected_count=sum(self.quiet_cadence_rejected_continue.values()),
                       by_reason=dict(self.quiet_cadence_rejected_continue))
        self.pool.shutdown(wait=False, cancel_futures=True)


MAVLINK_TYPES = ("BATTERY_STATUS", "WIND_COV", "SYS_STATUS", "HEARTBEAT", "GPS_RAW_INT",
                 "ESTIMATOR_STATUS", "EXTENDED_SYS_STATE", "VFR_HUD", "TERRAIN_REPORT", "MISSION_CURRENT")


class GimbalIngress:
    """Reuse the collector's existing single gimbal writer and readback loop."""

    hold = None

    def gimbal_control(self, *, pitch_deg, yaw_deg):
        hold = self.hold
        if hold is None or hold.awaiting_readback or hold.session.poll() not in {"idle", "ready"}:
            return SimpleNamespace(ok=False, ack_result=1)
        previous_target, previous_stamp = hold.target, hold.sent_vehicle_ms
        hold.target = {"pitch": pitch_deg, "yaw": yaw_deg, "roll": 0.0}
        hold.sent_vehicle_ms = None
        try:
            hold.tick()
        finally:
            if not hold.awaiting_readback:
                hold.target, hold.sent_vehicle_ms = previous_target, previous_stamp
        return SimpleNamespace(ok=hold.awaiting_readback, ack_result=None)

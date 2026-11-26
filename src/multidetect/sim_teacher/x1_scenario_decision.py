"""Collector-side Altair request and receipt plumbing for X1 SITL."""
from __future__ import annotations

import json
import math
from collections import deque
import time
from concurrent.futures import ThreadPoolExecutor
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
from multidetect.autonomy.x1_contingency_state import X1StateBuilder
from multidetect.autonomy.x1_perception_tools import TOOL_SCHEMA, ToolProtocolError


def llama_decisions(port, sentence):
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
        with urlopen(req, timeout=2.5) as response:
            wire = json.loads(response.read(65_536))["choices"][0]["message"]["content"]
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
                 build_proof=None, cpu_replay=False, native_port=None, decision_cadence_s=0, tool_runner=None):
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
            X1AirbornePlanAdapter(revision, replace_remaining=lambda *_: False),
            port, package, X1Envelope(profile), claim_native=claim,
            capabilities=frozenset(package.document["capabilities"]),
        )
        self.mission_id = package.mission_id
        self.revision = revision
        self.backend, self.record, self.now_ms = backend, record, now_ms
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
        self.recent_receipts = deque(maxlen=4)
        self.last_admission = None

    def _mark(self, kind, **fields):
        self.record({"message_type": f"scenario_decision_{kind}",
                     "receive_monotonic_ns": time.monotonic_ns(), **fields})

    def tick(self, frame, *, mav_rows=(), observations=(), events=(), event_current=True, wind=None,
             mission_progress=None, mode=None, cadence_active=True):
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
            request, offered, previous = self.pending
            cadence = request.get("trigger") == "cadence"
            changed = next((key for key in ("step", "mode", "revision") if signature[key] != previous[key]), None)
            if cadence and event_id != previous["event"]:
                self._finish(request, now, Admission("rejected", "preempted_by_event"))
            elif cadence and (changed or not cadence_active):
                self._finish(request, now, Admission("rejected", f"cadence_{changed or 'flight_state'}_changed"))
            elif self.cadence_s > 0 and changed:
                self._finish(request, now, Admission("rejected", f"event_{changed}_changed"))
            elif now > request["deadline_ms"]:
                self._finish(request, now, Admission("rejected", "decision_timeout"))
            elif self.future.done():
                current_id = request["event_id"] if cadence else event_id if event_current else 0
                self._apply_reply(request, offered, replace(frame, event_id=current_id), now)
        # A running call may not be cancellable. Drain it without accepting its
        # reply, then give the current event first use of the single worker.
        if self.future is not None and self.future.done():
            self.future = None
        if self.future is not None or (self.tool_runner is not None and self.tool_runner.backend_busy):
            return
        interval_s = max(1, self.cadence_s) * (1 if phase == "cruise" else 0.5)
        cadence_due = (self.cadence_s > 0 and cadence_active and mission_progress is not None
                       and (self.last_submit_ms is None or (now - self.last_submit_ms) / 1000 >= interval_s))
        if not event_due and not cadence_due:
            return
        holds = dict(frame.hold_sites)
        progress = None if event_due else {**mission_progress, "recent_receipts": list(self.recent_receipts)}
        request = self.executor.open_request(snapshot, run_id=self.run_id, hold_refs=tuple(holds),
                                             remaining_plan_sha256=self.plan_digest if event_due else remaining_sha,
                                             mission_progress=progress)
        if event_due:
            self.last_requested = event_id
            self._mark("request", request=request)
        self.last_submit_ms = now
        self.pending = request, holds, signature
        if self.tool_runner is None:
            self.future = self.pool.submit(self.backend, request)
        else:
            self.tool_cancel = Event()
            self.future = self.pool.submit(self.tool_runner.run_window, request, self.backend,
                                           request["deadline_ms"], cancelled=self.tool_cancel)

    def _apply_reply(self, request, offered, frame, now):
        proof = None

        def record_proof(decision, current_frame):
            nonlocal proof
            proof = self.build_proof(decision, current_frame)
            return proof

        try:
            reply = self.future.result()
            if self.tool_runner is not None:
                reply.check_fresh(now)
                wire = reply.wire
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
        holds = {ref: site for ref, site in frame.hold_sites.items() if offered.get(ref) == site}
        admission = self.executor.apply(wire, replace(frame, hold_sites=holds),
                                        now_ms=now, build_proof=record_proof if self.build_proof else None)
        wire = wire if isinstance(wire, str) else None
        self.last_admission = admission, proof, now, wire
        cadence_continue = request.get("trigger") == "cadence" and admission.action == "continue"
        trace = {}
        if self.provenance and not cadence_continue:
            trace["motion_proof"] = asdict(proof) if proof is not None else None
        if not cadence_continue:
            trace["wire"] = wire
        self._finish(request, now, admission, **trace)

    def _finish(self, request, now, admission, **trace):
        if self.tool_cancel is not None:
            self.tool_cancel.set()
            self.tool_cancel = None
        self.executor.discard_request(request["request_id"])
        self.pending = None
        receipt = {"request_id": request["request_id"], "event_id": request["event_id"],
                   "trigger": request.get("trigger", "event"), **asdict(admission)}
        self.recent_receipts.append(receipt)
        progress = request.get("mission_progress")
        if progress is not None:
            trace.update(phase=progress["phase"], step_index=progress["step_index"])
        self._mark("receipt", **receipt, latency_ms=now - request["state"]["t_ms"], **self.provenance, **trace)

    def close(self):
        if self.pending is not None:
            request, _, _ = self.pending
            self._finish(request, self.now_ms(), Admission("rejected", "collector_exit"))
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

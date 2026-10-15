"""Mission-level policy above the executor; communication is always optional."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned

from .mission_executor import MissionExecutor
from .mission_package import POLICIES, MissionPackage, number, parse_policy
from .mission_plan import validate_mission_plan
from .onboard_mission_log import DownlinkQueue, OnboardMissionLog


class MissionSupervisor:
    """Wrap a validated executor without changing its step graph or safety layer.

    Return and land decisions latch. A reconnect can release a policy hold but
    cannot resume an abandoned mission. Updates can only tighten the policy or
    its existing deadline. Event timestamps use the host clock, not remote time.
    """

    def __init__(
        self,
        executor: MissionExecutor,
        package: MissionPackage,
        *,
        log: OnboardMissionLog,
        downlink: DownlinkQueue | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        package.validate_plan(executor.plan)
        self.executor, self.package, self.log, self.downlink = executor, package, log, downlink
        self.clock = clock
        self.started = self._now = clock()
        if not math.isfinite(self.started):
            raise ValueError("invalid supervisor clock")
        self.last_heartbeat: float | None = None
        self.policy, self.timeout_s = parse_policy(package.document["link_loss_policy"])
        self.paused_at: float | None = None
        self.action, self.reason = "continue", "mission admitted; onboard operation enabled"
        self._terminal: str | None = None
        self._recovery: MissionExecutor | None = None
        self._recovery_kind: str | None = None
        self._brake_pending = False
        self._low_confidence = 0
        self._remaining_time: float | None = None
        self._time_estimated_at = self.started
        self._remaining_energy: float | None = None
        self._finished = False
        executor.home = tuple(package.document["home"])
        self._record("mission_started", package=package.document, plan=asdict(executor.plan))

    def __getattr__(self, name):
        return getattr(self.executor, name)

    @property
    def done(self) -> bool:
        if self._terminal is not None:
            skill = "land" if self._terminal == "land_now" else "return_home"
            if self._recovery_kind != skill or self._brake_pending:
                return False
        return self._recovery.done if self._recovery is not None else self.executor.done

    @property
    def connected(self) -> bool:
        return (
            self.last_heartbeat is not None
            and self._now - self.last_heartbeat < self.package.document["heartbeat_timeout_s"]
        )

    def _record(self, kind: str, **payload) -> None:
        self.log.append(kind, self._now, **payload)
        if self.downlink is not None:
            self.downlink.notify()

    def _time(self) -> None:
        now = self.clock()
        if not math.isfinite(now) or now < self._now:
            raise ValueError("invalid supervisor clock")
        self._now = now

    def ground_event(self, event: dict[str, Any]) -> bool:
        self._time()
        if event.get("mission_id") != self.package.mission_id:
            self._record("ground_event_rejected", reason="mission id mismatch")
            return False
        kind = event.get("type")
        if kind not in {"heartbeat", "abort", "pause", "resume", "policy_update"}:
            self._record("ground_event_rejected", reason="unknown event type")
            return False
        try:
            stamp = number(event.get("timestamp", self._now), "event timestamp")
            if not self.started <= stamp <= self._now:
                raise ValueError("event timestamp outside this flight")
            if kind == "policy_update":
                policy, timeout = parse_policy(event.get("policy"))
                old_rank, new_rank = POLICIES.index(self.policy), POLICIES.index(policy)
                timed = policy in {"continue_then_return", "hold_then_return"}
                if new_rank < old_rank or (
                    new_rank == old_rank and timed and timeout > self.timeout_s
                ):
                    raise ValueError("policy update would relax operator policy")
                if timed and self.policy in {"continue_then_return", "hold_then_return"}:
                    # A stronger hold policy must not extend the old return deadline.
                    old_deadline = self.timeout_s + (
                        self.package.document["heartbeat_timeout_s"]
                        if self.policy == "hold_then_return"
                        else 0
                    )
                    offset = (
                        self.package.document["heartbeat_timeout_s"]
                        if policy == "hold_then_return"
                        else 0
                    )
                    if old_deadline < offset:
                        raise ValueError("policy update would extend the existing return deadline")
                    timeout = max(0.0, min(timeout, old_deadline - offset))
                self.policy, self.timeout_s = policy, timeout
            elif kind == "heartbeat":
                if self.last_heartbeat is not None and stamp < self.last_heartbeat:
                    raise ValueError("stale heartbeat")
                self.last_heartbeat = stamp
            elif kind == "abort":
                self._escalate("return_now", "explicit operator abort")
            elif kind == "pause":
                if self.paused_at is None:
                    self.paused_at = self._now
            elif kind == "resume":
                self.paused_at = None
        except (ValueError, TypeError, KeyError) as exc:
            self._record("ground_event_rejected", reason=str(exc), event_type=kind)
            return False
        self._record(
            "ground_event",
            event_type=kind,
            received_timestamp=stamp,
            policy=self.policy,
            timeout_s=self.timeout_s,
        )
        if self.downlink is not None:
            self.downlink.set_connected(self.connected)
        return True

    def update_estimates(
        self, *, remaining_time_s=None, remaining_energy_wh=None, low_confidence_count=None
    ) -> None:
        self._time()
        if remaining_time_s is not None:
            self._remaining_time = number(remaining_time_s, "remaining_time_s")
            self._time_estimated_at = self._now
        if remaining_energy_wh is not None:
            self._remaining_energy = number(remaining_energy_wh, "remaining_energy_wh")
        if low_confidence_count is not None:
            if (
                isinstance(low_confidence_count, bool)
                or not isinstance(low_confidence_count, int)
                or low_confidence_count < 0
            ):
                raise ValueError("low_confidence_count must be a nonnegative integer")
            self._low_confidence = low_confidence_count
        self._record(
            "estimates",
            remaining_time_s=self._remaining_time,
            remaining_energy_wh=self._remaining_energy,
            low_confidence_count=self._low_confidence,
        )

    def _escalate(self, action: str, reason: str) -> None:
        if self._terminal is None or POLICIES.index(action) > POLICIES.index(self._terminal):
            self._terminal = action
            self._finished = False
            self._brake_pending = action == "return_now"
            self.reason = reason
            self._record("escalation", action=action, reason=reason, operator_policy=self.policy)

    def evaluate(self, *, pose_ned=None, proposed_action=None, yaw_deg=0.0) -> str:
        self._time()
        doc = self.package.document
        silence = self._now - (
            self.last_heartbeat if self.last_heartbeat is not None else self.started
        )
        lost = not self.connected and silence >= doc["heartbeat_timeout_s"]
        action, reason = "continue", "operator policy permits autonomous execution"
        if lost or self.paused_at is not None:
            if self.policy in {"return_now", "land_now"}:
                self._escalate(
                    self.policy,
                    "operator link-loss policy" if lost else "pause bounded by operator policy",
                )
            elif self.policy in {"continue_then_return", "hold_then_return"}:
                deadline = self.timeout_s + (
                    doc["heartbeat_timeout_s"] if lost and self.policy == "hold_then_return" else 0
                )
                silence_expired = lost and silence >= deadline
                pause_expired = (
                    self.paused_at is not None and self._now - self.paused_at >= self.timeout_s
                )
                if silence_expired or pause_expired:
                    self._escalate("return_now", "operator policy silence/pause timeout")
                elif self.policy == "hold_then_return" or self.paused_at is not None:
                    action, reason = "hold", "waiting for link or operator resume"
            elif self.paused_at is not None:
                action, reason = "hold", "operator pause; awaiting resume"
        budget = doc["budget"]
        remaining = budget["time_s"] - (self._now - self.started)
        if self._remaining_time is not None:
            remaining = min(remaining, self._remaining_time - (self._now - self._time_estimated_at))
        if remaining <= budget["reserve_time_s"]:
            self._escalate("return_now", "remaining time at or below reserve")
        if (
            self._remaining_energy is not None
            and self._remaining_energy <= budget["reserve_energy_wh"]
        ):
            self._escalate("return_now", "remaining energy at or below reserve")
        if self._low_confidence >= 3:
            self._escalate("return_now", "repeated low-confidence perception verdicts")
        if lost:
            step = self.executor.current_step
            if step is not None and step.id in doc["oversight_steps"]:
                self._escalate(
                    "return_now", f"step {step.id} requires operator oversight without link"
                )
            if pose_ned is not None:
                end = pose_ned[:2]
                if proposed_action is not None and proposed_action.kind == "translate":
                    dn, de, _ = body_to_ned(
                        proposed_action.forward_m,
                        proposed_action.right_m,
                        proposed_action.up_m,
                        yaw_deg,
                    )
                    end = (pose_ned[0] + dn, pose_ned[1] + de)
                for name, zone in doc["zones"].items():
                    if not zone.get("oversight_required") or not zone.get("risk_tag"):
                        continue
                    # Check the whole proposed segment, including crossing a zone.
                    a = pose_ned[:2]
                    d = (end[0] - a[0], end[1] - a[1])
                    c = zone["centre_ne"]
                    length2 = d[0] ** 2 + d[1] ** 2
                    t = (
                        max(0, min(1, ((c[0] - a[0]) * d[0] + (c[1] - a[1]) * d[1]) / length2))
                        if length2
                        else 0
                    )
                    if math.dist(c, (a[0] + t * d[0], a[1] + t * d[1])) <= zone["radius_m"]:
                        self._escalate(
                            "return_now",
                            f"high-risk zone {name} ({zone['risk_tag']}) "
                            "requires oversight without link",
                        )
        if self._terminal is not None:
            action, reason = self._terminal, self.reason
        self.action, self.reason = action, reason
        if self.downlink is not None:
            self.downlink.set_connected(self.connected)
        self._record(
            "decision",
            action=action,
            reason=reason,
            connected=self.connected,
            silence_s=silence,
            step=self.executor.current_step.id if self.executor.current_step else None,
        )
        return action

    def observe(self, *args, **kwargs):
        if self.evaluate(pose_ned=kwargs.get("pose_ned")) != "continue":
            return []
        return self.executor.observe(*args, **kwargs)

    def apply_verdict(self, track_id, decision, confidence=None, **kwargs):
        bad = decision is None or (confidence is not None and confidence < 0.5)
        self._low_confidence = self._low_confidence + 1 if bad else 0
        self.executor.apply_verdict(track_id, decision, confidence, **kwargs)
        self._time()
        self._record(
            "perception_verdict",
            track_id=track_id,
            decision=decision,
            confidence=confidence,
            findings=self.executor.findings,
            report=asdict(self.executor.report()),
        )

    def _recover(self, pose_ned) -> MissionExecutor:
        skill = "land" if self.action == "land_now" else "return_home"
        if self._recovery is None or self._recovery_kind != skill:
            plan = validate_mission_plan(
                {
                    "plan_version": 1,
                    "goal": "mission safety recovery",
                    "report": ["status"],
                    "steps": [
                        {
                            "id": "recovery",
                            "skill": skill,
                            "params": {},
                            "on_success": None,
                            "on_fail": None,
                        }
                    ],
                }
            )
            self._recovery = MissionExecutor(
                plan,
                loop_factory=self.executor.loop_factory,
                refs=self.package.refs,
                config=self.executor.config,
            )
            self._recovery.home = tuple(self.package.document["home"])
            self._recovery.cruise_altitude_m = self.executor.cruise_altitude_m or max(
                0, -pose_ned[2]
            )
            self._recovery_kind = skill
        return self._recovery

    def next_action(self, **kwargs):
        if self.executor.cruise_altitude_m is None:
            area = self.package.document["operating_area"]
            self.executor.cruise_altitude_m = min(
                area["max_altitude_m"], max(area["min_altitude_m"], -kwargs["pose_ned"][2])
            )
        self.evaluate(pose_ned=kwargs["pose_ned"])
        if self.action == "continue":
            result = self.executor.next_action(**kwargs)
            self.evaluate(
                pose_ned=kwargs["pose_ned"], yaw_deg=kwargs["yaw_deg"], proposed_action=result[0]
            )
        else:
            result = None
        if self.action == "hold" or self._brake_pending:
            self._brake_pending = False
            result = ActionPrimitive.hold(self.executor.config.hold_s), {}
        elif self.action in {"return_now", "land_now"}:
            result = self._recover(kwargs["pose_ned"]).next_action(**kwargs)
        action, info = result
        info = {**info, "supervisor_action": self.action, "supervisor_reason": self.reason}
        self._record(
            "execution",
            action=action.to_dict(),
            state=info,
            report=asdict(self.executor.report()),
            findings=self.executor.findings,
            history=self.executor.history,
            counts=self.executor.counts,
        )
        if self.done and not self._finished:
            self.finish("completed" if self._terminal is None else "recovery_finished")
        return action, info

    def finish(self, status: str = "stopped") -> None:
        if self._finished:
            return
        self._time()
        self.log.finish(
            self._now,
            {
                "status": status,
                "action": self.action,
                "reason": self.reason,
                "report": asdict(self.executor.report()),
                "findings": self.executor.findings,
                "history": self.executor.history,
                "counts": self.executor.counts,
                "recovery_history": self._recovery.history if self._recovery else [],
            },
        )
        self._finished = True
        if self.downlink is not None:
            self.downlink.notify()

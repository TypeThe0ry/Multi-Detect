"""Run a validated MissionPlan on board, one bounded skill at a time (pure logic).

The executor turns the multi-purpose plan contract (``mission_plan``) into
primitives. Perception skills reuse ``OnboardLoop``: FAST proposals become
ground-anchored tracks, the SLOW model gives identity verdicts for the task
text of the step, and the host decides approach and confirmation. Geometry
skills (goto, inspect orbit, hover, return home) are host logic with the same
static-map clearance check. Findings, counts and evidence come only from what
the vehicle observed; nothing here reads the simulator's answer key.

Per control cycle the host calls ``observe`` (detections -> tracks that need a
slow verdict), ``apply_verdict`` for each answered track, then ``next_action``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from multidetect.autonomy.mission_plan import (
    MissionPlan,
    MissionReport,
    MissionStep,
    validate_mission_plan,
)
from multidetect.autonomy.onboard_loop import Detection, OnboardLoop, Track
from multidetect.sim_teacher.primitives import ActionPrimitive

GroundPoint = Callable[[float, float], tuple[float, float] | None]
Pose = tuple[float, float, float]
PERCEPTION_SKILLS = frozenset({"area_search", "count", "inspect"})


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def task_text_for(step: MissionStep) -> str:
    """The wording the slow model was trained on, with the step's query."""
    return f"Find the {step.params['query']} and record its position."


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    step_budget: int = 60
    # Flying skills (goto, inspect, return_home) are bounded separately from searches.
    travel_budget: int = 150
    max_translate_m: float = 15.0
    arrive_tolerance_m: float = 3.0
    turn_tolerance_deg: float = 10.0
    min_altitude_m: float = 8.0
    # Inspection views look at the centre this far below the camera axis.
    inspect_depression_margin_rad: float = 0.35
    max_blocked_legs: int = 2
    hold_s: float = 1.0


@dataclass(slots=True)
class _StepRun:
    step: MissionStep
    loop: OnboardLoop | None = None
    primitives: int = 0
    state: str = "running"
    reason: str | None = None
    # Geometry skills.
    goal_ne: tuple[float, float] | None = None
    goal_altitude_m: float | None = None
    blocked: int = 0
    held_s: float = 0.0
    # inspect
    centre_ne: tuple[float, float] | None = None
    viewpoints: list[tuple[float, float]] = field(default_factory=list)
    view_index: int = 0
    phase: str = "fly"
    captured: int = 0
    skipped: int = 0
    pending_track: str | None = None


class MissionExecutor:
    def __init__(
        self,
        plan: MissionPlan | Mapping[str, Any] | str,
        *,
        loop_factory: Callable[..., OnboardLoop],
        refs: Mapping[str, tuple[float, float]] | None = None,
        config: ExecutorConfig | None = None,
        task_text: Callable[[MissionStep], str] = task_text_for,
    ) -> None:
        self.plan = plan if isinstance(plan, MissionPlan) else validate_mission_plan(plan)
        self.loop_factory = loop_factory
        self.refs = dict(refs or {})
        self.config = config or ExecutorConfig()
        self.task_text = task_text
        self._steps = {step.id: step for step in self.plan.steps}
        # A probe loop gives the camera model and the static-map clearance check.
        self._probe = loop_factory("Find the object and record its position.")
        self.home: tuple[float, float] | None = None
        self.cruise_altitude_m: float | None = None
        self.findings: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.evidence: list[str] = []
        self.track_ids: list[str] = []
        self.answers: list[str] = []
        self.alerts: list[str] = []
        self.history: list[dict[str, Any]] = []
        self.done = False
        self._run: _StepRun | None = self._start(self.plan.steps[0])
        self._current: list[Track] = []

    # -- step lifecycle ---------------------------------------------------
    def _start(self, step: MissionStep) -> _StepRun:
        run = _StepRun(step=step)
        if step.skill == "area_search":
            run.loop = self.loop_factory(self.task_text(step))
        elif step.skill == "count":
            run.loop = self.loop_factory(self.task_text(step), stop_on_confirm=False)
        elif step.skill == "inspect":
            run.loop = self.loop_factory(self.task_text(step))
        return run

    def _finish(self, state: str, reason: str | None = None) -> None:
        run = self._run
        assert run is not None
        run.state, run.reason = state, reason
        self.history.append(
            {"step": run.step.id, "skill": run.step.skill, "state": state, "reason": reason}
        )
        if state == "failed":
            self.alerts.append(f"{run.step.id}:{reason or 'failed'}")
        following = run.step.on_success if state == "succeeded" else run.step.on_fail
        if following is None:
            self.done, self._run = True, None
        else:
            self._run = self._start(self._steps[following])

    @property
    def current_step(self) -> MissionStep | None:
        return self._run.step if self._run is not None else None

    @property
    def current_task_text(self) -> str | None:
        step = self.current_step
        return self.task_text(step) if step is not None and "query" in step.params else None

    @property
    def current_tracks(self) -> Sequence[Track]:
        return tuple(self._current)

    @property
    def active_loop(self) -> OnboardLoop | None:
        return self._run.loop if self._run is not None else None

    def resolve(self, ref: str) -> tuple[float, float] | None:
        if ref == "home":
            return self.home
        if ref.startswith("finding:"):
            try:
                index = int(ref.split(":", 1)[1])
            except ValueError:
                return None
            if 1 <= index <= len(self.findings):
                return tuple(self.findings[index - 1]["position_ne"])
            return None
        return self.refs.get(ref)

    # -- perception hooks ---------------------------------------------------
    def observe(
        self,
        detections: Sequence[Detection],
        *,
        pose_ned: Pose,
        yaw_deg: float,
        ground_point: GroundPoint | None = None,
    ) -> list[Track]:
        """Update the active step's tracks; return the tracks that need a slow verdict."""
        self._init_home(pose_ned)
        run = self._run
        if run is None or run.loop is None:
            self._current = []
            return []
        self._current = run.loop.update_tracks(
            detections, pose_ned=pose_ned, yaw_deg=yaw_deg, ground_point=ground_point
        )
        if run.step.skill == "inspect":
            if run.phase != "capture":
                return []
            nearest = self._nearest_to_centre(run, self._current)
            run.pending_track = nearest.track_id if nearest else None
            return [nearest] if nearest else []
        if run.loop.state not in {"searching", "approaching"}:
            return []
        return run.loop.tracks_needing_verdict(self._current)

    def apply_verdict(self, track_id: str, decision: str | None) -> None:
        run = self._run
        if run is None or run.loop is None:
            return
        run.loop.apply_verdict(track_id, decision)
        if run.step.skill == "inspect" and track_id == run.pending_track:
            self.answers.append(f"{run.step.id}:view{run.view_index + 1}:{decision}")

    # -- control -------------------------------------------------------------
    def _init_home(self, pose_ned: Pose) -> None:
        if self.home is None:
            self.home = (pose_ned[0], pose_ned[1])
            self.cruise_altitude_m = max(0.0, -pose_ned[2])

    def next_action(
        self,
        *,
        pose_ned: Pose,
        yaw_deg: float,
        ground_point: GroundPoint | None = None,
        frame_id: str | None = None,
    ) -> tuple[ActionPrimitive, dict[str, Any]]:
        self._init_home(pose_ned)
        for _ in range(len(self.plan.steps) + 1):
            run = self._run
            if run is None:
                return ActionPrimitive.stop(), {"mission": "done"}
            if run.primitives >= self._budget(run.step):
                self._budget_end(run)
                continue
            result = self._act(run, pose_ned, yaw_deg, ground_point, frame_id)
            if result is None:
                continue  # the step finished without moving; start the next one now
            action, info = result
            run.primitives += 1
            info.update(step=run.step.id, skill=run.step.skill, step_state=run.state)
            return action, info
        return ActionPrimitive.hold(self.config.hold_s), {"mission": "stalled"}

    def _budget(self, step: MissionStep) -> int:
        if step.skill == "hover":
            return max(self.config.step_budget, int(float(step.params["duration_s"])) + 2)
        if step.skill in {"goto", "inspect", "return_home"}:
            return self.config.travel_budget
        return self.config.step_budget

    def _budget_end(self, run: _StepRun) -> None:
        if run.step.skill == "count":
            self._report_count(run)
            self._finish("succeeded", "budget_reached")
        else:
            self._finish("failed", "step_budget")

    def _act(self, run, pose, yaw, ground_point, frame_id):
        skill = run.step.skill
        handler = {
            "area_search": self._area_search,
            "count": self._count,
            "inspect": self._inspect,
            "goto": self._goto,
            "hover": self._hover,
            "return_home": self._return_home,
            "land": self._land,
            "record": self._record,
        }.get(skill)
        if handler is None:
            self._finish("failed", "skill_not_implemented")
            return None
        return handler(run, pose, yaw, ground_point, frame_id)

    # -- perception skills ---------------------------------------------------
    def _area_search(self, run, pose, yaw, ground_point, frame_id):
        action, info = run.loop.next_action(
            self._current, pose_ned=pose, yaw_deg=yaw, ground_point=ground_point
        )
        if run.loop.confirmed is not None:
            self._add_finding(run, run.loop.confirmed, frame_id)
            self._finish("succeeded", "confirmed")
            return ActionPrimitive.hold(self.config.hold_s), info
        return action, info

    def _count(self, run, pose, yaw, ground_point, frame_id):
        action, info = run.loop.next_action(
            self._current, pose_ned=pose, yaw_deg=yaw, ground_point=ground_point
        )
        if "counted" in info:
            self._add_finding(run, info["counted"], frame_id)
        limit = run.step.params.get("max_count")
        if limit is not None and len(run.loop.confirmations) >= int(limit):
            self._report_count(run)
            self._finish("succeeded", "max_count")
            return ActionPrimitive.hold(self.config.hold_s), info
        return action, info

    def _report_count(self, run: _StepRun) -> None:
        self.counts[run.step.id] = len(run.loop.confirmations) if run.loop else 0

    def _add_finding(self, run: _StepRun, record: Mapping[str, Any], frame_id: str | None):
        position = record.get("position_estimate_ne")
        if position is None:
            return
        self.findings.append(
            {
                "step": run.step.id,
                "query": run.step.params.get("query"),
                "position_ne": (float(position[0]), float(position[1])),
                "track_id": record.get("track_id"),
                "bbox": record.get("bbox"),
                "rule": record.get("rule"),
                "frame_id": frame_id,
            }
        )
        self.track_ids.append(str(record.get("track_id")))
        if frame_id:
            self.evidence.append(frame_id)

    # -- inspect: orbit the referenced ground point ----------------------------
    def _inspect_altitude(self, run: _StepRun) -> float:
        if "altitude_m" in run.step.params:
            return float(run.step.params["altitude_m"])
        camera = self._probe.camera
        depression = camera.pitch_down_rad + self.config.inspect_depression_margin_rad
        standoff = float(run.step.params["standoff_m"])
        return max(self.config.min_altitude_m, standoff * math.tan(depression))

    def _plan_views(self, run: _StepRun, pose: Pose) -> None:
        centre = self.resolve(str(run.step.params["target_ref"]))
        run.centre_ne = centre
        if centre is None:
            return
        views = int(run.step.params["views"])
        standoff = float(run.step.params["standoff_m"])
        start = math.atan2(pose[1] - centre[1], pose[0] - centre[0])
        run.viewpoints = [
            (
                centre[0] + standoff * math.cos(start + 2.0 * math.pi * k / views),
                centre[1] + standoff * math.sin(start + 2.0 * math.pi * k / views),
            )
            for k in range(views)
        ]
        run.goal_altitude_m = self._inspect_altitude(run)

    def _nearest_to_centre(self, run: _StepRun, current: Sequence[Track]) -> Track | None:
        if run.centre_ne is None:
            return None
        anchored = [t for t in current if t.ground is not None]
        if anchored:
            best = min(
                anchored,
                key=lambda t: math.hypot(
                    t.ground[0] - run.centre_ne[0], t.ground[1] - run.centre_ne[1]
                ),
            )
            gap = math.hypot(best.ground[0] - run.centre_ne[0], best.ground[1] - run.centre_ne[1])
            return best if gap <= self._probe.config.ground_gate_min_m * 2 else None
        return None

    def _inspect(self, run, pose, yaw, ground_point, frame_id):
        if run.centre_ne is None and not run.viewpoints:
            self._plan_views(run, pose)
            if run.centre_ne is None:
                self._finish("failed", "unknown_ref")
                return None
        if run.phase == "capture":
            if frame_id:
                self.evidence.append(frame_id)
            run.captured += 1
            run.view_index += 1
            run.phase = "fly"
        while run.view_index < len(run.viewpoints):
            moved = self._move_to(
                run, pose, yaw, run.viewpoints[run.view_index], run.goal_altitude_m
            )
            if moved == "blocked":
                run.skipped += 1
                self.alerts.append(f"{run.step.id}:view{run.view_index + 1}_blocked")
                run.view_index += 1
                run.blocked = 0
                continue
            if moved is not None:
                return moved, {"view": run.view_index + 1, "phase": "fly"}
            # Arrived: face the centre, then capture on the next cycle.
            bearing = math.degrees(
                math.atan2(run.centre_ne[1] - pose[1], run.centre_ne[0] - pose[0])
            )
            delta = _wrap(bearing - yaw)
            if abs(delta) > self.config.turn_tolerance_deg:
                return ActionPrimitive.turn(delta), {"view": run.view_index + 1, "phase": "face"}
            run.phase = "capture"
            return ActionPrimitive.hold(self.config.hold_s), {
                "view": run.view_index + 1,
                "phase": "capture",
            }
        if run.captured:
            self._finish("succeeded", "views_captured")
        else:
            self._finish("failed", "no_view_reachable")
        return None

    # -- geometry skills -------------------------------------------------------
    def _move_to(self, run, pose, yaw, goal_ne, altitude_m=None):
        """One primitive toward ``goal_ne`` (altitude first); None when arrived."""
        altitude = max(0.0, -pose[2])
        if altitude_m is not None and abs(altitude - altitude_m) > 1.0:
            target = (pose[0], pose[1], -altitude_m)
            if altitude_m < altitude and not self._probe.segment_clear(target, pose[0], pose[1]):
                run.blocked += 1
                if run.blocked > self.config.max_blocked_legs:
                    return "blocked"
                return ActionPrimitive.hold(self.config.hold_s)
            return ActionPrimitive.translate(0.0, 0.0, altitude_m - altitude)
        dn, de = goal_ne[0] - pose[0], goal_ne[1] - pose[1]
        distance = math.hypot(dn, de)
        if distance <= self.config.arrive_tolerance_m:
            return None
        delta = _wrap(math.degrees(math.atan2(de, dn)) - yaw)
        if abs(delta) > self.config.turn_tolerance_deg:
            return ActionPrimitive.turn(delta)
        step = min(self.config.max_translate_m, distance)
        heading = math.radians(yaw)
        end = (pose[0] + step * math.cos(heading), pose[1] + step * math.sin(heading))
        if not self._probe.segment_clear(pose, end[0], end[1]):
            run.blocked += 1
            if run.blocked > self.config.max_blocked_legs:
                return "blocked"
            return ActionPrimitive.hold(self.config.hold_s)
        run.blocked = 0
        return ActionPrimitive.translate(step, 0.0, 0.0)

    def _goto(self, run, pose, yaw, ground_point, frame_id):
        if run.goal_ne is None:
            run.goal_ne = self.resolve(str(run.step.params["target_ref"]))
            if run.goal_ne is None:
                self._finish("failed", "unknown_ref")
                return None
            if "altitude_m" in run.step.params:
                run.goal_altitude_m = float(run.step.params["altitude_m"])
        moved = self._move_to(run, pose, yaw, run.goal_ne, run.goal_altitude_m)
        if moved == "blocked":
            self._finish("failed", "blocked")
            return None
        if moved is None:
            self._finish("succeeded", "arrived")
            return None
        return moved, {}

    def _return_home(self, run, pose, yaw, ground_point, frame_id):
        if run.goal_ne is None:
            run.goal_ne = self.home
            altitude = run.step.params.get("altitude_m", self.cruise_altitude_m)
            run.goal_altitude_m = None if altitude is None else float(altitude)
        current = max(0.0, -pose[2])
        if run.goal_altitude_m is not None and current < run.goal_altitude_m - 1.0:
            # Climb back to the map-safe cruise altitude before flying home.
            return ActionPrimitive.translate(0.0, 0.0, run.goal_altitude_m - current), {}
        moved = self._move_to(run, pose, yaw, run.goal_ne, None)
        if moved == "blocked":
            self._finish("failed", "blocked")
            return None
        if moved is None:
            self._finish("succeeded", "arrived")
            return None
        return moved, {}

    def _hover(self, run, pose, yaw, ground_point, frame_id):
        remaining = float(run.step.params["duration_s"]) - run.held_s
        if remaining <= 1e-6:
            self._finish("succeeded", "duration_elapsed")
            return None
        seconds = min(5.0, remaining)
        run.held_s += seconds
        return ActionPrimitive.hold(seconds), {"remaining_s": remaining - seconds}

    def _land(self, run, pose, yaw, ground_point, frame_id):
        self._finish("succeeded", "landed")
        return ActionPrimitive.stop(), {}

    def _record(self, run, pose, yaw, ground_point, frame_id):
        if frame_id:
            self.evidence.append(frame_id)
        self._finish("succeeded", "views_captured")
        return None

    # -- report -----------------------------------------------------------------
    def report(self) -> MissionReport:
        return MissionReport(
            findings=tuple(str(f["query"]) for f in self.findings),
            count=sum(self.counts.values()) if self.counts else None,
            evidence_frame_ids=tuple(self.evidence),
            track_ids=tuple(self.track_ids),
            positions=tuple(
                f"{f['position_ne'][0]:.1f},{f['position_ne'][1]:.1f}" for f in self.findings
            ),
            state_answers=tuple(self.answers),
            alerts=tuple(self.alerts),
        )


def _plan(goal: str, steps: list[dict[str, Any]]) -> MissionPlan:
    return validate_mission_plan(
        {"plan_version": 1, "goal": goal, "steps": steps, "report": ["findings", "positions"]}
    )


def plan_for_find_task(query: str) -> MissionPlan:
    """Host template: search for ``query``, then return home either way."""
    return _plan(
        f"find {query}",
        [
            {
                "id": "search",
                "skill": "area_search",
                "params": {"region": "zone:all", "query": query},
                "on_success": "home",
                "on_fail": "home",
            },
            {
                "id": "home",
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            },
        ],
    )


def plan_for_find_and_inspect(query: str, views: int = 4, standoff_m: float = 12.0) -> MissionPlan:
    """Host template: find ``query``, orbit it for ``views`` views, return home."""
    return _plan(
        f"find and inspect {query}",
        [
            {
                "id": "search",
                "skill": "area_search",
                "params": {"region": "zone:all", "query": query},
                "on_success": "inspect",
                "on_fail": "home",
            },
            {
                "id": "inspect",
                "skill": "inspect",
                "params": {
                    "target_ref": "finding:1",
                    "query": query,
                    "views": views,
                    "standoff_m": standoff_m,
                },
                "on_success": "home",
                "on_fail": "home",
            },
            {
                "id": "home",
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            },
        ],
    )


__all__ = [
    "ExecutorConfig",
    "MissionExecutor",
    "PERCEPTION_SKILLS",
    "plan_for_find_and_inspect",
    "plan_for_find_task",
    "task_text_for",
]

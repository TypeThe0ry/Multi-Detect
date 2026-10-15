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
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from multidetect.autonomy.mission_plan import (
    MissionPlan,
    MissionReport,
    MissionStep,
    validate_mission_plan,
)
from multidetect.autonomy.onboard_loop import Detection, OnboardLoop, PlaceVerdictMemory, Track
from multidetect.sim_teacher.primitives import ActionPrimitive

GroundPoint = Callable[[float, float], tuple[float, float] | None]
Pose = tuple[float, float, float]
_TRACKING_SKILLS = frozenset({"patrol", "monitor", "follow"})
PERCEPTION_SKILLS = frozenset({"area_search", "count", "inspect"}) | _TRACKING_SKILLS


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def task_text_for(step: MissionStep) -> str:
    """The wording the slow model was trained on, with the step's query."""
    return f"Find the {step.params['query']} and record its position."


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    step_budget: int = 60
    # Flying skills are bounded separately from searches.
    travel_budget: int = 150
    max_translate_m: float = 15.0
    arrive_tolerance_m: float = 3.0
    # Flown yaw lands within ~15 deg of the command; the camera sees +-50 deg.
    turn_tolerance_deg: float = 20.0
    min_altitude_m: float = 8.0
    max_altitude_m: float = 120.0
    # Inspection views look at the centre this far below the camera axis.
    inspect_depression_margin_rad: float = 0.35
    # Shallower survey rays are too distant for reliable detection.
    survey_min_depression_rad: float = 0.35
    max_blocked_legs: int = 2
    hold_s: float = 1.0
    follow_stale_s: float = 3.0
    follow_stale_cycles: int = 2
    # Patrol/monitor alerts require this many identity-positive votes.  The
    # default preserves the historical first-positive behaviour.
    alert_min_identity_matches: int = 1
    # When enabled, identity-positive votes must outnumber reject votes.
    alert_require_majority: bool = False
    # Identity votes from fragmented tracks are pooled when their ground
    # positions are within this radius.  This is also the historical alert
    # de-duplication radius.
    alert_cluster_radius_m: float = 4.0
    # A commanded landing remains pending while the host reports that the
    # vehicle is still airborne.  The bound prevents a stale telemetry stream
    # from leaving a mission in an unbounded terminal state.
    landing_timeout_steps: int = 15
    deliver_min_altitude_m: float = 1.0
    deliver_max_altitude_m: float = 5.0
    deliver_altitude_m: float = 2.0
    deliver_position_tolerance_m: float = 1.0
    deliver_hold_s: float = 2.0
    deliver_ack_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        values = (
            self.deliver_min_altitude_m, self.deliver_max_altitude_m,
            self.deliver_altitude_m, self.deliver_position_tolerance_m,
            self.deliver_hold_s, self.deliver_ack_timeout_s,
        )
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("deliver settings must be finite positive numbers")
        if not (
            self.deliver_min_altitude_m <= self.deliver_altitude_m <= self.deliver_max_altitude_m
        ):
            raise ValueError("deliver altitude must be within configured bounds")


@dataclass(slots=True)
class _AlertCluster:
    """Votes belonging to one physical location during a tracking step."""

    centre: tuple[float, float]
    members: int = 0
    positive: int = 0
    negative: int = 0
    alerted: bool = False
    last_track_id: str | None = None

    def add(self, point: tuple[float, float], *, positive: bool, track_id: str) -> None:
        # Keep the centre as a running mean of all member ground points.  This
        # makes a fragmented track's observations stable while still allowing
        # the cluster to follow a small amount of position drift.
        n = self.members
        self.centre = (
            (self.centre[0] * n + point[0]) / (n + 1),
            (self.centre[1] * n + point[1]) / (n + 1),
        )
        self.members += 1
        if positive:
            self.positive += 1
        else:
            self.negative += 1
        self.last_track_id = track_id


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
    # Coverage, routes and timed observation.
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    waypoint_index: int = 0
    visited: int = 0
    elapsed_s: float = 0.0
    clock_origin_s: float | None = None
    last_timestamp_s: float | None = None
    pending_elapsed_s: float = 0.0
    observation_start_s: float | None = None
    coverage_points: list[tuple[float, float]] = field(default_factory=list)
    covered_points: set[int] = field(default_factory=set)
    # Patrol/monitor identity votes are pooled by ground position because the
    # tracker may give the same object several track IDs.
    alert_clusters: list[_AlertCluster] = field(default_factory=list)
    # Follow retains the selected identity and updates one position record.
    follow_track_id: str | None = None
    acquire_track: bool = False
    lost_cycles: int = 0
    follow_finding: dict[str, Any] | None = None
    # Landing is a two-phase command/observation operation.
    landing_commanded: bool = False
    # Number of control cycles for which landing has remained unconfirmed.
    landing_wait_steps: int = 0
    fresh_ground_tracks: set[str] = field(default_factory=set)
    last_ground_s: float | None = None
    follow_blocked_attempts: int = 0
    # Delivery is a host request/ack handshake, never a hardware operation.
    release_altitude_m: float | None = None
    release_request_id: str | None = None
    release_deadline: float = 0.0
    release_ack: bool | None = None


class MissionExecutor:
    def __init__(
        self,
        plan: MissionPlan | Mapping[str, Any] | str,
        *,
        loop_factory: Callable[..., OnboardLoop],
        refs: Mapping[str, Sequence[float] | Sequence[tuple[float, float]]] | None = None,
        config: ExecutorConfig | None = None,
        task_text: Callable[[MissionStep], str] = task_text_for,
        capabilities: frozenset[str] = frozenset(),
        release_authorized: bool = False,
        clock: Callable[[], float] = time.monotonic,
        no_fly_zones: Sequence[tuple[Sequence[float], float]] = (),
        mission_package: Any | None = None,
        package: Any | None = None,
    ) -> None:
        # Validate the contract here; execution gates fail the step through on_fail.
        self.plan = plan if isinstance(plan, MissionPlan) else validate_mission_plan(
            plan, capabilities=frozenset({"payload"})
        )
        self.capabilities = frozenset(capabilities)
        self.release_authorized = release_authorized is True
        self._clock = clock
        self.loop_factory = loop_factory
        self.refs = dict(refs or {})
        self.config = config or ExecutorConfig()
        self.task_text = task_text
        package = mission_package if mission_package is not None else package
        if package is not None and not no_fly_zones:
            no_fly_zones = getattr(package, "no_fly_zones", ())
        self.no_fly_zones = tuple(
            ((float(centre[0]), float(centre[1])), float(radius))
            for centre, radius in no_fly_zones
            if len(centre) >= 2 and math.isfinite(float(radius)) and float(radius) > 0
        )
        self._steps = {step.id: step for step in self.plan.steps}
        # A probe loop gives the camera model and the static-map clearance check.
        self._probe = loop_factory("Find the object and record its position.")
        self.home: tuple[float, float] | None = None
        self.cruise_altitude_m: float | None = None
        self.findings: list[dict[str, Any]] = []
        self._finding_loops: dict[int, OnboardLoop] = {}
        self.counts: dict[str, int] = {}
        self.coverage: dict[str, float] = {}
        self.evidence: list[str] = []
        self.track_ids: list[str] = []
        self.answers: list[str] = []
        self.alerts: list[str] = []
        self.history: list[dict[str, Any]] = []
        self.done = False
        self._last_loop: OnboardLoop | None = None
        self._run: _StepRun | None = self._start(self.plan.steps[0])
        self._current: list[Track] = []
        # Only findings from completed steps on the active execution path are
        # valid for finding:N references.  The executor never searches for a
        # missing finding as a substitute for a bad reference.
        self._completed_step_ids: list[str] = []
        self._successful_step_ids: list[str] = []
        self._landed_observation: bool | None = None

    # -- step lifecycle ---------------------------------------------------
    def region_of(self, step: MissionStep) -> tuple[float, float, float] | None:
        """A zone with a radius bounds the step's search; zone:all (or none) does not."""
        ref = step.params.get("region")
        if ref is None or str(ref) == "zone:all":
            return None
        if not str(ref).startswith("zone:"):
            return None
        value = self.refs.get(str(ref)) if ref else None
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 3:
            try:
                if any(isinstance(value[index], bool) for index in range(3)):
                    return None
                region = (float(value[0]), float(value[1]), float(value[2]))
            except (TypeError, ValueError):
                return None
            if all(math.isfinite(v) for v in region) and region[2] > 0:
                return region
        return None

    def _region_error(self, step: MissionStep) -> str | None:
        """Return a bounded-region contract error, if ``step`` has one.

        ``zone:all`` is the explicit unbounded-region spelling.  Every other
        region must name a host zone carrying a finite positive radius; a POI,
        route, finding, or malformed zone is never silently treated as a
        global search.
        """
        if "region" not in step.params:
            return None
        ref = str(step.params["region"])
        if ref == "zone:all":
            return None
        if not ref.startswith("zone:"):
            return "invalid_region_ref"
        value = self.refs.get(ref)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 3:
            return "invalid_region_ref"
        try:
            if any(isinstance(value[index], bool) for index in range(3)):
                return "invalid_region_ref"
            region = tuple(float(value[index]) for index in range(3))
        except (TypeError, ValueError, IndexError):
            return "invalid_region_ref"
        if not all(math.isfinite(v) for v in region) or region[2] <= 0:
            return "invalid_region_ref"
        return None

    def _loop(self, step: MissionStep, **overrides: Any) -> OnboardLoop:
        region = self.region_of(step)
        if region is not None:
            overrides["region"] = region
        return self.loop_factory(self.task_text(step), **overrides)

    def _start(self, step: MissionStep) -> _StepRun:
        run = _StepRun(step=step)
        # The step budget bounds a search; the loop's own step cap must not end it.
        if step.skill == "area_search":
            run.loop = self._loop(step, max_steps=10**6)
        elif step.skill == "count":
            run.loop = self._loop(step, stop_on_confirm=False, max_steps=10**6)
        elif step.skill == "inspect":
            run.loop = self.loop_factory(self.task_text(step))
        elif step.skill in {"patrol", "monitor"}:
            run.loop = self._loop(step, stop_on_confirm=False, max_steps=10**6)
        elif step.skill == "follow":
            ref = str(step.params["track_ref"])
            finding = None
            if ref.startswith("track:"):
                try:
                    index = int(ref.split(":", 1)[1])
                except ValueError:
                    index = 0
                if 1 <= index <= len(self.findings):
                    finding = self.findings[index - 1]
            source_loop = self._finding_loops.get(id(finding)) if finding is not None else None
            if source_loop is self._last_loop and source_loop is not None:
                run.loop = source_loop
                # Following inherits association state, but starts a new task's
                # identity memory just like steps that allocate a new loop.
                run.loop._place_memory = PlaceVerdictMemory()
                run.follow_track_id = finding.get("track_id")
            else:
                run.acquire_track = True
                run.loop = self._loop(step, stop_on_confirm=False, max_steps=10**6)
            run.loop.config = replace(
                run.loop.config, lost_after_steps=max(7, run.loop.config.lost_after_steps)
            )
        return run

    def _finish(self, state: str, reason: str | None = None) -> None:
        run = self._run
        assert run is not None
        run.state, run.reason = state, reason
        self._last_loop = run.loop
        status = {"step": run.step.id, "skill": run.step.skill, "state": state, "reason": reason}
        if run.step.skill == "count":
            status["count_is_lower_bound"] = reason in {"budget_exhausted", "max_count"}
        self.history.append(status)
        if run.step.id not in self._completed_step_ids:
            self._completed_step_ids.append(run.step.id)
        if state == "succeeded" and run.step.id not in self._successful_step_ids:
            self._successful_step_ids.append(run.step.id)
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
                finding = self.findings[index - 1]
                source_step = finding.get("step")
                if source_step not in self._steps or source_step not in self._successful_step_ids:
                    return None
                try:
                    point = tuple(finding["position_ne"])
                    if len(point) != 2:
                        return None
                    return (float(point[0]), float(point[1]))
                except (KeyError, TypeError, ValueError):
                    return None
            return None
        value = self.refs.get(ref)
        if value is None or len(value) < 2:
            return None
        try:
            point = (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
        return point if all(math.isfinite(v) for v in point) else None

    def route_of(self, ref: str) -> list[tuple[float, float]] | None:
        """Resolve a host route without treating it as a point reference."""
        value = self.refs.get(ref)
        if not value:
            return None
        route = []
        for item in value:
            if not isinstance(item, Sequence) or isinstance(item, str) or len(item) != 2:
                return None
            try:
                point = (float(item[0]), float(item[1]))
            except (TypeError, ValueError):
                return None
            if not all(math.isfinite(v) for v in point):
                return None
            route.append(point)
        return route

    # -- perception hooks ---------------------------------------------------
    def observe(
        self,
        detections: Sequence[Detection],
        *,
        pose_ned: Pose,
        yaw_deg: float,
        ground_point: GroundPoint | None = None,
        landed: bool | None = None,
    ) -> list[Track]:
        """Update the active step's tracks; return the tracks that need a slow verdict."""
        self._init_home(pose_ned)
        self._landed_observation = landed if type(landed) is bool else None
        run = self._run
        if run is None or run.loop is None:
            self._current = []
            return []
        if run.step.skill in _TRACKING_SKILLS:
            # These skills own flight; advance association/verdict age without loop control.
            run.loop.step_index += 1
        # A visible box can retain its previous ground estimate when its latest
        # ray misses the ground. Following must distinguish that from a fresh fix.
        projected = {}
        project = ground_point
        if run.step.skill == "follow" and ground_point is not None:
            def project(u, v):
                point = ground_point(u, v)
                projected[u, v] = point
                return point
        self._current = run.loop.update_tracks(
            detections, pose_ned=pose_ned, yaw_deg=yaw_deg, ground_point=project
        )
        if run.step.skill == "follow":
            run.fresh_ground_tracks = {
                track.track_id for track in self._current
                if projected.get(((track.bbox[0] + track.bbox[2]) / 2.0, track.bbox[3]))
                is not None
            }
        if run.step.skill == "inspect":
            if run.phase != "capture":
                return []
            nearest = self._nearest_to_centre(run, self._current)
            run.pending_track = nearest.track_id if nearest else None
            return [nearest] if nearest else []
        if run.step.skill not in _TRACKING_SKILLS and run.loop.state not in {
            "searching",
            "approaching",
        }:
            return []
        return run.loop.tracks_needing_verdict(self._current)

    def apply_verdict(
        self,
        track_id: str,
        decision: str | None,
        confidence: float | None = None,
        *,
        remembered: bool = False,
    ) -> None:
        run = self._run
        if run is None or run.loop is None:
            return
        run.loop.apply_verdict(track_id, decision, confidence, remembered=remembered)
        if run.step.skill == "inspect" and track_id == run.pending_track:
            self.answers.append(f"{run.step.id}:view{run.view_index + 1}:{decision}")
        if run.step.skill in {"patrol", "monitor"}:
            self._alert_for_track(run, track_id)

    def _alert_for_track(self, run: _StepRun, track_id: str) -> None:
        track = run.loop.tracks.get(track_id)
        if (
            track is None
            or track.ground is None
            or track.verdict not in {"confirm", "approach", "no_match"}
        ):
            return

        # Find the nearest physical-location vote cluster.  Track IDs are not
        # used as the evidence key: onboard association can fragment one
        # object into several IDs from successive views.
        cluster = min(
            run.alert_clusters,
            key=lambda item: math.dist(track.ground, item.centre),
            default=None,
        )
        if (
            cluster is None
            or math.dist(track.ground, cluster.centre) > self.config.alert_cluster_radius_m
        ):
            cluster = _AlertCluster(track.ground)
            run.alert_clusters.append(cluster)
        cluster.add(
            track.ground,
            positive=track.verdict in {"confirm", "approach"},
            track_id=track_id,
        )
        if cluster.alerted:
            return

        if (
            cluster.positive < self.config.alert_min_identity_matches
            or (
                self.config.alert_require_majority
                and cluster.positive <= cluster.negative
            )
        ):
            return
        cluster.alerted = True
        north, east = cluster.centre
        self.alerts.append(f"{run.step.id}:alert:{run.step.params['query']}@{north:.1f},{east:.1f}")
        self._add_finding(
            run,
            {
                "track_id": track_id,
                "position_estimate_ne": cluster.centre,
                "bbox": track.bbox,
            },
            None,
        )

    @property
    def alert_vote_clusters(self) -> tuple[dict[str, Any], ...]:
        """Current patrol/monitor location clusters for onboard labels."""
        run = self._run
        if run is None:
            return ()
        return tuple(
            {
                "centre": [round(v, 3) for v in cluster.centre],
                "members": cluster.members,
                "positive": cluster.positive,
                "negative": cluster.negative,
                "alerted": cluster.alerted,
                "track_id": cluster.last_track_id,
            }
            for cluster in run.alert_clusters
        )

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
        landed: bool | None = None,
        timestamp_s: float | None = None,
    ) -> tuple[ActionPrimitive, dict[str, Any]]:
        self._init_home(pose_ned)
        # ``next_action`` is also a valid observation entry point for hosts
        # that do not run a perception update every cycle.
        if type(landed) is bool:
            self._landed_observation = landed
        for _ in range(len(self.plan.steps) + 1):
            run = self._run
            if run is None:
                return ActionPrimitive.stop(), {"mission": "done"}
            self._advance_time(run, timestamp_s)
            if run.primitives >= self._budget(run.step):
                self._budget_end(run)
                continue
            result = self._act(run, pose_ned, yaw_deg, ground_point, frame_id)
            if result is None:
                continue  # the step finished without moving; start the next one now
            action, info = result
            if action.kind == "translate" and self._translate_enters_no_fly(
                pose_ned, yaw_deg, action
            ):
                self.alerts.append(f"{run.step.id}:no_fly_zone")
                self._finish("failed", "no_fly_zone")
                continue
            run.primitives += 1
            # Used only when the next observation has no pose/simulation clock.
            # Never credit a commanded hold before its following observation.
            run.pending_elapsed_s = self._estimated_seconds(action)
            info.update(step=run.step.id, skill=run.step.skill, step_state=run.state)
            return action, info
        return ActionPrimitive.hold(self.config.hold_s), {"mission": "stalled"}

    @staticmethod
    def _segment_circle_distance(
        start: tuple[float, float],
        end: tuple[float, float],
        centre: tuple[float, float],
    ) -> float:
        dn, de = end[0] - start[0], end[1] - start[1]
        length2 = dn * dn + de * de
        if length2 <= 1e-12:
            return math.dist(start, centre)
        t = max(
            0.0,
            min(1.0, ((centre[0] - start[0]) * dn + (centre[1] - start[1]) * de) / length2),
        )
        return math.dist(centre, (start[0] + t * dn, start[1] + t * de))

    def _translate_enters_no_fly(
        self, pose: Pose, yaw_deg: float, action: ActionPrimitive
    ) -> bool:
        """Check the exact horizontal leg represented by a translate primitive."""
        heading = math.radians(yaw_deg)
        end = (
            pose[0] + action.forward_m * math.cos(heading) - action.right_m * math.sin(heading),
            pose[1] + action.forward_m * math.sin(heading) + action.right_m * math.cos(heading),
        )
        start = (pose[0], pose[1])
        return any(
            self._segment_circle_distance(start, end, centre) <= radius
            for centre, radius in self.no_fly_zones
        )

    @staticmethod
    def _estimated_seconds(action: ActionPrimitive) -> float:
        if action.kind == "translate":
            return math.sqrt(action.forward_m**2 + action.right_m**2 + action.up_m**2) / 5.0
        if action.kind == "turn":
            return abs(action.yaw_delta_deg) / 90.0
        return action.seconds if action.kind == "hold" else 0.0

    def _advance_time(self, run: _StepRun, timestamp_s: float | None) -> None:
        """Prefer the observation's pose clock; wall-clock inference time is unrelated.

        Missing clocks use primitive estimates. A returning clock reconciles
        those estimates, while duplicate/backward stamps cannot advance time.
        """
        valid = (
            isinstance(timestamp_s, (int, float))
            and not isinstance(timestamp_s, bool)
            and math.isfinite(timestamp_s)
            and timestamp_s >= 0
        )
        if valid:
            if run.clock_origin_s is None:
                # Before the first clock there is no measured interval. Retain
                # the preceding action's fallback estimate when anchoring it.
                run.elapsed_s += run.pending_elapsed_s
                run.clock_origin_s = timestamp_s - run.elapsed_s
            if run.last_timestamp_s is None or timestamp_s >= run.last_timestamp_s:
                run.elapsed_s = max(0.0, timestamp_s - run.clock_origin_s)
                run.last_timestamp_s = timestamp_s
        else:
            run.elapsed_s += run.pending_elapsed_s
        run.pending_elapsed_s = 0.0
        if run.observation_start_s is not None:
            run.held_s = max(0.0, run.elapsed_s - run.observation_start_s)

    @staticmethod
    def _begin_observation(run: _StepRun) -> None:
        if run.observation_start_s is None:
            run.observation_start_s = run.elapsed_s

    def _budget(self, step: MissionStep) -> int:
        if step.skill == "land":
            return max(1, self.config.landing_timeout_steps + 1)
        if step.skill == "hover":
            return max(self.config.step_budget, int(float(step.params["duration_s"])) + 2)
        if step.skill in {"goto", "inspect", "return_home", "survey", "deliver"} | _TRACKING_SKILLS:
            return self.config.travel_budget
        return self.config.step_budget

    def _budget_end(self, run: _StepRun) -> None:
        if run.step.skill == "count":
            self._report_count(run)
            self._finish("succeeded", "budget_exhausted")
        elif run.step.skill == "follow" and run.follow_finding is not None and run.lost_cycles <= 6:
            if run.step.params.get("duration_s") is None or self._duration_reached(run):
                self._finish("succeeded", "duration_elapsed")
            else:
                self._finish("failed", "step_budget")
        else:
            self._finish("failed", "step_budget")

    def _act(self, run, pose, yaw, ground_point, frame_id):
        skill = run.step.skill
        region_error = self._region_error(run.step)
        if region_error is not None:
            self._finish("failed", region_error)
            return None
        finding_error = self._finding_ref_error(run.step)
        if finding_error is not None:
            self._finish("failed", finding_error)
            return None
        handler = {
            "area_search": self._area_search,
            "count": self._count,
            "inspect": self._inspect,
            "survey": self._survey,
            "patrol": self._patrol,
            "monitor": self._monitor,
            "follow": self._follow,
            "goto": self._goto,
            "hover": self._hover,
            "return_home": self._return_home,
            "land": self._land,
            "record": self._record,
            "deliver": self._deliver,
        }.get(skill)
        if handler is None:
            self._finish("failed", "skill_not_implemented")
            return None
        return handler(run, pose, yaw, ground_point, frame_id)

    def _finding_ref_error(self, step: MissionStep) -> str | None:
        """Reject missing or forward finding references before a skill can search."""
        for value in step.params.values():
            if not isinstance(value, str) or not value.startswith("finding:"):
                continue
            try:
                index = int(value.split(":", 1)[1])
            except ValueError:
                return "invalid_finding_ref"
            if index < 1 or index > len(self.findings):
                return "finding_not_found"
            source_step = self.findings[index - 1].get("step")
            if source_step not in self._steps or source_step not in self._successful_step_ids:
                return "finding_not_found"
        return None

    # -- perception skills ---------------------------------------------------
    def _area_search(self, run, pose, yaw, ground_point, frame_id):
        action, info = run.loop.next_action(
            self._current, pose_ned=pose, yaw_deg=yaw, ground_point=ground_point
        )
        if run.loop.confirmed is not None:
            self._add_finding(run, run.loop.confirmed, frame_id)
            self._finish("succeeded", "confirmed")
            return ActionPrimitive.hold(self.config.hold_s), info
        if run.loop.state not in {"searching", "approaching"}:
            # The loop's own budget ended its search; flying home is the plan's job.
            self._finish("failed", "search_exhausted")
            return None
        return action, info

    def _count(self, run, pose, yaw, ground_point, frame_id):
        action, info = run.loop.next_action(
            self._current, pose_ned=pose, yaw_deg=yaw, ground_point=ground_point
        )
        if "counted" in info:
            self._add_finding(run, info["counted"], frame_id)
        # The count so far is reported live, so an interrupted mission still says it.
        self._report_count(run)
        if run.loop.state not in {"searching", "approaching"}:
            self._report_count(run)
            self._finish("succeeded", "search_exhausted")
            return None
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
        finding = {
            "step": run.step.id,
            "query": run.step.params.get("query"),
            "position_ne": (float(position[0]), float(position[1])),
            "track_id": record.get("track_id"),
            "bbox": record.get("bbox"),
            "rule": record.get("rule"),
            "frame_id": frame_id,
        }
        self.findings.append(finding)
        if run.loop is not None:
            self._finding_loops[id(finding)] = run.loop
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
            # A deliberate view is evidence only when the nearest candidate
            # to the requested centre was actually visible in that frame.
            visible = any(
                track.track_id == run.pending_track
                and track.verdict in {"confirm", "approach"}
                for track in self._current
            )
            if visible and frame_id:
                self.evidence.append(frame_id)
            if visible:
                run.captured += 1
            else:
                run.skipped += 1
            run.view_index += 1
            run.pending_track = None
            run.phase = "fly"
        while run.view_index < len(run.viewpoints):
            moved = self._move_to(
                run, pose, yaw, run.viewpoints[run.view_index], run.goal_altitude_m
            )
            if moved == "no_fly":
                return None
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
        minimum = int(run.step.params.get("min_views_seen", 1))
        if run.captured >= minimum:
            self._finish("succeeded", "views_captured")
        else:
            self._finish("failed", "target_not_seen")
        return None

    # -- coverage and observation along a route --------------------------------
    def _survey(self, run, pose, yaw, ground_point, frame_id):
        if not run.waypoints:
            region = self.region_of(run.step)
            if region is None:
                self._finish("failed", "unknown_ref")
                return None
            north, east, radius = region
            run.goal_altitude_m = float(run.step.params.get("altitude_m", max(0.0, -pose[2])))
            camera = self._probe.camera
            hfov = 2.0 * math.atan2(camera.width, 2.0 * camera.fx)
            vfov = 2.0 * math.atan2(camera.height, 2.0 * camera.fy)
            far_depression = max(
                self.config.survey_min_depression_rad, camera.pitch_down_rad - vfov / 2.0
            )
            near_depression = min(math.pi / 2.0, camera.pitch_down_rad + vfov / 2.0)
            near_range = run.goal_altitude_m / max(1e-6, math.tan(near_depression))
            far_range = run.goal_altitude_m / max(1e-6, math.tan(far_depression))
            # Use the midpoint on the ground, not the shallow image-centre ray.
            mid_depression = math.atan2(run.goal_altitude_m, (near_range + far_range) / 2.0)
            slant_mid = run.goal_altitude_m / max(1e-6, math.sin(mid_depression))
            swath = 2.0 * slant_mid * math.tan(hfov / 2.0)
            spacing = max(3.0, min(60.0, swath * (1.0 - float(run.step.params["overlap"]))))
            # Inset lanes in equal strips, no farther apart than the requested spacing.
            # Keep three crossings even when one swath spans the whole bounding square.
            lanes = max(3, math.ceil(2.0 * radius / spacing))
            for lane in range(lanes):
                offset = -radius + (lane + 0.5) * 2.0 * radius / lanes
                half_length = math.sqrt(max(0.0, radius**2 - offset**2))
                ends = [(north - half_length, east + offset), (north + half_length, east + offset)]
                run.waypoints.extend(ends if lane % 2 == 0 else reversed(ends))
            self.coverage[run.step.id] = 0.0
        while run.waypoint_index < len(run.waypoints):
            moved = self._move_to(
                run, pose, yaw, run.waypoints[run.waypoint_index], run.goal_altitude_m
            )
            if moved == "no_fly":
                return None
            if moved == "blocked":
                self.alerts.append(f"{run.step.id}:waypoint{run.waypoint_index + 1}_blocked")
                run.blocked = 0
            elif moved is not None:
                return moved, {"waypoint": run.waypoint_index + 1}
            else:
                run.visited += 1
                self.coverage[run.step.id] = run.visited / len(run.waypoints)
                if frame_id:
                    self.evidence.append(frame_id)
            run.waypoint_index += 1
        if run.visited:
            self._finish("succeeded", "region_covered")
        else:
            self._finish("failed", "no_waypoint_reachable")
        return None

    def _duration_reached(self, run: _StepRun) -> bool:
        duration = run.step.params.get("duration_s")
        return duration is not None and run.elapsed_s >= float(duration) - 1e-6

    def _patrol(self, run, pose, yaw, ground_point, frame_id):
        if not run.waypoints:
            route = self.route_of(str(run.step.params["route_ref"]))
            if route is None:
                self._finish("failed", "unknown_ref")
                return None
            run.waypoints = route
            if "altitude_m" in run.step.params:
                run.goal_altitude_m = float(run.step.params["altitude_m"])
        if self._duration_reached(run):
            self._finish("succeeded", "duration_elapsed")
            return None
        total = len(run.waypoints) * int(run.step.params["loops"])
        while run.waypoint_index < total:
            index = run.waypoint_index % len(run.waypoints)
            moved = self._move_to(run, pose, yaw, run.waypoints[index], run.goal_altitude_m)
            if moved == "no_fly":
                return None
            if moved == "blocked":
                self._finish("failed", "blocked")
                return None
            if moved is not None:
                return moved, {
                    "waypoint": index + 1,
                    "loop": run.waypoint_index // len(run.waypoints) + 1,
                }
            run.visited += 1
            run.waypoint_index += 1
        self._finish("succeeded", "duration_elapsed")
        return None

    def _monitor_centre_altitude(self, radius: float) -> float | None:
        """Height whose pinhole footprint contains the entire ground disk.

        Each image edge is a half-plane on the ground. Its closest point on
        the disk must remain inside; a camera with a nadir blind spot cannot
        cover the disk from its centre at any height.
        """
        camera = self._probe.camera
        sine, cosine = math.sin(camera.pitch_down_rad), math.cos(camera.pitch_down_rad)
        edges = (
            (sine, abs(cosine)),
            (camera.cx * sine, math.hypot(camera.cx * cosine, camera.fx)),
            ((camera.width - camera.cx) * sine,
             math.hypot((camera.width - camera.cx) * cosine, camera.fx)),
            (camera.fy * cosine + camera.cy * sine,
             abs(camera.cy * cosine - camera.fy * sine)),
            ((camera.height - camera.cy) * sine - camera.fy * cosine,
             abs((camera.height - camera.cy) * cosine + camera.fy * sine)),
        )
        if any(coefficient <= 1e-6 for coefficient, _ in edges):
            return None
        return max(radius * extent / coefficient for coefficient, extent in edges)

    def _plan_monitor(self, run: _StepRun, pose: Pose) -> None:
        region = self.region_of(run.step)
        altitude = float(run.step.params.get("altitude_m", max(0.0, -pose[2])))
        if region is not None:
            north, east, radius = region
            needed = self._monitor_centre_altitude(radius)
            if needed is not None and needed <= self.config.max_altitude_m and (
                "altitude_m" not in run.step.params or altitude >= needed
            ):
                altitude = max(altitude, needed)
                run.waypoints = [(north, east)]
            else:
                # A shallow camera leaves a blind disk at the centre. Observe
                # from eight positions inside the zone instead, at a lower
                # height when no explicit altitude was requested.
                if "altitude_m" not in run.step.params:
                    altitude = min(altitude, max(self.config.min_altitude_m, radius / 2.0))
                start = math.atan2(pose[1] - east, pose[0] - north)
                run.waypoints = [
                    (north + 0.75 * radius * math.cos(start + index * math.pi / 4.0),
                     east + 0.75 * radius * math.sin(start + index * math.pi / 4.0))
                    for index in range(8)
                ]
            # Equal-area disk samples estimate the union of actual camera
            # footprints; reaching all waypoints alone never implies coverage.
            run.coverage_points = [
                (north + radius * math.sqrt((ring + 0.5) / 12.0) * math.cos(angle * math.tau / 48),
                 east + radius * math.sqrt((ring + 0.5) / 12.0) * math.sin(angle * math.tau / 48))
                for ring in range(12) for angle in range(48)
            ]
            self.coverage[run.step.id] = 0.0
        else:
            run.waypoints = [run.goal_ne]
        run.goal_altitude_m = max(
            self.config.min_altitude_m, min(self.config.max_altitude_m, altitude)
        )

    def _monitor_coverage(self, run: _StepRun, pose: Pose, yaw: float, frame_id) -> None:
        camera = self._probe.camera
        heading = math.radians(yaw)
        sine, cosine = math.sin(camera.pitch_down_rad), math.cos(camera.pitch_down_rad)
        altitude = max(0.0, -pose[2])
        for index, (north, east) in enumerate(run.coverage_points):
            dn, de = north - pose[0], east - pose[1]
            forward = dn * math.cos(heading) + de * math.sin(heading)
            right = -dn * math.sin(heading) + de * math.cos(heading)
            depth = forward * cosine + altitude * sine
            if depth <= 1e-6:
                continue
            u = camera.cx + camera.fx * right / depth
            v = camera.cy + camera.fy * (-forward * sine + altitude * cosine) / depth
            if 0 <= u <= camera.width and 0 <= v <= camera.height:
                run.covered_points.add(index)
        if run.coverage_points:
            self.coverage[run.step.id] = len(run.covered_points) / len(run.coverage_points)
        if frame_id:
            self.evidence.append(frame_id)

    def _monitor(self, run, pose, yaw, ground_point, frame_id):
        if run.goal_ne is None:
            region = self.region_of(run.step)
            run.goal_ne = region[:2] if region else self.resolve(str(run.step.params["region"]))
            if run.goal_ne is None:
                self._finish("failed", "unknown_ref")
                return None
            self._plan_monitor(run, pose)
        if run.observation_start_s is not None:
            self._monitor_coverage(run, pose, yaw, frame_id)
            if run.held_s >= float(run.step.params["duration_s"]) - 1e-6:
                self._finish("succeeded", "duration_elapsed")
                return None
        if run.phase == "fly":
            goal = run.waypoints[run.waypoint_index % len(run.waypoints)]
            moved = self._move_to(run, pose, yaw, goal, run.goal_altitude_m)
            if moved == "no_fly":
                return None
            if moved == "blocked":
                self._finish("failed", "blocked")
                return None
            if moved is not None:
                return moved, {"phase": "fly"}
            run.phase = "hold"
            self._begin_observation(run)
            self._monitor_coverage(run, pose, yaw, frame_id)
        # Transit to the first viewpoint is excluded. Turns and travel between
        # subsequent viewpoints are part of the active observation interval.
        remaining = float(run.step.params["duration_s"]) - run.held_s
        if remaining <= 1e-6:
            self._finish("succeeded", "duration_elapsed")
            return None
        if run.phase == "turn":
            if run.captured >= 4 and len(run.waypoints) > 1:
                run.waypoint_index += 1
                run.captured = 0
                run.phase = "fly"
                return self._monitor(run, pose, yaw, ground_point, frame_id)
            run.phase = "hold"
            return ActionPrimitive.turn(90.0), {"phase": "turn"}
        seconds = min(2.0, remaining)
        run.captured += 1
        run.phase = "turn"
        return ActionPrimitive.hold(seconds), {"phase": "hold", "remaining_s": remaining - seconds}

    def _follow(self, run, pose, yaw, ground_point, frame_id):
        if run.follow_track_id is None and run.acquire_track:
            candidate = next(
                (t for t in self._current if t.verdict in {"confirm", "approach"}
                 and t.track_id in run.fresh_ground_tracks),
                None,
            )
            if candidate is not None:
                run.follow_track_id = candidate.track_id
        track = next((t for t in self._current if t.track_id == run.follow_track_id), None)
        fresh = track is not None and track.track_id in run.fresh_ground_tracks
        if fresh:
            run.lost_cycles = 0
            run.last_ground_s = run.elapsed_s
            if run.follow_finding is None:
                self._add_finding(
                    run,
                    {
                        "track_id": track.track_id,
                        "position_estimate_ne": track.ground,
                        "bbox": track.bbox,
                    },
                    frame_id,
                )
                run.follow_finding = self.findings[-1]
            else:
                run.follow_finding["position_ne"] = track.ground
        if self._duration_reached(run):
            self._finish("succeeded", "duration_elapsed")
            return None
        if not fresh:
            run.lost_cycles += 1
            if run.lost_cycles > 6:
                if run.follow_finding is not None:
                    self._finish("succeeded", "target_lost_timeout")
                else:
                    self._finish("failed", "target_not_found")
                return None
            age = None if run.last_ground_s is None else max(0.0, run.elapsed_s - run.last_ground_s)
            stale = run.lost_cycles >= self.config.follow_stale_cycles or (
                age is not None and age >= self.config.follow_stale_s
            )
            confidence = 0.0 if age is None else 2.0 ** (
                -max(age / max(self.config.follow_stale_s, 1e-6),
                     run.lost_cycles / max(self.config.follow_stale_cycles, 1))
            )
            info = {
                "lost_cycles": run.lost_cycles,
                "ground_age_s": age,
                "follow_confidence": confidence,
                "follow_state": "searching" if stale else "holding",
            }
            # Reacquire locally instead of translating toward a retained fix.
            action = (
                ActionPrimitive.turn(30.0 if run.lost_cycles % 2 == 0 else -30.0)
                if stale else ActionPrimitive.hold(self.config.hold_s)
            )
            return action, info
        altitude = run.step.params.get("altitude_m")
        moved = self._move_to(
            run, pose, yaw, pose[:2], None if altitude is None else float(altitude)
        )
        if moved == "no_fly":
            return None
        if moved == "blocked":
            self._finish("failed", "blocked")
            return None
        if moved is not None:
            return moved, {"track_id": track.track_id}
        dn, de = track.ground[0] - pose[0], track.ground[1] - pose[1]
        distance = math.hypot(dn, de)
        standoff = max(15.0, run.loop._keep_range(max(0.0, -pose[2])))
        info = {
            "track_id": track.track_id, "range_m": distance, "standoff_m": standoff,
            "follow_confidence": 1.0, "ground_age_s": 0.0, "follow_state": "tracking",
        }
        delta = _wrap(math.degrees(math.atan2(de, dn)) - yaw)
        if abs(delta) > self.config.turn_tolerance_deg:
            return ActionPrimitive.turn(delta), info
        if distance > standoff + 3.0:
            goal = (
                track.ground[0] - standoff * dn / distance,
                track.ground[1] - standoff * de / distance,
            )
            blocked_before = run.blocked
            moved = self._move_to(run, pose, yaw, goal)
            if moved == "no_fly":
                return None
            if run.blocked > blocked_before:
                run.follow_blocked_attempts += 1
            # Keep a separate obstruction budget: intermittent successful
            # positioning must not reset repeated blocked standoff attempts.
            if moved == "blocked" or run.follow_blocked_attempts > self.config.max_blocked_legs:
                self._finish("failed", "blocked")
                return None
            if moved is not None:
                return moved, info
        return ActionPrimitive.hold(self.config.hold_s), info

    # -- geometry skills -------------------------------------------------------
    def _move_to(self, run, pose, yaw, goal_ne, altitude_m=None, *, tolerance_m=None):
        """Move one primitive toward ``goal_ne`` and enforce package geofences.

        The return values are ``None`` on arrival, ``blocked`` for an obstacle
        refusal, and ``no_fly`` when the direct NED leg intersects a package
        no-fly circle.
        """
        if self._leg_enters_no_fly((pose[0], pose[1]), goal_ne):
            self.alerts.append(f"{run.step.id}:no_fly_zone")
            self._finish("failed", "no_fly_zone")
            return "no_fly"
        altitude = max(0.0, -pose[2])
        altitude_tolerance = 1.0 if tolerance_m is None else min(1.0, tolerance_m / 2.0)
        if altitude_m is not None and abs(altitude - altitude_m) > altitude_tolerance:
            target = (pose[0], pose[1], -altitude_m)
            # A pure climb leaves the obstacle footprint vertically; obstacle
            # clearance at the current position must not hold a legal escape.
            if altitude_m < altitude and not self._probe.segment_clear(
                target, pose[0], pose[1]
            ):
                run.blocked += 1
                if run.blocked > self.config.max_blocked_legs:
                    return "blocked"
                return ActionPrimitive.hold(self.config.hold_s)
            run.blocked = 0
            return ActionPrimitive.translate(0.0, 0.0, altitude_m - altitude)
        dn, de = goal_ne[0] - pose[0], goal_ne[1] - pose[1]
        distance = math.hypot(dn, de)
        if distance <= (self.config.arrive_tolerance_m if tolerance_m is None else tolerance_m):
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

    def _leg_enters_no_fly(
        self, start: tuple[float, float], end: Sequence[float]
    ) -> bool:
        """Return whether a complete straight NED leg intersects a no-fly circle."""
        finish = (float(end[0]), float(end[1]))
        return any(
            self._segment_circle_distance(start, finish, centre) <= radius
            for centre, radius in self.no_fly_zones
        )

    def acknowledge_payload_release(self, request_id: str, *, released: bool = True) -> bool:
        """Accept one host receipt for the active request, before its deadline."""
        run = self._run
        if (
            run is None or run.step.skill != "deliver" or run.phase != "await_ack"
            or run.release_request_id != request_id or run.release_ack is not None
            or self._clock() >= run.release_deadline or type(released) is not bool
        ):
            return False
        run.release_ack = released
        return True

    def _deliver(self, run, pose, yaw, ground_point, frame_id):
        if "payload" not in self.capabilities:
            self._finish("failed", "payload_capability_required")
            return None
        if not self.release_authorized:
            self._finish("failed", "payload_release_not_authorized")
            return None
        if not all(math.isfinite(v) for v in (*pose, yaw)):
            self._finish("failed", "invalid_pose")
            return None
        cfg = self.config
        tolerance = cfg.deliver_position_tolerance_m
        if run.goal_ne is None:
            run.goal_ne = self.resolve(str(run.step.params["target_ref"]))
            if run.goal_ne is None:
                self._finish("failed", "unknown_ref")
                return None
            requested = float(run.step.params.get("altitude_m", cfg.deliver_altitude_m))
            if not math.isfinite(requested):
                self._finish("failed", "invalid_release_altitude")
                return None
            run.release_altitude_m = max(
                cfg.deliver_min_altitude_m, min(cfg.deliver_max_altitude_m, requested)
            )
            run.goal_altitude_m = max(
                -pose[2], self.cruise_altitude_m or 0.0,
                cfg.min_altitude_m, run.release_altitude_m,
            )

        release_pose = (*run.goal_ne, -run.release_altitude_m)
        # Check the low endpoint before descent, release, and each wait cycle.
        # The map API checks horizontal clearance at the supplied altitude.
        if run.phase != "climb" and not self._probe.segment_clear(
            release_pose, run.goal_ne[0], run.goal_ne[1]
        ):
            self._finish("failed", "release_obstacle")
            return None

        if run.phase in {"hold", "await_ack"}:
            error = math.dist(pose, release_pose)
            if error > tolerance:
                self._finish("failed", "release_position_error")
                return None
            if run.phase == "hold":
                run.release_request_id = uuid.uuid4().hex
                run.release_deadline = self._clock() + cfg.deliver_ack_timeout_s
                run.phase = "await_ack"
                return ActionPrimitive.hold(cfg.hold_s), {
                    "phase": "await_ack",
                    "event": {
                        "kind": "payload_release",
                        "request_id": run.release_request_id,
                        "payload_ref": str(run.step.params["payload_ref"]),
                        "target_ref": str(run.step.params["target_ref"]),
                        "position_ned": list(release_pose),
                        "timeout_s": cfg.deliver_ack_timeout_s,
                    },
                }
            if run.release_ack is False:
                self._finish("failed", "payload_release_rejected")
                return None
            if run.release_ack is True:
                self.answers.append(f"{run.step.id}:payload_release_ack:{run.release_request_id}")
                run.phase = "climb"
            else:
                remaining = run.release_deadline - self._clock()
                if remaining <= 0:
                    self._finish("failed", "payload_release_ack_timeout")
                    return None
                return ActionPrimitive.hold(min(cfg.hold_s, remaining)), {
                    "phase": "await_ack", "request_id": run.release_request_id,
                }

        altitude = run.release_altitude_m if run.phase == "descend" else run.goal_altitude_m
        moved = self._move_to(
            run, pose, yaw, run.goal_ne, altitude, tolerance_m=tolerance / 2.0
        )
        if moved == "no_fly":
            return None
        if moved == "blocked" or run.blocked:
            self._finish("failed", "blocked")
            return None
        if moved is not None:
            return moved, {"phase": run.phase}
        if run.phase == "fly":
            run.phase = "descend"
            return self._deliver(run, pose, yaw, ground_point, frame_id)
        if run.phase == "descend":
            run.phase = "hold"
            return ActionPrimitive.hold(cfg.deliver_hold_s), {"phase": "hold"}
        self._finish("succeeded", "payload_released")
        return None

    def _goto(self, run, pose, yaw, ground_point, frame_id):
        if run.goal_ne is None:
            run.goal_ne = self.resolve(str(run.step.params["target_ref"]))
            if run.goal_ne is None:
                self._finish("failed", "unknown_ref")
                return None
            if "altitude_m" in run.step.params:
                run.goal_altitude_m = float(run.step.params["altitude_m"])
        moved = self._move_to(run, pose, yaw, run.goal_ne, run.goal_altitude_m)
        if moved == "no_fly":
            return None
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
        moved = self._move_to(run, pose, yaw, run.goal_ne, run.goal_altitude_m)
        if moved == "no_fly":
            return None
        if moved == "blocked":
            self._finish("failed", "blocked")
            return None
        if moved is None:
            self._finish("succeeded", "arrived")
            return None
        return moved, {}

    def _hover(self, run, pose, yaw, ground_point, frame_id):
        if run.goal_ne is None and "region" in run.step.params:
            if str(run.step.params["region"]) == "zone:all":
                # The whole-area spelling has no finite centre to fly to;
                # retain the current position and apply any requested
                # altitude change below.
                run.goal_ne = (pose[0], pose[1])
            region = self.region_of(run.step)
            if region is not None:
                run.goal_ne = region[:2]
            elif str(run.step.params["region"]) != "zone:all":
                run.goal_ne = self.resolve(str(run.step.params["region"]))
            if run.goal_ne is None:
                self._finish("failed", "unknown_ref")
                return None
        if "altitude_m" in run.step.params and run.goal_altitude_m is None:
            run.goal_altitude_m = float(run.step.params["altitude_m"])
        if run.goal_ne is not None:
            # Hover's region is a horizontal destination.  Reach its centre
            # before changing altitude, so the vertical leg is checked from
            # the settled position and cannot move over an unintended area.
            moved = self._move_to(run, pose, yaw, run.goal_ne, None)
            if moved is None and run.goal_altitude_m is not None:
                moved = self._move_to(run, pose, yaw, run.goal_ne, run.goal_altitude_m)
            if moved == "blocked":
                self._finish("failed", "blocked")
                return None
            if moved is not None:
                return moved, {"phase": "position"}
        self._begin_observation(run)
        remaining = float(run.step.params["duration_s"]) - run.held_s
        if remaining <= 1e-6:
            self._finish("succeeded", "duration_elapsed")
            return None
        seconds = min(5.0, remaining)
        return ActionPrimitive.hold(seconds), {"remaining_s": remaining - seconds}

    def _land(self, run, pose, yaw, ground_point, frame_id):
        landed = self._landed_observation
        if landed is True:
            self._finish("succeeded", "landed")
            return ActionPrimitive.stop(), {"phase": "landed"}
        if landed is None:
            # A host that cannot expose EXTENDED_SYS_STATE still gets the
            # historical bounded stop behaviour, but the record makes the
            # missing confirmation explicit.
            self._finish("succeeded", "landing_commanded_unconfirmed")
            return ActionPrimitive.stop(), {"phase": "landing_commanded"}
        if not run.landing_commanded:
            run.landing_commanded = True
            run.landing_wait_steps = 0
            return ActionPrimitive.stop(), {"phase": "landing_commanded"}
        run.landing_wait_steps += 1
        if run.landing_wait_steps >= self.config.landing_timeout_steps:
            self._finish("failed", "landing_timeout")
            return None
        return ActionPrimitive.stop(), {
            "phase": "landing_pending",
            "landing_wait_steps": run.landing_wait_steps,
        }

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
            state_answers=tuple(self.answers)
            + tuple(f"{step_id}:coverage:{value:.2f}" for step_id, value in self.coverage.items()),
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


def plan_for_count_task(query: str, region: str = "zone:all") -> MissionPlan:
    """Host template: count ``query`` in ``region`` over the search budget, then return home."""
    return _plan(
        f"count {query}",
        [
            {
                "id": "count",
                "skill": "count",
                "params": {"region": region, "query": query},
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
    "plan_for_count_task",
    "plan_for_find_and_inspect",
    "plan_for_find_task",
    "task_text_for",
]

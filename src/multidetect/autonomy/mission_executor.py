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

import numpy as np

from multidetect.autonomy.mission_package import MAX_NO_FLY_ZONES, effective_no_fly_radius
from multidetect.autonomy.mission_plan import (
    MissionPlan,
    MissionReport,
    MissionStep,
    validate_mission_plan,
)
from multidetect.autonomy.motion_intent import AirframeMotionAdapter, MotionCommand
from multidetect.autonomy.obstacle_source import ObstacleSource
from multidetect.autonomy.onboard_loop import (
    Detection,
    OnboardLoop,
    PlaceVerdictMemory,
    Track,
    appearances_consistent,
    co_visibility_proves_distinct,
    label_matches_query,
)
from multidetect.sim_teacher.primitives import ActionPrimitive

GroundPoint = Callable[[float, float], tuple[float, float] | None]
Pose = tuple[float, float, float]
_TRACKING_SKILLS = frozenset({"patrol", "monitor", "follow"})
PERCEPTION_SKILLS = frozenset({"area_search", "count", "inspect"}) | _TRACKING_SKILLS
_DETOUR_TIMEOUT = object()


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def task_text_for(step: MissionStep) -> str:
    """The wording the slow model was trained on, with the step's query."""
    return f"Find the {step.params['query']} and record its position."


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    step_budget: int = 60
    # Count/area-search budgets are derived from the bounded region and the
    # generated coverage route.  ``step_budget`` remains the legacy fallback
    # (and an explicit per-step value remains a hard ceiling).
    count_budget_max: int = 4096
    # Outer collector/session primitive limit. Adaptive expansion is enabled
    # only when this value is supplied; otherwise legacy fixed behavior stays.
    runtime_step_limit: int | None = None
    runtime_tail_reserve: int = 10
    # Flying skills are bounded separately from searches.
    travel_budget: int = 150
    # Patrol/monitor use a duration/route-derived primitive budget.  This is
    # only a fail-safe ceiling; normal completion is owned by duration/loops.
    tracking_budget_max: int = 4096
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
    # Three metres keeps the commanded path clear of circle-boundary and
    # position-estimation error; the package checker uses the same margin.
    no_fly_margin_m: float = 3.0
    detour_replan_budget_ms: float = 50.0
    detour_max_replan_failures: int = 3
    visibility_graph_build_limit_ms: float = 2000.0
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
    # A new positive this close to an earlier alert is that object again even
    # when the range-scaled appearance disagrees (a far and a near look at one
    # backpack, 0.3 m apart, raised two SITL alerts); only co-visibility can
    # still prove two objects.  0 keeps the appearance check at any distance.
    alert_same_place_m: float = 1.5
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
    # Before area_search/count end their search, each lost place whose last
    # identity verdict was positive but unresolved is revisited this many times.
    # Zero keeps the historical behaviour of ending immediately.
    unresolved_revisit_attempts: int = 1
    # Count-only looks for places rejected before a usable-size or near view.
    count_rejection_revisit_attempts: int = 3
    count_rejection_revisit_range_m: float = 15.0
    # count/area_search: places of query-relevant tracks that were only ever too
    # small to judge get a closer look before the search ends, at most this many.
    small_unjudged_revisit_attempts: int = 2
    # Far/small rejection and small unjudged looks of one step share this cap.
    completion_revisit_max: int = 4
    # Count loops: a small-box rejection of a track with two positive views
    # cancels one of them instead of resetting the track, this many times per
    # track (LoopConfig.small_reject_net_votes).  0 disables.
    count_small_reject_net_votes: int = 1
    # With a supervisor energy/time margin, a small unjudged look is flown only
    # while this multiple of its estimated duration stays inside the margin.
    revisit_margin_factor: float = 2.0
    revisit_speed_mps: float = 5.0
    # Patrol/monitor fly one height and never close in: a backpack stays at
    # 7-12 px, too small to judge or rejected at that size (SITL fullsuite
    # 20261023 patrol-fence-backpacks: 1/3).  At a route waypoint, or when
    # leaving a monitor viewpoint, a query-relevant place seen only that small
    # gets a closer look (the count small-unjudged viewpoint), at most this many
    # per step.  0 disables.
    alert_look_attempts: int = 3
    # Only places whose viewpoint is within this distance of the vehicle.
    alert_look_max_detour_m: float = 30.0
    # Patrol/monitor loops re-ask a grown small rejection
    # (LoopConfig.grown_reject_reask_ratio).  None disables.
    alert_grown_reask_ratio: float | None = 1.25

    def __post_init__(self) -> None:
        if (
            isinstance(self.tracking_budget_max, bool)
            or not isinstance(self.tracking_budget_max, int)
            or self.tracking_budget_max < 1
        ):
            raise ValueError("tracking_budget_max must be a positive integer")
        if (
            isinstance(self.count_budget_max, bool)
            or not isinstance(self.count_budget_max, int)
            or self.count_budget_max < 1
        ):
            raise ValueError("count_budget_max must be a positive integer")
        if self.runtime_step_limit is not None and (
            isinstance(self.runtime_step_limit, bool)
            or not isinstance(self.runtime_step_limit, int)
            or self.runtime_step_limit < 1
        ):
            raise ValueError("runtime_step_limit must be a positive integer or None")
        if (
            isinstance(self.runtime_tail_reserve, bool)
            or not isinstance(self.runtime_tail_reserve, int)
            or self.runtime_tail_reserve < 0
        ):
            raise ValueError("runtime_tail_reserve must be a non-negative integer")
        for name in (
            "small_unjudged_revisit_attempts",
            "completion_revisit_max",
            "alert_look_attempts",
            "count_small_reject_net_votes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("revisit_margin_factor", "revisit_speed_mps", "alert_look_max_detour_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        ratio = self.alert_grown_reask_ratio
        if ratio is not None and (
            isinstance(ratio, bool) or not math.isfinite(ratio) or ratio <= 1.0
        ):
            raise ValueError("alert_grown_reask_ratio must be finite and > 1, or None")
        if (
            isinstance(self.unresolved_revisit_attempts, bool)
            or not isinstance(self.unresolved_revisit_attempts, int)
            or self.unresolved_revisit_attempts < 0
        ):
            raise ValueError("unresolved revisit attempts must be a non-negative integer")
        if (
            isinstance(self.count_rejection_revisit_attempts, bool)
            or not isinstance(self.count_rejection_revisit_attempts, int)
            or self.count_rejection_revisit_attempts < 0
        ):
            raise ValueError("count rejection revisit attempts must be a non-negative integer")
        if (
            isinstance(self.alert_same_place_m, bool)
            or not math.isfinite(self.alert_same_place_m)
            or self.alert_same_place_m < 0
        ):
            raise ValueError("alert same place distance must be finite and non-negative")
        if (
            isinstance(self.count_rejection_revisit_range_m, bool)
            or not math.isfinite(self.count_rejection_revisit_range_m)
            or self.count_rejection_revisit_range_m <= 0
        ):
            raise ValueError("count rejection revisit range must be finite and positive")
        values = (
            self.deliver_min_altitude_m,
            self.deliver_max_altitude_m,
            self.deliver_altitude_m,
            self.deliver_position_tolerance_m,
            self.deliver_hold_s,
            self.deliver_ack_timeout_s,
        )
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("deliver settings must be finite positive numbers")
        if not (
            self.deliver_min_altitude_m <= self.deliver_altitude_m <= self.deliver_max_altitude_m
        ):
            raise ValueError("deliver altitude must be within configured bounds")
        if (
            isinstance(self.no_fly_margin_m, bool)
            or not math.isfinite(self.no_fly_margin_m)
            or self.no_fly_margin_m < 0
        ):
            raise ValueError("no-fly margin must be finite and non-negative")
        if (
            isinstance(self.detour_replan_budget_ms, bool)
            or not math.isfinite(self.detour_replan_budget_ms)
            or self.detour_replan_budget_ms <= 0
        ):
            raise ValueError("detour replan budget must be finite and positive")
        if isinstance(self.detour_max_replan_failures, bool) or self.detour_max_replan_failures < 1:
            raise ValueError("detour max failures must be positive")
        if (
            isinstance(self.visibility_graph_build_limit_ms, bool)
            or not math.isfinite(self.visibility_graph_build_limit_ms)
            or self.visibility_graph_build_limit_ms <= 0
        ):
            raise ValueError("visibility graph build limit must be finite and positive")


@dataclass(slots=True)
class _AlertCluster:
    """Votes belonging to one physical location during a tracking step."""

    # ``centre`` is intentionally the first positive vote.  It is an anchor,
    # not a running mean: moving it can chain two nearby objects into one.
    centre: tuple[float, float]
    appearance: Any = None
    members: int = 0
    positive: int = 0
    negative: int = 0
    alerted: bool = False
    last_track_id: str | None = None
    track_ids: set[str] = field(default_factory=set)
    footprint_m: float | None = None

    def add(self, *, positive: bool, track_id: str) -> None:
        # Rejects can contribute to majority counting, but never alter the
        # first-positive anchor.  Positive callers have already passed the
        # appearance/geometry membership gate.
        self.members += 1
        if positive:
            self.positive += 1
        else:
            self.negative += 1
        self.last_track_id = track_id
        self.track_ids.add(track_id)


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
    # Default alert policy retains the original per-track first-positive
    # decision and spatial de-duplication independently of vote pooling.
    alert_positive_tracks: set[str] = field(default_factory=set)
    alert_positions: list[tuple[float, float]] = field(default_factory=list)
    alert_observations: list[dict[str, Any]] = field(default_factory=list)
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
    # Return-home geofence detour state.
    detour_waypoints: list[tuple[float, float]] = field(default_factory=list)
    detour_index: int = 0
    detour_planned: bool = False
    detour_replan_failures: int = 0
    # Places of lost positive-but-unresolved tracks already revisited by this step.
    revisited_places: list[tuple[float, float]] = field(default_factory=list)
    rejection_revisit_places: list[tuple[float, float]] = field(default_factory=list)
    rejection_revisit_skipped: list[dict[str, Any]] = field(default_factory=list)
    # Closer (descending) looks at small-box rejections: their records.
    rejection_revisits: list[dict[str, Any]] = field(default_factory=list)
    # Small unjudged looks started by this step and why pending places got none.
    small_unjudged_revisits: list[dict[str, Any]] = field(default_factory=list)
    small_unjudged_skipped: list[dict[str, Any]] = field(default_factory=list)
    # Patrol/monitor closer looks: records, why pending places got none, the
    # running look and the point/altitude the route resumes from afterwards.
    alert_looks: list[dict[str, Any]] = field(default_factory=list)
    alert_look_skipped: list[dict[str, Any]] = field(default_factory=list)
    alert_look_active: bool = False
    alert_look_resume: tuple[float, float, float] | None = None
    # Bounded, decision-only audit trail for offline revisit ranking analysis.
    revisit_planning: list[dict[str, Any]] = field(default_factory=list)
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
        obstacle_source: ObstacleSource | None = None,
        airframe_adapter: AirframeMotionAdapter | None = None,
    ) -> None:
        # Validate the contract here; execution gates fail the step through on_fail.
        self.plan = (
            plan
            if isinstance(plan, MissionPlan)
            else validate_mission_plan(plan, capabilities=frozenset({"payload"}))
        )
        self.capabilities = frozenset(capabilities)
        self.release_authorized = release_authorized is True
        self._clock = clock
        self.loop_factory = loop_factory
        self.refs = dict(refs or {})
        self.config = config or ExecutorConfig()
        self.airframe_adapter = airframe_adapter
        self.task_text = task_text
        package = mission_package if mission_package is not None else package
        self._package_backed = package is not None
        if package is not None and not no_fly_zones:
            no_fly_zones = getattr(package, "no_fly_zones", ())
        self.no_fly_zones = tuple(
            ((float(centre[0]), float(centre[1])), float(radius))
            for centre, radius in no_fly_zones
            if len(centre) >= 2 and math.isfinite(float(radius)) and float(radius) > 0
        )
        package_document = getattr(package, "document", None)
        self.operating_area = (
            package_document.get("operating_area")
            if isinstance(package_document, Mapping)
            else None
        )
        self._inflated_no_fly_zones = tuple(
            (centre, effective_no_fly_radius(radius, self.config.no_fly_margin_m))
            for centre, radius in self.no_fly_zones
        )
        self._visibility_graph_build_ms = 0.0
        self.mission_log: list[dict[str, Any]] = []
        self._detour_vertices: list[tuple[float, float]] = []
        self._detour_edges: dict[int, list[tuple[int, float]]] = {}
        self._detour_visibility: np.ndarray | None = None
        self._detour_distances: np.ndarray | None = None
        self._visibility_graph_area_key: tuple[tuple[str, float], ...] | None = None
        self._build_visibility_graph()
        self.mission_log.append(
            {
                "event": "visibility_graph_built",
                "build_ms": self._visibility_graph_build_ms,
                "vertices": len(self._detour_vertices),
            }
        )
        self.package_violations: list[dict[str, Any]] = []
        if (
            self._package_backed
            and self._visibility_graph_build_ms > self.config.visibility_graph_build_limit_ms
        ):
            self.package_violations.append(
                {
                    "code": "VISIBILITY_GRAPH_BUILD_EXCEEDED",
                    "step_id": self.plan.steps[0].id if self.plan.steps else None,
                    "message_zh": ("禁飞区可见图构建耗时超过准入上限，任务拒绝起飞"),
                    "message_en": (
                        "Visibility graph build time exceeds the admission limit; takeoff refused"
                    ),
                }
            )
        self.obstacle_source = obstacle_source
        self._steps = {step.id: step for step in self.plan.steps}
        # A probe loop gives the camera model and the configured clearance source.
        self._probe = self._make_loop("Find the object and record its position.")
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
        # Ephemeral protocol records consumed by MissionSupervisor/downlink.
        # Keeping these separate from the durable mission log avoids coupling
        # the pure executor to a transport implementation.
        self._protocol_events: list[dict[str, Any]] = []
        self.done = False
        # Seconds of flight left before the return reserve, when a supervisor
        # energy/time model supplies it (MissionSupervisor.evaluate).
        self.energy_margin_s: float | None = None
        self._last_loop: OnboardLoop | None = None
        self._run: _StepRun | None = self._start(self.plan.steps[0])
        self._current: list[Track] = []
        # Only findings from completed steps on the active execution path are
        # valid for finding:N references.  The executor never searches for a
        # missing finding as a substitute for a bad reference.
        self._completed_step_ids: list[str] = []
        self._successful_step_ids: list[str] = []
        self._landed_observation: bool | None = None

    def _make_loop(self, task_text: str, **overrides: Any) -> OnboardLoop:
        """Create a loop while threading the optional typed obstacle source."""
        if self.obstacle_source is not None:
            overrides.setdefault("obstacle_source", self.obstacle_source)
        return self.loop_factory(task_text, **overrides)

    def _segment_clear(
        self, start: Pose, north: float, east: float, end_down: float | None = None
    ) -> bool:
        """Apply the configured source to an executor geometry leg.

        ``end_down`` makes the leg a descent from ``start``: it must be checked
        from the current altitude, not from an already lowered start, or a roof
        between the two altitudes is never seen.
        """
        down = start[2] if end_down is None else end_down
        if self.obstacle_source is not None:
            return self.obstacle_source.segment_clear(start, (north, east, down))
        return self._probe.segment_clear((start[0], start[1], max(start[2], down)), north, east)

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
        if step.skill in {"area_search", "count", "inspect", "survey"}:
            overrides["static_ground_veto"] = True
        else:
            overrides["static_ground_veto"] = False
        loop = self._make_loop(self.task_text(step), **overrides)
        if self._inflated_no_fly_zones:
            # Fly-by orbits leave the per-step legs this executor checks; their
            # whole circle must stay out of the package no-fly zones too.
            previous = loop.pass_clear
            loop.pass_clear = lambda start, end: (
                (previous is None or previous(start, end))
                and not self._leg_enters_no_fly(start, end)
            )
        return loop

    def _start(self, step: MissionStep) -> _StepRun:
        run = _StepRun(step=step)
        # The step budget bounds a search; the loop's own step cap must not end it.
        if step.skill == "area_search":
            run.loop = self._loop(step, max_steps=10**6, restart_route=False)
        elif step.skill == "count":
            run.loop = self._loop(step, stop_on_confirm=False, max_steps=10**6, restart_route=False)
        if step.skill in {"area_search", "count"} and run.loop is not None:
            query = step.params.get("query")
            config = run.loop.config
            if isinstance(query, str) and hasattr(config, "small_unjudged_query"):
                # Detector labels naming this query mark small unjudged tracks.
                run.loop.config = replace(config, small_unjudged_query=query)
        if step.skill == "count" and run.loop is not None:
            config = run.loop.config
            if hasattr(config, "small_reject_net_votes"):
                run.loop.config = replace(
                    config, small_reject_net_votes=self.config.count_small_reject_net_votes
                )
        elif step.skill == "inspect":
            run.loop = self._loop(step)
        elif step.skill in {"patrol", "monitor"}:
            run.loop = self._loop(step, stop_on_confirm=False, max_steps=10**6)
            query = step.params.get("query")
            config = run.loop.config
            if isinstance(query, str) and hasattr(config, "small_unjudged_query"):
                # Query-labelled boxes are asked first, their small views are
                # remembered for closer looks, and a grown small rejection is
                # asked again.
                run.loop.config = replace(
                    config,
                    small_unjudged_query=query,
                    grown_reject_reask_ratio=self.config.alert_grown_reask_ratio,
                )
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
                run.loop.config,
                lost_after_steps=max(7, run.loop.config.lost_after_steps),
                # Step triangulation assumes a static target. A moving-target
                # skill must also ignore estimates inherited from a finding.
                step_triangulation=False,
                # A search loop vetoes far ground jumps as a static-object rule; a
                # followed object moves, so a brief loss must not split its track.
                static_ground_veto=False,
                # Following moving objects must not opt into static-place re-id.
                ground_reid_max_steps=(
                    0
                    if run.loop.config.ground_reid_max_steps is None
                    else run.loop.config.ground_reid_max_steps
                ),
            )
        return run

    def _finish(self, state: str, reason: str | None = None) -> None:
        run = self._run
        assert run is not None
        run.state, run.reason = state, reason
        self._last_loop = run.loop
        status = {"step": run.step.id, "skill": run.step.skill, "state": state, "reason": reason}
        if run.step.skill == "count":
            unresolved = bool(run.loop and run.loop.unresolved_positive_tracks())
            # Only a full coverage pass with nothing left unresolved is a complete
            # count; any other ending (budget, max_count, a blocked or exhausted
            # search, an interruption) can have missed objects.  Pending places
            # follow the revisit skip reasons: a place outside the step region is
            # not part of the task, and a place already looked at again has had
            # its second look; one left unvisited for budget, viewpoint or margin
            # reasons is still open.
            status["count_is_lower_bound"] = (
                reason != "coverage_complete"
                or unresolved
                or bool(self._pending_rejection_places(run))
                or bool(self._pending_small_unjudged_places(run))
            )
            if run.rejection_revisit_skipped:
                status["rejection_revisit_skipped"] = run.rejection_revisit_skipped
            netted = getattr(run.loop, "netted_rejections", None)
            if netted:
                status["small_rejects_netted"] = list(netted)
            if run.rejection_revisits:
                for record in run.rejection_revisits:
                    if record.get("outcome") is None:
                        record["outcome"] = "interrupted"
                status["rejection_revisits"] = run.rejection_revisits
        if run.step.skill in {"count", "area_search"}:
            if run.small_unjudged_revisits:
                for record in run.small_unjudged_revisits:
                    if record.get("outcome") is None:
                        record["outcome"] = "interrupted"
                status["small_unjudged_revisits"] = run.small_unjudged_revisits
            if run.small_unjudged_skipped:
                status["small_unjudged_revisit_skipped"] = run.small_unjudged_skipped
        if run.step.skill in {"patrol", "monitor"}:
            if run.alert_looks:
                for record in run.alert_looks:
                    if record.get("outcome") is None:
                        record["outcome"] = "interrupted"
                status["alert_looks"] = run.alert_looks
            if run.alert_look_skipped:
                status["alert_look_skipped"] = run.alert_look_skipped
        if run.revisit_planning:
            status["revisit_candidate_snapshots"] = run.revisit_planning
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

    def drain_protocol_events(self) -> tuple[dict[str, Any], ...]:
        """Return and clear finding/alert records produced since last drain."""
        events = tuple(self._protocol_events)
        self._protocol_events.clear()
        return events

    @property
    def count_is_lower_bound(self) -> bool | None:
        """Whether any completed count step may have missed objects."""
        values = [
            bool(item["count_is_lower_bound"])
            for item in self.history
            if "count_is_lower_bound" in item
        ]
        return any(values) if values else None

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
        frame_rgb: Any = None,
        landed: bool | None = None,
        camera_ray: Callable[[float, float], Any] | None = None,
        timestamp_s: float | None = None,
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
            detections,
            pose_ned=pose_ned,
            yaw_deg=yaw_deg,
            ground_point=project,
            frame_rgb=frame_rgb,
            # A following target may move: static multi-view intersection is
            # intentionally excluded from this skill's live ground tracking.
            **(
                {"camera_ray": camera_ray, "timestamp_s": timestamp_s}
                if camera_ray is not None and run.step.skill != "follow"
                else {}
            ),
        )
        if run.step.skill == "follow":
            run.fresh_ground_tracks = {
                track.track_id
                for track in self._current
                if projected.get(((track.bbox[0] + track.bbox[2]) / 2.0, track.bbox[3])) is not None
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

        # Pool only appearance-compatible observations within a fixed
        # first-positive anchor.  A reject cannot establish a new identity.
        positive = track.verdict in {"confirm", "approach"}
        default_alerts = (
            self.config.alert_min_identity_matches == 1 and not self.config.alert_require_majority
        )
        if default_alerts and positive:
            if track_id in run.alert_positive_tracks:
                return
            run.alert_positive_tracks.add(track_id)
        appearance = self._alert_appearance(run, track)
        footprint = getattr(appearance, "footprint_m", None)

        def cluster_radius(item: _AlertCluster) -> float:
            # Missing geometry is common in hand-built/offline tracks. Keep
            # the configured projection-error tolerance in that case; real
            # descriptors use the object-sized radius below.
            size = item.footprint_m or footprint
            return max(1.5, 1.5 * size) if size else self.config.alert_cluster_radius_m

        membership_radius = (
            cluster_radius
            if positive
            else lambda item: max(cluster_radius(item), self.config.alert_cluster_radius_m)
        )

        def co_visible_elsewhere(item: _AlertCluster) -> bool:
            # A member seen beside this track proves a different object only
            # while both still sit where that frame saw them.
            return any(
                co_visibility_proves_distinct(
                    track, member, None, track.ground, item.centre, membership_radius(item)
                )
                for member in item.track_ids.intersection(track.co_visible_with)
            )

        cluster = min(
            (
                item
                for item in run.alert_clusters
                if math.dist(track.ground, item.centre) <= membership_radius(item)
                and not co_visible_elsewhere(item)
                and (
                    appearance is None
                    or item.appearance is None
                    or appearances_consistent(appearance, item.appearance, run.loop.config)
                )
            ),
            key=lambda item: math.dist(track.ground, item.centre),
            default=None,
        )
        if cluster is None:
            if not positive:
                return
            cluster = _AlertCluster(track.ground, appearance, footprint_m=footprint)
            run.alert_clusters.append(cluster)
        cluster.add(positive=positive, track_id=track_id)

        if default_alerts:
            # Preserve the original first positive + spatial de-duplication
            # exactly, including tracks whose first positive was suppressed
            # near an existing alert and later moved away.
            if not positive:
                return

            def alert_radius(item: dict[str, Any]) -> float:
                size = item.get("footprint_m") or footprint
                return max(1.5, 1.5 * size) if size else self.config.alert_cluster_radius_m

            if any(
                math.dist(track.ground, item["position"]) <= alert_radius(item)
                and not co_visibility_proves_distinct(
                    track,
                    item.get("track_id"),
                    item.get("co_visible_geometry"),
                    track.ground,
                    item["position"],
                    alert_radius(item),
                )
                and (
                    item.get("appearance") is None
                    or appearance is None
                    or math.dist(track.ground, item["position"]) <= self.config.alert_same_place_m
                    or appearances_consistent(item["appearance"], appearance, run.loop.config)
                )
                for item in run.alert_observations
            ):
                return
            cluster.alerted = True
            self._emit_alert(run, track, track.ground)
            return

        if cluster.alerted:
            return

        if cluster.positive < self.config.alert_min_identity_matches or (
            self.config.alert_require_majority and cluster.positive <= cluster.negative
        ):
            return
        cluster.alerted = True
        self._emit_alert(run, track, cluster.centre)

    @staticmethod
    def _alert_appearance(run: _StepRun, track: Track) -> Any:
        """Use the descriptor captured by association, including range scaling."""
        appearance = getattr(track, "appearance", None)
        return appearance

    def _emit_alert(self, run: _StepRun, track: Track, point: tuple[float, float]) -> None:
        run.alert_positions.append(point)
        run.alert_observations.append(
            {
                "position": point,
                "track_id": track.track_id,
                "co_visible_with": set(track.co_visible_with),
                "co_visible_geometry": dict(getattr(track, "co_visible_geometry", {})),
                "appearance": getattr(track, "appearance", None),
                "footprint_m": getattr(getattr(track, "appearance", None), "footprint_m", None),
            }
        )
        # Keep alert association/deduplication anchored to the existing ground
        # cluster; only the reported target location uses the optional estimate.
        report_point = point
        if run.loop.config.step_triangulation and track.position_estimate is not None:
            report_point = run.loop.target_position(track) or point
        north, east = report_point
        self.alerts.append(f"{run.step.id}:alert:{run.step.params['query']}@{north:.1f},{east:.1f}")
        self._add_finding(
            run,
            {
                "track_id": track.track_id,
                "position_estimate_ne": report_point,
                "bbox": track.bbox,
                **run.loop.position_metadata(track),
            },
            None,
        )
        # The alert has its own wire record while retaining the historical
        # finding entry used by report/count consumers.
        self._protocol_events.append(
            {
                "kind": "alert",
                "step": run.step.id,
                "id": f"a{len(self.alerts)}",
                "label": run.step.params.get("query"),
                "n": float(north),
                "e": float(east),
                "track": track.track_id,
                "t": self._clock(),
            }
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
            if run.primitives >= self._budget(run.step, run):
                self._budget_end(run)
                continue
            result = self._act(run, pose_ned, yaw_deg, ground_point, frame_id)
            if result is None:
                continue  # the step finished without moving; start the next one now
            action, info = result
            if action.kind == "translate" and self._translate_enters_no_fly(
                pose_ned, yaw_deg, action
            ):
                if run.step.skill == "return_home":
                    # A live pose can invalidate a cached tangent leg (for
                    # example after wind or a delayed observation).  Never
                    # fail recovery in the air: discard the stale route and
                    # let _move_to rebuild it on the next cycle.
                    run.detour_planned = False
                    run.detour_waypoints = []
                    run.detour_index = 0
                    run.detour_replan_failures += 1
                    if run.detour_replan_failures >= self.config.detour_max_replan_failures:
                        self.alerts.append(f"{run.step.id}:no_fly_detour_guard_land_fallback")
                        run.phase = "land_fallback"
                        continue
                    return ActionPrimitive.hold(self.config.hold_s), {
                        "step": run.step.id,
                        "skill": run.step.skill,
                        "step_state": run.state,
                        "detour_replan": True,
                    }
                self.alerts.append(f"{run.step.id}:no_fly_zone")
                self._finish("failed", "no_fly_zone")
                continue
            run.primitives += 1
            # Used only when the next observation has no pose/simulation clock.
            # Never credit a commanded hold before its following observation.
            run.pending_elapsed_s = self.estimated_seconds(action, pose_ned)
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

    def _translate_enters_no_fly(self, pose: Pose, yaw_deg: float, action: ActionPrimitive) -> bool:
        """Check the exact horizontal leg represented by a translate primitive."""
        heading = math.radians(yaw_deg)
        end = (
            pose[0] + action.forward_m * math.cos(heading) - action.right_m * math.sin(heading),
            pose[1] + action.forward_m * math.sin(heading) + action.right_m * math.cos(heading),
        )
        start = (pose[0], pose[1])
        return any(
            self._segment_circle_distance(start, end, centre) < radius - 1e-9
            for centre, radius in self._inflated_no_fly_zones
        )

    @staticmethod
    def _estimated_seconds(action: ActionPrimitive) -> float:
        if action.kind == "translate":
            return math.sqrt(action.forward_m**2 + action.right_m**2 + action.up_m**2) / 5.0
        if action.kind == "turn":
            return abs(action.yaw_delta_deg) / 90.0
        return action.seconds if action.kind == "hold" else 0.0

    def estimated_seconds(self, action: ActionPrimitive, pose_ned: Pose | None = None) -> float:
        """Estimate action duration from the selected profile.

        The static helper remains for old callers; profile-aware callers use
        this method and therefore do not assume a 5 m/s aircraft.
        """
        if self.airframe_adapter is None or self.airframe_adapter.is_multirotor:
            return self._estimated_seconds(action)
        if action.kind == "hold":
            return action.seconds
        if action.kind == "turn":
            return abs(action.yaw_delta_deg) / max(
                self.airframe_adapter.profile.limits.max_angular_rate_dps, 1e-6
            )
        distance = math.sqrt(action.forward_m**2 + action.right_m**2 + action.up_m**2)
        return distance / max(self.airframe_adapter.profile.cruise_airspeed_mps, 1e-6)

    def next_motion_intent(
        self, **kwargs: Any
    ) -> tuple[MotionCommand | ActionPrimitive, dict[str, Any]]:
        """Intent-facing entry point shared by SIM and real runtime adapters."""
        action, info = self.next_action(**kwargs)
        if self.airframe_adapter is None or self.airframe_adapter.is_multirotor:
            return action, info
        pose = kwargs.get("pose_ned", (0.0, 0.0, 0.0))
        yaw = float(kwargs.get("yaw_deg", 0.0))
        command = self.airframe_adapter.from_primitive(
            action, pose_ned=pose, yaw_deg=yaw, metadata=info
        )
        if isinstance(command, MotionCommand):
            command, safety = self.airframe_adapter.guard_command(
                command,
                pose_ned=pose,
                yaw_deg=yaw,
                current_agl_m=kwargs.get("current_agl_m"),
                skill=getattr(self.current_step, "skill", None),
            )
            if safety:
                info = {**info, "safety": safety}
        return command, info

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

    def _budget(self, step: MissionStep, run: _StepRun | None = None) -> int:
        if step.skill == "land":
            return max(1, self.config.landing_timeout_steps + 1)
        if step.skill == "hover":
            return max(self.config.step_budget, int(float(step.params["duration_s"])) + 2)
        if step.skill in {"patrol", "monitor"}:
            # A fixed 150 primitive ceiling truncates long observations.  Use
            # measured seconds/primitive once available, with hold_s as the
            # conservative first-cycle estimate, and retain a generous,
            # configurable ceiling for fail-closed behavior.
            required = max(1, self.config.travel_budget)
            if run is not None:
                duration = step.params.get("duration_s")
                if duration is not None:
                    duration = max(0.0, float(duration))
                    spent = run.held_s if step.skill == "monitor" else run.elapsed_s
                    observed = (
                        run.elapsed_s / run.primitives
                        if run.primitives > 0 and run.elapsed_s > 0
                        else self.config.hold_s
                    )
                    rate = max(0.1, float(observed), float(self.config.hold_s))
                    required = max(
                        required,
                        run.primitives + math.ceil(max(0.0, duration - spent) / rate) + 2,
                    )
                if step.skill == "patrol":
                    waypoints = run.waypoints or self.route_of(str(step.params.get("route_ref", ""))) or []
                    if not waypoints:
                        return min(required, self.config.tracking_budget_max)
                    total = len(waypoints) * max(1, int(step.params.get("loops", 1)))
                    here = (0.0, 0.0)
                    legs = 0
                    for index in range(run.waypoint_index, total):
                        point = waypoints[index % len(waypoints)]
                        legs += math.ceil(math.dist(here, point) / max(self.config.max_translate_m, 1e-6)) + 2
                        here = point
                    required = max(required, run.primitives + legs + 2)
            return min(max(required, self.config.travel_budget), self.config.tracking_budget_max)
        if step.skill in {"count", "area_search"}:
            return self._count_budget(step, run)
        if step.skill in {"goto", "inspect", "return_home", "survey", "deliver"} | {"follow"}:
            return self.config.travel_budget
        return self.config.step_budget

    def _count_budget(self, step: MissionStep, run: _StepRun | None) -> int:
        """Estimate the coverage route plus registered closer-look work.

        The estimate intentionally mirrors the multirotor survey route: two
        endpoints per lane, a turn at each endpoint, and translations broken
        into ``max_translate_m`` primitives.  It is a planning bound, while
        the loop still owns the exact route and all safety checks.
        """
        if self.config.runtime_step_limit is None:
            explicit = step.params.get("step_budget")
            return max(1, min(self.config.step_budget, int(explicit))) if explicit is not None else max(1, self.config.step_budget)

        region = self.region_of(step)
        if region is None:
            base = max(1, self.config.step_budget)
        else:
            _north, _east, radius = region
            loop = run.loop if run is not None else None
            camera = self._probe.camera
            altitude = (
                step.params.get("altitude_m")
                or (getattr(loop.config, "search_altitude_m", None) if loop else None)
                or (getattr(loop, "cruise_altitude_m", None) if loop else None)
                or 20.0
            )
            altitude = max(float(altitude), self.config.min_altitude_m)
            hfov = 2.0 * math.atan2(camera.width, camera.fx)
            vfov = 2.0 * math.atan2(camera.height, camera.fy)
            depression = max(0.35, camera.pitch_down_rad - vfov / 2.0)
            far_range = altitude / max(1e-6, math.tan(depression))
            near_depression = min(math.pi / 2.0, camera.pitch_down_rad + vfov / 2.0)
            near_range = altitude / max(1e-6, math.tan(near_depression))
            slant_mid = altitude / max(1e-6, math.sin(math.atan2(altitude, (near_range + far_range) / 2.0)))
            swath = 2.0 * slant_mid * math.tan(hfov / 2.0)
            overlap = float(step.params.get("overlap", 0.0))
            spacing = max(3.0, min(60.0, swath * (1.0 - overlap)))
            lanes = max(3, math.ceil(2.0 * radius / spacing))
            # Reconstruct the same serpentine endpoints used by survey and
            # charge every inter-view leg, including the lane change.  A
            # translate can be no longer than max_translate_m; each endpoint
            # also costs a turn and an observation/hold primitive.
            waypoints: list[tuple[float, float]] = []
            for lane in range(lanes):
                offset = -radius + (lane + 0.5) * 2.0 * radius / lanes
                half_length = math.sqrt(max(0.0, radius**2 - offset**2))
                ends = [(-half_length, offset), (half_length, offset)]
                waypoints.extend(ends if lane % 2 == 0 else reversed(ends))
            here = (0.0, 0.0)
            route_primitives = 0
            for point in waypoints:
                route_primitives += math.ceil(math.dist(here, point) / max(self.config.max_translate_m, 1e-6)) + 2
                here = point
            # Search control cycles include detector/tracker settling and
            # heading corrections between nominal route legs.  SITL traces
            # show roughly three primitives per geometric route primitive;
            # keep that conservative multiplier here so a full pass does not
            # consume the historical 140-step ceiling by construction.
            base = max(1, 3 * route_primitives + 2)

        pending = 0
        if run is not None and run.loop is not None:
            pending += len(self._pending_rejection_places(run))
            pending += len(self._pending_small_unjudged_places(run))
            pending += len(run.loop.unresolved_positive_tracks())
        look_cost = max(4, math.ceil(self.config.count_rejection_revisit_range_m / max(self.config.max_translate_m, 1e-6)) + 4)
        desired = base + pending * look_cost

        # A supervisor margin is a hard gate for speculative closer looks.  A
        # zero/negative margin also preserves the historical fixed budget.
        if self.energy_margin_s is not None:
            if self.energy_margin_s <= 0:
                desired = min(desired, max(1, self.config.step_budget))
            else:
                # A margin that cannot pay for even one closer look must not
                # turn the adaptive estimate into an expansion over legacy
                # step_budget; the supervisor will still protect return home.
                if self.energy_margin_s < self.config.revisit_margin_factor * self.config.hold_s * look_cost:
                    desired = min(desired, max(1, self.config.step_budget))
                allowed_extra = math.floor(
                    self.energy_margin_s / max(1e-6, self.config.revisit_margin_factor * self.config.hold_s)
                )
                desired = min(desired, base + max(0, allowed_extra) * look_cost)

        desired = min(desired, max(1, self.config.runtime_step_limit - self._runtime_tail_reserve(run)))
        explicit = step.params.get("step_budget")
        if explicit is not None:
            desired = min(desired, max(1, int(explicit)))
        elif self.config.step_budget < 140:
            # Existing callers use small step_budget values as a deliberate
            # test/safety cap; retain that compatibility.
            desired = min(desired, self.config.step_budget)
        return max(1, min(desired, self.config.count_budget_max))

    def _runtime_tail_reserve(self, run: _StepRun | None) -> int:
        """Reserve outer-loop primitives for steps after the active search."""
        if run is None or self.config.runtime_step_limit is None:
            return 0
        try:
            index = next(i for i, item in enumerate(self.plan.steps) if item.id == run.step.id)
        except StopIteration:
            return self.config.runtime_tail_reserve
        reserve = 0
        for item in self.plan.steps[index + 1 :]:
            reserve += self.config.runtime_tail_reserve if item.skill == "return_home" else 1
        return reserve

    def _budget_end(self, run: _StepRun) -> None:
        if run.step.skill in {"patrol", "monitor"} and self._duration_reached(run):
            self._finish("succeeded", "duration_elapsed")
            return
        if run.step.skill in {"patrol", "monitor"}:
            # The dynamic budget is capped only as a safety valve.  Reaching
            # that cap must be visible to the mission log instead of looking
            # like the legacy fixed-budget truncation.
            self.alerts.append(f"{run.step.id}:tracking_budget_exhausted")
            self._finish("failed", "tracking_budget_exhausted")
            return
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
            revisit = self._revisit_unresolved(run, pose, yaw, info)
            if revisit is not None:
                return revisit
            revisit = self._revisit_small_unjudged(run, pose, yaw, info)
            if revisit is not None:
                return revisit
            # The loop's own budget ended its search; flying home is the plan's job.
            reason = "coverage_complete" if run.loop.state == "completed" else "search_exhausted"
            self._finish("succeeded" if reason == "coverage_complete" else "failed", reason)
            return None
        return action, info

    @staticmethod
    def _round_num(value):
        if value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return round(value, 3) if math.isfinite(value) else None

    def _new_revisit_plan(self, run, kind, candidates, loop, here, remaining):
        """Create a bounded audit record without changing planner inputs."""
        rows = []
        for place in list(candidates)[:24]:
            if isinstance(place, Mapping):
                centre = place.get("ground") or place.get("centre")
                min_side = place.get("min_side_px")
                predicted = place.get("predicted_min_side_px")
            else:
                centre = getattr(place, "ground", None)
                min_side = getattr(place, "verdict_side_px", None)
                predicted = None
            if centre is None:
                continue
            priority_place = place if isinstance(place, Mapping) else {
                "ground": centre, "track_id": getattr(place, "track_id", None),
                "min_side_px": min_side,
            }
            key = self._revisit_priority(loop, priority_place, here) if kind != "alert_look" else (
                math.dist(tuple(centre[:2]), here),
            )
            sides = min_side
            if isinstance(sides, (list, tuple)):
                sides = min(sides) if sides else None
            rows.append({
                "ground_ne": [self._round_num(centre[0]), self._round_num(centre[1])],
                "kind": kind,
                "priority_key": [self._round_num(v) for v in key],
                "predicted_min_side_px": self._round_num(predicted or sides),
                "selected": False,
                "skip_reason": None,
            })
        record = {
            "kind": kind,
            "remaining_primitives": int(max(0, remaining)) if remaining is not None else None,
            "energy_margin_s": self._round_num(self.energy_margin_s),
            "candidates": rows,
        }
        run.revisit_planning.append(record)
        return record

    @staticmethod
    def _finish_revisit_plan(run, plan, remaining):
        plan["remaining_primitives_after"] = int(max(0, remaining))

    def _revisit_far_small_rejection(self, run, pose, yaw, info):
        loop = run.loop
        limit = self.config.count_rejection_revisit_attempts
        if loop is None or run.step.skill != "count" or limit < 1:
            return None
        if len(run.rejection_revisit_places) >= limit:
            return None
        if (
            len(run.rejection_revisit_places) + len(run.small_unjudged_revisits)
            >= self.config.completion_revisit_max
        ):
            return None
        here = (pose[0], pose[1])
        region = self.region_of(run.step)
        area = self.operating_area if isinstance(self.operating_area, Mapping) else None

        def allowed(point):
            return (region is None or math.dist(point, region[:2]) <= region[2]) and (
                area is None or self._point_inside_area(point, area)
            )

        def reachable(point):
            return allowed(point) and not self._leg_enters_no_fly(here, point)

        candidates = sorted(
            loop.rejected_far_small_places(self.config.count_rejection_revisit_range_m),
            key=lambda item: self._revisit_priority(loop, item, here),
        )
        remaining = self._budget(run.step, run) - run.primitives
        plan = self._new_revisit_plan(run, "rejection", candidates, loop, here, remaining)
        # Why each pending place was passed over: a SITL count missed a hydrant
        # that qualified for a revisit with looks to spare, and nothing said why.
        skipped = []

        def skip(centre, reason):
            skipped.append({"centre": [round(float(v), 2) for v in centre[:2]], "reason": reason})

        for index, rejection in enumerate(candidates[:24]):
            snap = plan["candidates"][index]
            centre = tuple(rejection["ground"])
            if not allowed(centre):
                snap["skip_reason"] = "outside_region"; skip(centre, "outside_region")
                continue
            if any(
                math.dist(centre, place) <= loop.config.count_dedupe_m
                for place in run.rejection_revisit_places
            ):
                snap["skip_reason"] = "already_revisited"; skip(centre, "already_revisited")
                continue
            if any(
                record.get("position_estimate_ne") is not None
                and math.dist(centre, record["position_estimate_ne"][:2])
                <= loop.config.count_dedupe_m
                for record in loop.confirmations
            ):
                snap["skip_reason"] = "co_visible_with_counted"; skip(centre, "co_visible_with_counted")
                continue
            look, reason = loop.plan_rejection_revisit(rejection, pose, allowed=reachable)
            if look is None:
                snap["skip_reason"] = reason or "no_clear_viewpoint"; skip(centre, reason or "no_clear_viewpoint")
                continue
            snap["predicted_min_side_px"] = self._round_num(look.get("record", {}).get("predicted_min_side_px"))
            if not allowed(look["goal"]):
                snap["skip_reason"] = "viewpoint_outside_region"; skip(centre, "viewpoint_outside_region")
                continue
            if self._leg_enters_no_fly(here, look["goal"]):
                snap["skip_reason"] = "leg_enters_no_fly"; skip(centre, "leg_enters_no_fly")
                continue
            if look.get("staged"):
                # The closer look: as a small unjudged look, descent and climb included.
                needed = self._staged_look_steps(loop, look)
                seconds = self._staged_look_seconds(loop, look, here)
                if needed > remaining:
                    snap["skip_reason"] = "budget"; skip(centre, "budget")
                    continue
                if (
                    self.energy_margin_s is not None
                    and self.config.revisit_margin_factor * seconds >= self.energy_margin_s
                ):
                    snap["skip_reason"] = "energy_margin"; skip(centre, "energy_margin")
                    continue
                self._fit_reapproach(loop, look, allowed, remaining - needed, seconds)
            else:
                legs = math.ceil(
                    math.dist(here, look["goal"]) / max(loop.config.max_translate_m, 1e-6)
                )
                if legs + 3 > remaining:
                    snap["skip_reason"] = "budget"; skip(centre, "budget")
                    continue
            rejection["revisited"] = True
            run.rejection_revisit_places.append(centre)
            action = loop.begin_place_revisit(look, pose, yaw, info)
            if action is None:
                rejection["revisited"] = False
                run.rejection_revisit_places.pop()
                snap["skip_reason"] = "revisit_not_started"; skip(centre, "revisit_not_started")
                continue
            snap["selected"] = True
            record = {"centre": list(look["centre"]), "track_id": rejection.get("track_id")}
            if look.get("staged"):
                record = look["record"]
                record["track_id"] = rejection.get("track_id")
                run.rejection_revisits.append(record)
            info["count_rejection_revisit"] = record
            self._finish_revisit_plan(run, plan, self._budget(run.step, run) - run.primitives)
            self.mission_log.append(
                {"event": "count_rejection_revisit", "step_id": run.step.id, **record}
            )
            return action, info
        self._finish_revisit_plan(run, plan, self._budget(run.step, run) - run.primitives)
        run.rejection_revisit_skipped = skipped
        if skipped:
            info["count_rejection_revisit_skipped"] = skipped
            self.mission_log.append(
                {
                    "event": "count_rejection_revisit_skipped",
                    "step_id": run.step.id,
                    "skipped": skipped,
                }
            )
        return None

    def _count(self, run, pose, yaw, ground_point, frame_id):
        action, info = run.loop.next_action(
            self._current, pose_ned=pose, yaw_deg=yaw, ground_point=ground_point
        )
        if "counted" in info:
            self._add_finding(run, info["counted"], frame_id)
        # The count so far is reported live, so an interrupted mission still says it.
        self._report_count(run)
        if run.loop.state not in {"searching", "approaching"}:
            revisit = self._revisit_unresolved(run, pose, yaw, info)
            if revisit is not None:
                return revisit
            revisit = self._revisit_small_unjudged(run, pose, yaw, info)
            if revisit is not None:
                return revisit
            revisit = self._revisit_far_small_rejection(run, pose, yaw, info)
            if revisit is not None:
                return revisit
            self._report_count(run)
            reason = "coverage_complete" if run.loop.state == "completed" else "search_exhausted"
            self._finish("succeeded", reason)
            return None
        limit = run.step.params.get("max_count")
        if limit is not None and len(run.loop.confirmations) >= int(limit):
            self._report_count(run)
            self._finish("succeeded", "max_count")
            return ActionPrimitive.hold(self.config.hold_s), info
        return action, info

    def _revisit_unresolved(self, run, pose, yaw, info):
        """Before a search ends, look again at a lost positive place that was never resolved.

        A small candidate judged "approach" can be dropped by the tracker before it
        is ever confirmed.  Nearest first, each such place gets at most
        ``unresolved_revisit_attempts`` looks, and only when the place and the
        viewpoint lie in the step region and operating area, the leg avoids
        no-fly zones, and the remaining step budget pays for the flight.  The
        loop then judges the place with its normal identity and confirm rules.
        """
        loop = run.loop
        limit = self.config.unresolved_revisit_attempts
        if loop is None or limit < 1:
            return None
        here = (pose[0], pose[1])
        region = self.region_of(run.step)
        area = self.operating_area if isinstance(self.operating_area, Mapping) else None

        def allowed(point: tuple[float, float]) -> bool:
            return (region is None or math.dist(point, region[:2]) <= region[2]) and (
                area is None or self._point_inside_area(point, area)
            )

        remaining = self._budget(run.step, run) - run.primitives
        candidates = sorted(
            loop.unresolved_positive_tracks(),
            key=lambda track: math.dist(loop.target_position(track), here),
        )
        plan = self._new_revisit_plan(run, "revisit", candidates, loop, here, remaining)
        for index, track in enumerate(candidates[:24]):
            snap = plan["candidates"][index]
            centre = loop.target_position(track)
            radius = max(loop.place_radius(track), loop.config.count_dedupe_m)
            attempts = sum(math.dist(centre, place) <= radius for place in run.revisited_places)
            if attempts >= limit or not allowed(centre):
                snap["skip_reason"] = "attempts_exhausted" if attempts >= limit else "outside_region"
                continue
            look = loop.plan_place_revisit(track, pose)
            if look is None or not allowed(look["goal"]):
                snap["skip_reason"] = "no_clear_viewpoint"
                continue
            if self._leg_enters_no_fly(here, look["goal"]):
                snap["skip_reason"] = "leg_enters_no_fly"
                continue
            # Turn, legs, face and look; the confirmation itself runs on the remainder.
            legs = math.ceil(math.dist(here, look["goal"]) / max(loop.config.max_translate_m, 1e-6))
            if legs + 3 > remaining:
                snap["skip_reason"] = "budget"
                continue
            run.revisited_places.append(centre)
            action = loop.begin_place_revisit(look, pose, yaw, info)
            if action is None:
                snap["skip_reason"] = "revisit_not_started"
                continue
            snap["selected"] = True
            record = {"track_id": track.track_id, "centre": list(look["centre"])}
            info["unresolved_revisit"] = record
            self._finish_revisit_plan(run, plan, self._budget(run.step, run) - run.primitives)
            self.mission_log.append(
                {"event": "unresolved_place_revisit", "step_id": run.step.id, **record}
            )
            return action, info
        self._finish_revisit_plan(run, plan, remaining)
        return None

    def _in_step_area(self, run: _StepRun, point: Sequence[float]) -> bool:
        """Whether ``point`` lies in the step's region and the operating area."""
        region = self.region_of(run.step)
        area = self.operating_area if isinstance(self.operating_area, Mapping) else None
        return (region is None or math.dist(point[:2], region[:2]) <= region[2]) and (
            area is None or self._point_inside_area(point, area)
        )

    def _looked_places(self, run: _StepRun) -> list[tuple[float, float]]:
        """Places this step already flew back to look at, by any revisit kind."""
        return [
            *run.revisited_places,
            *run.rejection_revisit_places,
            *(tuple(record["centre"]) for record in run.small_unjudged_revisits),
        ]

    def _pending_rejection_places(self, run: _StepRun) -> list[dict[str, Any]]:
        """Far/small rejected places that still lack a usable second look.

        The same exclusions as ``_revisit_far_small_rejection``'s skip reasons:
        near a counted object, outside the step region, already revisited, or
        within the de-duplication radius of a counted object (so it is the
        same object). Co-visibility alone is not evidence of identity.
        """
        loop = run.loop
        if loop is None:
            return []
        looked = self._looked_places(run)
        pending = []
        for item in loop.rejected_far_small_places(self.config.count_rejection_revisit_range_m):
            centre = item.get("ground")
            if centre is None:
                continue
            if any(
                record.get("position_estimate_ne") is not None
                and math.dist(centre, record["position_estimate_ne"][:2])
                <= loop.config.count_dedupe_m
                for record in loop.confirmations
            ):
                continue
            if not self._in_step_area(run, centre):
                continue
            if any(math.dist(centre, place) <= loop.config.count_dedupe_m for place in looked):
                continue
            if any(
                record.get("position_estimate_ne") is not None
                and math.dist(centre, record["position_estimate_ne"][:2])
                <= loop.config.count_dedupe_m
                for record in loop.confirmations
            ):
                continue
            pending.append(item)
        return pending

    @staticmethod
    def _revisit_priority(loop: OnboardLoop, place: Mapping[str, Any], here) -> tuple:
        """Order revisit work by evidence, query relevance, size, then distance."""
        tracks = getattr(loop, "_tracks", {})
        ids = [place.get("track_id")] if place.get("track_id") else place.get("track_ids", ())
        items = [tracks[track_id] for track_id in ids if track_id in tracks]
        positive = any(
            bool(getattr(track, "confirm_steps", ()) or getattr(track, "match_steps", ()))
            or any(v in {"match", "approach", "confirm"} for v in getattr(track, "identity_verdict_history", ()))
            or getattr(track, "verdict", None) in {"approach", "confirm"}
            for track in items
        )
        query = getattr(loop.config, "small_unjudged_query", None)
        relevant = any(
            bool(query) and label_matches_query(getattr(track, "label", None), query)
            for track in items
        )
        sides = place.get("min_side_px") or ()
        if isinstance(sides, (int, float)):
            smallest = float(sides)
        else:
            smallest = min(sides) if sides else float("inf")
        centre = place.get("centre") or place.get("ground")
        return (0 if positive else 1, 0 if relevant else 1, smallest, math.dist(centre, here))

    def _pending_small_unjudged_places(self, run: _StepRun) -> list[dict[str, Any]]:
        """Relevant small unjudged places inside the step that were never looked at."""
        loop = run.loop
        if loop is None:
            return []
        looked = self._looked_places(run)
        return [
            place
            for place in loop.small_unjudged_places()
            if self._in_step_area(run, place["centre"])
            and not any(
                math.dist(place["centre"], point) <= loop.config.count_dedupe_m for point in looked
            )
        ]

    @staticmethod
    def _staged_look_steps(loop, look) -> int:
        """Primitives of a count closer look: turns, legs, descent, face, holds,
        climb back, and two to judge."""
        return look["legs"] + 2 * look["vertical_steps"] + loop.config.small_unjudged_look_holds + 5

    def _staged_look_seconds(self, loop, look, here) -> float:
        """Seconds of a count closer look, including any extra way home from its viewpoint."""
        home = self.home or here
        extra_home = max(0.0, math.dist(look["goal"], home) - math.dist(here, home))
        return (
            look["flight_m"] + extra_home + 2.0 * look["vertical_m"]
        ) / self.config.revisit_speed_mps + (
            loop.config.small_unjudged_look_holds + 3
        ) * self.config.hold_s

    def _fit_reapproach(self, loop, look, allowed, spare_steps, seconds, spare_s=None) -> None:
        """Let a closer look move in once more only when the step can pay for it.

        ``spare_steps`` is what the step budget leaves after the look itself,
        ``seconds`` the look's own time and ``spare_s`` any duration left after
        it.  A re-approach leg must stay in the step's area and out of no-fly
        zones from wherever it starts.
        """
        look["leg_allowed"] = lambda start, point: (
            allowed(point) and not self._leg_enters_no_fly(start, point)
        )
        if look.get("reapproaches_left", 0) < 1:
            return
        extra_s = (
            2.0 * look["reapproach_m"] / self.config.revisit_speed_mps
            + (loop.config.small_unjudged_look_holds + 2) * self.config.hold_s
        )
        if (
            look["reapproach_steps"] > spare_steps
            or (spare_s is not None and extra_s >= spare_s)
            or (
                self.energy_margin_s is not None
                and self.config.revisit_margin_factor * (seconds + extra_s) >= self.energy_margin_s
            )
        ):
            look["reapproaches_left"] = 0
            look["record"]["reapproach_skipped"] = "budget"

    def _revisit_small_unjudged(self, run, pose, yaw, info):
        """Before a search ends, look closer at query-relevant places never judged.

        A far object of the queried class can stay below the judging size for
        every frame it is in (SITL gen_cnt2_w0001: a hydrant seen once at
        7.1 px was never judged, and the coverage pass turned away), and a
        judgeable box can lose the per-step slow-call cap to larger boxes and
        leave the view (episode 9d8bbe13: a 15.4 px hydrant, once).  Nearest
        first, each such place gets one look from a viewpoint where its box
        reaches the judging size, while the step's attempts, the cap shared
        with far/small rejection looks, the step budget and any supervisor
        energy/time margin allow it.  The loop then judges and counts the place
        with its normal rules.  Why a pending place got no look is recorded.
        """
        loop = run.loop
        limit = self.config.small_unjudged_revisit_attempts
        if loop is None or run.step.skill not in {"count", "area_search"} or limit < 1:
            return None
        places = loop.small_unjudged_places()
        if not places:
            return None
        here = (pose[0], pose[1])
        region = self.region_of(run.step)
        area = self.operating_area if isinstance(self.operating_area, Mapping) else None

        def allowed(point):
            return (region is None or math.dist(point, region[:2]) <= region[2]) and (
                area is None or self._point_inside_area(point, area)
            )

        def reachable(point):
            return allowed(point) and not self._leg_enters_no_fly(here, point)

        skipped: list[dict[str, Any]] = []

        def skip(place, reason):
            skipped.append(
                {
                    "centre": [round(float(v), 2) for v in place["centre"][:2]],
                    "track_ids": list(place["track_ids"]),
                    "reason": reason,
                }
            )

        looked = [
            *run.revisited_places,
            *run.rejection_revisit_places,
            *(tuple(record["centre"]) for record in run.small_unjudged_revisits),
        ]
        remaining = self._budget(run.step, run) - run.primitives
        places = sorted(places, key=lambda item: self._revisit_priority(loop, item, here))
        plan = self._new_revisit_plan(run, "small_unjudged", places, loop, here, remaining)
        for index, place in enumerate(places[:24]):
            snap = plan["candidates"][index]
            centre = place["centre"]
            if not allowed(centre):
                snap["skip_reason"] = "outside_region"
                skip(place, "outside_region")
                continue
            if any(math.dist(centre, point) <= loop.config.count_dedupe_m for point in looked):
                snap["skip_reason"] = "already_revisited"
                skip(place, "already_revisited")
                continue
            if len(run.small_unjudged_revisits) >= limit:
                snap["skip_reason"] = "attempts_exhausted"
                skip(place, "attempts_exhausted")
                continue
            used = len(run.small_unjudged_revisits) + (
                len(run.rejection_revisit_places) if run.step.skill == "count" else 0
            )
            if used >= self.config.completion_revisit_max:
                snap["skip_reason"] = "revisit_cap"
                skip(place, "revisit_cap")
                continue
            look, reason = loop.plan_small_unjudged_revisit(place, pose, allowed=reachable)
            if look is None:
                snap["skip_reason"] = reason or "no_clear_viewpoint"
                skip(place, reason or "no_clear_viewpoint")
                continue
            needed = self._staged_look_steps(loop, look)
            snap["predicted_min_side_px"] = self._round_num(look.get("record", {}).get("predicted_min_side_px"))
            if needed > remaining:
                snap["skip_reason"] = "budget"
                skip(place, "budget")
                continue
            seconds = self._staged_look_seconds(loop, look, here)
            if (
                self.energy_margin_s is not None
                and self.config.revisit_margin_factor * seconds >= self.energy_margin_s
            ):
                snap["skip_reason"] = "energy_margin"
                skip(place, "energy_margin")
                continue
            self._fit_reapproach(loop, look, allowed, remaining - needed, seconds)
            record = look["record"]
            action = loop.begin_place_revisit(look, pose, yaw, info)
            if action is None:
                snap["skip_reason"] = "revisit_not_started"
                skip(place, "revisit_not_started")
                continue
            run.small_unjudged_revisits.append(record)
            snap["selected"] = True
            info["small_unjudged_revisit"] = record
            self._finish_revisit_plan(run, plan, self._budget(run.step, run) - run.primitives)
            self.mission_log.append(
                {"event": "small_unjudged_revisit", "step_id": run.step.id, **record}
            )
            if skipped:
                run.small_unjudged_skipped = skipped
            return action, info
        self._finish_revisit_plan(run, plan, remaining)
        run.small_unjudged_skipped = skipped
        if skipped:
            info["small_unjudged_revisit_skipped"] = skipped
            self.mission_log.append(
                {
                    "event": "small_unjudged_revisit_skipped",
                    "step_id": run.step.id,
                    "skipped": skipped,
                }
            )
        return None

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
        finding.update(
            {
                key: value
                for key, value in record.items()
                if key.startswith("position_") and key != "position_estimate_ne"
            }
        )
        self.findings.append(finding)
        if run.loop is not None:
            self._finding_loops[id(finding)] = run.loop
        self.track_ids.append(str(record.get("track_id")))
        if frame_id:
            self.evidence.append(frame_id)
        event = {
            "kind": "finding",
            "step": run.step.id,
            "id": f"f{len(self.findings)}",
            "label": finding.get("query"),
            "n": finding["position_ne"][0],
            "e": finding["position_ne"][1],
            "evidence_frame": frame_id,
            "track": finding.get("track_id"),
            "t": self._clock(),
        }
        for source, target in (("sigma_m", "sigma_m"), ("confidence", "confidence")):
            if source in record:
                event[target] = record[source]
        self._protocol_events.append(event)

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
        if self.airframe_adapter is not None and not self.airframe_adapter.is_multirotor:
            orbit = self.airframe_adapter.observation_orbit(
                (*centre, 0.0), standoff, altitude_m=self._inspect_altitude(run), views=views
            )
            run.viewpoints = [point.position_ne for point in orbit.waypoints[:-1]]
            run.goal_altitude_m = self._inspect_altitude(run)
            return
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
                track.track_id == run.pending_track and track.verdict in {"confirm", "approach"}
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
            if self.airframe_adapter is not None and not self.airframe_adapter.is_multirotor:
                route = self.airframe_adapter.search_path(
                    (north - radius, north + radius, east - radius, east + radius),
                    footprint_width_m=max(swath, spacing),
                    overlap=float(run.step.params["overlap"]),
                )
                run.waypoints.extend(point.position_ne for point in route.waypoints)
            else:
                # Multirotor route generation is intentionally unchanged.
                lanes = max(3, math.ceil(2.0 * radius / spacing))
                for lane in range(lanes):
                    offset = -radius + (lane + 0.5) * 2.0 * radius / lanes
                    half_length = math.sqrt(max(0.0, radius**2 - offset**2))
                    ends = [
                        (north - half_length, east + offset),
                        (north + half_length, east + offset),
                    ]
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

    def _tracking_reserve(self, run: _StepRun, pose: Pose) -> int:
        """Primitives the rest of a patrol/monitor step still needs."""
        reserves = []
        duration = run.step.params.get("duration_s")
        if duration is not None:
            spent = run.held_s if run.step.skill == "monitor" else run.elapsed_s
            remaining_s = max(0.0, float(duration) - spent)
            # Observed seconds per primitive (holds, turns and legs alike).
            rate = max(1.0, run.elapsed_s / max(1, run.primitives))
            reserves.append(math.ceil(remaining_s / rate))
        if run.step.skill == "patrol" and run.waypoints:
            total = len(run.waypoints) * int(run.step.params["loops"])
            here, legs = (pose[0], pose[1]), 0
            for index in range(run.waypoint_index, total):
                point = run.waypoints[index % len(run.waypoints)]
                legs += math.ceil(math.dist(here, point) / max(self.config.max_translate_m, 1e-6))
                legs += 1  # the turn onto the leg
                here = point
            reserves.append(legs)
        return min(reserves) if reserves else 0

    def _alert_look(self, run: _StepRun, pose: Pose, yaw: float, *, start: bool):
        """Run, finish or start a patrol/monitor closer look at a small place.

        Patrol and monitor hold one height, so a query-relevant object can be
        seen only below the judging size, or rejected only from such boxes,
        for the whole step (SITL fullsuite 20261023: patrol 1/3 backpacks; one
        never above 8 px, one rejected three times at 10 px from 28 m).  At a
        decision point (``start``: a route waypoint reached, a monitor viewpoint
        done) the nearest such place gets the count small-unjudged look: a
        viewpoint where its box reaches the judging size, clear legs and a clear
        descent, the slow model asked again there, then the climb back.  The
        vehicle then returns to where the look began and the route resumes.  A
        look needs a free attempt, a viewpoint within ``alert_look_max_detour_m``,
        the step's primitive budget beyond what the rest of the step needs, the
        remaining duration and any supervisor margin.  Nothing here raises an
        alert: the normal alert rule judges the verdicts the look collects.
        """
        loop = run.loop
        if loop is None or not hasattr(loop, "continue_revisit"):
            return None
        if run.alert_look_active:
            info: dict[str, Any] = {"phase": "alert_look"}
            action = loop.continue_revisit(pose, yaw, info)
            if action is not None:
                return action, info
            run.alert_look_active = False
            record = run.alert_looks[-1]
            self.mission_log.append(
                {
                    "event": "alert_look_end",
                    "step_id": run.step.id,
                    "centre": record.get("centre"),
                    "outcome": record.get("outcome"),
                }
            )
        if run.alert_look_resume is not None:
            resume = run.alert_look_resume
            moved = self._move_to(run, pose, yaw, resume[:2], resume[2])
            if moved == "no_fly":
                return None
            if moved is not None and moved != "blocked":
                return moved, {"phase": "alert_look_return"}
            # Arrived (or the way back is blocked: the route's own leg decides).
            run.alert_look_resume = None
            run.blocked = 0
        if not start or self.config.alert_look_attempts < 1:
            return None
        if getattr(loop.config, "fixed_wing_pass", False) or (
            self.airframe_adapter is not None and not self.airframe_adapter.is_multirotor
        ):
            # The look hovers, descends and turns in place: multirotor only.
            return None
        places = [
            *({**place, "kind": "small_unjudged"} for place in loop.small_unjudged_places()),
            *({**place, "kind": "small_rejected"} for place in loop.small_rejected_places()),
        ]
        if not places:
            return None
        here = (pose[0], pose[1])
        region = self.region_of(run.step)
        area = self.operating_area if isinstance(self.operating_area, Mapping) else None

        def allowed(point):
            return (region is None or math.dist(point, region[:2]) <= region[2]) and (
                area is None or self._point_inside_area(point, area)
            )

        def reachable(point):
            return allowed(point) and not self._leg_enters_no_fly(here, point)

        skipped: list[dict[str, Any]] = []

        def skip(place, reason):
            skipped.append(
                {
                    "centre": [round(float(v), 2) for v in place["centre"][:2]],
                    "track_ids": list(place["track_ids"]),
                    "kind": place["kind"],
                    "reason": reason,
                }
            )

        dedupe = loop.config.count_dedupe_m
        looked = [tuple(record["centre"]) for record in run.alert_looks]
        remaining = self._budget(run.step, run) - run.primitives
        duration = run.step.params.get("duration_s")
        remaining_s = None
        if duration is not None:
            spent = run.held_s if run.step.skill == "monitor" else run.elapsed_s
            remaining_s = float(duration) - spent
        started = None
        places = sorted(places, key=lambda item: math.dist(item["centre"], here))
        plan = self._new_revisit_plan(run, "alert_look", places, loop, here, remaining)
        for index, place in enumerate(places[:24]):
            snap = plan["candidates"][index]
            centre = place["centre"]
            if not allowed(centre):
                snap["skip_reason"] = "outside_region"
                skip(place, "outside_region")
                continue
            if any(math.dist(centre, point) <= dedupe for point in looked):
                snap["skip_reason"] = "already_looked"
                skip(place, "already_looked")
                continue
            if any(math.dist(centre, point) <= dedupe for point in run.alert_positions):
                snap["skip_reason"] = "already_alerted"
                skip(place, "already_alerted")
                continue
            if started is not None:
                snap["skip_reason"] = "another_candidate_selected"
                continue
            if len(run.alert_looks) >= self.config.alert_look_attempts:
                snap["skip_reason"] = "attempts_exhausted"
                skip(place, "attempts_exhausted")
                continue
            look, reason = loop.plan_small_unjudged_revisit(place, pose, allowed=reachable)
            if look is None:
                snap["skip_reason"] = reason or "no_clear_viewpoint"
                skip(place, reason or "no_clear_viewpoint")
                continue
            if math.dist(here, look["goal"]) > self.config.alert_look_max_detour_m:
                snap["skip_reason"] = "too_far"
                skip(place, "too_far")
                continue
            # Out: turns, legs, descent, face, holds, climb, two to judge; back: legs.
            needed = (
                2 * look["legs"]
                + 2 * look["vertical_steps"]
                + loop.config.small_unjudged_look_holds
                + 5
            )
            # The rest of the step keeps its primitives.  A step that cannot
            # finish inside its budget anyway (a 900 s monitor at ~3 s per
            # primitive) only needs room for the look itself.
            reserve = self._tracking_reserve(run, pose)
            if needed > remaining or (reserve <= remaining and needed + reserve > remaining):
                snap["skip_reason"] = "budget"
                skip(place, "budget")
                continue
            seconds = (
                2.0 * look["flight_m"] + 2.0 * look["vertical_m"]
            ) / self.config.revisit_speed_mps + (
                loop.config.small_unjudged_look_holds + 3
            ) * self.config.hold_s
            if remaining_s is not None and seconds >= remaining_s:
                snap["skip_reason"] = "duration"
                skip(place, "duration")
                continue
            if (
                self.energy_margin_s is not None
                and self.config.revisit_margin_factor * seconds >= self.energy_margin_s
            ):
                snap["skip_reason"] = "energy_margin"
                skip(place, "energy_margin")
                continue
            self._fit_reapproach(
                loop,
                look,
                allowed,
                remaining - needed - (reserve if reserve <= remaining else 0),
                seconds,
                None if remaining_s is None else remaining_s - seconds,
            )
            look["alert_look"] = True
            record = look["record"]
            record["kind"] = place["kind"]
            info = {"phase": "alert_look"}
            action = loop.begin_place_revisit(look, pose, yaw, info)
            if action is None:
                snap["skip_reason"] = "look_not_started"
                skip(place, "look_not_started")
                continue
            run.alert_looks.append(record)
            snap["predicted_min_side_px"] = self._round_num(record.get("predicted_min_side_px"))
            snap["selected"] = True
            run.alert_look_active = True
            run.alert_look_resume = (pose[0], pose[1], max(0.0, -pose[2]))
            info["alert_look"] = record
            self.mission_log.append({"event": "alert_look", "step_id": run.step.id, **record})
            started = (action, info)
        self._finish_revisit_plan(run, plan, remaining)
        if skipped != run.alert_look_skipped:
            run.alert_look_skipped = skipped
            if skipped:
                self.mission_log.append(
                    {"event": "alert_look_skipped", "step_id": run.step.id, "skipped": skipped}
                )
        return started

    def _patrol(self, run, pose, yaw, ground_point, frame_id):
        if not run.waypoints:
            route = self.route_of(str(run.step.params["route_ref"]))
            if route is None:
                self._finish("failed", "unknown_ref")
                return None
            run.waypoints = route
            if "altitude_m" in run.step.params:
                run.goal_altitude_m = float(run.step.params["altitude_m"])
        # A running look (and the flight back to the route) finishes first.
        look = self._alert_look(run, pose, yaw, start=False)
        if look is not None:
            return look
        if run.state != "running":
            return None
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
            look = self._alert_look(run, pose, yaw, start=True)
            if look is not None:
                return look
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
            (
                (camera.width - camera.cx) * sine,
                math.hypot((camera.width - camera.cx) * cosine, camera.fx),
            ),
            (camera.fy * cosine + camera.cy * sine, abs(camera.cy * cosine - camera.fy * sine)),
            (
                (camera.height - camera.cy) * sine - camera.fy * cosine,
                abs((camera.height - camera.cy) * cosine + camera.fy * sine),
            ),
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
            if (
                needed is not None
                and needed <= self.config.max_altitude_m
                and ("altitude_m" not in run.step.params or altitude >= needed)
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
                    (
                        north + 0.75 * radius * math.cos(start + index * math.pi / 4.0),
                        east + 0.75 * radius * math.sin(start + index * math.pi / 4.0),
                    )
                    for index in range(8)
                ]
            # Equal-area disk samples estimate the union of actual camera
            # footprints; reaching all waypoints alone never implies coverage.
            run.coverage_points = [
                (
                    north
                    + radius * math.sqrt((ring + 0.5) / 12.0) * math.cos(angle * math.tau / 48),
                    east
                    + radius * math.sqrt((ring + 0.5) / 12.0) * math.sin(angle * math.tau / 48),
                )
                for ring in range(12)
                for angle in range(48)
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
        # A running look (and the flight back to the viewpoint) finishes first.
        look = self._alert_look(run, pose, yaw, start=False)
        if look is not None:
            return look
        if run.state != "running":
            return None
        if run.observation_start_s is not None:
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
            if run.captured >= 4:
                # The four views of this viewpoint are done: a closer look at a
                # small place, if one is pending, before the next viewpoint.
                look = self._alert_look(run, pose, yaw, start=True)
                if look is not None:
                    if len(run.waypoints) > 1:
                        run.waypoint_index += 1
                    run.captured = 0
                    run.phase = "fly"
                    return look
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
                (
                    t
                    for t in self._current
                    if t.verdict in {"confirm", "approach"}
                    and t.track_id in run.fresh_ground_tracks
                ),
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
            confidence = (
                0.0
                if age is None
                else 2.0
                ** (
                    -max(
                        age / max(self.config.follow_stale_s, 1e-6),
                        run.lost_cycles / max(self.config.follow_stale_cycles, 1),
                    )
                )
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
                if stale
                else ActionPrimitive.hold(self.config.hold_s)
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
            "track_id": track.track_id,
            "range_m": distance,
            "standoff_m": standoff,
            "follow_confidence": 1.0,
            "ground_age_s": 0.0,
            "follow_state": "tracking",
        }
        delta = _wrap(math.degrees(math.atan2(de, dn)) - yaw)
        # Detour edges are tangent to the inflated polygon.  The normal
        # flight tolerance can cut across that tangent, so align the vehicle
        # tightly before each cached-detour leg.
        turn_tolerance = (
            1.0
            if run.step.skill == "return_home" and run.detour_planned
            else self.config.turn_tolerance_deg
        )
        if abs(delta) > turn_tolerance:
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
        altitude = max(0.0, -pose[2])
        altitude_tolerance = 1.0 if tolerance_m is None else min(1.0, tolerance_m / 2.0)
        if altitude_m is not None and abs(altitude - altitude_m) > altitude_tolerance:
            # A pure climb leaves the obstacle footprint vertically; obstacle
            # clearance at the current position must not hold a legal escape.
            if altitude_m < altitude and not self._segment_clear(
                pose, pose[0], pose[1], -altitude_m
            ):
                run.blocked += 1
                if run.blocked > self.config.max_blocked_legs:
                    return "blocked"
                return ActionPrimitive.hold(self.config.hold_s)
            run.blocked = 0
            return ActionPrimitive.translate(0.0, 0.0, altitude_m - altitude)
        target = (float(goal_ne[0]), float(goal_ne[1]))
        if run.step.skill == "return_home" and run.detour_planned:
            while run.detour_index < len(run.detour_waypoints):
                waypoint = run.detour_waypoints[run.detour_index]
                if math.dist((pose[0], pose[1]), waypoint) > (
                    self.config.arrive_tolerance_m if tolerance_m is None else tolerance_m
                ):
                    target = waypoint
                    break
                run.detour_index += 1
            if run.detour_index < len(run.detour_waypoints):
                target = run.detour_waypoints[run.detour_index]
        if self._leg_enters_no_fly((pose[0], pose[1]), target):
            if run.step.skill == "return_home" and run.detour_planned:
                # Numerical error or a changed pose invalidated this cached
                # tangent. Replan from the observed position on the next
                # cycle; recovery must not be failed in the air.
                run.detour_planned = False
                run.detour_waypoints = []
                run.detour_index = 0
                return ActionPrimitive.hold(self.config.hold_s)
            if run.step.skill == "return_home" and not run.detour_planned:
                detour = self._return_home_detour((pose[0], pose[1]), target)
                if detour is _DETOUR_TIMEOUT:
                    run.detour_replan_failures += 1
                    if run.detour_replan_failures >= self.config.detour_max_replan_failures:
                        self.alerts.append(f"{run.step.id}:no_fly_detour_timeout_land_fallback")
                        run.phase = "land_fallback"
                        return "land_fallback"
                    return ActionPrimitive.hold(self.config.hold_s)
                run.detour_replan_failures = 0
                run.detour_planned = True
                if detour:
                    run.detour_waypoints = detour
                    run.detour_index = 0
                    target = detour[0]
                else:
                    self.alerts.append(f"{run.step.id}:no_fly_zone_land_fallback")
                    run.phase = "land_fallback"
                    return "land_fallback"
            else:
                self.alerts.append(f"{run.step.id}:no_fly_zone")
                self._finish("failed", "no_fly_zone")
                return "no_fly"
        dn, de = target[0] - pose[0], target[1] - pose[1]
        distance = math.hypot(dn, de)
        if distance <= (self.config.arrive_tolerance_m if tolerance_m is None else tolerance_m):
            return None
        delta = _wrap(math.degrees(math.atan2(de, dn)) - yaw)
        turn_tolerance = (
            1.0
            if run.step.skill == "return_home" and run.detour_planned
            else self.config.turn_tolerance_deg
        )
        if abs(delta) > turn_tolerance:
            return ActionPrimitive.turn(delta)
        step = min(self.config.max_translate_m, distance)
        heading = math.radians(yaw)
        end = (pose[0] + step * math.cos(heading), pose[1] + step * math.sin(heading))
        if not self._segment_clear(pose, end[0], end[1]):
            run.blocked += 1
            if run.blocked > self.config.max_blocked_legs:
                return "blocked"
            return ActionPrimitive.hold(self.config.hold_s)
        run.blocked = 0
        return ActionPrimitive.translate(step, 0.0, 0.0)

    def _return_home_detour(
        self, start: tuple[float, float], goal: tuple[float, float]
    ) -> list[tuple[float, float]] | None | object:
        """Connect live endpoints to the cached static visibility graph."""
        area = self.operating_area
        if not isinstance(area, Mapping):
            return None
        area_key = tuple(
            sorted(
                (str(key), float(area[key]))
                for key in ("north_min_m", "north_max_m", "east_min_m", "east_max_m")
                if key in area
            )
        )
        # Legacy callers may attach an area after construction. Package-backed
        # executors always build before takeoff and never rebuild here.
        if self._visibility_graph_area_key != area_key and not self._package_backed:
            self._build_visibility_graph()
        # Graph construction is an admission-time cost; the runtime budget
        # covers only endpoint connection and route search.
        started = time.perf_counter()
        circles = self._inflated_no_fly_zones
        if not circles:
            return []

        def inside(point: tuple[float, float]) -> bool:
            return (
                area["north_min_m"] <= point[0] <= area["north_max_m"]
                and area["east_min_m"] <= point[1] <= area["east_max_m"]
            )

        if (
            not inside(start)
            or not inside(goal)
            or any(
                math.dist(point, centre) < radius - 1e-9
                for point in (start, goal)
                for centre, radius in circles
            )
        ):
            return None
        import heapq

        vertices = [start, goal, *self._detour_vertices]
        edges: dict[int, list[tuple[int, float]]] = {0: [], 1: []}
        visibility = self._detour_visibility
        distances_matrix = self._detour_distances
        endpoint_visibility: dict[int, np.ndarray] = {}
        for endpoint_index, endpoint in ((0, start), (1, goal)):
            visible = self._endpoint_visible(endpoint)
            endpoint_visibility[endpoint_index] = visible
            for node_index in np.flatnonzero(visible).tolist():
                node_index += 2
                length = math.dist(endpoint, vertices[node_index])
                edges[endpoint_index].append((node_index, length))
                edges.setdefault(node_index, []).append((endpoint_index, length))
        if self._detour_visible(start, goal):
            length = math.dist(start, goal)
            edges[0].append((1, length))
            edges[1].append((0, length))
        else:
            common = endpoint_visibility[0] & endpoint_visibility[1]
            if np.any(common):
                candidates = np.flatnonzero(common)
                lengths = np.asarray(
                    [
                        math.dist(start, self._detour_vertices[i])
                        + math.dist(self._detour_vertices[i], goal)
                        for i in candidates
                    ]
                )
                best = int(candidates[int(np.argmin(lengths))])
                return [self._detour_vertices[best]]
        distances = [math.inf] * len(vertices)
        previous: list[int | None] = [None] * len(vertices)
        distances[0] = 0.0
        queue = [(0.0, 0)]
        while queue:
            distance, index = heapq.heappop(queue)
            if distance != distances[index]:
                continue
            if index == 1:
                break
            for other, length in edges.get(index, ()):
                candidate = distance + length
                if candidate < distances[other]:
                    distances[other] = candidate
                    previous[other] = index
                    heapq.heappush(queue, (candidate, other))
            if index >= 2 and visibility is not None and distances_matrix is not None:
                for neighbour in np.flatnonzero(visibility[index - 2]).tolist():
                    other = neighbour + 2
                    candidate = distance + float(distances_matrix[index - 2, neighbour])
                    if candidate < distances[other]:
                        distances[other] = candidate
                        previous[other] = index
                        heapq.heappush(queue, (candidate, other))
        if not math.isfinite(distances[1]):
            return None
        path: list[tuple[float, float]] = []
        index: int | None = 1
        while index is not None:
            path.append(vertices[index])
            index = previous[index]
        path.reverse()
        if (time.perf_counter() - started) * 1000.0 > self.config.detour_replan_budget_ms:
            return _DETOUR_TIMEOUT
        return path[1:-1]

    def _detour_visible(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        area = self.operating_area
        return (
            isinstance(area, Mapping)
            and self._point_inside_area(a, area)
            and self._point_inside_area(b, area)
            and all(
                self._segment_circle_distance(a, b, centre) >= radius - 1e-9
                for centre, radius in self._inflated_no_fly_zones
            )
        )

    def _endpoint_visible(self, endpoint: tuple[float, float]) -> np.ndarray:
        """Vectorized visibility from one live endpoint to cached vertices."""
        vertices = np.asarray(self._detour_vertices, dtype=np.float32)
        if not len(vertices):
            return np.zeros(0, dtype=bool)
        if not isinstance(self.operating_area, Mapping) or not self._point_inside_area(
            endpoint, self.operating_area
        ):
            return np.zeros(len(vertices), dtype=bool)
        centres = np.asarray([c for c, _ in self._inflated_no_fly_zones], dtype=np.float64)
        radii = np.asarray([r for _, r in self._inflated_no_fly_zones], dtype=np.float64)
        delta = vertices - np.asarray(endpoint, dtype=np.float64)
        denom = np.einsum("ij,ij->i", delta, delta)
        rel = centres - np.asarray(endpoint, dtype=np.float64)
        t = np.divide(
            np.einsum("zk,ik->iz", rel, delta),
            denom[:, None],
            out=np.zeros((len(vertices), len(centres))),
            where=denom[:, None] > 1e-12,
        )
        closest = np.asarray(endpoint) + np.clip(t, 0.0, 1.0)[:, :, None] * delta[:, None, :]
        distance = np.linalg.norm(closest - centres[None, :, :], axis=2)
        return np.all(distance >= radii[None, :] - 1e-9, axis=1)

    @staticmethod
    def _point_inside_area(point: tuple[float, float], area: Mapping[str, float]) -> bool:
        return (
            area["north_min_m"] <= point[0] <= area["north_max_m"]
            and area["east_min_m"] <= point[1] <= area["east_max_m"]
        )

    def _build_visibility_graph(self) -> None:
        """Build the static polygon vertices and their mutual visibility once."""
        started = time.perf_counter()
        if len(self._inflated_no_fly_zones) > MAX_NO_FLY_ZONES:
            self._visibility_graph_build_ms = (time.perf_counter() - started) * 1000.0
            return
        area = self.operating_area
        if not isinstance(area, Mapping):
            return
        area_key = tuple(
            sorted(
                (str(key), float(area[key]))
                for key in ("north_min_m", "north_max_m", "east_min_m", "east_max_m")
                if key in area
            )
        )
        if self._visibility_graph_area_key == area_key:
            return
        self._detour_vertices.clear()
        self._detour_edges.clear()
        # Twenty vertices keeps the circumscribed approximation within the
        # required 16–24 range while leaving headroom for the 1 s admission
        # target on the development machine.
        sides = 20
        for centre, radius in self._inflated_no_fly_zones:
            # A millimetre beyond the circumscribed polygon absorbs floating
            # point error while retaining tangent-edge semantics for the
            # actual inflated circle.
            vertex_radius = radius / math.cos(math.pi / sides) + 1e-3
            for index in range(sides):
                angle = 2 * math.pi * index / sides
                point = (
                    centre[0] + vertex_radius * math.cos(angle),
                    centre[1] + vertex_radius * math.sin(angle),
                )
                if self._point_inside_area(point, area) and all(
                    math.dist(point, c) >= r - 1e-9 for c, r in self._inflated_no_fly_zones
                ):
                    self._detour_vertices.append(point)
        self._detour_edges = {i: [] for i in range(len(self._detour_vertices))}
        vertices = np.asarray(self._detour_vertices, dtype=np.float32)
        self._detour_visibility = np.ones((len(vertices), len(vertices)), dtype=bool)
        self._detour_distances = (
            np.linalg.norm(vertices[:, None, :] - vertices[None, :, :], axis=2).astype(
                np.float32, copy=False
            )
            if len(vertices)
            else np.zeros((0, 0), dtype=np.float32)
        )
        if len(vertices):
            row, col = np.triu_indices(len(vertices), 1)
            start = vertices[row]
            end = vertices[col]
            clear = np.ones(len(row), dtype=bool)
            min_n = np.minimum(start[:, 0], end[:, 0])
            max_n = np.maximum(start[:, 0], end[:, 0])
            min_e = np.minimum(start[:, 1], end[:, 1])
            max_e = np.maximum(start[:, 1], end[:, 1])
            # Broad-phase bounding boxes avoid distance math for remote pairs.
            for centre, radius in self._inflated_no_fly_zones:
                candidate = (
                    (min_n <= centre[0] + radius)
                    & (max_n >= centre[0] - radius)
                    & (min_e <= centre[1] + radius)
                    & (max_e >= centre[1] - radius)
                    & clear
                )
                if not np.any(candidate):
                    continue
                a = start[candidate]
                delta = end[candidate] - a
                rel = np.asarray(centre, dtype=np.float64) - a
                denom = np.einsum("ij,ij->i", delta, delta)
                t = np.divide(
                    np.einsum("ij,ij->i", rel, delta),
                    denom,
                    out=np.zeros(len(a)),
                    where=denom > 1e-12,
                )
                closest = a + np.clip(t, 0.0, 1.0)[:, None] * delta
                offset = closest - centre
                blocked = np.einsum("ij,ij->i", offset, offset) < (radius - 1e-9) ** 2
                clear[np.flatnonzero(candidate)[blocked]] = False
            self._detour_visibility[row, col] = clear
            self._detour_visibility[col, row] = clear
            np.fill_diagonal(self._detour_visibility, False)
            self._detour_edges.clear()
        self._visibility_graph_build_ms = (time.perf_counter() - started) * 1000.0
        self._visibility_graph_area_key = area_key

    def _leg_enters_no_fly(self, start: tuple[float, float], end: Sequence[float]) -> bool:
        """Return whether a complete straight NED leg intersects a no-fly circle."""
        finish = (float(end[0]), float(end[1]))
        return any(
            self._segment_circle_distance(start, finish, centre) < radius - 1e-9
            for centre, radius in self._inflated_no_fly_zones
        )

    def acknowledge_payload_release(self, request_id: str, *, released: bool = True) -> bool:
        """Accept one host receipt for the active request, before its deadline."""
        run = self._run
        if (
            run is None
            or run.step.skill != "deliver"
            or run.phase != "await_ack"
            or run.release_request_id != request_id
            or run.release_ack is not None
            or self._clock() >= run.release_deadline
            or type(released) is not bool
        ):
            return False
        run.release_ack = released
        return True

    def _deliver(self, run, pose, yaw, ground_point, frame_id):
        if (
            self.airframe_adapter is not None
            and self.airframe_adapter.profile.category == "fixed_wing"
        ):
            self._finish("failed", "fixed_wing_delivery_requires_validated_fly_by_window")
            return None
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
                -pose[2],
                self.cruise_altitude_m or 0.0,
                cfg.min_altitude_m,
                run.release_altitude_m,
            )

        release_pose = (*run.goal_ne, -run.release_altitude_m)
        # Check the low endpoint before descent, release, and each wait cycle.
        # The map API checks horizontal clearance at the supplied altitude.
        if run.phase != "climb" and not self._segment_clear(
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
                    "phase": "await_ack",
                    "request_id": run.release_request_id,
                }

        altitude = run.release_altitude_m if run.phase == "descend" else run.goal_altitude_m
        moved = self._move_to(run, pose, yaw, run.goal_ne, altitude, tolerance_m=tolerance / 2.0)
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
        handoff = self._autopilot_return(run, pose)
        if handoff is not None:
            return handoff
        moved = self._move_to(run, pose, yaw, run.goal_ne, run.goal_altitude_m)
        if moved == "land_fallback":
            return self._land(run, pose, yaw, ground_point, frame_id)
        if moved == "no_fly":
            return None
        if moved == "blocked":
            self._finish("failed", "blocked")
            return None
        if moved is None:
            self._finish("succeeded", "arrived")
            return None
        return moved, {}

    def _autopilot_return(self, run, pose):
        """Hand the flight home to the autopilot for an aircraft that cannot hover.

        ``_move_to`` turns and steps until within ``arrive_tolerance_m``; a plane
        turns each step into a reposition plus an autopilot loiter and circles home
        instead (SITL: 30-57 extra steps).  return_home is a terminal step, so when
        the direct leg home is clear of no-fly zones a stop hands over to RTL, which
        flies straight home and runs the landing.  A leg that needs a no-fly detour
        keeps the waypoint path.
        """
        adapter = self.airframe_adapter
        if adapter is None or adapter.is_multirotor or run.goal_ne is None:
            return None
        if self._leg_enters_no_fly((pose[0], pose[1]), run.goal_ne):
            return None
        self._finish("succeeded", "autopilot_return")
        return ActionPrimitive.stop(), {
            "phase": "autopilot_return",
            "home_ne": [float(run.goal_ne[0]), float(run.goal_ne[1])],
            "requested_altitude_m": run.goal_altitude_m,
        }

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

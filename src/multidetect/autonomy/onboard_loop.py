"""Truth-free onboard search, approach and confirm loop (pure logic, no I/O).

FAST supplies per-frame candidate boxes from the class-agnostic detector; the
loop tracks them, asks the SLOW model for an identity verdict on unverified
tracks, and turns the best identity match into bounded primitives. The host
decides proximity: a match is confirmed once its box is large enough to verify
(or stops growing while approaching), never from ground truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from multidetect.autonomy.ground_model import GroundModel
from multidetect.autonomy.obstacle_source import ObstacleSource
from multidetect.sim_teacher.primitives import ActionPrimitive

Box = tuple[float, float, float, float]


def _clear_default(_pose: tuple[float, float, float], _north: float, _east: float) -> bool:
    """Fallback used when a loop is created without a clearance source."""
    return True


def _iou(a: Box, b: Box) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0.0


def _center(box: Box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _min_side(box: Box) -> float:
    return min(box[2] - box[0], box[3] - box[1])


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


@dataclass(frozen=True, slots=True)
class Detection:
    bbox: Box
    score: float = 0.0
    label: str | None = None


@dataclass(frozen=True, slots=True)
class CameraModel:
    """Pinhole camera pitched down from the body x axis (forward-right-down)."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    pitch_down_rad: float = 0.18

    def bearing_deg(self, u: float) -> float:
        return math.degrees(math.atan2(u - self.cx, self.fx))

    def ground_point(
        self, u: float, v: float, *, north: float, east: float, height_m: float, yaw_deg: float,
        ground_model: GroundModel | None = None,
    ) -> tuple[float, float] | None:
        """Intersect a level-flight ray; height_m is local up for a supplied model."""
        x, y = (u - self.cx) / self.fx, (v - self.cy) / self.fy
        pitch = self.pitch_down_rad
        forward = math.cos(pitch) - y * math.sin(pitch)
        down = math.sin(pitch) + y * math.cos(pitch)
        if ground_model is not None:
            yaw = math.radians(yaw_deg)
            return ground_model.intersect_ray(
                (north, east, height_m),
                (forward * math.cos(yaw) - x * math.sin(yaw),
                 forward * math.sin(yaw) + x * math.cos(yaw), down),
            )
        if down <= 1e-6 or height_m <= 0:
            return None
        t = height_m / down
        yaw = math.radians(yaw_deg)
        f, r = forward * t, x * t
        return north + f * math.cos(yaw) - r * math.sin(yaw), east + f * math.sin(
            yaw
        ) + r * math.cos(yaw)

    def rotate_pixel(self, u: float, v: float, yaw_delta_deg: float) -> tuple[float, float]:
        """Where a distant scene point at ``(u, v)`` appears after the vehicle yaws.

        The camera is pitched down, so a yaw moves points vertically as well as
        sideways; a pure horizontal shift misses small boxes after large turns.
        """
        x, y = (u - self.cx) / self.fx, (v - self.cy) / self.fy
        pitch = self.pitch_down_rad
        forward = math.cos(pitch) - y * math.sin(pitch)
        down = math.sin(pitch) + y * math.cos(pitch)
        theta = math.radians(yaw_delta_deg)
        f2 = forward * math.cos(theta) + x * math.sin(theta)
        r2 = -forward * math.sin(theta) + x * math.cos(theta)
        depth = f2 * math.cos(pitch) + down * math.sin(pitch)
        if depth <= 1e-6:
            return u, v
        y2 = -f2 * math.sin(pitch) + down * math.cos(pitch)
        return self.cx + self.fx * r2 / depth, self.cy + self.fy * y2 / depth


@dataclass(slots=True)
class Track:
    track_id: str
    bbox: Box
    first_step: int
    last_step: int
    hits: int = 1
    verdict: str | None = None
    verdict_step: int = -1
    max_min_side: float = 0.0
    confirm_steps: list[int] = field(default_factory=list)
    # Steps whose verdict said "this is the task's object" (approach or confirm).
    match_steps: list[int] = field(default_factory=list)
    # Cumulative identity votes used by patrol/monitor alert policy.  These are
    # deliberately separate from match_steps, which is reset after a rejection
    # for the find/count confirmation state machine.
    identity_positive_matches: int = 0
    identity_reject_matches: int = 0
    identity_verdict_history: list[str] = field(default_factory=list)
    approach_steps: int = 0
    exhausted: bool = False
    # Ground (north, east) of the box bottom centre from the latest view, if known.
    ground: tuple[float, float] | None = None
    verdict_remembered: bool = False


@dataclass(frozen=True, slots=True)
class LoopConfig:
    confirm_min_side_px: float = 20.0
    verdict_ttl_steps: int = 12
    max_slow_per_step: int = 3
    min_box_side_px: float = 6.0
    max_box_area_fraction: float = 0.25
    max_detections: int = 8
    lost_after_steps: int = 4
    approach_bearing_deg: float = 8.0
    max_translate_m: float = 15.0
    min_translate_m: float = 3.0
    overhead_range_m: float = 6.0
    stall_steps: int = 2
    stall_growth: float = 1.1
    home_tolerance_m: float = 3.0
    max_steps: int = 120
    confirm_matches: int = 2
    border_margin_px: float = 2.0
    max_approach_steps: int = 8
    min_verdict_side_px: float = 10.0
    approach_min_altitude_m: float = 8.0
    descent_step_m: float = 4.0
    # Ground association gate: max(min, fraction * range to the candidate).
    ground_gate_min_m: float = 4.0
    ground_gate_range_fraction: float = 0.15
    # Keep the focus this much farther than the lowest visible ground ray.
    keep_in_view_margin: float = 1.25
    # False keeps searching after a confirmation (counting); confirmations within
    # count_dedupe_m of an earlier one are the same object seen again.
    stop_on_confirm: bool = True
    count_dedupe_m: float = 4.0
    # Second look: after this many steps without a confirmation, fly to the far side
    # of candidates that were rejected from one side only and ask again (an object
    # can look different from behind, e.g. the grey back of a stop sign).
    second_look_after_steps: int | None = None
    second_look_max: int = 3
    second_look_standoff_m: float = 10.0
    # Only revisit rejections the slow model was less sure of than this (decision
    # token probability); None revisits every one-sided rejection.
    second_look_max_confidence: float | None = None
    # Search below the cruise altitude (e.g. for small objects), when the map is clear.
    search_altitude_m: float | None = None
    # At the closest view, this many views whose verdict says "this is the task's
    # object" (approach or confirm) confirm it; None requires confirm_target votes.
    closest_view_identity_matches: int | None = 2
    # A confirm verdict for a candidate farther than this counts only as a match
    # ("it is the object, keep closing in"): the host decides proximity.
    confirm_max_range_m: float | None = None
    # Task-local identity memory. Only actual VLM looks train it; final looks
    # remain fresh even when a place has a stable identity history.
    memory_enabled: bool = False
    memory_radius_m: float = 4.0
    memory_min_verdicts: int = 3
    memory_max_range_ratio: float = 0.7

    def __post_init__(self) -> None:
        if not math.isfinite(self.memory_radius_m) or self.memory_radius_m <= 0:
            raise ValueError("memory_radius_m must be finite and positive")
        if self.memory_min_verdicts < 1:
            raise ValueError("memory_min_verdicts must be positive")
        if not math.isfinite(self.memory_max_range_ratio) or not (
            0 < self.memory_max_range_ratio <= 1
        ):
            raise ValueError("memory_max_range_ratio must be in (0, 1]")


@dataclass(slots=True)
class _PlaceVerdicts:
    center: tuple[float, float]
    verdicts: list[str] = field(default_factory=list)
    closest_range_m: float = math.inf


@dataclass(slots=True)
class PlaceVerdictMemory:
    """Online nearest-centroid clusters of real judgments, scoped to one loop.

    A disagreement (including an unknown result) keeps that place ineligible
    for the rest of the task. Reused verdicts never reinforce their own history
    or move the closest judged range.
    """

    places: list[_PlaceVerdicts] = field(default_factory=list)

    @staticmethod
    def _valid(point: tuple[float, float] | None, range_m: float | None) -> bool:
        return (
            point is not None
            and len(point) == 2
            and all(math.isfinite(v) for v in point)
            and range_m is not None
            and math.isfinite(range_m)
            and range_m >= 0
        )

    def _nearest(self, point: tuple[float, float], radius: float) -> _PlaceVerdicts | None:
        place = min(self.places, key=lambda p: math.dist(point, p.center), default=None)
        return place if place is not None and math.dist(point, place.center) <= radius else None

    def lookup(
        self, point: tuple[float, float] | None, range_m: float | None, config: LoopConfig
    ) -> str | None:
        if not config.memory_enabled or not self._valid(point, range_m):
            return None
        place = self._nearest(point, config.memory_radius_m)
        if place is None or len(place.verdicts) < config.memory_min_verdicts:
            return None
        decision = place.verdicts[0]
        if decision not in {"reject_track", "approach_track"} or any(
            verdict != decision for verdict in place.verdicts
        ):
            return None
        if range_m < config.memory_max_range_ratio * place.closest_range_m:
            return None
        return decision

    def record(
        self,
        point: tuple[float, float] | None,
        range_m: float | None,
        decision: str | None,
        config: LoopConfig,
    ) -> None:
        if not config.memory_enabled or not self._valid(point, range_m):
            return
        place = self._nearest(point, config.memory_radius_m)
        if place is None:
            place = _PlaceVerdicts(point)
            self.places.append(place)
        count = len(place.verdicts)
        place.center = tuple(
            (old * count + new) / (count + 1) for old, new in zip(place.center, point, strict=True)
        )
        place.verdicts.append(str(decision))
        place.closest_range_m = min(place.closest_range_m, range_m)


@dataclass(slots=True)
class OnboardLoop:
    """Search -> approach -> confirm -> return state machine.

    ``search_action`` supplies the target-blind search pattern (expanding square
    over a known static map); ``segment_clear`` checks a planned horizontal
    segment against that map. Neither ever sees the target.
    """

    camera: CameraModel
    search_action: Callable[[tuple[float, float, float], float], ActionPrimitive]
    segment_clear: Callable[[tuple[float, float, float], float, float], bool] = _clear_default
    config: LoopConfig = field(default_factory=LoopConfig)
    # Optional typed source.  ``segment_clear`` remains accepted for backwards
    # compatibility; when a source is provided it is authoritative.
    obstacle_source: ObstacleSource | None = None
    state: str = "searching"
    step_index: int = 0
    home: tuple[float, float] | None = None
    cruise_altitude_m: float | None = None
    confirmed: dict[str, Any] | None = None
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    _tracks: dict[str, Track] = field(default_factory=dict)
    _next_id: int = 1
    _focus: str | None = None
    _approach_sizes: list[float] = field(default_factory=list)
    _last_translated: bool = False
    _last_turn_deg: float = 0.0
    _last_yaw_deg: float | None = None
    _at_closest: bool = False
    _last_pose: tuple[float, float, float] | None = None
    # Rejected candidates: ground point and where they were seen from.
    _rejections: list[dict[str, Any]] = field(default_factory=list)
    _revisit: dict[str, Any] | None = None
    _revisits_done: int = 0
    _place_memory: PlaceVerdictMemory = field(default_factory=PlaceVerdictMemory)

    def _segment_clear(
        self, pose_ned: tuple[float, float, float], north: float, east: float
    ) -> bool:
        if self.obstacle_source is not None:
            return self.obstacle_source.segment_clear(pose_ned, (north, east, pose_ned[2]))
        return self.segment_clear(pose_ned, north, east)

    @property
    def tracks(self) -> Mapping[str, Track]:
        return self._tracks

    def filter_detections(self, detections: Sequence[Detection]) -> list[Detection]:
        area_limit = self.config.max_box_area_fraction * self.camera.width * self.camera.height
        kept = [
            d
            for d in detections
            if _min_side(d.bbox) >= self.config.min_box_side_px
            and (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]) <= area_limit
        ]
        kept.sort(key=lambda d: d.score, reverse=True)
        return kept[: self.config.max_detections]

    def update_tracks(
        self,
        detections: Sequence[Detection],
        *,
        pose_ned: tuple[float, float, float] | None = None,
        yaw_deg: float | None = None,
        ground_point: Callable[[float, float], tuple[float, float] | None] | None = None,
    ) -> list[Track]:
        """Associate detections with tracks by ground position and predicted pixels.

        With ``pose_ned`` and ``ground_point`` every box is anchored to the ground
        point under its bottom centre, so association survives both turns and
        translations. The pixel fallback rotates the previous box by the measured
        yaw change (or the last commanded turn when no yaw is given).
        """
        current: list[Track] = []
        free = {
            tid: t
            for tid, t in self._tracks.items()
            if self.step_index - t.last_step <= self.config.lost_after_steps
        }
        if yaw_deg is not None and self._last_yaw_deg is not None:
            turn = _wrap(yaw_deg - self._last_yaw_deg)
        else:
            turn = self._last_turn_deg
        self._last_turn_deg = 0.0
        if yaw_deg is not None:
            self._last_yaw_deg = yaw_deg
        if pose_ned is not None:
            self._last_pose = tuple(pose_ned)
        predicted = {}
        for tid, t in free.items():
            u, v = _center(t.bbox)
            pu, pv = self.camera.rotate_pixel(u, v, turn)
            predicted[tid] = (
                t.bbox[0] + pu - u,
                t.bbox[1] + pv - v,
                t.bbox[2] + pu - u,
                t.bbox[3] + pv - v,
            )
        for detection in self.filter_detections(detections):
            ground = None
            if ground_point is not None and pose_ned is not None:
                ground = ground_point(_center(detection.bbox)[0], detection.bbox[3])
            best, best_score = None, 0.0
            cx, cy = _center(detection.bbox)
            for track in free.values():
                expected = predicted[track.track_id]
                overlap = _iou(expected, detection.bbox)
                tx, ty = _center(expected)
                scale = max(40.0, _min_side(track.bbox) * 4.0)
                near = max(0.0, 1.0 - math.hypot(cx - tx, cy - ty) / scale)
                score = overlap if overlap >= 0.1 else 0.5 * near
                if ground is not None and track.ground is not None:
                    distance = math.hypot(ground[0] - track.ground[0], ground[1] - track.ground[1])
                    reach = math.hypot(ground[0] - pose_ned[0], ground[1] - pose_ned[1])
                    gate = max(
                        self.config.ground_gate_min_m,
                        self.config.ground_gate_range_fraction * reach,
                    )
                    score = max(score, 1.0 - distance / gate)
                if score > best_score:
                    best, best_score = track, score
            if best is None or best_score < 0.2:
                best = Track(f"t{self._next_id}", detection.bbox, self.step_index, self.step_index)
                self._next_id += 1
                self._tracks[best.track_id] = best
            else:
                free.pop(best.track_id)
                best.bbox = detection.bbox
                best.last_step = self.step_index
                best.hits += 1
            if ground is not None:
                best.ground = ground
            best.max_min_side = max(best.max_min_side, _min_side(best.bbox))
            current.append(best)
        return current

    def tracks_needing_verdict(self, current: Sequence[Track]) -> list[Track]:
        stale = [
            t
            for t in current
            # Boxes too small to judge are not worth a slow call.
            if _min_side(t.bbox) >= self.config.min_verdict_side_px
            and not t.exhausted
            and (
                t.verdict is None
                or self.step_index - t.verdict_step > self.config.verdict_ttl_steps
                # Re-verify the approach focus from each new view until confirmed.
                or (
                    t.track_id == self._focus
                    and t.verdict in {"approach", "confirm"}
                    and t.verdict_step < self.step_index
                )
            )
        ]
        stale.sort(key=lambda t: (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]), reverse=True)
        return stale[: self.config.max_slow_per_step]

    def apply_verdict(
        self,
        track_id: str,
        decision: str | None,
        confidence: float | None = None,
        *,
        remembered: bool = False,
    ) -> None:
        """Apply a real or remembered identity decision.

        approach_track means "worth a closer look" (the model uses it for distant
        or uncertain candidates), reject_track resets the confirmation votes.
        Remembered approaches steer and preserve identity/alert events, but add
        no fresh match or confirm evidence and cannot finish find/count.
        """
        track = self._tracks.get(track_id)
        if track is None:
            return
        if remembered and decision not in {"reject_track", "approach_track"}:
            raise ValueError("only reject_track and approach_track may be remembered")
        if not remembered:
            self._place_memory.record(track.ground, self._track_range(track), decision, self.config)
        verdict = {
            "confirm_target": "confirm",
            "approach_track": "approach",
            "reject_track": "no_match",
        }.get(str(decision), "unknown")
        limit = self.config.confirm_max_range_m
        if (
            verdict == "confirm"
            and limit is not None
            and track.ground is not None
            and self._last_pose is not None
            and math.dist(track.ground, self._last_pose[:2]) > limit
        ):
            verdict = "approach"
        track.verdict, track.verdict_step = verdict, self.step_index
        track.verdict_remembered = remembered
        if verdict in {"confirm", "approach"}:
            track.identity_positive_matches += 1
            track.identity_verdict_history.append(verdict)
        elif verdict == "no_match":
            track.identity_reject_matches += 1
            track.identity_verdict_history.append(verdict)
        if (
            not remembered
            and verdict in {"confirm", "approach"}
            and self.step_index not in track.match_steps
        ):
            track.match_steps.append(self.step_index)
        if verdict == "confirm" and self.step_index not in track.confirm_steps:
            track.confirm_steps.append(self.step_index)
        elif verdict == "no_match":
            track.confirm_steps.clear()
            track.match_steps.clear()
            if not remembered:
                self._remember_rejection(track, confidence)

    def _track_range(self, track: Track) -> float | None:
        if track.ground is None or self._last_pose is None:
            return None
        return math.dist(track.ground, self._last_pose[:2])

    def remembered_verdict(self, track_id: str) -> dict[str, Any] | None:
        """Return a labeled substitute for a pending slow call, without applying it.

        Focus tracks still get fresh approach verification, so caching cannot
        starve find/count of terminal looks. A planned second look also bypasses
        memory, since it deliberately seeks evidence from another side.
        """
        track = self._tracks.get(track_id)
        if track is None or self._revisit is not None:
            return None
        decision = self._place_memory.lookup(track.ground, self._track_range(track), self.config)
        if decision is None or (decision == "approach_track" and track_id == self._focus):
            return None
        return {
            "track_id": track_id,
            "identity": "match" if decision == "approach_track" else "no_match",
            "decision": decision,
            "remembered": True,
            "source": "place_verdict_memory",
            "reason": "unanimous prior real judgments at this ground place",
            "confidence": None,
            "error": None,
            "latency_s": 0.0,
        }

    def _remember_rejection(self, track: Track, confidence: float | None = None) -> None:
        if track.ground is None or self._last_pose is None:
            return
        seen_from = (self._last_pose[0], self._last_pose[1])
        for item in self._rejections:
            if math.dist(item["ground"], track.ground) <= self.config.count_dedupe_m:
                item["ground"] = track.ground
                item["views"].append(seen_from)
                if confidence is not None:
                    item["confidence"] = max(item.get("confidence") or 0.0, confidence)
                return
        self._rejections.append(
            {
                "ground": track.ground,
                "views": [seen_from],
                "revisited": False,
                "confidence": confidence,
            }
        )

    def _one_sided(self, item: dict[str, Any]) -> bool:
        """Every rejection so far came from within 60 deg of the first view direction."""
        gx, gy = item["ground"]
        first = math.atan2(item["views"][0][1] - gy, item["views"][0][0] - gx)
        return all(
            abs(_wrap(math.degrees(math.atan2(v[1] - gy, v[0] - gx) - first))) <= 60.0
            for v in item["views"]
        )

    def _plan_second_look(self, pose_ned) -> dict[str, Any] | None:
        limit = self.config.second_look_max_confidence
        candidates = [
            item
            for item in self._rejections
            if not item["revisited"]
            and self._one_sided(item)
            and (limit is None or (item["confidence"] is not None and item["confidence"] < limit))
        ]
        candidates.sort(key=lambda item: math.dist(item["ground"], pose_ned[:2]))
        for item in candidates:
            gx, gy = item["ground"]
            vx, vy = item["views"][0]
            away = math.atan2(gy - vy, gx - vx)  # from the first view through the object
            standoff = max(
                self.config.second_look_standoff_m,
                self._keep_range(max(0.0, -pose_ned[2])) + self.config.min_translate_m,
            )
            goal = (gx + standoff * math.cos(away), gy + standoff * math.sin(away))
            if self._segment_clear(pose_ned, goal[0], goal[1]):
                # A blocked route provides no new view; keep the candidate
                # eligible for a later attempt from another search position.
                item["revisited"] = True
                return {
                    "goal": goal,
                    "centre": (gx, gy),
                    "phase": "fly",
                    "steps": 0,
                    "rejection": item,
                }
        return None

    def _second_look_action(self, pose_ned, yaw_deg, info):
        look = self._revisit
        look["steps"] += 1
        info["second_look"] = {"centre": list(look["centre"]), "phase": look["phase"]}
        if look["steps"] > 2 * self.config.max_approach_steps:
            if look["phase"] == "fly":
                look["rejection"]["revisited"] = False
            self._revisit = None
            return None
        if look["phase"] == "fly":
            dn, de = look["goal"][0] - pose_ned[0], look["goal"][1] - pose_ned[1]
            distance = math.hypot(dn, de)
            if distance > self.config.min_translate_m:
                delta = _wrap(math.degrees(math.atan2(de, dn)) - yaw_deg)
                if abs(delta) > self.config.approach_bearing_deg:
                    self._last_turn_deg = delta
                    return ActionPrimitive.turn(delta)
                step = min(self.config.max_translate_m, distance)
                action = self._safe(pose_ned, ActionPrimitive.translate(step, 0.0, 0.0), yaw_deg)
                if action.kind != "translate":
                    look["rejection"]["revisited"] = False
                    self._revisit = None
                return action
            look["phase"] = "face"
        if look["phase"] == "face":
            cn, ce = look["centre"][0] - pose_ned[0], look["centre"][1] - pose_ned[1]
            delta = _wrap(math.degrees(math.atan2(ce, cn)) - yaw_deg)
            if abs(delta) > self.config.approach_bearing_deg:
                self._last_turn_deg = delta
                return ActionPrimitive.turn(delta)
            # Facing the far side: forget old verdicts near the centre so the slow
            # model is asked again from this view.
            for track in self._tracks.values():
                if track.ground is not None and (
                    math.dist(track.ground, look["centre"]) <= self.config.count_dedupe_m
                ):
                    track.verdict, track.confirm_steps, track.exhausted = None, [], False
            look["phase"] = "look"
            return ActionPrimitive.hold(1.0)
        self._revisits_done += 1
        self._revisit = None
        return None

    def _touches_border(self, box: Box) -> bool:
        margin = self.config.border_margin_px
        return (
            box[0] <= margin
            or box[1] <= margin
            or box[2] >= self.camera.width - margin
            or box[3] >= self.camera.height - margin
        )

    def _best_match(self, current: Sequence[Track]) -> Track | None:
        visible = [t for t in current if t.verdict in {"approach", "confirm"} and not t.exhausted]
        if self._focus is not None:
            focus = next((t for t in visible if t.track_id == self._focus), None)
            if focus is not None:
                return focus
        if not visible:
            return None
        return max(
            visible,
            key=lambda t: (
                t.verdict == "confirm",
                (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]),
            ),
        )

    def _safe(self, pose_ned, action: ActionPrimitive, yaw_deg: float) -> ActionPrimitive:
        yaw = math.radians(yaw_deg)
        end_n = pose_ned[0] + action.forward_m * math.cos(yaw) - action.right_m * math.sin(yaw)
        end_e = pose_ned[1] + action.forward_m * math.sin(yaw) + action.right_m * math.cos(yaw)
        return action if self._segment_clear(pose_ned, end_n, end_e) else ActionPrimitive.hold(1.0)

    def _count_confirmation(
        self,
        track: Track,
        record: dict[str, Any],
        pose_ned: tuple[float, float, float],
        yaw_deg: float,
        info: dict[str, Any],
    ) -> tuple[ActionPrimitive, dict[str, Any]]:
        """Counting mode: keep the confirmation (unless already counted) and search on."""
        position = record["position_estimate_ne"]
        duplicate = position is not None and any(
            prior["position_estimate_ne"] is not None
            and math.hypot(
                position[0] - prior["position_estimate_ne"][0],
                position[1] - prior["position_estimate_ne"][1],
            )
            <= self.config.count_dedupe_m
            for prior in self.confirmations
        )
        if duplicate:
            info["duplicate_of_counted"] = track.track_id
        else:
            self.confirmations.append(record)
            info["counted"] = record
        # A counted object is never approached or confirmed again.
        track.exhausted = True
        self.state, self._focus = "searching", None
        self._approach_sizes.clear()
        self._at_closest = False
        action = self._search_step(pose_ned, yaw_deg)
        self._last_turn_deg = action.yaw_delta_deg if action.kind == "turn" else 0.0
        return action, info

    def _search_step(self, pose_ned, yaw_deg: float) -> ActionPrimitive:
        """The search pattern, after descending to the search altitude if one is set."""
        target = self.config.search_altitude_m
        altitude = max(0.0, -pose_ned[2])
        if target is not None and altitude > target + 1.0:
            drop = min(altitude - target, 2.0 * self.config.descent_step_m)
            lower = (pose_ned[0], pose_ned[1], pose_ned[2] + drop)
            if self._segment_clear(lower, pose_ned[0], pose_ned[1]):
                return ActionPrimitive.translate(0.0, 0.0, -drop)
        return self.search_action(pose_ned, yaw_deg)

    def _keep_range(self, altitude_m: float) -> float:
        """Closest ground range still inside the image at ``altitude_m`` (with margin)."""
        lowest = self.camera.pitch_down_rad + math.atan2(self.camera.cy, self.camera.fy)
        if lowest >= math.pi / 2:
            return 0.0
        return self.config.keep_in_view_margin * altitude_m / math.tan(lowest)

    def _close_in(
        self,
        pose_ned: tuple[float, float, float],
        yaw_deg: float,
        bearing: float,
        horizontal: float | None,
        info: dict[str, Any],
    ) -> ActionPrimitive:
        """Turn toward ``bearing`` or fly a bounded leg toward it, descending if clear."""
        if abs(bearing) > self.config.approach_bearing_deg:
            self._last_turn_deg = bearing
            return ActionPrimitive.turn(bearing)
        distance = horizontal if horizontal is not None else self.config.max_translate_m
        step = min(self.config.max_translate_m, max(self.config.min_translate_m, 0.6 * distance))
        # Small objects stay small from cruise altitude: descend while approaching
        # when the static map is clear at the lower altitude along the leg.
        altitude = max(0.0, -pose_ned[2])
        descend = min(
            self.config.descent_step_m,
            max(0.0, altitude - self.config.approach_min_altitude_m),
        )
        if horizontal is not None:
            # Never fly so close that the focus drops below the bottom of the image.
            step = min(step, horizontal - self._keep_range(altitude - max(0.0, descend)))
            if step < self.config.min_translate_m:
                lower = (pose_ned[0], pose_ned[1], pose_ned[2] + descend)
                if descend > 0.5 and self._segment_clear(lower, pose_ned[0], pose_ned[1]):
                    # Too close to fly on at this altitude: descend in place instead.
                    self._last_translated = True
                    info["descend_m"] = descend
                    return ActionPrimitive.translate(0.0, 0.0, -descend)
                info["reason"] = "closest_view"
                self._at_closest = True
                return ActionPrimitive.hold(1.0)
        if descend > 0.5:
            lower = (pose_ned[0], pose_ned[1], pose_ned[2] + descend)
            yaw = math.radians(yaw_deg)
            end_n = pose_ned[0] + step * math.cos(yaw)
            end_e = pose_ned[1] + step * math.sin(yaw)
            if self._segment_clear(lower, end_n, end_e):
                action = ActionPrimitive.translate(step, 0.0, -descend)
                self._last_translated = True
                info["descend_m"] = descend
                return action
        action = self._safe(pose_ned, ActionPrimitive.translate(step, 0.0, 0.0), yaw_deg)
        self._last_translated = action.kind == "translate"
        return action

    def next_action(
        self,
        current: Sequence[Track],
        *,
        pose_ned: tuple[float, float, float],
        yaw_deg: float,
        ground_point: Callable[[float, float], tuple[float, float] | None] | None = None,
    ) -> tuple[ActionPrimitive, dict[str, Any]]:
        """Choose the next primitive after tracks and verdicts are up to date.

        ``ground_point(u, v)`` maps a pixel to the configured ground with full vehicle
        attitude and camera mount; without it a level-flight approximation is used.
        """
        if ground_point is None:

            def ground_point(u: float, v: float) -> tuple[float, float] | None:
                return self.camera.ground_point(
                    u,
                    v,
                    north=pose_ned[0],
                    east=pose_ned[1],
                    height_m=max(0.0, -pose_ned[2]),
                    yaw_deg=yaw_deg,
                )

        if self.home is None:
            self.home = (pose_ned[0], pose_ned[1])
            self.cruise_altitude_m = max(0.0, -pose_ned[2])
        info: dict[str, Any] = {"state_before": self.state, "step": self.step_index}
        self.step_index += 1
        if self.state not in {"returning", "done"} and self.step_index > self.config.max_steps:
            self.state = "returning"
            info["reason"] = "step_budget"
        if self.state in {"searching", "approaching"}:
            best = self._best_match(current)
            if best is None:
                if self.state == "approaching" and self._focus is not None:
                    focus = self._tracks[self._focus]
                    lost = self.step_index - focus.last_step
                    # Only a focus that is still wanted and actually out of view; a focus
                    # rejected while visible is dropped. The approach budget still applies.
                    wanted = (
                        focus.verdict in {"approach", "confirm"}
                        and not focus.exhausted
                        and all(t.track_id != focus.track_id for t in current)
                    )
                    if focus.approach_steps >= self.config.max_approach_steps and (
                        not focus.confirm_steps
                    ):
                        focus.exhausted, wanted = True, False
                        info.update(reason="approach_exhausted", dropped=focus.track_id)
                    if wanted and lost <= self.config.lost_after_steps:
                        info["reason"] = "focus_briefly_lost"
                        if focus.ground is None:
                            return ActionPrimitive.hold(1.0), info
                        # Small objects drop out of single frames: keep closing on
                        # the last ground estimate instead of hovering.
                        focus.approach_steps += 1
                        dn, de = focus.ground[0] - pose_ned[0], focus.ground[1] - pose_ned[1]
                        horizontal = math.hypot(dn, de)
                        bearing = _wrap(math.degrees(math.atan2(de, dn)) - yaw_deg)
                        info.update(
                            focus=focus.track_id, bearing_deg=bearing, horizontal_m=horizontal
                        )
                        if horizontal <= self.config.overhead_range_m:
                            return ActionPrimitive.hold(1.0), info
                        return self._close_in(pose_ned, yaw_deg, bearing, horizontal, info), info
                self.state, self._focus = "searching", None
                self._approach_sizes.clear()
                info["state"] = self.state
                after = self.config.second_look_after_steps
                if self._revisit is None and (
                    after is not None
                    and self.step_index > after
                    and self._revisits_done < self.config.second_look_max
                ):
                    self._revisit = self._plan_second_look(pose_ned)
                if self._revisit is not None:
                    look = self._second_look_action(pose_ned, yaw_deg, info)
                    if look is not None:
                        return look, info
                action = self._search_step(pose_ned, yaw_deg)
                self._last_turn_deg = action.yaw_delta_deg if action.kind == "turn" else 0.0
                return action, info
            if best.track_id != self._focus:
                self._approach_sizes.clear()
                self._at_closest = False
            self.state, self._focus = "approaching", best.track_id
            best.approach_steps += 1
            if best.approach_steps > self.config.max_approach_steps and not best.confirm_steps:
                # Never confirmed from any view: stop chasing it and resume the search.
                best.exhausted = True
                self.state, self._focus = "searching", None
                self._approach_sizes.clear()
                info.update(reason="approach_exhausted", dropped=best.track_id)
                action = self._search_step(pose_ned, yaw_deg)
                self._last_turn_deg = action.yaw_delta_deg if action.kind == "turn" else 0.0
                return action, info
            u, v = _center(best.bbox)
            bearing = self.camera.bearing_deg(u)
            # The bottom centre of the box is where the object meets the ground.
            ground = ground_point(u, best.bbox[3])
            horizontal = (
                math.hypot(ground[0] - pose_ned[0], ground[1] - pose_ned[1]) if ground else None
            )
            size = _min_side(best.bbox)
            if self._last_translated or not self._approach_sizes:
                # Growth is only meaningful after moving closer, not after a turn.
                self._approach_sizes.append(size)
            self._last_translated = False
            stalled = (
                len(self._approach_sizes) > self.config.stall_steps
                and size
                < self.config.stall_growth * self._approach_sizes[-1 - self.config.stall_steps]
            )
            info.update(
                focus=best.track_id, bearing_deg=bearing, horizontal_m=horizontal, min_side_px=size
            )
            # Two confirm_target views (a clipped box is only part of an object), or one
            # confirm view once the approach cannot improve the view any further.
            votes = len(best.confirm_steps)
            unclipped = not self._touches_border(best.bbox)
            # Near: overhead, or already at the closest view that keeps the focus in frame.
            near = self._at_closest or (
                horizontal is not None and horizontal <= self.config.overhead_range_m
            )
            info["confirm_views"] = votes
            # At the closest view the host cannot improve the picture: identity is all
            # that is left to judge, so "approach" answers count as matching views too.
            matches = len(best.match_steps)
            info["match_views"] = matches
            if unclipped and not best.verdict_remembered and (
                votes >= self.config.confirm_matches
                or (votes >= 1 and (stalled or near))
                or (
                    near
                    and self.config.closest_view_identity_matches is not None
                    and matches >= self.config.closest_view_identity_matches
                )
            ):
                record = {
                    "track_id": best.track_id,
                    "bbox": list(best.bbox),
                    "position_estimate_ne": list(ground) if ground else None,
                    "min_side_px": size,
                    "rule": "two_views"
                    if votes >= self.config.confirm_matches
                    else "identity_at_closest_view"
                    if votes == 0
                    else (
                        "stalled"
                        if stalled
                        else ("closest_view" if self._at_closest else "overhead")
                    ),
                    "confirm_views": votes,
                    "step": self.step_index - 1,
                }
                if not self.config.stop_on_confirm:
                    return self._count_confirmation(best, record, pose_ned, yaw_deg, info)
                self.state = "returning"
                self.confirmed = record
                info["confirmed"] = self.confirmed
                return ActionPrimitive.hold(2.0), info
            return self._close_in(pose_ned, yaw_deg, bearing, horizontal, info), info
        if self.state == "returning":
            altitude = max(0.0, -pose_ned[2])
            if self.cruise_altitude_m is not None and altitude < self.cruise_altitude_m - 1.0:
                # Climb back to the map-safe cruise altitude before flying home.
                return ActionPrimitive.translate(0.0, 0.0, self.cruise_altitude_m - altitude), info
            dn, de = self.home[0] - pose_ned[0], self.home[1] - pose_ned[1]
            distance = math.hypot(dn, de)
            if distance <= self.config.home_tolerance_m:
                self.state = "done"
                return ActionPrimitive.stop(), info
            desired = math.degrees(math.atan2(de, dn))
            delta = _wrap(desired - yaw_deg)
            if abs(delta) > 10.0:
                return ActionPrimitive.turn(delta), info
            step = min(self.config.max_translate_m, distance)
            return self._safe(pose_ned, ActionPrimitive.translate(step, 0.0, 0.0), yaw_deg), info
        return ActionPrimitive.stop(), info


__all__ = [
    "CameraModel",
    "Detection",
    "LoopConfig",
    "OnboardLoop",
    "PlaceVerdictMemory",
    "Track",
]

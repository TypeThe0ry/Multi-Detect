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

from multidetect.sim_teacher.primitives import ActionPrimitive

Box = tuple[float, float, float, float]


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
        self, u: float, v: float, *, north: float, east: float, height_m: float, yaw_deg: float
    ) -> tuple[float, float] | None:
        """Intersect the pixel ray with flat ground ``height_m`` below the camera."""
        x, y = (u - self.cx) / self.fx, (v - self.cy) / self.fy
        pitch = self.pitch_down_rad
        forward = math.cos(pitch) - y * math.sin(pitch)
        down = math.sin(pitch) + y * math.cos(pitch)
        if down <= 1e-6 or height_m <= 0:
            return None
        t = height_m / down
        yaw = math.radians(yaw_deg)
        f, r = forward * t, x * t
        return north + f * math.cos(yaw) - r * math.sin(yaw), east + f * math.sin(
            yaw
        ) + r * math.cos(yaw)


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
    approach_steps: int = 0
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class LoopConfig:
    confirm_min_side_px: float = 20.0
    verdict_ttl_steps: int = 12
    max_slow_per_step: int = 3
    min_box_side_px: float = 6.0
    max_box_area_fraction: float = 0.25
    max_detections: int = 8
    lost_after_steps: int = 3
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


@dataclass(slots=True)
class OnboardLoop:
    """Search -> approach -> confirm -> return state machine.

    ``search_action`` supplies the target-blind search pattern (expanding square
    over a known static map); ``segment_clear`` checks a planned horizontal
    segment against that map. Neither ever sees the target.
    """

    camera: CameraModel
    search_action: Callable[[tuple[float, float, float], float], ActionPrimitive]
    segment_clear: Callable[[tuple[float, float, float], float, float], bool]
    config: LoopConfig = field(default_factory=LoopConfig)
    state: str = "searching"
    step_index: int = 0
    home: tuple[float, float] | None = None
    confirmed: dict[str, Any] | None = None
    _tracks: dict[str, Track] = field(default_factory=dict)
    _next_id: int = 1
    _focus: str | None = None
    _approach_sizes: list[float] = field(default_factory=list)
    _last_translated: bool = False
    _last_turn_deg: float = 0.0

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

    def update_tracks(self, detections: Sequence[Detection]) -> list[Track]:
        """Associate detections with tracks (greedy IoU, then centre distance)."""
        current: list[Track] = []
        free = {
            tid: t
            for tid, t in self._tracks.items()
            if self.step_index - t.last_step <= self.config.lost_after_steps
        }
        # Ego-motion: a yaw of +theta moves scene points left by fx * tan(theta).
        shift = -self.camera.fx * math.tan(math.radians(self._last_turn_deg))
        self._last_turn_deg = 0.0
        predicted = {
            tid: (t.bbox[0] + shift, t.bbox[1], t.bbox[2] + shift, t.bbox[3])
            for tid, t in free.items()
        }
        for detection in self.filter_detections(detections):
            best, best_score = None, 0.0
            cx, cy = _center(detection.bbox)
            for track in free.values():
                expected = predicted[track.track_id]
                overlap = _iou(expected, detection.bbox)
                tx, ty = _center(expected)
                scale = max(40.0, _min_side(track.bbox) * 4.0)
                near = max(0.0, 1.0 - math.hypot(cx - tx, cy - ty) / scale)
                score = overlap if overlap >= 0.1 else 0.5 * near
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

    def apply_verdict(self, track_id: str, decision: str | None) -> None:
        """Record a slow decision; only confirm_target counts toward confirmation.

        approach_track means "worth a closer look" (the model uses it for distant
        or uncertain candidates), reject_track resets the confirmation votes.
        """
        track = self._tracks.get(track_id)
        if track is None:
            return
        verdict = {
            "confirm_target": "confirm",
            "approach_track": "approach",
            "reject_track": "no_match",
        }.get(str(decision), "unknown")
        track.verdict, track.verdict_step = verdict, self.step_index
        if verdict == "confirm" and self.step_index not in track.confirm_steps:
            track.confirm_steps.append(self.step_index)
        elif verdict == "no_match":
            track.confirm_steps.clear()

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
        return action if self.segment_clear(pose_ned, end_n, end_e) else ActionPrimitive.hold(1.0)

    def next_action(
        self,
        current: Sequence[Track],
        *,
        pose_ned: tuple[float, float, float],
        yaw_deg: float,
        ground_point: Callable[[float, float], tuple[float, float] | None] | None = None,
    ) -> tuple[ActionPrimitive, dict[str, Any]]:
        """Choose the next primitive after tracks and verdicts are up to date.

        ``ground_point(u, v)`` maps a pixel to flat ground with the full vehicle
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
        info: dict[str, Any] = {"state_before": self.state, "step": self.step_index}
        self.step_index += 1
        if self.state not in {"returning", "done"} and self.step_index > self.config.max_steps:
            self.state = "returning"
            info["reason"] = "step_budget"
        if self.state in {"searching", "approaching"}:
            best = self._best_match(current)
            if best is None:
                if self.state == "approaching" and self._focus is not None:
                    lost = self.step_index - self._tracks[self._focus].last_step
                    if lost <= self.config.lost_after_steps:
                        info["reason"] = "focus_briefly_lost"
                        return ActionPrimitive.hold(1.0), info
                self.state, self._focus = "searching", None
                self._approach_sizes.clear()
                info["state"] = self.state
                action = self.search_action(pose_ned, yaw_deg)
                self._last_turn_deg = action.yaw_delta_deg if action.kind == "turn" else 0.0
                return action, info
            if best.track_id != self._focus:
                self._approach_sizes.clear()
            self.state, self._focus = "approaching", best.track_id
            best.approach_steps += 1
            if best.approach_steps > self.config.max_approach_steps and not best.confirm_steps:
                # Never confirmed from any view: stop chasing it and resume the search.
                best.exhausted = True
                self.state, self._focus = "searching", None
                self._approach_sizes.clear()
                info.update(reason="approach_exhausted", dropped=best.track_id)
                action = self.search_action(pose_ned, yaw_deg)
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
            near = horizontal is not None and horizontal <= self.config.overhead_range_m
            info["confirm_views"] = votes
            if unclipped and (
                votes >= self.config.confirm_matches or (votes >= 1 and (stalled or near))
            ):
                self.state = "returning"
                self.confirmed = {
                    "track_id": best.track_id,
                    "bbox": list(best.bbox),
                    "position_estimate_ne": list(ground) if ground else None,
                    "min_side_px": size,
                    "rule": "two_views"
                    if votes >= self.config.confirm_matches
                    else ("stalled" if stalled else "overhead"),
                    "confirm_views": votes,
                    "step": self.step_index - 1,
                }
                info["confirmed"] = self.confirmed
                return ActionPrimitive.hold(2.0), info
            if abs(bearing) > self.config.approach_bearing_deg:
                self._last_turn_deg = bearing
                return ActionPrimitive.turn(bearing), info
            distance = horizontal if horizontal is not None else self.config.max_translate_m
            step = min(
                self.config.max_translate_m, max(self.config.min_translate_m, 0.6 * distance)
            )
            action = self._safe(pose_ned, ActionPrimitive.translate(step, 0.0, 0.0), yaw_deg)
            self._last_translated = action.kind == "translate"
            return action, info
        if self.state == "returning":
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


__all__ = ["CameraModel", "Detection", "LoopConfig", "OnboardLoop", "Track"]

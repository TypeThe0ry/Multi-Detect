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
    approach_steps: int = 0
    exhausted: bool = False
    # Ground (north, east) of the box bottom centre from the latest view, if known.
    ground: tuple[float, float] | None = None


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
    cruise_altitude_m: float | None = None
    confirmed: dict[str, Any] | None = None
    _tracks: dict[str, Track] = field(default_factory=dict)
    _next_id: int = 1
    _focus: str | None = None
    _approach_sizes: list[float] = field(default_factory=list)
    _last_translated: bool = False
    _last_turn_deg: float = 0.0
    _last_yaw_deg: float | None = None

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
        if descend > 0.5:
            lower = (pose_ned[0], pose_ned[1], pose_ned[2] + descend)
            yaw = math.radians(yaw_deg)
            end_n = pose_ned[0] + step * math.cos(yaw)
            end_e = pose_ned[1] + step * math.sin(yaw)
            if self.segment_clear(lower, end_n, end_e):
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
                    if lost <= self.config.lost_after_steps:
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


__all__ = ["CameraModel", "Detection", "LoopConfig", "OnboardLoop", "Track"]

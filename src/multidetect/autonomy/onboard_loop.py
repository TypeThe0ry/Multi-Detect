"""Truth-free onboard search, approach and confirm loop (pure logic, no I/O).

FAST supplies per-frame candidate boxes from the class-agnostic detector; the
loop tracks them, asks the SLOW model for an identity verdict on unverified
tracks, and turns the best identity match into bounded primitives. The host
decides proximity: a match is confirmed once its box is large enough to verify
(or stops growing while approaching), never from ground truth.
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from multidetect.autonomy.ground_model import GroundModel
from multidetect.autonomy.obstacle_source import ObstacleSource
from multidetect.autonomy.step_triangulation import (
    PositionEstimate,
    ViewObservation,
    fuse_position_estimates,
    make_ground_estimate,
    triangulate_views,
)
from multidetect.sim_teacher.primitives import ActionPrimitive

Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class AppearanceSignature:
    """Small onboard identity cue; size is normalized by observed ground range."""

    size_range: float
    aspect_ratio: float
    footprint_m: float | None = None
    color_histogram: tuple[float, ...] | None = None
    short_side_px: float | None = None


def appearance_from_box(
    box: Box,
    range_m: float | None,
    *,
    focal_px: float | None = None,
    frame_rgb: Any = None,
) -> AppearanceSignature | None:
    """Describe geometry and, when available, 64 RGB crop samples in HSV space.

    Frames are optional so offline logs and pure logic callers need no imaging
    dependency. A missing/invalid range supplies no reliable re-identification.
    """
    width, height = box[2] - box[0], box[3] - box[1]
    if (
        range_m is None
        or not math.isfinite(range_m)
        or range_m <= 0
        or not all(math.isfinite(v) for v in box)
        or min(width, height) <= 0
    ):
        return None
    histogram = None
    if frame_rgb is not None:
        image_height, image_width = frame_rgb.shape[:2]
        left, top = max(0, int(box[0])), max(0, int(box[1]))
        right, bottom = min(image_width, math.ceil(box[2])), min(image_height, math.ceil(box[3]))
        if right > left and bottom > top:
            bins = [0.0] * 72
            for row in range(8):
                y = min(bottom - 1, top + (2 * row + 1) * (bottom - top) // 16)
                for col in range(8):
                    x = min(right - 1, left + (2 * col + 1) * (right - left) // 16)
                    r, g, b = (float(v) / 255.0 for v in frame_rgb[y, x, :3])
                    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
                    index = min(7, int(hue * 8)) * 9 + min(2, int(saturation * 3)) * 3
                    bins[index + min(2, int(value * 3))] += 1.0 / 64
            histogram = tuple(bins)
    return AppearanceSignature(
        min(width, height) * range_m,
        width / height,
        max(width, height) * range_m / focal_px if focal_px and focal_px > 0 else None,
        histogram,
        min(width, height),
    )


def appearances_consistent(
    first: AppearanceSignature | None,
    second: AppearanceSignature | None,
    config: LoopConfig,
) -> bool:
    """Return a tolerant appearance score rather than an all-fields hard gate.

    Projection/range error makes apparent size noisy, and a rotated object can
    exchange its image width and height.  Colour is useful for sufficiently
    large crops, but remains a tie-breaker when crops are too small to trust.
    """
    if first is None or second is None:
        return False

    def ratio_score(a: float, b: float, tolerance: float) -> float:
        if min(a, b) <= 0:
            return 0.0
        ratio = max(a, b) / min(a, b)
        return max(0.0, 1.0 - math.log(ratio) / math.log(tolerance))

    # Ground range and partial occlusion make the short side substantially
    # noisier than the pixel box itself; keep this wider than the old 1.6x
    # hard gate while retaining the geometry contribution to the score.
    size = ratio_score(first.size_range, second.size_range, max(config.appearance_size_ratio, 3.2))
    first_aspect = max(first.aspect_ratio, 1.0 / first.aspect_ratio)
    second_aspect = max(second.aspect_ratio, 1.0 / second.aspect_ratio)
    aspect = ratio_score(first_aspect, second_aspect, config.appearance_aspect_ratio)
    score = 0.6 * size + 0.4 * aspect
    if (
        first.color_histogram is not None
        and second.color_histogram is not None
        and (first.short_side_px or 0.0) >= 24.0
        and (second.short_side_px or 0.0) >= 24.0
    ):
        distance = (
            sum(
                abs(a - b)
                for a, b in zip(first.color_histogram, second.color_histogram, strict=True)
            )
            / 2
        )
        colour = max(0.0, 1.0 - distance / max(config.appearance_color_distance, 1e-9))
        score = 0.5 * score + 0.5 * colour
    # A completely incompatible large-crop colour signature must lose the
    # tie even when geometry is otherwise perfect; geometry remains soft.
    return score > 0.5


def _clear_default(_pose: tuple[float, float, float], _north: float, _east: float) -> bool:
    """Fail closed when no obstacle source or callback was configured."""
    return False


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

    def ray_ned(self, u: float, v: float, yaw_deg: float) -> tuple[float, float, float]:
        """Unit ray for the same level-flight model as ``ground_point``."""
        x, y = (u - self.cx) / self.fx, (v - self.cy) / self.fy
        pitch, yaw = self.pitch_down_rad, math.radians(yaw_deg)
        forward = math.cos(pitch) - y * math.sin(pitch)
        down = math.sin(pitch) + y * math.cos(pitch)
        raw = (
            forward * math.cos(yaw) - x * math.sin(yaw),
            forward * math.sin(yaw) + x * math.cos(yaw),
            down,
        )
        norm = math.sqrt(sum(value * value for value in raw))
        return tuple(value / norm for value in raw)

    def ground_point(
        self,
        u: float,
        v: float,
        *,
        north: float,
        east: float,
        height_m: float,
        yaw_deg: float,
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
                (
                    forward * math.cos(yaw) - x * math.sin(yaw),
                    forward * math.sin(yaw) + x * math.cos(yaw),
                    down,
                ),
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
    appearance: AppearanceSignature | None = None
    # Track ids observed as separate detections in one frame are permanently
    # distinct, even when later ground projections are close.
    co_visible_with: set[str] = field(default_factory=set)
    # True once bounded co-visibility history has been truncated.  The
    # remaining history is incomplete, so this track must not be ground-reid.
    covis_truncated: bool = False
    needs_fresh_verdict: bool = False
    # Number of small-box rejection rechecks already scheduled for this track.
    small_reject_rechecks: int = 0
    # Independent of the bottom-pixel ground anchor used by tracking and memory.
    position_views: list[ViewObservation] = field(default_factory=list)
    position_estimate: PositionEstimate | None = None


@dataclass(frozen=True, slots=True)
class LoopConfig:
    confirm_min_side_px: float = 20.0
    # Disabled by default.  When set, a reject below this pixel size is treated
    # as provisional and the host keeps approaching until the normal confirmation
    # size gate is reached before asking the slow model once more.
    small_reject_recheck_px: float | None = None
    small_reject_recheck_max: int = 1
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
    # None enables 60-step ground re-identification for continuing (count,
    # patrol, monitor) loops only. Zero disables it, including for those loops.
    ground_reid_max_steps: int | None = None
    ground_reid_radius_m: float = 1.5
    ground_reid_footprint_factor: float = 1.5
    appearance_size_ratio: float = 1.6
    appearance_aspect_ratio: float = 1.6
    appearance_color_distance: float = 0.45
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
    memory_radius_m: float = 1.5
    memory_footprint_factor: float = 1.5
    memory_min_verdicts: int = 3
    memory_max_range_ratio: float = 0.7
    max_blocked_legs: int = 2
    step_triangulation: bool = False
    step_triangulation_max_views: int = 8
    ground_height_sigma_m: float = 1.0
    step_triangulation_sigma_scale: float = 2.5
    # Closed-loop tracking uses the ground hit as its association anchor.
    # Triangulation may move it horizontally by at most
    # max(ground_gate_m, ground_gate_sigma * horizontal ground sigma), where the
    # horizontal sigma is ground_height_sigma_m / tan(bottom-ray depression):
    # an uncertain ground or a shallow ray lets geometry correct the hit, a
    # trusted ground keeps it pinned.  ground_gate_sigma=0 is the fixed gate.
    step_triangulation_ground_gate_m: float = 0.1
    step_triangulation_ground_gate_sigma: float = 2.5
    # Required parallax grows with range because a fixed pixel/ray error turns
    # into a larger along-ray position error at distance.
    step_triangulation_parallax_deg_per_m: float = 0.3
    step_triangulation_min_ray_angle_deg: float = 10.0
    # Near monocular depth is an opt-in supplement to the geometric estimate.
    near_depth: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.step_triangulation, bool):
            raise ValueError("step_triangulation must be a boolean")
        if not isinstance(self.near_depth, bool):
            raise ValueError("near_depth must be a boolean")
        if (
            isinstance(self.step_triangulation_max_views, bool)
            or not isinstance(self.step_triangulation_max_views, int)
            or not 2 <= self.step_triangulation_max_views <= 64
        ):
            raise ValueError("step_triangulation_max_views must be an integer in [2, 64]")
        for name, minimum in (
            ("ground_height_sigma_m", 0.0),
            ("step_triangulation_sigma_scale", 1.0),
            ("step_triangulation_ground_gate_m", 0.0),
            ("step_triangulation_parallax_deg_per_m", 0.0),
            ("step_triangulation_min_ray_angle_deg", 0.0),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                or value < minimum
            ):
                raise ValueError(f"{name} must be finite and positive, and >= {minimum}")
        gate_sigma = self.step_triangulation_ground_gate_sigma
        if (
            isinstance(gate_sigma, bool)
            or not isinstance(gate_sigma, (int, float))
            or not math.isfinite(gate_sigma)
            or gate_sigma < 0
        ):
            raise ValueError("step_triangulation_ground_gate_sigma must be finite and >= 0")
        if self.small_reject_recheck_px is not None and (
            not math.isfinite(self.small_reject_recheck_px) or self.small_reject_recheck_px <= 0
        ):
            raise ValueError("small_reject_recheck_px must be finite and positive or None")
        if (
            isinstance(self.small_reject_recheck_max, bool)
            or not isinstance(self.small_reject_recheck_max, int)
            or self.small_reject_recheck_max < 0
        ):
            raise ValueError("small_reject_recheck_max must be a non-negative integer")
        if self.ground_reid_max_steps is not None and (
            isinstance(self.ground_reid_max_steps, bool)
            or not isinstance(self.ground_reid_max_steps, int)
            or self.ground_reid_max_steps < 0
        ):
            raise ValueError("ground_reid_max_steps must be a non-negative integer or None")
        if not math.isfinite(self.ground_reid_radius_m) or self.ground_reid_radius_m <= 0:
            raise ValueError("ground_reid_radius_m must be finite and positive")
        if (
            not math.isfinite(self.ground_reid_footprint_factor)
            or self.ground_reid_footprint_factor < 0
        ):
            raise ValueError("ground_reid_footprint_factor must be finite and non-negative")
        if not math.isfinite(self.memory_radius_m) or self.memory_radius_m <= 0:
            raise ValueError("memory_radius_m must be finite and positive")
        if not math.isfinite(self.memory_footprint_factor) or self.memory_footprint_factor < 0:
            raise ValueError("memory_footprint_factor must be finite and non-negative")
        for name in ("appearance_size_ratio", "appearance_aspect_ratio"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be finite and at least one")
        if not math.isfinite(self.appearance_color_distance) or not (
            0 <= self.appearance_color_distance <= 1
        ):
            raise ValueError("appearance_color_distance must be in [0, 1]")
        if self.memory_min_verdicts < 1:
            raise ValueError("memory_min_verdicts must be positive")
        if not math.isfinite(self.memory_max_range_ratio) or not (
            0 < self.memory_max_range_ratio <= 1
        ):
            raise ValueError("memory_max_range_ratio must be in (0, 1]")
        if self.max_blocked_legs < 1:
            raise ValueError("max_blocked_legs must be positive")


@dataclass(slots=True)
class _PlaceVerdicts:
    center: tuple[float, float]
    appearance: AppearanceSignature
    verdicts: list[str] = field(default_factory=list)
    closest_range_m: float = math.inf
    track_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class PlaceVerdictMemory:
    """Object-sized regions anchored at their first real look, scoped to one loop.

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

    def _nearest(
        self, point: tuple[float, float], appearance: AppearanceSignature | None, config: LoopConfig
    ) -> _PlaceVerdicts | None:
        def radius_for(place: _PlaceVerdicts) -> float:
            if config.memory_radius_m < 1.5 or place.appearance is None:
                return config.memory_radius_m
            return max(
                config.memory_radius_m,
                config.memory_footprint_factor * (place.appearance.footprint_m or 0.0),
            )

        candidates = [
            p
            for p in self.places
            if math.dist(point, p.center) <= radius_for(p)
            and (
                p.appearance is None
                or appearance is None
                or appearances_consistent(p.appearance, appearance, config)
            )
        ]
        return min(candidates, key=lambda p: math.dist(point, p.center), default=None)

    def lookup(
        self,
        point: tuple[float, float] | None,
        range_m: float | None,
        config: LoopConfig,
        appearance: AppearanceSignature | None = None,
        track_id: str | None = None,
        co_visible_with: set[str] | None = None,
    ) -> str | None:
        if not config.memory_enabled or not self._valid(point, range_m):
            return None
        place = self._nearest(point, appearance, config)
        if (
            place is not None
            and track_id is not None
            and co_visible_with
            and place.track_ids.intersection(co_visible_with)
        ):
            place = None
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
        appearance: AppearanceSignature | None = None,
        track_id: str | None = None,
        co_visible_with: set[str] | None = None,
    ) -> None:
        if not config.memory_enabled or not self._valid(point, range_m):
            return
        place = self._nearest(point, appearance, config)
        if place is None:
            place = _PlaceVerdicts(point, appearance)
            self.places.append(place)
        if (
            track_id is not None
            and co_visible_with
            and place.track_ids.intersection(co_visible_with)
        ):
            # A place trained by a co-visible object must never absorb this
            # object's verdict history merely because projections overlap.
            place = _PlaceVerdicts(point, appearance)
            self.places.append(place)
        place.verdicts.append(str(decision))
        place.closest_range_m = min(place.closest_range_m, range_m)
        if track_id is not None:
            place.track_ids.add(track_id)


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
    _co_visibility_pairs: dict[tuple[str, str], int] = field(default_factory=dict)
    co_visibility_max_pairs: int = 4096
    _blocked_search_legs: int = 0

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
        frame_rgb: Any = None,
        camera_ray: Callable[
            [float, float], tuple[tuple[float, float, float], tuple[float, float, float]] | None
        ]
        | None = None,
        timestamp_s: float | None = None,
    ) -> list[Track]:
        """Associate detections with tracks by ground position and predicted pixels.

        With ``pose_ned`` and ``ground_point`` every box is anchored to the ground
        point under its bottom centre, so association survives both turns and
        translations. The pixel fallback rotates the previous box by the measured
        yaw change (or the last commanded turn when no yaw is given).

        Continuing missions may revive an unmatched, recently lost track by a
        reliable ground anchor. Active matches have priority; stale image boxes
        never supply a re-identification score. The existing track retains its
        verdict age and evidence, so revival does not manufacture a fresh look.
        """
        current: list[Track] = []
        free = {
            tid: t
            for tid, t in self._tracks.items()
            if self.step_index - t.last_step <= self.config.lost_after_steps and not t.exhausted
        }
        # Count-confirmed tracks are never candidates for ordinary active
        # association: a nearby new object must receive its own track/verdict.
        # Preserve only exact pixel continuation for the same or immediately
        # following frame, which prevents a one-frame bookkeeping gap from
        # manufacturing a duplicate counted object.
        exhausted_continuations = {
            tid: t
            for tid, t in self._tracks.items()
            if t.exhausted and self.step_index - t.last_step in (0, 1)
        }
        continuation_used: set[str] = set()
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
        pending: list[
            tuple[Detection, tuple[float, float] | None, Track | None, AppearanceSignature | None]
        ] = []
        for detection in self.filter_detections(detections):
            ground = None
            if ground_point is not None and pose_ned is not None:
                ground = ground_point(_center(detection.bbox)[0], detection.bbox[3])
            range_m = (
                math.dist(ground, pose_ned[:2])
                if ground is not None and pose_ned is not None
                else None
            )
            appearance = appearance_from_box(
                detection.bbox,
                range_m,
                focal_px=(self.camera.fx + self.camera.fy) / 2,
                frame_rgb=frame_rgb,
            )
            exact = next(
                (
                    track
                    for track in exhausted_continuations.values()
                    if track.track_id not in continuation_used
                    and _iou(track.bbox, detection.bbox) >= 0.99
                ),
                None,
            )
            if exact is not None:
                continuation_used.add(exact.track_id)
                pending.append((detection, ground, exact, appearance))
                continue
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
                best = None
            else:
                free.pop(best.track_id)
            pending.append((detection, ground, best, appearance))

        reid_steps = self.config.ground_reid_max_steps
        if reid_steps is None:
            reid_steps = 0 if self.config.stop_on_confirm else 60

        def reliable(box: Box, point: tuple[float, float] | None) -> bool:
            margin = self.config.border_margin_px
            return (
                point is not None
                and all(math.isfinite(value) for value in point)
                and box[0] > margin
                and box[1] > margin
                and box[2] < self.camera.width - margin
                and box[3] < self.camera.height - margin
            )

        # _tracks already retains lost tracks for evidence. Use that task-local
        # graveyard without extending the active/focus timeout or pixel gate.
        lost = (
            [
                track
                for track in self._tracks.values()
                if self.config.lost_after_steps < self.step_index - track.last_step <= reid_steps
                and reliable(track.bbox, track.ground)
                and not track.covis_truncated
            ]
            if reid_steps
            else []
        )

        def revival_radius(track: Track, appearance: AppearanceSignature | None) -> float:
            footprint = (appearance.footprint_m if appearance else None) or (
                track.appearance.footprint_m if track.appearance else None
            )
            return max(
                1.5,
                self.config.ground_reid_radius_m,
                self.config.ground_reid_footprint_factor * (footprint or 0.0),
            )

        pairs = sorted(
            (math.dist(ground, track.ground), index, track.track_id)
            for index, (detection, ground, best, appearance) in enumerate(pending)
            if best is None and reliable(detection.bbox, ground)
            for track in lost
            if appearances_consistent(track.appearance, appearance, self.config)
            if math.dist(ground, track.ground) < revival_radius(track, appearance)
        )
        revived: dict[int, Track] = {}
        used: set[str] = set()
        for _, index, track_id in pairs:
            if index not in revived and track_id not in used:
                revived[index] = self._tracks[track_id]
                used.add(track_id)

        for index, (detection, ground, best, appearance) in enumerate(pending):
            was_revived = best is None and index in revived
            best = best or revived.get(index)
            if best is None:
                best = Track(f"t{self._next_id}", detection.bbox, self.step_index, self.step_index)
                self._next_id += 1
                self._tracks[best.track_id] = best
            else:
                best.bbox = detection.bbox
                best.last_step = self.step_index
                best.hits += 1
                if was_revived:
                    best.needs_fresh_verdict = True
                    best.confirm_steps.clear()
                    best.match_steps.clear()
                    best.approach_steps = 0
                    best.verdict = None
                    best.verdict_step = -1
                    best.verdict_remembered = False
            if ground is not None:
                best.ground = ground
            if self.config.step_triangulation or self.config.near_depth:
                self._update_position(
                    best,
                    ground,
                    pose_ned,
                    yaw_deg,
                    camera_ray,
                    timestamp_s,
                    allow_level_ray=ground_point is None,
                )
            if appearance is not None:
                best.appearance = appearance
            best.max_min_side = max(best.max_min_side, _min_side(best.bbox))
            current.append(best)
        # Separate detections in one frame are distinct objects forever. Prune
        # stale relations so long monitor missions stay bounded.
        reid_window = self.config.ground_reid_max_steps
        if reid_window is None:
            reid_window = 0 if self.config.stop_on_confirm else 60
        live_ids = {
            t.track_id
            for t in self._tracks.values()
            if self.step_index - t.last_step <= max(self.config.lost_after_steps, reid_window)
        }
        for track in self._tracks.values():
            track.co_visible_with.intersection_update(live_ids)
        for left_index, left in enumerate(current):
            for right in current[left_index + 1 :]:
                if left.track_id != right.track_id:
                    left.co_visible_with.add(right.track_id)
                    right.co_visible_with.add(left.track_id)
                    key = tuple(sorted((left.track_id, right.track_id)))
                    self._co_visibility_pairs[key] = self.step_index
        for key in list(self._co_visibility_pairs):
            if key[0] not in live_ids or key[1] not in live_ids:
                self._co_visibility_pairs.pop(key, None)
        while len(self._co_visibility_pairs) > self.co_visibility_max_pairs:
            oldest = min(self._co_visibility_pairs, key=self._co_visibility_pairs.get)
            self._co_visibility_pairs.pop(oldest, None)
            left = self._tracks.get(oldest[0])
            right = self._tracks.get(oldest[1])
            if left is not None:
                left.covis_truncated = True
                left.co_visible_with.discard(oldest[1])
            if right is not None:
                right.covis_truncated = True
                right.co_visible_with.discard(oldest[0])
        for track in self._tracks.values():
            if len(track.co_visible_with) > 4096:
                removed = track.co_visible_with - set(sorted(track.co_visible_with)[-4096:])
                track.covis_truncated = True
                track.co_visible_with.difference_update(removed)
                for other_id in removed:
                    other = self._tracks.get(other_id)
                    if other is not None:
                        other.covis_truncated = True
        return current

    def _update_position(
        self, track, ground, pose_ned, yaw_deg, camera_ray, timestamp_s, *, allow_level_ray: bool
    ) -> None:
        """Estimate after association; never feed this result back into identity."""
        track.position_estimate = None
        track.position_views[:] = [
            view
            for view in track.position_views
            if self.step_index - self.config.step_triangulation_max_views
            < view.step
            <= self.step_index
        ]
        try:
            if camera_ray is None:
                # A custom ground projector needs its matching calibrated ray callback.
                if not allow_level_ray or pose_ned is None or yaw_deg is None:
                    return

                def camera_ray(u, v):
                    return (tuple(pose_ned), self.camera.ray_ned(u, v, yaw_deg))

                ground = self.camera.ground_point(
                    _center(track.bbox)[0],
                    track.bbox[3],
                    north=pose_ned[0],
                    east=pose_ned[1],
                    height_m=max(0.0, -pose_ned[2]),
                    yaw_deg=yaw_deg,
                )
            stamp = float(self.step_index) if timestamp_s is None else timestamp_s
            if not math.isfinite(stamp) or stamp < 0:
                return
            centre_ray = camera_ray(*_center(track.bbox))
            if centre_ray is None:
                return
            camera, direction = centre_ray
            # Repeated calls in one action step are a single observation.
            track.position_views[:] = [
                view for view in track.position_views if view.step != self.step_index
            ]
            if track.position_views and stamp < track.position_views[-1].timestamp_s:
                track.position_views.clear()
            track.position_views.append(
                ViewObservation(tuple(camera), tuple(direction), self.step_index, stamp)
            )
            track.position_views[:] = track.position_views[
                -self.config.step_triangulation_max_views :
            ]
            tri = (
                triangulate_views(
                    track.position_views,
                    max_views=self.config.step_triangulation_max_views,
                    sigma_scale=self.config.step_triangulation_sigma_scale,
                )
                if self.config.step_triangulation
                else None
            )
            ground_estimate = None
            anchor_gate_m = self.config.step_triangulation_ground_gate_m
            bottom_ray = camera_ray(_center(track.bbox)[0], track.bbox[3])
            if ground is not None and bottom_ray is not None:
                origin, ray = bottom_ray
                horizontal_norm2 = ray[0] ** 2 + ray[1] ** 2
                # NE alone cannot recover height from a vertical ray.
                if horizontal_norm2 > 1e-12:
                    scale = (
                        sum((ground[i] - origin[i]) * ray[i] for i in range(2)) / horizontal_norm2
                    )
                    down = origin[2] + scale * ray[2]
                    ground_estimate = make_ground_estimate(
                        (ground[0], ground[1], down),
                        camera_ned=tuple(origin),
                        direction_ned=tuple(ray),
                        ground_height_sigma_m=self.config.ground_height_sigma_m,
                        timestamp_s=stamp,
                    )
                    if ground_estimate is not None:
                        # Horizontal shift of the hit per sigma of ground height.
                        anchor_gate_m = max(
                            anchor_gate_m,
                            self.config.step_triangulation_ground_gate_sigma
                            * self.config.ground_height_sigma_m
                            * math.sqrt(horizontal_norm2)
                            / ray[2],
                        )
            if tri is not None:
                # Always require distance-aware parallax before trusting the
                # poorly conditioned along-ray component: near-collinear rays
                # produce metre-scale errors whatever the ground model.
                latest_camera = track.position_views[-1].camera_ned
                range_m = math.dist(tri.position_ned, latest_camera)
                required_angle = max(
                    self.config.step_triangulation_min_ray_angle_deg,
                    self.config.step_triangulation_parallax_deg_per_m * range_m,
                )
                if tri.ray_angle_deg < required_angle:
                    tri = None
                    if ground_estimate is not None:
                        ground_estimate = replace(
                            ground_estimate,
                            consistent=False,
                            consistency_note="parallax_below_distance_gate:fallback_ground",
                        )
            track.position_estimate = (
                fuse_position_estimates(
                    ground_estimate, tri, ground_anchor_max_error_m=anchor_gate_m
                )
                if tri is not None
                else ground_estimate
            )
        except (ValueError, TypeError, ArithmeticError, LookupError, AttributeError, RuntimeError):
            # Sensor/calibration faults cannot interrupt the mission state machine.
            track.position_estimate = None

    def target_position(self, track: Track) -> tuple[float, float] | None:
        """Position for reporting/navigation, independent of association anchors."""
        if (
            self.config.step_triangulation or self.config.near_depth
        ) and track.position_estimate is not None:
            return tuple(track.position_estimate.position_ned[:2])
        return track.ground

    def position_metadata(self, track: Track) -> dict[str, Any]:
        if not (self.config.step_triangulation or self.config.near_depth):
            return {}
        estimate = track.position_estimate
        return {
            "position_source": estimate.source if estimate else "ground",
            "position_sigma_m": estimate.sigma_m if estimate else None,
            "position_view_count": max(
                estimate.view_count if estimate else 0, len(track.position_views)
            ),
            "position_baseline_m": estimate.baseline_m if estimate else 0.0,
            "position_ray_angle_deg": estimate.ray_angle_deg if estimate else 0.0,
            "position_consistent": estimate.consistent if estimate else True,
            "position_consistency_note": estimate.consistency_note if estimate else None,
        }

    def tracks_needing_verdict(self, current: Sequence[Track]) -> list[Track]:
        stale = [
            t
            for t in current
            # Boxes too small to judge are not worth a slow call.
            if _min_side(t.bbox) >= self.config.min_verdict_side_px
            and not t.exhausted
            and (
                t.needs_fresh_verdict
                or t.verdict is None
                # A provisional small-box reject is re-asked only at the confirmation size,
                # not on TTL expiry while still too small to judge.
                or (
                    t.verdict != "pending_near"
                    and self.step_index - t.verdict_step > self.config.verdict_ttl_steps
                )
                or (
                    t.verdict == "pending_near"
                    and _min_side(t.bbox) >= self.config.confirm_min_side_px
                )
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
        if track.needs_fresh_verdict and remembered:
            return
        if remembered and decision not in {"reject_track", "approach_track"}:
            raise ValueError("only reject_track and approach_track may be remembered")
        small_recheck = (
            not remembered
            and decision == "reject_track"
            and self.config.small_reject_recheck_px is not None
            and _min_side(track.bbox) < self.config.small_reject_recheck_px
            and track.small_reject_rechecks < self.config.small_reject_recheck_max
        )
        if not remembered and not small_recheck:
            self._place_memory.record(
                track.ground,
                self._track_range(track),
                decision,
                self.config,
                track.appearance,
                track.track_id,
                track.co_visible_with,
            )
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
        # A remembered negative is never allowed to erase positive real evidence.
        if remembered and (
            track.confirm_steps or track.match_steps or track.verdict in {"approach", "confirm"}
        ):
            return
        track.verdict, track.verdict_step = verdict, self.step_index
        track.needs_fresh_verdict = False
        track.verdict_remembered = remembered
        if small_recheck:
            # Do not train place memory or count this as a terminal negative.  The
            # pending state remains an eligible focus so next_action keeps closing
            # in; tracks_needing_verdict asks again once the normal size gate is met.
            track.small_reject_rechecks += 1
            track.verdict = "pending_near"
            track.verdict_remembered = False
            track.identity_verdict_history.append("pending_near")
            return
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
        if (
            track is None
            or self._revisit is not None
            or track.needs_fresh_verdict
            or track.verdict == "pending_near"
        ):
            return None
        if track.confirm_steps or track.match_steps or track.verdict in {"approach", "confirm"}:
            return None
        decision = self._place_memory.lookup(
            track.ground,
            self._track_range(track),
            self.config,
            track.appearance,
            track.track_id,
            track.co_visible_with,
        )
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
        footprint = getattr(track.appearance, "footprint_m", None)

        def compatible(item: dict[str, Any]) -> bool:
            if item.get("track_id") in track.co_visible_with or track.track_id in item.get(
                "co_visible_with", ()
            ):
                return False
            prior = item.get("appearance")
            return (
                prior is None
                or track.appearance is None
                or appearances_consistent(prior, track.appearance, self.config)
            )

        for item in self._rejections:
            radius = (
                max(
                    1.5,
                    self.config.memory_footprint_factor
                    * (item.get("footprint_m") or footprint or 0.0),
                )
                if (item.get("footprint_m") or footprint)
                else self.config.count_dedupe_m
            )
            if compatible(item) and math.dist(item["ground"], track.ground) <= radius:
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
                "track_id": track.track_id,
                "co_visible_with": set(track.co_visible_with),
                "appearance": track.appearance,
                "footprint_m": footprint,
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
                if track.ground is None:
                    continue
                footprint = look["rejection"].get("footprint_m") or getattr(
                    track.appearance, "footprint_m", None
                )
                radius = (
                    max(1.5, self.config.memory_footprint_factor * footprint)
                    if footprint
                    else self.config.count_dedupe_m
                )
                compatible = (
                    look["rejection"].get("track_id") not in track.co_visible_with
                    and track.track_id not in look["rejection"].get("co_visible_with", ())
                    and (
                        look["rejection"].get("appearance") is None
                        or track.appearance is None
                        or appearances_consistent(
                            look["rejection"]["appearance"], track.appearance, self.config
                        )
                    )
                )
                if compatible and math.dist(track.ground, look["centre"]) <= radius:
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
        visible = [
            t
            for t in current
            if t.verdict in {"approach", "confirm", "pending_near"} and not t.exhausted
        ]
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
            and not (
                prior.get("track_id") in track.co_visible_with
                or track.track_id in prior.get("co_visible_with", ())
            )
            and (
                prior.get("appearance") is None
                or track.appearance is None
                or appearances_consistent(prior["appearance"], track.appearance, self.config)
            )
            and math.hypot(
                position[0] - prior["position_estimate_ne"][0],
                position[1] - prior["position_estimate_ne"][1],
            )
            <= (
                max(
                    1.5,
                    self.config.memory_footprint_factor
                    * (prior.get("footprint_m") or getattr(track.appearance, "footprint_m", 0.0)),
                )
                if (prior.get("footprint_m") or getattr(track.appearance, "footprint_m", None))
                else self.config.count_dedupe_m
            )
            for prior in self.confirmations
        )
        if track.needs_fresh_verdict:
            info["count_blocked"] = "fresh_verdict_required"
            self.state, self._focus = "searching", None
            self._approach_sizes.clear()
            return self._search_step(pose_ned, yaw_deg), info
        if duplicate:
            info["duplicate_of_counted"] = track.track_id
        else:
            record["track_id"] = track.track_id
            record["co_visible_with"] = set(track.co_visible_with)
            record["appearance"] = track.appearance
            record["footprint_m"] = getattr(track.appearance, "footprint_m", None)
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
        action = self.search_action(pose_ned, yaw_deg)
        if self.obstacle_source is None:
            return action
        safe = self._safe(pose_ned, action, yaw_deg)
        if action.kind == "translate" and safe.kind != "translate":
            self._blocked_search_legs += 1
            if self._blocked_search_legs >= self.config.max_blocked_legs:
                self.state = "blocked"
        elif action.kind == "translate":
            self._blocked_search_legs = 0
        return safe

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
                        focus.verdict in {"approach", "confirm", "pending_near"}
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
                        focus_position = self.target_position(focus)
                        if focus_position is None:
                            return ActionPrimitive.hold(1.0), info
                        # Small objects drop out of single frames: keep closing on
                        # the last ground estimate instead of hovering.
                        focus.approach_steps += 1
                        dn, de = focus_position[0] - pose_ned[0], focus_position[1] - pose_ned[1]
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
            if self.config.step_triangulation and best.position_estimate is not None:
                ground = self.target_position(best)
                if ground is not None:
                    bearing = _wrap(
                        math.degrees(math.atan2(ground[1] - pose_ned[1], ground[0] - pose_ned[0]))
                        - yaw_deg
                    )
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
            if (
                unclipped
                and not best.needs_fresh_verdict
                and not best.verdict_remembered
                and (
                    votes >= self.config.confirm_matches
                    or (votes >= 1 and (stalled or near))
                    or (
                        near
                        and self.config.closest_view_identity_matches is not None
                        and matches >= self.config.closest_view_identity_matches
                    )
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
                record.update(self.position_metadata(best))
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

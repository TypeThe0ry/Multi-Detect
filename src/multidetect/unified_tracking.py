from __future__ import annotations

import math
import time
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .assignment import rectangular_linear_assignment
from .domain import BoundingBox, Detection

_AssociationCandidate = tuple[int, float, str, int, float | None, bool, bool]

_VEHICLE_LABELS = frozenset(
    {
        "vehicle",
        "car",
        "van",
        "truck",
        "bus",
        "motorcycle",
        "motorbike",
        "motor",
        "bicycle",
        "tricycle",
        "awning-tricycle",
        "awning_tricycle",
        "train",
        "boat",
    }
)
_PERSON_LABELS = frozenset({"person", "firefighter"})
_AIRCRAFT_LABELS = frozenset(
    {"aircraft", "airplane", "aeroplane", "plane", "helicopter", "drone", "uav"}
)
_FIRE_LABELS = frozenset({"fire", "flame", "smoke", "smoldering_area", "burned_area"})


@dataclass(frozen=True, slots=True)
class _MotionProfile:
    """Class-specific image-plane dynamics used by association and Kalman prediction.

    The common detector observes very different motion regimes: a pedestrian can
    reverse direction between frames, a distant aircraft can cross many pixels at
    nearly constant velocity, while a ground vehicle is usually smoother.  Keeping
    one permissive gate for every class either loses fast targets or merges nearby
    vehicles.  These bounded multipliers retain the configured global limits while
    adapting the covariance and gates to the semantic family.
    """

    process_noise_scale: float = 1.0
    center_gate_scale: float = 1.0
    innovation_gate_scale: float = 1.0
    reacquisition_gate_scale: float = 1.0
    measurement_noise_scale: float = 1.0
    size_smoothing_scale: float = 1.0
    velocity_correction_scale: float = 1.0
    motion_hint_scale: float = 1.0


_MOTION_PROFILES = {
    # Human pose changes and abrupt direction reversals need more model uncertainty.
    "person": _MotionProfile(1.65, 1.25, 1.30, 1.35),
    # Road vehicles can cover a little more image distance between staggered detector
    # passes, but the gate stays narrow enough that adjacent traffic is not merged.
    "vehicle": _MotionProfile(1.20, 1.20, 1.15, 1.20, 1.10, 0.90, 1.0),
    # Fast image-plane travel and scale changes are normal for aircraft.
    "aircraft": _MotionProfile(2.40, 1.80, 1.75, 1.85),
    # A fixed ignition source inherits camera motion, while its detected contour can
    # flicker.  Trust the camera-compensated prediction more than each contour edge,
    # damp residual velocity, and smooth size much more conservatively.
    "fire": _MotionProfile(0.70, 1.30, 1.35, 1.40, 3.00, 0.35, 0.35, 0.35),
}


def _label_family(label: str) -> str:
    normalized = label.strip().lower()
    if normalized in _VEHICLE_LABELS:
        return "vehicle"
    if normalized in _PERSON_LABELS:
        return "person"
    if normalized in _AIRCRAFT_LABELS:
        return "aircraft"
    if normalized in _FIRE_LABELS:
        return "fire"
    return normalized


def _motion_profile(label: str) -> _MotionProfile:
    return _MOTION_PROFILES.get(_label_family(label), _MotionProfile())


class UnifiedTrackState(str, Enum):
    DETECTED = "detected"
    LOCKED = "locked"
    TRACKING = "tracking"
    OCCLUDED = "occluded"
    REACQUIRING = "reacquiring"
    RECOVERED = "recovered"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class AppearanceEmbedding:
    """Unit-normalized appearance vector produced by an external ReID encoder."""

    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.values) < 2:
            raise ValueError("appearance embedding must contain at least two values")
        if not all(math.isfinite(value) for value in self.values):
            raise ValueError("appearance embedding values must be finite")
        norm = math.sqrt(sum(value * value for value in self.values))
        if norm <= 1e-12:
            raise ValueError("appearance embedding cannot be a zero vector")
        object.__setattr__(self, "values", tuple(value / norm for value in self.values))

    def cosine_distance(self, other: AppearanceEmbedding) -> float:
        if len(self.values) != len(other.values):
            raise ValueError("appearance embedding dimensions must match")
        similarity = sum(
            left * right for left, right in zip(self.values, other.values, strict=True)
        )
        return max(0.0, min(2.0, 1.0 - similarity))


@dataclass(frozen=True, slots=True)
class TargetObservation:
    label: str
    confidence: float
    bbox: BoundingBox
    appearance: AppearanceEmbedding | None = None
    appearance_reliable: bool = True
    source: str = "detector"

    def __post_init__(self) -> None:
        normalized_label = self.label.strip().lower()
        if not normalized_label:
            raise ValueError("target observation label cannot be empty")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("target observation confidence must be in [0, 1]")
        if not self.source.strip():
            raise ValueError("target observation source cannot be empty")
        object.__setattr__(self, "label", normalized_label)

    @classmethod
    def from_detection(
        cls,
        detection: Detection,
        *,
        appearance: AppearanceEmbedding | None = None,
        appearance_reliable: bool = True,
    ) -> TargetObservation:
        return cls(
            label=detection.label,
            confidence=detection.confidence,
            bbox=detection.bbox,
            appearance=appearance,
            appearance_reliable=appearance_reliable,
            source=detection.model_version,
        )


@dataclass(frozen=True, slots=True)
class CameraMotionEstimate:
    """Normalized image-plane motion from the previous frame to this frame.

    ``dx``/``dy`` move the image centre in normalized coordinates.  ``rotation_deg``
    is measured in the OpenCV image coordinate system (positive is clockwise on a
    conventional screen) and ``aspect_ratio`` keeps roll compensation geometrically
    correct for non-square frames such as 16:9 RTSP video.  ``affine`` adds bounded
    yaw/pitch shear.  ``homography`` maps absolute normalized image coordinates and
    is used for the residual perspective component of a larger camera attitude
    change; when supplied it takes precedence over the affine representation.
    """

    dx: float
    dy: float
    scale: float = 1.0
    confidence: float = 1.0
    rotation_deg: float = 0.0
    aspect_ratio: float = 1.0
    affine: tuple[float, float, float, float] | None = None
    homography: tuple[float, float, float, float, float, float, float, float, float] | None = (
        None
    )

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.dx,
                self.dy,
                self.scale,
                self.confidence,
                self.rotation_deg,
                self.aspect_ratio,
            )
        ):
            raise ValueError("camera motion values must be finite")
        if not 0.5 <= self.scale <= 2.0:
            raise ValueError("camera motion scale must be in [0.5, 2.0]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("camera motion confidence must be in [0, 1]")
        if not 0.1 <= self.aspect_ratio <= 10.0:
            raise ValueError("camera motion aspect_ratio must be in [0.1, 10]")
        if self.affine is not None:
            if len(self.affine) != 4 or not all(math.isfinite(value) for value in self.affine):
                raise ValueError("camera motion affine must contain four finite values")
            affine = tuple(float(value) for value in self.affine)
            determinant = affine[0] * affine[3] - affine[1] * affine[2]
            if not 0.25 <= determinant <= 4.0:
                raise ValueError("camera motion affine determinant must be in [0.25, 4]")
            object.__setattr__(self, "affine", affine)
        if self.homography is not None:
            if len(self.homography) != 9 or not all(
                math.isfinite(value) for value in self.homography
            ):
                raise ValueError("camera motion homography must contain nine finite values")
            homography = _normalize_homography(tuple(float(value) for value in self.homography))
            center_denominator = homography[6] * 0.5 + homography[7] * 0.5 + homography[8]
            if abs(center_denominator) < 1e-8:
                raise ValueError("camera motion homography cannot have a centre projective pole")
            center_jacobian = _homography_jacobian(homography, 0.5, 0.5)
            determinant = (
                center_jacobian[0] * center_jacobian[3]
                - center_jacobian[1] * center_jacobian[2]
            )
            if not 0.25 <= determinant <= 4.0:
                raise ValueError(
                    "camera motion homography centre determinant must be in [0.25, 4]"
                )
            object.__setattr__(self, "homography", homography)

    @property
    def homography_matrix(
        self,
    ) -> tuple[float, float, float, float, float, float, float, float, float]:
        """Absolute normalized-coordinate transform from previous to current frame."""

        if self.homography is not None:
            return self.homography
        linear_xx, linear_xy, linear_yx, linear_yy = self.linear_matrix
        return (
            linear_xx,
            linear_xy,
            0.5 - 0.5 * (linear_xx + linear_xy) + self.dx,
            linear_yx,
            linear_yy,
            0.5 - 0.5 * (linear_yx + linear_yy) + self.dy,
            0.0,
            0.0,
            1.0,
        )

    @property
    def linear_matrix(self) -> tuple[float, float, float, float]:
        """Image-centre linearization, including optional affine shear or perspective."""

        return self.local_linear_matrix(0.5, 0.5)

    def local_linear_matrix(self, x: float, y: float) -> tuple[float, float, float, float]:
        """Return the local camera linearization at one prior-frame normalized point."""

        if self.homography is not None:
            return _homography_jacobian(self.homography, x, y)
        if self.affine is not None:
            return self.affine
        return _similarity_linear_matrix(
            scale=self.scale,
            rotation_deg=self.rotation_deg,
            aspect_ratio=self.aspect_ratio,
        )

    @property
    def effective_scale(self) -> float:
        """Area-preserving scale equivalent used by local-flow residual gating."""

        return self.local_scale_at(0.5, 0.5)

    def local_scale_at(self, x: float, y: float) -> float:
        """Return the local area-preserving scale at one prior-frame image point."""

        linear_xx, linear_xy, linear_yx, linear_yy = self.local_linear_matrix(x, y)
        return math.sqrt(linear_xx * linear_yy - linear_xy * linear_yx)

    def transform_point(self, x: float, y: float) -> tuple[float, float]:
        """Apply the measured global image motion to one normalized image point."""

        return _apply_homography_point(self.homography_matrix, x, y)


@dataclass(frozen=True, slots=True)
class TargetMotionHint:
    """Per-frame prediction correction; never counts as an identity observation."""

    track_id: str
    residual_dx: float
    residual_dy: float
    residual_scale: float = 1.0
    confidence: float = 1.0
    source: str = "optical_flow"

    def __post_init__(self) -> None:
        if not self.track_id.strip():
            raise ValueError("target motion hint track_id cannot be empty")
        if not self.source.strip():
            raise ValueError("target motion hint source cannot be empty")
        if not all(
            math.isfinite(value)
            for value in (
                self.residual_dx,
                self.residual_dy,
                self.residual_scale,
                self.confidence,
            )
        ):
            raise ValueError("target motion hint values must be finite")
        if abs(self.residual_dx) > 0.5 or abs(self.residual_dy) > 0.5:
            raise ValueError("target motion hint displacement is outside the bounded image gate")
        if not 0.5 <= self.residual_scale <= 2.0:
            raise ValueError("target motion hint scale must be in [0.5, 2.0]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("target motion hint confidence must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class UnifiedTargetPoolConfig:
    maximum_tracks: int = 64
    minimum_confirmed_hits: int = 3
    tentative_timeout_s: float = 0.75
    occluded_after_s: float = 0.35
    reacquisition_timeout_s: float = 2.0
    locked_reacquisition_timeout_s: float | None = None
    lost_retention_s: float = 15.0
    locked_lost_retention_s: float = 30.0
    minimum_iou: float = 0.08
    maximum_center_distance: float = 0.16
    maximum_appearance_distance: float = 0.38
    strict_reid_distance: float = 0.22
    # The deployed person encoder is trained on a broader set of viewing poses
    # than the vehicle encoder.  Keep a separate opt-in override so a person
    # calibration does not silently weaken vehicle or aircraft association.
    person_maximum_appearance_distance: float | None = None
    person_strict_reid_distance: float | None = None
    size_smoothing: float = 0.55
    appearance_history_size: int = 20
    frame_id_history_size: int = 4096
    minimum_motion_hint_confidence: float = 0.55
    strict_reid_ambiguity_margin: float = 0.035
    allow_locked_full_frame_reid: bool = True
    minimum_association_confidence: float = 0.10
    priority_minimum_new_track_confidence: float = 0.25
    minimum_new_track_confidence: float = 0.35
    high_confidence_threshold: float = 0.55
    kalman_process_noise: float = 0.04
    kalman_measurement_noise: float = 0.0004
    kalman_initial_position_variance: float = 0.0025
    kalman_initial_velocity_variance: float = 0.25
    kalman_max_prediction_horizon_s: float = 2.0
    kalman_gate_sigma: float = 4.0

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("maximum_tracks", self.maximum_tracks, 10),
            ("minimum_confirmed_hits", self.minimum_confirmed_hits, 1),
            ("appearance_history_size", self.appearance_history_size, 1),
            ("frame_id_history_size", self.frame_id_history_size, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not 0.0 < self.tentative_timeout_s <= self.occluded_after_s + 1.0:
            raise ValueError("tentative_timeout_s must be finite and positive")
        if not 0.0 < self.occluded_after_s < self.reacquisition_timeout_s:
            raise ValueError("timeouts must satisfy 0 < occluded < reacquisition")
        if not self.reacquisition_timeout_s < self.lost_retention_s:
            raise ValueError("lost_retention_s must exceed reacquisition_timeout_s")
        if self.locked_reacquisition_timeout_s is not None:
            if (
                not math.isfinite(self.locked_reacquisition_timeout_s)
                or self.locked_reacquisition_timeout_s <= self.reacquisition_timeout_s
            ):
                raise ValueError(
                    "locked_reacquisition_timeout_s must exceed reacquisition_timeout_s"
                )
            if self.locked_reacquisition_timeout_s >= self.locked_lost_retention_s:
                raise ValueError(
                    "locked_lost_retention_s must exceed locked_reacquisition_timeout_s"
                )
        if self.locked_lost_retention_s < self.lost_retention_s:
            raise ValueError("locked lost retention cannot be shorter than normal retention")
        for name, value in (
            ("minimum_iou", self.minimum_iou),
            ("maximum_center_distance", self.maximum_center_distance),
            ("maximum_appearance_distance", self.maximum_appearance_distance),
            ("strict_reid_distance", self.strict_reid_distance),
            ("size_smoothing", self.size_smoothing),
            ("minimum_motion_hint_confidence", self.minimum_motion_hint_confidence),
            ("strict_reid_ambiguity_margin", self.strict_reid_ambiguity_margin),
        ):
            if not math.isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.strict_reid_distance > self.maximum_appearance_distance:
            raise ValueError("strict ReID distance cannot exceed the normal appearance gate")
        for name, value in (
            ("person_maximum_appearance_distance", self.person_maximum_appearance_distance),
            ("person_strict_reid_distance", self.person_strict_reid_distance),
        ):
            if value is not None and (not math.isfinite(value) or not 0.0 < value <= 1.0):
                raise ValueError(f"{name} must be None or in (0, 1]")
        effective_person_appearance_gate = (
            self.person_maximum_appearance_distance
            if self.person_maximum_appearance_distance is not None
            else self.maximum_appearance_distance
        )
        effective_person_strict_reid = (
            self.person_strict_reid_distance
            if self.person_strict_reid_distance is not None
            else self.strict_reid_distance
        )
        if effective_person_strict_reid > effective_person_appearance_gate:
            raise ValueError(
                "person strict ReID distance cannot exceed the person appearance gate"
            )
        if not isinstance(self.allow_locked_full_frame_reid, bool):
            raise ValueError("allow_locked_full_frame_reid must be a boolean")
        confidence_thresholds = (
            self.minimum_association_confidence,
            self.priority_minimum_new_track_confidence,
            self.minimum_new_track_confidence,
            self.high_confidence_threshold,
        )
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in confidence_thresholds):
            raise ValueError("target-pool confidence thresholds must be finite and in [0, 1]")
        if not (
            self.minimum_association_confidence
            <= self.priority_minimum_new_track_confidence
            <= self.minimum_new_track_confidence
            <= self.high_confidence_threshold
        ):
            raise ValueError(
                "target-pool confidence thresholds must satisfy association <= priority new "
                "track <= fallback new track <= high"
            )
        for name, value in (
            ("kalman_process_noise", self.kalman_process_noise),
            ("kalman_measurement_noise", self.kalman_measurement_noise),
            ("kalman_initial_position_variance", self.kalman_initial_position_variance),
            ("kalman_initial_velocity_variance", self.kalman_initial_velocity_variance),
            ("kalman_max_prediction_horizon_s", self.kalman_max_prediction_horizon_s),
            ("kalman_gate_sigma", self.kalman_gate_sigma),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class UnifiedTrackSnapshot:
    track_id: str
    state: UnifiedTrackState
    label: str
    bbox: BoundingBox
    predicted_bbox: BoundingBox
    first_seen_at_s: float
    last_seen_at_s: float
    state_changed_at_s: float
    observation_count: int
    missed_frame_count: int
    confidence: float
    tracking_quality: float
    velocity_x_s: float
    velocity_y_s: float
    appearance_sample_count: int
    last_appearance_distance: float | None
    reid_confirmed: bool
    locked: bool
    primary: bool
    actionable: bool


@dataclass(frozen=True, slots=True)
class PrimaryTargetSwitchResult:
    previous_track_id: str | None
    primary_track_id: str
    switch_latency_ms: float
    switched_at_s: float
    background_locked_track_ids: tuple[str, ...]
    flight_control_enabled: bool = False


@dataclass(frozen=True, slots=True)
class TargetPoolUpdate:
    frame_id: str
    captured_at_s: float
    tracks: tuple[UnifiedTrackSnapshot, ...]
    created_track_ids: tuple[str, ...]
    recovered_track_ids: tuple[str, ...]
    lost_track_ids: tuple[str, ...]
    removed_track_ids: tuple[str, ...]
    dropped_observation_count: int
    association_latency_ms: float
    primary_track_id: str | None
    accepted_motion_hint_count: int
    rejected_motion_hint_count: int
    ambiguous_reid_recovery_count: int
    visual_confirmed_track_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class _Track:
    track_id: str
    label_votes: Counter[str]
    bbox: BoundingBox
    predicted_bbox: BoundingBox
    first_seen_at_s: float
    last_seen_at_s: float
    state_changed_at_s: float
    state: UnifiedTrackState
    observation_count: int
    missed_frame_count: int
    confidence: float
    center_x: float
    center_y: float
    width: float
    height: float
    velocity_x_s: float
    velocity_y_s: float
    covariance_x: tuple[float, float, float]
    covariance_y: tuple[float, float, float]
    camera_homography: tuple[float, float, float, float, float, float, float, float, float]
    camera_box_scale: float
    appearance_history: deque[AppearanceEmbedding]
    last_appearance_distance: float | None
    reid_confirmed: bool
    locked: bool

    @property
    def label(self) -> str:
        return min(self.label_votes, key=lambda item: (-self.label_votes[item], item))

    @classmethod
    def create(
        cls,
        track_id: str,
        observation: TargetObservation,
        captured_at_s: float,
        config: UnifiedTargetPoolConfig,
    ) -> _Track:
        center_x, center_y = observation.bbox.center
        appearance_history: deque[AppearanceEmbedding] = deque(
            maxlen=config.appearance_history_size
        )
        if observation.appearance is not None and observation.appearance_reliable:
            appearance_history.append(observation.appearance)
        return cls(
            track_id=track_id,
            label_votes=Counter({observation.label: 1}),
            bbox=observation.bbox,
            predicted_bbox=observation.bbox,
            first_seen_at_s=captured_at_s,
            last_seen_at_s=captured_at_s,
            state_changed_at_s=captured_at_s,
            state=UnifiedTrackState.DETECTED,
            observation_count=1,
            missed_frame_count=0,
            confidence=observation.confidence,
            center_x=center_x,
            center_y=center_y,
            width=observation.bbox.x2 - observation.bbox.x1,
            height=observation.bbox.y2 - observation.bbox.y1,
            velocity_x_s=0.0,
            velocity_y_s=0.0,
            covariance_x=(
                config.kalman_initial_position_variance,
                0.0,
                config.kalman_initial_velocity_variance,
            ),
            covariance_y=(
                config.kalman_initial_position_variance,
                0.0,
                config.kalman_initial_velocity_variance,
            ),
            camera_homography=_identity_homography(),
            camera_box_scale=1.0,
            appearance_history=appearance_history,
            last_appearance_distance=None,
            reid_confirmed=False,
            locked=False,
        )

    def appearance_prototype(self) -> AppearanceEmbedding | None:
        if not self.appearance_history:
            return None
        dimension = len(self.appearance_history[0].values)
        if any(len(sample.values) != dimension for sample in self.appearance_history):
            return None
        mean = tuple(
            sum(sample.values[index] for sample in self.appearance_history)
            / len(self.appearance_history)
            for index in range(dimension)
        )
        try:
            return AppearanceEmbedding(mean)
        except ValueError:
            return None

    def appearance_distance(
        self,
        observation: AppearanceEmbedding,
        *,
        include_gallery: bool,
    ) -> float | None:
        """Compare an observation with the mean prototype and optional pose gallery.

        A single averaged embedding becomes fragile when a moving person turns: two
        valid views can average into a feature that is not close enough to either
        view for strict recovery.  An operator-locked target therefore keeps the
        bounded reliable history as a small multi-pose gallery and uses its closest
        verified view.  Ordinary DET/TRK association keeps the mean-only behavior
        so one stale sample cannot make a background identity over-eager.
        """

        distances: list[float] = []
        prototype = self.appearance_prototype()
        if prototype is not None:
            distances.append(prototype.cosine_distance(observation))
        if include_gallery:
            distances.extend(
                sample.cosine_distance(observation) for sample in self.appearance_history
            )
        return min(distances) if distances else None


class UnifiedTargetPool:
    """Bounded multi-target bank with motion prediction and conservative ReID recovery."""

    def __init__(self, config: UnifiedTargetPoolConfig | None = None) -> None:
        self.config = config or UnifiedTargetPoolConfig()
        self._tracks: dict[str, _Track] = {}
        self._next_track_number = 1
        self._primary_track_id: str | None = None
        self._last_frame_time_s: float | None = None
        self._seen_frame_ids: set[str] = set()
        self._frame_id_history: deque[str] = deque()

    @property
    def primary_track_id(self) -> str | None:
        return self._primary_track_id

    def update(
        self,
        *,
        frame_id: str,
        captured_at_s: float,
        observations: Sequence[TargetObservation],
        camera_motion: CameraMotionEstimate | None = None,
        motion_hints: Sequence[TargetMotionHint] = (),
        visual_confirmation_track_ids: Sequence[str] = (),
    ) -> TargetPoolUpdate:
        started = time.perf_counter()
        self._validate_frame(frame_id, captured_at_s)
        motion = (
            camera_motion if camera_motion is not None and camera_motion.confidence >= 0.5 else None
        )
        hints_by_track: dict[str, TargetMotionHint] = {}
        rejected_motion_hints = 0
        for hint in motion_hints:
            if hint.track_id in hints_by_track:
                raise ValueError("target motion hints must contain unique track IDs")
            if (
                hint.track_id not in self._tracks
                or hint.confidence < self.config.minimum_motion_hint_confidence
            ):
                rejected_motion_hints += 1
                continue
            hints_by_track[hint.track_id] = hint
        visual_confirmation_ids: set[str] = set()
        for track_id in visual_confirmation_track_ids:
            normalized_track_id = track_id.strip()
            if not normalized_track_id:
                raise ValueError("visual confirmation track IDs cannot be empty")
            if normalized_track_id in visual_confirmation_ids:
                raise ValueError("visual confirmation track IDs must be unique")
            if normalized_track_id in self._tracks:
                visual_confirmation_ids.add(normalized_track_id)
        self._remember_frame(frame_id, captured_at_s)
        for track in self._tracks.values():
            if motion is not None:
                self._accumulate_camera_motion(track, motion)
            hint = hints_by_track.get(track.track_id)
            if hint is not None:
                self._accumulate_motion_hint(
                    track,
                    hint,
                    scale=_motion_profile(track.label).motion_hint_scale,
                )
            track.predicted_bbox = self._predict_bbox(track, captured_at_s)

        candidates: list[_AssociationCandidate] = []
        for track_id, track in self._tracks.items():
            for observation_index, observation in enumerate(observations):
                if observation.confidence < self.config.minimum_association_confidence:
                    continue
                association = self._association(track, observation, captured_at_s)
                if association is None:
                    continue
                (
                    stage,
                    cost,
                    appearance_distance,
                    reid_confirmed,
                    full_frame_reid,
                ) = association
                candidates.append(
                    (
                        stage,
                        cost,
                        track_id,
                        observation_index,
                        appearance_distance,
                        reid_confirmed,
                        full_frame_reid,
                    )
                )

        ambiguous_reid_pairs = self._ambiguous_full_frame_reid_pairs(candidates)
        if ambiguous_reid_pairs:
            candidates = [
                candidate
                for candidate in candidates
                if (candidate[2], candidate[3]) not in ambiguous_reid_pairs
            ]

        matched_tracks: set[str] = set()
        matched_observations: set[int] = set()
        recovered_track_ids: list[str] = []
        for stage in sorted({candidate[0] for candidate in candidates}):
            stage_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate[0] == stage
                and candidate[2] not in matched_tracks
                and candidate[3] not in matched_observations
            )
            for (
                _stage,
                _cost,
                track_id,
                observation_index,
                appearance_distance,
                reid_confirmed,
                _full_frame_reid,
            ) in _minimum_cost_candidate_assignment(stage_candidates):
                track = self._tracks[track_id]
                previous_state = track.state
                self._observe(
                    track,
                    observations[observation_index],
                    captured_at_s,
                    appearance_distance=appearance_distance,
                    reid_confirmed=reid_confirmed,
                )
                if previous_state in {
                    UnifiedTrackState.OCCLUDED,
                    UnifiedTrackState.REACQUIRING,
                    UnifiedTrackState.LOST,
                }:
                    recovered_track_ids.append(track_id)
                matched_tracks.add(track_id)
                matched_observations.add(observation_index)

        visual_confirmed_track_ids: list[str] = []
        for track_id in sorted(visual_confirmation_ids):
            if track_id in matched_tracks or track_id not in hints_by_track:
                continue
            track = self._tracks[track_id]
            if track.state is UnifiedTrackState.LOST:
                continue
            self._confirm_visual_hint(
                track,
                captured_at_s,
                hint=hints_by_track[track_id],
            )
            matched_tracks.add(track_id)
            visual_confirmed_track_ids.append(track_id)

        lost_track_ids: list[str] = []
        for track_id, track in tuple(self._tracks.items()):
            if track_id in matched_tracks:
                continue
            previous_state = track.state
            self._miss(track, captured_at_s)
            if (
                track.state is UnifiedTrackState.LOST
                and previous_state is not UnifiedTrackState.LOST
            ):
                lost_track_ids.append(track_id)

        removed_track_ids = self._remove_expired(captured_at_s)
        created_track_ids: list[str] = []
        dropped_observations = sum(
            observation.confidence < self._new_track_threshold(observation)
            for index, observation in enumerate(observations)
            if index not in matched_observations
        )
        for observation_index, observation in enumerate(observations):
            if observation_index in matched_observations:
                continue
            if observation.confidence < self._new_track_threshold(observation):
                continue
            if len(self._tracks) >= self.config.maximum_tracks:
                evicted = self._evict_one(captured_at_s)
                if evicted is None:
                    dropped_observations += 1
                    continue
                removed_track_ids.append(evicted)
            track_id = f"target-{self._next_track_number:06d}"
            self._next_track_number += 1
            self._tracks[track_id] = _Track.create(
                track_id,
                observation,
                captured_at_s,
                self.config,
            )
            created_track_ids.append(track_id)

        snapshots = self.snapshots()
        return TargetPoolUpdate(
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            tracks=snapshots,
            created_track_ids=tuple(created_track_ids),
            recovered_track_ids=tuple(recovered_track_ids),
            lost_track_ids=tuple(lost_track_ids),
            removed_track_ids=tuple(removed_track_ids),
            dropped_observation_count=dropped_observations,
            association_latency_ms=(time.perf_counter() - started) * 1_000.0,
            primary_track_id=self._primary_track_id,
            accepted_motion_hint_count=len(hints_by_track),
            rejected_motion_hint_count=rejected_motion_hints,
            ambiguous_reid_recovery_count=len(ambiguous_reid_pairs),
            visual_confirmed_track_ids=tuple(visual_confirmed_track_ids),
        )

    def _new_track_threshold(self, observation: TargetObservation) -> float:
        if _label_family(observation.label) in {"person", "vehicle", "aircraft", "fire"}:
            return self.config.priority_minimum_new_track_confidence
        return self.config.minimum_new_track_confidence

    def _maximum_appearance_distance(self, label: str) -> float:
        if (
            _label_family(label) == "person"
            and self.config.person_maximum_appearance_distance is not None
        ):
            return self.config.person_maximum_appearance_distance
        return self.config.maximum_appearance_distance

    def _strict_reid_distance(self, label: str) -> float:
        if _label_family(label) == "person" and self.config.person_strict_reid_distance is not None:
            return self.config.person_strict_reid_distance
        return self.config.strict_reid_distance

    def lock(self, track_id: str, *, now_s: float) -> UnifiedTrackSnapshot:
        track = self._required_track(track_id)
        if track.state in {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.LOST,
        }:
            raise ValueError("cannot lock an uncertain target without reliable reacquisition")
        track.locked = True
        self._transition(track, UnifiedTrackState.LOCKED, now_s)
        if self._primary_track_id is None:
            self._primary_track_id = track_id
        return self._snapshot(track)

    def unlock(self, track_id: str, *, now_s: float) -> UnifiedTrackSnapshot:
        track = self._required_track(track_id)
        track.locked = False
        if self._primary_track_id == track_id:
            self._primary_track_id = None
        if track.state is UnifiedTrackState.LOCKED:
            self._transition(track, UnifiedTrackState.TRACKING, now_s)
        return self._snapshot(track)

    def switch_primary(self, track_id: str, *, now_s: float) -> PrimaryTargetSwitchResult:
        started = time.perf_counter()
        track = self._required_track(track_id)
        if not track.locked:
            raise ValueError("primary target must already be in the locked target pool")
        if track.state in {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.LOST,
        }:
            raise ValueError("cannot switch primary control to an uncertain target")
        previous = self._primary_track_id
        self._primary_track_id = track_id
        background = tuple(
            sorted(
                candidate.track_id
                for candidate in self._tracks.values()
                if candidate.locked and candidate.track_id != track_id
            )
        )
        return PrimaryTargetSwitchResult(
            previous_track_id=previous,
            primary_track_id=track_id,
            switch_latency_ms=(time.perf_counter() - started) * 1_000.0,
            switched_at_s=now_s,
            background_locked_track_ids=background,
        )

    def snapshots(self) -> tuple[UnifiedTrackSnapshot, ...]:
        return tuple(self._snapshot(self._tracks[track_id]) for track_id in sorted(self._tracks))

    def _association(
        self,
        track: _Track,
        observation: TargetObservation,
        captured_at_s: float,
    ) -> tuple[int, float, float | None, bool, bool] | None:
        # Detector subtypes can fluctuate at distance. Preserve identity within
        # a semantic family while rejecting every cross-family association.
        if _label_family(observation.label) != _label_family(track.label):
            return None
        overlap = track.predicted_bbox.iou(observation.bbox)
        center_distance = track.predicted_bbox.center_distance(observation.bbox)
        motion_profile = _motion_profile(track.label)
        innovation_gate = (
            self.config.kalman_gate_sigma * motion_profile.innovation_gate_scale
        )
        kalman_innovation_distance = self._kalman_innovation_distance(
            track,
            observation,
            captured_at_s,
        )
        age_s = max(0.0, captured_at_s - track.last_seen_at_s)
        expanded_center_gate = (
            self.config.maximum_center_distance
            * motion_profile.center_gate_scale
            * min(2.5, 1.0 + age_s / self.config.reacquisition_timeout_s)
        )
        appearance_distance = None
        appearance_gate = self._maximum_appearance_distance(track.label)
        strict_reid_gate = self._strict_reid_distance(track.label)
        if observation.appearance is not None and observation.appearance_reliable:
            try:
                appearance_distance = track.appearance_distance(
                    observation.appearance,
                    include_gallery=track.locked,
                )
            except ValueError:
                return None
            if (
                appearance_distance is not None
                and appearance_distance > appearance_gate
            ):
                return None

        if track.state is UnifiedTrackState.LOST:
            if (
                appearance_distance is None
                or appearance_distance > strict_reid_gate
            ):
                return None
            spatially_plausible = center_distance <= expanded_center_gate or overlap > 0.0
            full_frame_reid = not spatially_plausible
            if full_frame_reid and not (track.locked and self.config.allow_locked_full_frame_reid):
                return None
            stage = 4 if full_frame_reid else 3
            reid_confirmed = True
        elif track.state is UnifiedTrackState.REACQUIRING:
            strong_motion = overlap >= max(self.config.minimum_iou, 0.2) or (
                center_distance <= 0.08 * motion_profile.reacquisition_gate_scale
                and kalman_innovation_distance <= innovation_gate
            )
            strong_appearance = (
                appearance_distance is not None
                and appearance_distance <= strict_reid_gate
            )
            if not (strong_motion or strong_appearance):
                return None
            stage = 2
            reid_confirmed = strong_appearance
            full_frame_reid = strong_appearance and (
                center_distance > expanded_center_gate and overlap <= 0.0
            )
        else:
            if overlap < self.config.minimum_iou and (
                center_distance > expanded_center_gate
                or kalman_innovation_distance > innovation_gate
            ):
                return None
            if track.state is UnifiedTrackState.DETECTED:
                stage = 1
            elif observation.confidence >= self.config.high_confidence_threshold:
                stage = 0
            else:
                stage = 1
            reid_confirmed = False
            full_frame_reid = False

        # Once the operator enters exclusive LCK, preserve that identity before
        # active duplicate tracks can consume the same observation.  Strict ReID
        # is the strongest route; motion-gated locked associations still outrank
        # ordinary DET/TRK candidates.  Full-frame ambiguity rejection below is
        # unchanged, so a near-tied ReID result remains conservative.
        if track.locked:
            stage = -2 if reid_confirmed else -1

        motion_cost = min(
            1.0,
            kalman_innovation_distance / innovation_gate,
        )
        overlap_cost = 1.0 - overlap
        if appearance_distance is None:
            cost = 0.62 * motion_cost + 0.38 * overlap_cost
        else:
            appearance_cost = min(
                1.0, appearance_distance / appearance_gate
            )
            cost = 0.50 * appearance_cost + 0.30 * motion_cost + 0.20 * overlap_cost
        cost += (1.0 - observation.confidence) * 0.05
        return stage, cost, appearance_distance, reid_confirmed, full_frame_reid

    def _kalman_innovation_distance(
        self,
        track: _Track,
        observation: TargetObservation,
        captured_at_s: float,
    ) -> float:
        """Return the 2D normalized innovation distance for association gating."""

        dt = min(
            self.config.kalman_max_prediction_horizon_s,
            max(0.0, captured_at_s - track.last_seen_at_s),
        )
        process_noise = (
            self.config.kalman_process_noise * _motion_profile(track.label).process_noise_scale
        )
        predicted_x, _velocity_x, covariance_x = _kalman_forecast_axis(
            track.center_x,
            track.velocity_x_s,
            track.covariance_x,
            dt,
            process_noise,
        )
        predicted_y, _velocity_y, covariance_y = _kalman_forecast_axis(
            track.center_y,
            track.velocity_y_s,
            track.covariance_y,
            dt,
            process_noise,
        )
        base_predicted_x, base_predicted_y = predicted_x, predicted_y
        predicted_x, predicted_y = _apply_homography_point(
            track.camera_homography,
            base_predicted_x,
            base_predicted_y,
        )
        covariance_x, covariance_y = _camera_transformed_axis_covariances(
            covariance_x,
            covariance_y,
            linear_matrix=_homography_jacobian(
                track.camera_homography,
                base_predicted_x,
                base_predicted_y,
            ),
        )
        observed_x, observed_y = observation.bbox.center
        measurement_noise = (
            self.config.kalman_measurement_noise
            * _motion_profile(track.label).measurement_noise_scale
        )
        innovation_variance_x = covariance_x[0] + measurement_noise
        innovation_variance_y = covariance_y[0] + measurement_noise
        normalized_squared = (observed_x - predicted_x) ** 2 / innovation_variance_x + (
            observed_y - predicted_y
        ) ** 2 / innovation_variance_y
        return math.sqrt(max(0.0, normalized_squared))

    def _ambiguous_full_frame_reid_pairs(
        self,
        candidates: Sequence[_AssociationCandidate],
    ) -> set[tuple[str, int]]:
        """Reject near-tied full-frame identity recovery instead of guessing.

        The detector continues to emit unmatched observations as new targets.  The old locked
        identity remains LOST until one appearance candidate is separated by a sufficient
        margin.  This is deliberately stricter than normal spatial association.
        """

        full_frame = [
            candidate
            for candidate in candidates
            if candidate[6] and candidate[4] is not None and candidate[5]
        ]
        blocked: set[tuple[str, int]] = set()
        by_track: dict[str, list[tuple[float, int]]] = {}
        by_observation: dict[int, list[tuple[float, str]]] = {}
        for candidate in full_frame:
            distance = candidate[4]
            if distance is None:  # Narrowed by the comprehension; retained for type checkers.
                continue
            track_id = candidate[2]
            observation_index = candidate[3]
            by_track.setdefault(track_id, []).append((distance, observation_index))
            by_observation.setdefault(observation_index, []).append((distance, track_id))

        margin = self.config.strict_reid_ambiguity_margin
        for track_id, matches in by_track.items():
            ordered = sorted(matches)
            if len(ordered) >= 2 and ordered[1][0] - ordered[0][0] < margin:
                blocked.update((track_id, observation_index) for _, observation_index in ordered)
        for observation_index, matches in by_observation.items():
            ordered = sorted(matches)
            if len(ordered) >= 2 and ordered[1][0] - ordered[0][0] < margin:
                blocked.update((track_id, observation_index) for _, track_id in ordered)
        return blocked

    def _observe(
        self,
        track: _Track,
        observation: TargetObservation,
        captured_at_s: float,
        *,
        appearance_distance: float | None,
        reid_confirmed: bool,
    ) -> None:
        previous_state = track.state
        motion_profile = _motion_profile(track.label)
        dt = min(
            self.config.kalman_max_prediction_horizon_s,
            max(1e-3, captured_at_s - track.last_seen_at_s),
        )
        observed_center_x, observed_center_y = observation.bbox.center
        process_noise = self.config.kalman_process_noise * motion_profile.process_noise_scale
        predicted_x, predicted_velocity_x, predicted_covariance_x = _kalman_forecast_axis(
            track.center_x,
            track.velocity_x_s,
            track.covariance_x,
            dt,
            process_noise,
        )
        predicted_y, predicted_velocity_y, predicted_covariance_y = _kalman_forecast_axis(
            track.center_y,
            track.velocity_y_s,
            track.covariance_y,
            dt,
            process_noise,
        )
        base_predicted_x, base_predicted_y = predicted_x, predicted_y
        camera_linear_matrix = _homography_jacobian(
            track.camera_homography,
            base_predicted_x,
            base_predicted_y,
        )
        predicted_x, predicted_y = _apply_homography_point(
            track.camera_homography,
            base_predicted_x,
            base_predicted_y,
        )
        predicted_velocity_x, predicted_velocity_y = _apply_camera_linear(
            predicted_velocity_x,
            predicted_velocity_y,
            linear_matrix=camera_linear_matrix,
        )
        predicted_covariance_x, predicted_covariance_y = _camera_transformed_axis_covariances(
            predicted_covariance_x,
            predicted_covariance_y,
            linear_matrix=camera_linear_matrix,
        )
        corrected_x, corrected_velocity_x, track.covariance_x = _kalman_correct_axis(
            predicted_x,
            predicted_velocity_x,
            predicted_covariance_x,
            observed_center_x,
            self.config.kalman_measurement_noise * motion_profile.measurement_noise_scale,
        )
        corrected_y, corrected_velocity_y, track.covariance_y = _kalman_correct_axis(
            predicted_y,
            predicted_velocity_y,
            predicted_covariance_y,
            observed_center_y,
            self.config.kalman_measurement_noise * motion_profile.measurement_noise_scale,
        )
        track.center_x = corrected_x
        track.center_y = corrected_y
        track.velocity_x_s = predicted_velocity_x + motion_profile.velocity_correction_scale * (
            corrected_velocity_x - predicted_velocity_x
        )
        track.velocity_y_s = predicted_velocity_y + motion_profile.velocity_correction_scale * (
            corrected_velocity_y - predicted_velocity_y
        )
        track.camera_homography = _identity_homography()
        track.camera_box_scale = 1.0
        observed_width = observation.bbox.x2 - observation.bbox.x1
        observed_height = observation.bbox.y2 - observation.bbox.y1
        size_smoothing = min(
            1.0,
            max(0.01, self.config.size_smoothing * motion_profile.size_smoothing_scale),
        )
        track.width += size_smoothing * (observed_width - track.width)
        track.height += size_smoothing * (observed_height - track.height)
        track.bbox = _bbox_from_center(track.center_x, track.center_y, track.width, track.height)
        track.predicted_bbox = track.bbox
        track.last_seen_at_s = captured_at_s
        track.observation_count += 1
        track.missed_frame_count = 0
        track.confidence = 0.7 * track.confidence + 0.3 * observation.confidence
        track.label_votes[observation.label] += 1
        track.last_appearance_distance = appearance_distance
        track.reid_confirmed = reid_confirmed
        if observation.appearance is not None and observation.appearance_reliable:
            track.appearance_history.append(observation.appearance)

        if previous_state in {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.LOST,
        }:
            self._transition(track, UnifiedTrackState.RECOVERED, captured_at_s)
        elif previous_state is UnifiedTrackState.LOCKED:
            self._transition(track, UnifiedTrackState.TRACKING, captured_at_s)
        elif previous_state is UnifiedTrackState.RECOVERED:
            self._transition(track, UnifiedTrackState.TRACKING, captured_at_s)
        elif (
            previous_state is UnifiedTrackState.DETECTED
            and track.observation_count >= self.config.minimum_confirmed_hits
        ):
            self._transition(track, UnifiedTrackState.TRACKING, captured_at_s)

    def _miss(self, track: _Track, captured_at_s: float) -> None:
        track.missed_frame_count += 1
        track.bbox = track.predicted_bbox
        age_s = captured_at_s - track.last_seen_at_s
        if track.state is UnifiedTrackState.DETECTED and age_s > self.config.tentative_timeout_s:
            self._transition(track, UnifiedTrackState.LOST, captured_at_s)
        elif age_s <= self.config.occluded_after_s:
            self._transition(track, UnifiedTrackState.OCCLUDED, captured_at_s)
        elif age_s <= self._reacquisition_timeout_s(track):
            self._transition(track, UnifiedTrackState.REACQUIRING, captured_at_s)
        else:
            self._transition(track, UnifiedTrackState.LOST, captured_at_s)

    def _confirm_visual_hint(
        self,
        track: _Track,
        captured_at_s: float,
        *,
        hint: TargetMotionHint,
    ) -> None:
        """Commit a validated local-flow/template prediction as visual continuity.

        Motion hints remain prediction-only for ordinary DET targets.  The caller
        opts in only operator-selected manual tracks and the sole exclusive LCK
        target.  This lets several manual boxes keep receiving real image evidence
        after another selection becomes active, and bridges detector gaps for a
        moving locked person without fabricating a new semantic identity.
        """

        previous_state = track.state
        dt = max(1e-3, captured_at_s - track.last_seen_at_s)
        # A fire region is normally fixed in the scene.  Its local optical-flow
        # texture changes with the flame contour, so retain only a bounded share
        # of that residual after camera compensation.  Dynamic families keep the
        # original full local-flow correction.
        hint_scale = _motion_profile(track.label).motion_hint_scale
        residual_dx = hint.residual_dx * hint_scale
        residual_dy = hint.residual_dy * hint_scale
        # A short-term hint is the residual after camera motion and the current
        # Kalman velocity have already been removed.  Feed that residual back into
        # the velocity estimate so manual tracks coast through an occasional weak
        # optical-flow frame instead of freezing at the last confirmed rectangle.
        base_predicted_x = track.center_x + track.velocity_x_s * dt
        base_predicted_y = track.center_y + track.velocity_y_s * dt
        camera_linear_matrix = _homography_jacobian(
            track.camera_homography,
            base_predicted_x,
            base_predicted_y,
        )
        predicted_velocity_x, predicted_velocity_y = _apply_camera_linear(
            track.velocity_x_s,
            track.velocity_y_s,
            linear_matrix=camera_linear_matrix,
        )
        track.covariance_x, track.covariance_y = _camera_transformed_axis_covariances(
            track.covariance_x,
            track.covariance_y,
            linear_matrix=camera_linear_matrix,
        )
        measured_velocity_x = predicted_velocity_x + residual_dx / dt
        measured_velocity_y = predicted_velocity_y + residual_dy / dt
        velocity_gain = min(0.90, max(0.35, 0.25 + 0.65 * hint.confidence))
        track.velocity_x_s = predicted_velocity_x + velocity_gain * (
            measured_velocity_x - predicted_velocity_x
        )
        track.velocity_y_s = predicted_velocity_y + velocity_gain * (
            measured_velocity_y - predicted_velocity_y
        )
        committed = track.predicted_bbox
        track.bbox = committed
        track.center_x, track.center_y = committed.center
        track.width = committed.x2 - committed.x1
        track.height = committed.y2 - committed.y1
        track.camera_homography = _identity_homography()
        track.camera_box_scale = 1.0
        track.last_seen_at_s = captured_at_s
        track.observation_count += 1
        track.missed_frame_count = 0
        track.confidence = 0.85 * track.confidence + 0.15 * hint.confidence
        track.reid_confirmed = False
        if previous_state in {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
        }:
            self._transition(track, UnifiedTrackState.RECOVERED, captured_at_s)
        elif previous_state is UnifiedTrackState.LOCKED:
            self._transition(track, UnifiedTrackState.TRACKING, captured_at_s)
        elif previous_state is UnifiedTrackState.DETECTED:
            self._transition(track, UnifiedTrackState.TRACKING, captured_at_s)

    def _predict_bbox(
        self,
        track: _Track,
        captured_at_s: float,
    ) -> BoundingBox:
        dt = min(
            self.config.kalman_max_prediction_horizon_s,
            max(0.0, captured_at_s - track.last_seen_at_s),
        )
        process_noise = (
            self.config.kalman_process_noise * _motion_profile(track.label).process_noise_scale
        )
        center_x, _velocity_x, _covariance_x = _kalman_forecast_axis(
            track.center_x,
            track.velocity_x_s,
            track.covariance_x,
            dt,
            process_noise,
        )
        center_y, _velocity_y, _covariance_y = _kalman_forecast_axis(
            track.center_y,
            track.velocity_y_s,
            track.covariance_y,
            dt,
            process_noise,
        )
        return _camera_transformed_bbox(
            center_x,
            center_y,
            track.width,
            track.height,
            homography=track.camera_homography,
            box_scale=track.camera_box_scale,
        )

    @staticmethod
    def _accumulate_camera_motion(
        track: _Track,
        camera_motion: CameraMotionEstimate,
    ) -> None:
        track.camera_homography = _multiply_homographies(
            camera_motion.homography_matrix,
            track.camera_homography,
        )

    @staticmethod
    def _accumulate_motion_hint(
        track: _Track,
        hint: TargetMotionHint,
        *,
        scale: float = 1.0,
    ) -> None:
        """Apply an already camera-compensated local-flow residual.

        `scale` lets static semantic families suppress contour-induced local
        jitter without weakening the independently estimated global camera
        motion.  Keeping it here also makes the subsequent visual confirmation
        consume the same predicted box that association saw.
        """

        bounded_scale = min(1.0, max(0.0, scale))
        track.camera_homography = _multiply_homographies(
            _translation_homography(
                hint.residual_dx * bounded_scale,
                hint.residual_dy * bounded_scale,
            ),
            track.camera_homography,
        )
        residual_scale = 1.0 + bounded_scale * (hint.residual_scale - 1.0)
        track.camera_box_scale = min(2.0, max(0.5, track.camera_box_scale * residual_scale))

    def _remove_expired(self, captured_at_s: float) -> list[str]:
        removed: list[str] = []
        for track_id, track in tuple(self._tracks.items()):
            if track.state is not UnifiedTrackState.LOST:
                continue
            retention_s = (
                self.config.locked_lost_retention_s
                if track.locked
                else self.config.lost_retention_s
            )
            if captured_at_s - track.last_seen_at_s <= retention_s:
                continue
            removed.append(track_id)
            del self._tracks[track_id]
            if self._primary_track_id == track_id:
                self._primary_track_id = None
        return removed

    def _evict_one(self, captured_at_s: float) -> str | None:
        candidates = [track for track in self._tracks.values() if not track.locked]
        if not candidates:
            return None
        candidate = min(
            candidates,
            key=lambda track: (
                0 if track.state is UnifiedTrackState.LOST else 1,
                track.last_seen_at_s,
                track.confidence,
                track.track_id,
            ),
        )
        del self._tracks[candidate.track_id]
        if self._primary_track_id == candidate.track_id:
            self._primary_track_id = None
        return candidate.track_id

    def _snapshot(self, track: _Track) -> UnifiedTrackSnapshot:
        age_s = max(0.0, (self._last_frame_time_s or track.last_seen_at_s) - track.last_seen_at_s)
        freshness = max(0.0, 1.0 - age_s / self._reacquisition_timeout_s(track))
        continuity = min(1.0, track.observation_count / self.config.minimum_confirmed_hits)
        state_factor = {
            UnifiedTrackState.DETECTED: 0.45,
            UnifiedTrackState.LOCKED: 0.8,
            UnifiedTrackState.TRACKING: 1.0,
            UnifiedTrackState.OCCLUDED: 0.65,
            UnifiedTrackState.REACQUIRING: 0.3,
            UnifiedTrackState.RECOVERED: 0.8,
            UnifiedTrackState.LOST: 0.0,
        }[track.state]
        quality = max(
            0.0,
            min(1.0, track.confidence * freshness * (0.5 + 0.5 * continuity) * state_factor),
        )
        actionable = track.state in {
            UnifiedTrackState.LOCKED,
            UnifiedTrackState.TRACKING,
            UnifiedTrackState.RECOVERED,
        }
        return UnifiedTrackSnapshot(
            track_id=track.track_id,
            state=track.state,
            label=track.label,
            bbox=track.bbox,
            predicted_bbox=track.predicted_bbox,
            first_seen_at_s=track.first_seen_at_s,
            last_seen_at_s=track.last_seen_at_s,
            state_changed_at_s=track.state_changed_at_s,
            observation_count=track.observation_count,
            missed_frame_count=track.missed_frame_count,
            confidence=track.confidence,
            tracking_quality=quality,
            velocity_x_s=track.velocity_x_s,
            velocity_y_s=track.velocity_y_s,
            appearance_sample_count=len(track.appearance_history),
            last_appearance_distance=track.last_appearance_distance,
            reid_confirmed=track.reid_confirmed,
            locked=track.locked,
            primary=track.track_id == self._primary_track_id,
            actionable=actionable,
        )

    def _reacquisition_timeout_s(self, track: _Track) -> float:
        if track.locked and self.config.locked_reacquisition_timeout_s is not None:
            return self.config.locked_reacquisition_timeout_s
        return self.config.reacquisition_timeout_s

    def _required_track(self, track_id: str) -> _Track:
        if not track_id.strip():
            raise ValueError("track_id cannot be empty")
        try:
            return self._tracks[track_id]
        except KeyError as exc:
            raise KeyError(f"unknown target track: {track_id}") from exc

    def _validate_frame(self, frame_id: str, captured_at_s: float) -> None:
        if not frame_id.strip():
            raise ValueError("frame_id cannot be empty")
        if not math.isfinite(captured_at_s) or captured_at_s < 0.0:
            raise ValueError("captured_at_s must be finite and non-negative")
        if frame_id in self._seen_frame_ids:
            raise ValueError(f"duplicate frame_id: {frame_id}")
        if self._last_frame_time_s is not None and captured_at_s <= self._last_frame_time_s:
            raise ValueError("frame timestamps must be strictly increasing")

    def _remember_frame(self, frame_id: str, captured_at_s: float) -> None:
        self._seen_frame_ids.add(frame_id)
        self._frame_id_history.append(frame_id)
        if len(self._frame_id_history) > self.config.frame_id_history_size:
            self._seen_frame_ids.remove(self._frame_id_history.popleft())
        self._last_frame_time_s = captured_at_s

    @staticmethod
    def _transition(track: _Track, state: UnifiedTrackState, now_s: float) -> None:
        if track.state is state:
            return
        track.state = state
        track.state_changed_at_s = now_s


def _identity_homography() -> tuple[float, float, float, float, float, float, float, float, float]:
    return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def _normalize_homography(
    homography: tuple[float, float, float, float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Normalize a projective transform so the final element is one."""

    scale = homography[8]
    if not math.isfinite(scale) or abs(scale) < 1e-9:
        raise ValueError("camera homography has an invalid normalization scale")
    normalized = tuple(value / scale for value in homography)
    if not all(math.isfinite(value) for value in normalized):
        raise ValueError("camera homography normalization produced non-finite values")
    return normalized


def _multiply_homographies(
    left: tuple[float, float, float, float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Compose normalized image transforms as ``left @ right``."""

    left_00, left_01, left_02, left_10, left_11, left_12, left_20, left_21, left_22 = left
    right_00, right_01, right_02, right_10, right_11, right_12, right_20, right_21, right_22 = (
        right
    )
    return _normalize_homography(
        (
            left_00 * right_00 + left_01 * right_10 + left_02 * right_20,
            left_00 * right_01 + left_01 * right_11 + left_02 * right_21,
            left_00 * right_02 + left_01 * right_12 + left_02 * right_22,
            left_10 * right_00 + left_11 * right_10 + left_12 * right_20,
            left_10 * right_01 + left_11 * right_11 + left_12 * right_21,
            left_10 * right_02 + left_11 * right_12 + left_12 * right_22,
            left_20 * right_00 + left_21 * right_10 + left_22 * right_20,
            left_20 * right_01 + left_21 * right_11 + left_22 * right_21,
            left_20 * right_02 + left_21 * right_12 + left_22 * right_22,
        )
    )


def _translation_homography(
    dx: float,
    dy: float,
) -> tuple[float, float, float, float, float, float, float, float, float]:
    return (1.0, 0.0, dx, 0.0, 1.0, dy, 0.0, 0.0, 1.0)


def _apply_homography_point(
    homography: tuple[float, float, float, float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    """Transform a point while failing closed to the unwarped point at a projective pole."""

    h00, h01, h02, h10, h11, h12, h20, h21, h22 = homography
    denominator = h20 * x + h21 * y + h22
    if not math.isfinite(denominator) or abs(denominator) < 1e-8:
        return x, y
    transformed_x = (h00 * x + h01 * y + h02) / denominator
    transformed_y = (h10 * x + h11 * y + h12) / denominator
    if not math.isfinite(transformed_x) or not math.isfinite(transformed_y):
        return x, y
    return transformed_x, transformed_y


def _homography_jacobian(
    homography: tuple[float, float, float, float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float, float, float]:
    """Return the local image-plane linearization of a normalized homography."""

    h00, h01, h02, h10, h11, h12, h20, h21, h22 = homography
    denominator = h20 * x + h21 * y + h22
    if not math.isfinite(denominator) or abs(denominator) < 1e-8:
        return (1.0, 0.0, 0.0, 1.0)
    numerator_x = h00 * x + h01 * y + h02
    numerator_y = h10 * x + h11 * y + h12
    denominator_squared = denominator * denominator
    jacobian = (
        (h00 * denominator - numerator_x * h20) / denominator_squared,
        (h01 * denominator - numerator_x * h21) / denominator_squared,
        (h10 * denominator - numerator_y * h20) / denominator_squared,
        (h11 * denominator - numerator_y * h21) / denominator_squared,
    )
    if not all(math.isfinite(value) for value in jacobian):
        return (1.0, 0.0, 0.0, 1.0)
    return jacobian


def _camera_transformed_bbox(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
    *,
    homography: tuple[float, float, float, float, float, float, float, float, float],
    box_scale: float,
) -> BoundingBox:
    """Project all box corners so off-axis perspective does not collapse to a pan."""

    half_width = width * 0.5
    half_height = height * 0.5
    corners = tuple(
        _apply_homography_point(homography, x, y)
        for x, y in (
            (center_x - half_width, center_y - half_height),
            (center_x + half_width, center_y - half_height),
            (center_x + half_width, center_y + half_height),
            (center_x - half_width, center_y + half_height),
        )
    )
    minimum_x = min(point[0] for point in corners)
    maximum_x = max(point[0] for point in corners)
    minimum_y = min(point[1] for point in corners)
    maximum_y = max(point[1] for point in corners)
    transformed_center_x = (minimum_x + maximum_x) * 0.5
    transformed_center_y = (minimum_y + maximum_y) * 0.5
    return _bbox_from_center(
        transformed_center_x,
        transformed_center_y,
        (maximum_x - minimum_x) * box_scale,
        (maximum_y - minimum_y) * box_scale,
    )


def _similarity_linear_matrix(
    *,
    scale: float,
    rotation_deg: float,
    aspect_ratio: float,
) -> tuple[float, float, float, float]:
    """Build a centered normalized-image similarity transform for a square-pixel frame."""

    radians = math.radians(rotation_deg)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return (
        scale * cosine,
        -scale * sine / aspect_ratio,
        scale * sine * aspect_ratio,
        scale * cosine,
    )


def _multiply_camera_linear_matrices(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Compose centered image transforms as ``left @ right``."""

    left_xx, left_xy, left_yx, left_yy = left
    right_xx, right_xy, right_yx, right_yy = right
    return (
        left_xx * right_xx + left_xy * right_yx,
        left_xx * right_xy + left_xy * right_yy,
        left_yx * right_xx + left_yy * right_yx,
        left_yx * right_xy + left_yy * right_yy,
    )


def _apply_camera_transform(
    x: float,
    y: float,
    *,
    offset_x: float,
    offset_y: float,
    linear_matrix: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Apply global camera affine motion about the optical-image centre."""

    transformed_x, transformed_y = _apply_camera_linear(
        x - 0.5,
        y - 0.5,
        linear_matrix=linear_matrix,
    )
    return 0.5 + transformed_x + offset_x, 0.5 + transformed_y + offset_y


def _apply_camera_linear(
    x: float,
    y: float,
    *,
    linear_matrix: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Apply only the linear camera component to velocity or displacement."""

    linear_xx, linear_xy, linear_yx, linear_yy = linear_matrix
    return linear_xx * x + linear_xy * y, linear_yx * x + linear_yy * y


def _camera_transformed_axis_covariances(
    covariance_x: tuple[float, float, float],
    covariance_y: tuple[float, float, float],
    *,
    linear_matrix: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Approximate independent-axis Kalman covariances after camera affine motion.

    The tracker intentionally keeps the Kalman state scalar-per-axis.  Roll,
    non-uniform scale and small yaw/pitch shear couple the axes, so retain the
    transformed diagonal terms rather than pretending old x/y variances still
    describe the current frame.
    """

    coefficient_x_from_x, coefficient_x_from_y, coefficient_y_from_x, coefficient_y_from_y = (
        linear_matrix
    )

    def transform_axis(
        coefficient_from_x: float,
        coefficient_from_y: float,
    ) -> tuple[float, float, float]:
        x_weight = coefficient_from_x * coefficient_from_x
        y_weight = coefficient_from_y * coefficient_from_y
        return (
            max(1e-12, x_weight * covariance_x[0] + y_weight * covariance_y[0]),
            x_weight * covariance_x[1] + y_weight * covariance_y[1],
            max(1e-12, x_weight * covariance_x[2] + y_weight * covariance_y[2]),
        )

    return (
        transform_axis(coefficient_x_from_x, coefficient_x_from_y),
        transform_axis(coefficient_y_from_x, coefficient_y_from_y),
    )


def _camera_transformed_bbox_size(
    width: float,
    height: float,
    *,
    linear_matrix: tuple[float, float, float, float],
    box_scale: float,
) -> tuple[float, float]:
    """Return axis-aligned extent after affine camera motion and local box scaling."""

    linear_xx, linear_xy, linear_yx, linear_yy = linear_matrix
    return (
        box_scale * (abs(linear_xx) * width + abs(linear_xy) * height),
        box_scale * (abs(linear_yx) * width + abs(linear_yy) * height),
    )


def _bbox_from_center(center_x: float, center_y: float, width: float, height: float) -> BoundingBox:
    width = min(1.0, max(1e-6, width))
    height = min(1.0, max(1e-6, height))
    center_x = min(1.0 - width / 2.0, max(width / 2.0, center_x))
    center_y = min(1.0 - height / 2.0, max(height / 2.0, center_y))
    return BoundingBox(
        center_x - width / 2.0,
        center_y - height / 2.0,
        center_x + width / 2.0,
        center_y + height / 2.0,
    )


def _minimum_cost_candidate_assignment(
    candidates: Sequence[_AssociationCandidate],
) -> tuple[_AssociationCandidate, ...]:
    """Find the maximum-cardinality, minimum-cost association for one cascade tier.

    Each track receives a private dummy column with a penalty above every valid
    association cost.  Invalid real edges remain much more expensive than the dummy,
    so the rectangular Hungarian solution first retains every compatible match and
    then minimizes their total cost.  Sorting IDs and indices makes equal-cost output
    deterministic across Python and Jetson runs.
    """

    if not candidates:
        return ()
    track_ids = sorted({candidate[2] for candidate in candidates})
    observation_indices = sorted({candidate[3] for candidate in candidates})
    candidate_by_pair = {(candidate[2], candidate[3]): candidate for candidate in candidates}
    real_column_count = len(observation_indices)
    dummy_penalty = 2.0
    invalid_penalty = 1_000_000.0
    costs: list[list[float]] = []
    for row_index, track_id in enumerate(track_ids):
        row = [
            candidate_by_pair.get((track_id, observation_index), (0, invalid_penalty))[1]
            for observation_index in observation_indices
        ]
        row.extend(
            dummy_penalty + abs(row_index - dummy_index) * 1e-9
            for dummy_index in range(len(track_ids))
        )
        costs.append(row)

    selected: list[_AssociationCandidate] = []
    for row_index, column_index in enumerate(rectangular_linear_assignment(costs)):
        if column_index < 0 or column_index >= real_column_count:
            continue
        pair = (track_ids[row_index], observation_indices[column_index])
        candidate = candidate_by_pair.get(pair)
        if candidate is not None:
            selected.append(candidate)
    return tuple(sorted(selected, key=lambda candidate: (candidate[2], candidate[3])))


def _kalman_forecast_axis(
    position: float,
    velocity: float,
    covariance: tuple[float, float, float],
    dt: float,
    process_noise: float,
) -> tuple[float, float, tuple[float, float, float]]:
    """Forecast one normalized image axis with a white-acceleration CV model.

    The compact covariance tuple stores ``(position variance, position/velocity
    covariance, velocity variance)``.  Keeping the filter scalar-per-axis avoids a
    NumPy dependency in the 15+ Hz metadata path while still retaining a real
    uncertainty-propagating Kalman model.
    """

    position_variance, position_velocity_covariance, velocity_variance = covariance
    dt2 = dt * dt
    dt3 = dt2 * dt
    dt4 = dt2 * dt2
    predicted_position = position + velocity * dt
    predicted_position_variance = (
        position_variance
        + 2.0 * dt * position_velocity_covariance
        + dt2 * velocity_variance
        + 0.25 * dt4 * process_noise
    )
    predicted_position_velocity_covariance = (
        position_velocity_covariance + dt * velocity_variance + 0.5 * dt3 * process_noise
    )
    predicted_velocity_variance = velocity_variance + dt2 * process_noise
    return (
        predicted_position,
        velocity,
        (
            max(1e-12, predicted_position_variance),
            predicted_position_velocity_covariance,
            max(1e-12, predicted_velocity_variance),
        ),
    )


def _kalman_correct_axis(
    predicted_position: float,
    predicted_velocity: float,
    predicted_covariance: tuple[float, float, float],
    measurement: float,
    measurement_noise: float,
) -> tuple[float, float, tuple[float, float, float]]:
    position_variance, position_velocity_covariance, velocity_variance = predicted_covariance
    innovation_variance = position_variance + measurement_noise
    position_gain = position_variance / innovation_variance
    velocity_gain = position_velocity_covariance / innovation_variance
    innovation = measurement - predicted_position
    corrected_position = predicted_position + position_gain * innovation
    corrected_velocity = predicted_velocity + velocity_gain * innovation

    # H=[1, 0].  This is the symmetric scalar form of P=(I-KH)P.
    corrected_position_variance = (1.0 - position_gain) * position_variance
    corrected_position_velocity_covariance = (1.0 - position_gain) * position_velocity_covariance
    corrected_velocity_variance = velocity_variance - velocity_gain * position_velocity_covariance
    return (
        corrected_position,
        corrected_velocity,
        (
            max(1e-12, corrected_position_variance),
            corrected_position_velocity_covariance,
            max(1e-12, corrected_velocity_variance),
        ),
    )


__all__ = [
    "AppearanceEmbedding",
    "CameraMotionEstimate",
    "PrimaryTargetSwitchResult",
    "TargetObservation",
    "TargetMotionHint",
    "TargetPoolUpdate",
    "UnifiedTargetPool",
    "UnifiedTargetPoolConfig",
    "UnifiedTrackSnapshot",
    "UnifiedTrackState",
]

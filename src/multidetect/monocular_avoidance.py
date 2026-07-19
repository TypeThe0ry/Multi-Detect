from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import atan2, degrees, hypot, isfinite
from time import perf_counter
from typing import Any

from .vision import VisionDependencyError


class CollisionRiskState(str, Enum):
    CLEAR = "clear"
    CAUTION = "caution"
    AVOID = "avoid"
    INVALID = "invalid"


class VisionZone(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class SparseFlowSample:
    x: float
    y: float
    dx: float
    dy: float

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.x, self.y, self.dx, self.dy)):
            raise ValueError("sparse flow values must be finite")


@dataclass(frozen=True, slots=True)
class MonocularAvoidanceConfig:
    minimum_feature_count: int = 24
    minimum_zone_feature_count: int = 5
    minimum_radius_fraction: float = 0.08
    minimum_outward_speed_px_s: float = 3.0
    caution_ttc_s: float = 3.0
    avoid_ttc_s: float = 1.5
    maximum_data_age_s: float = 0.25
    maximum_frame_interval_s: float = 0.5
    maximum_features: int = 320
    analysis_width: int = 640

    def __post_init__(self) -> None:
        if self.minimum_feature_count < 8:
            raise ValueError("minimum_feature_count must be at least 8")
        if self.minimum_zone_feature_count < 1:
            raise ValueError("minimum_zone_feature_count must be positive")
        if not 0.0 < self.minimum_radius_fraction < 0.5:
            raise ValueError("minimum_radius_fraction must be in (0, 0.5)")
        if not isfinite(self.minimum_outward_speed_px_s) or self.minimum_outward_speed_px_s <= 0:
            raise ValueError("minimum_outward_speed_px_s must be finite and positive")
        if not 0.0 < self.avoid_ttc_s < self.caution_ttc_s:
            raise ValueError("TTC thresholds must satisfy 0 < avoid < caution")
        if not isfinite(self.maximum_data_age_s) or self.maximum_data_age_s <= 0:
            raise ValueError("maximum_data_age_s must be finite and positive")
        if not isfinite(self.maximum_frame_interval_s) or self.maximum_frame_interval_s <= 0:
            raise ValueError("maximum_frame_interval_s must be finite and positive")
        if self.maximum_features < self.minimum_feature_count:
            raise ValueError("maximum_features cannot be below minimum_feature_count")
        if self.analysis_width < 160:
            raise ValueError("analysis_width must be at least 160 pixels")


@dataclass(frozen=True, slots=True)
class ZoneCollisionRisk:
    zone: VisionZone
    state: CollisionRiskState
    feature_count: int
    outward_feature_count: int
    ttc_s: float | None
    confidence: float


@dataclass(frozen=True, slots=True)
class MonocularAvoidanceAssessment:
    frame_id: str
    state: CollisionRiskState
    zones: tuple[ZoneCollisionRisk, ...]
    captured_at_s: float
    produced_at_s: float
    data_age_s: float
    frame_interval_s: float | None
    valid_feature_count: int
    rotation_compensated: bool
    processing_time_ms: float
    reason: str | None = None
    advisory_only: bool = True
    camera_motion_dx: float | None = None
    camera_motion_dy: float | None = None
    camera_motion_scale: float | None = None
    camera_motion_confidence: float | None = None
    camera_motion_rotation_deg: float | None = None
    camera_motion_aspect_ratio: float | None = None
    camera_motion_affine: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if not self.frame_id.strip():
            raise ValueError("avoidance assessment frame_id cannot be empty")
        if self.captured_at_s > self.produced_at_s:
            raise ValueError("avoidance assessment cannot predate its source frame")
        if not self.advisory_only:
            raise ValueError("monocular avoidance output must remain advisory-only")
        camera_values = (
            self.camera_motion_dx,
            self.camera_motion_dy,
            self.camera_motion_scale,
            self.camera_motion_confidence,
        )
        if any(value is not None for value in camera_values):
            if not all(value is not None and isfinite(value) for value in camera_values):
                raise ValueError("camera-motion metadata must be complete and finite")
            if not 0.5 <= float(self.camera_motion_scale) <= 2.0:
                raise ValueError("camera-motion scale must be in [0.5, 2.0]")
            if not 0.0 <= float(self.camera_motion_confidence) <= 1.0:
                raise ValueError("camera-motion confidence must be in [0, 1]")
        extended_camera_values = (
            self.camera_motion_rotation_deg,
            self.camera_motion_aspect_ratio,
        )
        if any(value is not None for value in extended_camera_values):
            if not all(value is not None and isfinite(value) for value in camera_values):
                raise ValueError("extended camera-motion metadata requires base camera motion")
            if not all(value is not None and isfinite(value) for value in extended_camera_values):
                raise ValueError("extended camera-motion metadata must be complete and finite")
            if not 0.1 <= float(self.camera_motion_aspect_ratio) <= 10.0:
                raise ValueError("camera-motion aspect ratio must be in [0.1, 10]")
        if self.camera_motion_affine is not None:
            if not all(value is not None and isfinite(value) for value in camera_values):
                raise ValueError("camera-motion affine requires base camera motion")
            if len(self.camera_motion_affine) != 4 or not all(
                isfinite(value) for value in self.camera_motion_affine
            ):
                raise ValueError("camera-motion affine must contain four finite values")
            affine = tuple(float(value) for value in self.camera_motion_affine)
            determinant = affine[0] * affine[3] - affine[1] * affine[2]
            if not 0.25 <= determinant <= 4.0:
                raise ValueError("camera-motion affine determinant must be in [0.25, 4]")
            object.__setattr__(self, "camera_motion_affine", affine)


class MonocularCollisionRiskEvaluator:
    """Convert rotation-compensated sparse flow into advisory TTC risk zones."""

    def __init__(self, config: MonocularAvoidanceConfig | None = None) -> None:
        self.config = config or MonocularAvoidanceConfig()

    def evaluate(
        self,
        *,
        frame_id: str,
        width: int,
        height: int,
        captured_at_s: float,
        produced_at_s: float,
        frame_interval_s: float,
        samples: tuple[SparseFlowSample, ...],
        rotation_compensated: bool,
        processing_time_ms: float = 0.0,
        camera_motion_dx: float | None = None,
        camera_motion_dy: float | None = None,
        camera_motion_scale: float | None = None,
        camera_motion_confidence: float | None = None,
        camera_motion_rotation_deg: float | None = None,
        camera_motion_aspect_ratio: float | None = None,
        camera_motion_affine: tuple[float, float, float, float] | None = None,
    ) -> MonocularAvoidanceAssessment:
        if not frame_id.strip():
            raise ValueError("frame_id cannot be empty")
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        if not isfinite(captured_at_s) or not isfinite(produced_at_s):
            raise ValueError("avoidance timestamps must be finite")
        if captured_at_s > produced_at_s:
            raise ValueError("avoidance source frame cannot postdate its result")
        if not isfinite(frame_interval_s) or frame_interval_s <= 0:
            raise ValueError("frame_interval_s must be finite and positive")

        age_s = produced_at_s - captured_at_s
        invalid_reason = None
        if age_s > self.config.maximum_data_age_s:
            invalid_reason = "STALE_FRAME"
        elif frame_interval_s > self.config.maximum_frame_interval_s:
            invalid_reason = "FRAME_GAP"
        elif not rotation_compensated:
            invalid_reason = "ROTATION_UNCOMPENSATED"

        valid_samples = tuple(
            sample for sample in samples if 0.0 <= sample.x < width and 0.0 <= sample.y < height
        )
        if invalid_reason is None and len(valid_samples) < self.config.minimum_feature_count:
            invalid_reason = "INSUFFICIENT_FEATURES"
        if invalid_reason is not None:
            return self._invalid_assessment(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                frame_interval_s=frame_interval_s,
                valid_feature_count=len(valid_samples),
                rotation_compensated=rotation_compensated,
                processing_time_ms=processing_time_ms,
                reason=invalid_reason,
            )

        center_x = width * 0.5
        center_y = height * 0.5
        minimum_radius = hypot(width, height) * self.config.minimum_radius_fraction
        zone_samples: dict[VisionZone, list[tuple[SparseFlowSample, float | None]]] = {
            zone: [] for zone in VisionZone
        }
        for sample in valid_samples:
            radius_x = sample.x - center_x
            radius_y = sample.y - center_y
            radius = hypot(radius_x, radius_y)
            if radius < minimum_radius:
                continue
            radial_delta_px = (sample.dx * radius_x + sample.dy * radius_y) / radius
            radial_speed_px_s = radial_delta_px / frame_interval_s
            ttc_s = (
                radius / radial_speed_px_s
                if radial_speed_px_s >= self.config.minimum_outward_speed_px_s
                else None
            )
            zone_samples[self._zone_for_x(sample.x, width)].append((sample, ttc_s))

        zones = tuple(
            self._evaluate_zone(zone, observations) for zone, observations in zone_samples.items()
        )
        valid_zones = tuple(zone for zone in zones if zone.state is not CollisionRiskState.INVALID)
        if not valid_zones:
            overall_state = CollisionRiskState.INVALID
            reason = "INSUFFICIENT_ZONE_FEATURES"
        elif any(zone.state is CollisionRiskState.AVOID for zone in valid_zones):
            overall_state = CollisionRiskState.AVOID
            reason = None
        elif any(zone.state is CollisionRiskState.CAUTION for zone in valid_zones):
            overall_state = CollisionRiskState.CAUTION
            reason = None
        else:
            overall_state = CollisionRiskState.CLEAR
            reason = None

        return MonocularAvoidanceAssessment(
            frame_id=frame_id,
            state=overall_state,
            zones=zones,
            captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
            data_age_s=age_s,
            frame_interval_s=frame_interval_s,
            valid_feature_count=len(valid_samples),
            rotation_compensated=rotation_compensated,
            processing_time_ms=max(0.0, processing_time_ms),
            reason=reason,
            camera_motion_dx=camera_motion_dx,
            camera_motion_dy=camera_motion_dy,
            camera_motion_scale=camera_motion_scale,
            camera_motion_confidence=camera_motion_confidence,
            camera_motion_rotation_deg=camera_motion_rotation_deg,
            camera_motion_aspect_ratio=camera_motion_aspect_ratio,
            camera_motion_affine=camera_motion_affine,
        )

    def invalid(
        self,
        *,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
        reason: str,
        frame_interval_s: float | None = None,
        processing_time_ms: float = 0.0,
    ) -> MonocularAvoidanceAssessment:
        return self._invalid_assessment(
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
            frame_interval_s=frame_interval_s,
            valid_feature_count=0,
            rotation_compensated=False,
            processing_time_ms=processing_time_ms,
            reason=reason,
        )

    def _evaluate_zone(
        self,
        zone: VisionZone,
        observations: list[tuple[SparseFlowSample, float | None]],
    ) -> ZoneCollisionRisk:
        feature_count = len(observations)
        if feature_count < self.config.minimum_zone_feature_count:
            return ZoneCollisionRisk(zone, CollisionRiskState.INVALID, feature_count, 0, None, 0.0)
        ttc_values = sorted(ttc for _sample, ttc in observations if ttc is not None)
        confidence = min(1.0, feature_count / self.config.minimum_feature_count)
        if not ttc_values:
            return ZoneCollisionRisk(
                zone,
                CollisionRiskState.CLEAR,
                feature_count,
                0,
                None,
                confidence,
            )
        percentile_index = max(0, int((len(ttc_values) - 1) * 0.2))
        ttc_s = ttc_values[percentile_index]
        outward_count = len(ttc_values)
        outward_confidence = min(1.0, outward_count / self.config.minimum_zone_feature_count)
        confidence *= outward_confidence
        if ttc_s <= self.config.avoid_ttc_s:
            state = CollisionRiskState.AVOID
        elif ttc_s <= self.config.caution_ttc_s:
            state = CollisionRiskState.CAUTION
        else:
            state = CollisionRiskState.CLEAR
        return ZoneCollisionRisk(
            zone,
            state,
            feature_count,
            outward_count,
            ttc_s,
            confidence,
        )

    def _invalid_assessment(
        self,
        *,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
        frame_interval_s: float | None,
        valid_feature_count: int,
        rotation_compensated: bool,
        processing_time_ms: float,
        reason: str,
    ) -> MonocularAvoidanceAssessment:
        zones = tuple(
            ZoneCollisionRisk(zone, CollisionRiskState.INVALID, 0, 0, None, 0.0)
            for zone in VisionZone
        )
        return MonocularAvoidanceAssessment(
            frame_id=frame_id,
            state=CollisionRiskState.INVALID,
            zones=zones,
            captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
            data_age_s=max(0.0, produced_at_s - captured_at_s),
            frame_interval_s=frame_interval_s,
            valid_feature_count=valid_feature_count,
            rotation_compensated=rotation_compensated,
            processing_time_ms=max(0.0, processing_time_ms),
            reason=reason,
        )

    @staticmethod
    def _zone_for_x(x: float, width: int) -> VisionZone:
        if x < width / 3.0:
            return VisionZone.LEFT
        if x < width * 2.0 / 3.0:
            return VisionZone.CENTER
        return VisionZone.RIGHT


class OpenCVSparseFlowAvoidance:
    """Lightweight sparse-flow frontend; it emits no flight-control commands."""

    def __init__(self, config: MonocularAvoidanceConfig | None = None) -> None:
        self.config = config or MonocularAvoidanceConfig()
        self._evaluator = MonocularCollisionRiskEvaluator(self.config)
        self._previous_gray: Any | None = None
        self._previous_captured_at_s: float | None = None

    def update(
        self,
        image_bgr: Any,
        *,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
    ) -> MonocularAvoidanceAssessment:
        started = perf_counter()
        cv2, np = self._require_dependencies()
        if not hasattr(image_bgr, "shape") or len(image_bgr.shape) < 2:
            raise ValueError("monocular avoidance requires an image array")
        frame_height, frame_width = image_bgr.shape[:2]
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("monocular avoidance frame cannot be empty")
        scale = min(1.0, self.config.analysis_width / frame_width)
        if scale < 1.0:
            analysis = cv2.resize(
                image_bgr,
                (round(frame_width * scale), round(frame_height * scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            analysis = image_bgr
        gray = cv2.cvtColor(analysis, cv2.COLOR_BGR2GRAY)
        previous_gray = self._previous_gray
        previous_captured_at_s = self._previous_captured_at_s
        self._previous_gray = gray
        self._previous_captured_at_s = captured_at_s
        if previous_gray is None or previous_captured_at_s is None:
            return self._evaluator.invalid(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                reason="WARMUP",
                processing_time_ms=(perf_counter() - started) * 1000.0,
            )
        frame_interval_s = captured_at_s - previous_captured_at_s
        if frame_interval_s <= 0.0 or frame_interval_s > self.config.maximum_frame_interval_s:
            return self._evaluator.invalid(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                reason="FRAME_GAP",
                frame_interval_s=frame_interval_s,
                processing_time_ms=(perf_counter() - started) * 1000.0,
            )

        previous_points = cv2.goodFeaturesToTrack(
            previous_gray,
            maxCorners=self.config.maximum_features,
            qualityLevel=0.01,
            minDistance=8,
            blockSize=7,
        )
        if previous_points is None or len(previous_points) < self.config.minimum_feature_count:
            return self._evaluator.invalid(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                reason="INSUFFICIENT_FEATURES",
                frame_interval_s=frame_interval_s,
                processing_time_ms=(perf_counter() - started) * 1000.0,
            )
        current_points, status, _errors = cv2.calcOpticalFlowPyrLK(
            previous_gray,
            gray,
            previous_points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
        )
        if current_points is None or status is None:
            return self._evaluator.invalid(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                reason="OPTICAL_FLOW_FAILED",
                frame_interval_s=frame_interval_s,
                processing_time_ms=(perf_counter() - started) * 1000.0,
            )
        mask = status.reshape(-1).astype(bool)
        previous = previous_points.reshape(-1, 2)[mask]
        current = current_points.reshape(-1, 2)[mask]
        if len(previous) < self.config.minimum_feature_count:
            return self._evaluator.invalid(
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                reason="INSUFFICIENT_TRACKED_FEATURES",
                frame_interval_s=frame_interval_s,
                processing_time_ms=(perf_counter() - started) * 1000.0,
            )

        transform, inliers = cv2.estimateAffine2D(
            previous,
            current,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.5,
            maxIters=500,
            confidence=0.98,
            refineIters=5,
        )
        if transform is None or inliers is None:
            transform, inliers = cv2.estimateAffinePartial2D(
                previous,
                current,
                method=cv2.RANSAC,
                ransacReprojThreshold=2.5,
                maxIters=500,
                confidence=0.98,
                refineIters=5,
            )
        rotation_compensated = transform is not None and inliers is not None
        camera_motion_dx = None
        camera_motion_dy = None
        camera_motion_scale = None
        camera_motion_confidence = None
        camera_motion_rotation_deg = None
        camera_motion_aspect_ratio = None
        camera_motion_affine = None
        if rotation_compensated:
            matrix = transform[:, :2]
            determinant = float(np.linalg.det(matrix))
            if not isfinite(determinant) or determinant <= 0.0:
                rotation_compensated = False
            else:
                scale = determinant**0.5
                if not 0.5 <= scale <= 2.0:
                    rotation_compensated = False
                else:
                    rotation_deg = degrees(
                        atan2(
                            float(matrix[1, 0] - matrix[0, 1]),
                            float(matrix[0, 0] + matrix[1, 1]),
                        )
                    )
                    radians = rotation_deg * 3.141592653589793 / 180.0
                    rigid_matrix = np.asarray(
                        (
                            (np.cos(radians), -np.sin(radians)),
                            (np.sin(radians), np.cos(radians)),
                        ),
                        dtype=np.float64,
                    )
                    translation = transform[:, 2]
                    predicted = previous @ rigid_matrix.T + translation
                    residual = current - predicted
                    frame_center = np.asarray(
                        [gray.shape[1] * 0.5, gray.shape[0] * 0.5],
                        dtype=np.float64,
                    )
                    transformed_center = frame_center @ matrix.T + translation
                    camera_motion_dx = float(
                        (transformed_center[0] - frame_center[0]) / gray.shape[1]
                    )
                    camera_motion_dy = float(
                        (transformed_center[1] - frame_center[1]) / gray.shape[0]
                    )
                    camera_motion_scale = scale
                    camera_motion_confidence = float(np.mean(inliers.reshape(-1).astype(bool)))
                    camera_motion_rotation_deg = rotation_deg
                    camera_motion_aspect_ratio = gray.shape[1] / gray.shape[0]
                    camera_motion_affine = (
                        float(matrix[0, 0]),
                        float(matrix[0, 1]) * gray.shape[0] / gray.shape[1],
                        float(matrix[1, 0]) * gray.shape[1] / gray.shape[0],
                        float(matrix[1, 1]),
                    )
        if not rotation_compensated:
            residual = current - previous

        samples = tuple(
            SparseFlowSample(float(point[0]), float(point[1]), float(delta[0]), float(delta[1]))
            for point, delta in zip(current, residual, strict=True)
        )
        return self._evaluator.evaluate(
            frame_id=frame_id,
            width=gray.shape[1],
            height=gray.shape[0],
            captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
            frame_interval_s=frame_interval_s,
            samples=samples,
            rotation_compensated=rotation_compensated,
            processing_time_ms=(perf_counter() - started) * 1000.0,
            camera_motion_dx=camera_motion_dx,
            camera_motion_dy=camera_motion_dy,
            camera_motion_scale=camera_motion_scale,
            camera_motion_confidence=camera_motion_confidence,
            camera_motion_rotation_deg=camera_motion_rotation_deg,
            camera_motion_aspect_ratio=camera_motion_aspect_ratio,
            camera_motion_affine=camera_motion_affine,
        )

    @staticmethod
    def _require_dependencies() -> tuple[Any, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError(
                "Install live vision dependencies: pip install -e '.[vision]'"
            ) from exc
        return cv2, np


__all__ = [
    "CollisionRiskState",
    "MonocularAvoidanceAssessment",
    "MonocularAvoidanceConfig",
    "MonocularCollisionRiskEvaluator",
    "OpenCVSparseFlowAvoidance",
    "SparseFlowSample",
    "VisionZone",
    "ZoneCollisionRisk",
]

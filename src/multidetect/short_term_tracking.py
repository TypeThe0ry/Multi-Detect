from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .unified_tracking import (
    CameraMotionEstimate,
    TargetMotionHint,
    UnifiedTrackSnapshot,
    UnifiedTrackState,
)
from .vision import VisionDependencyError


class ShortTermTrackingStatus(str, Enum):
    WARMUP = "warmup"
    SKIPPED = "skipped"
    OK = "ok"
    DEGRADED = "degraded"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ShortTermTrackingConfig:
    analysis_width: int = 640
    maximum_tracks: int = 16
    maximum_features_per_track: int = 48
    minimum_flow_points: int = 6
    flow_quality_level: float = 0.01
    flow_minimum_distance_px: float = 4.0
    forward_backward_error_px: float = 1.5
    template_minimum_correlation: float = 0.72
    template_minimum_peak_margin: float = 0.05
    template_minimum_standard_deviation: float = 8.0
    search_expansion: float = 2.5
    occluded_search_multiplier: float = 1.5
    reacquiring_search_multiplier: float = 2.0
    maximum_search_expansion: float = 6.0
    maximum_retained_template_age_s: float = 2.0
    minimum_box_size_px: int = 12
    maximum_frame_interval_s: float = 0.5
    maximum_residual_displacement: float = 0.25
    frame_stride: int = 1
    camera_motion_maximum_features: int = 160
    camera_motion_minimum_features: int = 20
    camera_motion_minimum_inlier_ratio: float = 0.55
    camera_motion_exclusion_margin: float = 0.02
    camera_motion_maximum_displacement: float = 0.25
    camera_motion_minimum_scale: float = 0.80
    camera_motion_maximum_scale: float = 1.25
    camera_motion_maximum_rotation_deg: float = 40.0
    camera_motion_maximum_anisotropy: float = 1.35
    # Keep numerically-near-affine RANSAC fits on the affine path; 0.008 is below
    # a material off-axis perspective warp but above observed affine-fit noise.
    camera_motion_minimum_projective: float = 0.008
    camera_motion_maximum_projective: float = 0.35
    camera_motion_phase_correlation_minimum_response: float = 0.50

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("analysis_width", self.analysis_width, 160),
            ("maximum_tracks", self.maximum_tracks, 10),
            ("maximum_features_per_track", self.maximum_features_per_track, 8),
            ("minimum_flow_points", self.minimum_flow_points, 4),
            ("minimum_box_size_px", self.minimum_box_size_px, 4),
            ("frame_stride", self.frame_stride, 1),
            ("camera_motion_maximum_features", self.camera_motion_maximum_features, 32),
            ("camera_motion_minimum_features", self.camera_motion_minimum_features, 8),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.minimum_flow_points > self.maximum_features_per_track:
            raise ValueError("minimum_flow_points cannot exceed maximum_features_per_track")
        if self.camera_motion_minimum_features > self.camera_motion_maximum_features:
            raise ValueError(
                "camera_motion_minimum_features cannot exceed camera_motion_maximum_features"
            )
        for name, value in (
            ("flow_quality_level", self.flow_quality_level),
            ("template_minimum_correlation", self.template_minimum_correlation),
            ("template_minimum_peak_margin", self.template_minimum_peak_margin),
            ("camera_motion_minimum_inlier_ratio", self.camera_motion_minimum_inlier_ratio),
            (
                "camera_motion_phase_correlation_minimum_response",
                self.camera_motion_phase_correlation_minimum_response,
            ),
        ):
            if not math.isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
        for name, value in (
            ("flow_minimum_distance_px", self.flow_minimum_distance_px),
            ("forward_backward_error_px", self.forward_backward_error_px),
            ("template_minimum_standard_deviation", self.template_minimum_standard_deviation),
            ("maximum_frame_interval_s", self.maximum_frame_interval_s),
            ("maximum_residual_displacement", self.maximum_residual_displacement),
            ("camera_motion_maximum_displacement", self.camera_motion_maximum_displacement),
            ("camera_motion_maximum_rotation_deg", self.camera_motion_maximum_rotation_deg),
            ("camera_motion_maximum_anisotropy", self.camera_motion_maximum_anisotropy),
            ("camera_motion_minimum_projective", self.camera_motion_minimum_projective),
            ("camera_motion_maximum_projective", self.camera_motion_maximum_projective),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.search_expansion) or self.search_expansion < 1.25:
            raise ValueError("search_expansion must be at least 1.25")
        for name, value in (
            ("occluded_search_multiplier", self.occluded_search_multiplier),
            ("reacquiring_search_multiplier", self.reacquiring_search_multiplier),
        ):
            if not math.isfinite(value) or value < 1.0:
                raise ValueError(f"{name} must be finite and at least 1")
        if (
            not math.isfinite(self.maximum_search_expansion)
            or self.maximum_search_expansion < self.search_expansion
        ):
            raise ValueError("maximum_search_expansion cannot be below search_expansion")
        if (
            not math.isfinite(self.maximum_retained_template_age_s)
            or self.maximum_retained_template_age_s <= 0.0
        ):
            raise ValueError("maximum_retained_template_age_s must be finite and positive")
        if self.maximum_residual_displacement > 0.5:
            raise ValueError("maximum_residual_displacement cannot exceed 0.5")
        if not 0.0 <= self.camera_motion_exclusion_margin <= 0.20:
            raise ValueError("camera_motion_exclusion_margin must be in [0, 0.20]")
        if self.camera_motion_maximum_displacement > 0.5:
            raise ValueError("camera_motion_maximum_displacement cannot exceed 0.5")
        if self.camera_motion_maximum_rotation_deg > 180.0:
            raise ValueError("camera_motion_maximum_rotation_deg cannot exceed 180")
        if self.camera_motion_maximum_anisotropy < 1.0:
            raise ValueError("camera_motion_maximum_anisotropy must be at least 1")
        if self.camera_motion_maximum_projective > 1.0:
            raise ValueError("camera_motion_maximum_projective cannot exceed 1")
        if self.camera_motion_minimum_projective > self.camera_motion_maximum_projective:
            raise ValueError(
                "camera_motion_minimum_projective cannot exceed camera_motion_maximum_projective"
            )
        if not (
            0.5 <= self.camera_motion_minimum_scale <= 1.0
            <= self.camera_motion_maximum_scale <= 2.0
        ):
            raise ValueError(
                "camera motion scales must satisfy 0.5 <= minimum <= 1 <= maximum <= 2"
            )
        if self.frame_stride > 10:
            raise ValueError("frame_stride cannot exceed 10")


@dataclass(frozen=True, slots=True)
class ShortTermTrackingResult:
    status: ShortTermTrackingStatus
    hints: tuple[TargetMotionHint, ...]
    attempted_track_count: int
    optical_flow_hint_count: int
    template_hint_count: int
    processing_time_ms: float
    frame_interval_s: float | None
    reason: str | None = None
    camera_motion: CameraMotionEstimate | None = None
    camera_motion_source: str | None = None
    camera_motion_feature_count: int = 0
    metadata_only: bool = True
    flight_control_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.metadata_only or self.flight_control_enabled:
            raise ValueError("short-term tracking must remain metadata-only")
        if self.camera_motion_feature_count < 0:
            raise ValueError("camera motion feature count cannot be negative")
        if (self.camera_motion is None) != (self.camera_motion_source is None):
            raise ValueError("camera motion and source must be present together")


@dataclass(frozen=True, slots=True)
class _RetainedTemplate:
    image: Any
    captured_at_s: float
    pixel_box: tuple[int, int, int, int]
    capture_to_current_homography: tuple[
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
    ]
    camera_warp_valid: bool


class OpenCVShortTermTargetTracker:
    """Local flow with conservative template fallback for prediction hints only."""

    def __init__(self, config: ShortTermTrackingConfig | None = None) -> None:
        self.config = config or ShortTermTrackingConfig()
        self._previous_gray: Any | None = None
        self._previous_captured_at_s: float | None = None
        self._previous_tracks: tuple[UnifiedTrackSnapshot, ...] = ()
        self._retained_templates: dict[str, _RetainedTemplate] = {}
        self._frame_number = 0

    @property
    def retained_template_count(self) -> int:
        return len(self._retained_templates)

    def update_frame(
        self,
        image_bgr: Any,
        *,
        captured_at_s: float,
        camera_motion: CameraMotionEstimate | None = None,
        prefer_background_motion: bool = False,
        exclusive_track_id: str | None = None,
    ) -> ShortTermTrackingResult:
        started = time.perf_counter()
        if exclusive_track_id is not None and not exclusive_track_id.strip():
            raise ValueError("exclusive short-term track ID cannot be empty")
        if not isinstance(prefer_background_motion, bool):
            raise ValueError("prefer_background_motion must be boolean")
        cv2, np = self._require_dependencies()
        if not hasattr(image_bgr, "shape") or len(image_bgr.shape) < 2:
            raise ValueError("short-term tracking requires an image array")
        frame_height, frame_width = image_bgr.shape[:2]
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("short-term tracking frame cannot be empty")
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
        previous_at_s = self._previous_captured_at_s
        previous_tracks = self._previous_tracks
        self._previous_gray = gray
        self._previous_captured_at_s = captured_at_s
        self._frame_number += 1

        if previous_gray is None or previous_at_s is None:
            return self._result(
                started,
                ShortTermTrackingStatus.WARMUP,
                reason="WARMUP",
            )
        frame_interval_s = captured_at_s - previous_at_s
        if frame_interval_s <= 0.0 or frame_interval_s > self.config.maximum_frame_interval_s:
            self._invalidate_retained_template_camera_warps()
            return self._result(
                started,
                ShortTermTrackingStatus.INVALID,
                frame_interval_s=frame_interval_s,
                reason="FRAME_GAP",
            )
        if previous_gray.shape != gray.shape:
            self._invalidate_retained_template_camera_warps()
            return self._result(
                started,
                ShortTermTrackingStatus.INVALID,
                frame_interval_s=frame_interval_s,
                reason="FRAME_GEOMETRY_CHANGED",
            )
        effective_frame_stride = 1 if exclusive_track_id is not None else self.config.frame_stride
        if self._frame_number % effective_frame_stride != 0:
            # The reference frame advances even while short-term work is skipped.
            # Do not pretend that an older retained crop can be geometrically
            # reprojected through an unmeasured camera-motion gap.
            self._invalidate_retained_template_camera_warps()
            return self._result(
                started,
                ShortTermTrackingStatus.SKIPPED,
                frame_interval_s=frame_interval_s,
                reason="FRAME_STRIDE",
            )

        eligible_tracks = tuple(
            track
            for track in sorted(previous_tracks, key=self._track_priority)
            if track.state is not UnifiedTrackState.LOST
            and (exclusive_track_id is None or track.track_id == exclusive_track_id)
        )[: self.config.maximum_tracks]
        effective_camera_motion = camera_motion
        camera_motion_source = "external" if camera_motion is not None else None
        camera_motion_feature_count = 0
        if prefer_background_motion or effective_camera_motion is None:
            (
                background_camera_motion,
                camera_motion_feature_count,
                background_camera_motion_source,
            ) = self._estimate_background_camera_motion(
                cv2,
                np,
                previous_gray,
                gray,
                previous_tracks,
            )
            if background_camera_motion is not None:
                effective_camera_motion = background_camera_motion
                camera_motion_source = background_camera_motion_source
            elif camera_motion is not None:
                effective_camera_motion = camera_motion
                camera_motion_source = "external"
        self._advance_retained_template_camera_warps(np, effective_camera_motion)
        hints: list[TargetMotionHint] = []
        flow_count = 0
        template_count = 0
        expected_motion = {
            track.track_id: self._expected_motion(
                track,
                frame_interval_s,
                effective_camera_motion,
            )
            for track in eligible_tracks
        }
        flow_hints = self._batched_flow_hints(
            cv2,
            np,
            previous_gray,
            gray,
            eligible_tracks,
            expected_motion,
            frame_interval_s=frame_interval_s,
            camera_motion=effective_camera_motion,
        )
        camera_warped_previous_gray = None
        if (
            effective_camera_motion is not None
            and any(track.track_id not in flow_hints for track in eligible_tracks)
        ):
            camera_warped_previous_gray = self._camera_warped_previous_frame(
                cv2,
                np,
                previous_gray,
                effective_camera_motion,
            )
        for track in eligible_tracks:
            expected_dx, expected_dy, _expected_scale = expected_motion[track.track_id]
            hint = flow_hints.get(track.track_id)
            if hint is not None:
                hints.append(hint)
                flow_count += 1
                continue
            hint = self._template_hint(
                cv2,
                np,
                previous_gray,
                gray,
                track,
                expected_dx,
                expected_dy,
                captured_at_s,
                camera_motion=effective_camera_motion,
                camera_warped_previous_gray=camera_warped_previous_gray,
            )
            if hint is not None:
                hints.append(hint)
                template_count += 1

        if not eligible_tracks:
            status = ShortTermTrackingStatus.WARMUP
            reason = "NO_TRACKS"
        elif hints:
            status = ShortTermTrackingStatus.OK
            reason = None
        else:
            status = ShortTermTrackingStatus.DEGRADED
            reason = "NO_RELIABLE_LOCAL_MOTION"
        return self._result(
            started,
            status,
            hints=tuple(hints),
            attempted_track_count=len(eligible_tracks),
            optical_flow_hint_count=flow_count,
            template_hint_count=template_count,
            frame_interval_s=frame_interval_s,
            reason=reason,
            camera_motion=effective_camera_motion,
            camera_motion_source=camera_motion_source,
            camera_motion_feature_count=camera_motion_feature_count,
        )

    def _estimate_background_camera_motion(
        self,
        cv2: Any,
        np: Any,
        previous_gray: Any,
        gray: Any,
        tracks: tuple[UnifiedTrackSnapshot, ...],
    ) -> tuple[CameraMotionEstimate | None, int, str | None]:
        """Estimate global image motion while excluding every known target region."""

        height, width = previous_gray.shape[:2]
        mask = np.full((height, width), 255, dtype=np.uint8)
        margin_x = round(width * self.config.camera_motion_exclusion_margin)
        margin_y = round(height * self.config.camera_motion_exclusion_margin)
        for track in tracks:
            x1, y1, x2, y2 = self._pixel_box(track.bbox, width, height)
            mask[
                max(0, y1 - margin_y) : min(height, y2 + margin_y),
                max(0, x1 - margin_x) : min(width, x2 + margin_x),
            ] = 0
        previous_points = cv2.goodFeaturesToTrack(
            previous_gray,
            mask=mask,
            maxCorners=self.config.camera_motion_maximum_features,
            qualityLevel=self.config.flow_quality_level,
            minDistance=max(6.0, self.config.flow_minimum_distance_px),
            blockSize=7,
        )
        if (
            previous_points is None
            or len(previous_points) < self.config.camera_motion_minimum_features
        ):
            return None, 0 if previous_points is None else len(previous_points), None
        initial_feature_count = len(previous_points)
        current_points, forward_status, _ = cv2.calcOpticalFlowPyrLK(
            previous_gray,
            gray,
            previous_points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
        )
        if current_points is None or forward_status is None:
            return self._phase_correlation_camera_motion(
                cv2,
                np,
                previous_gray,
                gray,
                initial_feature_count,
            )
        backward_points, backward_status, _ = cv2.calcOpticalFlowPyrLK(
            gray,
            previous_gray,
            current_points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
        )
        if backward_points is None or backward_status is None:
            return self._phase_correlation_camera_motion(
                cv2,
                np,
                previous_gray,
                gray,
                initial_feature_count,
            )
        previous = previous_points.reshape(-1, 2)
        current = current_points.reshape(-1, 2)
        backward = backward_points.reshape(-1, 2)
        valid = forward_status.reshape(-1).astype(bool)
        valid &= backward_status.reshape(-1).astype(bool)
        error = np.linalg.norm(previous - backward, axis=1)
        valid &= np.isfinite(error)
        valid &= error <= self.config.forward_backward_error_px
        previous = previous[valid]
        current = current[valid]
        feature_count = len(previous)
        if feature_count < self.config.camera_motion_minimum_features:
            return self._phase_correlation_camera_motion(
                cv2,
                np,
                previous_gray,
                gray,
                initial_feature_count,
            )
        if hasattr(cv2, "findHomography"):
            homography, homography_inliers = cv2.findHomography(
                previous,
                current,
                method=cv2.RANSAC,
                ransacReprojThreshold=2.5,
                maxIters=500,
                confidence=0.98,
            )
            perspective_motion = self._perspective_camera_motion(
                np,
                homography,
                homography_inliers,
                width=width,
                height=height,
            )
            if perspective_motion is not None:
                return perspective_motion, feature_count, "background_homography_flow"
        transform, inliers = cv2.estimateAffine2D(
            previous,
            current,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.5,
            maxIters=500,
            confidence=0.98,
            refineIters=5,
        )
        camera_motion_source = "background_affine_flow"
        if transform is None or inliers is None:
            # Full affine absorbs the small shear/non-uniform scale caused by yaw
            # and pitch.  If its four-parameter fit cannot be formed, retain the
            # old similarity fit instead of dropping a usable camera-motion update.
            transform, inliers = cv2.estimateAffinePartial2D(
                previous,
                current,
                method=cv2.RANSAC,
                ransacReprojThreshold=2.5,
                maxIters=500,
                confidence=0.98,
                refineIters=5,
            )
            camera_motion_source = "background_similarity_flow"
        if transform is None or inliers is None:
            return self._phase_correlation_camera_motion(
                cv2,
                np,
                previous_gray,
                gray,
                initial_feature_count,
            )
        inlier_ratio = float(np.mean(inliers.reshape(-1).astype(bool)))
        if inlier_ratio < self.config.camera_motion_minimum_inlier_ratio:
            return self._phase_correlation_camera_motion(
                cv2,
                np,
                previous_gray,
                gray,
                initial_feature_count,
            )
        matrix = transform[:, :2]
        determinant = float(np.linalg.det(matrix))
        if not math.isfinite(determinant) or determinant <= 0.0:
            return None, feature_count, None
        singular_values = np.linalg.svd(matrix, compute_uv=False)
        if len(singular_values) != 2 or singular_values[1] <= 1e-6:
            return None, feature_count, None
        anisotropy = float(singular_values[0] / singular_values[1])
        scale = math.sqrt(determinant)
        rotation_deg = math.degrees(
            math.atan2(
                float(matrix[1, 0] - matrix[0, 1]),
                float(matrix[0, 0] + matrix[1, 1]),
            )
        )
        if not (
            self.config.camera_motion_minimum_scale
            <= scale
            <= self.config.camera_motion_maximum_scale
        ):
            return None, feature_count, None
        if (
            not math.isfinite(anisotropy)
            or anisotropy > self.config.camera_motion_maximum_anisotropy
        ):
            return None, feature_count, None
        if (
            not math.isfinite(rotation_deg)
            or abs(rotation_deg) > self.config.camera_motion_maximum_rotation_deg
        ):
            return None, feature_count, None
        frame_center = np.asarray([width * 0.5, height * 0.5], dtype=np.float64)
        transformed_center = frame_center @ matrix.T + transform[:, 2]
        dx = float((transformed_center[0] - frame_center[0]) / width)
        dy = float((transformed_center[1] - frame_center[1]) / height)
        if (
            not math.isfinite(dx)
            or not math.isfinite(dy)
            or abs(dx) > self.config.camera_motion_maximum_displacement
            or abs(dy) > self.config.camera_motion_maximum_displacement
        ):
            return None, feature_count, None
        return (
            CameraMotionEstimate(
                dx=dx,
                dy=dy,
                scale=scale,
                confidence=inlier_ratio,
                rotation_deg=rotation_deg,
                aspect_ratio=width / height,
                affine=(
                    float(matrix[0, 0]),
                    float(matrix[0, 1]) * height / width,
                    float(matrix[1, 0]) * width / height,
                    float(matrix[1, 1]),
                ),
            ),
            feature_count,
            camera_motion_source,
        )

    def _perspective_camera_motion(
        self,
        np: Any,
        homography: Any,
        inliers: Any,
        *,
        width: int,
        height: int,
    ) -> CameraMotionEstimate | None:
        """Accept only bounded, material perspective motion from target-excluded flow."""

        if homography is None or inliers is None:
            return None
        inlier_ratio = float(np.mean(inliers.reshape(-1).astype(bool)))
        if inlier_ratio < self.config.camera_motion_minimum_inlier_ratio:
            return None
        matrix = np.asarray(homography, dtype=np.float64)
        if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
            return None
        pixel_to_normalized = np.asarray(
            ((1.0 / width, 0.0, 0.0), (0.0, 1.0 / height, 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        normalized_to_pixel = np.asarray(
            ((float(width), 0.0, 0.0), (0.0, float(height), 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        normalized = pixel_to_normalized @ matrix @ normalized_to_pixel
        normalization_scale = float(normalized[2, 2])
        if not math.isfinite(normalization_scale) or abs(normalization_scale) < 1e-9:
            return None
        normalized /= normalization_scale
        if not np.all(np.isfinite(normalized)):
            return None
        projective_strength = max(abs(float(normalized[2, 0])), abs(float(normalized[2, 1])))
        if not (
            self.config.camera_motion_minimum_projective
            <= projective_strength
            <= self.config.camera_motion_maximum_projective
        ):
            # A numerically-near-affine fit is intentionally handled by the faster,
            # more stable affine estimator below instead of adding perspective jitter.
            return None
        h00, h01, h02 = (float(value) for value in normalized[0])
        h10, h11, h12 = (float(value) for value in normalized[1])
        h20, h21, h22 = (float(value) for value in normalized[2])
        denominator = h20 * 0.5 + h21 * 0.5 + h22
        if not math.isfinite(denominator) or abs(denominator) < 1e-8:
            return None
        numerator_x = h00 * 0.5 + h01 * 0.5 + h02
        numerator_y = h10 * 0.5 + h11 * 0.5 + h12
        transformed_center_x = numerator_x / denominator
        transformed_center_y = numerator_y / denominator
        denominator_squared = denominator * denominator
        jacobian = np.asarray(
            (
                (
                    (h00 * denominator - numerator_x * h20) / denominator_squared,
                    (h01 * denominator - numerator_x * h21) / denominator_squared,
                ),
                (
                    (h10 * denominator - numerator_y * h20) / denominator_squared,
                    (h11 * denominator - numerator_y * h21) / denominator_squared,
                ),
            ),
            dtype=np.float64,
        )
        if not np.all(np.isfinite(jacobian)):
            return None
        determinant = float(np.linalg.det(jacobian))
        if not math.isfinite(determinant) or determinant <= 0.0:
            return None
        singular_values = np.linalg.svd(jacobian, compute_uv=False)
        if len(singular_values) != 2 or singular_values[1] <= 1e-6:
            return None
        anisotropy = float(singular_values[0] / singular_values[1])
        scale = math.sqrt(determinant)
        rotation_deg = math.degrees(
            math.atan2(
                float(jacobian[1, 0] - jacobian[0, 1]),
                float(jacobian[0, 0] + jacobian[1, 1]),
            )
        )
        dx = transformed_center_x - 0.5
        dy = transformed_center_y - 0.5
        if not (
            self.config.camera_motion_minimum_scale
            <= scale
            <= self.config.camera_motion_maximum_scale
        ):
            return None
        if (
            not math.isfinite(anisotropy)
            or anisotropy > self.config.camera_motion_maximum_anisotropy
        ):
            return None
        if (
            not math.isfinite(rotation_deg)
            or abs(rotation_deg) > self.config.camera_motion_maximum_rotation_deg
        ):
            return None
        if (
            not math.isfinite(dx)
            or not math.isfinite(dy)
            or abs(dx) > self.config.camera_motion_maximum_displacement
            or abs(dy) > self.config.camera_motion_maximum_displacement
        ):
            return None
        try:
            return CameraMotionEstimate(
                dx=dx,
                dy=dy,
                scale=scale,
                confidence=inlier_ratio,
                rotation_deg=rotation_deg,
                aspect_ratio=width / height,
                homography=(h00, h01, h02, h10, h11, h12, h20, h21, h22),
            )
        except ValueError:
            return None

    def _phase_correlation_camera_motion(
        self,
        cv2: Any,
        np: Any,
        previous_gray: Any,
        gray: Any,
        feature_count: int,
    ) -> tuple[CameraMotionEstimate | None, int, str | None]:
        """Recover a large translation after sparse LK loses its pyramid match.

        This fallback is only reached when the initial frame had enough
        non-target background features.  It therefore cannot turn an isolated
        moving target on a blank frame into a false camera pan.  It is
        translation-only and remains bounded by the same displacement limit as
        the sparse-flow estimate.
        """

        height, width = previous_gray.shape[:2]
        window = cv2.createHanningWindow((width, height), cv2.CV_32F)
        (shift_x_px, shift_y_px), response = cv2.phaseCorrelate(
            previous_gray.astype(np.float32),
            gray.astype(np.float32),
            window,
        )
        response = float(response)
        dx = float(shift_x_px / width)
        dy = float(shift_y_px / height)
        if (
            not math.isfinite(response)
            or response < self.config.camera_motion_phase_correlation_minimum_response
            or not math.isfinite(dx)
            or not math.isfinite(dy)
            or abs(dx) > self.config.camera_motion_maximum_displacement
            or abs(dy) > self.config.camera_motion_maximum_displacement
        ):
            return None, feature_count, None
        return (
            CameraMotionEstimate(
                dx=dx,
                dy=dy,
                scale=1.0,
                confidence=min(1.0, response),
                rotation_deg=0.0,
                aspect_ratio=width / height,
            ),
            feature_count,
            "background_phase_correlation",
        )

    def synchronize_tracks(
        self,
        tracks: tuple[UnifiedTrackSnapshot, ...],
        *,
        exclusive_track_id: str | None = None,
    ) -> None:
        track_ids = [track.track_id for track in tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("short-term tracking snapshots must have unique track IDs")
        if exclusive_track_id is not None:
            if not exclusive_track_id.strip():
                raise ValueError("exclusive short-term track ID cannot be empty")
            tracks = tuple(track for track in tracks if track.track_id == exclusive_track_id)
        self._previous_tracks = tracks
        retained_tracks = tuple(
            sorted(tracks, key=self._track_priority)[: self.config.maximum_tracks]
        )
        retained_ids = {track.track_id for track in retained_tracks}
        self._retained_templates = {
            track_id: template
            for track_id, template in self._retained_templates.items()
            if track_id in retained_ids
        }
        gray = self._previous_gray
        captured_at_s = self._previous_captured_at_s
        if gray is None or captured_at_s is None:
            return
        height, width = gray.shape[:2]
        reliable_states = {
            UnifiedTrackState.DETECTED,
            UnifiedTrackState.LOCKED,
            UnifiedTrackState.TRACKING,
            UnifiedTrackState.RECOVERED,
        }
        for track in retained_tracks:
            if track.missed_frame_count != 0 or track.state not in reliable_states:
                continue
            x1, y1, x2, y2 = self._pixel_box(track.bbox, width, height)
            if not self._box_is_usable((x1, y1, x2, y2)):
                continue
            template = gray[y1:y2, x1:x2]
            if template.size == 0:
                continue
            self._retained_templates[track.track_id] = _RetainedTemplate(
                image=template.copy(),
                captured_at_s=captured_at_s,
                pixel_box=(x1, y1, x2, y2),
                capture_to_current_homography=(
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ),
                camera_warp_valid=True,
            )

    def _invalidate_retained_template_camera_warps(self) -> None:
        """Keep the appearance crop, but drop an unmeasured geometric chain."""

        if not self._retained_templates:
            return
        self._retained_templates = {
            track_id: _RetainedTemplate(
                image=template.image,
                captured_at_s=template.captured_at_s,
                pixel_box=template.pixel_box,
                capture_to_current_homography=template.capture_to_current_homography,
                camera_warp_valid=False,
            )
            for track_id, template in self._retained_templates.items()
        }

    def _advance_retained_template_camera_warps(
        self,
        np: Any,
        camera_motion: CameraMotionEstimate | None,
    ) -> None:
        """Carry retained crops through every measured previous-to-current warp.

        A retained appearance crop can span multiple occluded frames.  The old
        path compared that crop directly against the current image, so even a
        modest roll/zoom made reacquisition fail after the first lost frame.
        Compose only trusted, consecutive camera transforms; a skipped or
        unmeasured frame invalidates the geometric chain while preserving the
        conservative raw-template fallback.
        """

        if not self._retained_templates:
            return
        if camera_motion is None or camera_motion.confidence < 0.5:
            self._invalidate_retained_template_camera_warps()
            return
        delta = np.asarray(camera_motion.homography_matrix, dtype=np.float64).reshape(3, 3)
        if not np.all(np.isfinite(delta)):
            self._invalidate_retained_template_camera_warps()
            return
        advanced: dict[str, _RetainedTemplate] = {}
        for track_id, template in self._retained_templates.items():
            if not template.camera_warp_valid:
                advanced[track_id] = template
                continue
            previous = np.asarray(
                template.capture_to_current_homography,
                dtype=np.float64,
            ).reshape(3, 3)
            combined = delta @ previous
            normalization = float(combined[2, 2])
            if (
                not np.all(np.isfinite(combined))
                or not math.isfinite(normalization)
                or abs(normalization) < 1e-9
            ):
                advanced[track_id] = _RetainedTemplate(
                    image=template.image,
                    captured_at_s=template.captured_at_s,
                    pixel_box=template.pixel_box,
                    capture_to_current_homography=template.capture_to_current_homography,
                    camera_warp_valid=False,
                )
                continue
            combined /= normalization
            advanced[track_id] = _RetainedTemplate(
                image=template.image,
                captured_at_s=template.captured_at_s,
                pixel_box=template.pixel_box,
                capture_to_current_homography=tuple(
                    float(value) for value in combined.reshape(-1)
                ),
                camera_warp_valid=True,
            )
        self._retained_templates = advanced

    def _batched_flow_hints(
        self,
        cv2: Any,
        np: Any,
        previous_gray: Any,
        gray: Any,
        tracks: tuple[UnifiedTrackSnapshot, ...],
        expected_motion: dict[str, tuple[float, float, float]],
        *,
        frame_interval_s: float,
        camera_motion: CameraMotionEstimate | None,
    ) -> dict[str, TargetMotionHint]:
        height, width = previous_gray.shape[:2]
        point_chunks: list[Any] = []
        initial_point_chunks: list[Any] = []
        slices: list[tuple[UnifiedTrackSnapshot, int, int]] = []
        point_offset = 0
        for track in tracks:
            pixel_box = self._pixel_box(track.bbox, width, height)
            if not self._box_is_usable(pixel_box):
                continue
            x1, y1, x2, y2 = pixel_box
            roi = previous_gray[y1:y2, x1:x2]
            points = cv2.goodFeaturesToTrack(
                roi,
                maxCorners=self.config.maximum_features_per_track,
                qualityLevel=self.config.flow_quality_level,
                minDistance=self.config.flow_minimum_distance_px,
                blockSize=5,
            )
            if points is None or len(points) < self.config.minimum_flow_points:
                continue
            points = points.astype(np.float32, copy=True)
            points[:, 0, 0] += x1
            points[:, 0, 1] += y1
            point_chunks.append(points)
            if camera_motion is not None and camera_motion.confidence >= 0.5:
                initial_point_chunks.append(
                    self._camera_compensated_initial_points(
                        np,
                        points,
                        track,
                        frame_interval_s=frame_interval_s,
                        camera_motion=camera_motion,
                        width=width,
                        height=height,
                    )
                )
            slices.append((track, point_offset, point_offset + len(points)))
            point_offset += len(points)
        if not point_chunks:
            return {}
        previous_points = np.concatenate(point_chunks, axis=0)
        if initial_point_chunks:
            initial_points = np.concatenate(initial_point_chunks, axis=0)
            current_points, forward_status, _ = cv2.calcOpticalFlowPyrLK(
                previous_gray,
                gray,
                previous_points,
                initial_points,
                winSize=(21, 21),
                maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
                flags=cv2.OPTFLOW_USE_INITIAL_FLOW,
            )
        else:
            current_points, forward_status, _ = cv2.calcOpticalFlowPyrLK(
                previous_gray,
                gray,
                previous_points,
                None,
                winSize=(21, 21),
                maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
            )
        if current_points is None or forward_status is None:
            return {}
        backward_points, backward_status, _ = cv2.calcOpticalFlowPyrLK(
            gray,
            previous_gray,
            current_points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
        )
        if backward_points is None or backward_status is None:
            return {}
        previous = previous_points.reshape(-1, 2)
        current = current_points.reshape(-1, 2)
        backward = backward_points.reshape(-1, 2)
        forward_valid = forward_status.reshape(-1).astype(bool)
        backward_valid = backward_status.reshape(-1).astype(bool)
        hints: dict[str, TargetMotionHint] = {}
        for track, start, end in slices:
            expected_dx, expected_dy, expected_scale = expected_motion[track.track_id]
            hint = self._flow_hint_from_points(
                np,
                previous[start:end],
                current[start:end],
                backward[start:end],
                forward_valid[start:end],
                backward_valid[start:end],
                track,
                expected_dx,
                expected_dy,
                expected_scale,
                width,
                height,
            )
            if hint is not None:
                hints[track.track_id] = hint
        return hints

    @staticmethod
    def _camera_compensated_initial_points(
        np: Any,
        points: Any,
        track: UnifiedTrackSnapshot,
        *,
        frame_interval_s: float,
        camera_motion: CameraMotionEstimate,
        width: int,
        height: int,
    ) -> Any:
        """Seed LK in the current frame using per-feature camera and target prediction."""

        previous = points.reshape(-1, 2).astype(np.float64, copy=False)
        normalized = previous.copy()
        normalized[:, 0] = normalized[:, 0] / width + track.velocity_x_s * frame_interval_s
        normalized[:, 1] = normalized[:, 1] / height + track.velocity_y_s * frame_interval_s
        homography = np.asarray(camera_motion.homography_matrix, dtype=np.float64).reshape(3, 3)
        homogeneous = np.column_stack(
            (normalized, np.ones(len(normalized), dtype=np.float64))
        )
        transformed = homogeneous @ homography.T
        denominator = transformed[:, 2]
        valid = np.isfinite(denominator) & (np.abs(denominator) >= 1e-8)
        result = previous.copy()
        if int(np.count_nonzero(valid)):
            result[valid, 0] = transformed[valid, 0] / denominator[valid] * width
            result[valid, 1] = transformed[valid, 1] / denominator[valid] * height
        result[:, 0] = np.clip(result[:, 0], 0.0, float(width - 1))
        result[:, 1] = np.clip(result[:, 1], 0.0, float(height - 1))
        return result.astype(np.float32, copy=False).reshape(-1, 1, 2)

    def _flow_hint_from_points(
        self,
        np: Any,
        previous: Any,
        current: Any,
        backward: Any,
        forward_valid: Any,
        backward_valid: Any,
        track: UnifiedTrackSnapshot,
        expected_dx: float,
        expected_dy: float,
        expected_scale: float,
        width: int,
        height: int,
    ) -> TargetMotionHint | None:
        input_point_count = len(previous)
        valid = forward_valid & backward_valid
        forward_backward_error = np.linalg.norm(previous - backward, axis=1)
        valid &= np.isfinite(forward_backward_error)
        valid &= forward_backward_error <= self.config.forward_backward_error_px
        if int(np.count_nonzero(valid)) < self.config.minimum_flow_points:
            return None
        previous = previous[valid]
        current = current[valid]
        forward_backward_error = forward_backward_error[valid]
        displacement = current - previous
        median_displacement = np.median(displacement, axis=0)
        deviation = np.linalg.norm(displacement - median_displacement, axis=1)
        median_deviation = float(np.median(deviation))
        inlier_threshold = max(2.0, 2.5 * median_deviation)
        inliers = deviation <= inlier_threshold
        if int(np.count_nonzero(inliers)) < self.config.minimum_flow_points:
            return None
        previous = previous[inliers]
        current = current[inliers]
        forward_backward_error = forward_backward_error[inliers]
        displacement = current - previous
        total_dx_px, total_dy_px = np.median(displacement, axis=0)
        residual_dx = float(total_dx_px / width - expected_dx)
        residual_dy = float(total_dy_px / height - expected_dy)
        residual_scale = self._flow_residual_scale(np, previous, current, expected_scale)
        if not self._residual_is_bounded(residual_dx, residual_dy):
            return None
        retention = len(previous) / input_point_count
        consistency = max(
            0.0,
            1.0 - float(np.median(forward_backward_error)) / self.config.forward_backward_error_px,
        )
        confidence = min(1.0, 0.45 + 0.30 * retention + 0.25 * consistency)
        return TargetMotionHint(
            track_id=track.track_id,
            residual_dx=residual_dx,
            residual_dy=residual_dy,
            residual_scale=residual_scale,
            confidence=confidence,
            source="optical_flow_fb",
        )

    def _template_hint(
        self,
        cv2: Any,
        np: Any,
        previous_gray: Any,
        gray: Any,
        track: UnifiedTrackSnapshot,
        expected_dx: float,
        expected_dy: float,
        captured_at_s: float,
        *,
        camera_motion: CameraMotionEstimate | None,
        camera_warped_previous_gray: Any | None,
    ) -> TargetMotionHint | None:
        height, width = previous_gray.shape[:2]
        x1, y1, x2, y2 = self._pixel_box(track.bbox, width, height)
        if not self._box_is_usable((x1, y1, x2, y2)):
            return None
        template = previous_gray[y1:y2, x1:x2]
        source = "template_correlation"
        retained = self._retained_templates.get(track.track_id)
        uses_retained_template = False
        if (
            track.state in {UnifiedTrackState.OCCLUDED, UnifiedTrackState.REACQUIRING}
            and retained is not None
            and 0.0
            <= captured_at_s - retained.captured_at_s
            <= self.config.maximum_retained_template_age_s
        ):
            template = retained.image
            source = "retained_template_correlation"
            uses_retained_template = True
            warped_retained = self._camera_warped_retained_template(
                cv2,
                np,
                retained,
                width=width,
                height=height,
            )
            if warped_retained is not None and warped_retained.size > 0:
                template = warped_retained
                source = "camera_warped_retained_template_correlation"
        if not uses_retained_template and camera_warped_previous_gray is not None:
            projected_box = self._camera_transformed_pixel_box(
                track.bbox,
                camera_motion,
                width,
                height,
            )
            if projected_box is not None and self._box_is_usable(projected_box):
                projected_x1, projected_y1, projected_x2, projected_y2 = projected_box
                projected_template = camera_warped_previous_gray[
                    projected_y1:projected_y2,
                    projected_x1:projected_x2,
                ]
                if projected_template.size > 0:
                    template = projected_template
                    source = "camera_warped_template_correlation"
        if (
            template.size == 0
            or float(np.std(template)) < self.config.template_minimum_standard_deviation
        ):
            return None
        previous_center_x = (x1 + x2) * 0.5
        previous_center_y = (y1 + y2) * 0.5
        expected_center_x = previous_center_x + expected_dx * width
        expected_center_y = previous_center_y + expected_dy * height
        box_width = max(x2 - x1, int(template.shape[1]))
        box_height = max(y2 - y1, int(template.shape[0]))
        search_expansion = self._search_expansion(track.state)
        search_half_width = box_width * search_expansion * 0.5
        search_half_height = box_height * search_expansion * 0.5
        search_x1 = max(0, int(math.floor(expected_center_x - search_half_width)))
        search_y1 = max(0, int(math.floor(expected_center_y - search_half_height)))
        search_x2 = min(width, int(math.ceil(expected_center_x + search_half_width)))
        search_y2 = min(height, int(math.ceil(expected_center_y + search_half_height)))
        search = gray[search_y1:search_y2, search_x1:search_x2]
        if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
            return None
        correlation = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        if correlation.size == 0 or not np.all(np.isfinite(correlation)):
            return None
        _minimum, peak, _minimum_location, peak_location = cv2.minMaxLoc(correlation)
        second_peak = self._second_peak(np, correlation, peak_location, template.shape)
        peak_margin = float(peak - second_peak)
        if (
            peak < self.config.template_minimum_correlation
            or peak_margin < self.config.template_minimum_peak_margin
        ):
            return None
        matched_center_x = search_x1 + peak_location[0] + template.shape[1] * 0.5
        matched_center_y = search_y1 + peak_location[1] + template.shape[0] * 0.5
        total_dx = (matched_center_x - previous_center_x) / width
        total_dy = (matched_center_y - previous_center_y) / height
        residual_dx = float(total_dx - expected_dx)
        residual_dy = float(total_dy - expected_dy)
        if not self._residual_is_bounded(residual_dx, residual_dy):
            return None
        confidence = min(
            1.0,
            0.55 * float(peak) + 0.45 * min(1.0, peak_margin / 0.20),
        )
        return TargetMotionHint(
            track_id=track.track_id,
            residual_dx=residual_dx,
            residual_dy=residual_dy,
            residual_scale=1.0,
            confidence=confidence,
            source=source,
        )

    @staticmethod
    def _camera_warped_previous_frame(
        cv2: Any,
        np: Any,
        previous_gray: Any,
        camera_motion: CameraMotionEstimate,
    ) -> Any | None:
        """Reproject the preceding analysis frame into the current camera coordinates."""

        height, width = previous_gray.shape[:2]
        normalized = np.asarray(camera_motion.homography_matrix, dtype=np.float64).reshape(3, 3)
        if not np.all(np.isfinite(normalized)):
            return None
        pixel_to_normalized = np.asarray(
            ((1.0 / width, 0.0, 0.0), (0.0, 1.0 / height, 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        normalized_to_pixel = np.asarray(
            ((float(width), 0.0, 0.0), (0.0, float(height), 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        pixel_homography = normalized_to_pixel @ normalized @ pixel_to_normalized
        scale = float(pixel_homography[2, 2])
        if not math.isfinite(scale) or abs(scale) < 1e-9:
            return None
        pixel_homography /= scale
        try:
            return cv2.warpPerspective(
                previous_gray,
                pixel_homography,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            )
        except (cv2.error, TypeError, ValueError):
            return None

    @staticmethod
    def _camera_warped_retained_template(
        cv2: Any,
        np: Any,
        retained: _RetainedTemplate,
        *,
        width: int,
        height: int,
    ) -> Any | None:
        """Project an older cropped appearance into the current camera frame.

        ``capture_to_current_homography`` is only advanced through consecutive,
        high-confidence camera estimates.  This preserves the existing raw
        retained-template path whenever the chain is incomplete.
        """

        if not retained.camera_warp_valid:
            return None
        x1, y1, x2, y2 = retained.pixel_box
        if (
            x2 <= x1
            or y2 <= y1
            or retained.image.ndim != 2
            or retained.image.shape[:2] != (y2 - y1, x2 - x1)
        ):
            return None
        normalized = np.asarray(
            retained.capture_to_current_homography,
            dtype=np.float64,
        ).reshape(3, 3)
        if not np.all(np.isfinite(normalized)):
            return None
        pixel_to_normalized = np.asarray(
            ((1.0 / width, 0.0, 0.0), (0.0, 1.0 / height, 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        normalized_to_pixel = np.asarray(
            ((float(width), 0.0, 0.0), (0.0, float(height), 0.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        pixel_homography = normalized_to_pixel @ normalized @ pixel_to_normalized
        normalization = float(pixel_homography[2, 2])
        if not math.isfinite(normalization) or abs(normalization) < 1e-9:
            return None
        pixel_homography /= normalization
        corners = np.asarray(
            ((x1, y1, 1.0), (x2, y1, 1.0), (x2, y2, 1.0), (x1, y2, 1.0)),
            dtype=np.float64,
        )
        projected = corners @ pixel_homography.T
        denominator = projected[:, 2]
        if not np.all(np.isfinite(denominator)) or np.any(np.abs(denominator) < 1e-8):
            return None
        projected = projected[:, :2] / denominator[:, None]
        if not np.all(np.isfinite(projected)):
            return None
        output_x1 = max(0, int(math.floor(float(np.min(projected[:, 0])))))
        output_y1 = max(0, int(math.floor(float(np.min(projected[:, 1])))))
        output_x2 = min(width, int(math.ceil(float(np.max(projected[:, 0])))))
        output_y2 = min(height, int(math.ceil(float(np.max(projected[:, 1])))))
        if output_x2 <= output_x1 or output_y2 <= output_y1:
            return None
        crop_to_source = np.asarray(
            ((1.0, 0.0, float(x1)), (0.0, 1.0, float(y1)), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        current_to_output = np.asarray(
            (
                (1.0, 0.0, -float(output_x1)),
                (0.0, 1.0, -float(output_y1)),
                (0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )
        crop_to_output = current_to_output @ pixel_homography @ crop_to_source
        try:
            return cv2.warpPerspective(
                retained.image,
                crop_to_output,
                (output_x2 - output_x1, output_y2 - output_y1),
                flags=cv2.INTER_LINEAR,
                # The crop has no pixels outside its original target extent.
                # Reflecting its texture into rotated corner triangles creates
                # a false repeated pattern and depresses the true match score.
                borderMode=cv2.BORDER_CONSTANT,
            )
        except (cv2.error, TypeError, ValueError):
            return None

    @staticmethod
    def _camera_transformed_pixel_box(
        bbox: Any,
        camera_motion: CameraMotionEstimate | None,
        width: int,
        height: int,
    ) -> tuple[int, int, int, int] | None:
        """Return the axis-aligned current-frame extent of one prior-frame box."""

        if camera_motion is None:
            return None
        projected_corners = tuple(
            camera_motion.transform_point(x, y)
            for x, y in (
                (bbox.x1, bbox.y1),
                (bbox.x2, bbox.y1),
                (bbox.x2, bbox.y2),
                (bbox.x1, bbox.y2),
            )
        )
        if not all(
            math.isfinite(value)
            for point in projected_corners
            for value in point
        ):
            return None
        minimum_x = min(point[0] for point in projected_corners)
        minimum_y = min(point[1] for point in projected_corners)
        maximum_x = max(point[0] for point in projected_corners)
        maximum_y = max(point[1] for point in projected_corners)
        return (
            max(0, min(width - 1, int(math.floor(minimum_x * width)))),
            max(0, min(height - 1, int(math.floor(minimum_y * height)))),
            max(1, min(width, int(math.ceil(maximum_x * width)))),
            max(1, min(height, int(math.ceil(maximum_y * height)))),
        )

    def _search_expansion(self, state: UnifiedTrackState) -> float:
        multiplier = 1.0
        if state is UnifiedTrackState.OCCLUDED:
            multiplier = self.config.occluded_search_multiplier
        elif state is UnifiedTrackState.REACQUIRING:
            multiplier = self.config.reacquiring_search_multiplier
        return min(
            self.config.maximum_search_expansion,
            self.config.search_expansion * multiplier,
        )

    @staticmethod
    def _expected_motion(
        track: UnifiedTrackSnapshot,
        frame_interval_s: float,
        camera_motion: CameraMotionEstimate | None,
    ) -> tuple[float, float, float]:
        track_center_x, track_center_y = track.bbox.center
        predicted_x = track_center_x + track.velocity_x_s * frame_interval_s
        predicted_y = track_center_y + track.velocity_y_s * frame_interval_s
        if camera_motion is not None and camera_motion.confidence >= 0.5:
            transformed_x, transformed_y = camera_motion.transform_point(
                predicted_x,
                predicted_y,
            )
            return (
                transformed_x - track_center_x,
                transformed_y - track_center_y,
                camera_motion.local_scale_at(predicted_x, predicted_y),
            )
        return (
            predicted_x - track_center_x,
            predicted_y - track_center_y,
            1.0,
        )

    @staticmethod
    def _flow_residual_scale(
        np: Any,
        previous: Any,
        current: Any,
        expected_scale: float,
    ) -> float:
        if len(previous) < 8:
            return 1.0
        previous_center = np.median(previous, axis=0)
        current_center = np.median(current, axis=0)
        previous_radius = np.linalg.norm(previous - previous_center, axis=1)
        current_radius = np.linalg.norm(current - current_center, axis=1)
        valid = previous_radius >= 2.0
        if int(np.count_nonzero(valid)) < 6:
            return 1.0
        observed_scale = float(np.median(current_radius[valid] / previous_radius[valid]))
        residual_scale = observed_scale / max(expected_scale, 1e-6)
        return min(1.25, max(0.8, residual_scale))

    @staticmethod
    def _second_peak(
        np: Any,
        correlation: Any,
        peak_location: tuple[int, int],
        template_shape: tuple[int, ...],
    ) -> float:
        suppressed = correlation.copy()
        radius_x = max(1, template_shape[1] // 4)
        radius_y = max(1, template_shape[0] // 4)
        x, y = peak_location
        suppressed[
            max(0, y - radius_y) : min(suppressed.shape[0], y + radius_y + 1),
            max(0, x - radius_x) : min(suppressed.shape[1], x + radius_x + 1),
        ] = -1.0
        if suppressed.size == 0:
            return -1.0
        return float(np.max(suppressed))

    def _residual_is_bounded(self, residual_dx: float, residual_dy: float) -> bool:
        return (
            math.isfinite(residual_dx)
            and math.isfinite(residual_dy)
            and abs(residual_dx) <= self.config.maximum_residual_displacement
            and abs(residual_dy) <= self.config.maximum_residual_displacement
        )

    def _box_is_usable(self, box: tuple[int, int, int, int]) -> bool:
        x1, y1, x2, y2 = box
        return (
            x2 - x1 >= self.config.minimum_box_size_px
            and y2 - y1 >= self.config.minimum_box_size_px
        )

    @staticmethod
    def _pixel_box(bbox: Any, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            max(0, min(width - 1, int(math.floor(bbox.x1 * width)))),
            max(0, min(height - 1, int(math.floor(bbox.y1 * height)))),
            max(1, min(width, int(math.ceil(bbox.x2 * width)))),
            max(1, min(height, int(math.ceil(bbox.y2 * height)))),
        )

    @staticmethod
    def _track_priority(track: UnifiedTrackSnapshot) -> tuple[int, int, float, str]:
        state_priority = {
            UnifiedTrackState.LOCKED: 0,
            UnifiedTrackState.TRACKING: 1,
            UnifiedTrackState.RECOVERED: 2,
            UnifiedTrackState.OCCLUDED: 3,
            UnifiedTrackState.REACQUIRING: 4,
            UnifiedTrackState.DETECTED: 5,
            UnifiedTrackState.LOST: 6,
        }
        return (
            0 if track.primary else 1 if track.locked else 2,
            state_priority[track.state],
            -track.tracking_quality,
            track.track_id,
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

    @staticmethod
    def _result(
        started: float,
        status: ShortTermTrackingStatus,
        *,
        hints: tuple[TargetMotionHint, ...] = (),
        attempted_track_count: int = 0,
        optical_flow_hint_count: int = 0,
        template_hint_count: int = 0,
        frame_interval_s: float | None = None,
        reason: str | None = None,
        camera_motion: CameraMotionEstimate | None = None,
        camera_motion_source: str | None = None,
        camera_motion_feature_count: int = 0,
    ) -> ShortTermTrackingResult:
        return ShortTermTrackingResult(
            status=status,
            hints=hints,
            attempted_track_count=attempted_track_count,
            optical_flow_hint_count=optical_flow_hint_count,
            template_hint_count=template_hint_count,
            processing_time_ms=(time.perf_counter() - started) * 1_000.0,
            frame_interval_s=frame_interval_s,
            reason=reason,
            camera_motion=camera_motion,
            camera_motion_source=camera_motion_source,
            camera_motion_feature_count=camera_motion_feature_count,
        )


__all__ = [
    "OpenCVShortTermTargetTracker",
    "ShortTermTrackingConfig",
    "ShortTermTrackingResult",
    "ShortTermTrackingStatus",
]

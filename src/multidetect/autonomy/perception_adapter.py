"""Truth-free RGB/depth to OnboardAI perception observations.

The adapter is deliberately shared by the Gazebo camera path and the future
Jetson camera path.  It only consumes pixels, detector output, and an optional
metric-depth payload; scenario truth never enters the decision contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    TargetImageObservation,
    _camera_offset_ned,
    _target_rays,
)
from .onboard_task_state import OnboardTaskAction
from .realtime_bridge import OnboardPerceptionObservation


@dataclass(frozen=True, slots=True)
class FramePerceptionResult:
    """Auditable detector/color/depth result for one camera frame."""

    observation: OnboardPerceptionObservation
    detector_label: str | None
    detector_confidence: float
    detector_count: int
    color_label: str | None
    color_confidence: float
    distance_m: float | None
    depth_payload_sha256: str | None
    nearest_observed: bool = False
    candidate_distances_m: tuple[float, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        return {
            "observation": self.observation.to_mapping(),
            "detector_label": self.detector_label,
            "detector_confidence": self.detector_confidence,
            "detector_count": self.detector_count,
            "color_label": self.color_label,
            "color_confidence": self.color_confidence,
            "distance_m": self.distance_m,
            "depth_payload_sha256": self.depth_payload_sha256,
            "nearest_observed": self.nearest_observed,
            "candidate_distances_m": list(self.candidate_distances_m),
            "truth_free": True,
        }


@dataclass(frozen=True, slots=True)
class FrameCameraPose:
    """Camera calibration and aircraft pose captured for one sensor frame.

    The timestamp and frame hash are part of the pose contract, not optional
    metadata.  This prevents a live/latest attitude or GPS sample from being
    silently paired with an older RGB/depth frame.  The calibration contains
    the camera intrinsics, mount Euler angles and camera lever arm; the pose
    contains the full roll/pitch/yaw attitude.
    """

    calibration: CameraCalibration
    aircraft_pose: AircraftPose
    source_frame_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.calibration, CameraCalibration):
            raise TypeError("calibration must be CameraCalibration")
        if not isinstance(self.aircraft_pose, AircraftPose):
            raise TypeError("aircraft_pose must be AircraftPose")
        if (
            not isinstance(self.source_frame_sha256, str)
            or len(self.source_frame_sha256) != 64
            or any(
                character not in "0123456789abcdefABCDEF" for character in self.source_frame_sha256
            )
        ):
            raise ValueError("source_frame_sha256 must be a 64-character hexadecimal digest")

    @property
    def captured_at_s(self) -> float:
        return self.aircraft_pose.captured_at_s


class OnboardFramePerceptionAdapter:
    """Convert a detector result plus optional depth into the shared event shape."""

    def __init__(
        self,
        detector: Any,
        *,
        target_class: str = "vehicle",
        target_color: str | None = None,
        fx_px: float | None = None,
        cx_px: float | None = None,
        minimum_detection_confidence: float = 0.25,
    ) -> None:
        if not callable(getattr(detector, "detect", None)):
            raise TypeError("detector must expose detect(image_bgr)")
        if target_class not in {"vehicle", "car", "person", "aircraft"}:
            raise ValueError("unsupported target_class")
        if target_color not in {None, "red", "green", "blue", "yellow", "unknown"}:
            raise ValueError("unsupported target_color")
        for name, value in (("fx_px", fx_px), ("cx_px", cx_px)):
            if value is not None and (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if (
            isinstance(minimum_detection_confidence, bool)
            or not isinstance(minimum_detection_confidence, (int, float))
            or not math.isfinite(float(minimum_detection_confidence))
            or not 0.0 <= float(minimum_detection_confidence) <= 1.0
        ):
            raise ValueError("minimum_detection_confidence must be finite within [0, 1]")
        self.detector = detector
        self.target_class = target_class
        self.target_color = target_color
        self.fx_px = float(fx_px) if fx_px is not None else None
        self.cx_px = float(cx_px) if cx_px is not None else None
        self.minimum_detection_confidence = float(minimum_detection_confidence)
        self._match_confirmation_pending = False
        self._match_confirmation_count = 0
        self._last_match_bbox: tuple[float, float, float, float] | None = None
        self._last_match_observed_at_s: float | None = None
        self._last_match_observation_id: str | None = None
        self._last_match_source_frame_sha256: str | None = None

    def observe(
        self,
        *,
        observation_id: str,
        observed_at_s: float,
        image_bgr: Any,
        source_frame_sha256: str | None = None,
        depth_frame: Any | None = None,
        target_north_offset_m: float | None = None,
        target_east_offset_m: float | None = None,
        aircraft_heading_deg: float | None = None,
        camera_pose: FrameCameraPose | None = None,
    ) -> FramePerceptionResult:
        if (target_north_offset_m is None) != (target_east_offset_m is None):
            raise ValueError(
                "target_north_offset_m and target_east_offset_m must be supplied together"
            )
        if aircraft_heading_deg is not None and (
            not isinstance(aircraft_heading_deg, (int, float))
            or not math.isfinite(float(aircraft_heading_deg))
            or not 0.0 <= float(aircraft_heading_deg) < 360.0
        ):
            raise ValueError("aircraft_heading_deg must be finite within [0, 360)")
        if camera_pose is not None and not isinstance(camera_pose, FrameCameraPose):
            raise TypeError("camera_pose must be FrameCameraPose")
        if camera_pose is not None:
            if camera_pose.captured_at_s != float(observed_at_s):
                raise ValueError("camera_pose timestamp must exactly match observed_at_s")
            if source_frame_sha256 != camera_pose.source_frame_sha256:
                raise ValueError("camera_pose source hash must match source_frame_sha256")
        np = self._numpy()
        image = np.asarray(image_bgr)
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("image_bgr must have shape HxWx3")
        height, width = int(image.shape[0]), int(image.shape[1])
        detections = tuple(self.detector.detect(image))
        matching = tuple(item for item in detections if self._class_matches(item.label))
        depth_hash = str(depth_frame.payload_sha256) if depth_frame is not None else None
        if not matching:
            self._match_confirmation_pending = False
            self._match_confirmation_count = 0
            self._last_match_bbox = None
            self._last_match_observed_at_s = None
            self._last_match_observation_id = None
            self._last_match_source_frame_sha256 = None
            observation = OnboardPerceptionObservation(
                observation_id=observation_id,
                observed_at_s=observed_at_s,
                source="rgb-detector-tracker",
                target_visible=False,
                target_class=None,
                target_color=None,
                target_class_match=False,
                target_color_match=False,
                target_confidence=0.0,
                target_match_score=0.0,
                obstacle_distance_m=5_000.0,
                task_phase=OnboardTaskAction.SEARCH,
                detail="no target-class detection in RGB frame",
                source_frame_sha256=source_frame_sha256,
            )
            return FramePerceptionResult(
                observation=observation,
                detector_label=None,
                detector_confidence=0.0,
                detector_count=len(detections),
                color_label=None,
                color_confidence=0.0,
                distance_m=None,
                depth_payload_sha256=depth_hash,
                nearest_observed=False,
            )

        # Evaluate every target-class candidate.  A task without a colour
        # constraint considers every class-matched candidate; a constrained
        # task filters by the measured colour before applying the nearest
        # metric-range rule.  Colour is therefore a task attribute, not a
        # baked-in model identity.
        evaluated: list[tuple[Any, str | None, float, float | None, tuple[int, int, int, int]]] = []
        for candidate in matching:
            bbox = candidate.bbox
            candidate_x1 = max(0, min(width - 1, int(float(bbox.x1) * width)))
            candidate_y1 = max(0, min(height - 1, int(float(bbox.y1) * height)))
            candidate_x2 = max(
                candidate_x1 + 1,
                min(width, int(math.ceil(float(bbox.x2) * width))),
            )
            candidate_y2 = max(
                candidate_y1 + 1,
                min(height, int(math.ceil(float(bbox.y2) * height))),
            )
            candidate_color, candidate_color_confidence = self._classify_color(
                image[candidate_y1:candidate_y2, candidate_x1:candidate_x2]
            )
            candidate_distance = self._sample_depth(
                depth_frame,
                candidate_x1,
                candidate_y1,
                candidate_x2,
                candidate_y2,
                width,
                height,
            )
            evaluated.append(
                (
                    candidate,
                    candidate_color,
                    candidate_color_confidence,
                    candidate_distance,
                    (candidate_x1, candidate_y1, candidate_x2, candidate_y2),
                )
            )
        color_constrained = self.target_color not in {None, "unknown"}
        matching_color = (
            [item for item in evaluated if item[1] == self.target_color]
            if color_constrained
            else evaluated
        )
        with_valid_range = [item for item in matching_color if item[3] is not None]
        if with_valid_range:
            selected, color, color_confidence, distance_m, (x1, y1, x2, y2) = min(
                with_valid_range, key=lambda item: float(item[3])
            )
            # If one matching-colour candidate has no metric range, this is
            # only the nearest *known* target, not proof that it is nearest.
            # Keep the range for diagnostics but do not permit GPS projection
            # or a nearest-target claim from incomplete evidence.
            nearest_observed = len(with_valid_range) == len(matching_color)
        elif matching_color:
            selected, color, color_confidence, distance_m, (x1, y1, x2, y2) = max(
                matching_color, key=lambda item: float(item[0].confidence)
            )
            nearest_observed = False
        else:
            selected, color, color_confidence, distance_m, (x1, y1, x2, y2) = max(
                evaluated, key=lambda item: float(item[0].confidence)
            )
            nearest_observed = False
        class_match = self._class_matches(selected.label)
        color_match: bool | None = color == self.target_color if color_constrained else None
        attribute_match = class_match and (not color_constrained or color_match is True)
        confidence = float(selected.confidence)
        # Keep detector confidence and target-attribute agreement separate.
        # Aerial targets can occupy only a few pixels, so a low detector score
        # does not by itself invalidate an otherwise explicit class/color
        # match.  ``target_confidence`` carries the detector score; the task
        # state gate consumes this semantic attribute score and still requires
        # the class, color, calibrated range and GPS gates below.
        detector_confidence_eligible = confidence >= self.minimum_detection_confidence
        match_score = (
            (
                color_confidence
                if color_constrained and color is not None
                else confidence
            )
            if detector_confidence_eligible and attribute_match
            else 0.0
        )
        distance_m = self._sample_depth(depth_frame, x1, y1, x2, y2, width, height)
        # Without a calibrated pose this is only an image/camera-relative
        # bearing (positive means right in the image).  It must not be fed to
        # the NED navigation head as though it were an absolute heading.
        bearing_deg = self._bearing_deg((x1 + x2) / 2.0, width)
        geometry_frame = "camera_relative"
        if (
            target_north_offset_m is None
            and target_east_offset_m is None
            and distance_m is not None
            and nearest_observed
            and detector_confidence_eligible
            and class_match
            and attribute_match
            and camera_pose is not None
        ):
            if (
                camera_pose.calibration.width_px != width
                or camera_pose.calibration.height_px != height
            ):
                raise ValueError("camera calibration dimensions must match the RGB frame")
            target = TargetImageObservation(
                target_id=observation_id,
                frame_id=observation_id,
                captured_at_s=float(observed_at_s),
                center_x=((x1 + x2) / 2.0) / width,
                center_y=((y1 + y2) / 2.0) / height,
            )
            _, ray_ned = _target_rays(camera_pose.calibration, camera_pose.aircraft_pose, target)
            camera_offset_ned = _camera_offset_ned(
                camera_pose.calibration,
                camera_pose.aircraft_pose,
            )
            target_north_offset_m = camera_offset_ned[0] + distance_m * ray_ned[0]
            target_east_offset_m = camera_offset_ned[1] + distance_m * ray_ned[1]
        if target_north_offset_m is not None and target_east_offset_m is not None:
            horizontal_distance_m = math.hypot(target_north_offset_m, target_east_offset_m)
            # The calibrated NED projection is the canonical geometry used by
            # the model and GPS recorder.  Keep the raw slant range in the
            # FramePerceptionResult, but expose horizontal NED range/bearing
            # to the shared OnboardAI event so the teacher and runtime use the
            # same coordinate frame.
            if horizontal_distance_m > 1.0e-6:
                bearing_deg = math.degrees(
                    math.atan2(target_east_offset_m, target_north_offset_m)
                )
                distance_m = horizontal_distance_m
            geometry_frame = "ned"
        canonical_label = self._canonical_label(selected.label)
        if not attribute_match or not detector_confidence_eligible:
            self._match_confirmation_pending = False
            self._match_confirmation_count = 0
            self._last_match_observation_id = None
            self._last_match_source_frame_sha256 = None
            phase = OnboardTaskAction.SEARCH
        else:
            # Keep the lifecycle on the same model path as the Jetson loop:
            # first matching frame = SEARCH evidence, second consecutive
            # same-track frame = MATCH_TARGET, third consecutive frame =
            # RECORD_LOCATION proposal.  A single low-confidence detector
            # result therefore cannot jump directly to GPS/RTL.
            current_bbox = tuple(float(value) for value in selected.bbox.rounded(6))
            same_track = (
                self._match_confirmation_pending
                and self._last_match_bbox is not None
                and self._bbox_iou(self._last_match_bbox, current_bbox) >= 0.30
                and self._last_match_observed_at_s is not None
                and 0.0 < float(observed_at_s) - self._last_match_observed_at_s <= 1.0
                and self._last_match_observation_id != observation_id
            )
            if same_track:
                self._match_confirmation_count = min(3, self._match_confirmation_count + 1)
            else:
                self._match_confirmation_count = 1
            # A frame identity is mandatory before confirmation.  The first
            # frame can be a useful SEARCH candidate, while MATCH_TARGET and
            # RECORD_LOCATION are reserved for distinct, time-ordered sensor
            # records.  Equal payload hashes are valid for a stationary target.
            if not isinstance(source_frame_sha256, str) or len(source_frame_sha256) != 64:
                self._match_confirmation_count = 1
            phase = {
                1: OnboardTaskAction.SEARCH,
                2: OnboardTaskAction.MATCH_TARGET,
            }.get(self._match_confirmation_count, OnboardTaskAction.RECORD_LOCATION)
            self._match_confirmation_pending = True
            self._last_match_bbox = current_bbox
            self._last_match_observed_at_s = float(observed_at_s)
            self._last_match_observation_id = observation_id
            self._last_match_source_frame_sha256 = source_frame_sha256
        detail = (
            f"{canonical_label} detection color={color or 'unknown'} "
            f"target_color={self.target_color or 'unconstrained'}; "
            f"nearest_observed={str(nearest_observed).lower()}"
        )
        observation = OnboardPerceptionObservation(
            observation_id=observation_id,
            observed_at_s=observed_at_s,
            source="rgb-detector-tracker",
            target_visible=True,
            target_class=canonical_label,
            target_color=color,
            target_class_match=class_match,
            target_color_match=color_match,
            target_bearing_deg=bearing_deg,
            target_geometry_frame=geometry_frame,
            target_distance_m=distance_m,
            target_confidence=confidence,
            target_match_score=match_score,
            target_north_offset_m=target_north_offset_m,
            target_east_offset_m=target_east_offset_m,
            obstacle_distance_m=5_000.0,
            task_phase=phase,
            detail=detail,
            source_frame_sha256=source_frame_sha256,
        )
        return FramePerceptionResult(
            observation=observation,
            detector_label=str(selected.label),
            detector_confidence=confidence,
            detector_count=len(detections),
            color_label=color,
            color_confidence=color_confidence,
            distance_m=distance_m,
            depth_payload_sha256=depth_hash,
            nearest_observed=nearest_observed,
            candidate_distances_m=tuple(
                sorted(float(item[3]) for item in with_valid_range if item[3] is not None)
            ),
        )

    @staticmethod
    def _bbox_iou(
        left: tuple[float, float, float, float],
        right: tuple[float, float, float, float],
    ) -> float:
        lx1, ly1, lx2, ly2 = left
        rx1, ry1, rx2, ry2 = right
        intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
            0.0, min(ly2, ry2) - max(ly1, ry1)
        )
        left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
        right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
        union = left_area + right_area - intersection
        return intersection / union if union > 0.0 else 0.0

    def _class_matches(self, label: object) -> bool:
        canonical = self._canonical_label(str(label))
        return canonical == self._canonical_label(self.target_class)

    @staticmethod
    def _canonical_label(label: str) -> str:
        return {"car": "vehicle", "truck": "vehicle", "bus": "vehicle"}.get(label, label)

    def _bearing_deg(self, center_x_px: float, width: int) -> float | None:
        if self.fx_px is None:
            return None
        cx_px = self.cx_px if self.cx_px is not None else width / 2.0
        return math.degrees(math.atan2(center_x_px - cx_px, self.fx_px))

    @staticmethod
    def _classify_color(crop: Any) -> tuple[str | None, float]:
        np = OnboardFramePerceptionAdapter._numpy()
        pixels = np.asarray(crop, dtype=np.float32).reshape(-1, 3)
        if pixels.size == 0:
            return None, 0.0
        # A detector box contains road, sky, and shadow around the object.  A
        # full-box mean therefore dilutes the object's chroma and can turn a
        # clearly coloured vehicle into ``unknown``.  Keep pixels with useful
        # brightness and chroma first; fall back to the complete crop when the
        # frame is genuinely low-light/low-saturation.
        brightness = pixels.max(axis=1)
        chroma = brightness - pixels.min(axis=1)
        foreground = pixels[(brightness >= 30.0) & (chroma >= 20.0)]
        if foreground.shape[0] >= max(4, int(pixels.shape[0] * 0.05)):
            pixels = foreground
        b, g, r = (float(value) for value in pixels.mean(axis=0))
        total = max(1.0, r + g + b)
        scores = {
            "red": max(0.0, (r - max(g, b)) / total),
            "green": max(0.0, (g - max(r, b)) / total),
            "blue": max(0.0, (b - max(r, g)) / total),
            "yellow": max(0.0, (min(r, g) - b) / total),
        }
        label, score = max(scores.items(), key=lambda item: item[1])
        confidence = min(1.0, score * 4.0)
        if confidence < 0.08:
            return "unknown", confidence
        return label, confidence

    @staticmethod
    def _sample_depth(
        depth_frame: Any | None,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        width: int,
        height: int,
    ) -> float | None:
        if depth_frame is None:
            return None
        np = OnboardFramePerceptionAdapter._numpy()
        frame_width = int(getattr(depth_frame, "width", 0))
        frame_height = int(getattr(depth_frame, "height", 0))
        if (frame_width, frame_height) != (width, height):
            return None
        encoding = str(getattr(depth_frame, "encoding", ""))
        if encoding not in {"UINT16_MM", "FLOAT32_M"}:
            return None
        bytes_per_pixel = 2 if encoding == "UINT16_MM" else 4
        if (
            int(getattr(depth_frame, "stride_bytes", width * bytes_per_pixel))
            != width * bytes_per_pixel
        ):
            return None
        dtype = np.dtype("<u2") if encoding == "UINT16_MM" else np.dtype("<f4")
        try:
            values = np.frombuffer(depth_frame.payload, dtype=dtype).reshape(
                (frame_height, frame_width)
            )
        except (AttributeError, ValueError):
            return None
        sample = values[y1:y2, x1:x2].astype(np.float32)
        if encoding == "UINT16_MM":
            sample /= 1_000.0
        distance_semantics = str(getattr(depth_frame, "distance_semantics", "ray_slant_m"))
        if distance_semantics == "optical_axis_z_m":
            fx_px = getattr(depth_frame, "fx_px", None)
            fy_px = getattr(depth_frame, "fy_px", None)
            cx_px = getattr(depth_frame, "cx_px", None)
            cy_px = getattr(depth_frame, "cy_px", None)
            if any(value is None for value in (fx_px, fy_px, cx_px, cy_px)):
                return None
            columns = np.arange(x1, x2, dtype=np.float32) + 0.5
            rows = np.arange(y1, y2, dtype=np.float32) + 0.5
            ray_x = (columns - float(cx_px)) / float(fx_px)
            ray_y = (rows - float(cy_px)) / float(fy_px)
            sample *= np.sqrt(1.0 + ray_y[:, None] ** 2 + ray_x[None, :] ** 2)
        elif distance_semantics != "ray_slant_m":
            return None
        sample = sample[np.isfinite(sample)]
        minimum = float(getattr(depth_frame, "minimum_depth_m", 0.4))
        maximum = float(getattr(depth_frame, "maximum_depth_m", 800.0))
        sample = sample[(sample >= minimum) & (sample <= maximum) & (sample > 0.0)]
        return float(np.median(sample)) if sample.size else None

    @staticmethod
    def _numpy() -> Any:
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - optional vision dependency
            raise RuntimeError("numpy is required for RGB/depth perception") from exc
        return np


__all__ = ["FrameCameraPose", "FramePerceptionResult", "OnboardFramePerceptionAdapter"]

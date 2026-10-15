"""Calibrated real-camera ground rays using the existing ranging geometry."""

from __future__ import annotations

from typing import Any

from multidetect.multimodal_ranging import (
    AircraftPose,
    MultiModalRangingEngine,
    TargetImageObservation,
    _camera_offset_ned,
)
from multidetect.multimodal_ranging import CameraCalibration as RangingCalibration

from .camera import CameraCalibration


class GroundProjector:
    """Intersect a surveyed camera ray with a local horizontal ground plane."""

    def __init__(self, calibration: CameraCalibration, *, ground_down_m: float = 0) -> None:
        mount = calibration.extrinsics
        required = (
            "mount_pitch_down_deg", "mount_yaw_right_deg", "mount_roll_clockwise_deg",
            "camera_offset_forward_m", "camera_offset_right_m", "camera_offset_down_m",
        )
        missing = set(required) - mount.keys()
        if missing:
            raise ValueError(f"real camera extrinsics missing: {', '.join(sorted(missing))}")
        if len(calibration.distortion) not in {0, 5}:
            raise ValueError("ground projection supports five Brown-Conrady distortion values")
        distortion = dict(zip(
            ("k1", "k2", "p1", "p2", "k3"), calibration.distortion or (0,) * 5, strict=True,
        ))
        self.calibration = RangingCalibration(
            calibration_id="onboard-runtime", width_px=calibration.width,
            height_px=calibration.height, fx_px=calibration.fx, fy_px=calibration.fy,
            cx_px=calibration.cx, cy_px=calibration.cy,
            **{name: float(mount[name]) for name in required}, **distortion,
        )
        self.ground_down_m = ground_down_m

    def __call__(
        self, u: float, v: float, observation: dict[str, Any],
    ) -> tuple[float, float] | None:
        attitude = observation["attitude"]
        pose = AircraftPose(
            captured_at_s=0, roll_deg=attitude["roll_deg"], pitch_deg=attitude["pitch_deg"],
            heading_deg=attitude["yaw_deg"],
        )
        target = TargetImageObservation(
            target_id="pixel", frame_id="latest", captured_at_s=0,
            center_x=u / self.calibration.width_px, center_y=v / self.calibration.height_px,
        )
        ray = MultiModalRangingEngine.target_ray_ned(
            calibration=self.calibration, pose=pose, target=target,
        )
        offset = _camera_offset_ned(self.calibration, pose)
        position = observation["pose"]["local_ned_m"]
        centre = tuple(position[i] + offset[i] for i in range(3))
        height = self.ground_down_m - centre[2]
        if ray[2] <= 1e-6 or height <= 0:
            return None
        scale = height / ray[2]
        return centre[0] + scale * ray[0], centre[1] + scale * ray[1]

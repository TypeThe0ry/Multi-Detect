"""Camera render and RTSP defaults for leased Polaris SITL sessions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CameraProfile:
    name: str
    sensor_type: str
    camera_width: int
    camera_height: int
    camera_fps: int
    video_width: int
    video_height: int
    video_fps: int

    @property
    def overrides_model(self) -> bool:
        return self.name != "hd" or self.sensor_type != "camera"

    def video_settings(self) -> dict[str, int]:
        return {
            "width": self.video_width,
            "height": self.video_height,
            "fps": self.video_fps,
        }


CAMERA_PROFILES = {
    "lite": CameraProfile("lite", "rgbd_camera", 640, 480, 10, 640, 480, 10),
    "standard": CameraProfile("standard", "rgbd_camera", 960, 720, 15, 960, 720, 15),
    "hd": CameraProfile("hd", "rgbd_camera", 1280, 960, 30, 1280, 720, 20),
}


def get_camera_profile(name: str) -> CameraProfile:
    try:
        return CAMERA_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"camera profile must be one of: {', '.join(CAMERA_PROFILES)}") from exc


__all__ = ["CAMERA_PROFILES", "CameraProfile", "get_camera_profile"]

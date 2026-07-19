from __future__ import annotations

from pathlib import Path

from multidetect.video_evidence import probe_video_evidence, video_evidence_document


class _Image:
    def __init__(self, height: int, width: int) -> None:
        self.shape = (height, width, 3)


class _Capture:
    def __init__(
        self,
        frames: list[_Image],
        *,
        fps: float,
        declared_frames: float,
        opened: bool = True,
    ) -> None:
        self.frames = frames
        self.fps = fps
        self.declared_frames = declared_frames
        self.opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def get(self, property_id: int) -> float:
        if property_id == _CV2.CAP_PROP_FRAME_COUNT:
            return self.declared_frames
        if property_id == _CV2.CAP_PROP_FPS:
            return self.fps
        return 0.0

    def read(self):
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self) -> None:
        self.released = True


class _CV2:
    CAP_ANY = 0
    CAP_PROP_FRAME_COUNT = 1
    CAP_PROP_FPS = 2

    def __init__(self, capture: _Capture) -> None:
        self.capture = capture

    def VideoCapture(self, _path: str, _backend: int) -> _Capture:
        return self.capture


def test_video_evidence_probe_decodes_every_frame_and_reports_metadata(tmp_path: Path) -> None:
    path = tmp_path / "recording.avi"
    path.write_bytes(b"video-placeholder")
    capture = _Capture([_Image(480, 640) for _ in range(5)], fps=10.0, declared_frames=5)

    probe = probe_video_evidence(path, cv2_module=_CV2(capture))

    assert probe.passed is True
    assert probe.decoded_frame_count == 5
    assert probe.declared_frame_count == 5
    assert probe.fps == 10.0
    assert probe.width == 640
    assert probe.height == 480
    assert probe.duration_s == 0.5
    assert probe.full_frame_scan_completed is True
    assert capture.released is True
    assert video_evidence_document(probe)["failure_reasons"] == []


def test_video_evidence_probe_rejects_bad_fps_dimensions_and_truncation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "broken.avi"
    path.write_bytes(b"broken-video")
    capture = _Capture(
        [_Image(480, 640), _Image(720, 1280)],
        fps=0.0,
        declared_frames=100,
    )

    probe = probe_video_evidence(path, cv2_module=_CV2(capture))

    assert probe.passed is False
    assert probe.full_frame_scan_completed is False
    assert probe.stable_dimensions is False
    assert "FPS metadata" in " ".join(probe.failure_reasons)
    assert "dimensions" in " ".join(probe.failure_reasons)
    assert "declared frame count" in " ".join(probe.failure_reasons)


def test_video_evidence_probe_rejects_unopenable_video(tmp_path: Path) -> None:
    path = tmp_path / "unopenable.avi"
    path.write_bytes(b"not-video")
    capture = _Capture([], fps=0.0, declared_frames=0.0, opened=False)

    probe = probe_video_evidence(path, cv2_module=_CV2(capture))

    assert probe.passed is False
    assert probe.decoded_frame_count == 0
    assert probe.failure_reasons == ("source video could not be opened by OpenCV",)
    assert capture.released is True

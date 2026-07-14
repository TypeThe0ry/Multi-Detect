from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
from multidetect.camera_bench import CameraBenchConfig, run_camera_bench
from multidetect.cli import main
from multidetect.compat import UTC
from multidetect.vision import CameraReadError, CaptureConfig

OBSERVED_AT = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)


class _StepClock:
    def __init__(self, step: float = 0.01) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class _Source:
    reconnect_count = 2

    def __init__(self, sizes: list[tuple[int, int]]) -> None:
        self.sizes = sizes
        self.index = 0

    def read(self):
        if self.index >= len(self.sizes):
            raise CameraReadError("secret backend detail")
        width, height = self.sizes[self.index]
        self.index += 1
        return SimpleNamespace(width=width, height=height)


def _observed_at() -> datetime:
    return OBSERVED_AT


def test_rtsp_bench_passes_with_stable_frames_without_recording_credentials() -> None:
    secret = "rtsp://SECRET_USER:SECRET_PASSWORD@camera.invalid/live"
    source = _Source([(1280, 720)] * 3)

    result = run_camera_bench(
        source,
        CaptureConfig(secret),
        CameraBenchConfig(
            minimum_frames=3,
            minimum_duration_seconds=0,
            maximum_duration_seconds=1,
        ),
        clock=_StepClock(),
        observed_at=_observed_at,
    )

    encoded = json.dumps(result)
    assert result["event"] == "rtsp_camera_bench_passed"
    assert result["passed"] is True
    assert result["processed_frames"] == 3
    assert result["resolution_stable"] is True
    assert result["reconnect_count"] == 2
    assert result["credentials_recorded"] is False
    assert "SECRET_USER" not in encoded
    assert "SECRET_PASSWORD" not in encoded


def test_camera_bench_fails_closed_when_resolution_changes() -> None:
    result = run_camera_bench(
        _Source([(1280, 720), (640, 480), (640, 480)]),
        CaptureConfig("rtsp://camera.invalid/live"),
        CameraBenchConfig(
            minimum_frames=3,
            minimum_duration_seconds=0,
            maximum_duration_seconds=1,
        ),
        clock=_StepClock(),
        observed_at=_observed_at,
    )

    assert result["event"] == "rtsp_camera_bench_failed"
    assert result["passed"] is False
    assert result["resolution_stable"] is False
    assert result["reasons"] == ["camera resolution changed during the bench"]


def test_camera_bench_redacts_read_failure_and_reports_no_hardware_observation() -> None:
    secret = "SECRET_PASSWORD_MUST_NOT_APPEAR"

    class _FailingSource:
        reconnect_count = 3

        def read(self):
            raise CameraReadError(f"rtsp://user:{secret}@camera.invalid/live")

    result = run_camera_bench(
        _FailingSource(),
        CaptureConfig("rtsp://user:another-secret@camera.invalid/live"),
        CameraBenchConfig(
            minimum_frames=1,
            minimum_duration_seconds=0,
            maximum_duration_seconds=1,
        ),
        clock=_StepClock(),
        observed_at=_observed_at,
    )

    encoded = json.dumps(result)
    assert result["passed"] is False
    assert result["hardware_observed"] is False
    assert result["capture_read_failures"] == 1
    assert result["reasons"] == ["camera read failed after configured reconnect attempts"]
    assert secret not in encoded
    assert "another-secret" not in encoded


def test_camera_bench_deadline_fails_when_minimum_frames_are_not_reached() -> None:
    result = run_camera_bench(
        _Source([(640, 480)] * 20),
        CaptureConfig(0),
        CameraBenchConfig(
            minimum_frames=100,
            minimum_duration_seconds=0.1,
            maximum_duration_seconds=0.2,
        ),
        clock=_StepClock(step=0.05),
        observed_at=_observed_at,
    )

    assert result["event"] == "local_camera_bench_failed"
    assert result["passed"] is False
    assert "minimum frame count was not reached before the deadline" in result["reasons"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"minimum_frames": 0}, "minimum frames"),
        ({"minimum_duration_seconds": -1}, "minimum duration"),
        (
            {"minimum_duration_seconds": 2, "maximum_duration_seconds": 2},
            "maximum duration",
        ),
    ],
)
def test_camera_bench_config_rejects_invalid_limits(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CameraBenchConfig(**kwargs)


def test_camera_bench_cli_writes_redacted_evidence_from_source_env(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    secret_uri = "rtsp://SECRET_USER:SECRET_PASSWORD@camera.invalid/live"
    received = {}

    class _CliSource:
        def __init__(self, config) -> None:
            received["source"] = config.source

        def close(self) -> None:
            received["closed"] = True

    def fake_run(source, capture_config, bench_config):
        del source
        received["minimum_frames"] = bench_config.minimum_frames
        received["is_rtsp"] = capture_config.is_rtsp
        return {
            "event": "rtsp_camera_bench_passed",
            "passed": True,
            "credentials_recorded": False,
        }

    output = tmp_path / "rtsp-evidence.json"
    monkeypatch.setenv("MULTIDETECT_TEST_RTSP", secret_uri)
    monkeypatch.setattr(cli_module, "OpenCVFrameSource", _CliSource)
    monkeypatch.setattr(cli_module, "run_camera_bench", fake_run)

    assert (
        main(
            [
                "camera-bench",
                "--source-env",
                "MULTIDETECT_TEST_RTSP",
                "--minimum-frames",
                "3",
                "--minimum-duration-seconds",
                "0",
                "--maximum-duration-seconds",
                "1",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert received == {
        "source": secret_uri,
        "minimum_frames": 3,
        "is_rtsp": True,
        "closed": True,
    }
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert "SECRET_USER" not in captured.out + captured.err + output.read_text(encoding="utf-8")
    assert "SECRET_PASSWORD" not in captured.out + captured.err + output.read_text(encoding="utf-8")

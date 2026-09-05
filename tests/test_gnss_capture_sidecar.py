from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.backends import RawReplayTelemetrySample
from multidetect.gnss_capture import capture_gnss_samples, write_gnss_capture_report
from multidetect.gnss_capture_sidecar import create_gnss_replay_sidecar
from multidetect.replay_telemetry_join import join_raw_replay_telemetry
from multidetect.video_replay_recording import create_video_replay_recording

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        self.value += 0.1
        return self.value

    def epoch(self) -> float:
        return 1_800_000_000.0 + self.value


class _Message:
    def __init__(self, message_type: str, values: dict[str, object]) -> None:
        self.message_type = message_type
        self.values = values

    def get_type(self) -> str:
        return self.message_type

    def to_dict(self) -> dict[str, object]:
        return dict(self.values)


class _Connection:
    def __init__(self, messages: list[_Message]) -> None:
        self.messages = list(messages)

    def recv_match(self, *, blocking: bool, timeout: float) -> _Message | None:
        del blocking, timeout
        return self.messages.pop(0) if self.messages else None

    def close(self) -> None:
        return None


class _Encoded:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def tobytes(self) -> bytes:
        return self.payload


class _VideoCapture:
    def __init__(self) -> None:
        self.frames = [SimpleNamespace(index=index, ndim=3, shape=(2, 3, 3)) for index in range(3)]
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, property_id: int) -> float:
        assert property_id == _FakeCv2.CAP_PROP_FPS
        return 5.0

    def read(self) -> tuple[bool, object | None]:
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self) -> None:
        self.released = True


class _FakeCv2:
    CAP_PROP_FPS = 5

    def __init__(self, capture: _VideoCapture) -> None:
        self.capture = capture

    def VideoCapture(self, path: str) -> _VideoCapture:
        assert path.endswith("camera.video")
        return self.capture

    @staticmethod
    def imencode(extension: str, image: object) -> tuple[bool, _Encoded]:
        assert extension == ".png"
        return True, _Encoded(f"frame-{image.index}".encode("ascii"))


def _raw_message() -> _Message:
    return _Message(
        "GPS_RAW_INT",
        {
            "time_usec": 12_345_678,
            "fix_type": 3,
            "satellites_visible": 12,
            "lat": 13_000_000,
            "lon": 103_800_000,
            "alt": 15_000,
            "eph": 120,
            "epv": 250,
            "vel": 325,
            "cog": 9_000,
        },
    )


def _capture_report(tmp_path: Path, *, message: _Message | None = None) -> Path:
    clock = _Clock()
    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.4,
        connection_factory=lambda *_args, **_kwargs: _Connection([message or _raw_message()]),
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
    )
    path = tmp_path / "gnss-capture.json"
    write_gnss_capture_report(path, report)
    return path


def test_converter_verifies_capture_and_emits_complete_sidecar(tmp_path: Path) -> None:
    capture_path = _capture_report(tmp_path)
    output = tmp_path / "telemetry.jsonl"

    summary = create_gnss_replay_sidecar(capture_path, output)

    payload = output.read_bytes()
    assert summary["raw_gps_sample_count"] == 1
    assert summary["qualified_raw_gps_sample_count"] == 1
    assert summary["global_position_promoted"] is False
    assert summary["gate_passed"] is False
    assert summary["output_sha256"] == hashlib.sha256(payload).hexdigest()
    sample = RawReplayTelemetrySample.from_mapping(json.loads(output.read_text(encoding="utf-8")))
    assert sample.observed_at_s == pytest.approx(0.0)
    assert sample.telemetry.position_healthy is True
    assert sample.telemetry.latitude_deg == pytest.approx(1.3)
    assert sample.telemetry.longitude_deg == pytest.approx(10.38)
    assert sample.telemetry.gps_absolute_altitude_m == pytest.approx(15.0)
    assert sample.telemetry.altitude_agl_m != sample.telemetry.altitude_agl_m
    assert sample.telemetry.in_allowed_zone is None
    assert sample.telemetry.flight_mode_allows_deploy is False


def test_converter_normalizes_origin_and_offset_without_interpolation(tmp_path: Path) -> None:
    capture_path = _capture_report(tmp_path)
    output = tmp_path / "offset.jsonl"

    summary = create_gnss_replay_sidecar(
        capture_path,
        output,
        time_origin_monotonic_s=0.2,
        time_offset_s=0.5,
    )

    assert summary["time_origin_monotonic_s"] == pytest.approx(0.2)
    assert summary["time_offset_s"] == pytest.approx(0.5)
    sample = RawReplayTelemetrySample.from_mapping(json.loads(output.read_text(encoding="utf-8")))
    assert sample.observed_at_s == pytest.approx(0.6)
    assert sample.telemetry.gps_observed_at_s == pytest.approx(0.6)


def test_converter_rejects_global_position_only_and_negative_time_mapping(tmp_path: Path) -> None:
    global_capture = _capture_report(
        tmp_path,
        message=_Message(
            "GLOBAL_POSITION_INT",
            {"lat": 13_000_000, "lon": 103_800_000, "alt": 15_000},
        ),
    )
    with pytest.raises(AutonomyContractError, match="no GPS_RAW_INT"):
        create_gnss_replay_sidecar(global_capture, tmp_path / "global.jsonl")

    raw_capture = _capture_report(tmp_path / "raw")
    with pytest.raises(AutonomyContractError, match="negative"):
        create_gnss_replay_sidecar(
            raw_capture,
            tmp_path / "negative.jsonl",
            time_origin_monotonic_s=0.4,
        )
    assert not (tmp_path / "negative.jsonl").exists()


def test_converter_rejects_tampered_capture_and_preserves_existing_output(tmp_path: Path) -> None:
    capture_path = _capture_report(tmp_path)
    document = json.loads(capture_path.read_text(encoding="utf-8"))
    document["records"][0]["qualified"] = False
    capture_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="verification"):
        create_gnss_replay_sidecar(capture_path, tmp_path / "tampered.jsonl")

    output = tmp_path / "existing.jsonl"
    output.write_text("sentinel\n", encoding="utf-8")
    fresh_capture = _capture_report(tmp_path / "fresh")
    with pytest.raises(FileExistsError):
        create_gnss_replay_sidecar(fresh_capture, output)
    assert output.read_text(encoding="utf-8") == "sentinel\n"


def test_gnss_sidecar_feeds_video_join_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "camera.video"
    video.write_bytes(b"camera-container")
    capture = _VideoCapture()
    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2(capture))
    recording_dir = tmp_path / "camera-recording"
    create_video_replay_recording(video, recording_dir)

    capture_path = _capture_report(tmp_path / "capture")
    sidecar = tmp_path / "telemetry.jsonl"
    create_gnss_replay_sidecar(capture_path, sidecar)
    joined = join_raw_replay_telemetry(
        recording_dir / "recording.manifest.json",
        sidecar,
        tmp_path / "joined",
    )

    assert joined["telemetry_match"] == "latest_at_or_before_frame_no_interpolation"
    assert joined["frame_count"] == 3
    assert joined["gps_qualified_all_frames"] is True
    assert capture.released is True


def test_schema_and_wrapper_help_are_present() -> None:
    schema = json.loads(
        (ROOT / "configs" / "schemas" / "raw-replay-telemetry-sidecar.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["additionalProperties"] is False
    assert schema["properties"]["kind"]["const"] == "telemetry"
    telemetry = schema["$defs"]["vehicle_telemetry"]
    assert telemetry["additionalProperties"] is False
    assert "position_healthy" in telemetry["required"]
    assert "gps_observed_at_s" in telemetry["properties"]
    assert "gps_absolute_altitude_m" in telemetry["properties"]

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gnss_capture_to_replay_telemetry.py"),
            "--help",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--time-origin-monotonic-s" in completed.stdout

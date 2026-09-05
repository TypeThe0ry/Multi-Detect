from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.backends import RawReplayTelemetrySample, RawSensorReplayRecording
from multidetect.domain import VehicleTelemetry
from multidetect.replay_telemetry_join import join_raw_replay_telemetry
from multidetect.video_replay_recording import create_video_replay_recording

ROOT = Path(__file__).resolve().parents[1]


class _Encoded:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def tobytes(self) -> bytes:
        return self.payload


class _Capture:
    def __init__(self, frames: list[object], *, fps: float) -> None:
        self.frames = list(frames)
        self.fps = fps
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, property_id: int) -> float:
        assert property_id == _CV2.CAP_PROP_FPS
        return self.fps

    def read(self) -> tuple[bool, object | None]:
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self) -> None:
        self.released = True


class _CV2:
    CAP_PROP_FPS = 5

    def __init__(self, capture: _Capture) -> None:
        self.capture = capture

    def VideoCapture(self, path: str) -> _Capture:
        assert path.endswith("input.video")
        return self.capture

    @staticmethod
    def imencode(extension: str, image: object) -> tuple[bool, _Encoded]:
        assert extension == ".png"
        return True, _Encoded(f"frame-{image.index}".encode("ascii"))


def _camera_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "input.video"
    source.write_bytes(b"camera-container")
    frames = [
        SimpleNamespace(index=index, ndim=3, shape=(4, 6, 3))
        for index in range(3)
    ]
    capture = _Capture(frames, fps=5.0)
    monkeypatch.setitem(sys.modules, "cv2", _CV2(capture))
    output = tmp_path / "camera-recording"
    result = create_video_replay_recording(source, output)
    assert result["frame_count"] == 3
    assert capture.released is True
    return output


def _bind_scenario(recording_dir: Path) -> None:
    """Rewrite only the source manifest with a valid deterministic scenario binding."""

    source_manifest = recording_dir / "recording.manifest.json"
    source = RawSensorReplayRecording.from_manifest(source_manifest)
    scenario_hash = "ab" * 32
    rebound = RawSensorReplayRecording.from_jsonl(
        recording_dir / "events.jsonl",
        recording_id=source.recording_id,
        airframe_profile_hash=source.airframe_profile_hash,
        started_at_utc=source.started_at_utc,
        expected_sha256=source.event_stream_sha256,
        expected_byte_count=source.event_stream_bytes,
        scenario_id="scenario-alpha",
        scenario_manifest_hash=scenario_hash,
        scenario_seed=7,
    )
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recording_id": rebound.recording_id,
                "airframe_profile_hash": rebound.airframe_profile_hash,
                "started_at_utc": rebound.started_at_utc.isoformat().replace("+00:00", "Z"),
                "recording_hash": rebound.recording_hash,
                "events": {
                    "path": "events.jsonl",
                    "sha256": rebound.event_stream_sha256,
                    "bytes": rebound.event_stream_bytes,
                },
                "scenario": {
                    "scenario_id": rebound.scenario_id,
                    "scenario_manifest_hash": rebound.scenario_manifest_hash,
                    "scenario_seed": rebound.scenario_seed,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _telemetry(observed_at_s: float, *, qualified: bool = True) -> RawReplayTelemetrySample:
    return RawReplayTelemetrySample(
        observed_at_s=observed_at_s,
        telemetry=VehicleTelemetry(
            altitude_agl_m=20.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=1.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=qualified,
            link_healthy=True,
            flight_mode_allows_deploy=False,
            release_zone_clear=None,
            latitude_deg=1.3521 if qualified else float("nan"),
            longitude_deg=103.8198 if qualified else float("nan"),
            gps_fix_type=3 if qualified else 2,
            satellites_visible=10 if qualified else 4,
            gps_horizontal_accuracy_m=1.0 if qualified else float("nan"),
            gps_vertical_accuracy_m=2.0 if qualified else float("nan"),
            gps_observed_at_s=observed_at_s if qualified else float("nan"),
            attitude_observed_at_s=observed_at_s,
            position_observed_at_s=observed_at_s,
            altitude_observed_at_s=observed_at_s,
            armed=False,
            flight_mode="HOLD",
        ),
    )


def _write_sidecar(path: Path, samples: list[RawReplayTelemetrySample]) -> None:
    payload = "".join(
        json.dumps(sample.to_mapping(), sort_keys=True, separators=(",", ":")) + "\n"
        for sample in samples
    )
    path.write_text(payload, encoding="utf-8", newline="\n")


def test_join_binds_latest_telemetry_to_frames_and_recomputes_gps_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = _camera_recording(tmp_path, monkeypatch)
    source_manifest = camera / "recording.manifest.json"
    sidecar = tmp_path / "telemetry.jsonl"
    samples = [_telemetry(time) for time in (0.0, 0.2, 0.4)]
    _write_sidecar(sidecar, samples)
    output = tmp_path / "joined"

    summary = join_raw_replay_telemetry(source_manifest, sidecar, output)

    assert multidetect.join_raw_replay_telemetry is join_raw_replay_telemetry
    assert summary["telemetry_mode"] == "joined_raw_sensor_sidecar"
    assert summary["frame_count"] == 3
    assert summary["telemetry_sample_count"] == 3
    assert summary["maximum_observed_frame_telemetry_age_s"] == pytest.approx(0.0)
    assert summary["gps_bound"] is True
    assert summary["gps_qualified_frame_count"] == 3
    assert summary["gps_qualified_all_frames"] is True
    assert summary["deployment_domain_evidence_complete"] is False
    assert summary["gate_passed"] is False
    assert output.is_dir()
    assert not tuple(tmp_path.glob(".joined.*"))

    manifest_path = output / "recording.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == summary[
        "recording_manifest_sha256"
    ]
    recording = RawSensorReplayRecording.from_manifest(manifest_path)
    assert manifest["recording_hash"] == recording.recording_hash
    assert len(recording.frames) == 3
    assert len(recording.telemetry_samples) == 3
    assert recording.telemetry_samples[1].telemetry.latitude_deg == pytest.approx(1.3521)
    assert all(frame.image.resolved_path.is_file() for frame in recording.frames)


def test_join_uses_latest_sample_without_interpolation_and_reports_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = _camera_recording(tmp_path, monkeypatch)
    sidecar = tmp_path / "telemetry.jsonl"
    _write_sidecar(sidecar, [_telemetry(0.0), _telemetry(0.3)])

    summary = join_raw_replay_telemetry(
        camera / "recording.manifest.json",
        sidecar,
        tmp_path / "joined",
        maximum_telemetry_age_s=0.2,
    )

    assert summary["telemetry_match"] == "latest_at_or_before_frame_no_interpolation"
    assert summary["maximum_observed_frame_telemetry_age_s"] == pytest.approx(0.2)
    assert summary["mean_observed_frame_telemetry_age_s"] == pytest.approx(0.1)
    assert summary["gps_qualified_all_frames"] is True


def test_join_preserves_source_scenario_binding_for_sim_hil_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = _camera_recording(tmp_path, monkeypatch)
    _bind_scenario(camera)
    sidecar = tmp_path / "telemetry.jsonl"
    _write_sidecar(sidecar, [_telemetry(0.0), _telemetry(0.3)])

    output = tmp_path / "scenario-joined"
    summary = join_raw_replay_telemetry(
        camera / "recording.manifest.json",
        sidecar,
        output,
    )

    assert summary["scenario_binding_preserved"] is True
    assert summary["scenario_id"] == "scenario-alpha"
    assert summary["scenario_manifest_hash"] == "ab" * 32
    assert summary["scenario_seed"] == 7
    manifest = json.loads((output / "recording.manifest.json").read_text(encoding="utf-8"))
    assert manifest["scenario"] == {
        "scenario_id": "scenario-alpha",
        "scenario_manifest_hash": "ab" * 32,
        "scenario_seed": 7,
    }
    recording = RawSensorReplayRecording.from_manifest(output / "recording.manifest.json")
    assert recording.scenario_id == "scenario-alpha"
    assert recording.scenario_manifest_hash == "ab" * 32
    assert recording.scenario_seed == 7


def test_join_rejects_future_or_stale_telemetry_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = _camera_recording(tmp_path, monkeypatch)
    manifest = camera / "recording.manifest.json"

    future_sidecar = tmp_path / "future.jsonl"
    _write_sidecar(future_sidecar, [_telemetry(0.1), _telemetry(0.2)])
    with pytest.raises(ValueError, match="at or before"):
        join_raw_replay_telemetry(manifest, future_sidecar, tmp_path / "future-joined")
    assert not tuple(tmp_path.glob(".future-joined.*"))

    stale_sidecar = tmp_path / "stale.jsonl"
    _write_sidecar(stale_sidecar, [_telemetry(0.0)])
    output = tmp_path / "stale-joined"
    with pytest.raises(ValueError, match="stale"):
        join_raw_replay_telemetry(
            manifest,
            stale_sidecar,
            output,
            maximum_telemetry_age_s=0.1,
        )
    assert not output.exists()
    assert not tuple(tmp_path.glob(".stale-joined.*"))


def test_join_rejects_duplicate_or_malformed_sidecar_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = _camera_recording(tmp_path, monkeypatch)
    manifest = camera / "recording.manifest.json"
    duplicate = tmp_path / "duplicate.jsonl"
    _write_sidecar(duplicate, [_telemetry(0.0), _telemetry(0.0)])
    with pytest.raises(AutonomyContractError, match="strictly increasing"):
        join_raw_replay_telemetry(manifest, duplicate, tmp_path / "duplicate-joined")

    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("{\"kind\":\"telemetry\"}\n", encoding="utf-8")
    with pytest.raises(AutonomyContractError):
        join_raw_replay_telemetry(manifest, malformed, tmp_path / "malformed-joined")

    duplicate_key = tmp_path / "duplicate-key.jsonl"
    duplicate_key.write_text(
        '{"schema_version":1,"kind":"telemetry","kind":"telemetry",'
        '"observed_at_s":0.0,"telemetry":{}}\n',
        encoding="utf-8",
    )
    with pytest.raises(AutonomyContractError, match="strict JSON"):
        join_raw_replay_telemetry(manifest, duplicate_key, tmp_path / "duplicate-key-joined")


def test_join_script_help_works_without_pythonpath() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "join_raw_replay_telemetry.py"), "--help"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--maximum-telemetry-age-s" in completed.stdout
    assert "latest-at-or-before" in completed.stdout

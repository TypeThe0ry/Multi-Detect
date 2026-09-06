from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
from multidetect.backends import RawSensorReplayRecording
from multidetect.domain import MissionPhase
from multidetect.synthetic_model import create_synthetic_hil_model_bundle

ROOT = Path(__file__).resolve().parents[1]
MISSION_PATH = ROOT / "configs/missions/fire_patrol.demo.json"


def _telemetry_mapping(observed_at_s: float) -> dict[str, object]:
    return {
        "altitude_agl_m": 20.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "ground_speed_mps": 0.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": None,
        "latitude_deg": 1.3521,
        "longitude_deg": 103.8198,
        "gps_fix_type": 3,
        "satellites_visible": 15,
        "altitude_observed_at_s": observed_at_s,
        "attitude_observed_at_s": observed_at_s,
        "position_observed_at_s": observed_at_s,
    }


def _write_real_integrated_replay_recording(tmp_path: Path) -> tuple[Path, str]:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")

    events: list[dict[str, object]] = []
    for index, captured_at_s in enumerate((1.0, 2.0), start=1):
        observed_at_s = captured_at_s - 0.1
        events.append(
            {
                "schema_version": 1,
                "kind": "telemetry",
                "observed_at_s": observed_at_s,
                "telemetry": _telemetry_mapping(observed_at_s),
            }
        )
        image = np.zeros((640, 640, 3), dtype=np.uint8)
        image[:, :, 1] = index * 10
        encoded_ok, encoded = cv2.imencode(".png", image)
        assert encoded_ok
        payload = encoded.tobytes()
        image_path = tmp_path / f"frame-{index:04d}.png"
        image_path.write_bytes(payload)
        events.append(
            {
                "schema_version": 1,
                "kind": "frame",
                "frame_id": f"frame-{index:04d}",
                "captured_at_s": captured_at_s,
                "image": {
                    "path": image_path.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "width": 640,
                    "height": 640,
                    "encoding": "png",
                },
            }
        )

    event_payload = "".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in events
    ).encode("utf-8")
    event_path = tmp_path / "events.jsonl"
    event_path.write_bytes(event_payload)
    event_sha256 = hashlib.sha256(event_payload).hexdigest()
    recording = RawSensorReplayRecording.from_jsonl(
        event_path,
        recording_id="integrated-cli-real-stack",
        airframe_profile_hash="a" * 64,
        started_at_utc=datetime(2026, 8, 25, tzinfo=timezone.utc),
        expected_sha256=event_sha256,
        expected_byte_count=len(event_payload),
    )
    manifest = {
        "schema_version": 1,
        "recording_id": recording.recording_id,
        "airframe_profile_hash": recording.airframe_profile_hash,
        "started_at_utc": "2026-08-25T00:00:00Z",
        "recording_hash": recording.recording_hash,
        "events": {
            "path": event_path.name,
            "sha256": event_sha256,
            "bytes": len(event_payload),
        },
    }
    manifest_payload = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_path = tmp_path / "recording.manifest.json"
    manifest_path.write_bytes(manifest_payload)
    return manifest_path, hashlib.sha256(manifest_payload).hexdigest()


class _FakeBackend:
    instance: _FakeBackend | None = None

    def __init__(self) -> None:
        self.recording = SimpleNamespace(
            recording_id="raw-cli-fixture",
            recording_hash="b" * 64,
        )
        self.descriptor = SimpleNamespace(canonical_hash="c" * 64)
        self.frame_count = 2
        self.closed = False
        type(self).instance = self

    @classmethod
    def from_manifest(cls, path: Path, *, expected_manifest_sha256: str):
        assert path == Path("recording.manifest.json")
        assert expected_manifest_sha256 == "a" * 64
        return cls()

    def close(self) -> None:
        self.closed = True


class _FakePerception:
    def __init__(self) -> None:
        self.detector = object()
        self.rgb_fire_verifier = None
        self.rgb_fire_corroborator = None
        self.semantic_context_runner = None
        self.all_configured_artifacts_verified = True
        self.all_configured_artifacts_production_approved = True
        self.model_set_sha256 = "d" * 64
        self.configured_model_components = ("primary",)
        self.synthetic_hil_components = ()
        self.person_safety_evidence_qualified = False
        self.closed = False

    def close(self) -> bool:
        self.closed = True
        return True


@dataclass(frozen=True)
class _FakeResult:
    processed_frames: int = 2
    final_phase: MissionPhase = MissionPhase.SEARCHING


def test_integrated_replay_cli_reuses_live_perception_namespace_and_writes_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    perception = _FakePerception()
    captured: dict[str, object] = {}

    def fake_factory(args, mission_config):
        captured["perception_args"] = args
        captured["mission_id"] = mission_config.mission_id
        return perception

    def fake_session_builder(**kwargs):
        captured["session_kwargs"] = kwargs
        identity = cli_module.IntegratedReplayIdentity(
            runtime_descriptor_hash="c" * 64,
            recording_hash="b" * 64,
            mission_config_hash=kwargs["mission_config_hash"],
            perception_identity_hash=kwargs["perception_identity_hash"],
        )
        return SimpleNamespace(
            identity=identity,
            run=lambda: _FakeResult(),
        )

    monkeypatch.setattr(cli_module, "RawSensorReplayBackend", _FakeBackend)
    monkeypatch.setattr(cli_module, "build_production_perception", fake_factory)
    monkeypatch.setattr(cli_module, "build_integrated_replay_session", fake_session_builder)
    result_path = tmp_path / "result.json"

    exit_code = cli_module.main(
        [
            "integrated-replay",
            str(MISSION_PATH),
            "recording.manifest.json",
            "--recording-manifest-sha256",
            "a" * 64,
            "--result-out",
            str(result_path),
            "--operator-id",
            "operator-test",
            "--perception-args",
            "--onnx-model",
            "primary.onnx",
            "--model-manifest",
            "primary.manifest.json",
            "--class-names",
            "fire,smoke",
            "--warmup-iterations",
            "0",
        ]
    )

    assert exit_code == 0
    parsed = captured["perception_args"]
    assert parsed.command == "live-camera"
    assert parsed.onnx_model == Path("primary.onnx")
    assert parsed.model_manifest == Path("primary.manifest.json")
    assert parsed.class_names == "fire,smoke"
    session_kwargs = captured["session_kwargs"]
    assert session_kwargs["detector"] is perception.detector
    assert session_kwargs["perception_identity_hash"] == "d" * 64
    assert session_kwargs["config"].max_frames == 2
    assert session_kwargs["config"].display is False
    assert session_kwargs["config"].operator_id == "operator-test"
    assert (
        session_kwargs["mission_config_hash"]
        == hashlib.sha256(MISSION_PATH.read_bytes()).hexdigest()
    )
    assert perception.closed is True
    assert _FakeBackend.instance is not None and _FakeBackend.instance.closed is True

    document = json.loads(result_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["event"] == "integrated_replay_finished"
    assert document["runtime_mode"] == "replay"
    assert document["gate_passed"] is True
    assert (
        document["run_identity_hash"]
        == cli_module.IntegratedReplayIdentity(
            runtime_descriptor_hash="c" * 64,
            recording_hash="b" * 64,
            mission_config_hash=session_kwargs["mission_config_hash"],
            perception_identity_hash="d" * 64,
        ).run_identity_hash
    )
    assert document["ground_truth_isolated"] is True
    assert document["hardware_outputs_present"] is False
    assert "qgc_synchronized_visual_gate" not in document
    assert document["qgc_transport_acceptance_gate"] == ("not_run_headless_integrated_replay")
    assert document["operator_visual_acceptance"] == "not_claimed"
    assert document["result"]["processed_frames"] == 2
    assert '"integrated_replay_started"' in capsys.readouterr().out


def test_integrated_replay_cli_runs_real_raw_decoder_factory_onnx_and_live_core(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    manifest_path, manifest_sha256 = _write_real_integrated_replay_recording(recording_dir)
    model_path, model_manifest_path = create_synthetic_hil_model_bundle(tmp_path / "model")
    first_result = tmp_path / "first-result.json"
    second_result = tmp_path / "second-result.json"
    first_audit = tmp_path / "first-audit.jsonl"
    second_audit = tmp_path / "second-audit.jsonl"

    common = [
        "integrated-replay",
        str(MISSION_PATH),
        str(manifest_path),
        "--recording-manifest-sha256",
        manifest_sha256,
        "--perception-args",
        "--onnx-model",
        str(model_path),
        "--model-manifest",
        str(model_manifest_path),
        "--output-coordinates",
        "normalized_xyxy",
        "--allow-synthetic-hil-model",
        "--warmup-iterations",
        "0",
        "--candidate-stability-frames",
        "1",
    ]
    assert (
        cli_module.main(
            [
                *common[:5],
                "--audit-out",
                str(first_audit),
                "--result-out",
                str(first_result),
                *common[5:],
            ]
        )
        == 0
    )
    first_stdout = capsys.readouterr().out
    assert (
        cli_module.main(
            [
                *common[:5],
                "--audit-out",
                str(second_audit),
                "--result-out",
                str(second_result),
                *common[5:],
            ]
        )
        == 0
    )
    second_stdout = capsys.readouterr().out

    first = json.loads(first_result.read_text(encoding="utf-8"))
    second = json.loads(second_result.read_text(encoding="utf-8"))
    assert first == second
    assert first["result"]["processed_frames"] == 2
    assert first["perception_components"] == ["primary"]
    assert first["perception_production_approved"] is False
    assert first["synthetic_hil_components"] == ["primary"]
    assert first["ground_truth_isolated"] is True
    assert first["hardware_outputs_present"] is False
    assert len(first["run_identity_hash"]) == 64
    assert first_audit.read_bytes() == second_audit.read_bytes()
    assert '"integrated_replay_started"' in first_stdout
    assert '"integrated_replay_finished"' in first_stdout
    assert first_stdout == second_stdout


@pytest.mark.parametrize(
    "forbidden",
    (
        ["--pixhawk-endpoint", "udp:127.0.0.1:14550"],
        ["--fixed-wing-aim-control"],
        ["--semantic-context-onnx-model", "semantic.onnx"],
    ),
)
def test_integrated_replay_cli_rejects_non_perception_or_async_options(
    forbidden: list[str],
) -> None:
    with pytest.raises(ValueError, match="production perception options only"):
        cli_module._integrated_replay_perception_namespace(
            MISSION_PATH,
            [
                "--onnx-model",
                "primary.onnx",
                "--model-manifest",
                "primary.manifest.json",
                *forbidden,
            ],
        )

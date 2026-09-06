from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from base64 import b64decode
from pathlib import Path
from urllib.request import urlopen

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy import AutonomyContractError
from multidetect.backends import RawSensorReplayRecording
from multidetect.deployment_recording_attestation import (
    ATTESTATION_NAME,
    write_deployment_recording_attestation,
)
from multidetect.field_replay_campaign import (
    PLAN_NAME,
    advance_field_replay_campaign,
    create_field_replay_campaign,
    field_replay_annotation_workbench_argv,
    inspect_field_replay_campaign,
)
from multidetect.replay_annotation_workspace import review_replay_annotation_workspace
from multidetect.replay_set import REPLAY_SET_STRATA
from multidetect.synthetic_model import create_synthetic_hil_model_bundle

ROOT = Path(__file__).resolve().parents[1]
PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACklEQVQIHWMAAgAABAABDTukuQAAAABJRU5ErkJggg=="
)


def _telemetry() -> dict[str, object]:
    return {
        "altitude_agl_m": 10.0,
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
        "satellites_visible": 12,
        "gps_horizontal_accuracy_m": 1.5,
        "gps_vertical_accuracy_m": 2.5,
        "gps_observed_at_s": 1.0,
        "position_observed_at_s": 1.0,
        "attitude_observed_at_s": 1.0,
    }


def _write_recording(directory: Path, stratum: str) -> None:
    directory.mkdir(parents=True)
    (directory / "frame-0001.png").write_bytes(PNG)
    events = [
        {
            "schema_version": 1,
            "kind": "telemetry",
            "observed_at_s": 1.0,
            "telemetry": _telemetry(),
        },
        {
            "schema_version": 1,
            "kind": "frame",
            "frame_id": f"{stratum}-frame-0001",
            "captured_at_s": 1.0,
            "image": {
                "path": "frame-0001.png",
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "bytes": len(PNG),
                "width": 1,
                "height": 1,
                "encoding": "png",
            },
        },
    ]
    payload = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for event in events
    )
    events_path = directory / "events.jsonl"
    events_path.write_bytes(payload)
    recording = RawSensorReplayRecording.from_jsonl(
        events_path,
        recording_id=f"recording-{stratum}",
        airframe_profile_hash="a" * 64,
        started_at_utc="2026-09-06T00:00:00Z",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_byte_count=len(payload),
    )
    (directory / "recording.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "recording_id": recording.recording_id,
                "airframe_profile_hash": recording.airframe_profile_hash,
                "started_at_utc": "2026-09-06T00:00:00Z",
                "recording_hash": recording.recording_hash,
                "events": {
                    "path": "events.jsonl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    write_deployment_recording_attestation(
        directory,
        stratum=stratum,
        captured_by="capture-operator",
        capture_device_id="field-camera-01",
        capture_site="test-field-site",
        source_type="local_camera",
        started_at_utc="2026-09-06T00:00:00Z",
        ended_at_utc="2026-09-06T00:00:01Z",
    )


def _sources(root: Path, *, create: bool) -> dict[str, Path]:
    result = {stratum: root / stratum for stratum in REPLAY_SET_STRATA}
    if create:
        for stratum, directory in result.items():
            _write_recording(directory, stratum)
    return result


def _placeholder_model(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    model = root / "candidate.onnx"
    manifest = root / "candidate.manifest.json"
    model.write_bytes(b"immutable-model")
    manifest.write_text('{"fixture":true}\n', encoding="utf-8")
    return model, manifest


def _create(
    workspace: Path,
    sources: dict[str, Path],
    model: Path,
    manifest: Path,
) -> dict[str, object]:
    return create_field_replay_campaign(
        workspace,
        campaign_id="field-campaign-001",
        dataset_id="deployment-field-2026",
        dataset_version="v1",
        sources=sources,
        model_path=model,
        model_manifest_path=manifest,
        providers=("CPUExecutionProvider",),
        warmup_iterations=0,
        minimum_precision=0.0,
        minimum_recall=0.0,
        maximum_false_alarm_frame_rate=1.0,
        maximum_missed_detection_frame_rate=1.0,
        maximum_latency_p95_ms=10_000.0,
        calibration_thresholds=(0.25, 0.50, 0.75),
    )


def _complete_annotations(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["annotator"] = "annotator-a"
    for frame in document["frames"]:
        if frame["stratum"] == "no_fire":
            frame["annotation"] = {"status": "negative", "objects": []}
        else:
            frame["annotation"] = {
                "status": "positive",
                "objects": [{"label": "flame", "bbox": [0.25, 0.25, 0.55, 0.55]}],
            }
    path.write_text(json.dumps(document), encoding="utf-8")
    review_replay_annotation_workspace(
        path.parent / "intake",
        path,
        reviewer="reviewer-b",
        reviewed_at_utc="2026-09-06T01:00:00Z",
    )


def test_campaign_plan_is_strict_hash_bound_and_waits_for_real_sources(
    tmp_path: Path,
) -> None:
    model, manifest = _placeholder_model(tmp_path / "model")
    workspace = tmp_path / "campaign"
    status = _create(
        workspace,
        _sources(tmp_path / "not-collected", create=False),
        model,
        manifest,
    )

    assert status["stage"] == "awaiting_sources"
    assert status["complete"] is False
    assert status["release_ready"] is False
    assert len(status["missing"]) == 6
    plan = json.loads((workspace / PLAN_NAME).read_text(encoding="utf-8"))
    schema = json.loads(
        (ROOT / "configs/schemas/field-replay-campaign-plan.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(plan)
    assert not any(plan["claims"].values())

    model.write_bytes(b"tampered-model!")
    with pytest.raises(AutonomyContractError, match="SHA-256 changed"):
        inspect_field_replay_campaign(workspace)


def test_campaign_waits_for_all_physical_source_attestations(tmp_path: Path) -> None:
    model, manifest = _placeholder_model(tmp_path / "model")
    sources = _sources(tmp_path / "sources", create=True)
    (sources["haze"] / ATTESTATION_NAME).unlink()

    status = _create(tmp_path / "campaign", sources, model, manifest)

    assert status["stage"] == "awaiting_source_attestations"
    assert status["complete"] is False
    assert status["release_ready"] is False
    assert len(status["missing"]) == 1
    assert status["missing"][0].startswith("haze:")
    assert "deployment-recording-source.json" in status["missing"][0]


def test_campaign_advances_intake_and_stops_at_human_annotation(
    tmp_path: Path,
) -> None:
    model, manifest = _placeholder_model(tmp_path / "model")
    workspace = tmp_path / "campaign"
    created = _create(
        workspace,
        _sources(tmp_path / "sources", create=True),
        model,
        manifest,
    )
    assert created["stage"] == "intake_ready"

    status = advance_field_replay_campaign(workspace)
    assert status["stage"] == "annotation_required"
    assert status["advanced_stages"] == [
        "intake_ready",
        "annotation_workspace_ready",
    ]
    assert Path(status["paths"]["annotations"]).is_file()
    assert status["next_argv"][2:4] == ["annotate", status["paths"]["intake"]]
    assert status["next_argv"][5] == "day-frame-0001"
    assert status["artifacts"]["annotations"]["status_counts"] == {
        "unannotated": 6,
        "positive": 0,
        "negative": 0,
    }
    assert status["annotation_workbench_argv"] == [
        "python",
        "scripts/run_field_replay_campaign.py",
        "annotation-ui",
        str(workspace),
        "--open-browser",
    ]
    assert field_replay_annotation_workbench_argv(
        workspace, host="127.0.0.1", port=0, open_browser=True
    ) == [
        status["paths"]["intake"],
        status["paths"]["annotations"],
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--open-browser",
    ]

    _complete_annotations(workspace / "annotations.json")
    legacy = json.loads((workspace / "annotations.json").read_text(encoding="utf-8"))
    legacy["schema_version"] = 1
    legacy.pop("review_content_hash")
    (workspace / "annotations.json").write_text(json.dumps(legacy), encoding="utf-8")
    downgraded = inspect_field_replay_campaign(workspace)
    assert downgraded["stage"] == "annotation_required"
    assert downgraded["artifacts"]["annotations"]["workspace_schema_version"] == 1
    assert not (workspace / "reviewed").exists()

    help_run = subprocess.run(
        [sys.executable, "scripts/run_field_replay_campaign.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_run.returncode == 0, help_run.stderr
    assert "field replay campaign" in help_run.stdout.lower()
    assert "annotation-ui" in help_run.stdout


def test_campaign_workbench_rejects_non_annotation_stages(tmp_path: Path) -> None:
    model, manifest = _placeholder_model(tmp_path / "model")
    workspace = tmp_path / "campaign"
    _create(
        workspace,
        _sources(tmp_path / "sources", create=True),
        model,
        manifest,
    )

    with pytest.raises(AutonomyContractError, match="current stage is intake_ready"):
        field_replay_annotation_workbench_argv(workspace)


def test_campaign_annotation_ui_runs_bound_loopback_workbench(tmp_path: Path) -> None:
    model, manifest = _placeholder_model(tmp_path / "model")
    workspace = tmp_path / "campaign"
    _create(
        workspace,
        _sources(tmp_path / "sources", create=True),
        model,
        manifest,
    )
    advance_field_replay_campaign(workspace)

    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/run_field_replay_campaign.py",
            "annotation-ui",
            str(workspace),
            "--port",
            "0",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        authorized = json.loads(process.stdout.readline())
        assert authorized["event"] == "field_replay_annotation_workbench_authorized"
        assert authorized["campaign_id"] == "field-campaign-001"
        assert authorized["dataset_id"] == "deployment-field-2026"
        assert authorized["dataset_version"] == "v1"
        assert authorized["stage"] == "annotation_required"
        assert len(authorized["plan_canonical_hash"]) == 64
        started = json.loads(process.stdout.readline())
        assert started["event"] == "replay_annotation_workbench_started"
        assert started["loopback_only"] is True
        assert started["hardware_io"] is False
        with urlopen(started["url"], timeout=5.0) as response:  # noqa: S310
            page = response.read().decode("utf-8")
        assert response.status == 200
        assert "Multi-Detect 标注工作台" in page
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10.0)

    advance_field_replay_campaign(workspace)
    _complete_annotations(workspace / "annotations.json")
    assert inspect_field_replay_campaign(workspace)["stage"] == "annotation_finalize_ready"
    with pytest.raises(AutonomyContractError, match="current stage is annotation_finalize_ready"):
        field_replay_annotation_workbench_argv(workspace)


def test_campaign_runs_real_model_accuracy_calibration_and_fresh_verification(
    tmp_path: Path,
) -> None:
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("cv2")
    model, manifest = create_synthetic_hil_model_bundle(
        tmp_path / "model", input_width=32, input_height=32
    )
    workspace = tmp_path / "campaign"
    _create(
        workspace,
        _sources(tmp_path / "sources", create=True),
        model,
        manifest,
    )

    first = advance_field_replay_campaign(workspace)
    assert first["stage"] == "annotation_required"
    _complete_annotations(workspace / "annotations.json")

    with pytest.raises(AutonomyContractError, match="must be four distinct people"):
        advance_field_replay_campaign(
            workspace,
            accuracy_reviewer="reviewer-b",
        )

    completed = advance_field_replay_campaign(
        workspace,
        accuracy_reviewer="accuracy-reviewer-c",
        calibration_reviewer="threshold-reviewer-d",
    )
    assert completed["stage"] == "complete"
    assert completed["complete"] is True
    assert completed["release_ready"] is True
    assert completed["advanced_stages"] == [
        "annotation_finalize_ready",
        "prediction_ready",
        "accuracy_review_required",
        "calibration_review_required",
        "calibration_verification_ready",
    ]
    verification = completed["artifacts"]["calibration"]
    assert verification["gate_passed"] is True
    assert verification["report_provenance_complete"] is True
    assert verification["recomputed_provenance_complete"] is True
    assert verification["report_promotion_ready"] is True
    assert verification["recomputed_promotion_ready"] is True

    repeated = advance_field_replay_campaign(workspace)
    assert repeated["stage"] == "complete"
    assert repeated["advanced_stages"] == []

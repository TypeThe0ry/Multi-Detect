from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from base64 import b64decode
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy import AutonomyContractError
from multidetect.backends import RawSensorReplayRecording
from multidetect.cli import main as cli_main
from multidetect.deployment_recording_attestation import (
    ATTESTATION_NAME,
    write_deployment_recording_attestation,
)
from multidetect.model_manifest import (
    create_candidate_model_manifest,
    write_candidate_model_manifest,
)
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    build_release_gate_manifest,
    write_release_gate_manifest,
)
from multidetect.replay_annotation_workbench import create_replay_annotation_workbench
from multidetect.replay_annotation_workspace import (
    annotate_replay_frame,
    create_replay_annotation_workspace,
    finalize_replay_annotation_workspace,
    inspect_replay_annotation_workspace,
    review_replay_annotation_workspace,
    verify_finalized_replay_annotations,
)
from multidetect.replay_prediction_run import run_replay_predictions, verify_replay_predictions
from multidetect.replay_set import (
    REPLAY_SET_STRATA,
    build_replay_set_manifest,
    load_replay_set_manifest,
)
from multidetect.replay_set_calibration import (
    ReplaySetCalibrationError,
    calibrate_replay_set_thresholds,
    verify_replay_set_calibration_report,
)
from multidetect.replay_set_evaluation import evaluate_replay_set
from multidetect.replay_set_intake import (
    prepare_deployment_replay_set,
    verify_deployment_replay_intake,
)
from multidetect.replay_set_readiness import check_replay_set_readiness
from multidetect.synthetic_model import create_synthetic_hil_model_bundle


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


def _write_stratum(
    root: Path,
    stratum: str,
    *,
    with_predictions: bool = False,
    valid_image: bool = False,
) -> None:
    directory = root / stratum
    directory.mkdir(parents=True)
    image_payload = (
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACklEQVQIHWMAAgAABAABDTukuQAAAABJRU5ErkJggg=="
        )
        if valid_image
        else f"{stratum}-image".encode()
    )
    (directory / "frame-0001.png").write_bytes(image_payload)
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
                "sha256": hashlib.sha256(image_payload).hexdigest(),
                "bytes": len(image_payload),
                "width": 1,
                "height": 1,
                "encoding": "png",
            },
        },
    ]
    event_payload = b"".join(
        (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for event in events
    )
    events_path = directory / "events.jsonl"
    events_path.write_bytes(event_payload)
    recording = RawSensorReplayRecording.from_jsonl(
        events_path,
        recording_id=f"recording-{stratum}",
        airframe_profile_hash="a" * 64,
        started_at_utc="2026-08-25T00:00:00Z",
        expected_sha256=hashlib.sha256(event_payload).hexdigest(),
        expected_byte_count=len(event_payload),
    )
    recording_manifest = {
        "schema_version": 1,
        "recording_id": recording.recording_id,
        "airframe_profile_hash": recording.airframe_profile_hash,
        "started_at_utc": "2026-08-25T00:00:00Z",
        "recording_hash": recording.recording_hash,
        "events": {
            "path": "events.jsonl",
            "sha256": hashlib.sha256(event_payload).hexdigest(),
            "bytes": len(event_payload),
        },
    }
    (directory / "recording.manifest.json").write_text(
        json.dumps(recording_manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    write_deployment_recording_attestation(
        directory,
        stratum=stratum,
        captured_by="capture-operator",
        capture_device_id="field-camera-01",
        capture_site="test-field-site",
        source_type="local_camera",
        started_at_utc="2026-08-25T00:00:00Z",
        ended_at_utc="2026-08-25T00:00:01Z",
    )
    (directory / "ground-truth.jsonl").write_text(
        json.dumps(
            {"frame_id": f"{stratum}-frame-0001", "objects": []},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    if with_predictions:
        (directory / "predictions.jsonl").write_text(
            json.dumps(
                {
                    "frame_id": f"{stratum}-frame-0001",
                    "inference_latency_ms": 2.0,
                    "detections": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )


def _intake_sources(root: Path, *, valid_images: bool = False) -> dict[str, Path]:
    for stratum in REPLAY_SET_STRATA:
        _write_stratum(root, stratum, with_predictions=True, valid_image=valid_images)
    return {stratum: root / stratum for stratum in REPLAY_SET_STRATA}


def test_deployment_replay_intake_stages_raw_recordings_without_claims(tmp_path: Path) -> None:
    sources = _intake_sources(tmp_path / "sources")
    output = tmp_path / "intake"

    report = prepare_deployment_replay_set(
        sources,
        output,
        dataset_id="field-2026",
        dataset_version="v1",
    )

    assert report["entry_count"] == 6
    assert report["frame_count"] == 6
    assert report["annotation_state"] == "pending"
    assert report["prediction_state"] == "pending"
    assert report["deployment_sources_attested"] is True
    assert report["release_ready"] is False
    receipt_path = output / "intake.receipt.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "configs/schemas/replay-set-intake-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(receipt_schema)
    Draft202012Validator(receipt_schema).validate(receipt)
    assert all(entry["source_attestation"]["canonical_hash"] for entry in receipt["entries"])
    for stratum in REPLAY_SET_STRATA:
        assert (output / stratum / "recording.manifest.json").is_file()
        assert (output / stratum / "events.jsonl").is_file()
        assert (output / stratum / ATTESTATION_NAME).is_file()
        assert (output / stratum / "frame-0001.png").is_file()
        assert not (output / stratum / "ground-truth.jsonl").exists()
        assert not (output / stratum / "predictions.jsonl").exists()
    assert verify_deployment_replay_intake(output)["canonical_hash"] == report["canonical_hash"]


def test_deployment_replay_intake_requires_exact_strata_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    sources = _intake_sources(tmp_path / "sources")
    missing = dict(sources)
    missing.pop("night")
    with pytest.raises(AutonomyContractError, match="exactly six strata"):
        prepare_deployment_replay_set(
            missing,
            tmp_path / "missing",
            dataset_id="field-2026",
            dataset_version="v1",
        )

    output = tmp_path / "intake"
    prepare_deployment_replay_set(
        sources,
        output,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    with pytest.raises(AutonomyContractError, match="already exists"):
        prepare_deployment_replay_set(
            sources,
            output,
            dataset_id="field-2026",
            dataset_version="v1",
        )


def test_deployment_replay_intake_rejects_reused_recording_and_rolls_back(
    tmp_path: Path,
) -> None:
    sources = _intake_sources(tmp_path / "sources")
    sources["night"] = sources["day"]
    duplicate_output = tmp_path / "duplicate"
    with pytest.raises(AutonomyContractError, match="distinct source directory"):
        prepare_deployment_replay_set(
            sources,
            duplicate_output,
            dataset_id="field-2026",
            dataset_version="v1",
        )
    assert not duplicate_output.exists()

    sources = _intake_sources(tmp_path / "fresh-sources")
    (sources["reflection"] / "frame-0001.png").write_bytes(b"broken")
    broken_output = tmp_path / "broken"
    with pytest.raises(AutonomyContractError, match="image frame-0001.png"):
        prepare_deployment_replay_set(
            sources,
            broken_output,
            dataset_id="field-2026",
            dataset_version="v1",
        )
    assert not broken_output.exists()
    assert not list(tmp_path.glob(".broken.*"))


def test_deployment_replay_intake_requires_bound_physical_source_attestation(
    tmp_path: Path,
) -> None:
    sources = _intake_sources(tmp_path / "sources")
    (sources["night"] / ATTESTATION_NAME).unlink()

    with pytest.raises(AutonomyContractError, match="requires deployment-recording-source"):
        prepare_deployment_replay_set(
            sources,
            tmp_path / "intake",
            dataset_id="field-2026",
            dataset_version="v1",
        )

    assert not (tmp_path / "intake").exists()


def test_deployment_replay_intake_verifier_detects_artifact_and_receipt_tampering(
    tmp_path: Path,
) -> None:
    sources = _intake_sources(tmp_path / "sources")
    first = tmp_path / "first"
    prepare_deployment_replay_set(
        sources,
        first,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    (first / "day" / "frame-0001.png").write_bytes(b"tampered")
    with pytest.raises(AutonomyContractError, match="hash mismatch"):
        verify_deployment_replay_intake(first)

    second = tmp_path / "second"
    prepare_deployment_replay_set(
        sources,
        second,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    receipt_path = second / "intake.receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["claims"]["annotations_reviewed"] = True
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="must not assert"):
        verify_deployment_replay_intake(second)

    third = tmp_path / "third"
    prepare_deployment_replay_set(
        sources,
        third,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    attestation_path = third / "day" / ATTESTATION_NAME
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation["capture_site"] = "tampered-field-site"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="artifact hash mismatch"):
        verify_deployment_replay_intake(third)


def test_prepare_deployment_replay_set_cli_round_trip(tmp_path: Path) -> None:
    sources = _intake_sources(tmp_path / "sources")
    output = tmp_path / "intake"
    command = [
        sys.executable,
        "scripts/prepare_deployment_replay_set.py",
        "--output",
        str(output),
        "--dataset-id",
        "field-2026",
        "--dataset-version",
        "v1",
    ]
    for stratum in REPLAY_SET_STRATA:
        command.extend(("--source", f"{stratum}={sources[stratum]}"))
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["release_ready"] is False

    verified = subprocess.run(
        [
            sys.executable,
            "scripts/prepare_deployment_replay_set.py",
            "--verify",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["entry_count"] == 6


def _annotation_fixture(tmp_path: Path) -> tuple[Path, Path]:
    intake = tmp_path / "intake"
    prepare_deployment_replay_set(
        _intake_sources(tmp_path / "sources"),
        intake,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    workspace = tmp_path / "annotations.json"
    create_replay_annotation_workspace(intake, workspace)
    return intake, workspace


def _complete_annotations(
    workspace: Path,
    *,
    positive_all_detection_strata: bool = False,
) -> dict[str, object]:
    document = json.loads(workspace.read_text(encoding="utf-8"))
    document["annotator"] = "annotator-a"
    document["reviewer"] = None
    document["reviewed_at_utc"] = None
    for index, frame in enumerate(document["frames"]):
        if index == 0 or (positive_all_detection_strata and frame["stratum"] != "no_fire"):
            frame["annotation"] = {
                "status": "positive",
                "objects": [{"label": "flame", "bbox": [0.1, 0.2, 0.4, 0.6]}],
            }
        else:
            frame["annotation"] = {"status": "negative", "objects": []}
    workspace.write_text(json.dumps(document), encoding="utf-8")
    review_replay_annotation_workspace(
        workspace.parent / "intake",
        workspace,
        reviewer="reviewer-b",
        reviewed_at_utc="2026-09-06T02:00:00Z",
    )
    return json.loads(workspace.read_text(encoding="utf-8"))


def _reviewed_annotation_fixture(tmp_path: Path) -> Path:
    intake, workspace = _annotation_fixture(tmp_path)
    _complete_annotations(workspace)
    output = tmp_path / "reviewed"
    finalize_replay_annotation_workspace(intake, workspace, output)
    return output


def _candidate_model_fixture(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "candidate.onnx"
    model.write_bytes(b"fixed-model-artifact")
    manifest = tmp_path / "candidate.manifest.json"
    write_candidate_model_manifest(
        manifest,
        create_candidate_model_manifest(
            model,
            model_id="fire-smoke-candidate",
            model_version="v-test",
            class_names=("fire", "smoke"),
            input_width=32,
            input_height=32,
            output_coordinates="letterbox_xyxy_px",
            source_description="unit-test fixture",
            native_output_format="post_nms_N_x_6",
        ),
    )
    return model, manifest


class _EmptyDetector:
    provider_names = ("FixtureExecutionProvider",)
    provider_selection = None

    def warmup(self, *, iterations: int) -> None:
        assert iterations >= 0

    def detect(self, image_bgr: object) -> tuple[object, ...]:
        assert getattr(image_bgr, "shape", None) == (1, 1, 3)
        return ()


def _fixture_decoder(payload: bytes, reference: object) -> object:
    import numpy as np

    assert payload
    return np.zeros((1, 1, 3), dtype=np.uint8)


def test_replay_annotation_workspace_keeps_empty_frames_unannotated(tmp_path: Path) -> None:
    intake, workspace = _annotation_fixture(tmp_path)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "configs/schemas/replay-annotation-workspace.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(json.loads(workspace.read_text(encoding="utf-8")))
    report = inspect_replay_annotation_workspace(intake, workspace)
    assert report["frame_count"] == 6
    assert report["status_counts"] == {"unannotated": 6, "positive": 0, "negative": 0}
    assert report["finalization_ready"] is False

    output = tmp_path / "final"
    with pytest.raises(AutonomyContractError, match="explicitly positive or negative"):
        finalize_replay_annotation_workspace(intake, workspace, output)
    assert not output.exists()


def test_replay_annotation_commands_atomically_assign_annotate_and_review(
    tmp_path: Path,
) -> None:
    intake, workspace = _annotation_fixture(tmp_path)
    document = json.loads(workspace.read_text(encoding="utf-8"))
    frames = document["frames"]

    with pytest.raises(AutonomyContractError, match="every frame must be annotated"):
        review_replay_annotation_workspace(intake, workspace, reviewer="reviewer-b")

    first = annotate_replay_frame(
        intake,
        workspace,
        frame_id=frames[0]["frame_id"],
        annotator="annotator-a",
        status="positive",
        objects=({"label": "fire", "bbox": [0.1, 0.2, 0.4, 0.6]},),
    )
    assert first["status_counts"] == {"unannotated": 5, "positive": 1, "negative": 0}
    assert first["next_unannotated"]["frame_id"] == frames[1]["frame_id"]

    with pytest.raises(AutonomyContractError, match="another annotator"):
        annotate_replay_frame(
            intake,
            workspace,
            frame_id=frames[1]["frame_id"],
            annotator="annotator-x",
            status="negative",
        )

    for frame in frames[1:]:
        annotate_replay_frame(
            intake,
            workspace,
            frame_id=frame["frame_id"],
            annotator="annotator-a",
            status="negative",
        )
    with pytest.raises(AutonomyContractError, match="present and distinct"):
        review_replay_annotation_workspace(
            intake,
            workspace,
            reviewer="ANNOTATOR-A",
            reviewed_at_utc="2026-09-06T02:00:00Z",
        )

    reviewed = review_replay_annotation_workspace(
        intake,
        workspace,
        reviewer="reviewer-b",
        reviewed_at_utc="2026-09-06T02:00:00Z",
    )
    assert reviewed["all_frames_annotated"] is True
    assert reviewed["next_unannotated"] is None
    assert reviewed["finalization_ready"] is True
    assert reviewed["workspace_schema_version"] == 2
    assert reviewed["review_content_bound"] is True
    with pytest.raises(AutonomyContractError, match="another reviewer"):
        review_replay_annotation_workspace(
            intake,
            workspace,
            reviewer="reviewer-c",
            reviewed_at_utc="2026-09-06T03:00:00Z",
        )

    unchanged = annotate_replay_frame(
        intake,
        workspace,
        frame_id=frames[0]["frame_id"],
        annotator="annotator-a",
        status="positive",
        objects=({"label": "flame", "bbox": [0.1, 0.2, 0.4, 0.6]},),
    )
    assert unchanged["finalization_ready"] is True

    changed = annotate_replay_frame(
        intake,
        workspace,
        frame_id=frames[0]["frame_id"],
        annotator="annotator-a",
        status="negative",
    )
    assert changed["all_frames_annotated"] is True
    assert changed["annotator_reviewer_distinct"] is False
    assert changed["finalization_ready"] is False


def test_replay_annotation_workspace_finalizes_reviewed_truth_atomically(
    tmp_path: Path,
) -> None:
    intake, workspace = _annotation_fixture(tmp_path)
    _complete_annotations(workspace)
    inspected = inspect_replay_annotation_workspace(intake, workspace)
    assert inspected["finalization_ready"] is True

    output = tmp_path / "final"
    report = finalize_replay_annotation_workspace(intake, workspace, output)
    assert report["annotations_reviewed"] is True
    assert report["predictions_complete"] is False
    assert report["release_ready"] is False
    manifest = load_replay_set_manifest(output / "replay-set.manifest.json")
    assert manifest.is_complete is True
    assert manifest.claims["contains_predictions"] is False
    review = json.loads((output / "annotation-review.receipt.json").read_text(encoding="utf-8"))
    assert review["annotator"] == "annotator-a"
    assert review["reviewer"] == "reviewer-b"
    assert review["claims"]["accuracy_validated"] is False
    assert verify_finalized_replay_annotations(output)["annotations_reviewed"] is True

    (output / "day" / "ground-truth.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="truth artifact hash mismatch"):
        verify_finalized_replay_annotations(output)


def test_replay_annotation_workspace_rejects_same_reviewer_and_assignment_tampering(
    tmp_path: Path,
) -> None:
    intake, workspace = _annotation_fixture(tmp_path)
    document = _complete_annotations(workspace)
    document["reviewer"] = "ANNOTATOR-A"
    workspace.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="present and distinct"):
        finalize_replay_annotation_workspace(intake, workspace, tmp_path / "same-person")

    document["reviewer"] = "reviewer-b"
    document["frames"][0]["frame_id"] = "substituted-frame"
    workspace.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="does not match verified intake"):
        finalize_replay_annotation_workspace(intake, workspace, tmp_path / "tampered")


def test_replay_annotation_workspace_v2_binds_review_to_annotation_content(
    tmp_path: Path,
) -> None:
    intake, workspace = _annotation_fixture(tmp_path)
    document = _complete_annotations(workspace)
    document["frames"][0]["annotation"] = {"status": "negative", "objects": []}
    workspace.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(AutonomyContractError, match="review content hash mismatch"):
        inspect_replay_annotation_workspace(intake, workspace)


def test_replay_annotation_workspace_keeps_v1_reviewed_workspace_compatible(
    tmp_path: Path,
) -> None:
    intake, workspace = _annotation_fixture(tmp_path)
    document = _complete_annotations(workspace)
    document["schema_version"] = 1
    document.pop("review_content_hash")
    workspace.write_text(json.dumps(document), encoding="utf-8")
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "configs/schemas/replay-annotation-workspace.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)

    status = inspect_replay_annotation_workspace(intake, workspace)
    assert status["workspace_schema_version"] == 1
    assert status["review_content_bound"] is False
    assert status["finalization_ready"] is True
    assert (
        finalize_replay_annotation_workspace(intake, workspace, tmp_path / "legacy-finalized")[
            "annotations_reviewed"
        ]
        is True
    )


def test_replay_annotation_workspace_cli_create_inspect_finalize_verify(tmp_path: Path) -> None:
    intake = tmp_path / "intake"
    prepare_deployment_replay_set(
        _intake_sources(tmp_path / "sources"),
        intake,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    workspace = tmp_path / "annotations.json"
    script = "scripts/replay_annotation_workspace.py"
    root = Path(__file__).resolve().parents[1]
    created = subprocess.run(
        [sys.executable, script, "create", str(intake), str(workspace)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    pending = subprocess.run(
        [sys.executable, script, "inspect", str(intake), str(workspace)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pending.returncode == 2
    assert json.loads(pending.stdout)["finalization_ready"] is False
    annotation = json.loads(workspace.read_text(encoding="utf-8"))
    for index, frame in enumerate(annotation["frames"]):
        command = [
            sys.executable,
            script,
            "annotate",
            str(intake),
            str(workspace),
            frame["frame_id"],
            "--annotator",
            "annotator-a",
        ]
        command.extend(["--object", "flame:0.1:0.2:0.4:0.6"] if index == 0 else ["--negative"])
        annotated = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert annotated.returncode == 0, annotated.stderr
    reviewed = subprocess.run(
        [
            sys.executable,
            script,
            "review",
            str(intake),
            str(workspace),
            "--reviewer",
            "reviewer-b",
            "--reviewed-at-utc",
            "2026-09-06T02:00:00Z",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reviewed.returncode == 0, reviewed.stderr
    assert json.loads(reviewed.stdout)["finalization_ready"] is True
    output = tmp_path / "final"
    finalized = subprocess.run(
        [sys.executable, script, "finalize", str(intake), str(workspace), str(output)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert finalized.returncode == 0, finalized.stderr
    verified = subprocess.run(
        [sys.executable, script, "verify", str(output)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["annotations_reviewed"] is True


def test_replay_annotation_workbench_serves_verified_images_and_tokenized_mutations(
    tmp_path: Path,
) -> None:
    intake = tmp_path / "intake"
    prepare_deployment_replay_set(
        _intake_sources(tmp_path / "sources", valid_images=True),
        intake,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    workspace = tmp_path / "annotations.json"
    create_replay_annotation_workspace(intake, workspace)
    with pytest.raises(AutonomyContractError, match="loopback only"):
        create_replay_annotation_workbench(intake, workspace, host="0.0.0.0")

    help_run = subprocess.run(
        [sys.executable, "scripts/run_replay_annotation_workbench.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_run.returncode == 0, help_run.stderr
    assert "loopback-only" in help_run.stdout.lower()

    workbench = create_replay_annotation_workbench(intake, workspace, port=0)
    thread = threading.Thread(target=workbench.serve_forever, daemon=True)
    thread.start()

    def request_json(
        path: str,
        body: dict[str, object] | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, object]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Annotation-Token"] = token
        request = Request(workbench.url + path.lstrip("/"), data=payload, headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        with urlopen(workbench.url, timeout=5) as response:
            html = response.read().decode("utf-8")
            csp = response.headers["Content-Security-Policy"]
        assert "Multi-Detect 标注工作台" in html
        assert '<canvas id="canvas">' in html
        assert "script-src 'nonce-" in csp
        assert "script-src 'unsafe-inline'" not in csp
        assert "__TOKEN__" not in html and "__NONCE__" not in html

        snapshot = request_json("/api/workspace")
        frames = snapshot["document"]["frames"]
        with urlopen(
            workbench.url + "api/image?frame_id=" + frames[0]["frame_id"], timeout=5
        ) as response:
            assert response.headers.get_content_type() == "image/png"
            assert response.read().startswith(b"\x89PNG")

        with pytest.raises(HTTPError) as forbidden:
            request_json(
                "/api/annotate",
                {
                    "frame_id": frames[0]["frame_id"],
                    "annotator": "annotator-a",
                    "status": "negative",
                    "objects": [],
                },
            )
        assert forbidden.value.code == 403

        duplicate_request = Request(
            workbench.url + "api/annotate",
            data=b'{"frame_id":"first","frame_id":"second"}',
            headers={
                "Content-Type": "application/json",
                "X-Annotation-Token": workbench.token,
            },
        )
        with pytest.raises(HTTPError) as duplicate:
            urlopen(duplicate_request, timeout=5)
        assert duplicate.value.code == 409

        with pytest.raises(HTTPError) as unknown_field:
            request_json(
                "/api/annotate",
                {
                    "frame_id": frames[0]["frame_id"],
                    "annotator": "annotator-a",
                    "status": "negative",
                    "objects": [],
                    "approved": True,
                },
                token=workbench.token,
            )
        assert unknown_field.value.code == 409

        for index, frame in enumerate(frames):
            positive = index == 0
            result = request_json(
                "/api/annotate",
                {
                    "frame_id": frame["frame_id"],
                    "annotator": "annotator-a",
                    "status": "positive" if positive else "negative",
                    "objects": (
                        [{"label": "flame", "bbox": [0.1, 0.2, 0.4, 0.6]}] if positive else []
                    ),
                },
                token=workbench.token,
            )
        assert result["status"]["all_frames_annotated"] is True
        assert result["status"]["status_counts"] == {
            "unannotated": 0,
            "positive": 1,
            "negative": 5,
        }
        reviewed = request_json(
            "/api/review",
            {"reviewer": "reviewer-b"},
            token=workbench.token,
        )
        assert reviewed["status"]["finalization_ready"] is True
        assert reviewed["status"]["review_content_bound"] is True
    finally:
        workbench.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_replay_predictions_materialize_fixed_model_provenance_atomically(
    tmp_path: Path,
) -> None:
    reviewed = _reviewed_annotation_fixture(tmp_path)
    model, manifest = _candidate_model_fixture(tmp_path)
    output = tmp_path / "predicted"

    report = run_replay_predictions(
        reviewed,
        output,
        model_path=model,
        model_manifest_path=manifest,
        detector_factory=lambda config: _EmptyDetector(),
        image_decoder=_fixture_decoder,
    )

    assert report["frame_count"] == 6
    assert report["predictions_complete"] is True
    assert report["runtime_inference_executed"] is False
    assert report["accuracy_validated"] is False
    assert report["release_ready"] is False
    assert (output / "annotation-source.replay-set.manifest.json").is_file()
    replay_manifest = load_replay_set_manifest(output / "replay-set.manifest.json")
    assert replay_manifest.claims["contains_predictions"] is True
    receipt = json.loads((output / "prediction-run.receipt.json").read_text(encoding="utf-8"))
    assert (
        receipt["model"]["artifact_sha256"] == hashlib.sha256(b"fixed-model-artifact").hexdigest()
    )
    assert receipt["inference"]["shared_runtime_pipeline"] is True
    assert receipt["inference"]["test_double_used"] is True
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "configs/schemas/replay-prediction-run-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(receipt)
    assert verify_replay_predictions(output)["canonical_hash"] == report["canonical_hash"]

    verified = subprocess.run(
        [sys.executable, "scripts/run_replay_predictions.py", "verify", str(output)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["predictions_complete"] is True


def test_replay_predictions_reject_overwrite_and_detect_tampering(tmp_path: Path) -> None:
    reviewed = _reviewed_annotation_fixture(tmp_path)
    model, manifest = _candidate_model_fixture(tmp_path)
    output = tmp_path / "predicted"
    options = {
        "model_path": model,
        "model_manifest_path": manifest,
        "detector_factory": lambda config: _EmptyDetector(),
        "image_decoder": _fixture_decoder,
    }
    run_replay_predictions(reviewed, output, **options)
    with pytest.raises(AutonomyContractError, match="already exists"):
        run_replay_predictions(reviewed, output, **options)

    prediction = output / "day" / "predictions.jsonl"
    prediction.write_text("{}\n", encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="artifact hash mismatch"):
        verify_replay_predictions(output)


def test_replay_predictions_reject_model_format_mismatch_without_partial_output(
    tmp_path: Path,
) -> None:
    reviewed = _reviewed_annotation_fixture(tmp_path)
    model, manifest = _candidate_model_fixture(tmp_path)
    output = tmp_path / "predicted"
    with pytest.raises(AutonomyContractError, match="does not match model manifest"):
        run_replay_predictions(
            reviewed,
            output,
            model_path=model,
            model_manifest_path=manifest,
            model_format="ultralytics_raw",
            detector_factory=lambda config: _EmptyDetector(),
            image_decoder=_fixture_decoder,
        )
    assert not output.exists()


def test_replay_predictions_execute_shared_onnx_runtime_pipeline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("cv2")
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    intake = tmp_path / "intake"
    prepare_deployment_replay_set(
        _intake_sources(tmp_path / "sources", valid_images=True),
        intake,
        dataset_id="field-2026",
        dataset_version="v1",
    )
    workspace = tmp_path / "annotations.json"
    create_replay_annotation_workspace(intake, workspace)
    _complete_annotations(workspace, positive_all_detection_strata=True)
    reviewed = tmp_path / "reviewed"
    finalize_replay_annotation_workspace(intake, workspace, reviewed)
    model, manifest = create_synthetic_hil_model_bundle(
        tmp_path / "model", input_width=32, input_height=32
    )

    report = run_replay_predictions(
        reviewed,
        tmp_path / "predicted",
        model_path=model,
        model_manifest_path=manifest,
    )

    assert report["frame_count"] == 6
    assert report["runtime_inference_executed"] is True
    assert report["accuracy_validated"] is False
    receipt = json.loads(
        (tmp_path / "predicted" / "prediction-run.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["model"]["synthetic_hil_only"] is True
    assert receipt["claims"]["release_ready"] is False
    verified_prediction = verify_replay_predictions(tmp_path / "predicted")
    assert verified_prediction["production_approved"] is False
    predictions = [
        json.loads(line)
        for line in (tmp_path / "predicted" / "day" / "predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert predictions[0]["detections"][0]["label"] == "flame"

    predicted_root = tmp_path / "predicted"
    accuracy = evaluate_replay_set(
        predicted_root / "replay-set.manifest.json",
        prediction_run_receipt=predicted_root / "prediction-run.receipt.json",
    )
    assert accuracy["model_binding"]["model_id"] == "synthetic-fire-nx6-hil"
    assert accuracy["prediction_run_binding"]["receipt_canonical_hash"] == report["canonical_hash"]
    assert accuracy["prediction_run_binding"]["runtime_inference_executed"] is True
    assert accuracy["prediction_run_binding"]["production_approved"] is False
    assert accuracy["prediction_run_binding"]["synthetic_hil_only"] is True
    assert accuracy["accuracy_validated"] is False
    accuracy_path = predicted_root / "accuracy-report.json"
    accuracy_path.write_text(json.dumps(accuracy), encoding="utf-8")
    readiness = check_replay_set_readiness(
        predicted_root / "replay-set.manifest.json",
        accuracy_report_path=accuracy_path,
    )
    assert readiness["accuracy_report"]["recomputed"] is True
    assert not any(
        "fresh evaluator recomputation" in reason for reason in readiness["blocking_reasons"]
    )

    with pytest.raises(ValueError, match="conflicts with verified prediction run receipt"):
        evaluate_replay_set(
            predicted_root / "replay-set.manifest.json",
            prediction_run_receipt=predicted_root / "prediction-run.receipt.json",
            model_id="substituted-model",
        )

    evaluated = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_replay_set.py",
            str(predicted_root / "replay-set.manifest.json"),
            "--prediction-run-receipt",
            str(predicted_root / "prediction-run.receipt.json"),
            "--report-only",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert evaluated.returncode == 0, evaluated.stderr
    assert (
        json.loads(evaluated.stdout)["prediction_run_binding"]["receipt_canonical_hash"]
        == report["canonical_hash"]
    )

    assert (
        cli_main(
            [
                "evaluate-replay-set",
                str(predicted_root / "replay-set.manifest.json"),
                "--prediction-run-receipt",
                str(predicted_root / "prediction-run.receipt.json"),
                "--report-only",
            ]
        )
        == 0
    )
    installed_cli_report = json.loads(capsys.readouterr().out)
    assert (
        installed_cli_report["prediction_run_binding"]["receipt_canonical_hash"]
        == report["canonical_hash"]
    )

    reviewed_accuracy = evaluate_replay_set(
        predicted_root / "replay-set.manifest.json",
        reviewed=True,
        reviewer="accuracy-reviewer",
        prediction_run_receipt=predicted_root / "prediction-run.receipt.json",
    )
    assert reviewed_accuracy["gate_passed"] is True
    reviewed_accuracy_path = predicted_root / "accuracy-reviewed.json"
    reviewed_accuracy_path.write_text(json.dumps(reviewed_accuracy), encoding="utf-8")
    calibration = calibrate_replay_set_thresholds(
        predicted_root / "replay-set.manifest.json",
        thresholds=(0.25, 0.50, 0.75),
        reviewed=True,
        reviewer="threshold-reviewer",
        prediction_run_receipt=predicted_root / "prediction-run.receipt.json",
        accuracy_report_path=reviewed_accuracy_path,
    )
    assert calibration["provenance_complete"] is True
    assert calibration["prediction_run_binding"]["production_approved"] is False
    assert calibration["accuracy_report_binding"]["gate_passed"] is True
    assert (
        calibration["accuracy_report_binding"]["prediction_receipt_canonical_hash"]
        == report["canonical_hash"]
    )
    calibration_path = predicted_root / "threshold-calibration.json"
    calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
    calibration_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "configs/schemas/replay-set-calibration-report.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(calibration_schema)
    Draft202012Validator(calibration_schema).validate(calibration)
    calibration_verification = verify_replay_set_calibration_report(calibration_path)
    assert calibration_verification["gate_passed"] is True
    assert calibration_verification["report_provenance_complete"] is True
    assert calibration_verification["recomputed_provenance_complete"] is True

    verification_path = predicted_root / "threshold-calibration-verification.json"
    verification_path.write_text(json.dumps(calibration_verification), encoding="utf-8")
    evidence: dict[str, Path] = {}
    statuses: dict[str, dict[str, str]] = {}
    for mode in ("replay", "sim", "hil", "real"):
        evidence_path = tmp_path / "evidence" / f"{mode}.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "runtime_mode": mode,
                    "gate_passed": True,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = evidence_path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
        }
    parity = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}
    release_gate_path = tmp_path / "release" / "release-gate.json"
    release_gate = build_release_gate_manifest(
        evidence,
        parity=parity,
        statuses=statuses,
        output_path=release_gate_path,
        replay_set_manifest=predicted_root / "replay-set.manifest.json",
        replay_set_accuracy_evidence=reviewed_accuracy_path,
        replay_set_calibration_verification=verification_path,
    )
    calibration_binding = release_gate["replay_set"]["calibration_verification"]
    assert calibration_binding["report_provenance_complete"] is True
    assert calibration_binding["recomputed_provenance_complete"] is True
    write_release_gate_manifest(release_gate_path, release_gate)
    release_evaluation = evaluate_release_gate(release_gate_path)
    assert release_evaluation["replay_set"]["calibration_verification"]["passed"] is True
    assert (
        release_evaluation["replay_set"]["calibration_verification"]["provenance_complete"] is True
    )

    assert (
        cli_main(
            [
                "calibrate-replay-set",
                str(predicted_root / "replay-set.manifest.json"),
                "--thresholds",
                "0.25,0.50,0.75",
                "--reviewed",
                "--reviewer",
                "threshold-reviewer",
                "--prediction-run-receipt",
                str(predicted_root / "prediction-run.receipt.json"),
                "--accuracy-report",
                str(reviewed_accuracy_path),
                "--report-only",
            ]
        )
        == 0
    )
    cli_calibration = json.loads(capsys.readouterr().out)
    assert cli_calibration["accuracy_report_binding"] == calibration["accuracy_report_binding"]

    with pytest.raises(ReplaySetCalibrationError, match="must be supplied together"):
        calibrate_replay_set_thresholds(
            predicted_root / "replay-set.manifest.json",
            thresholds=(0.25, 0.50, 0.75),
            prediction_run_receipt=predicted_root / "prediction-run.receipt.json",
        )

    reviewed_accuracy["reviewer"] = "tampered-reviewer"
    reviewed_accuracy_path.write_text(json.dumps(reviewed_accuracy), encoding="utf-8")
    tampered_verification = verify_replay_set_calibration_report(calibration_path)
    assert tampered_verification["gate_passed"] is False
    assert any(
        "differs from deterministic re-computation" in reason
        for reason in tampered_verification["reasons"]
    )


def test_replay_set_builder_binds_all_six_strata_and_reports_pending_accuracy(
    tmp_path: Path,
) -> None:
    for stratum in REPLAY_SET_STRATA:
        _write_stratum(tmp_path, stratum, with_predictions=stratum == "day")

    manifest = build_replay_set_manifest(
        tmp_path,
        tmp_path / "replay-set.manifest.json",
        dataset_id="fire-smoke-field-2026",
        dataset_version="v1",
        source_kind="deployment_recording",
        annotations_reviewed=True,
    )

    assert manifest.is_complete is True
    assert manifest.missing_strata == ()
    assert manifest.claims["contains_ground_truth"] is True
    assert manifest.claims["contains_predictions"] is False
    assert manifest.claims["accuracy_validated"] is False
    assert manifest.coverage()["night"] == {"entry_count": 1, "frame_count": 1}
    serialized = json.loads((tmp_path / "replay-set.manifest.json").read_text(encoding="utf-8"))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "configs/schemas/replay-set-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(serialized)
    assert all(
        entry["source_attestation"]["path"] == f"{entry['entry_id']}/{ATTESTATION_NAME}"
        for entry in serialized["entries"]
    )
    assert (
        manifest.canonical_hash
        == load_replay_set_manifest(tmp_path / "replay-set.manifest.json").canonical_hash
    )


def test_deployment_replay_manifest_requires_and_reopens_bound_source_attestation(
    tmp_path: Path,
) -> None:
    _write_stratum(tmp_path, "day")
    attestation_path = tmp_path / "day" / ATTESTATION_NAME
    attestation_payload = attestation_path.read_bytes()
    attestation_path.unlink()
    with pytest.raises(AutonomyContractError, match="requires deployment-recording-source"):
        build_replay_set_manifest(
            tmp_path,
            tmp_path / "missing-attestation.manifest.json",
            dataset_id="field",
            dataset_version="v1",
            required_strata=("day",),
            source_kind="deployment_recording",
            annotations_reviewed=True,
        )

    attestation_path.write_bytes(attestation_payload)
    manifest_path = tmp_path / "bound.manifest.json"
    build_replay_set_manifest(
        tmp_path,
        manifest_path,
        dataset_id="field",
        dataset_version="v1",
        required_strata=("day",),
        source_kind="deployment_recording",
        annotations_reviewed=True,
    )
    attestation_path.write_bytes(attestation_payload + b"\n")
    with pytest.raises(AutonomyContractError, match="source_attestation .* mismatch"):
        load_replay_set_manifest(manifest_path)

    attestation_path.write_bytes(attestation_payload)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["entries"][0].pop("source_attestation")
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="requires source_attestation"):
        load_replay_set_manifest(manifest_path)


def test_replay_set_allows_pending_collection_but_require_complete_fails(
    tmp_path: Path,
) -> None:
    _write_stratum(tmp_path, "day")
    manifest = build_replay_set_manifest(
        tmp_path,
        tmp_path / "pending.manifest.json",
        dataset_id="pending",
        dataset_version="v1",
        allow_incomplete=True,
    )
    assert manifest.is_complete is False
    assert set(manifest.missing_strata) == set(REPLAY_SET_STRATA) - {"day"}

    with pytest.raises(AutonomyContractError, match="deployment gate is incomplete"):
        load_replay_set_manifest(tmp_path / "pending.manifest.json", require_complete=True)


def test_replay_set_rejects_claim_tampering_and_ground_truth_misalignment(
    tmp_path: Path,
) -> None:
    _write_stratum(tmp_path, "day")
    build_replay_set_manifest(
        tmp_path,
        tmp_path / "tampered.manifest.json",
        dataset_id="tampered",
        dataset_version="v1",
        allow_incomplete=True,
    )
    manifest_path = tmp_path / "tampered.manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["claims"]["deployment_domain_evidence_complete"] = True
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="claims do not match"):
        load_replay_set_manifest(manifest_path)

    raw["claims"]["deployment_domain_evidence_complete"] = False
    (tmp_path / "day" / "ground-truth.jsonl").write_text(
        '{"frame_id":"wrong-frame","objects":[]}\n', encoding="utf-8"
    )
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="ground_truth .* mismatch"):
        load_replay_set_manifest(manifest_path)


def test_replay_set_manifest_parser_rejects_duplicate_keys_and_nonfinite_json(
    tmp_path: Path,
) -> None:
    """Release evidence must not depend on last-key-wins JSON semantics."""

    _write_stratum(tmp_path, "day")
    build_replay_set_manifest(
        tmp_path,
        tmp_path / "strict.manifest.json",
        dataset_id="strict",
        dataset_version="v1",
        allow_incomplete=True,
    )
    manifest_path = tmp_path / "strict.manifest.json"

    duplicate_payload = (
        '{"schema_version":1,"dataset_id":"strict","dataset_id":"shadow",'
        '"dataset_version":"v1","class_names":["flame","smoke"],'
        '"required_strata":["day","haze","night","no_fire","reflection",'
        '"smoke_only"],"entries":[],"claims":{"contains_ground_truth":false,'
        '"contains_predictions":false,"deployment_domain_evidence_complete":false,'
        '"accuracy_validated":false}}'
    )
    manifest_path.write_text(duplicate_payload, encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="duplicate key 'dataset_id'"):
        load_replay_set_manifest(manifest_path)

    nonfinite_payload = (
        '{"schema_version":NaN,"dataset_id":"strict","dataset_version":"v1",'
        '"class_names":["flame","smoke"],"required_strata":["day","haze",'
        '"night","no_fire","reflection","smoke_only"],"entries":[],'
        '"claims":{"contains_ground_truth":false,"contains_predictions":false,'
        '"deployment_domain_evidence_complete":false,"accuracy_validated":false}}'
    )
    manifest_path.write_text(nonfinite_payload, encoding="utf-8")
    with pytest.raises(AutonomyContractError, match="non-finite JSON number NaN"):
        load_replay_set_manifest(manifest_path)

    bom_payload = json.dumps(
        {
            "schema_version": 1,
            "dataset_id": "bom",
            "dataset_version": "v1",
            "class_names": ["flame", "smoke"],
            "required_strata": list(REPLAY_SET_STRATA),
            "entries": [],
            "claims": {
                "contains_ground_truth": False,
                "contains_predictions": False,
                "deployment_domain_evidence_complete": False,
                "accuracy_validated": False,
            },
        },
        separators=(",", ":"),
    )
    manifest_path.write_bytes(("\ufeff" + bom_payload).encode("utf-8"))
    loaded = load_replay_set_manifest(manifest_path)
    assert loaded.dataset_id == "bom"


def test_replay_set_rejects_manifest_and_artifact_symlinks(tmp_path: Path) -> None:
    _write_stratum(tmp_path, "day")
    build_replay_set_manifest(
        tmp_path,
        tmp_path / "symlink.manifest.json",
        dataset_id="symlink",
        dataset_version="v1",
        allow_incomplete=True,
    )
    manifest_path = tmp_path / "symlink.manifest.json"

    manifest_alias = tmp_path / "manifest-alias.json"
    try:
        os.symlink(manifest_path, manifest_alias)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    with pytest.raises(AutonomyContractError, match="manifest path must not be a symbolic link"):
        load_replay_set_manifest(manifest_alias)

    truth_path = tmp_path / "day" / "ground-truth.jsonl"
    truth_target = tmp_path / "day" / "ground-truth-target.jsonl"
    truth_target.write_bytes(truth_path.read_bytes())
    truth_path.unlink()
    os.symlink(truth_target, truth_path)
    with pytest.raises(
        AutonomyContractError,
        match="ground_truth path must not be a symbolic link",
    ):
        load_replay_set_manifest(manifest_path)


def test_validate_replay_set_cli_distinguishes_structural_and_complete_gates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_stratum(tmp_path, "day")
    build_replay_set_manifest(
        tmp_path,
        tmp_path / "cli.manifest.json",
        dataset_id="cli",
        dataset_version="v1",
        allow_incomplete=True,
    )
    report_path = tmp_path / "report.json"
    assert (
        cli_main(
            [
                "validate-replay-set",
                str(tmp_path / "cli.manifest.json"),
                "--require-complete",
                "--out",
                str(report_path),
            ]
        )
        == 2
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["structurally_valid"] is True
    assert report["gate_passed"] is False
    assert "night" in report["missing_strata"]
    assert "replay_set_manifest_validated" in capsys.readouterr().out


def test_replay_set_wrapper_runs_from_clean_checkout_without_pythonpath() -> None:
    """The field/replay operator can inspect the wrapper before installation."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/create_replay_set_manifest.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "strict six-stratum detector replay-set manifest" in completed.stdout


def test_sim_replay_set_fixture_wrapper_runs_from_clean_checkout() -> None:
    """The SIM staging wrapper must be callable without an installed package."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/create_sim_replay_set_fixture.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "six-stratum SIM replay-set" in completed.stdout

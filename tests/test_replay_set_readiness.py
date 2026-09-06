from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from multidetect.backends import RawSensorReplayRecording
from multidetect.cli import main as cli_main
from multidetect.deployment_recording_attestation import (
    write_deployment_recording_attestation,
)
from multidetect.replay_set import REPLAY_SET_STRATA, build_replay_set_manifest
from multidetect.replay_set_evaluation import evaluate_replay_set
from multidetect.replay_set_readiness import check_replay_set_readiness

REPO_ROOT = Path(__file__).resolve().parents[1]
SIM_MANIFEST = (
    REPO_ROOT
    / "artifacts"
    / "evaluation"
    / "terminal-continuation-v34"
    / "replay-set-sim-gazebo-2026"
    / "replay-set.manifest.json"
)


def _write_stratum(
    root: Path,
    stratum: str,
    *,
    positive: bool,
    with_predictions: bool = True,
) -> None:
    directory = root / stratum
    directory.mkdir(parents=True)
    frame_id = f"{stratum}-frame-0001"
    image_payload = f"{stratum}-image".encode()
    (directory / "frame-0001.png").write_bytes(image_payload)
    events = [
        {
            "schema_version": 1,
            "kind": "telemetry",
            "observed_at_s": 1.0,
            "telemetry": {
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
            },
        },
        {
            "schema_version": 1,
            "kind": "frame",
            "frame_id": frame_id,
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
    event_payload = "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
    ).encode("utf-8")
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
    (directory / "recording.manifest.json").write_text(
        json.dumps(
            {
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
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    write_deployment_recording_attestation(
        directory,
        stratum=stratum,
        captured_by="fixture-operator",
        capture_device_id="fixture-camera",
        capture_site="fixture-field",
        source_type="local_camera",
        started_at_utc="2026-08-25T00:00:00Z",
        ended_at_utc="2026-08-25T00:00:01Z",
    )
    objects = [{"label": "flame", "bbox": [0.1, 0.1, 0.8, 0.8]}] if positive else []
    (directory / "ground-truth.jsonl").write_text(
        json.dumps({"frame_id": frame_id, "objects": objects}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if with_predictions:
        detections = (
            [{"label": "flame", "confidence": 0.95, "bbox": [0.1, 0.1, 0.8, 0.8]}]
            if positive
            else []
        )
        (directory / "predictions.jsonl").write_text(
            json.dumps(
                {
                    "frame_id": frame_id,
                    "inference_latency_ms": 2.0,
                    "detections": detections,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )


def _build_complete_fixture(
    root: Path,
    *,
    positive_strata: set[str] | None = None,
) -> Path:
    for stratum in REPLAY_SET_STRATA:
        positive = (
            stratum in positive_strata
            if positive_strata is not None
            else stratum != "no_fire"
        )
        _write_stratum(root, stratum, positive=positive)
    manifest_path = root / "replay-set.manifest.json"
    build_replay_set_manifest(
        root,
        manifest_path,
        dataset_id="readiness-fixture",
        dataset_version="v1",
        source_kind="deployment_recording",
        annotations_reviewed=True,
    )
    return manifest_path


def test_sim_fixture_is_reported_pending_with_actionable_reasons() -> None:
    report = check_replay_set_readiness(SIM_MANIFEST)

    assert report["checks"]["required_strata"] is True
    assert report["checks"]["deployment_recording_source"] is False
    assert report["checks"]["annotations_reviewed"] is False
    assert report["checks"]["positive_truth_evidence"] is False
    assert report["checks"]["positive_prediction_evidence"] is False
    assert report["gate_passed"] is False
    assert any("synthetic_demo" in reason for reason in report["blocking_reasons"])
    assert any("genuine positive" in action for action in report["next_actions"])


def test_reviewed_positive_fixture_and_fresh_accuracy_report_pass_readiness(tmp_path: Path) -> None:
    manifest_path = _build_complete_fixture(tmp_path)
    accuracy = evaluate_replay_set(manifest_path, reviewed=True, reviewer="qa-team")
    accuracy_path = tmp_path / "accuracy.json"
    accuracy_path.write_text(json.dumps(accuracy, indent=2) + "\n", encoding="utf-8")

    report = check_replay_set_readiness(
        manifest_path,
        accuracy_report_path=accuracy_path,
    )

    assert report["manifest_gate_passed"] is True
    assert report["accuracy_gate_passed"] is True
    assert report["accuracy_report"]["recomputed"] is True
    assert report["accuracy_report"]["gate_passed"] is True
    assert report["blocking_reasons"] == []
    assert report["next_actions"] == []
    assert report["gate_passed"] is True


def test_readiness_reports_missing_positive_stratum_coverage(tmp_path: Path) -> None:
    manifest_path = _build_complete_fixture(tmp_path, positive_strata={"day"})
    accuracy = evaluate_replay_set(manifest_path, reviewed=True, reviewer="qa-team")
    accuracy_path = tmp_path / "accuracy.json"
    accuracy_path.write_text(json.dumps(accuracy, indent=2) + "\n", encoding="utf-8")

    report = check_replay_set_readiness(
        manifest_path,
        accuracy_report_path=accuracy_path,
    )

    assert report["checks"]["positive_truth_by_stratum"] is False
    assert report["checks"]["positive_prediction_by_stratum"] is False
    assert report["accuracy_gate_passed"] is False
    assert report["gate_passed"] is False
    assert any("positive ground-truth coverage" in reason for reason in report["blocking_reasons"])
    assert any("every positive deployment stratum" in action for action in report["next_actions"])


def test_readiness_rejects_report_tampering_after_hash_binding(tmp_path: Path) -> None:
    manifest_path = _build_complete_fixture(tmp_path)
    accuracy = evaluate_replay_set(manifest_path, reviewed=True, reviewer="qa-team")
    accuracy["metrics"]["overall"]["precision"] = 0.5
    accuracy_path = tmp_path / "tampered-accuracy.json"
    accuracy_path.write_text(json.dumps(accuracy, indent=2) + "\n", encoding="utf-8")

    report = check_replay_set_readiness(
        manifest_path,
        accuracy_report_path=accuracy_path,
    )

    assert report["manifest_gate_passed"] is True
    assert report["accuracy_report"]["manifest_bound"] is True
    assert report["accuracy_report"]["recomputed"] is True
    assert report["accuracy_gate_passed"] is False
    assert report["gate_passed"] is False
    assert any("fresh evaluator recomputation" in reason for reason in report["blocking_reasons"])


def test_readiness_rejects_duplicate_and_nonfinite_accuracy_report_json(
    tmp_path: Path,
) -> None:
    manifest_path = _build_complete_fixture(tmp_path)
    accuracy = evaluate_replay_set(manifest_path, reviewed=True, reviewer="qa-team")
    accuracy_path = tmp_path / "accuracy.json"
    accuracy_path.write_text(json.dumps(accuracy, indent=2) + "\n", encoding="utf-8")

    duplicate = accuracy_path.read_text(encoding="utf-8").replace(
        '  "reviewed": true,\n',
        '  "reviewed": true,\n  "reviewed": false,\n',
        1,
    )
    accuracy_path.write_text(duplicate, encoding="utf-8")
    duplicate_report = check_replay_set_readiness(
        manifest_path,
        accuracy_report_path=accuracy_path,
    )
    assert duplicate_report["accuracy_report"]["recomputed"] is False
    assert any(
        "accuracy report contains duplicate key 'reviewed'" in reason
        for reason in duplicate_report["blocking_reasons"]
    )

    # Rebuild from the clean evaluator output so the second case is isolated
    # from the duplicate-key rejection above.
    clean_accuracy = json.dumps(accuracy, indent=2) + "\n"
    nonfinite = clean_accuracy.replace(
        '    "iou_threshold": 0.5,\n',
        '    "iou_threshold": NaN,\n',
        1,
    )
    accuracy_path.write_text(nonfinite, encoding="utf-8")
    nonfinite_report = check_replay_set_readiness(
        manifest_path,
        accuracy_report_path=accuracy_path,
    )
    assert nonfinite_report["accuracy_report"]["recomputed"] is False
    assert any(
        "accuracy report contains non-finite JSON number NaN" in reason
        for reason in nonfinite_report["blocking_reasons"]
    )


def test_readiness_cli_report_only_and_clean_checkout_wrapper() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/check_replay_set_readiness.py", "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "emit a readiness" in completed.stdout

    assert (
        cli_main(
            [
                "check-replay-set-readiness",
                str(SIM_MANIFEST),
                "--report-only",
            ]
        )
        == 0
    )

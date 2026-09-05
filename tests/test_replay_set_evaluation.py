from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.backends import RawSensorReplayRecording
from multidetect.cli import main as cli_main
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    build_release_gate_manifest,
    write_release_gate_manifest,
)
from multidetect.replay_set import REPLAY_SET_STRATA, build_replay_set_manifest
from multidetect.replay_set_evaluation import (
    evaluate_replay_set,
    positive_strata_coverage_is_complete,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"


def _write_stratum(
    root: Path,
    stratum: str,
    *,
    with_prediction: bool,
    with_positive: bool = True,
) -> None:
    directory = root / stratum
    directory.mkdir(parents=True)
    image_payload = f"{stratum}-image".encode()
    (directory / "frame-0001.png").write_bytes(image_payload)
    frame_id = f"{stratum}-frame-0001"
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
    objects = (
        []
        if stratum == "no_fire" or not with_positive
        else [{"label": "flame", "bbox": [0.1, 0.1, 0.8, 0.8]}]
    )
    (directory / "ground-truth.jsonl").write_text(
        json.dumps({"frame_id": frame_id, "objects": objects}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if with_prediction:
        detections = (
            []
            if stratum == "no_fire" or not with_positive
            else [{"label": "flame", "confidence": 0.95, "bbox": [0.1, 0.1, 0.8, 0.8]}]
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


def _manifest(
    tmp_path: Path,
    *,
    predictions: bool,
    with_positive: bool = True,
    positive_strata: set[str] | None = None,
) -> Path:
    for stratum in REPLAY_SET_STRATA:
        _write_stratum(
            tmp_path,
            stratum,
            with_prediction=predictions,
            with_positive=(
                stratum in positive_strata if positive_strata is not None else with_positive
            ),
        )
    destination = tmp_path / "replay-set.manifest.json"
    build_replay_set_manifest(
        tmp_path,
        destination,
        dataset_id="field-accuracy",
        dataset_version="v1",
        source_kind="deployment_recording",
        annotations_reviewed=True,
    )
    return destination


def test_replay_set_accuracy_report_is_pending_until_explicit_review(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, predictions=True)

    report = evaluate_replay_set(manifest)

    assert report["structurally_valid"] is True
    assert report["predictions_complete"] is True
    assert report["metrics_evaluated"] is True
    assert report["metrics_gate_passed"] is True
    assert report["accuracy_validated"] is False
    assert report["gate_passed"] is False
    assert "independent accuracy review is not acknowledged" in report["failure_reasons"]

    reviewed = evaluate_replay_set(manifest, reviewed=True, reviewer="qa-team")
    assert reviewed["reviewed"] is True
    assert reviewed["reviewer"] == "qa-team"
    assert reviewed["accuracy_validated"] is True
    assert reviewed["gate_passed"] is True
    assert reviewed["metrics"]["overall"]["precision"] == 1.0
    assert reviewed["metrics"]["overall"]["recall"] == 1.0


def test_replay_set_accuracy_report_can_bind_the_exact_model_artifact(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "replay", predictions=True)
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"candidate-model-v1")
    model_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()

    report = evaluate_replay_set(
        manifest,
        reviewed=True,
        reviewer="qa-team",
        model_artifact_sha256=model_sha256,
        model_id="fire-candidate",
        model_version="candidate-v1",
    )

    assert report["model_binding"] == {
        "artifact_sha256": model_sha256,
        "model_id": "fire-candidate",
        "model_version": "candidate-v1",
    }


def test_replay_set_accuracy_model_metadata_requires_an_artifact_hash(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "replay", predictions=True)

    with pytest.raises(ValueError, match="model_artifact_sha256"):
        evaluate_replay_set(manifest, model_id="fire-candidate")


def test_replay_set_accuracy_report_keeps_missing_predictions_closed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, predictions=False)

    report = evaluate_replay_set(manifest, reviewed=True, reviewer="qa-team")

    assert report["predictions_complete"] is False
    assert report["metrics_evaluated"] is False
    assert report["accuracy_validated"] is False
    assert report["gate_passed"] is False
    assert any("predictions are missing" in reason for reason in report["failure_reasons"])


def test_replay_set_accuracy_cli_writes_hash_bound_report_and_exit_state(
    tmp_path: Path,
    capsys,
) -> None:
    manifest = _manifest(tmp_path, predictions=True)
    output = tmp_path / "accuracy.json"

    assert (
        cli_main(
            [
                "evaluate-replay-set",
                str(manifest),
                "--reviewed",
                "--reviewer",
                "qa-team",
                "--minimum-precision",
                "1.0",
                "--minimum-recall",
                "1.0",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert emitted == written
    assert written["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert written["canonical_sha256"]
    assert written["accuracy_validated"] is True


def test_replay_set_accuracy_threshold_failure_is_explicit(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, predictions=True)

    report = evaluate_replay_set(
        manifest,
        maximum_latency_p95_ms=1.0,
        reviewed=True,
        reviewer="qa-team",
    )

    assert report["accuracy_validated"] is False
    assert report["metrics_gate_passed"] is False
    assert any("latency p95" in reason for reason in report["failure_reasons"])


def test_replay_set_accuracy_rejects_empty_positive_evidence_even_when_reviewed(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, predictions=True, with_positive=False)

    report = evaluate_replay_set(manifest, reviewed=True, reviewer="qa-team")

    assert report["structurally_valid"] is True
    assert report["predictions_complete"] is True
    assert report["metrics_evaluated"] is True
    assert report["metrics_gate_passed"] is False
    assert report["reviewed"] is True
    assert report["accuracy_validated"] is False
    assert report["gate_passed"] is False
    assert any(
        "precision/recall are undefined" in reason
        for reason in report["failure_reasons"]
    )


def test_replay_set_accuracy_requires_positive_truth_and_prediction_in_each_positive_stratum(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, predictions=True, positive_strata={"day"})

    report = evaluate_replay_set(manifest, reviewed=True, reviewer="qa-team")

    assert report["metrics_evaluated"] is True
    assert report["positive_strata"] == ["day", "night", "haze", "reflection", "smoke_only"]
    assert report["positive_strata_coverage_passed"] is False
    assert report["positive_stratum_coverage"]["day"] == {
        "positive_truth_object_count": 1,
        "positive_prediction_count": 1,
    }
    assert report["positive_stratum_coverage"]["night"] == {
        "positive_truth_object_count": 0,
        "positive_prediction_count": 0,
    }
    assert report["metrics_gate_passed"] is False
    assert report["accuracy_validated"] is False
    assert any(
        "night stratum contains no positive" in reason
        for reason in report["failure_reasons"]
    )


def test_positive_strata_coverage_predicate_rejects_missing_or_zero_counts(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, predictions=True)
    report = evaluate_replay_set(manifest, reviewed=True, reviewer="qa-team")

    assert positive_strata_coverage_is_complete(report) is True
    missing = dict(report)
    missing.pop("positive_stratum_coverage")
    assert positive_strata_coverage_is_complete(missing) is False
    zero = dict(report)
    zero["positive_stratum_coverage"] = {
        **report["positive_stratum_coverage"],
        "night": {
            "positive_truth_object_count": 0,
            "positive_prediction_count": 1,
        },
    }
    assert positive_strata_coverage_is_complete(zero) is False


def test_replay_set_accuracy_wrapper_runs_from_clean_checkout() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/evaluate_replay_set.py", "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "hash-bound six-stratum replay-set" in completed.stdout


def test_release_gate_can_bind_reviewed_accuracy_evidence(tmp_path: Path) -> None:
    replay_manifest = _manifest(tmp_path / "replay-set", predictions=True)
    accuracy_report = evaluate_replay_set(
        replay_manifest,
        reviewed=True,
        reviewer="qa-team",
    )
    accuracy_path = tmp_path / "replay-set-accuracy.json"
    accuracy_path.write_text(
        json.dumps(accuracy_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence = {}
    statuses = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = evidence_directory / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "gate_passed": True,
                    "runtime_mode": mode,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
        }
    release_manifest = tmp_path / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity={field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)},
        statuses=statuses,
        output_path=release_manifest,
        version_manifest_path=VERSION_MANIFEST,
        replay_set_manifest=replay_manifest,
        replay_set_accuracy_evidence=accuracy_path,
    )
    write_release_gate_manifest(release_manifest, document)
    assert document["replay_set"]["accuracy_evidence"]["accuracy_validated"] is True
    report = evaluate_release_gate(release_manifest, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is True
    assert report["replay_set"]["passed"] is True
    assert report["replay_set"]["accuracy_evidence"]["accuracy_validated"] is True


def test_release_gate_rejects_pending_accuracy_evidence(tmp_path: Path) -> None:
    replay_manifest = _manifest(tmp_path / "replay-set", predictions=True)
    accuracy_report = evaluate_replay_set(replay_manifest)
    accuracy_path = tmp_path / "replay-set-accuracy-pending.json"
    accuracy_path.write_text(
        json.dumps(accuracy_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence = {}
    statuses = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = evidence_directory / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "gate_passed": True,
                    "runtime_mode": mode,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
        }
    release_manifest = tmp_path / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity={field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)},
        statuses=statuses,
        output_path=release_manifest,
        version_manifest_path=VERSION_MANIFEST,
        replay_set_manifest=replay_manifest,
        replay_set_accuracy_evidence=accuracy_path,
    )
    write_release_gate_manifest(release_manifest, document)
    report = evaluate_release_gate(release_manifest, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["replay_set"]["passed"] is False
    assert any("accuracy evidence gate_passed is false" in reason
               for reason in report["replay_set"]["reasons"])


def test_release_gate_rejects_claimed_accuracy_with_undefined_metrics(tmp_path: Path) -> None:
    replay_manifest = _manifest(tmp_path / "replay-set", predictions=True)
    accuracy_report = evaluate_replay_set(
        replay_manifest,
        reviewed=True,
        reviewer="qa-team",
    )
    accuracy_path = tmp_path / "replay-set-accuracy.json"
    accuracy_report["metrics"] = {
        "frame_count": 30,
        "overall": {
            "label": "__overall__",
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": 0,
            "precision": None,
            "recall": None,
        },
    }
    accuracy_report["metrics_gate_passed"] = True
    accuracy_report["positive_strata_coverage_passed"] = False
    accuracy_report["accuracy_validated"] = True
    accuracy_report["gate_passed"] = True
    accuracy_report["failure_reasons"] = []
    accuracy_path.write_text(
        json.dumps(accuracy_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence = {}
    statuses = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = evidence_directory / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "gate_passed": True,
                    "runtime_mode": mode,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
        }
    # The writer accepts only a semantically valid report, so build a normal
    # manifest first, then replace the bound report with the adversarial one.
    valid_accuracy_path = tmp_path / "valid-accuracy.json"
    valid_accuracy_path.write_text(
        json.dumps(evaluate_replay_set(replay_manifest, reviewed=True, reviewer="qa-team")),
        encoding="utf-8",
    )
    release_manifest = tmp_path / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity={field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)},
        statuses=statuses,
        output_path=release_manifest,
        version_manifest_path=VERSION_MANIFEST,
        replay_set_manifest=replay_manifest,
        replay_set_accuracy_evidence=valid_accuracy_path,
    )
    document["replay_set"]["accuracy_evidence"]["path"] = accuracy_path.name
    document["replay_set"]["accuracy_evidence"]["sha256"] = hashlib.sha256(
        accuracy_path.read_bytes()
    ).hexdigest()
    release_manifest.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = evaluate_release_gate(release_manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["gate_passed"] is False
    assert any(
        "aggregate precision/recall are undefined" in reason
        for reason in report["replay_set"]["reasons"]
    )
    assert any(
        "positive-stratum coverage is incomplete" in reason
        for reason in report["replay_set"]["reasons"]
    )

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.backends import RawSensorReplayRecording
from multidetect.cli import build_parser
from multidetect.cli import main as cli_main
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    ReleaseGateManifestError,
    build_release_gate_manifest,
    write_release_gate_manifest,
)
from multidetect.replay_set import REPLAY_SET_STRATA, build_replay_set_manifest
from multidetect.replay_set_calibration import (
    ReplaySetCalibrationError,
    calibrate_replay_set_thresholds,
    verify_replay_set_calibration_report,
)


def _write_entry(
    root: Path,
    stratum: str,
    *,
    with_prediction: bool = True,
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
        if stratum == "no_fire"
        else [{"label": "flame", "bbox": [0.1, 0.1, 0.8, 0.8]}]
    )
    (directory / "ground-truth.jsonl").write_text(
        json.dumps({"frame_id": frame_id, "objects": objects}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    if with_prediction:
        detections = (
            []
            if stratum == "no_fire"
            else [
                {
                    "label": "flame",
                    "confidence": 0.90,
                    "bbox": [0.1, 0.1, 0.8, 0.8],
                }
            ]
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


def _manifest(tmp_path: Path, *, missing_prediction: str | None = None) -> Path:
    for stratum in REPLAY_SET_STRATA:
        _write_entry(
            tmp_path,
            stratum,
            with_prediction=stratum != missing_prediction,
        )
    destination = tmp_path / "replay-set.manifest.json"
    build_replay_set_manifest(
        tmp_path,
        destination,
        dataset_id="calibration-fixture",
        dataset_version="v0.3.0-pre.8",
        source_kind="deployment_recording",
        annotations_reviewed=True,
    )
    return destination


def test_replay_set_calibration_is_hash_bound_and_reports_strata(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    report = calibrate_replay_set_thresholds(
        manifest,
        thresholds=(0.3, 0.6, 0.9),
        current_thresholds={"flame": 0.72, "smoke": 0.60},
        reviewed=True,
        reviewer="qa-team",
    )

    assert report["event"] == "replay_set_threshold_calibration_completed"
    assert report["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert report["deployment_domain_evidence_complete"] is True
    assert report["predictions_complete"] is True
    assert report["promotion_ready"] is True
    assert set(report["coverage"]) == set(REPLAY_SET_STRATA)
    assert all(report["coverage"][name]["entry_count"] == 1 for name in REPLAY_SET_STRATA)
    assert report["stability_tolerance"] == 0.10
    flame_stability = report["stability"]["flame"]
    assert flame_stability["recommended_strata"] == [
        "day",
        "night",
        "haze",
        "reflection",
        "smoke_only",
    ]
    assert flame_stability["missing_recommendation_strata"] == ["no_fire"]
    assert flame_stability["threshold_range"] == 0.0
    assert flame_stability["coverage_ratio"] == pytest.approx(5 / 6)
    assert flame_stability["stable_at_tolerance"] is True
    assert report["calibration"]["recommendations"]
    assert report["calibration"]["frame_count"] == 6
    assert set(report["strata"]) == set(REPLAY_SET_STRATA)
    assert all(report["strata"][name]["available"] for name in REPLAY_SET_STRATA)
    assert report["strata"]["no_fire"]["calibration"]["recommendations"] == []


def test_replay_set_calibration_wrapper_runs_from_clean_checkout() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/calibrate_replay_set.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "hash-bound replay-set" in completed.stdout


def test_replay_set_calibration_verifier_wrapper_runs_from_clean_checkout() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/verify_replay_set_calibration.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Independently verify" in completed.stdout


def test_main_cli_exposes_replay_set_calibration_command(tmp_path: Path, capsys) -> None:
    manifest = _manifest(tmp_path)
    parsed = build_parser().parse_args(
        [
            "calibrate-replay-set",
            str(manifest),
            "--thresholds",
            "0.3,0.6,0.9",
            "--current-thresholds",
            "none",
            "--reviewed",
            "--reviewer",
            "qa-team",
            "--report-only",
        ]
    )
    assert parsed.command == "calibrate-replay-set"
    assert cli_main(
        [
            "calibrate-replay-set",
            str(manifest),
            "--thresholds",
            "0.3,0.6,0.9",
            "--current-thresholds",
            "none",
            "--reviewed",
            "--reviewer",
            "qa-team",
            "--report-only",
        ]
    ) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["event"] == "replay_set_threshold_calibration_completed"
    assert emitted["promotion_ready"] is True


def test_replay_set_calibration_verifier_recomputes_report_and_hashes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "source")
    report = calibrate_replay_set_thresholds(
        manifest,
        thresholds=(0.3, 0.6, 0.9),
        current_thresholds={"flame": 0.72, "smoke": 0.60},
        reviewed=True,
        reviewer="qa-team",
    )
    report_path = tmp_path / "source" / "threshold-calibration.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    verification = verify_replay_set_calibration_report(report_path)

    assert verification["event"] == "replay_set_threshold_calibration_verified"
    assert verification["gate_passed"] is True
    assert verification["report_promotion_ready"] is True
    assert verification["recomputed_promotion_ready"] is True
    assert verification["report_provenance_complete"] is False
    assert verification["recomputed_provenance_complete"] is False
    assert all(verification["checks"].values())

    overridden = verify_replay_set_calibration_report(
        report_path,
        manifest_path=manifest,
    )
    assert overridden["gate_passed"] is True
    assert all(overridden["checks"].values())


def test_replay_set_calibration_verifier_reads_pre_provenance_report(tmp_path: Path) -> None:
    """A pre-v112 report stays verifiable but cannot claim provenance completeness."""

    manifest = _manifest(tmp_path / "source")
    report = calibrate_replay_set_thresholds(
        manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=True,
        reviewer="qa-team",
    )
    report.pop("prediction_run_binding")
    report.pop("accuracy_report_binding")
    report.pop("provenance_complete")
    report_path = tmp_path / "source" / "legacy-threshold-calibration.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    verification = verify_replay_set_calibration_report(report_path)

    assert verification["gate_passed"] is True
    assert verification["checks"]["recomputed_report_matches"] is True
    assert verification["report_provenance_complete"] is False
    assert verification["recomputed_provenance_complete"] is False


def test_replay_set_calibration_verifier_rejects_tampered_stability(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "source")
    report = calibrate_replay_set_thresholds(
        manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=True,
        reviewer="qa-team",
    )
    report["stability"]["flame"]["threshold_range"] = 0.5
    report_path = tmp_path / "source" / "tampered.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    verification = verify_replay_set_calibration_report(report_path)

    assert verification["gate_passed"] is False
    assert verification["checks"]["manifest_sha256_matches"] is True
    assert verification["checks"]["canonical_sha256_matches"] is True
    assert verification["checks"]["recomputed_report_matches"] is False
    assert any("deterministic re-computation" in reason for reason in verification["reasons"])


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            '{"event":"replay_set_threshold_calibration_completed",'
            '"event":"replay_set_threshold_calibration_completed"}',
            "duplicate JSON key",
        ),
        (
            '{"event":"replay_set_threshold_calibration_completed",'
            '"schema_version":NaN}',
            "non-finite JSON number NaN",
        ),
    ),
)
def test_replay_set_calibration_verifier_rejects_ambiguous_json(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    report_path = tmp_path / "ambiguous-calibration.json"
    report_path.write_bytes(("\ufeff" + payload).encode("utf-8"))

    verification = verify_replay_set_calibration_report(report_path)

    assert verification["gate_passed"] is False
    assert any(message in reason for reason in verification["reasons"])


def test_main_cli_exposes_replay_set_calibration_verifier(tmp_path: Path, capsys) -> None:
    manifest = _manifest(tmp_path / "source")
    report = calibrate_replay_set_thresholds(
        manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=True,
        reviewer="qa-team",
    )
    report_path = tmp_path / "source" / "threshold-calibration.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    verification_path = tmp_path / "verification.json"

    parsed = build_parser().parse_args(
        ["verify-replay-set-calibration", str(report_path), "--out", str(verification_path)]
    )
    assert parsed.command == "verify-replay-set-calibration"
    assert cli_main(
        ["verify-replay-set-calibration", str(report_path), "--out", str(verification_path)]
    ) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["gate_passed"] is True
    assert json.loads(verification_path.read_text(encoding="utf-8"))["gate_passed"] is True


def test_release_gate_binds_independent_calibration_verification(tmp_path: Path) -> None:
    replay_manifest = _manifest(tmp_path / "replay")
    report = calibrate_replay_set_thresholds(
        replay_manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=True,
        reviewer="qa-team",
    )
    report_path = replay_manifest.parent / "threshold-calibration.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    verification_path = replay_manifest.parent / "threshold-calibration-verification.json"
    verification_path.write_text(
        json.dumps(verify_replay_set_calibration_report(report_path), indent=2) + "\n",
        encoding="utf-8",
    )

    evidence: dict[str, Path] = {}
    statuses: dict[str, dict[str, str]] = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = tmp_path / "evidence" / f"{mode}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "runtime_mode": mode,
                    "gate_passed": True,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required"
            if mode in {"replay", "sim"}
            else "passed",
        }

    parity = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}
    output = tmp_path / "release" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=parity,
        statuses=statuses,
        output_path=output,
        replay_set_manifest=replay_manifest,
        replay_set_calibration_verification=verification_path,
    )
    write_release_gate_manifest(output, document)

    binding = document["replay_set"]["calibration_verification"]
    assert binding["gate_passed"] is True
    assert binding["report_promotion_ready"] is True
    assert binding["report_provenance_complete"] is False
    assert binding["recomputed_provenance_complete"] is False
    report = evaluate_release_gate(output)
    assert report["replay_set"]["calibration_verification"]["passed"] is True
    assert report["replay_set"]["calibration_verification"]["promotion_ready"] is True
    assert report["replay_set"]["calibration_verification"]["provenance_complete"] is False
    assert report["gate_passed"] is True

    historical_verification = json.loads(verification_path.read_text(encoding="utf-8"))
    historical_verification.pop("report_provenance_complete")
    historical_verification.pop("recomputed_provenance_complete")
    verification_path.write_text(
        json.dumps(historical_verification, indent=2) + "\n",
        encoding="utf-8",
    )
    historical_manifest = json.loads(output.read_text(encoding="utf-8"))
    historical_binding = historical_manifest["replay_set"]["calibration_verification"]
    historical_binding.pop("report_provenance_complete")
    historical_binding.pop("recomputed_provenance_complete")
    historical_binding["sha256"] = hashlib.sha256(verification_path.read_bytes()).hexdigest()
    output.write_text(json.dumps(historical_manifest, indent=2) + "\n", encoding="utf-8")

    historical_report = evaluate_release_gate(output)
    assert historical_report["replay_set"]["calibration_verification"]["passed"] is True
    assert (
        historical_report["replay_set"]["calibration_verification"]["provenance_complete"]
        is False
    )
    assert historical_report["replay_set"]["calibration_verification"]["promotion_ready"] is True

    verification_path.write_text(
        verification_path.read_text(encoding="utf-8").replace(
            '"gate_passed": true', '"gate_passed": false'
        ),
        encoding="utf-8",
    )
    tampered = evaluate_release_gate(output)
    assert tampered["gate_passed"] is False
    assert any(
        "calibration verification SHA-256 does not match" in reason
        for reason in tampered["release_blocked_reasons"]
    )


def test_release_gate_rejects_calibration_verification_bound_to_other_manifest(
    tmp_path: Path,
) -> None:
    """A valid verifier artifact cannot be transplanted to another replay set."""

    replay_manifest = _manifest(tmp_path / "replay")
    other_manifest = _manifest(tmp_path / "other")
    other_report = calibrate_replay_set_thresholds(
        other_manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=True,
        reviewer="qa-team",
    )
    other_report_path = other_manifest.parent / "threshold-calibration.json"
    other_report_path.write_text(
        json.dumps(other_report, indent=2) + "\n",
        encoding="utf-8",
    )
    verification_path = other_manifest.parent / "threshold-calibration-verification.json"
    verification_path.write_text(
        json.dumps(verify_replay_set_calibration_report(other_report_path), indent=2) + "\n",
        encoding="utf-8",
    )

    evidence: dict[str, Path] = {}
    statuses: dict[str, dict[str, str]] = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = tmp_path / "evidence" / f"{mode}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "runtime_mode": mode,
                    "gate_passed": True,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required"
            if mode in {"replay", "sim"}
            else "passed",
        }

    parity = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}
    with pytest.raises(
        ReleaseGateManifestError,
        match="bound to a different replay-set manifest path",
    ):
        build_release_gate_manifest(
            evidence,
            parity=parity,
            statuses=statuses,
            output_path=tmp_path / "release" / "release-gate.json",
            replay_set_manifest=replay_manifest,
            replay_set_calibration_verification=verification_path,
        )


def test_final_release_requires_promotion_ready_calibration(tmp_path: Path) -> None:
    """A deterministic but unapproved calibration remains prerelease-only."""

    replay_manifest = _manifest(tmp_path / "replay")
    calibration = calibrate_replay_set_thresholds(
        replay_manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=False,
    )
    assert calibration["promotion_ready"] is False
    report_path = replay_manifest.parent / "pending-threshold-calibration.json"
    report_path.write_text(json.dumps(calibration, indent=2) + "\n", encoding="utf-8")
    verification_path = replay_manifest.parent / "pending-calibration-verification.json"
    verification_path.write_text(
        json.dumps(verify_replay_set_calibration_report(report_path), indent=2) + "\n",
        encoding="utf-8",
    )

    evidence: dict[str, Path] = {}
    statuses: dict[str, dict[str, str]] = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = tmp_path / "evidence" / f"{mode}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "runtime_mode": mode,
                    "gate_passed": True,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required"
            if mode in {"replay", "sim"}
            else "passed",
        }

    parity = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}
    output = tmp_path / "release" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=parity,
        statuses=statuses,
        output_path=output,
        replay_set_manifest=replay_manifest,
        replay_set_calibration_verification=verification_path,
    )
    write_release_gate_manifest(output, document)
    prerelease = evaluate_release_gate(output)
    assert prerelease["gate_passed"] is True
    assert prerelease["replay_set"]["calibration_verification"]["promotion_ready"] is False

    final_manifest = json.loads(output.read_text(encoding="utf-8"))
    final_manifest["release_label"] = "0.3.0"
    output.write_text(json.dumps(final_manifest, indent=2) + "\n", encoding="utf-8")
    version_path = tmp_path / "product-version-release.json"
    version = json.loads(
        (
            Path(__file__).resolve().parents[1] / "configs" / "product-version.json"
        ).read_text(encoding="utf-8")
    )
    version.update({"release_label": "0.3.0", "python": "0.3.0", "channel": "release"})
    version_path.write_text(json.dumps(version, indent=2) + "\n", encoding="utf-8")

    final = evaluate_release_gate(output, version_manifest_path=version_path)

    assert final["release_allowed"] is False
    assert any(
        "final release requires promotion-ready replay_set calibration verification" in reason
        for reason in final["release_blocked_reasons"]
    )


def test_replay_set_calibration_keeps_review_and_prediction_gates_separate(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, missing_prediction="night")

    report = calibrate_replay_set_thresholds(
        manifest,
        thresholds=(0.3, 0.6, 0.9),
        reviewed=True,
        reviewer="qa-team",
    )

    assert report["deployment_domain_evidence_complete"] is True
    assert report["predictions_complete"] is False
    assert report["promotion_ready"] is False
    assert any("night predictions are missing" in reason for reason in report["failure_reasons"])

    unreviewed = calibrate_replay_set_thresholds(
        _manifest(tmp_path / "reviewed"),
        thresholds=(0.3, 0.6, 0.9),
    )
    assert unreviewed["promotion_ready"] is False
    assert "independent threshold review is not acknowledged" in unreviewed[
        "failure_reasons"
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"thresholds": ()}, "calibration thresholds"),
        ({"thresholds": (0.5,), "reviewed": True}, "reviewer"),
        ({"thresholds": (0.5,), "beta": 0.0}, "beta"),
        ({"thresholds": (0.5,), "minimum_recall": 1.1}, "minimum_recall"),
        ({"thresholds": (0.5,), "stability_tolerance": -0.1}, "stability_tolerance"),
    ],
)
def test_replay_set_calibration_rejects_invalid_requests(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(ReplaySetCalibrationError, match=message):
        calibrate_replay_set_thresholds(manifest, **kwargs)

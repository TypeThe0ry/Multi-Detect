from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.autonomy import AutonomyContractError
from multidetect.backends import RawSensorReplayRecording
from multidetect.cli import main as cli_main
from multidetect.replay_set import (
    REPLAY_SET_STRATA,
    build_replay_set_manifest,
    load_replay_set_manifest,
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


def _write_stratum(root: Path, stratum: str, *, with_predictions: bool = False) -> None:
    directory = root / stratum
    directory.mkdir(parents=True)
    image_payload = f"{stratum}-image".encode()
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
    assert (
        manifest.canonical_hash
        == load_replay_set_manifest(tmp_path / "replay-set.manifest.json").canonical_hash
    )


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

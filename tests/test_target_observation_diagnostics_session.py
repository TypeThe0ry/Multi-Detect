from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.release_parity import DEFAULT_FILE_LIST
from multidetect.target_observation_diagnostics_session import (
    SCHEMA,
    SCHEMA_PATH,
    TargetObservationDiagnosticsSessionError,
    main,
    verify_target_observation_diagnostics_session,
)
from multidetect.target_observation_recording_manifest import (
    MANIFEST_NAME,
    TargetObservationRecordingManifestError,
    verify_target_observation_recording_manifest,
)
from multidetect.target_observation_recording_manifest import (
    SCHEMA_PATH as RECORDING_SCHEMA_PATH,
)
from multidetect.target_observation_recording_manifest import (
    main as recording_manifest_main,
)


def _snapshot(
    *, local_now_ms: int, epoch: int, sequence: int, locatable: bool = True
) -> dict[str, object]:
    sent_at_ms = local_now_ms - 100
    envelope = {
        "atomicObservation": True,
        "producerEpoch": epoch,
        "sequence": sequence,
        "sentAtMs": sent_at_ms,
        "sourceAgeMs": 100,
        "advisoryOnly": True,
        "flightControlEnabled": False,
        "physicalReleaseEnabled": False,
    }
    geolocation: dict[str, object] = {
        **envelope,
        "targetId": "target-3",
        "sourceFrameId": f"frame-{sequence}",
        "available": locatable,
        "reason": "gps_qualified" if locatable else "horizontal_accuracy_exceeded",
    }
    if locatable:
        geolocation.update(
            {"latitudeDeg": 30.2741, "longitudeDeg": 120.1551, "horizontalSigmaM": 4.2}
        )
    range_status = {
        **envelope,
        "targetId": "target-3",
        "sourceFrameId": f"frame-{sequence}",
        "validity": "VALID",
        "slantRangeM": 80.0,
    }
    gnss_status = {
        **envelope,
        "qualified": locatable,
        "reason": "gps_qualified" if locatable else "horizontal_accuracy_exceeded",
        "fixType": 6,
        "satellitesVisible": 18,
    }
    receive = {"received_at_ms": local_now_ms - 50, "age_ms": 50}
    generated = datetime.fromtimestamp(local_now_ms / 1000, tz=timezone.utc)
    return {
        "schema": "multidetect.target-observation-diagnostics.v1",
        "schema_version": 1,
        "generated_at_utc": generated.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "local_now_ms": local_now_ms,
        "maximum_age_ms": 1500,
        "readiness": {
            "locatable": locatable,
            "reasons": [] if locatable else ["gnss_not_qualified"],
            "atomic_identity_matched": True,
            "status_fresh": True,
            "coordinates_included": locatable,
        },
        "producer_epoch_summary": {"active_epoch": epoch},
        "range_status": range_status,
        "target_geolocation_status": geolocation,
        "gnss_status": gnss_status,
        "local_receive": {
            "range": dict(receive),
            "target_geolocation": dict(receive),
            "gnss": dict(receive),
        },
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _write(directory: Path, index: int, document: object) -> Path:
    path = directory / f"target-observation-diagnostics-{index:03d}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _write_recording_manifest(directory: Path, paths: list[Path]) -> Path:
    entries = [
        {
            "relative_path": path.name,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    document = {
        "schema": "multidetect.target-observation-recording-manifest.v1",
        "schema_version": 1,
        "session_id": "09459b71-367b-4419-9ae8-e8cfba311423",
        "started_at_utc": "1970-01-01T00:00:00.000Z",
        "ended_at_utc": "1970-01-01T00:00:04.000Z",
        "interval_ms": 1000,
        "maximum_samples": 3600,
        "sample_count": len(paths),
        "stop_reason": "operator_stopped",
        "snapshots": entries,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }
    manifest = directory / MANIFEST_NAME
    manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return manifest


def test_schema_parity_and_installed_command_are_bound() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    Draft202012Validator.check_schema(json.loads(RECORDING_SCHEMA_PATH.read_text(encoding="utf-8")))
    ui_contract = set(DEFAULT_FILE_LIST["ui_contract"])
    assert "configs/schemas/target-observation-diagnostics-session.schema.json" in ui_contract
    assert "src/multidetect/target_observation_diagnostics_session.py" in ui_contract
    assert "scripts/verify_target_observation_diagnostics_session.py" in ui_contract
    assert "configs/schemas/target-observation-recording-manifest.schema.json" in ui_contract
    assert "src/multidetect/target_observation_recording_manifest.py" in ui_contract
    assert "scripts/verify_target_observation_recording_manifest.py" in ui_contract
    assert (
        "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetObservationRecorder.cc"
        in ui_contract
    )
    assert (
        "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetObservationRecorder.h"
        in ui_contract
    )
    recorder_source = (
        SCHEMA_PATH.parents[2]
        / "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetObservationRecorder.cc"
    ).read_text(encoding="utf-8")
    assert "multidetect.target-observation-recording-live-quality.v1" in recorder_source
    assert 'QStringLiteral("cadence_violation_count")' in recorder_source
    assert 'QStringLiteral("longest_non_locatable_run")' in recorder_source
    assert 'QStringLiteral("epoch_transition_count")' in recorder_source
    assert 'QStringLiteral("flight_control_enabled"), false' in recorder_source
    assert 'QStringLiteral("physical_release_enabled"), false' in recorder_source
    pyproject = (SCHEMA_PATH.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert "multidetect-target-observation-session-verify" in pyproject
    assert "multidetect-target-observation-recording-manifest-verify" in pyproject


def test_recording_manifest_closes_directory_and_binds_session(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1)),
        _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=7, sequence=2)),
    ]
    manifest = _write_recording_manifest(tmp_path, paths)

    verification = verify_target_observation_recording_manifest(manifest)
    assert verification["gate_passed"] is True
    assert verification["directory_closed"] is True
    assert verification["sample_count"] == 2
    assert [item["path"] for item in verification["snapshots"]] == [
        str(path.resolve()) for path in paths
    ]

    session = verify_target_observation_diagnostics_session(
        [tmp_path], require_recording_manifest=True
    )
    assert session["gate_passed"] is True
    assert session["recording_manifest"]["sha256"] == verification["manifest_sha256"]
    assert session["recording_manifest"]["directory_closed"] is True
    assert session["requirements"]["recording_manifest_required"] is True


def test_zero_sample_recording_manifest_is_valid_but_not_a_session(tmp_path: Path) -> None:
    manifest = _write_recording_manifest(tmp_path, [])
    verification = verify_target_observation_recording_manifest(manifest)
    assert verification["gate_passed"] is True
    assert verification["sample_count"] == 0
    assert verification["snapshots"] == []

    with pytest.raises(TargetObservationDiagnosticsSessionError, match="at least one input"):
        verify_target_observation_diagnostics_session([tmp_path], require_recording_manifest=True)


@pytest.mark.parametrize("mutation", ["bytes", "sha256", "sample_count"])
def test_recording_manifest_rejects_tampered_bindings(tmp_path: Path, mutation: str) -> None:
    path = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    manifest = _write_recording_manifest(tmp_path, [path])
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if mutation == "sample_count":
        document["sample_count"] = 2
    elif mutation == "bytes":
        document["snapshots"][0]["bytes"] += 1
    else:
        document["snapshots"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(TargetObservationRecordingManifestError):
        verify_target_observation_recording_manifest(manifest)


def test_recording_manifest_rejects_unreferenced_snapshot_and_wrong_order(
    tmp_path: Path,
) -> None:
    first = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    second = _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=7, sequence=2))
    manifest = _write_recording_manifest(tmp_path, [first])
    with pytest.raises(TargetObservationRecordingManifestError, match="not closed"):
        verify_target_observation_recording_manifest(manifest)

    manifest = _write_recording_manifest(tmp_path, [second, first])
    with pytest.raises(TargetObservationRecordingManifestError, match="strictly increasing"):
        verify_target_observation_recording_manifest(manifest)


def test_recording_manifest_rejects_duplicate_key_and_unknown_field(tmp_path: Path) -> None:
    path = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    manifest = _write_recording_manifest(tmp_path, [path])
    text = manifest.read_text(encoding="utf-8").replace(
        '"schema_version": 1,', '"schema_version": 1, "schema_version": 1,'
    )
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(TargetObservationRecordingManifestError, match="duplicate JSON key"):
        verify_target_observation_recording_manifest(manifest)

    manifest = _write_recording_manifest(tmp_path, [path])
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["unexpected"] = True
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(TargetObservationRecordingManifestError, match="schema validation"):
        verify_target_observation_recording_manifest(manifest)


def test_recording_manifest_requirement_and_cli_fail_closed(tmp_path: Path) -> None:
    path = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    with pytest.raises(TargetObservationDiagnosticsSessionError, match="manifest is required"):
        verify_target_observation_diagnostics_session([tmp_path], require_recording_manifest=True)

    manifest = _write_recording_manifest(tmp_path, [path])
    output = tmp_path / "manifest-verification.json"
    assert recording_manifest_main([str(manifest), "--out", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["gate_passed"] is True

    manifest.write_text("{}", encoding="utf-8")
    assert recording_manifest_main([str(manifest), "--out", str(output)]) == 1
    failure = json.loads(output.read_text(encoding="utf-8"))
    assert failure["gate_passed"] is False
    assert failure["flight_control_enabled"] is False


def test_session_sorts_inputs_and_reports_quality_and_epoch_transition(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path, 3, _snapshot(local_now_ms=3000, epoch=8, sequence=1)),
        _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1)),
        _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=7, sequence=2, locatable=False)),
    ]
    report = verify_target_observation_diagnostics_session(
        paths, minimum_samples=3, minimum_locatable_rate=0.5
    )

    assert report["schema"] == SCHEMA
    assert report["gate_passed"] is True
    assert report["sample_count"] == 3
    assert report["locatable_count"] == 2
    assert report["non_locatable_count"] == 1
    assert report["locatable_rate"] == pytest.approx(2 / 3)
    assert report["longest_non_locatable_run"] == 1
    assert report["observed_epochs"] == [7, 8]
    assert report["epoch_transition_count"] == 1
    assert report["epoch_transitions"] == [
        {
            "sample_index": 2,
            "local_now_ms": 3000,
            "generated_at_utc": "1970-01-01T00:00:03.000Z",
            "from_epoch": 7,
            "to_epoch": 8,
        }
    ]
    assert report["reason_counts"] == {"gnss_not_qualified": 1}
    assert [item["local_now_ms"] for item in report["samples"]] == [1000, 2000, 3000]


def test_session_reports_cadence_gaps_and_contiguous_quality_segments(
    tmp_path: Path,
) -> None:
    documents = [
        _snapshot(local_now_ms=1000, epoch=7, sequence=1),
        _snapshot(local_now_ms=2000, epoch=7, sequence=2, locatable=False),
        _snapshot(local_now_ms=5000, epoch=7, sequence=3, locatable=False),
        _snapshot(local_now_ms=6000, epoch=8, sequence=1),
    ]
    documents[2]["range_status"]["validity"] = "STALE"  # type: ignore[index]
    paths = [_write(tmp_path, index, document) for index, document in enumerate(documents)]
    manifest = _write_recording_manifest(tmp_path, paths)
    manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_document["ended_at_utc"] = "1970-01-01T00:00:07.000Z"
    manifest.write_text(json.dumps(manifest_document, indent=2) + "\n", encoding="utf-8")

    report = verify_target_observation_diagnostics_session(
        [tmp_path],
        minimum_samples=4,
        require_recording_manifest=True,
        maximum_non_locatable_run=1,
        maximum_sampling_gap_ms=2000,
    )

    assert report["observation_duration_ms"] == 5000
    assert report["sampling"] == {
        "interval_count": 3,
        "minimum_gap_ms": 1000,
        "maximum_gap_ms": 3000,
        "mean_gap_ms": pytest.approx(5000 / 3),
        "expected_interval_ms": 1000,
        "cadence_tolerance_ms": 500,
        "cadence_violation_count": 1,
        "gap_events": [
            {
                "previous_index": 1,
                "current_index": 2,
                "gap_ms": 3000,
                "expected_interval_ms": 1000,
            }
        ],
    }
    non_locatable = report["quality_segments"]["non_locatable"]
    assert len(non_locatable) == 1
    assert non_locatable[0]["start_index"] == 1
    assert non_locatable[0]["end_index"] == 2
    assert non_locatable[0]["sample_count"] == 2
    assert non_locatable[0]["duration_ms"] == 3000
    assert non_locatable[0]["diagnostic_reasons"] == ["gnss_not_qualified"]
    assert report["quality_segments"]["range_invalid"][0]["start_index"] == 2
    assert report["quality_segments"]["gnss_unqualified"][0]["sample_count"] == 2
    assert report["epoch_transitions"][0]["from_epoch"] == 7
    assert report["epoch_transitions"][0]["to_epoch"] == 8
    assert report["gate_passed"] is False
    assert len(report["reasons"]) == 2


def test_directory_input_and_atomic_cli_report(tmp_path: Path) -> None:
    _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=7, sequence=2))
    output = tmp_path / "session-report.json"

    assert main([str(tmp_path), "--out", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gate_passed"] is True
    assert report["sample_count"] == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_minimum_sample_and_locatable_rate_gates_fail_closed(tmp_path: Path) -> None:
    first = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    second = _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=7, sequence=2, locatable=False))

    report = verify_target_observation_diagnostics_session(
        [first, second], minimum_samples=3, minimum_locatable_rate=0.75
    )
    assert report["gate_passed"] is False
    assert len(report["reasons"]) == 2
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


def test_cli_applies_sampling_and_non_locatable_run_limits(tmp_path: Path) -> None:
    _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    _write(tmp_path, 2, _snapshot(local_now_ms=3000, epoch=7, sequence=2, locatable=False))
    output = tmp_path / "limited-session-report.json"

    assert (
        main(
            [
                str(tmp_path),
                "--maximum-non-locatable-run",
                "0",
                "--maximum-sampling-gap-ms",
                "1500",
                "--out",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gate_passed"] is False
    assert len(report["reasons"]) == 2


def test_sequence_and_sent_time_rollbacks_are_rejected(tmp_path: Path) -> None:
    first = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=2))
    sequence_rollback = _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=7, sequence=1))
    with pytest.raises(TargetObservationDiagnosticsSessionError, match="sequence rollback"):
        verify_target_observation_diagnostics_session([first, sequence_rollback])

    later = _snapshot(local_now_ms=3000, epoch=7, sequence=3)
    for status_name in ("range_status", "target_geolocation_status", "gnss_status"):
        later[status_name]["sentAtMs"] = 800  # type: ignore[index]
    sent_rollback = _write(tmp_path, 3, later)
    with pytest.raises(TargetObservationDiagnosticsSessionError, match="sentAtMs rollback"):
        verify_target_observation_diagnostics_session([first, sent_rollback])


def test_retired_epoch_reappearance_is_rejected(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1)),
        _write(tmp_path, 2, _snapshot(local_now_ms=2000, epoch=8, sequence=1)),
        _write(tmp_path, 3, _snapshot(local_now_ms=3000, epoch=7, sequence=2)),
    ]
    with pytest.raises(TargetObservationDiagnosticsSessionError, match="retired producer epoch"):
        verify_target_observation_diagnostics_session(paths)


def test_duplicate_inputs_and_equal_local_times_are_rejected(tmp_path: Path) -> None:
    first = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    with pytest.raises(TargetObservationDiagnosticsSessionError, match="duplicate session input"):
        verify_target_observation_diagnostics_session([first, first])

    second = _write(tmp_path, 2, _snapshot(local_now_ms=1000, epoch=7, sequence=2))
    with pytest.raises(TargetObservationDiagnosticsSessionError, match="strictly increasing"):
        verify_target_observation_diagnostics_session([first, second])


@pytest.mark.parametrize(
    (
        "minimum_samples",
        "minimum_locatable_rate",
        "maximum_non_locatable_run",
        "maximum_sampling_gap_ms",
    ),
    [
        (True, 0.0, None, None),
        (1.5, 0.0, None, None),
        (1, True, None, None),
        (1, float("nan"), None, None),
        (1, 1.01, None, None),
        (1, 0.0, True, None),
        (1, 0.0, -1, None),
        (1, 0.0, None, True),
        (1, 0.0, None, 0),
    ],
)
def test_gate_thresholds_require_strict_finite_types(
    tmp_path: Path,
    minimum_samples: object,
    minimum_locatable_rate: object,
    maximum_non_locatable_run: object,
    maximum_sampling_gap_ms: object,
) -> None:
    source = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    with pytest.raises(TargetObservationDiagnosticsSessionError):
        verify_target_observation_diagnostics_session(
            [source],
            minimum_samples=minimum_samples,  # type: ignore[arg-type]
            minimum_locatable_rate=minimum_locatable_rate,  # type: ignore[arg-type]
            maximum_non_locatable_run=maximum_non_locatable_run,  # type: ignore[arg-type]
            maximum_sampling_gap_ms=maximum_sampling_gap_ms,  # type: ignore[arg-type]
        )


def test_directory_input_rejects_snapshot_symlink_when_supported(tmp_path: Path) -> None:
    source_directory = tmp_path / "source"
    session_directory = tmp_path / "session"
    source_directory.mkdir()
    session_directory.mkdir()
    source = _write(source_directory, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    alias = session_directory / "target-observation-diagnostics-001.json"
    try:
        alias.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(TargetObservationDiagnosticsSessionError, match="symbolic link"):
        verify_target_observation_diagnostics_session([session_directory])


def test_invalid_snapshot_cli_writes_fail_closed_report(tmp_path: Path) -> None:
    source = _write(tmp_path, 1, {"schema": "invalid"})
    output = tmp_path / "failure.json"

    assert main([str(source), "--out", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gate_passed"] is False
    assert report["advisory_only"] is True
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False

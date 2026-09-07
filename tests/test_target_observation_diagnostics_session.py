from __future__ import annotations

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


def test_schema_parity_and_installed_command_are_bound() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    ui_contract = set(DEFAULT_FILE_LIST["ui_contract"])
    assert "configs/schemas/target-observation-diagnostics-session.schema.json" in ui_contract
    assert "src/multidetect/target_observation_diagnostics_session.py" in ui_contract
    assert "scripts/verify_target_observation_diagnostics_session.py" in ui_contract
    assert "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetObservationRecorder.cc" in ui_contract
    assert "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetObservationRecorder.h" in ui_contract
    pyproject = (SCHEMA_PATH.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert "multidetect-target-observation-session-verify" in pyproject


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
    assert report["reason_counts"] == {"gnss_not_qualified": 1}
    assert [item["local_now_ms"] for item in report["samples"]] == [1000, 2000, 3000]


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
    ("minimum_samples", "minimum_locatable_rate"),
    [(True, 0.0), (1.5, 0.0), (1, True), (1, float("nan")), (1, 1.01)],
)
def test_gate_thresholds_require_strict_finite_types(
    tmp_path: Path, minimum_samples: object, minimum_locatable_rate: object
) -> None:
    source = _write(tmp_path, 1, _snapshot(local_now_ms=1000, epoch=7, sequence=1))
    with pytest.raises(TargetObservationDiagnosticsSessionError):
        verify_target_observation_diagnostics_session(
            [source],
            minimum_samples=minimum_samples,  # type: ignore[arg-type]
            minimum_locatable_rate=minimum_locatable_rate,  # type: ignore[arg-type]
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

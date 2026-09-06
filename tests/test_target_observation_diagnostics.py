from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect.release_parity import DEFAULT_FILE_LIST
from multidetect.target_observation_diagnostics import (
    SCHEMA,
    SCHEMA_PATH,
    TargetObservationDiagnosticsError,
    main,
    validate_target_observation_diagnostics,
    verify_target_observation_diagnostics,
)


def _document(*, locatable: bool = True) -> dict[str, object]:
    local_now_ms = int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp() * 1000)
    envelope: dict[str, object] = {
        "atomicObservation": True,
        "producerEpoch": 7,
        "sequence": 42,
        "sentAtMs": local_now_ms - 120,
        "sourceAgeMs": 120,
        "advisoryOnly": True,
        "flightControlEnabled": False,
        "physicalReleaseEnabled": False,
    }
    range_status = {
        **envelope,
        "targetId": "target-3",
        "sourceFrameId": "frame-88",
        "validity": "VALID",
        "slantRangeM": 87.5,
        "groundRangeM": 86.9,
        "absoluteBearingDeg": 31.2,
    }
    geolocation_status: dict[str, object] = {
        **envelope,
        "targetId": "target-3",
        "sourceFrameId": "frame-88",
        "available": locatable,
    }
    if locatable:
        geolocation_status.update(
            {
                "latitudeDeg": 30.2741,
                "longitudeDeg": 120.1551,
                "horizontalSigmaM": 4.2,
            }
        )
    else:
        geolocation_status["reason"] = "target_geolocation_unavailable"
    gnss_status = {
        **envelope,
        "qualified": True,
        "reason": "gps_qualified",
        "fixType": 6,
        "satellitesVisible": 18,
        "horizontalAccuracyM": 0.8,
        "verticalAccuracyM": 1.2,
    }
    receive_entry = {"received_at_ms": local_now_ms - 100, "age_ms": 100}
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "generated_at_utc": "2026-09-07T00:00:00.000Z",
        "local_now_ms": local_now_ms,
        "maximum_age_ms": 1500,
        "readiness": {
            "locatable": locatable,
            "reasons": [] if locatable else ["target_geolocation_unavailable"],
            "atomic_identity_matched": True,
            "status_fresh": True,
            "coordinates_included": locatable,
        },
        "producer_epoch_summary": {
            "active_epoch": 7,
            "health_level": "HEALTHY",
            "health_reason": "producer_epoch_healthy",
            "producer_restart_count": 0,
            "retired_epoch_reject_count": 0,
            "epoch_transition_reject_count": 0,
            "epoch_changed_at_ms": local_now_ms - 5000,
            "epoch_last_incident_at_ms": 0,
        },
        "range_status": range_status,
        "target_geolocation_status": geolocation_status,
        "gnss_status": gnss_status,
        "local_receive": {
            "range": dict(receive_entry),
            "target_geolocation": dict(receive_entry),
            "gnss": dict(receive_entry),
        },
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_schema_is_valid_and_accepts_both_readiness_states() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(_document())
    validator.validate(_document(locatable=False))


def test_schema_verifier_and_wrapper_are_bound_to_ui_parity() -> None:
    ui_contract = set(DEFAULT_FILE_LIST["ui_contract"])
    assert "configs/schemas/target-observation-diagnostics.schema.json" in ui_contract
    assert "src/multidetect/target_observation_diagnostics.py" in ui_contract
    assert "scripts/verify_target_observation_diagnostics.py" in ui_contract

    pyproject = (SCHEMA_PATH.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert "multidetect-target-observation-diagnostics-verify" in pyproject


def test_valid_locatable_snapshot_is_hash_bound(tmp_path: Path) -> None:
    path = _write(tmp_path / "diagnostics.json", _document())
    report = verify_target_observation_diagnostics(path, require_locatable=True)

    assert report["gate_passed"] is True
    assert report["diagnostics_locatable"] is True
    assert report["coordinates_included"] is True
    assert report["input_bytes"] == path.stat().st_size
    assert report["input_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_non_locatable_snapshot_is_valid_but_optional_gate_stays_closed(tmp_path: Path) -> None:
    path = _write(tmp_path / "diagnostics.json", _document(locatable=False))

    assert verify_target_observation_diagnostics(path)["gate_passed"] is True
    report = verify_target_observation_diagnostics(path, require_locatable=True)
    assert report["gate_passed"] is False
    assert report["diagnostics_locatable"] is False
    assert report["coordinates_included"] is False
    assert report["diagnostic_reasons"] == ["target_geolocation_unavailable"]


def test_startup_snapshot_without_statuses_is_valid_and_fail_closed(tmp_path: Path) -> None:
    document = _document(locatable=False)
    document["range_status"] = {}
    document["target_geolocation_status"] = {}
    document["gnss_status"] = {}
    document["readiness"] = {
        "locatable": False,
        "reasons": [
            "range_status_missing",
            "target_geolocation_status_missing",
            "gnss_status_missing",
        ],
        "atomic_identity_matched": False,
        "status_fresh": False,
        "coordinates_included": False,
    }
    document["local_receive"] = {
        "range": {"received_at_ms": 0, "age_ms": None},
        "target_geolocation": {"received_at_ms": 0, "age_ms": None},
        "gnss": {"received_at_ms": 0, "age_ms": None},
    }
    path = _write(tmp_path / "startup-diagnostics.json", document)

    report = verify_target_observation_diagnostics(path, require_locatable=True)
    assert report["gate_passed"] is False
    assert report["diagnostics_locatable"] is False
    assert report["coordinates_included"] is False


def test_non_locatable_snapshot_cannot_smuggle_coordinates() -> None:
    document = _document(locatable=False)
    document["target_geolocation_status"].update(  # type: ignore[union-attr]
        {"latitudeDeg": 30.0, "longitudeDeg": 120.0, "horizontalSigmaM": 1.0}
    )

    with pytest.raises(TargetObservationDiagnosticsError, match="must omit"):
        validate_target_observation_diagnostics(document)


def test_atomic_identity_declaration_must_match_all_three_envelopes() -> None:
    document = _document(locatable=False)
    document["gnss_status"]["sequence"] = 43  # type: ignore[index]

    with pytest.raises(TargetObservationDiagnosticsError, match="atomic_identity_matched"):
        validate_target_observation_diagnostics(document)


def test_status_fresh_declaration_must_match_receive_age() -> None:
    document = _document(locatable=False)
    local_now_ms = document["local_now_ms"]
    assert isinstance(local_now_ms, int)
    document["local_receive"]["range"] = {  # type: ignore[index]
        "received_at_ms": local_now_ms - 2000,
        "age_ms": 2000,
    }

    with pytest.raises(TargetObservationDiagnosticsError, match="status_fresh"):
        validate_target_observation_diagnostics(document)


def test_receive_age_is_recomputed_from_local_clock() -> None:
    document = _document(locatable=False)
    document["local_receive"]["gnss"]["age_ms"] = 99  # type: ignore[index]

    with pytest.raises(TargetObservationDiagnosticsError, match="does not match local_now_ms"):
        validate_target_observation_diagnostics(document)


def test_unknown_fields_and_control_enablement_are_rejected() -> None:
    unknown = _document()
    unknown["unexpected"] = True
    with pytest.raises(TargetObservationDiagnosticsError, match="Additional properties"):
        validate_target_observation_diagnostics(unknown)

    enabled = _document()
    enabled["flight_control_enabled"] = True
    with pytest.raises(TargetObservationDiagnosticsError, match="False was expected"):
        validate_target_observation_diagnostics(enabled)


def test_generated_timestamp_must_match_local_clock() -> None:
    document = _document()
    document["generated_at_utc"] = "2026-09-07T00:01:00.000Z"
    with pytest.raises(TargetObservationDiagnosticsError, match="differ by more than"):
        validate_target_observation_diagnostics(document)


def test_non_finite_numbers_are_rejected_even_when_json_overflows(tmp_path: Path) -> None:
    document = _document(locatable=False)
    serialized = json.dumps(document).replace('"slantRangeM": 87.5', '"slantRangeM": 1e9999')
    path = tmp_path / "overflow.json"
    path.write_text(serialized, encoding="utf-8")

    with pytest.raises(TargetObservationDiagnosticsError, match="finite number"):
        verify_target_observation_diagnostics(path)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema": "first", "schema": "second"}\n', encoding="utf-8")

    with pytest.raises(TargetObservationDiagnosticsError, match="duplicate JSON key"):
        verify_target_observation_diagnostics(path)


def test_cli_writes_atomic_verification_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "diagnostics.json", _document())
    output = tmp_path / "verification.json"

    assert main([str(source), "--require-locatable", "--out", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gate_passed"] is True
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True


def test_cli_emits_fail_closed_report_for_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write(tmp_path / "diagnostics.json", {"schema": SCHEMA})

    assert main([str(source)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["gate_passed"] is False
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


def test_input_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    source = _write(tmp_path / "diagnostics.json", _document())
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(TargetObservationDiagnosticsError, match="symbolic link"):
        verify_target_observation_diagnostics(alias)


def test_failed_validation_does_not_mutate_input() -> None:
    document = _document(locatable=False)
    before = copy.deepcopy(document)
    validate_target_observation_diagnostics(document)
    assert document == before

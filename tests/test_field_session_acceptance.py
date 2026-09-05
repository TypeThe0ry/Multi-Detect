from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from multidetect.field_session_acceptance import load_jsonl, validate_field_session
from multidetect.field_session_acceptance import main as field_session_main


def _record(state: str, t: float, *, event: str | None = None) -> dict[str, object]:
    return {
        "event": event or state,
        "state": state,
        "timestamp_s": t,
        "gps": {
            "fix_type": 3,
            "satellites_visible": 12,
            "h_acc_m": 1.2,
            "v_acc_m": 2.4,
            "latitude_deg": 22.3001,
            "longitude_deg": 114.1734,
            "position_healthy": True,
            "observed_at_s": t,
        },
        "range": {
            "validity": "VALID",
            "distance_m": 12.5,
            "source_age_s": 0.1,
            "data_freshness_s": 0.1,
        },
    }


def test_field_session_accepts_full_sequence_and_reacquisition() -> None:
    report = validate_field_session(
        [
            _record("DET", 0.0),
            _record("TRK", 0.1),
            _record("LCK", 0.2),
            _record("TGT", 0.3),
            {"event": "cancel_trk", "cancelled": True, "timestamp_s": 0.4},
            _record("DET", 0.5),
            _record("TRK", 0.6),
        ]
    )
    assert report["gate_passed"] is True
    assert report["state_sequence_valid"] is True
    assert report["reacquired_after_cancel"] is True
    assert report["state_path"] == ["DET", "TRK", "LCK", "TGT", "DET", "TRK"]
    assert report["target_binding"]["valid"] is True
    assert report["target_binding"]["same_record_pairs"] == [0, 1, 2, 3, 5, 6]
    assert report["target_binding"]["same_record_without_id"] == [0, 1, 2, 3, 5, 6]


def test_target_state_evidence_gate_requires_fresh_samples_in_tgt_context() -> None:
    records = [_record(state, index * 0.1) for index, state in enumerate(("DET", "TRK", "LCK"))]
    stale_target = _record("TGT", 0.3)
    stale_target["gps"]["observed_at_s"] = -2.0  # type: ignore[index]
    stale_target["range"]["source_age_s"] = 2.0  # type: ignore[index]
    records.append(stale_target)

    legacy = validate_field_session(records)
    strict = validate_field_session(records, require_target_state_evidence=True)

    # The legacy gate still proves that the stream contains qualified sensor
    # data somewhere.  The strict field gate rejects using those earlier
    # samples to justify a TGT location after the data went stale.
    assert legacy["gate_passed"] is True
    assert strict["gate_passed"] is False
    assert strict["target_state_evidence"] == {
        "required": True,
        "state": "TGT",
        "gps_qualified_records": [],
        "range_qualified_records": [],
        "valid": False,
        "reasons": ["target_state_gps_missing", "target_state_range_missing"],
    }
    assert strict["gps"]["results"][-1]["state_context"] == "TGT"
    assert strict["range"]["results"][-1]["state_context"] == "TGT"


def test_target_state_evidence_inherits_sensor_only_records() -> None:
    records = [_record(state, index * 0.1) for index, state in enumerate(("DET", "TRK", "LCK"))]
    records.append(_record("TGT", 0.3))
    sensor_only = _record("DET", 0.4)
    sensor_only.pop("event")
    sensor_only.pop("state")
    records.append(sensor_only)

    report = validate_field_session(records, require_target_state_evidence=True)
    assert report["gate_passed"] is True
    assert report["target_state_evidence"]["gps_qualified_records"] == [3, 4]
    assert report["target_state_evidence"]["range_qualified_records"] == [3, 4]
    assert report["gps"]["results"][-1]["state_context"] == "TGT"
    assert report["range"]["results"][-1]["state_context"] == "TGT"


def test_cancel_clears_target_state_evidence_context() -> None:
    records = [
        _record("DET", 0.0),
        _record("TRK", 0.1),
        _record("LCK", 0.2),
        {"event": "TGT", "state": "TGT", "timestamp_s": 0.3},
        {"event": "cancel_trk", "cancelled": True, "timestamp_s": 0.4},
        _record("TRK", 0.5),
    ]
    report = validate_field_session(records, require_target_state_evidence=True)
    assert report["gate_passed"] is False
    assert report["target_state_evidence"]["reasons"] == [
        "target_state_gps_missing",
        "target_state_range_missing",
    ]
    assert report["gps"]["results"][-1]["state_context"] == "TRK"
    assert report["range"]["results"][-1]["state_context"] == "TRK"


def test_field_session_rejects_stale_gps_and_range() -> None:
    record = _record("DET", 0.0)
    record["gps"]["observed_at_s"] = -2.0  # type: ignore[index]
    record["range"]["source_age_s"] = 2.0  # type: ignore[index]
    report = validate_field_session([record])
    assert report["gps"]["qualified"] == 0
    assert report["range"]["qualified"] == 0
    assert report["gate_passed"] is False


def test_field_session_rejects_decreasing_event_timestamps() -> None:
    records = [_record(state, timestamp) for state, timestamp in (
        ("DET", 1.0),
        ("TRK", 1.2),
        ("LCK", 1.1),
        ("TGT", 1.3),
    )]
    report = validate_field_session(records)
    assert report["timestamps"]["non_decreasing"] is False
    assert report["timestamps"]["errors"] == [
        {
            "index": 2,
            "previous_timestamp_s": 1.2,
            "timestamp_s": 1.1,
            "reason": "timestamp_decreased",
        }
    ]
    assert report["gate_passed"] is False


def test_field_session_rejects_missing_or_negative_event_timestamp() -> None:
    missing = _record("DET", 0.0)
    del missing["timestamp_s"]
    report = validate_field_session([missing])
    assert report["timestamps"]["errors"] == [{"index": 0, "reason": "timestamp_missing"}]
    assert report["gate_passed"] is False

    negative = _record("DET", -0.1)
    report = validate_field_session([negative])
    assert report["timestamps"]["errors"] == [{"index": 0, "reason": "timestamp_invalid"}]
    assert report["gate_passed"] is False


def test_field_session_accepts_same_target_ids_across_separate_sensor_records() -> None:
    records: list[dict[str, object]] = []
    for index, state in enumerate(("DET", "TRK", "LCK", "TGT")):
        record = _record(state, index * 0.1)
        record["target_id"] = "target-a"
        if index % 2 == 0:
            record["range"] = {}
        else:
            record["gps"] = {}
        records.append(record)
    report = validate_field_session(records)
    assert report["gate_passed"] is True
    assert report["target_binding"]["valid"] is True
    assert report["target_binding"]["matched_target_ids"] == ["target-a"]
    assert report["target_binding"]["same_record_pairs"] == []


def test_field_session_rejects_anonymous_sensor_samples_in_separate_records() -> None:
    records: list[dict[str, object]] = []
    for index, state in enumerate(("DET", "TRK", "LCK", "TGT")):
        record = _record(state, index * 0.1)
        if index % 2 == 0:
            record["range"] = {}
        else:
            record["gps"] = {}
        records.append(record)
    report = validate_field_session(records)
    assert report["target_binding"]["valid"] is False
    assert report["target_binding"]["same_record_pairs"] == []
    assert report["gate_passed"] is False


def test_field_session_rejects_disjoint_target_ids_across_separate_records() -> None:
    records: list[dict[str, object]] = []
    for index, state in enumerate(("DET", "TRK", "LCK", "TGT")):
        record = _record(state, index * 0.1)
        record["target_id"] = "target-a" if index % 2 == 0 else "target-b"
        if index % 2 == 0:
            record["range"] = {}
        else:
            record["gps"] = {}
        records.append(record)
    report = validate_field_session(records)
    assert report["target_binding"]["valid"] is False
    assert report["target_binding"]["gps_target_ids"] == ["target-a"]
    assert report["target_binding"]["range_target_ids"] == ["target-b"]
    assert report["target_binding"]["matched_target_ids"] == []
    assert report["gate_passed"] is False


def test_field_session_rejects_partial_target_id_overlap_across_separate_records() -> None:
    # The GPS stream briefly switches to target-b while RANGE continues to
    # report target-a.  No individual record contains both sensors, so a
    # same-record-only check would miss the conflict; the session gate must
    # reject the non-unique identity instead of accepting the shared ``a``.
    records: list[dict[str, object]] = [
        {"state": "DET", "timestamp_s": 0.0},
        {
            "timestamp_s": 0.01,
            "gps": {
                "target_id": "target-a",
                "position_healthy": True,
                "latitude_deg": 22.3001,
                "longitude_deg": 114.1734,
                "fix_type": 3,
                "satellites_visible": 12,
                "h_acc_m": 1.2,
                "v_acc_m": 2.4,
                "observed_at_s": 0.01,
            },
        },
        {"state": "TRK", "timestamp_s": 0.1},
        {
            "timestamp_s": 0.11,
            "gps": {
                "target_id": "target-b",
                "position_healthy": True,
                "latitude_deg": 22.3001,
                "longitude_deg": 114.1734,
                "fix_type": 3,
                "satellites_visible": 12,
                "h_acc_m": 1.2,
                "v_acc_m": 2.4,
                "observed_at_s": 0.11,
            },
        },
        {
            "timestamp_s": 0.12,
            "range": {
                "target_id": "target-a",
                "validity": "VALID",
                "distance_m": 12.5,
                "source_age_s": 0.1,
                "data_freshness_s": 0.1,
            },
        },
        {"state": "LCK", "timestamp_s": 0.2},
        {
            "timestamp_s": 0.21,
            "gps": {
                "target_id": "target-a",
                "position_healthy": True,
                "latitude_deg": 22.3001,
                "longitude_deg": 114.1734,
                "fix_type": 3,
                "satellites_visible": 12,
                "h_acc_m": 1.2,
                "v_acc_m": 2.4,
                "observed_at_s": 0.21,
            },
        },
        {
            "timestamp_s": 0.22,
            "range": {
                "target_id": "target-a",
                "validity": "VALID",
                "distance_m": 12.5,
                "source_age_s": 0.1,
                "data_freshness_s": 0.1,
            },
        },
        {"state": "TGT", "timestamp_s": 0.3},
    ]

    report = validate_field_session(records)

    assert report["target_binding"]["valid"] is False
    assert report["target_binding"]["matched_target_ids"] == ["target-a"]
    assert report["target_binding"]["cross_record_mismatches"] == [
        {"gps_only": ["target-b"], "range_only": []}
    ]
    assert report["target_binding"]["multiple_target_ids"] == ["target-a", "target-b"]
    assert report["gate_passed"] is False


def test_field_session_rejects_mismatched_target_ids_in_the_same_record() -> None:
    records: list[dict[str, object]] = []
    for index, state in enumerate(("DET", "TRK", "LCK", "TGT")):
        record = _record(state, index * 0.1)
        record["gps"]["target_id"] = "target-a"  # type: ignore[index]
        record["range"]["target_id"] = "target-b"  # type: ignore[index]
        records.append(record)
    report = validate_field_session(records)
    assert report["target_binding"]["valid"] is False
    assert report["target_binding"]["matched_target_ids"] == []
    assert report["target_binding"]["mismatched_same_record"] == [0, 1, 2, 3]
    assert report["gate_passed"] is False


def test_field_session_rejects_conflicting_root_and_nested_target_ids() -> None:
    records = [
        _record(state, index * 0.1)
        for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
    ]
    for record in records:
        record["target_id"] = "target-a"
        record["range"]["source_target_id"] = "target-b"  # type: ignore[index]
    report = validate_field_session(records)
    assert report["target_binding"]["valid"] is False
    assert report["target_binding"]["errors"]
    assert all(
        error["reason"] == "target_id_context_conflict"
        for error in report["target_binding"]["errors"]
    )
    assert report["gate_passed"] is False


def test_field_session_rejects_mismatched_ids_on_nested_sensor_parents() -> None:
    records: list[dict[str, object]] = []
    for index, state in enumerate(("DET", "TRK", "LCK", "TGT")):
        records.append(
            {
                "state": state,
                "timestamp_s": index * 0.1,
                "target_geolocation": {
                    "target_id": "target-a",
                    "gps_quality": {
                        "position_healthy": True,
                        "latitude_deg": 22.3001,
                        "longitude_deg": 114.1734,
                        "fix_type": 3,
                        "satellites_visible": 12,
                        "h_acc_m": 1.5,
                        "v_acc_m": 2.5,
                        "observed_at_s": index * 0.1,
                    },
                },
                "ranging": {
                    "target_id": "target-b",
                    "range_status": {
                        "validity": "VALID",
                        "slant_range_m": 20.0,
                        "source_age_s": 0.2,
                        "data_freshness_s": 0.2,
                    },
                },
            }
        )
    report = validate_field_session(records)
    assert report["target_binding"]["valid"] is False
    assert report["target_binding"]["matched_target_ids"] == []
    assert report["target_binding"]["mismatched_same_record"] == [0, 1, 2, 3]
    assert report["gate_passed"] is False


def test_jsonl_payload_is_serializable(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(_record("DET", 0.0)) + "\n", encoding="utf-8")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_audit_envelope_is_normalized_and_mavlink_accuracy_is_converted() -> None:
    def audit_record(state: str, timestamp_s: float) -> dict[str, object]:
        return {
            "sequence": int(timestamp_s * 10) + 1,
            "timestamp_s": timestamp_s,
            "event_type": "field.state_changed",
            "details": {
                "state": state.lower(),
                "gps": {
                    "fix_type": 3,
                    "satellites_visible": 10,
                    "eph_cm": 120,
                    "epv_cm": 240,
                    "latitude_deg": 22.3001,
                    "longitude_deg": 114.1734,
                    "position_healthy": True,
                    "observed_at_s": timestamp_s,
                },
                "range": {
                    "validity": "VALID",
                    "slant_range_m": 18.0,
                    "source_age_s": 0.1,
                    "data_freshness_s": 0.1,
                },
            },
        }

    report = validate_field_session(
        [
            audit_record("detected", 0.0),
            audit_record("tracking", 0.1),
            audit_record("locked", 0.2),
            audit_record("target_confirmed", 0.3),
            {
                "timestamp_s": 0.4,
                "event_type": "operator.selection",
                "details": {"event": "cancel_trk", "cancelled": True},
            },
            audit_record("detected", 0.5),
            audit_record("tracking", 0.6),
        ]
    )
    assert report["gate_passed"] is True
    assert report["state_path"] == ["DET", "TRK", "LCK", "TGT", "DET", "TRK"]
    assert report["gps"]["qualified"] == 6
    assert report["range"]["qualified"] == 6


def test_generic_tracking_audit_event_is_not_state_evidence() -> None:
    report = validate_field_session(
        [
            {
                "timestamp_s": 1.0,
                "event_type": "tracking.unified_target_pool_changed",
                "details": {"track_count": 1, "metadata_only": True},
            }
        ]
    )
    assert report["state_path"] == []
    assert report["state_sequence_valid"] is False


def test_nested_geolocation_and_range_details_are_recomputed() -> None:
    records: list[dict[str, object]] = []
    for index, state in enumerate(("DET", "TRK", "LCK", "TGT")):
        records.append(
            {
                "sequence": index + 1,
                "timestamp_s": index * 0.1,
                "event_type": "ranging.primary_target_solution",
                "details": {
                    "state": state.lower(),
                    "target_geolocation": {
                        "target_id": "target-a",
                        "gps_quality": {
                            "position_healthy": True,
                            "latitude_deg": 22.3001,
                            "longitude_deg": 114.1734,
                            "fix_type": 3,
                            "satellites_visible": 12,
                            "h_acc_m": 1.5,
                            "v_acc_m": 2.5,
                            "observed_at_s": index * 0.1,
                        }
                    },
                    "ranging": {
                        "target_id": "target-a",
                        "range_status": {
                            "validity": "DEGRADED",
                            "slant_range_m": 20.0,
                            "source_age_s": 0.2,
                            "data_freshness_s": 0.2,
                        }
                    },
                },
            }
        )
    report = validate_field_session(records)
    assert report["gate_passed"] is True
    assert report["gps"]["qualified"] == 4
    assert report["range"]["qualified"] == 4
    assert report["target_binding"]["matched_target_ids"] == ["target-a"]


def test_gps_pole_coordinate_is_rejected() -> None:
    record = _record("DET", 0.0)
    record["gps"]["latitude_deg"] = 90.0  # type: ignore[index]
    report = validate_field_session([record])
    assert report["gps"]["qualified"] == 0
    assert report["gps"]["results"][0]["reason"] == "coordinates_invalid"


def test_cancelled_tracking_can_reacquire_without_a_duplicate_state_transition() -> None:
    first = _record("TRK", 0.0)
    second = _record("TRK", 0.2)
    report = validate_field_session(
        [
            _record("DET", 0.0),
            _record("TRK", 0.1),
            _record("LCK", 0.2),
            _record("TGT", 0.3),
            {"event": "cancel_trk", "cancelled": True, "timestamp_s": 0.4},
            first,
            second,
        ]
    )
    assert report["reacquired_after_cancel"] is True


def test_raw_gps_sample_wins_over_top_level_derived_quality_summary() -> None:
    record = _record("DET", 0.0)
    # Some capture adapters expose the derived verdict at the root and retain
    # the raw sample below target_geolocation.  The validator must recompute
    # from the raw fields instead of accepting a stale summary.
    record["gps_quality"] = {"qualified": True, "age_s": 0.0}
    record["target_geolocation"] = {
        "gps_sample": {
            "position_healthy": True,
            "latitude_deg": 22.3001,
            "longitude_deg": 114.1734,
            "fix_type": 3,
            "satellites_visible": 12,
            "h_acc_m": 1.2,
            "v_acc_m": 2.4,
            "observed_at_s": 0.0,
        }
    }
    report = validate_field_session([record])
    assert report["gps"]["qualified"] == 1


def test_gps_fix_and_satellite_fields_require_integer_wire_values() -> None:
    record = _record("DET", 0.0)
    record["gps"]["fix_type"] = 3.5  # type: ignore[index]
    assert validate_field_session([record])["gps"]["results"][0]["reason"] == "fix_type<3"

    record = _record("DET", 0.0)
    record["gps"]["satellites_visible"] = 6.0  # type: ignore[index]
    assert validate_field_session([record])["gps"]["results"][0]["reason"] == "satellites_invalid"


def test_field_validator_caps_relaxed_gps_quality_thresholds() -> None:
    cases = (
        ("h_acc_m", 6.1, {"max_hacc_m": 20.0}, "h_acc_invalid"),
        ("v_acc_m", 10.1, {"max_vacc_m": 20.0}, "v_acc_invalid"),
    )
    for field, value, options, reason in cases:
        record = _record("DET", 0.0)
        record["gps"][field] = value  # type: ignore[index]
        report = validate_field_session([record], **options)
        assert report["gps"]["results"][0]["qualified"] is False
        assert report["gps"]["results"][0]["reason"] == reason


def test_field_validator_caps_relaxed_gps_and_range_age_thresholds() -> None:
    gps_record = _record("DET", 0.0)
    gps_record["gps"]["observed_at_s"] = -1.6  # type: ignore[index]
    gps_report = validate_field_session([gps_record], max_gps_age_s=10.0)
    assert gps_report["gps"]["results"][0]["qualified"] is False
    assert gps_report["gps"]["results"][0]["reason"] == "gps_age_invalid"

    range_record = _record("DET", 0.0)
    range_record["range"]["source_age_s"] = 1.6  # type: ignore[index]
    range_record["range"]["data_freshness_s"] = 1.6  # type: ignore[index]
    range_report = validate_field_session([range_record], max_range_age_s=10.0)
    assert range_report["range"]["results"][0]["qualified"] is False
    assert range_report["range"]["results"][0]["reason"] == "source_age_invalid"


def test_string_cancel_flag_is_recognized() -> None:
    report = validate_field_session(
        [
            _record("DET", 0.0),
            _record("TRK", 0.1),
            _record("LCK", 0.2),
            _record("TGT", 0.3),
            {"event": "selection", "cancelled": "true", "timestamp_s": 0.4},
            _record("TRK", 0.5),
        ]
    )
    assert report["cancel_seen"] is True
    assert report["reacquired_after_cancel"] is True


def test_validator_cli_can_bind_a_link_preflight_and_stays_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session = tmp_path / "session.jsonl"
    session.write_text(
        "\n".join(
            json.dumps(_record(state, index * 0.1))
            for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
        )
        + "\n",
        encoding="utf-8",
    )
    link = tmp_path / "field-link.json"
    link.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "validation.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "multidetect-field-session",
            str(session),
            "--link-preflight",
            str(link),
            "--out",
            str(output),
        ],
    )

    assert field_session_main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["link_preflight"]["hardware_link_ready"] is False
    assert report["gate_passed"] is False


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"state":"DET","state":"TRK"}\n', "duplicate JSON key"),
        ('{"state":"DET","timestamp_s":NaN}\n', "non-finite JSON number NaN"),
    ),
)
def test_field_session_jsonl_loader_is_strict_and_accepts_windows_bom(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(("\ufeff" + payload).encode("utf-8"))

    with pytest.raises(ValueError, match=message):
        load_jsonl(path)

    valid = tmp_path / "valid-session.jsonl"
    valid.write_bytes(
        ("\ufeff" + json.dumps(_record("DET", 0.0), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
    )
    assert len(load_jsonl(valid)) == 1

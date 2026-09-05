from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.field_session_capture import (
    capture_field_session,
    main,
    verify_field_session_capture,
    verify_main,
)


def _audit_record(
    state: str,
    timestamp_s: float,
    *,
    session_id: str = "session-a",
) -> dict[str, object]:
    return {
        "sequence": int(timestamp_s * 10) + 1,
        "session_id": session_id,
        "timestamp_s": timestamp_s,
        "event_type": "field.state_changed",
        "details": {
            "state": state,
            "target_id": "target-a",
            "gps": {
                "position_healthy": True,
                "latitude_deg": 22.3001,
                "longitude_deg": 114.1734,
                "fix_type": 3,
                "satellites_visible": 12,
                "h_acc_m": 1.2,
                "v_acc_m": 2.4,
                "observed_at_s": timestamp_s,
            },
            "range": {
                "validity": "VALID",
                "distance_m": 12.5,
                "source_age_s": 0.1,
                "data_freshness_s": 0.1,
            },
        },
    }


def _write_jsonl(path: Path, records: list[object], *, final_newline: bool = True) -> None:
    encoded = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if final_newline:
        encoded += "\n"
    path.write_text(encoded, encoding="utf-8")


def _write_ready_link_report(path: Path, *, ready: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"name": "field", "ok": ready}],
                "summary": {"all_passed": ready, "hardware_link_ready": ready},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_static_capture_stops_at_target_and_runs_shared_gate(tmp_path: Path) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "field-session.jsonl"
    records = [
        _audit_record(state, index * 0.1)
        for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
    ]
    records.append(_audit_record("TRK", 0.4))
    _write_jsonl(source, records)

    report = capture_field_session(source, output, stop_state="target")

    captured = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [item["details"]["state"] for item in captured] == ["DET", "TRK", "LCK", "TGT"]
    assert report["capture"]["stop_reason"] == "stop_state:TGT"
    assert report["capture"]["captured_records"] == 4
    assert report["capture"]["observed_session_ids"] == ["session-a"]
    assert report["validation"]["gate_passed"] is True
    assert report["gate_passed"] is True
    assert report["capture"]["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    capture = report["capture"]
    source_bytes = source.read_bytes()
    prefix_size = capture["source_prefix_bytes_consumed"]
    assert prefix_size < len(source_bytes)
    assert capture["source_prefix_complete"] is True
    assert capture["source_prefix_bytes"] == prefix_size
    assert capture["source_prefix_sha256"] == hashlib.sha256(source_bytes[:prefix_size]).hexdigest()
    assert capture["captured_output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert capture["captured_output_bytes"] == output.stat().st_size

    # Appending a later event changes the whole-file digest, but never the
    # prefix identity of the evidence that was actually copied.
    prefix_hash = capture["source_prefix_sha256"]
    with source.open("ab") as handle:
        handle.write((json.dumps(_audit_record("TRK", 0.5)) + "\n").encode("utf-8"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() != capture["source_sha256"]
    assert hashlib.sha256(source.read_bytes()[:prefix_size]).hexdigest() == prefix_hash


def test_capture_strict_target_state_evidence_is_recomputed_by_verifier(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "field-session.jsonl"
    report_path = tmp_path / "field-session.capture.json"
    records = [
        _audit_record(state, index * 0.1)
        for index, state in enumerate(("DET", "TRK", "LCK"))
    ]
    stale_target = _audit_record("TGT", 0.3)
    stale_target["details"]["gps"]["observed_at_s"] = -2.0  # type: ignore[index]
    stale_target["details"]["range"]["source_age_s"] = 2.0  # type: ignore[index]
    records.append(stale_target)
    _write_jsonl(source, records)

    report = capture_field_session(
        source,
        output,
        require_target_state_evidence=True,
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert report["capture"]["require_target_state_evidence"] is True
    assert report["validation"]["gate_passed"] is False
    assert report["validation"]["target_state_evidence"]["valid"] is False
    verified = verify_field_session_capture(report_path)
    assert verified["gate_passed"] is False
    assert verified["checks"]["validation_match"] is True


def test_capture_can_bind_to_ready_link_preflight_and_verifier_rechecks_hash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "field-session.jsonl"
    report_path = tmp_path / "field-session.capture.json"
    link_path = tmp_path / "field-link.json"
    _write_jsonl(
        source,
        [
            _audit_record(state, index * 0.1)
            for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
        ],
    )
    _write_ready_link_report(link_path)

    report = capture_field_session(
        source,
        output,
        link_preflight=link_path,
        require_link_preflight=True,
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert report["gate_passed"] is True
    assert report["link_preflight"]["required"] is True
    assert report["link_preflight"]["verified"] is True
    assert report["link_preflight"]["hardware_link_ready"] is True
    verified = verify_field_session_capture(report_path)
    assert verified["gate_passed"] is True
    assert verified["checks"]["link_preflight"]["verified"] is True

    _write_ready_link_report(link_path, ready=False)
    changed = verify_field_session_capture(report_path)
    assert changed["gate_passed"] is False
    assert "link_preflight report SHA-256 does not match" in changed["reasons"]


def test_capture_with_failed_link_preflight_stays_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "field-session.jsonl"
    link_path = tmp_path / "field-link.json"
    _write_jsonl(
        source,
        [
            _audit_record(state, index * 0.1)
            for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
        ],
    )
    _write_ready_link_report(link_path, ready=False)

    report = capture_field_session(
        source,
        output,
        link_preflight=link_path,
        require_link_preflight=True,
    )

    assert report["validation"]["gate_passed"] is True
    assert report["link_preflight"]["verified"] is False
    assert report["gate_passed"] is False


def test_capture_requires_link_path_when_requested(tmp_path: Path) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "field-session.jsonl"
    _write_jsonl(source, [_audit_record("DET", 0.0)])

    report = capture_field_session(
        source,
        output,
        require_link_preflight=True,
    )

    assert report["link_preflight"]["verified"] is False
    assert report["link_preflight"]["reasons"] == ["link_preflight path is required"]
    assert report["gate_passed"] is False


def test_capture_filters_audit_sessions_without_rewriting_records(tmp_path: Path) -> None:
    source = tmp_path / "combined.audit.jsonl"
    output = tmp_path / "session-b.jsonl"
    records = [_audit_record("telemetry", 0.0, session_id="session-old")]
    records.extend(
        _audit_record(state, index * 0.1, session_id="session-b")
        for index, state in enumerate(("DET", "TRK", "LCK", "TGT"), start=1)
    )
    _write_jsonl(source, records)

    report = capture_field_session(source, output, session_id="session-b")

    captured = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(captured) == 4
    assert all(item["session_id"] == "session-b" for item in captured)
    assert report["capture"]["filtered_records"] == 1
    assert report["validation"]["state_sequence_valid"] is True
    assert report["gate_passed"] is True


def test_malformed_source_line_is_reported_and_closes_gate(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    valid = [
        _audit_record(state, index * 0.1)
        for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
    ]
    source.write_text(
        "\n".join(json.dumps(item) for item in valid[:2])
        + "\n{not-json}\n"
        + "\n".join(json.dumps(item) for item in valid[2:])
        + "\n",
        encoding="utf-8",
    )

    report = capture_field_session(source, output)

    assert report["capture"]["source_errors"] == [{"line": 3, "reason": "invalid_json"}]
    assert report["validation"]["gate_passed"] is True
    assert report["gate_passed"] is False


def test_capture_accepts_source_bom_but_rejects_ambiguous_json_values(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strict.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    valid = json.dumps(_audit_record("DET", 0.0), ensure_ascii=False).encode("utf-8")
    source.write_bytes(
        b"\xef\xbb\xbf"
        + valid
        + b"\n{\"state\":\"TRK\",\"state\":\"LCK\"}\n"
        + b"{\"state\":\"TGT\",\"timestamp_s\":Infinity}\n"
    )

    report = capture_field_session(source, output)

    assert report["capture"]["captured_records"] == 1
    assert report["capture"]["source_errors"] == [
        {"line": 2, "reason": "invalid_json"},
        {"line": 3, "reason": "invalid_json"},
    ]
    assert report["gate_passed"] is False


def test_unterminated_tail_is_accepted_in_static_mode(tmp_path: Path) -> None:
    source = tmp_path / "final-record.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    record = _audit_record("DET", 0.0)
    _write_jsonl(source, [record], final_newline=False)

    report = capture_field_session(source, output)

    assert report["capture"]["incomplete_source_tail"] is False
    assert report["capture"]["captured_records"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["event_type"] == "field.state_changed"


def test_follow_mode_marks_incomplete_tail_and_stops_on_idle(tmp_path: Path) -> None:
    source = tmp_path / "partial.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    source.write_text('{"timestamp_s": 1', encoding="utf-8")

    current = [0.0]

    def clock() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        current[0] += seconds

    report = capture_field_session(
        source,
        output,
        follow=True,
        idle_timeout_s=0.5,
        poll_interval_s=0.25,
        clock=clock,
        sleep=sleep,
    )

    assert report["capture"]["stop_reason"] == "idle_timeout"
    assert report["capture"]["incomplete_source_tail"] is True
    assert report["capture"]["capture_errors"] == [{"reason": "incomplete_source_tail"}]
    assert report["gate_passed"] is False
    assert output.read_text(encoding="utf-8") == ""


def test_capture_rejects_existing_destination_and_same_path(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "output.jsonl"
    _write_jsonl(source, [_audit_record("DET", 0.0)])
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        capture_field_session(source, output)
    with pytest.raises(ValueError, match="different files"):
        capture_field_session(source, source, overwrite=True)


def test_cli_require_gate_uses_default_report_path(tmp_path: Path) -> None:
    source = tmp_path / "bad.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    _write_jsonl(source, [_audit_record("DET", 0.0)])

    exit_code = main([str(source), "--output", str(output), "--require-gate"])

    assert exit_code == 1
    report_path = output.with_suffix(".jsonl.capture.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["gate_passed"] is False
    assert report["validation"]["state_sequence_valid"] is False


def test_start_sequence_is_fail_closed_when_audit_sequence_is_missing(tmp_path: Path) -> None:
    source = tmp_path / "sequence.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    first = _audit_record("DET", 0.0)
    second = _audit_record("TRK", 0.1)
    del second["sequence"]
    _write_jsonl(source, [first, second])

    report = capture_field_session(source, output, start_sequence=1)

    assert report["capture"]["selection_errors"] == [
        {"line": 2, "reason": "sequence_missing_or_invalid"}
    ]
    assert report["gate_passed"] is False


def test_capture_report_verifier_binds_output_and_allows_source_append(tmp_path: Path) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    report_path = tmp_path / "captured.capture.json"
    records = [
        _audit_record(state, index * 0.1)
        for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
    ]
    _write_jsonl(source, records)
    report = capture_field_session(source, output, stop_state="TGT")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with source.open("ab") as handle:
        handle.write((json.dumps(_audit_record("TRK", 0.5)) + "\n").encode("utf-8"))

    verified = verify_field_session_capture(report_path)
    assert verified["gate_passed"] is True
    assert verified["checks"]["source"]["source_changed_after_capture"] is True
    assert verified["checks"]["validation_match"] is True


def test_capture_report_verifier_keeps_pre_target_state_reports_compatible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.audit.jsonl"
    output = tmp_path / "legacy-captured.jsonl"
    report_path = tmp_path / "legacy.capture.json"
    _write_jsonl(
        source,
        [
            _audit_record(state, index * 0.1)
            for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
        ],
    )
    report = capture_field_session(source, output)
    # Simulate a pre-strict-gate report on disk: remove only the fields that
    # did not exist before the TGT-context provenance was introduced.
    report["capture"].pop("require_target_state_evidence", None)
    report["validation"].pop("target_state_evidence", None)
    for section_name in ("gps", "range"):
        for item in report["validation"][section_name]["results"]:
            item.pop("state_context", None)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    verified = verify_field_session_capture(report_path)
    assert verified["gate_passed"] is True
    assert verified["checks"]["validation_match"] is True


def test_capture_report_verifier_rejects_output_drift_and_cli_requires_gate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.audit.jsonl"
    output = tmp_path / "captured.jsonl"
    report_path = tmp_path / "captured.capture.json"
    _write_jsonl(
        source,
        [
            _audit_record(state, index * 0.1)
            for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
        ],
    )
    report = capture_field_session(source, output)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    output.write_text(output.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    verified = verify_field_session_capture(report_path)
    assert verified["gate_passed"] is False
    assert "captured output SHA-256 does not match" in verified["reasons"]
    assert verify_main([str(report_path), "--require-gate"]) == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"schema_version":1,"schema_version":1}', "duplicate JSON key"),
        ('{"schema_version":NaN}', "non-finite JSON number NaN"),
    ),
)
def test_capture_report_verifier_rejects_ambiguous_json(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    report_path = tmp_path / "ambiguous.capture.json"
    report_path.write_text(payload, encoding="utf-8")

    verified = verify_field_session_capture(report_path)

    assert verified["gate_passed"] is False
    assert any(message in reason for reason in verified["reasons"])

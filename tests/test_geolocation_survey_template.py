from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from multidetect.geolocation_survey_template import (
    TEMPLATE_COLUMNS,
    build_geolocation_survey_template,
    write_geolocation_survey_template,
    write_template_summary,
)
from multidetect.target_event_replay import (
    TargetEventInput,
    TargetEventReplayBuilder,
    TargetGpsEvidence,
    TargetMediaInput,
    TargetRangeEvidence,
)

ROOT = Path(__file__).resolve().parents[1]


def _event(
    index: int,
    *,
    observed_at_s: float | None = 1.0,
    qualified: bool = True,
) -> TargetEventInput:
    captured_at_s = float(index + 1)
    if qualified:
        gps = TargetGpsEvidence(
            status="qualified",
            latitude_deg=1.3000 + index * 0.00001,
            longitude_deg=103.8000 + index * 0.00001,
            horizontal_sigma_m=2.0,
            fix_type=3,
            satellites_visible=12,
            horizontal_accuracy_m=2.5,
            vertical_accuracy_m=4.0,
            observed_at_s=captured_at_s if observed_at_s == 1.0 else observed_at_s,
        )
    else:
        gps = TargetGpsEvidence(status="unqualified")
    return TargetEventInput(
        event_id=f"event-{index:02d}",
        frame_id=f"frame-{index:02d}",
        target_id="target-a" if index != 2 else "target-b",
        target_label="person",
        captured_at_s=captured_at_s,
        gps=gps,
        ranging=TargetRangeEvidence(
            validity="VALID",
            range_m=20.0,
            relative_bearing_deg=0.0,
            range_kind="ground",
        ),
        video=TargetMediaInput(
            kind="video_frame",
            payload=f"frame-{index}".encode("ascii"),
            encoding="jpeg",
        ),
    )


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "target-events"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(_event(0, observed_at_s=1.0))
    builder.append(_event(1, observed_at_s=None))
    builder.append(_event(2, qualified=False))
    builder.finalize()
    return root / "manifest.json"


def test_template_selects_only_fresh_qualified_events_and_records_reasons(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    result = build_geolocation_survey_template(manifest)

    assert result.selected_event_count == 3
    assert result.eligible_event_count == 1
    assert result.skipped_event_count == 2
    assert result.skipped_event_ids == ("event-01", "event-02")
    assert result.skipped_reasons == {
        "gps_timestamp_unknown": 1,
        "gps_not_qualified": 1,
    }
    assert result.rows[0]["sample_id"] == "event-00"
    assert result.rows[0]["source_frame_id"] == "frame-00"
    assert result.rows[0]["ground_truth_latitude_deg"] is None
    assert result.summary()["contains_truth"] is False
    assert result.summary()["gate_passed"] is False

    payload = manifest.read_bytes()
    assert result.source_manifest_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.source_manifest_bytes == len(payload)


def test_template_target_and_time_filters_are_applied_before_freshness(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    result = build_geolocation_survey_template(
        manifest,
        target_id="target-a",
        start_s=1.0,
        end_s=2.0,
    )
    assert result.selected_event_count == 2
    assert result.eligible_event_count == 1
    assert result.target_id == "target-a"
    assert result.start_s == 1.0
    assert result.end_s == 2.0

    with pytest.raises(ValueError, match="must not be before"):
        build_geolocation_survey_template(manifest, start_s=3.0, end_s=2.0)


def test_template_writers_emit_fillable_csv_json_and_jsonl(tmp_path: Path) -> None:
    result = build_geolocation_survey_template(_bundle(tmp_path))

    csv_path = tmp_path / "survey.csv"
    assert write_geolocation_survey_template(csv_path, result) == csv_path.resolve()
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == TEMPLATE_COLUMNS
    assert rows[0]["ground_truth_latitude_deg"] == ""
    assert rows[0]["estimated_latitude_deg"]
    assert "frame_id" not in rows[0]

    json_path = tmp_path / "survey.json"
    write_geolocation_survey_template(json_path, result)
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["evidence_type"] == "multidetect.target-geolocation-survey-template.v1"
    assert document["contains_truth"] is False
    assert document["rows"][0]["ground_truth_longitude_deg"] is None

    jsonl_path = tmp_path / "survey.jsonl"
    write_geolocation_survey_template(jsonl_path, result)
    jsonl_rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert jsonl_rows[0]["sample_id"] == "event-00"

    summary_path = tmp_path / "survey.summary.json"
    write_template_summary(summary_path, result, output_path=csv_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["output"] == str(csv_path.resolve())
    assert summary["output_format"] == "csv"


def test_template_json_schema_matches_truth_free_contract() -> None:
    schema_path = ROOT / "configs" / "schemas" / "target-geolocation-survey-template.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["evidence_type"]["const"] == (
        "multidetect.target-geolocation-survey-template.v1"
    )
    assert schema["properties"]["contains_truth"]["const"] is False
    assert schema["properties"]["gate_passed"]["const"] is False
    row = schema["$defs"]["row"]["properties"]
    assert row["gps_freshness"]["const"] == "fresh"
    assert row["ground_truth_latitude_deg"]["$ref"] == "#/$defs/optionalLatitude"


def test_template_writer_protects_filled_sheet_without_overwrite(tmp_path: Path) -> None:
    result = build_geolocation_survey_template(_bundle(tmp_path))
    output = tmp_path / "survey.csv"
    output.write_text("operator-filled-truth\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="use --overwrite"):
        write_geolocation_survey_template(output, result)
    write_geolocation_survey_template(output, result, overwrite=True)
    assert output.read_text(encoding="utf-8").startswith("sample_id,event_id,")

    with pytest.raises(ValueError, match="must use .csv"):
        write_geolocation_survey_template(tmp_path / "survey.txt", result)


def test_template_cli_writes_selection_sidecar_and_returns_ready_rows(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    output = tmp_path / "survey.csv"
    summary_output = tmp_path / "survey-summary.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "create_target_geolocation_survey_template.py"),
            str(manifest),
            "--output",
            str(output),
            "--summary-output",
            str(summary_output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(summary_output.read_text(encoding="utf-8"))
    assert summary["eligible_event_count"] == 1
    assert summary["skipped_event_count"] == 2
    printed = json.loads(completed.stdout)
    assert printed["output_format"] == "csv"


def test_template_cli_returns_one_when_no_fresh_event_exists(tmp_path: Path) -> None:
    root = tmp_path / "target-events"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(_event(0, qualified=False))
    builder.finalize()
    output = tmp_path / "empty.csv"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "create_target_geolocation_survey_template.py"),
            str(root / "manifest.json"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert output.is_file()
    assert "eligible_event_count" in completed.stdout


def test_template_script_help_runs_without_pythonpath() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "create_target_geolocation_survey_template.py"),
            "--help",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--summary-output" in completed.stdout

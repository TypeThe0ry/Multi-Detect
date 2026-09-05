from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.geolocation_accuracy import verify_geolocation_accuracy_report
from multidetect.geolocation_survey import (
    build_accuracy_report_from_target_event_replay,
    load_survey_truth,
)
from multidetect.target_event_replay import (
    TargetEventInput,
    TargetEventReplayBuilder,
    TargetGpsEvidence,
    TargetMediaInput,
    TargetRangeEvidence,
)

ROOT = Path(__file__).resolve().parents[1]
_UNSET = object()


def _event(
    index: int,
    *,
    observed_at_s: float | None | object = _UNSET,
    status: str = "qualified",
) -> TargetEventInput:
    captured_at_s = float(index + 1)
    gps = (
        TargetGpsEvidence(
            status="unavailable",
        )
        if status == "unavailable"
        else TargetGpsEvidence(
            status="qualified",
            latitude_deg=1.3000 + index * 0.00001,
            longitude_deg=103.8000 + index * 0.00001,
            horizontal_sigma_m=2.0,
            fix_type=3,
            satellites_visible=12,
            horizontal_accuracy_m=2.5,
            vertical_accuracy_m=4.0,
            observed_at_s=captured_at_s if observed_at_s is _UNSET else observed_at_s,  # type: ignore[arg-type]
        )
    )
    return TargetEventInput(
        event_id=f"event-{index:02d}",
        frame_id=f"frame-{index:02d}",
        target_id="target-a",
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


def _bundle(tmp_path: Path, count: int = 10) -> Path:
    root = tmp_path / "target-events"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    for index in range(count):
        builder.append(_event(index))
    builder.finalize()
    return root / "manifest.json"


def _truth_for(count: int = 10, *, key_name: str = "sample_id") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        row: dict[str, object] = {
            key_name: f"event-{index:02d}" if key_name != "frame_id" else f"frame-{index:02d}",
            "ground_truth_latitude_deg": 1.3000 + index * 0.00001,
            "ground_truth_longitude_deg": 103.8000 + index * 0.00001,
            "target_id": "target-a",
        }
        rows.append(row)
    return rows


def test_replay_truth_join_uses_verified_event_ids_and_fresh_gps(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    result = build_accuracy_report_from_target_event_replay(
        manifest,
        _truth_for(),
        maximum_p95_m=0.01,
        minimum_one_sigma_coverage=1.0,
        minimum_two_sigma_coverage=1.0,
    )

    assert result.truth_row_count == 10
    assert result.eligible_event_count == 10
    assert result.matched_event_count == 10
    assert result.skipped_event_count == 0
    assert result.unmatched_event_ids == ()
    assert result.unused_truth_ids == ()
    assert result.report["gate_passed"] is True
    assert result.report["measurement"]["sample_count"] == 10  # type: ignore[index]
    assert result.report["samples"][0]["sample_id"] == "event-00"  # type: ignore[index]
    selection = result.report["selection"]
    assert selection["selected_event_count"] == 10  # type: ignore[index]
    assert selection["eligible_event_count"] == 10  # type: ignore[index]
    assert selection["matched_event_count"] == 10  # type: ignore[index]
    assert selection["skipped_event_count"] == 0  # type: ignore[index]
    assert selection["unmatched_event_ids"] == []  # type: ignore[index]
    assert (
        verify_geolocation_accuracy_report(result.report, require_gate_passed=False)["passed"]
        is True
    )


def test_replay_truth_join_accepts_frame_id_labels(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path, count=2)
    result = build_accuracy_report_from_target_event_replay(
        manifest,
        _truth_for(2, key_name="frame_id"),
        minimum_samples=2,
    )
    assert result.matched_event_count == 2
    assert result.report["gate_passed"] is True


def test_replay_truth_selection_metadata_is_tamper_evident(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path, count=2)
    result = build_accuracy_report_from_target_event_replay(
        manifest,
        _truth_for(2),
        minimum_samples=2,
    )
    selection = result.report["selection"]
    selection["matched_event_count"] = 1  # type: ignore[index]
    verification = verify_geolocation_accuracy_report(
        result.report,
        require_gate_passed=False,
    )
    assert verification["passed"] is False
    assert any("selection matched_event_count" in reason for reason in verification["reasons"])


def test_survey_truth_loader_accepts_json_and_jsonl_wrappers(tmp_path: Path) -> None:
    rows = _truth_for(1)
    json_path = tmp_path / "truth.json"
    json_path.write_text(json.dumps({"truth": rows}), encoding="utf-8")
    assert load_survey_truth(json_path)[0].key == "event-00"

    jsonl_path = tmp_path / "truth.jsonl"
    jsonl_path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    assert load_survey_truth(jsonl_path)[0].latitude_deg == pytest.approx(1.3)


@pytest.mark.parametrize(
    ("suffix", "payload", "message"),
    (
        (
            ".json",
            '{"truth":[{"event_id":"event-00","event_id":"other",'
            '"ground_truth_latitude_deg":1.3,"ground_truth_longitude_deg":103.8}]}',
            "duplicate JSON key",
        ),
        (
            ".jsonl",
            '{"event_id":"event-00","ground_truth_latitude_deg":NaN,'
            '"ground_truth_longitude_deg":103.8}\n',
            "non-finite JSON number NaN",
        ),
    ),
)
def test_survey_truth_loader_rejects_ambiguous_json(
    tmp_path: Path,
    suffix: str,
    payload: str,
    message: str,
) -> None:
    path = tmp_path / f"truth{suffix}"
    path.write_bytes(("\ufeff" + payload).encode("utf-8"))

    with pytest.raises(ValueError, match=message):
        load_survey_truth(path)


def test_replay_truth_join_skips_stale_gps_and_never_uses_unknown_timestamp(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target-events"
    builder = TargetEventReplayBuilder(root, session_id=str(uuid4()))
    builder.append(_event(0))
    builder.append(_event(1, status="unavailable"))
    builder.append(_event(2, observed_at_s=None))
    builder.finalize()

    truth = _truth_for(1)
    result = build_accuracy_report_from_target_event_replay(
        root / "manifest.json",
        truth,
        minimum_samples=1,
    )
    assert result.eligible_event_count == 1
    assert result.matched_event_count == 1
    assert result.skipped_event_count == 2
    assert result.report["gate_passed"] is True


def test_replay_truth_join_fails_closed_on_unmatched_or_unused_rows(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path, count=2)
    with pytest.raises(ValueError, match="unmatched fresh replay events"):
        build_accuracy_report_from_target_event_replay(
            manifest,
            _truth_for(1),
            minimum_samples=1,
        )

    with pytest.raises(ValueError, match="truth rows not present in replay"):
        build_accuracy_report_from_target_event_replay(
            manifest,
            _truth_for(3),
            minimum_samples=1,
        )


def test_replay_truth_join_rejects_conflicting_target_identity(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path, count=1)
    truth = _truth_for(1)
    truth[0]["target_id"] = "target-other"
    with pytest.raises(ValueError, match="target_id does not match"):
        build_accuracy_report_from_target_event_replay(manifest, truth, minimum_samples=1)


def test_replay_truth_join_rejects_conflicting_event_and_frame_rows(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path, count=1)
    truth = [
        {
            "event_id": "event-00",
            "ground_truth_latitude_deg": 1.3,
            "ground_truth_longitude_deg": 103.8,
        },
        {
            "frame_id": "frame-00",
            "ground_truth_latitude_deg": 1.3001,
            "ground_truth_longitude_deg": 103.8001,
        },
    ]
    with pytest.raises(ValueError, match="duplicated|conflicting"):
        build_accuracy_report_from_target_event_replay(manifest, truth, minimum_samples=1)


def test_replay_truth_join_cli_writes_report_and_prints_selection(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    truth_path = tmp_path / "truth.csv"
    with truth_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "sample_id",
                "ground_truth_latitude_deg",
                "ground_truth_longitude_deg",
                "target_id",
            ),
        )
        writer.writeheader()
        writer.writerows(_truth_for())
    output = tmp_path / "accuracy.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_target_geolocation_accuracy.py"),
            str(manifest),
            str(truth_path),
            "--output",
            str(output),
            "--maximum-p95-m",
            "0.01",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["evidence_type"] == "multidetect.target-geolocation-accuracy.v1"
    assert report["gate_passed"] is True
    assert "eligible_events=10" in completed.stdout
    assert "matched_events=10" in completed.stdout
    assert "source_manifest_sha256=" in completed.stdout


def test_replay_truth_join_rejects_invalid_bundle_before_join(tmp_path: Path) -> None:
    manifest = tmp_path / "missing-manifest.json"
    with pytest.raises((AutonomyContractError, FileNotFoundError)):
        build_accuracy_report_from_target_event_replay(manifest, _truth_for())


def test_target_accuracy_report_schema_declares_offline_contract() -> None:
    schema_path = ROOT / "configs" / "schemas" / "target-geolocation-accuracy-report.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["evidence_type"]["const"] == (
        "multidetect.target-geolocation-accuracy.v1"
    )
    assert schema["properties"]["ground_truth_isolated"]["const"] is True
    assert schema["properties"]["advisory_only"]["const"] is True
    assert schema["properties"]["contains_flight_control"]["const"] is False
    assert schema["$defs"]["coordinateLatitude"]["exclusiveMinimum"] == -90
    assert schema["$defs"]["coordinateLatitude"]["exclusiveMaximum"] == 90
    assert schema["properties"]["error_ellipse"]["$ref"] == "#/$defs/errorEllipse"
    assert schema["$defs"]["errorEllipse"]["additionalProperties"] is False
    assert "major_95_m" in schema["$defs"]["errorEllipse"]["required"]
    selection = schema["$defs"]["selection"]
    assert selection["additionalProperties"] is False
    assert selection["properties"]["source_manifest_sha256"]["pattern"] == (
        "^[0-9a-fA-F]{64}$"
    )

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from multidetect.depth_calibration import load_calibration_document

ROOT = Path(__file__).resolve().parents[1]


def _audit_event(
    sequence: int,
    timestamp_s: float,
    event_type: str,
    details: dict[str, object],
) -> str:
    return json.dumps(
        {
            "sequence": sequence,
            "timestamp_s": timestamp_s,
            "event_type": event_type,
            "details": details,
        }
    )


def test_exporter_keeps_only_geometry_accepted_target_depth_and_fitter_ignores_blanks(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "field.audit.jsonl"
    audit.write_text(
        "\n".join(
            (
                _audit_event(
                    1,
                    10.0,
                    "ranging.metric_depth_updated",
                    {
                        "frame_id": "frame-grid",
                        "target_id": "depth-grid",
                        "raw_slant_range_m": 7.0,
                        "slant_range_m": 3.5,
                        "sigma_m": 0.5,
                        "calibration_profile": "outdoor-temp",
                        "calibration_scale": 0.5,
                    },
                ),
                _audit_event(
                    2,
                    10.1,
                    "ranging.metric_depth_updated",
                    {
                        "frame_id": "frame-bad",
                        "target_id": "target-1",
                        "raw_slant_range_m": 9.0,
                        "slant_range_m": 4.5,
                        "sigma_m": 0.5,
                        "calibration_profile": "outdoor-temp",
                        "calibration_scale": 0.5,
                    },
                ),
                _audit_event(
                    3,
                    10.2,
                    "ranging.metric_depth_association",
                    {
                        "target_id": "target-1",
                        "source_frame_id": "frame-bad",
                        "status": "geometry_rejected",
                    },
                ),
                _audit_event(
                    4,
                    11.0,
                    "ranging.metric_depth_updated",
                    {
                        "frame_id": "frame-good",
                        "target_id": "target-1",
                        "raw_slant_range_m": 10.0,
                        "slant_range_m": 5.0,
                        "sigma_m": 0.5,
                        "calibration_profile": "outdoor-temp",
                        "calibration_scale": 0.5,
                    },
                ),
                _audit_event(
                    5,
                    11.1,
                    "ranging.metric_depth_association",
                    {
                        "target_id": "target-1",
                        "source_frame_id": "frame-good",
                        "status": "accepted",
                        "source_iou": 0.9,
                        "center_distance": 0.01,
                        "source_age_s": 0.2,
                        "compatible_sample_count": 3,
                    },
                ),
                _audit_event(
                    6,
                    11.5,
                    "ranging.metric_depth_updated",
                    {
                        "frame_id": "frame-duplicate",
                        "target_id": "target-1",
                        "raw_slant_range_m": 10.1,
                        "slant_range_m": 5.05,
                        "sigma_m": 0.5,
                        "calibration_profile": "outdoor-temp",
                        "calibration_scale": 0.5,
                    },
                ),
                _audit_event(
                    7,
                    11.6,
                    "ranging.metric_depth_association",
                    {
                        "target_id": "target-1",
                        "source_frame_id": "frame-duplicate",
                        "status": "accepted",
                        "source_iou": 0.9,
                        "center_distance": 0.01,
                        "source_age_s": 0.2,
                        "compatible_sample_count": 3,
                    },
                ),
                _audit_event(
                    8,
                    14.0,
                    "ranging.metric_depth_updated",
                    {
                        "frame_id": "frame-decreasing",
                        "target_id": "target-1",
                        "raw_slant_range_m": 2.0,
                        "slant_range_m": 1.0,
                        "sigma_m": 0.5,
                        "calibration_profile": "outdoor-temp",
                        "calibration_scale": 0.5,
                    },
                ),
                _audit_event(
                    9,
                    14.1,
                    "ranging.metric_depth_association",
                    {
                        "target_id": "target-1",
                        "source_frame_id": "frame-decreasing",
                        "status": "accepted",
                        "source_iou": 0.9,
                        "center_distance": 0.01,
                        "source_age_s": 0.2,
                        "compatible_sample_count": 3,
                    },
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    candidates = tmp_path / "candidates.csv"
    exported = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/export_outdoor_depth_calibration_candidates.py"),
            str(audit),
            "--output",
            str(candidates),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "2 accepted calibration candidates" in exported.stdout
    with candidates.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["source_frame_id"] == "frame-good"
    assert rows[0]["raw_depth_m"] == "10.0"
    assert rows[1]["source_frame_id"] == "frame-decreasing"

    # Keep the unmeasured candidate and append four independent surveyed points.
    for index, (raw, actual) in enumerate(((4, 2.1), (8, 4.1), (14, 7.1), (20, 10.1)), 1):
        rows.append(
            {
                "sample_id": f"measured-{index}",
                "actual_distance_m": str(actual),
                "raw_depth_m": str(raw),
            }
        )
    with candidates.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({key for row in rows for key in row}))
        writer.writeheader()
        writer.writerows(rows)
    document = tmp_path / "outdoor.json"
    fitted = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/fit_outdoor_depth_calibration.py"),
            str(candidates),
            "--output",
            str(document),
            "--profile",
            "outdoor-survey-exported",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "inliers=4/4" in fitted.stdout
    assert load_calibration_document(document).profile == "outdoor-survey-exported"

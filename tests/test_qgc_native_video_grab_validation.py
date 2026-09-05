from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

from scripts.validate_qgc_native_video_grab import build_evidence, main


def _write_structured(
    path: Path,
    *,
    black: bool = False,
    width: int = 640,
    height: int = 360,
) -> None:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    if not black:
        gradient = np.linspace(50, 220, height, dtype=np.uint8)[:, None, None]
        image[:] = gradient
        image[height // 4 : height * 3 // 4, width // 4 : width * 3 // 4] = (
            30,
            110,
            230,
        )
    assert cv2.imwrite(str(path), image)


def _marker(
    *,
    role: str,
    path: Path,
    sequence: int,
    recorded_at: datetime,
    fixed_wing: bool,
    match_count: int = 1,
    output_width: float = 640,
    output_height: float = 360,
    capture_width: int = 640,
    capture_height: int = 360,
    device_pixel_ratio: float = 1.0,
) -> dict[str, object]:
    return {
        "schema": "multidetect.qgc-hil-video-grab",
        "schema_version": 1,
        "sequence": sequence,
        "recorded_at_utc": recorded_at.isoformat().replace("+00:00", "Z"),
        "role": role,
        "path": str(path.resolve()),
        "object_name": "videoContent",
        "native_output_match_count": match_count,
        "output_width": output_width,
        "output_height": output_height,
        "device_pixel_ratio": device_pixel_ratio,
        "capture_width": capture_width,
        "capture_height": capture_height,
        "decoded_width": 1280,
        "decoded_height": 960,
        "decoding": True,
        "output_visible": True,
        "video_full_state": True,
        "vtol_in_fwd_flight": fixed_wing,
        "queued": True,
        "saved": True,
    }


def _write_log(path: Path, records: list[dict[str, object]]) -> None:
    lines = [
        "qml: MULTIDETECT_HIL_VIDEO_GRAB "
        + json.dumps(record, separators=(",", ":"))
        for record in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, datetime]:
    start = datetime.now(timezone.utc) - timedelta(seconds=2)
    capture_a = tmp_path / "native-pip.png"
    capture_b = tmp_path / "native-fixed.png"
    log = tmp_path / "qgc.log"
    _write_structured(capture_a)
    _write_structured(capture_b)
    now = datetime.now(timezone.utc)
    _write_log(
        log,
        [
            _marker(
                role="pip-swapped",
                path=capture_a,
                sequence=1,
                recorded_at=now,
                fixed_wing=False,
            ),
            _marker(
                role="fixed-wing",
                path=capture_b,
                sequence=2,
                recorded_at=now + timedelta(milliseconds=20),
                fixed_wing=True,
            ),
        ],
    )
    return capture_a, capture_b, log, start


def _evaluate(tmp_path: Path) -> dict[str, object]:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    return build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )


def test_native_videooutput_grabs_pass_without_claiming_human_acceptance(
    tmp_path: Path,
) -> None:
    evidence = _evaluate(tmp_path)

    assert evidence["gate_passed"] is True
    assert evidence["native_qquickitem_video_pixels_proved"] is True
    assert evidence["transport_bound_native_qquickitem_video_surface_proved"] is True
    assert evidence["native_qgc_ui_contract"]["second_video_output_created"] is False
    assert evidence["desktop_capture_gpu_surface_proved"] is False
    assert evidence["complete_qml_rendering_semantics_proved"] is False
    assert evidence["operator_visual_acceptance"] == "not_claimed"


def test_dpr_1_25_maps_logical_output_to_physical_grab_with_qt_rounding(
    tmp_path: Path,
) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    _write_structured(capture_a, width=859, height=644)
    _write_structured(capture_b, width=859, height=644)
    now = datetime.now(timezone.utc)
    _write_log(
        log,
        [
            _marker(
                role="pip-swapped",
                path=capture_a,
                sequence=1,
                recorded_at=now,
                fixed_wing=False,
                output_width=687,
                output_height=515,
                capture_width=859,
                capture_height=644,
                device_pixel_ratio=1.25,
            ),
            _marker(
                role="fixed-wing",
                path=capture_b,
                sequence=2,
                recorded_at=now + timedelta(milliseconds=20),
                fixed_wing=True,
                output_width=687,
                output_height=515,
                capture_width=859,
                capture_height=644,
                device_pixel_ratio=1.25,
            ),
        ],
    )

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is True
    for capture in (evidence["capture_a"], evidence["capture_b"]):
        assert capture["marker_checks"]["device_pixel_ratio_positive_finite"] is True
        assert (
            capture["marker_checks"][
                "capture_geometry_matches_logical_output_and_dpr"
            ]
            is True
        )


def test_wrong_dpr_is_rejected_even_when_png_and_marker_capture_sizes_match(
    tmp_path: Path,
) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    _write_structured(capture_a, width=859, height=644)
    _write_structured(capture_b, width=859, height=644)
    records = []
    for line in log.read_text(encoding="utf-8").splitlines():
        record = json.loads(line.split("MULTIDETECT_HIL_VIDEO_GRAB ", 1)[1])
        record.update(
            {
                "output_width": 687,
                "output_height": 515,
                "capture_width": 859,
                "capture_height": 644,
                "device_pixel_ratio": 1.0,
            }
        )
        records.append(record)
    _write_log(log, records)

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is False
    assert evidence["capture_a"]["marker_checks"]["capture_geometry_matches_png"] is True
    assert (
        evidence["capture_a"]["marker_checks"][
            "capture_geometry_matches_logical_output_and_dpr"
        ]
        is False
    )


@pytest.mark.parametrize("invalid_dpr", [None, 0.0, -1.0, "NaN"])
def test_missing_nonpositive_or_nonfinite_dpr_is_rejected(
    tmp_path: Path,
    invalid_dpr: object,
) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    records = []
    for line in log.read_text(encoding="utf-8").splitlines():
        record = json.loads(line.split("MULTIDETECT_HIL_VIDEO_GRAB ", 1)[1])
        if invalid_dpr is None:
            record.pop("device_pixel_ratio")
        else:
            record["device_pixel_ratio"] = invalid_dpr
        records.append(record)
    _write_log(log, records)

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is False
    assert (
        evidence["capture_a"]["marker_checks"][
            "device_pixel_ratio_positive_finite"
        ]
        is False
    )
    assert evidence["checks"]["capture_a_marker_valid"] is False


def test_black_native_videooutput_pixels_are_rejected(tmp_path: Path) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    _write_structured(capture_b, black=True)

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is False
    assert "capture_b_native_video_pixels_present" in evidence["failure_reasons"]
    assert evidence["capture_b"]["surface_metrics"]["nonblack"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_name", "thermalVideo"),
        ("native_output_match_count", 2),
        ("saved", False),
        ("video_full_state", False),
        ("vtol_in_fwd_flight", False),
    ],
)
def test_marker_contract_is_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    records = []
    for line in log.read_text(encoding="utf-8").splitlines():
        records.append(json.loads(line.split("MULTIDETECT_HIL_VIDEO_GRAB ", 1)[1]))
    records[1][field] = value
    _write_log(log, records)

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is False
    assert evidence["checks"]["capture_b_marker_valid"] is False


def test_marker_paths_and_order_are_bound_to_current_qgc_run(tmp_path: Path) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    stale = start - timedelta(seconds=1)
    records = [
        _marker(
            role="pip-swapped",
            path=capture_b,
            sequence=2,
            recorded_at=stale,
            fixed_wing=False,
        ),
        _marker(
            role="fixed-wing",
            path=capture_a,
            sequence=1,
            recorded_at=stale,
            fixed_wing=True,
        ),
    ]
    _write_log(log, records)

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is False
    assert evidence["checks"]["capture_a_marker_valid"] is False
    assert evidence["checks"]["marker_sequence_and_time_ordered"] is False


def test_type24_sequence_advance_is_a_hard_gate(tmp_path: Path) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=14,
    )

    assert evidence["gate_passed"] is False
    assert "post_adoption_type24_sequence_advanced" in evidence["failure_reasons"]


def test_capture_files_must_be_newer_than_qgc_process(tmp_path: Path) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    old_timestamp = (start - timedelta(seconds=2)).timestamp()
    os.utime(capture_a, (old_timestamp, old_timestamp))

    evidence = build_evidence(
        capture_a,
        capture_b,
        log_paths=[log],
        qgc_started_at_utc=start.isoformat(),
        sequence_a=10,
        sequence_b=15,
    )

    assert evidence["gate_passed"] is False
    assert evidence["checks"]["capture_files_timestamp_bound_to_qgc_process"] is False


def test_cli_writes_atomic_evidence_and_refuses_overwrite(tmp_path: Path) -> None:
    capture_a, capture_b, log, start = _fixture(tmp_path)
    result = tmp_path / "result.json"
    argv = [
        "--capture-a",
        str(capture_a),
        "--capture-b",
        str(capture_b),
        "--qgc-log",
        str(log),
        "--qgc-started-at-utc",
        start.isoformat(),
        "--sequence-a",
        "10",
        "--sequence-b",
        "15",
        "--result-out",
        str(result),
    ]

    assert main(argv) == 0
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["schema"] == "multidetect.qgc-native-video-grab-proof"
    assert document["capture_a"]["sha256"]
    with pytest.raises(ValueError, match="refusing to overwrite"):
        main(argv)

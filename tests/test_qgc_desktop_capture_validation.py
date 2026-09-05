from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from scripts.validate_qgc_desktop_capture import (
    DEFAULT_VIDEO_REGIONS,
    build_evidence,
    main,
)


def _write_capture(
    path: Path,
    *,
    structured_video: bool,
    flat_video: bool = False,
    change_inside_video: bool = False,
    change_outside_video: bool = False,
    width: int = 900,
    height: int = 600,
) -> None:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    if structured_video or flat_video:
        for region_index, (x0, y0, x1, y1) in enumerate(DEFAULT_VIDEO_REGIONS):
            left = int(round(x0 * width))
            top = int(round(y0 * height))
            right = int(round(x1 * width))
            bottom = int(round(y1 * height))
            if flat_video:
                image[top:bottom, left:right] = 180
            else:
                gradient = np.linspace(
                    155 + region_index * 10,
                    220 - region_index * 5,
                    max(1, bottom - top),
                    dtype=np.uint8,
                )[:, None, None]
                image[top:bottom, left:right] = gradient
    if change_inside_video:
        x0, y0, x1, y1 = DEFAULT_VIDEO_REGIONS[0]
        left = int(round((x0 + (x1 - x0) * 0.25) * width))
        top = int(round((y0 + (y1 - y0) * 0.25) * height))
        right = int(round((x0 + (x1 - x0) * 0.75) * width))
        bottom = int(round((y0 + (y1 - y0) * 0.75) * height))
        image[top:bottom, left:right] = (30, 90, 240)
    if change_outside_video:
        # Simulates a changing QGC toolbar/runtime badge. It must not be
        # mistaken for a changing RTSP video frame.
        image[10:60, 20:260] = (20, 220, 40)
    assert cv2.imwrite(str(path), image)


def _evaluate(
    capture_a: Path,
    capture_b: Path,
    *,
    sequence_a: int = 10,
    sequence_b: int = 15,
    streaming: bool = True,
    decoding: bool = True,
) -> dict[str, object]:
    return build_evidence(
        capture_a,
        capture_b,
        sequence_a=sequence_a,
        sequence_b=sequence_b,
        rtsp_streaming_proved=streaming,
        rtsp_decoding_proved=decoding,
        rtsp_width=1280,
        rtsp_height=960,
    )


def test_static_structured_video_passes_when_native_sequence_advances(tmp_path: Path) -> None:
    capture_a = tmp_path / "a.png"
    capture_b = tmp_path / "b.png"
    _write_capture(capture_a, structured_video=True)
    _write_capture(capture_b, structured_video=True)

    evidence = _evaluate(capture_a, capture_b)

    assert evidence["gate_passed"] is True
    assert evidence["transport_bound_visible_video_surface_proved"] is True
    assert evidence["temporal_source_motion_proved"] is False
    assert evidence["temporal_diagnostic"]["required_for_gate"] is False
    assert evidence["runtime_binding"]["sequence_advance"] == 5
    assert evidence["exact_source_frame_identity_proved"] is False
    assert evidence["qml_rendering_proved"] is False
    assert evidence["operator_visual_acceptance"] == "not_claimed"


def test_black_gpu_surface_is_rejected_even_when_other_gates_pass(tmp_path: Path) -> None:
    capture_a = tmp_path / "black-a.png"
    capture_b = tmp_path / "black-b.png"
    _write_capture(capture_a, structured_video=False)
    _write_capture(capture_b, structured_video=False, change_outside_video=True)

    evidence = _evaluate(capture_a, capture_b)

    assert evidence["gate_passed"] is False
    assert evidence["capture_a"]["video_roi_metrics"]["nonblack"] is False
    assert evidence["capture_b"]["video_roi_metrics"]["nonblack"] is False
    assert "capture_a_video_roi_nonblack_nonuniform" in evidence["failure_reasons"]
    assert evidence["temporal_diagnostic"]["temporal_pixel_change_observed"] is False


def test_flat_nonblack_surface_is_rejected_as_nonuniform(tmp_path: Path) -> None:
    capture_a = tmp_path / "flat-a.png"
    capture_b = tmp_path / "flat-b.png"
    _write_capture(capture_a, structured_video=False, flat_video=True)
    _write_capture(capture_b, structured_video=False, flat_video=True)

    evidence = _evaluate(capture_a, capture_b)

    assert evidence["gate_passed"] is False
    assert evidence["capture_a"]["video_roi_metrics"]["nonblack"] is True
    assert evidence["capture_a"]["video_roi_metrics"]["nonuniform"] is False


@pytest.mark.parametrize(
    ("streaming", "decoding", "missing_check"),
    [
        (False, True, "rtsp_streaming_proved"),
        (True, False, "rtsp_decoding_proved"),
    ],
)
def test_rtsp_log_preconditions_are_hard_gates(
    tmp_path: Path,
    streaming: bool,
    decoding: bool,
    missing_check: str,
) -> None:
    capture_a = tmp_path / "a.png"
    capture_b = tmp_path / "b.png"
    _write_capture(capture_a, structured_video=True)
    _write_capture(capture_b, structured_video=True)

    evidence = _evaluate(capture_a, capture_b, streaming=streaming, decoding=decoding)

    assert evidence["gate_passed"] is False
    assert missing_check in evidence["failure_reasons"]


def test_type24_sequence_must_advance_by_five(tmp_path: Path) -> None:
    capture_a = tmp_path / "a.png"
    capture_b = tmp_path / "b.png"
    _write_capture(capture_a, structured_video=True)
    _write_capture(capture_b, structured_video=True)

    evidence = _evaluate(capture_a, capture_b, sequence_a=20, sequence_b=24)

    assert evidence["gate_passed"] is False
    assert evidence["runtime_binding"]["sequence_advanced"] is False
    assert "post_adoption_type24_sequence_advanced" in evidence["failure_reasons"]


def test_temporal_diagnostic_only_reads_video_roi(tmp_path: Path) -> None:
    capture_a = tmp_path / "a.png"
    outside_change = tmp_path / "outside-change.png"
    inside_change = tmp_path / "inside-change.png"
    _write_capture(capture_a, structured_video=True)
    _write_capture(outside_change, structured_video=True, change_outside_video=True)
    _write_capture(inside_change, structured_video=True, change_inside_video=True)

    outside = _evaluate(capture_a, outside_change)
    inside = _evaluate(capture_a, inside_change)

    assert outside["temporal_diagnostic"]["temporal_pixel_change_observed"] is False
    assert inside["temporal_diagnostic"]["temporal_pixel_change_observed"] is True
    assert inside["gate_passed"] is True


def test_geometry_mismatch_is_a_hard_gate(tmp_path: Path) -> None:
    capture_a = tmp_path / "a.png"
    capture_b = tmp_path / "b.png"
    _write_capture(capture_a, structured_video=True, width=900, height=600)
    _write_capture(capture_b, structured_video=True, width=1000, height=600)

    evidence = _evaluate(capture_a, capture_b)

    assert evidence["gate_passed"] is False
    assert evidence["checks"]["same_exact_client_geometry"] is False
    assert evidence["temporal_diagnostic"]["unavailable_reason"] == "capture geometry differs"


def test_cli_writes_atomic_identity_bound_json_and_refuses_overwrite(tmp_path: Path) -> None:
    capture_a = tmp_path / "a.png"
    capture_b = tmp_path / "b.png"
    result = tmp_path / "result.json"
    _write_capture(capture_a, structured_video=True)
    _write_capture(capture_b, structured_video=True)
    argv = [
        "--capture-a",
        str(capture_a),
        "--capture-b",
        str(capture_b),
        "--sequence-a",
        "7",
        "--sequence-b",
        "12",
        "--rtsp-streaming-proved",
        "--rtsp-decoding-proved",
        "--result-out",
        str(result),
    ]

    assert main(argv) == 0
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["schema"] == "multidetect.qgc-desktop-live-video-proof"
    assert document["capture_a"]["sha256"]
    assert document["capture_b"]["bytes"] == capture_b.stat().st_size
    with pytest.raises(ValueError, match="refusing to overwrite"):
        main(argv)

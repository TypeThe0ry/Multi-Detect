from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from scripts.validate_qgc_toolbar_layout import build_evidence, main


def _write_captures(
    closed: Path,
    opened: Path,
    *,
    width: int = 1000,
    height: int = 800,
    draw_drawer: bool = True,
) -> None:
    base = np.zeros((height, width, 3), dtype=np.uint8)
    base[:] = (30, 35, 40)
    base[:100, :] = (45, 50, 55)
    opened_image = base.copy()
    if draw_drawer:
        opened_image[120:500, 650:950] = (155, 165, 175)
        opened_image[150:470:20, 680:920] = (60, 70, 80)
    assert cv2.imwrite(str(closed), base)
    assert cv2.imwrite(str(opened), opened_image)


def _observation(*, drawer_bottom: int = 500) -> dict[str, object]:
    return {
        "qgc_pid": 1234,
        "qgc_main_hwnd": "0x0000000000012345",
        "client_screen_rect": {
            "left": 100,
            "top": 200,
            "right": 1100,
            "bottom": 1000,
        },
        "indicator": {
            "accessible_name": "MultiDetect AI Status",
            "control_type": "ControlType.Button",
            "bounding_rect": {
                "left": 700,
                "top": 220,
                "right": 880,
                "bottom": 270,
            },
        },
        "drawer": {
            "accessible_name": "MultiDetect AI Status Drawer",
            "control_type": "ControlType.Pane",
            "bounding_rect": {
                "left": 750,
                "top": 320,
                "right": 1050,
                "bottom": 200 + drawer_bottom,
            },
        },
        "click": {
            "screen_x": 790,
            "screen_y": 245,
            "window_from_point_hwnd": "0x0000000000012345",
            "owner_pid": 1234,
            "owner_matches_qgc": True,
        },
        "visible_top_level_hwnds_closed": ["0x0000000000012345"],
        "visible_top_level_hwnds_open": ["0x0000000000012345"],
        "visible_top_level_hwnds_after_close": ["0x0000000000012345"],
        "drawer_closed_after_escape": True,
        "automation_error": None,
    }


def _qml_geometry_observation(*, drawer_bottom: int = 500) -> dict[str, object]:
    observation = _observation(drawer_bottom=drawer_bottom)
    indicator = observation["indicator"]
    drawer = observation["drawer"]
    assert isinstance(indicator, dict)
    assert isinstance(drawer, dict)
    indicator.update(
        {
            "control_type": "Qml.Accessible.Button",
            "geometry_source": "qgc_qml_scene_geometry_export",
            "geometry_schema": "multidetect.qgc-hil-ui-geometry",
            "role": "indicator_ready",
            "qgc_pid_matches": True,
            "qgc_hwnd_matches": True,
        }
    )
    drawer.update(
        {
            "control_type": "Qml.Accessible.Pane",
            "geometry_source": "qgc_qml_scene_geometry_export",
            "geometry_schema": "multidetect.qgc-hil-ui-geometry",
            "role": "drawer_open",
            "qgc_pid_matches": True,
            "qgc_hwnd_matches": True,
        }
    )
    observation["automation_error"] = "Qt UIA provider unavailable; QML export used"
    observation["geometry_error"] = "UIA provider unavailable diagnostic"
    return observation


def test_native_toolbar_indicator_and_drawer_geometry_passes(tmp_path: Path) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    _write_captures(closed, opened)

    evidence = build_evidence(_observation(), closed, opened)

    assert evidence["gate_passed"] is True
    assert evidence["machine_visual_toolbar_smoke"] == "passed"
    assert evidence["geometry"]["reserved_intersection_area_pixels"] == 0
    assert evidence["pixel_change"]["changed_pixel_ratio"] >= 0.01
    assert evidence["complete_qml_rendering_semantics_proved"] is False
    assert evidence["operator_visual_acceptance"] == "not_claimed"


def test_strict_qgc_qml_geometry_export_passes_despite_uia_diagnostic(
    tmp_path: Path,
) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    _write_captures(closed, opened)

    evidence = build_evidence(_qml_geometry_observation(), closed, opened)

    assert evidence["gate_passed"] is True
    assert evidence["geometry_evidence_path"] == "qgc_qml_scene_geometry_export"
    assert evidence["automation_error"]
    assert evidence["geometry_error"]
    assert evidence["checks"]["geometry_source_is_qgc_qml_scene_export"] is True
    assert evidence["checks"]["geometry_schema_is_qgc_hil_ui_geometry"] is True
    assert (
        evidence["checks"]["indicator_geometry_bound_to_exact_qgc_pid_hwnd"]
        is True
    )
    assert evidence["checks"]["drawer_geometry_bound_to_exact_qgc_pid_hwnd"] is True
    assert evidence["complete_qml_rendering_semantics_proved"] is False
    assert evidence["operator_visual_acceptance"] == "not_claimed"


@pytest.mark.parametrize(
    ("element", "field", "value", "expected_failure"),
    [
        (
            "indicator",
            "geometry_schema",
            "wrong.schema",
            "geometry_schema_is_qgc_hil_ui_geometry",
        ),
        (
            "indicator",
            "role",
            "drawer_open",
            "indicator_geometry_role_is_ready",
        ),
        (
            "drawer",
            "qgc_pid_matches",
            False,
            "drawer_geometry_bound_to_exact_qgc_pid_hwnd",
        ),
        (
            "drawer",
            "qgc_hwnd_matches",
            False,
            "drawer_geometry_bound_to_exact_qgc_pid_hwnd",
        ),
    ],
)
def test_qgc_qml_geometry_export_fails_closed_on_provenance_mismatch(
    tmp_path: Path,
    element: str,
    field: str,
    value: object,
    expected_failure: str,
) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    _write_captures(closed, opened)
    observation = _qml_geometry_observation()
    target = observation[element]
    assert isinstance(target, dict)
    target[field] = value

    evidence = build_evidence(observation, closed, opened)

    assert evidence["gate_passed"] is False
    assert expected_failure in evidence["failure_reasons"]


def test_drawer_inflation_rejects_native_bottom_right_overlap(tmp_path: Path) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    _write_captures(closed, opened)
    observation = _observation(drawer_bottom=800)

    evidence = build_evidence(observation, closed, opened)

    assert evidence["gate_passed"] is False
    assert evidence["geometry"]["reserved_intersection_area_pixels"] > 0
    assert "drawer_reserved_region_intersection_zero" in evidence["failure_reasons"]


def test_unchanged_drawer_crop_is_rejected(tmp_path: Path) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    _write_captures(closed, opened, draw_drawer=False)

    evidence = build_evidence(_observation(), closed, opened)

    assert evidence["gate_passed"] is False
    assert evidence["pixel_change"]["changed_pixel_ratio"] == 0.0
    assert "drawer_open_closed_visual_change" in evidence["failure_reasons"]


def test_second_qgc_top_level_surface_is_rejected(tmp_path: Path) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    _write_captures(closed, opened)
    observation = _observation()
    observation["visible_top_level_hwnds_open"] = [
        "0x0000000000012345",
        "0x0000000000067890",
    ]

    evidence = build_evidence(observation, closed, opened)

    assert evidence["gate_passed"] is False
    assert "single_visible_qgc_top_level_surface" in evidence["failure_reasons"]


def test_cli_writes_identity_bound_result_and_refuses_overwrite(tmp_path: Path) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    observation_path = tmp_path / "observation.json"
    result = tmp_path / "result.json"
    _write_captures(closed, opened)
    observation_path.write_text(json.dumps(_observation()), encoding="utf-8")
    argv = [
        "--observation",
        str(observation_path),
        "--closed-capture",
        str(closed),
        "--open-capture",
        str(opened),
        "--result-out",
        str(result),
    ]

    assert main(argv) == 0
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["schema"] == "multidetect.qgc-native-toolbar-indicator-proof"
    assert document["closed_capture"]["sha256"]
    with pytest.raises(FileExistsError):
        main(argv)


def test_cli_writes_fail_closed_result_when_uia_provider_exposes_no_elements(
    tmp_path: Path,
) -> None:
    closed = tmp_path / "closed.png"
    opened = tmp_path / "open.png"
    observation_path = tmp_path / "missing-uia.json"
    result = tmp_path / "failed-result.json"
    _write_captures(closed, opened, draw_drawer=False)
    observation = _observation()
    observation["indicator"] = None
    observation["drawer"] = None
    observation["automation_error"] = "Qt accessibility provider returned no element"
    observation_path.write_text(json.dumps(observation), encoding="utf-8")

    exit_code = main(
        [
            "--observation",
            str(observation_path),
            "--closed-capture",
            str(closed),
            "--open-capture",
            str(opened),
            "--result-out",
            str(result),
        ]
    )

    document = json.loads(result.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert document["gate_passed"] is False
    assert document["machine_visual_toolbar_smoke"] == "failed"
    assert document["automation_error"]
    assert document["complete_qml_rendering_semantics_proved"] is False
    assert document["operator_visual_acceptance"] == "not_claimed"

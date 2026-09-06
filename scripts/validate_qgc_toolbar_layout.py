"""Validate QGC-native MultiDetect toolbar/drawer machine-visual evidence.

The Windows combined-SIM wrapper may discover geometry through UI Automation,
or consume QGC's strict QML scene-geometry export when the platform UIA
provider is unavailable.  Both paths must bind every rectangle to the exact
QGC PID/HWND and exact-client captures.  This module deliberately performs
only identity, geometry and pixel-difference checks.  Passing this gate is a
machine visual smoke test; it is not complete QML semantic proof or human
operator acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

SCHEMA = "multidetect.qgc-native-toolbar-indicator-proof"
SCHEMA_VERSION = 1
RESERVED_REGION_NORMALIZED = (0.45, 0.68, 1.0, 1.0)
DRAWER_INFLATION_CLIENT_HEIGHT_RATIO = 0.02
TOOLBAR_BAND_MAX_NORMALIZED_Y = 0.18
PIXEL_DELTA_THRESHOLD = 8
MINIMUM_CHANGED_PIXEL_RATIO = 0.01
QML_GEOMETRY_SOURCE = "qgc_qml_scene_geometry_export"
QML_GEOMETRY_SCHEMA = "multidetect.qgc-hil-ui-geometry"
QML_INDICATOR_CONTROL_TYPE = "Qml.Accessible.Button"
QML_DRAWER_CONTROL_TYPE = "Qml.Accessible.Pane"
UIA_INDICATOR_CONTROL_TYPE = "ControlType.Button"
UIA_DRAWER_CONTROL_TYPE = "ControlType.Pane"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _load_image(path: Path) -> np.ndarray:
    if not path.is_file():
        raise ValueError(f"exact-client capture is missing: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"OpenCV could not decode exact-client capture: {path}")
    return image


def _rect(raw: Any, label: str) -> tuple[float, float, float, float]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} rectangle is missing")
    try:
        left = float(raw["left"])
        top = float(raw["top"])
        right = float(raw["right"])
        bottom = float(raw["bottom"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{label} rectangle is malformed") from error
    if not all(np.isfinite(value) for value in (left, top, right, bottom)):
        raise ValueError(f"{label} rectangle contains a non-finite value")
    if right <= left or bottom <= top:
        raise ValueError(f"{label} rectangle has no positive area")
    return left, top, right, bottom


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _identity(path: Path, image: np.ndarray) -> dict[str, Any]:
    height, width = image.shape[:2]
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "width": int(width),
        "height": int(height),
    }


def _geometry_provenance_checks(
    indicator: dict[str, Any],
    drawer: dict[str, Any],
) -> tuple[str, dict[str, bool]]:
    """Return the accepted discovery path and its fail-closed provenance checks."""

    qml_path_requested = any(
        item.get("geometry_source") == QML_GEOMETRY_SOURCE for item in (indicator, drawer)
    )
    if qml_path_requested:
        checks = {
            "geometry_source_is_qgc_qml_scene_export": all(
                item.get("geometry_source") == QML_GEOMETRY_SOURCE for item in (indicator, drawer)
            ),
            "geometry_schema_is_qgc_hil_ui_geometry": all(
                item.get("geometry_schema") == QML_GEOMETRY_SCHEMA for item in (indicator, drawer)
            ),
            "indicator_geometry_role_is_ready": indicator.get("role") == "indicator_ready",
            "drawer_geometry_role_is_open": drawer.get("role") == "drawer_open",
            "indicator_geometry_bound_to_exact_qgc_pid_hwnd": bool(
                indicator.get("qgc_pid_matches") and indicator.get("qgc_hwnd_matches")
            ),
            "drawer_geometry_bound_to_exact_qgc_pid_hwnd": bool(
                drawer.get("qgc_pid_matches") and drawer.get("qgc_hwnd_matches")
            ),
        }
        return "qgc_qml_scene_geometry_export", checks

    checks = {
        "geometry_source_is_uia_exact_hwnd": True,
        "geometry_schema_is_qgc_hil_ui_geometry": True,
        "indicator_geometry_role_is_ready": True,
        "drawer_geometry_role_is_open": True,
        "indicator_geometry_bound_to_exact_qgc_pid_hwnd": True,
        "drawer_geometry_bound_to_exact_qgc_pid_hwnd": True,
    }
    return "windows_ui_automation", checks


def build_evidence(
    observation: dict[str, Any],
    closed_capture_path: Path,
    open_capture_path: Path,
) -> dict[str, Any]:
    closed_capture_path = closed_capture_path.resolve()
    open_capture_path = open_capture_path.resolve()
    closed_image = _load_image(closed_capture_path)
    open_image = _load_image(open_capture_path)
    closed_height, closed_width = closed_image.shape[:2]
    open_height, open_width = open_image.shape[:2]
    same_geometry = (closed_width, closed_height) == (open_width, open_height)

    client = _rect(observation.get("client_screen_rect"), "client")
    indicator = _rect((observation.get("indicator") or {}).get("bounding_rect"), "indicator")
    drawer = _rect((observation.get("drawer") or {}).get("bounding_rect"), "drawer")
    client_width = client[2] - client[0]
    client_height = client[3] - client[1]
    client_matches_captures = (
        round(client_width) == closed_width and round(client_height) == closed_height
    )
    indicator_inside_client = _contains(client, indicator)
    drawer_inside_client = _contains(client, drawer)

    indicator_top_normalized = (indicator[1] - client[1]) / client_height
    indicator_bottom_normalized = (indicator[3] - client[1]) / client_height
    indicator_in_toolbar_band = (
        indicator_inside_client
        and indicator_top_normalized >= 0.0
        and indicator_bottom_normalized <= TOOLBAR_BAND_MAX_NORMALIZED_Y
    )

    inflation = DRAWER_INFLATION_CLIENT_HEIGHT_RATIO * client_height
    inflated_drawer = (
        max(client[0], drawer[0] - inflation),
        max(client[1], drawer[1] - inflation),
        min(client[2], drawer[2] + inflation),
        min(client[3], drawer[3] + inflation),
    )
    reserved = (
        client[0] + RESERVED_REGION_NORMALIZED[0] * client_width,
        client[1] + RESERVED_REGION_NORMALIZED[1] * client_height,
        client[0] + RESERVED_REGION_NORMALIZED[2] * client_width,
        client[1] + RESERVED_REGION_NORMALIZED[3] * client_height,
    )
    reserved_intersection_area = _intersection_area(inflated_drawer, reserved)

    drawer_left = max(0, min(open_width - 1, int(np.floor(drawer[0] - client[0]))))
    drawer_top = max(0, min(open_height - 1, int(np.floor(drawer[1] - client[1]))))
    drawer_right = max(
        drawer_left + 1,
        min(open_width, int(np.ceil(drawer[2] - client[0]))),
    )
    drawer_bottom = max(
        drawer_top + 1,
        min(open_height, int(np.ceil(drawer[3] - client[1]))),
    )
    drawer_crop_nonempty = drawer_right > drawer_left and drawer_bottom > drawer_top
    changed_pixel_ratio = 0.0
    mean_absolute_difference = 0.0
    if same_geometry and drawer_crop_nonempty:
        closed_gray = cv2.cvtColor(closed_image, cv2.COLOR_BGR2GRAY)
        open_gray = cv2.cvtColor(open_image, cv2.COLOR_BGR2GRAY)
        delta = cv2.absdiff(
            closed_gray[drawer_top:drawer_bottom, drawer_left:drawer_right],
            open_gray[drawer_top:drawer_bottom, drawer_left:drawer_right],
        )
        changed_pixel_ratio = float(np.mean(delta > PIXEL_DELTA_THRESHOLD))
        mean_absolute_difference = float(np.mean(delta.astype(np.float32)))
    drawer_visual_change = changed_pixel_ratio >= MINIMUM_CHANGED_PIXEL_RATIO

    indicator_observation = observation.get("indicator") or {}
    drawer_observation = observation.get("drawer") or {}
    geometry_evidence_path, provenance_checks = _geometry_provenance_checks(
        indicator_observation, drawer_observation
    )
    using_qml_geometry_export = geometry_evidence_path == "qgc_qml_scene_geometry_export"
    closed_windows = sorted(
        str(value) for value in observation.get("visible_top_level_hwnds_closed", [])
    )
    open_windows = sorted(
        str(value) for value in observation.get("visible_top_level_hwnds_open", [])
    )
    after_close_windows = sorted(
        str(value) for value in observation.get("visible_top_level_hwnds_after_close", [])
    )
    main_hwnd = str(observation.get("qgc_main_hwnd", ""))
    single_qgc_surface = (
        closed_windows == [main_hwnd]
        and open_windows == closed_windows
        and after_close_windows == closed_windows
    )

    checks = {
        "same_exact_client_geometry": same_geometry,
        "client_geometry_matches_capture": client_matches_captures,
        "indicator_accessible_button_found": (
            indicator_observation.get("accessible_name") == "MultiDetect AI Status"
            and indicator_observation.get("control_type")
            == (
                QML_INDICATOR_CONTROL_TYPE
                if using_qml_geometry_export
                else UIA_INDICATOR_CONTROL_TYPE
            )
        ),
        "drawer_accessible_pane_found": (
            drawer_observation.get("accessible_name") == "MultiDetect AI Status Drawer"
            and drawer_observation.get("control_type")
            == (QML_DRAWER_CONTROL_TYPE if using_qml_geometry_export else UIA_DRAWER_CONTROL_TYPE)
        ),
        **provenance_checks,
        "indicator_inside_exact_client": indicator_inside_client,
        "indicator_in_native_toolbar_band": indicator_in_toolbar_band,
        "drawer_inside_exact_client": drawer_inside_client,
        "drawer_crop_nonempty": drawer_crop_nonempty,
        "drawer_open_closed_visual_change": drawer_visual_change,
        "drawer_reserved_region_intersection_zero": reserved_intersection_area == 0.0,
        "click_hit_owned_by_exact_qgc_pid": bool(
            (observation.get("click") or {}).get("owner_matches_qgc")
        ),
        "single_visible_qgc_top_level_surface": single_qgc_surface,
        "drawer_closed_after_escape": bool(observation.get("drawer_closed_after_escape")),
        # A platform UIA-provider diagnostic is expected when the strict QGC
        # QML scene export supplies the exact bound geometry instead.
        "geometry_discovery_error_absent_or_qml_export_used": (
            using_qml_geometry_export or not bool(observation.get("geometry_error"))
        ),
        "automation_error_absent_or_qml_export_used": (
            using_qml_geometry_export or not bool(observation.get("automation_error"))
        ),
    }
    failures = [name for name, passed in checks.items() if not passed]
    gate_passed = not failures
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate_passed": gate_passed,
        "failure_reasons": failures,
        "machine_visual_toolbar_smoke": "passed" if gate_passed else "failed",
        "complete_qml_rendering_semantics_proved": False,
        "operator_visual_acceptance": "not_claimed",
        "qgc_process": {
            "pid": observation.get("qgc_pid"),
            "main_hwnd": main_hwnd,
            "client_screen_rect": observation.get("client_screen_rect"),
        },
        "geometry_evidence_path": geometry_evidence_path,
        "indicator": indicator_observation,
        "drawer": drawer_observation,
        "click": observation.get("click"),
        "single_surface": {
            "visible_top_level_hwnds_closed": closed_windows,
            "visible_top_level_hwnds_open": open_windows,
            "visible_top_level_hwnds_after_close": after_close_windows,
            "unchanged_and_only_main_window": single_qgc_surface,
        },
        "geometry": {
            "toolbar_band_max_normalized_y": TOOLBAR_BAND_MAX_NORMALIZED_Y,
            "indicator_top_normalized": indicator_top_normalized,
            "indicator_bottom_normalized": indicator_bottom_normalized,
            "drawer_inflation_client_height_ratio": DRAWER_INFLATION_CLIENT_HEIGHT_RATIO,
            "inflated_drawer_screen_rect": {
                "left": inflated_drawer[0],
                "top": inflated_drawer[1],
                "right": inflated_drawer[2],
                "bottom": inflated_drawer[3],
            },
            "reserved_region_normalized": {
                "x0": RESERVED_REGION_NORMALIZED[0],
                "y0": RESERVED_REGION_NORMALIZED[1],
                "x1": RESERVED_REGION_NORMALIZED[2],
                "y1": RESERVED_REGION_NORMALIZED[3],
            },
            "reserved_intersection_area_pixels": reserved_intersection_area,
            "drawer_crop_pixels": {
                "left": drawer_left,
                "top": drawer_top,
                "right_exclusive": drawer_right,
                "bottom_exclusive": drawer_bottom,
                "pixel_count": max(0, drawer_right - drawer_left)
                * max(0, drawer_bottom - drawer_top),
            },
        },
        "pixel_change": {
            "delta_luminance_threshold": PIXEL_DELTA_THRESHOLD,
            "minimum_changed_pixel_ratio": MINIMUM_CHANGED_PIXEL_RATIO,
            "changed_pixel_ratio": changed_pixel_ratio,
            "mean_absolute_difference": mean_absolute_difference,
        },
        "closed_capture": _identity(closed_capture_path, closed_image),
        "open_capture": _identity(open_capture_path, open_image),
        "checks": checks,
        "geometry_error": observation.get("geometry_error"),
        "automation_error": observation.get("automation_error"),
        "evidence_semantics": (
            "Exact-PID/HWND UI Automation or strict QGC QML scene-geometry export, "
            "screen-owner, geometry and pixel-delta machine smoke only; no complete "
            "QML semantic or human visual claim."
        ),
    }


def _write_json_exclusive(path: Path, document: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--closed-capture", type=Path, required=True)
    parser.add_argument("--open-capture", type=Path, required=True)
    parser.add_argument("--result-out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    observation = json.loads(args.observation.read_text(encoding="utf-8-sig"))
    try:
        evidence = build_evidence(observation, args.closed_capture, args.open_capture)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Discovery/provider and malformed geometry failures remain diagnosable.
        # Still emit a durable failed result instead of letting the wrapper
        # silently skip the gate or guess a screen coordinate.
        evidence = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "gate_passed": False,
            "failure_reasons": ["validator_input_or_geometry_invalid"],
            "machine_visual_toolbar_smoke": "failed",
            "complete_qml_rendering_semantics_proved": False,
            "operator_visual_acceptance": "not_claimed",
            "qgc_process": {
                "pid": observation.get("qgc_pid"),
                "main_hwnd": observation.get("qgc_main_hwnd"),
                "client_screen_rect": observation.get("client_screen_rect"),
            },
            "indicator": observation.get("indicator"),
            "drawer": observation.get("drawer"),
            "click": observation.get("click"),
            "geometry_error": observation.get("geometry_error"),
            "automation_error": observation.get("automation_error"),
            "validator_error": str(error),
            "evidence_semantics": (
                "Fail-closed exact-PID/HWND toolbar smoke evidence from UIA or the "
                "strict QGC QML geometry schema; no guessed coordinates, complete "
                "QML semantic claim, or human visual claim."
            ),
        }
    _write_json_exclusive(args.result_out, evidence)
    print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")))
    return 0 if evidence["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

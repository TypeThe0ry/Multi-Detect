from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_video_pinch_zoom_uses_directional_pointer_handler() -> None:
    qml = (REPO_ROOT / "src/FlyView/FlightDisplayViewVideo.qml").read_text(encoding="utf-8")

    assert "PinchArea {" not in qml
    assert "PinchHandler {" in qml
    assert "target:  null" in qml
    assert "zoomAccumulator *= delta" in qml
    assert "_camera.stepZoom(1)" in qml
    assert "_camera.stepZoom(-1)" in qml


def test_qml_warning_is_not_suppressed_by_the_unit_test_framework() -> None:
    unit_test = (REPO_ROOT / "test/UnitTestFramework/UnitTest.cc").read_text(encoding="utf-8")

    assert "QQuickPinchArea overrides a member" not in unit_test

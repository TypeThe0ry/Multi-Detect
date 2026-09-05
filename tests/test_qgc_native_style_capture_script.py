from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_qgc_native_style.ps1"


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_native_style_capture_script_is_present_and_exact_client_bound() -> None:
    source = _read()
    for fragment in (
        "--offline",
        "--allow-multiple",
        "GetClientRect",
        "ClientToScreen",
        "VisibleWindows",
        "external_occluders_after",
        "single_native_surface_machine_check",
        "ComputerUseOperatorAuthorized",
        "HideStubbornOccluders",
        "hidden_occluder_count",
        "SW_HIDE",
        "IsWindowVisible($remaining.Handle)",
        "computer_use_operator_authorized = [bool]$ComputerUseOperatorAuthorized",
        "operator_visual_acceptance = 'not_claimed'",
        "qml_rendering_proved = $false",
        "desktop_capture_gpu_surface_proved = $false",
    ):
        assert fragment in source


def test_native_style_capture_restores_every_window_state_and_owns_process_exit() -> None:
    source = _read()
    assert "finally {" in source
    assert "SetWindowPlacement($saved.Handle" in source
    assert "ShowWindowAsync($saved.Handle, $showCommand)" in source
    assert "SetWindowPlacement($qgcHandle" in source
    assert "CloseMainWindow()" in source
    assert "process.Kill()" in source
    assert "Remove-Item" not in source
    assert "Stop-Process -Name" not in source

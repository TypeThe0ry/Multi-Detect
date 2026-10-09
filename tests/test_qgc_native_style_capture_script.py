from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_qgc_native_style.ps1"
SCHEMA = ROOT / "configs" / "schemas" / "qgc-native-style-capture.schema.json"


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_native_style_capture_script_is_present_and_exact_client_bound() -> None:
    assert SCHEMA.is_file()
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
        "ExistingProcessId",
        "[Parameter(Mandatory = $true)]",
        "[string]$RuntimeMode",
        "ReleaseParityPath",
        "RunId",
        "schema_version = 2",
        "event = 'qgc_native_style_capture'",
        "executable_identity",
        "release_parity",
        "capture_gate_passed",
        "CaptureWidth = 1600",
        "CaptureHeight = 900",
        "[ValidateRange(640, 7680)]",
        "[ValidateRange(480, 4320)]",
        "$requestedWindowWidth = [Math]::Max",
        "($captureRect.Right - $captureRect.Left) -gt 0",
        "($captureRect.Bottom - $captureRect.Top) -gt 0",
        "attached_existing_process = [bool]$attachedProcess",
        "ProcessName -cne 'MultiDetectGCS'",
        "hidden_occluder_count",
        "SW_HIDE",
        "ShowWindowAsync($remaining.Handle, 0)",
        "computer_use_operator_authorized = [bool]$ComputerUseOperatorAuthorized",
        "operator_visual_acceptance = 'not_claimed'",
        "qml_rendering_proved = $false",
        "desktop_capture_gpu_surface_proved = $false",
    ):
        assert fragment in source


def test_native_style_capture_restores_every_window_state_and_owns_process_exit() -> None:
    source = _read()
    assert "finally {" in source
    assert "$monitorPlacement = $null" in source
    assert "SetWindowPlacement($saved.Handle" in source
    assert "ShowWindowAsync($saved.Handle, $showCommand)" in source
    assert "SetWindowPlacement($qgcHandle" in source
    assert "CloseMainWindow()" in source
    assert "process.Kill()" in source
    assert "-not $attachedProcess -and -not $KeepProcess" in source
    assert "Remove-Item" not in source
    assert "Stop-Process -Name" not in source


def test_current_qgc_machine_capture_entry_is_hash_bound_and_not_human_acceptance() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    for entry_name in (
        "qgc_native_style_machine_capture_20260927_r4",
        "qgc_native_style_portable_current_machine_capture_20260927_r1",
    ):
        entry = progress[entry_name]
        assert entry["capture_gate_passed"] is True
        assert entry["single_native_surface_machine_check"] is True
        assert entry["operator_visual_acceptance"] == "not_claimed"
        assert entry["qml_rendering_proved"] is False
        assert entry["desktop_capture_gpu_surface_proved"] is False
        for path_key, bytes_key, sha_key in (
            ("capture", "capture_bytes", "capture_sha256"),
            ("machine_observation", "machine_observation_bytes", "machine_observation_sha256"),
        ):
            path = ROOT / entry[path_key]
            payload = path.read_bytes()
            assert len(payload) == entry[bytes_key]
            assert hashlib.sha256(payload).hexdigest() == entry[sha_key]

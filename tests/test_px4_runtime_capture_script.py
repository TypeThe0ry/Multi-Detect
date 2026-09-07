from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_px4_runtime_capture.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell parser is unavailable")
def test_windows_px4_runtime_capture_wrapper_parses_as_powershell() -> None:
    escaped_path = str(SCRIPT).replace("'", "''")
    command = (
        "$tokens = $null; $parseErrors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped_path}', "
        "[ref]$tokens, [ref]$parseErrors) | Out-Null; "
        "if ($parseErrors.Count -ne 0) { "
        "$parseErrors | ForEach-Object { [Console]::Error.WriteLine($_.ToString()) }; exit 1 }; "
        "exit 0"
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_wrapper_forwards_the_complete_bounded_capture_contract() -> None:
    text = _read()
    for token in (
        "[string]$BaseDirectory",
        "[string]$Mission",
        "[string]$RuntimeObservation",
        "[string]$RuntimeProfile",
        "[ValidateSet('hil', 'real')]",
        "[string]$Endpoint",
        "[int]$Baud",
        "[int]$ExpectedSystemId",
        "[int]$ExpectedComponentId",
        "[double]$ProviderStaleAfterSeconds",
        "[double]$DurationSeconds",
        "[double]$PollIntervalSeconds",
        "$NoRequestAutopilotVersion",
        "$RequestRangingStreams",
        "$RuntimeProfileHmacKeyEnv",
        "$RuntimeProfileKeyId",
        "$RuntimeSigningIdentityHash",
        "$RuntimeSigningKeyHexEnv",
        "$MissionFeedbackOut",
        "$EndpointVehicleIdentityOut",
        "$JoinOut",
        "$PythonPath",
        "$Overwrite",
        "multidetect.px4_runtime_capture",
        "--base-directory",
        "--runtime-observation",
        "--runtime-profile",
        "--runtime-mode",
        "--endpoint",
        "--provider-stale-after-seconds",
        "--duration-seconds",
        "--poll-interval-seconds",
        "--request-autopilot-version",
        "--no-request-autopilot-version",
        "--request-ranging-streams",
        "--runtime-profile-hmac-key-env",
        "--runtime-profile-key-id",
        "--runtime-signing-identity-hash",
        "--runtime-signing-key-hex-env",
        "--mission-feedback-out",
        "--endpoint-vehicle-identity-out",
        "--join-out",
        "--overwrite",
        "PYTHONPATH",
        "Get-Variable -Name LASTEXITCODE",
        "Get-NativeExitCode",
        "PSScriptRoot",
    ):
        assert token in text


def test_wrapper_keeps_secret_material_out_of_native_arguments_and_restores_environment() -> None:
    text = _read()
    assert "The HMAC/signing material is intentionally never placed" in text
    assert "[Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')" in text
    assert "Remove-Item -LiteralPath 'Env:PYTHONPATH'" in text
    assert "$env:PYTHONPATH = $previousPythonPath" in text
    # Only the names of environment variables are forwarded.  The wrapper
    # must not read a secret value or append it to the child argument list.
    assert "[Environment]::GetEnvironmentVariable($RuntimeProfileHmacKeyEnv" not in text
    assert "[Environment]::GetEnvironmentVariable($RuntimeSigningKeyHexEnv" not in text


def test_wrapper_fails_closed_for_mode_signing_identity_mismatch() -> None:
    text = _read()
    assert "REAL capture requires RuntimeSigningIdentityHash or RuntimeSigningKeyHexEnv" in text
    assert "HIL capture cannot carry a REAL signing identity" in text
    assert "RuntimeSigningIdentityHash and RuntimeSigningKeyHexEnv are mutually exclusive" in text

    # The helper is a transport/sidecar producer, not a network reconfiguration
    # script.  Address and route mutation must remain outside its scope.
    for forbidden in (
        "New-NetIPAddress",
        "Remove-NetIPAddress",
        "Set-NetIPAddress",
        "New-NetRoute",
        "Remove-NetRoute",
    ):
        assert forbidden not in text
